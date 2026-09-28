import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from deepseek_client import DeepSeekError
from requirement_flow import (
    RequirementError,
    add_manual_requirements,
    apply_requirement_decisions,
    attach_semantic_judgments,
    extract_requirement_candidates,
    find_requirements_with_model,
    main,
    propose_requirements,
)
from review import build_report
from typesafe_classifier import TypeSafeError


JD_TEXT = """About the role
Build useful software.

Requirements:
- Experience building services with Python
- Knowledge of relational databases

Benefits
Health insurance
"""


def review_input():
    return {
        "jd": {
            "text": JD_TEXT,
            "source": "https://example.test/jobs/1",
            "captured_at": "2026-09-18T12:00:00+00:00",
        },
        "facts": [
            {"id": "fact-1", "text": "使用 Python 编写后端课程项目。", "confirmed": True},
        ],
        "selected_requirements": [],
    }


class FakeFinder:
    """Stands in for DeepSeek: answers with fixed line numbers, or fails."""

    def __init__(self, picks=None, error=None):
        self.picks = picks
        self.error = error
        self.messages = None

    def __call__(self, messages, model, effort):
        self.messages = messages
        if self.error:
            raise self.error
        return {"model": "deepseek-flash", "content": {"requirements": self.picks},
                "usage": {"prompt_tokens": 50, "completion_tokens": 9}}


BOARD_JD = """About the role
Build ML services.
Must-Have Skills
Mastery of Python
Strong SQL skills
Nice-to-Have Skills
Experience with Kubernetes
Benefits
Health insurance
"""


