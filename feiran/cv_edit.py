"""A job's CV as its own editable document: lines added or changed for this job only.

Every line of a CV is named by a stable block ID: the ID of the saved entry it was built from,
or, for a line this CV holds on its own (an adopted suggestion, a line the user wrote), a new ID
of the same form, marked ``local``. Reordering, cuts, undos and edits never change a block's ID,
and a local line keeps its ID when it is saved to the profile, where it becomes a saved entry.

Each change is listed, in order, in the document's ``edits``: what it put on the CV, word for
word, the requirements it was made for and the line as it was before, so it can be undone or
done again (set_edit), saved to the profile when the user asks (save_target, mark_saved), and
applied again to the next CV prepared for the job (carry_over), after whatever a model reworded
or adjusted. Nothing here reads or writes a store, changes the profile or calls a model; the
multi-user store and the single-user app keep each result as a new version of the job's CV, so
an approval of the version before never covers it.
"""

import copy
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from feiran.cv import _valid_line
from feiran.cv_layout import shown_sections
from feiran.facts import tag_pattern
from feiran.gaps import (BULLET_TYPES, MAX_ITEM_CHARACTERS, MAX_ITEMS, SKILL_SEPARATORS, _entry_key, _joined,
                  _profile_entry_key, _skill_items, _suggestion_key, places)

LOCAL_PREFIX = "fact-cv-"  # a valid fact ID, so the same block can later be saved as a fact
MAX_EDITS = 200
LINE_RULE = "A CV line must be one line of at most 300 characters, with no [[placeholders]]"


class EditError(ValueError):
    """A change cannot apply to this CV as it is; nothing was changed."""


class StaleEdit(EditError):
    """The page acted on a line, an entry or a change that is no longer as it showed it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not _valid_line(text):
        raise EditError(LINE_RULE)
    return text


def _lines(draft: dict[str, Any]) -> Iterator[tuple[int, int, int, dict[str, Any]]]:
    for i, section in enumerate(draft["sections"]):
        for j, entry in enumerate(section["entries"]):
            for k, line in enumerate(entry["lines"]):
                yield i, j, k, line


def find_block(draft: dict[str, Any], block: str) -> tuple[int, int, int] | None:
    """Where the line with this block ID is stored: (section, entry, line) indexes, or None."""
    return next(((i, j, k) for i, j, k, line in _lines(draft) if line["fact_id"] == block), None)


def find_entry(draft: dict[str, Any], entry_key: str) -> tuple[int, int] | None:
    """The entry named by what it shows (gaps._entry_key), wherever it now sits."""
    return next(((i, j) for i, section in enumerate(draft["sections"]) for j, entry in enumerate(section["entries"])
                 if _entry_key(section["kind"], entry) == entry_key), None)


def shown_cv(draft: dict[str, Any]) -> dict[str, Any]:
    """The document as the CV shows it: cut parts left out, undone rewordings in the saved words."""
    return {**draft, "sections": shown_sections(draft)}


def shown_text(draft: dict[str, Any], block: str) -> str | None:
    """The words the CV shows for a block, or None when it does not show it."""
    return next((line["text"] for _i, _j, _k, line in _lines(shown_cv(draft)) if line["fact_id"] == block), None)


def _shown_entry(draft: dict[str, Any], entry_key: str) -> dict[str, Any] | None:
    return next((entry for section in shown_sections(draft) for entry in section["entries"]
                 if _entry_key(section["kind"], entry) == entry_key), None)


def _where(section: dict[str, Any], entry: dict[str, Any]) -> str:
    return entry.get("title") or section["title"]


def _layout_entry(draft: dict[str, Any], i: int, j: int) -> dict[str, Any] | None:
    """The plan's own listing of the entry, when the CV has a plan that shows it."""
    plan = draft.get("plan")
    if not plan:
        return None
    kind = draft["sections"][i]["kind"]
    section = next((item for item in plan["layout"] if item["kind"] == kind), None)
    return next((item for item in section["entries"] if item["entry"] == f"s{i}e{j}"), None) if section else None


