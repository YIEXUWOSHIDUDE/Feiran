"""V2 store behaviour beyond the first increment: accounts, sessions, uploads, CV versions,
approvals, final PDFs, gaps, boards, tasks and quotas. Real SQLite, synthetic data, no network."""
import copy
from contextlib import contextmanager
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import tenant_store
from cv import build_draft_from_facts, content_fingerprint, tailor_draft
from cv_plan import plan_draft
from tenant_store import (Conflict, FactSnapshot, LateResult, NotFound, Refused, StoreError, UserWorkspace,
                          apply_deletions, claim_tasks, create_session, delete_account, expire_tasks, finish_task,
                          initialize_store, mark_spending, provision_user, read_deletions, record_usage,
                          recover_tasks, resolve_session, revoke_session, save_login_attempt, set_setting,
                          set_user_active, sign_in, store_public_board, take_login_attempt)
from tests.test_cv import FACTS, PROFILE, FakeChat
from tests.test_cv_plan import CUT_API, FakePlanner
from gaps import check_state, find_gaps, places
from task_runner import TaskContext
from tests.test_gaps import FakeDeepSeek, decided_job, suggestions
from v2_flow import make_handlers

ISSUER = "https://cognito-idp.example.amazonaws.com/pool"
PDF = b"%PDF-1.7\nsynthetic\n%%EOF"


@contextmanager
def raw(path):
    """A plain SQLite connection for poking at the file directly, committed and then closed."""
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class Clock:
    """A controllable clock for the store (tenant_store._clock)."""

    def __init__(self):
        self.now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def open_site(path):
    initialize_store(path)
    set_setting(path, "user_daily_units", "20")
    set_setting(path, "site_daily_units", "100")


def run_task(database, user_id, operation, key="k", request=None, units=1, heavy="model", job_id=None):
    """Submit and claim one task; returns (task_id, token)."""
    task = UserWorkspace(database, user_id).submit_task(operation, key=key, request=request or {"x": key},
                                                        units=units, heavy=heavy, job_id=job_id)
    claimed = [item for item in claim_tasks(database, total=2, pdf=1, deadline=timedelta(minutes=5))
               if item["task_id"] == task["task_id"]]
    assert claimed, "task was not claimed"
    return task["task_id"], claimed[0]["token"]


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "v2.db"
        open_site(self.database)
        self.clock = Clock()
        patcher = patch.object(tenant_store, "_clock", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a_id = provision_user(self.database, issuer=ISSUER, subject="A")
        self.b_id = provision_user(self.database, issuer=ISSUER, subject="B")
        self.a, self.b = UserWorkspace(self.database, self.a_id), UserWorkspace(self.database, self.b_id)

    def ready(self, user, language="en"):
        """Facts confirmed, a profile, a job with decided requirements; returns job_id."""
        user.import_facts(FACTS)
        user.confirm_facts([(item["id"], 1) for item in FACTS])
        user.save_profile(language, copy.deepcopy(PROFILE), expected_version=0)
        decided = decided_job()
        job = user.create_job(decided["jd"], request_id="job")
        user.save_requirements(job["job_id"], decided, decided, expected_version=0)
        return job["job_id"]

    def prepare(self, user, job_id, language="en", plan=True, key="prepare"):
        """A prepare task publishing a draft (and a planned CV) the way the runner does."""
        inputs = user.cv_inputs(job_id, language)
        decided = inputs["requirements"]["decided"]
        draft = build_draft_from_facts(inputs["profile"]["profile"], inputs["facts"].current(
            [fact["id"] for fact in inputs["facts"].listed()]), language, job=decided)
        documents = [("draft", draft)]
        if plan:
            documents.append(("planned", plan_draft(draft, decided, chat=FakePlanner(CUT_API))))
        task_id, token = run_task(self.database, user.user_id, "prepare_cv", key=key, job_id=job_id)
        return user.publish_cv(task_id=task_id, token=token, job_id=job_id, language=language,
                               base_head=inputs["head"]["material_id"] if inputs["head"] else None,
                               documents=documents, profile_version=inputs["profile"]["version"],
                               requirements_version=inputs["requirements"]["version"], stages=[])


class AccountTests(StoreCase):
    def test_registration_needs_the_switch_finite_limits_and_room(self):
        with self.assertRaises(Refused) as refused:
            sign_in(self.database, issuer=ISSUER, subject="new")
        self.assertEqual(refused.exception.code, "registration_closed")
        fresh = Path(self.temp.name) / "fresh.db"
        initialize_store(fresh)
        with self.assertRaises(StoreError):
            set_setting(fresh, "registration_open", "1")  # no daily limits yet
        set_setting(self.database, "max_accounts", "3")
        set_setting(self.database, "registration_open", "1")
        created = sign_in(self.database, issuer=ISSUER, subject="new", starter=[
            {"provider": "greenhouse", "board": "example", "company": "Example"}])
        self.assertEqual(sign_in(self.database, issuer=ISSUER, subject="new"), created)
        self.assertEqual([b["board"] for b in UserWorkspace(self.database, created).followed_boards()], ["example"])
        with self.assertRaises(Refused) as full:
            sign_in(self.database, issuer=ISSUER, subject="fourth")
        self.assertEqual(full.exception.code, "registration_full")
        for bad in [("max_accounts", "0"), ("registration_open", "yes"), ("unknown", "1")]:
            with self.assertRaises(StoreError):
                set_setting(self.database, *bad)

    def test_disabled_account_loses_sessions_and_work_and_is_not_reopened_by_sign_in(self):
        token, _ = create_session(self.database, self.a_id, lifetime=timedelta(hours=8), idle=timedelta(hours=1))
        job = self.ready(self.a)
        task = self.a.submit_task("prepare_cv", key="q", request={}, units=2, heavy="model", job_id=job)
        set_user_active(self.database, self.a_id, active=False)
        self.assertIsNone(resolve_session(self.database, token))
        with self.assertRaises(NotFound):
            self.a.get_task(task["task_id"])
        set_setting(self.database, "registration_open", "1")
        with self.assertRaises(Refused) as refused:
            sign_in(self.database, issuer=ISSUER, subject="A")
        self.assertEqual(refused.exception.code, "account_disabled")
        set_user_active(self.database, self.a_id, active=True)
        self.assertEqual(self.a.get_task(task["task_id"])["status"], "cancelled")
        self.assertEqual(self.a.usage_today()["used"], 0)  # never started: its units came back

    def test_deleted_account_keeps_only_its_tombstone_and_ledger_reapplies_after_restore(self):
        job = self.ready(self.a)
        self.prepare(self.a, job)
        self.ready(self.b)
        ledger = Path(self.temp.name) / tenant_store.DELETION_LEDGER
        backup = Path(self.temp.name) / "before.db"
        with raw(self.database) as source, raw(backup) as target:
            source.backup(target)
        record = delete_account(self.database, self.a_id, ledger)
        self.assertEqual(read_deletions(ledger), [record])
        with raw(self.database) as connection:
            for table in tenant_store.USER_TABLES:
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?",
                                                    (self.a_id,)).fetchone()[0], 0, table)
        with self.assertRaises(NotFound):
            self.a.list_facts()
        set_setting(self.database, "registration_open", "1")
        with self.assertRaises(Refused) as refused:
            sign_in(self.database, issuer=ISSUER, subject="A")
        self.assertEqual(refused.exception.code, "account_deleted")
        self.assertEqual(len(self.b.list_facts()), len(FACTS))
        # An older backup still holds A; restoring it must not reopen the account.
        self.assertEqual(len(UserWorkspace(backup, self.a_id).list_facts()), len(FACTS))
        self.assertEqual(apply_deletions(backup, read_deletions(ledger)), 1)
        with self.assertRaises(NotFound):
            UserWorkspace(backup, self.a_id).list_facts()
        self.assertEqual(len(UserWorkspace(backup, self.b_id).list_facts()), len(FACTS))
        self.assertEqual(apply_deletions(backup, read_deletions(ledger)), 0)

    def test_request_keys_go_with_the_account(self):
        self.a.bind_request("paste_job", "client:k", "0" * 64)
        self.b.bind_request("paste_job", "client:k", "1" * 64)  # keys are per user
        with self.assertRaises(Conflict):
            self.a.bind_request("paste_job", "client:k", "1" * 64)
        delete_account(self.database, self.a_id)
        with raw(self.database) as connection:
            owners = [row[0] for row in connection.execute("SELECT user_id FROM request_keys")]
        self.assertEqual(owners, [self.b_id])

    def test_consent_is_per_user_and_per_notice_version(self):
        self.assertEqual(self.a.consents(), {})
        self.a.give_consent("upload_parsing", 1)
        self.assertIn("upload_parsing", self.a.consents())
        self.assertEqual(self.b.consents(), {})
        with self.assertRaises(Conflict):
            self.a.give_consent("job_processing", 99)
        with patch.dict(tenant_store.CONSENT_STAGES, {"upload_parsing": 2}):
            self.assertEqual(self.a.consents(), {})


