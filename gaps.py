"""Compare saved resume content with job requirements and propose complete additions.

Coverage describes the words present in the CV, not whether the applicant's claims are true.
Suggestions may extend beyond saved material. Only user adoption writes them into the CV.
Names, contact details, schools and employers are masked in model requests.
"""

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from claims import LINK
from cv import CVError, _localized
from cv_layout import original_layout, shown_sections
from deepseek_client import DEFAULT_MODEL, DeepSeekError
from facts import fact_source, tag_pattern
from privacy import mask, private_terms, stand_ins


GAPS_VERSION = 3
MAX_SETS = 3
MAX_SET_LINES = 3
MAX_RELATED = 3
MAX_MISSING_CHARACTERS = 120
EVIDENCE_RULES = """You check which lines of an applicant's resume show that they meet each of a job's
requirements, and reply in json only. The resume comes entry by entry (a job, school or project):
its "role" is the role or degree with dates, then its lines; a line may be a rewording of another
line of the same entry. "other_facts" are the applicant's saved material not on this resume.
Judge every line by its own words, and the years it covers only by its own entry's dates; the job
description is never evidence. For each requirement give one verdict:
- "supported": lines that together show every part of it, including any years, level, degree or
  scope it names; when it lists alternatives ("A, B or C"), any one of them is enough. Give "sets":
  the smallest groups of line ids that each show all of it on their own (usually one line per
  group), strongest first, at most 3 groups of at most 3 lines.
- "related": lines about the same skill or area that do not show all of it, such as the tool but
  not the years, or papers but not in the venues named. Give "lines": their ids, at most 3, and
  "missing": a few English words naming what no line shows.
- "none": no line is about it.
Being related is never "supported". Years, seniority and leadership count only when a line or
its own entry's dates state them. This describes resume coverage, not real-world verification;
it must never prevent generating or exporting new copy.
Reply as {"requirements": [{"id": "<requirement id>", "verdict": "supported", "sets": [["E1"]]},
{"id": "<requirement id>", "verdict": "related", "lines": ["E2"], "missing": "..."},
{"id": "<requirement id>", "verdict": "none"}]}"""
BULLET_TYPES = {"experience": "experience", "projects": "project"}
MAX_ITEMS = 5
MAX_ITEM_CHARACTERS = 40
MAX_LINE_CHARACTERS = 300
MAX_TAGS = 8
SUGGEST_RULES = """You write complete, job-targeted resume content and reply in JSON only.
Use the resume as context and style guidance, not as a factual boundary. The user wants useful
finished wording, including proposed experience, projects, skills, responsibilities, numbers or
years beyond the saved material. They choose which proposals to adopt. Do not demand evidence,
previous confirmation, answers to questions, or missing details before writing.
For each gap, produce one concise, specific addition in the resume language:
- "skill": items to append to an existing skills line whose category fits, with its line id.
  Keep category labels meaningful: Docker/Kubernetes belong to tools or platforms, not a
  programming-languages line. If no suitable skills line exists, write a bullet instead.
- "bullet": a complete resume sentence, at most 300 characters, under the most suitable existing
  experience/project entry, with its entry id and tags for terms actually in the sentence.
Prefer specific actions, tools and outcomes to generic claims. You may rephrase and expand existing
content. Do not merely copy the requirement, repeat an existing line verbatim, return "none",
or return a template, placeholders or questions. Do not include contact details or links.
The output is proposed copy, not an assertion that the system verified the applicant.
Reply as {"suggestions": [{"requirement": "<gap id>", "kind": "skill", "line": "<line id>",
"items": ["..."]}, {"requirement": "<gap id>", "kind": "bullet", "entry": "<entry id>",
"text": "...", "tags": ["..."]}]}"""


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


def _resume(
    draft: dict[str, Any], private: list[str]
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]], dict[str, dict[str, Any]]]:
    """The CV as stand-in line IDs and texts (masked for DeepSeek), the skills lines by
    stand-in (fact ID and text), and bullet entries by ID."""
    texts = {
        line["fact_id"]: line.get("source_text") or line["text"]
        for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]
    }
    out, _ = stand_ins(texts)
    layout = original_layout(draft)
    resume = [
        {"section": section["kind"], "entries": [
            {"entry": entry["entry"], "lines": [{"id": out[fact_id], "text": mask(texts[fact_id], private)}
                                                for fact_id in entry["lines"]]}
            for entry in section["entries"]
        ]}
        for section in layout
    ]
    skills = {
        out[fact_id]: (fact_id, texts[fact_id])
        for section in layout if section["kind"] == "skills"
        for entry in section["entries"] for fact_id in entry["lines"]
    }
    entries = {}
    for i, section in enumerate(draft["sections"]):
        if section["kind"] in BULLET_TYPES:
            for j, entry in enumerate(section["entries"]):
                entries[f"s{i}e{j}"] = {"kind": section["kind"], "where": entry.get("title") or section["title"],
                                        "key": _entry_key(section["kind"], entry),
                                        "beside": [line.get("source_text") or line["text"] for line in entry["lines"]]}
    return resume, skills, entries


