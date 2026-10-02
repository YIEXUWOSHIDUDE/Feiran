"""What each V2 task does. The existing rules (requirement_flow, cv, cv_plan, gaps, cv_import)
run on one user's snapshot outside any database transaction; results reach the store only
through its publish methods, which accept them only while the task still holds its execution
token and its inputs have not been replaced. Nothing here decides who the user is, confirms a
fact on a model's word or approves a CV.

A handler takes a task_runner.TaskContext: ``ctx.user`` (that user's UserWorkspace),
``ctx.request`` (what the task was asked), ``ctx.chat`` (the model, metered per task) and the
succeed/fail/stage calls that end or annotate the task.
"""

import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from cv import CVError, build_draft, export_pdf, print_with_chrome, tailor_draft
from cv_import import CVImportError, structure_cv
from cv_plan import plan_draft
from cv_status import STAGE_FAILED, STAGES, adjusted, job_language, reason, reworded, stage_entry, stage_failure
from gaps import find_gaps
from job_search import SearchError, fetch_selected, prepare_review_input
from job_url import fetch_job_url
from requirement_flow import RequirementError, apply_requirement_decisions, propose_requirements


# Model calls each operation may make at most (the quota it reserves); a PDF counts as one.
UNITS = {"upload_cv": 1, "prepare_job": 3, "job_from_url": 3, "start_listing": 3, "prepare_cv": 2,
         "tailor_cv": 1, "plan_cv": 1, "check_gaps": 2, "export_pdf": 1}
# The data flow each operation needs the user's agreement to (tenant_store.CONSENT_STAGES).
CONSENT = {"upload_cv": "upload_parsing", "prepare_job": "job_processing", "job_from_url": "job_processing",
           "start_listing": "job_processing", "prepare_cv": "job_processing", "tailor_cv": "job_processing",
           "plan_cv": "job_processing", "check_gaps": "job_processing"}
HEAVY = {"export_pdf": "pdf"}


