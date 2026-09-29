"""One folder per job under .local/jobs, holding the current file of each workflow step.

Redoing a step never overwrites: the old step and every step derived from it move
into history/<time>/ first, so the folder always shows one consistent chain.

A crash never leaves half a change behind. Each file is written whole or not at all, and
while a step is being replaced, a journal in the job's folder names what was moved to
history. At the next start, recover() puts back what a cut-short change had moved.
"""

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path(".local/jobs")
# How the data folder stores things, as a whole: the facts database, the CV profile, the job
# steps and their notes. A release that stores data so an earlier release could not read it any
# more raises this number. Each start records it in the data folder (claim_data_format), so no
# older release opens the data afterwards, wherever the data goes; the EC2 host refuses such a
# release before installing it (deploy/aws/host/install.sh); a backup is restored only by a
# release that can read it.
DATA_FORMAT = 1
DATA_FORMAT_FILE = ".workbench-format"
JOB_STEPS = ("input", "candidates", "decided")
# Optional extras derived from the requirements; the CV does not depend on them. Talking points
# (matches, linked) and gaps can be redone without touching the CV.
EXTRA_STEPS = ("matches", "linked", "gaps")
EXTRA_DEPENDENTS = {"matches": ("matches", "linked"), "linked": ("linked",), "gaps": ("gaps",)}
CV_STEPS = ("cv-draft", "cv-tailored", "cv-planned", "cv-approved", "cv-final")
LANGUAGES = ("en", "zh")
STEPS = JOB_STEPS + EXTRA_STEPS + tuple(f"{step}-{language}" for language in LANGUAGES for step in CV_STEPS)
JOB_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
# Small status files kept beside the steps and replaced in place: how the latest CV preparation
# went, stage by stage. They are not part of the step chain.
NOTES = (*(f"cv-status-{language}" for language in LANGUAGES), "interrupted")
# Written before a step is replaced and removed once the new file is in place. One this code
# did not write is set aside under the second name, for a person to look at.
JOURNAL = "change-in-progress.json"
UNREADABLE_JOURNAL = "change-in-progress.unreadable.json"
INTERRUPTED = "interrupted.json"  # the note a job's page shows about a change a crash cut short
HISTORY_NAME = re.compile(r"\d{8}T\d{12}-[0-9a-f]{4}")
# What a change of unknown extent is settled by: the requirements saved again, or a CV started over.
RESTARTS = ("candidates", "decided", *(f"cv-draft-{language}" for language in LANGUAGES))


# The temporary files write_atomically makes; startup deletes leftovers of this exact shape only.
TEMPORARY = re.compile(r"\.workbench-.+\.[0-9a-f]{8}\.tmp")


def _sync_folder(folder: Path) -> None:
    """Make renames in a folder last through a power cut: they live in the folder's own data."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_folder(folder: Path) -> None:
    """Create a folder and its missing parents, each entry flushed into its parent: a new
    folder's name lives in the folder above it."""
    missing = []
    while not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for new in reversed(missing):
        new.mkdir()
        _sync_folder(new.parent)


