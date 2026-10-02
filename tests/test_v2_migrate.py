"""Migrating a synthetic single-user workspace into V2 for one named owner, going back, and V2
backups. The V1 workspace is made by the V1 app itself with scripted DeepSeek and printer."""
import contextlib
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import tenant_store
import v2_backup
import v2_migrate
from facts import confirm_facts, import_facts, revise_fact
from identity import Identity, OIDCSettings
from tenant_store import UserWorkspace, claim_tasks, create_session, delete_account, read_deletions, set_setting
from tests.fake_identity import CLIENT_ID, DOMAIN, ISSUER, ORIGIN, FakeCognito
from tests.test_cv import FACTS, PROFILE, FakePrinter
from tests.test_web import FakeDeepSeek
from v2_flow import make_handlers
from web import create_app
from web_v2 import V2Settings, create_v2_app
from workspace import recorded_data_format

JD = (Path(__file__).resolve().parent.parent / "examples" / "synthetic_jd_zh.txt").read_text(encoding="utf-8")


def fingerprint(folder: Path) -> dict[str, str]:
    """Every file under ``folder`` by its hash: the migration must leave V1 exactly as it was."""
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*")) if path.is_file()}


def make_v1_workspace(data: Path) -> dict:
    """A V1 folder with two jobs: one approved and exported whose approval still holds, one whose
    approval a later fact correction made outdated; bilingual profile with a history version."""
    data.mkdir(parents=True)
    database = data / "workbench.db"
    import_facts(database, FACTS)
    confirm_facts(database, [(item["id"], 1) for item in FACTS])
    older = json.loads(json.dumps(PROFILE))
    older["contact"]["email"] = "old@example.com"
    (data / "profile-history").mkdir()
    (data / "profile-history" / "cv-profile-20260901T120000000000.json").write_text(json.dumps(older), encoding="utf-8")
    (data / "cv-profile.json").write_text(json.dumps(PROFILE, ensure_ascii=False), encoding="utf-8")
    chat = FakeDeepSeek()
    printer = FakePrinter()
    app = create_app(facts_db=database, jobs_root=data / "jobs", token="t", profile_path=data / "cv-profile.json",
                     chat=chat, printer=printer, starter=[], boards=lambda *_: [])
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-Workbench-Token": "t"}
    jobs = []
    for title in ("Kept approval", "Outdated approval"):
        created = client.post("/api/jobs", json={"title": title, "text": JD}, headers=headers)
        assert created.status_code == 200, created.text
        job_id = created.json()["job_id"]
        view = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        language = view["language"]
        approved = client.post(f"/api/jobs/{job_id}/cv/{language}/approve", headers=headers,
                               json={"expected_content_sha256": view["cv"][language]["content_sha256"]})
        assert approved.status_code == 200, approved.text
        assert client.post(f"/api/jobs/{job_id}/cv/{language}/export", headers=headers).status_code == 200
        jobs.append((job_id, language))
    # A correction confirmed afterwards: every CV using that fact is outdated, both approvals included...
    revise_fact(database, "fact-intern-tests", text="Wrote unit tests for payment code.")
    confirm_facts(database, [("fact-intern-tests", 2)])
    # ...and the first job's CV is prepared and approved again, so only the second stays outdated.
    job_id, language = jobs[0]
    client.post(f"/api/jobs/{job_id}/cv/{language}/prepare", headers=headers)
    view = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    client.post(f"/api/jobs/{job_id}/cv/{language}/approve", headers=headers,
                json={"expected_content_sha256": view["cv"][language]["content_sha256"]})
    client.post(f"/api/jobs/{job_id}/cv/{language}/export", headers=headers)
    return {"kept": jobs[0], "outdated": jobs[1]}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.v1 = self.root / "data"
        self.jobs = make_v1_workspace(self.v1)
        self.v2 = self.root / "data" / "v2.db"

    def test_the_owner_gets_every_version_and_only_approvals_that_still_hold(self):
        before = fingerprint(self.v1)
        report = v2_migrate.migrate(self.v1, self.v2, issuer=ISSUER, subject="owner-sub")
        # The V1 files stay as they were; only V2's database and its (empty) deletion ledger are added.
        self.assertEqual(fingerprint(self.v1), {**before, **{name: digest for name, digest in fingerprint(self.v1).items()
                                                            if name.startswith("v2.db")
                                                            or name == tenant_store.DELETION_LEDGER}})
        self.assertEqual((self.v1 / tenant_store.DELETION_LEDGER).read_bytes(), b"")
        self.assertEqual((report["facts"], report["fact_versions"]), (len(FACTS), len(FACTS) + 1))
        self.assertEqual(report["profiles"], {"en": 2, "zh": 2})
        self.assertEqual((report["jobs"], report["approvals_carried"], report["final_pdfs"]), (2, 1, 1))
        self.assertEqual(report["approvals_not_carried"], [
            {"job": self.jobs["outdated"][0], "language": self.jobs["outdated"][1],
             "reason": "the approved CV no longer rests on the current confirmed facts"}])
        self.assertEqual(report["verified"]["valid_approvals"], 1)
        self.assertEqual(report["verified"]["foreign_key_problems"], 0)
        owner = UserWorkspace(self.v2, report["owner_user_id"])
        self.assertEqual(owner.get_fact("fact-intern-tests")["version"], 2)
        self.assertEqual(owner.get_fact("fact-intern-tests", version=1)["status"], "confirmed")
        job_id, language = self.jobs["kept"]
        v1_pdf = (self.v1 / "jobs" / job_id / f"cv-final-{language}.pdf").read_bytes()
        self.assertEqual(owner.final_pdf(job_id, language), v1_pdf)
        outdated = owner.job_snapshot(self.jobs["outdated"][0])["cv"][self.jobs["outdated"][1]]
        self.assertIsNone(outdated["approval"])
        self.assertFalse(outdated["material"]["inputs_current"])
        self.assertEqual(owner.get_profile("en", version=1)["profile"]["contact"]["email"], "old@example.com")

    def test_the_migrated_owner_signs_in_and_nobody_else_sees_the_data(self):
        report = v2_migrate.migrate(self.v1, self.v2, issuer=ISSUER, subject="owner-sub")
        cognito = FakeCognito()
        settings = OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, domain=DOMAIN, public_origin=ORIGIN)
        app = create_v2_app(database=self.v2, identity=Identity(settings, self.v2, post=cognito.post, keys=cognito.keys),
                            settings=V2Settings(public_host="feiran.example", public_origin=ORIGIN),
                            handlers=make_handlers(printer=FakePrinter()), model=FakeDeepSeek(), starter=[])
        for key, value in [("user_daily_units", "10"), ("site_daily_units", "100"), ("registration_open", "1")]:
            set_setting(self.v2, key, value)
        with TestClient(app, base_url=ORIGIN) as owner:
            owner_jobs = self.signed_in(owner, cognito, "owner-sub").get("/api/jobs").json()["jobs"]
            self.assertEqual(len(owner_jobs), 2)
            job_id, language = self.jobs["kept"]
            self.assertEqual(owner.get(f"/download/{job_id}/{language}.pdf").status_code, 200)
            other = self.signed_in(TestClient(app, base_url=ORIGIN), cognito, "someone-else")
            self.assertEqual(other.get("/api/jobs").json(), {"jobs": []})
            self.assertEqual(other.get(f"/download/{job_id}/{language}.pdf").status_code, 404)
        self.assertEqual(report["owner_user_id"], UserWorkspace(self.v2, report["owner_user_id"]).user_id)

    def signed_in(self, client, cognito, subject):
        started = client.get("/login", follow_redirects=False)
        client.get("/auth/callback", params=cognito.authorize(started.headers["location"], subject), follow_redirects=False)
        return client

    def test_refusals_write_nothing(self):
        report = v2_migrate.migrate(self.v1, self.v2, issuer=ISSUER, subject="owner-sub")
        before = self.v2.read_bytes()
        with self.assertRaises(v2_migrate.MigrationError):
            v2_migrate.migrate(self.v1, self.v2, issuer=ISSUER, subject="owner-sub")  # no second copy, no merging
        self.assertEqual(len(UserWorkspace(self.v2, report["owner_user_id"]).list_facts()), len(FACTS))
        broken = self.root / "broken"
        make_v1_workspace(broken)
        job = next((broken / "jobs").iterdir())
        (job / "change-in-progress.json").write_text("{}", encoding="utf-8")
        target = self.root / "fresh.db"
        with self.assertRaises(v2_migrate.MigrationError):
            v2_migrate.migrate(broken, target, issuer=ISSUER, subject="x")
        self.assertFalse(target.exists())
        (job / "change-in-progress.json").unlink()
        (broken / ".workbench-format").write_text("2\n", encoding="utf-8")
        with self.assertRaises(v2_migrate.MigrationError):
            v2_migrate.migrate(broken, target, issuer=ISSUER, subject="x")
        self.assertFalse(target.exists())
        self.assertNotEqual(before, b"")

    def test_a_dry_run_reports_without_writing(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = v2_migrate.main(["run", "--v1-data", str(self.v1), "--v2-db", str(self.v2), "--owner-issuer", ISSUER,
                                    "--owner-subject", "owner-sub", "--dry-run"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual((report["dry_run"], report["approvals_carried"]), (True, 1))
        self.assertNotIn("Built REST APIs", output.getvalue())  # IDs and counts only, never CV text
        self.assertFalse(self.v2.exists())

    def test_rollback_keeps_what_v2_made_and_returns_the_folder_to_v1(self):
        report = v2_migrate.migrate(self.v1, self.v2, issuer=ISSUER, subject="owner-sub")
        (self.v1 / ".workbench-format").write_text("2\n", encoding="utf-8")
        owner = UserWorkspace(self.v2, report["owner_user_id"])
        owner.import_facts([{"text": "A line added in V2.", "type": "project", "tags": []}])
        archive = self.root / "backups" / "before-rollback.tar.gz"
        result = v2_migrate.rollback(self.v1, archive)
        self.assertEqual(recorded_data_format(self.v1), 1)
        self.assertFalse(self.v2.exists())
        self.assertTrue((self.v1 / result["set_aside"]).exists())
        restored = self.root / "restored"
        v2_backup.restore_backup(archive, restored, live_ledger=None)
        texts = [fact["text"] for fact in UserWorkspace(restored / "v2.db", report["owner_user_id"]).list_facts()]
        self.assertIn("A line added in V2.", texts)
        # A damaged final PDF in V2 data is found by the check.
        with tenant_store.transaction(restored / "v2.db", write=True) as connection:
            connection.execute("UPDATE artifacts SET data = ?", (b"%PDF-damaged",))
        self.assertTrue(any("does not match its hash" in problem
                            for problem in v2_backup.verify_data(restored)["problems"]))
        # The V1 app opens the folder again.
        create_app(facts_db=self.v1 / "workbench.db", jobs_root=self.v1 / "jobs", token="t",
                   profile_path=self.v1 / "cv-profile.json", starter=[])


class BackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.database = self.data / "v2.db"
        tenant_store.initialize_store(self.database)
        set_setting(self.database, "user_daily_units", "10")
        set_setting(self.database, "site_daily_units", "100")
        self.a = UserWorkspace(self.database, tenant_store.provision_user(self.database, issuer=ISSUER, subject="a"))
        self.b = UserWorkspace(self.database, tenant_store.provision_user(self.database, issuer=ISSUER, subject="b"))
        self.a.import_facts(FACTS)
        self.b.import_facts(FACTS[:1])
        self.ledger = self.data / tenant_store.DELETION_LEDGER
        tenant_store.ensure_ledger(self.database, self.ledger)  # as the app does when it starts

    def test_a_backup_while_work_runs_restores_with_that_work_interrupted_and_no_sessions(self):
        self.a.submit_task("t", key="k", request={}, units=1, heavy="model")
        claim_tasks(self.database, total=1, pdf=0, deadline=timedelta(minutes=5))
        token, _ = create_session(self.database, self.a.user_id, lifetime=timedelta(hours=1), idle=timedelta(hours=1))
        archive = self.root / "backups" / "b.tar.gz"
        made = v2_backup.create_backup(self.data, archive, revision="test")
        self.assertEqual((made["problems"], made["counts"]["tasks"]), ([], {"running": 1}))
        with tarfile_names(archive) as names:
            self.assertEqual(sorted(names), ["data/deleted-accounts.jsonl", "data/v2.db", "manifest.json"])
        restored = self.root / "restored"
        result = v2_backup.restore_backup(archive, restored, live_ledger=self.ledger)
        self.assertEqual((result["interrupted_tasks"], result["problems"]), (1, []))
        self.assertIsNone(tenant_store.resolve_session(restored / "v2.db", token))
        self.assertEqual(len(UserWorkspace(restored / "v2.db", self.a.user_id).list_facts()), len(FACTS))
        self.assertEqual(recorded_data_format(restored), 2)
        with self.assertRaises(v2_backup.BackupError):
            v2_backup.restore_backup(archive, restored, live_ledger=self.ledger)  # never over existing data

    def test_an_account_deleted_after_the_backup_stays_deleted_after_restoring_it(self):
        archive = self.root / "backups" / "before-delete.tar.gz"
        v2_backup.create_backup(self.data, archive)
        delete_account(self.database, self.a.user_id, self.ledger)
        restored = self.root / "restored"
        result = v2_backup.restore_backup(archive, restored, live_ledger=self.ledger)
        self.assertEqual(result["deleted_again"], 1)
        with self.assertRaises(tenant_store.NotFound):
            UserWorkspace(restored / "v2.db", self.a.user_id).list_facts()
        self.assertEqual(len(UserWorkspace(restored / "v2.db", self.b.user_id).list_facts()), 1)
        self.assertEqual(len(read_deletions(restored / tenant_store.DELETION_LEDGER)), 1)
        set_setting(restored / "v2.db", "registration_open", "1")
        with self.assertRaises(tenant_store.Refused):
            tenant_store.sign_in(restored / "v2.db", issuer=ISSUER, subject="a")

    def test_a_tampered_archive_is_refused_whole(self):
        archive = self.root / "b.tar.gz"
        v2_backup.create_backup(self.data, archive)
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0xFF
        broken = self.root / "broken.tar.gz"
        broken.write_bytes(bytes(data))
        with self.assertRaises(v2_backup.BackupError):
            v2_backup.restore_backup(broken, self.root / "restored", live_ledger=None)
        self.assertFalse((self.root / "restored").exists())
        self.assertEqual(v2_backup.inspect_archive(archive)["problems"], [])

    def test_a_live_ledger_that_is_missing_or_unreadable_refuses_the_restore(self):
        archive = self.root / "before-delete.tar.gz"
        v2_backup.create_backup(self.data, archive)
        delete_account(self.database, self.a.user_id, self.ledger)
        garbled = self.root / "garbled.jsonl"
        garbled.write_text("not a ledger line\n", encoding="utf-8")
        restored = self.root / "restored"
        for given in (self.root / "typo.jsonl", self.root, garbled):
            with self.assertRaises(v2_backup.BackupError):
                v2_backup.restore_backup(archive, restored, live_ledger=given)
            self.assertFalse(restored.exists())
            self.assertEqual([path.name for path in self.root.iterdir() if path.name.startswith(".feiran-v2-restore-")], [])
            with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as said:
                code = v2_backup.main(["restore", "--archive", str(archive), "--into", str(restored), "--ledger", str(given)])
            self.assertEqual(code, 2)
            self.assertIn("refused", said.getvalue())
        # Only the mode named for a lost ledger restores without it, and then the later deletion is not applied.
        result = v2_backup.restore_backup(archive, restored, live_ledger=None)
        self.assertEqual((result["deleted_again"], result["problems"]), (0, []))
        self.assertEqual(len(UserWorkspace(restored / "v2.db", self.a.user_id).list_facts()), len(FACTS))
        self.assertTrue((restored / tenant_store.DELETION_LEDGER).is_file())
        self.assertEqual(v2_backup.verify_data(restored)["problems"], [])

    def test_the_ledger_is_rebuilt_from_the_database_when_it_goes_missing(self):
        delete_account(self.database, self.a.user_id)
        self.ledger.unlink()
        self.assertIn("the deletion ledger is missing", v2_backup.verify_data(self.data)["problems"])
        cognito = FakeCognito()
        settings = OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, domain=DOMAIN, public_origin=ORIGIN)
        create_v2_app(database=self.database, identity=Identity(settings, self.database, post=cognito.post,
                                                                 keys=cognito.keys),
                      settings=V2Settings(public_host="feiran.example", public_origin=ORIGIN), starter=[])
        self.assertEqual([record["user_id"] for record in read_deletions(self.ledger)], [self.a.user_id])
        self.assertEqual(tenant_store.ensure_ledger(self.database, self.ledger), 0)  # never listed twice
        self.assertEqual(v2_backup.verify_data(self.data)["problems"], [])


class tarfile_names:
    def __init__(self, archive):
        import tarfile
        self.tar = tarfile.open(archive, "r:gz")

    def __enter__(self):
        return self.tar.getnames()

    def __exit__(self, *exc):
        self.tar.close()


if __name__ == "__main__":
    unittest.main()
