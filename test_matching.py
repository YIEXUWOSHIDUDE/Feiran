import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from deepseek_client import DeepSeekError
from facts import FactStoreError, add_fact, confirm_fact, initialize_database, list_facts, revise_fact
from matching import MatchingError, apply_match_decisions, first_candidates, main, propose_matches
from requirement_flow import apply_requirement_decisions, propose_requirements
from review import build_report


JD_TEXT = """Requirements:
- Experience building services with Python
- Currently enrolled in a computer science degree
"""


def decided_requirements(include_education=False):
    data = {
        "jd": {
            "text": JD_TEXT,
            "source": "https://example.test/jobs/1",
            "captured_at": "2026-09-21T12:00:00+00:00",
        },
        "facts": [],
        "selected_requirements": [],
    }
    proposed = propose_requirements(data)
    proposed["requirement_candidates"][0]["semantic_judgment"] = {
        "category": {"choice": "skill_or_experience"}
    }
    proposed["requirement_candidates"][1]["semantic_judgment"] = {
        "category": {"choice": "education_or_eligibility"}
    }
    ids = [item["id"] for item in proposed["requirement_candidates"]]
    confirmed = set(ids if include_education else ids[:1])
    return apply_requirement_decisions(proposed, confirmed, set()), ids


class FakeMatcher:
    """Stands in for DeepSeek: returns fixed fact choices per requirement, or fails. Choices
    name facts by ID; the answer uses the IDs the request gave those facts' texts."""

    def __init__(self, matches=None, error=None, database=None):
        self.matches = matches
        self.error = error
        self.database = database
        self.messages = None

    def __call__(self, messages, model, effort):
        self.messages = messages
        if self.error:
            raise self.error
        sent = {fact["text"]: fact["id"] for fact in json.loads(messages[-1]["content"])["facts"]}
        ids = {fact["id"]: sent[fact["text"]] for fact in list_facts(self.database) if fact["text"] in sent}
        matches = [{**item, "facts": [ids.get(fact_id, fact_id) for fact_id in item["facts"]]} for item in self.matches]
        return {"model": "deepseek-flash", "content": {"matches": matches},
                "usage": {"prompt_tokens": 40, "completion_tokens": 8}}


