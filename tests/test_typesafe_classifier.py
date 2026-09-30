import json
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, Mock, patch

from typesafe_classifier import (
    CATEGORIES,
    STRENGTHS,
    TypeSafeError,
    _post_json,
    build_request,
    classify_requirement_candidates,
)


JD_TEXT = """Requirements:
- Experience building services with Python
"""
CANDIDATES = [{
    "id": "req-python",
    "text": "Experience building services with Python",
    "section": "Requirements",
    "status": "pending",
    "fact_id": None,
}]


def valid_response():
    return {
        "model": "jev-1.13.0",
        "answers": {
            "candidate_0_is_requirement": {"type": "noul", "noul": 0.96},
            "candidate_0_category": {
                "type": "choice",
                "choice": "skill_or_experience",
                "confidence": 0.91,
                "probabilities": {
                    option: 0.94 if option == "skill_or_experience" else 0.01
                    for option in CATEGORIES
                },
            },
            "candidate_0_strength": {
                "type": "choice",
                "choice": "required",
                "confidence": 0.82,
                "probabilities": {
                    "required": 0.87,
                    "preferred": 0.05,
                    "unclear_or_not_requirement": 0.08,
                },
            },
        },
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }


class TypeSafeClassifierTests(unittest.TestCase):
    def test_request_contains_only_jd_and_candidate_context(self):
        payload, answer_ids = build_request(JD_TEXT, CANDIDATES)
        serialized = json.dumps(payload)
        self.assertEqual(set(payload["state"]), {"job_description", "candidate_requirements"})
        self.assertNotIn("facts", serialized)
        self.assertNotIn("candidate profile", serialized.casefold())
        self.assertIsInstance(payload["questions"], dict)
        self.assertEqual(len(payload["questions"]), 3)
        self.assertNotIn("options", payload["questions"]["candidate_0_category"])
        self.assertEqual(
            set(payload["questions"]["candidate_0_category"]["criteria"]),
            set(CATEGORIES),
        )
        self.assertEqual(answer_ids["req-python"]["is_requirement"], "candidate_0_is_requirement")

    def test_preserves_typed_probabilities_and_resolved_model(self):
        post_json = Mock(return_value=valid_response())
        result = classify_requirement_candidates(
            JD_TEXT, CANDIDATES, api_key="test-key", post_json=post_json
        )
        judgment = result["judgments"]["req-python"]
        self.assertEqual(result["resolved_model"], "jev-1.13.0")
        self.assertEqual(judgment["is_requirement_probability"], 0.96)
        self.assertEqual(judgment["category"]["choice"], "skill_or_experience")
        self.assertEqual(judgment["strength"]["probabilities"]["required"], 0.87)
        sent_payload, sent_key = post_json.call_args.args
        self.assertEqual(sent_key, "test-key")
        self.assertEqual(sent_payload["model"], "jev-latest")

    def test_missing_key_fails_without_calling_service(self):
        post_json = Mock()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(TypeSafeError, "TYPESAFE_API_KEY"):
                classify_requirement_candidates(JD_TEXT, CANDIDATES, post_json=post_json)
        post_json.assert_not_called()

    def test_no_candidates_needs_no_key_or_service_call(self):
        post_json = Mock()
        with patch.dict(os.environ, {}, clear=True):
            result = classify_requirement_candidates(JD_TEXT, [], post_json=post_json)
        self.assertEqual(result["judgments"], {})
        self.assertIsNone(result["resolved_model"])
        post_json.assert_not_called()

    def test_rejects_invalid_probability(self):
        response = valid_response()
        response["answers"]["candidate_0_is_requirement"]["noul"] = 1.4
        with self.assertRaisesRegex(TypeSafeError, "0 到 1"):
            classify_requirement_candidates(
                JD_TEXT,
                CANDIDATES,
                api_key="test-key",
                post_json=Mock(return_value=response),
            )

    def test_rejects_incomplete_choice_distribution(self):
        response = valid_response()
        del response["answers"]["candidate_0_strength"]["probabilities"][STRENGTHS[-1]]
        with self.assertRaisesRegex(TypeSafeError, "概率分布无效"):
            classify_requirement_candidates(
                JD_TEXT,
                CANDIDATES,
                api_key="test-key",
                post_json=Mock(return_value=response),
            )

    def test_retries_overload_with_bounded_backoff(self):
        overload = urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone", 529, "overloaded", None, None
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(valid_response()).encode("utf-8")
        with patch("typesafe_classifier.urllib.request.urlopen", side_effect=[overload, response]) as urlopen:
            with patch("typesafe_classifier.time.sleep") as sleep:
                parsed = _post_json({"state": "test", "model": "jev-latest", "questions": {}}, "test-key")
        self.assertEqual(parsed["model"], "jev-1.13.0")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
