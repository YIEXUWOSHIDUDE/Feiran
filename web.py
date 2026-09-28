"""Local web page for the workbench: a thin FastAPI layer over the existing modules.

The server only listens on 127.0.0.1. It also rejects any Host other than localhost
(DNS rebinding) and any /api request without the token embedded in the page at start,
so other websites open in the browser cannot read facts or trigger DeepSeek calls.
"""

import argparse
import json
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cv import (
    CVError,
    approve_draft,
    build_draft,
    export_pdf,
    is_final_approval,
    print_with_chrome,
    profile_languages,
    rejected_lines,
    render_html,
    rewritten_lines,
    tailor_draft,
)
from cv_import import MAX_PDF_BYTES, CVImportError, build_profile, read_pdf, structure_cv
from cv_plan import plan_draft, set_change
from deepseek_client import DeepSeekError, chat_json
from facts import DEFAULT_DATABASE, FactStoreError, confirm_facts, import_facts, list_facts, parse_fact_refs
from gaps import accept_gap, decline_gap, find_gaps
from job_search import (
    SearchError,
    fetch_board,
    fetch_selected,
    parse_board_link,
    prepare_pasted_jd,
    prepare_review_input,
)
from listings import (
    ListingsError,
    add_source,
    get_listing,
    initialize,
    list_sources,
    load_starter,
    ranked_listings,
    refresh_source,
    remove_source,
)
from matching import MatchingError, apply_match_decisions, first_candidates, propose_matches
from privacy import private_terms
from requirement_flow import (
    RequirementError,
    add_manual_requirements,
    apply_requirement_decisions,
    propose_requirements,
)
from review import build_report
from workspace import DEFAULT_ROOT, Workspace, WorkspaceError


WEB_DIR = Path(__file__).parent / "web"
DEFAULT_PROFILE = Path(".local/cv-profile.json")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
CANDIDATE_FIELDS = ("id", "text", "section", "strength", "status", "decided_by", "extraction_method")
CV_LANGUAGES = ("en", "zh")
QUERY_TOKEN_PATHS = ("/preview/", "/download/")
UPLOAD_KEEP_SECONDS = 24 * 3600


class ConfirmRequest(BaseModel):
    refs: list[str]


class NewJobRequest(BaseModel):
    title: str
    text: str
    company: str | None = None
    url: str | None = None
    location: str | None = None


class AddRequirementRequest(BaseModel):
    text: str


class RequirementDecisionRequest(BaseModel):
    confirm: list[str] = []
    exclude: list[str] = []


FALLBACK_PATH = re.compile(r"sections\[(\d+)\](?:\.entries\[(\d+)\])?\.(title|subtitle|location|dates)")
FIELD_NAMES = {"title": "name", "subtitle": "role or degree", "location": "location", "dates": "dates"}


# What each kind of failure means and what to do about it. The page never quotes DeepSeek's raw
# error, so neither a key nor response text can reach it.
REASONS = {
    "missing_key": "No DeepSeek API key was found. Add it to the Keychain (service deepseek-api-key) "
                   "or set DEEPSEEK_API_KEY, then try again.",
    "key_rejected": "DeepSeek refused the API key. Check or replace the key, then try again.",
    "rate_limited": "DeepSeek is busy right now. Try again in a minute.",
    "unreachable": "DeepSeek could not be reached. Check the internet connection, then try again.",
    "request_failed": "DeepSeek returned an error. Try again later.",
    "bad_response": "DeepSeek's answer could not be used. Try again.",
    "nothing_found": "DeepSeek found no requirement lines.",
    "no_profile": "There is no CV yet. Upload your CV on the Facts page.",
    "no_facts": "There are no facts yet. Upload your CV on the Facts page.",
    "facts_not_confirmed": "Some lines of your CV are not confirmed yet. Confirm them on the Facts page.",
    "facts_missing": "Your CV lists facts that are no longer stored. Upload your CV again on the Facts page.",
    "profile_unreadable": "Your CV layout file could not be read, so it is not clear what must stay private. "
                          "Nothing was sent to DeepSeek. Upload your CV again on the Facts page.",
    "backup_unreadable": "A backup of your CV layout ({file} in .local/profile-history) could not be read, so it "
                         "is not clear what must stay private. Nothing was sent to DeepSeek. Delete or fix that "
                         "file, then try again.",
}
STAGES = ("draft", "rewording", "layout")
STAGE_FAILED = {"draft": "Not prepared", "rewording": "Not reworded", "layout": "Not adjusted for this job"}
STAGE_KEPT = {"rewording": "Your confirmed wording is used.", "layout": "Your usual layout is used."}
STAGE_KEPT_EARLIER = {"rewording": "The earlier rewording below is kept.", "layout": "The earlier adjusted layout below is kept."}


