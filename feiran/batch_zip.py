"""One download for a batch of jobs: every final PDF that is ready, in one ZIP, with a manifest of
the whole batch. Shared by both apps; each app reads its jobs as one consistent snapshot (the
V2 store in one transaction, the single-user workspace under its lock) and hands them here.

What is ready is decided here, the same way for both: a CV whose approval is of its current
version, whose inputs are current, and whose final PDF of that approval exists. The page says
which versions it saw ready (``ready``); when that is not exactly what is ready now (one was
edited, approved or printed since), nothing is sent and the page is asked to reload, so a ZIP is
never a different set of CVs than the page showed. Nothing here writes to the user's computer
beyond the response; members are flat names inside safe folders, with no path from user text.
"""

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Iterable

from feiran.download_name import folder_name, pdf_filename

MAX_JOBS = 20
MAX_BYTES = 60 * 1024 * 1024  # all PDFs together; a CV is far smaller
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
LANGUAGES = ("en", "zh")
ROOT = "Feiran-CVs"
MANIFEST = "manifest.json"
NOT_FOUND = "One of these jobs is not available; nothing was downloaded."
CHANGED = "These CVs changed after the page showed them; reload the batch and download again."
NONE_READY = "No CV in this batch has a current final PDF yet, so there is nothing to download."
TOO_MANY = f"A batch holds at most {MAX_JOBS} jobs."
TOO_BIG = "These PDFs are too large to download together; download them one by one."


class BatchError(ValueError):
    """The whole download is refused: ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def parse_request(jobs: str | None, ready: Iterable[str]) -> tuple[list[str], set[tuple[str, str, str]]]:
    """The batch's job IDs (in order, each once, at most MAX_JOBS) and the versions the page saw
    ready, as (job_id, language, content_sha256). Anything malformed refuses the whole request,
    the same way as a job that is not found, so nothing tells another user's ID apart."""
    ids: list[str] = []
    for value in (jobs or "").split(","):
        value = value.strip()
        if not value:
            continue
        if not JOB_ID.fullmatch(value):
            raise BatchError(NOT_FOUND, 404)
        if value not in ids:
            ids.append(value)
    if not ids:
        raise BatchError(NOT_FOUND, 404)
    if len(ids) > MAX_JOBS:
        raise BatchError(TOO_MANY, 400)
    seen: set[tuple[str, str, str]] = set()
    for item in ready:
        parts = item.split(":") if isinstance(item, str) else []
        if (len(parts) != 3 or parts[0] not in ids or parts[1] not in LANGUAGES
                or not SHA256.fullmatch(parts[2])):
            raise BatchError(NOT_FOUND, 404)
        seen.add((parts[0], parts[1], parts[2]))
    if len(seen) > MAX_JOBS * len(LANGUAGES):
        raise BatchError(TOO_MANY, 400)
    return ids, seen


def state_of(cv: dict[str, Any]) -> str:
    """One CV's state: ready, stale, approved_no_pdf, not_approved or not_prepared."""
    if not cv.get("content_sha256"):
        return "not_prepared"
    if cv.get("stale"):
        return "stale"
    if not cv.get("approved"):
        return "not_approved"
    return "ready" if cv.get("pdf") is not None else "approved_no_pdf"


def build(jobs: list[dict[str, Any]], expected: set[tuple[str, str, str]]) -> tuple[bytes, dict[str, Any]]:
    """The ZIP and its manifest from one snapshot of the batch. ``jobs``: each with job_id,
    company, title and cvs (language -> content_sha256, stale, approved, pages, stages, and the
    approved final PDF's bytes as ``pdf`` or None). Refuses (BatchError) when what is ready is not
    exactly ``expected``, when nothing is ready, or when it is too large."""
    ready_now = {(job["job_id"], language, cv["content_sha256"]) for job in jobs
                 for language, cv in job["cvs"].items() if state_of(cv) == "ready"}
    if ready_now != expected:
        raise BatchError(CHANGED, 409)
    if not ready_now:
        raise BatchError(NONE_READY, 409)
    members: list[tuple[str, bytes]] = []
    manifest_jobs = []
    for job in jobs:
        folder = folder_name(company=job.get("company"), title=job.get("title"), job_id=job["job_id"])
        entries = []
        for language in LANGUAGES:
            cv = job["cvs"].get(language)
            if cv is None:
                continue
            state = state_of(cv)
            file = None
            if state == "ready":
                file = f"{ROOT}/{folder}/" + pdf_filename(company=job.get("company"), title=job.get("title"),
                                                          language=language, job_id=job["job_id"],
                                                          version=cv["content_sha256"])
                members.append((file, cv["pdf"]))
            entries.append({"language": language, "state": state, "content_sha256": cv.get("content_sha256"),
                            "version": (cv.get("content_sha256") or "")[:8] or None, "stale": bool(cv.get("stale")),
                            "approved": bool(cv.get("approved")), "pages": cv.get("pages"),
                            "stages": cv.get("stages") or [], "file": file})
        manifest_jobs.append({"job_id": job["job_id"], "company": job.get("company"), "title": job.get("title"),
                              "folder": f"{ROOT}/{folder}", "cvs": entries})
    if sum(len(data) for _name, data in members) > MAX_BYTES:
        raise BatchError(TOO_BIG, 413)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "included": len(members),
                "total": sum(len(job["cvs"]) for job in manifest_jobs), "jobs": manifest_jobs,
                "note": "Only CVs whose approved version is current are included; the others are listed with their state."}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{ROOT}/{MANIFEST}", json.dumps(manifest, ensure_ascii=False, indent=1))
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue(), manifest


def disposition(count: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f'attachment; filename="Feiran-CVs-{stamp}-{count}.zip"'