def _insert(draft: dict[str, Any], i: int, j: int, line: dict[str, Any], ahead_of: Any = None) -> None:
    """Add a line to an entry, and to the plan's listing of it, so the CV shows it wherever the
    entry is shown and nothing else moves: at the end, or back where it was (``ahead_of``: the
    lines that came after it, as _remove found them)."""
    stored_next, listed_next = ahead_of or (None, None)
    lines = draft["sections"][i]["entries"][j]["lines"]
    lines.insert(next((n for n, other in enumerate(lines) if other["fact_id"] == stored_next), len(lines)), line)
    listed = _layout_entry(draft, i, j)
    if listed is not None and line["fact_id"] not in listed["lines"]:
        order = listed["lines"]
        order.insert(order.index(listed_next) if listed_next in order else len(order), line["fact_id"])


def _remove(draft: dict[str, Any], block: str) -> list[str | None]:
    """Take a line out of its entry and of the plan's listing; returns the lines that came after
    it there, so it can go back in the same place."""
    place = find_block(draft, block)
    if place is None:
        return [None, None]
    i, j, k = place
    lines = draft["sections"][i]["entries"][j]["lines"]
    stored_next = lines[k + 1]["fact_id"] if k + 1 < len(lines) else None
    del lines[k]
    listed, listed_next = _layout_entry(draft, i, j), None
    if listed is not None and block in listed["lines"]:
        position = listed["lines"].index(block)
        listed_next = listed["lines"][position + 1] if position + 1 < len(listed["lines"]) else None
        listed["lines"].remove(block)
    return [stored_next, listed_next]


def _replace(draft: dict[str, Any], block: str, line: dict[str, Any]) -> None:
    i, j, k = find_block(draft, block)
    draft["sections"][i]["entries"][j]["lines"][k] = line


def _in_effect(edit: dict[str, Any]) -> bool:
    return not edit["undone"] and edit.get("placed", True)


def _latest(edits: list[dict[str, Any]], block: str) -> dict[str, Any] | None:
    """The newest change of a block that is in effect."""
    return next((edit for edit in reversed(edits) if edit["block"] == block and _in_effect(edit)), None)


def _later(edits: list[dict[str, Any]], position: int) -> bool:
    """Whether a newer change of the same block is in effect."""
    return any(other["block"] == edits[position]["block"] and _in_effect(other) for other in edits[position + 1:])


def _repeated(draft: dict[str, Any], request: str | None) -> bool:
    """Whether a change was made by a request with this key already (the same request again)."""
    return request is not None and any(request in edit.get("requests", ()) for edit in draft.get("edits") or ())


def _link(edits: list[dict[str, Any]], block: str, requirements: list[str], request: str | None) -> bool | None:
    """Name more requirements on the change that put a block's words on the CV. None when no
    change of this CV did; otherwise whether any requirement was new to it."""
    edit = _latest(edits, block)
    if edit is None:
        return None
    added = [rid for rid in requirements if rid not in edit["requirements"]]
    edit["requirements"].extend(added)
    if added and request is not None:
        edit.setdefault("requests", []).append(request)
    return bool(added)


def _record(edits: list[dict[str, Any]], *, request: str | None, tags: Iterable[str], **fields: Any) -> None:
    if len(edits) >= MAX_EDITS:
        raise EditError("This CV has too many changes; use Start over to continue")
    edits.append({"id": "edit-" + secrets.token_hex(6), **fields, "tags": list(dict.fromkeys(tags)),
                  "undone": False, "placed": True, "saved": None,
                  "requests": [request] if request is not None else [], "created_at": _now()})


