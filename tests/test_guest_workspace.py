"""Guest workspaces end to end with synthetic data: the switch and its bounded limits, the
one-click start (Origin, start token, double clicks), isolation between guests and from a
signed-in user, the routes and work a guest is refused (no model call, upload or page read),
the real edit, fit, approve and print flow, and the end of a workspace: expiry, removal of
everything it held, restarts, late results, backups and the operator's commands. The model,
printer and job boards are scripted stand-ins; nothing leaves the machine."""
import contextlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from feiran import guest_sample, tenant_store, v2_admin, v2_backup
from feiran.identity import Identity, OIDCSettings
from feiran.tenant_store import (GUEST_ISSUER, LateResult, NotFound, Refused, StoreError, UserWorkspace, claim_tasks,
                          initialize_store, provision_user, set_setting)
from tests import test_web_v2
from tests.fake_identity import CLIENT_ID, DOMAIN, ISSUER, ORIGIN
from tests.test_cv_fit import RowPrinter
from feiran.v2_flow import make_handlers
from feiran.web_v2 import V2Settings, create_v2_app

START_COOKIE = "__Host-feiran-guest-start"
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
NODE = shutil.which("node")


class Clock:
    """The store's clock (tenant_store._clock), starting now and moved on by the test."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def rows_naming(database, value):
    """Every (table, column) whose rows still hold ``value``: the whole database is searched, so
    a table added later that keeps a guest's ID is found too."""
    found = []
    connection = sqlite3.connect(database)
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            for column in [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]:
                count = connection.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" = ?', (value,)).fetchone()[0]
                if count:
                    found.append((table, column))
    finally:
        connection.close()
    return found


def guest_ids(database):
    with tenant_store.transaction(database) as connection:
        return [row[0] for row in connection.execute("SELECT user_id FROM guest_workspaces ORDER BY created_at")]


class GuestCase(test_web_v2.V2Case):
    """The V2 app with guest workspaces switched on, a controllable clock and the row-counting
    stand-in printer (so the sample can be made to need two pages)."""

    def setUp(self):
        self.clock = Clock()
        clock = patch.object(tenant_store, "_clock", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        with patch.object(test_web_v2, "FakePrinter", RowPrinter):
            super().setUp()
        set_setting(self.database, "guest_enabled", "1")

    def new_app(self):
        """Another process on the same data folder: a restart."""
        settings = OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, domain=DOMAIN, public_origin=ORIGIN,
                                client_secret="client-secret")
        app = create_v2_app(database=self.database, identity=Identity(settings, self.database, post=self.cognito.post,
                                                                      keys=self.cognito.keys),
                            settings=V2Settings(public_host=test_web_v2.HOST, public_origin=ORIGIN),
                            handlers=make_handlers(printer=self.printer, posting=self.read_posting, page=self.read_page),
                            model=self.chat, boards=self.boards, starter=[], ledger=self.root / "ledger.jsonl")
        return app

    def welcome(self, client):
        page = client.get("/welcome")
        self.assertEqual(page.status_code, 200, page.text)
        return re.search(r'name="guest-start" content="([^"]+)"', page.text).group(1)

    def guest(self, app=None):
        """A new browser with a guest workspace of its own; returns (client, headers, job_id)."""
        client = TestClient(app or self.app, base_url=ORIGIN)
        start = self.welcome(client)
        response = client.post("/guest/start", headers={"X-Guest-Start": start, "Origin": ORIGIN})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["resumed"])
        page = client.get("/")
        self.assertIn('name="workbench-guest"', page.text)
        headers = {"X-Workbench-Token": re.search(r'name="workbench-token" content="([^"]+)"', page.text).group(1)}
        jobs = client.get("/api/jobs", headers=headers).json()["jobs"]
        self.assertEqual(len(jobs), 1)
        return client, headers, jobs[0]["job_id"]

    def cv(self, client, headers, job_id):
        return client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]

    def two_pages(self, client, headers, job_id):
        """Make the stand-in printer find the CV one line too long for one page."""
        cv = self.cv(client, headers, job_id)
        html = client.get(f"/preview/{job_id}/en", params={"v": cv["content_sha256"]}, headers=headers).text
        self.printer.rows = 1
        self.printer.rows = self.printer.count(html) - 1

    def printed(self, client, headers, job_id):
        """Approve the CV as shown and print it; returns the export task."""
        cv = self.cv(client, headers, job_id)
        approved = client.post(f"/api/jobs/{job_id}/cv/en/approve", headers=headers,
                               json={"expected_content_sha256": cv["content_sha256"]})
        self.assertEqual(approved.status_code, 200, approved.text)
        return self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers))


