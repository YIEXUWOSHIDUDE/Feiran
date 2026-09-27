"""What a per-job CV shows: section order, entry order and lines, within fixed guardrails.

A layout never adds anything. It only orders and leaves out lines the draft already holds,
each a confirmed fact, possibly with a checked rewrite. The draft keeps every line, so any
change can be undone without asking a model again. Entries are named by their place in the
draft ("s2e1" is the second entry of the third section), so a layout cannot move a line
between entries or sections.
"""

import copy
from typing import Any

KEEP_SECTIONS = {"education"}  # never cut
KEEP_PLACE = {"education"}  # stays where the profile puts it: a student's degree and dates lead
FIXED_ORDER = {"education", "experience"}  # schools and jobs stay in date order
MIN_LINES = {"experience": 1}  # every job keeps at least one bullet, so no job is dropped
EMPTY_OK = {"education"}  # a school may be listed without lines, so no school is dropped
BULLET_KINDS = {"experience", "projects"}


def _entry_id(section_index: int, entry_index: int) -> str:
    return f"s{section_index}e{entry_index}"


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def original_layout(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """The usual CV: every section, entry and line in profile order."""
    layout = [
        {
            "kind": section["kind"],
            "entries": [
                {"entry": _entry_id(i, j), "lines": [line["fact_id"] for line in entry["lines"]]}
                for j, entry in enumerate(section["entries"])
            ],
        }
        for i, section in enumerate(draft["sections"])
    ]
    if len({section["kind"] for section in layout}) != len(layout):
        raise ValueError("每种简历栏目只能出现一次，才能按岗位调整结构")
    return layout


def guarded_layout(draft: dict[str, Any], proposal: Any) -> list[dict[str, Any]]:
    """Keep what a proposal may decide; anything unknown, moved across entries or against a
    guardrail is ignored or put back, never trusted.

    Leaving something out has to be explicit: a section missing from the section list, a line
    missing from its entry's list, or an entry listed with no lines. Entries the proposal does
    not mention stay as they are, after the ones it lists.
    """
    original = original_layout(draft)
    by_kind = {section["kind"]: section for section in original}
    proposal = proposal if isinstance(proposal, dict) else {}
    order = [kind for kind in dict.fromkeys(_strings(proposal.get("sections"))) if kind in by_kind]
    for index, section in enumerate(original):
        kind = section["kind"]
        if kind in KEEP_PLACE and kind in order:
            order.remove(kind)
        if kind in KEEP_PLACE or (kind in KEEP_SECTIONS and kind not in order):
            order.insert(min(index, len(order)), kind)
    proposed: dict[str, list[str]] = {}
    for item in proposal.get("entries") if isinstance(proposal.get("entries"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("entry"), str) and item["entry"] not in proposed:
            proposed[item["entry"]] = _strings(item.get("lines"))
    layout = []
    for kind in order:
        known = {entry["entry"]: entry["lines"] for entry in by_kind[kind]["entries"]}
        ids = [entry_id for entry_id in proposed if entry_id in known]
        ids += [entry_id for entry_id in known if entry_id not in ids]
        if kind in FIXED_ORDER:
            ids = [entry_id for entry_id in known if entry_id in ids]
        entries = []
        for entry_id in ids:
            allowed = known[entry_id]
            if entry_id in proposed:
                lines = [fact_id for fact_id in dict.fromkeys(proposed[entry_id]) if fact_id in allowed]
            else:
                lines = list(allowed)
            minimum = MIN_LINES.get(kind, 0)
            if len(lines) < minimum:
                lines += [fact_id for fact_id in allowed if fact_id not in lines][:minimum - len(lines)]
            if allowed and not lines and kind not in EMPTY_OK:
                continue
            entries.append({"entry": entry_id, "lines": lines})
        if entries or kind in KEEP_SECTIONS:
            layout.append({"kind": kind, "entries": entries})
    return layout


def _names(draft: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Section titles by kind, entry names by ID and line texts by fact ID, for change labels."""
    sections, entries, lines = {}, {}, {}
    for i, section in enumerate(draft["sections"]):
        sections[section["kind"]] = section["title"]
        for j, entry in enumerate(section["entries"]):
            entries[_entry_id(i, j)] = entry.get("title") or section["title"]
            for line in entry["lines"]:
                lines[line["fact_id"]] = line.get("source_text") or line["text"]
    return sections, entries, lines


def describe_changes(draft: dict[str, Any], layout: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Every difference from the usual CV, each with an ID that can be undone on its own."""
    section_names, entry_names, line_texts = _names(draft)
    kept = {section["kind"]: section for section in layout}
    original = original_layout(draft)
    changes: list[dict[str, str]] = []
    kept_order = [section["kind"] for section in layout]
    if kept_order != [section["kind"] for section in original if section["kind"] in kept]:
        changes.append({"id": "order:sections", "type": "order",
                        "label": "Section order: " + ", ".join(section_names[kind] for kind in kept_order)})
    for index, section in enumerate(original):
        kind = section["kind"]
        if kind not in kept:
            changes.append({"id": f"cut:{kind}", "type": "cut", "label": f"Cut section: {section_names[kind]}"})
            continue
        entries = {entry["entry"]: entry["lines"] for entry in kept[kind]["entries"]}
        if list(entries) != [entry["entry"] for entry in section["entries"] if entry["entry"] in entries]:
            changes.append({"id": f"order:s{index}", "type": "order",
                            "label": f"{section_names[kind]} order: " + ", ".join(entry_names[entry_id] for entry_id in entries)})
        for entry in section["entries"]:
            entry_id = entry["entry"]
            if entry_id not in entries:
                changes.append({"id": f"cut:{entry_id}", "type": "cut", "label": f"Cut: {entry_names[entry_id]}"})
                continue
            lines = entries[entry_id]
            if lines != [fact_id for fact_id in entry["lines"] if fact_id in lines]:
                noun = "bullets" if kind in BULLET_KINDS else "lines"
                changes.append({"id": f"order:{entry_id}", "type": "order",
                                "label": f"{entry_names[entry_id]}: {noun} reordered"})
            for fact_id in entry["lines"]:
                if fact_id not in lines:
                    changes.append({"id": f"cut:{fact_id}", "type": "cut", "label": f"Cut: {line_texts[fact_id]}"})
    return changes


def _put_back(items: list[Any], item: Any, key: Any, original_keys: list[Any], key_of: Any) -> None:
    """Insert ``item`` right after the nearest item that came before it in the original order."""
    present = [key_of(existing) for existing in items]
    for earlier in reversed(original_keys[:original_keys.index(key)]):
        if earlier in present:
            items.insert(present.index(earlier) + 1, item)
            return
    items.insert(0, item)


def effective_layout(draft: dict[str, Any], layout: list[dict[str, Any]], undone: set[str]) -> list[dict[str, Any]]:
    """The plan with the user's undone changes reverted: cut parts come back where they
    were, and an undone reordering returns to the usual order."""
    original = original_layout(draft)
    result = copy.deepcopy(layout)
    kinds = [section["kind"] for section in original]
    for index, section in enumerate(original):
        current = next((item for item in result if item["kind"] == section["kind"]), None)
        if current is None:
            if f"cut:{section['kind']}" in undone:
                _put_back(result, copy.deepcopy(section), section["kind"], kinds, lambda item: item["kind"])
            continue
        entry_ids = [entry["entry"] for entry in section["entries"]]
        for entry in section["entries"]:
            present = next((item for item in current["entries"] if item["entry"] == entry["entry"]), None)
            if present is None:
                if f"cut:{entry['entry']}" in undone:
                    _put_back(current["entries"], copy.deepcopy(entry), entry["entry"], entry_ids, lambda item: item["entry"])
                continue
            for fact_id in entry["lines"]:
                if fact_id not in present["lines"] and f"cut:{fact_id}" in undone:
                    _put_back(present["lines"], fact_id, fact_id, entry["lines"], lambda item: item)
    if "order:sections" in undone:
        result.sort(key=lambda item: kinds.index(item["kind"]))
    for section in result:
        index = kinds.index(section["kind"])
        entry_ids = [entry["entry"] for entry in original[index]["entries"]]
        if f"order:s{index}" in undone:
            section["entries"].sort(key=lambda item: entry_ids.index(item["entry"]))
        for entry in section["entries"]:
            line_ids = original[index]["entries"][entry_ids.index(entry["entry"])]["lines"]
            if f"order:{entry['entry']}" in undone:
                entry["lines"].sort(key=line_ids.index)
    return result


def shown_sections(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """The sections as the CV shows them: the draft itself, or its plan with undone changes
    reverted. An undone rewrite shows the confirmed fact word for word again."""
    plan = draft.get("plan")
    if not plan:
        return draft["sections"]
    undone = set(plan.get("undone", []))
    by_kind = {section["kind"]: (i, section) for i, section in enumerate(draft["sections"])}
    shown = []
    for section in effective_layout(draft, plan["layout"], undone):
        index, original = by_kind[section["kind"]]
        entries = []
        for item in section["entries"]:
            entry = original["entries"][int(item["entry"].split("e")[1])]
            lines = {line["fact_id"]: line for line in entry["lines"]}
            chosen = []
            for fact_id in item["lines"]:
                line = lines[fact_id]
                if f"reword:{fact_id}" in undone and "source_text" in line:
                    line = {"fact_id": fact_id, "fact_version": line["fact_version"], "text": line["source_text"]}
                chosen.append(line)
            entries.append({**entry, "lines": chosen})
        shown.append({**original, "entries": entries})
    return shown
