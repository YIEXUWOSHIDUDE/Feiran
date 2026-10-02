"""V2 web app end to end with synthetic users: sign-in through a test-only Cognito stand-in,
sessions, CSRF, every private route's isolation, tasks, quotas and the CV flow up to a PDF.
The model, printer and job boards are scripted; nothing leaves the machine."""
import json
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import tenant_store
from identity import Identity, OIDCSettings
from tenant_store import UserWorkspace, set_setting, set_user_active
from tests.fake_identity import CLIENT_ID, DOMAIN, ISSUER, ORIGIN, FakeCognito
from tests.test_cv import FakePrinter
from tests.test_cv_import import ANSWER, LINES, minimal_pdf
from tests.test_listings import FakeBoards, posting
from tests.test_web import FakeDeepSeek
from v2_flow import make_handlers
from cv_import import MAX_PDF_BYTES
from web_v2 import MAX_BODY, BodyLimit, RequestTooLarge, V2Settings, create_v2_app

HOST = "feiran.example"
JD_TEXT = (Path(__file__).resolve().parent.parent / "examples" / "synthetic_jd_zh.txt").read_text(encoding="utf-8")
CONTACT = {"name": "Alex Example", "location": "Los Angeles, CA", "email": "alex@example.com", "links": []}


def pdf_of(lines):
    """A synthetic CV PDF of ``lines`` (left and right columns), in the ASCII the hand-built PDF's
    font can carry: a bullet becomes "- " and a dash "-"."""
    items = []
    for index, parts in enumerate(lines):
        for column, text in zip((72, 430), parts):
            items.append((column, 720 - 14 * index, text.replace("•", "-").replace("–", "-")))
    return minimal_pdf(items)


def no_evidence(request):
    return [{"id": item["id"], "verdict": "none"} for item in request["requirements"]]


