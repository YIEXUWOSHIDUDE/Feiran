"""The JD details panel's rule (jd_diagnostics), the batch page and the batch ZIP (batch_zip) in
both apps: what the ZIP holds is exactly what the page showed ready, in safe folders, with a
manifest of the whole batch; anything changed, missing, malformed or another user's refuses the
whole download; reading and packing call no model and use no quota. Synthetic jobs and CVs; the
model and the printer are stand-ins."""
import io
import json
import threading
import zipfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from feiran import batch_zip, web
from feiran.jd_diagnostics import jd_diagnostics
from feiran.job_search import prepare_pasted_jd, prepare_review_input
from tests import test_bilingual_web, test_guest_workspace, test_web, test_web_v2
from tests.test_sponsor_ui import NODE, SponsorUITests

SHA = "a" * 64
FULL = ("Backend Engineer\nResponsibilities\nBuild service APIs and test production systems with the team. " * 3
        + "\nQualifications\nExperience with Python, SQL and distributed systems. " * 3)


def snapshot(**fields):
    return {"title": "Backend Engineer", "company": "Example", "text": FULL, "provider": "web",
            "source": "https://careers.example.org/job/42", "captured_at": "2026-10-24T00:00:00+00:00",
            "extraction_method": "jobposting-jsonld"} | fields


class JDDiagnosticsTests(unittest.TestCase):
    def test_a_complete_posting_has_nothing_to_warn_but_its_missing_posting_date(self):
        facts = jd_diagnostics(snapshot())
        self.assertEqual((facts["duties_detected"], facts["qualifications_detected"]), (True, True))
        self.assertEqual(facts["notes"], ["no_posted_at"])
        self.assertEqual((facts["reader"], facts["source"]), ("structured", "https://careers.example.org/job/42"))
        self.assertIsNone(facts["posted_at"])  # never the capture time
        self.assertEqual(facts["captured_at"], "2026-10-24T00:00:00+00:00")
        self.assertEqual(jd_diagnostics(snapshot(posted_at="2026-10-01"))["posted_at"], "2026-10-01")

    def test_missing_duties_or_qualifications_are_named_only_as_not_identified(self):
        duties_only = "Responsibilities\n" + "Build service APIs and own their tests every week. " * 10
        facts = jd_diagnostics(snapshot(text=duties_only, provider="greenhouse"))
        self.assertEqual((facts["duties_detected"], facts["qualifications_detected"]), (True, False))
        self.assertIn("no_qualifications", facts["notes"])
        qualifications_only = "Qualifications\n" + "Experience with Python and SQL is required here. " * 10
        facts = jd_diagnostics(snapshot(text=qualifications_only, provider="lever"))
        self.assertEqual((facts["duties_detected"], facts["qualifications_detected"]), (False, True))
        self.assertIn("no_duties", facts["notes"])
        self.assertEqual(facts["reader"], "job_board")

    def test_a_short_or_cut_off_web_text_says_so(self):
        facts = jd_diagnostics(snapshot(text="Join our team and build great things. Read more"))
        self.assertTrue({"short", "truncated", "web_summary", "no_duties", "no_qualifications"} <= set(facts["notes"]))
        self.assertEqual(facts["characters"], len("Join our team and build great things. Read more"))

    def test_pasted_text_without_a_source_and_an_old_snapshot_fall_back_naturally(self):
        pasted = prepare_pasted_jd(FULL, "Engineer", None, None, None)["jd"]
        facts = jd_diagnostics(pasted)
        self.assertEqual((facts["reader"], facts["source"]), ("pasted", None))
        self.assertIn("no_source", facts["notes"])
        self.assertNotIn("web_summary", facts["notes"])  # pasted text is never refused for it
        old = {"title": "Engineer", "text": FULL}  # saved by an early release: no provider, method or times
        facts = jd_diagnostics(old)
        self.assertEqual(facts["reader"], "unknown")
        self.assertTrue({"no_source", "no_posted_at", "no_captured_at"} <= set(facts["notes"]))
        for broken in (None, [], {"text": None}, {"text": 7, "source": 3}):
            self.assertEqual(jd_diagnostics(broken)["characters"], 0)

    def test_a_skill_listed_under_requirements_is_not_a_duty(self):
        # Found in review by Codex, word for word: "Testing" under Requirements was taken for a duty.
        exact = "Senior Backend Engineer\nRequirements:\n- Python services\n- SQL databases\n- Testing with pytest\n"
        facts = jd_diagnostics(snapshot(text=exact, provider="manual"))
        self.assertEqual((facts["duties_detected"], facts["qualifications_detected"]), (False, True))
        self.assertIn("no_duties", facts["notes"])
        self.assertNotIn("no_qualifications", facts["notes"])
        # (text, duties detected, qualifications detected)
        for text, duties, qualifications in [
                ("Qualifications\n- Design skills\n- Testing with pytest", False, True),
                ("About us\nWe build great products.\nRequirements\n- Python", False, True),
                ("Responsibilities:\nQualifications: Python", False, True),  # a heading with nothing under it
                ("Design skills and Python.", False, False),
                ("The latest contest results.", False, False),
                ("任职要求：\n- 参与过开源项目", False, True),
                # Duties stated as duties are still found.
                ("Responsibilities\n- Python services\nQualifications\n- Testing with pytest", True, True),
                ("Responsibilities: Build APIs and test them", True, False),
                ("We build backend services. Must have experience with Python.", True, True),
                ("You'll design and maintain our billing APIs.", True, False),
                ("Designing APIs for payments.", True, False),
                ("岗位职责：\n1. 负责后端开发\n任职要求：\n1. 熟悉Python", True, True),
                ("公司简介，参与开源社区，熟悉Python", True, True)]:
            facts = jd_diagnostics(snapshot(text=text, provider="manual"))
            self.assertEqual((facts["duties_detected"], facts["qualifications_detected"]), (duties, qualifications), text)
            self.assertEqual("no_duties" in facts["notes"], not duties, text)

    def test_an_unsafe_source_is_never_a_link_and_html_stays_text(self):
        for source in ("javascript:alert(1)", "data:text/html,<b>x</b>", "file:///etc/passwd", "https://", "ftp://x.example/1"):
            facts = jd_diagnostics(snapshot(source=source, requested_url=source))
            self.assertEqual((facts["source"], facts["requested_url"]), (None, None), source)
            self.assertIn("unsafe_source", facts["notes"])
        html = '<script>alert(1)</script><img src=x onerror="alert(2)">'
        self.assertEqual(jd_diagnostics(snapshot(text=FULL + html))["characters"], len(FULL + html))