def _existing_lines(draft: dict[str, Any]) -> list[str]:
    """Every line the CV holds, in its source and rewritten forms."""
    return [
        text for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]
        for text in (line["text"], line.get("source_text")) if text
    ]


def _suggestion(
    item: Any, skills: dict[str, tuple[str, str]], entries: dict[str, dict[str, Any]], existing: list[str]
) -> dict[str, Any] | None:
    """Validate destination and output shape; candidate authenticity is not a filter."""
    if not isinstance(item, dict):
        return None
    if item.get("kind") == "skill" and isinstance(item.get("line"), str) and item["line"] in skills:
        fact_id, line = skills[item["line"]]
        items: list[str] = []
        for value in item.get("items") if isinstance(item.get("items"), list) else []:
            if not isinstance(value, str):
                continue
            value = " ".join(value.split())
            if (value and len(value) <= MAX_ITEM_CHARACTERS and len(value.split()) <= 4
                    and "[[" not in value and "]]" not in value
                    and not any(tag_pattern(value).search(known) for known in existing)
                    and value.casefold() not in {existing.casefold() for existing in items}):
                items.append(value)
        if not items:
            return None
        items = items[:MAX_ITEMS]
        return {"kind": "skill", "fact_id": fact_id, "items": items, "where": line, "new_text": _joined(line, items)}
    if item.get("kind") == "bullet" and isinstance(item.get("entry"), str) and item["entry"] in entries:
        text = " ".join(item.get("text").split()) if isinstance(item.get("text"), str) else ""
        entry = entries[item["entry"]]
        if (not text or len(text) > MAX_LINE_CHARACTERS or LINK.search(text)
                or "[[" in text or "]]" in text
                or any(text.casefold() == known.casefold() for known in existing)):
            return None
        tags = [
            tag.strip() for tag in (item.get("tags") if isinstance(item.get("tags"), list) else [])
            if isinstance(tag, str) and tag.strip() and tag_pattern(tag.strip()).search(text)
        ]
        # The lines already under the entry go with it, so a reworded one is plain to see.
        return {"kind": "bullet", "entry": item["entry"], "entry_key": entry["key"], "fact_type": BULLET_TYPES[entry["kind"]],
                "text": text, "tags": list(dict.fromkeys(tags))[:MAX_TAGS], "where": entry["where"], "beside": entry["beside"]}
    return None


def _header(entry: dict[str, Any]) -> str | None:
    """An entry's role or degree with its dates, as evidence; never its name, which is a school,
    an employer or a project."""
    subtitle = entry.get("subtitle")
    if not subtitle:
        return None
    return f"{subtitle} ({entry['dates']})" if entry.get("dates") else subtitle


def _item_key(item: dict[str, Any]) -> str:
    """What was judged: the words and, for a line on the CV, the entry it sits in, since the
    years a line covers come from that entry's dates."""
    if item["kind"] == "entry":
        return json.dumps(["entry", item["entry_key"], item["text"]], ensure_ascii=False)
    return json.dumps(["fact", item["fact_id"], item["fact_version"], item["text"], item.get("entry_key")],
                      ensure_ascii=False)


Pair = tuple[dict[str, Any], dict[str, Any] | None]


def _cv_parts(cv: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None, list[Pair]]]:
    """This CV entry by entry: its section, its role or degree with dates (if any), and each line
    in its saved words with, if reworded, its new words."""
    parts = []
    for section in cv["sections"]:
        for entry in section["entries"]:
            key = _entry_key(section["kind"], entry)
            header = _header(entry)
            role = {"kind": "entry", "section": section["kind"], "entry_key": key, "text": header} if header else None
            lines: list[Pair] = []
            for line in entry["lines"]:
                fact = {"kind": "fact", "fact_id": line["fact_id"], "fact_version": line["fact_version"], "entry_key": key}
                source = line.get("source_text") or line["text"]
                lines.append(({**fact, "text": source}, {**fact, "text": line["text"]} if line["text"] != source else None))
            parts.append((section["kind"], role, lines))
    return parts