def _reason(exc: Exception) -> str:
    """The kind of failure: DeepSeek's own, or the one a domain error names."""
    for error in (exc, exc.__cause__):
        if isinstance(error, DeepSeekError):
            return error.reason
    return getattr(exc, "reason", "not_usable")


def _stage(stage: str, status: str, message: str, reason_code: str | None = None, output: bool = True) -> dict:
    return {"stage": stage, "status": status, "reason_code": reason_code, "message": message, "output_available": output}


def _stage_failure(stage: str, exc: Exception, earlier: bool = False) -> dict:
    """A stage that did not work: a fallback when a result stays usable, else failed. ``earlier``
    says a retry failed while this stage's earlier result is still shown."""
    code = _reason(exc)
    # Domain messages hold no key or response text.
    explanation = (REASONS.get(code) or str(exc)).replace("{file}", getattr(exc, "file", ""))
    if stage == "draft":
        return _stage(stage, "failed", f"{STAGE_FAILED[stage]}: {explanation}", code, output=False)
    kept = STAGE_KEPT_EARLIER[stage] if earlier else STAGE_KEPT[stage]
    return _stage(stage, "fallback", f"{STAGE_FAILED[stage]}{' again' if earlier else ''}: {explanation} {kept}", code)


def _reworded(head: dict) -> dict:
    changed, kept = len(rewritten_lines(head)), head["tailoring"]["rejected"]
    note = f"; {kept} kept as confirmed because the rewording failed the fact check" if kept else ""
    return _stage("rewording", "done", f"Reworded for this job: {changed} line(s) changed{note}.")


def _adjusted(planned: dict) -> dict:
    count = len(planned["plan"]["changes"])
    return _stage("layout", "done", f"Adjusted for this job: {count} change(s), listed below." if count
                  else "Adjusted for this job: your usual layout already fits.")


def _explained(record: dict | None) -> dict | None:
    """A fallback record with its reason in plain words for the page."""
    if isinstance(record, dict) and record.get("fallback_code"):
        return {**record, "message": REASONS.get(record["fallback_code"]) or record.get("fallback_reason")}
    return record


def _describe_fallbacks(draft: dict) -> list[str]:
    """Turn profile paths such as sections[1].entries[0].title into words the user can act on."""
    labels = []
    for path in draft["language_fallbacks"]:
        match = FALLBACK_PATH.fullmatch(path)
        if path == "name":
            labels.append(f"Name: {draft['header']['name']}")
        elif path == "contact.location":
            labels.append(f"Location: {draft['header']['details'][0]['text']}")
        elif match and match[2] is None:
            section = draft["sections"][int(match[1])]
            labels.append(f"{section['kind'].capitalize()} heading: {section['title']}")
        elif match:
            section = draft["sections"][int(match[1])]
            value = section["entries"][int(match[2])][match[3]]
            labels.append(f"{section['kind'].capitalize()} {FIELD_NAMES[match[3]]}: {value}")
        else:
            labels.append(path)
    return labels


class AddSourceRequest(BaseModel):
    link: str


class StartListingRequest(BaseModel):
    provider: str
    board: str
    job_id: str


class ChangeRequest(BaseModel):
    change_id: str
    undone: bool = True


class ContactLink(BaseModel):
    label: str = ""
    url: str


class SaveCVRequest(BaseModel):
    """The name and contact details as the user checked them; they never came from DeepSeek."""

    name: str
    location: str = ""
    phone: str = ""
    email: str = ""
    links: list[ContactLink] = []


def _job_language(text: str) -> str:
    """The CV language a posting most likely wants: Chinese when it is mostly written in Chinese."""
    chinese = sum("\u4e00" <= character <= "\u9fff" for character in text)
    latin = sum(character.isascii() and character.isalpha() for character in text)
    return "zh" if chinese * 4 > latin else "en"


class MatchDecisionRequest(BaseModel):
    links: dict[str, str] = {}
    no_match: list[str] = []


