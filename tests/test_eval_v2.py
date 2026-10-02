"""The B6 evaluation: the offline claim-check gate, and the paid runner's limits with a fake model."""

import contextlib
import io
import json
import unittest

import eval_v2
from gaps import EVIDENCE_RULES


class OfflineGateTests(unittest.TestCase):
    def test_the_claim_checks_meet_every_label_they_are_built_for(self) -> None:
        report = eval_v2.offline(eval_v2.load())
        self.assertEqual(report["regressions"], [])
        self.assertEqual(report["cases"], len(eval_v2.load()["rewrites"]))

    def test_samples_are_labelled_consistently(self) -> None:
        samples = eval_v2.load()
        ids = [case["id"] for part in ("rewrites", "evidence") for case in samples[part]]
        self.assertEqual(len(ids), len(set(ids)))
        for case in samples["rewrites"]:
            self.assertIn(case["label"], ("overclaim", "supported"))
            self.assertIn(case["rules"], ("must_catch", "known_gap"))
            if case["rules"] == "known_gap":
                self.assertEqual(case["label"], "overclaim", case["id"])
        for case in samples["evidence"]:
            self.assertIn(case["expected"], ("supported", "related", "none"))

    def test_known_gaps_are_reported_as_missed_not_passed(self) -> None:
        report = eval_v2.offline(eval_v2.load())
        gaps = {case["id"] for case in eval_v2.load()["rewrites"] if case["rules"] == "known_gap"}
        self.assertEqual(set(report["known_gaps_missed"]) | set(report["known_gaps_caught"]), gaps)
        self.assertFalse(gaps & set(report["regressions"]))


class FakeModel:
    """Answers the evidence check with one verdict and records every request it sees."""

    def __init__(self, verdict: str) -> None:
        self.verdict, self.prompts = verdict, []

    def __call__(self, messages, model, effort):
        self.prompts.append(messages[0]["content"])
        request = json.loads(messages[1]["content"])
        line = request["resume"][0]["lines"][0]["id"]
        pick = {"id": request["requirements"][0]["id"], "verdict": self.verdict}
        pick.update({"supported": {"sets": [[line]]}, "related": {"lines": [line], "missing": "years"}}.get(self.verdict, {}))
        return {"content": {"requirements": [pick]}, "model": "fake-model", "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


class LiveRunnerTests(unittest.TestCase):
    def test_only_the_evidence_check_reaches_the_model(self) -> None:
        model = FakeModel("none")
        report = eval_v2.live(eval_v2.load(), 100, model)
        self.assertEqual(set(model.prompts), {EVIDENCE_RULES})
        self.assertEqual(report["calls"], len(eval_v2.load()["evidence"]))
        self.assertEqual(report["usage"], {"prompt_tokens": 10 * report["calls"], "completion_tokens": 2 * report["calls"]})
        self.assertEqual(report["answered_by"], ["fake-model"])

    def test_the_call_limit_is_never_exceeded(self) -> None:
        model = FakeModel("supported")
        report = eval_v2.live(eval_v2.load(), 3, model)
        self.assertEqual(len(model.prompts), 3)
        self.assertEqual(report["judged"], 3)
        self.assertEqual(sum(row["model"] == "not_run" for row in report["rows"]), len(report["rows"]) - 3)

    def test_overcredit_is_counted_against_the_labels(self) -> None:
        report = eval_v2.live(eval_v2.load(), 100, FakeModel("supported"))
        expected = [case["id"] for case in eval_v2.load()["evidence"] if case["expected"] != "supported"]
        self.assertEqual(report["model_overcredits"], expected)

    def test_a_paid_run_is_refused_without_confirmation_or_a_limit(self) -> None:
        for argv in (["live", "--max-calls", "5", "--out", "x.json"],
                     ["live", "--max-calls", "0", "--confirm-paid", "--out", "x.json"],
                     ["live", "--max-calls", "1000", "--confirm-paid", "--out", "x.json"]):
            with contextlib.redirect_stderr(io.StringIO()) as said:
                self.assertEqual(eval_v2.main(argv), 2)
            self.assertIn("refused", said.getvalue())

    def test_the_baseline_takes_a_shared_word_for_support(self) -> None:
        cases = {case["id"]: case for case in eval_v2.load()["evidence"]}
        self.assertEqual(eval_v2.baseline(cases["ev-compound-1"]), "supported")
        self.assertEqual(eval_v2.baseline(cases["ev-none-1"]), "none")
        self.assertEqual(eval_v2.baseline(cases["ev-zh-2"]), "supported")


if __name__ == "__main__":
    unittest.main()
