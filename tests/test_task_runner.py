"""The bounded in-process task runner: concurrency limits, editing while work runs, deadlines,
late results, restart recovery and what a failure tells the user. Scripted handlers only."""
import json
import logging
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

import run_log
import tenant_store
from task_runner import TaskRunner
from tenant_store import UserWorkspace, claim_tasks, initialize_store, provision_user, set_setting
from v2_flow import TaskFailed


class Gate:
    """Holds handlers until released, counting how many (and which kinds) run at once."""

    def __init__(self):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running, self.most, self.most_pdf, self.pdf = set(), 0, 0, 0
        self.users_seen_together = []

    def handler(self, kind):
        def run(ctx):
            with self.lock:
                self.running.add(ctx.user.user_id + ctx.task_id)
                self.pdf += kind == "pdf"
                self.most = max(self.most, len(self.running))
                self.most_pdf = max(self.most_pdf, self.pdf)
                self.users_seen_together.append(sorted(key[:32] for key in self.running))
            self.release.wait(10)
            with self.lock:
                self.running.discard(ctx.user.user_id + ctx.task_id)
                self.pdf -= kind == "pdf"
            ctx.finish("succeeded", result={"ok": True})
        return run


class RunnerCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "v2.db"
        initialize_store(self.database)
        set_setting(self.database, "user_daily_units", "50")
        set_setting(self.database, "site_daily_units", "500")
        self.users = [UserWorkspace(self.database, provision_user(self.database, issuer="https://issuer", subject=str(n)))
                      for n in range(4)]

    def submit(self, user, operation="model_work", key="k", heavy="model"):
        return user.submit_task(operation, key=key, request={"key": key}, units=1, heavy=heavy)

    def runner(self, handlers, **options):
        runner = TaskRunner(self.database, handlers, poll_seconds=0.02, **options)
        runner.start()
        self.addCleanup(runner.stop, 5)
        return runner

    def wait(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError("waited in vain")
            time.sleep(0.01)


class LimitTests(RunnerCase):
    def test_at_most_two_run_and_one_prints_while_editing_goes_on(self):
        gate = Gate()
        runner = self.runner({"model_work": gate.handler("model"), "pdf_work": gate.handler("pdf")}, total=2, pdf=1)
        for index, user in enumerate(self.users):
            self.submit(user, "pdf_work" if index < 2 else "model_work", key="a", heavy="pdf" if index < 2 else "model")
        self.wait(lambda: len(gate.running) == 2)
        started = time.monotonic()
        self.users[0].import_facts([{"text": "Edited while the model works.", "type": "project", "tags": []}])
        self.assertLess(time.monotonic() - started, 1.0)  # no task holds the database
        time.sleep(0.2)
        self.assertEqual(len(gate.running), 2)
        gate.release.set()
        self.assertTrue(runner.drain(10))
        self.assertEqual((gate.most, gate.most_pdf), (2, 1))
        for together in gate.users_seen_together:
            self.assertEqual(len(together), len(set(together)))  # never two of one user's tasks at once
        statuses = [task["status"] for user in self.users for task in [user.get_task(t["task_id"]) for t in
                    tenant_store_tasks(self.database, user.user_id)]]
        self.assertEqual(set(statuses), {"succeeded"})


def tenant_store_tasks(database, user_id):
    with tenant_store.transaction(database) as connection:
        return [dict(row) for row in connection.execute("SELECT task_id FROM tasks WHERE user_id = ?", (user_id,))]


class FailureTests(RunnerCase):
    def test_a_task_past_its_deadline_fails_and_its_late_result_is_refused(self):
        finished = threading.Event()
        outcome = []

        def slow(ctx):
            time.sleep(1.2)
            try:
                ctx.finish("succeeded", result={"late": True})
            except tenant_store.LateResult:
                outcome.append("refused")
            finished.set()
        runner = self.runner({"model_work": slow}, deadline=timedelta(seconds=0.5))
        task = self.submit(self.users[0])
        self.assertTrue(finished.wait(10))
        self.assertTrue(runner.drain(10))
        view = self.users[0].get_task(task["task_id"])
        self.assertEqual((view["status"], view["error_code"], view["result"]), ("failed", "timeout", None))
        self.assertEqual(outcome, ["refused"])

    def test_a_failed_call_keeps_the_cost_unknown_after_a_later_call_answers(self):
        from deepseek_client import DeepSeekError
        answers = [DeepSeekError("lost", reason="unreachable"),
                   {"model": "m", "content": {}, "usage": {"prompt_tokens": 5, "completion_tokens": 1}}]

        def model(messages, model, effort):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        def two_calls(ctx):
            try:
                ctx.chat([{"role": "user", "content": "a"}], model="m", effort="none")
            except DeepSeekError:
                pass  # a stage may go on without it, as rewording does
            ctx.chat([{"role": "user", "content": "b"}], model="m", effort="none")
            ctx.finish("succeeded", result={})
        runner = self.runner({"model_work": two_calls}, model=model)
        task = self.submit(self.users[0], key="two")
        self.assertTrue(runner.drain(5))
        view = self.users[0].get_task(task["task_id"])
        self.assertEqual((view["status"], view["cost"]), ("succeeded", "unknown"))

    def test_a_call_cut_off_by_a_restart_keeps_the_cost_unknown_after_a_successful_retry(self):
        task = self.submit(self.users[0], key="restart")
        token = claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))[0]["token"]
        tenant_store.mark_spending(self.database, self.users[0].user_id, task["task_id"], token, "model")  # then the process dies

        def answers(ctx):
            ctx.chat([{"role": "user", "content": "again"}], model="m", effort="none")
            ctx.finish("succeeded", result={})
        runner = self.runner({"model_work": answers},
                             model=lambda messages, model, effort: {"model": "m", "content": {}, "usage": {"prompt_tokens": 3}})
        self.assertEqual(self.users[0].get_task(task["task_id"])["status"], "interrupted")
        self.submit(self.users[0], key="restart")  # the user runs it again
        runner.wake()
        self.assertTrue(runner.drain(5))
        view = self.users[0].get_task(task["task_id"])
        self.assertEqual((view["status"], view["attempts"], view["cost"]), ("succeeded", 2, "unknown"))

    def test_no_model_call_is_made_after_the_deadline(self):
        calls = []

        def model(messages, model, effort):
            calls.append(messages)
            return {"model": "m", "content": {}, "usage": {"prompt_tokens": 1}}

        def slow_then_call(ctx):
            time.sleep(0.8)
            ctx.chat([{"role": "user", "content": "late"}], model="m", effort="none")
            ctx.finish("succeeded", result={})
        runner = self.runner({"model_work": slow_then_call}, model=model, deadline=timedelta(seconds=0.3))
        task = self.submit(self.users[0], key="late-call")
        self.assertTrue(runner.drain(10))
        self.assertEqual(calls, [])
        self.wait(lambda: self.users[0].get_task(task["task_id"])["status"] == "failed")
        self.assertEqual(self.users[0].get_task(task["task_id"])["error_code"], "timeout")

    def test_restart_interrupts_what_the_last_process_left_running(self):
        task = self.submit(self.users[0])
        claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))  # "running" in a process now gone
        ran = []
        runner = self.runner({"model_work": lambda ctx: ran.append(ctx.task_id)})
        self.assertTrue(runner.drain(5))
        self.assertEqual(self.users[0].get_task(task["task_id"])["status"], "interrupted")
        self.assertEqual(ran, [])  # never rerun by itself

    def test_failures_tell_the_user_what_happened_but_never_leak_error_text(self):
        def refuses(ctx):
            raise TaskFailed("请先准备这个岗位的简历", "no_cv")

        def breaks(ctx):
            raise RuntimeError("secret CV line: Alex Example worked at Example Corp")
        runner = self.runner({"refuses": refuses, "breaks": breaks})
        # Capture before submitting: the running runner may take the tasks at once.
        with self.assertLogs("workbench.tasks", level="INFO") as logs:
            refused = self.submit(self.users[0], "refuses", key="r")
            broken = self.submit(self.users[1], "breaks", key="b")
            unknown = self.submit(self.users[2], "no_such_work", key="u")
            self.assertTrue(runner.drain(5))
        self.assertEqual((self.users[0].get_task(refused["task_id"])["error_code"],
                          self.users[0].get_task(refused["task_id"])["message"]), ("no_cv", "请先准备这个岗位的简历"))
        view = self.users[1].get_task(broken["task_id"])
        self.assertEqual(view["error_code"], "internal_error")
        self.assertNotIn("Alex", view["message"])
        self.assertEqual(self.users[2].get_task(unknown["task_id"])["error_code"], "unknown_operation")
        # As the container logs them (JSON): the error's type and place, never its words.
        lines = [run_log.JsonLines().format(record) for record in logs.records]
        self.assertTrue(any('"error": "RuntimeError"' in line for line in lines))
        self.assertFalse(any("Alex" in line or "Example Corp" in line for line in lines))
        self.assertEqual(self.users[1].usage_today()["used"], 0)  # nothing was spent: the units came back

    def test_the_model_is_metered_per_task_and_never_called_for_a_cancelled_one(self):
        calls = []

        def model(messages, model, effort):
            calls.append(messages)
            return {"model": "m", "content": {}, "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
        cancel_first = threading.Event()

        def asks(ctx):
            if ctx.request["key"] == "cancel":
                ctx.user.cancel_task(ctx.task_id)
            ctx.chat([{"role": "user", "content": "hi"}], model="deepseek-flash", effort="none")
            ctx.finish("succeeded", result={})
        runner = self.runner({"model_work": asks}, model=model)
        paid = self.submit(self.users[0], key="paid")
        cancelled = self.submit(self.users[1], key="cancel")
        self.assertTrue(runner.drain(5))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.users[0].get_task(paid["task_id"])["cost"], "known")
        self.assertEqual(self.users[1].get_task(cancelled["task_id"])["status"], "cancelled")
        with tenant_store.transaction(self.database) as connection:
            usage = connection.execute("SELECT usage FROM tasks WHERE task_id = ?", (paid["task_id"],)).fetchone()[0]
            calls = [dict(row) for row in connection.execute(
                "SELECT status, prompt_tokens, completion_tokens FROM model_calls WHERE task_id = ?", (paid["task_id"],))]
        # The task keeps its call counters; the tokens are the call's, in the ledger (never the task's own).
        self.assertEqual(json.loads(usage), {"tokens": {}, "calls": 1, "open_calls": 0, "attempt_calls": 1,
                                             "late_calls": 0})
        self.assertEqual(calls, [{"status": "settled", "prompt_tokens": 7, "completion_tokens": 2}])
        self.assertEqual(self.users[0].get_task(paid["task_id"])["model_use"]["total"], 9)
        self.assertFalse(cancel_first.is_set())



