"""What a job asks for that the CV does not show yet, with honest suggestions to close it.

Gaps are the requirements DeepSeek's strict matching finds no confirmed fact for (tag matching
when DeepSeek is unavailable). For each gap DeepSeek may suggest one line the user could add
if it is true: items for an existing skills line, or a bullet under an existing entry.
Nothing reaches the CV until the user says the line is true; it then becomes a confirmed fact
like any other. Only requirement texts and CV line texts are sent, never names or employers.
"""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from claims import LINK, NUMBER, _leadership
from cv import CVError, _localized
from cv_layout import original_layout
from deepseek_client import DEFAULT_MODEL, DeepSeekError
from facts import add_fact, confirm_fact, list_facts, load_current_facts, revise_fact, tag_pattern
from matching import propose_matches
from privacy import mask, private_terms


GAPS_VERSION = 1
BULLET_TYPES = {"experience": "experience", "projects": "project"}
MAX_ITEMS = 5
MAX_ITEM_CHARACTERS = 40
MAX_LINE_CHARACTERS = 300
MAX_TAGS = 8
SUGGEST_RULES = """You help an applicant close the gaps between a job's requirements and their resume,
and reply in json only. The applicant confirms every suggestion before anything is added, so for
each gap that names a concrete tool, language, framework, platform, method or practice, suggest the
smallest addition that would show it, true or not for this applicant; they will decide. Suggest at
most one addition per gap, in the same language as the resume lines:
- "skill": items to append to one existing skills line (give that line's id), such as a tool name.
- "bullet": one new bullet under an existing experience or project entry (give the entry id),
  written like the other bullets there and describing plain hands-on use.
- "none": when no honest addition fits, such as years of experience, seniority, leadership, a
  degree, work authorization or personal traits.
Never include numbers, metrics, results, team sizes or leadership words, and never claim more than
basic hands-on use. Give "tags": the skill words the bullet names.
Reply as {"suggestions": [{"requirement": "<gap id>", "kind": "skill", "line": "<line id>",
"items": ["..."]}, {"requirement": "<gap id>", "kind": "bullet", "entry": "<entry id>",
"text": "...", "tags": ["..."]}, {"requirement": "<gap id>", "kind": "none"}]}"""


def _entry_key(kind: str, fields: dict[str, Any]) -> str:
    """An entry told apart by what it shows, not by its position: a suggestion made for one
    employer must never land under another after the layout changes."""
    return json.dumps([kind, *(fields.get(field) for field in ("title", "subtitle", "location", "dates"))],
                      ensure_ascii=False)


def _profile_entry_key(kind: str, entry: Any, language: str) -> str | None:
    try:
        return _entry_key(kind, {field: _localized(entry.get(field), language, field, [])
                                 for field in ("title", "subtitle", "location", "dates")})
    except (AttributeError, CVError):
        return None


def _resume(draft: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, tuple[str, str, str]]]:
    """The CV as ids and line texts (masked for DeepSeek), the skills lines by fact ID, and
    bullet entries by ID."""
    texts = {
        line["fact_id"]: line.get("source_text") or line["text"]
        for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]
    }
    private = private_terms(draft)
    layout = original_layout(draft)
    resume = [
        {"section": section["kind"], "entries": [
            {"entry": entry["entry"], "lines": [{"id": fact_id, "text": mask(texts[fact_id], private)}
                                                for fact_id in entry["lines"]]}
            for entry in section["entries"]
        ]}
        for section in layout
    ]
    skills = {
        fact_id: texts[fact_id]
        for section in layout if section["kind"] == "skills"
        for entry in section["entries"] for fact_id in entry["lines"]
    }
    entries = {}
    for i, section in enumerate(draft["sections"]):
        if section["kind"] in BULLET_TYPES:
            for j, entry in enumerate(section["entries"]):
                entries[f"s{i}e{j}"] = (section["kind"], entry.get("title") or section["title"],
                                        _entry_key(section["kind"], entry))
    return resume, skills, entries


def _existing_lines(draft: dict[str, Any]) -> list[str]:
    """Every line the CV holds, as confirmed and as reworded."""
    return [
        text for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]
        for text in (line["text"], line.get("source_text")) if text
    ]


