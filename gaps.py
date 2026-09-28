"""What each of a job's requirements has behind it: evidence the CV shows, evidence it leaves
out, related lines only, or nothing; with honest suggestions for what is missing.

DeepSeek judges each requirement against the user's confirmed facts, the words this CV shows
(a reworded line is judged as reworded) and each entry's role or degree with its dates: a
line that is only about the same thing never counts as showing it. What the CV shows is
worked out later, from the CV as it is then (see ``coverage``), so undoing a cut changes it
at once. Without an answer a requirement stays unchecked; a matching skill word never stands
in for one.

For requirements nothing shows, DeepSeek may suggest one line the user could add if it is
true: items for an existing skills line, or a bullet under an existing entry. Nothing reaches
the CV until the user says the line is true; it then becomes a confirmed fact like any other.
Only requirement texts, line texts and roles or degrees with dates are sent, under stand-in
IDs, never names, schools, employers or fact IDs.
"""

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from claims import LINK, NUMBER, _leadership
from cv import CVError, _localized
from cv_layout import original_layout, shown_sections
from deepseek_client import DEFAULT_MODEL, DeepSeekError
from facts import add_fact, confirm_fact, list_facts, load_current_facts, revise_fact, tag_pattern
from privacy import mask, private_terms, stand_ins


GAPS_VERSION = 3
MAX_SETS = 3
MAX_SET_LINES = 3
MAX_RELATED = 3
MAX_MISSING_CHARACTERS = 120
EVIDENCE_RULES = """You check which lines of an applicant's resume show that they meet each of a job's
requirements, and reply in json only. The resume comes entry by entry (a job, school or project):
its "role" is the role or degree with dates, then its lines; a line may be a rewording of another
line of the same entry. "other_facts" are the applicant's confirmed facts not on this resume.
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
its own entry's dates state them.
Reply as {"requirements": [{"id": "<requirement id>", "verdict": "supported", "sets": [["E1"]]},
{"id": "<requirement id>", "verdict": "related", "lines": ["E2"], "missing": "..."},
{"id": "<requirement id>", "verdict": "none"}]}"""
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
- "skill": items to append to one existing skills line whose label fits them (give that line's id),
  such as a tool name; never put an item into a line whose label it does not fit, such as a concept
  into a list of programming languages.
- "bullet": one new bullet under an existing experience or project entry (give the entry id), only
  when that entry's own lines already show closely related work, so the new line adds a tool or
  practice to that same work. Write it like the other bullets there, describing plain hands-on use.
- "none": when no honest addition fits: years of experience, seniority, leadership, a degree, work
  authorization or personal traits; experience that needs a project of its own, such as a model
  architecture, a research area or a field like autonomous driving; or a gap that is only about how
  an existing line is worded.
Never restate or reword a line the resume already has, never copy the requirement's own wording,
and never invent a model, system, feature or result. Never include numbers, metrics, results, team
sizes or leadership words, and never claim more than basic hands-on use. Give "tags": the skill
words the bullet names.
Reply as {"suggestions": [{"requirement": "<gap id>", "kind": "skill", "line": "<line id>",
"items": ["..."]}, {"requirement": "<gap id>", "kind": "bullet", "entry": "<entry id>",
"text": "...", "tags": ["..."]}, {"requirement": "<gap id>", "kind": "none"}]}"""


# Words that say nothing about what a line is about, left out when comparing lines.
GENERIC_WORDS = frozenset("""
with from that this these those using used into onto over under their they them have been were which while
through across within including based work worked working build built develop developed implement implemented
design designed added create created make made wrote write writing support supported supporting team teams
project projects other also more most such each both than then when where what will would could should about
after before between during without help helped
""".split())
NEAR_COPY = 0.6  # share of a new line's words already in one existing line


def _words(text: str) -> set[str]:
    """The words that say what a line is about, lightly stemmed; Chinese as character pairs."""
    words = set()
    for word in re.findall(r"[^\W_]+", text.casefold()):
        if re.search(r"[\u4e00-\u9fff]", word):
            words.update(word[index:index + 2] for index in range(len(word) - 1))
            continue
        stem = word
        for suffix in ("ing", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                stem = word[:-len(suffix)]
                break
        if len(stem) >= 4 and word not in GENERIC_WORDS and stem not in GENERIC_WORDS:
            words.add(stem)
    return words


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
    stand-in (fact ID and text), and bullet entries by ID with the words of what they show."""
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
                shown = [entry.get("title") or "", entry.get("subtitle") or "",
                         *(text for line in entry["lines"] for text in (line["text"], line.get("source_text") or ""))]
                entries[f"s{i}e{j}"] = {"kind": section["kind"], "where": entry.get("title") or section["title"],
                                        "key": _entry_key(section["kind"], entry), "words": _words(" ".join(shown))}
    return resume, skills, entries


def _existing_lines(draft: dict[str, Any]) -> list[str]:
    """Every line the CV holds, as confirmed and as reworded."""
    return [
        text for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]
        for text in (line["text"], line.get("source_text")) if text
    ]


