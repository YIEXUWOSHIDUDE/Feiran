import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from facts import confirm_facts, import_facts
from test_cv import FACTS as CV_FACTS, PROFILE, FakeChat, FakePrinter
from test_listings import FakeBoards, posting

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
if HAS_FASTAPI:
    from fastapi.testclient import TestClient

    from web import create_app

TOKEN = "test-token"
FACTS = [
    {"id": "fact-web-python", "type": "skill", "text": "Built services in Python.", "tags": ["Python"]},
    {"id": "fact-web-sql", "type": "skill", "text": "Wrote SQL queries.", "tags": ["SQL"]},
]


@unittest.skipUnless(HAS_FASTAPI, "web tests need the packages in requirements.txt")
class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database = root / "workbench.db"
        profile = root / "cv-profile.json"
        profile.write_text(json.dumps(PROFILE, ensure_ascii=False), encoding="utf-8")
        self.chat = FakeChat({"fact-intern-api": "For an internal tool, built REST APIs."})
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
        with self.assertRaises(AssertionError):
            self.cv_step(job_id, "approve")
        self.cv_step(job_id, "draft")
        tailored = self.cv_step(job_id, "tailor")
        preview = self.client.get(f"/preview/{job_id}/en?token={TOKEN}")
        approved = self.cv_step(job_id, "approve")
        final = self.cv_step(job_id, "export")
        download = self.client.get(f"/download/{job_id}/en.pdf?token={TOKEN}")
        self.assertEqual(tailored["rewrites"], [{
            "fact_id": "fact-intern-api",
            "from": "Built REST APIs for an internal tool.",
            "to": "For an internal tool, built REST APIs.",
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


if __name__ == "__main__":
    unittest.main()