class SwitchTests(GuestCase):
    def test_off_unless_switched_on_and_every_limit_stays_bounded(self):
        set_setting(self.database, "guest_enabled", "0")
        client = TestClient(self.app, base_url=ORIGIN)
        self.assertEqual(client.get("/", follow_redirects=False).headers["location"], "/login?return_to=/")
        self.assertEqual(client.get("/welcome", follow_redirects=False).headers["location"], "/")
        refused = client.post("/guest/start", headers={"Origin": ORIGIN, "X-Guest-Start": "x" * 40})
        self.assertEqual((refused.status_code, refused.json()["code"]), (404, "guest_disabled"))
        with self.assertRaises(Refused):
            tenant_store.start_guest(self.database, start="y" * 40)
        self.assertEqual(guest_ids(self.database), [])
        fresh = Path(tempfile.mkdtemp(dir=self.root)) / "v2.db"
        initialize_store(fresh)
        self.assertEqual(tenant_store.guest_settings(fresh), {"enabled": False, "max_active": 10, "starts_per_hour": 20,
                                                             "lifetime_minutes": 120, "max_tasks": 20})
        for key, bad in [("guest_enabled", "yes"), ("guest_max_active", "0"), ("guest_max_active", "101"),
                         ("guest_starts_per_hour", "1000"), ("guest_lifetime_minutes", "5"),
                         ("guest_lifetime_minutes", "100000"), ("guest_max_tasks", "-1"), ("guest_max_tasks", "２")]:
            with self.assertRaises(StoreError, msg=(key, bad)):
                set_setting(self.database, key, bad)
        # A value written past the checks is still read within its bounds.
        with tenant_store.transaction(self.database, write=True) as connection:
            connection.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                                   [("guest_max_active", "100000"), ("guest_lifetime_minutes", "1"),
                                    ("guest_starts_per_hour", "junk"), ("guest_max_tasks", "0")])
        self.assertEqual(tenant_store.guest_settings(self.database),
                         {"enabled": False, "max_active": 100, "starts_per_hour": 20, "lifetime_minutes": 10,
                          "max_tasks": 20})

    def test_a_guest_identity_can_never_sign_in_or_be_provisioned(self):
        with self.assertRaises(StoreError):
            provision_user(self.database, issuer=GUEST_ISSUER, subject="guest-anything")
        with self.assertRaises(Refused):
            tenant_store.sign_in(self.database, issuer=GUEST_ISSUER, subject="guest-anything")
        with self.assertRaises(Exception):  # an identity provider must be https
            OIDCSettings(issuer=GUEST_ISSUER, client_id=CLIENT_ID, domain=DOMAIN, public_origin=ORIGIN)
        _client, _headers, _job = self.guest()
        user_id = guest_ids(self.database)[0]
        with self.assertRaises(NotFound):  # a signed-in user's session is never made for a guest
            tenant_store.create_session(self.database, user_id, lifetime=timedelta(hours=1), idle=timedelta(hours=1))


