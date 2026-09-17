import json
import unittest
from pathlib import Path

from review import build_report


SAMPLE = Path(__file__).parent / "examples" / "synthetic_input.json"


class BuildReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads(SAMPLE.read_text(encoding="utf-8"))

    def test_candidate_evidence_and_unknown_stay_separate(self) -> None:
        report = build_report(self.data)
        self.assertEqual(report["jd_source"], "未知")
        self.assertEqual(report["official_post_status"], "未知")
        self.assertEqual(report["material_status"], "待人工审核草稿")
        linked, unknown = report["items"]
        self.assertEqual(linked["requirement_quote"], "熟悉 Python")
        self.assertEqual(linked["fact_quote"], self.data["facts"][0]["text"])
        self.assertIn("参与课程项目", linked["draft_suggestion"])
        self.assertNotIn("主导", linked["draft_suggestion"])
        self.assertEqual(unknown["evidence_status"], "未知")
        self.assertIsNone(unknown["draft_suggestion"])

    def test_unconfirmed_fact_cannot_be_used(self) -> None:
        self.data["facts"][0]["confirmed"] = False
        with self.assertRaisesRegex(ValueError, "事实尚未确认"):
            build_report(self.data)

    def test_versioned_fact_requires_exact_selection_version(self) -> None:
        self.data["facts"][0]["version"] = 2
        self.data["selected_requirements"][0]["fact_version"] = 1
        with self.assertRaisesRegex(ValueError, "事实版本不匹配"):
            build_report(self.data)
        self.data["selected_requirements"][0]["fact_version"] = 2
        report = build_report(self.data)
        self.assertEqual(report["items"][0]["fact_version"], 2)

    def test_requirement_must_be_in_jd_snapshot(self) -> None:
        self.data["selected_requirements"][0]["text"] = "五年 Python 经验"
        with self.assertRaisesRegex(ValueError, "要求不在 JD 原文中"):
            build_report(self.data)

    def test_capture_time_requires_timezone(self) -> None:
        self.data["jd"]["captured_at"] = "2026-09-18T12:00:00"
        with self.assertRaisesRegex(ValueError, "必须包含时区"):
            build_report(self.data)

    def test_empty_requirement_selection_is_a_valid_pending_state(self) -> None:
        self.data["selected_requirements"] = []
        report = build_report(self.data)
        self.assertEqual(report["material_status"], "待选择岗位要求")
        self.assertEqual(report["items"], [])


if __name__ == "__main__":
    unittest.main()