class TaskFailed(Exception):
    """The task cannot do what it was asked; ``code`` and the message are shown to the user."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def auto_decide(candidates: dict, kept: dict[str, str] | None = None) -> dict | None:
    """Keep the user's own decisions (requirement ID -> status) and count every other found
    requirement, recorded as decided automatically, never as reviewed. None when nothing counts."""
    ids = {item["id"] for item in candidates["requirement_candidates"]}
    kept = {key: status for key, status in (kept or {}).items() if key in ids}
    confirmed = {key for key, status in kept.items() if status == "confirmed"}
    excluded = {key for key, status in kept.items() if status == "excluded"}
    if not ids - excluded:
        return None
    decided = candidates
    if confirmed or excluded:
        decided = apply_requirement_decisions(decided, confirmed, excluded)
    if ids - confirmed - excluded:
        decided = apply_requirement_decisions(decided, ids - confirmed - excluded, set(), decided_by="auto")
    return decided


def cv_language(chosen: dict | None, jd_text: str, available: list[str]) -> str:
    """The CV language for a job: the user's choice, else the posting's own language, as long as
    a CV in it exists; otherwise the language a CV exists in."""
    wanted = chosen.get("language") if chosen else job_language(jd_text)
    return wanted if wanted in available else (available or ["en"])[0]


def _logged(stage: dict, started: float) -> dict:
    return {**stage, "duration_ms": int((time.monotonic() - started) * 1000)}


def _stages_after(status: dict | None, draft_id: str | None, stage: dict, not_run: tuple[str, ...] = ()) -> list[dict]:
    """How the preparation stands after one stage was redone: that stage replaced, the stages
    built on its old result marked not run (as the single-user page shows it)."""
    earlier = status["stages"] if status and status.get("draft_material_id") == draft_id else [
        stage_entry("draft", "done", "Built from your confirmed facts.")]
    kept = [item for item in earlier if item["stage"] not in (stage["stage"], *not_run)]
    again = [stage_entry(name, "skipped", f"{STAGE_FAILED[name]} since the last change; use the button below.")
             for name in not_run]
    return sorted([*kept, stage, *again], key=lambda item: STAGES.index(item["stage"]))


def prepare_cv(ctx: Any, job_id: str, language: str, *, explicit: bool, result: dict | None = None) -> None:
    """Draft, reword and adjust one language's CV for the job's current requirements, and publish
    them as new versions with how each stage went. When the model cannot reword or adjust, the
    CV stays at the last stage that worked and says why. ``explicit``: the user asked for this;
    a CV that cannot even be drafted then fails the task, while after finding requirements it
    only records why (the job itself was made)."""
    inputs = ctx.user.cv_inputs(job_id, language)
    head = inputs["head"]
    base = ctx.request.get("base_head", head["material_id"] if head else None)
    if (head["material_id"] if head else None) != base:
        ctx.finish("superseded", error_code="superseded", message="A newer CV was made before this one started")
        return
    old_root = inputs["chain"][-1]["material_id"] if inputs["chain"] else None
    requirements = inputs["requirements"]
    decided = requirements.get("decided") if requirements else None
    started = time.monotonic()
    try:
        if not decided:
            raise CVError("先确认这个岗位的要求", reason="no_requirements")
        if inputs["profile"] is None:
            raise CVError("请先上传这个语言的简历", reason="no_profile")
        if not inputs["facts"].listed():
            raise CVError("还没有事实", reason="no_facts")
        draft = build_draft(inputs["profile"]["profile"], inputs["facts"], language, job=decided)
    except CVError as exc:
        failure = stage_failure("draft", exc)
        if old_root:
            failure.update(output_available=True,
                           message=failure["message"] + " The CV below is from the last time it could be prepared.")
        stages = [failure, *(stage_entry(name, "skipped", f"{STAGE_FAILED[name]}: the CV could not be drafted.",
                                         output=bool(old_root)) for name in ("rewording", "layout"))]
        ctx.user.record_stages(task_id=ctx.task_id, token=ctx.token, job_id=job_id, language=language,
                               draft_id=old_root, stages=stages)
        if explicit:
            ctx.fail(reason(exc), str(exc))
        else:
            ctx.finish("succeeded", result={**(result or {}), "cv": "not_prepared", "reason": reason(exc)})
        return
    documents = [("draft", draft)]
    stages = [stage_entry("draft", "done", "Built from your confirmed facts.")]
    head_document = draft
    ctx.stage("rewording")
    started = time.monotonic()
    try:
        # Thinking off: on a real CV it gave the same result in 2 s instead of 8 s (2026-09).
        head_document = tailor_draft(draft, inputs["facts"], job=decided, chat=ctx.chat, effort="none",
                                     private=inputs["private"])
        documents.append(("tailored", head_document))
        stages.append(_logged(reworded(head_document), started))
    except CVError as exc:
        stages.append(_logged(stage_failure("rewording", exc), started))
    ctx.stage("layout")
    started = time.monotonic()
    try:
        planned = plan_draft(head_document, decided, chat=ctx.chat, private=inputs["private"])
        documents.append(("planned", planned))
        stages.append(_logged(adjusted(planned), started))
    except CVError as exc:
        stages.append(_logged(stage_failure("layout", exc), started))
    ctx.user.publish_cv(task_id=ctx.task_id, token=ctx.token, job_id=job_id, language=language,
                        base_head=base, documents=documents, profile_version=inputs["profile"]["version"],
                        requirements_version=requirements["version"], stages=stages,
                        result={**(result or {}), "job_id": job_id, "language": language, "cv": "prepared"})


def _redo_stage(ctx: Any, stage: str) -> None:
    """Reword again (from the draft) or adjust again (from the reworded CV, else the draft) and
    publish the result as the newest version; a failure keeps the CV and records why."""
    job_id, language = ctx.request["job_id"], ctx.request["language"]
    inputs = ctx.user.cv_inputs(job_id, language)
    head = inputs["head"]
    if head is None:
        raise TaskFailed("请先准备这个岗位的简历", "no_cv")
    if head["material_id"] != ctx.request.get("base_head"):
        ctx.finish("superseded", error_code="superseded", message="A newer CV was made before this one started")
        return
    if not head["inputs_current"]:
        raise TaskFailed("简历资料已更新，请重新准备并审核这份简历", "stale")
    chain = inputs["chain"]
    root = chain[-1]
    decided = inputs["requirements"]["decided"]
    started = time.monotonic()
    if stage == "rewording":
        base = root
        try:
            document = tailor_draft(root["draft"], inputs["facts"], job=decided, chat=ctx.chat,
                                    private=inputs["private"])
            entry = _logged(reworded(document), started)
        except CVError as exc:
            earlier = any(item["stage"] == "tailored" for item in chain)
            ctx.user.record_stages(task_id=ctx.task_id, token=ctx.token, job_id=job_id, language=language,
                                   draft_id=root["material_id"],
                                   stages=_stages_after(inputs["status"], root["material_id"],
                                                        _logged(stage_failure("rewording", exc, earlier), started)))
            ctx.fail(reason(exc), str(exc))
            return
        kind, not_run = "tailored", ("layout",)
    else:
        base = next((item for item in chain if item["stage"] == "tailored"), root)
        try:
            document = plan_draft(base["draft"], decided, chat=ctx.chat, private=inputs["private"])
            entry = _logged(adjusted(document), started)
        except CVError as exc:
            earlier = any(item["stage"] == "planned" for item in chain)
            ctx.user.record_stages(task_id=ctx.task_id, token=ctx.token, job_id=job_id, language=language,
                                   draft_id=root["material_id"],
                                   stages=_stages_after(inputs["status"], root["material_id"],
                                                        _logged(stage_failure("layout", exc, earlier), started)))
            ctx.fail(reason(exc), str(exc))
            return
        kind, not_run = "planned", ()
    stages = _stages_after(inputs["status"], root["material_id"], entry, not_run)
    ctx.user.publish_cv(task_id=ctx.task_id, token=ctx.token, job_id=job_id, language=language,
                        base_head=head["material_id"], documents=[(kind, document)],
                        profile_version=head["profile_version"], requirements_version=head["requirements_version"],
                        stages=stages, draft_id=base["material_id"],
                        result={"job_id": job_id, "language": language})


def _find_and_prepare(ctx: Any, job_id: str, base_version: int, kept: dict | None, result: dict) -> None:
    """Find the job's requirements (the model picks JD lines; the heading rules if it cannot),
    count them, save them, then prepare the CV in the job's language."""
    current = ctx.user.requirements(job_id)
    if not (current and current.get("task_id") == ctx.task_id):  # a retry that saved them already goes on
        if (current["version"] if current else 0) != base_version:  # changed since: no model call for nothing
            ctx.finish("superseded", error_code="superseded",
                       message="The requirements were changed while they were being found")
            return
        job = ctx.user.get_job(job_id)
        ctx.stage("requirements")
        try:
            candidates = propose_requirements({"jd": job["jd"], "facts": [], "selected_requirements": []},
                                              chat=ctx.chat)
            decided = auto_decide(candidates, kept)
        except RequirementError as exc:
            raise TaskFailed(str(exc), "bad_job") from exc
        if ctx.user.task_requirements(task_id=ctx.task_id, token=ctx.token, job_id=job_id, base_version=base_version,
                                      candidates=candidates, decided=decided) is None:
            return  # superseded: the user saved other requirements meanwhile
    snapshot = ctx.user.job_snapshot(job_id)
    language = cv_language(snapshot["notes"].get("cv-language"), snapshot["job"]["jd"]["text"], snapshot["cv_languages"])
    if not snapshot["requirements"].get("decided"):
        ctx.finish("succeeded", result={**result, "cv": "not_prepared", "reason": "no_requirements"})
        return
    head = snapshot["cv"][language]
    ctx.request = {**ctx.request, "base_head": head["material"]["material_id"] if head else None}
    prepare_cv(ctx, job_id, language, explicit=False, result=result)