def _cv_items(cv: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything this CV can show, whether or not it is shown now."""
    return [item for _, role, lines in _cv_parts(cv) for item in (role, *(item for pair in lines for item in pair)) if item]


def _evidence(
    cv: dict[str, Any], confirmed: list[dict[str, Any]], private: list[str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """What to judge, under stand-in IDs; the request's resume, entry by entry (an entry is named
    only by a stand-in, since its name is a school, an employer or a project); and the saved
    facts not on this CV."""
    items: dict[str, dict[str, Any]] = {}
    refs: dict[str, str] = {}

    def sent(item: dict[str, Any], **extra: str) -> dict[str, str]:
        key = _item_key(item)
        if key not in refs:
            refs[key] = f"E{len(refs) + 1}"
            items[refs[key]] = item
        return {"id": refs[key], "text": mask(item["text"], private), **extra}

    resume = []
    for number, (section, role, pairs) in enumerate(_cv_parts(cv), 1):
        lines = []
        for source, rewording in pairs:
            lines.append(sent(source))
            if rewording:
                lines.append(sent(rewording, rewording_of=lines[-1]["id"]))
        resume.append({"entry": f"J{number}", "section": section, **({"role": sent(role)} if role else {}), "lines": lines})
    on_cv = {(item["fact_id"], item["fact_version"], item["text"]) for item in items.values() if item["kind"] == "fact"}
    other = [
        sent({"kind": "fact", "fact_id": fact["id"], "fact_version": fact["version"], "text": fact["text"], "entry_key": None})
        for fact in confirmed if (fact["id"], fact["version"], fact["text"]) not in on_cv
    ]
    return items, resume, other


def _verdicts(content: Any, wanted: set[str], items: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each requirement's verdict with the lines behind it. A requirement the answer leaves
    out, or answers without a usable line, stays unchecked."""
    picks = content.get("requirements") if isinstance(content, dict) else None
    if not isinstance(picks, list):
        raise ValueError("DeepSeek 的回答缺少 requirements 数组")
    verdicts: dict[str, dict[str, Any]] = {}
    for pick in picks:
        requirement = pick.get("id") if isinstance(pick, dict) else None
        if not isinstance(requirement, str) or requirement not in wanted or requirement in verdicts:
            continue
        verdict = pick.get("verdict")
        if verdict == "supported":
            sets = []
            for group in pick.get("sets") if isinstance(pick.get("sets"), list) else []:
                # A group only counts whole: a shortened one would claim less than it needs.
                if isinstance(group, list) and group and all(isinstance(ref, str) and ref in items for ref in group):
                    group = list(dict.fromkeys(group))
                    if len(group) <= MAX_SET_LINES:
                        sets.append(group)
            if sets:
                verdicts[requirement] = {"evidence": "supported", "sets": sets[:MAX_SETS]}
        elif verdict == "related":
            named = pick.get("lines") if isinstance(pick.get("lines"), list) else []
            lines = list(dict.fromkeys(ref for ref in named if isinstance(ref, str) and ref in items))[:MAX_RELATED]
            missing = " ".join(pick["missing"].split())[:MAX_MISSING_CHARACTERS] if isinstance(pick.get("missing"), str) else ""
            if lines:
                verdicts[requirement] = {"evidence": "related", "related": lines, "missing": missing or None}
        elif verdict == "none":
            verdicts[requirement] = {"evidence": "none"}
    return verdicts