def cv_entry(sha=SHA, *, stale=False, approved=True, pdf=b"%PDF-1.4 synthetic"):
    return {"content_sha256": sha, "stale": stale, "approved": approved, "pdf": pdf, "pages": 1,
            "stages": [{"stage": "draft", "status": "done"}]}


class BatchRuleTests(unittest.TestCase):
    def test_ids_are_deduplicated_capped_and_anything_malformed_refuses_everything(self):
        ids, ready = batch_zip.parse_request("job-1, job-2,job-1,,", [f"job-2:en:{SHA}", f"job-2:en:{SHA}"])
        self.assertEqual((ids, ready), (["job-1", "job-2"], {("job-2", "en", SHA)}))
        for jobs, items in [("", []), ("../etc", []), ("a/b", []), ("job-1", ["job-2:en:" + SHA]),
                            ("job-1", ["job-1:fr:" + SHA]), ("job-1", ["job-1:en:" + "A" * 64]),
                            ("job-1", ["job-1:en"]), ("x" * 65, [])]:
            with self.subTest(jobs=jobs, items=items), self.assertRaises(batch_zip.BatchError) as caught:
                batch_zip.parse_request(jobs, items)
            self.assertEqual((caught.exception.status, str(caught.exception)), (404, batch_zip.NOT_FOUND))
        self.assertEqual(len(batch_zip.parse_request(",".join(["j1"] * 50), [])[0]), 1)  # repeats are not counted
        with self.assertRaises(batch_zip.BatchError) as caught:
            batch_zip.parse_request(",".join(f"j{n}" for n in range(21)), [])
        self.assertEqual(caught.exception.status, 400)

    def test_states_follow_the_current_approved_version_only(self):
        self.assertEqual(batch_zip.state_of({"content_sha256": None}), "not_prepared")
        self.assertEqual(batch_zip.state_of(cv_entry(stale=True)), "stale")
        self.assertEqual(batch_zip.state_of(cv_entry(approved=False)), "not_approved")
        self.assertEqual(batch_zip.state_of(cv_entry(pdf=None)), "approved_no_pdf")
        self.assertEqual(batch_zip.state_of(cv_entry()), "ready")

    def test_members_are_safe_names_in_one_folder_per_job_with_a_manifest_of_everything(self):
        jobs = [{"job_id": "job-1", "company": "../../Evil\\Co", "title": "/abs/../Role\r\n", "cvs": {
                    "en": cv_entry(), "zh": cv_entry("b" * 64, approved=False, pdf=b"old approved PDF")}},
                {"job_id": "job-2", "company": None, "title": None, "cvs": {"en": {"content_sha256": None}}}]
        data, manifest = batch_zip.build(jobs, {("job-1", "en", SHA)})
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        self.assertEqual(len(names), 2)
        for name in names:
            self.assertTrue(name.startswith("Feiran-CVs/"), name)
            self.assertFalse(any(part in ("", ".", "..") or part.startswith(".") for part in name.split("/")), name)
            self.assertNotIn("\\", name)
            self.assertFalse(any(ord(char) < 32 for char in name))
            self.assertEqual(name.count("/"), 1 if name.endswith("manifest.json") else 2)
        pdf = next(name for name in names if name.endswith(".pdf"))
        self.assertEqual(archive.read(pdf), b"%PDF-1.4 synthetic")
        self.assertNotIn(b"old approved PDF", b"".join(archive.read(name) for name in names))
        self.assertEqual(json.loads(archive.read("Feiran-CVs/manifest.json")), manifest)
        self.assertEqual((manifest["included"], manifest["total"]), (1, 3))
        states = [(job["job_id"], cv["language"], cv["state"], cv["file"]) for job in manifest["jobs"] for cv in job["cvs"]]
        self.assertEqual(states, [("job-1", "en", "ready", pdf), ("job-1", "zh", "not_approved", None),
                                  ("job-2", "en", "not_prepared", None)])
        self.assertEqual(manifest["jobs"][0]["cvs"][0]["version"], SHA[:8])

    def test_a_different_ready_set_none_ready_or_too_large_sends_nothing(self):
        jobs = [{"job_id": "job-1", "company": "A", "title": "B", "cvs": {"en": cv_entry()}}]
        for expected in (set(), {("job-1", "en", "c" * 64)}, {("job-1", "en", SHA), ("job-1", "zh", SHA)}):
            with self.assertRaises(batch_zip.BatchError) as caught:
                batch_zip.build(jobs, expected)
            self.assertEqual((caught.exception.status, str(caught.exception)), (409, batch_zip.CHANGED))
        nothing = [{"job_id": "job-1", "company": "A", "title": "B", "cvs": {"en": cv_entry(approved=False)}}]
        with self.assertRaises(batch_zip.BatchError) as caught:
            batch_zip.build(nothing, set())
        self.assertEqual((caught.exception.status, str(caught.exception)), (409, batch_zip.NONE_READY))
        with patch.object(batch_zip, "MAX_BYTES", 4), self.assertRaises(batch_zip.BatchError) as caught:
            batch_zip.build(jobs, {("job-1", "en", SHA)})
        self.assertEqual(caught.exception.status, 413)


