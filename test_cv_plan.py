import copy
import json
import tempfile
import unittest

from cv import CVError, approve_draft, build_draft, is_final_approval, render_html, rewritten_lines, tailor_draft
from cv_plan import plan_draft, set_change
from deepseek_client import DeepSeekError
from facts import confirm_facts, import_facts
from requirement_flow import apply_requirement_decisions, propose_requirements
from test_cv import FACTS, PROFILE, FakeChat, make_store, stand_ins_for, swap_ids


def decided_job():
    data = {
        "jd": {"text": "Requirements:\n- Writing unit tests for services\n- Experience with Python",
               "title": "Backend Intern", "source": None, "captured_at": "2026-09-26T00:00:00+00:00"},
        "facts": [],
        "selected_requirements": [],
    }
    proposed = propose_requirements(data)
    return apply_requirement_decisions(proposed, {item["id"] for item in proposed["requirement_candidates"]}, set())


class FakePlanner:
    """Stands in for DeepSeek: returns one fixed structure proposal, or fails. The proposal
    names lines by fact ID; the answer uses the IDs the request gave them."""

    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.messages = None

    def __call__(self, messages, model, effort):
        self.messages = messages
        if self.error:
            raise self.error
        request = json.loads(messages[-1]["content"])
        lines = [line for section in request["resume"] for entry in section["entries"] for line in entry["lines"]]
        return {"model": "deepseek-flash", "content": swap_ids(self.content, stand_ins_for(lines)),
                "usage": {"prompt_tokens": 30, "completion_tokens": 20}}


CUT_API = {
    "sections": ["education", "skills", "experience"],
    "entries": [{"entry": "s1e0", "lines": ["fact-intern-tests"]}],
    "reasons": [
        {"target": "sections", "reason": "The job asks for hands-on testing first."},
        {"target": "fact-intern-api", "reason": "REST APIs are not asked for."},
    ],
}


class CVPlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = make_store(self.directory.name)
        self.draft = build_draft(PROFILE, self.database, "en")

    def tearDown(self):
        self.directory.cleanup()

    def test_the_planner_is_told_which_requirements_are_only_preferred(self):
        data = {"jd": {"text": "Requirements:\n- Experience with Python\nNice to have:\n- Writing unit tests for services",
                       "title": "Backend Intern", "source": None, "captured_at": "2026-09-26T00:00:00+00:00"},
                "facts": [], "selected_requirements": []}
        proposed = propose_requirements(data)
        job = apply_requirement_decisions(proposed, {item["id"] for item in proposed["requirement_candidates"]}, set())
        planner = FakePlanner(CUT_API)
        plan_draft(self.draft, job, chat=planner)
        self.assertEqual(json.loads(planner.messages[-1]["content"])["job_requirements"], [
            {"text": "Experience with Python", "strength": "required"},
            {"text": "Writing unit tests for services", "strength": "preferred"},
        ])

    def test_the_planner_never_sees_the_drafts_own_private_words_whatever_extra_words_are_given(self):
        billing = {"id": "fact-intern-billing", "type": "experience", "text": "Built the Example Corp billing tool.", "tags": []}
        import_facts(self.database, [billing])
        confirm_facts(self.database, [("fact-intern-billing", 1)])
        profile = copy.deepcopy(PROFILE)
        profile["sections"][1]["entries"][0]["facts"].append("fact-intern-billing")
        planner = FakePlanner(CUT_API)
        plan_draft(build_draft(profile, self.database, "en"), decided_job(), chat=planner, private=[])
        self.assertIn("billing tool", planner.messages[-1]["content"])
        self.assertNotIn("Example Corp", planner.messages[-1]["content"])

    def test_the_answer_can_name_only_lines_the_request_gave_it(self):
        # A fact ID never goes out, so an answer naming one did not come from the request.
        def planner(messages, model, effort):
            return {"model": "deepseek-flash", "usage": {}, "content": {
                "sections": ["education", "experience", "skills"],
                "entries": [{"entry": "s1e0", "lines": ["fact-intern-tests", "L99"]}]}}
        planned = plan_draft(self.draft, decided_job(), chat=planner)
        experience = next(section for section in planned["plan"]["layout"] if section["kind"] == "experience")
        self.assertEqual(experience["entries"], [{"entry": "s1e0", "lines": ["fact-intern-api"]}])  # its one kept line

    def test_a_cut_section_takes_the_reason_given_for_the_section_list(self):
        # Seen on a real posting: DeepSeek explained leaving out Publications under "sections",
        # and the order of the rest did not change, so the cut showed no reason.
        planned = plan_draft(self.draft, decided_job(), chat=FakePlanner({
            "sections": ["education", "experience"], "entries": [],
            "reasons": [{"target": "sections", "reason": "Skills repeat the bullets."}]}))
        self.assertEqual([(item["id"], item["reason"]) for item in planned["plan"]["changes"]],
                         [("cut:skills", "Skills repeat the bullets.")])

    def test_plan_orders_and_cuts_with_reasons_and_sends_no_names(self):
        planner = FakePlanner(CUT_API)
        planned = plan_draft(self.draft, decided_job(), chat=planner)
        sent = planner.messages[-1]["content"]
        html = render_html(planned)
        self.assertEqual({item["id"]: item["reason"] for item in planned["plan"]["changes"]}, {
            "order:sections": "The job asks for hands-on testing first.",
            "cut:fact-intern-api": "REST APIs are not asked for.",
        })
        for private in ("Alex Example", "alex@example.com", "Example Corp", "Example University", "Los Angeles",
                        *(fact["id"] for fact in FACTS)):
            self.assertNotIn(private, sent)
        self.assertIn("Wrote unit tests for billing code.", sent)
        self.assertLess(html.index("Python, Java"), html.index("Example Corp"))
        self.assertNotIn("Built REST APIs", html)
        self.assertEqual(planned["sections"], self.draft["sections"])  # every line is still stored

    def test_an_undone_change_is_shown_again_and_needs_a_new_approval(self):
        planned = plan_draft(self.draft, decided_job(), chat=FakePlanner(CUT_API))
        approved = approve_draft(planned, self.database)
        restored = set_change(planned, "cut:fact-intern-api", undone=True)
        tampered = {**approved, "plan": {**approved["plan"], "undone": ["cut:fact-intern-api"]}}
        self.assertIn("Built REST APIs", render_html(restored))
        self.assertEqual(set_change(restored, "cut:fact-intern-api", undone=False)["plan"]["undone"], [])
        self.assertTrue(is_final_approval(approved))
        with self.assertRaisesRegex(CVError, "批准后内容已改变"):
            is_final_approval(tampered)
        with self.assertRaisesRegex(CVError, "没有这项改动"):
            set_change(planned, "cut:fact-unknown", undone=True)
        with self.assertRaisesRegex(CVError, "已批准"):
            set_change(approved, "cut:fact-intern-api", undone=True)

    def test_rewrites_of_cut_lines_are_not_listed_and_a_rewrite_can_be_undone(self):
        rewrites = {"fact-intern-api": "For an internal tool, built REST APIs.",
                    "fact-intern-tests": "For billing code, wrote unit tests."}
        tailored = tailor_draft(self.draft, self.database, job=decided_job(), chat=FakeChat(rewrites))
        planned = plan_draft(tailored, decided_job(), chat=FakePlanner(CUT_API))
        undone = set_change(planned, "reword:fact-intern-tests", undone=True)
        self.assertEqual([(line["fact_id"], line["undone"]) for line in rewritten_lines(planned)],
                         [("fact-intern-tests", False)])
        self.assertEqual([(line["fact_id"], line["undone"]) for line in rewritten_lines(undone)],
                         [("fact-intern-tests", True)])
        self.assertIn("For billing code, wrote unit tests.", render_html(planned))
        self.assertIn("Wrote unit tests for billing code.", render_html(undone))

    def test_a_failed_or_malformed_answer_leaves_the_cv_unplanned(self):
        for planner in (FakePlanner(error=DeepSeekError("DeepSeek API 返回 HTTP 503")), FakePlanner({"lines": []})):
            with self.subTest(planner=planner.content), self.assertRaisesRegex(CVError, "DeepSeek"):
                plan_draft(self.draft, decided_job(), chat=planner)
        planned = plan_draft(self.draft, decided_job(), chat=FakePlanner(CUT_API))
        with self.assertRaisesRegex(CVError, "已经按岗位调整"):
            plan_draft(planned, decided_job(), chat=FakePlanner(CUT_API))


if __name__ == "__main__":
    unittest.main()
