"""Fit a job's CV on one page by leaving out the lines that help this job least.

Nothing is reworded or deleted here. Each line left out is a cut in the CV's plan (cv_layout),
listed with its reason and undone like any other change, and the draft keeps every line, so
the shared profile and the material behind it are never touched. The printed page count
decides: candidates are tried in order, least related to the job's requirements first, and a
result counts only once it was printed on one page. The search is bounded (MAX_RENDERS).

Never left out: a line the job's requirements were matched to, a line this CV changed or added
(cv_edit), a line the user put back by undoing a cut, the identity header, education other than
coursework, one line of every job and one skills line. If one page needs more than that, or more
than MAX_CUT_SHARE of what the CV shows, nothing changes and FitError says what stands in the way.
"""

import copy
import re
from datetime import datetime, timezone
from typing import Any, Callable

from feiran.cv import CVError
from feiran.cv_edit import _in_effect
from feiran.cv_layout import EMPTY_OK, describe_changes, effective_layout, guarded_layout, original_layout, shown_sections

FIT_VERSION = 1
MAX_PAGES = 1
MAX_RENDERS = 8  # one print as it is, one with every candidate left out, then a binary search
MAX_CUT_SHARE = 0.5  # never leave out more than half of the lines the CV shows
FIT_REASON = "Left out to fit one page: less related to this job's requirements than what stays."
# Which kind of line goes first among equally related ones: papers, then coursework, projects,
# skills and, last, experience.
CUT_ORDER = {"publications": 0, "education": 1, "projects": 2, "skills": 3, "experience": 4}
COURSEWORK = re.compile(r"^\s*(relevant\s+)?(course\s*work|courses)\b|^\s*(主修|相关|核心)?课程", re.IGNORECASE)
WORD = re.compile(r"[a-z0-9][a-z0-9+#./-]*[a-z0-9+#]|[a-z0-9]")
CJK = re.compile(r"[\u4e00-\u9fff]{2,}")
STOP = {"and", "or", "the", "a", "an", "of", "to", "in", "for", "with", "on", "at", "by", "as", "is", "are",
        "be", "our", "you", "your", "we", "will", "can", "using", "use", "from", "that", "this", "years",
        "year", "experience", "ability", "strong", "skills", "work", "working", "knowledge", "etc"}


class FitError(CVError):
    """One page is not reachable without leaving out what must stay; nothing was changed.
    ``outcome`` says how far it got, for the page to explain (see fit_one_page)."""

    def __init__(self, message: str, outcome: dict[str, Any]) -> None:
        super().__init__(message, reason="cannot_fit")
        self.outcome = outcome


KEPT_WORDS = {"matched": "linked to this job's requirements", "edited": "added or edited in this CV",
              "put_back": "put back by you", "education": "education details",
              "one_per_entry": "the one line a job or the skills keep"}


def _refusal(before: int, pages: int, tried: int, kept: dict[str, int], capped: bool) -> str:
    held = "; ".join(f"{count} {KEPT_WORDS[name]}" for name, count in kept.items() if count)
    tried_words = (f"even with {tried} less related line(s) left out{' (at most half of what it shows)' if capped else ''}"
                   if tried else "and no line may be left out")
    return (f"One page is not possible without leaving out what must stay: this CV prints on {pages} page(s) "
            f"{tried_words}. {'Lines that stay: ' + held + '. ' if held else ''}Nothing was changed. Shorten or undo "
            f"some of your own changes, or keep {before} pages.")


def _terms(text: str) -> set[str]:
    """Words for comparing a line with the requirements: Latin words, and two-character runs of
    Chinese, which has no spaces between words."""
    text = text.casefold()
    words = {word for word in WORD.findall(text) if word not in STOP and (len(word) > 2 or not word.isalpha())}
    for run in CJK.findall(text):
        words.update(run[n:n + 2] for n in range(len(run) - 1))
    return words


def _requirement_terms(job: Any) -> set[str]:
    items = job.get("selected_requirements") if isinstance(job, dict) else None
    return {term for item in items or [] if isinstance(item, dict) and isinstance(item.get("text"), str)
            for term in _terms(item["text"])}