def state(cv):
    """The batch page's rule (cvState in app.js), from a job's page view."""
    if not cv.get("head") or not cv.get("content_sha256"):
        return "not_prepared"
    if cv["stale"]:
        return "stale"
    if cv["head"] != "approved":
        return "not_approved"
    return "ready" if cv["final_pdf"] else "approved_no_pdf"


def shown(views):
    """What the batch page shows: per job, the languages counted (batchItems), and the ready keys."""
    items, ready = [], []
    for view in views:
        languages = [language for language in ("en", "zh") if (view["cv"].get(language) or {}).get("head")]
        for language in languages or [view["language"]]:
            cv = view["cv"].get(language) or {}
            items.append((view["job_id"], language, state(cv)))
            if state(cv) == "ready":
                ready.append((f"{view['job_id']}:{language}:{cv['content_sha256']}", cv["download_name"]))
    return items, ready


def unzip(response):
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    manifest = json.loads(archive.read("Feiran-CVs/manifest.json"))
    pdfs = {name: archive.read(name) for name in archive.namelist() if name.endswith(".pdf")}
    return manifest, pdfs


class V1BatchTests(test_bilingual_web.BilingualWebTests):
    """Five jobs in both languages, in mixed states, on the single-user app."""

    def jobs(self, count):
        """Jobs added as the page adds them: requirements found and the English CV prepared."""
        ids = []
        self.chat.requirement_lines = {"Experience writing Python services"}
        for n in range(count):
            created = self.client.post("/api/jobs", json={"title": f"Engineer {n}", "company": f"Company {n}",
                                                          "text": "Requirements:\n- Experience writing Python services"})
            self.assertEqual(created.status_code, 200, created.text)
            job_id = created.json()["job_id"]
            self.assertIsNotNone(self.cv(job_id, "en")["head"])
            ids.append(job_id)
        return ids

    def saved_only(self):
        """A job saved with nothing prepared for it."""
        return self.app.state.workspace.create_job(prepare_review_input(
            prepare_pasted_jd("Requirements:\n- Python", "Engineer", "Company", None, "Shanghai")))

    def post(self, job_id, language, action, body=None):
        response = self.client.post(f"/api/jobs/{job_id}/cv/{language}/{action}", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def cv(self, job_id, language):
        return self.client.get(f"/api/jobs/{job_id}").json()["cv"][language]

    def approve(self, job_id, language, export=True):
        self.post(job_id, language, "pages")
        self.post(job_id, language, "approve", {"expected_content_sha256": self.cv(job_id, language)["content_sha256"],
                                                "allow_multiple_pages": True})
        if export:
            self.post(job_id, language, "export")

    def batch(self, ids, ready, token=test_web.TOKEN):
        params = [("jobs", ",".join(ids))] + [("ready", key) for key in ready] + ([("token", token)] if token else [])
        return self.client.get("/download/batch.zip", params=params)

    def files(self):
        return sorted((path.relative_to(self.root).as_posix(), path.stat().st_mtime_ns)
                      for path in self.root.rglob("*") if path.is_file())

    def test_five_jobs_in_both_languages_download_exactly_what_the_page_shows(self):
        self.upload()
        ready_both, approved_only, edited, chinese_only = self.jobs(4)
        untouched = self.saved_only()
        ids = [ready_both, approved_only, untouched, edited, chinese_only]
        self.post(ready_both, "zh", "prepare")
        for language in ("en", "zh"):
            self.approve(ready_both, language)
        self.approve(approved_only, "en", export=False)
        self.post(approved_only, "zh", "prepare")
        self.approve(edited, "en")
        old_pdf = self.client.get(f"/download/{edited}/en.pdf", params={"token": test_web.TOKEN}).content
        line = self.cv(edited, "en")["lines"][0]
        self.post(edited, "en", f"lines/{line['block']}", {"text": line["text"] + " (Updated)", "expected_text": line["text"]})
        self.post(chinese_only, "zh", "prepare")
        self.approve(chinese_only, "zh")
        views = [self.client.get(f"/api/jobs/{job_id}").json() for job_id in ids]
        items, ready = shown(views)
        self.assertEqual(items, [(ready_both, "en", "ready"), (ready_both, "zh", "ready"),
                                 (approved_only, "en", "approved_no_pdf"), (approved_only, "zh", "not_approved"),
                                 (untouched, views[2]["language"], "not_prepared"), (edited, "en", "not_approved"),
                                 (chinese_only, "en", "not_approved"), (chinese_only, "zh", "ready")])
        sent, before = len(self.chat.sent), self.files()
        response = self.batch(ids, [key for key, _ in ready])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((len(self.chat.sent), self.files()), (sent, before))  # nothing written, no model asked
        self.assertIn('filename="Feiran-CVs-', response.headers["content-disposition"])
        self.assertTrue(response.headers["content-disposition"].endswith('-3.zip"'))
        manifest, pdfs = unzip(response)
        self.assertEqual(sorted(name.rsplit("/", 1)[1] for name in pdfs), sorted(name for _, name in ready))
        for key, name in ready:
            job_id, language, _ = key.split(":")
            single = self.client.get(f"/download/{job_id}/{language}.pdf", params={"token": test_web.TOKEN})
            self.assertEqual(next(data for path, data in pdfs.items() if path.endswith("/" + name)), single.content)
        self.assertEqual([(job["job_id"], cv["language"], cv["state"]) for job in manifest["jobs"] for cv in job["cvs"]], items)
        self.assertEqual((manifest["included"], manifest["total"]), (3, 8))
        edited_entry = next(job for job in manifest["jobs"] if job["job_id"] == edited)["cvs"][0]
        self.assertEqual((edited_entry["file"], edited_entry["content_sha256"]),
                         (None, self.cv(edited, "en")["content_sha256"]))
        self.assertTrue(old_pdf.startswith(b"%PDF"))  # the edited job's old PDF existed, and is not in the ZIP
        self.assertEqual(len(pdfs), 3)

    def test_a_change_after_the_page_read_the_batch_refuses_the_download(self):
        job_id, = self.jobs(1)
        self.approve(job_id, "en")
        seen = f"{job_id}:en:{self.cv(job_id, 'en')['content_sha256']}"
        line = self.cv(job_id, "en")["lines"][0]
        self.post(job_id, "en", f"lines/{line['block']}", {"text": line["text"] + " (Updated)", "expected_text": line["text"]})
        refused = self.batch([job_id], [seen])
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, batch_zip.CHANGED))
        nothing = self.batch([job_id], [])
        self.assertEqual((nothing.status_code, nothing.json()["error"]), (409, batch_zip.NONE_READY))
        self.approve(job_id, "en")
        current = f"{job_id}:en:{self.cv(job_id, 'en')['content_sha256']}"
        self.assertNotEqual(current, seen)
        self.assertEqual(self.batch([job_id], [seen]).status_code, 409)  # never the newer CV in place of the one seen
        self.assertEqual(self.batch([job_id], [current]).status_code, 200)

    def test_a_stale_cv_is_not_ready_and_a_profile_change_refuses_a_ready_set(self):
        self.upload()
        job_id, = self.jobs(1)
        self.post(job_id, "zh", "prepare")
        self.approve(job_id, "zh")
        seen = f"{job_id}:zh:{self.cv(job_id, 'zh')['content_sha256']}"
        self.upload(line="编程语言：Go")  # the Chinese profile changed: its CVs are out of date
        self.assertEqual(state(self.cv(job_id, "zh")), "stale")
        self.assertEqual(self.batch([job_id], [seen]).status_code, 409)
        self.assertEqual(self.batch([job_id], []).json()["error"], batch_zip.NONE_READY)

    def test_two_jobs_with_the_same_company_and_title_made_the_same_day_keep_their_own_folders(self):
        # Found in a real-browser check: a single-user job ID starts with its date, so the short
        # ID in file names is the same for both; the folders use the whole ID.
        ids = []
        self.chat.requirement_lines = {"Experience writing Python services"}
        for _ in range(2):
            ids.append(self.client.post("/api/jobs", json={"title": "Engineer", "company": "Same Co", "text":
                                                           "Requirements:\n- Experience writing Python services"}).json()["job_id"])
            self.approve(ids[-1], "en")
        self.assertEqual(ids[0][:8], ids[1][:8])
        _, ready = shown([self.client.get(f"/api/jobs/{job_id}").json() for job_id in ids])
        manifest, pdfs = unzip(self.batch(ids, [key for key, _ in ready]))
        self.assertEqual(len(pdfs), 2)
        self.assertEqual(len({path.rsplit("/", 1)[0] for path in pdfs}), 2)
        self.assertEqual([job["folder"] for job in manifest["jobs"]], [f"Feiran-CVs/Same Co-Engineer-{job_id}" for job_id in ids])

    def packed_while_a_fact_changes(self, ids, ready):
        """Download the batch while, on another thread, the fact both jobs' CVs use is edited
        through the app the moment the first job's CV has been checked against the facts (found
        in review by Codex). Returns the download, the edit's answer, and whether the edit had to
        wait at the app's change boundary instead of landing in the middle of the read."""
        original, checked, edit_done = web.verify_draft, [], threading.Event()
        with TestClient(self.app, base_url="http://127.0.0.1:8765", headers={"X-Workbench-Token": test_web.TOKEN}) as client, \
                ThreadPoolExecutor(1) as pool:
            def edit():
                try:
                    return client.post("/api/facts/fact-en/edit", json={
                        "text": "Languages: Python and SQL", "tags": ["Python"], "expected_version": 1})
                finally:
                    edit_done.set()

            def interleave(*args, **kwargs):
                result = original(*args, **kwargs)
                checked.append(True)
                if len(checked) == 1:
                    checked.append(pool.submit(edit))
                    # A real second thread: it either gets its change in now, or is held at the
                    # one-change-at-a-time boundary until this read is over.
                    edit_done.wait(1.5)
                    checked.append(not edit_done.is_set() and self.app.state.waiting_changes == 1)
                return result

            with patch.object(web, "verify_draft", interleave):
                response = client.get("/download/batch.zip", params=[("jobs", ",".join(ids)), ("token", test_web.TOKEN)]
                                      + [("ready", key) for key in ready])
            edited = checked[1].result(10)
        return response, edited, checked[2]

    def two_jobs_on_one_fact(self):
        ids = self.jobs(2)
        for job_id in ids:
            self.approve(job_id, "en")
        _, ready = shown([self.client.get(f"/api/jobs/{job_id}").json() for job_id in ids])
        self.assertEqual(len(ready), 2)
        return ids, [key for key, _ in ready]

    def test_a_fact_edited_while_packing_waits_so_the_zip_is_all_before_it(self):
        ids, ready = self.two_jobs_on_one_fact()
        response, edited, waited = self.packed_while_a_fact_changes(ids, ready)
        self.assertTrue(waited)  # the edit could not land between the two jobs' checks
        self.assertEqual(edited.status_code, 200, edited.text)  # and went in right after
        self.assertEqual(response.status_code, 200, response.text)
        manifest, pdfs = unzip(response)
        self.assertEqual([cv["state"] for job in manifest["jobs"] for cv in job["cvs"]], ["ready", "ready"])
        self.assertEqual(len(pdfs), 2)
        # Afterwards both are out of date, so what the page saw before is refused.
        self.assertEqual([state(self.cv(job_id, "en")) for job_id in ids], ["stale", "stale"])
        refused = self.batch(ids, ready)
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, batch_zip.CHANGED))
        self.assertEqual(self.batch(ids, []).json()["error"], batch_zip.NONE_READY)

    def test_a_batch_state_that_never_existed_is_never_sent(self):
        # The reviewer's exact case: only the first job sent as ready. Before the edit both were
        # ready, after it both are stale; [ready, stale] never existed and must not be served.
        ids, ready = self.two_jobs_on_one_fact()
        response, edited, waited = self.packed_while_a_fact_changes(ids, ready[:1])
        self.assertTrue(waited)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual((response.status_code, response.json()["error"]), (409, batch_zip.CHANGED))

    def test_unknown_malformed_and_tokenless_requests_get_nothing(self):
        job_id, = self.jobs(1)
        self.approve(job_id, "en")
        ready = [f"{job_id}:en:{self.cv(job_id, 'en')['content_sha256']}"]
        for ids, keys in [([job_id, "20260101-000000-abcdef"], ready), ([job_id, "../jobs"], ready),
                          ([job_id], [ready[0] + "x"])]:
            refused = self.batch(ids, keys)
            self.assertEqual((refused.status_code, refused.json()["error"]), (404, batch_zip.NOT_FOUND), ids)
        self.assertEqual(self.batch([job_id], ready, token=None).status_code, 403)
        self.assertEqual(self.batch([job_id], ready, token="wrong").status_code, 403)
        self.assertEqual(self.batch([job_id, job_id], ready).status_code, 200)  # a repeated ID counts once
        self.assertEqual(self.batch([f"j{n}" for n in range(21)], []).status_code, 400)