def _suggestion(
    item: Any, skills: dict[str, str], entries: dict[str, tuple[str, str, str]], existing: list[str]
) -> dict[str, Any] | None:
    """Keep a suggestion only if it points at a real line or entry, adds something the CV does
    not already say, and claims nothing more than plain use."""
    if not isinstance(item, dict):
        return None
    if item.get("kind") == "skill" and item.get("line") in skills:
        line = skills[item["line"]]
        items: list[str] = []
        for value in item.get("items") if isinstance(item.get("items"), list) else []:
            if not isinstance(value, str):
                continue
            value = " ".join(value.split())
            if (value and len(value) <= MAX_ITEM_CHARACTERS and len(value.split()) <= 4
                    and not _leadership(value)
                    and not any(tag_pattern(value).search(known) for known in existing)
                    and value.casefold() not in {existing.casefold() for existing in items}):
                items.append(value)
        if not items:
            return None
        items = items[:MAX_ITEMS]
        return {"kind": "skill", "fact_id": item["line"], "items": items, "where": line,
                "new_text": f"{line}, {', '.join(items)}"}
    if item.get("kind") == "bullet" and item.get("entry") in entries:
        text = " ".join(item.get("text").split()) if isinstance(item.get("text"), str) else ""
        if (not text or len(text) > MAX_LINE_CHARACTERS or NUMBER.search(text)
                or _leadership(text) or LINK.search(text)
                or any(text.casefold() in known.casefold() or known.casefold() in text.casefold() for known in existing)):
            return None
        tags = [
            tag.strip() for tag in (item.get("tags") if isinstance(item.get("tags"), list) else [])
            if isinstance(tag, str) and tag.strip() and tag_pattern(tag.strip()).search(text)
        ]
        kind, where, key = entries[item["entry"]]
        return {"kind": "bullet", "entry": item["entry"], "entry_key": key, "fact_type": BULLET_TYPES[kind],
                "text": text, "tags": list(dict.fromkeys(tags))[:MAX_TAGS], "where": where}
    return None


