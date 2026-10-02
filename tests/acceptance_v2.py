"""V2 pilot acceptance on one machine: synthetic users only, a scripted model, no network.

    .venv/bin/python -m tests.acceptance_v2 --out /tmp/v2-acceptance [--chrome]

It serves the real V2 app over HTTP (Uvicorn on 127.0.0.1) and plays the checks of
docs/v2-plan.md section 6 against it:

  load       100 accounts, each with 1 profile, 20 facts and 5 jobs; 10 browsers at once reading
             and editing, touching all 100 accounts; every answer is checked for other users' data
             and every edit for lost updates. Latency, CPU time, memory and storage errors recorded.
  burst      10 users submit CV preparations at the same moment against a slow model double that
             also fails some calls; submit latency, how many run at once and whether a user keeps
             editing meanwhile; then the queue and quota limits refuse with 429.
  restart    a process running a task is killed; the next start marks the task interrupted, the
             same request runs it again once, and no second CV version appears.
  pdf        (with --chrome) English and Chinese CVs approved and printed by the local Chrome;
             pages, text, links and a rendered image are checked.
  migrate    (with --chrome) a single-user folder made by deploy/smoke.py is moved to V2 for a
             named owner: dry run first, V1 files left as they were, a backup made and checked,
             and the owner, signed in to V2, downloads the very PDFs approved in V1.

Sign-in goes through the test-only Cognito stand-in (tests/fake_identity.py), injected into the
app object here; the app has no such route. Numbers describe this machine and this run only:
they are measurements for the pilot's sizing decision, not a capacity promise.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import random
import re
import resource
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx2
import uvicorn

import tenant_store
from cv import print_with_chrome
from deepseek_client import DeepSeekError
from identity import Identity, OIDCSettings
from requirement_flow import propose_requirements
from tenant_store import UserWorkspace, set_setting
from tests.fake_identity import CLIENT_ID, DOMAIN, ISSUER, FakeCognito
from tests.test_cv import PROFILE, FakePrinter
from tests.test_cv_import import ANSWER, LINES, minimal_pdf
from tests.test_web import FakeDeepSeek
from v2_flow import auto_decide, make_handlers
from web_v2 import V2Settings, create_v2_app

REPO = Path(__file__).resolve().parent.parent
JD_EN = "Senior Backend Engineer\nRequirements:\n- Python services\n- SQL databases\n- Testing with pytest\n"
USERS, FACTS_EACH, JOBS_EACH, BROWSERS = 100, 20, 5, 10
MARKER = re.compile(r"Marker (\d{3}):")


def percentile(values, share):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))] if ordered else None


def summary(values):
    return {"n": len(values), "p50_ms": round(1000 * percentile(values, 0.5), 1) if values else None,
            "p95_ms": round(1000 * percentile(values, 0.95), 1) if values else None,
            "max_ms": round(1000 * max(values), 1) if values else None}


def max_rss_mb():
    """The process's peak memory so far (the harness and the server share one process)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    """The V2 app served by Uvicorn in this process, as one worker, the way the host runs it."""

    def __init__(self, database, model, printer, *, slots=(2, 1)):
        self.port = free_port()
        self.origin = f"http://localhost:{self.port}"
        self.cognito = FakeCognito()
        settings = OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, domain=DOMAIN, public_origin=self.origin)
        self.app = create_v2_app(database=database, identity=Identity(settings, database, post=self.cognito.post,
                                                                      keys=self.cognito.keys),
                                 settings=V2Settings(public_host="localhost", public_origin=self.origin),
                                 handlers=make_handlers(printer=printer), model=model, starter=[],
                                 runner_slots=slots)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False,
                                lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(30)

    def browser(self, subject):
        """A signed-in browser: its own cookies, the page's CSRF token in its headers."""
        client = httpx2.Client(base_url=self.origin, timeout=60)
        started = client.get("/login", params={"return_to": "/"})
        assert started.status_code == 303, started.text
        callback = client.get("/auth/callback", params=self.cognito.authorize(started.headers["location"], subject))
        assert callback.status_code == 303, callback.text
        token = re.search(r'name="workbench-token" content="([^"]+)"', client.get("/").text).group(1)
        client.headers["X-Workbench-Token"] = token
        return client