class V2BatchTests(test_web_v2.V2Case):
    def printed(self, client, headers, job_id):
        self.assertEqual(self.approve(client, headers, job_id).status_code, 200)
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers))
        self.assertEqual(task["status"], "succeeded", task)

    def second_job(self, client, headers):
        created = client.post("/api/jobs", headers=headers, json={
            "title": "Data Intern", "company": "Other Co", "text": test_web_v2.JD_TEXT + "\n- 熟悉数据分析"})
        return self.settle(client, headers, created)["result"]["job_id"]

    def batch(self, client, headers, ids, ready):
        return client.get("/download/batch.zip", headers=headers,
                          params=[("jobs", ",".join(ids))] + [("ready", key) for key in ready])

    def views(self, client, headers, ids):
        return [client.get(f"/api/jobs/{job_id}", headers=headers).json() for job_id in ids]

    def test_the_zip_matches_the_page_and_reading_or_packing_uses_no_quota(self):
        client, headers = self.ready_user()
        printed, waiting = self.new_job(client, headers), self.second_job(client, headers)
        self.printed(client, headers, printed)
        items, ready = shown(self.views(client, headers, [printed, waiting]))
        self.assertEqual([item[2] for item in items], ["ready", "not_approved"])
        me, sent, tasks = client.get("/api/me", headers=headers).json()["usage"], len(self.chat.sent), self.task_count()
        response = self.batch(client, headers, [printed, waiting, printed], [key for key, _ in ready])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((client.get("/api/me", headers=headers).json()["usage"], len(self.chat.sent), self.task_count()),
                         (me, sent, tasks))
        manifest, pdfs = unzip(response)
        (path, data), = pdfs.items()
        self.assertEqual(path.rsplit("/", 1)[1], ready[0][1])
        self.assertEqual(data, client.get(f"/download/{printed}/en.pdf", headers=headers).content)
        self.assertEqual([(job["job_id"], cv["state"]) for job in manifest["jobs"] for cv in job["cvs"]],
                         [(printed, "ready"), (waiting, "not_approved")])
        self.assertTrue(manifest["jobs"][0]["folder"].startswith("Feiran-CVs/示例公司-后端开发实习生-"))

    def test_an_edit_since_the_page_read_it_refuses_and_an_old_pdf_is_never_sent(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        self.printed(client, headers, job_id)
        (seen, _), = shown(self.views(client, headers, [job_id]))[1]
        line = client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]["lines"][0]
        edited = client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                             json={"text": line["text"] + " Updated.", "expected_text": line["text"]})
        self.assertEqual(edited.status_code, 200, edited.text)
        refused = self.batch(client, headers, [job_id], [seen])
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, batch_zip.CHANGED))
        self.assertEqual(self.batch(client, headers, [job_id], []).json()["error"], batch_zip.NONE_READY)
        self.printed(client, headers, job_id)
        (current, name), = shown(self.views(client, headers, [job_id]))[1]
        self.assertEqual(self.batch(client, headers, [job_id], [seen]).status_code, 409)
        manifest, pdfs = unzip(self.batch(client, headers, [job_id], [current]))
        self.assertEqual([path.rsplit("/", 1)[1] for path in pdfs], [name])
        self.assertEqual(manifest["jobs"][0]["cvs"][0]["content_sha256"], current.split(":")[2])

    def test_another_users_job_refuses_the_whole_batch_like_one_that_does_not_exist(self):
        alice, alice_headers = self.ready_user("alice")
        theirs = self.new_job(alice, alice_headers)
        self.printed(alice, alice_headers, theirs)
        (key, _), = shown(self.views(alice, alice_headers, [theirs]))[1]
        bob, bob_headers = self.ready_user("bob")
        own = self.new_job(bob, bob_headers)
        self.printed(bob, bob_headers, own)
        (own_key, _), = shown(self.views(bob, bob_headers, [own]))[1]
        answers = [self.batch(bob, bob_headers, [theirs], [key]), self.batch(bob, bob_headers, [own, theirs], [own_key]),
                   self.batch(bob, bob_headers, [own, "20260101-000000-abcdef"], [own_key])]
        self.assertEqual({(answer.status_code, answer.text) for answer in answers},
                         {(404, json.dumps({"error": batch_zip.NOT_FOUND}, separators=(",", ":")))})
        self.assertEqual(self.batch(bob, bob_headers, [own], [own_key]).status_code, 200)
        for ids, keys in [([own, "../x"], [own_key]), ([own], [own_key + ":x"]), ([own], [own_key.replace(":en:", ":fr:")])]:
            self.assertEqual(self.batch(bob, bob_headers, ids, keys).status_code, 404)
        self.assertEqual(self.batch(bob, bob_headers, [f"j{n}" for n in range(21)], []).status_code, 400)
        anonymous = test_web_v2.TestClient(self.app, base_url=test_web_v2.ORIGIN)
        self.assertEqual(anonymous.get("/download/batch.zip", params={"jobs": own}).status_code, 401)

    def task_count(self):
        from feiran import tenant_store
        with tenant_store.transaction(self.database) as connection:
            return connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