def run_prepare_job(ctx: Any) -> None:
    request = ctx.request
    _find_and_prepare(ctx, request["job_id"], request["base_requirements_version"], request.get("kept"),
                      {"job_id": request["job_id"]})


def _job_from_posting(ctx: Any, selected: dict) -> None:
    jd = selected.get("jd") or {}
    if not isinstance(jd.get("text"), str) or not jd["text"].strip():
        raise TaskFailed("岗位接口未返回 JD 正文，请使用手动粘贴。", "no_text")
    existing = ctx.user.find_job(source=jd.get("source"))
    if existing and not (ctx.job_id and existing == ctx.job_id):
        ctx.finish("succeeded", result={"job_id": existing, "existing": True})
        return
    job = ctx.user.task_job(task_id=ctx.task_id, token=ctx.token, jd=prepare_review_input(selected)["jd"])
    ctx.job_id = job["job_id"]
    _find_and_prepare(ctx, job["job_id"], 0, None, {"job_id": job["job_id"], "existing": False})


def make_handlers(*, printer: Callable[[Path, Path], None] = print_with_chrome,
                  posting: Callable[..., dict] = fetch_selected,
                  page: Callable[[str], dict] = fetch_job_url) -> dict[str, Callable[[Any], None]]:
    """The task handlers by operation. ``posting`` reads one board posting, ``page`` any public
    job page and ``printer`` prints HTML to PDF; tests pass stand-ins, the app the real ones."""

    def run_upload(ctx: Any) -> None:
        request = ctx.request
        try:
            proposal = structure_cv(request["lines"], ctx.chat, links=request["links"])
        except CVImportError as exc:
            raise TaskFailed(str(exc), reason(exc)) from exc
        proposal["language"] = request["language"]
        proposal["source_sha256"] = request["source_sha256"]
        ctx.user.publish_upload(task_id=ctx.task_id, token=ctx.token, proposal=proposal,
                                language=request["language"], source_sha256=request["source_sha256"])

    def run_job_from_url(ctx: Any) -> None:
        request = ctx.request
        ctx.stage("reading")
        try:
            if request.get("identity"):
                provider, board, posting_id = request["identity"]
                selected = posting(board, posting_id, provider)
            else:
                selected = page(request["url"])
        except SearchError as exc:
            raise TaskFailed(str(exc), "unreadable") from exc
        if isinstance(selected.get("jd"), dict):
            selected["jd"]["requested_url"] = request["url"]
        _job_from_posting(ctx, selected)

    def run_start_listing(ctx: Any) -> None:
        request = ctx.request
        ctx.stage("reading")
        try:
            selected = posting(request["board"], request["posting_id"], request["provider"])
        except SearchError as exc:
            raise TaskFailed(str(exc), "unreadable") from exc
        if isinstance(selected.get("jd"), dict):
            selected["jd"]["company"] = selected["jd"].get("company") or request.get("company")
        _job_from_posting(ctx, selected)

    def run_prepare_cv(ctx: Any) -> None:
        prepare_cv(ctx, ctx.request["job_id"], ctx.request["language"], explicit=True)

    def run_check_gaps(ctx: Any) -> None:
        request = ctx.request
        inputs = ctx.user.gaps_inputs(request["job_id"], request["language"])
        if inputs["head"] is None:
            raise TaskFailed("请先准备这个岗位的简历，再检查缺口", "no_cv")
        decided = (inputs["requirements"] or {}).get("decided")
        if not decided:
            raise TaskFailed("请先确认这个岗位的要求", "no_requirements")
        ctx.stage("checking")
        gaps = find_gaps(decided, inputs["head"]["draft"], inputs["facts"], ctx.chat, private=inputs["private"],
                         previous=inputs["gaps"]["gaps"] if inputs["gaps"] else None)
        ctx.user.publish_gaps(task_id=ctx.task_id, token=ctx.token, job_id=request["job_id"],
                              base_version=request["base_gaps_version"], gaps=gaps,
                              checked_head=inputs["head"]["material_id"],
                              requirements_version=inputs["requirements"]["version"])

    def run_export(ctx: Any) -> None:
        request = ctx.request
        inputs = ctx.user.export_inputs(request["job_id"], request["language"])
        if inputs["approval_id"] != request["approval_id"]:
            ctx.finish("superseded", error_code="superseded", message="The CV was approved again; export the new one")
            return
        ctx.stage("printing")
        with tempfile.TemporaryDirectory(prefix="cv-v2-") as directory:
            output = Path(directory) / "cv.pdf"
            try:
                summary = export_pdf(inputs["document"], inputs["facts"], output, printer=printer)
            except CVError as exc:
                raise TaskFailed(str(exc), "pdf_failed") from exc
            data = output.read_bytes()
        ctx.user.publish_pdf(task_id=ctx.task_id, token=ctx.token, approval_id=inputs["approval_id"], data=data,
                             pages=summary["pages"])

    return {
        "upload_cv": run_upload, "prepare_job": run_prepare_job, "job_from_url": run_job_from_url,
        "start_listing": run_start_listing, "prepare_cv": run_prepare_cv,
        "tailor_cv": lambda ctx: _redo_stage(ctx, "rewording"), "plan_cv": lambda ctx: _redo_stage(ctx, "layout"),
        "check_gaps": run_check_gaps, "export_pdf": run_export,
    }