class SessionTests(StoreCase):
    def test_sessions_expire_by_idle_and_absolute_limits_and_store_only_a_hash(self):
        token, session = create_session(self.database, self.a_id, lifetime=timedelta(hours=2), idle=timedelta(minutes=30))
        with raw(self.database) as connection:
            stored = connection.execute("SELECT token_hash FROM sessions").fetchall()
        self.assertNotIn(token, json.dumps(stored))
        self.assertEqual(resolve_session(self.database, token).user_id, self.a_id)
        for _ in range(5):  # activity keeps it alive past the first idle limit
            self.clock.advance(minutes=20)
            self.assertIsNotNone(resolve_session(self.database, token))
        self.clock.advance(minutes=20)  # 120 minutes: absolute limit
        self.assertIsNone(resolve_session(self.database, token))
        token, _ = create_session(self.database, self.a_id, lifetime=timedelta(hours=2), idle=timedelta(minutes=30))
        self.clock.advance(minutes=31)
        self.assertIsNone(resolve_session(self.database, token))
        token, _ = create_session(self.database, self.a_id, lifetime=timedelta(hours=2), idle=timedelta(minutes=30))
        revoke_session(self.database, token)
        self.assertIsNone(resolve_session(self.database, token))
        for bad in [None, "", "x" * 5, "y" * 500, token + "z"]:
            self.assertIsNone(resolve_session(self.database, bad))

    def test_login_attempt_is_single_use_bound_to_its_browser_and_short_lived(self):
        save_login_attempt(self.database, state="s1", binding="b1", nonce="n", code_verifier="v", return_to="/")
        self.assertIsNone(take_login_attempt(self.database, state="s1", binding="other"))
        self.assertIsNone(take_login_attempt(self.database, state="s1", binding=""))
        self.assertEqual(take_login_attempt(self.database, state="s1", binding="b1")["nonce"], "n")  # still its own
        self.assertIsNone(take_login_attempt(self.database, state="s1", binding="b1"))  # used up
        save_login_attempt(self.database, state="s2", binding="b2", nonce="n", code_verifier="v", return_to="/#jobs")
        self.assertEqual(take_login_attempt(self.database, state="s2", binding="b2")["return_to"], "/#jobs")
        self.assertIsNone(take_login_attempt(self.database, state="s2", binding="b2"))
        save_login_attempt(self.database, state="s3", binding="b3", nonce="n", code_verifier="v", return_to="/")
        self.clock.advance(minutes=11)
        self.assertIsNone(take_login_attempt(self.database, state="s3", binding="b3"))
        with patch.object(tenant_store, "MAX_PENDING_LOGINS", 2):
            save_login_attempt(self.database, state="s4", binding="b", nonce="n", code_verifier="v", return_to="/")
            save_login_attempt(self.database, state="s5", binding="b", nonce="n", code_verifier="v", return_to="/")
            with self.assertRaises(Refused):
                save_login_attempt(self.database, state="s6", binding="b", nonce="n", code_verifier="v", return_to="/")


class UploadTests(StoreCase):
    PROPOSAL = {"private": {"name": "Alex Example"}, "not_imported": [], "sections": [
        {"kind": "experience", "title": None, "entries": [
            {"title": "Example Corp", "subtitle": "Software Intern", "dates": "2025",
             "facts": [{"text": "Built REST APIs for an internal tool.", "tags": ["REST APIs"]}]}]},
        {"kind": "skills", "entries": [{"facts": [{"text": "Languages: Python, Java", "tags": ["Python"]}]}]}]}

    def publish(self, user, key="u"):
        task_id, token = run_task(self.database, user.user_id, "upload_cv", key=key)
        return user.publish_upload(task_id=task_id, token=token, proposal=self.PROPOSAL, language="en",
                                   source_sha256="a" * 64)["upload_id"]

    def test_saving_an_upload_imports_pending_facts_and_the_profile_together(self):
        upload = self.publish(self.a)
        with self.assertRaises(NotFound):
            self.b.get_upload(upload)
        with self.assertRaises(NotFound):
            self.b.commit_upload(upload, {"name": "Blair"})
        saved = self.a.commit_upload(upload, {"name": "Alex Example", "email": "alex@example.com"})
        self.assertEqual(saved, {"imported": 2, "reused": 0})
        self.assertTrue(all(fact["status"] == "pending" for fact in self.a.list_facts()))
        self.assertEqual(self.a.get_profile("en")["profile"]["name"], {"en": "Alex Example"})
        self.assertEqual(self.a.cv_languages(), ["en"])
        with self.assertRaises(NotFound):
            self.a.get_upload(upload)  # used up
        again = self.publish(self.a, key="u2")
        self.assertEqual(self.a.commit_upload(again, {"name": "Alex Example", "email": "alex@example.com"}),
                         {"imported": 0, "reused": 2})
        self.assertEqual(self.a.get_profile("en")["version"], 1)  # the same CV saved again changes nothing
        self.assertEqual(self.b.list_facts(), [])

    def test_a_refused_save_changes_nothing_and_uploads_expire(self):
        upload = self.publish(self.a)
        with self.assertRaises(StoreError):
            self.a.commit_upload(upload, {"name": "  "})
        self.assertEqual(self.a.list_facts(), [])
        self.assertEqual(self.a.get_upload(upload)["upload_id"], upload)
        self.clock.advance(hours=25)
        with self.assertRaises(NotFound):
            self.a.get_upload(upload)