def seed_user(database, user_id, index):
    """One synthetic account: 20 confirmed facts marked with its number, an English profile and
    5 jobs with their requirements and a first CV draft. Written through the store directly."""
    user = UserWorkspace(database, user_id)
    facts = [{"id": f"fact-u{index:03d}-{k:02d}", "type": "experience",
              "text": f"Marker {index:03d}: built service {k} with Python and SQL.", "tags": ["Python", "SQL"]}
             for k in range(FACTS_EACH)]
    user.import_facts(facts)
    user.confirm_facts([(fact["id"], 1) for fact in facts])
    # Letters, not the number: name words are masked before any model call, and the number marks every fact.
    letters = chr(65 + index // 26) + chr(65 + index % 26)
    profile = {"profile_version": 1, "name": {"en": f"Synthetic Person {letters}"}, "contact": {},
               "sections": [{"kind": "experience", "entries": [
                   {"title": "Example Corp", "subtitle": "Engineer", "dates": "2025",
                    "facts": [fact["id"] for fact in facts]}]}]}
    user.save_profile("en", profile, expected_version=0)
    jobs = []
    for k in range(JOBS_EACH):
        jd = {"text": JD_EN, "title": f"Job {k}", "source": None, "captured_at": "2026-09-30T00:00:00+00:00"}
        job = user.create_job(jd, request_id=f"seed-{k}")
        candidates = propose_requirements({"jd": jd, "facts": [], "selected_requirements": []})
        user.save_requirements(job["job_id"], candidates, auto_decide(candidates), expected_version=0)
        user.create_draft(job["job_id"], "en", request_id=f"draft-{k}", expected_profile_version=1,
                          expected_facts=[(fact["id"], 1) for fact in facts])
        jobs.append(job["job_id"])
    return jobs


def foreign_markers(text, own):
    return {int(number) for number in MARKER.findall(text)} - {own}


def run_load(workdir, report):
    database = workdir / "load" / "v2.db"
    database.parent.mkdir(parents=True)
    tenant_store.initialize_store(database)
    for key, value in [("user_daily_units", "100"), ("site_daily_units", "10000"), ("registration_open", "1"),
                       ("max_accounts", str(USERS))]:
        set_setting(database, key, value)
    with Server(database, FakeDeepSeek(), FakePrinter()) as server:
        started = time.monotonic()
        browsers, jobs = [], []
        for index in range(USERS):
            browsers.append(server.browser(f"load-{index:03d}"))
        with tenant_store.transaction(database) as connection:
            ids = [row[0] for row in connection.execute("SELECT user_id FROM users ORDER BY created_at, rowid")]
        for index, user_id in enumerate(ids):
            jobs.append(seed_user(database, user_id, index))
        report["load_setup_s"] = round(time.monotonic() - started, 1)
        timings = {name: [] for name in ("jobs", "job", "facts", "preview", "edit", "confirm", "language")}
        problems = {"foreign_data": 0, "errors": 0, "lost_updates": 0}
        edits = {}
        lock = threading.Lock()
        cpu_before = time.process_time()

        def timed(name, call):
            begun = time.monotonic()
            response = call()
            elapsed = time.monotonic() - begun
            with lock:
                timings[name].append(elapsed)
            return response

        def browse(thread):
            rng = random.Random(thread)
            for round_number in range(30):
                index = thread + BROWSERS * (round_number % (USERS // BROWSERS))
                client, job_ids = browsers[index], jobs[index]
                job_id = rng.choice(job_ids)
                answers = [timed("jobs", lambda: client.get("/api/jobs")),
                           timed("job", lambda: client.get(f"/api/jobs/{job_id}")),
                           timed("facts", lambda: client.get("/api/facts")),
                           timed("preview", lambda: client.get(f"/preview/{job_id}/en"))]
                if round_number % 3 == 0:
                    fact = rng.choice(answers[2].json()["facts"])
                    answers.append(timed("edit", lambda: client.post(f"/api/facts/{fact['id']}/edit", json={
                        "expected_version": fact["version"], "text": fact["text"] + " Revised.", "tags": fact["tags"]})))
                    updated = answers[-1].json().get("fact", {})
                    if updated.get("version"):
                        with lock:
                            edits[(index, fact["id"])] = edits.get((index, fact["id"]), 1) + 1
                        answers.append(timed("confirm", lambda: client.post("/api/facts/confirm", json={
                            "refs": [f"{fact['id']}@{updated['version']}"]})))
                if round_number % 10 == 0:
                    answers.append(timed("language", lambda: client.post(f"/api/jobs/{job_id}/language",
                                                                         json={"language": "en"})))
                for response in answers:
                    with lock:
                        if response.status_code >= 400:
                            problems["errors"] += 1
                        if foreign_markers(response.text, index):
                            problems["foreign_data"] += 1

        with ThreadPoolExecutor(BROWSERS) as pool:
            list(pool.map(browse, range(BROWSERS)))
        for (index, fact_id), expected in edits.items():
            fact = UserWorkspace(database, ids[index]).get_fact(fact_id)
            if fact["version"] != expected:
                problems["lost_updates"] += 1
        reads = timings["jobs"] + timings["job"] + timings["facts"] + timings["preview"]
        report["load"] = {
            "accounts": USERS, "facts_each": FACTS_EACH, "jobs_each": JOBS_EACH, "browsers": BROWSERS,
            "reads": summary(reads), "by_route": {name: summary(values) for name, values in timings.items()},
            "edits_checked": len(edits), **problems,
            "cpu_s": round(time.process_time() - cpu_before, 1),
            "max_rss_mb": max_rss_mb(),
            "database_mb": round(database.stat().st_size / 1e6, 1),
            "target_read_p95_ms": 2000, "read_p95_met": percentile(reads, 0.95) < 2.0,
        }
        for browser in browsers:
            browser.close()


class SlowModel(FakeDeepSeek):
    """A model double with a fixed delay; every fourth call fails as rate limited. It counts how
    many calls are inside at once. It proves scheduling, not the real model's speed."""

    def __init__(self, delay):
        super().__init__()
        self.cv_structure = ANSWER
        self.delay = delay
        self.lock = threading.Lock()
        self.inside = self.most = self.calls = 0

    def __call__(self, messages, model, effort):
        with self.lock:
            self.calls += 1
            number = self.calls
            self.inside += 1
            self.most = max(self.most, self.inside)
        try:
            time.sleep(self.delay)
            if number % 4 == 0:
                raise DeepSeekError("injected rate limit", reason="rate_limited")
            return super().__call__(messages, model, effort)
        finally:
            with self.lock:
                self.inside -= 1


def run_burst(workdir, report):
    database = workdir / "burst" / "v2.db"
    database.parent.mkdir(parents=True)
    tenant_store.initialize_store(database)
    for key, value in [("user_daily_units", "20"), ("site_daily_units", "1000"), ("registration_open", "1")]:
        set_setting(database, key, value)
    model = SlowModel(0.3)
    with Server(database, model, FakePrinter()) as server:
        browsers = [server.browser(f"burst-{index:02d}") for index in range(11)]
        with tenant_store.transaction(database) as connection:
            ids = [row[0] for row in connection.execute("SELECT user_id FROM users ORDER BY created_at, rowid")]
        jobs = [seed_user(database, user_id, index) for index, user_id in enumerate(ids)]
        for browser in browsers:
            for stage in ("upload_parsing", "job_processing"):
                browser.post("/api/consent", json={"stage": stage, "version": 1})
        barrier = threading.Barrier(10)
        submits, statuses, editor = [], [], []
        stop = threading.Event()

        def submit(index):
            barrier.wait(10)
            begun = time.monotonic()
            response = browsers[index].post(f"/api/jobs/{jobs[index][0]}/cv/en/prepare")
            submits.append(time.monotonic() - begun)
            statuses.append(response.status_code)
            return response.json().get("task", {}).get("task_id")

        def keep_editing():
            client = browsers[10]
            while not stop.is_set():
                begun = time.monotonic()
                facts = client.get("/api/facts").json()["facts"]
                fact = facts[0]
                client.post(f"/api/facts/{fact['id']}/edit", json={"expected_version": fact["version"],
                                                                   "text": fact["text"] + " Again.", "tags": fact["tags"]})
                editor.append(time.monotonic() - begun)
                time.sleep(0.05)

        watcher = threading.Thread(target=keep_editing)
        watcher.start()
        with ThreadPoolExecutor(10) as pool:
            task_ids = list(pool.map(submit, range(10)))
        drained = server.app.state.runner.drain(120)
        stop.set()
        watcher.join()
        done = [browsers[index].get(f"/api/tasks/{task_id}").json()["task"]["status"] for index, task_id in enumerate(task_ids)]
        versions = [len(UserWorkspace(database, ids[index]).list_materials(jobs[index][0])) for index in range(10)]
        set_setting(database, "queue_limit", "3")
        refused = []
        original = server.app.state.runner.wake
        server.app.state.runner.wake = lambda: None  # keep them queued while counting refusals
        for index in range(10):
            refused.append(browsers[index].post(f"/api/jobs/{jobs[index][1]}/cv/en/prepare").status_code)
        server.app.state.runner.wake = original
        again = browsers[0].post(f"/api/jobs/{jobs[0][2]}/cv/en/prepare")
        server.app.state.runner.drain(120)
        set_setting(database, "user_daily_units", "1")  # below what one more preparation needs
        spent = browsers[5].post(f"/api/jobs/{jobs[5][3]}/cv/en/prepare")
        report["burst"] = {
            "users": 10, "model_delay_s": model.delay, "model_calls": model.calls, "model_most_at_once": model.most,
            "submit": summary(submits), "submit_statuses": sorted(set(statuses)), "all_drained": drained,
            "final_statuses": sorted(set(done)), "cv_versions_per_job": sorted(set(versions)),
            "editing_during_burst": summary(editor),
            "over_capacity_statuses": {str(code): refused.count(code) for code in sorted(set(refused))},
            "second_waiting_task_status": again.status_code, "quota_exhausted_status": spent.status_code,
            "quota_exhausted_code": spent.json().get("code"),
            "target_submit_p95_ms": 1000, "submit_p95_met": percentile(submits, 0.95) < 1.0,
        }
        for browser in browsers:
            browser.close()


HOLD = """
import sys, time
from pathlib import Path
from task_runner import TaskRunner
database = Path(sys.argv[1])
def hold(ctx):
    ctx.chat([{"role": "user", "content": "x"}], model="m", effort="none")
    print("holding", flush=True)
    time.sleep(600)
runner = TaskRunner(database, {"prepare_cv": hold}, poll_seconds=0.05,
                    model=lambda messages, model, effort: {"model": "m", "content": {}, "usage": {"prompt_tokens": 1}})
runner.start()
time.sleep(600)
"""


def run_restart(workdir, report):
    database = workdir / "restart" / "v2.db"
    database.parent.mkdir(parents=True)
    tenant_store.initialize_store(database)
    for key, value in [("user_daily_units", "20"), ("site_daily_units", "100")]:
        set_setting(database, key, value)
    user_id = tenant_store.provision_user(database, issuer=ISSUER, subject="restart")
    job_ids = seed_user(database, user_id, 0)
    user = UserWorkspace(database, user_id)
    user.give_consent("job_processing", 1)
    head = user.job_snapshot(job_ids[0])["cv"]["en"]["material"]["material_id"]
    request = {"job_id": job_ids[0], "language": "en", "base_head": head}
    task = user.submit_task("prepare_cv", key="restart-1", request=request, units=2, heavy="model",
                            job_id=job_ids[0], consent="job_processing")
    child = subprocess.Popen([sys.executable, "-c", HOLD, str(database)], cwd=REPO, stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline()
    before = user.get_task(task["task_id"])
    child.send_signal(signal.SIGKILL)
    child.wait(10)
    from task_runner import TaskRunner
    runner = TaskRunner(database, make_handlers(printer=FakePrinter()), poll_seconds=0.05, model=FakeDeepSeek())
    runner.start()
    try:
        after = user.get_task(task["task_id"])
        again = user.submit_task("prepare_cv", key="restart-1", request=request, units=2, heavy="model",
                                 job_id=job_ids[0], consent="job_processing")
        runner.wake()
        runner.drain(30)
        final = user.get_task(task["task_id"])
    finally:
        runner.stop(10)
    report["restart"] = {
        "child_said": line.strip(), "status_before_kill": before["status"], "cost_before_kill": before["cost"],
        "status_after_restart": after["status"], "cost_after_restart": after["cost"],
        "resubmitted_same_task": again["task_id"] == task["task_id"], "final_status": final["status"],
        "attempts": final["attempts"], "cv_versions": len(user.list_materials(job_ids[0])),
        "stages": [(stage["stage"], stage["status"], stage.get("reason_code"))
                   for stage in (user.note(job_ids[0], "cv-status-en") or {}).get("stages", [])],
        "quota_used": user.usage_today()["used"],
    }


def run_pdf(workdir, report):
    database = workdir / "pdf" / "v2.db"
    database.parent.mkdir(parents=True)
    tenant_store.initialize_store(database)
    for key, value in [("user_daily_units", "40"), ("site_daily_units", "100"), ("registration_open", "1")]:
        set_setting(database, key, value)
    model = FakeDeepSeek()
    model.cv_structure = ANSWER
    results = {}
    with Server(database, model, print_with_chrome) as server:
        client = server.browser("pdf-owner")
        for stage in ("upload_parsing", "job_processing"):
            client.post("/api/consent", json={"stage": stage, "version": 1})
        pdf = minimal_pdf([(x, 720 - 14 * index, text.replace("•", "-").replace("–", "-"))
                           for index, parts in enumerate(LINES) for x, text in zip((72, 430), parts)])
        task = follow(server, client, client.post("/api/cv/upload?language=en", content=pdf,
                                                  headers={"Content-Type": "application/pdf"}))
        upload = task["result"]["upload_id"]
        client.post(f"/api/cv/uploads/{upload}/save", json={"name": "Alex Example", "email": "alex@example.com",
                                                             "links": [{"label": "GitHub", "url": "https://github.com/alex-example"}]})
        with tenant_store.transaction(database) as connection:
            user_id = connection.execute("SELECT user_id FROM users").fetchone()[0]
        user = UserWorkspace(database, user_id)
        facts = client.get("/api/facts").json()["facts"]
        client.post("/api/facts/confirm", json={"refs": [f"{fact['id']}@{fact['version']}" for fact in facts]})
        zh_facts = [{"id": f"fact-zh-{k}", "type": "experience", "text": text, "tags": []}
                    for k, text in enumerate(["参与后端服务开发与测试，使用 Python 编写接口。", "为计费模块编写单元测试。"])]
        user.import_facts(zh_facts)
        user.confirm_facts([(fact["id"], 1) for fact in zh_facts])
        user.save_profile("zh", {"profile_version": 1, "name": {"zh": "示例候选人"}, "contact": {"email": "alex@example.com"},
                                 "sections": [{"kind": "education", "entries": [{"title": "示例大学", "subtitle": "计算机科学硕士",
                                                                                 "dates": "2026 - 2028"}]},
                                              {"kind": "experience", "entries": [{"title": "示例公司", "subtitle": "软件实习生",
                                                                                  "dates": "2025", "facts": [f["id"] for f in zh_facts]}]}]},
                          expected_version=0)
        text = (REPO / "examples" / "synthetic_jd_zh.txt").read_text(encoding="utf-8")
        for language, title in (("en", "Backend Intern"), ("zh", "后端开发实习生")):
            created = follow(server, client, client.post("/api/jobs", json={"title": title, "text": text if language == "zh" else JD_EN}))
            job_id = created["result"]["job_id"]
            client.post(f"/api/jobs/{job_id}/language", json={"language": language})
            follow(server, client, client.post(f"/api/jobs/{job_id}/cv/{language}/prepare"))
            view = client.get(f"/api/jobs/{job_id}").json()
            approved = client.post(f"/api/jobs/{job_id}/cv/{language}/approve",
                                   json={"expected_content_sha256": view["cv"][language]["content_sha256"]})
            assert approved.status_code == 200, approved.text
            exported = follow(server, client, client.post(f"/api/jobs/{job_id}/cv/{language}/export"))
            download = client.get(f"/download/{job_id}/{language}.pdf")
            path = workdir / f"v2-{language}.pdf"
            path.write_bytes(download.content)
            results[language] = inspect_pdf(path, language, exported)
        client.close()
    report["pdf"] = results


def follow(server, client, response):
    assert response.status_code in (200, 202), response.text
    body = response.json()
    if "task" not in body:
        return {"result": body}
    server.app.state.runner.drain(120)
    task = client.get(f"/api/tasks/{body['task']['task_id']}").json()["task"]
    assert task["status"] == "succeeded", task
    return task


def inspect_pdf(path, language, task):
    from pypdf import PdfReader
    import unicodedata
    reader = PdfReader(str(path))
    text = unicodedata.normalize("NFKC", "".join(page.extract_text() for page in reader.pages))
    links = [annotation.get_object().get("/A", {}).get("/URI") for page in reader.pages
             for annotation in (page.get("/Annots") or [])]
    expected = (["Built REST APIs", "Wrote unit tests", "Example Corp"] if language == "en"
                else ["示例候选人", "示例公司", "单元测试"])
    image = None
    pdftoppm = shutil.which("pdftoppm") or next((str(candidate) for candidate in Path.home().glob(
        ".cache/codex-runtimes/*/dependencies/bin/override/pdftoppm")), None)
    if pdftoppm:
        subprocess.run([pdftoppm, "-png", "-r", "70", "-f", "1", "-l", "1", str(path), str(path.with_suffix(""))],
                       check=True, capture_output=True)
        image = str(path.with_suffix("")) + "-1.png"
    return {"pages": len(reader.pages), "task_pages": (task.get("result") or {}).get("pages"),
            "has_text": {word: word in text.replace(" ", "") or word in text for word in expected},
            "watermark": ("DRAFT" in text) or ("草稿" in text), "links": [link for link in links if link],
            "image": image, "bytes": path.stat().st_size}


def fingerprints(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*")) if path.is_file()}


def run_migrate(workdir, report):
    import v2_backup
    import v2_migrate
    from pypdf import PdfReader
    root = workdir / "migrate"
    data = root / "data"
    data.mkdir(parents=True)
    (data / ".workbench-data").touch()
    made = subprocess.run([sys.executable, "deploy/smoke.py", "create", "--data", str(data), "--out", str(root / "smoke")],
                          cwd=REPO, capture_output=True, text=True)
    assert made.returncode == 0, made.stderr[-2000:]
    before = fingerprints(data)
    with contextlib.redirect_stdout(io.StringIO()):
        dry_exit = v2_migrate.main(["run", "--v1-data", str(data), "--v2-db", str(data / "v2.db"),
                                    "--owner-issuer", ISSUER, "--owner-subject", "migrated-owner", "--dry-run"])
    dry_wrote = (data / "v2.db").exists()
    migrated = v2_migrate.migrate(data, data / "v2.db", issuer=ISSUER, subject="migrated-owner")
    after = fingerprints(data)
    archive = root / "backup.tar.gz"
    backup = v2_backup.create_backup(data, archive, "acceptance")
    downloads = {}
    with Server(data / "v2.db", FakeDeepSeek(), FakePrinter()) as server:
        client = server.browser("migrated-owner")
        for job in client.get("/api/jobs").json()["jobs"]:
            for language in ("en", "zh"):
                answer = client.get(f"/download/{job['job_id']}/{language}.pdf")
                if answer.status_code != 200:
                    continue
                v1 = data / "jobs" / job["job_id"] / f"cv-final-{language}.pdf"
                downloads[language] = {"same_bytes_as_v1": v1.is_file() and v1.read_bytes() == answer.content,
                                       "pages": len(PdfReader(io.BytesIO(answer.content)).pages)}
        probe = httpx2.Client(base_url=server.origin, timeout=60)  # registration stays closed after a migration
        started = probe.get("/login", params={"return_to": "/"})
        closed = probe.get("/auth/callback", params=server.cognito.authorize(started.headers["location"], "someone-else"))
        probe.close()
        tenant_store.provision_user(data / "v2.db", issuer=ISSUER, subject="someone-else")
        stranger = server.browser("someone-else")
        foreign = sorted({stranger.get(f"/download/{folder.name}/{language}.pdf").status_code
                          for folder in (data / "jobs").iterdir() if folder.is_dir() for language in ("en", "zh")})
        client.close()
        stranger.close()
    report["migrate"] = {
        "dry_run_exit": dry_exit, "dry_run_wrote_v2_db": dry_wrote,
        "v1_files_changed": sorted(name for name in before if before[name] != after.get(name)),
        "files_added": sorted(name for name in after if name not in before),
        "migrated": migrated, "backup_problems": backup["problems"],
        "archive_problems": v2_backup.inspect_archive(archive)["problems"],
        "verify_problems": v2_backup.verify_data(data)["problems"],
        "owner_downloads": downloads, "new_identity_while_closed": closed.status_code,
        "other_account_download_statuses": foreign,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chrome", action="store_true", help="also print CVs with the local Chrome")
    parser.add_argument("--only", choices=("load", "burst", "restart", "pdf", "migrate"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="work-", dir=args.out))
    report = {"python": sys.version.split()[0], "platform": sys.platform, "cpus": os.cpu_count()}
    steps = {"load": run_load, "burst": run_burst, "restart": run_restart}
    if args.chrome:
        steps["pdf"] = run_pdf
        steps["migrate"] = run_migrate
    for name, step in steps.items():
        if args.only and name != args.only:
            continue
        began = time.monotonic()
        step(workdir, report)
        report.setdefault("durations_s", {})[name] = round(time.monotonic() - began, 1)
        print(f"{name}: done", flush=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