class StartTests(GuestCase):
    def test_a_start_needs_this_sites_origin_and_the_welcome_pages_token(self):
        client = TestClient(self.app, base_url=ORIGIN)
        start = self.welcome(client)
        self.assertEqual(client.cookies.get(START_COOKIE), start)
        for headers in [{"X-Guest-Start": start}, {"X-Guest-Start": start, "Origin": "https://evil.example"},
                        {"X-Guest-Start": start, "Origin": "null"}, {"Origin": ORIGIN},
                        {"X-Guest-Start": start[:-1] + ("A" if start[-1] != "A" else "B"), "Origin": ORIGIN}]:
            response = client.post("/guest/start", headers=headers)
            self.assertEqual((response.status_code, response.json()["code"]), (403, "guest_start_refused"), headers)
        # Without the cookie (another site's form, or a token copied out of the page) it is refused too.
        other = TestClient(self.app, base_url=ORIGIN)
        response = other.post("/guest/start", headers={"X-Guest-Start": start, "Origin": ORIGIN})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(guest_ids(self.database), [])
        # Opening the welcome page starts nothing.
        for _ in range(3):
            self.welcome(TestClient(self.app, base_url=ORIGIN))
        self.assertEqual(guest_ids(self.database), [])

    def test_a_double_click_or_a_resend_opens_the_one_workspace_it_made(self):
        client = TestClient(self.app, base_url=ORIGIN)
        start = self.welcome(client)
        headers = {"X-Guest-Start": start, "Origin": ORIGIN}
        # Two clicks in flight at once, both before either answer set the session cookie.
        browsers = [TestClient(self.app, base_url=ORIGIN) for _ in range(4)]
        for browser in browsers:
            browser.cookies.set(START_COOKIE, start, domain=test_web_v2.HOST)
        answers = []
        threads = [threading.Thread(target=lambda b=browser: answers.append(b.post("/guest/start", headers=headers)))
                   for browser in browsers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(answer.status_code for answer in answers), [200] * 4)
        self.assertEqual(sorted(answer.json()["resumed"] for answer in answers), [False, True, True, True])
        self.assertEqual(len(guest_ids(self.database)), 1)
        jobs = {browser.get("/api/jobs").json()["jobs"][0]["job_id"] for browser in browsers}
        self.assertEqual(len(jobs), 1)  # one workspace, one sample
        # Sessions of one workspace stay few, however often the start is resent.
        for _ in range(6):
            again = TestClient(self.app, base_url=ORIGIN)
            again.cookies.set(START_COOKIE, start, domain=test_web_v2.HOST)
            self.assertEqual(again.post("/guest/start", headers=headers).status_code, 200)
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM guest_sessions").fetchone()[0],
                             tenant_store.GUEST_SESSIONS)
        self.assertEqual(len(guest_ids(self.database)), 1)
        # The browser that is in its workspace already is sent back to it.
        # (the welcome page it reopens says so, and hands it a new start token).
        # The newest sessions are kept (the oldest went); the last browser is still in.
        page = again.get("/welcome").text
        self.assertIn('content="guest"', page)
        start = re.search(r'name="guest-start" content="([^"]+)"', page).group(1)
        resumed = again.post("/guest/start", headers={"X-Guest-Start": start, "Origin": ORIGIN})
        self.assertTrue(resumed.json()["resumed"])
        self.assertEqual(len(guest_ids(self.database)), 1)

    def test_starts_are_capped_per_hour_and_by_open_workspaces_across_restarts(self):
        set_setting(self.database, "guest_max_active", "2")
        set_setting(self.database, "guest_starts_per_hour", "3")
        first, first_headers, _job = self.guest()
        self.guest()
        client = TestClient(self.app, base_url=ORIGIN)
        full = client.post("/guest/start", headers={"X-Guest-Start": self.welcome(client), "Origin": ORIGIN})
        self.assertEqual((full.status_code, full.json()["code"]), (503, "guest_full"))
        self.assertEqual(len(guest_ids(self.database)), 2)  # a refusal writes nothing
        self.assertEqual(first.post("/guest/end", headers=first_headers).json()["ended"], True)
        self.guest()  # the third start of the hour
        # Ending a workspace early never gives its start back, and a restart forgets nothing.
        self.assertEqual(first.post("/guest/end", headers=first_headers).status_code, 401)
        tenant_store.expire_guests(self.database, everything=True)
        restarted = self.new_app()
        with TestClient(restarted, base_url=ORIGIN) as client:
            limited = client.post("/guest/start", headers={"X-Guest-Start": self.welcome(client), "Origin": ORIGIN})
            self.assertEqual((limited.status_code, limited.json()["code"]), (429, "guest_rate_limited"))
            self.assertEqual(limited.headers["Retry-After"], "60")
            self.clock.advance(minutes=61)
            self.guest(restarted)
        # The limit counts the whole site: X-Forwarded-For names no one here.
        set_setting(self.database, "guest_starts_per_hour", "1")
        client = TestClient(self.app, base_url=ORIGIN)
        spoofed = client.post("/guest/start", headers={"X-Guest-Start": self.welcome(client), "Origin": ORIGIN,
                                                       "X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(spoofed.json()["code"], "guest_rate_limited")

    def test_a_start_that_fails_halfway_leaves_nothing_and_no_session(self):
        client = TestClient(self.app, base_url=ORIGIN, raise_server_exceptions=False)
        start = self.welcome(client)
        with patch.object(guest_sample, "seed", side_effect=RuntimeError("synthetic failure")):
            failed = client.post("/guest/start", headers={"X-Guest-Start": start, "Origin": ORIGIN})
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(guest_ids(self.database), [])
        self.assertEqual(client.get("/api/me").status_code, 401)
        # A workspace a crash left unready is never opened and goes after GUEST_SEED_WAIT.
        user_id, made = tenant_store.start_guest(self.database, start="z" * 40)
        self.assertTrue(made)
        with self.assertRaises(NotFound):
            tenant_store.create_guest_session(self.database, user_id)
        with self.assertRaises(Refused):
            tenant_store.start_guest(self.database, start="z" * 40)
        self.assertEqual(tenant_store.expire_guests(self.database), 0)
        self.clock.advance(minutes=6)
        self.assertEqual(tenant_store.expire_guests(self.database), 1)
        self.assertEqual(rows_naming(self.database, user_id), [])


class SignedInTests(GuestCase):
    def test_a_signed_in_user_is_never_replaced_by_a_guest_and_works_as_before(self):
        anonymous = TestClient(self.app, base_url=ORIGIN)
        self.assertEqual(anonymous.get("/", follow_redirects=False).headers["location"], "/welcome")
        self.assertEqual(anonymous.get("/api/me").status_code, 401)
        login = anonymous.get("/login", params={"return_to": "/#jobs"}, follow_redirects=False)
        self.assertEqual(login.status_code, 303)
        client, headers = self.ready_user("alice")
        start = self.welcome(TestClient(self.app, base_url=ORIGIN))
        self.assertEqual(client.get("/welcome", follow_redirects=False).headers["location"], "/")
        client.cookies.set(START_COOKIE, start, domain=test_web_v2.HOST)
        response = client.post("/guest/start", headers={"X-Guest-Start": start, "Origin": ORIGIN})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "signed_in"))
        self.assertEqual(guest_ids(self.database), [])
        self.assertEqual(client.post("/guest/end", headers=headers).json()["code"], "not_guest")
        me = client.get("/api/me", headers=headers).json()
        self.assertNotIn("guest", me)
        self.assertIsNotNone(me["usage"])
        page = client.get("/").text
        self.assertNotIn("workbench-guest", page)
        job_id = self.new_job(client, headers)  # model work goes on as before
        self.assertTrue(self.chat.sent)
        self.assertEqual(client.get(f"/api/jobs/{job_id}", headers=headers).status_code, 200)
        self.assertNotIn(GUEST_ISSUER, json.dumps(tenant_store.list_accounts(self.database)))

    def test_signing_in_from_a_guest_workspace_ends_it_and_opens_the_account(self):
        client, _headers, _job = self.guest()
        user_id = guest_ids(self.database)[0]
        started = client.get("/login", params={"return_to": "/"}, follow_redirects=False)
        callback = client.get("/auth/callback", params=self.cognito.authorize(started.headers["location"], "bob"),
                              follow_redirects=False)
        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertEqual(guest_ids(self.database), [])
        self.assertEqual(rows_naming(self.database, user_id), [])
        page = client.get("/").text
        self.assertNotIn("workbench-guest", page)
        self.assertEqual(client.get("/api/jobs").json()["jobs"], [])  # the sample is not carried over
        self.assertEqual([account["subject"] for account in tenant_store.list_accounts(self.database)], ["bob"])