class V2Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = self.root / "v2.db"
        self.cognito = FakeCognito(secret="client-secret")
        settings = OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, domain=DOMAIN, public_origin=ORIGIN,
                                client_secret="client-secret")
        self.chat = FakeDeepSeek()
        self.chat.cv_structure = ANSWER
        self.chat.evidence = no_evidence
        self.chat.gap_suggestions = lambda ids: [{"requirement": rid, "kind": "none"} for rid in ids.values()]
        self.printer = FakePrinter()
        self.boards = FakeBoards([posting("1", "Python Intern", "Requirements:\n- Python and SQL"),
                                  posting("2", "Designer", "Figma.")])
        handlers = make_handlers(printer=self.printer, posting=self.read_posting, page=self.read_page)
        self.app = create_v2_app(database=self.database, identity=Identity(settings, self.database,
                                                                          post=self.cognito.post, keys=self.cognito.keys),
                                 settings=V2Settings(public_host=HOST, public_origin=ORIGIN), handlers=handlers,
                                 model=self.chat, boards=self.boards, starter=[], ledger=self.root / "ledger.jsonl")
        for key, value in [("user_daily_units", "40"), ("site_daily_units", "400"), ("registration_open", "1")]:
            set_setting(self.database, key, value)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()  # runs the app's startup: the task runner
        self.addCleanup(self.client.__exit__, None, None, None)

    def read_posting(self, board, job_id, provider="greenhouse"):
        job = next(job for job in self.boards.jobs if job["job_id"] == job_id)
        return {"jd": {key: value for key, value in job.items() if key != "captured_at"} | {
            "captured_at": "2026-09-30T00:00:00+00:00"}, "source_status": "test"}

    def read_page(self, url):
        from job_search import SearchError
        raise SearchError("测试：网页没有职位正文")

    def browser(self, subject="alice"):
        """A new browser (its own cookies) signed in as ``subject``; returns (client, headers)."""
        client = TestClient(self.app, base_url=ORIGIN) if subject else self.client
        started = client.get("/login", params={"return_to": "/"}, follow_redirects=False)
        self.assertEqual(started.status_code, 303)
        callback = client.get("/auth/callback", params=self.cognito.authorize(started.headers["location"], subject),
                              follow_redirects=False)
        self.assertEqual((callback.status_code, callback.headers["location"]), (303, "/"), callback.text)
        page = client.get("/")
        token = re.search(r'name="workbench-token" content="([^"]+)"', page.text).group(1)
        self.assertIn('name="workbench-mode" content="v2"', page.text)
        return client, {"X-Workbench-Token": token}

    def settle(self, client, headers, response):
        """Follow a queued task to its end, as the page does; returns the final task."""
        self.assertIn(response.status_code, (200, 202), response.text)
        body = response.json()
        if "task" not in body:
            return None
        self.assertTrue(self.app.state.runner.drain(20))
        return client.get(f"/api/tasks/{body['task']['task_id']}", headers=headers).json()["task"]

    def consent(self, client, headers, *stages):
        for stage in stages:
            self.assertEqual(client.post("/api/consent", json={"stage": stage, "version": 1}, headers=headers).status_code, 200)

    def ready_user(self, subject="alice"):
        """Signed in, agreed to both data flows, CV uploaded, saved and every fact confirmed."""
        client, headers = self.browser(subject)
        self.consent(client, headers, "upload_parsing", "job_processing")
        task = self.settle(client, headers, client.post("/api/cv/upload?language=en", content=pdf_of(LINES),
                                                        headers={**headers, "Content-Type": "application/pdf"}))
        self.assertEqual(task["status"], "succeeded", task)
        upload = task["result"]["upload_id"]
        proposal = client.get(f"/api/cv/uploads/{upload}", headers=headers).json()
        self.assertEqual(proposal["private"]["email"], "alex@example.com")
        saved = client.post(f"/api/cv/uploads/{upload}/save", json=CONTACT, headers=headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        refs = [f"{fact['id']}@{fact['version']}" for fact in client.get("/api/facts", headers=headers).json()["facts"]]
        self.assertEqual(client.post("/api/facts/confirm", json={"refs": refs}, headers=headers).status_code, 200)
        return client, headers

    def new_job(self, client, headers):
        response = client.post("/api/jobs", json={"title": "后端开发实习生", "company": "示例公司", "text": JD_TEXT},
                               headers=headers)
        task = self.settle(client, headers, response)
        self.assertEqual(task["status"], "succeeded", task)
        return response.json()["job_id"]


class SignInTests(V2Case):
    def test_everything_private_needs_a_session_and_writes_need_the_csrf_token(self):
        anonymous = TestClient(self.app, base_url=ORIGIN)
        self.assertEqual(anonymous.get("/", follow_redirects=False).headers["location"], "/login?return_to=/")
        for path in ["/api/facts", "/api/jobs", "/preview/x/en", "/download/x/en.pdf", "/api/tasks/x"]:
            self.assertEqual(anonymous.get(path).status_code, 401, path)
        self.assertEqual(anonymous.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(anonymous.get("/static/app.js").status_code, 200)
        client, headers = self.browser()
        self.assertEqual(client.get("/api/facts", headers=headers).json(), {"facts": []})
        self.assertEqual(client.post("/api/consent", json={"stage": "upload_parsing", "version": 1}).status_code, 403)
        self.assertEqual(client.post("/api/consent", json={"stage": "upload_parsing", "version": 1},
                                     headers={**headers, "Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(client.post("/api/consent", json={"stage": "upload_parsing", "version": 1},
                                     headers={**headers, "Origin": ORIGIN}).status_code, 200)
        self.assertEqual(client.get("/api/facts", headers={"Host": "evil.example"}).status_code, 403)

    def test_one_account_per_identity_and_registration_rules_apply(self):
        set_setting(self.database, "registration_open", "0")
        closed = TestClient(self.app, base_url=ORIGIN)
        started = closed.get("/login", follow_redirects=False)
        refused = closed.get("/auth/callback", params=self.cognito.authorize(started.headers["location"], "nobody"),
                             follow_redirects=False)
        self.assertEqual(refused.status_code, 403)
        self.assertIn("not taking new accounts", refused.text)
        self.assertEqual(closed.get("/api/facts").status_code, 401)
        set_setting(self.database, "registration_open", "1")
        first, _ = self.browser("alice")
        second, _ = self.browser("alice")
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            user_id = connection.execute("SELECT user_id FROM users").fetchone()[0]
        set_user_active(self.database, user_id, active=False)
        self.assertEqual(first.get("/api/facts").status_code, 401)  # its session ended
        again = TestClient(self.app, base_url=ORIGIN)
        started = again.get("/login", follow_redirects=False)
        self.assertEqual(again.get("/auth/callback", params=self.cognito.authorize(started.headers["location"], "alice"),
                                   follow_redirects=False).status_code, 403)

    def test_a_forged_or_replayed_callback_signs_no_one_in(self):
        client = TestClient(self.app, base_url=ORIGIN)
        started = client.get("/login", follow_redirects=False)
        callback = self.cognito.authorize(started.headers["location"], "alice")
        other = TestClient(self.app, base_url=ORIGIN)  # another browser, without the login cookie
        self.assertEqual(other.get("/auth/callback", params=callback, follow_redirects=False).status_code, 400)
        self.assertEqual(other.get("/api/facts").status_code, 401)
        self.assertEqual(client.get("/auth/callback", params=callback, follow_redirects=False).status_code, 303)
        self.assertEqual(client.get("/auth/callback", params=callback, follow_redirects=False).status_code, 400)
        client.post("/logout", headers={"X-Workbench-Token": re.search(
            r'name="workbench-token" content="([^"]+)"', client.get("/").text).group(1)})
        self.cognito.override = lambda claims: {**claims, "aud": "someone-else"}
        started = client.get("/login", follow_redirects=False)
        bad = client.get("/auth/callback", params=self.cognito.authorize(started.headers["location"], "alice"),
                         follow_redirects=False)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(client.get("/api/facts").status_code, 401)
        self.assertEqual(client.get("/auth/callback", params={"error": "access_denied"}).status_code, 400)

    def test_logout_and_expiry_end_the_session_on_the_server(self):
        client, headers = self.browser()
        cookie = client.cookies.get("__Host-feiran")
        logout = client.post("/logout", headers=headers)
        self.assertTrue(logout.json()["logout_url"].startswith(f"{DOMAIN}/logout?client_id={CLIENT_ID}"))
        replay = TestClient(self.app, base_url=ORIGIN, cookies={"__Host-feiran": cookie})
        self.assertEqual(replay.get("/api/facts").status_code, 401)
        client, headers = self.browser()
        later = tenant_store._clock() + timedelta(hours=3)
        with patch.object(tenant_store, "_clock", lambda: later):
            self.assertEqual(client.get("/api/facts", headers=headers).status_code, 401)


class FlowTests(V2Case):
    def test_upload_needs_consent_and_parses_in_a_task(self):
        client, headers = self.browser()
        refused = client.post("/api/cv/upload?language=en", content=pdf_of(LINES),
                              headers={**headers, "Content-Type": "application/pdf"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "consent_required"))
        self.assertEqual(self.chat.sent, [])  # nothing reached the model
        self.consent(client, headers, "upload_parsing")
        response = client.post("/api/cv/upload?language=en", content=pdf_of(LINES),
                               headers={**headers, "Content-Type": "application/pdf"})
        self.assertEqual(response.status_code, 202)
        task = self.settle(client, headers, response)
        self.assertEqual(task["status"], "succeeded")
        for private in ("alex@example.com", "000-000-0000", "ALEX EXAMPLE"):
            self.assertNotIn(private, "".join(self.chat.sent))
        self.assertEqual(client.post("/api/cv/upload?language=en", content=b"x" * 10, headers={
            **headers, "Content-Type": "application/pdf"}).status_code, 400)
        big = client.post("/api/cv/upload?language=en", content=b"%PDF" + b"0" * 5_000_001,
                          headers={**headers, "Content-Type": "application/pdf"})
        self.assertEqual(big.status_code, 413)
        with tenant_store.transaction(self.database) as connection:
            stored = connection.execute("SELECT request_payload FROM tasks WHERE operation = 'upload_cv'").fetchone()[0]
        self.assertNotIn("alex@example.com", stored)  # the finished upload's CV lines are dropped

    def test_the_whole_cv_flow_ends_in_the_approved_pdf(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        view = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        self.assertEqual([item["text"] for item in view["candidates"]],
                         ["本科及以上学历，计算机相关专业", "熟悉Python，了解SQL", "每周至少实习4天"])
        cv = view["cv"]["en"]
        self.assertEqual((cv["head"], cv["stale"]), ("tailored", False))
        self.assertEqual([stage["status"] for stage in cv["stages"]], ["done", "done", "fallback"])
        gaps = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/gaps", headers=headers))
        self.assertEqual(gaps["status"], "succeeded")
        self.assertEqual(client.get(f"/api/jobs/{job_id}", headers=headers).json()["gaps"]["counts"]["none"], 3)
        preview = client.get(f"/preview/{job_id}/en", params={"v": cv["content_sha256"]}, headers=headers)
        self.assertIn("DRAFT", preview.text)
        self.assertEqual(client.get(f"/download/{job_id}/en.pdf").status_code, 404)
        approved = client.post(f"/api/jobs/{job_id}/cv/en/approve", headers=headers,
                               json={"expected_content_sha256": cv["content_sha256"]})
        self.assertEqual(approved.json()["cv"]["en"]["head"], "approved")
        exported = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers))
        self.assertEqual(exported["status"], "succeeded", exported)
        pdf = client.get(f"/download/{job_id}/en.pdf")
        self.assertEqual((pdf.status_code, pdf.content[:4]), (200, b"%PDF"))
        self.assertNotIn("DRAFT", self.printer.html)
        self.assertNotIn("DRAFT", client.get(f"/preview/{job_id}/en").text)
        self.assertIn("cv-approved-en", client.get("/api/jobs", headers=headers).json()["jobs"][0]["steps"])

    def test_an_old_page_cannot_approve_and_a_changed_fact_withdraws_the_final_pdf(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        old = client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]["content_sha256"]
        self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=headers))
        head = client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]
        stale = client.post(f"/api/jobs/{job_id}/cv/en/approve", json={"expected_content_sha256": "0" * 64},
                            headers=headers)
        self.assertEqual(stale.status_code, 409)
        client.post(f"/api/jobs/{job_id}/cv/en/approve", json={"expected_content_sha256": head["content_sha256"]},
                    headers=headers)
        self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers))
        self.assertEqual(client.get(f"/download/{job_id}/en.pdf").status_code, 200)
        fact = next(fact for fact in client.get("/api/facts", headers=headers).json()["facts"]
                    if fact["text"].startswith("Wrote unit tests"))
        edited = client.post(f"/api/facts/{fact['id']}/edit", headers=headers,
                             json={"expected_version": fact["version"], "text": "Wrote unit tests for payment code.",
                                   "tags": fact["tags"]})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(client.get(f"/download/{job_id}/en.pdf").status_code, 404)
        cv = client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]
        self.assertTrue(cv["stale"])
        self.assertIn("DRAFT", client.get(f"/preview/{job_id}/en").text)
        self.assertEqual(client.post(f"/api/jobs/{job_id}/cv/en/approve", headers=headers,
                                     json={"expected_content_sha256": cv["content_sha256"]}).status_code, 409)
        self.assertNotEqual(old, head["content_sha256"])

    def test_the_same_request_is_one_task_and_a_reused_key_is_refused(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        keyed = {**headers, "Idempotency-Key": "click-1"}
        with patch.object(self.app.state.runner, "wake"):  # keep it queued while clicking twice
            first = client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=keyed)
            second = client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=keyed)
        self.assertEqual(first.json()["task"]["task_id"], second.json()["task"]["task_id"])
        other = client.post(f"/api/jobs/{job_id}/cv/en/tailor", headers=keyed)  # another operation, its own key space
        self.assertIn(other.status_code, (202, 429))
        self.settle(client, headers, first)
        with tenant_store.transaction(self.database) as connection:
            tasks = connection.execute("SELECT COUNT(*) FROM tasks WHERE operation = 'prepare_cv'").fetchone()[0]
        self.assertEqual(tasks, 1)

    def test_an_action_sent_again_after_its_answer_was_lost_finds_its_task(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        keyed = {**headers, "Idempotency-Key": "action-1"}
        first = client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=keyed)
        self.assertEqual(self.settle(client, headers, first)["status"], "succeeded")

        def materials():
            with tenant_store.transaction(self.database) as connection:
                return connection.execute("SELECT COUNT(*) FROM materials WHERE job_id = ?", (job_id,)).fetchone()[0]

        made = materials()
        again = client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=keyed)
        self.assertEqual((again.status_code, again.json()["task"]["task_id"]), (202, first.json()["task"]["task_id"]))
        self.assertTrue(self.app.state.runner.drain(20))
        self.assertEqual(materials(), made)  # nothing was prepared twice
        other_job = client.post("/api/jobs", json={"title": "Other", "company": "示例公司", "text": JD_TEXT + "\n- Go"},
                                headers=headers)
        self.settle(client, headers, other_job)
        for path in (f"/api/jobs/{job_id}/cv/zh/prepare", f"/api/jobs/{other_job.json()['job_id']}/cv/en/prepare"):
            self.assertEqual(client.post(path, headers=keyed).status_code, 409)  # the same key, another request
        fresh = client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers={**headers, "Idempotency-Key": "action-2"})
        self.assertNotEqual(fresh.json()["task"]["task_id"], first.json()["task"]["task_id"])  # a new click is new work

    def test_a_paste_sent_again_keeps_its_job_its_capture_time_and_its_work(self):
        client, headers = self.ready_user()
        with tenant_store.transaction(self.database) as connection:
            user = UserWorkspace(self.database, connection.execute("SELECT user_id FROM users").fetchone()[0])
        body = {"title": "后端开发实习生", "company": "示例公司", "text": JD_TEXT}
        keyed = {**headers, "Idempotency-Key": "paste-1"}

        def counts():
            with tenant_store.transaction(self.database) as connection:
                return (connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                        connection.execute("SELECT COUNT(*) FROM tasks WHERE operation = 'prepare_job'").fetchone()[0])

        first = client.post("/api/jobs", json=body, headers=keyed)
        self.assertEqual(self.settle(client, headers, first)["status"], "succeeded")
        job_id = first.json()["job_id"]
        captured = user.get_job(job_id)["jd"]["captured_at"]
        sent = len(self.chat.sent)
        for again in (keyed, {**headers, "Idempotency-Key": "paste-2"}, headers):  # resent; a new click; no key
            answer = client.post("/api/jobs", json=body, headers=again)
            self.assertEqual((answer.status_code, answer.json()), (200, {"job_id": job_id, "existing": True}))
        self.assertEqual(counts(), (1, 1))
        self.assertEqual(user.get_job(job_id)["jd"]["captured_at"], captured)  # a resend is not a new capture
        self.assertEqual(len(self.chat.sent), sent)  # nothing was found or paid for twice
        other = client.post("/api/jobs", json={**body, "text": JD_TEXT + "\n- Go"}, headers=keyed)
        self.assertEqual(other.status_code, 409)  # the same key for other words
        self.assertEqual(counts(), (1, 1))  # and nothing of it was saved

    def test_a_paste_while_its_first_one_is_worked_on_joins_that_work(self):
        client, headers = self.ready_user()
        body = {"title": "后端开发实习生", "company": "示例公司", "text": JD_TEXT}
        with patch.object(self.app.state.runner, "wake"):
            first = client.post("/api/jobs", json=body, headers={**headers, "Idempotency-Key": "paste-a"})
            second = client.post("/api/jobs", json=body, headers={**headers, "Idempotency-Key": "paste-b"})
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertEqual(second.json()["task"]["task_id"], first.json()["task"]["task_id"])
        self.assertEqual(self.settle(client, headers, second)["status"], "succeeded")
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks WHERE operation = 'prepare_job'").fetchone()[0], 1)

    def test_a_paste_that_cannot_be_worked_on_now_is_kept_and_worked_on_later(self):
        client, headers = self.ready_user()
        body = {"title": "后端开发实习生", "company": "示例公司", "text": JD_TEXT}
        set_setting(self.database, "tasks_enabled", "0")
        paused = client.post("/api/jobs", json=body, headers={**headers, "Idempotency-Key": "paste-x"})
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["task_error"]["code"], "tasks_paused")
        set_setting(self.database, "tasks_enabled", "1")
        later = client.post("/api/jobs", json=body, headers={**headers, "Idempotency-Key": "paste-x"})
        self.assertEqual((later.status_code, later.json()["job_id"]), (202, paused.json()["job_id"]))
        self.assertEqual(self.settle(client, headers, later)["status"], "succeeded")

    def test_finding_requirements_that_changed_meanwhile_calls_no_model(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        with tenant_store.transaction(self.database) as connection:
            user = UserWorkspace(self.database, connection.execute("SELECT user_id FROM users").fetchone()[0])
        used = user.usage_today()["used"]
        stale = user.submit_task("prepare_job", key="stale-find", request={"job_id": job_id},
                                 context={"base_requirements_version": 0}, units=3, heavy="model", job_id=job_id,
                                 consent="job_processing")
        sent = len(self.chat.sent)
        self.app.state.runner.wake()
        self.assertTrue(self.app.state.runner.drain(20))
        task = user.get_task(stale["task_id"])
        self.assertEqual((task["status"], task["cost"]), ("superseded", "none"))
        self.assertEqual(len(self.chat.sent), sent)  # no model call for work that could not be kept
        self.assertEqual(user.usage_today()["used"], used)  # and its units came back

    def test_daily_quota_and_waiting_work_are_bounded(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        set_setting(self.database, "user_daily_units", "4")  # already used: 1 upload + 3 for the job
        refused = client.post(f"/api/jobs/{job_id}/gaps", headers=headers)
        self.assertEqual((refused.status_code, refused.json()["code"]), (429, "quota_exhausted"))
        self.assertEqual(refused.headers["Retry-After"], "60")
        set_setting(self.database, "user_daily_units", "40")
        set_setting(self.database, "tasks_enabled", "0")
        paused = client.post(f"/api/jobs/{job_id}/gaps", headers=headers)
        self.assertEqual((paused.status_code, paused.json()["code"]), (503, "tasks_paused"))


class IsolationTests(V2Case):
    def test_another_users_ids_find_nothing_on_any_private_route(self):
        a, a_headers = self.ready_user("alice")
        job_id = self.new_job(a, a_headers)
        cv = a.get(f"/api/jobs/{job_id}", headers=a_headers).json()["cv"]["en"]
        a.post(f"/api/jobs/{job_id}/cv/en/approve", json={"expected_content_sha256": cv["content_sha256"]},
               headers=a_headers)
        export = a.post(f"/api/jobs/{job_id}/cv/en/export", headers=a_headers)
        self.settle(a, a_headers, export)
        task_id = export.json()["task"]["task_id"]
        gaps = self.settle(a, a_headers, a.post(f"/api/jobs/{job_id}/gaps", headers=a_headers))
        requirement = a.get(f"/api/jobs/{job_id}", headers=a_headers).json()["gaps"]["requirements"][0]["requirement_id"]
        fact = a.get("/api/facts", headers=a_headers).json()["facts"][0]
        before = (a.get(f"/api/jobs/{job_id}", headers=a_headers).json(), a.get("/api/facts", headers=a_headers).json())
        b, b_headers = self.browser("bob")
        self.consent(b, b_headers, "upload_parsing", "job_processing")
        probes = [
            ("get", f"/api/jobs/{job_id}", None), ("get", f"/preview/{job_id}/en", None),
            ("get", f"/download/{job_id}/en.pdf", None), ("get", f"/api/tasks/{task_id}", None),
            ("post", f"/api/tasks/{task_id}/cancel", None), ("post", "/api/notices/dismiss", {"id": task_id}),
            ("post", f"/api/jobs/{job_id}/cv/en/approve", {"expected_content_sha256": cv["content_sha256"]}),
            ("post", f"/api/jobs/{job_id}/cv/en/export", None), ("post", f"/api/jobs/{job_id}/cv/en/prepare", None),
            ("post", f"/api/jobs/{job_id}/cv/en/tailor", None), ("post", f"/api/jobs/{job_id}/cv/en/plan", None),
            ("post", f"/api/jobs/{job_id}/cv/en/change", {"change_id": "x", "undone": True}),
            ("post", f"/api/jobs/{job_id}/gaps", None), ("post", f"/api/jobs/{job_id}/gaps/{requirement}/accept", None),
            ("post", f"/api/jobs/{job_id}/gaps/{requirement}/decline", None),
            ("post", f"/api/jobs/{job_id}/gaps/{requirement}/write", {"place": "x", "text": "y"}),
            ("post", f"/api/jobs/{job_id}/requirements/decide", {"confirm": [], "exclude": [], "expected_version": 1}),
            ("post", f"/api/jobs/{job_id}/requirements/add", {"text": "熟悉Python", "expected_version": 1}),
            ("post", f"/api/jobs/{job_id}/requirements/find", {"expected_version": 1}),
            ("post", f"/api/jobs/{job_id}/language", {"language": "en"}),
            ("post", f"/api/facts/{fact['id']}/edit", {"expected_version": 1, "text": "Injected", "tags": []}),
            ("post", "/api/facts/confirm", {"refs": [f"{fact['id']}@1"]}),
        ]
        for method, path, body in probes:
            with self.subTest(path=path, method=method):
                response = getattr(b, method)(path, headers=b_headers, **({"json": body} if body is not None else {}))
                self.assertIn(response.status_code, (400, 404), response.text)
                self.assertNotIn("Example Corp", response.text)
        self.assertEqual(b.get("/api/jobs", headers=b_headers).json(), {"jobs": []})
        self.assertEqual(b.get("/api/facts", headers=b_headers).json(), {"facts": []})
        after = (a.get(f"/api/jobs/{job_id}", headers=a_headers).json(), a.get("/api/facts", headers=a_headers).json())
        self.assertEqual(before, after)
        self.assertEqual(a.get(f"/download/{job_id}/en.pdf").status_code, 200)
        self.assertEqual(gaps["status"], "succeeded")

    def test_followed_companies_and_their_ranking_are_personal(self):
        a, a_headers = self.ready_user("alice")
        added = a.post("/api/sources", json={"link": "https://job-boards.greenhouse.io/example"}, headers=a_headers)
        self.assertEqual(added.status_code, 200, added.text)
        ranked = a.get("/api/listings", headers=a_headers).json()
        self.assertEqual([item["title"] for item in ranked["listings"]], ["Python Intern", "Designer"])
        self.assertEqual(ranked["listings"][0]["matched"], ["Python"])
        b, b_headers = self.browser("bob")
        self.assertEqual(b.get("/api/sources", headers=b_headers).json(), {"sources": []})
        self.assertEqual(b.get("/api/listings", headers=b_headers).json()["listings"], [])
        refused = b.post("/api/listings/start", json={"provider": "greenhouse", "board": "example", "job_id": "1"},
                         headers=b_headers)
        self.assertEqual(refused.status_code, 404)
        started = a.post("/api/listings/start", json={"provider": "greenhouse", "board": "example", "job_id": "1"},
                         headers=a_headers)
        task = self.settle(a, a_headers, started)
        self.assertEqual(task["status"], "succeeded", task)
        again = a.post("/api/listings/start", json={"provider": "greenhouse", "board": "example", "job_id": "1"},
                       headers=a_headers).json()
        self.assertEqual((again["job_id"], again["existing"]), (task["result"]["job_id"], True))
        self.assertEqual(len(self.boards.calls), 1)  # B never read the board; A read it once
        b.post("/api/sources", json={"link": "https://job-boards.greenhouse.io/example"}, headers=b_headers)
        self.assertEqual(len(self.boards.calls), 1)  # a fresh public copy is shared, not read again

    def test_deleting_an_account_removes_its_data_and_it_cannot_sign_in_again(self):
        a, a_headers = self.ready_user("alice")
        self.new_job(a, a_headers)
        self.assertEqual(a.post("/api/account/delete", json={"confirm": "yes"}, headers=a_headers).status_code, 400)
        deleted = a.post("/api/account/delete", json={"confirm": "DELETE"}, headers=a_headers)
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(a.get("/api/facts").status_code, 401)
        with tenant_store.transaction(self.database) as connection:
            for table in tenant_store.USER_TABLES:
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        self.assertEqual(len(tenant_store.read_deletions(self.root / "ledger.jsonl")), 1)
        again = TestClient(self.app, base_url=ORIGIN)
        started = again.get("/login", follow_redirects=False)
        refused = again.get("/auth/callback", params=self.cognito.authorize(started.headers["location"], "alice"),
                            follow_redirects=False)
        self.assertEqual(refused.status_code, 403)


if __name__ == "__main__":
    unittest.main()


class MoreRouteTests(V2Case):
    PLAN = {"sections": ["education", "skills", "experience"], "entries": [],
            "reasons": [{"target": "sections", "reason": "Skills first."}]}

    def job(self, client, headers, job_id):
        return client.get(f"/api/jobs/{job_id}", headers=headers).json()

    def test_requirement_decisions_prepare_again_and_each_change_can_be_undone(self):
        self.chat.plan = self.PLAN
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        view = self.job(client, headers, job_id)
        self.assertEqual(view["cv"]["en"]["head"], "planned")
        change = view["cv"]["en"]["changes"][0]
        undone = client.post(f"/api/jobs/{job_id}/cv/en/change", json={"change_id": change["id"], "undone": True},
                             headers=headers).json()
        self.assertTrue(undone["cv"]["en"]["changes"][0]["undone"])
        self.assertNotEqual(undone["cv"]["en"]["content_sha256"], view["cv"]["en"]["content_sha256"])
        added = client.post(f"/api/jobs/{job_id}/requirements/add", headers=headers,
                            json={"text": "有开源项目经历", "expected_version": view["requirements_version"]})
        self.assertEqual(self.settle(client, headers, added)["status"], "succeeded")
        view = self.job(client, headers, job_id)
        by_text = {item["text"]: item for item in view["candidates"]}
        self.assertEqual((by_text["有开源项目经历"]["status"], by_text["有开源项目经历"]["decided_by"]), ("confirmed", "user"))
        self.assertFalse(view["cv"]["en"]["stale"])
        invented = client.post(f"/api/jobs/{job_id}/requirements/add", headers=headers,
                               json={"text": "精通 Kubernetes", "expected_version": view["requirements_version"]})
        self.assertEqual(invented.status_code, 400)
        decided = client.post(f"/api/jobs/{job_id}/requirements/decide", headers=headers, json={
            "confirm": [by_text["熟悉Python，了解SQL"]["id"]], "exclude": [by_text["每周至少实习4天"]["id"]],
            "expected_version": view["requirements_version"]})
        self.assertEqual(self.settle(client, headers, decided)["status"], "succeeded")
        view = self.job(client, headers, job_id)
        self.assertNotIn("每周至少实习4天", [item["text"] for item in view["selected_requirements"]])
        self.assertFalse(view["cv"]["en"]["stale"])
        reworded = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/tailor", headers=headers))
        self.assertEqual(reworded["status"], "succeeded", reworded)
        view = self.job(client, headers, job_id)
        self.assertEqual(view["cv"]["en"]["head"], "tailored")
        self.assertEqual([stage["status"] for stage in view["cv"]["en"]["stages"]], ["done", "done", "skipped"])
        adjusted = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/plan", headers=headers))
        self.assertEqual(adjusted["status"], "succeeded", adjusted)
        self.assertEqual(self.job(client, headers, job_id)["cv"]["en"]["head"], "planned")
        refused = client.post(f"/api/jobs/{job_id}/language", json={"language": "zh"}, headers=headers)
        self.assertEqual(refused.status_code, 400)  # no Chinese CV uploaded
        found = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/requirements/find", headers=headers, json={
            "expected_version": self.job(client, headers, job_id)["requirements_version"]}))
        self.assertEqual(found["status"], "succeeded", found)

    def test_a_fact_edited_while_its_cv_is_prepared_is_not_overwritten_by_the_old_result(self):
        self.chat.plan = self.PLAN
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        with tenant_store.transaction(self.database) as connection:
            user = UserWorkspace(self.database, connection.execute("SELECT user_id FROM users").fetchone()[0])
        made = len(user.list_materials(job_id))
        shown = user.job_snapshot(job_id)["cv"]["en"]["material"]["draft"]["facts"][0]
        fact = user.get_fact(shown["id"])
        # The user edits a line the CV shows while the model adjusts it.
        self.chat.while_planning = lambda: user.revise_fact(fact["id"], expected_version=fact["version"],
                                                            text=fact["text"] + " Edited meanwhile.", tags=fact["tags"])
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=headers))
        self.assertEqual((task["status"], task["error_code"]), ("superseded", "inputs_changed"))
        self.assertEqual(len(user.list_materials(job_id)), made)
        self.assertEqual(user.get_fact(fact["id"])["version"], fact["version"] + 1)

    def test_a_reused_request_key_never_hands_a_new_change_an_old_preparation(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        keyed = {**headers, "Idempotency-Key": "reused"}
        view = self.job(client, headers, job_id)
        first = view["candidates"][0]["id"]
        one = client.post(f"/api/jobs/{job_id}/requirements/decide", headers=keyed, json={
            "confirm": [], "exclude": [first], "expected_version": view["requirements_version"]})
        self.assertEqual(self.settle(client, headers, one)["status"], "succeeded")
        view = self.job(client, headers, job_id)
        two = client.post(f"/api/jobs/{job_id}/requirements/decide", headers=keyed, json={
            "confirm": [first], "exclude": [], "expected_version": view["requirements_version"]})
        self.assertEqual(self.settle(client, headers, two)["status"], "succeeded")
        self.assertNotEqual(two.json()["task"]["task_id"], one.json()["task"]["task_id"])
        self.assertFalse(self.job(client, headers, job_id)["cv"]["en"]["stale"])  # prepared for the newest decision

    def test_a_page_left_open_cannot_overwrite_newer_requirement_decisions(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        old = self.job(client, headers, job_id)
        first, second = old["candidates"][0]["id"], old["candidates"][1]["id"]
        current = client.post(f"/api/jobs/{job_id}/requirements/decide", headers=headers, json={
            "confirm": [], "exclude": [first], "expected_version": old["requirements_version"]})
        self.settle(client, headers, current)
        newer = self.job(client, headers, job_id)
        self.assertEqual(newer["requirements_version"], old["requirements_version"] + 1)
        for path, body in ((f"/api/jobs/{job_id}/requirements/decide", {"confirm": [first], "exclude": [second]}),
                           (f"/api/jobs/{job_id}/requirements/add", {"text": "有开源项目经历"}),
                           (f"/api/jobs/{job_id}/requirements/find", {})):
            stale = client.post(path, headers=headers, json={**body, "expected_version": old["requirements_version"]})
            self.assertEqual((stale.status_code, stale.json()["error"]), (409, "This page is out of date; reload it"))
        after = self.job(client, headers, job_id)
        self.assertEqual(after["requirements_version"], newer["requirements_version"])
        self.assertEqual({item["id"]: item["status"] for item in after["candidates"]},
                         {item["id"]: item["status"] for item in newer["candidates"]})
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks WHERE status = 'queued'").fetchone()[0], 0)

    def test_answers_to_gaps_become_confirmed_lines_and_the_cv_is_prepared_again(self):
        self.chat.gap_suggestions = lambda ids: [
            {"requirement": ids["熟悉Python，了解SQL"], "kind": "skill", "line": "fact-skills-languages", "items": ["SQL"]}]
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        self.assertEqual(self.settle(client, headers, client.post(f"/api/jobs/{job_id}/gaps", headers=headers))["status"],
                         "succeeded")
        gaps = self.job(client, headers, job_id)["gaps"]
        by_text = {item["text"]: item for item in gaps["requirements"]}
        python = by_text["熟悉Python，了解SQL"]
        self.assertEqual(python["suggestion"]["items"], ["SQL"])
        accepted = client.post(f"/api/jobs/{job_id}/gaps/{python['requirement_id']}/accept", headers=headers)
        self.assertEqual(self.settle(client, headers, accepted)["status"], "succeeded")
        facts = client.get("/api/facts", headers=headers).json()["facts"]
        skills = next(fact for fact in facts if fact["text"].startswith("Languages:"))
        self.assertEqual((skills["text"], skills["status"], skills["version"]), ("Languages: Python, Java, SQL", "confirmed", 2))
        view = self.job(client, headers, job_id)
        self.assertFalse(view["cv"]["en"]["stale"])
        degree = by_text["本科及以上学历，计算机相关专业"]
        declined = client.post(f"/api/jobs/{job_id}/gaps/{degree['requirement_id']}/decline", headers=headers)
        self.assertEqual(declined.status_code, 200)
        place = next(item for item in view["gaps"]["places"] if item["kind"] == "bullet")
        weekly = by_text["每周至少实习4天"]
        written = client.post(f"/api/jobs/{job_id}/gaps/{weekly['requirement_id']}/write", headers=headers,
                              json={"place": place["id"], "text": "Worked four days a week during the internship."})
        self.assertEqual(self.settle(client, headers, written)["status"], "succeeded")
        own = [fact for fact in client.get("/api/facts", headers=headers).json()["facts"]
               if fact["text"] == "Worked four days a week during the internship."]
        self.assertEqual([fact["status"] for fact in own], ["confirmed"])

    def test_cancelled_and_failed_work_is_explained_on_the_page(self):
        client, headers = self.ready_user()
        failed = self.settle(client, headers, client.post("/api/jobs/from-url", headers=headers,
                                                          json={"url": "https://careers.example.com/jobs/1"}))
        self.assertEqual((failed["status"], failed["error_code"]), ("failed", "unreadable"))
        self.assertIn("网页没有职位正文", failed["message"])
        self.assertEqual(client.get("/api/jobs", headers=headers).json(), {"jobs": []})
        job_id = self.new_job(client, headers)
        response = client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers={**headers, "Idempotency-Key": "c1"})
        task_id = response.json()["task"]["task_id"]
        cancelled = client.post(f"/api/tasks/{task_id}/cancel", headers=headers).json()["task"]
        self.assertIn(cancelled["status"], ("cancelled", "succeeded"))  # it may have finished first
        self.assertTrue(self.app.state.runner.drain(20))
        tasks = self.job(client, headers, job_id)["tasks"]
        self.assertIn(task_id, [task["task_id"] for task in tasks])
        dismissed = client.post("/api/notices/dismiss", json={"id": task_id}, headers=headers)
        self.assertEqual(dismissed.status_code, 200)
        self.assertNotIn(task_id, [task["task_id"] for task in self.job(client, headers, job_id)["tasks"]])


class RequestKeyTests(V2Case):
    """A request key names one request. Used again for anything else it is refused (409), whatever
    the first request led to, and the refusal makes no job and no task and spends nothing."""

    BODY = {"title": "后端开发实习生", "company": "示例公司", "text": JD_TEXT}
    OTHER = {"title": "数据实习生", "company": "示例公司", "text": JD_TEXT + "\n- SQL"}

    def setUp(self):
        super().setUp()
        self.client, self.headers = self.ready_user()
        with tenant_store.transaction(self.database) as connection:
            self.user = UserWorkspace(self.database, connection.execute("SELECT user_id FROM users").fetchone()[0])

    def post(self, path, key, body):
        return self.client.post(path, json=body, headers={**self.headers, "Idempotency-Key": key})

    def state(self):
        with tenant_store.transaction(self.database) as connection:
            counts = tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("jobs", "tasks"))
        return (*counts, self.user.usage_today()["used"])

    def refused_without_trace(self, path, key, body):
        before = self.state()
        answer = self.post(path, key, body)
        self.assertEqual(answer.status_code, 409, answer.text)
        self.assertEqual(answer.json()["error"], "This request key was already used for a different request")
        self.assertTrue(self.app.state.runner.drain(20))
        self.assertEqual(self.state(), before)

    def test_a_paste_key_is_bound_on_every_path_a_paste_is_accepted_by(self):
        # New work queued.
        self.assertEqual(self.settle(self.client, self.headers, self.post("/api/jobs", "new", self.BODY))["status"],
                         "succeeded")
        self.refused_without_trace("/api/jobs", "new", self.OTHER)
        # The job's requirements are found already: answered by the job.
        self.assertEqual(self.post("/api/jobs", "existing", self.BODY).json()["existing"], True)
        self.refused_without_trace("/api/jobs", "existing", self.OTHER)
        self.assertEqual(self.post("/api/jobs", "existing", self.BODY).json()["existing"], True)  # the same request again
        # Work already under way for the same words: joined.
        third = {**self.BODY, "text": JD_TEXT + "\n- Docker"}
        with patch.object(self.app.state.runner, "wake"):
            started = self.post("/api/jobs", "first-of-third", third)
            joined = self.post("/api/jobs", "joined", third)
        self.assertEqual(joined.json()["task"]["task_id"], started.json()["task"]["task_id"])
        self.refused_without_trace("/api/jobs", "joined", self.OTHER)
        # Saved while no work can be queued; the same request queues it later.
        set_setting(self.database, "tasks_enabled", "0")
        fourth = {**self.BODY, "text": JD_TEXT + "\n- Linux"}
        saved = self.post("/api/jobs", "saved", fourth)
        self.assertEqual((saved.status_code, saved.json()["task_error"]["code"]), (200, "tasks_paused"))
        set_setting(self.database, "tasks_enabled", "1")
        self.refused_without_trace("/api/jobs", "saved", self.OTHER)
        later = self.post("/api/jobs", "saved", fourth)
        self.assertEqual((later.status_code, later.json()["job_id"]), (202, saved.json()["job_id"]))
        self.assertEqual(self.settle(self.client, self.headers, later)["status"], "succeeded")
        # The other words were never accepted under any of these keys; a new key takes them.
        self.assertEqual(self.post("/api/jobs", "fresh", self.OTHER).status_code, 202)

    def test_a_url_key_is_bound_whether_it_made_a_job_or_found_one(self):
        url, other = "https://job-boards.greenhouse.io/example/jobs/1", "https://job-boards.greenhouse.io/example/jobs/2"
        made = self.post("/api/jobs/from-url", "url-new", {"url": url})
        self.assertEqual(self.settle(self.client, self.headers, made)["status"], "succeeded")
        self.refused_without_trace("/api/jobs/from-url", "url-new", {"url": other})
        found = self.post("/api/jobs/from-url", "url-found", {"url": url})
        self.assertEqual((found.status_code, found.json()["existing"]), (200, True))
        self.refused_without_trace("/api/jobs/from-url", "url-found", {"url": other})
        self.assertEqual(self.post("/api/jobs/from-url", "url-found", {"url": url}).json()["existing"], True)

    def test_a_listing_key_is_bound_whether_it_made_a_job_or_found_one(self):
        self.client.post("/api/sources", json={"link": "https://job-boards.greenhouse.io/example"}, headers=self.headers)
        first, second = {"provider": "greenhouse", "board": "example", "job_id": "1"}, \
            {"provider": "greenhouse", "board": "example", "job_id": "2"}
        made = self.post("/api/listings/start", "list-new", first)
        self.assertEqual(self.settle(self.client, self.headers, made)["status"], "succeeded")
        self.refused_without_trace("/api/listings/start", "list-new", second)
        found = self.post("/api/listings/start", "list-found", first)
        self.assertEqual((found.status_code, found.json()["existing"]), (200, True))
        self.refused_without_trace("/api/listings/start", "list-found", second)


