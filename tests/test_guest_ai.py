"""Guest workspaces with AI and no time limit, with synthetic data and a scripted model: the
one-time token allowance of each workspace (across UTC days, restarts, retries, late, unknown and
duplicate reports, overages), all guests' daily allowance (kept when a workspace ends and in
backups), the reservation of what is left before each call, the caps in either quota mode and
with an exemption, signed-in accounts' quotas unchanged, consent and the routes a guest may use,
editing and printing after the allowance is used, workspaces and sessions without an end (the
persistent cookie, restarts, the migration of workspaces made with a fixed end) and the guest
page's state in both languages. Nothing leaves the machine."""
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from feiran import guest_sample, tenant_store, v2_backup
from feiran.tenant_store import (GUEST_NO_END, LateResult, Refused, StoreError, UserWorkspace, begin_call, claim_tasks,
                                 configure_quota, fail_call, finish_task, initialize_store, provision_user,
                                 recover_tasks, set_setting, settle_call)
from tests import test_sponsor_ui
from tests.fake_identity import ORIGIN
from tests.test_guest_workspace import START_COOKIE, GuestCase, guest_ids, rows_naming

DEADLINE = timedelta(minutes=5)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def usage(prompt, completion):
    return {"prompt_tokens": prompt, "completion_tokens": completion}