def add_line(draft: dict[str, Any], entry_key: str, text: Any, *, requirements: Iterable[str] = (),
             origin: str = "written", tags: Iterable[str] = (), source: str | None = None,
             request: str | None = None) -> tuple[dict[str, Any], bool]:
    """Add one line, word for word, at the end of an experience or project entry the CV shows.
    Returns the new document and whether anything changed. The same words in that entry already
    are not added again: the requirements are named on the change that added them."""
    text, requirements = _text(text), list(dict.fromkeys(requirements))
    if _repeated(draft, request):
        return draft, False
    place = find_entry(draft, entry_key)
    if place is None:
        raise StaleEdit("This entry is no longer on this CV; reload the page")
    i, j = place
    section = draft["sections"][i]
    if section["kind"] not in BULLET_TYPES:
        raise EditError("A new line can only go under an experience or project entry")
    shown = _shown_entry(draft, entry_key)
    if shown is None:
        raise EditError("This entry is hidden in this CV; show it again first")
    result = copy.deepcopy(draft)
    edits = result.setdefault("edits", [])
    same = next((line for line in shown["lines"] if line["text"].casefold() == text.casefold()), None)
    if same is not None:
        linked = _link(edits, same["fact_id"], requirements, request)
        if linked is None:
            raise EditError("This CV already shows this line")
        return (result, True) if linked else (draft, False)
    if any(line["text"].casefold() == text.casefold() for _i, _j, _k, line in _lines(shown_cv(draft))):
        raise EditError("This CV already shows this line under another entry")
    block = LOCAL_PREFIX + secrets.token_hex(8)
    while find_block(draft, block) is not None:
        block = LOCAL_PREFIX + secrets.token_hex(8)
    _insert(result, i, j, {"fact_id": block, "fact_version": 0, "text": text, "local": True})
    _record(edits, op="add", block=block, section=section["kind"], entry_key=entry_key,
            where=_where(section, section["entries"][j]), text=text, before=None, requirements=requirements,
            origin=origin, source=source, tags=tags, request=request)
    return result, True


def change_line(draft: dict[str, Any], block: str, text: Any, *, expected_text: str | None = None,
                requirements: Iterable[str] = (), origin: str = "edited", tags: Iterable[str] = (),
                source: str | None = None, request: str | None = None) -> tuple[dict[str, Any], bool]:
    """Put new words on one line the CV shows. ``expected_text`` is what the page showed: if the
    line reads otherwise now, nothing changes and StaleEdit says so. The line as it was, with any
    rewording, is kept on the change for undo; requirements named on earlier changes of the line
    stay named."""
    text, requirements = _text(text), list(dict.fromkeys(requirements))
    if _repeated(draft, request):
        return draft, False
    place = find_block(draft, block)
    if place is None:
        raise StaleEdit("This line is no longer on this CV; reload the page")
    shown = shown_text(draft, block)
    if shown is None:
        raise EditError("This line is hidden in this CV; show it again first")
    if expected_text is not None and shown != expected_text:
        raise StaleEdit("This line changed after the page showed it; reload the page")
    result = copy.deepcopy(draft)
    edits = result.setdefault("edits", [])
    if shown == text:
        return (result, True) if _link(edits, block, requirements, request) else (draft, False)
    i, j, k = place
    section = result["sections"][i]
    entry = section["entries"][j]
    before = entry["lines"][k]
    earlier = _latest(edits, block)
    entry["lines"][k] = {"fact_id": block, "fact_version": before["fact_version"], "text": text,
                         **({"local": True} if before.get("local") else {})}
    _record(edits, op="change", block=block, section=section["kind"], entry_key=_entry_key(section["kind"], entry),
            where=_where(section, entry), text=text, before=before,
            requirements=list(dict.fromkeys([*(earlier["requirements"] if earlier else []), *requirements])),
            origin=origin, source=source, tags=tags, request=request)
    return result, True


def adopt(draft: dict[str, Any], suggestion: dict[str, Any], requirement_id: str, *, origin: str,
          source: str | None = None, request: str | None = None) -> tuple[dict[str, Any], bool]:
    """Adopt a suggestion (see gaps: a skill suggestion or a line under an entry) for one
    requirement into this CV only, as given: its skills at the end of the skills line as the CV
    shows it, or its sentence at the end of its entry."""
    if suggestion["kind"] != "skill":
        return add_line(draft, suggestion["entry_key"], suggestion["text"], requirements=[requirement_id],
                        origin=origin, tags=suggestion.get("tags") or (), source=source, request=request)
    block = suggestion["fact_id"]
    if _repeated(draft, request):
        return draft, False
    if find_block(draft, block) is None:
        raise StaleEdit("This skills line is no longer on this CV; reload the page")
    shown = shown_text(draft, block)
    if shown is None:
        raise EditError("This skills line is hidden in this CV; show it again first")
    listed = {item.strip().casefold() for item in SKILL_SEPARATORS.split(_skill_items(shown))}
    new = [item for item in dict.fromkeys(" ".join(item.split()) for item in suggestion["items"])
           if item and item.casefold() not in listed]
    if not new and _latest(draft.get("edits") or [], block) is None:
        raise EditError("This skills line already lists these")
    return change_line(draft, block, _joined(shown, new) if new else shown, requirements=[requirement_id],
                       origin=origin, tags=new, source=source, request=request)