class GuestBatchTests(test_guest_workspace.GuestCase):
    def test_a_guest_downloads_its_own_approved_sample_and_nothing_else(self):
        client, headers, job_id = self.guest()
        before = client.get("/api/me", headers=headers).json()["guest"]
        refused = client.get("/download/batch.zip", headers=headers, params={"jobs": job_id})
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, batch_zip.NONE_READY))
        self.assertEqual(self.printed(client, headers, job_id)["status"], "succeeded")
        cv = self.cv(client, headers, job_id)
        guest_after_print = client.get("/api/me", headers=headers).json()["guest"]
        response = client.get("/download/batch.zip", headers=headers,
                              params=[("jobs", job_id), ("ready", f"{job_id}:en:{cv['content_sha256']}")])
        self.assertEqual(response.status_code, 200, response.text)
        _manifest, pdfs = unzip(response)
        self.assertEqual([path.rsplit("/", 1)[1] for path in pdfs], [cv["download_name"]])
        self.assertEqual(client.get("/api/me", headers=headers).json()["guest"], guest_after_print)
        self.assertEqual(before["ai"], guest_after_print["ai"])  # printing and packing use no AI allowance
        user, user_headers = self.ready_user("carol")
        theirs = self.new_job(user, user_headers)
        self.assertEqual(client.get("/download/batch.zip", headers=headers, params={"jobs": f"{job_id},{theirs}"}).status_code, 404)


