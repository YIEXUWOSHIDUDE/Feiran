import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from facts import (
    FactStoreError,
    add_fact,
    confirm_fact,
    find_confirmed_facts,
    import_facts,
    initialize_database,
    list_facts,
    load_confirmed_fact,
    load_confirmed_fact_texts,
    load_search_terms,
    main,
    revise_fact,
    tag_finder,
    tag_pattern,
)


def run_cli(arguments):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(arguments)
    return exit_code, json.loads(stdout.getvalue()) if exit_code == 0 else None


class FactStoreTests(unittest.TestCase):
    def test_fact_type_and_tags_are_part_of_pending_version(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".local" / "workbench.db"
            fact, created = add_fact(
                database,
                "使用 Python 和 FastAPI 完成后端课程项目。",
                "project",
                ["Python", "FastAPI", "python"],
            )
        self.assertTrue(created)
        self.assertEqual(fact["status"], "pending")
        self.assertEqual(fact["fact_type"], "project")
        self.assertEqual(fact["tags"], ["FastAPI", "Python"])

    def test_confirmed_current_version_can_be_loaded_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            fact, _ = add_fact(database, "熟悉 Python。", "skill", ["Python"])
            confirm_fact(database, fact["id"], 1)
            loaded = load_confirmed_fact(database, fact["id"], 1)
        self.assertEqual(loaded["fact_type"], "skill")
        self.assertEqual(loaded["tags"], ["Python"])

    def test_new_pending_version_blocks_old_confirmed_version(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            original, _ = add_fact(database, "使用 Python。", "skill", ["Python"])
            confirm_fact(database, original["id"], 1)
            revised, created = revise_fact(
                database,
                original["id"],
                text="使用 Python 和 FastAPI。",
                fact_type="project",
                tags=["Python", "FastAPI"],
            )
            history = list_facts(database, original["id"])
            with self.assertRaisesRegex(FactStoreError, "已不是当前已确认版本"):
                load_confirmed_fact(database, original["id"], 1)
        self.assertTrue(created)
        self.assertEqual(revised["version"], 2)
        self.assertEqual([item["status"] for item in history], ["confirmed", "pending"])
        self.assertEqual([item["fact_type"] for item in history], ["skill", "project"])

    def test_revision_carries_forward_unspecified_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            fact, _ = add_fact(database, "使用 Python。", "skill", ["Python"])
            revised, _ = revise_fact(database, fact["id"], text="熟练使用 Python。")
        self.assertEqual(revised["fact_type"], "skill")
        self.assertEqual(revised["tags"], ["Python"])

    def test_repeated_complete_payload_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            first, first_created = add_fact(database, "使用 Python。", "skill", ["Python"])
            duplicate, duplicate_created = add_fact(database, "使用 Python。", "skill", ["python"])
            revision, revision_created = revise_fact(database, first["id"], fact_type="project")
            repeated, repeated_created = revise_fact(database, first["id"], fact_type="project")
            history = list_facts(database, first["id"])
        self.assertTrue(first_created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate["id"], first["id"])
        self.assertTrue(revision_created)
        self.assertFalse(repeated_created)
        self.assertEqual(repeated["version"], revision["version"])
        self.assertEqual(len(history), 2)

    def test_retrieval_uses_only_current_confirmed_tags_and_types(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            python, _ = add_fact(database, "使用 Python 开发后端。", "project", ["Python", "backend"])
            degree, _ = add_fact(database, "正在攻读计算机硕士。", "education", ["Computer Science"])
            pending, _ = add_fact(database, "使用 Java。", "skill", ["Java"])
            confirm_fact(database, python["id"], 1)
            confirm_fact(database, degree["id"], 1)
            by_tag = find_confirmed_facts(database, "Experience with Python", limit=5)
            by_type = find_confirmed_facts(
                database, "Currently enrolled in a degree", preferred_types=["education"], limit=5
            )
            terms = load_search_terms(database)
            displayed_texts = load_confirmed_fact_texts(
                database, [(python["id"], 1)]
            )
        self.assertEqual([item["id"] for item in by_tag], [python["id"]])
        self.assertEqual(by_tag[0]["retrieval_basis"]["matched_tags"], ["Python"])
        self.assertEqual([item["id"] for item in by_type], [degree["id"]])
        self.assertNotIn(pending["id"], {item["fact_id"] for item in terms})
        self.assertNotIn("fact_quote", terms[0])
        self.assertEqual(
            displayed_texts[(python["id"], 1)], "使用 Python 开发后端。"
        )

    def test_display_text_loader_rejects_a_stale_version(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            fact, _ = add_fact(database, "使用 Python。", "skill", ["Python"])
            confirm_fact(database, fact["id"], 1)
            revise_fact(database, fact["id"], text="熟练使用 Python。")
            texts = load_confirmed_fact_texts(database, [(fact["id"], 1)])
        self.assertEqual(texts, {})

    def test_short_ascii_tag_uses_word_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            fact, _ = add_fact(database, "使用 C 编程。", "skill", ["C"])
            confirm_fact(database, fact["id"], 1)
            false_positive = find_confirmed_facts(database, "Experience designing services")
            actual_match = find_confirmed_facts(database, "Experience with C and Linux")
        self.assertEqual(false_positive, [])
        self.assertEqual([item["id"] for item in actual_match], [fact["id"]])

    def test_short_tags_do_not_match_common_words(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            for text, tag in [("使用 Go 开发服务。", "Go"), ("使用 R 做统计。", "R"), ("使用 C 编程。", "C")]:
                fact, _ = add_fact(database, text, "skill", [tag])
                confirm_fact(database, fact["id"], 1)
            common_words = find_confirmed_facts(database, "Help us go to market, grow R&D and brief the C-suite")
            languages = find_confirmed_facts(database, "Experience with Go, R or C")
        self.assertEqual(common_words, [])
        self.assertEqual(len(languages), 3)

    def test_ascii_and_chinese_tags_match_inside_chinese_text(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            python, _ = add_fact(database, "使用 Python 开发后端。", "project", ["Python"])
            learning, _ = add_fact(database, "完成机器学习课程项目。", "project", ["机器学习"])
            confirm_fact(database, python["id"], 1)
            confirm_fact(database, learning["id"], 1)
            found = find_confirmed_facts(database, "熟悉Python编程，了解机器学习算法")
        self.assertEqual({item["id"] for item in found}, {python["id"], learning["id"]})

    def test_migrates_v1_data_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE facts (
                    fact_id TEXT PRIMARY KEY,
                    current_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE fact_versions (
                    fact_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    PRIMARY KEY (fact_id, version)
                );
                INSERT INTO facts VALUES ('fact-legacy', 1, '2026-01-01T00:00:00+00:00');
                INSERT INTO fact_versions VALUES (
                    'fact-legacy', 1, '旧事实', 'confirmed',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00'
                );
                PRAGMA user_version = 1;
                """
            )
            connection.close()
            initialize_database(database)
            facts = list_facts(database)
            connection = sqlite3.connect(database)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.close()
        self.assertEqual(version, 2)
        self.assertEqual(facts[0]["text"], "旧事实")
        self.assertEqual(facts[0]["fact_type"], "other")
        self.assertEqual(facts[0]["tags"], [])

    def test_cli_requires_and_records_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "add", "--db", str(database), "--text", "使用 SQLite。",
                    "--type", "skill", "--tag", "SQLite",
                ])
            saved = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(saved["fact"]["fact_type"], "skill")
        self.assertEqual(saved["fact"]["tags"], ["SQLite"])

    def test_import_adds_pending_facts_and_reimport_creates_no_duplicates(self):
        items = [
            {"type": "skill", "text": f"合成事实 {index}：使用工具 {index}。", "tags": [f"Tool{index}"]}
            for index in range(20)
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            import_path = Path(directory) / "facts.json"
            import_path.write_text(json.dumps({"facts": items}, ensure_ascii=False), encoding="utf-8")
            first_code, first = run_cli(["import", str(import_path), "--db", str(database)])
            second_code, second = run_cli(["import", str(import_path), "--db", str(database)])
            stored = list_facts(database)
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual((first["created_count"], first["existing_count"]), (20, 0))
        self.assertEqual((second["created_count"], second["existing_count"]), (0, 20))
        self.assertEqual(len(stored), 20)
        self.assertTrue(all(fact["status"] == "pending" for fact in stored))

    def test_imported_facts_are_retrieved_only_after_confirming_them_together(self):
        items = [
            {"type": "project", "text": "使用 Python 开发后端课程项目。", "tags": ["Python"]},
            {"type": "skill", "text": "熟悉 SQL 查询。", "tags": ["SQL"]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            import_path = Path(directory) / "facts.json"
            import_path.write_text(json.dumps({"facts": items}, ensure_ascii=False), encoding="utf-8")
            _, imported = run_cli(["import", str(import_path), "--db", str(database)])
            before = find_confirmed_facts(database, "Python and SQL experience")
            refs = [f"{fact['id']}@{fact['version']}" for fact in imported["facts"]]
            exit_code, confirmed = run_cli(["confirm", *refs, "--db", str(database)])
            after = find_confirmed_facts(database, "Python and SQL experience")
        self.assertEqual(before, [])
        self.assertIn(" ".join(refs), imported["next_step"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(confirmed["changed_count"], 2)
        self.assertEqual(len(after), 2)

    def test_confirming_several_facts_is_all_or_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            first, _ = add_fact(database, "使用 Python。", "skill", ["Python"])
            second, _ = add_fact(database, "使用 SQL。", "skill", ["SQL"])
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main([
                    "confirm", f"{first['id']}@1", f"{second['id']}@2", "--db", str(database)
                ])
            statuses = [fact["status"] for fact in list_facts(database)]
        self.assertEqual(exit_code, 2)
        self.assertIn(second["id"], stderr.getvalue())
        self.assertEqual(statuses, ["pending", "pending"])

    def test_one_invalid_item_rejects_the_whole_import(self):
        items = [
            {"type": "skill", "text": "使用 Python。", "tags": ["Python"]},
            {"type": "skill", "text": "使用 SQL。", "tag": ["SQL"]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            import_path = Path(directory) / "facts.json"
            import_path.write_text(json.dumps({"facts": items}, ensure_ascii=False), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = main(["import", str(import_path), "--db", str(database)])
        self.assertEqual(exit_code, 2)
        self.assertIn("facts[1]", stderr.getvalue())
        self.assertIn("tag", stderr.getvalue())
        self.assertFalse(database.exists())

    def test_import_with_stable_ids_creates_then_versions_changed_items(self):
        original = [{
            "id": "fact-uni-coursework", "type": "education",
            "text": "Coursework: Analysis of Algorithms", "tags": ["Algorithms"],
        }]
        edited = [{**original[0], "text": "Coursework: Analysis of Algorithms, Database Systems"}]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            created = import_facts(database, original)
            confirm_fact(database, "fact-uni-coursework", 1)
            unchanged = import_facts(database, original)
            changed = import_facts(database, edited)
            history = list_facts(database, "fact-uni-coursework")
        self.assertEqual((created[0][0]["id"], created[0][1]), ("fact-uni-coursework", True))
        self.assertFalse(unchanged[0][1])
        self.assertEqual(
            (changed[0][0]["version"], changed[0][0]["status"], changed[0][1]), (2, "pending", True)
        )
        self.assertEqual([item["status"] for item in history], ["confirmed", "pending"])

    def test_import_rejects_new_id_that_duplicates_existing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            existing, _ = add_fact(database, "使用 Python。", "skill", ["Python"])
            with self.assertRaisesRegex(FactStoreError, existing["id"]):
                import_facts(database, [
                    {"id": "fact-sql", "type": "skill", "text": "使用 SQL。", "tags": ["SQL"]},
                    {"id": "fact-python", "type": "skill", "text": "使用 Python。", "tags": ["Python"]},
                ])
            stored = [fact["id"] for fact in list_facts(database)]
        self.assertEqual(stored, [existing["id"]])

    def test_single_fact_can_still_be_confirmed_with_version_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            fact, _ = add_fact(database, "使用 Python。", "skill", ["Python"])
            exit_code, confirmed = run_cli([
                "confirm", fact["id"], "--version", "1", "--db", str(database)
            ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(confirmed["facts"][0]["status"], "confirmed")


class TagFinderTests(unittest.TestCase):
    def test_quick_finder_gives_exactly_the_tags_the_full_patterns_give(self):
        tags = ["Python", "Go", "C++", "machine learning", "CI/CD", "SQL", "机器学习", "Linux", "Kubernetes"]
        texts = [
            "Go to market", "We use Go and C++.", "MACHINE LEARNING systems", "熟悉机器学习", "MySQL only",
            "CI/CD pipelines", "lınux kernel", "LİNUX", "KUBERNETES", "Pythonic", "",
        ]
        find = tag_finder(tags)
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(find(text), [tag for tag in tags if tag_pattern(tag).search(text)])


if __name__ == "__main__":
    unittest.main()
