"""Offline run of the pasted-JD workflow through every command-line entry point."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import facts
import job_search
import matching
import requirement_flow
import review


EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class EndToEndTests(unittest.TestCase):
    def run_step(self, module, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = module.main([str(argument) for argument in arguments])
        self.assertEqual(exit_code, 0, stderr.getvalue())
        return json.loads(stdout.getvalue())

    def test_pasted_chinese_jd_reaches_a_two_sided_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "workbench.db"
            self.run_step(job_search, [
                "paste", "--file", EXAMPLES / "synthetic_jd_zh.txt",
                "--title", "后端开发实习生", "--output", root / "input.json",
            ])
            proposed = self.run_step(requirement_flow, [
                "propose", root / "input.json", "--output", root / "candidates.json",
            ])
            added = self.run_step(requirement_flow, [
                "add", root / "candidates.json", "--text", "有开源项目经历",
                "--output", root / "added.json",
            ])
            requirement_ids = {
                item["text"]: item["id"] for item in proposed["candidates"] + added["added"]
            }
            self.assertEqual(list(requirement_ids), [
                "本科及以上学历，计算机相关专业",
                "熟悉Python，了解SQL",
                "每周至少实习4天",
                "有开源项目经历",
            ])
            decide_arguments = ["decide", root / "added.json", "--output", root / "decided.json"]
            for requirement_id in requirement_ids.values():
                decide_arguments += ["--confirm", requirement_id]
            self.run_step(requirement_flow, decide_arguments)

            imported = self.run_step(facts, [
                "import", EXAMPLES / "synthetic_facts_import.json", "--db", database,
            ])
            before_confirm = self.run_step(matching, [
                "propose", root / "decided.json", "--facts-db", database,
                "--output", root / "matches-before-confirm.json",
            ])
            refs = [f"{fact['id']}@{fact['version']}" for fact in imported["facts"]]
            self.run_step(facts, ["confirm", *refs, "--db", database])
            self.run_step(matching, [
                "propose", root / "decided.json", "--facts-db", database,
                "--output", root / "matches.json",
            ])

            fact_ids = {fact["text"]: fact["id"] for fact in imported["facts"]}
            links = {
                "本科及以上学历，计算机相关专业": "计算机科学本科在读，预计 2027 年毕业。",
                "熟悉Python，了解SQL": "使用 Python 和 SQL 完成课程后端项目。",
                "有开源项目经历": "向一个开源项目提交过文档修复并被合并。",
            }
            link_arguments = [
                "decide", root / "matches.json", "--facts-db", database,
                "--no-match", requirement_ids["每周至少实习4天"],
                "--output", root / "linked.json",
            ]
            for requirement_text, fact_text in links.items():
                link_arguments += [
                    "--link", f"{requirement_ids[requirement_text]}={fact_ids[fact_text]}"
                ]
            self.run_step(matching, link_arguments)
            report = self.run_step(review, [root / "linked.json"])

        quotes = {item["requirement_quote"]: item["fact_quote"] for item in report["items"]}
        self.assertEqual(before_confirm["candidate_count"], 0)
        self.assertEqual(report["jd_source"], "未知")
        self.assertEqual(report["material_status"], "待人工审核草稿")
        self.assertEqual(quotes, {**links, "每周至少实习4天": None})


if __name__ == "__main__":
    unittest.main()