def _suggest(
    decided: dict[str, Any], cv: dict[str, Any], gaps: list[dict[str, Any]], chat: Callable[..., dict[str, Any]],
    effort: str, private: list[str], *, strict: bool = False,
) -> dict[str, Any]:
    """Ask for one complete line per gap and validate its format and destination; returns how it went."""
    resume, skills, entries = _resume(cv, private)
    request = {
        "job_title": decided["jd"].get("title"),
        "language": cv["language"],
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
    if strict and (not isinstance(picks, list) or not all(
            any(isinstance(pick, dict) and pick.get('requirement') == gap['requirement_id']
                and pick.get('kind') in ('skill', 'bullet') for pick in picks) for gap in gaps)):
        if suggesting.get('fallback_reason'):
            raise DeepSeekError(suggesting['fallback_reason'], reason=suggesting.get('fallback_code', 'bad_response'))
        raise DeepSeekError('DeepSeek returned no usable suggestion response.')
    for pick in picks if isinstance(picks, list) else []:
        requirement = pick.get("requirement") if isinstance(pick, dict) else None
        # The first usable pick: one dropped (skills the CV already lists) leaves room for the next.
        if isinstance(requirement, str) and not by_requirement.get(requirement):
            by_requirement[requirement] = _suggestion(pick, skills, entries, _existing_lines(cv))
    for gap in gaps:
        gap["suggestion"] = by_requirement.get(gap["requirement_id"])
        if strict and not gap["suggestion"]:
            raise DeepSeekError("DeepSeek did not return a complete suggested entry. Try again.")
    return suggesting


def _suggestion_key(requirement: str, suggestion: dict[str, Any]) -> str:
    """What a suggestion claims: skills wherever they would be listed, or a line under one entry."""
    if suggestion.get("kind") == "skill":
        claim = sorted(item.casefold() for item in suggestion.get("items") or [] if isinstance(item, str))
    else:
        claim = [suggestion.get("text"), suggestion.get("entry_key") or suggestion.get("where")]
    return json.dumps([requirement, suggestion.get("kind"), claim], ensure_ascii=False)


def suggestion_target(gaps: dict, decided: dict, cv: dict, requirement_id: str,
                      requirements_version: int | None = None) -> dict:
    """Validate the saved check before reusing or requesting a row's suggestion: still usable
    for this CV's language and the job's current requirements (applies), and a row the check
    found missing or only related. The CV may have changed since (another line adopted), so one
    adoption never forces a new check: a suggestion is asked with the CV as it is now, and the
    page says the row awaits a new check."""
    if unusable(gaps, cv['language'], decided, requirements_version):
        raise ValueError('The CV, facts or requirements changed. Check again before requesting a suggestion.')
    item = _gap(gaps, requirement_id)
    if item['evidence'] not in ('related', 'none'):
        raise ValueError('Only a missing or related requirement needs a suggested entry.')
    return item


def suggest_gap(gaps: dict, decided: dict, cv: dict, facts_db: Any, requirement_id: str,
                chat: Callable[..., dict], private: Iterable[str] = (), requirements_version: int | None = None) -> dict:
    """Generate complete copy for one row; reuse full proposals but retry old empty/templates.
    The returned check contains proposals only; no saved material or CV is changed. The request
    carries only the CV and the requirement; ``facts_db`` stays for the callers' signature."""
    updated = copy.deepcopy(gaps)
    item = suggestion_target(updated, decided, cv, requirement_id, requirements_version)
    if item.get('suggestion') or item['status'] in ('added', 'declined'):
        return updated
    private = sorted({*private_terms(cv), *private}, key=len, reverse=True)
    result = _suggest(decided, cv, [item], chat, 'none', private, strict=True)
    if result.get('fallback_reason'):
        # No cached success on a transport or quota failure: the user's next click can retry.
        raise DeepSeekError(result['fallback_reason'], reason=result.get('fallback_code', 'bad_response'))
    if item['suggestion'] and _suggestion_key(item['text'], item['suggestion']) in _declined(gaps):
        raise DeepSeekError("This suggestion was previously declined. Try a different entry.")
    item['ai_checked'] = True
    item.pop('ai_needs_details', None)
    item.pop('guidance', None)
    return updated


def _declined(previous: Any) -> set[str]:
    """What the user skipped in an earlier check, by requirement and claim; kept even
    through a check that failed and so suggested nothing."""
    if not isinstance(previous, dict):
        return set()
    records = previous.get("requirements") or previous.get("gaps") or []
    kept = {key for key in previous.get("declined") or [] if isinstance(key, str)}
    return kept | {
        _suggestion_key(item["text"], item["suggestion"]) for item in records
        if isinstance(item, dict) and item.get("status") == "declined" and isinstance(item.get("suggestion"), dict)
    }


def find_gaps(
    decided: dict[str, Any],
    cv: dict[str, Any],
    facts_db: Path,
    chat: Callable[..., dict[str, Any]],
    effort: str = "none",
    private: Iterable[str] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every requirement with DeepSeek's verdict on the lines behind it, and for those nothing
    shows, at most one checked suggestion. ``cv`` is this job's CV at any stage; ``private``
    adds words requests must mask to those of the CV itself. A suggestion declined in the
    ``previous`` check stays declined. ``facts_db`` is the single-user database or a fact
    source (facts.fact_source)."""
    private = sorted({*private_terms(cv), *(private or ())}, key=len, reverse=True)
    confirmed = list(fact_source(facts_db).listed())
    items, resume, other = _evidence(cv, confirmed, private)
    requirements = [
        {"requirement_id": item["id"], "text": item["text"], "strength": item.get("strength") or "unclear",
         "evidence": "unchecked", "sets": [], "related": [], "missing": None, "suggestion": None, "status": "open"}
        for item in decided["selected_requirements"]
    ]
    request = {
        "requirements": [{"id": item["requirement_id"], "text": item["text"], "strength": item["strength"]}
                         for item in requirements],
        "resume": resume,
        "other_facts": other,
    }
    messages = [
        {"role": "system", "content": EVIDENCE_RULES},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]
    try:
        answer = chat(messages, model=DEFAULT_MODEL, effort=effort)
        verdicts = _verdicts(answer.get("content"), {item["requirement_id"] for item in requirements}, items)
        checking = {"model": answer.get("model"), "usage": answer.get("usage")}
    except (DeepSeekError, ValueError) as exc:
        verdicts, checking = {}, {"fallback_reason": str(exc), "fallback_code": getattr(exc, "reason", "bad_response")}
    for item in requirements:
        item.update(verdicts.get(item["requirement_id"], {}))
    gaps = [item for item in requirements if item["evidence"] in ("related", "none")]
    suggesting = _suggest(decided, cv, gaps, chat, effort, private) if gaps else {}
    declined = _declined(previous)
    for gap in gaps:
        if gap["suggestion"] and _suggestion_key(gap["text"], gap["suggestion"]) in declined:
            gap["status"] = "declined"
    return {
        "gaps_version": GAPS_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "language": cv["language"],
        "checked_facts": sorted([fact["id"], fact["version"]] for fact in confirmed),
        "checked_cv": sorted({_item_key(item) for item in _cv_items(cv)}),
        "declined": sorted(declined),
        "evidence_check": checking,
        "suggesting": suggesting,
        "items": items,
        "requirements": requirements,
    }


STATUSES = ("not_shown", "related", "none", "unchecked", "recheck", "shown")
# How a saved check stands now (check_state), from usable as it is to not usable at all.
CHECK_STATES = ("current", "cv_changed", "unbound", "requirements_changed", "outdated")
# What every row of a check in this format holds (find_gaps).
ROW_KEYS = frozenset({"requirement_id", "text", "strength", "evidence", "sets", "related", "missing",
                      "suggestion", "status"})


def judged_requirements(decided: dict[str, Any] | None) -> list[tuple[str, str, str]]:
    """The requirements a check of ``decided`` judges: each selected one's ID, text and strength,
    in order, as find_gaps records them in its rows."""
    return [(item["id"], item["text"], item.get("strength") or "unclear")
            for item in ((decided or {}).get("selected_requirements") or [])]


def _bound_version(gaps: dict[str, Any]) -> Any:
    """The requirements version a check was made from, if it was saved with one (V2 records it
    in ``inputs`` when it keeps the check)."""
    inputs = gaps.get("inputs")
    return inputs.get("requirements_version") if isinstance(inputs, dict) else None


def unusable(gaps: Any, language: str | None, decided: dict[str, Any] | None,
             requirements_version: int | None = None) -> str | None:
    """Why a saved check cannot be used at all for a CV in ``language`` and the job's current
    requirements (``decided``; ``requirements_version`` where requirements are versioned), or None.

    "outdated": another format or rules version (GAPS_VERSION), another language, or a record
    missing what it is read by. "requirements_changed": the requirements it judged are not the
    current ones (one added, removed or reworded), or it names the requirements version it read
    and they were saved again since, even in the same words. Neither is ever read as a result for
    the job now; the record itself stays."""
    if not isinstance(gaps, dict) or gaps.get("gaps_version") != GAPS_VERSION or gaps.get("language") != language:
        return "outdated"
    rows = gaps.get("requirements")
    if not (isinstance(rows, list) and isinstance(gaps.get("items"), dict) and isinstance(gaps.get("checked_facts"), list)
            and isinstance(gaps.get("checked_cv"), list)
            and all(isinstance(row, dict) and ROW_KEYS <= row.keys() for row in rows)):
        return "outdated"
    bound = _bound_version(gaps)
    if ([(row["requirement_id"], row["text"], row["strength"]) for row in rows] != judged_requirements(decided)
            or (requirements_version is not None and bound is not None and bound != requirements_version)):
        return "requirements_changed"
    return None


def check_state(gaps: Any, cv: dict[str, Any], confirmed: Iterable[tuple[str, int]], decided: dict[str, Any] | None,
                requirements_version: int | None = None) -> str:
    """How a saved check stands for this CV, the current facts (``confirmed``, all (ID, version)
    pairs) and the job's requirements now: the one rule that showing a check, asking for a
    suggestion and adopting or skipping one share (CHECK_STATES).

    - "outdated" or "requirements_changed" (see unusable): nothing it found is shown as a result
      and nothing is adopted from it until the job is checked again.
    - "cv_changed": the CV's lines or the facts changed since (an adopted line, say): its rows are
      to check again, and its suggestions stay usable.
    - "unbound": the same requirements in the same words, but saved before checks recorded the
      requirements version they read (only where ``requirements_version`` is given), so it cannot
      show it was made from the current one: to check again like "cv_changed", never current.
    - "current": none of these."""
    reason = unusable(gaps, cv.get("language"), decided, requirements_version)
    if reason:
        return reason
    if (sorted([fact_id, version] for fact_id, version in confirmed) != gaps["checked_facts"]
            or sorted({_item_key(item) for item in _cv_items(cv)}) != gaps["checked_cv"]):
        return "cv_changed"
    if requirements_version is not None and _bound_version(gaps) is None:
        return "unbound"
    return "current"


def _shown_now(cv: dict[str, Any]) -> tuple[dict[tuple[str, int, str], str], dict[str, str]]:
    """What the CV shows now, after cuts and undone changes: the words of each fact version in
    its entry, and each entry's role or degree with dates."""
    lines: dict[tuple[str, int, str], str] = {}
    entries: dict[str, str] = {}
    for section in shown_sections(cv):
        for entry in section["entries"]:
            key = _entry_key(section["kind"], entry)
            header = _header(entry)
            if header:
                entries[key] = header
            for line in entry["lines"]:
                lines[(line["fact_id"], line["fact_version"], key)] = line["text"]
    return lines, entries


def _is_shown(item: dict[str, Any], lines: dict[tuple[str, int, str], str], entries: dict[str, str]) -> bool:
    if item["kind"] == "entry":
        return entries.get(item["entry_key"]) == item["text"]
    return lines.get((item["fact_id"], item["fact_version"], item.get("entry_key"))) == item["text"]


def _undoable(cv: dict[str, Any]) -> set[str]:
    """Changes the user can undo now: the plan's cuts and DeepSeek's rewordings."""
    plan = cv.get("plan")
    if not plan or "approval" in cv:
        return set()
    rewordings = {
        f"reword:{line['fact_id']}" for section in cv["sections"] for entry in section["entries"]
        for line in entry["lines"] if line.get("tailoring", {}).get("status") == "accepted"
    }
    return ({change["id"] for change in plan["changes"]} | rewordings) - set(plan["undone"])


def _left_out(
    item: dict[str, Any], cv: dict[str, Any], lines: dict[tuple[str, int, str], str], undoable: set[str]
) -> tuple[str, list[str]]:
    """Why the CV does not show an item, and the changes that would show it again in the words
    that were judged, when they can be undone."""
    rewording = f"reword:{item.get('fact_id')}"
    if item["kind"] == "fact" and (item["fact_id"], item["fact_version"], item.get("entry_key")) in lines:
        return "reworded", [rewording] if rewording in undoable else []
    for index, section in enumerate(cv["sections"]):
        for position, entry in enumerate(section["entries"]):
            if _entry_key(section["kind"], entry) != item["entry_key"]:
                continue
            cuts = [f"cut:{section['kind']}", f"cut:s{index}e{position}"]
            words: list[str] = []
            if item["kind"] == "entry":
                if _header(entry) != item["text"]:
                    continue
            else:
                line = next((line for line in entry["lines"]
                             if (line["fact_id"], line["fact_version"]) == (item["fact_id"], item["fact_version"])), None)
                if line is None:
                    continue
                cuts.append(f"cut:{item['fact_id']}")
                source = line.get("source_text") or line["text"]
                comes_back_reworded = line["text"] != source and rewording in undoable
                if item["text"] == source and comes_back_reworded:
                    words = [rewording]  # put back the saved words that show it
                elif item["text"] != source and not comes_back_reworded:
                    return "cut", []  # only the undone rewording shows it; undoing the cut would not
            cut = next((change for change in cuts if change in undoable), None)
            return "cut", [cut, *words] if cut else []
    return "not_on_cv", []


def coverage(gaps: dict[str, Any], cv: dict[str, Any], confirmed: Iterable[tuple[str, int]],
             decided: dict[str, Any] | None, requirements_version: int | None = None) -> dict[str, Any]:
    """What this CV shows for each requirement, from the CV as it is shown now (so undoing a
    cut changes it at once) and the verdicts found by ``find_gaps``, as far as the check still
    stands (``state``, see check_state: ``decided`` and ``requirements_version`` are the job's
    current requirements).

    An outdated check gives only ``outdated``. A check of other requirements gives no rows at all
    (``requirements_changed``): none of its verdicts is a result for the requirements now.
    Otherwise a requirement is "shown" when a whole group of lines that shows it is on the CV in
    the words and entries that were judged, "not_shown" when its evidence is cut, reworded or not
    on this CV (with the changes to undo to show it), and otherwise "related", "none" or
    "unchecked" as judged. A ``stale`` check (any state but "current") never judged the CV's new
    words or lines, or cannot show it judged these requirements, so evidence it no longer finds
    word for word, and its "related" and "none" verdicts, are "recheck" (why "changed", nothing
    to undo), never left out or missing: the CV may show it now. ``verdict`` keeps what that
    check found.
    """
    state = check_state(gaps, cv, confirmed, decided, requirements_version)
    if state == "outdated":
        return {"state": state, "outdated": True}
    if state == "requirements_changed":
        return {"state": state, "stale": True, "requirements_changed": True,
                "counts": {name: 0 for name in STATUSES}, "requirements": []}
    lines, entries = _shown_now(cv)
    items = gaps["items"]
    # Cuts, reordering and undos only change which of the judged words are shown.
    stale = state != "current"
    undoable = _undoable(cv)

    def described(ref: str) -> dict[str, Any]:
        item = items[ref]
        if _is_shown(item, lines, entries):
            return {"text": item["text"], "shown": True, "why": None, "undo": []}
        if stale:
            return {"text": item["text"], "shown": False, "why": "changed", "undo": []}
        why, undo = _left_out(item, cv, lines, undoable)
        return {"text": item["text"], "shown": False, "why": why, "undo": undo}

    requirements = []
    for record in gaps["requirements"]:
        status, refs = record["evidence"], []
        if status == "supported":
            shown = [[_is_shown(items[ref], lines, entries) for ref in group] for group in record["sets"]]
            whole = next((group for group, flags in zip(record["sets"], shown) if all(flags)), None)
            # When no group is whole, explain the one closest to being shown.
            status, refs = ("shown", whole) if whole else (
                "recheck" if stale else "not_shown", max(zip(record["sets"], shown), key=lambda pair: sum(pair[1]))[0])
        elif status == "related":
            refs = record["related"]
        if stale and status in ("related", "none"):
            status = "recheck"
        requirements.append({
            "requirement_id": record["requirement_id"], "text": record["text"], "strength": record["strength"],
            "status": status, "verdict": record["evidence"],
            "evidence": [described(ref) for ref in refs], "missing": record["missing"],
            "suggestion": record["suggestion"], "suggestion_status": record["status"],
            "ai_checked": bool(record.get("ai_checked") and record.get("suggestion")),
        })
    return {
        "state": state,
        "stale": stale,
        "counts": {name: sum(item["status"] == name for item in requirements) for name in STATUSES},
        "requirements": requirements,
    }


def _gap(gaps: dict[str, Any], requirement_id: str) -> dict[str, Any]:
    gap = next((item for item in gaps["requirements"] if item["requirement_id"] == requirement_id), None)
    if gap is None:
        raise ValueError(f"没有这条缺口：{requirement_id}")
    return gap


def accept_gap(
    gaps: dict[str, Any], requirement_id: str, facts_db: Path, profile: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The user adopts the suggestion: save it using the legacy confirmed status.

    A skill becomes a new confirmed version of that skills line, already on the CV. A bullet
    becomes a new confirmed fact and a new profile (returned) lists it under its entry; the
    caller's profile is not changed in place.

    A suggestion applies only to what it was made for: the skills line with the same text, or
    the entry with the same name, role, place and dates wherever it now sits. Every step
    can be repeated, so if saving stops before the caller records the gap as added, accepting
    it again finishes the job without adding anything twice; accepting a suggestion recorded as
    added writes nothing and succeeds, so a retry after a stop can finish (the caller then
    prepares the CV again). ``facts_db`` is the single-user database or a fact source whose
    writes the caller commits together with the returned gaps and profile.
    """
    facts = fact_source(facts_db)
    updated = copy.deepcopy(gaps)
    gap = _gap(updated, requirement_id)
    if gap["status"] == "declined":
        raise ValueError("这条建议已被跳过；如需添加可自行编辑内容")
    if gap["status"] == "added":
        return updated, None
    suggestion = gap["suggestion"]
    if not suggestion:
        raise ValueError("这条要求没有可添加的建议")
    new_profile = None
    if suggestion["kind"] == "skill":
        fact_id = suggestion["fact_id"]
        current = facts.current([fact_id]).get(fact_id)
        if current is not None and current["text"] == suggestion["new_text"]:
            pass  # Already saved; the legacy confirmation flag is not a prerequisite.
        elif current is not None and current["text"] == suggestion["where"]:
            facts.revise_confirmed(fact_id, suggestion["new_text"], [*current["tags"], *suggestion["items"]])
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
        # Added before, whatever skill words it was stored with (a retry after a save that
        # stopped halfway may no longer have them): nothing more to write.
        listed = profile["sections"][section]["entries"][entry].get("facts") or []
        there = facts.current(listed)
        before = next((fact_id for fact_id in listed if (there.get(fact_id) or {}).get("text") == suggestion["text"]), None)
        if before is not None:
            gap.update(status="added", fact_id=before)
            return updated, None
        elsewhere = {
            known for i, other in enumerate(profile["sections"]) for j, listed in enumerate(other.get("entries", []))
            if (i, j) != (section, entry) for known in listed.get("facts") or []
        }
        # The fact add_fact would return for this line, if it exists already: checked before
        # anything is written, so a refused acceptance changes nothing.
        tags = sorted({tag.casefold() for tag in suggestion["tags"]})
        same = {fact["id"] for fact in facts.listed() if fact["text"] == suggestion["text"]
                and fact["fact_type"] == suggestion["fact_type"] and sorted({tag.casefold() for tag in fact["tags"]}) == tags}
        if same & elsewhere:
            raise ValueError(f"这一行已在简历的其他条目下，不在 {suggestion['where']}；请重新检查缺口")
        fact_id = facts.add_confirmed(suggestion["text"], suggestion["fact_type"], suggestion["tags"])["id"]
        if fact_id not in (profile["sections"][section]["entries"][entry].get("facts") or []):
            new_profile = copy.deepcopy(profile)
            new_profile["sections"][section]["entries"][entry].setdefault("facts", []).append(fact_id)
    gap.update(status="added", fact_id=fact_id)
    return updated, new_profile


SKILL_SEPARATORS = re.compile(r"[,，、;；]")


def _skill_items(line: str) -> str:
    """The items of a skills line, after its label (Languages: … or 编程语言：…)."""
    return re.split(r"[:：]", line, maxsplit=1)[-1]


def _joined(line: str, items: list[str]) -> str:
    """A skills line with items added in its own style: 、 or ， in a Chinese line."""
    listed = _skill_items(line)
    separator = "、" if "、" in listed else "，" if "，" in listed else ", "
    return separator.join([line, *items])


def places(cv: dict[str, Any]) -> list[dict[str, Any]]:
    """Where the user can add a line of their own: items for one of the skills lines, or a new
    line under an experience or project entry. An entry is named by what it shows, so a line
    can never land under another entry after the layout changes."""
    found = []
    for section in cv["sections"]:
        for entry in section["entries"]:
            if section["kind"] == "skills":
                found += [{"id": f"skill:{line['fact_id']}", "kind": "skill", "fact_id": line["fact_id"],
                           "where": line.get("source_text") or line["text"]} for line in entry["lines"]]
            elif section["kind"] in BULLET_TYPES:
                key = _entry_key(section["kind"], entry)
                found.append({"id": f"entry:{key}", "kind": "bullet", "entry_key": key,
                              "fact_type": BULLET_TYPES[section["kind"]], "where": entry.get("title") or section["title"]})
    return found


def addition(suggestion: dict[str, Any]) -> dict[str, str]:
    """What adding ``suggestion`` puts on the CV, however it was asked for (accepted, or written
    by the user): the line or entry it goes to and the text there. Two requests add the same
    thing exactly when these are equal; tags and where it was shown do not matter."""
    if suggestion["kind"] == "skill":
        return {"where": f"skill:{suggestion['fact_id']}", "text": suggestion["new_text"]}
    return {"where": f"entry:{suggestion['entry_key']}", "text": suggestion["text"]}


def suggested_addition(gaps: dict[str, Any], requirement_id: str) -> dict[str, str]:
    """What accepting the requirement's suggestion adds (see addition); refused when there is none."""
    suggestion = _gap(gaps, requirement_id)["suggestion"]
    if not suggestion:
        raise ValueError("这条要求没有可添加的建议")
    return addition(suggestion)


def line_to_write(gaps: dict[str, Any], requirement_id: str, place_id: str, text: str, cv: dict[str, Any]) -> dict[str, Any]:
    """The suggestion a line the user writes makes: skills for one of the skills lines, or a new
    line under an entry. It is refused when empty, too long, already on the CV, or when its
    place is gone; nothing has been written by then."""
    place = next((item for item in places(cv) if item["id"] == place_id), None)
    if place is None:
        raise ValueError("这个位置已不在简历中，请刷新页面后重试")
    text = " ".join(text.split()) if isinstance(text, str) else ""
    if '[[' in text or ']]' in text:
        raise ValueError('Replace every [[placeholder]] with your own details before adding this line.')
    gap = _gap(gaps, requirement_id)
    if place["kind"] == "skill":
        known = {item.strip().casefold() for item in SKILL_SEPARATORS.split(_skill_items(place["where"]))}
        items: list[str] = []
        for item in (part.strip() for part in SKILL_SEPARATORS.split(text)):
            if item and item.casefold() not in known:
                known.add(item.casefold())
                items.append(item)
        if not items:
            raise ValueError("请写要加入这一行的技能（用逗号分隔），不要与这一行已有的重复")
        if len(items) > MAX_ITEMS or any(len(item) > MAX_ITEM_CHARACTERS for item in items):
            raise ValueError(f"一次最多加 {MAX_ITEMS} 项，每项最多 {MAX_ITEM_CHARACTERS} 个字符")
        suggestion = {"kind": "skill", "fact_id": place["fact_id"], "items": items, "where": place["where"],
                      "new_text": _joined(place["where"], items)}
    else:
        if not text:
            raise ValueError("请写要加入简历的一行")
        if len(text) > MAX_LINE_CHARACTERS:
            raise ValueError(f"一行最多 {MAX_LINE_CHARACTERS} 个字符")
        if any(text.casefold() == known.casefold() for known in _existing_lines(cv)):
            raise ValueError("简历中已经有这一行")
        # Skill words from the suggestion it replaces are kept if the user's line still names them.
        named = [tag for tag in ((gap.get("suggestion") or {}).get("tags") or []) if tag_pattern(tag).search(text)]
        suggestion = {"kind": "bullet", "entry_key": place["entry_key"], "fact_type": place["fact_type"],
                      "text": text, "tags": named, "where": place["where"]}
    return suggestion


def write_line(
    gaps: dict[str, Any], requirement_id: str, place_id: str, text: str, cv: dict[str, Any],
    facts_db: Path, profile: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The user writes what shows a requirement themselves (see line_to_write), even after
    skipping a suggestion. Their adopted text is saved using the legacy confirmed status as
    written, numbers included. Adding works as for a suggestion (see accept_gap), with the same
    protection against a changed line or entry."""
    suggestion = line_to_write(gaps, requirement_id, place_id, text, cv)
    gap = _gap(gaps, requirement_id)
    if gap["status"] == "added":
        done = gap.get("suggestion") or {}
        if done.get("written") and all(done.get(key) == value for key, value in suggestion.items()):
            return copy.deepcopy(gaps), None  # the same line again, after a stop: saved already
        raise ValueError("这条要求已经添加过一行")
    updated = copy.deepcopy(gaps)
    _gap(updated, requirement_id).update(suggestion={**suggestion, "written": True}, status="open")
    return accept_gap(updated, requirement_id, facts_db, profile)


def decline_gap(gaps: dict[str, Any], requirement_id: str) -> dict[str, Any]:
    """The user skips the suggestion: leave it off the CV."""
    updated = copy.deepcopy(gaps)
    gap = _gap(updated, requirement_id)
    if gap["status"] == "added":
        raise ValueError("这条建议已经添加；如需撤回请在事实库中修改")
    gap["status"] = "declined"
    return updated