BATCH_VIEW = r'''
const views = {
  "job-ready": {job_id:"job-ready", language:"en", jd:{title:"Ready Role", company:"Acme"}, tasks:[], cv:{
    en:{head:"approved", content_sha256:"a".repeat(64), stale:false, final_pdf:true, approved_at:"2026-10-20T00:00:00Z",
        pagination:{pages:1, checked_at:"2026-10-20T00:00:00Z"}, download_name:"CV-Acme-Ready-Role-EN-jobready-vaaaaaaaa.pdf",
        stages:[{stage:"draft",status:"done",message:"Prepared"}]},
    zh:{head:"planned", content_sha256:"b".repeat(64), stale:false, final_pdf:false, pagination:{pages:2, checked_at:"2026-10-20T00:00:00Z"},
        stages:[{stage:"rewording",status:"fallback",message:"Not reworded again: DeepSeek is busy right now. Try again in a minute. The earlier rewording below is kept."}]}}},
  "job-busy": {job_id:"job-busy", language:"en", jd:{title:"Busy Role"}, tasks:[{task_id:"t1", operation:"tailor_cv", status:"running"}],
    cv:{en:{head:"draft", content_sha256:"c".repeat(64), stale:false, final_pdf:false}, zh:{head:null}}},
  "job-new": {job_id:"job-new", language:"en", jd:{title:"New Role"}, tasks:[], interrupted:{step:"cv-draft-en"},
    cv:{en:{head:null, stages:[{stage:"draft",status:"failed",message:"Not prepared: DeepSeek is busy right now. Try again in a minute."}]}, zh:{head:null}}},
  "job-stale": {job_id:"job-stale", language:"en", jd:{title:"Stale Role"}, tasks:[],
    cv:{en:{head:"approved", content_sha256:"d".repeat(64), stale:true, final_pdf:false}, zh:{head:null}}},
  "job-printing": {job_id:"job-printing", language:"en", jd:{title:"Approved Role"}, tasks:[],
    cv:{en:{head:"approved", content_sha256:"e".repeat(64), stale:false, final_pdf:false, approved_at:"2026-10-20T00:00:00Z",
        pagination:{pages:1, checked_at:"2026-10-20T00:00:00Z"}}, zh:{head:null}}},
};
sandbox.__views = views;
run(`api = async (path, options = {}) => { __calls.push({path, method: options.method || "GET", body: options.body});
  const id = path.split("/")[3];
  if (options.method === "POST") return {};
  if (!__views[decodeURIComponent(id)]) throw new Error("Job not found");
  return JSON.parse(JSON.stringify(__views[decodeURIComponent(id)])); };`);
'''