class GuestMeterCase(unittest.TestCase):
    """The store alone: guest workspaces seeded with the sample, a signed-in user, legacy limits."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name) / "data"
        self.database = self.data / tenant_store.DATABASE
        initialize_store(self.database)
        self.clock = Clock()
        clock = patch.object(tenant_store, "_clock", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        for key, value in [("user_daily_units", "20"), ("site_daily_units", "1000"), ("guest_enabled", "1"),
                           ("guest_starts_per_hour", "200")]:
            set_setting(self.database, key, value)
        self.alice = UserWorkspace(self.database, provision_user(self.database, issuer="https://issuer", subject="alice"))
        self.starts = 0

    def guest(self):
        """A new guest workspace with the sample in it; returns (UserWorkspace, job_id)."""
        self.starts += 1
        user_id, made = tenant_store.start_guest(self.database, start=f"synthetic-start-{self.starts:03d}-" + "x" * 20)
        self.assertTrue(made)
        job_id = guest_sample.seed(UserWorkspace(self.database, user_id))
        tenant_store.guest_ready(self.database, user_id)
        return UserWorkspace(self.database, user_id), job_id

    def running(self, user, job_id, key, operation="tailor_cv"):
        """A model task of ``user`` submitted and started; returns (task_id, token)."""
        task = user.submit_task(operation, key=key, request={"job_id": job_id, "key": key}, units=1, heavy="model",
                                job_id=job_id)
        claimed = [item for item in claim_tasks(self.database, total=2, pdf=1, deadline=DEADLINE)
                   if item["task_id"] == task["task_id"]]
        self.assertTrue(claimed, "the task was not started")
        return task["task_id"], claimed[0]["token"]

    def ai(self, user):
        return user.guest()["ai"]

    def spent(self, user, job_id, key, prompt, completion):
        """One task with one call that reports ``prompt`` + ``completion`` tokens."""
        task_id, token = self.running(user, job_id, key)
        call, _budget = begin_call(self.database, user.user_id, task_id, token, prompt_tokens=500, max_output=1024)
        self.assertEqual(settle_call(self.database, user.user_id, call, usage(prompt, completion)), "settled")
        finish_task(self.database, user.user_id, task_id, token, "succeeded")


class LifetimeAllowanceTests(GuestMeterCase):
    def test_it_is_given_once_across_days_restarts_and_retries_and_overages_are_charged(self):
        guest, job_id = self.guest()
        self.assertEqual(self.ai(guest), {"limit": 100_000, "used": 0, "remaining": 100_000, "refills": False,
                                          "reported": 0, "reserved": 0, "pending_calls": 0, "unknown_calls": 0})
        task_id, token = self.running(guest, job_id, "first")
        call, budget = begin_call(self.database, guest.user_id, task_id, token, prompt_tokens=1_000, max_output=8_192)
        self.assertEqual(budget, 8_192)
        self.assertEqual(self.ai(guest)["used"], 9_192)  # held while the call runs
        # More than was reserved is charged as reported, not hidden.
        self.assertEqual(settle_call(self.database, guest.user_id, call, usage(30_000, 20_000)), "settled")
        self.assertEqual(settle_call(self.database, guest.user_id, call, usage(1, 1)), "settled")  # a duplicate: no change
        self.assertEqual(self.ai(guest)["used"], 50_000)
        # A retry of the transport call that brings no usage back keeps its whole reservation.
        call, _budget = begin_call(self.database, guest.user_id, task_id, token, prompt_tokens=1_000, max_output=8_192)
        self.assertEqual(fail_call(self.database, guest.user_id, call, "http_500"), "unknown")
        self.assertEqual((self.ai(guest)["used"], self.ai(guest)["unknown_calls"]), (59_192, 1))
        finish_task(self.database, guest.user_id, task_id, token, "failed", error_code="model_error", message="x")
        # The next UTC day, after a restart, the same request sent again: nothing comes back.
        self.clock.advance(days=1)
        recover_tasks(self.database)
        self.assertEqual(self.ai(guest)["used"], 59_192)
        task_id, token = self.running(guest, job_id, "first")  # the same key: a retry of the same task
        self.assertEqual(self.ai(guest)["remaining"], 40_808)
        call, budget = begin_call(self.database, guest.user_id, task_id, token, prompt_tokens=1_000, max_output=8_192)
        self.assertEqual(settle_call(self.database, guest.user_id, call, usage(39_000, 6_000)), "settled")
        finish_task(self.database, guest.user_id, task_id, token, "succeeded")
        state = self.ai(guest)
        self.assertEqual((state["used"], state["remaining"]), (104_192, 0))  # over by what was reported
        with self.assertRaises(Refused) as refused:
            guest.submit_task("plan_cv", key="after", request={"job_id": job_id}, units=1, heavy="model", job_id=job_id)
        self.assertEqual(refused.exception.code, "guest_ai_exhausted")
        self.clock.advance(days=30)
        with self.assertRaises(Refused):
            guest.submit_task("plan_cv", key="later", request={"job_id": job_id}, units=1, heavy="model", job_id=job_id)
        self.assertEqual(guest.guest()["ai"]["used"], 104_192)  # never refilled, never reset

    def test_each_call_reserves_only_what_is_left_and_two_calls_never_both_spend_it(self):
        set_setting(self.database, "guest_lifetime_tokens", "10000")
        guest, job_id = self.guest()
        task_id, token = self.running(guest, job_id, "k")
        answers, barrier = [], threading.Barrier(2)

        def start():
            barrier.wait()
            try:
                answers.append(begin_call(self.database, guest.user_id, task_id, token, prompt_tokens=1_000,
                                          max_output=8_192)[1])
            except Refused as exc:
                answers.append(exc.code)
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(answers, key=str), [8_192, "guest_ai_exhausted"])
        self.assertLessEqual(self.ai(guest)["used"], 10_000)
        # What is left is all a call may reserve, and less than one call's minimum is refused.
        set_setting(self.database, "guest_lifetime_tokens", "14000")
        call, budget = begin_call(self.database, guest.user_id, task_id, token, prompt_tokens=1_000, max_output=8_192)
        self.assertEqual(budget, 14_000 - 9_192 - 1_000)
        with self.assertRaises(Refused):
            begin_call(self.database, guest.user_id, task_id, token, prompt_tokens=10, max_output=10)
        # A late answer, after the task was cancelled, still settles its own call.
        guest.cancel_task(task_id)
        self.assertEqual(settle_call(self.database, guest.user_id, call, usage(100, 50)), "settled")
        with self.assertRaises(LateResult):
            begin_call(self.database, guest.user_id, task_id, token, prompt_tokens=10, max_output=10)
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT late FROM model_calls WHERE call_id = ?", (call,)).fetchone()[0], 1)

    def test_a_retried_or_resent_request_never_resets_it(self):
        guest, job_id = self.guest()
        self.spent(guest, job_id, "same", 20_000, 5_000)
        for _ in range(3):  # the same request again finds the same finished task: no new allowance
            again = guest.submit_task("tailor_cv", key="same", request={"job_id": job_id, "key": "same"}, units=1,
                                      heavy="model", job_id=job_id)
            self.assertEqual(again["status"], "succeeded")
        self.assertEqual(self.ai(guest)["used"], 25_000)


class CapTests(GuestMeterCase):
    def test_the_guest_caps_hold_in_tokens_mode_and_with_an_exemption_and_the_site_cap_too(self):
        configure_quota(self.database, "tokens", user_daily_tokens=1_000_000, site_daily_tokens=10_000_000)
        set_setting(self.database, "guest_lifetime_tokens", "5000")
        guest, job_id = self.guest()
        with self.assertRaises(StoreError):  # no operator can exempt a guest
            tenant_store.set_test_unlimited(self.database, guest.user_id, enabled=True)
        with tenant_store.transaction(self.database, write=True) as connection:  # nor a row written some other way
            connection.execute("INSERT INTO quota_exemptions VALUES (?, 'test_unlimited', ?)", (guest.user_id, "x"))
        task_id, token = self.running(guest, job_id, "k")
        _call, budget = begin_call(self.database, guest.user_id, task_id, token, prompt_tokens=1_000, max_output=8_192)
        self.assertEqual(budget, 4_000)
        with self.assertRaises(Refused) as refused:
            begin_call(self.database, guest.user_id, task_id, token, prompt_tokens=10, max_output=10)
        self.assertEqual(refused.exception.code, "guest_ai_exhausted")
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT exempt FROM model_calls WHERE user_id = ?",
                                                (guest.user_id,)).fetchone()[0], 0)
        # The site's day binds a guest too.
        other, other_job = self.guest()
        configure_quota(self.database, "tokens", user_daily_tokens=1_000_000, site_daily_tokens=7_000)
        with self.assertRaises(Refused) as refused:
            other.submit_task("tailor_cv", key="k", request={"job_id": other_job}, units=1, heavy="model", job_id=other_job)
        self.assertEqual((refused.exception.code, refused.exception.detail["scope"]), ("quota_exhausted", "site"))

    def test_legacy_mode_counts_a_guest_in_the_sites_units_but_never_a_users_daily_limit(self):
        set_setting(self.database, "user_daily_units", "1")
        guest, job_id = self.guest()
        guest.submit_task("tailor_cv", key="a", request={"job_id": job_id}, units=1, heavy="model", job_id=job_id)
        claim_tasks(self.database, total=2, pdf=1, deadline=DEADLINE)
        guest.submit_task("plan_cv", key="b", request={"job_id": job_id}, units=1, heavy="model", job_id=job_id)
        with tenant_store.transaction(self.database) as connection:
            site = connection.execute("SELECT units FROM quota_usage WHERE scope = '*'").fetchone()[0]
        self.assertEqual(site, 2)
        set_setting(self.database, "site_daily_units", "2")
        another, another_job = self.guest()
        with self.assertRaises(Refused) as refused:
            another.submit_task("tailor_cv", key="a", request={"job_id": another_job}, units=1, heavy="model",
                                job_id=another_job)
        self.assertEqual((refused.exception.code, refused.exception.detail["scope"]), ("quota_exhausted", "site"))

    def test_signed_in_accounts_keep_their_own_quotas_and_exemptions(self):
        configure_quota(self.database, "tokens", user_daily_tokens=3_000, site_daily_tokens=10_000_000)
        set_setting(self.database, "guest_lifetime_tokens", "1000")
        guest, job_id = self.guest()
        job = self.alice.create_job({"text": "Requirements:\n- Python", "title": "t", "company": "c",
                                     "source": "synthetic", "captured_at": "2026-10-24T00:00:00+00:00"},
                                    request_id="r")["job_id"]
        task_id, token = self.running(self.alice, job, "a")
        _call, budget = begin_call(self.database, self.alice.user_id, task_id, token, prompt_tokens=500, max_output=8_192)
        self.assertEqual(budget, 2_500)  # the account's daily limit, not a guest's allowance
        self.assertEqual(self.alice.usage_today()["limit"], 3_000)
        self.assertIsNone(self.alice.guest())
        tenant_store.set_test_unlimited(self.database, self.alice.user_id, enabled=True)
        _call, budget = begin_call(self.database, self.alice.user_id, task_id, token, prompt_tokens=500, max_output=8_192)
        self.assertEqual(budget, 8_192)  # an exemption still works for an account
        with self.assertRaises(Refused):
            guest.submit_task("tailor_cv", key="g", request={"job_id": job_id}, units=1, heavy="model", job_id=job_id)


class AllGuestsDayTests(GuestMeterCase):
    def test_ending_a_workspace_gives_back_nothing_of_all_guests_day_or_the_sites(self):
        set_setting(self.database, "guest_daily_tokens", "10000")
        first, first_job = self.guest()
        self.spent(first, first_job, "k", 5_000, 3_000)
        self.assertTrue(tenant_store.end_guest(self.database, first.user_id))
        listed = tenant_store.list_guests(self.database)["ai_today"]
        self.assertEqual((listed["charged"], listed["limit"]), (8_000, 10_000))
        self.assertEqual(tenant_store.usage_on(self.database)["tokens"]["site"]["charged"], 8_000)
        fresh, fresh_job = self.guest()  # a fresh workspace has its own allowance, not all guests' day
        self.assertEqual(self.ai(fresh)["remaining"], 100_000)
        with self.assertRaises(Refused) as refused:
            fresh.submit_task("tailor_cv", key="k", request={"job_id": fresh_job}, units=1, heavy="model", job_id=fresh_job)
        self.assertEqual(refused.exception.code, "guest_ai_busy")
        set_setting(self.database, "guest_daily_tokens", "11000")
        task_id, token = self.running(fresh, fresh_job, "k")
        _call, budget = begin_call(self.database, fresh.user_id, task_id, token, prompt_tokens=500, max_output=8_192)
        self.assertEqual(budget, 3_000 - 500)
        with self.assertRaises(Refused) as refused:
            begin_call(self.database, fresh.user_id, task_id, token, prompt_tokens=10, max_output=10)
        self.assertEqual(refused.exception.code, "guest_ai_busy")
        finish_task(self.database, fresh.user_id, task_id, token, "succeeded")
        # The next UTC day all guests start again; each workspace's own allowance does not.
        self.clock.advance(days=1)
        self.assertEqual(tenant_store.list_guests(self.database)["ai_today"]["charged"], 0)
        task_id, token = self.running(fresh, fresh_job, "next")
        self.assertEqual(self.ai(fresh)["used"], 3_000)

    def test_a_backup_keeps_the_day_totals_without_any_guest(self):
        guest, job_id = self.guest()
        self.spent(guest, job_id, "k", 5_000, 3_000)
        archive = self.data.parent / "backup.tar.gz"
        v2_backup.create_backup(self.data, archive)
        self.assertEqual(self.ai(guest)["used"], 8_000)  # the live workspace is untouched
        restored = self.data.parent / "restored"
        result = v2_backup.restore_backup(archive, restored, live_ledger=None)
        self.assertEqual(result["problems"], [])
        copy = restored / tenant_store.DATABASE
        self.assertEqual(rows_naming(copy, guest.user_id), [])
        self.assertEqual(rows_naming(copy, job_id), [])
        self.assertEqual(tenant_store.list_guests(copy)["ai_today"]["charged"], 8_000)
        self.assertEqual(tenant_store.usage_on(copy)["tokens"]["site"]["charged"], 8_000)


class NoEndTests(GuestMeterCase):
    def test_workspaces_made_with_a_fixed_end_are_kept_whole_and_given_none(self):
        current, current_job = self.guest()
        ended, ended_job = self.guest()
        token, _session = tenant_store.create_guest_session(self.database, current.user_id)
        normal, _ = tenant_store.create_session(self.database, self.alice.user_id, lifetime=timedelta(hours=1),
                                                idle=timedelta(hours=1))
        old_messages = list(tenant_store.GUEST_OLD_STAGES)[:2]
        with tenant_store.transaction(self.database, write=True) as connection:  # as the release before left them
            for user, ends in ((current, self.clock.now + timedelta(hours=2)), (ended, self.clock.now - timedelta(minutes=1))):
                connection.execute("UPDATE guest_workspaces SET expires_at = ? WHERE user_id = ?", (ends.isoformat(), user.user_id))
                connection.execute("UPDATE guest_sessions SET expires_at = ? WHERE user_id = ?", (ends.isoformat(), user.user_id))
                row = connection.execute("SELECT payload FROM job_notes WHERE user_id = ? AND name = 'cv-status-en'",
                                         (user.user_id,)).fetchone()
                payload = json.loads(row[0])
                payload["stages"][1]["message"], payload["stages"][2]["message"] = old_messages
                connection.execute("UPDATE job_notes SET payload = ? WHERE user_id = ? AND name = 'cv-status-en'",
                                   (json.dumps(payload), user.user_id))
        before = {user.user_id: {table: len(rows_naming(self.database, user.user_id)) for table in ("x",)}
                  for user in (current, ended)}
        counts = self.counts()
        initialize_store(self.database)  # the next start
        self.assertEqual(self.counts(), counts)
        with tenant_store.transaction(self.database) as connection:
            ends = {row[0] for row in connection.execute("SELECT expires_at FROM guest_workspaces UNION "
                                                         "SELECT expires_at FROM guest_sessions")}
        self.assertEqual(ends, {GUEST_NO_END})
        self.assertEqual({user.user_id: {"x": len(rows_naming(self.database, user.user_id))} for user in (current, ended)},
                         before)
        self.assertEqual(tenant_store.expire_guests(self.database), 0)
        self.clock.advance(days=3 * 365)
        session = tenant_store.resolve_session(self.database, token)
        self.assertTrue(session.guest)
        self.assertEqual(current.list_jobs()[0]["job_id"], current_job)
        self.assertEqual(ended.list_jobs()[0]["job_id"], ended_job)
        stages = current.note(current_job, "cv-status-en")["stages"]
        self.assertEqual([stage["message"] for stage in stages[1:]],
                         [guest_sample.NO_AI_REWORDING, guest_sample.NO_AI_LAYOUT])
        self.assertIsNone(tenant_store.resolve_session(self.database, normal))  # an account's session still ends
        self.assertEqual(tenant_store.upgrade_store(self.database), [])  # once is enough

    def counts(self):
        with tenant_store.transaction(self.database) as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("users", "facts", "profiles", "profile_versions", "jobs", "materials", "job_requirements",
                                  "guest_sessions", "sessions")}

    def test_the_stored_workspaces_are_capped_and_none_is_removed_to_make_room(self):
        set_setting(self.database, "guest_max_active", "2")
        first, _ = self.guest()
        self.guest()
        self.clock.advance(days=365)
        with self.assertRaises(Refused) as refused:
            tenant_store.start_guest(self.database, start="one-too-many-" + "x" * 30)
        self.assertEqual(refused.exception.code, "guest_full")
        self.assertEqual(len(guest_ids(self.database)), 2)
        tenant_store.end_guest(self.database, first.user_id)  # ending one makes room
        tenant_store.start_guest(self.database, start="room-again-" + "x" * 30)


class GuestAIWebTests(GuestCase):
    def test_ai_needs_the_guests_agreement_then_runs_and_counts_on_the_sample_job_only(self):
        client, headers, job_id = self.guest()
        refused = client.post(f"/api/jobs/{job_id}/cv/en/tailor", headers=headers)
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "consent_required"))
        self.assertEqual(self.chat.sent, [])
        notices = client.get("/api/me", headers=headers).json()["notices"]
        self.assertIn("job_processing", notices)
        agreed = client.post("/api/consent", headers=headers, json={"stage": "job_processing",
                                                                     "version": notices["job_processing"]["version"]})
        self.assertEqual(agreed.status_code, 200, agreed.text)
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/tailor", headers=headers))
        self.assertEqual(task["status"], "succeeded", task)
        self.assertTrue(self.chat.sent)
        ai = client.get("/api/me", headers=headers).json()["guest"]["ai"]
        with tenant_store.transaction(self.database) as connection:
            charged = connection.execute(f"SELECT SUM({tenant_store._CHARGE}) FROM model_calls").fetchone()[0]
        self.assertEqual((ai["used"], ai["remaining"]), (charged, 100_000 - charged))
        self.assertGreater(charged, 0)
        cv = self.cv(client, headers, job_id)
        requirements = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        for path, body in [(f"/api/jobs/{job_id}/cv/en/plan", None), (f"/api/jobs/{job_id}/cv/en/prepare", None),
                           (f"/api/jobs/{job_id}/gaps", {"language": "en"}),
                           (f"/api/jobs/{job_id}/requirements/find", {"expected_version": requirements["requirements_version"]
                                                                      if "requirements_version" in requirements else 1})]:
            response = client.post(path, headers=headers, json=body)
            self.assertNotEqual(response.json().get("code") if response.status_code >= 400 else None, "guest_not_allowed", path)
            if response.status_code == 202:
                self.settle(client, headers, response)
        self.assertNotEqual(cv["head"], "draft")
        # Still refused: uploads, new jobs, profile changes.
        for path in ("/api/jobs", "/api/cv/upload?language=en", f"/api/jobs/{job_id}/cv/en/lines/b1/save"):
            self.assertEqual(client.post(path, headers=headers, json={}).json()["code"], "guest_not_allowed")

    def test_with_the_allowance_used_up_editing_fitting_and_printing_still_work(self):
        set_setting(self.database, "guest_max_tasks", "3")  # a fit, a page check and a PDF
        client, headers, job_id = self.guest()
        client.post("/api/consent", headers=headers, json={"stage": "job_processing", "version": 1})
        self.assertEqual(self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/tailor", headers=headers))
                         ["status"], "succeeded")
        used = client.get("/api/me", headers=headers).json()["guest"]["ai"]["used"]
        set_setting(self.database, "guest_lifetime_tokens", str(max(1_000, used)))
        for path, body in [(f"/api/jobs/{job_id}/cv/en/plan", None), (f"/api/jobs/{job_id}/gaps", {"language": "en"})]:
            refused = client.post(path, headers=headers, json=body)
            self.assertEqual((refused.status_code, refused.json()["code"]), (403, "guest_ai_exhausted"), path)
            self.assertIn("does not refill", refused.json()["error"])
        line = self.cv(client, headers, job_id)["lines"][0]
        edited = client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                             json={"text": "Edited after the AI allowance was used.", "expected_text": line["text"]})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers))
                         ["status"], "succeeded")
        self.assertEqual(self.printed(client, headers, job_id)["status"], "succeeded")
        self.assertEqual(client.get(f"/download/{job_id}/en.pdf", headers=headers).status_code, 200)
        guest = client.get("/api/me", headers=headers).json()["guest"]
        # What is left is less than any AI task needs to start; the page shows it as it is.
        self.assertEqual((guest["prints_used"], guest["print_limit"], guest["ai"]["remaining"]),
                         (3, 3, max(1_000, used) - used))
        self.assertLess(guest["ai"]["remaining"], tenant_store.MIN_START_TOKENS)
        self.assertEqual(guest_ids(self.database), [guest_ids(self.database)[0]])  # still there: nothing removed it

    def test_a_sample_from_before_guests_had_ai_asks_to_start_over_and_sends_nothing(self):
        client, headers, job_id = self.guest()
        with tenant_store.transaction(self.database, write=True) as connection:  # as seeded by the release before
            connection.execute("UPDATE materials SET requirements_version = NULL WHERE job_id = ?", (job_id,))
        client.post("/api/consent", headers=headers, json={"stage": "job_processing", "version": 1})
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/tailor", headers=headers))
        self.assertEqual((task["status"], task["error_code"]), ("failed", "not_prepared"), task)
        self.assertIn("Start over", task["message"])
        self.assertEqual(self.chat.sent, [])
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=headers))
        self.assertEqual(task["status"], "succeeded", task)  # starting over prepares it for the requirements

    def test_the_session_cookie_lasts_and_is_renewed_and_an_accounts_is_unchanged(self):
        client = TestClient(self.app, base_url=ORIGIN)
        start = self.welcome(client)
        started = client.post("/guest/start", headers={"X-Guest-Start": start, "Origin": ORIGIN})
        cookie = started.headers["set-cookie"]
        self.assertIn("Max-Age=34560000", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertNotIn("expires_at", started.json())
        token = client.cookies.get("__Host-feiran")
        for path in ("/", "/welcome"):  # the app and the welcome page both renew it
            renewed = client.get(path)
            self.assertEqual(renewed.status_code, 200)
            self.assertIn(f"__Host-feiran={token}", renewed.headers["set-cookie"])
            self.assertIn("Max-Age=34560000", renewed.headers["set-cookie"])
        self.assertIn('name="workbench-guest" content="guest"', client.get("/").text)
        self.clock.advance(days=2 * 365)
        self.assertEqual(client.get("/api/me").status_code, 200)  # the server never ended it
        alice, _headers = self.browser("alice")
        page = alice.get("/")
        self.assertNotIn("set-cookie", page.headers)
        self.assertNotIn("workbench-guest", page.text)

    def test_the_welcome_page_says_what_a_guest_workspace_is_now(self):
        page = TestClient(self.app, base_url=ORIGIN).get("/welcome").text
        self.assertIn('name="guest-tokens" content="100000"', page)
        self.assertNotIn("guest-ends", page)
        self.assertNotIn("No AI requests", page)
        self.assertIn("There is no time limit", page)


@unittest.skipUnless(test_sponsor_ui.NODE, "Node is needed for frontend behavior checks")
class GuestPageStateTests(unittest.TestCase):
    def check_guest_js(self, script):
        harness = test_sponsor_ui.HARNESS.replace(
            'querySelector(selector) { return { content: selector.includes("mode") ? MODE : "synthetic-token" }; }',
            'querySelector(selector) { return { content: selector.includes("mode") ? MODE : selector.includes("guest") ? "guest" : "synthetic-token" }; }')
        self.assertNotEqual(harness, test_sponsor_ui.HARNESS)
        result = subprocess.run([test_sponsor_ui.NODE, "-e", 'const MODE = "v2";\n' + harness + "\n(async () => {\n" + script
                                 + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"],
                                cwd=test_sponsor_ui.REPO, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_guest_page_shows_its_allowance_and_ai_tools_in_both_languages_and_no_countdown(self):
        self.check_guest_js(r'''
assert.equal(run("GUEST"), true);
const left = String(run('guestAllowanceText({limit:100000,used:12345,remaining:87655})'));
assert(left.includes("87,655 of 100,000 tokens left"), left);
assert(left.includes("does not refill"));
const out = String(run('guestAllowanceText({limit:100000,used:104192,remaining:0})'));
assert(out.includes("used up") && out.includes("104,192") && out.includes("downloading still work"), out);
const block = run('cvBlock({job_id:"j",cv:{en:{head:"draft",content_sha256:"c",lines:[],edits:[],stages:[]}}},"en",async()=>{})');
for (const label of ["Reword with DeepSeek","Adjust for this job","Fit to one page","Start over"]) assert(button(block,label), label);
assert(block.textContent.includes("prepared without AI"));
run('document.documentElement={}; document.getElementById=(id)=>id==="app"?__app:id==="interface-language"?{}:null; NodeFilter={SHOW_ELEMENT:1,SHOW_TEXT:4}; document.createTreeWalker=()=>({nextNode:()=>null}); I18n.setLanguage("zh")');
assert(String(run('guestAllowanceText({limit:100000,used:1,remaining:99999})')).includes("AI 额度：剩余 99,999 / 100,000 token"));
assert(String(run('guestAllowanceText({limit:100000,used:100000,remaining:0})')).includes("AI 额度已用完"));
''')
        source = (Path(test_sponsor_ui.REPO) / "web" / "app.js").read_text(encoding="utf-8")
        for gone in ("GUEST_ENDS", "Ends at", "guestTimeText", "makes no AI requests", "No AI requests"):
            self.assertNotIn(gone, source)
        welcome = (Path(test_sponsor_ui.REPO) / "web" / "welcome.js").read_text(encoding="utf-8")
        self.assertNotIn("minutes", welcome)

    def test_a_signed_in_page_is_never_a_guests(self):
        test_sponsor_ui.SponsorUITests.check_js(self, 'assert.equal(run("GUEST"), false);')


if __name__ == "__main__":
    unittest.main()