def write_atomically(path: Path, data: bytes) -> None:
    """Write all of a file or none of it: a temporary file beside it, flushed to disk, then
    renamed over it. A crash leaves the old file or the new one, never part of one."""
    temporary = path.with_name(f".workbench-{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _sync_folder(path.parent)


def remove_durably(path: Path) -> None:
    """Delete a file, if there, and flush its folder so the deletion outlasts a power cut."""
    path.unlink(missing_ok=True)
    _sync_folder(path.parent)


def remove_leftovers(folder: Path) -> None:
    """Delete the temporary files write_atomically left behind when the process died. Only
    files of its exact name pattern are touched."""
    if folder.is_dir():
        for stray in folder.iterdir():
            if TEMPORARY.fullmatch(stray.name) and stray.is_file() and not stray.is_symlink():
                stray.unlink()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _note_bytes(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class WorkspaceError(Exception):
    """A job or step name is invalid, or the requested step cannot be stored."""


class DataFormatError(WorkspaceError):
    """The data folder is in a format this release may not read, or its record of it is unreadable."""


def claim_data_format(folder: Path) -> None:
    """Refuse data a newer release has stored in a format this one may not read; otherwise
    record this release's format in the data folder before anything uses the data. A release that
    changes how data is stored must raise DATA_FORMAT, and so be recorded before it changes anything."""
    record = Path(folder) / DATA_FORMAT_FILE
    try:
        recorded = int(record.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        recorded = 0
    except (OSError, ValueError) as exc:
        raise DataFormatError(f"{DATA_FORMAT_FILE} cannot be read, so the data's format is unknown") from exc
    if recorded > DATA_FORMAT:
        raise DataFormatError(f"the data is in format {recorded}, newer than this release's format {DATA_FORMAT}; "
                              "install a release that can read it")
    if recorded < DATA_FORMAT:
        make_folder(Path(folder))
        write_atomically(record, f"{DATA_FORMAT}\n".encode("utf-8"))


def _file_name(step: str) -> str:
    return f"{step}.pdf" if step.startswith("cv-final-") else f"{step}.json"


STEP_FILES = frozenset(_file_name(step) for step in STEPS)


def _read_journal(directory: Path) -> dict[str, Any] | None:
    """The journal of the change in progress, or None if it is not one this code wrote."""
    try:
        journal = json.loads((directory / JOURNAL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(journal, dict):
        return None
    step, history, moved, digest = journal.get("step"), journal.get("history"), journal.get("moved"), journal.get("sha256")
    if step not in STEPS or not isinstance(moved, list) or not isinstance(digest, str):
        return None
    chain = {_file_name(later) for later in _later_steps(step)}
    trusted = (all(isinstance(name, str) and name in chain for name in moved)  # only its own step's chain moves
               and (history is None if not moved else isinstance(history, str) and bool(HISTORY_NAME.fullmatch(history)))
               and re.fullmatch(r"[0-9a-f]{64}", digest))
    return journal if trusted else None


def _later_steps(step: str) -> list[str]:
    """The step itself plus everything derived from it."""
    if step in JOB_STEPS:
        return list(JOB_STEPS[JOB_STEPS.index(step):]) + list(STEPS[len(JOB_STEPS):])
    if step in EXTRA_DEPENDENTS:
        return list(EXTRA_DEPENDENTS[step])
    base, language = step.rsplit("-", 1)
    return [f"{later}-{language}" for later in CV_STEPS[CV_STEPS.index(base):]]


class Workspace:
    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        # Held while files move or are replaced (moments, never across a DeepSeek call), and by
        # readers that need several files to agree, such as a job's page.
        self.lock = threading.RLock()

    def _job_dir(self, job_id: Any) -> Path:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise WorkspaceError("岗位编号格式无效")
        directory = self.root / job_id
        if directory.is_symlink() or not directory.is_dir():  # a link is not a job this workbench made
            raise WorkspaceError(f"岗位不存在：{job_id}")
        return directory

    def path(self, job_id: str, step: str) -> Path:
        if step not in STEPS:
            raise WorkspaceError(f"未知的步骤：{step}")
        return self._job_dir(job_id) / _file_name(step)

    def create_job(self, review_input: dict[str, Any]) -> str:
        if not isinstance(review_input, dict) or not isinstance(review_input.get("jd"), dict):
            raise WorkspaceError("新岗位必须包含 jd")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        job_id = f"{stamp}-{secrets.token_hex(3)}"
        with self.lock:
            make_folder(self.root / job_id)
            try:
                self.write(job_id, "input", review_input)
            except Exception:
                (self.root / job_id).rmdir()  # empty again: the write undid itself
                raise
        return job_id

    def read(self, job_id: str, step: str) -> dict[str, Any] | None:
        with self.lock:
            try:
                return json.loads(self.path(job_id, step).read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None

    def state(self, job_id: str) -> list[str]:
        with self.lock:
            return [step for step in STEPS if self.path(job_id, step).exists()]

    def read_bytes(self, job_id: str, step: str) -> bytes | None:
        with self.lock:
            try:
                return self.path(job_id, step).read_bytes()
            except FileNotFoundError:
                return None

    def _replace_step(self, job_id: str, step: str, data: bytes) -> None:
        with self.lock:
            self._replace_step_locked(job_id, step, data)

    def _replace_step_locked(self, job_id: str, step: str, data: bytes) -> None:
        """Move the step and every step derived from it into history, then write the new file.
        The journal, flushed first, names what will move: an error undoes the change at once,
        a crash at the next start (recover). The change is committed once its new file is in
        place with everything it moved in history."""
        directory = self._job_dir(job_id)
        if (directory / JOURNAL).exists():
            self._finish(directory)  # a change a crash cut short, not recovered yet
        moving = [path for path in (self.path(job_id, later) for later in _later_steps(step)) if path.exists()]
        # Random too, so two changes never share a history folder even if the clock goes back.
        stamp = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{secrets.token_hex(2)}"
        journal = {"step": step, "history": stamp if moving else None, "moved": [path.name for path in moving],
                   "sha256": hashlib.sha256(data).hexdigest(), "started_at": _now()}
        write_atomically(directory / JOURNAL, json.dumps(journal).encode("utf-8"))
        try:
            if moving:
                history = directory / "history" / stamp
                make_folder(history)
                for path in moving:
                    path.rename(history / path.name)
                _sync_folder(history)  # both sides of each move
                _sync_folder(directory)
            write_atomically(self.path(job_id, step), data)
        except Exception:
            self._finish(directory, tell=False)  # the page shows the error itself
            raise
        self._commit(directory, journal)

    def _commit(self, directory: Path, journal: dict[str, Any]) -> None:
        """The change stands. Redoing a cut-short step, or one it depends on, settles the note
        about it (the page's own requirement checks do not); then the journal goes."""
        step = journal["step"]
        try:
            cut_short = json.loads((directory / INTERRUPTED).read_text(encoding="utf-8")).get("step")
        except (OSError, ValueError, AttributeError):
            cut_short = None
        if cut_short in _later_steps(step) or (cut_short == "unknown" and step in RESTARTS):
            (directory / INTERRUPTED).unlink()
            _sync_folder(directory)
        (directory / JOURNAL).unlink()
        _sync_folder(directory)

    def _set_aside(self, directory: Path, tell: bool) -> str:
        """A journal that cannot be trusted, or a moved file found in neither place: nothing
        more is moved, the journal is kept aside for a person to look at, the job is marked."""
        if tell:
            write_atomically(directory / INTERRUPTED, _note_bytes({"step": "unknown", "undone_at": _now()}))
        (directory / JOURNAL).rename(directory / UNREADABLE_JOURNAL)
        _sync_folder(directory)
        return "unknown"

    def _finish(self, directory: Path, tell: bool = True) -> str | None:
        """Close the change the journal describes: commit it (None) if its new file is complete
        and all it moved is in history; otherwise put every moved file back and return the step.
        Rolling back never overwrites and can itself be cut short and resumed. With tell, the
        job is marked interrupted before the journal goes, so a crash cannot lose the note.
        Nothing is written here but files put back and the note, so this can never approve a CV
        or confirm anything."""
        journal = _read_journal(directory)
        if journal is None or (directory / "history").is_symlink():
            return self._set_aside(directory, tell)
        target = directory / _file_name(journal["step"])
        history = directory / "history" / journal["history"] if journal["history"] else None
        if history is not None and history.is_symlink():
            return self._set_aside(directory, tell)
        moved = journal["moved"]
        if (target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == journal["sha256"]
                and all((history / name).exists() for name in moved)):
            self._commit(directory, journal)
            return None
        for name in moved:
            if (directory / name).exists():
                continue  # never moved, or put back already
            if history is None or not (history / name).exists():
                return self._set_aside(directory, tell)
            (history / name).rename(directory / name)
        if history is not None:
            if history.is_dir():  # a resumed rollback may have removed it already
                _sync_folder(history)
            for folder in (history, history.parent):  # history/ too, if this change made it
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
                    _sync_folder(folder.parent)
        remove_leftovers(directory)
        _sync_folder(directory)
        creation = journal["step"] == "input"  # the job itself was being made: there is no job to mark
        if tell and not creation:
            write_atomically(directory / INTERRUPTED, _note_bytes({"step": journal["step"], "undone_at": _now()}))
        (directory / JOURNAL).unlink()
        _sync_folder(directory)
        return journal["step"]

    def recover(self) -> list[dict[str, str]]:
        with self.lock:
            return self._recover_locked()

    def _recover_locked(self) -> list[dict[str, str]]:
        """At start, close every change a crash cut short, and say which were undone. A job so
        marked tells the page (the interrupted note) until the step is done again."""
        undone: list[dict[str, str]] = []
        if not self.root.is_dir():
            return undone
        for directory in sorted(self.root.iterdir()):
            if directory.is_symlink() or not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
                continue  # a link is left alone, wherever it points
            if (directory / JOURNAL).exists():
                step = self._finish(directory)
                if step and step != "input":
                    undone.append({"job_id": directory.name, "step": step})
            remove_leftovers(directory)
            if not any(directory.iterdir()):
                directory.rmdir()  # a job whose creation was cut short before it held anything
                _sync_folder(directory.parent)
        return undone

    def write(self, job_id: str, step: str, data: dict[str, Any]) -> None:
        if step.startswith("cv-final-"):
            raise WorkspaceError("PDF 请用 write_bytes 保存")
        if step not in STEPS:
            raise WorkspaceError(f"未知的步骤：{step}")
        self._replace_step(job_id, step, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def _note_path(self, job_id: str, name: str) -> Path:
        if name not in NOTES:
            raise WorkspaceError(f"未知的状态文件：{name}")
        return self._job_dir(job_id) / f"{name}.json"

    def write_note(self, job_id: str, name: str, data: dict[str, Any]) -> None:
        with self.lock:
            write_atomically(self._note_path(job_id, name), _note_bytes(data))

    def read_note(self, job_id: str, name: str) -> dict[str, Any] | None:
        with self.lock:
            try:
                return json.loads(self._note_path(job_id, name).read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None

    def write_bytes(self, job_id: str, step: str, data: bytes) -> None:
        if not step.startswith("cv-final-"):
            raise WorkspaceError("只有最终 PDF 以字节保存")
        if step not in STEPS:
            raise WorkspaceError(f"未知的步骤：{step}")
        self._replace_step(job_id, step, data)

    def jobs(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._jobs_locked()

    def _jobs_locked(self) -> list[dict[str, Any]]:
        """Summaries of every job, newest first, read from each job's input step."""
        if not self.root.is_dir():
            return []
        summaries = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            if directory.is_symlink() or not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
                continue
            jd = (self.read(directory.name, "input") or {}).get("jd", {})
            summaries.append({
                "job_id": directory.name,
                "title": jd.get("title"),
                "company": jd.get("company") or jd.get("board"),
                "source": jd.get("source"),
                "captured_at": jd.get("captured_at"),
                "steps": self.state(directory.name),
            })
        return summaries