class MatchingTests(unittest.TestCase):
    def facts_for_model(self, database):
        python, _ = add_fact(database, "Built services in Python.", "project", ["Python"])
        degree, _ = add_fact(database, "Enrolled in a computer science degree.", "education", ["computer science"])
        pending, _ = add_fact(database, "Ran Kubernetes clusters.", "skill", ["Kubernetes"])
        confirm_fact(database, python["id"], 1)
        confirm_fact(database, degree["id"], 1)
        return python, degree, pending

    def test_deepseek_links_only_confirmed_facts_it_names(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            python, degree, pending = self.facts_for_model(database)
            data, ids = decided_requirements(include_education=True)
            matcher = FakeMatcher([
                {"requirement": ids[0], "facts": [python["id"], pending["id"], "fact-unknown", python["id"]]},
                {"requirement": ids[1], "facts": []},
                {"requirement": "req-unknown", "facts": [degree["id"]]},
            ], database=database)
            proposed = propose_matches(data, database, chat=matcher)
            linked = apply_match_decisions(proposed, database, *first_candidates(proposed), decided_by="auto")
        sent = json.loads(matcher.messages[-1]["content"])
        self.assertEqual(
            [(item["requirement_id"], item["fact_id"]) for item in proposed["match_candidates"]],
            [(ids[0], python["id"])],
        )
        self.assertEqual(proposed["fact_matching"]["method"], "deepseek-facts-v1")
        # Pending facts are never offered, so they are never sent either; nor is any fact ID.
        self.assertEqual(sorted(fact["text"] for fact in sent["facts"]), sorted([python["text"], degree["text"]]))
        for fact in (python, degree, pending):
            self.assertNotIn(fact["id"], matcher.messages[-1]["content"])
        self.assertEqual(
            {key: (value["status"], value["decided_by"]) for key, value in linked["match_decisions"].items()},
            {ids[0]: ("linked", "auto"), ids[1]: ("no_match", "auto")},
        )

    def test_word_matching_takes_over_when_deepseek_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            python, _, _ = self.facts_for_model(database)
            data, ids = decided_requirements()
            matcher = FakeMatcher(error=DeepSeekError("DeepSeek API 返回 HTTP 503"))
            proposed = propose_matches(data, database, chat=matcher)
        self.assertEqual(proposed["fact_matching"]["fallback_reason"], "DeepSeek API 返回 HTTP 503")
        self.assertEqual(proposed["fact_matching"]["method"], "versioned-tags-and-type-v1")
        self.assertEqual([item["fact_id"] for item in proposed["match_candidates"]], [python["id"]])

    def test_cli_propose_writes_candidates_without_copying_all_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "workbench.db"
            fact, _ = add_fact(database, "使用 Python。", "skill", ["Python"])
            confirm_fact(database, fact["id"], 1)
            data, _ = decided_requirements()
            input_path = root / "decided.json"
            output_path = root / "matches.json"
            input_path.write_text(json.dumps(data), encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "propose", str(input_path), "--facts-db", str(database),
                    "--limit", "5", "--output", str(output_path),
                ])
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(saved["facts"], [])
        self.assertEqual(saved["match_candidates"][0]["fact_id"], fact["id"])
        self.assertEqual(summary["candidate_count"], 1)

    def test_propose_retrieves_bounded_relevant_facts_without_copying_all(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            python, _ = add_fact(
                database, "使用 Python 开发后端课程项目。", "project", ["Python", "backend"]
            )
            degree, _ = add_fact(
                database, "正在攻读计算机硕士。", "education", ["Computer Science"]
            )
            pending, _ = add_fact(database, "使用 Java。", "skill", ["Java"])
            confirm_fact(database, python["id"], 1)
            confirm_fact(database, degree["id"], 1)
            data, ids = decided_requirements()
            result = propose_matches(data, database, limit=5)
        self.assertEqual(result["facts"], [])
        self.assertEqual({item["fact_id"] for item in result["match_candidates"]}, {python["id"]})
        self.assertNotIn(degree["id"], {item["fact_id"] for item in result["match_candidates"]})
        self.assertNotIn(pending["id"], {item["fact_id"] for item in result["match_candidates"]})
        self.assertEqual(result["match_candidates"][0]["requirement_id"], ids[0])
        self.assertEqual(result["match_candidates"][0]["status"], "pending")

    def test_link_copies_only_selected_exact_fact_version(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            fact, _ = add_fact(database, "使用 Python 开发后端。", "project", ["Python"])
            confirm_fact(database, fact["id"], 1)
            data, ids = decided_requirements()
            proposed = propose_matches(data, database)
            decided = apply_match_decisions(proposed, database, {ids[0]: fact["id"]}, set())
        self.assertEqual(decided["selected_requirements"][0]["fact_id"], fact["id"])
        self.assertEqual(decided["selected_requirements"][0]["fact_version"], 1)
        self.assertEqual([item["id"] for item in decided["facts"]], [fact["id"]])
        self.assertEqual(build_report(decided)["items"][0]["fact_quote"], "使用 Python 开发后端。")

    def test_revised_fact_invalidates_an_old_match_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            fact, _ = add_fact(database, "使用 Python。", "skill", ["Python"])
            confirm_fact(database, fact["id"], 1)
            data, ids = decided_requirements()
            proposed = propose_matches(data, database)
            revise_fact(database, fact["id"], text="熟练使用 Python。")
            with self.assertRaisesRegex(FactStoreError, "已不是当前已确认版本"):
                apply_match_decisions(proposed, database, {ids[0]: fact["id"]}, set())

    def test_no_match_is_an_explicit_unknown_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            initialize_database(database)
            data, ids = decided_requirements()
            proposed = propose_matches(data, database)
            decided = apply_match_decisions(proposed, database, {}, {ids[0]})
        self.assertEqual(decided["match_decisions"][ids[0]]["status"], "no_match")
        self.assertEqual(decided["selected_requirements"][0]["fact_id"], None)
        self.assertEqual(build_report(decided)["items"][0]["evidence_status"], "未知")

    def test_incremental_decisions_preserve_earlier_selected_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            python, _ = add_fact(database, "使用 Python。", "skill", ["Python"])
            degree, _ = add_fact(
                database, "正在攻读计算机硕士。", "education", ["Computer Science"]
            )
            confirm_fact(database, python["id"], 1)
            confirm_fact(database, degree["id"], 1)
            data, ids = decided_requirements(include_education=True)
            proposed = propose_matches(data, database)
            first = apply_match_decisions(proposed, database, {ids[0]: python["id"]}, set())
            second = apply_match_decisions(first, database, {ids[1]: degree["id"]}, set())
        self.assertEqual(second["fact_matching"]["decision_counts"]["linked"], 2)
        self.assertEqual({item["id"] for item in second["facts"]}, {python["id"], degree["id"]})

    def test_link_must_be_one_of_the_presented_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            fact, _ = add_fact(database, "无关事实。", "education", ["Biology"])
            confirm_fact(database, fact["id"], 1)
            data, ids = decided_requirements()
            proposed = propose_matches(data, database)
            with self.assertRaisesRegex(MatchingError, "不是该要求的候选"):
                apply_match_decisions(proposed, database, {ids[0]: fact["id"]}, set())


if __name__ == "__main__":
    unittest.main()