class MaterialTests(StoreCase):
    def test_prepare_publishes_a_chain_and_approval_binds_reviewer_content_and_inputs(self):
        job = self.ready(self.a)
        published = self.prepare(self.a, job)
        self.assertEqual(len(published["materials"]), 2)
        snapshot = self.a.job_snapshot(job)
        head = snapshot["cv"]["en"]["material"]
        self.assertEqual((head["stage"], head["version"], head["inputs_current"]), ("planned", 2, True))
        self.assertEqual(snapshot["cv"]["en"]["draft_id"], published["materials"][0])
        with self.assertRaises(Conflict):
            self.a.approve(job, "en", "0" * 64)  # an old page's fingerprint
        approval = self.a.approve(job, "en", head["content_sha256"])
        self.assertEqual((approval["reviewer_id"], approval["content_sha256"]), (self.a_id, head["content_sha256"]))
        self.assertEqual(self.a.approve(job, "en", head["content_sha256"])["approval_id"], approval["approval_id"])
        with self.assertRaises(NotFound):
            self.b.approve(job, "en", head["content_sha256"])
        document, final = self.a.preview(job, "en")
        self.assertTrue(final)
        self.assertEqual(content_fingerprint(document), head["content_sha256"])

    def test_pending_material_expansion_can_publish_approve_and_export(self):
        self.a.import_facts(FACTS)
        self.a.save_profile("en", copy.deepcopy(PROFILE), expected_version=0)
        decided = decided_job()
        job = self.a.create_job(decided["jd"], request_id="pending-job")["job_id"]
        self.a.save_requirements(job, decided, decided, expected_version=0)
        inputs = self.a.cv_inputs(job, "en")
        draft = build_draft_from_facts(PROFILE, inputs["facts"].current([f["id"] for f in FACTS]), "en")
        expanded = "Led 4 engineers for 3 years, adding AWS and reducing latency by 40%."
        tailored = tailor_draft(draft, inputs["facts"], chat=FakeChat({"fact-intern-api": expanded}))
        task, token = run_task(self.database, self.a_id, "prepare_cv", key="pending", job_id=job)
        result = self.a.publish_cv(task_id=task, token=token, job_id=job, language="en", base_head=None,
                                  documents=[("draft", draft), ("tailored", tailored)], profile_version=1,
                                  requirements_version=1, stages=[])
        self.assertNotIn("superseded", result)
        snapshot = self.a.job_snapshot(job)
        self.assertEqual(len(snapshot["confirmed"]), len(FACTS))  # legacy field contains all current versions
        head = snapshot["cv"]["en"]["material"]
        self.assertTrue(head["inputs_current"])
        self.a.approve(job, "en", head["content_sha256"])
        exported = self.a.export_inputs(job, "en")
        task, token = run_task(self.database, self.a_id, "export_pdf", heavy="pdf", job_id=job)
        self.a.publish_pdf(task_id=task, token=token, approval_id=exported["approval_id"], data=PDF, pages=1)
        self.assertEqual(self.a.final_pdf(job, "en"), PDF)
        self.assertTrue(all(f["status"] == "pending" for f in self.a.list_facts()))
        with self.assertRaises(NotFound):
            self.b.final_pdf(job, "en")

    def test_changed_inputs_stop_approval_and_final_pdf_and_old_page_cannot_approve(self):
        job = self.ready(self.a)
        self.prepare(self.a, job)
        head = self.a.job_snapshot(job)["cv"]["en"]["material"]
        self.a.approve(job, "en", head["content_sha256"])
        exported = self.a.export_inputs(job, "en")
        task_id, token = run_task(self.database, self.a_id, "export_pdf", heavy="pdf", job_id=job)
        self.a.revise_fact("fact-intern-api", expected_version=1, text="Built REST APIs for two tools.", tags=["REST APIs"])
        # The PDF finished printing after the fact changed: it is not offered as final.
        self.assertEqual(self.a.publish_pdf(task_id=task_id, token=token, approval_id=exported["approval_id"],
                                           data=PDF, pages=1), {"superseded": True})
        self.assertEqual(self.a.get_task(task_id)["status"], "superseded")
        with self.assertRaises(NotFound):
            self.a.final_pdf(job, "en")
        with self.assertRaises(Conflict):
            self.a.export_inputs(job, "en")
        snapshot = self.a.job_snapshot(job)
        self.assertFalse(snapshot["cv"]["en"]["material"]["inputs_current"])
        self.assertTrue(snapshot["cv"]["en"]["approved_but_outdated"])
        with self.assertRaises(Conflict):
            self.a.approve(job, "en", head["content_sha256"])
        document, final = self.a.preview(job, "en")
        self.assertFalse(final)

    def test_final_pdf_belongs_to_the_valid_approval_only(self):
        job = self.ready(self.a)
        self.prepare(self.a, job)
        head = self.a.job_snapshot(job)["cv"]["en"]["material"]
        self.a.approve(job, "en", head["content_sha256"])
        exported = self.a.export_inputs(job, "en")
        task_id, token = run_task(self.database, self.a_id, "export_pdf", heavy="pdf", job_id=job)
        self.a.publish_pdf(task_id=task_id, token=token, approval_id=exported["approval_id"], data=PDF, pages=1)
        self.assertEqual(self.a.final_pdf(job, "en"), PDF)
        with self.assertRaises(NotFound):
            self.b.final_pdf(job, "en")
        # Undoing a change makes a new CV version: the approval and its PDF stay with the old one.
        change = self.a.job_snapshot(job)["cv"]["en"]["material"]["draft"]["plan"]["changes"][0]["id"]
        changed = self.a.change_cv(job, "en", change, True)
        self.assertEqual(changed["version"], 3)
        with self.assertRaises(NotFound):
            self.a.final_pdf(job, "en")
        self.assertIsNone(self.a.job_snapshot(job)["cv"]["en"]["approval"])
        with raw(self.database) as connection:
            connection.execute("UPDATE artifacts SET data = ?", (b"%PDF-tampered",))
        self.a.change_cv(job, "en", change, False)  # back to the approved content, but a newer version
        with self.assertRaises(NotFound):
            self.a.final_pdf(job, "en")

    def test_late_and_superseded_results_are_not_published(self):
        job = self.ready(self.a)
        self.prepare(self.a, job, key="first")
        inputs = self.a.cv_inputs(job, "en")
        draft = build_draft_from_facts(inputs["profile"]["profile"], inputs["facts"].current(
            [f["id"] for f in FACTS]), "en", job=inputs["requirements"]["decided"])
        slow_id, slow_token = run_task(self.database, self.a_id, "prepare_cv", key="slow", job_id=job)
        change = inputs["head"]["draft"]["plan"]["changes"][0]["id"]
        self.a.change_cv(job, "en", change, True)  # the user undid a change while it ran
        outcome = self.a.publish_cv(task_id=slow_id, token=slow_token, job_id=job, language="en",
                                    base_head=inputs["head"]["material_id"], documents=[("draft", draft)],
                                    profile_version=1, requirements_version=1, stages=[])
        self.assertEqual(outcome, {"superseded": True})
        self.assertEqual(self.a.get_task(slow_id)["status"], "superseded")
        self.assertEqual(len(self.a.list_materials(job)), 3)
        head = self.a.job_snapshot(job)["cv"]["en"]["material"]["material_id"]
        cancelled_id, cancelled_token = run_task(self.database, self.a_id, "prepare_cv", key="cancel", job_id=job)
        self.a.cancel_task(cancelled_id)
        with self.assertRaises(LateResult):
            self.a.publish_cv(task_id=cancelled_id, token=cancelled_token, job_id=job, language="en",
                              base_head=head, documents=[("draft", draft)], profile_version=1,
                              requirements_version=1, stages=[])
        with self.assertRaises(LateResult):
            self.b.publish_cv(task_id=cancelled_id, token=cancelled_token, job_id=job, language="en", base_head=None,
                              documents=[("draft", draft)], profile_version=1, requirements_version=1, stages=[])
        self.assertEqual(len(self.a.list_materials(job)), 3)

    def test_a_cv_made_from_inputs_that_changed_meanwhile_is_not_published(self):
        job = self.ready(self.a)

        def changed_facts():
            self.a.revise_fact(FACTS[0]["id"], expected_version=1, text=FACTS[0]["text"] + " Changed.",
                               tags=FACTS[0]["tags"])

        def changed_profile():
            current = self.a.get_profile("en")
            self.a.save_profile("en", {**current["profile"], "name": {"en": "Someone Else"}},
                                expected_version=current["version"])

        def changed_requirements():
            current = self.a.requirements(job)
            self.a.save_requirements(job, current["candidates"], current["decided"], expected_version=current["version"])

        for key, change in (("facts", changed_facts), ("profile", changed_profile), ("requirements", changed_requirements)):
            with self.subTest(changed=key):
                inputs = self.a.cv_inputs(job, "en")
                draft = build_draft_from_facts(inputs["profile"]["profile"], inputs["facts"].current(
                    [fact["id"] for fact in inputs["facts"].listed() if fact["status"] == "confirmed"]),
                    "en", job=inputs["requirements"]["decided"])
                task_id, token = run_task(self.database, self.a_id, "prepare_cv", key=key, job_id=job)
                change()  # while the task worked
                materials = len(self.a.list_materials(job))
                outcome = self.a.publish_cv(task_id=task_id, token=token, job_id=job, language="en",
                                            base_head=inputs["head"]["material_id"] if inputs["head"] else None,
                                            documents=[("draft", draft)], profile_version=inputs["profile"]["version"],
                                            requirements_version=inputs["requirements"]["version"], stages=[])
                self.assertEqual(outcome, {"superseded": True})
                task = self.a.get_task(task_id)
                self.assertEqual((task["status"], task["error_code"]), ("superseded", "inputs_changed"))
                self.assertEqual(len(self.a.list_materials(job)), materials)
                if key == "facts":
                    self.a.confirm_facts([(FACTS[0]["id"], 2)])
        published = self.prepare(self.a, job, key="now")  # on unchanged inputs it is published
        self.assertEqual(len(published["materials"]), 2)

    def test_a_gap_check_of_a_cv_that_changed_meanwhile_is_not_kept(self):
        from gaps import find_gaps
        job = self.ready(self.a)
        self.prepare(self.a, job)
        for key in ("cv", "facts", "requirements"):
            with self.subTest(changed=key):
                inputs = self.a.gaps_inputs(job, "en")
                gaps = find_gaps(inputs["requirements"]["decided"], inputs["head"]["draft"], inputs["facts"],
                                 FakeDeepSeek(suggestions), private=inputs["private"])
                task_id, token = run_task(self.database, self.a_id, "check_gaps", key=f"gaps-{key}", job_id=job)
                if key == "cv":  # the user undid one of the adjustments while it was checked
                    self.a.change_cv(job, "en", inputs["head"]["draft"]["plan"]["changes"][0]["id"], True)
                elif key == "facts":
                    self.a.revise_fact(FACTS[1]["id"], expected_version=1, text=FACTS[1]["text"] + " Changed.",
                                       tags=FACTS[1]["tags"])
                else:
                    current = self.a.requirements(job)
                    self.a.save_requirements(job, current["candidates"], current["decided"],
                                             expected_version=current["version"])
                outcome = self.a.publish_gaps(task_id=task_id, token=token, job_id=job, language="en", base_version=0,
                                              gaps=gaps, checked_head=inputs["head"]["material_id"],
                                              requirements_version=inputs["requirements"]["version"])
                self.assertEqual(outcome, {"superseded": True})
                self.assertEqual(self.a.get_task(task_id)["error_code"], "inputs_changed")
                self.assertEqual(self.a.job_snapshot(job)["gaps"], {"en": None, "zh": None})

    def test_requirement_change_makes_the_cv_outdated(self):
        job = self.ready(self.a)
        self.prepare(self.a, job)
        current = self.a.requirements(job)
        decided = current["decided"]
        with self.assertRaises(Conflict):
            self.a.save_requirements(job, current["candidates"], decided, expected_version=0)
        self.a.save_requirements(job, current["candidates"], decided, expected_version=1)
        self.assertFalse(self.a.job_snapshot(job)["cv"]["en"]["material"]["inputs_current"])
        with self.assertRaises(NotFound):
            self.b.save_requirements(job, current["candidates"], decided, expected_version=2)

    def test_a_request_sent_again_finds_its_job_as_first_captured(self):
        jd = {"text": "Python and SQL.", "title": "Intern", "source": None, "captured_at": "2026-09-30T00:00:00+00:00"}
        first = self.a.create_job(jd, request_id="paste:abc")
        again = self.a.create_job({**jd, "captured_at": "2026-09-30T00:05:00+00:00"}, request_id="paste:abc")
        self.assertEqual((again["job_id"], again["jd"]["captured_at"]), (first["job_id"], "2026-09-30T00:00:00+00:00"))
        with self.assertRaises(Conflict):
            self.a.create_job({**jd, "text": "Go."}, request_id="paste:abc")
        # A task reading its posting again on a retry keeps the job its first attempt made.
        task_id, token = run_task(self.database, self.a_id, "job_from_url", key="url")
        made = self.a.task_job(task_id=task_id, token=token, jd=jd)
        recover_tasks(self.database)  # the process stopped; the user runs it again
        self.a.submit_task("job_from_url", key="url", request={"x": "url"}, units=1, heavy="model")
        token = claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))[0]["token"]
        reread = self.a.task_job(task_id=task_id, token=token,
                                 jd={**jd, "text": "Python and SQL, updated.", "captured_at": "2026-09-30T01:00:00+00:00"})
        self.assertEqual((reread["job_id"], reread["jd"]), (made["job_id"], made["jd"]))

    def test_summaries_list_only_own_jobs_with_their_steps(self):
        job = self.ready(self.a)
        self.prepare(self.a, job)
        head = self.a.job_snapshot(job)["cv"]["en"]["material"]
        self.a.approve(job, "en", head["content_sha256"])
        summaries = self.a.job_summaries()
        self.assertEqual([item["job_id"] for item in summaries], [job])
        self.assertIn("cv-approved-en", summaries[0]["steps"])
        self.assertEqual(self.b.job_summaries(), [])