def _suggestion(
    item: Any, skills: dict[str, tuple[str, str]], entries: dict[str, dict[str, Any]], existing: list[str]
) -> dict[str, Any] | None:
    """Keep a suggestion only if it points at a real line or entry, adds something the CV does
    not already say, and claims nothing more than plain use. A new line must be about the work
    its entry already shows, and must not be an existing line in other words."""
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
                    and not _leadership(value)
                    and not any(tag_pattern(value).search(known) for known in existing)
                    and value.casefold() not in {existing.casefold() for existing in items}):
                items.append(value)
        if not items:
            return None
        items = items[:MAX_ITEMS]
        return {"kind": "skill", "fact_id": fact_id, "items": items, "where": line,
                "new_text": f"{line}, {', '.join(items)}"}
    if item.get("kind") == "bullet" and isinstance(item.get("entry"), str) and item["entry"] in entries:
        text = " ".join(item.get("text").split()) if isinstance(item.get("text"), str) else ""
        entry = entries[item["entry"]]
        words = _words(text)
        if (not text or len(text) > MAX_LINE_CHARACTERS or NUMBER.search(text)
                or _leadership(text) or LINK.search(text)
                or any(text.casefold() in known.casefold() or known.casefold() in text.casefold() for known in existing)
                or not words & entry["words"]
                or any(len(words & _words(known)) >= NEAR_COPY * len(words) for known in existing)):
            return None
        tags = [
            tag.strip() for tag in (item.get("tags") if isinstance(item.get("tags"), list) else [])
            if isinstance(tag, str) and tag.strip() and tag_pattern(tag.strip()).search(text)
        ]
        return {"kind": "bullet", "entry": item["entry"], "entry_key": entry["key"], "fact_type": BULLET_TYPES[entry["kind"]],
                "text": text, "tags": list(dict.fromkeys(tags))[:MAX_TAGS], "where": entry["where"]}
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
    in its confirmed words with, if reworded, its new words."""
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
    only by a stand-in, since its name is a school, an employer or a project); and the confirmed
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
    effort: str, private: list[str],
) -> dict[str, Any]:
    """Ask for one honest line per gap and keep the ones that pass; returns how it went."""
    resume, skills, entries = _resume(cv, private)
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
            by_requirement[requirement] = _suggestion(pick, skills, entries, _existing_lines(cv))
    for gap in gaps:
        gap["suggestion"] = by_requirement.get(gap["requirement_id"])
    return suggesting


def _suggestion_key(requirement: str, suggestion: dict[str, Any]) -> str:
    """What a suggestion claims: skills wherever they would be listed, or a line under one entry."""
    if suggestion.get("kind") == "skill":
        claim = sorted(item.casefold() for item in suggestion.get("items") or [] if isinstance(item, str))
    else:
        claim = [suggestion.get("text"), suggestion.get("entry_key") or suggestion.get("where")]
    return json.dumps([requirement, suggestion.get("kind"), claim], ensure_ascii=False)


def _declined(previous: Any) -> set[str]:
    """What the user said is not true in any earlier check, by requirement and claim; kept even
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
    ``previous`` check stays declined."""
    private = sorted({*private_terms(cv), *(private or ())}, key=len, reverse=True)
    confirmed = [fact for fact in list_facts(facts_db) if fact["status"] == "confirmed"]
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


STATUSES = ("not_shown", "related", "none", "unchecked", "shown")


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
                    words = [rewording]  # put back in the confirmed words that show it
                elif item["text"] != source and not comes_back_reworded:
                    return "cut", []  # only the undone rewording shows it; undoing the cut would not
            cut = next((change for change in cuts if change in undoable), None)
            return "cut", [cut, *words] if cut else []
    return "not_on_cv", []


def coverage(gaps: dict[str, Any], cv: dict[str, Any], confirmed: Iterable[tuple[str, int]]) -> dict[str, Any]:
    """What this CV shows for each requirement, from the CV as it is shown now (so undoing a
    cut changes it at once) and the verdicts found by ``find_gaps``.

    A requirement is "shown" when a whole group of lines that shows it is on the CV in the words
    and entries that were judged, "not_shown" when its evidence is cut, reworded or not on this
    CV (with the changes to undo to show it), and otherwise "related", "none" or "unchecked" as
    judged. ``confirmed`` lists the (fact ID, version) pairs confirmed now: the check is
    ``stale`` once they, or anything the CV could show, differ from what was judged.
    """
    lines, entries = _shown_now(cv)
    items = gaps["items"]
    # Cuts, reordering and undos only change which of the judged words are shown.
    stale = (sorted([fact_id, version] for fact_id, version in confirmed) != gaps["checked_facts"]
             or sorted({_item_key(item) for item in _cv_items(cv)}) != gaps["checked_cv"])
    undoable = _undoable(cv)

    def described(ref: str) -> dict[str, Any]:
        item = items[ref]
        if _is_shown(item, lines, entries):
            return {"text": item["text"], "shown": True, "why": None, "undo": []}
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
                "not_shown", max(zip(record["sets"], shown), key=lambda pair: sum(pair[1]))[0])
        elif status == "related":
            refs = record["related"]
        requirements.append({
            "requirement_id": record["requirement_id"], "text": record["text"], "strength": record["strength"],
            "status": status, "evidence": [described(ref) for ref in refs], "missing": record["missing"],
            "suggestion": record["suggestion"], "suggestion_status": record["status"],
        })
    return {
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
