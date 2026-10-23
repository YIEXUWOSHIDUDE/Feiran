import tempfile
import unittest
from unittest.mock import Mock

from feiran.cv import CVError, build_draft, tailor_draft
from feiran.cv_edit import change_line, shown_text
from feiran.cv_plan import plan_draft
from feiran.cv_preparation import prepare_steps
from tests.test_cv import PROFILE, FakeChat, make_store
from tests.test_cv_plan import CUT_API, FakePlanner
from tests.test_gaps import decided_job


class PreparationTests(unittest.TestCase):
    def test_fallback_keeps_draft_and_local_edits_without_sending_edits_to_model(self):
        with tempfile.TemporaryDirectory() as directory:
            facts = make_store(directory)
            draft = build_draft(PROFILE, facts, "en")
            previous, _ = change_line(draft, "fact-intern-tests", "Local words to preserve.")
            planner = FakePlanner(CUT_API)
            tailor = Mock(side_effect=CVError("unavailable", reason="unreachable"))
            def plan(document, job, **kwargs):
                self.assertNotEqual(shown_text(document, "fact-intern-tests"), "Local words to preserve.")
                return plan_draft(document, job, chat=planner)
            stages = []
            result = list(prepare_steps(draft, facts, decided_job(), chat=None, private=[], previous=previous,
                                        tailor=tailor, plan=plan, on_stage=stages.append))
            self.assertEqual(stages, ["rewording", "layout"])
            self.assertIsNone(result[0].document)
            self.assertEqual(result[0].status["status"], "fallback")
            self.assertEqual(result[1].kind, "planned")
            self.assertEqual(shown_text(result[1].document, "fact-intern-tests"), "Local words to preserve.")
            self.assertEqual(tailor.call_args.kwargs["effort"], "none")

    def test_unexpected_error_does_not_become_success_or_continue_to_layout(self):
        plan = Mock()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            list(prepare_steps({}, None, {}, chat=None, private=[], previous=None,
                               tailor=Mock(side_effect=RuntimeError("cancelled")), plan=plan))
        plan.assert_not_called()
