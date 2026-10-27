"""Where the user last worked, so opening the workbench again (after signing in again, or after
closing the page) goes back there: one job or a batch of jobs, the CV language they were on, the
part of the page they were working in, and which version of each CV they saw (only to say, in one
line, that it changed since). Both apps keep one such place per account (V2 in its store, the
single-user app in its data folder); this module only checks its shape.

No CV or job text is kept, nor any history: the next place replaces the last, unless it records
an older action of the user's than the place kept (an older tab saving late, or closing): each
place is kept with the user action it records and when that was, on the server's clock
(action_time). A place never starts work, never approves or prints anything: what the page shows on return is read
from the server as it is then.
"""

import re
from typing import Any, Callable, Iterable

MAX_JOBS = 20  # a batch's limit
LANGUAGES = ("en", "zh")
AREAS = ("review", "requirements", "cv", "gaps", "batch")
KINDS = ("job", "batch")
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class PlaceError(ValueError):
    """The place sent is not one the page could have been at."""


def clean_place(value: Any) -> dict[str, Any]:
    """The place as it is kept, from what the page sent: kind, jobs (one for a job, up to
    MAX_JOBS for a batch, each once), language and area (or None), and seen (per job and
    language, the content hash of the CV the page showed). Anything else refuses it."""
    if not isinstance(value, dict) or set(value) - {"kind", "jobs", "language", "area", "seen"}:
        raise PlaceError("Invalid place")
    kind, jobs = value.get("kind"), value.get("jobs")
    if kind not in KINDS or not isinstance(jobs, list) or not jobs:
        raise PlaceError("Invalid place")
    if any(not isinstance(job, str) or not JOB_ID.fullmatch(job) for job in jobs) or len(set(jobs)) != len(jobs):
        raise PlaceError("Invalid place")
    if len(jobs) > (1 if kind == "job" else MAX_JOBS):
        raise PlaceError("Invalid place")
    language, area = value.get("language"), value.get("area")
    if language not in (None, *LANGUAGES) or area not in (None, *AREAS):
        raise PlaceError("Invalid place")
    seen = value.get("seen") or {}
    if not isinstance(seen, dict) or set(seen) - set(jobs):
        raise PlaceError("Invalid place")
    kept_seen = {}
    for job, versions in seen.items():
        if not isinstance(versions, dict) or set(versions) - set(LANGUAGES):
            raise PlaceError("Invalid place")
        if any(version is not None and (not isinstance(version, str) or not SHA256.fullmatch(version))
               for version in versions.values()):
            raise PlaceError("Invalid place")
        kept_seen[job] = {language: version for language, version in versions.items() if version}
    return {"kind": kind, "jobs": list(jobs), "language": language, "area": area,
            "seen": {job: versions for job, versions in kept_seen.items() if versions}}


def still_there(place: Any, exists: Callable[[Iterable[str]], set[str]]) -> dict[str, Any]:
    """What to answer about a kept place now: {"place": ...} with only the jobs that still exist
    (``exists`` gives which of them do), or {"place": None, "gone": True} when the job, or every
    job of the batch, is gone; {"place": None, "gone": False} when nothing usable was kept."""
    try:
        place = clean_place(place)
    except PlaceError:
        return {"place": None, "gone": False}
    present = exists(place["jobs"])
    jobs = [job for job in place["jobs"] if job in present]
    if not jobs:
        return {"place": None, "gone": True}
    return {"place": {**place, "jobs": jobs, "seen": {job: versions for job, versions in place["seen"].items()
                                                      if job in present}}, "gone": False}


MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000  # an action older than this counts as this old
ACTION = re.compile(r"[A-Za-z0-9_-]{8,64}")


def action_time(action: Any, ago_ms: Any, now_ms: int) -> tuple[str | None, int]:
    """The user action a save records, and when it was, on the server's clock: the page says how
    many milliseconds ago it was (measured by its own steady clock, never its date and time), and
    that is taken from now, at most MAX_AGE_MS. No device's clock running fast or slow can then
    put its place over a later one from another device. No action (an older page) is now."""
    if action is None and ago_ms is None:
        return None, now_ms
    if not isinstance(action, str) or not ACTION.fullmatch(action) or type(ago_ms) is not int or ago_ms < 0:
        raise PlaceError("Invalid place")
    return action, now_ms - min(ago_ms, MAX_AGE_MS)


def kept_record(stored: Any) -> tuple[Any, int, str | None]:
    """The place, when its action was (server milliseconds) and the action's ID, from what is
    stored ({"place", "acted_at", "action"}); a place stored bare, by the first version of this,
    counts as acted on at no time (any action replaces it)."""
    if isinstance(stored, dict) and set(stored) >= {"place", "acted_at"} and type(stored["acted_at"]) is int:
        action = stored.get("action")
        return stored["place"], stored["acted_at"], action if isinstance(action, str) else None
    return stored, 0, None


def replaces(stored: Any, action: str | None, acted_at: int) -> tuple[bool, int]:
    """Whether a save of ``action`` at ``acted_at`` replaces the stored place, and the action time
    to keep: the same action saved again (the page closing with what it last showed) always does,
    keeping its first time; another one only if it is not older."""
    _place, kept_at, kept_action = kept_record(stored)
    if action is not None and action == kept_action:
        return True, kept_at
    return acted_at >= kept_at, acted_at