def shown_places(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """Where a line can be written in this CV (gaps.places): the skills lines and the experience
    and project entries it shows."""
    return places(shown_cv(draft))


def written(draft: dict[str, Any], place_id: str, text: Any, suggested: dict[str, Any] | None = None) -> dict[str, Any]:
    """The suggestion a line the user writes makes, for adopt: skills for a skills line the CV
    shows, separated by commas, or a line under an entry it shows. A line keeps the skill words
    of the requirement's own suggestion (``suggested``) that it names."""
    place = next((item for item in shown_places(draft) if item["id"] == place_id), None)
    if place is None:
        raise StaleEdit("This place is no longer shown in this CV; reload the page")
    text = " ".join(text.split()) if isinstance(text, str) else ""
    if place["kind"] == "skill":
        items = list(dict.fromkeys(item.strip() for item in SKILL_SEPARATORS.split(text) if item.strip()))
        if not items:
            raise EditError("Write the skills to add, separated by commas")
        if len(items) > MAX_ITEMS or any(len(item) > MAX_ITEM_CHARACTERS for item in items):
            raise EditError(f"Add at most {MAX_ITEMS} skills at a time, each at most {MAX_ITEM_CHARACTERS} characters")
        return {"kind": "skill", "fact_id": place["fact_id"], "items": items}
    named = [tag for tag in (suggested or {}).get("tags") or [] if tag_pattern(tag).search(text)]
    return {"kind": "bullet", "entry_key": place["entry_key"], "text": _text(text), "tags": named}


def adopt_for(draft: dict[str, Any], row: dict[str, Any], *, place: str | None = None, text: Any = None,
              request: str | None = None) -> tuple[dict[str, Any], bool]:
    """Adopt for one requirement of a gap check (``row``, as gaps.find_gaps keeps it) into this
    CV only: the row's own suggestion as it is, or the user's own words for ``place`` (written).
    A skipped suggestion is not adopted; a row whose suggestion was adopted the old way (into the
    profile, which the CV is prepared from) has nothing more to add."""
    if place is None:
        if row["status"] == "declined":
            raise EditError("这条建议已被跳过；如需添加可自行编辑内容")
        if row["status"] == "added":
            return draft, False
        if not isinstance(row.get("suggestion"), dict):
            raise EditError("这条要求没有可添加的建议")
        suggestion, origin = row["suggestion"], "suggestion"
    else:
        suggestion, origin = written(draft, place, text, row.get("suggestion")), "written"
    return adopt(draft, suggestion, row["requirement_id"], origin=origin, request=request,
                 source=_suggestion_key(row["text"], suggestion))


def set_edit(draft: dict[str, Any], edit_id: str, undone: bool, *,
             request: str | None = None) -> tuple[dict[str, Any], bool]:
    """Undo one change, or do it again. A line's newest change in effect is undone first; a
    change saved to the profile stays (the line is a saved entry now)."""
    edits = draft.get("edits") or []
    position = next((n for n, edit in enumerate(edits) if edit["id"] == edit_id), None)
    if position is None:
        raise StaleEdit("This change is no longer on this CV; reload the page")
    edit = edits[position]
    if edit["undone"] == undone or _repeated(draft, request):
        return draft, False
    if edit.get("saved"):
        raise EditError("This line is saved to your profile; edit the line instead")
    if _later(edits, position):
        raise StaleEdit("A later change was made to this line; undo that one first")
    result = copy.deepcopy(draft)
    mine = result["edits"][position]
    if not mine.get("placed", True):
        if not undone:
            raise EditError("This change has no place in the current CV")
    elif undone and mine["op"] == "add":
        mine["ahead_of"] = _remove(result, mine["block"])
    elif undone:
        if find_block(result, mine["block"]) is None:
            raise StaleEdit("This line is no longer on this CV; reload the page")
        _replace(result, mine["block"], copy.deepcopy(mine["before"]))
    elif mine["op"] == "add":
        place = find_entry(result, mine["entry_key"])
        if place is None or find_block(result, mine["block"]) is not None:
            raise StaleEdit("This entry changed after the page showed it; reload the page")
        _insert(result, *place, {"fact_id": mine["block"], "fact_version": 0, "text": mine["text"], "local": True},
                mine.pop("ahead_of", None))
    else:
        place = find_block(result, mine["block"])
        current = result["sections"][place[0]]["entries"][place[1]]["lines"][place[2]] if place else None
        if current is None or current["text"] != mine["before"]["text"]:
            raise StaleEdit("This line changed after the page showed it; reload the page")
        _replace(result, mine["block"], {"fact_id": mine["block"], "fact_version": current["fact_version"],
                                         "text": mine["text"], **({"local": True} if current.get("local") else {})})
    mine["undone"] = undone
    if request is not None:
        mine.setdefault("requests", []).append(request)
    return result, True


def without_edits(draft: dict[str, Any]) -> dict[str, Any]:
    """The document as it was prepared, before this job's own changes (those saved to the
    profile excepted: they are saved entries now): the base to reword or adjust again, before
    carry_over puts the changes back."""
    result = copy.deepcopy(draft)
    for edit in reversed(result.pop("edits", None) or []):
        if not _in_effect(edit) or edit.get("saved"):
            continue
        if edit["op"] == "add":
            _remove(result, edit["block"])
        elif find_block(result, edit["block"]) is not None:
            _replace(result, edit["block"], copy.deepcopy(edit["before"]))
    return result


def _carry(document: dict[str, Any], edit: dict[str, Any], renamed: dict[str, str]) -> bool:
    """Apply one change again to a newly prepared CV; whether it found its place. A line added
    whose words the new CV already has in its entry (saved to the profile from another job's CV,
    say) is not added twice: the change names that line from then on (``renamed``)."""
    edit["block"] = renamed.get(edit["block"], edit["block"])
    place = find_block(document, edit["block"])
    if edit.get("saved"):
        return place is not None  # a saved entry now: prepared like any other
    if edit["op"] == "add":
        if place is not None:
            return True
        entry = find_entry(document, edit["entry_key"])
        if entry is None:
            return False
        words = edit["text"].casefold()
        same = next((line for line in document["sections"][entry[0]]["entries"][entry[1]]["lines"]
                     if words in (line["text"].casefold(), (line.get("source_text") or "").casefold())), None)
        if same is not None:
            renamed[edit["block"]] = edit["block"] = same["fact_id"]
            if not same.get("local"):
                edit["saved"] = {"fact_id": same["fact_id"], "version": same["fact_version"]}
            return True
        _insert(document, *entry, {"fact_id": edit["block"], "fact_version": 0, "text": edit["text"], "local": True})
        return True
    if place is None:
        return False
    i, j, k = place
    before = document["sections"][i]["entries"][j]["lines"][k]
    document["sections"][i]["entries"][j]["lines"][k] = {
        "fact_id": edit["block"], "fact_version": before["fact_version"], "text": edit["text"],
        **({"local": True} if before.get("local") else {})}
    edit["before"] = before
    return True


def carry_over(previous: dict[str, Any] | None, document: dict[str, Any]) -> dict[str, Any]:
    """``document`` (a CV just prepared, reworded or adjusted for the job, without changes of
    its own) with every change of ``previous`` (the job's CV until now) that is in effect applied
    again, in order and word for word. A change whose entry or line the new CV no longer has is
    kept as not placed, for the page to list; undone changes are left behind."""
    carried = [copy.deepcopy(edit) for edit in (previous or {}).get("edits") or [] if not edit["undone"]]
    if not carried:
        return document
    result, renamed = copy.deepcopy(document), {}
    for edit in carried:
        edit["placed"] = _carry(result, edit, renamed)
    result["edits"] = carried
    return result


def already_saved(draft: dict[str, Any], block: str, expected_text: str) -> bool:
    """Check the words the page offered to save, including on a repeated save click."""
    if shown_text(draft, block) != expected_text:
        raise StaleEdit("This line changed after the page showed it; reload the page")
    latest = _latest(draft.get("edits") or [], block)
    return bool(latest and latest.get("saved"))


def save_target(draft: dict[str, Any], block: str) -> dict[str, Any]:
    """What saving one line of this CV to the profile writes: its words now, the entry it is
    under, the kind of entry for a new one, and the skill words its changes named. Only a line
    this CV changed, or holds on its own, and has not saved yet."""
    edits = draft.get("edits") or []
    latest = _latest(edits, block)
    place = find_block(draft, block)
    if latest is None or latest.get("saved") or place is None:
        raise StaleEdit("This line has no unsaved change in this CV; reload the page")
    i, j, k = place
    section = draft["sections"][i]
    line = section["entries"][j]["lines"][k]
    tags = [tag for edit in edits if edit["block"] == block and _in_effect(edit) for tag in edit.get("tags") or []]
    return {"block": block, "text": line["text"], "new": bool(line.get("local")), "version": line["fact_version"],
            "fact_type": BULLET_TYPES.get(section["kind"]), "entry_key": _entry_key(section["kind"], section["entries"][j]),
            "tags": list(dict.fromkeys(tags))}


def _profile_entry(profile: dict[str, Any], language: str, entry_key: str) -> tuple[int, int]:
    found = [(i, j) for i, section in enumerate(profile.get("sections") or [])
             for j, entry in enumerate(section.get("entries") or [])
             if _profile_entry_key(section.get("kind"), entry, language) == entry_key]
    if len(found) != 1:
        raise EditError("This line's entry is not in your profile in this language, so it can only stay in this CV")
    return found[0]


def profile_with_line(profile: dict[str, Any], language: str, entry_key: str, fact_id: str) -> dict[str, Any]:
    """A copy of the profile listing ``fact_id`` last under the entry the line belongs to."""
    i, j = _profile_entry(profile, language, entry_key)
    result = copy.deepcopy(profile)
    facts = result["sections"][i]["entries"][j].setdefault("facts", [])
    if fact_id not in facts:
        facts.append(fact_id)
    return result


def _payload(text: str, fact_type: str, tags: Iterable[str]) -> tuple[str, str, list[str]]:
    return text, fact_type, sorted({tag.strip().casefold() for tag in tags})


def save_plan(target: dict[str, Any], profile: dict[str, Any], language: str, facts: Any) -> dict[str, Any]:
    """What saving a line (save_target) to the profile writes, decided before anything is
    written, from the profile in the CV's language and the saved entries now (``facts``: a fact
    source, read only). A new line becomes a new saved entry under the line's own block ID,
    listed last under its entry (``profile``: the profile to save); a changed saved entry gets a
    new version (``revise``, with its own and the change's skill words). Saving what is already
    saved under that ID (a save cut short, done again) writes no second entry. Refused, with
    nothing written: an entry the profile does not have, the same words already listed under it
    or saved as another entry, and a saved entry that changed since the CV was made."""
    block = target["block"]
    existing = facts.current([block]).get(block)
    if not target["new"]:
        if existing is None:
            raise StaleEdit("This line's saved entry is gone; prepare the CV again")
        if existing["text"] == target["text"]:
            return {"fact": existing, "create": False, "revise": False, "profile": None}
        if existing["version"] != target["version"]:
            raise StaleEdit("This line's saved entry changed after this CV was made; prepare the CV again")
        tags = {tag.casefold(): tag for tag in [*target["tags"], *existing["tags"]]}
        return {"fact": existing, "create": False, "revise": True, "profile": None, "tags": list(tags.values())}
    i, j = _profile_entry(profile, language, target["entry_key"])
    listed = [fact_id for fact_id in profile["sections"][i]["entries"][j].get("facts") or [] if fact_id != block]
    if any(fact["text"] == target["text"] for fact in facts.current(listed).values()):
        raise EditError("Your profile already lists this line under this entry")
    if existing is not None and existing["text"] != target["text"]:
        raise StaleEdit("Another saved entry has this line's ID")
    wanted = _payload(target["text"], target["fact_type"], target["tags"])
    if existing is None and any(_payload(fact["text"], fact["fact_type"], fact["tags"]) == wanted
                                for fact in facts.listed()):
        raise EditError("Your saved material already has this line under another entry")
    updated = profile_with_line(profile, language, target["entry_key"], block)
    return {"fact": existing, "create": existing is None, "revise": False,
            "profile": updated if updated != profile else None, "tags": target["tags"]}


def mark_saved(draft: dict[str, Any], block: str, version: int, *, profile_sha256: str | None = None) -> dict[str, Any]:
    """The CV once one of its lines is saved to the profile as entry ``block`` at ``version``:
    the line quotes that entry, and its changes are marked saved, so preparing the CV again
    finds the line in the profile instead of adding it twice. ``profile_sha256``: the profile it
    now matches, when it matched the one before."""
    result = copy.deepcopy(draft)
    i, j, k = find_block(result, block)
    line = result["sections"][i]["entries"][j]["lines"][k]
    result["sections"][i]["entries"][j]["lines"][k] = {"fact_id": block, "fact_version": version, "text": line["text"]}
    result["facts"] = [*(item for item in result["facts"] if item["id"] != block), {"id": block, "version": version}]
    for edit in result.get("edits") or []:
        if edit["block"] == block and _in_effect(edit):
            edit["saved"] = {"fact_id": block, "version": version}
    if profile_sha256 is not None:
        result["profile_sha256"] = profile_sha256
    return result


def edits_view(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """This CV's own changes as the page lists them, newest first, with what can be done now."""
    edits = draft.get("edits") or []
    shown = {line["fact_id"] for _i, _j, _k, line in _lines(shown_cv(draft))}
    rows = []
    for position, edit in enumerate(edits):
        active, later, saved = _in_effect(edit), _later(edits, position), bool(edit.get("saved"))
        place = find_block(draft, edit["block"])
        current = draft["sections"][place[0]]["entries"][place[1]]["lines"][place[2]] if place else None
        redo_ready = (current is None and find_entry(draft, edit["entry_key"]) is not None
                      if edit["op"] == "add" else current is not None
                      and current["text"] == (edit.get("before") or {}).get("text"))
        rows.append({
            "id": edit["id"], "op": edit["op"], "block": edit["block"], "text": edit["text"],
            "before": (edit.get("before") or {}).get("text"), "where": edit.get("where"),
            "requirements": edit["requirements"], "origin": edit["origin"], "undone": edit["undone"],
            "placed": edit.get("placed", True), "shown": active and edit["block"] in shown, "saved": saved,
            "latest": active and not later,
            "can_undo": active and not later and not saved,
            "can_redo": edit["undone"] and edit.get("placed", True) and not later and not saved and redo_ready,
            "can_save": active and edit["block"] in shown and not later and not saved,
        })
    return rows[::-1]


def adoptions(draft: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """What this CV holds for each requirement: the lines whose newest change in effect names it,
    with that change, for the requirement's row to show and undo or save."""
    found: dict[str, list[dict[str, Any]]] = {}
    for row in edits_view(draft):
        if not row["latest"]:
            continue  # undone, set aside, or an older change of a line changed again since
        for requirement in row["requirements"]:
            found.setdefault(requirement, []).append(row)
    return found


def adopted_sources(draft: dict[str, Any]) -> set[str]:
    """The suggestions (gaps._suggestion_key) adopted into this CV and not undone."""
    return {edit["source"] for edit in draft.get("edits") or [] if edit.get("source") and _in_effect(edit)}


def with_adoptions(rows: list[dict[str, Any]], draft: dict[str, Any]) -> list[dict[str, Any]]:
    """A check's rows (gaps.coverage) with what this CV holds for each requirement (``adopted``)
    and whether the row's own suggestion is among it (``suggestion_adopted``)."""
    held, sources = adoptions(draft), adopted_sources(draft)
    return [{**row, "adopted": held.get(row["requirement_id"], []),
             "suggestion_adopted": isinstance(row.get("suggestion"), dict)
             and _suggestion_key(row["text"], row["suggestion"]) in sources}
            for row in rows]


def lines_view(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """Every line the CV shows, by block ID, for editing it in this CV only."""
    changed = {edit["block"] for edit in draft.get("edits") or [] if _in_effect(edit)}
    return [{"block": line["fact_id"], "text": line["text"], "section": section["kind"],
             "where": _where(section, entry), "local": bool(line.get("local")), "edited": line["fact_id"] in changed}
            for section in shown_sections(draft) for entry in section["entries"] for line in entry["lines"]]
