import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import anyio
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from deepseek_client import DeepSeekError
from facts import confirm_facts, import_facts, list_facts, revise_fact
from cv_import import STRUCTURE_RULES
from cv_plan import PLAN_RULES
from gaps import EVIDENCE_RULES, SUGGEST_RULES
from matching import MATCH_RULES
from requirement_flow import FIND_RULES
import run_log
from test_cv import FACTS as CV_FACTS, PROFILE, FakeChat, FakePrinter
from test_gaps import resume_ids, sent_lines
from test_listings import FakeBoards, posting
from test_workspace import PROCESS_DIES, REPO, dies_after_renaming_into, dies_before_renaming_into

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
if HAS_FASTAPI:
    from fastapi.testclient import TestClient

    import web as web_module
    from web import DATA_MARKER, create_app, data_problem, main, server_settings

TOKEN = "test-token"
FACTS = [
    {"id": "fact-web-python", "type": "skill", "text": "Built services in Python.", "tags": ["Python"]},
    {"id": "fact-web-sql", "type": "skill", "text": "Wrote SQL queries.", "tags": ["SQL"]},
]


class FakeDeepSeek(FakeChat):
    """Tailors like FakeChat. For requirement finding it picks the lines listed in
    requirement_lines, for CV planning it returns ``plan``, for the evidence check the verdicts
    ``evidence`` gives for a request, and for gap suggestions what ``gap_suggestions`` gives;
    while one is None that request fails like an unreachable API. Fact matching always fails
    that way, so the tag-matching fallback is what these tests see."""

    def __init__(self, rewrites=None):
        super().__init__(rewrites)
        self.requirement_lines = None
        self.plan = None
        self.gap_suggestions = None
        self.evidence = None
        self.cv_structure = None
        self.tailor_error = None
        self.while_planning = None  # runs while DeepSeek "adjusts" a CV, to hold a request open
        self.sent = []
        self.systems = []

    def __call__(self, messages, model, effort):
        self.sent.append(messages[-1]["content"])
        self.systems.append(messages[0]["content"])
        if self.tailor_error and messages[0]["content"].startswith("You rewrite resume lines"):
            raise self.tailor_error
        if messages[0]["content"] == STRUCTURE_RULES:
            if self.cv_structure is None:
                raise DeepSeekError("测试中不联网", reason="unreachable")
            return {"model": "deepseek-flash", "content": self.cv_structure, "usage": {}}
        if messages[0]["content"] == EVIDENCE_RULES:
            if self.evidence is None:
                raise DeepSeekError("测试中不联网", reason="unreachable")
            request = json.loads(messages[-1]["content"])
            return {"model": "deepseek-flash", "content": {"requirements": self.evidence(request)}, "usage": {}}
        if messages[0]["content"] == SUGGEST_RULES:
            if self.gap_suggestions is None:
                raise DeepSeekError("测试中不联网", reason="unreachable")
            request = json.loads(messages[-1]["content"])
            ids = {item["text"]: item["id"] for item in request["gaps"]}
            return {"model": "deepseek-flash", "content": {"suggestions": resume_ids(self.gap_suggestions(ids), request)},
                    "usage": {}}
        if messages[0]["content"] == MATCH_RULES or (messages[0]["content"] == PLAN_RULES and self.plan is None):
            raise DeepSeekError("测试中不联网", reason="unreachable")
        if messages[0]["content"] == PLAN_RULES:
            if self.while_planning:
                self.while_planning()
            return {"model": "deepseek-flash", "content": resume_ids(self.plan, json.loads(messages[-1]["content"])),
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        if messages[0]["content"] != FIND_RULES:
            return super().__call__(messages, model, effort)
        if self.requirement_lines is None:
            raise DeepSeekError("测试中不联网", reason="unreachable")
        lines = json.loads(messages[-1]["content"])["lines"]
        picks = [{"line": line["n"], "kind": "required"} for line in lines
                 if line["text"].lstrip("- ") in self.requirement_lines]
        return {"model": "deepseek-flash", "content": {"requirements": picks},
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class Gate:
    """Stands in for a slow DeepSeek call: records each request that reaches it and holds it
    there until released, counting how many are inside at once."""

    def __init__(self):
        self.arrived = threading.Semaphore(0)
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.inside = self.most = self.count = 0

    def __call__(self):
        with self.guard:
            self.inside += 1
            self.count += 1
            self.most = max(self.most, self.inside)
        self.arrived.release()
        self.release.wait(10)
        with self.guard:
            self.inside -= 1


class WatchedLock:
    """Stands in for the storage lock, and tells when a thread has to wait for it."""

    def __init__(self):
        self.lock = threading.RLock()
        self.waiting = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        if self.lock.acquire(blocking=False):
            return True
        self.waiting.set()
        return self.lock.acquire(blocking, timeout)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exception):
        self.release()


def wait_until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("waited in vain")
        time.sleep(0.01)


async def asgi_post(app, path, body_ends=True):
    """Send one POST straight to the ASGI app, as the server would, from a client that stays;
    with body_ends=False the client sends part of a body and then nothing more. Returns the
    status code."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"127.0.0.1:8765"), (b"x-workbench-token", TOKEN.encode()),
                         (b"content-type", b"application/pdf" if not body_ends else b"application/json")],
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765)}
    body_sent, status = False, []

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"%PDF-1.4" if not body_ends else b"", "more_body": not body_ends}
        await anyio.sleep_forever()

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(message["status"])

    await app(scope, receive, send)
    return status[0] if status else None


@unittest.skipUnless(HAS_FASTAPI, "web tests need the packages in requirements.txt")
class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database = root / "workbench.db"
        profile = root / "cv-profile.json"
        profile.write_text(json.dumps(PROFILE, ensure_ascii=False), encoding="utf-8")
        self.profile_path = profile
        self.chat = FakeDeepSeek({"fact-intern-api": "For an internal tool, built REST APIs."})
        self.boards = FakeBoards([
            posting("1", "Data Engineer", "Requirements:\n- Python and SQL"),
            posting("2", "Designer", "Figma."),
        ])
        self.selected = []
        app = create_app(
            facts_db=self.database, jobs_root=root / "jobs", token=TOKEN,
            profile_path=profile, chat=self.chat, printer=FakePrinter(),
            starter=[], boards=self.boards, selected_posting=self.read_posting,
        )
        self.app = app
        self.client = TestClient(app, base_url="http://127.0.0.1:8765")
        self.headers = {"X-Workbench-Token": TOKEN}

    def tearDown(self):
        self.directory.cleanup()

    def read_posting(self, board, job_id, provider="greenhouse"):
        """The posting as its board would return it now; no network."""
        self.selected.append((provider, board, job_id))
        job = next(job for job in self.boards.jobs if job["job_id"] == job_id)
        return {"jd": {key: value for key, value in job.items()}, "source_status": "测试：公开接口返回"}

    def test_only_local_requests_with_the_page_token_reach_the_api(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(TOKEN, page.text)
        self.assertEqual(self.client.get("/api/facts").status_code, 403)
        rebinding = self.client.get("/api/facts", headers={**self.headers, "Host": "evil.example"})
        self.assertEqual(rebinding.status_code, 403)
        self.assertEqual(self.client.get("/api/facts", headers=self.headers).json(), {"facts": []})

    def test_health_says_ok_without_a_token_or_any_personal_data(self):
        response = self.client.get("/healthz")
        self.assertEqual((response.status_code, response.json()), (200, {"status": "ok"}))
        self.assertEqual(self.client.get("/healthz", headers={"Host": "evil.example"}).status_code, 403)
        self.directory.cleanup()  # the data folder is gone
        self.assertEqual(self.client.get("/healthz").status_code, 503)

    def test_the_access_log_never_holds_the_page_token(self):
        # Preview and download links carry the token in their address; logs keep only the route.
        with self.assertLogs("workbench.access", level="INFO") as logs:
            self.client.get(f"/preview/20260101-000000-abcdef/en?token={TOKEN}")
            self.client.get("/api/facts", headers=self.headers)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("route=/preview/{job_id}/{language} job_id=20260101-000000-abcdef", logs.output[0])
        self.assertFalse(any(TOKEN in line for line in logs.output))

    def test_facts_page_confirms_only_the_selected_versions(self):
        import_facts(self.database, FACTS)
        confirmed = self.client.post(
            "/api/facts/confirm", json={"refs": ["fact-web-python@1"]}, headers=self.headers
        )
        facts = self.client.get("/api/facts", headers=self.headers).json()["facts"]
        stale = self.client.post(
            "/api/facts/confirm", json={"refs": ["fact-web-sql@2"]}, headers=self.headers
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(
            {fact["id"]: fact["status"] for fact in facts},
            {"fact-web-python": "confirmed", "fact-web-sql": "pending"},
        )
        self.assertEqual(stale.status_code, 400)
        self.assertIn("只能确认当前版本", stale.json()["error"])

    def create_job(self):
        text = (Path(__file__).parent / "examples" / "synthetic_jd_zh.txt").read_text(encoding="utf-8")
        response = self.client.post(
            "/api/jobs",
            json={"title": "后端开发实习生", "company": "示例公司", "text": text},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["job_id"]

    def job(self, job_id):
        return self.client.get(f"/api/jobs/{job_id}", headers=self.headers).json()

    def test_pasted_job_is_created_with_its_requirements_found(self):
        job_id = self.create_job()
        view = self.job(job_id)
        listed = self.client.get("/api/jobs", headers=self.headers).json()["jobs"]
        self.assertEqual(view["jd"]["title"], "后端开发实习生")
        self.assertEqual(
            [item["text"] for item in view["candidates"]],
            ["本科及以上学历，计算机相关专业", "熟悉Python，了解SQL", "每周至少实习4天"],
        )
        self.assertEqual([job["job_id"] for job in listed], [job_id])

    def test_missed_quotes_and_requirement_decisions_are_saved(self):
        job_id = self.create_job()
        added = self.client.post(
            f"/api/jobs/{job_id}/requirements/add", json={"text": "有开源项目经历"}, headers=self.headers
        )
        invented = self.client.post(
            f"/api/jobs/{job_id}/requirements/add", json={"text": "精通 Kubernetes"}, headers=self.headers
        )
        ids = {item["text"]: item["id"] for item in self.job(job_id)["candidates"]}
        decided = self.client.post(
            f"/api/jobs/{job_id}/requirements/decide",
            json={
                "confirm": [ids["熟悉Python，了解SQL"], ids["有开源项目经历"]],
                "exclude": [ids["每周至少实习4天"]],
            },
            headers=self.headers,
        )
        view = self.job(job_id)
        bad_id = self.client.get("/api/jobs/../../etc", headers=self.headers)
        self.assertEqual((added.status_code, invented.status_code), (200, 400))
        self.assertIn("不是 JD 原文片段", invented.json()["error"])
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(
            [item["text"] for item in view["selected_requirements"]], ["熟悉Python，了解SQL", "有开源项目经历"]
        )
        self.assertIn(bad_id.status_code, (400, 404))

    def test_requirements_counted_automatically_stay_marked_so_until_the_user_decides(self):
        job_id = self.create_job()
        automatic = self.job(job_id)
        self.client.post(f"/api/jobs/{job_id}/requirements/add", json={"text": "有开源项目经历"}, headers=self.headers)
        ids = {item["text"]: item["id"] for item in self.job(job_id)["candidates"]}
        self.client.post(f"/api/jobs/{job_id}/requirements/decide", headers=self.headers, json={
            "confirm": [ids["熟悉Python，了解SQL"], ids["有开源项目经历"]], "exclude": [ids["每周至少实习4天"]]})
        self.client.post(f"/api/jobs/{job_id}/requirements/add", json={"text": "参与后端服务开发与测试"}, headers=self.headers)
        later = self.job(job_id)
        by_text = lambda view: {item["text"]: (item["status"], item["decided_by"]) for item in view["candidates"]}
        self.assertEqual(set(by_text(automatic).values()), {("confirmed", "auto")})
        self.assertEqual({item["text"]: item["strength"] for item in automatic["candidates"]},
                         {"本科及以上学历，计算机相关专业": "required", "熟悉Python，了解SQL": "required", "每周至少实习4天": "required"})
        self.assertEqual(by_text(later), {
            "本科及以上学历，计算机相关专业": ("confirmed", "auto"),  # left out of the review: counted automatically
            "熟悉Python，了解SQL": ("confirmed", "user"), "有开源项目经历": ("confirmed", "user"),
            "每周至少实习4天": ("excluded", "user"), "参与后端服务开发与测试": ("confirmed", "user"),
        })
        self.assertEqual({item["text"]: item["strength"] for item in later["selected_requirements"]}["有开源项目经历"], "unclear")

    def test_private_details_inside_facts_never_reach_deepseek(self):
        # Found in review by Codex: a fact copied from an uploaded CV can hold the name or a link,
        # and every later request built from CV lines must still leave them out.
        paper = {"id": "fact-paper", "type": "achievement", "tags": ["parser"],
                 "text": "Alex Example and Sam Lee. Parser design. https://alex.example.com/paper. alex@example.com"}
        demo = {"id": "fact-demo", "type": "experience", "tags": ["parser"],
                "text": "Showed the parser demo to the Example University robotics club."}
        import_facts(self.database, CV_FACTS + [paper, demo])
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS + [paper, demo]])
        profile = json.loads(json.dumps(PROFILE))
        profile["sections"][1]["entries"][0]["facts"].append("fact-demo")
        profile["sections"].append({"kind": "publications", "entries": [{"facts": ["fact-paper"]}]})
        self.profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        self.chat.plan = {"sections": ["education", "experience", "skills", "publications"], "entries": [], "reasons": []}
        self.chat.gap_suggestions = lambda ids: []
        self.chat.evidence = lambda request: [{"id": item["id"], "verdict": "none"} for item in request["requirements"]]
        job_id = self.client.post("/api/jobs", headers=self.headers,
                                  json={"title": "Parser Intern", "text": "Requirements:\n- Parser design\n- Kubernetes operations"}).json()["job_id"]
        self.client.post(f"/api/jobs/{job_id}/gaps", headers=self.headers)
        self.client.post(f"/api/jobs/{job_id}/matches/propose", headers=self.headers)
        sent = "\n".join(self.chat.sent)
        preview = self.client.get(f"/preview/{job_id}/en?token={TOKEN}").text
        # The evidence check and gap suggestions were both asked for.
        self.assertTrue({EVIDENCE_RULES, SUGGEST_RULES} <= set(self.chat.systems))
        self.assertIn("Parser design", sent)  # the rest of the line still counts
        for private in ("Alex", "alex.example.com", "alex@example.com", "Example University", "Example Corp", "Los Angeles"):
            self.assertNotIn(private, sent)
        self.assertIn("Alex Example and Sam Lee. Parser design.", preview)  # the CV itself is unchanged
        # Found in review of PR #2 by Codex: the Chinese CV masked only the Chinese names, and a
        # fact from an earlier CV named an employer the current CV no longer lists.
        history = self.profile_path.parent / "profile-history"
        history.mkdir(exist_ok=True)
        (history / "cv-profile-20260101T000000000000.json").write_text(json.dumps(
            {**PROFILE, "sections": [{"kind": "experience", "entries": [{"title": "Acme Corporation", "facts": []}]}]}), encoding="utf-8")
        old = {"id": "fact-acme", "type": "experience", "tags": ["Python"], "text": "Built Python tools at Acme Corporation."}
        import_facts(self.database, [old])
        confirm_facts(self.database, [("fact-acme", 1)])
        self.chat.sent.clear()
        self.client.post(f"/api/jobs/{job_id}/cv/zh/prepare", headers=self.headers)
        self.client.post(f"/api/jobs/{job_id}/gaps", headers=self.headers)
        self.client.post(f"/api/jobs/{job_id}/matches/propose", headers=self.headers)
        later = "\n".join(self.chat.sent)
        self.assertTrue(later)
        for private in ("Alex", "Example University", "Acme Corporation"):
            self.assertNotIn(private, later)

    @unittest.skipUnless(HAS_PYPDF, "reading PDFs needs pypdf from requirements.txt")
    def test_a_cancelled_or_forgotten_upload_leaves_no_personal_data_behind(self):
        from test_cv_import import minimal_pdf

        uploads = self.profile_path.parent / "cv-uploads"
        uploads.mkdir()
        forgotten = uploads / "0123456789abcdef.json"
        forgotten.write_text("{}", encoding="utf-8")
        os.utime(forgotten, (0, 0))  # an upload never saved or cancelled, long ago
        self.chat.cv_structure = {"sections": [{"kind": "skills", "heading": 2, "entries": [{"facts": [{"lines": [3], "tags": []}]}]}]}
        pdf = minimal_pdf([(72, 740, "ALEX EXAMPLE"), (72, 700, "SKILLS"), (72, 686, "Languages: Python, Java")])
        upload = self.client.post("/api/cv/upload", content=pdf, headers={**self.headers, "Content-Type": "application/pdf"}).json()
        cancelled = self.client.delete(f"/api/cv/uploads/{upload['upload_id']}", headers=self.headers)
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(list(uploads.glob("*.json")), [])

    @unittest.skipUnless(HAS_PYPDF, "reading PDFs needs pypdf from requirements.txt")
    def test_an_upload_still_works_when_another_one_removed_an_old_upload_first(self):
        from test_cv_import import minimal_pdf

        uploads = self.profile_path.parent / "cv-uploads"
        uploads.mkdir()
        # A link to nothing: reading its age fails as it does for an old upload that another
        # upload removed after this one listed the folder.
        (uploads / "0123456789abcdef.json").symlink_to(uploads / "removed.json")
        self.chat.cv_structure = {"sections": [{"kind": "skills", "heading": 2, "entries": [{"facts": [{"lines": [3], "tags": []}]}]}]}
        pdf = minimal_pdf([(72, 740, "ALEX EXAMPLE"), (72, 700, "SKILLS"), (72, 686, "Languages: Python, Java")])
        upload = self.client.post("/api/cv/upload", content=pdf, headers={**self.headers, "Content-Type": "application/pdf"})
        self.assertEqual(upload.status_code, 200, upload.text)

    def test_each_request_is_one_line_without_its_query_string(self):
        job_id = self.planned_job()
        with self.assertLogs("workbench.access", level="INFO") as logs:
            preview = self.client.get(f"/preview/{job_id}/en?token={TOKEN}&v=stale")
            self.client.get("/healthz")  # a passing health check is not logged
        [record] = logs.records
        line = json.loads(run_log.JsonLines().format(record))
        self.assertEqual({key: line[key] for key in ("event", "method", "route", "job_id", "status")},
                         {"event": "request", "method": "GET", "route": "/preview/{job_id}/{language}", "job_id": job_id,
                          "status": 409})
        self.assertEqual(line["request_id"], preview.headers["X-Request-Id"])
        self.assertIsInstance(line["duration_ms"], int)
        self.assertNotIn(TOKEN, json.dumps(line))

    def test_a_path_the_sender_chose_never_reaches_a_log(self):
        with self.assertLogs("workbench.access", level="INFO") as logs:
            self.client.get("/api/jobs/Alex-Example-at-Acme")  # no token: refused before it is routed
            self.client.get("/api/jobs/Alex-Example-at-Acme", headers=self.headers)  # routed, not a job ID
            self.client.get("/Alex-Example-at-Acme")
        lines = [json.loads(run_log.JsonLines().format(record)) for record in logs.records]
        self.assertEqual([(line["route"], line["status"]) for line in lines],
                         [("/api/jobs/{job_id}", 403), ("/api/jobs/{job_id}", 400), ("(no route)", 404)])
        self.assertNotIn("Alex", json.dumps(lines))

    def test_a_method_the_sender_made_up_never_reaches_a_log(self):
        with self.assertLogs("workbench.access", level="INFO") as logs:
            self.client.request("ALEX-EXAMPLE", "/api/jobs", headers=self.headers)
            self.client.request("ALEX-EXAMPLE", "/api/jobs")  # no token: refused before it is routed
        lines = [json.loads(run_log.JsonLines().format(record)) for record in logs.records]
        self.assertEqual([line["method"] for line in lines], ["(other)", "(other)"])
        self.assertNotIn("ALEX", json.dumps(lines))

    def test_an_unexpected_error_is_logged_by_its_type_never_its_words(self):
        job_id = self.planned_job()

        def broken(*args, **kwargs):
            raise RuntimeError("Alex Example, Acme Corporation")  # words from a CV, in an error nobody expected

        client = TestClient(self.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
        with patch("web.plan_draft", broken), self.assertLogs("workbench.access", level="ERROR") as logs:
            response = client.post(f"/api/jobs/{job_id}/cv/en/plan", headers=self.headers)
        self.assertEqual(response.status_code, 500)
        [line] = [json.loads(run_log.JsonLines().format(record)) for record in logs.records]
        self.assertEqual((line["event"], line["route"], line["status"], line["error"]),
                         ("request", "/api/jobs/{job_id}/cv/{language}/plan", 500, "RuntimeError"))
        self.assertEqual(response.headers.get("X-Request-Id"), line["request_id"])  # the page can quote it
        self.assertNotIn("Alex", response.text)
        self.assertRegex(line["at"], r"^test_web\.py:\d+$")
        self.assertNotIn("Alex", json.dumps(line))

    def test_each_stage_is_logged_with_its_outcome_and_time_never_its_text(self):
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        self.chat.tailor_error = DeepSeekError("DeepSeek answered: Alex Example", reason="rate_limited")
        with self.assertLogs("workbench.stages", level="INFO") as logs:
            self.client.post("/api/jobs", json={"title": "Backend Intern", "text": "Requirements:\n- Python and SQL"},
                             headers=self.headers)
        lines = [json.loads(run_log.JsonLines().format(record)) for record in logs.records]
        self.assertEqual([(line["stage"], line["status"], line["reason"]) for line in lines],
                         [("draft", "done", None), ("rewording", "fallback", "rate_limited"), ("layout", "fallback", "unreachable")])
        self.assertTrue(all(isinstance(line["duration_ms"], int) and line["language"] == "en" for line in lines))
        self.assertEqual(len({line["request_id"] for line in lines}), 1)  # all from the one request
        allowed = {"time", "level", "logger", "event", "request_id", "job_id", "language", "stage", "status", "reason",
                   "duration_ms"}
        self.assertTrue(all(set(line) <= allowed for line in lines), lines)  # no message, nothing from the CV
        self.assertNotIn("Alex", json.dumps(lines))

    def test_nothing_is_sent_when_the_private_details_cannot_be_read(self):
        # Found in review by Codex: an unreadable profile gave an empty list of private words,
        # which replaced the CV's own, so an employer went out verbatim.
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        job_id = self.client.post("/api/jobs", headers=self.headers,
                                  json={"title": "Backend Intern", "text": "Requirements:\n- Python and SQL"}).json()["job_id"]
        self.profile_path.write_text("{ not json", encoding="utf-8")
        self.chat.sent.clear()
        retry = self.client.post(f"/api/jobs/{job_id}/cv/en/plan", headers=self.headers)
        self.assertEqual(retry.status_code, 400)
        self.assertEqual(self.chat.sent, [])
        # A broken backup stops requests too, and the page names the file to delete or fix.
        self.profile_path.write_text(json.dumps(PROFILE, ensure_ascii=False), encoding="utf-8")
        history = self.profile_path.parent / "profile-history"
        history.mkdir(exist_ok=True)
        (history / "cv-profile-20260101T000000000000.json").write_text("[", encoding="utf-8")
        again = self.client.post(f"/api/jobs/{job_id}/cv/en/plan", headers=self.headers)
        layout = next(stage for stage in self.job(job_id)["cv"]["en"]["stages"] if stage["stage"] == "layout")
        self.assertEqual((again.status_code, self.chat.sent), (400, []))
        self.assertIn("cv-profile-20260101T000000000000.json", layout["message"])

    @unittest.skipUnless(HAS_PYPDF, "reading PDFs needs pypdf from requirements.txt")
    def test_uploading_a_cv_again_repairs_an_unreadable_layout(self):
        # Found in review by Codex: the broken file went into the backups and kept blocking.
        from test_cv_import import minimal_pdf

        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        self.profile_path.write_text("{ broken", encoding="utf-8")
        self.chat.cv_structure = {"sections": [{"kind": "skills", "heading": 2, "entries": [{"facts": [{"lines": [3], "tags": []}]}]}]}
        pdf = minimal_pdf([(72, 740, "ALEX EXAMPLE"), (72, 700, "SKILLS"), (72, 686, "Languages: Python, Java")])
        upload = self.client.post("/api/cv/upload", content=pdf, headers={**self.headers, "Content-Type": "application/pdf"}).json()
        saved = self.client.post(f"/api/cv/uploads/{upload['upload_id']}/save", headers=self.headers,
                                 json={"name": "Alex Example", "links": []})
        job = self.client.post("/api/jobs", headers=self.headers,
                               json={"title": "Backend Intern", "text": "Requirements:\n- Experience with Python and SQL"})
        stages = self.job(job.json()["job_id"])["cv"]["en"]["stages"]
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(stages[0]["status"], "done")
        self.assertFalse({"profile_unreadable", "backup_unreadable"} & {stage["reason_code"] for stage in stages})
        self.assertEqual(len(list((self.profile_path.parent / "profile-history").glob("*.broken"))), 1)  # kept aside

    def test_a_failed_retry_says_the_earlier_result_is_kept(self):
        # Found in review of PR #2 by Codex: a failed "Adjust again" said the usual layout was
        # used while the earlier adjusted layout was still shown.
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        self.chat.plan = {"sections": ["education", "experience", "skills"], "entries": [], "reasons": []}
        job_id = self.client.post("/api/jobs", headers=self.headers,
                                  json={"title": "Backend Intern", "text": "Requirements:\n- Python and SQL"}).json()["job_id"]
        self.chat.plan = None
        again = self.client.post(f"/api/jobs/{job_id}/cv/en/plan", headers=self.headers)
        cv = self.job(job_id)["cv"]["en"]
        layout = next(stage for stage in cv["stages"] if stage["stage"] == "layout")
        self.assertEqual(again.status_code, 400)
        self.assertEqual((cv["head"], layout["status"], layout["output_available"]), ("planned", "fallback", True))
        self.assertIn("earlier adjusted layout", layout["message"])

    def test_the_cv_says_which_stage_did_not_work_and_why(self):
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        self.chat.tailor_error = DeepSeekError("key sk-test-secret was refused", reason="key_rejected")
        job_id = self.client.post("/api/jobs", headers=self.headers,
                                  json={"title": "Backend Intern", "text": "Requirements:\n- Python and SQL"}).json()["job_id"]
        failed = self.job(job_id)["cv"]["en"]
        self.chat.tailor_error = None
        self.chat.plan = {"sections": ["education", "experience", "skills"], "entries": [], "reasons": []}
        self.client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=self.headers)
        fixed = self.job(job_id)["cv"]["en"]
        stages = lambda cv: [(item["stage"], item["status"], item["reason_code"], item["output_available"]) for item in cv["stages"]]
        self.assertEqual(failed["head"], "draft")
        self.assertEqual(stages(failed), [("draft", "done", None, True), ("rewording", "fallback", "key_rejected", True),
                                          ("layout", "fallback", "unreachable", True)])
        self.assertIn("refused the API key", failed["stages"][1]["message"])
        self.assertNotIn("sk-test-secret", json.dumps(failed))
        self.assertEqual(stages(fixed), [("draft", "done", None, True), ("rewording", "done", None, True),
                                         ("layout", "done", None, True)])

    def test_a_cv_that_cannot_be_drafted_says_what_to_do(self):
        import_facts(self.database, CV_FACTS)  # imported, not confirmed yet
        job_id = self.client.post("/api/jobs", headers=self.headers,
                                  json={"title": "Backend Intern", "text": "Requirements:\n- Python and SQL"}).json()["job_id"]
        cv = self.job(job_id)["cv"]["en"]
        self.assertIsNone(cv["head"])
        self.assertEqual([(item["stage"], item["status"], item["reason_code"]) for item in cv["stages"]],
                         [("draft", "failed", "facts_not_confirmed"), ("rewording", "skipped", None), ("layout", "skipped", None)])
        self.assertIn("Facts page", cv["stages"][0]["message"])
        # Once a CV exists, a later failure is shown next to it instead of being hidden.
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        self.client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=self.headers)
        revise_fact(self.database, "fact-skills-languages", text="Languages: Python, Java, Go")
        again = self.client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=self.headers)
        cv = self.job(job_id)["cv"]["en"]
        self.assertEqual(again.status_code, 400)
        self.assertEqual((cv["head"], cv["stages"][0]["status"], cv["stages"][0]["output_available"]), ("tailored", "failed", True))
        self.assertIn("last time it could be prepared", cv["stages"][0]["message"])

    def decided_job(self):
        job_id = self.create_job()
        ids = {item["text"]: item["id"] for item in self.job(job_id)["candidates"]}
        self.client.post(
            f"/api/jobs/{job_id}/requirements/decide",
            json={"confirm": [ids["熟悉Python，了解SQL"], ids["每周至少实习4天"]]},
            headers=self.headers,
        )
        return job_id, ids

    def test_matching_offers_only_confirmed_facts_and_builds_the_evidence_report(self):
        import_facts(self.database, FACTS)
        self.client.post("/api/facts/confirm", json={"refs": ["fact-web-python@1"]}, headers=self.headers)
        job_id, ids = self.decided_job()
        proposed = self.client.post(f"/api/jobs/{job_id}/matches/propose", headers=self.headers).json()
        offered = {item["requirement_id"]: [fact["fact_id"] for fact in item["candidates"]]
                   for item in proposed["matching"]["requirements"]}
        python, availability = ids["熟悉Python，了解SQL"], ids["每周至少实习4天"]
        linked = self.client.post(
            f"/api/jobs/{job_id}/matches/decide",
            json={"links": {python: "fact-web-python"}, "no_match": [availability]},
            headers=self.headers,
        ).json()
        quotes = {item["requirement_quote"]: item["fact_quote"] for item in linked["report"]["items"]}
        self.assertEqual(offered, {python: ["fact-web-python"], availability: []})
        self.assertEqual(quotes, {"熟悉Python，了解SQL": "Built services in Python.", "每周至少实习4天": None})

    def cv_step(self, job_id, action):
        response = self.client.post(f"/api/jobs/{job_id}/cv/en/{action}", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["cv"]["en"]

    def test_cv_goes_from_draft_to_reviewed_rewrites_to_an_approved_download(self):
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        job_id = self.create_job()
        view = self.job(job_id)
        # A Chinese posting gets its Chinese CV at once; the English one comes on request.
        self.assertEqual((view["language"], view["cv"]["zh"]["head"], view["cv"]["en"]["head"]), ("zh", "tailored", None))
        prepared = self.cv_step(job_id, "prepare")
        preview = self.client.get(f"/preview/{job_id}/en?token={TOKEN}&v={prepared['content_sha256']}")
        approved = self.approve(job_id, prepared["content_sha256"]).json()["cv"]["en"]
        final = self.cv_step(job_id, "export")
        download = self.client.get(f"/download/{job_id}/en.pdf?token={TOKEN}")
        self.assertEqual(prepared["head"], "tailored")  # planning is offline in these tests
        self.assertEqual(prepared["rewrites"], [{
            "fact_id": "fact-intern-api",
            "from": "Built REST APIs for an internal tool.",
            "to": "For an internal tool, built REST APIs.",
            "undone": False,
        }])
        self.assertIn('class="watermark"', preview.text)
        self.assertEqual(approved["head"], "approved")
        self.assertTrue(final["final_pdf"])
        self.assertEqual(download.headers["content-type"], "application/pdf")
        self.assertTrue(download.content.startswith(b"%PDF"))

    def test_missing_chinese_profile_text_is_named_in_plain_words(self):
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        job_id = self.create_job()
        response = self.client.post(f"/api/jobs/{job_id}/cv/zh/draft", headers=self.headers)
        self.assertEqual(
            response.json()["cv"]["zh"]["language_fallbacks"],
            ["Education role or degree: M.S. in Computer Science"],
        )

    def test_preview_and_download_need_the_token_and_a_local_host(self):
        job_id = self.create_job()
        self.assertEqual(self.client.get(f"/preview/{job_id}/en").status_code, 403)
        rebinding = self.client.get(f"/preview/{job_id}/en?token={TOKEN}", headers={"Host": "evil.example"})
        self.assertEqual(rebinding.status_code, 403)
        self.assertEqual(self.client.get(f"/download/{job_id}/en.pdf?token=wrong").status_code, 403)


    def test_a_followed_company_is_ranked_by_confirmed_skills(self):
        import_facts(self.database, FACTS)
        self.client.post("/api/facts/confirm", json={"refs": ["fact-web-python@1", "fact-web-sql@1"]}, headers=self.headers)
        added = self.client.post("/api/sources", json={"link": "https://boards.greenhouse.io/example"}, headers=self.headers)
        wrong = self.client.post("/api/sources", json={"link": "https://example.com/careers"}, headers=self.headers)
        ranked = self.client.get("/api/listings", headers=self.headers).json()
        refreshed = self.client.post("/api/sources/greenhouse/example/refresh", headers=self.headers)
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("jobs.lever.co", wrong.json()["error"])
        self.assertEqual([(item["title"], item["matched"]) for item in ranked["listings"]],
                         [("Data Engineer", ["Python", "SQL"]), ("Designer", [])])
        self.assertEqual(ranked["skill_count"], 2)
        self.assertEqual(refreshed.json()["open"], 2)
        self.assertEqual(self.boards.calls, [("greenhouse", "example")] * 2)
        removed = self.client.delete("/api/sources/greenhouse/example", headers=self.headers)
        self.assertEqual(removed.json()["sources"], [])
        self.assertEqual(self.client.get("/api/listings", headers=self.headers).json()["listings"], [])

    def test_starting_a_listing_reads_it_again_and_never_makes_a_second_job(self):
        self.client.post("/api/sources", json={"link": "https://boards.greenhouse.io/example"}, headers=self.headers)
        first = self.client.post("/api/listings/start", json={"provider": "greenhouse", "board": "example", "job_id": "1"}, headers=self.headers)
        again = self.client.post("/api/listings/start", json={"provider": "greenhouse", "board": "example", "job_id": "1"}, headers=self.headers)
        job_id = first.json()["job_id"]
        view = self.job(job_id)
        ranked = self.client.get("/api/listings", headers=self.headers).json()["listings"]
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(again.json()["job_id"], job_id)
        self.assertEqual(self.selected, [("greenhouse", "example", "1")])
        self.assertEqual(view["jd"]["company"], "Example")
        self.assertEqual([item["text"] for item in view["candidates"]], ["Python and SQL"])
        self.assertEqual({item["title"]: item["started_job"] for item in ranked}, {"Data Engineer": job_id, "Designer": None})


    def test_a_started_job_is_ready_for_cv_review_without_clicks(self):
        import_facts(self.database, FACTS + CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in FACTS + CV_FACTS])
        self.chat.requirement_lines = {"Python and SQL"}
        self.client.post("/api/sources", json={"link": "https://boards.greenhouse.io/example"}, headers=self.headers)
        started = self.client.post("/api/listings/start", json={"provider": "greenhouse", "board": "example", "job_id": "1"}, headers=self.headers)
        job_id = started.json()["job_id"]
        view = self.job(job_id)
        self.assertEqual(
            [(item["text"], item["status"], item["extraction_method"]) for item in view["candidates"]],
            [("Python and SQL", "confirmed", "deepseek-lines-v1")],
        )
        self.assertEqual((view["language"], view["cv"]["en"]["head"], view["cv"]["zh"]["head"]), ("en", "tailored", None))
        self.assertNotIn("matching", view)  # talking points are made only on request
        self.assertEqual(view["extraction"]["method"], "deepseek-lines-v1")
        # Changing a requirement decision prepares the CV again by itself.
        self.client.post(f"/api/jobs/{job_id}/requirements/decide",
                         json={"confirm": [view["candidates"][0]["id"]]}, headers=self.headers)
        self.assertTrue({"decided", "cv-draft-en", "cv-tailored-en"} <= set(self.job(job_id)["steps"]))

    def test_the_cv_is_adjusted_for_the_job_and_each_change_can_be_undone(self):
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        self.chat.plan = {
            "sections": ["education", "skills", "experience"],
            "entries": [{"entry": "s1e0", "lines": ["fact-intern-tests"]}],
            "reasons": [{"target": "fact-intern-api", "reason": "REST APIs are not asked for."}],
        }
        job_id = self.client.post(
            "/api/jobs", json={"title": "Test Engineer", "text": "Requirements:\n- Writing unit tests"}, headers=self.headers,
        ).json()["job_id"]
        cv = self.job(job_id)["cv"]["en"]
        before = self.client.get(f"/preview/{job_id}/en?token={TOKEN}").text
        undone = self.client.post(f"/api/jobs/{job_id}/cv/en/change",
                                  json={"change_id": "cut:fact-intern-api", "undone": True}, headers=self.headers)
        after = self.client.get(f"/preview/{job_id}/en?token={TOKEN}").text
        talking = self.client.post(f"/api/jobs/{job_id}/matches/propose", headers=self.headers)
        self.assertEqual(cv["head"], "planned")
        self.assertEqual({item["id"]: (item["reason"], item["undone"]) for item in cv["changes"]}, {
            "order:sections": (None, False),
            "cut:fact-intern-api": ("REST APIs are not asked for.", False),
        })
        self.assertNotIn("REST APIs", before)
        self.assertIn("REST APIs", after)
        self.assertTrue(next(item for item in undone.json()["cv"]["en"]["changes"] if item["id"] == "cut:fact-intern-api")["undone"])
        # Talking points are optional extra reading: making them leaves the CV alone.
        self.assertEqual(talking.status_code, 200, talking.text)
        self.assertIn("cv-planned-en", talking.json()["steps"])

    def planned_job(self):
        """A job whose English CV is adjusted for it, with one cut line that can be put back."""
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        self.chat.plan = {
            "sections": ["education", "skills", "experience"],
            "entries": [{"entry": "s1e0", "lines": ["fact-intern-tests"]}],
            "reasons": [{"target": "fact-intern-api", "reason": "REST APIs are not asked for."}],
        }
        return self.client.post(
            "/api/jobs", json={"title": "Test Engineer", "text": "Requirements:\n- Writing unit tests"}, headers=self.headers,
        ).json()["job_id"]

    def approve(self, job_id, fingerprint, language="en"):
        return self.client.post(f"/api/jobs/{job_id}/cv/{language}/approve",
                                json={"expected_content_sha256": fingerprint}, headers=self.headers)

    def put_back_the_cut_line(self, job_id):
        self.client.post(f"/api/jobs/{job_id}/cv/en/change",
                         json={"change_id": "cut:fact-intern-api", "undone": True}, headers=self.headers)

    def test_approving_approves_only_the_cv_the_page_showed(self):
        job_id = self.planned_job()
        shown = self.job(job_id)["cv"]["en"]["content_sha256"]  # tab A shows the CV
        self.put_back_the_cut_line(job_id)  # meanwhile tab B changes it
        stale = self.approve(job_id, shown)  # tab A approves what it showed
        unnamed = self.client.post(f"/api/jobs/{job_id}/cv/en/approve", headers=self.headers)
        now = self.job(job_id)["cv"]["en"]
        self.assertEqual(stale.status_code, 409)
        self.assertIn("changed", stale.json()["error"])
        self.assertEqual(unnamed.status_code, 422)  # an approval must say which CV it is for
        self.assertEqual(now["head"], "planned")
        self.assertNotEqual(now["content_sha256"], shown)
        approved = self.approve(job_id, now["content_sha256"])  # after reading it again
        self.assertEqual(approved.json()["cv"]["en"]["head"], "approved")

    def test_a_second_click_on_approve_changes_nothing(self):
        job_id = self.planned_job()
        shown = self.job(job_id)["cv"]["en"]["content_sha256"]
        first, second = self.approve(job_id, shown), self.approve(job_id, shown)
        self.assertEqual((first.status_code, second.status_code), (200, 200), second.text)
        self.assertEqual(second.json()["cv"]["en"]["approved_at"], first.json()["cv"]["en"]["approved_at"])
        self.assertEqual(second.json()["cv"]["en"]["content_sha256"], shown)

    def test_the_preview_shows_only_the_cv_the_page_holds(self):
        job_id = self.planned_job()
        shown = self.job(job_id)["cv"]["en"]["content_sha256"]
        current = self.client.get(f"/preview/{job_id}/en?token={TOKEN}&v={shown}")
        self.put_back_the_cut_line(job_id)
        stale = self.client.get(f"/preview/{job_id}/en?token={TOKEN}&v={shown}")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(stale.status_code, 409)
        self.assertIn("changed", stale.text)
        self.assertNotIn("REST APIs", stale.text)  # the newer CV is not shown in its place

    def test_changes_from_two_tabs_run_one_at_a_time(self):
        # While DeepSeek adjusts the CV for one tab, a change from another waits for it to finish,
        # so two requests never write one job's files at the same moment.
        job_id = self.planned_job()
        gate = self.chat.while_planning = Gate()
        plan = f"/api/jobs/{job_id}/cv/en/plan"
        with TestClient(self.app, base_url="http://127.0.0.1:8765") as client, ThreadPoolExecutor(2) as pool:
            try:
                first = pool.submit(client.post, plan, headers=self.headers)
                self.assertTrue(gate.arrived.acquire(timeout=5))
                second = pool.submit(client.post, plan, headers=self.headers)
                wait_until(lambda: self.app.state.waiting_changes == 1)  # the second has reached the lock
                self.assertFalse(gate.arrived.acquire(timeout=0.5))  # and does not get past it
            finally:
                gate.release.set()
            replies = [first.result(10), second.result(10)]
        self.assertEqual([reply.status_code for reply in replies], [200, 200])
        self.assertEqual((gate.count, gate.most), (2, 1))

    def test_a_change_whose_client_goes_away_keeps_others_out_until_it_has_finished(self):
        # The handler keeps running in its thread after its request is cancelled (a closed tab,
        # a stopping server); the next change must still wait for it.
        job_id = self.planned_job()
        gate = self.chat.while_planning = Gate()
        plan = f"/api/jobs/{job_id}/cv/en/plan"

        async def scenario():
            async with anyio.create_task_group() as group:
                first = anyio.CancelScope()

                async def run_first():
                    with first:
                        await asgi_post(self.app, plan)

                group.start_soon(run_first)
                try:
                    self.assertTrue(await anyio.to_thread.run_sync(gate.arrived.acquire, True, 5))
                    first.cancel()
                    group.start_soon(asgi_post, self.app, plan)
                    with anyio.fail_after(5):
                        while self.app.state.waiting_changes != 1 and gate.count < 2:
                            await anyio.sleep(0.01)
                    return await anyio.to_thread.run_sync(gate.arrived.acquire, True, 0.5)
                finally:
                    gate.release.set()

        self.assertFalse(anyio.run(scenario))  # the second change never got in while the first ran
        self.assertEqual((gate.count, gate.most), (2, 1))

    def test_a_stalled_upload_holds_up_no_change(self):
        # An upload only writes its own new file, so a client that stops sending in the middle of
        # a PDF must not keep the user's changes waiting.
        job_id = self.planned_job()

        async def scenario():
            async with anyio.create_task_group() as group:
                group.start_soon(asgi_post, self.app, "/api/cv/upload", False)
                await anyio.sleep(0.1)
                with anyio.fail_after(3):
                    status = await asgi_post(self.app, f"/api/jobs/{job_id}/cv/en/plan")
                group.cancel_scope.cancel()
                return status

        self.assertEqual(anyio.run(scenario), 200)

    def test_following_and_refreshing_companies_never_waits_for_a_cv_change(self):
        # Job boards are refreshed four at a time when Find jobs opens; they live in their own
        # store, so they need not wait while DeepSeek adjusts a CV.
        job_id = self.planned_job()
        gate = self.chat.while_planning = Gate()
        with TestClient(self.app, base_url="http://127.0.0.1:8765") as client, ThreadPoolExecutor(3) as pool:
            try:
                slow = pool.submit(client.post, f"/api/jobs/{job_id}/cv/en/plan", headers=self.headers)
                self.assertTrue(gate.arrived.acquire(timeout=5))
                follow = pool.submit(client.post, "/api/sources", json={"link": "https://boards.greenhouse.io/example"},
                                     headers=self.headers).result(timeout=3)
                refresh = pool.submit(client.post, "/api/sources/greenhouse/example/refresh",
                                      headers=self.headers).result(timeout=3)
            finally:
                gate.release.set()
            self.assertEqual(slow.result(10).status_code, 200)
        self.assertEqual((follow.status_code, refresh.status_code), (200, 200), refresh.text)

    def test_a_busy_database_is_reported_as_busy(self):
        with patch("web.ranked_listings", side_effect=sqlite3.OperationalError("database is locked")):
            response = self.client.get("/api/listings", headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Try again", response.json()["error"])

    def test_a_page_never_sees_a_step_half_moved_to_history(self):
        # Start over moves the old draft, rewording and layout to history one file at a time; a page
        # loaded at that moment waits the moment out instead of showing a chain with a hole in it.
        job_id = self.planned_job()
        paused, resume = threading.Event(), threading.Event()
        real_rename = Path.rename

        def slow_rename(source, target):
            if source.name == "cv-tailored-en.json" and "history" in target.parts:
                paused.set()
                resume.wait(5)
            return real_rename(source, target)

        watched = self.app.state.workspace.lock = WatchedLock()
        with patch.object(Path, "rename", slow_rename), \
                TestClient(self.app, base_url="http://127.0.0.1:8765") as client, ThreadPoolExecutor(2) as pool:
            try:
                start_over = pool.submit(client.post, f"/api/jobs/{job_id}/cv/en/prepare", headers=self.headers)
                self.assertTrue(paused.wait(5))
                page = pool.submit(client.get, f"/api/jobs/{job_id}", headers=self.headers)
                self.assertTrue(watched.waiting.wait(5))  # the page has reached the storage lock
                self.assertFalse(page.done())  # and waits there while the move is half done
            finally:
                resume.set()
            view = page.result(10).json()
            self.assertEqual(start_over.result(10).status_code, 200)
        steps = set(view["steps"])
        self.assertTrue("cv-draft-en" in steps or not steps & {"cv-tailored-en", "cv-planned-en"}, sorted(steps))

    def test_no_step_moves_while_a_page_is_being_read(self):
        # The page reads a job's steps one file at a time; Start over must not move them between
        # two of those reads, or the page would mix the old chain with the new.
        job_id = self.planned_job()
        draft = Path(self.directory.name) / "jobs" / job_id / "cv-draft-en.json"
        before = draft.read_bytes()
        reading, resume = threading.Event(), threading.Event()
        workspace = self.app.state.workspace
        real_state = type(workspace).state

        def slow_state(this, job):  # the page's first read pauses there
            steps = real_state(this, job)
            if not reading.is_set():
                reading.set()
                resume.wait(5)
            return steps

        watched = workspace.lock = WatchedLock()
        with patch.object(type(workspace), "state", slow_state), \
                TestClient(self.app, base_url="http://127.0.0.1:8765") as client, \
                ThreadPoolExecutor(1) as reader, ThreadPoolExecutor(1) as writer:
            try:
                page = reader.submit(client.get, f"/api/jobs/{job_id}", headers=self.headers)
                self.assertTrue(reading.wait(5))
                start_over = writer.submit(client.post, f"/api/jobs/{job_id}/cv/en/prepare", headers=self.headers)
                self.assertTrue(watched.waiting.wait(5))  # Start over has reached the storage lock
                self.assertFalse(start_over.done())
                self.assertEqual(draft.read_bytes(), before)  # and has moved nothing
            finally:
                resume.set()
            self.assertEqual((page.result(10).status_code, start_over.result(10).status_code), (200, 200))

    def test_a_change_that_waits_too_long_is_told_to_try_again(self):
        job_id = self.planned_job()
        gate = self.chat.while_planning = Gate()
        change = {"change_id": "cut:fact-intern-api", "undone": True}
        with patch("web.CHANGE_WAIT_SECONDS", 0.05), \
                TestClient(self.app, base_url="http://127.0.0.1:8765") as client, ThreadPoolExecutor(3) as pool:
            try:
                slow = pool.submit(client.post, f"/api/jobs/{job_id}/cv/en/plan", headers=self.headers)
                self.assertTrue(gate.arrived.acquire(timeout=5))
                waited = client.post(f"/api/jobs/{job_id}/cv/en/change", json=change, headers=self.headers)
                # Both answer while the slow change is still held: reading and the token check never wait for it.
                page = pool.submit(client.get, f"/api/jobs/{job_id}", headers=self.headers).result(timeout=2)
                stranger = pool.submit(client.post, f"/api/jobs/{job_id}/cv/en/change", json=change).result(timeout=2)
                self.assertEqual(gate.inside, 1)
            finally:
                gate.release.set()
            self.assertEqual(slow.result(10).status_code, 200)
        self.assertEqual(waited.status_code, 409)
        self.assertIn("Try again", waited.json()["error"])
        self.assertEqual(page.status_code, 200)
        self.assertEqual(stranger.status_code, 403)
        cut = next(item for item in self.job(job_id)["cv"]["en"]["changes"] if item["id"] == "cut:fact-intern-api")
        self.assertFalse(cut["undone"])  # the refused change was not made

    def restarted(self):
        """A client for the next start of the workbench, on the same folders."""
        return TestClient(create_app(
            facts_db=self.database, jobs_root=Path(self.directory.name) / "jobs", token=TOKEN,
            profile_path=self.profile_path, chat=self.chat, printer=FakePrinter(), starter=[],
        ), base_url="http://127.0.0.1:8765")

    def die_in_app(self, action, crash_point):
        """Run ``action`` (using ``client``, ``headers`` and ``chat``) against this test's folders
        in a new process that dies at ``crash_point`` the way a killed one does: no finally, no
        except, no temporary file removed."""
        code = "\n".join([
            "import os, sys, json", "from pathlib import Path", f"sys.path.insert(0, {str(REPO)!r})",
            "from fastapi.testclient import TestClient", "from test_web import FakeDeepSeek, TOKEN",
            "from test_cv import FakePrinter", "from test_cv_import import minimal_pdf", "from web import create_app",
            "chat = FakeDeepSeek({'fact-intern-api': 'For an internal tool, built REST APIs.'})",
            f"app = create_app(facts_db=Path({str(self.database)!r}), jobs_root=Path({str(Path(self.directory.name) / 'jobs')!r}),"
            f" token=TOKEN, profile_path=Path({str(self.profile_path)!r}), chat=chat, printer=FakePrinter(), starter=[])",
            "client = TestClient(app, base_url='http://127.0.0.1:8765')", "headers = {'X-Workbench-Token': TOKEN}",
            "real_replace, real_rename, real_open = os.replace, Path.rename, Path.open", crash_point, action,
        ])
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, cwd=REPO,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, PROCESS_DIES, result.stderr[-1500:])

    def line_job(self, requirements=("Hands-on Docker and Kubernetes",), suggestions=None):
        """A job whose requirement check found nothing showing each requirement."""
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        text = "Requirements:\n" + "\n".join(f"- {line}" for line in requirements)
        job_id = self.client.post("/api/jobs", json={"title": "Backend Intern", "text": text}, headers=self.headers).json()["job_id"]
        self.chat.evidence = lambda request: [{"id": item["id"], "verdict": "none"} for item in request["requirements"]]
        self.chat.gap_suggestions = suggestions or (lambda ids: [])
        gaps = self.client.post(f"/api/jobs/{job_id}/gaps", headers=self.headers).json()["gaps"]
        return job_id, gaps

    def own_line(self, gaps, index=0, text="Deployed the internal tool with Docker."):
        place = next(place["id"] for place in gaps["places"] if place["kind"] == "bullet")
        return gaps["requirements"][index]["requirement_id"], {"place": place, "text": text}

    UPLOAD = """
chat.cv_structure = {"sections": [
    {"kind": "experience", "heading": 3, "entries": [
        {"title": [4, 1], "location": [4, 2], "subtitle": [5, 1], "dates": [5, 2],
         "facts": [{"lines": [6], "tags": ["REST APIs"]}]}]},
    {"kind": "skills", "heading": 7, "entries": [{"facts": [{"lines": [8], "tags": ["Python", "Java"]}]}]},
]}
pdf = minimal_pdf([
    (72, 740, "ALEX EXAMPLE"), (72, 726, "Los Angeles, CA | 000-000-0000 | alex@example.com"),
    (72, 700, "EXPERIENCE"), (72, 686, "Example Corp"), (430, 686, "Chengdu, China"),
    (72, 672, "Software Intern"), (430, 672, "Jun 2025 - Aug 2025"),
    (72, 658, "- Built REST APIs for an internal tool."), (72, 630, "SKILLS"), (72, 616, "Languages: Python, Java"),
])
contact = {"name": "Alex Example", "location": "Los Angeles, CA", "phone": "000-000-0000", "email": "alex@example.com", "links": []}
def upload_and_save():
    proposal = client.post("/api/cv/upload", content=pdf, headers={**headers, "Content-Type": "application/pdf"}).json()
    return client.post(f"/api/cv/uploads/{proposal['upload_id']}/save", json=contact, headers=headers)
"""

    @unittest.skipUnless(HAS_PYPDF, "reading PDFs needs pypdf from requirements.txt")
    def test_saving_an_uploaded_cv_cut_short_is_reported_and_can_be_repeated(self):
        self.die_in_app(self.UPLOAD + "upload_and_save()", dies_before_renaming_into("cv-profile.json"))
        client = self.restarted()
        stopped = client.get("/api/facts", headers=self.headers).json()
        self.assertEqual([notice["kind"] for notice in stopped["interrupted"]], ["save_cv"])
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8"))["name"], PROFILE["name"])  # still the old one
        # The same PDF, uploaded and saved again: its lines are reused and the notice is settled.
        self.die_in_app(self.UPLOAD + "upload_and_save(); os._exit(9)", "")
        self.assertNotIn("interrupted", client.get("/api/facts", headers=self.headers).json())
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8"))["name"], "Alex Example")
        texts = [fact["text"] for fact in list_facts(self.database)]
        self.assertEqual(sorted(texts), sorted(set(texts)))  # nothing added twice

    @unittest.skipUnless(HAS_PYPDF, "reading PDFs needs pypdf from requirements.txt")
    def test_a_refused_retry_keeps_the_notice_about_the_action_it_repeats(self):
        self.die_in_app(self.UPLOAD + "upload_and_save()", dies_before_renaming_into("cv-profile.json"))
        # The same PDF again, refused before anything is written (no name): the notice stays.
        self.die_in_app(self.UPLOAD + "contact['name'] = ''\nassert upload_and_save().status_code == 400\nos._exit(9)", "")
        stopped = self.restarted().get("/api/facts", headers=self.headers).json()
        self.assertEqual([notice["kind"] for notice in stopped["interrupted"]], ["save_cv"])

    def test_a_notice_that_finishes_while_the_page_reads_it_is_not_shown(self):
        unfinished = self.profile_path.parent / "unfinished"
        (unfinished / "0123456789abcdef.json").write_text('{"kind": "save_cv"}', encoding="utf-8")
        real_read = Path.read_text

        def finished(file, *args, **kwargs):
            if file.parent == unfinished:
                raise FileNotFoundError(file)  # removed after the folder was listed
            return real_read(file, *args, **kwargs)

        with patch.object(Path, "read_text", finished):
            self.assertNotIn("interrupted", self.client.get("/api/facts", headers=self.headers).json())

    def test_an_error_partway_through_adding_a_line_leaves_a_notice(self):
        # A storage error after the fact went in: the page shows the error, and the notice
        # stays, since part of the change is done.
        job_id, gaps = self.line_job()
        requirement, line = self.own_line(gaps)
        real_write = web_module.write_atomically

        def disk_full(path, data):
            if Path(path).name == "cv-profile.json":
                raise OSError(28, "No space left on device")
            real_write(path, data)

        client = TestClient(self.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
        with patch("web.write_atomically", disk_full):
            refused = client.post(f"/api/jobs/{job_id}/gaps/{requirement}/write", headers=self.headers, json=line)
        self.assertEqual(refused.status_code, 500)
        notices = self.job(job_id)["interrupted_operations"]
        self.assertEqual([(notice["kind"], notice["requirement"], notice["line"], notice["language"]) for notice in notices],
                         [("add_line", "Hands-on Docker and Kubernetes", line["text"], "en")])

    def test_adding_a_line_cut_short_after_it_was_recorded_can_be_finished_by_adding_it_again(self):
        job_id, gaps = self.line_job()
        requirement, line = self.own_line(gaps)
        write = f"/api/jobs/{job_id}/gaps/{requirement}/write"
        self.die_in_app(f"client.post({write!r}, headers=headers, json={line!r})",
                        dies_after_renaming_into("gaps.json"))  # the fact, profile and gaps are all in
        client = self.restarted()
        view = client.get(f"/api/jobs/{job_id}", headers=self.headers).json()
        self.assertEqual([notice["line"] for notice in view["interrupted_operations"]], [line["text"]])
        again = client.post(write, headers=self.headers, json=line)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertNotIn("interrupted_operations", again.json())
        self.assertIn(line["text"], client.get(f"/preview/{job_id}/en?token={TOKEN}").text)  # the CV was prepared again
        self.assertEqual([fact["text"] for fact in list_facts(self.database)].count(line["text"]), 1)

    def test_accepting_a_skill_cut_short_is_reported_and_can_be_repeated(self):
        # Cut short before the requirement check recorded it as added, just after, and while the
        # CV was being prepared again.
        for index, (label, crash_point) in enumerate((("before", dies_before_renaming_into("gaps.json")),
                                                      ("after", dies_after_renaming_into("gaps.json")),
                                                      ("preparing", dies_before_renaming_into("cv-draft-en.json")))):
            with self.subTest(label):
                if index:  # a fresh workbench for each crash point
                    self.tearDown()
                    self.setUp()
                job_id, gaps = self.line_job(("Experience with Go",), suggestions=lambda ids: [
                    {"requirement": ids["Experience with Go"], "kind": "skill", "line": "fact-skills-languages", "items": ["Go"]}])
                requirement = gaps["requirements"][0]["requirement_id"]
                accept = f"/api/jobs/{job_id}/gaps/{requirement}/accept"
                self.die_in_app(f"client.post({accept!r}, headers=headers)", crash_point)
                client = self.restarted()
                self.assertEqual([(notice["requirement"], notice["line"]) for notice in client.get(
                    f"/api/jobs/{job_id}", headers=self.headers).json()["interrupted_operations"]],
                                 [("Experience with Go", "Languages: Python, Java, Go")])
                again = client.post(accept, headers=self.headers)
                self.assertEqual(again.status_code, 200, again.text)
                self.assertNotIn("interrupted_operations", again.json())
                languages = [fact for fact in list_facts(self.database) if fact["id"] == "fact-skills-languages"]
                self.assertEqual([(fact["text"], fact["status"]) for fact in languages],
                                 [("Languages: Python, Java, Go", "confirmed")])

    def test_a_crash_while_the_cv_is_prepared_again_keeps_the_notice_until_it_is(self):
        # The line is in; the process dies preparing the CV again, which the notice waits for.
        job_id, gaps = self.line_job()
        requirement, line = self.own_line(gaps)
        write = f"/api/jobs/{job_id}/gaps/{requirement}/write"
        self.die_in_app(f"client.post({write!r}, headers=headers, json={line!r})",
                        dies_before_renaming_into("cv-draft-en.json"))
        client = self.restarted()
        view = client.get(f"/api/jobs/{job_id}", headers=self.headers).json()
        self.assertEqual(view["interrupted"]["step"], "cv-draft-en")  # the CV says it was cut short
        self.assertEqual([notice["line"] for notice in view["interrupted_operations"]], [line["text"]])
        again = client.post(write, headers=self.headers, json=line)  # the same line: saved already, so only the CV is prepared
        self.assertEqual(again.status_code, 200, again.text)
        self.assertNotIn("interrupted_operations", again.json())
        self.assertIn(line["text"], client.get(f"/preview/{job_id}/en?token={TOKEN}").text)
        self.assertEqual([fact["text"] for fact in list_facts(self.database)].count(line["text"]), 1)

    def test_a_retry_that_stops_just_before_its_notice_goes_leaves_the_notice(self):
        job_id, gaps = self.line_job()
        requirement, line = self.own_line(gaps)
        write = f"client.post({f'/api/jobs/{job_id}/gaps/{requirement}/write'!r}, headers=headers, json={line!r})"
        self.die_in_app(write, dies_before_renaming_into("gaps.json"))  # the first try leaves a notice
        self.die_in_app(write, dies_after_renaming_into("cv-status-en.json"))  # the retry prepares the CV, then dies
        client = self.restarted()
        [notice] = client.get(f"/api/jobs/{job_id}", headers=self.headers).json()["interrupted_operations"]
        self.assertEqual(notice["line"], line["text"])
        self.assertIn(line["text"], client.get(f"/preview/{job_id}/en?token={TOKEN}").text)
        self.assertEqual([fact["text"] for fact in list_facts(self.database)].count(line["text"]), 1)

    def test_writing_the_suggested_line_yourself_finishes_the_same_action(self):
        job_id, gaps = self.line_job(suggestions=lambda ids: [
            {"requirement": ids["Hands-on Docker and Kubernetes"], "kind": "bullet", "entry": "s1e0",
             "text": "Deployed the internal tool with Docker.", "tags": ["Docker"]}])
        [record] = gaps["requirements"]
        accept = f"/api/jobs/{job_id}/gaps/{record['requirement_id']}/accept"
        self.die_in_app(f"client.post({accept!r}, headers=headers)", dies_before_renaming_into("gaps.json"))
        own = {"place": f"entry:{record['suggestion']['entry_key']}", "text": record["suggestion"]["text"]}
        done = self.restarted().post(f"/api/jobs/{job_id}/gaps/{record['requirement_id']}/write", headers=self.headers, json=own)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertNotIn("interrupted_operations", done.json())

    def test_an_action_still_under_way_is_not_shown_as_unfinished(self):
        job_id, gaps = self.line_job()
        requirement, line = self.own_line(gaps)
        self.chat.plan = {"sections": ["education", "skills", "experience"], "entries": [], "reasons": []}
        gate = self.chat.while_planning = Gate()
        unfinished = self.profile_path.parent / "unfinished"
        with TestClient(self.app, base_url="http://127.0.0.1:8765") as client, ThreadPoolExecutor(1) as pool:
            try:
                adding = pool.submit(client.post, f"/api/jobs/{job_id}/gaps/{requirement}/write", headers=self.headers,
                                     json=line)
                self.assertTrue(gate.arrived.acquire(timeout=5))  # the line is saved; the CV is being prepared again
                self.assertEqual(len(list(unfinished.glob("*.json"))), 1)  # its file is there, in case the process dies
                self.assertNotIn("interrupted_operations", client.get(f"/api/jobs/{job_id}", headers=self.headers).json())
            finally:
                gate.release.set()
            self.assertEqual(adding.result(10).status_code, 200)
        self.assertEqual(list(unfinished.glob("*.json")), [])

    def test_finishing_another_line_leaves_the_notice_about_this_one(self):
        job_id, gaps = self.line_job(("Hands-on Docker and Kubernetes", "Experience with Go"))
        first, first_line = self.own_line(gaps, 0)
        second, second_line = self.own_line(gaps, 1, text="Wrote a small service in Go.")
        write_first = f"/api/jobs/{job_id}/gaps/{first}/write"
        self.die_in_app(f"client.post({write_first!r}, headers=headers, json={first_line!r})",
                        dies_before_renaming_into("gaps.json"))
        client = self.restarted()
        done = client.post(f"/api/jobs/{job_id}/gaps/{second}/write", headers=self.headers, json=second_line)
        [notice] = done.json()["interrupted_operations"]
        self.assertEqual(notice["line"], first_line["text"])
        client.post("/api/notices/dismiss", headers=self.headers, json={"id": notice["id"]})
        self.assertNotIn("interrupted_operations", client.get(f"/api/jobs/{job_id}", headers=self.headers).json())

    def test_another_line_for_the_same_requirement_leaves_the_notice_about_the_first(self):
        job_id, gaps = self.line_job()
        requirement, first = self.own_line(gaps)
        _, second = self.own_line(gaps, text="Ran the internal tool in Docker containers.")
        write = f"/api/jobs/{job_id}/gaps/{requirement}/write"
        self.die_in_app(f"client.post({write!r}, headers=headers, json={first!r})", dies_before_renaming_into("gaps.json"))
        done = self.restarted().post(write, headers=self.headers, json=second)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual([notice["line"] for notice in done.json()["interrupted_operations"]], [first["text"]])

    def test_a_notice_that_cannot_be_read_is_shown_and_kept_until_dismissed(self):
        unfinished = self.profile_path.parent / "unfinished"
        unfinished.mkdir(exist_ok=True)
        (unfinished / "0123456789abcdef.json").write_text("{ not json", encoding="utf-8")
        (unfinished / "notes.json").write_text('{"kind": "save_cv"}', encoding="utf-8")  # not a notice's name
        (unfinished / "fedcba9876543210.json").symlink_to(self.profile_path)  # nor is a link
        client = self.restarted()
        stopped = client.get("/api/facts", headers=self.headers).json()["interrupted"]
        self.assertEqual([(notice["kind"], notice["id"]) for notice in stopped], [("unknown", "0123456789abcdef")])
        job_id, gaps = self.line_job()  # other actions finish around it
        requirement, line = self.own_line(gaps)
        self.assertEqual(client.post(f"/api/jobs/{job_id}/gaps/{requirement}/write", headers=self.headers,
                                     json=line).status_code, 200)
        self.assertEqual((unfinished / "0123456789abcdef.json").read_text(encoding="utf-8"), "{ not json")
        for strange in ("../cv-profile", "0123456789ABCDEF", ""):
            self.assertEqual(client.post("/api/notices/dismiss", headers=self.headers, json={"id": strange}).status_code, 400)
        self.assertTrue(self.profile_path.exists())
        client.post("/api/notices/dismiss", headers=self.headers, json={"id": "0123456789abcdef"})
        self.assertNotIn("interrupted", client.get("/api/facts", headers=self.headers).json())

    def test_an_approval_cut_short_by_a_crash_is_no_approval_after_the_restart(self):
        job_id = self.planned_job()
        shown = self.job(job_id)["cv"]["en"]["content_sha256"]
        approve = f"/api/jobs/{job_id}/cv/en/approve"
        self.die_in_app(f"client.post({approve!r}, headers=headers, json={{'expected_content_sha256': {shown!r}}})",
                        dies_before_renaming_into("cv-approved-en.json"))
        view = self.restarted().get(f"/api/jobs/{job_id}", headers=self.headers).json()
        self.assertEqual(view["cv"]["en"]["head"], "planned")  # recovery never approves
        self.assertEqual(view["cv"]["en"]["content_sha256"], shown)
        self.assertEqual(view["interrupted"]["step"], "cv-approved-en")

    def test_a_restart_during_generation_shows_only_what_was_finished(self):
        job_id = self.planned_job()
        dies_rewording = """
real_call = FakeDeepSeek.__call__
def dying(self, messages, model, effort):
    if messages[0]["content"].startswith("You rewrite resume lines"):
        os._exit(9)
    return real_call(self, messages, model, effort)
FakeDeepSeek.__call__ = dying
"""
        prepare = f"/api/jobs/{job_id}/cv/en/prepare"
        self.die_in_app(f"client.post({prepare!r}, headers=headers)", dies_rewording)  # Start over
        view = self.restarted().get(f"/api/jobs/{job_id}", headers=self.headers).json()
        self.assertEqual(view["cv"]["en"]["head"], "draft")  # the finished draft stays; nothing after it exists
        self.assertNotIn("stages", view["cv"]["en"])  # no stage is reported as done for this draft
        self.assertNotIn("interrupted", view)  # no file was cut short, so nothing needed undoing

    def test_nothing_found_leaves_the_requirements_step_to_the_user_and_can_be_retried(self):
        import_facts(self.database, FACTS)
        confirm_facts(self.database, [("fact-web-python", 1)])
        job_id = self.client.post(
            "/api/jobs", json={"title": "Engineer", "text": "We build tools.\nYou will ship code daily."}, headers=self.headers,
        ).json()["job_id"]
        view = self.job(job_id)
        self.assertEqual(view["steps"], ["input", "candidates"])
        self.assertEqual(view["extraction"]["fallback_reason"], "测试中不联网")
        self.chat.requirement_lines = {"You will ship code daily."}
        retried = self.client.post(f"/api/jobs/{job_id}/requirements/find", headers=self.headers)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual([item["text"] for item in retried.json()["candidates"]], ["You will ship code daily."])
        self.assertIn("decided", retried.json()["steps"])


    def test_an_english_only_profile_gets_english_cvs_even_for_chinese_postings(self):
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        english_only = {**PROFILE, "name": {"en": "Alex Example", "zh": ""}}
        self.profile_path.write_text(json.dumps(english_only, ensure_ascii=False), encoding="utf-8")
        view = self.job(self.create_job())
        self.assertEqual((view["cv_languages"], view["language"]), (["en"], "en"))
        self.assertEqual((view["cv"]["en"]["head"], view["cv"]["zh"]["head"]), ("tailored", None))


    def test_each_requirement_says_what_the_cv_shows_and_a_line_true_for_you_joins_the_cv(self):
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        text = ("Requirements:\n- Hands-on Docker and Kubernetes\n- 3+ years of backend experience\n- Writing integration tests"
                "\n- Experience with Go")
        job_id = self.client.post("/api/jobs", json={"title": "Backend Intern", "text": text}, headers=self.headers).json()["job_id"]
        suggestions = {
            "Hands-on Docker and Kubernetes": {"kind": "skill", "line": "fact-skills-languages", "items": ["Docker"]},
            "Experience with Go": {"kind": "skill", "line": "fact-skills-languages", "items": ["Go"]},
            "3+ years of backend experience": {"kind": "none"},
            "Writing integration tests": {"kind": "bullet", "entry": "s1e0",
                                          "text": "Wrote integration tests for the internal tool.", "tags": ["integration tests"]},
        }
        self.chat.gap_suggestions = lambda ids: [{"requirement": ids[text], **suggestion}
                                                 for text, suggestion in suggestions.items() if text in ids]

        def judged(request):
            """Shown by any line naming it; the years only in part, by the internship's dates."""
            lines = sent_lines(request)
            ids = {item["text"]: item["id"] for item in request["requirements"]}
            answer = [{"id": ids["3+ years of backend experience"], "verdict": "related",
                       "lines": [lines["Software Intern (2025)"]], "missing": "3+ years"},
                      {"id": ids["Experience with Go"], "verdict": "none"}]
            for requirement, words in (("Hands-on Docker and Kubernetes", "Docker"), ("Writing integration tests", "integration tests")):
                naming = [[ref] for line, ref in lines.items() if words in line]
                answer.append({"id": ids[requirement], "verdict": "supported", "sets": naming} if naming
                              else {"id": ids[requirement], "verdict": "none"})
            return answer

        self.chat.evidence = judged
        found = self.client.post(f"/api/jobs/{job_id}/gaps", headers=self.headers)
        self.assertEqual(found.status_code, 200, found.text)
        shown = {item["text"]: item for item in found.json()["gaps"]["requirements"]}
        self.assertEqual({text: item["status"] for text, item in shown.items()}, {
            "Hands-on Docker and Kubernetes": "none", "3+ years of backend experience": "related", "Writing integration tests": "none",
            "Experience with Go": "none"})
        self.assertEqual(shown["3+ years of backend experience"]["missing"], "3+ years")
        self.assertFalse(found.json()["gaps"]["stale"])
        docker, years, tests, go = (shown[text]["requirement_id"] for text in (
            "Hands-on Docker and Kubernetes", "3+ years of backend experience", "Writing integration tests", "Experience with Go"))
        self.client.post(f"/api/jobs/{job_id}/gaps/{docker}/accept", headers=self.headers)
        self.client.post(f"/api/jobs/{job_id}/gaps/{tests}/accept", headers=self.headers)
        self.client.post(f"/api/jobs/{job_id}/gaps/{go}/decline", headers=self.headers)
        declined = self.client.post(f"/api/jobs/{job_id}/gaps/{years}/decline", headers=self.headers).json()
        preview = self.client.get(f"/preview/{job_id}/en?token={TOKEN}").text
        saved = json.loads(self.profile_path.read_text(encoding="utf-8"))
        backups = list((self.profile_path.parent / "profile-history").glob("*.json"))
        self.assertEqual({item["requirement_id"]: item["suggestion_status"] for item in declined["gaps"]["requirements"]},
                         {docker: "added", years: "declined", tests: "added", go: "declined"})
        self.assertIn("Python, Java, Docker", preview)
        self.assertIn("Wrote integration tests for the internal tool.", preview)
        self.assertEqual(len(saved["sections"][1]["entries"][0]["facts"]), 3)
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), PROFILE)
        # The facts and the CV changed, so the check is out of date; checked again, the added
        # lines are what shows each requirement, and the line said to be untrue is not offered again.
        self.assertTrue(declined["gaps"]["stale"])
        again = self.client.post(f"/api/jobs/{job_id}/gaps", headers=self.headers).json()["gaps"]
        self.assertEqual({item["text"]: item["status"] for item in again["requirements"]}, {
            "Hands-on Docker and Kubernetes": "shown", "3+ years of backend experience": "related", "Writing integration tests": "shown",
            "Experience with Go": "none"})
        self.assertEqual(next(item for item in again["requirements"] if item["requirement_id"] == go)["suggestion_status"], "declined")
        self.assertFalse(again["stale"])

    def test_a_line_the_user_writes_for_a_requirement_joins_the_cv(self):
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        job_id = self.client.post("/api/jobs", json={"title": "Backend Intern", "text": "Requirements:\n- Hands-on Docker and Kubernetes"},
                                  headers=self.headers).json()["job_id"]
        self.chat.evidence = lambda request: [{"id": item["id"], "verdict": "none"} for item in request["requirements"]]
        self.chat.gap_suggestions = lambda ids: []
        gaps = self.client.post(f"/api/jobs/{job_id}/gaps", headers=self.headers).json()["gaps"]
        self.assertEqual({(place["kind"], place["where"]) for place in gaps["places"]},
                         {("bullet", "Example Corp"), ("skill", "Languages: Python, Java")})
        place = next(place["id"] for place in gaps["places"] if place["kind"] == "bullet")
        requirement = gaps["requirements"][0]["requirement_id"]
        written = self.client.post(f"/api/jobs/{job_id}/gaps/{requirement}/write", headers=self.headers,
                                   json={"place": place, "text": "Deployed the internal tool with Docker."})
        self.assertEqual(written.status_code, 200, written.text)
        self.assertEqual(written.json()["gaps"]["requirements"][0]["suggestion_status"], "added")
        self.assertIn("Deployed the internal tool with Docker.", self.client.get(f"/preview/{job_id}/en?token={TOKEN}").text)
        refused = self.client.post(f"/api/jobs/{job_id}/gaps/{requirement}/write", headers=self.headers,
                                   json={"place": place, "text": "Something else."})
        self.assertEqual(refused.status_code, 400)

    def test_a_line_that_is_saved_is_not_reported_as_refused_when_the_cv_cannot_be_prepared(self):
        # Found in review by Codex: the line and the profile were saved, then preparing the CV
        # failed on another fact waiting for confirmation, and the page showed a refusal.
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        job_id = self.client.post("/api/jobs", json={"title": "Backend Intern", "text": "Requirements:\n- Hands-on Docker and Kubernetes"},
                                  headers=self.headers).json()["job_id"]
        self.chat.evidence = lambda request: [{"id": item["id"], "verdict": "none"} for item in request["requirements"]]
        self.chat.gap_suggestions = lambda ids: []
        gaps = self.client.post(f"/api/jobs/{job_id}/gaps", headers=self.headers).json()["gaps"]
        revise_fact(self.database, "fact-intern-tests", text="Wrote unit tests for the billing code.")  # now pending
        place = next(place["id"] for place in gaps["places"] if place["kind"] == "bullet")
        written = self.client.post(f"/api/jobs/{job_id}/gaps/{gaps['requirements'][0]['requirement_id']}/write",
                                   headers=self.headers, json={"place": place, "text": "Deployed the internal tool with Docker."})
        self.assertEqual(written.status_code, 200, written.text)
        view = written.json()
        self.assertEqual(view["gaps"]["requirements"][0]["suggestion_status"], "added")
        draft_stage = next(stage for stage in view["cv"]["en"]["stages"] if stage["stage"] == "draft")
        self.assertEqual((draft_stage["status"], draft_stage["reason_code"]), ("failed", "facts_not_confirmed"))

    def test_without_the_evidence_check_nothing_counts_as_shown_and_the_page_says_why(self):
        import_facts(self.database, CV_FACTS)
        confirm_facts(self.database, [(item["id"], 1) for item in CV_FACTS])
        job_id = self.client.post("/api/jobs", json={"title": "Backend Intern", "text": "Requirements:\n- Experience with Python"},
                                  headers=self.headers).json()["job_id"]
        gaps = self.client.post(f"/api/jobs/{job_id}/gaps", headers=self.headers).json()["gaps"]
        self.assertEqual([item["status"] for item in gaps["requirements"]], ["unchecked"])
        self.assertEqual(gaps["evidence_check"]["fallback_code"], "unreachable")
        self.assertIn("could not be reached", gaps["evidence_check"]["message"])
        # A check saved in the older format is only marked out of date, so the page checks again.
        path = self.client.app.state.workspace.path(job_id, "gaps")
        path.write_text(json.dumps({"gaps_version": 1, "language": "en", "gaps": []}), encoding="utf-8")
        self.assertEqual(self.job(job_id)["gaps"], {"outdated": True})

    @unittest.skipUnless(HAS_PYPDF, "reading PDFs needs pypdf from requirements.txt")
    def test_an_uploaded_cv_becomes_pending_facts_and_the_cv_after_the_user_checks_the_contact(self):
        from test_cv_import import minimal_pdf

        self.profile_path.unlink()  # a new user: no CV yet
        pdf = minimal_pdf([
            (72, 740, "ALEX EXAMPLE"), (72, 726, "Los Angeles, CA | 000-000-0000 | alex@example.com"),
            (72, 700, "EXPERIENCE"), (72, 686, "Example Corp"), (430, 686, "Chengdu, China"),
            (72, 672, "Software Intern"), (430, 672, "Jun 2025 - Aug 2025"),
            (72, 658, "- Built REST APIs for an internal tool."), (72, 630, "SKILLS"), (72, 616, "Languages: Python, Java"),
        ])
        self.chat.cv_structure = {"sections": [
            {"kind": "experience", "heading": 3, "entries": [
                {"title": [4, 1], "location": [4, 2], "subtitle": [5, 1], "dates": [5, 2],
                 "facts": [{"lines": [6], "tags": ["REST APIs"]}]}]},
            {"kind": "skills", "heading": 7, "entries": [{"facts": [{"lines": [8], "tags": ["Python", "Java"]}]}]},
        ]}
        wrong = self.client.post("/api/cv/upload", content=b"hello", headers=self.headers)
        upload = self.client.post("/api/cv/upload", content=pdf, headers={**self.headers, "Content-Type": "application/pdf"})
        proposal = upload.json()
        contact = {"name": "Alex Example", "location": "Los Angeles, CA", "phone": "000-000-0000",
                   "email": "alex@example.com", "links": [{"label": "github.com/alex-example", "url": "github.com/alex-example"}]}
        saved = self.client.post(f"/api/cv/uploads/{proposal['upload_id']}/save", json=contact, headers=self.headers)
        again = self.client.post(f"/api/cv/uploads/{proposal['upload_id']}/save", json=contact, headers=self.headers)
        facts = self.client.get("/api/facts", headers=self.headers).json()["facts"]
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertEqual((proposal["private"]["name"], proposal["private"]["email"], proposal["has_profile"]),
                         ("Alex Example", "alex@example.com", False))
        self.assertEqual([fact["text"] for section in proposal["sections"] for entry in section["entries"] for fact in entry["facts"]],
                         ["Built REST APIs for an internal tool.", "Languages: Python, Java"])
        self.assertFalse(any(private in sent for sent in self.chat.sent
                             for private in ("ALEX", "alex@example.com", "000-000-0000")))
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json(), {"imported": 2, "reused": 0})
        self.assertEqual(again.status_code, 400)  # each upload is saved once
        self.assertEqual({(fact["text"], fact["status"]) for fact in facts},
                         {("Built REST APIs for an internal tool.", "pending"), ("Languages: Python, Java", "pending")})
        self.assertEqual((profile["name"], profile["contact"]["links"][0]["url"]), ("Alex Example", "https://github.com/alex-example"))
        self.assertEqual(profile["sections"][0]["entries"][0]["title"], "Example Corp")