@unittest.skipUnless(NODE, "Node is needed for frontend behavior checks")
class BatchUITests(unittest.TestCase):
    check_js = SponsorUITests.check_js

    def test_the_jd_panel_says_only_what_the_rule_found_and_keeps_text_as_text(self):
        self.check_js(r'''
const jd = {text:'<script>alert(1)</script> Responsibilities', source:"javascript:alert(1)", captured_at:"2026-10-24T00:00:00Z"};
const panel = run('jdPanel')(jd, {source:null, captured_at:"2026-10-24T00:00:00Z", posted_at:null, reader:"page_text",
  characters:40, lines:1, duties_detected:true, qualifications_detected:false, notes:["unsafe_source","no_posted_at","no_qualifications"]}, "job-1");
assert.equal(panel.tagName, "details");
assert("open" in panel.attributes);
assert(!nodes(panel).some(n => n.tagName === "script" || n.tagName === "a"));
const text = panel.textContent;
assert(text.includes("<script>alert(1)</script> Responsibilities"));
assert(text.includes("javascript:alert(1)"));  // shown as text, never linked
assert(text.includes("not given by the source"));
assert(text.includes("detected") && text.includes("not identified"));
assert(text.includes("No qualifications were identified in this text."));
assert(!/\b(complete|verified|official)\b/i.test(text.replace("that the source is official", "")));
// Folding is remembered for that job while the page refreshes; another job opens unfolded.
panel.open = false; await panel.fire("toggle");
assert(!("open" in run('jdPanel')(jd, {}, "job-1").attributes));
assert("open" in run('jdPanel')(jd, {}, "job-2").attributes);
// An older job with no diagnostics at all still shows its text and unknowns.
const old = run('jdPanel')({text:"Old text"}, undefined, "job-3");
assert(old.textContent.includes("Old text") && old.textContent.includes("unknown"));
''', mode="local")

    def test_the_jobs_list_picks_at_most_twenty_into_a_batch_address(self):
        self.check_js(r'''
sandbox.__jobs = Array.from({length: 21}, (_, n) => ({job_id: "job-" + n, title: "Role " + n, steps: ["input"]}));
run('api = async () => ({jobs: __jobs})');
await run('renderJobs()');
const boxes = nodes(app).filter(n => n.tagName === "input" && n.attributes.type === "checkbox");
assert.equal(boxes.length, 21);
const open = find(app, n => n.tagName === "button" && n.textContent.startsWith("Open selected together"));
assert(open.disabled);
for (const box of boxes) { if (!box.disabled) { box.checked = true; await box.fire("change"); } }
assert.equal(open.textContent, "Open selected together (20/20)");
assert(boxes[20].disabled);
await open.fire("click");
assert.equal(sandbox.location.hash, "#batch/" + Array.from({length: 20}, (_, n) => "job-" + n).join(","));
''')

    def test_the_batch_shows_each_job_and_language_as_it_is_and_opening_it_changes_nothing(self):
        self.check_js(BATCH_VIEW + r'''
const later = sandbox.setTimeout;
sandbox.setTimeout = (action, delay) => delay === 2000 ? 0 : later(action, delay);  // polling is tested below
sandbox.location.hash = "#batch/job-ready,job-busy,job-new,job-stale,job-printing";
await run('renderBatch("job-ready,job-busy,job-new,job-stale,job-printing,job-ready,../x")');
assert(sandbox.__calls.every(call => call.method === "GET"));  // opening asks no model and writes nothing
assert.deepEqual(sandbox.__calls.map(call => call.path).slice(0, 5), ["/api/jobs/job-ready", "/api/jobs/job-busy",
  "/api/jobs/job-new", "/api/jobs/job-stale", "/api/jobs/job-printing"]);
const text = app.textContent;
assert(text.includes("Batch of 5 job(s)"));
assert(text.includes("1/6 downloadable"));
assert(text.includes("aaaaaaaa") && text.includes("bbbbbbbb"));
assert(text.includes("Not reworded again") && text.includes("The earlier rewording below is kept."));
assert(text.includes("Not prepared: DeepSeek is busy"));
assert(text.includes("The workbench stopped while preparing your CV"));
assert(text.includes("Working on it: rewording your CV."));
assert(text.includes("Out of date: your profile or facts changed"));
assert(text.includes("Approved; final PDF not created yet"));
assert(!text.includes("null") && !text.includes("[object"));
const card = (id) => find(app, n => n.attributes?.["data-job"] === id);
// A task is the job's: it is shown once above the CV rows, never on a language.
assert(!find(card("job-busy"), n => n.tagName === "tr" && n.textContent.includes("Working on it")));
const link = find(card("job-ready"), n => n.tagName === "a" && n.textContent === "CV-Acme-Ready-Role-EN-jobready-vaaaaaaaa.pdf");
assert.equal(link.attributes.href, "/download/job-ready/en.pdf");
assert(find(card("job-new"), n => n.tagName === "a" && n.attributes.href === "#job/job-new" && n.textContent === "Prepare it on the job page"));
assert(find(app, n => n.tagName === "li" && n.textContent === "New Role (EN): Not prepared yet."));
// Approval is never one click: this version's box first, and its extra page accepted.
const zh = find(card("job-ready"), n => n.attributes?.["data-language"] === "zh");
const approve = button(zh, "Approve this version");
assert(approve.disabled);
const [accept, read] = nodes(zh).filter(n => n.tagName === "input");
read.checked = true; await read.fire("change");
assert(approve.disabled);
accept.checked = true; await accept.fire("change");
assert(!approve.disabled);
sandbox.__calls.length = 0;
await approve.fire("click");
await new Promise(resolve => setTimeout(resolve, 0));
const post = sandbox.__calls[0];
assert.equal(post.path, "/api/jobs/job-ready/cv/zh/approve");
assert.deepEqual(JSON.parse(post.body), {expected_content_sha256: "b".repeat(64), allow_multiple_pages: true});
assert.deepEqual(sandbox.__calls.slice(1).map(call => call.path), ["/api/jobs/job-ready"]);  // only that job reloads
await button(card("job-printing"), "Create final PDF").fire("click");
await new Promise(resolve => setTimeout(resolve, 0));
assert(sandbox.__calls.some(call => call.path === "/api/jobs/job-printing/cv/en/export" && call.method === "POST"));
''')

    def test_a_cv_shown_partly_in_the_other_language_says_so_on_its_row_in_both_interfaces(self):
        # Found in review by Codex: the batch let such a CV be approved and downloaded with no word
        # of it. The job page's warning, with the fields, is on that language's row; nothing more
        # is refused for it.
        for interface, expected in (("en", "Shown in the other language because your profile has no English text for — "
                                           "education.0.title; Name: Alex Example"),
                                    ("zh", "以下内容缺少英文版本，因此显示另一语言原文：education.0.title; Name: Alex Example")):
            with self.subTest(interface=interface):
                self.check_js(BATCH_VIEW + r'''
const later = sandbox.setTimeout;
sandbox.setTimeout = (action, delay) => delay === 2000 ? 0 : later(action, delay);  // no polling here
if (INTERFACE === "zh") run('document.documentElement={}; document.getElementById=(id)=>id==="app"?__app:id==="interface-language"?{}:null; NodeFilter={SHOW_ELEMENT:1,SHOW_TEXT:4}; document.createTreeWalker=()=>({nextNode:()=>null}); I18n.setLanguage("zh")');
sandbox.__views["job-ready"].cv.en.language_fallbacks = ["education.0.title", "Name: Alex Example"];
sandbox.__views["job-ready"].cv.zh.language_fallbacks = [];
sandbox.__views["job-busy"].cv.en.language_fallbacks = ["education.0.title"];
sandbox.location.hash = "#batch/job-ready,job-busy";
await run('renderBatch("job-ready,job-busy")');
const row = (job, language) => find(app, n => n.attributes?.["data-job"] === job)
  .children.flatMap(nodes).find(n => n.tagName === "tr" && n.attributes?.["data-language"] === language);
const note = (job, language) => find(row(job, language), n => n.attributes?.class?.includes("language-fallbacks"));
assert.equal(note("job-ready", "en").textContent, EXPECTED);
assert(!note("job-ready", "zh"));  // only the language it is about
assert(note("job-busy", "en"));
// No new refusal: the ready CV still downloads, the other can still be approved.
assert(find(row("job-ready", "en"), n => n.tagName === "a" && n.attributes.href === "/download/job-ready/en.pdf"));
assert(find(row("job-busy", "en"), n => n.tagName === "button" && n.textContent === (INTERFACE === "zh" ? "批准这个版本" : "Approve this version")));
'''.replace("INTERFACE", json.dumps(interface)).replace("INTERFACE", json.dumps(interface))
                    .replace("INTERFACE", json.dumps(interface)).replace("EXPECTED", json.dumps(expected)))

    def test_a_batch_with_everything_ready_shows_no_stray_text(self):
        # Found live by Codex: a guest's one approved PDF showed "1/1 downloadable" and the word
        # "null" under the ZIP button, where the list of what is not ready goes.
        self.check_js(BATCH_VIEW + r'''
// The double keeps the DOM's own behaviour that showed this: a null given to append is the text "null".
const probe = run('document.createElement("div")');
probe.append(null);
assert.equal(probe.textContent, "null");
sandbox.__views["job-ready"].cv.zh = {head: null};
sandbox.location.hash = "#batch/job-ready";
await run('renderBatch("job-ready")');
const summary = find(app, n => n.attributes?.class === "batch-summary");
assert.equal(find(summary, n => n.attributes?.class === "batch-count").textContent, "1/1 downloadable");
assert(button(summary, "Download ready CVs (ZIP)"));
assert(!find(summary, n => n.tagName === "details"));  // nothing listed as not ready
assert(!nodes(summary).some(n => !(n.tagName) && n.textContent === "null"));
assert(!app.textContent.includes("null"));
''')

    def test_polling_reloads_only_the_busy_job_and_stops_when_the_page_is_left(self):
        self.check_js(BATCH_VIEW + r'''
const timers = [];
sandbox.setTimeout = (action, delay) => { timers.push({action, delay}); return timers.length; };
sandbox.location.hash = "#batch/job-ready,job-busy";
await run('renderBatch("job-ready,job-busy")');
assert.equal(timers.filter(timer => timer.delay === 2000).length, 1);
sandbox.__calls.length = 0;
sandbox.__views["job-busy"].tasks = [];
sandbox.__views["job-busy"].cv.en = {...sandbox.__views["job-ready"].cv.en, content_sha256: "f".repeat(64), download_name: "busy.pdf"};
await timers.find(timer => timer.delay === 2000).action();
await new Promise(resolve => setImmediate(resolve));
assert.deepEqual(sandbox.__calls.map(call => call.path), ["/api/jobs/job-busy"]);
assert(app.textContent.includes("2/3 downloadable"));
// Once the user has left the batch, a late poll does nothing.
const pending = timers.length;
sandbox.__views["job-busy"].tasks = [{task_id:"t2", operation:"plan_cv", status:"running"}];
run('pageGeneration += 1'); sandbox.location.hash = "#jobs";
sandbox.__calls.length = 0;
await timers[timers.length - 1].action();
assert.equal(sandbox.__calls.length, 0);
''')

    def test_the_zip_asks_for_the_versions_shown_and_a_refusal_is_shown(self):
        for mode in ("v2", "local"):
            with self.subTest(mode=mode):
                self.check_js(BATCH_VIEW + r'''
const fetched = [];
let answer = {ok: false, status: 409, json: async () => ({error: "These CVs changed after the page showed them; reload the batch and download again."})};
sandbox.fetch = async (url, options) => { fetched.push(url); return answer; };
run('fetch = window.fetch');
sandbox.location.hash = "#batch/job-ready,job-printing";
await run('renderBatch("job-ready,job-printing")');
assert(app.textContent.includes("1/3 downloadable"));
await button(app, "Download ready CVs (ZIP)").fire("click");
await new Promise(resolve => setTimeout(resolve, 0));
const url = new URL(fetched[0], "https://x.example");
assert.equal(url.pathname, "/download/batch.zip");
assert.equal(url.searchParams.get("jobs"), "job-ready,job-printing");
assert.deepEqual(url.searchParams.getAll("ready"), ["job-ready:en:" + "a".repeat(64)]);
assert.equal(url.searchParams.get("token"), MODE === "v2" ? null : "synthetic-token");
assert(sandbox.__app && run('message').textContent.includes("These CVs changed after the page showed them"));
// Nothing ready: no ZIP button at all; a missing job: no download together.
sandbox.location.hash = "#batch/job-busy";
run('pageGeneration += 1');
await run('renderBatch("job-busy")');
assert(!button(app, "Download ready CVs (ZIP)"));
assert(app.textContent.includes("Nothing is ready to download yet, so no ZIP is made."));
sandbox.location.hash = "#batch/job-ready,gone";
run('pageGeneration += 1');
await run('renderBatch("job-ready,gone")');
assert(!button(app, "Download ready CVs (ZIP)"));
assert(find(app, n => n.tagName === "a" && n.attributes.href === "#batch/job-ready"));
''', mode=mode)


def load_tests(loader, standard_tests, pattern):
    """Only the tests written here: the classes reuse other modules' fixtures, not their tests."""
    suite = unittest.TestSuite()
    for case in (JDDiagnosticsTests, BatchRuleTests, V1BatchTests, V2BatchTests, GuestBatchTests, BatchUITests):
        for name in loader.getTestCaseNames(case):
            if name in vars(case):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