class IsolationTests(GuestCase):
    def test_two_guests_and_a_signed_in_user_never_reach_each_others_work(self):
        alice, alice_headers = self.ready_user("alice")
        alice_job = self.new_job(alice, alice_headers)
        a, a_headers, a_job = self.guest()
        b, b_headers, b_job = self.guest()
        self.assertNotEqual(a_job, b_job)
        self.assertEqual(self.printed(a, a_headers, a_job)["status"], "succeeded")
        a_task = a.get(f"/api/jobs/{a_job}", headers=a_headers).json()
        a_tasks = [row["task_id"] for row in self.tasks(guest_ids(self.database)[0])]
        line = self.cv(b, b_headers, b_job)["lines"][0]
        edited = b.post(f"/api/jobs/{b_job}/cv/en/lines/{line['block']}", headers=b_headers,
                        json={"text": "Guest B's own line.", "expected_text": line["text"]})
        self.assertEqual(edited.status_code, 200, edited.text)
        for client, headers, own, others in [(a, a_headers, a_job, [b_job, alice_job]),
                                             (b, b_headers, b_job, [a_job, alice_job]),
                                             (alice, alice_headers, alice_job, [a_job, b_job])]:
            self.assertEqual([job["job_id"] for job in client.get("/api/jobs", headers=headers).json()["jobs"]], [own])
            for other in others:
                for path in [f"/api/jobs/{other}", f"/preview/{other}/en", f"/download/{other}/en.pdf"]:
                    self.assertEqual(client.get(path, headers=headers).status_code, 404, path)
                for path, body in [(f"/api/jobs/{other}/cv/en/lines/b1", {"text": "x", "expected_text": "y"}),
                                   (f"/api/jobs/{other}/cv/en/approve", {"expected_content_sha256": "0" * 64}),
                                   (f"/api/jobs/{other}/cv/en/export", None), (f"/api/jobs/{other}/cv/en/fit", None)]:
                    self.assertIn(client.post(path, headers=headers, json=body).status_code, (404, 409), path)
            for task_id in a_tasks if client is not a else []:
                self.assertEqual(client.get(f"/api/tasks/{task_id}", headers=headers).status_code, 404)
                self.assertEqual(client.post(f"/api/tasks/{task_id}/cancel", headers=headers).status_code, 404)
        self.assertEqual(a.get(f"/download/{a_job}/en.pdf", headers=a_headers).status_code, 200)
        self.assertNotIn("Guest B's own line.", json.dumps(a_task))
        self.assertEqual(self.cv(b, b_headers, b_job)["lines"][0]["text"], "Guest B's own line.")

    def tasks(self, user_id):
        with tenant_store.transaction(self.database) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM tasks WHERE user_id = ?", (user_id,))]

    def test_a_guests_writes_need_its_csrf_token_and_this_sites_origin(self):
        client, headers, job_id = self.guest()
        line = self.cv(client, headers, job_id)["lines"][0]
        path, body = f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", {"text": "Edited.", "expected_text": line["text"]}
        for bad in [{}, {"X-Workbench-Token": "wrong" * 8}, {**headers, "Origin": "https://evil.example"}]:
            self.assertEqual(client.post(path, headers=bad, json=body).status_code, 403, bad)
        self.assertEqual(client.post("/guest/end", headers={}).status_code, 403)
        self.assertEqual(self.cv(client, headers, job_id)["lines"][0]["text"], line["text"])
        self.assertEqual(client.post(path, headers={**headers, "Origin": ORIGIN}, json=body).status_code, 200)


