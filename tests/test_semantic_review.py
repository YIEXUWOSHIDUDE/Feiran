"""Writing observations use synthetic expanded material and never assess claim support."""
import tempfile
import unittest
from pathlib import Path
from feiran.semantic_review import _pairs, _request, DIMENSIONS, observe_rewrites
from feiran.cv import build_draft, tailor_draft
from tests.test_cv import PROFILE, make_store, FakeChat


class WritingObservationTests(unittest.TestCase):
    def test_expansion_is_observed_for_writing_quality_without_claim_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory, confirm=False)
            draft = tailor_draft(build_draft(PROFILE, database), database, chat=FakeChat({
                "fact-intern-api": "Led 4 engineers for 3 years to build AWS services and reduce latency by 40%."
            }))
            pairs = _pairs(draft, PROFILE, database, ())
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["rule_reasons"], [])
        self.assertEqual(pairs[0]["status"], "pending")
        payload = _request(pairs, "test")
        self.assertEqual(set(DIMENSIONS), {"unclear_wording", "generic_wording", "unnecessary_length"})
        self.assertEqual(len(payload["questions"]), 3)
        for question in payload["questions"].values():
            self.assertIn("do not judge factual support", question["instructions"])

    def test_observation_disabled_makes_no_external_request(self):
        def forbidden(*args):
            self.fail("disabled observations must not call a provider")
        report = observe_rewrites(None, None, Path("missing"), post_json=forbidden)
        self.assertEqual(report["status"], "disabled")