class LocalCVTests(StoreCase):
    def local_job(self):
        job = self.ready(self.a)
        self.prepare(self.a, job, plan=False)
        return job

    def head(self, job):
        return self.a.cv_inputs(job, "en")["head"]

    def test_edit_undo_redo_are_local_and_each_changed_version_needs_approval(self):
        from cv_edit import shown_text
        job = self.local_job()
        other = self.a.create_job(decided_job()["jd"], request_id="other")["job_id"]
        self.a.save_requirements(other, decided_job(), decided_job(), expected_version=0)
        self.prepare(self.a, other, plan=False, key="other-cv")
        other_head = self.head(other)
        before = self.head(job)
        facts, profile = self.a.list_facts(), self.a.get_profile("en")
        block = "fact-intern-api"
        old = shown_text(before["draft"], block)
        self.a.approve(job, "en", before["content_sha256"])
        changed = self.a.edit_cv_line(job, "en", block, "Led API delivery with AWS.", expected_text=old, request_id="edit-1")
        self.assertFalse(self.a.preview(job, "en")[1])
        with self.assertRaises(Conflict):
            self.a.approve(job, "en", before["content_sha256"])
        self.assertEqual(self.a.edit_cv_line(job, "en", block, "Led API delivery with AWS.",
                                           expected_text=old, request_id="edit-1"), changed)
        with self.assertRaises(Conflict):
            self.a.edit_cv_line(job, "en", block, "Different retry", expected_text=old, request_id="edit-1")
        with self.assertRaises(Conflict):
            self.a.edit_cv_line(job, "en", block, "Unseen overwrite", expected_text=old)
        with self.assertRaises(NotFound):
            self.b.edit_cv_line(job, "en", block, "Cross user", expected_text=old)
        edit = self.head(job)["draft"]["edits"][-1]
        self.a.set_cv_edit(job, "en", edit["id"], True, request_id="undo")
        self.assertEqual(shown_text(self.head(job)["draft"], block), old)
        self.a.set_cv_edit(job, "en", edit["id"], False, request_id="redo")
        self.assertEqual(shown_text(self.head(job)["draft"], block), "Led API delivery with AWS.")
        self.a.approve(job, "en", self.head(job)["content_sha256"])
        self.assertTrue(self.a.preview(job, "en")[1])
        self.assertEqual((self.a.list_facts(), self.a.get_profile("en"), self.head(other)), (facts, profile, other_head))

    def test_explicit_save_checks_visible_words_is_atomic_and_retry_does_not_add_versions(self):
        from cv_edit import shown_text
        job = self.local_job()
        block = "fact-intern-api"
        old = shown_text(self.head(job)["draft"], block)
        text = "Delivered API services on AWS."
        self.a.edit_cv_line(job, "en", block, text, expected_text=old)
        before = (self.a.list_facts(), self.a.get_profile("en"), self.head(job))
        with self.assertRaises(Conflict):
            self.a.save_cv_line(job, "en", block, expected_text=old)
        with patch.object(UserWorkspace, "_insert_material", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaisesRegex(StoreError, "storage operation failed"):
                self.a.save_cv_line(job, "en", block, expected_text=text, request_id="save")
        self.assertEqual((self.a.list_facts(), self.a.get_profile("en"), self.head(job)), before)
        saved = self.a.save_cv_line(job, "en", block, expected_text=text, request_id="save")
        self.assertEqual(self.a.get_fact(block)["status"], "pending")
        self.assertEqual(self.a.get_fact(block)["version"], 2)
        self.assertTrue(self.a.job_snapshot(job)["cv"]["en"]["material"]["inputs_current"])
        self.assertEqual(self.a.save_cv_line(job, "en", block, expected_text=text, request_id="save"), saved)
        self.assertFalse(self.a.save_cv_line(job, "en", block, expected_text=text, request_id="save-again")["changed"])
        self.assertEqual(self.a.get_fact(block)["version"], 2)
        self.assertEqual(self.head(job)["material_id"], saved["material_id"])
        with self.assertRaises(NotFound):
            self.b.save_cv_line(job, "en", block, expected_text=text)

    def test_new_local_line_save_adds_one_pending_entry_and_keeps_current_cv(self):
        from cv_edit import add_line
        from tests.test_cv_edit import experience_key
        job = self.local_job()
        self.a._edit_cv(job, "en", "add", ["synthetic-add"], lambda c, h, d, key:
            (*add_line(d, experience_key(d), "Led a Kubernetes rollout.", request=key), None))
        edit = self.head(job)["draft"]["edits"][-1]
        before = len(self.a.list_facts())
        self.a.save_cv_line(job, "en", edit["block"], expected_text=edit["text"], request_id="save")
        self.assertEqual(len(self.a.list_facts()), before + 1)
        self.assertEqual(self.a.get_fact(edit["block"])["status"], "pending")
        self.assertIn(edit["block"], json.dumps(self.a.get_profile("en")["profile"]))
        self.assertEqual(self.a.get_profile("en")["version"], 2)
        self.assertTrue(self.a.job_snapshot(job)["cv"]["en"]["material"]["inputs_current"])
        self.assertFalse(self.a.save_cv_line(job, "en", edit["block"], expected_text=edit["text"])["changed"])
        self.assertEqual(len(self.a.list_facts()), before + 1)


class GapStoreTests(StoreCase):
    def test_accepting_a_gap_changes_only_this_cv_and_repeating_adds_nothing(self):
        job = self.ready(self.a)
        self.prepare(self.a, job, plan=False)
        inputs = self.a.gaps_inputs(job, "en")
        from gaps import find_gaps
        gaps = find_gaps(inputs["requirements"]["decided"], inputs["head"]["draft"], inputs["facts"],
                         FakeDeepSeek(suggestions), private=inputs["private"])
        task_id, token = run_task(self.database, self.a_id, "check_gaps", key="g", job_id=job)
        self.assertEqual(self.a.publish_gaps(task_id=task_id, token=token, job_id=job, language="en", base_version=0,
                                             gaps=gaps, checked_head=inputs["head"]["material_id"],
                                             requirements_version=inputs["requirements"]["version"]),
                         {"gaps_version": 1})
        integration = next(item["requirement_id"] for item in gaps["requirements"]
                           if item["text"] == "Writing integration tests for services")
        with self.assertRaises(NotFound):
            self.b.accept_gap(job, integration, language="en", shown_version=1)
        before = self.a.list_facts()
        self.a.accept_gap(job, integration, language="en", shown_version=1)
        facts = self.a.list_facts()
        self.assertEqual(facts, before)
        self.assertEqual(self.a.get_profile("en")["version"], 1)
        head = self.a.job_snapshot(job)["cv"]["en"]["material"]
        self.assertIn("Wrote integration tests for internal services.", json.dumps(head["draft"]))
        self.assertEqual(self.a.job_snapshot(job)["gaps"]["en"]["version"], 1)
        self.a.accept_gap(job, integration, language="en", shown_version=1)
        self.assertEqual(self.a.job_snapshot(job)["cv"]["en"]["material"]["material_id"], head["material_id"])
        docker = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        self.a.decline_gap(job, docker, language="en", shown_version=1)
        with self.assertRaises(StoreError):
            self.a.accept_gap(job, docker, language="en", shown_version=2)
        self.assertTrue(self.a.job_snapshot(job)["cv"]["en"]["material"]["inputs_current"])

    def test_local_adoption_allows_profile_changes_but_saving_refuses_a_stale_fact(self):
        job = self.ready(self.a)
        self.prepare(self.a, job, plan=False)
        inputs = self.a.gaps_inputs(job, "en")
        from gaps import find_gaps
        gaps = find_gaps(inputs["requirements"]["decided"], inputs["head"]["draft"], inputs["facts"],
                         FakeDeepSeek(suggestions))
        task_id, token = run_task(self.database, self.a_id, "check_gaps", key="g", job_id=job)
        self.a.publish_gaps(task_id=task_id, token=token, job_id=job, language="en", base_version=0, gaps=gaps,
                            checked_head=inputs["head"]["material_id"],
                            requirements_version=inputs["requirements"]["version"])
        skills = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        self.a.revise_fact("fact-skills-languages", expected_version=1, text="Languages: Python", tags=["Python"])
        # A shared profile change does not forbid local writing, but does keep this CV stale.
        self.a.accept_gap(job, skills, language="en", shown_version=1)
        self.assertEqual(self.a.get_fact("fact-skills-languages")["status"], "pending")
        head = self.a.job_snapshot(job)["cv"]["en"]["material"]
        self.assertFalse(head["inputs_current"])
        before = self.a.list_facts()
        from cv_edit import shown_text
        with self.assertRaises(Conflict):
            self.a.save_cv_line(job, "en", "fact-skills-languages",
                                expected_text=shown_text(head["draft"], "fact-skills-languages"))
        self.assertEqual(self.a.list_facts(), before)
        self.assertEqual(self.a.job_snapshot(job)["gaps"]["en"]["version"], 1)



def no_evidence(request):
    """An evidence answer for whatever requirements are asked: none is shown."""
    return [{"id": item["id"], "verdict": "none"} for item in request["requirements"]]


class GapCheckTests(StoreCase):
    """A check stands only for the requirements it judged and the CV language it was made for
    (docs/architecture-handoff.md, round 1 A and B), through the real check task (v2_flow) and
    store, with an offline model stand-in."""

    def setUp(self):
        super().setUp()
        self.handlers = make_handlers()
        self.model = FakeDeepSeek(suggestions, evidence=no_evidence)
        self.runs = 0

    def run_check(self, user, job, language="en"):
        """The check_gaps task as the runner runs it, from what the page's request queues."""
        last = user.gaps_inputs(job, language)["gaps"]
        request = {"job_id": job, "language": language, "base_gaps_version": last["version"] if last else 0}
        self.runs += 1
        task, token = run_task(self.database, user.user_id, "check_gaps", key=f"check-{self.runs}", request=request,
                               job_id=job)
        self.handlers["check_gaps"](TaskContext(self.database, user, task, token, "check_gaps", request, job,
                                                model=self.model))
        return user.get_task(task)

    def state(self, user, job, language="en"):
        snapshot = user.job_snapshot(job)
        record, requirements = snapshot["gaps"][language], snapshot["requirements"]
        return check_state(record["gaps"], snapshot["cv"][language]["material"]["draft"], snapshot["confirmed"],
                           requirements["decided"], requirements["version"])

    def without_first_requirement(self, user, job):
        current = user.requirements(job)
        changed = copy.deepcopy(current["decided"])
        removed = changed["selected_requirements"].pop(0)
        for candidate in changed["requirement_candidates"]:
            if candidate["id"] == removed["id"]:
                candidate["status"] = "excluded"
        user.save_requirements(job, current["candidates"], changed, expected_version=current["version"])
        return removed

    def bilingual(self, user):
        job = self.ready(user)
        user.save_profile("zh", copy.deepcopy(user.get_profile("en")["profile"]), expected_version=0)
        for language in ("en", "zh"):
            self.prepare(user, job, language=language, plan=False, key=f"prepare-{language}")
        return job

    def rows(self, user, job, language="en"):
        return {row["text"]: row for row in user.job_snapshot(job)["gaps"][language]["gaps"]["requirements"]}

    def test_requirements_change_invalidates_old_check_even_when_words_stay(self):
        job = self.ready(self.a)
        self.prepare(self.a, job, plan=False)
        self.assertEqual(self.run_check(self.a, job)["status"], "succeeded")
        old = self.a.job_snapshot(job)["gaps"]["en"]
        self.assertEqual(old["gaps"]["inputs"], {"requirements_version": 1,
                                                 "material_id": self.a.job_snapshot(job)["cv"]["en"]["material"]["material_id"]})
        self.assertEqual(self.state(self.a, job), "current")
        removed = self.without_first_requirement(self.a, job)
        self.prepare(self.a, job, plan=False, key="prepare-new-requirements")  # the same words, for the new requirements
        snapshot = self.a.job_snapshot(job)
        self.assertTrue(snapshot["cv"]["en"]["material"]["inputs_current"])
        self.assertEqual(self.state(self.a, job), "requirements_changed")
        # Nothing is adopted, skipped or written from it; the check itself stays, as history.
        kept = next(text for text in self.rows(self.a, job) if text != removed["text"])
        rid = self.rows(self.a, job)[kept]["requirement_id"]
        place = next(item["id"] for item in places(snapshot["cv"]["en"]["material"]["draft"]) if item["kind"] == "bullet")
        before = (self.a.list_facts(), self.a.get_profile("en"))
        for act in (lambda: self.a.accept_gap(job, rid, language="en", shown_version=old["version"]),
                    lambda: self.a.decline_gap(job, rid, language="en", shown_version=old["version"]),
                    lambda: self.a.write_gap_line(job, rid, place, "Wrote integration tests for services.",
                                                  language="en", shown_version=old["version"])):
            with self.assertRaisesRegex(Conflict, "requirements changed"):
                act()
        self.assertEqual((self.a.list_facts(), self.a.get_profile("en")), before)
        self.assertEqual(self.a.job_snapshot(job)["gaps"]["en"], old)
        # Checking again judges the requirements of now, and that check stands.
        self.assertEqual(self.run_check(self.a, job)["status"], "succeeded")
        self.assertEqual(self.state(self.a, job), "current")
        self.assertNotIn(removed["text"], self.rows(self.a, job))
        self.assertEqual(self.a.job_snapshot(job)["gaps"]["en"]["gaps"]["inputs"]["requirements_version"], 2)
        with raw(self.database) as connection:
            self.assertEqual(connection.execute("SELECT version FROM job_gaps ORDER BY version").fetchall(), [(1,), (2,)])

    def test_requirements_saved_again_in_the_same_words_are_another_version(self):
        job = self.ready(self.a)
        self.prepare(self.a, job, plan=False)
        self.run_check(self.a, job)
        current = self.a.requirements(job)
        self.a.save_requirements(job, current["candidates"], current["decided"], expected_version=current["version"])
        self.assertEqual(self.state(self.a, job), "requirements_changed")

    def test_each_language_reads_its_own_latest_check(self):
        job = self.bilingual(self.a)
        for language in ("en", "zh"):
            self.assertEqual(self.run_check(self.a, job, language)["status"], "succeeded")
        en, zh = (self.a.gaps_inputs(job, language)["gaps"] for language in ("en", "zh"))
        # One version sequence per job (the table's key), each language finding its own newest.
        self.assertEqual((en["gaps"]["language"], en["version"], zh["gaps"]["language"], zh["version"]), ("en", 1, "zh", 2))
        self.assertEqual(self.a.job_snapshot(job)["gaps"], {"en": en, "zh": zh})
        self.assertEqual((self.state(self.a, job, "en"), self.state(self.a, job, "zh")), ("current", "current"))

    def test_a_language_never_checked_borrows_nothing_from_the_other(self):
        job = self.bilingual(self.a)
        self.run_check(self.a, job, "en")
        self.assertIsNone(self.a.gaps_inputs(job, "zh")["gaps"])
        self.assertIsNone(self.a.job_snapshot(job)["gaps"]["zh"])
        docker = self.rows(self.a, job)["Hands-on Docker and Kubernetes"]["requirement_id"]
        with self.assertRaises(NotFound):  # the English check's version names no Chinese check
            self.a.decline_gap(job, docker, language="zh", shown_version=1)
        self.assertEqual(self.rows(self.a, job)["Hands-on Docker and Kubernetes"]["status"], "open")

    def test_checks_and_changes_in_one_language_never_replace_or_block_the_other(self):
        job = self.bilingual(self.a)
        self.run_check(self.a, job, "en")  # version 1
        docker = self.rows(self.a, job)["Hands-on Docker and Kubernetes"]["requirement_id"]
        # A Chinese check starts; meanwhile the English one is changed (a skipped suggestion).
        zh_inputs = self.a.gaps_inputs(job, "zh")
        found = find_gaps(zh_inputs["requirements"]["decided"], zh_inputs["head"]["draft"], zh_inputs["facts"], self.model)
        task, token = run_task(self.database, self.a_id, "check_gaps", key="zh-meanwhile", job_id=job)
        self.assertEqual(self.a.decline_gap(job, docker, language="en", shown_version=1)["gaps_version"], 2)
        # Its base is the Chinese newest (none), which the English change did not move.
        self.assertEqual(self.a.publish_gaps(task_id=task, token=token, job_id=job, language="zh", base_version=0,
                                             gaps=found, checked_head=zh_inputs["head"]["material_id"],
                                             requirements_version=zh_inputs["requirements"]["version"]),
                         {"gaps_version": 3})
        self.assertEqual(self.rows(self.a, job, "en")["Hands-on Docker and Kubernetes"]["status"], "declined")
        self.assertEqual(self.rows(self.a, job, "zh")["Hands-on Docker and Kubernetes"]["status"], "open")
        # Adopting in Chinese changes only that CV; neither shared profile nor check moves.
        tests = self.rows(self.a, job, "zh")["Writing integration tests for services"]["requirement_id"]
        with self.assertRaisesRegex(Conflict, "page showed"):  # the English check's version, from a page of the other language
            self.a.accept_gap(job, tests, language="zh", shown_version=2)
        self.a.accept_gap(job, tests, language="zh", shown_version=3)
        self.assertEqual((self.a.get_profile("en")["version"], self.a.get_profile("zh")["version"]), (1, 1))
        self.assertEqual(self.a.job_snapshot(job)["gaps"]["en"]["version"], 2)
        self.assertEqual(self.rows(self.a, job, "en")["Writing integration tests for services"]["status"], "open")
        # A check of one language that read an older check of that language is still superseded.
        en = self.a.gaps_inputs(job, "en")
        task, token = run_task(self.database, self.a_id, "check_gaps", key="en-stale", job_id=job)
        self.assertEqual(self.a.publish_gaps(task_id=task, token=token, job_id=job, language="en", base_version=1,
                                             gaps=find_gaps(en["requirements"]["decided"], en["head"]["draft"], en["facts"],
                                                            self.model),
                                             checked_head=en["head"]["material_id"],
                                             requirements_version=en["requirements"]["version"]),
                         {"superseded": True})
        self.assertEqual(self.a.get_task(task)["error_code"], "superseded")  # its base, before any input
        with raw(self.database) as connection:
            self.assertEqual(connection.execute("SELECT version, json_extract(payload, '$.language') FROM job_gaps "
                                                "ORDER BY version").fetchall(), [(1, "en"), (2, "en"), (3, "zh")])

    def test_a_page_adopts_or_skips_only_the_suggestion_it_showed(self):
        job = self.ready(self.a)
        self.prepare(self.a, job, plan=False)
        self.run_check(self.a, job)  # version 1
        rows = self.rows(self.a, job)
        docker, tests = (rows[text]["requirement_id"] for text in ("Hands-on Docker and Kubernetes",
                                                                   "Writing integration tests for services"))
        self.a.decline_gap(job, tests, language="en", shown_version=1)  # version 2: another row, from the same page
        # Checked again in another tab, with another suggestion for Docker (version 3).
        self.model.suggestions = lambda ids: [{"requirement": ids["Hands-on Docker and Kubernetes"], "kind": "skill",
                                               "line": "fact-skills-languages", "items": ["Docker", "Helm"]}]
        self.run_check(self.a, job)
        for act in (lambda: self.a.accept_gap(job, docker, language="en", shown_version=1),
                    lambda: self.a.decline_gap(job, docker, language="en", shown_version=1)):
            with self.assertRaisesRegex(Conflict, "suggestion changed"):
                act()
        self.assertEqual(self.rows(self.a, job)["Hands-on Docker and Kubernetes"]["status"], "open")
        self.a.decline_gap(job, docker, language="en", shown_version=3)
        self.assertEqual(self.rows(self.a, job)["Hands-on Docker and Kubernetes"]["status"], "declined")

    def test_checks_kept_before_they_named_their_inputs_stay_and_are_to_check_again(self):
        job = self.ready(self.a)
        self.prepare(self.a, job, plan=False)
        inputs = self.a.gaps_inputs(job, "en")
        earlier = find_gaps(inputs["requirements"]["decided"], inputs["head"]["draft"], inputs["facts"], self.model)
        # As an earlier release (or v2_migrate) kept them: no record of the requirements version read;
        # and one so old it names no language.
        with raw(self.database) as connection:
            connection.execute("INSERT INTO job_gaps VALUES (?, ?, 1, ?, ?)",
                               (self.a_id, job, json.dumps({"gaps_version": 1, "gaps": []}), "2026-09-01T00:00:00+00:00"))
            connection.execute("INSERT INTO job_gaps VALUES (?, ?, 2, ?, ?)",
                               (self.a_id, job, tenant_store._json(earlier), "2026-09-02T00:00:00+00:00"))
        self.assertEqual(self.a.gaps_inputs(job, "en")["gaps"]["version"], 2)
        self.assertEqual(self.state(self.a, job), "unbound")  # never current by default
        # Its suggestions stay usable, and what is saved from it never claims inputs it did not read.
        docker = self.rows(self.a, job)["Hands-on Docker and Kubernetes"]["requirement_id"]
        self.assertEqual(self.a.decline_gap(job, docker, language="en", shown_version=2)["gaps_version"], 3)
        self.assertNotIn("inputs", self.a.job_snapshot(job)["gaps"]["en"]["gaps"])
        self.assertEqual(self.state(self.a, job), "unbound")
        # Requirements changed since: not usable at all.
        self.without_first_requirement(self.a, job)
        self.assertEqual(self.state(self.a, job), "requirements_changed")
        tests = self.rows(self.a, job)["Writing integration tests for services"]["requirement_id"]
        with self.assertRaises(Conflict):
            self.a.accept_gap(job, tests, language="en", shown_version=3)
        # A new check is kept with its inputs, after every earlier row, none of which is gone.
        self.prepare(self.a, job, plan=False, key="prepare-again")
        self.run_check(self.a, job)
        self.assertEqual(self.state(self.a, job), "current")
        with raw(self.database) as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT version FROM job_gaps ORDER BY version")],
                             [1, 2, 3, 4])


