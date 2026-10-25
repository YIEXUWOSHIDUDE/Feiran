"""A JD read automatically from a web page that holds only the page's summary never reaches the
model, in either app: refused when it is read (job_url.extract_job) and, for a job saved before
that check, before every step that would generate from it (job_url.require_complete_jd). Pasted
text and the native job-board readers are never refused for it. Synthetic pages; the model is a
scripted stand-in."""
import unittest

from feiran.job_search import SearchError
import json

from feiran.job_url import INCOMPLETE_JD, extract_job, has_job_details, require_complete_jd
from feiran.tenant_store import UserWorkspace
from tests import test_web, test_web_v2

SUMMARY = ("Join our engineering team and build reliable services. " * 10).strip()
FULL = ("Responsibilities\nBuild service APIs and test production systems with the team.\n"
        "Qualifications\nExperience with Python, SQL and distributed systems.")


def web_jd(text, provider="web"):
    return {"title": "Backend Engineer", "company": "Example", "text": text, "provider": provider,
            "source": "https://careers.example.org/job/42", "captured_at": "2026-10-24T00:00:00+00:00",
            "extraction_method": "jobposting-jsonld"}


class GateRuleTests(unittest.TestCase):
    def test_only_a_web_summary_is_refused(self):
        with self.assertRaisesRegex(SearchError, "简介"):
            require_complete_jd(web_jd(SUMMARY))
        require_complete_jd(web_jd(FULL))
        for provider in ("manual", "greenhouse", "lever", "ashby", "tencent", None):  # pasted and native readers
            require_complete_jd(web_jd(SUMMARY, provider))
        require_complete_jd({})  # nothing to judge
        self.assertFalse(has_job_details(SUMMARY))
        self.assertTrue(has_job_details(FULL))

    def test_obvious_truncation_is_not_details(self):
        self.assertFalse(has_job_details(FULL + "..."))
        self.assertFalse(has_job_details(FULL + " Read more"))


def page(description, main, title="Backend Engineer", head_title="Backend Engineer | Careers", extra=""):
    schema = json.dumps({"@type": "JobPosting", "title": title, "description": description})
    return (f"<html><head><title>{head_title}</title></head><body>{extra}<main>{main}</main>"
            f'<script type="application/ld+json">{schema}</script></body></html>')


BODY = ("<h2>Responsibilities</h2><p>" + "Build service APIs and test production systems. " * 20 + "</p>"
        "<h2>Qualifications</h2><p>FULLTEXT_ONLY: Python, SQL and distributed systems experience.</p>")


class ReaderScopeTests(unittest.TestCase):
    """The reported case: a page whose JobPosting data holds a 499-character summary while its
    main content, with no H1, holds the whole posting."""

    def test_a_summary_gives_way_to_the_postings_own_body_without_an_h1(self):
        jd = extract_job(page(SUMMARY[:499], BODY), "https://careers.example.org/job/42")["jd"]
        self.assertIn("FULLTEXT_ONLY", jd["text"])
        self.assertEqual(jd["extraction_method"], "jobposting-jsonld+html-text")

    def test_a_body_is_not_this_postings_when_the_page_names_another(self):
        for kwargs in ({"head_title": "Careers home"}, {"extra": "<h1>Data Scientist</h1>"}):
            with self.subTest(**kwargs), self.assertRaisesRegex(SearchError, "简介"):
                extract_job(page(SUMMARY, BODY, **kwargs), "https://careers.example.org/job/42")

    def test_a_complete_description_is_kept_unless_the_body_contains_it_all(self):
        description = FULL
        longer_other = BODY + "<p>" + "Unrelated site information. " * 40 + "</p>"
        jd = extract_job(page(description, longer_other), "https://careers.example.org/job/42")["jd"]
        self.assertEqual((jd["text"], jd["extraction_method"]), (FULL, "jobposting-jsonld"))  # longer, but not a copy
        fuller = "<p>" + FULL.replace("\n", "</p><p>") + "</p><h2>Benefits</h2><p>Remote friendly.</p>"
        jd = extract_job(page(description, fuller), "https://careers.example.org/job/42")["jd"]
        self.assertIn("Benefits", jd["text"])
        self.assertEqual(jd["extraction_method"], "jobposting-jsonld+html-text")


SEO_SUMMARY = ("Backend role requiring experience with Python. The team creates production products for customers. "
               "Join us to learn more about the team and enjoy an inclusive workplace. ")