@unittest.skipUnless(HAS_FASTAPI, "web tests need the packages in requirements.txt")
class ServerSettingsTests(unittest.TestCase):
    def test_one_data_folder_sets_every_path_and_the_older_options_still_win(self):
        settings = server_settings(["--data", "/srv/data", "--profile", "/elsewhere/cv-profile.json"], {})
        self.assertEqual((settings.facts_db, settings.jobs, settings.profile, settings.host, settings.port),
                         (Path("/srv/data/workbench.db"), Path("/srv/data/jobs"), Path("/elsewhere/cv-profile.json"),
                          "127.0.0.1", 8765))
        container = server_settings([], {"WORKBENCH_DATA": "/data", "WORKBENCH_HOST": "0.0.0.0", "WORKBENCH_PORT": "9000",
                                         "WORKBENCH_REQUIRE_DATA": "1"})
        self.assertEqual((container.facts_db, container.host, container.port, container.require_data),
                         (Path("/data/workbench.db"), "0.0.0.0", 9000, True))
        local = server_settings([], {})
        self.assertEqual((local.facts_db, local.require_data, local.json_logs), (Path(".local/workbench.db"), False, False))
        self.assertTrue(server_settings([], {"WORKBENCH_LOG_FORMAT": "json"}).json_logs)  # as the image sets it

    def test_a_required_data_folder_must_be_the_marked_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.assertIn(DATA_MARKER, data_problem(folder))  # an empty folder is not the data volume
            (folder / DATA_MARKER).write_text("", encoding="utf-8")
            self.assertIsNone(data_problem(folder))
            self.assertIsNotNone(data_problem(folder / "missing"))

    def test_startup_refuses_a_data_folder_that_is_not_the_volume(self):
        # Logging is set up for the whole process when the server starts; not in a test run.
        said, complained = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch("uvicorn.run") as run, patch.dict(os.environ, {}, clear=True), \
                patch("web.logging.basicConfig"), redirect_stdout(said), redirect_stderr(complained):
            self.assertEqual(main(["--data", directory, "--require-data"]), 2)
            run.assert_not_called()
            self.assertIn(DATA_MARKER, complained.getvalue())  # says why, not only that it stopped
            (Path(directory) / DATA_MARKER).write_text("", encoding="utf-8")
            self.assertEqual(main(["--data", directory, "--require-data", "--host", "0.0.0.0"]), 0)
        self.assertEqual((run.call_args.kwargs["host"], run.call_args.kwargs["access_log"]), ("0.0.0.0", False))
        self.assertNotIn("log_config", run.call_args.kwargs)  # on a terminal, uvicorn's own log setup

    def test_in_a_container_every_line_is_json_from_the_first(self):
        with tempfile.TemporaryDirectory() as directory, patch("uvicorn.run") as run, \
                patch("web.run_log.configure") as configure, patch("web.logging.basicConfig") as plain:
            with self.assertLogs("workbench", level="ERROR") as refused:
                self.assertEqual(main(["--data", directory, "--require-data", "--json-logs"]), 2)
            self.assertIn(DATA_MARKER, refused.records[0].fields["reason"])  # the refusal, as an event
            (Path(directory) / DATA_MARKER).write_text("", encoding="utf-8")
            self.assertEqual(main(["--data", directory, "--require-data", "--json-logs"]), 0)
        configure.assert_called_with(sys.stderr)
        plain.assert_not_called()
        self.assertIsNone(run.call_args.kwargs["log_config"])  # uvicorn's lines go through the JSON format too

    def test_a_failure_to_start_is_logged_by_its_type_never_its_words(self):
        def broken(*args, **kwargs):
            raise RuntimeError("Alex Example, Acme Corporation")  # say, a damaged database quoting a fact

        with tempfile.TemporaryDirectory() as directory, patch("uvicorn.run") as run, patch("web.run_log.configure"), \
                patch("web.create_app", broken), self.assertLogs("workbench", level="ERROR") as logs:
            self.assertEqual(main(["--data", directory, "--json-logs"]), 1)
        run.assert_not_called()
        [line] = [json.loads(run_log.JsonLines().format(record)) for record in logs.records]
        self.assertEqual((line["event"], line["error"]), ("failed_to_start", "RuntimeError"))
        self.assertNotIn("Alex", json.dumps(line))

    def test_data_a_newer_release_has_used_is_refused_wherever_the_options_put_it(self):
        from workspace import DATA_FORMAT, DATA_FORMAT_FILE
        with tempfile.TemporaryDirectory() as newer, tempfile.TemporaryDirectory() as other, \
                patch.object(web_module.Workspace, "recover") as recover:
            (Path(newer) / DATA_FORMAT_FILE).write_text(f"{DATA_FORMAT + 1}\n", encoding="utf-8")
            for facts_db, jobs in ((Path(newer) / "workbench.db", Path(other) / "jobs"),
                                   (Path(other) / "workbench.db", Path(newer) / "jobs")):
                with self.assertRaisesRegex(Exception, f"format {DATA_FORMAT + 1}"):
                    create_app(facts_db=facts_db, jobs_root=jobs, profile_path=Path(other) / "cv-profile.json")
            recover.assert_not_called()  # no change a crash cut short was touched
            self.assertEqual(sorted(path.name for path in Path(other).iterdir()), [])  # nothing written either

    def test_data_a_newer_release_has_used_is_never_opened(self):
        from workspace import DATA_FORMAT, DATA_FORMAT_FILE
        with tempfile.TemporaryDirectory() as directory, patch("uvicorn.run") as run, patch("web.run_log.configure"), \
                self.assertLogs("workbench", level="ERROR") as logs:
            (Path(directory) / DATA_FORMAT_FILE).write_text(f"{DATA_FORMAT + 1}\n", encoding="utf-8")
            self.assertEqual(main(["--data", directory, "--json-logs"]), 2)
            self.assertFalse((Path(directory) / "workbench.db").exists())  # nothing was opened or made
        run.assert_not_called()
        [line] = [json.loads(run_log.JsonLines().format(record)) for record in logs.records]
        self.assertEqual(line["event"], "refused_to_start")
        self.assertIn(f"format {DATA_FORMAT + 1}", line["reason"])