def find_gaps(
    decided: dict[str, Any],
    draft: dict[str, Any],
    facts_db: Path,
    chat: Callable[..., dict[str, Any]],
    effort: str = "none",
) -> dict[str, Any]:
    """The requirements no confirmed fact covers, each with at most one checked suggestion."""
    matches = propose_matches(decided, facts_db, chat=chat, effort=effort, private=private_terms(draft))
    covered = {item["requirement_id"] for item in matches["match_candidates"]}
    gaps = [
        {"requirement_id": item["id"], "text": item["text"], "strength": item.get("strength") or "unclear",
         "suggestion": None, "status": "open"}
        for item in decided["selected_requirements"] if item["id"] not in covered
    ]
    suggesting: dict[str, Any] = {}
    if gaps:
        resume, skills, entries = _resume(draft)
        request = {
            "job_title": decided["jd"].get("title"),
            "gaps": [{"id": gap["requirement_id"], "text": gap["text"], "strength": gap["strength"]} for gap in gaps],
            "resume": resume,
        }
        messages = [
            {"role": "system", "content": SUGGEST_RULES},
            {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
        ]
        try:
            answer = chat(messages, model=DEFAULT_MODEL, effort=effort)
            picks = answer["content"].get("suggestions")
            suggesting = {"model": answer.get("model"), "usage": answer.get("usage")}
        except DeepSeekError as exc:
            picks, suggesting = [], {"fallback_reason": str(exc), "fallback_code": exc.reason}
        by_requirement: dict[str, Any] = {}
        for pick in picks if isinstance(picks, list) else []:
            requirement = pick.get("requirement") if isinstance(pick, dict) else None
            if isinstance(requirement, str) and requirement not in by_requirement:
                by_requirement[requirement] = _suggestion(pick, skills, entries, _existing_lines(draft))
        for gap in gaps:
            gap["suggestion"] = by_requirement.get(gap["requirement_id"])
    fact_matching = matches["fact_matching"]
    return {
        "gaps_version": GAPS_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "language": draft["language"],
        "matching": {key: fact_matching.get(key) for key in ("method", "fallback_reason", "fallback_code")
                     if fact_matching.get(key)},
        "suggesting": suggesting,
        "gaps": gaps,
    }


def _gap(gaps: dict[str, Any], requirement_id: str) -> dict[str, Any]:
    gap = next((item for item in gaps["gaps"] if item["requirement_id"] == requirement_id), None)
    if gap is None:
        raise ValueError(f"没有这条缺口：{requirement_id}")
    return gap


def accept_gap(
    gaps: dict[str, Any], requirement_id: str, facts_db: Path, profile: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The user says the suggestion is true: store it as a confirmed fact.

    A skill becomes a new confirmed version of that skills line, already on the CV. A bullet
    becomes a new confirmed fact and a new profile (returned) lists it under its entry; the
    caller's profile is not changed in place.

    A suggestion applies only to what it was made for: the skills line with the same text, or
    the entry with the same name, role, place and dates wherever it now sits. Every step
    can be repeated, so if saving stops before the caller records the gap as added, accepting
    it again finishes the job without adding anything twice.
    """
    updated = copy.deepcopy(gaps)
    gap = _gap(updated, requirement_id)
    if gap["status"] == "declined":
        raise ValueError("这条建议已标记为不属实；如需添加请重新检查缺口")
    if gap["status"] == "added":
        raise ValueError("这条建议已经添加过")
    suggestion = gap["suggestion"]
    if not suggestion:
        raise ValueError("这条要求没有可添加的建议")
    new_profile = None
    if suggestion["kind"] == "skill":
        fact_id = suggestion["fact_id"]
        current = load_current_facts(facts_db, [fact_id]).get(fact_id)
        if current is not None and current["text"] == suggestion["new_text"]:
            # Added before. A pending version with this text may carry other tags nobody has
            # reviewed, so it is confirmed on the Facts page, never here.
            if current["status"] != "confirmed":
                raise ValueError("这行技能有一个尚未确认的新版本，请先在 Facts 页面核对确认，再重新检查缺口")
        elif current is not None and current["status"] == "confirmed" and current["text"] == suggestion["where"]:
            revised, _ = revise_fact(facts_db, fact_id, text=suggestion["new_text"],
                                     tags=[*current["tags"], *suggestion["items"]])
            confirm_fact(facts_db, fact_id, revised["version"])
        else:
            raise ValueError("这行技能已经改变，请重新检查缺口")
    else:
        places = [
            (i, j) for i, section in enumerate(profile["sections"]) for j, entry in enumerate(section.get("entries", []))
            if suggestion.get("entry_key") is not None
            and _profile_entry_key(section.get("kind"), entry, gaps["language"]) == suggestion["entry_key"]
        ]
        if len(places) != 1:
            raise ValueError(f"建议所属的条目（{suggestion['where']}）已经改变，请重新检查缺口")
        section, entry = places[0]
        elsewhere = {
            known for i, other in enumerate(profile["sections"]) for j, listed in enumerate(other.get("entries", []))
            if (i, j) != (section, entry) for known in listed.get("facts") or []
        }
        # The fact add_fact would return for this line, if it exists already: checked before
        # anything is written, so a refused acceptance changes nothing.
        tags = sorted({tag.casefold() for tag in suggestion["tags"]})
        same = {fact["id"] for fact in list_facts(facts_db) if fact["text"] == suggestion["text"]
                and fact["fact_type"] == suggestion["fact_type"] and sorted({tag.casefold() for tag in fact["tags"]}) == tags}
        if same & elsewhere:
            raise ValueError(f"这一行已在简历的其他条目下，不在 {suggestion['where']}；请重新检查缺口")
        fact, _ = add_fact(facts_db, suggestion["text"], suggestion["fact_type"], suggestion["tags"])
        confirm_fact(facts_db, fact["id"], fact["version"])
        fact_id = fact["id"]
        if fact_id not in (profile["sections"][section]["entries"][entry].get("facts") or []):
            new_profile = copy.deepcopy(profile)
            new_profile["sections"][section]["entries"][entry].setdefault("facts", []).append(fact_id)
    gap.update(status="added", fact_id=fact_id)
    return updated, new_profile


def decline_gap(gaps: dict[str, Any], requirement_id: str) -> dict[str, Any]:
    """The user says the suggestion is not true: it stays a gap and never reaches the CV."""
    updated = copy.deepcopy(gaps)
    gap = _gap(updated, requirement_id)
    if gap["status"] == "added":
        raise ValueError("这条建议已经添加；如需撤回请在事实库中修改")
    gap["status"] = "declined"
    return updated
