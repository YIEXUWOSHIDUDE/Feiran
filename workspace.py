"""One folder per job under .local/jobs, holding the current file of each workflow step.

Redoing a step never overwrites: the old step and every step derived from it move
into history/<time>/ first, so the folder always shows one consistent chain.
"""

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path(".local/jobs")
JOB_STEPS = ("input", "candidates", "decided", "matches", "linked")
CV_STEPS = ("cv-draft", "cv-tailored", "cv-approved", "cv-final")
LANGUAGES = ("en", "zh")
STEPS = JOB_STEPS + tuple(f"{step}-{language}" for language in LANGUAGES for step in CV_STEPS)
JOB_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")


class WorkspaceError(Exception):
    """A job or step name is invalid, or the requested step cannot be stored."""


def _file_name(step: str) -> str:
    return f"{step}.pdf" if step.startswith("cv-final-") else f"{step}.json"


def _later_steps(step: str) -> list[str]:
    """The step itself plus everything derived from it."""
    if step in JOB_STEPS:
        return list(JOB_STEPS[JOB_STEPS.index(step):]) + list(STEPS[len(JOB_STEPS):])
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
        self.write(job_id, "input", review_input)
        return job_id

    def read(self, job_id: str, step: str) -> dict[str, Any] | None:
        path = self.path(job_id, step)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def state(self, job_id: str) -> list[str]:
        return [step for step in STEPS if self.path(job_id, step).exists()]

    def _archive_from(self, job_id: str, step: str) -> None:
        existing = [self.path(job_id, later) for later in _later_steps(step)]
        existing = [path for path in existing if path.exists()]
        if not existing:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        history = self._job_dir(job_id) / "history" / stamp
        history.mkdir(parents=True)
        for path in existing:
            path.rename(history / path.name)

    def write(self, job_id: str, step: str, data: dict[str, Any]) -> None:
        if step.startswith("cv-final-"):
            raise WorkspaceError("PDF 请用 write_bytes 保存")
        self._archive_from(job_id, step)
        with self.path(job_id, step).open("x", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")

    def write_bytes(self, job_id: str, step: str, data: bytes) -> None:
        if not step.startswith("cv-final-"):
            raise WorkspaceError("只有最终 PDF 以字节保存")
        self._archive_from(job_id, step)
        with self.path(job_id, step).open("xb") as output:
            output.write(data)

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