class Gated:
    """A handler each call of which starts, waits until the test releases it, then tries to
    finish, in the order the calls were dispatched: the test decides when each one returns, so
    the order below is certain, not a race."""

    def __init__(self, calls=4):
        self.started = [threading.Event() for _ in range(calls)]
        self.release = [threading.Event() for _ in range(calls)]
        self.returned = [threading.Event() for _ in range(calls)]
        self.users, self.late = [], []
        self.lock = threading.Lock()

    def __call__(self, ctx):
        with self.lock:
            index = len(self.users)
            self.users.append(ctx.user.user_id)
        self.started[index].set()
        try:
            if not self.release[index].wait(10):
                raise RuntimeError("never released")
            try:
                ctx.finish("succeeded", result={"call": index})
            except tenant_store.LateResult:
                self.late.append(index)
        finally:
            self.returned[index].set()

    def open_all(self):
        for event in self.release:
            event.set()


class ExecutionTests(RunnerCase):
    """A cancelled or timed-out task's handler keeps its user's turn and its slot until it returns,
    and a retry of the same task never takes over that place (docs/architecture-handoff.md,
    round 1 C). The runner is dispatched by hand, between gated handler steps."""

    def runner_for(self, gated, **options):
        runner = TaskRunner(self.database, {"model_work": gated, "pdf_work": gated}, **options)
        self.addCleanup(runner.stop, 5)
        self.addCleanup(gated.open_all)  # first: the handlers return, then the runner stops
        return runner

    def test_cancel_retry_retains_actual_user_slot_until_old_handler_exits(self):
        gated = Gated()
        runner = self.runner_for(gated, total=2)
        task = self.submit(self.users[0])
        runner._dispatch()
        self.assertTrue(gated.started[0].wait(2))
        self.users[0].cancel_task(task["task_id"])
        retry = self.submit(self.users[0])
        self.assertEqual(retry["task_id"], task["task_id"])
        runner._dispatch()
        self.assertFalse(gated.started[1].wait(0.2))  # the cancelled handler still runs: the user waits
        self.assertFalse(runner.idle())
        self.assertEqual(self.users[0].get_task(task["task_id"])["status"], "queued")
        gated.release[0].set()
        self.assertTrue(gated.returned[0].wait(2))
        self.wait(runner.idle)  # only now is its place given back
        runner._dispatch()
        self.assertTrue(gated.started[1].wait(2))
        self.assertFalse(runner.idle())  # the retry occupies a slot while it runs
        self.assertEqual(self.users[0].get_task(task["task_id"])["status"], "running")
        self.assertEqual(gated.late, [0])  # the cancelled execution's result was refused
        gated.release[1].set()
        self.assertTrue(runner.drain(5))
        view = self.users[0].get_task(task["task_id"])
        self.assertEqual((view["status"], view["result"], view["attempts"]), ("succeeded", {"call": 1}, 2))

    def test_a_timed_out_handler_keeps_its_users_turn_while_others_use_the_free_slot(self):
        gated = Gated()
        runner = self.runner_for(gated, total=2, deadline=timedelta(seconds=0.3))
        task = self.submit(self.users[0])
        runner._dispatch()
        self.assertTrue(gated.started[0].wait(2))
        time.sleep(0.4)
        runner._dispatch()  # past its deadline: ended as timed out, its handler still waiting
        self.assertEqual(self.users[0].get_task(task["task_id"])["error_code"], "timeout")
        retry = self.submit(self.users[0])
        other = self.submit(self.users[1])
        third = self.submit(self.users[2])
        runner._dispatch()
        # The other user gets the free slot; the retry waits for the old handler; the third user
        # waits for a slot (two handlers run, one of them the timed-out one).
        self.assertTrue(gated.started[1].wait(2))
        self.assertEqual(gated.users[1], self.users[1].user_id)
        self.assertFalse(gated.started[2].wait(0.2))
        self.assertEqual([self.users[n].get_task(t["task_id"])["status"] for n, t in ((0, retry), (1, other), (2, third))],
                         ["queued", "running", "queued"])
        gated.release[0].set()
        self.assertTrue(gated.returned[0].wait(2))
        self.wait(lambda: len(runner._running) == 1)
        runner._dispatch()
        self.assertTrue(gated.started[2].wait(2))
        self.assertEqual(gated.users[2], self.users[0].user_id)  # the older retry, now its user is free
        self.assertEqual(gated.late, [0])
        gated.open_all()
        self.wait(lambda: len(runner._running) == 0)
        runner._dispatch()
        self.assertTrue(gated.started[3].wait(2))
        self.assertEqual(gated.users[3], self.users[2].user_id)
        self.assertTrue(runner.drain(5))

    def test_a_cancelled_print_keeps_the_print_slot_until_it_returns(self):
        gated = Gated()
        runner = self.runner_for(gated, total=2, pdf=1)
        printing = self.submit(self.users[0], "pdf_work", key="p0", heavy="pdf")
        runner._dispatch()
        self.assertTrue(gated.started[0].wait(2))
        self.users[0].cancel_task(printing["task_id"])
        waiting = self.submit(self.users[1], "pdf_work", key="p1", heavy="pdf")
        model = self.submit(self.users[2], key="m2")
        runner._dispatch()
        self.assertTrue(gated.started[1].wait(2))
        self.assertEqual(gated.users[1], self.users[2].user_id)  # a model slot is free
        self.assertEqual(self.users[1].get_task(waiting["task_id"])["status"], "queued")  # the print slot is not
        gated.release[0].set()
        self.assertTrue(gated.returned[0].wait(2))
        self.wait(lambda: len(runner._running) == 1)
        runner._dispatch()
        self.assertTrue(gated.started[2].wait(2))
        self.assertEqual(gated.users[2], self.users[1].user_id)
        gated.open_all()
        self.assertTrue(runner.drain(5))
        self.assertEqual([self.users[n].get_task(t["task_id"])["status"] for n, t in ((0, printing), (1, waiting), (2, model))],
                         ["cancelled", "succeeded", "succeeded"])


if __name__ == "__main__":
    unittest.main()