SEO_MAIN = ("<h1>Backend Engineer</h1><h2>Responsibilities</h2><p>Design backend APIs and own database migrations.</p>"
            "<h2>Qualifications</h2><p>FULLTEXT_ONLY: Must have three years of SQL experience.</p>")


class ReviewFindingTests(unittest.TestCase):
    """Cases found in independent review (Codex, round 1), word for word."""

    def test_an_overview_with_a_qualification_phrase_gives_way_to_the_postings_sections(self):
        self.assertFalse(has_job_details(SEO_SUMMARY))  # a qualification phrase alone is how summaries read
        jd = extract_job(page(SEO_SUMMARY, SEO_MAIN, head_title="Backend Engineer"), "https://careers.example.org/job/7")["jd"]
        self.assertIn("FULLTEXT_ONLY", jd["text"])
        self.assertNotIn("inclusive workplace", jd["text"])
        self.assertEqual(jd["extraction_method"], "jobposting-jsonld+html-text")
        with self.assertRaisesRegex(SearchError, "简介"):  # the same summary saved by an older reader
            require_complete_jd(web_jd(SEO_SUMMARY))

    def test_part_of_another_postings_title_is_not_this_postings_title(self):
        other = ("<h2>Responsibilities</h2><p>Build data pipelines and test them.</p>"
                 "<h2>Qualifications</h2><p>OTHER_JOB: experience with Spark.</p>")
        with self.assertRaisesRegex(SearchError, "简介"):
            extract_job(page(SUMMARY, other, title="Engineer", head_title="Data Engineer | Example Careers"),
                        "https://careers.example.org/job/8")
        # A site's own suffix after the very same title still names the posting.
        jd = extract_job(page(SUMMARY, other, title="Data Engineer", head_title="Data Engineer | Example Careers"),
                         "https://careers.example.org/job/8")["jd"]
        self.assertIn("OTHER_JOB", jd["text"])

    def test_an_unheaded_description_needs_a_duty_besides_a_qualification(self):
        self.assertTrue(has_job_details("Build backend services. Must have experience with Python."))
        self.assertTrue(has_job_details("负责后端服务开发，熟悉 Python 和数据库。"))
        self.assertFalse(has_job_details("A great team. Must have experience with Python."))


class V1GateTests(test_web.WebTests):
    def read_url_posting(self, url):  # as an older reader saved it: the summary, provider web
        return {"jd": web_jd(SUMMARY), "source_status": "synthetic"}

    def test_a_web_summary_job_is_never_made_or_sent_to_the_model(self):
        response = self.client.post("/api/jobs/from-url", json={"url": "https://careers.example.org/job/42"},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("简介", response.json()["error"])
        self.assertEqual(self.chat.sent, [])
        self.assertEqual(self.client.get("/api/jobs", headers=self.headers).json()["jobs"], [])


class V2GateTests(test_web_v2.V2Case):
    def old_job(self, user_id, text, provider="web"):
        """A job saved before the reader refused summaries."""
        return UserWorkspace(self.database, user_id).create_job(web_jd(text, provider), request_id=f"old-{provider}")["job_id"]

    def test_an_old_web_summary_job_fails_before_any_model_call_and_pasted_text_is_unaffected(self):
        client, headers = self.ready_user()
        user_id = next(iter(self.users()))
        job_id = self.old_job(user_id, SUMMARY)
        sent = len(self.chat.sent)
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/requirements/find", headers=headers,
                                                         json={"expected_version": 0}))
        self.assertEqual((task["status"], task["error_code"]), ("failed", "incomplete_jd"), task)
        self.assertEqual(task["message"], INCOMPLETE_JD)
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=headers))
        self.assertEqual((task["status"], task["error_code"]), ("failed", "incomplete_jd"), task)
        self.assertEqual(len(self.chat.sent), sent)  # nothing went to the model
        pasted = self.old_job(user_id, SUMMARY, provider="manual")
        task = self.settle(client, headers, client.post(f"/api/jobs/{pasted}/requirements/find", headers=headers,
                                                         json={"expected_version": 0}))
        self.assertNotEqual(task["error_code"], "incomplete_jd", task)

    def users(self):
        from feiran import tenant_store
        return [account["user_id"] for account in tenant_store.list_accounts(self.database)]


def load_tests(loader, standard_tests, pattern):
    """Only the tests written here: the classes reuse other modules' fixtures, not their tests."""
    suite = unittest.TestSuite()
    for case in (GateRuleTests, ReaderScopeTests, ReviewFindingTests, V1GateTests, V2GateTests):
        for name in loader.getTestCaseNames(case):
            if name in vars(case):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