def _matching_view(matches: dict, linked: dict | None) -> dict:
    """Each confirmed requirement with the facts offered for it and any saved decision."""
    texts = {item["id"]: item["text"] for item in matches["selected_requirements"]}
    decisions = (linked or {}).get("match_decisions", {})
    candidates: dict[str, list] = {}
    for item in matches["match_candidates"]:
        candidates.setdefault(item["requirement_id"], []).append({
            key: item[key] for key in
            ("fact_id", "fact_version", "fact_text", "fact_type", "fact_tags", "retrieval_basis")
        })
    return {"requirements": [
        {
            "requirement_id": summary["requirement_id"],
            "text": texts[summary["requirement_id"]],
            "candidates": candidates.get(summary["requirement_id"], []),
            "decision": decisions.get(summary["requirement_id"]),
        }
        for summary in matches["fact_matching"]["requirements"]
    ]}


def create_app(
    facts_db: Path = DEFAULT_DATABASE,
    jobs_root: Path = DEFAULT_ROOT,
    token: str | None = None,
    profile_path: Path = DEFAULT_PROFILE,
    chat: Callable[..., dict] = chat_json,
    printer: Callable[[Path, Path], None] = print_with_chrome,
    listings_db: Path | None = None,
    starter: Iterable[dict[str, str]] | None = None,
    boards: Callable[..., list[dict]] = fetch_board,
    selected_posting: Callable[..., dict] = fetch_selected,
) -> FastAPI:
    token = token or secrets.token_urlsafe(32)
    workspace = Workspace(jobs_root)
    # Next to the fact store by default, so tests with a temporary fact store stay temporary too.
    listings_db = Path(listings_db or Path(facts_db).parent / "listings.db")
    initialize(listings_db, load_starter() if starter is None else starter)
    start_lock = threading.Lock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"error": "只接受本机访问"}, status_code=403)
        path = request.url.path
        # Frames and download links cannot send headers, so those read-only paths
        # carry the same per-start token in the query string instead.
        if path.startswith("/api/"):
            supplied = request.headers.get("x-workbench-token", "")
        elif path.startswith(QUERY_TOKEN_PATHS):
            supplied = request.query_params.get("token", "")
        else:
            supplied = token
        if not secrets.compare_digest(supplied, token):
            return JSONResponse({"error": "缺少或错误的访问令牌"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(FactStoreError)
    @app.exception_handler(WorkspaceError)
    @app.exception_handler(SearchError)
    @app.exception_handler(RequirementError)
    @app.exception_handler(MatchingError)
    @app.exception_handler(CVError)
    @app.exception_handler(ListingsError)
    @app.exception_handler(ValueError)
    async def domain_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    def require(job_id: str, step: str) -> dict:
        data = workspace.read(job_id, step)
        if data is None:
            raise WorkspaceError(f"请先完成上一步（缺少 {step}）")
        return data

    def job_view(job_id: str) -> dict:
        """What the page needs for one job: the JD and the current file of each step."""
        steps = workspace.state(job_id)
        jd = {key: value for key, value in require(job_id, "input")["jd"].items() if key != "raw_content"}
        view: dict = {
            "job_id": job_id, "steps": steps, "jd": jd,
            "language": job_cv_language(jd["text"]), "cv_languages": cv_languages(),
        }
        decided = workspace.read(job_id, "decided")
        requirements = decided or workspace.read(job_id, "candidates")
        if requirements:
            view["extraction"] = _explained(requirements.get("requirement_extraction"))
            view["candidates"] = [
                {field: item.get(field) for field in CANDIDATE_FIELDS}
                for item in requirements["requirement_candidates"]
            ]
        if decided:
            view["selected_requirements"] = decided["selected_requirements"]
        matches = workspace.read(job_id, "matches")
        linked = workspace.read(job_id, "linked")
        if matches:
            view["matching"] = _matching_view(matches, linked)
        if linked:
            view["report"] = build_report(linked)
        view["cv"] = {language: cv_view(job_id, language) for language in CV_LANGUAGES}
        gaps = workspace.read(job_id, "gaps")
        if gaps:
            view["gaps"] = {**gaps, "matching": _explained(gaps.get("matching")), "suggesting": _explained(gaps.get("suggesting"))}
        return view

    def check_language(language: str) -> str:
        if language not in CV_LANGUAGES:
            raise WorkspaceError(f"简历语言必须是：{', '.join(CV_LANGUAGES)}")
        return language

    def cv_head(job_id: str, language: str) -> tuple[str | None, dict | None]:
        """The newest CV file for a language; later steps always derive from earlier ones."""
        for step in ("approved", "planned", "tailored", "draft"):
            data = workspace.read(job_id, f"cv-{step}-{check_language(language)}")
            if data is not None:
                return step, data
        return None, None

    def cv_view(job_id: str, language: str) -> dict:
        head_step, head = cv_head(job_id, language)
        view: dict = {
            "head": head_step,
            "final_pdf": workspace.path(job_id, f"cv-final-{language}").exists(),
        }
        draft = workspace.read(job_id, f"cv-draft-{language}")
        if draft:
            view["language_fallbacks"] = _describe_fallbacks(draft)
        status = workspace.read_note(job_id, f"cv-status-{language}")
        if status and status.get("draft_created_at") == (draft or {}).get("created_at"):
            view["stages"] = status["stages"]
        if head and "tailoring" in head:
            tailoring = head["tailoring"]
            view["tailoring"] = {key: tailoring.get(key) for key in ("model", "accepted", "rejected", "usage")}
            view["rewrites"] = rewritten_lines(head)
            view["rejected"] = rejected_lines(head)
        if head and "plan" in head:
            undone = set(head["plan"]["undone"])
            view["changes"] = [{**item, "undone": item["id"] in undone} for item in head["plan"]["changes"]]
        if head_step == "approved":
            view["approved_at"] = head["approval"]["approved_at"]
        return view

    def load_profile() -> dict:
        if not Path(profile_path).exists():
            raise CVError(f"缺少简历 profile：{profile_path}", reason="no_profile")
        try:
            return json.loads(Path(profile_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CVError(f"无法读取简历 profile：{profile_path}", reason="profile_unreadable") from exc

    def save_profile(profile: dict) -> None:
        """Replace the profile, keeping the previous one in profile-history/ first."""
        path = Path(profile_path)
        if path.exists():
            history = path.parent / "profile-history"
            history.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            raw = path.read_bytes()
            try:
                readable = isinstance(json.loads(raw.decode("utf-8")), dict)
            except ValueError:
                readable = False
            # A broken file is kept for the user to look at, but never read as a backup.
            with (history / f"cv-profile-{stamp}.{'json' if readable else 'broken'}").open("xb") as backup:
                backup.write(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    # The page exists to help the user present their best CV for each job, not to grade them.
    # Everything up to the CV review happens by itself: the requirements DeepSeek (or the
    # heading rules) finds all count, and the CV in the posting's language is drafted, reworded
    # and adjusted to them. The user can undo any change; approving the CV is never automatic.
    def known_private_terms() -> list[str]:
        """What requests must mask: the name, contact details, schools and employers of the
        current CV in every language, and of every earlier one, since an older fact can still
        name a past employer."""
        path = Path(profile_path)
        terms: set[str] = set()
        for source in [path, *sorted((path.parent / "profile-history").glob("*.json"))]:
            if not source.exists():
                continue
            try:
                terms.update(private_terms(json.loads(source.read_text(encoding="utf-8"))))
            except (OSError, ValueError, AttributeError) as exc:
                # Unsure what is private: send nothing rather than guess.
                if source == path:
                    raise CVError(f"无法读取简历 profile：{source.name}", reason="profile_unreadable") from exc
                error = CVError(f"无法读取 profile 备份：{source.name}", reason="backup_unreadable")
                error.file = source.name
                raise error from exc
        return sorted(terms, key=len, reverse=True)

    def record_stages(job_id: str, language: str, stages: list[dict]) -> None:
        """How the latest preparation went, tied to the draft it produced (None if none)."""
        draft = workspace.read(job_id, f"cv-draft-{language}")
        workspace.write_note(job_id, f"cv-status-{language}", {
            "draft_created_at": draft["created_at"] if draft else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "stages": sorted(stages, key=lambda item: STAGES.index(item["stage"])),
        })

    def update_stage(job_id: str, language: str, stage: dict, not_run: tuple[str, ...] = ()) -> None:
        """Replace one stage after a retry; stages built on its old result are marked not run."""
        draft = workspace.read(job_id, f"cv-draft-{language}")
        status = workspace.read_note(job_id, f"cv-status-{language}")
        if not draft or not status or status.get("draft_created_at") != draft["created_at"]:
            status = {"stages": [_stage("draft", "done", "Built from your confirmed facts.")]}
        kept = [item for item in status["stages"] if item["stage"] not in (stage["stage"], *not_run)]
        again = [_stage(name, "skipped", f"{STAGE_FAILED[name]} since the last change; use the button below.")
                 for name in not_run]
        record_stages(job_id, language, [*kept, stage, *again])

    def prepare_cv(job_id: str, language: str) -> None:
        """Draft, reword and adjust one language's CV for this job, recording how each stage
        went. When DeepSeek cannot reword or adjust, the CV stays at the last stage that worked
        and the page says why, so a usable CV is never mistaken for a tailored one."""
        job = require(job_id, "decided")
        try:
            if not Path(facts_db).exists():
                raise CVError("还没有事实库", reason="no_facts")
            head = build_draft(load_profile(), facts_db, check_language(language), job=job)
        except (CVError, FactStoreError) as exc:
            # An earlier CV, if any, stays as it was; the page says it is not up to date.
            earlier = workspace.read(job_id, f"cv-draft-{language}") is not None
            failure = _stage_failure("draft", exc)
            if earlier:
                failure.update(output_available=True, message=failure["message"] + " The CV below is from the last "
                               "time it could be prepared.")
            record_stages(job_id, language, [failure, *(
                _stage(name, "skipped", f"{STAGE_FAILED[name]}: the CV could not be drafted.", output=earlier)
                for name in ("rewording", "layout"))])
            raise
        workspace.write(job_id, f"cv-draft-{language}", head)
        stages = [_stage("draft", "done", "Built from your confirmed facts.")]
        private = known_private_terms()
        try:
            # Thinking off: on a real CV it gave the same result in 2 s instead of 8 s (2026-09).
            head = tailor_draft(head, facts_db, job=job, chat=chat, effort="none", private=private)
            workspace.write(job_id, f"cv-tailored-{language}", head)
            stages.append(_reworded(head))
        except CVError as exc:
            stages.append(_stage_failure("rewording", exc))
        try:
            planned = plan_draft(head, job, chat=chat, private=private)
            workspace.write(job_id, f"cv-planned-{language}", planned)
            stages.append(_adjusted(planned))
        except CVError as exc:
            stages.append(_stage_failure("layout", exc))
        record_stages(job_id, language, stages)

    def cv_languages() -> list[str]:
        """Languages the user's resume is written in; only those get CVs."""
        try:
            return profile_languages(load_profile())
        except (CVError, OSError, ValueError):
            return ["en"]

    def job_cv_language(jd_text: str) -> str:
        """The posting's language when the resume has it, otherwise the resume's own language."""
        available = cv_languages()
        wanted = _job_language(jd_text)
        return wanted if wanted in available else available[0]

    def prepare_cv_quietly(job_id: str) -> None:
        """The CV for this posting in a language the resume is written in. Without a profile
        or confirmed facts yet, the CV panel's Prepare button shows what is missing."""
        try:
            prepare_cv(job_id, job_cv_language(require(job_id, "input")["jd"]["text"]))
        except (CVError, FactStoreError, OSError, ValueError):
            pass

    def match_automatically(job_id: str) -> None:
        """Optional talking points: the confirmed fact that best speaks to each requirement."""
        if not Path(facts_db).exists():
            raise FactStoreError("还没有事实库；请先导入并确认事实")
        matches = propose_matches(require(job_id, "decided"), facts_db, chat=chat, private=known_private_terms())
        workspace.write(job_id, "matches", matches)
        links, no_match = first_candidates(matches)
        linked = apply_match_decisions(matches, facts_db, links, no_match, decided_by="auto")
        workspace.write(job_id, "linked", linked)

    def prepare_automatically(job_id: str, kept: dict[str, str] | None = None) -> None:
        """Keep the user's own decisions (requirement ID -> status), count every other found
        requirement, then prepare the CV. What is counted this way is recorded as decided
        automatically, never as reviewed."""
        candidates = require(job_id, "candidates")
        ids = {item["id"] for item in candidates["requirement_candidates"]}
        kept = {key: status for key, status in (kept or {}).items() if key in ids}
        confirmed = {key for key, status in kept.items() if status == "confirmed"}
        excluded = {key for key, status in kept.items() if status == "excluded"}
        if not ids - excluded:
            return
        decided = candidates
        if confirmed or excluded:
            decided = apply_requirement_decisions(decided, confirmed, excluded)
        if ids - confirmed - excluded:
            decided = apply_requirement_decisions(decided, ids - confirmed - excluded, set(), decided_by="auto")
        workspace.write(job_id, "decided", decided)
        prepare_cv_quietly(job_id)

    def create_prepared_job(review_input: dict) -> str:
        candidates = propose_requirements(review_input, chat=chat)
        job_id = workspace.create_job(review_input)
        workspace.write(job_id, "candidates", candidates)
        prepare_automatically(job_id)
        return job_id

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__WORKBENCH_TOKEN__", token)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/api/facts")
    def facts() -> dict:
        if not Path(facts_db).exists():
            return {"facts": []}
        return {"facts": list_facts(facts_db)}

    @app.post("/api/facts/confirm")
    def confirm(request: ConfirmRequest) -> dict:
        confirmed = confirm_facts(facts_db, parse_fact_refs(request.refs))
        return {"changed_count": sum(changed for _, changed in confirmed)}

    # An uploaded CV is read here; DeepSeek only sees its lines without the name and contact
    # details. What it proposes waits in cv-uploads/ until the user checks the details and saves.
    uploads = Path(profile_path).parent / "cv-uploads"

    def upload_path(upload_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", upload_id):
            raise CVImportError("找不到这次上传；请重新上传 PDF")
        return uploads / f"{upload_id}.json"

    def propose_cv(data: bytes) -> dict:
        pdf = read_pdf(data)
        proposal = structure_cv(pdf["lines"], chat, links=pdf["links"])
        upload_id = secrets.token_hex(8)
        uploads.mkdir(parents=True, exist_ok=True)
        # An upload holds contact details; one neither saved nor cancelled is not kept for long.
        for forgotten in uploads.glob("*.json"):
            if time.time() - forgotten.stat().st_mtime > UPLOAD_KEEP_SECONDS:
                forgotten.unlink(missing_ok=True)
        (uploads / f"{upload_id}.json").write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
        known = {fact["text"] for fact in list_facts(facts_db)} if Path(facts_db).exists() else set()
        for section in proposal["sections"]:
            for entry in section["entries"]:
                for fact in entry["facts"]:
                    fact["known"] = fact["text"] in known
        return {"upload_id": upload_id, "has_profile": Path(profile_path).exists(), **proposal}

    @app.post("/api/cv/upload")
    async def upload_cv(request: Request) -> dict:
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_PDF_BYTES:
                raise CVImportError(f"PDF 不能超过 {MAX_PDF_BYTES // 1_000_000} MB")
        return await run_in_threadpool(propose_cv, bytes(data))

    @app.post("/api/cv/uploads/{upload_id}/save")
    def save_uploaded_cv(upload_id: str, request: SaveCVRequest) -> dict:
        """Import the CV's lines as pending facts and make it the CV layout, old one backed up."""
        path = upload_path(upload_id)
        if not path.exists():
            raise CVImportError("找不到这次上传；请重新上传 PDF")
        proposal = json.loads(path.read_text(encoding="utf-8"))
        profile, items, reused = build_profile(proposal, request.model_dump(), facts_db)
        if items:
            import_facts(facts_db, items)
        save_profile(profile)
        path.unlink()
        return {"imported": len(items), "reused": reused}

    @app.delete("/api/cv/uploads/{upload_id}")
    def cancel_upload(upload_id: str) -> dict:
        upload_path(upload_id).unlink(missing_ok=True)
        return {"cancelled": True}

    def started_jobs() -> dict[str, str]:
        """Official posting link -> the newest job already made from it."""
        started: dict[str, str] = {}
        for summary in workspace.jobs():
            if summary["source"]:
                started.setdefault(summary["source"], summary["job_id"])
        return started

    @app.get("/api/sources")
    def sources() -> dict:
        return {"sources": list_sources(listings_db)}

    @app.post("/api/sources")
    def follow_source(request: AddSourceRequest) -> dict:
        provider, board = parse_board_link(request.link)
        added = add_source(listings_db, provider, board, fetch=boards)
        return {"added": added, "sources": list_sources(listings_db)}

    @app.delete("/api/sources/{provider}/{board}")
    def unfollow_source(provider: str, board: str) -> dict:
        remove_source(listings_db, provider, board)
        return {"sources": list_sources(listings_db)}

    @app.post("/api/sources/{provider}/{board}/refresh")
    def refresh(provider: str, board: str) -> dict:
        return refresh_source(listings_db, provider, board, fetch=boards)

    @app.get("/api/listings")
    def listings(
        title: str = "", location: str = "", limit: int = 50, offset: int = 0, hide_senior: bool = False
    ) -> dict:
        ranked = ranked_listings(listings_db, facts_db, title, location, limit, offset, hide_senior)
        started = started_jobs()
        for group in ranked["listings"]:
            group["started_job"] = next(
                (started[item["source"]] for item in group["postings"] if item["source"] in started), None
            )
        return ranked

    @app.post("/api/listings/start")
    def start_listing(request: StartListingRequest) -> dict:
        # One job per posting: a second click, even a concurrent one, opens the first job.
        with start_lock:
            listing = get_listing(listings_db, request.provider, request.board, request.job_id)
            existing = started_jobs().get(listing["source"])
            if existing:
                return {"job_id": existing, "existing": True}
            selected = selected_posting(request.board, request.job_id, request.provider)
            selected["jd"]["company"] = selected["jd"].get("company") or listing["company"]
            return {"job_id": create_prepared_job(prepare_review_input(selected)), "existing": False}

    @app.get("/api/jobs")
    def jobs() -> dict:
        return {"jobs": workspace.jobs()}

    @app.post("/api/jobs")
    def new_job(request: NewJobRequest) -> dict:
        selected = prepare_pasted_jd(request.text, request.title, request.company, request.url, request.location)
        return {"job_id": create_prepared_job(prepare_review_input(selected))}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict:
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/requirements/add")
    def add_requirement(job_id: str, request: AddRequirementRequest) -> dict:
        decided = workspace.read(job_id, "decided") or {}
        # What the user decided stays; only they ever exclude a line.
        kept = {
            item["id"]: item["status"] for item in decided.get("requirement_candidates", [])
            if item.get("decided_by") == "user" or item.get("status") == "excluded"
        }
        candidates = require(job_id, "candidates")
        added = add_manual_requirements(candidates, [request.text])
        workspace.write(job_id, "candidates", added)
        for item in added["requirement_candidates"][len(candidates["requirement_candidates"]):]:
            kept[item["id"]] = "confirmed"  # the user added it as a requirement
        prepare_automatically(job_id, kept)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/requirements/find")
    def find_requirements(job_id: str) -> dict:
        workspace.write(job_id, "candidates", propose_requirements(require(job_id, "input"), chat=chat))
        prepare_automatically(job_id)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/requirements/decide")
    def decide_requirements(job_id: str, request: RequirementDecisionRequest) -> dict:
        decided = apply_requirement_decisions(
            require(job_id, "candidates"), set(request.confirm), set(request.exclude)
        )
        workspace.write(job_id, "decided", decided)
        prepare_cv_quietly(job_id)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/matches/propose")
    def propose_job_matches(job_id: str) -> dict:
        match_automatically(job_id)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/matches/decide")
    def decide_job_matches(job_id: str, request: MatchDecisionRequest) -> dict:
        linked = apply_match_decisions(
            require(job_id, "matches"), facts_db, request.links, set(request.no_match)
        )
        workspace.write(job_id, "linked", linked)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/draft")
    def cv_draft(job_id: str, language: str) -> dict:
        draft = build_draft(
            load_profile(), facts_db, check_language(language), job=workspace.read(job_id, "decided")
        )
        workspace.write(job_id, f"cv-draft-{language}", draft)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/tailor")
    def cv_tailor(job_id: str, language: str) -> dict:
        draft = require(job_id, f"cv-draft-{check_language(language)}")
        try:
            tailored = tailor_draft(draft, facts_db, job=workspace.read(job_id, "decided"), chat=chat,
                                    private=known_private_terms())
        except CVError as exc:
            earlier = workspace.read(job_id, f"cv-tailored-{language}") is not None
            update_stage(job_id, language, _stage_failure("rewording", exc, earlier))
            raise
        workspace.write(job_id, f"cv-tailored-{language}", tailored)
        update_stage(job_id, language, _reworded(tailored), not_run=("layout",))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/gaps")
    def check_gaps(job_id: str) -> dict:
        """What this job asks for that the CV does not show yet, with suggestions."""
        language = job_cv_language(require(job_id, "input")["jd"]["text"])
        draft = workspace.read(job_id, f"cv-draft-{language}")
        if draft is None:
            raise WorkspaceError("请先准备这个岗位的简历，再检查缺口")
        workspace.write(job_id, "gaps", find_gaps(require(job_id, "decided"), draft, facts_db, chat,
                                                  private=known_private_terms()))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/gaps/{requirement_id}/accept")
    def accept_suggestion(job_id: str, requirement_id: str) -> dict:
        """The user says the suggested line is true: it becomes a confirmed fact on the CV."""
        gaps = require(job_id, "gaps")
        updated, profile = accept_gap(gaps, requirement_id, facts_db, load_profile())
        if profile is not None:
            save_profile(profile)
        workspace.write(job_id, "gaps", updated)
        prepare_cv(job_id, gaps["language"])
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/gaps/{requirement_id}/decline")
    def decline_suggestion(job_id: str, requirement_id: str) -> dict:
        workspace.write(job_id, "gaps", decline_gap(require(job_id, "gaps"), requirement_id))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/prepare")
    def cv_prepare(job_id: str, language: str) -> dict:
        prepare_cv(job_id, language)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/plan")
    def cv_plan(job_id: str, language: str) -> dict:
        """Adjust (again) for this job, starting from the reworded CV, or the draft if none."""
        base = workspace.read(job_id, f"cv-tailored-{check_language(language)}") or require(job_id, f"cv-draft-{language}")
        try:
            planned = plan_draft(base, require(job_id, "decided"), chat=chat, private=known_private_terms())
        except CVError as exc:
            earlier = workspace.read(job_id, f"cv-planned-{language}") is not None
            update_stage(job_id, language, _stage_failure("layout", exc, earlier))
            raise
        workspace.write(job_id, f"cv-planned-{language}", planned)
        update_stage(job_id, language, _adjusted(planned))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/change")
    def cv_change(job_id: str, language: str, request: ChangeRequest) -> dict:
        planned = require(job_id, f"cv-planned-{check_language(language)}")
        workspace.write(job_id, f"cv-planned-{language}", set_change(planned, request.change_id, request.undone))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/approve")
    def cv_approve(job_id: str, language: str) -> dict:
        head_step, head = cv_head(job_id, language)
        if head is None:
            raise WorkspaceError("请先生成简历草稿")
        if head_step == "approved":
            raise CVError("这份简历已经批准；如需修改请重新生成草稿")
        workspace.write(job_id, f"cv-approved-{language}", approve_draft(head, facts_db))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/export")
    def cv_export(job_id: str, language: str) -> dict:
        approved = require(job_id, f"cv-approved-{check_language(language)}")
        with tempfile.TemporaryDirectory(prefix="cv-web-") as directory:
            output = Path(directory) / "cv.pdf"
            export_pdf(approved, facts_db, output, printer=printer)
            data = output.read_bytes()
        workspace.write_bytes(job_id, f"cv-final-{language}", data)
        return job_view(job_id)

    @app.get("/preview/{job_id}/{language}", response_class=HTMLResponse)
    def cv_preview(job_id: str, language: str) -> str:
        head_step, head = cv_head(job_id, language)
        if head is None:
            raise WorkspaceError("请先生成简历草稿")
        try:
            final = head_step == "approved" and is_final_approval(head)
        except CVError:
            final = False
        return render_html(head, final=final)

    @app.get("/download/{job_id}/{language}.pdf")
    def cv_download(job_id: str, language: str) -> FileResponse:
        path = workspace.path(job_id, f"cv-final-{check_language(language)}")
        if not path.exists():
            raise WorkspaceError("还没有已批准的最终 PDF")
        return FileResponse(path, media_type="application/pdf", filename=f"CV-{language.upper()}.pdf")

    app.state.workspace = workspace
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在本机浏览器中使用岗位匹配与简历工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--facts-db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_ROOT, help="每个岗位一个文件夹")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="简历 profile JSON")
    args = parser.parse_args(argv)
    import uvicorn

    print(f"打开 http://127.0.0.1:{args.port}/ （只在本机可用；按 Ctrl+C 停止）")
    app = create_app(args.facts_db, args.jobs, profile_path=args.profile)
    uvicorn.run(app, host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