def _matched(job: Any, draft: dict[str, Any]) -> set[str]:
    items = job.get("selected_requirements") if isinstance(job, dict) else None
    ids = {item["fact_id"] for item in items or [] if isinstance(item, dict) and isinstance(item.get("fact_id"), str)}
    return ids | set((draft.get("job") or {}).get("matched_fact_ids") or [])


def _with_plan(draft: dict[str, Any]) -> dict[str, Any]:
    """The CV with a plan to add cuts to: its own, or the usual layout when it has none."""
    if draft.get("plan"):
        return copy.deepcopy(draft)
    try:
        layout = original_layout(draft)
    except ValueError as exc:
        raise CVError(str(exc)) from exc
    result = copy.deepcopy(draft)
    result["plan"] = {"plan_version": 1, "provider": "fit", "created_at": datetime.now(timezone.utc).isoformat(),
                      "job": draft.get("job"), "layout": layout, "changes": [], "undone": []}
    return result


def _proposal(layout: list[dict[str, Any]], draft: dict[str, Any]) -> dict[str, Any]:
    """A layout written as a proposal guarded_layout reproduces: entries it leaves out listed
    with no lines, since an entry a proposal does not name would come back."""
    listed = {entry["entry"]: entry["lines"] for section in layout for entry in section["entries"]}
    entries = [{"entry": entry_id, "lines": lines} for entry_id, lines in listed.items()]
    entries += [{"entry": entry["entry"], "lines": []} for section in original_layout(draft)
                for entry in section["entries"] if entry["entry"] not in listed]
    return {"sections": [section["kind"] for section in layout], "entries": entries}


def candidates(draft: dict[str, Any], job: Any) -> tuple[list[str], dict[str, int]]:
    """The lines that may be left out, least related to the job first, and how many shown lines
    stay for each reason. ``draft`` has a plan (see _with_plan)."""
    plan = draft["plan"]
    undone = set(plan["undone"])
    wanted, matched = _requirement_terms(job), _matched(job, draft)
    edited = {edit["block"] for edit in draft.get("edits") or [] if _in_effect(edit)}
    listed = {entry["entry"]: set(entry["lines"]) for section in plan["layout"] for entry in section["entries"]}
    by_kind = {section["kind"]: section for section in draft["sections"]}
    kept = {"matched": 0, "edited": 0, "put_back": 0, "education": 0, "one_per_entry": 0}
    found: list[tuple[tuple, str]] = []
    for section in effective_layout(draft, plan["layout"], undone):
        kind = section["kind"]
        stored = by_kind[kind]["entries"]
        free_in_section: list[tuple[tuple, str]] = []
        for entry_position, item in enumerate(section["entries"]):
            texts = {line["fact_id"]: line for line in stored[int(item["entry"].split("e")[1])]["lines"]}
            in_layout = listed.get(item["entry"], set())
            free: list[tuple[tuple, str]] = []
            keeps_entry = False
            for position, fact_id in enumerate(item["lines"]):
                line = texts[fact_id]
                score = len(_terms(line["text"]) & wanted)
                if fact_id in matched:
                    kept["matched"] += 1
                elif fact_id in edited:
                    kept["edited"] += 1
                elif fact_id not in in_layout:
                    kept["put_back"] += 1  # shown only because the user undid its cut
                elif kind == "education" and not COURSEWORK.search(line.get("source_text") or line["text"]):
                    kept["education"] += 1
                else:
                    free.append(((score > 0, CUT_ORDER.get(kind, 2), score, -position, -entry_position), fact_id))
                    continue
                keeps_entry = keeps_entry or fact_id in in_layout
            # A job keeps one bullet, and an entry the user put a line back in keeps one of its
            # own listed lines, or the plan would drop the whole entry with it (a school stays
            # listed without lines).
            if free and not keeps_entry and kind not in EMPTY_OK and (
                    kind == "experience" or len(free) < len(item["lines"])):
                best = max(free)
                free.remove(best)
                kept["one_per_entry"] += 1
            free_in_section.extend(free)
        shown = sum(len(item["lines"]) for item in section["entries"])
        if kind == "skills" and free_in_section and len(free_in_section) == shown:
            free_in_section.remove(max(free_in_section))  # one skills line stays
            kept["one_per_entry"] += 1
        found.extend(free_in_section)
    return [fact_id for _key, fact_id in sorted(found)], kept