class BodyLimitTests(V2Case):
    def test_a_body_is_limited_by_what_arrives_with_or_without_a_length(self):
        client, headers = self.browser()
        as_json = {**headers, "Content-Type": "application/json"}
        small = json.dumps({"stage": "upload_parsing", "version": 1}).encode()
        chunked = client.post("/api/consent", headers=as_json, content=iter([small[:5], small[5:]]))
        self.assertEqual(chunked.status_code, 200, chunked.text)
        self.assertEqual(chunked.request.headers.get("transfer-encoding"), "chunked")
        big = b"%PDF-1.4\n" + b"0" * MAX_PDF_BYTES
        as_pdf = {**headers, "Content-Type": "application/pdf"}
        for sent in (iter([big[:1_000_000], big[1_000_000:]]), big):
            refused = client.post("/api/cv/upload?language=en", headers=as_pdf, content=sent)
            self.assertEqual((refused.status_code, refused.json()["error"]), (413, "The PDF is larger than 5 MB"))
            self.assertEqual(refused.headers["Cache-Control"], "no-store")
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)


class BodyLimitReadingTests(unittest.IsolatedAsyncioTestCase):
    async def read_until_refused(self, path):
        pulled = 0

        async def receive():
            nonlocal pulled
            pulled += 1
            return {"type": "http.request", "body": b"x" * 1000, "more_body": True}

        async def app(scope, receive, send):
            while True:
                await receive()

        with self.assertRaises(RequestTooLarge):
            await BodyLimit(app)({"type": "http", "path": path, "headers": [(b"transfer-encoding", b"chunked")]},
                                 receive, None)
        return pulled

    async def test_reading_stops_at_the_first_chunk_past_the_limit(self):
        self.assertEqual(await self.read_until_refused("/api/consent"), MAX_BODY // 1000 + 1)
        self.assertEqual(await self.read_until_refused("/api/cv/upload"), MAX_PDF_BYTES // 1000 + 1)


class StartupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name) / "data"
        self.data.mkdir()
        self.environ = {"WORKBENCH_OIDC_ISSUER": ISSUER, "WORKBENCH_OIDC_CLIENT_ID": CLIENT_ID,
                        "WORKBENCH_OIDC_DOMAIN": DOMAIN, "WORKBENCH_PUBLIC_ORIGIN": ORIGIN,
                        "WORKBENCH_PUBLIC_HOST": HOST}

    def refused(self, environ):
        import io
        from contextlib import redirect_stderr
        from web_v2 import main
        errors = io.StringIO()
        with redirect_stderr(errors), patch("uvicorn.run") as started:
            code = main(["--data", str(self.data)], environ)
        self.assertFalse(started.called)
        return code, errors.getvalue()

    def test_v2_never_starts_without_cognito_or_on_unmigrated_single_user_data(self):
        code, said = self.refused({})
        self.assertEqual(code, 2)
        self.assertIn("Cognito", said)
        (self.data / "workbench.db").write_bytes(b"")
        code, said = self.refused(self.environ)
        self.assertEqual(code, 2)
        self.assertIn("v2_migrate.py", said)
        self.assertFalse((self.data / "v2.db").exists())

    def test_a_folder_claimed_by_v2_is_refused_by_the_single_user_app(self):
        from web import create_app
        from web_v2 import claim_v2_format
        from workspace import DataFormatError
        claim_v2_format(self.data)
        with self.assertRaises(DataFormatError):
            create_app(facts_db=self.data / "workbench.db", jobs_root=self.data / "jobs", token="t",
                       profile_path=self.data / "cv-profile.json", starter=[])
        (self.data / ".workbench-format").write_text("3\n", encoding="utf-8")
        with self.assertRaises(DataFormatError):
            claim_v2_format(self.data)