class BoardTests(StoreCase):
    POSTINGS = [{"job_id": "1", "title": "Python Intern", "location": "Remote", "source": "https://example.com/1",
                 "text": "Python and Java"}, {"job_id": "2", "title": "Designer", "location": None,
                                              "source": "https://example.com/2", "text": "Figma"}]

    def test_postings_are_shared_public_data_but_follows_and_matches_are_personal(self):
        store_public_board(self.database, "greenhouse", "example", "Example", self.POSTINGS)
        self.a.follow_board("greenhouse", "example", "Example")
        with self.assertRaises(Conflict):
            self.a.follow_board("greenhouse", "example", "Example")
        self.assertEqual(self.b.followed_boards(), [])
        self.assertEqual(self.b.ranking_rows(["Python"], lambda text: ["Python"]), [])
        calls = []

        def find(text):
            calls.append(text)
            return ["Python"] if "Python" in text else []
        rows = self.a.ranking_rows(["Python"], find)
        self.assertEqual({row["job_id"]: row["matched"] for row in rows}, {"1": ["Python"], "2": []})
        self.a.ranking_rows(["Python"], find)
        self.assertEqual(len(calls), 2)  # cached for this user and these skills
        self.a.ranking_rows(["Python", "Figma"], find)
        self.assertEqual(len(calls), 4)  # skills changed: matched again
        with self.assertRaises(NotFound):
            self.b.followed_posting("greenhouse", "example", "1")
        self.assertEqual(self.a.followed_posting("greenhouse", "example", "1")["company"], "Example")
        store_public_board(self.database, "greenhouse", "example", "Example", self.POSTINGS[:1])
        self.assertEqual([row["job_id"] for row in self.a.ranking_rows(["Python"], find)], ["1"])
        self.a.unfollow_board("greenhouse", "example")
        self.assertEqual(self.a.followed_boards(), [])
        with self.assertRaises(NotFound):
            self.a.unfollow_board("greenhouse", "example")