def _cut(draft: dict[str, Any], fact_ids: list[str], earlier: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The CV with these lines also left out, its changes listed again: earlier reasons kept,
    the new cuts explained as made to fit one page."""
    leave_out = set(fact_ids)
    result = copy.deepcopy(draft)
    plan = result["plan"]
    layout = [{**section, "entries": [{**entry, "lines": [fact_id for fact_id in entry["lines"] if fact_id not in leave_out]}
                                      for entry in section["entries"]]} for section in plan["layout"]]
    plan["layout"] = guarded_layout(draft, _proposal(layout, draft))
    changes = []
    for change in describe_changes(draft, plan["layout"]):
        before = earlier.get(change["id"])
        if before is not None:
            changes.append({**before, "label": change["label"]})
        else:
            changes.append({**change, "reason": FIT_REASON, "fit": True})
    plan["changes"] = changes
    return result


def _shown_count(draft: dict[str, Any]) -> int:
    return sum(len(entry["lines"]) for section in shown_sections(draft) for entry in section["entries"])


def fit_outcome(draft: Any, job: Any, pages: Callable[[dict[str, Any]], int]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """fit_one_page for the apps: the CV to keep as a new version (None when it already fits or
    cannot), and the outcome to show beside the CV. Other errors (printing, say) propagate."""
    try:
        document, outcome = fit_one_page(draft, job, pages)
    except FitError as exc:
        return None, exc.outcome
    return (document if outcome["status"] == "fitted" else None), outcome


def fit_one_page(draft: Any, job: Any, pages: Callable[[dict[str, Any]], int],
                 max_renders: int = MAX_RENDERS) -> tuple[dict[str, Any], dict[str, Any]]:
    """The CV on one printed page, and how that went. ``pages`` prints a CV and counts its pages.

    A CV that already fits comes back as it is. Otherwise the fewest candidates (see candidates)
    that print on one page are left out, found by printing at most ``max_renders`` times; the
    result is always one that printed on one page. FitError when even every candidate, within
    MAX_CUT_SHARE of the shown lines, leaves more than one page; nothing changes then.
    """
    if not isinstance(draft, dict) or "approval" in draft:
        raise CVError("已批准的简历不能再调整；请编辑当前版本后重新批准")
    renders = 1
    before = pages(draft)
    if before <= MAX_PAGES:
        return draft, {"status": "fits", "pages_before": before, "pages": before, "left_out": 0, "renders": renders}
    base = _with_plan(draft)
    earlier = {change["id"]: change for change in base["plan"]["changes"]}
    order, kept = candidates(base, job)
    limit = int(_shown_count(base) * MAX_CUT_SHARE)
    capped = len(order) > limit
    order = order[:limit]
    if not order:
        raise FitError(_refusal(before, before, 0, kept, capped),
                       {"status": "cannot_fit", "pages_before": before, "pages": before, "tried": 0,
                        "kept": kept, "capped": capped, "renders": renders})
    trials: dict[int, tuple[dict[str, Any], int]] = {}

    def trial(count: int) -> int:
        nonlocal renders
        if count not in trials:
            document = _cut(base, order[:count], earlier)
            renders += 1
            trials[count] = (document, pages(document))
        return trials[count][1]

    most = trial(len(order))
    if most > MAX_PAGES:
        raise FitError(_refusal(before, most, len(order), kept, capped),
                       {"status": "cannot_fit", "pages_before": before, "pages": most, "tried": len(order),
                        "kept": kept, "capped": capped, "renders": renders})
    low, high = 0, len(order)  # low: more than one page (printed first); high: one page
    while high - low > 1 and renders < max_renders:
        middle = (low + high) // 2
        if trial(middle) <= MAX_PAGES:
            high = middle
        else:
            low = middle
    result, after = trials[high]
    left_out = [change["id"] for change in result["plan"]["changes"] if change["id"] not in earlier]
    outcome = {"status": "fitted", "pages_before": before, "pages": after, "left_out": high,
               "changes": left_out, "renders": renders}
    result["plan"]["fit"] = {"fit_version": FIT_VERSION, "fitted_at": datetime.now(timezone.utc).isoformat(), **outcome}
    return result, outcome
