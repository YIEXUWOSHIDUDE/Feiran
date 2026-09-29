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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path(".local/jobs")
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
# Written before a step is replaced and removed once the new file is in place.
JOURNAL = "change-in-progress.json"


def _sync_folder(folder: Path) -> None:
    """Make renames in a folder last through a power cut: they live in the folder's own data."""
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_atomically(path: Path, data: bytes) -> None:
    """Write all of a file or none of it: a temporary file beside it, flushed to disk, then
    renamed over it. A crash leaves the old file or the new one, never part of one."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
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


def remove_leftovers(folder: Path) -> None:
    """Delete the temporary files a crash left behind in a folder (write_atomically's)."""
    if folder.is_dir():
        for stray in folder.glob(".*.tmp"):
            stray.unlink()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkspaceError(Exception):
    """A job or step name is invalid, or the requested step cannot be stored."""


def _file_name(step: str) -> str:
    return f"{step}.pdf" if step.startswith("cv-final-") else f"{step}.json"


STEP_FILES = frozenset(_file_name(step) for step in STEPS)


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

    def _job_dir(self, job_id: Any) -> Path:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise WorkspaceError("岗位编号格式无效")
        directory = self.root / job_id
        if not directory.is_dir():
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
        (self.root / job_id).mkdir(parents=True)
        try:
            self.write(job_id, "input", review_input)
        except Exception:
            (self.root / job_id).rmdir()  # empty again: the write undid itself
            raise
        return job_id

    def read(self, job_id: str, step: str) -> dict[str, Any] | None:
        path = self.path(job_id, step)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def state(self, job_id: str) -> list[str]:
        return [step for step in STEPS if self.path(job_id, step).exists()]

    def _replace_step(self, job_id: str, step: str, data: bytes) -> None:
        """Move the step and every step derived from it into history, then write the new file.
        The journal names what moved: an error undoes the change at once, a crash at the next
        start (recover)."""
        directory = self._job_dir(job_id)
        if (directory / JOURNAL).exists():
            self._finish(directory)
        moving = [path for path in (self.path(job_id, later) for later in _later_steps(step)) if path.exists()]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        journal = {"step": step, "history": stamp if moving else None, "moved": [path.name for path in moving],
                   "sha256": hashlib.sha256(data).hexdigest(), "started_at": _now()}
        write_atomically(directory / JOURNAL, json.dumps(journal).encode("utf-8"))
        try:
            if moving:
                history = directory / "history" / stamp
                history.mkdir(parents=True)
                for path in moving:
                    path.rename(history / path.name)
                _sync_folder(history)
            write_atomically(self.path(job_id, step), data)
        except Exception:
            self._finish(directory)
            raise
        (directory / JOURNAL).unlink()
        _sync_folder(directory)
        # Redoing the cut-short step, or one it depends on, settles the note about it; other work
        # (the page checks requirements by itself) leaves it for the user to read.
        interrupted = self.read_note(job_id, "interrupted")
        if interrupted and interrupted.get("step") in _later_steps(step):
            self._note_path(job_id, "interrupted").unlink()

    def _finish(self, directory: Path) -> bool:
        """Close the change the journal describes. If its new file was written in full, it stands;
        otherwise what it moved goes back. Returns whether it was undone. Nothing is written
        here but files put back, so this can never approve a CV or confirm anything."""
        journal = json.loads((directory / JOURNAL).read_text(encoding="utf-8"))
        target = directory / _file_name(journal["step"])
        finished = target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == journal["sha256"]
        if not finished and journal["history"]:
            history = directory / "history" / journal["history"]
            for name in journal["moved"]:
                if name in STEP_FILES and (history / name).exists():
                    (history / name).rename(directory / name)
            for folder in (history, history.parent):  # history/ too, if this change made it
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
        remove_leftovers(directory)
        (directory / JOURNAL).unlink()
        _sync_folder(directory)
        return not finished

    def recover(self) -> list[dict[str, str]]:
        """At start, close every change a crash cut short, and say which were undone. A job so
        marked tells the page (the interrupted note) until the next change to it."""
        undone: list[dict[str, str]] = []
        if not self.root.is_dir():
            return undone
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
                continue
            if (directory / JOURNAL).exists():
                step = json.loads((directory / JOURNAL).read_text(encoding="utf-8"))["step"]
                if self._finish(directory) and any(directory.iterdir()):
                    self.write_note(directory.name, "interrupted", {"step": step, "undone_at": _now()})
                    undone.append({"job_id": directory.name, "step": step})
            remove_leftovers(directory)
            if not any(directory.iterdir()):
                directory.rmdir()  # a job whose creation was cut short before it held anything
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
        write_atomically(self._note_path(job_id, name), (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def read_note(self, job_id: str, name: str) -> dict[str, Any] | None:
        path = self._note_path(job_id, name)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write_bytes(self, job_id: str, step: str, data: bytes) -> None:
        if not step.startswith("cv-final-"):
            raise WorkspaceError("只有最终 PDF 以字节保存")
        if step not in STEPS:
            raise WorkspaceError(f"未知的步骤：{step}")
        self._replace_step(job_id, step, data)

    def jobs(self) -> list[dict[str, Any]]:
        """Summaries of every job, newest first, read from each job's input step."""
        if not self.root.is_dir():
            return []
        summaries = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            if not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
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