class TaskTests(StoreCase):
    def test_same_key_returns_the_same_task_and_a_different_request_conflicts(self):
        first = self.a.submit_task("check_gaps", key="k1", request={"v": 1}, units=2, heavy="model")
        self.assertEqual(self.a.submit_task("check_gaps", key="k1", request={"v": 1}, units=2, heavy="model"), first)
        with self.assertRaises(Conflict):
            self.a.submit_task("check_gaps", key="k1", request={"v": 2}, units=2, heavy="model")
        self.assertEqual(self.a.usage_today()["used"], 2)  # the repeat reserved nothing
        other = self.b.submit_task("check_gaps", key="k1", request={"v": 1}, units=2, heavy="model")
        self.assertNotEqual(other["task_id"], first["task_id"])
        with self.assertRaises(NotFound):
            self.b.get_task(first["task_id"])
        with self.assertRaises(NotFound):
            self.b.cancel_task(first["task_id"])

    def test_the_same_request_finds_its_task_once_done_and_a_retry_takes_the_context_of_now(self):
        asked = {"job_id": "j", "language": "en"}
        first = self.a.submit_task("prepare_cv", key="click", request=asked, context={"base_head": "m1"},
                                   units=1, heavy="model")
        claimed = claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))
        self.assertEqual(claimed[0]["request"], {**asked, "base_head": "m1"})
        finish_task(self.database, self.a_id, first["task_id"], claimed[0]["token"], "succeeded", result={"ok": 1})
        # The answer was lost and the page sends the same action again, after the CV has moved on.
        again = self.a.submit_task("prepare_cv", key="click", request=asked, context={"base_head": "m2"},
                                   units=1, heavy="model")
        self.assertEqual((again["task_id"], again["status"]), (first["task_id"], "succeeded"))
        self.assertEqual(self.a.usage_today()["used"], 1)
        for changed in ({"job_id": "j", "language": "zh"}, {"job_id": "other", "language": "en"}, {"job_id": "j"}):
            with self.assertRaises(Conflict):
                self.a.submit_task("prepare_cv", key="click", request=changed, context={"base_head": "m2"},
                                   units=1, heavy="model")
        failed = self.a.submit_task("prepare_cv", key="click-2", request=asked, context={"base_head": "m2"},
                                    units=1, heavy="model")
        claimed = claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))
        finish_task(self.database, self.a_id, failed["task_id"], claimed[0]["token"], "failed", error_code="x")
        retried = self.a.submit_task("prepare_cv", key="click-2", request=asked, context={"base_head": "m3"},
                                     units=1, heavy="model")
        self.assertEqual((retried["task_id"], retried["status"]), (failed["task_id"], "queued"))
        claimed = claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))
        self.assertEqual(claimed[0]["request"], {**asked, "base_head": "m3"})

    def test_a_scrubbed_upload_keeps_what_was_asked_and_drops_the_cv_lines(self):
        asked = {"language": "en", "source_sha256": "0" * 64}
        task = self.a.submit_task("upload_cv", key="client:u1", request=asked,
                                  context={"lines": ["Alex Example, alex@example.com"]}, units=1, heavy="model")
        claimed = claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))
        finish_task(self.database, self.a_id, task["task_id"], claimed[0]["token"], "succeeded", result={"ok": 1})
        self.clock.advance(hours=25)
        expire_tasks(self.database)
        with raw(self.database) as connection:
            stored = connection.execute("SELECT request_payload FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone()[0]
        self.assertNotIn("alex@example.com", stored)
        again = self.a.submit_task("upload_cv", key="client:u1", request=asked, context={"lines": ["x"]},
                                   units=1, heavy="model")
        self.assertEqual((again["task_id"], again["status"]), (task["task_id"], "succeeded"))

    def test_one_waiting_task_per_user_bounded_queue_and_daily_quota(self):
        self.a.submit_task("t", key="1", request={}, units=1, heavy="model")
        with self.assertRaises(Refused) as waiting:
            self.a.submit_task("t", key="2", request={}, units=1, heavy="model")
        self.assertEqual(waiting.exception.code, "queue_full")
        claim_tasks(self.database, total=2, pdf=1, deadline=timedelta(minutes=5))
        self.a.submit_task("t", key="2", request={}, units=1, heavy="model")  # one running, one waiting
        set_setting(self.database, "queue_limit", "1")
        with self.assertRaises(Refused) as busy:
            self.b.submit_task("t", key="1", request={}, units=1, heavy="model")
        self.assertEqual(busy.exception.code, "queue_full")
        set_setting(self.database, "queue_limit", "20")
        with self.assertRaises(Refused) as spent:
            self.b.submit_task("t", key="big", request={}, units=21, heavy="model")
        self.assertEqual(spent.exception.code, "quota_exhausted")
        self.assertEqual(self.b.usage_today()["used"], 0)
        set_setting(self.database, "site_daily_units", "3")
        with self.assertRaises(Refused):
            self.b.submit_task("t", key="site", request={}, units=2, heavy="model")
        set_setting(self.database, "tasks_enabled", "0")
        with self.assertRaises(Refused) as paused:
            self.b.submit_task("t", key="p", request={}, units=0, heavy="model")
        self.assertEqual(paused.exception.code, "tasks_paused")

    def test_consent_is_required_before_model_work(self):
        with self.assertRaises(Refused) as refused:
            self.a.submit_task("upload_cv", key="x", request={}, units=1, heavy="model", consent="upload_parsing")
        self.assertEqual(refused.exception.code, "consent_required")
        self.a.give_consent("upload_parsing", 1)
        self.a.submit_task("upload_cv", key="x", request={}, units=1, heavy="model", consent="upload_parsing")

    def test_claims_run_one_task_per_user_and_one_pdf_at_a_time(self):
        c_id = provision_user(self.database, issuer=ISSUER, subject="C")
        c = UserWorkspace(self.database, c_id)
        self.a.submit_task("export_pdf", key="1", request={}, units=1, heavy="pdf")
        claimed = claim_tasks(self.database, total=2, pdf=1, deadline=timedelta(minutes=5))
        self.a.submit_task("t", key="2", request={}, units=1, heavy="model")
        self.b.submit_task("export_pdf", key="1", request={}, units=1, heavy="pdf")
        c.submit_task("t", key="1", request={}, units=1, heavy="model")
        more = claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))
        self.assertEqual([item["user_id"] for item in claimed + more], [self.a_id, c_id])
        set_user_active(self.database, self.b_id, active=False)
        self.assertEqual(claim_tasks(self.database, total=2, pdf=1, deadline=timedelta(minutes=5)), [])

    def test_failure_before_spending_gives_units_back_and_unknown_cost_keeps_them(self):
        task_id, token = run_task(self.database, self.a_id, "t", key="free", units=3)
        finish_task(self.database, self.a_id, task_id, token, "failed", error_code="bad_input", message="no")
        self.assertEqual(self.a.usage_today()["used"], 0)
        task_id, token = run_task(self.database, self.a_id, "t", key="paid", units=3)
        mark_spending(self.database, self.a_id, task_id, token, "model")
        finish_task(self.database, self.a_id, task_id, token, "failed", error_code="unreachable", message="lost")
        view = self.a.get_task(task_id)
        self.assertEqual((view["status"], view["cost"]), ("failed", "unknown"))
        self.assertEqual(self.a.usage_today()["used"], 3)
        task_id, token = run_task(self.database, self.b_id, "t", key="known", units=1)
        mark_spending(self.database, self.b_id, task_id, token, "model")
        record_usage(self.database, self.b_id, task_id, token, {"prompt_tokens": 10, "completion_tokens": 3})
        record_usage(self.database, self.b_id, task_id, token, {"prompt_tokens": 1})
        finish_task(self.database, self.b_id, task_id, token, "succeeded", result={"ok": True})
        self.assertEqual(self.b.get_task(task_id)["cost"], "known")
        with raw(self.database) as connection:
            usage = connection.execute("SELECT usage FROM tasks WHERE task_id = ?", (task_id,)).fetchone()[0]
        self.assertEqual(json.loads(usage), {"tokens": {"prompt_tokens": 11, "completion_tokens": 3}, "calls": 1,
                                             "open_calls": 0, "attempt_calls": 1, "late_calls": 0})
        with self.assertRaises(LateResult):
            mark_spending(self.database, self.b_id, task_id, token, "model")

    def test_one_call_without_a_known_cost_keeps_the_task_cost_unknown(self):
        task_id, token = run_task(self.database, self.a_id, "t", key="calls", units=2)
        mark_spending(self.database, self.a_id, task_id, token, "first")  # this call raised: no usage
        mark_spending(self.database, self.a_id, task_id, token, "second")
        record_usage(self.database, self.a_id, task_id, token, {"prompt_tokens": 10, "completion_tokens": 2})
        self.assertEqual(self.a.get_task(task_id)["cost"], "unknown")
        mark_spending(self.database, self.a_id, task_id, token, "third")
        record_usage(self.database, self.a_id, task_id, token, {})  # answered, but reported no usage
        self.assertEqual(self.a.get_task(task_id)["cost"], "unknown")
        finish_task(self.database, self.a_id, task_id, token, "failed", error_code="x")
        self.assertEqual(self.a.usage_today()["used"], 2)  # an unknown cost keeps its units
        task_id, token = run_task(self.database, self.b_id, "t", key="settled", units=1)
        for _ in range(2):
            mark_spending(self.database, self.b_id, task_id, token, "model")
            record_usage(self.database, self.b_id, task_id, token, {"prompt_tokens": 3})
        self.assertEqual(self.b.get_task(task_id)["cost"], "known")

    def test_an_answer_for_an_ended_execution_never_changes_the_retry(self):
        task_id, old_token = run_task(self.database, self.a_id, "t", key="late", units=2)
        mark_spending(self.database, self.a_id, task_id, old_token, "model")
        self.a.cancel_task(task_id)  # cancelled while its call was under way
        retry = self.a.submit_task("t", key="late", request={"x": "late"}, units=2, heavy="model")
        # The task's cost stays unknown through the retry: the first attempt's call never answered.
        self.assertEqual((retry["task_id"], retry["status"], retry["cost"]), (task_id, "queued", "unknown"))
        new_token = claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))[0]["token"]
        record_usage(self.database, self.a_id, task_id, old_token, {"prompt_tokens": 50})  # the old call answers now
        self.assertEqual(self.a.get_task(task_id)["cost"], "known")  # every call the task made has its usage
        finish_task(self.database, self.a_id, task_id, new_token, "failed", error_code="bad_input")  # before any call
        self.assertEqual(self.a.usage_today()["used"], 2)  # the retry's units came back; the first attempt's did not
        with raw(self.database) as connection:
            usage = json.loads(connection.execute("SELECT usage FROM tasks WHERE task_id = ?", (task_id,)).fetchone()[0])
        self.assertEqual(usage, {"tokens": {"prompt_tokens": 50}, "calls": 1, "open_calls": 0, "attempt_calls": 0,
                                 "late_calls": 1})

    def test_an_unknown_cost_stays_with_the_task_through_later_attempts(self):
        task_id, token = run_task(self.database, self.a_id, "t", key="lost", units=2)
        mark_spending(self.database, self.a_id, task_id, token, "model")  # its answer never comes
        finish_task(self.database, self.a_id, task_id, token, "failed", error_code="response_lost")

        def again():
            self.a.submit_task("t", key="lost", request={"x": "lost"}, units=2, heavy="model")
            return claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))[0]["token"]

        token = again()  # this attempt fails before calling the model: its own units come back
        finish_task(self.database, self.a_id, task_id, token, "failed", error_code="bad_input")
        self.assertEqual((self.a.usage_today()["used"], self.a.get_task(task_id)["cost"]), (2, "unknown"))
        token = again()  # this one calls, is answered and succeeds: the first call is still unknown
        mark_spending(self.database, self.a_id, task_id, token, "model")
        record_usage(self.database, self.a_id, task_id, token, {"prompt_tokens": 5, "completion_tokens": 2})
        finish_task(self.database, self.a_id, task_id, token, "succeeded", result={"ok": True})
        view = self.a.get_task(task_id)
        self.assertEqual((view["status"], view["attempts"], view["cost"]), ("succeeded", 3, "unknown"))
        self.assertEqual(self.a.usage_today()["used"], 4)
        with raw(self.database) as connection:
            usage = json.loads(connection.execute("SELECT usage FROM tasks WHERE task_id = ?", (task_id,)).fetchone()[0])
        self.assertEqual(usage, {"tokens": {"prompt_tokens": 5, "completion_tokens": 2}, "calls": 2, "open_calls": 1,
                                 "attempt_calls": 1, "late_calls": 0})

    def test_nothing_is_called_or_published_after_the_deadline_even_before_the_sweeper(self):
        job = self.ready(self.a)
        inputs = self.a.cv_inputs(job, "en")
        draft = build_draft_from_facts(inputs["profile"]["profile"], inputs["facts"].current(
            [fact["id"] for fact in inputs["facts"].listed()]), "en", job=inputs["requirements"]["decided"])
        task_id, token = run_task(self.database, self.a_id, "prepare_cv", key="slow", job_id=job)  # 5 minutes
        self.clock.advance(minutes=4, seconds=59)
        self.assertEqual(self.a.task_request(task_id, token), {"x": "slow"})
        mark_spending(self.database, self.a_id, task_id, token, "model")
        record_usage(self.database, self.a_id, task_id, token, {"prompt_tokens": 1})
        self.clock.advance(seconds=1)  # the deadline itself: from here on everything is late
        attempts = {
            "call": lambda: mark_spending(self.database, self.a_id, task_id, token, "model"),
            "stage": lambda: tenant_store.set_stage(self.database, self.a_id, task_id, token, "layout"),
            "request": lambda: self.a.task_request(task_id, token),
            "publish": lambda: self.a.publish_cv(task_id=task_id, token=token, job_id=job, language="en",
                                                 base_head=None, documents=[("draft", draft)],
                                                 profile_version=inputs["profile"]["version"],
                                                 requirements_version=inputs["requirements"]["version"], stages=[]),
            "stages": lambda: self.a.record_stages(task_id=task_id, token=token, job_id=job, language="en",
                                                   draft_id=None, stages=[]),
            "finish": lambda: finish_task(self.database, self.a_id, task_id, token, "succeeded", result={"late": 1}),
        }
        for name, attempt in attempts.items():
            with self.subTest(late=name), self.assertRaises(LateResult):
                attempt()
        self.assertEqual(self.a.get_task(task_id)["status"], "running")  # the sweeper has not run yet
        self.assertEqual(self.a.list_materials(job), [])
        self.assertEqual(expire_tasks(self.database), [(self.a_id, task_id)])
        view = self.a.get_task(task_id)
        self.assertEqual((view["status"], view["error_code"], view["cost"]), ("failed", "timeout", "known"))

    def test_restart_interrupts_running_work_and_resubmitting_retries_it(self):
        task_id, token = run_task(self.database, self.a_id, "t", key="k", units=1)
        mark_spending(self.database, self.a_id, task_id, token, "model")
        self.assertEqual(recover_tasks(self.database), 1)
        view = self.a.get_task(task_id)
        self.assertEqual((view["status"], view["cost"]), ("interrupted", "unknown"))
        with self.assertRaises(LateResult):
            finish_task(self.database, self.a_id, task_id, token, "succeeded")
        again = self.a.submit_task("t", key="k", request={"x": "k"}, units=1, heavy="model")
        self.assertEqual((again["task_id"], again["status"]), (task_id, "queued"))
        self.assertEqual(self.a.usage_today()["used"], 2)  # the retry is new work
        for _ in range(2):
            claimed = claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))
            finish_task(self.database, self.a_id, task_id, claimed[0]["token"], "failed", error_code="x")
            if self.a.get_task(task_id)["attempts"] < tenant_store.MAX_TASK_ATTEMPTS:
                self.a.submit_task("t", key="k", request={"x": "k"}, units=1, heavy="model")
        with self.assertRaises(Refused) as tried:
            self.a.submit_task("t", key="k", request={"x": "k"}, units=1, heavy="model")
        self.assertEqual(tried.exception.code, "too_many_attempts")

    def test_deadlines_end_running_and_long_waiting_tasks(self):
        task_id, token = run_task(self.database, self.a_id, "t", key="k", units=1)
        waiting = self.b.submit_task("t", key="w", request={}, units=1, heavy="model")
        self.clock.advance(minutes=6)
        self.assertEqual(expire_tasks(self.database), [(self.a_id, task_id)])
        with self.assertRaises(LateResult):
            finish_task(self.database, self.a_id, task_id, token, "succeeded")
        self.clock.advance(minutes=10)
        self.assertEqual(expire_tasks(self.database), [(self.b_id, waiting["task_id"])])
        self.assertEqual(self.b.get_task(waiting["task_id"])["error_code"], "expired")
        self.assertEqual(self.b.usage_today()["used"], 0)

    def test_task_views_never_show_stored_input(self):
        task = self.a.submit_task("upload_cv", key="k", request={"lines": ["Alex Example, alex@example.com"]},
                                  units=1, heavy="model")
        self.assertNotIn("alex@example.com", json.dumps(self.a.get_task(task["task_id"])))
        self.assertNotIn("request", self.a.get_task(task["task_id"]))


class SnapshotTests(unittest.TestCase):
    def test_a_snapshot_cannot_write(self):
        snapshot = FactSnapshot([{"id": "fact-a", "version": 1, "status": "confirmed", "text": "x"}])
        self.assertEqual(snapshot.current(["fact-a", "fact-missing"]), {"fact-a": snapshot.listed()[0]})
        with self.assertRaises(StoreError):
            snapshot.add_confirmed("text", "project", [])


if __name__ == "__main__":
    unittest.main()
