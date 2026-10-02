"""V2's bounded background execution, inside the one application process.

Pages submit work (tenant_store.UserWorkspace.submit_task) and read its state; this runner
claims queued tasks in short transactions and runs each handler in a worker thread. At most
``total`` tasks run at once, at most ``pdf`` of them printing, and one per user. No task runs
inside a database transaction, and a handler's results are published only while its execution
token is valid. There is deliberately no second process, Redis or Celery: one Uvicorn worker
runs this app (see docs/v2-plan.md for when that would change).
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import run_log
import tenant_store
from deepseek_client import chat_json
from tenant_store import LateResult, NotFound, StoreError, UserWorkspace
from v2_flow import TaskFailed


log = logging.getLogger("workbench.tasks")


@dataclass
class TaskContext:
    """One running task as its handler sees it."""

    database: Path
    user: UserWorkspace
    task_id: str
    token: str
    operation: str
    request: dict
    job_id: str | None
    model: Callable[..., dict] = field(repr=False)

    def chat(self, messages: list[dict], model: str, effort: str) -> dict:
        """The model, metered for this task: before each call the task is marked as possibly
        costing money (a cancelled or timed-out task raises LateResult here and no call is made);
        after it, the reported token counts are added under this execution's token, so an answer
        that comes after the task ended never changes a later attempt's accounting. A call that
        raises leaves the task's cost unknown."""
        tenant_store.mark_spending(self.database, self.user.user_id, self.task_id, self.token, "model")
        answer = self.model(messages, model=model, effort=effort)
        tenant_store.record_usage(self.database, self.user.user_id, self.task_id, self.token, answer.get("usage"))
        return answer

    def stage(self, name: str) -> None:
        tenant_store.set_stage(self.database, self.user.user_id, self.task_id, self.token, name)

    def finish(self, status: str, **details: Any) -> None:
        tenant_store.finish_task(self.database, self.user.user_id, self.task_id, self.token, status, **details)

    def fail(self, code: str, message: str) -> None:
        self.finish("failed", error_code=code, message=message)


class TaskRunner:
    def __init__(self, database: Path, handlers: dict[str, Callable[[TaskContext], None]], *,
                 total: int = 2, pdf: int = 1, deadline: timedelta = timedelta(minutes=10),
                 poll_seconds: float = 1.0, model: Callable[..., dict] = chat_json) -> None:
        if total < 1 or not 0 <= pdf <= total:
            raise ValueError("The runner needs at least one slot, and no more PDF slots than slots")
        self.database, self.handlers = Path(database), handlers
        self.total, self.pdf, self.deadline, self.poll = total, pdf, deadline, poll_seconds
        self.model = model
        self._wake = threading.Condition()
        self._running: dict[str, str] = {}  # task_id -> heavy kind, for this process's slots
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=total, thread_name_prefix="task")

    def start(self) -> int:
        """Mark what the last process left running as interrupted, then start claiming."""
        interrupted = tenant_store.recover_tasks(self.database)
        if interrupted:
            run_log.event(log, "tasks_interrupted", level=logging.WARNING, count=interrupted)
        self._thread = threading.Thread(target=self._loop, name="task-dispatch", daemon=True)
        self._thread.start()
        return interrupted

    def stop(self, timeout: float = 30) -> None:
        """Stop claiming; running handlers finish (or are interrupted at the next start)."""
        self._stop.set()
        self.wake()
        if self._thread is not None:
            self._thread.join(timeout)
        self._pool.shutdown(wait=True, cancel_futures=True)

    def wake(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def idle(self) -> bool:
        with self._wake:
            return not self._running

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._dispatch()
            except StoreError:
                run_log.event(log, "dispatch_failed", level=logging.ERROR, error=True)
            with self._wake:
                self._wake.wait(self.poll)

    def _dispatch(self) -> None:
        tenant_store.expire_tasks(self.database)
        with self._wake:
            free = self.total - len(self._running)
            free_pdf = self.pdf - sum(kind == "pdf" for kind in self._running.values())
        if free <= 0:
            return
        for claimed in tenant_store.claim_tasks(self.database, total=free, pdf=free_pdf, deadline=self.deadline):
            with self._wake:
                self._running[claimed["task_id"]] = claimed["heavy"]
            self._pool.submit(self._run, claimed)

    def _run(self, claimed: dict[str, Any]) -> None:
        context = TaskContext(self.database, UserWorkspace(self.database, claimed["user_id"]), claimed["task_id"],
                              claimed["token"], claimed["operation"], claimed["request"], claimed["job_id"],
                              model=self.model)
        try:
            handler = self.handlers.get(claimed["operation"])
            if handler is None:
                raise TaskFailed("This kind of work is not available", "unknown_operation")
            handler(context)
            outcome = "done"
        except LateResult:
            outcome = "late"  # cancelled, timed out or superseded meanwhile: nothing is published
        except NotFound:
            outcome = "gone"  # the account or job is no longer there
            self._end(context, "not_found", "This is no longer available")
        except TaskFailed as exc:
            outcome = "failed"
            self._end(context, exc.code, str(exc))
        except StoreError as exc:
            outcome = "refused"
            self._end(context, getattr(exc, "code", "refused"), str(exc))
        except Exception:  # a bug: the page gets a plain message, the log the error's type and place
            outcome = "error"
            run_log.event(log, "task_error", level=logging.ERROR, error=True, operation=claimed["operation"])
            self._end(context, "internal_error", "Something went wrong; try again, or report it if it happens again")
        finally:
            with self._wake:
                self._running.pop(claimed["task_id"], None)
                self._wake.notify_all()
        run_log.event(log, "task", operation=claimed["operation"], outcome=outcome)

    def _end(self, context: TaskContext, code: str, message: str) -> None:
        """Fail a task unless it has already ended (it may have been cancelled meanwhile)."""
        try:
            context.fail(code, message)
        except (LateResult, NotFound):
            pass
        except StoreError:
            run_log.event(log, "task_end_failed", level=logging.ERROR, error=True, operation=context.operation)

    def drain(self, timeout: float = 30) -> bool:
        """For tests and shutdown: wait until nothing is queued or running; False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.wake()
            with tenant_store.transaction(self.database) as connection:
                active = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN ('queued', 'running')").fetchone()[0]
            if not active and self.idle():
                return True
            time.sleep(0.02)
        return False