class GuestAbilityTests(GuestCase):
    def blocked_requests(self, job_id):
        return [("POST", "/api/cv/upload?language=en", None), ("GET", "/api/cv/uploads/u1", None),
                ("POST", "/api/cv/uploads/u1/save", {}), ("POST", "/api/facts/f1/edit", {"text": "x"}),
                ("POST", "/api/facts/confirm", {"refs": []}), ("GET", "/api/sources", None),
                ("POST", "/api/sources", {"provider": "greenhouse", "board": "mongodb"}),
                ("POST", "/api/sources/greenhouse/mongodb/refresh", None), ("GET", "/api/listings", None),
                ("POST", "/api/listings/start", {}), ("POST", "/api/jobs", {"title": "t", "text": "Requirements:\n- x"}),
                ("POST", "/api/jobs/from-url", {"url": "https://boards.greenhouse.io/x/jobs/1"}),
                ("POST", f"/api/jobs/{job_id}/sponsorship", {}), ("POST", f"/api/jobs/{job_id}/requirements/add", {}),
                ("POST", f"/api/jobs/{job_id}/requirements/find", {}),
                ("POST", f"/api/jobs/{job_id}/requirements/decide", {}),
                ("POST", f"/api/jobs/{job_id}/cv/en/prepare", None), ("POST", f"/api/jobs/{job_id}/cv/en/tailor", None),
                ("POST", f"/api/jobs/{job_id}/cv/en/plan", None), ("POST", f"/api/jobs/{job_id}/gaps", {"language": "en"}),
                ("POST", f"/api/jobs/{job_id}/gaps/r1/suggest", {}), ("POST", f"/api/jobs/{job_id}/gaps/r1/accept", {}),
                ("POST", f"/api/jobs/{job_id}/gaps/r1/write", {}), ("POST", f"/api/jobs/{job_id}/gaps/r1/decline", {}),
                ("POST", f"/api/jobs/{job_id}/cv/en/lines/b1/save", {}),
                ("POST", "/api/consent", {"stage": "job_processing", "version": 1}),
                ("POST", "/api/account/delete", {}), ("PUT", f"/api/jobs/{job_id}", {}), ("HEAD", "/api/sources", None)]

    def test_every_model_upload_and_page_read_is_refused_before_it_runs(self):
        client, headers, job_id = self.guest()
        before = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        user_id = guest_ids(self.database)[0]
        for method, path, body in self.blocked_requests(job_id):
            response = client.request(method, path, headers=headers, json=body)
            if method in ("PUT", "HEAD"):  # no such route: the router answers before anything runs
                self.assertEqual(response.status_code, 405)
                continue
            self.assertEqual(response.status_code, 403, (method, path, response.text))
            self.assertEqual(response.json()["code"], "guest_not_allowed", path)
        upload = client.post("/api/cv/upload?language=en", content=test_web_v2.pdf_of([("Synthetic", "")]),
                             headers={**headers, "Content-Type": "application/pdf"})
        self.assertEqual(upload.status_code, 403)
        self.assertEqual((self.chat.sent, self.boards.calls), ([], []))  # no model call, no board read
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user_id,)).fetchone()[0], 1)
        self.assertEqual(client.get(f"/api/jobs/{job_id}", headers=headers).json(), before)
        me = client.get("/api/me", headers=headers).json()
        self.assertEqual((me["usage"], me["consents"], me["notices"]), (None, {}, {}))
        self.assertEqual((me["guest"]["lifetime_minutes"], me["guest"]["task_limit"]), (120, 20))

    def test_the_store_refuses_a_guest_any_model_work_whatever_reaches_it(self):
        _client, _headers, job_id = self.guest()
        user_id = guest_ids(self.database)[0]
        user = UserWorkspace(self.database, user_id)
        for operation, heavy in [("tailor_cv", "model"), ("prepare_cv", "model"), ("export_pdf", "model"),
                                 ("find_requirements", "pdf"), ("read_page", "pdf")]:
            with self.assertRaises(Refused, msg=operation) as refused:
                user.submit_task(operation, key=operation, request={"job_id": job_id}, units=1, heavy=heavy, job_id=job_id)
            self.assertEqual(refused.exception.code, "guest_not_allowed")
        # A model task written into the queue some other way is ended, never started.
        self.app.state.runner.stop(5)
        task = user.submit_task("export_pdf", key="forged", request={"job_id": job_id}, units=0, heavy="pdf", job_id=job_id)
        with tenant_store.transaction(self.database, write=True) as connection:
            connection.execute("UPDATE tasks SET operation = 'tailor_cv', heavy = 'model', units = 1 WHERE task_id = ?",
                               (task["task_id"],))
        self.assertEqual(claim_tasks(self.database, total=2, pdf=1, deadline=timedelta(minutes=5)), [])
        ended = user.get_task(task["task_id"])
        self.assertEqual((ended["status"], ended["error_code"]), ("failed", "guest_not_allowed"))
        # And a running guest task can never start or mark a model call.
        task = user.submit_task("export_pdf", key="real", request={"job_id": job_id}, units=0, heavy="pdf", job_id=job_id)
        claimed = claim_tasks(self.database, total=2, pdf=1, deadline=timedelta(minutes=5))
        self.assertEqual([item["task_id"] for item in claimed], [task["task_id"]])
        with self.assertRaises(Refused):
            tenant_store.begin_call(self.database, user_id, task["task_id"], claimed[0]["token"], prompt_tokens=10,
                                    max_output=10)
        with self.assertRaises(StoreError):
            tenant_store.mark_spending(self.database, user_id, task["task_id"], claimed[0]["token"], "tailor")
        self.assertEqual(self.chat.sent, [])

    def test_the_sample_is_edited_fitted_undone_approved_and_printed_with_the_real_flow(self):
        client, headers, job_id = self.guest()
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        self.assertIn("synthetic", job["jd"]["source"].lower())
        cv = job["cv"]["en"]
        messages = [stage["message"] for stage in cv["stages"]]
        self.assertEqual([stage["status"] for stage in cv["stages"]], ["done", "skipped", "skipped"])
        self.assertTrue(messages[1].startswith("AI rewording not used:"), messages)
        self.assertTrue(messages[2].startswith("AI adjustment for this job not used:"), messages)
        self.assertGreaterEqual(len(cv["lines"]), 20)  # long enough to need fitting on a real page
        line = next(item for item in cv["lines"] if item["section"] == "experience")
        edited = client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                             json={"text": "Built a synthetic REST API in Python.", "expected_text": line["text"]})
        self.assertEqual(edited.status_code, 200, edited.text)
        edit = next(item for item in self.cv(client, headers, job_id)["edits"] if item["block"] == line["block"])
        undone = client.post(f"/api/jobs/{job_id}/cv/en/edits/{edit['id']}", headers=headers, json={"undone": True})
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(test_web_v2_rows(self.cv(client, headers, job_id))[line["block"]], line["text"])
        redone = client.post(f"/api/jobs/{job_id}/cv/en/edits/{edit['id']}", headers=headers, json={"undone": False})
        self.assertEqual(redone.status_code, 200, redone.text)
        self.two_pages(client, headers, job_id)
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers))
        self.assertEqual((task["status"], task["result"]["fit"]), ("succeeded", "fitted"), task)
        fitted = self.cv(client, headers, job_id)
        self.assertEqual(fitted["fit"]["pages"], 1)
        self.assertEqual(test_web_v2_rows(fitted)[line["block"]], "Built a synthetic REST API in Python.")
        cut = next(change for change in fitted["changes"] if change.get("fit"))
        put_back = client.post(f"/api/jobs/{job_id}/cv/en/change", headers=headers, json={"change_id": cut["id"], "undone": True})
        self.assertEqual(put_back.status_code, 200, put_back.text)
        cut_again = client.post(f"/api/jobs/{job_id}/cv/en/change", headers=headers, json={"change_id": cut["id"], "undone": False})
        self.assertEqual(cut_again.status_code, 200, cut_again.text)
        self.assertEqual(self.printed(client, headers, job_id)["status"], "succeeded")
        pdf = client.get(f"/download/{job_id}/en.pdf", headers=headers)
        self.assertEqual((pdf.status_code, pdf.content[:5]), (200, b"%PDF-"))
        self.assertIn("attachment", pdf.headers["content-disposition"])
        guest = client.get("/api/me", headers=headers).json()["guest"]
        self.assertEqual(guest["tasks_used"], 2)
        self.assertEqual(self.chat.sent, [])
        # A reload finds it all as it was.
        self.assertEqual(self.cv(client, headers, job_id)["final_pdf"], True)

    def test_a_guest_makes_a_bounded_number_of_runs_and_versions(self):
        set_setting(self.database, "guest_max_tasks", "2")
        client, headers, job_id = self.guest()
        self.assertEqual(self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers))["status"],
                         "succeeded")
        self.assertEqual(self.printed(client, headers, job_id)["status"], "succeeded")
        line = self.cv(client, headers, job_id)["lines"][0]
        client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                    json={"text": "Changed once more.", "expected_text": line["text"]})
        refused = client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers)
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "guest_task_limit"))
        with patch.object(tenant_store, "GUEST_MAX_VERSIONS", len(self.versions())):
            line = self.cv(client, headers, job_id)["lines"][0]
            refused = client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                                  json={"text": "One version too many.", "expected_text": line["text"]})
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "guest_version_limit"))
        self.assertEqual(self.cv(client, headers, job_id)["lines"][0]["text"], line["text"])

    def versions(self):
        with tenant_store.transaction(self.database) as connection:
            return connection.execute("SELECT material_id FROM materials").fetchall()