@unittest.skipUnless(HAS_FASTAPI and HAS_PYPDF, "the smoke run needs the packages in requirements.txt")
class SmokeRunTests(unittest.TestCase):
    """deploy/smoke.py uploads a CV and confirms facts, so it must never run on real data."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("smoke", Path(__file__).parent / "deploy" / "smoke.py")
        self.smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.smoke)

    def test_it_refuses_a_folder_that_already_holds_data(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            import_facts(data / "workbench.db", FACTS)  # someone's facts, not confirmed yet
            with self.assertRaisesRegex(SystemExit, "REFUSED"):
                self.smoke.create(data, data / "smoke")
            with self.assertRaisesRegex(SystemExit, "REFUSED"):
                self.smoke.verify(data)
            self.assertEqual([item.name for item in data.iterdir()], ["workbench.db"])
            self.assertEqual({fact["status"] for fact in list_facts(data / "workbench.db")}, {"pending"})

    def test_it_runs_where_the_workbench_has_only_started_on_no_data(self):
        # As on the EC2 host after the first install: the company list and an empty notices folder.
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / self.smoke.DATA_MARKER).write_text("", encoding="utf-8")
            create_app(facts_db=data / "workbench.db", jobs_root=data / "jobs", profile_path=data / "cv-profile.json")
            self.assertEqual(sorted(item.name for item in data.iterdir()),
                             [".workbench-data", ".workbench-format", "listings.db", "unfinished"])
            with patch.object(self.smoke, "client_for", side_effect=RuntimeError("past the check")):
                with self.assertRaisesRegex(RuntimeError, "past the check"):
                    self.smoke.create(data, data / "smoke")
                (data / "unfinished" / "0123456789abcdef.json").write_text("{}", encoding="utf-8")  # a notice: real use
                with self.assertRaisesRegex(SystemExit, "REFUSED"):
                    self.smoke.create(data, data / "smoke")


if __name__ == "__main__":
    unittest.main()
