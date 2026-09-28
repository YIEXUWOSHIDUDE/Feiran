import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from deepseek_client import DeepSeekError
from facts import confirm_facts, import_facts, revise_fact
from cv_import import STRUCTURE_RULES
from cv_plan import PLAN_RULES
from gaps import EVIDENCE_RULES, SUGGEST_RULES
from matching import MATCH_RULES
from requirement_flow import FIND_RULES
from test_cv import FACTS as CV_FACTS, PROFILE, FakeChat, FakePrinter
from test_gaps import resume_ids, sent_lines
from test_listings import FakeBoards, posting

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
if HAS_FASTAPI:
    from fastapi.testclient import TestClient

    from web import create_app

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
        preview = self.client.get(f"/preview/{job_id}/en?token={TOKEN}")
        approved = self.cv_step(job_id, "approve")
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


if __name__ == "__main__":
    unittest.main()