def test_web_v2_rows(view):
    return {line["block"]: line["text"] for line in view["lines"]}


class CapacityTests(unittest.TestCase):
    """Guests' work waits behind signed-in users' and holds little of the queue and runner."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "v2.db"
        initialize_store(self.database)
        for key, value in [("user_daily_units", "50"), ("site_daily_units", "500"), ("guest_enabled", "1"),
                           ("queue_limit", "20")]:
            set_setting(self.database, key, value)
        self.users = [UserWorkspace(self.database, provision_user(self.database, issuer="https://issuer", subject=str(n)))
                      for n in range(2)]
        self.guests = []
        for n in range(4):
            user_id, _made = tenant_store.start_guest(self.database, start=f"start-token-{n:02d}-" + "x" * 20)
            tenant_store.guest_ready(self.database, user_id)
            self.guests.append(UserWorkspace(self.database, user_id))

    def test_guests_queue_and_run_little_and_never_ahead_of_signed_in_users(self):
        for guest in self.guests[:2]:
            guest.submit_task("export_pdf", key="g", request={"g": 1}, units=3, heavy="pdf")
        with self.assertRaises(Refused) as refused:
            self.guests[2].submit_task("export_pdf", key="g", request={"g": 1}, units=0, heavy="pdf")
        self.assertEqual(refused.exception.code, "queue_full")
        for user in self.users:
            user.submit_task("model_work", key="u", request={"u": 1}, units=1, heavy="model")
        claimed = claim_tasks(self.database, total=4, pdf=4, deadline=timedelta(minutes=5))
        self.assertEqual([item["guest"] for item in claimed], [False, False, True])  # users first, one guest
        self.assertEqual(claim_tasks(self.database, total=4, pdf=4, deadline=timedelta(minutes=5)), [])
        # The runner's count of guest handlers still running holds the slot too.
        tenant_store.finish_task(self.database, claimed[2]["user_id"], claimed[2]["task_id"], claimed[2]["token"],
                                 "succeeded")
        self.assertEqual(claim_tasks(self.database, total=4, pdf=4, deadline=timedelta(minutes=5), guests_running=1), [])
        self.assertEqual(len(claim_tasks(self.database, total=4, pdf=4, deadline=timedelta(minutes=5))), 1)
        # No units of the model allowance are charged to a guest's print.
        with tenant_store.transaction(self.database) as connection:
            units = connection.execute("SELECT units FROM tasks t JOIN guest_workspaces g USING (user_id)").fetchall()
        self.assertEqual({row[0] for row in units}, {0})

    def test_a_small_queue_holds_no_guest_work_at_all(self):
        set_setting(self.database, "queue_limit", "1")
        with self.assertRaises(Refused):
            self.guests[0].submit_task("export_pdf", key="g", request={"g": 1}, units=0, heavy="pdf")
        self.users[0].submit_task("model_work", key="u", request={"u": 1}, units=1, heavy="model")


class EndTests(GuestCase):
    def test_an_ended_workspace_loses_its_session_and_everything_in_it_others_stay(self):
        alice, alice_headers = self.ready_user("alice")
        alice_job = self.new_job(alice, alice_headers)
        a, a_headers, a_job = self.guest()
        self.assertEqual(self.printed(a, a_headers, a_job)["status"], "succeeded")
        self.clock.advance(minutes=30)
        b, b_headers, b_job = self.guest()
        a_id, b_id = guest_ids(self.database)
        alice_rows = rows_naming(self.database, tenant_store.list_accounts(self.database)[0]["user_id"])
        self.clock.advance(minutes=91)  # a's fixed two hours are up; b has half an hour left
        self.assertEqual(a.get("/api/me", headers=a_headers).status_code, 401)
        self.assertEqual(a.get(f"/download/{a_job}/en.pdf", headers=a_headers).status_code, 401)
        with self.assertRaises(NotFound):  # even before it is removed
            UserWorkspace(self.database, a_id).list_jobs()
        self.assertTrue(self.app.state.runner.drain(5))
        self.assertTrue(self.wait_for(lambda: guest_ids(self.database) == [b_id]))  # the runner's loop removes it
        self.assertEqual(rows_naming(self.database, a_id), [])
        self.assertEqual(rows_naming(self.database, a_job), [])
        self.assertEqual(self.cv(b, b_headers, b_job)["head"], "draft")
        self.assertEqual(rows_naming(self.database, tenant_store.list_accounts(self.database)[0]["user_id"]), alice_rows)
        alice_id = tenant_store.list_accounts(self.database)[0]["user_id"]
        self.assertEqual(UserWorkspace(self.database, alice_id).get_job(alice_job)["job_id"], alice_job)
        self.assertFalse((self.root / "ledger.jsonl").exists() and (self.root / "ledger.jsonl").read_text())
        # Nothing extends a guest's time: using it right up to the end changes nothing.
        self.clock.advance(minutes=28)
        self.assertEqual(b.get("/api/me", headers=b_headers).status_code, 200)
        self.clock.advance(minutes=2)
        self.assertEqual(b.get("/api/me", headers=b_headers).status_code, 401)
        self.assertEqual(b.get("/", follow_redirects=False).headers["location"], "/welcome")

    def wait_for(self, condition, seconds=5):
        import time
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def test_ending_by_choice_or_logout_removes_the_workspace_at_once(self):
        a, a_headers, _job = self.guest()
        (a_id,) = guest_ids(self.database)
        b, b_headers, _job = self.guest()
        (b_id,) = set(guest_ids(self.database)) - {a_id}
        ended = a.post("/guest/end", headers=a_headers)
        self.assertEqual(ended.json(), {"ended": True, "logout_url": "/welcome?ended=1"})
        self.assertEqual(rows_naming(self.database, a_id), [])
        out = b.post("/logout", headers=b_headers).json()
        self.assertEqual(out["logout_url"], "/welcome?ended=1")  # never Cognito's sign-out
        self.assertEqual(rows_naming(self.database, b_id), [])
        self.assertEqual(self.cognito.requests, [])

    def test_a_restart_removes_workspaces_that_ended_while_it_was_stopped(self):
        _client, _headers, _job = self.guest()
        user_id = guest_ids(self.database)[0]
        self.app.state.runner.stop(5)
        self.clock.advance(hours=3)
        self.assertEqual(guest_ids(self.database), [user_id])
        with TestClient(self.new_app(), base_url=ORIGIN):
            self.assertEqual(guest_ids(self.database), [])
        self.assertEqual(rows_naming(self.database, user_id), [])

    def test_work_of_an_ended_workspace_is_cancelled_and_its_late_result_refused(self):
        self.app.state.runner.stop(5)
        client, headers, job_id = self.guest()
        user_id = guest_ids(self.database)[0]
        user = UserWorkspace(self.database, user_id)
        queued = client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers).json()["task"]
        self.assertEqual(queued["status"], "queued")
        running = claim_tasks(self.database, total=2, pdf=1, deadline=timedelta(minutes=5))[0]
        # Past its end but not yet removed: the result is refused, and no new task starts.
        self.clock.advance(hours=2, seconds=1)
        with self.assertRaises(LateResult):
            tenant_store.finish_task(self.database, user_id, running["task_id"], running["token"], "succeeded")
        with self.assertRaises(NotFound):
            user.get_task(running["task_id"])
        self.clock.now -= timedelta(hours=2, seconds=1)
        line = self.cv(client, headers, job_id)["lines"][0]
        client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                    json={"text": "Edited.", "expected_text": line["text"]})
        second = client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers)
        self.assertIn(second.status_code, (409, 429))
        self.assertTrue(tenant_store.end_guest(self.database, user_id))
        with self.assertRaises(LateResult):
            tenant_store.finish_task(self.database, user_id, running["task_id"], running["token"], "succeeded")
        self.assertEqual(rows_naming(self.database, user_id), [])
        self.assertEqual(rows_naming(self.database, running["task_id"]), [])

    def test_an_expired_guest_task_never_starts(self):
        self.app.state.runner.stop(5)
        client, headers, job_id = self.guest()
        client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers)
        self.clock.advance(hours=2, seconds=1)
        self.assertEqual(claim_tasks(self.database, total=2, pdf=1, deadline=timedelta(minutes=5)), [])


class OperatorTests(GuestCase):
    def admin(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = v2_admin.main(["--data", str(self.root), *args])
        return code, output.getvalue()

    def test_the_admin_lists_and_ends_guests_and_never_deletes_one_as_an_account(self):
        self.ready_user("alice")
        self.guest()
        self.guest()
        a_id, b_id = guest_ids(self.database)
        code, output = self.admin("guests")
        listed = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(item["user_id"] for item in listed["workspaces"]), sorted([a_id, b_id]))
        self.assertEqual((listed["settings"]["enabled"], listed["started_last_hour"]), (True, 2))
        self.assertNotIn("Example", output)  # no content
        _code, accounts = self.admin("accounts")
        self.assertNotIn(a_id, accounts)
        self.assertNotEqual(self.admin("delete", a_id, "--confirm", a_id)[0], 0)
        self.assertEqual(sorted(guest_ids(self.database)), sorted([a_id, b_id]))
        alice_id = tenant_store.list_accounts(self.database)[0]["user_id"]
        self.assertNotEqual(self.admin("guests", "end", alice_id)[0], 0)  # a signed-in account is no guest
        self.assertEqual(tenant_store.list_accounts(self.database)[0]["state"], "active")
        self.assertNotEqual(self.admin("guests", "end")[0], 0)
        self.assertEqual(self.admin("guests", "end", a_id)[0], 0)
        self.assertEqual(guest_ids(self.database), [b_id])
        self.assertEqual(json.loads(self.admin("guests", "end-all")[1]), {"removed": 1})
        self.assertEqual(guest_ids(self.database), [])
        self.assertEqual(self.admin("set", "guest_max_active", "500")[0] != 0, True)
        self.assertEqual(tenant_store.list_accounts(self.database)[0]["state"], "active")

    def test_disabling_or_deleting_a_signed_in_account_leaves_guests_alone(self):
        client, headers = self.ready_user("alice")
        guest, guest_headers, _job = self.guest()
        alice_id = tenant_store.list_accounts(self.database)[0]["user_id"]
        tenant_store.set_user_active(self.database, alice_id, active=False)
        self.assertEqual(client.get("/api/me", headers=headers).status_code, 401)
        self.assertEqual(guest.get("/api/me", headers=guest_headers).status_code, 200)
        record = tenant_store.delete_account(self.database, alice_id, self.root / "ledger.jsonl")
        self.assertEqual(record["user_id"], alice_id)
        self.assertEqual(len(guest_ids(self.database)), 1)
        self.assertEqual(guest.get("/api/me", headers=guest_headers).status_code, 200)


class BackupTests(GuestCase):
    def test_no_backup_keeps_a_guest_and_no_restore_brings_one_back(self):
        client, headers = self.ready_user("alice")
        alice_job = self.new_job(client, headers)
        guest, guest_headers, guest_job = self.guest()
        self.assertEqual(self.printed(guest, guest_headers, guest_job)["status"], "succeeded")
        guest_id = guest_ids(self.database)[0]
        archive = self.root / "backup.tar.gz"
        v2_backup.create_backup(self.root, archive)
        self.assertEqual(guest_ids(self.database), [guest_id])  # the live workspace goes on
        with tarfile.open(archive) as bundle, tempfile.TemporaryDirectory() as folder:
            member = next(item for item in bundle.getmembers() if item.name.endswith(tenant_store.DATABASE))
            bundle.extract(member, folder, filter="data")
            copied = Path(folder) / member.name
            self.assertEqual(rows_naming(copied, guest_id), [])
            self.assertEqual(rows_naming(copied, guest_job), [])
        restored = self.root / "restored"
        result = v2_backup.restore_backup(archive, restored, live_ledger=self.root / "ledger.jsonl")
        self.assertEqual((result["problems"], result["guest_workspaces_removed"]), ([], 0))
        self.assertEqual(guest_ids(restored / tenant_store.DATABASE), [])
        self.assertEqual(UserWorkspace(restored / tenant_store.DATABASE,
                                       tenant_store.list_accounts(restored / tenant_store.DATABASE)[0]["user_id"]
                                       ).get_job(alice_job)["job_id"], alice_job)

    def test_a_copy_that_still_holds_a_guest_is_restored_without_it(self):
        self.guest()
        guest_id = guest_ids(self.database)[0]
        archive = self.root / "older.tar.gz"
        with patch.object(v2_backup, "_without_guests", lambda _path: 0):  # as a copy made before guests were left out
            v2_backup.create_backup(self.root, archive)
        restored = self.root / "restored"
        result = v2_backup.restore_backup(archive, restored, live_ledger=self.root / "ledger.jsonl")
        self.assertEqual((result["problems"], result["guest_workspaces_removed"]), ([], 1))
        self.assertEqual(rows_naming(restored / tenant_store.DATABASE, guest_id), [])


@unittest.skipUnless(NODE, "Node is needed for frontend checks")
class GuestPageCopyTests(unittest.TestCase):
    """The welcome page and what the server tells a guest read in both languages."""

    def chinese(self):
        script = ("globalThis.window={}; require(" + json.dumps(str(WEB / "zh.js")) + ");"
                  "process.stdout.write(JSON.stringify(window.WorkbenchChinese))")
        return json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)

    def test_every_guest_sentence_has_its_chinese(self):
        chinese = self.chinese()
        page = (WEB / "welcome.html").read_text(encoding="utf-8")
        script = (WEB / "welcome.js").read_text(encoding="utf-8")
        wanted = set(re.findall(r'data-i18n="([^"]+)"', page)) | set(re.findall(r't\("([^"]+)"\)', script))
        for template in re.findall(r"t`([^`]+)`", script):
            count = iter(range(10))
            wanted.add(re.sub(r"\$\{[^}]+\}", lambda _match: "{%d}" % next(count), template))
        wanted |= {tenant_store.GUEST_ONLY, guest_sample.NO_AI_REWORDING, guest_sample.NO_AI_LAYOUT,
                   "The current CV already fits on one page; no further content was omitted."}
        refusals = {  # each refusal a guest's page can show (tenant_store, web_v2)
            "Guest workspaces are not available right now; sign in instead",
            "All guest workspaces are in use right now; try again later, or sign in",
            "Many guest workspaces were started in the last hour; try again later, or sign in",
            "Your guest workspace is still being prepared; try again in a moment",
            "This guest workspace has ended; reload the page to start a new one",
            "You are signed in already; open your workspace instead.",
            "This guest workspace has used all its fit and PDF runs; start a new guest workspace, or sign in",
            "This guest workspace has made as many CV versions as it may; start a new guest workspace, or sign in "
            "to keep editing",
            "Guest workspaces are busy right now; try again in a minute"}
        sources = (ROOT / "feiran" / "tenant_store.py").read_text(encoding="utf-8") + (ROOT / "feiran" / "web_v2.py").read_text(encoding="utf-8")
        flat = re.sub(r'"\s*\n\s*"', "", sources)  # literals split over lines, joined
        self.assertEqual([text for text in refusals if text not in flat], [])  # still what the server says
        wanted |= refusals
        missing = sorted(text for text in wanted if text not in chinese)
        self.assertEqual(missing, [])
        self.assertTrue(chinese[guest_sample.NO_AI_REWORDING].startswith("未使用 AI 润色"))
        self.assertNotIn("未能", chinese[guest_sample.NO_AI_LAYOUT])

    def test_the_scripts_parse(self):
        for name in ("welcome.js", "app.js", "zh.js", "i18n.js"):
            subprocess.run([NODE, "--check", str(WEB / name)], check=True)


if __name__ == "__main__":
    unittest.main()
