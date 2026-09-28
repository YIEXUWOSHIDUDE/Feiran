"""Per-job CV plan: DeepSeek proposes which sections, entries and lines to show, and in what order.

Only the job title, its requirements and the CV's line texts (under stand-in IDs) are sent:
never the name, contact details, schools, employers, project names or fact IDs. The proposal passes cv_layout's
guardrails before it is used. The draft keeps every line, so each listed change can be
undone, or done again, without asking the model.
"""

import copy
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from cv import CVError, _job_summary, requirement_briefs
from privacy import mask, private_terms, stand_ins
from cv_layout import describe_changes, guarded_layout, original_layout
from deepseek_client import DEFAULT_MODEL, DeepSeekError, chat_json


PLAN_VERSION = 1
MAX_REASON_CHARACTERS = 200
PLAN_RULES = """You arrange a one-page resume for one job application and reply in json only.
You get the job title, its requirements (each "required", "preferred" or "unclear"; required ones
matter most), and the resume as sections of entries, each with line ids and texts. Put the evidence most relevant to this job first and leave out what does not
help this job:
- "sections": section names in the order to show them. Leave out a section that does not help
  this job. Education always stays, in its current place.
- "entries": each entry you reorder or shorten, with the ids of the lines to show, most relevant
  first; list an entry with no lines to leave it out. Entries you do not list stay as they are.
  Leave out lines, projects, publications or skill lines that do not help this job. Experience
  entries stay in date order and keep at least one line.
- Never invent ids or text; only choose and order what is given.
- Keep enough to fill about one page: do not leave out lines that show skills or experience the
  job asks for.
- "reasons": one short reason per change, tied to the job's requirements, with the target id
  (a section name, an entry id or a line id), or "sections" for the section order.
Reply as {"sections": ["education", ...], "entries": [{"entry": "s1e0", "lines": ["line id"]}],
"reasons": [{"target": "id", "reason": "..."}]}"""


def _reasons(value: Any) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and isinstance(item.get("target"), str) and isinstance(item.get("reason"), str):
            reason = " ".join(item["reason"].split())[:MAX_REASON_CHARACTERS]
            if reason:
                reasons.setdefault(item["target"], reason)
    return reasons


def _facts_named(content: dict[str, Any], back: dict[str, str]) -> dict[str, Any]:
    """The answer with each line's stand-in turned back into its fact ID. A line name that was
    never sent is dropped; entry IDs and section names in reasons pass through."""
    entries = [
        {**item, "lines": [back[line] for line in item["lines"] if isinstance(line, str) and line in back]}
        if isinstance(item, dict) and isinstance(item.get("lines"), list) else item
        for item in content["entries"]
    ]
    reasons = [
        {**item, "target": back.get(item["target"], item["target"])}
        if isinstance(item, dict) and isinstance(item.get("target"), str) else item
        for item in (content.get("reasons") if isinstance(content.get("reasons"), list) else [])
    ]
    return {**content, "entries": entries, "reasons": reasons}


def _with_reasons(changes: list[dict[str, str]], reasons: dict[str, str], kinds: list[str]) -> list[dict[str, Any]]:
    """Attach the model's reason for each change's target; a section may be named by id or kind.
    A cut section with no reason of its own takes the one given for the section list, where
    DeepSeek tends to explain leaving a section out."""
    result = []
    for change in changes:
        target = change["id"].split(":", 1)[1]
        section = re.fullmatch(r"s(\d+)", target)
        names = [target] + ([kinds[int(section[1])]] if section else [])
        if change["type"] == "cut" and target in kinds:
            names.append("sections")
        reason = next((reasons[name] for name in names if name in reasons), None)
        result.append({**change, "reason": reason})
    return result


def plan_draft(
    draft: Any,
    job: Any,
    chat: Callable[..., dict[str, Any]] = chat_json,
    model: str = DEFAULT_MODEL,
    effort: str = "none",
    private: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a copy of the draft with a per-job layout and the list of changes it makes.
    ``private`` adds words the request must mask to those of the draft itself."""
    if not isinstance(draft, dict) or "approval" in draft:
        raise CVError("已批准的简历不能再调整结构；请重新准备简历")
    if "plan" in draft:
        raise CVError("这份简历已经按岗位调整过；请从未调整的版本开始")
    job_summary = _job_summary(job)
    if job_summary is None:
        raise CVError("按岗位调整结构需要这个岗位的要求")
    try:
        layout = original_layout(draft)
    except ValueError as exc:
        raise CVError(str(exc)) from exc
    private = sorted({*private_terms(draft), *(private or ())}, key=len, reverse=True)  # a line may hold the name or an employer
    texts = {
        line["fact_id"]: mask(line.get("source_text") or line["text"], private)
        for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]
    }
    out, back = stand_ins(texts)
    request = {
        "job_title": job_summary["title"],
        "job_requirements": requirement_briefs(job),
        "resume": [
            {"section": section["kind"], "entries": [
                {"entry": entry["entry"], "lines": [{"id": out[fact_id], "text": texts[fact_id]} for fact_id in entry["lines"]]}
                for entry in section["entries"]
            ]}
            for section in layout
        ],
    }
    messages = [
        {"role": "system", "content": PLAN_RULES},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]
    try:
        answer = chat(messages, model=model, effort=effort)
    except DeepSeekError as exc:
        raise CVError(f"DeepSeek 调整结构失败：{exc}") from exc
    content = answer.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("sections"), list) \
            or not isinstance(content.get("entries"), list):
        raise CVError("DeepSeek 的结构建议缺少 sections 或 entries")
    content = _facts_named(content, back)
    planned_layout = guarded_layout(draft, content)
    planned = copy.deepcopy(draft)
    planned["plan"] = {
        "plan_version": PLAN_VERSION,
        "provider": "deepseek",
        "requested_model": model,
        "model": answer.get("model"),
        "effort": effort,
        "usage": answer.get("usage"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "job": job_summary,
        "layout": planned_layout,
        "changes": _with_reasons(
            describe_changes(draft, planned_layout), _reasons(content.get("reasons")),
            [section["kind"] for section in layout],
        ),
        "undone": [],
    }
    return planned


def set_change(draft: Any, change_id: str, undone: bool = True) -> dict[str, Any]:
    """Undo one listed change (or a DeepSeek rewrite), or do it again, in a new copy."""
    if not isinstance(draft, dict) or "approval" in draft:
        raise CVError("已批准的简历不能再改动；如需修改，请重新准备简历")
    plan = draft.get("plan")
    if not isinstance(plan, dict):
        raise CVError("这份简历还没有按岗位调整")
    known = {item["id"] for item in plan["changes"]} | {
        f"reword:{line['fact_id']}"
        for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]
        if line.get("tailoring", {}).get("status") == "accepted"
    }
    if change_id not in known:
        raise CVError(f"没有这项改动：{change_id}")
    result = copy.deepcopy(draft)
    current = set(plan["undone"])
    if undone:
        current.add(change_id)
    else:
        current.discard(change_id)
    result["plan"]["undone"] = sorted(current)
    return result