class RequirementFlowTests(unittest.TestCase):
    def test_extracts_exact_lines_only_from_requirement_section(self):
        candidates = extract_requirement_candidates(JD_TEXT)
        self.assertEqual([item["text"] for item in candidates], [
            "Experience building services with Python",
            "Knowledge of relational databases",
        ])
        self.assertTrue(all(item["text"] in JD_TEXT for item in candidates))
        self.assertTrue(all(item["status"] == "pending" for item in candidates))

    def test_candidate_ids_are_stable_for_the_same_text(self):
        first = extract_requirement_candidates(JD_TEXT)
        second = extract_requirement_candidates(JD_TEXT)
        self.assertEqual([item["id"] for item in first], [item["id"] for item in second])

    def test_no_known_section_returns_no_candidates(self):
        self.assertEqual(extract_requirement_candidates("Join our team\nBuild useful things"), [])

    def test_decorated_headings_and_stop_sections_are_recognized(self):
        text = """🧠 What you'll do...
Build features
✅ What we're looking for...
Experience with Python
⭐ What you can expect...
Mentorship
"""
        candidates = extract_requirement_candidates(text)
        self.assertEqual([item["text"] for item in candidates], ["Experience with Python"])

    def test_chinese_requirement_sections_are_extracted_without_list_numbers(self):
        text = """岗位职责：
1、负责后端服务开发与维护
任职要求：
1、本科及以上学历
2.熟悉Python或Go语言
（3）有数据库使用经验
加分项
有开源项目经验者优先
福利待遇
五险一金，免费三餐
"""
        candidates = extract_requirement_candidates(text)
        self.assertEqual([item["text"] for item in candidates], [
            "本科及以上学历",
            "熟悉Python或Go语言",
            "有数据库使用经验",
            "有开源项目经验者优先",
        ])
        self.assertTrue(all(item["text"] in text for item in candidates))

    def test_common_heading_variants_and_decorations_are_recognized(self):
        text = """**Minimum Requirements**
Experience with Python
2. What you'll do
Build internal tools
You have:
Strong written communication
【岗位要求】
熟悉常用数据结构
二、任职要求：
能够实习六个月以上
Perks & Benefits
Free lunch every day
Nice to have
Familiarity with Kubernetes
"""
        candidates = extract_requirement_candidates(text)
        self.assertEqual([item["text"] for item in candidates], [
            "Experience with Python",
            "Strong written communication",
            "熟悉常用数据结构",
            "能够实习六个月以上",
            "Familiarity with Kubernetes",
        ])

    def test_headings_used_on_public_job_boards_start_and_end_sections(self):
        text = """A World-Changing Company
What We Require
Strong engineering background in Computer Science
About 3 years of experience with Python and SQL in production systems
Life at Palantir
We offer a comprehensive benefits package.
Your background looks something like:
Some experience with relational databases
About OpenAI
OpenAI is an AI research and deployment company.
We’re excited about you because…
You have built data pipelines in Python
In this role, you will:
Ship features every week to customers
You might thrive in this role if you:
Care deeply about product quality
Pay Range Transparency
$120,000—$180,000 USD
What we look for
At least two years of backend experience
Why Harvey
Harvey is growing quickly across the world.
Strong candidates may also have:
Experience with LLM evaluation
Annual base salary range (excluding equity and bonus)
$150,000 - $200,000 per year
"""
        self.assertEqual([item["text"] for item in extract_requirement_candidates(text)], [
            "Strong engineering background in Computer Science",
            "About 3 years of experience with Python and SQL in production systems",
            "Some experience with relational databases",
            "You have built data pipelines in Python",
            "Care deeply about product quality",
            "At least two years of backend experience",
            "Experience with LLM evaluation",
        ])

    def test_heading_variants_match_whole_lines_only(self):
        text = """Your Expertise
Strong experience with Python
The Difference You Will Make
Own services end to end at scale
You may be a good fit if you have:
Experience training large language models
You'll thrive in this role if you:
Enjoy fast iteration with users
Position Expectations:
Collaborate with project teams daily
On day one we will expect you to have:
Experience with distributed systems
Bonus points for the following:
Familiarity with Kubernetes operators
MongoDB's base salary range for this role in the U.S. is:
$130,000—$180,000 USD
Things We Love
Formal training in computer science
Representative Projects
Building a platform for data labeling
You may be a fit if
You might thrive in this role if you enjoy long meetings with many stakeholders
Applying
If there appears to be a fit, we will reach out to schedule interviews
Strong candidates may also have:
Experience with async Python
The annual compensation range for this role is listed below.
For sales roles, the range provided is the On Target Earnings range
A Typical Day
Planning meetings with the team
"""
        self.assertEqual([item["text"] for item in extract_requirement_candidates(text)], [
            "Strong experience with Python",
            "Experience training large language models",
            "Enjoy fast iteration with users",
            "Experience with distributed systems",
            "Familiarity with Kubernetes operators",
            "Formal training in computer science",
            "You might thrive in this role if you enjoy long meetings with many stakeholders",
            "Experience with async Python",
        ])

    def test_deepseek_chooses_lines_by_number_and_their_text_is_copied_exactly(self):
        finder = FakeFinder([
            {"line": 7, "kind": "preferred"}, {"line": 4, "kind": "required"}, {"line": 5}, {"line": 2},
            {"line": 1, "kind": "required"}, {"line": 99, "kind": "required"}, {"line": "4"},
        ])
        candidates, details = find_requirements_with_model(BOARD_JD, finder, title="ML Engineer")
        sent = json.loads(finder.messages[-1]["content"])
        # Without a strength from DeepSeek, the heading decides; with no sign at all it stays
        # unclear instead of silently becoming required.
        self.assertEqual(
            [(item["text"], item["strength"], item["section"]) for item in candidates],
            [("Build ML services.", "unclear", "About the role"),
             ("Mastery of Python", "required", "Must-Have Skills"), ("Strong SQL skills", "required", "Must-Have Skills"),
             ("Experience with Kubernetes", "preferred", "Nice-to-Have Skills")],
        )
        self.assertEqual({item["extraction_method"] for item in candidates}, {"deepseek-lines-v1"})
        self.assertEqual(set(sent), {"job_title", "lines"})
        self.assertEqual(sent["lines"][3], {"n": 4, "text": "Mastery of Python"})
        self.assertEqual(details["model"], "deepseek-flash")

    def test_the_lines_own_words_outrank_what_deepseek_says(self):
        # Found in review by Codex: DeepSeek's "preferred" overrode an explicit "must".
        jd = "Requirements:\nYou must know Python\nNice to have:\nKubernetes experience is a plus"
        finder = FakeFinder([{"line": 2, "kind": "preferred"}, {"line": 4, "kind": "required"}])
        candidates, _ = find_requirements_with_model(jd, finder)
        self.assertEqual([item["strength"] for item in candidates], ["required", "preferred"])

    def test_heading_rules_keep_whether_a_line_is_required_preferred_or_unclear(self):
        jd = (BOARD_JD + "Qualifications\nFamiliarity with Go\nRequirements\nExperience with Airflow is a plus\n"
              "A degree is not required\n任职要求\n熟悉优先队列和图算法\n有开源经验者优先\n")
        strengths = {item["text"]: item["strength"] for item in extract_requirement_candidates(jd)}
        self.assertEqual(strengths, {
            "Mastery of Python": "required", "Strong SQL skills": "required",
            "Experience with Kubernetes": "preferred", "Familiarity with Go": "unclear",
            "Experience with Airflow is a plus": "preferred",
            # Found in review by Codex: "not required" is not required, and 优先队列 is a
            # priority queue, not "preferred".
            "A degree is not required": "unclear", "熟悉优先队列和图算法": "required", "有开源经验者优先": "preferred",
        })

    def test_heading_rules_take_over_when_deepseek_fails_or_finds_nothing(self):
        for finder, reason in (
            (FakeFinder(error=DeepSeekError("无法连接 DeepSeek API")), "无法连接 DeepSeek API"),
            (FakeFinder(picks=[]), "DeepSeek 没有找到要求行"),
        ):
            with self.subTest(reason=reason):
                proposed = propose_requirements(review_input(), chat=finder)
                self.assertEqual(
                    [item["text"] for item in proposed["requirement_candidates"]],
                    ["Experience building services with Python", "Knowledge of relational databases"],
                )
                self.assertEqual(proposed["requirement_extraction"]["method"], "section-lines-v3")
                self.assertEqual(proposed["requirement_extraction"]["fallback_reason"], reason)

    def test_heading_with_inline_content_starts_a_section(self):
        text = """About the role
Build useful software.
Requirements: 3+ years of Python experience
Knowledge of relational databases
Benefits: Health insurance and a laptop
任职要求：熟悉 Linux 常用命令
福利待遇：五险一金
"""
        candidates = extract_requirement_candidates(text)
        self.assertEqual([item["text"] for item in candidates], [
            "3+ years of Python experience",
            "Knowledge of relational databases",
            "熟悉 Linux 常用命令",
        ])
        self.assertEqual(candidates[0]["section"], "Requirements")

    def test_manual_quote_becomes_pending_candidate_that_can_be_confirmed(self):
        data = review_input()
        data["jd"]["text"] = "Who should apply\n- Comfortable with SQL and Linux\n" + JD_TEXT
        proposed = propose_requirements(data)
        added = add_manual_requirements(proposed, ["- Comfortable with SQL and Linux"])
        candidate = added["requirement_candidates"][-1]
        decided = apply_requirement_decisions(added, {candidate["id"]}, set())
        self.assertEqual(len(added["requirement_candidates"]), 3)
        self.assertEqual(candidate["text"], "Comfortable with SQL and Linux")
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(candidate["extraction_method"], "manual-quote-v1")
        self.assertEqual(
            [item["text"] for item in decided["selected_requirements"]],
            ["Comfortable with SQL and Linux"],
        )

    def test_cli_add_rejects_text_that_is_not_in_the_jd(self):
        proposed = propose_requirements(review_input())
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidates.json"
            output_path = Path(directory) / "added.json"
            input_path.write_text(json.dumps(proposed), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main([
                    "add", str(input_path), "--text", "Experience with Rust",
                    "--output", str(output_path),
                ])
            self.assertEqual(exit_code, 2)
            self.assertFalse(output_path.exists())
            self.assertIn("不是 JD 原文片段", stderr.getvalue())

    def test_manual_quote_already_proposed_is_rejected_with_its_id(self):
        proposed = propose_requirements(review_input())
        existing = proposed["requirement_candidates"][0]
        with self.assertRaisesRegex(RequirementError, existing["id"]):
            add_manual_requirements(proposed, [existing["text"]])

    def test_manual_quote_cannot_be_added_after_fact_matching_started(self):
        proposed = propose_requirements(review_input())
        proposed["match_candidates"] = []
        with self.assertRaisesRegex(RequirementError, "事实匹配"):
            add_manual_requirements(proposed, ["Health insurance"])

    def test_manual_quote_needs_proposed_candidates_first(self):
        with self.assertRaisesRegex(RequirementError, "propose"):
            add_manual_requirements(review_input(), ["Health insurance"])

    def test_confirmed_and_excluded_candidates_have_distinct_states(self):
        proposed = propose_requirements(review_input())
        first_id, second_id = [item["id"] for item in proposed["requirement_candidates"]]
        decided = apply_requirement_decisions(proposed, {first_id}, {second_id})
        self.assertEqual(decided["selected_requirements"], [{
            "id": first_id,
            "text": "Experience building services with Python",
            "fact_id": None,
            "strength": "required",
            "decided_by": "user",
        }])
        self.assertEqual([item["status"] for item in decided["requirement_candidates"]], ["confirmed", "excluded"])
        self.assertEqual(build_report(decided)["items"][0]["evidence_status"], "未知")

    def test_requirements_counted_automatically_are_not_recorded_as_reviewed(self):
        proposed = propose_requirements(review_input())
        first_id, second_id = [item["id"] for item in proposed["requirement_candidates"]]
        automatic = apply_requirement_decisions(proposed, {first_id, second_id}, set(), decided_by="auto")
        reviewed = apply_requirement_decisions(automatic, set(), {second_id})
        self.assertEqual([item["decided_by"] for item in automatic["selected_requirements"]], ["auto", "auto"])
        self.assertEqual([(item["status"], item["decided_by"]) for item in reviewed["requirement_candidates"]],
                         [("confirmed", "auto"), ("excluded", "user")])
        with self.assertRaises(RequirementError):
            apply_requirement_decisions(proposed, {first_id}, set(), decided_by="someone")

    def test_pending_candidates_do_not_enter_review(self):
        proposed = propose_requirements(review_input())
        first_id = proposed["requirement_candidates"][0]["id"]
        decided = apply_requirement_decisions(proposed, set(), {first_id})
        self.assertEqual(decided["selected_requirements"], [])
        self.assertEqual(build_report(decided)["material_status"], "待选择岗位要求")

    def test_semantic_judgments_do_not_make_human_decisions(self):
        proposed = propose_requirements(review_input())
        candidate_ids = [item["id"] for item in proposed["requirement_candidates"]]
        classification = {
            "provider": "typesafe",
            "requested_model": "jev-latest",
            "resolved_model": "jev-1.13.0",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "judgments": {
                candidate_id: {
                    "is_requirement_probability": 0.9,
                    "category": {"choice": "skill_or_experience", "confidence": 0.8, "probabilities": {}},
                    "strength": {"choice": "required", "confidence": 0.7, "probabilities": {}},
                }
                for candidate_id in candidate_ids
            },
        }
        result = attach_semantic_judgments(proposed, classification)
        self.assertTrue(all(item["status"] == "pending" for item in result["requirement_candidates"]))
        self.assertEqual(result["selected_requirements"], [])
        self.assertEqual(
            result["requirement_extraction"]["semantic_classifier"]["resolved_model"],
            "jev-1.13.0",
        )

    def test_same_candidate_cannot_be_confirmed_and_excluded(self):
        proposed = propose_requirements(review_input())
        candidate_id = proposed["requirement_candidates"][0]["id"]
        with self.assertRaisesRegex(RequirementError, "不能同时确认和排除"):
            apply_requirement_decisions(proposed, {candidate_id}, {candidate_id})

    def test_cli_rejects_legacy_fact_link_before_writing_output(self):
        proposed = propose_requirements(review_input())
        candidate_id = proposed["requirement_candidates"][0]["id"]
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidates.json"
            output_path = Path(directory) / "decided.json"
            input_path.write_text(json.dumps(proposed), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main([
                    "decide", str(input_path), "--confirm", f"{candidate_id}=fact-1", "--output", str(output_path)
                ])
            self.assertEqual(exit_code, 2)
            self.assertFalse(output_path.exists())
            self.assertIn("事实关联由 matching.py 处理", stderr.getvalue())

    def test_cli_propose_writes_new_file_and_lists_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "review-input.json"
            output_path = Path(directory) / "candidates.json"
            input_path.write_text(json.dumps(review_input()), encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["propose", str(input_path), "--output", str(output_path)])
            summary = json.loads(stdout.getvalue())
            saved = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["candidate_count"], 2)
        self.assertEqual(len(saved["requirement_candidates"]), 2)

    def test_cli_propose_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "review-input.json"
            output_path = Path(directory) / "candidates.json"
            input_path.write_text(json.dumps(review_input()), encoding="utf-8")
            output_path.write_text("keep me", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                exit_code = main(["propose", str(input_path), "--output", str(output_path)])
            self.assertEqual(exit_code, 2)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "keep me")

    def test_cli_typesafe_adds_judgments_but_keeps_candidates_pending(self):
        def classify(_jd_text, candidates, model):
            return {
                "provider": "typesafe",
                "requested_model": model,
                "resolved_model": "jev-1.13.0",
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "judgments": {
                    item["id"]: {
                        "is_requirement_probability": 0.95,
                        "category": {"choice": "skill_or_experience", "confidence": 0.9, "probabilities": {}},
                        "strength": {"choice": "required", "confidence": 0.8, "probabilities": {}},
                    }
                    for item in candidates
                },
            }

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "review-input.json"
            output_path = Path(directory) / "candidates.json"
            input_path.write_text(json.dumps(review_input()), encoding="utf-8")
            with patch("requirement_flow.classify_requirement_candidates", side_effect=classify):
                with redirect_stdout(io.StringIO()):
                    exit_code = main([
                        "propose", str(input_path), "--typesafe", "--output", str(output_path)
                    ])
            saved = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertTrue(all(item["status"] == "pending" for item in saved["requirement_candidates"]))
        self.assertTrue(all("semantic_judgment" in item for item in saved["requirement_candidates"]))

    def test_cli_typesafe_failure_does_not_write_output(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "review-input.json"
            output_path = Path(directory) / "candidates.json"
            input_path.write_text(json.dumps(review_input()), encoding="utf-8")
            with patch(
                "requirement_flow.classify_requirement_candidates",
                side_effect=TypeSafeError("service unavailable"),
            ):
                with redirect_stderr(io.StringIO()):
                    exit_code = main([
                        "propose", str(input_path), "--typesafe", "--output", str(output_path)
                    ])
            self.assertEqual(exit_code, 2)
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
