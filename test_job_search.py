import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from facts import add_fact, confirm_fact
from job_search import (
    SearchError,
    fetch_board,
    fetch_selected,
    load_profile,
    main,
    normalize_board,
    plain_text,
    prepare_review_input,
    rank_jobs,
    write_new_json,
)
from requirement_flow import propose_requirements
from review import build_report


CAPTURED = "2026-09-18T12:00:00+00:00"
PASTED_JD = """  Software Engineer Intern

Requirements:
  - Experience building services with Python
- Knowledge of relational databases
"""


class FakeResponse:
    def __init__(self, data):
        self.body = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, _limit):
        return self.body


def posting(job_id, title, content, location=None):
    return {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://job-boards.greenhouse.io/example/jobs/{job_id}",
        "content": content,
        "location": {"name": location} if location else None,
    }


class JobSearchTests(unittest.TestCase):
    def test_escaped_html_preserves_literal_angle_brackets(self):
        self.assertEqual(plain_text("&lt;p&gt;Use &amp;lt;T&amp;gt; in Python.&lt;/p&gt;"), "Use <T> in Python.")

    def test_ranking_shows_both_quotes_without_claiming_eligibility(self):
        data = {"jobs": [
            posting(1, "Software Engineer Intern", "<p>Python is useful.</p>", "Remote"),
            posting(2, "Design Intern", "<p>Figma is useful.</p>", "Remote"),
            posting(1, "Duplicate", "<p>Python</p>", "Remote"),
        ]}
        jobs = normalize_board(data, "example", CAPTURED)
        terms = [{"term": "Python", "fact_id": "f1", "fact_quote": "使用 Python 编写脚本。"}]
        ranked = rank_jobs(jobs, terms, title_filter="intern", location_filter="remote")
        self.assertEqual([job["job_id"] for job in ranked], ["1", "2"])
        self.assertEqual(ranked[0]["candidate_evidence_count"], 1)
        self.assertIn("Python is useful", ranked[0]["candidate_evidence"][0]["jd_quote"])
        self.assertEqual(ranked[0]["candidate_evidence"][0]["fact_quote"], "使用 Python 编写脚本。")
        self.assertEqual(ranked[0]["eligibility_status"], "未核验")
        self.assertEqual(ranked[1]["candidate_evidence_count"], 0)

    def test_ranking_matches_tags_inside_chinese_text_but_not_common_words(self):
        jobs = normalize_board({"jobs": [
            posting(1, "后端开发实习生", "<p>熟悉Python编程，了解机器学习算法</p>"),
            posting(2, "Growth Intern", "<p>Help us go to market faster</p>"),
        ]}, "example", CAPTURED)
        terms = [
            {"term": "Python", "fact_id": "f1"},
            {"term": "机器学习", "fact_id": "f2"},
            {"term": "Go", "fact_id": "f3"},
        ]
        ranked = rank_jobs(jobs, terms)
        evidence = {job["job_id"]: [item["term"] for item in job["candidate_evidence"]] for job in ranked}
        self.assertEqual(evidence, {"1": ["Python", "机器学习"], "2": []})

    def test_ranking_counts_each_matched_tag_once(self):
        jobs = normalize_board({"jobs": [
            posting(1, "Python Intern", "<p>Python services</p>"),
            posting(2, "Data Intern", "<p>SQL and Docker</p>"),
        ]}, "example", CAPTURED)
        terms = [{"term": "Python", "fact_id": f"f{index}"} for index in range(3)]
        terms += [{"term": "SQL", "fact_id": "f3"}, {"term": "Docker", "fact_id": "f4"}]
        ranked = rank_jobs(jobs, terms)
        self.assertEqual([job["job_id"] for job in ranked], ["2", "1"])
        self.assertEqual([job["matched_term_count"] for job in ranked], [2, 1])
        self.assertEqual(ranked[1]["candidate_evidence_count"], 3)

    def test_unknown_location_is_retained_and_labeled(self):
        jobs = normalize_board({"jobs": [posting(1, "Intern", "<p>Python</p>"), posting(2, "Intern", "<p>Python</p>", "New York")]}, "example", CAPTURED)
        ranked = rank_jobs(jobs, [], location_filter="Remote")
        self.assertEqual([job["job_id"] for job in ranked], ["1"])
        self.assertEqual(ranked[0]["location_filter_status"], "未知")

    def test_unconfirmed_profile_facts_are_not_used(self):
        profile = {"facts": [
            {"id": "f1", "text": "使用 Python 编写脚本。", "confirmed": True, "terms": ["Python"]},
            {"id": "f2", "text": "使用 Java。", "confirmed": False, "terms": ["Java"]},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            terms = load_profile(path)
        self.assertEqual([(item["fact_id"], item["term"]) for item in terms], [("f1", "Python")])

    def test_selected_jd_becomes_a_reviewable_pending_input(self):
        selected = {"jd": {"text": "Python experience", "source": "https://example.test/jobs/1", "captured_at": CAPTURED}}
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "review-input.json"
            review_input = prepare_review_input(selected)
            write_new_json(output_path, review_input)
            saved = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["jd"]["source"], "https://example.test/jobs/1")
        self.assertEqual(saved["facts"], [])
        self.assertEqual(saved["selected_requirements"], [])
        self.assertEqual(build_report(saved)["material_status"], "待选择岗位要求")

    def test_review_input_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review-input.json"
            path.write_text("keep me", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                write_new_json(path, {"replacement": True})
            self.assertEqual(path.read_text(encoding="utf-8"), "keep me")

    def test_review_input_creates_its_private_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".local" / "review-input.json"
            write_new_json(path, {"selected_requirements": []})
            self.assertTrue(path.is_file())

    @patch("job_search.fetch_selected")
    def test_select_cli_writes_review_input(self, fetch_selected_mock):
        fetch_selected_mock.return_value = {
            "jd": {"text": "Python experience", "source": "https://example.test/jobs/1", "captured_at": CAPTURED},
            "source_status": "本次公开接口返回",
        }
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "review-input.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "select", "--board", "example", "--job-id", "1",
                    "--output", str(output_path),
                ])
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(saved["facts"], [])
        self.assertIn("匹配阶段", summary["fact_confirmation_status"])
        self.assertEqual(summary["selected_requirement_count"], 0)

    @patch("job_search.fetch_board")
    def test_search_uses_confirmed_database_tags_without_copying_fact_store(self, fetch_board_mock):
        fetch_board_mock.return_value = normalize_board(
            {"jobs": [posting(1, "Backend Intern", "<p>Python experience</p>", "Remote")]},
            "example",
            CAPTURED,
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            confirmed, _ = add_fact(
                database, "使用 Python 编写脚本。", "project", ["Python"]
            )
            confirm_fact(database, confirmed["id"], 1)
            add_fact(database, "尚未确认的 Java 经历。", "skill", ["Java"])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "search", "--board", "example", "--facts-db", str(database),
                ])
            result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["jobs"][0]["candidate_evidence_count"], 1)
        self.assertEqual(result["jobs"][0]["candidate_evidence"][0]["fact_id"], confirmed["id"])
        self.assertEqual(result["jobs"][0]["candidate_evidence"][0]["fact_version"], 1)
        self.assertEqual(
            result["jobs"][0]["candidate_evidence"][0]["fact_quote"],
            "使用 Python 编写脚本。",
        )
        self.assertIn("事实库", result["fact_confirmation_status"])

    def test_profile_term_must_occur_in_fact_text(self):
        profile = {"facts": [{"id": "f1", "text": "参与课程项目。", "confirmed": True, "terms": ["Python"]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(SearchError, "必须出现在事实原文"):
                load_profile(path)

    @patch("job_search._fetch_json")
    def test_selected_job_is_refetched_and_snapshot_keeps_source(self, fetch_json):
        fetch_json.return_value = posting(1, "Software Engineer Intern", "<p>Python</p>", "Remote")
        result = fetch_selected("example", "1")
        self.assertEqual(fetch_json.call_args.args[0], "https://boards-api.greenhouse.io/v1/boards/example/jobs/1")
        self.assertEqual(result["jd"]["job_id"], "1")
        self.assertEqual(result["jd"]["title"], "Software Engineer Intern")
        self.assertEqual(result["jd"]["text"], "Python")
        self.assertIn("本次公开接口返回", result["source_status"])

    @patch("job_search._fetch_json")
    def test_selected_job_id_mismatch_fails(self, fetch_json):
        fetch_json.return_value = posting(2, "Other Intern", "<p>Python</p>")
        with self.assertRaisesRegex(SearchError, "不同的岗位 ID"):
            fetch_selected("example", "1")

    @patch("job_search._fetch_json", side_effect=SearchError("岗位接口返回 HTTP 503"))
    def test_source_failure_does_not_return_cached_jobs(self, fetch_json):
        with self.assertRaisesRegex(SearchError, "HTTP 503"):
            fetch_board("example")

    def test_pasted_jd_becomes_a_review_input_with_unknown_source(self):
        with tempfile.TemporaryDirectory() as directory:
            jd_path = Path(directory) / "jd.txt"
            output_path = Path(directory) / "review-input.json"
            jd_path.write_text(PASTED_JD, encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                exit_code = main([
                    "paste", "--file", str(jd_path), "--title", "Software Engineer Intern",
                    "--company", "Example Co", "--output", str(output_path),
                ])
            saved = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(saved["jd"]["provider"], "manual")
        self.assertEqual(saved["jd"]["company"], "Example Co")
        self.assertEqual(saved["jd"]["raw_content"], PASTED_JD)
        self.assertEqual(saved["facts"], [])
        self.assertEqual(build_report(saved)["jd_source"], "未知")
        self.assertEqual(
            [item["text"] for item in propose_requirements(saved)["requirement_candidates"]],
            ["Experience building services with Python", "Knowledge of relational databases"],
        )

    def test_pasted_chinese_jd_from_stdin_keeps_its_url(self):
        chinese_jd = "岗位职责：\n负责后端服务开发\n任职要求：\n1、本科及以上学历\n2、熟悉 Python\n"
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "review-input.json"
            with patch("sys.stdin", io.StringIO(chinese_jd)), redirect_stdout(io.StringIO()):
                exit_code = main([
                    "paste", "--file", "-", "--title", "后端开发实习生",
                    "--url", "https://jobs.example.cn/123", "--output", str(output_path),
                ])
            saved = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(build_report(saved)["jd_source"], "https://jobs.example.cn/123")
        self.assertEqual(
            [item["text"] for item in propose_requirements(saved)["requirement_candidates"]],
            ["本科及以上学历", "熟悉 Python"],
        )

    def test_paste_rejects_bad_url_or_empty_text_without_writing(self):
        cases = [
            (PASTED_JD, ["--url", "jobs.example.com/1"], "https://"),
            ("  \n\n  ", [], "为空"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            jd_path = Path(directory) / "jd.txt"
            output_path = Path(directory) / "review-input.json"
            for text, extra, message in cases:
                with self.subTest(message=message):
                    jd_path.write_text(text, encoding="utf-8")
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        exit_code = main([
                            "paste", "--file", str(jd_path), "--title", "Intern",
                            "--output", str(output_path), *extra,
                        ])
                    self.assertEqual(exit_code, 2)
                    self.assertFalse(output_path.exists())
                    self.assertIn(message, stderr.getvalue())

    @patch("job_search.time.sleep")
    @patch("job_search.urlopen")
    def test_read_timeout_is_retried_once(self, urlopen_mock, sleep_mock):
        board = {"jobs": [posting(1, "Intern", "<p>Python</p>")]}
        urlopen_mock.side_effect = [TimeoutError("The read operation timed out"), FakeResponse(board)]
        jobs = fetch_board("example")
        self.assertEqual([job["job_id"] for job in jobs], ["1"])
        self.assertEqual(urlopen_mock.call_count, 2)
        sleep_mock.assert_called_once()

    @patch("job_search.time.sleep")
    @patch("job_search.urlopen", side_effect=TimeoutError("The read operation timed out"))
    def test_repeated_timeout_reports_a_clear_error(self, urlopen_mock, _sleep_mock):
        with self.assertRaisesRegex(SearchError, "超时"):
            fetch_board("example")
        self.assertEqual(urlopen_mock.call_count, 2)

    @patch("job_search.time.sleep")
    @patch("job_search.urlopen")
    def test_missing_board_is_not_retried(self, urlopen_mock, sleep_mock):
        urlopen_mock.side_effect = HTTPError("https://example.test", 404, "Not Found", None, None)
        with self.assertRaisesRegex(SearchError, "HTTP 404.*招聘板标识"):
            fetch_board("example")
        self.assertEqual(urlopen_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_invalid_board_token_is_rejected(self):
        with self.assertRaisesRegex(SearchError, "招聘板标识"):
            fetch_board("../other")


if __name__ == "__main__":
    unittest.main()
