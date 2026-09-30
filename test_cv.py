import copy
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cv import (
    CVError,
    approve_draft,
    build_draft,
    export_pdf,
    main,
    profile_languages,
    render_html,
    tailor_draft,
)
from facts import confirm_facts, import_facts, revise_fact


FACTS = [
    {"id": "fact-edu-coursework", "type": "education",
     "text": "Coursework: Algorithms, Databases", "tags": ["Algorithms"]},
    {"id": "fact-intern-api", "type": "experience",
     "text": "Built REST APIs for an internal tool.", "tags": ["REST APIs"]},
    {"id": "fact-intern-tests", "type": "experience",
     "text": "Wrote unit tests for billing code.", "tags": ["testing"]},
    {"id": "fact-skills-languages", "type": "skill",
     "text": "Languages: Python, Java", "tags": ["Python", "Java"]},
]
PROFILE = {
    "profile_version": 1,
    "name": {"en": "Alex Example", "zh": "示例"},
    "contact": {
        "location": {"en": "Los Angeles, CA", "zh": "美国 洛杉矶"},
        "email": "alex@example.com",
        "links": [{"label": "github.com/alex", "url": "https://github.com/alex"}],
    },
    "sections": [
        {"kind": "education", "entries": [{
            "title": {"en": "Example University", "zh": "示例大学"},
            "location": "Los Angeles, CA",
            "subtitle": {"en": "M.S. in Computer Science"},
            "dates": "2026 – 2028",
            "facts": ["fact-edu-coursework"],
        }]},
        {"kind": "experience", "entries": [{
            "title": "Example Corp",
            "subtitle": {"en": "Software Intern", "zh": "软件实习生"},
            "dates": "2025",
            "facts": ["fact-intern-api", "fact-intern-tests"],
        }]},
        {"kind": "skills", "entries": [{"facts": ["fact-skills-languages"]}]},
    ],
}


class ProfileLanguageTests(unittest.TestCase):
    def test_cvs_are_offered_only_in_the_languages_the_profile_is_written_in(self):
        cases = {
            "both": ({"en": "Alex Example", "zh": "示例"}, ["en", "zh"]),
            "english only": ({"en": "Alex Example", "zh": "  "}, ["en"]),
            "plain english": ("Alex Example", ["en"]),
            "plain chinese": ("张三", ["zh"]),
        }
        for label, (name, languages) in cases.items():
            with self.subTest(label):
                self.assertEqual(profile_languages({**PROFILE, "name": name}), languages)


def make_store(directory, confirm=True):
    database = Path(directory) / "workbench.db"
    import_facts(database, FACTS)
    if confirm:
        confirm_facts(database, [(item["id"], 1) for item in FACTS])
    return database


def linked_job(fact_ids):
    """A minimal matching.py decide output that links one requirement per fact."""
    requirements = [f"Requirement number {index}" for index, _ in enumerate(fact_ids)]
    return {
        "jd": {
            "text": "Requirements:\n" + "\n".join(requirements),
            "captured_at": "2026-09-23T00:00:00+00:00",
            "title": "Software Engineer Intern",
            "source": None,
        },
        "facts": [
            {"id": fact_id, "version": 1, "text": "quoted fact", "confirmed": True}
            for fact_id in fact_ids
        ],
        "selected_requirements": [
            {"id": f"req-{index}", "text": text, "fact_id": fact_id, "fact_version": 1}
            for index, (text, fact_id) in enumerate(zip(requirements, fact_ids))
        ],
    }


def lines_of(draft, kind):
    section = next(item for item in draft["sections"] if item["kind"] == kind)
    return [line["text"] for entry in section["entries"] for line in entry["lines"]]


class CVDraftTests(unittest.TestCase):
    def test_draft_uses_profile_header_and_exact_confirmed_fact_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            draft = build_draft(PROFILE, make_store(directory), "en")
        self.assertEqual(draft["header"]["name"], "Alex Example")
        self.assertEqual(draft["paper"], "letter")
        self.assertEqual(draft["sections"][0]["title"], "Education")
        self.assertEqual(lines_of(draft, "experience"), [
            "Built REST APIs for an internal tool.", "Wrote unit tests for billing code.",
        ])
        self.assertEqual(
            {(item["id"], item["version"]) for item in draft["facts"]},
            {(item["id"], 1) for item in FACTS},
        )
        self.assertEqual(len(draft["profile_sha256"]), 64)

    def test_unconfirmed_facts_are_refused_and_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory, confirm=False)
            confirm_facts(database, [("fact-edu-coursework", 1)])
            with self.assertRaises(CVError) as raised:
                build_draft(PROFILE, database, "en")
        message = str(raised.exception)
        self.assertIn("fact-intern-api@1", message)
        self.assertIn("fact-skills-languages@1", message)
        self.assertNotIn("fact-edu-coursework", message)

    def test_chinese_draft_uses_chinese_fields_and_lists_english_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            draft = build_draft(PROFILE, make_store(directory), "zh")
        self.assertEqual(draft["header"]["name"], "示例")
        self.assertEqual(draft["paper"], "a4")
        self.assertEqual(
            [section["title"] for section in draft["sections"]], ["教育背景", "实习经历", "专业技能"]
        )
        self.assertEqual(draft["sections"][1]["entries"][0]["subtitle"], "软件实习生")
        self.assertEqual(draft["sections"][0]["entries"][0]["subtitle"], "M.S. in Computer Science")
        self.assertEqual(draft["language_fallbacks"], ["sections[0].entries[0].subtitle"])

    def test_job_matched_facts_come_first_within_their_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            draft = build_draft(
                PROFILE, make_store(directory), "en", job=linked_job(["fact-intern-tests"])
            )
        self.assertEqual(lines_of(draft, "experience"), [
            "Wrote unit tests for billing code.", "Built REST APIs for an internal tool.",
        ])
        self.assertEqual(draft["job"]["title"], "Software Engineer Intern")
        self.assertEqual(draft["job"]["matched_fact_ids"], ["fact-intern-tests"])

    def test_invalid_profiles_fail_with_the_problem_path(self):
        def with_change(change):
            profile = copy.deepcopy(PROFILE)
            change(profile)
            return profile

        cases = [
            (lambda p: p["sections"][1]["entries"][0].update(fact=["x"]), "entries[0] 包含未知字段：fact"),
            (lambda p: p["contact"]["links"][0].update(url="github.com/alex"), "contact.links[0].url"),
            (lambda p: p["sections"][2].update(kind="hobbies"), "sections[2].kind"),
            (lambda p: p["sections"][1]["entries"][0].update(
                links={"REST APIs": "javascript:alert(1)"}), "links.REST APIs"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory, confirm=False)
            for change, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(CVError, re.escape(message)):
                        build_draft(with_change(change), database, "en")


def draft_for(language="en", change=None):
    profile = copy.deepcopy(PROFILE)
    if change:
        change(profile)
    with tempfile.TemporaryDirectory() as directory:
        return build_draft(profile, make_store(directory), language)


class CVRenderTests(unittest.TestCase):
    def test_html_escapes_text_links_only_listed_phrases_and_blocks_network(self):
        def change(profile):
            profile["name"]["en"] = "Alex <Example>"
            profile["sections"][1]["entries"][0]["links"] = {"internal tool": "https://example.com/t"}

        html = render_html(draft_for("en", change))
        self.assertIn("Alex &lt;Example&gt;", html)
        self.assertNotIn("<Example>", html)
        self.assertIn('<a href="https://example.com/t">internal tool</a>', html)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn('class="watermark"', html)
        self.assertNotIn('class="watermark"', render_html(draft_for("en"), final=True))

    def test_language_paper_and_skill_labels_are_rendered(self):
        english = render_html(draft_for("en"))
        chinese = render_html(draft_for("zh"))
        self.assertIn("<strong>Languages:</strong> Python, Java", english)
        translated = draft_for("zh")
        translated["sections"][2]["entries"][0]["lines"][0]["text"] = "语言：Python, Java"
        self.assertIn("<strong>语言：</strong>Python, Java", render_html(translated))
        self.assertIn("size: Letter", english)
        self.assertIn('lang="zh-CN"', chinese)
        self.assertIn("size: A4", chinese)
        self.assertIn("专业技能", chinese)


class FakePrinter:
    """Stands in for headless Chrome: records the HTML and writes a two-page PDF stub."""

    def __init__(self):
        self.html = None

    def __call__(self, html_path, pdf_path):
        self.html = Path(html_path).read_text(encoding="utf-8")
        Path(pdf_path).write_bytes(b"%PDF-1.4\n1 0 obj << /Type /Page >>\n2 0 obj << /Type /Page >>\n%%EOF\n")


class CVExportTests(unittest.TestCase):
    def test_export_writes_a_new_watermarked_pdf_and_counts_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            draft = build_draft(PROFILE, database, "en")
            output = Path(directory) / "cv.pdf"
            printer = FakePrinter()
            result = export_pdf(draft, database, output, printer=printer)
            self.assertTrue(output.read_bytes().startswith(b"%PDF"))
            with self.assertRaises(CVError):
                export_pdf(draft, database, output, printer=FakePrinter())
        self.assertEqual(result["pages"], 2)
        self.assertIn('class="watermark"', printer.html)

    def test_export_refuses_drafts_with_changed_facts_or_edited_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            edited = build_draft(PROFILE, database, "en")
            edited["sections"][1]["entries"][0]["lines"][0]["text"] = "Led the whole API platform."
            with self.assertRaisesRegex(CVError, "fact-intern-api"):
                export_pdf(edited, database, Path(directory) / "edited.pdf", printer=FakePrinter())
            stale = build_draft(PROFILE, database, "en")
            revise_fact(database, "fact-intern-tests", text="Wrote unit tests for payment code.")
            with self.assertRaisesRegex(CVError, "fact-intern-tests"):
                export_pdf(stale, database, Path(directory) / "stale.pdf", printer=FakePrinter())
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["workbench.db"])


FACT_IDS = {item["text"]: item["id"] for item in FACTS}


def stand_ins_for(lines, key="id"):
    """The ID a request gave each of FACTS's lines, found by text as DeepSeek would see it."""
    sent = {line["text"]: line[key] for line in lines}
    return {item["id"]: sent[item["text"]] for item in FACTS if item["text"] in sent}


def swap_ids(value, ids):
    """``value`` with every string that is a key of ``ids`` replaced by its value."""
    if isinstance(value, dict):
        return {key: swap_ids(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [swap_ids(item, ids) for item in value]
    return ids.get(value, value) if isinstance(value, str) else value


class FakeChat:
    """Stands in for DeepSeek: echoes each requested line unless a rewrite is given for the
    fact whose text it is (by fact ID, while the request names lines by stand-ins)."""

    def __init__(self, rewrites=None, drop=None):
        self.rewrites = rewrites or {}
        self.drop = drop
        self.messages = None

    def __call__(self, messages, model, effort):
        self.messages = messages
        request = json.loads(messages[-1]["content"])
        lines = [
            {"fact_id": line["fact_id"], "text": self.rewrites.get(FACT_IDS.get(line["text"]), line["text"])}
            for line in request["lines"] if self.drop is None or FACT_IDS.get(line["text"]) != self.drop
        ]
        return {
            "model": "deepseek-flash",
            "content": {"lines": lines},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def line_for(draft, fact_id):
    return next(
        line for section in draft["sections"] for entry in section["entries"]
        for line in entry["lines"] if line["fact_id"] == fact_id
    )


class CVTailorTests(unittest.TestCase):
    def test_supported_rewrites_are_used_and_unsupported_ones_keep_the_fact(self):
        chat = FakeChat({
            "fact-intern-api": "For an internal tool, built REST APIs.",
            "fact-intern-tests": "Wrote 40 unit tests for billing code.",
        })
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            draft = build_draft(PROFILE, database, "en")
            tailored = tailor_draft(draft, database, job=linked_job(["fact-intern-api"]), chat=chat)
        accepted = line_for(tailored, "fact-intern-api")
        rejected = line_for(tailored, "fact-intern-tests")
        self.assertEqual(accepted["text"], "For an internal tool, built REST APIs.")
        self.assertEqual(accepted["source_text"], "Built REST APIs for an internal tool.")
        self.assertEqual(accepted["tailoring"]["status"], "accepted")
        self.assertEqual(rejected["text"], "Wrote unit tests for billing code.")
        self.assertEqual(rejected["tailoring"]["status"], "rejected")
        self.assertTrue(any("40" in reason for reason in rejected["tailoring"]["reasons"]))
        self.assertEqual((tailored["tailoring"]["accepted"], tailored["tailoring"]["rejected"]), (3, 1))
        # A requirement saved before strengths were kept counts as unclear, never as required.
        self.assertEqual(json.loads(chat.messages[-1]["content"])["job_requirements"],
                         [{"text": "Requirement number 0", "strength": "unclear"}])

    def test_request_holds_only_lines_and_requirements_never_contact_or_papers(self):
        profile = copy.deepcopy(PROFILE)
        profile["contact"]["phone"] = "213-555-0100"
        profile["sections"].append({"kind": "publications", "entries": [{"facts": ["fact-paper"]}]})
        chat = FakeChat()
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            import_facts(database, [{
                "id": "fact-paper", "type": "achievement", "text": "A Private Paper Title. Venue, 2025.",
            }])
            confirm_facts(database, [("fact-paper", 1)])
            draft = build_draft(profile, database, "zh")
            tailor_draft(draft, database, job=linked_job(["fact-intern-api"]), chat=chat)
        request = json.dumps(chat.messages, ensure_ascii=False)
        for private in ("Alex Example", "示例", "alex@example.com", "213-555-0100",
                        "Los Angeles", "github.com/alex", "Example Corp", "Example University",
                        "A Private Paper Title"):
            self.assertNotIn(private, request)
        self.assertIn("Built REST APIs for an internal tool.", request)
        self.assertIn("Requirement number 0", request)

    def test_tailored_draft_exports_until_its_fact_changes(self):
        chat = FakeChat({"fact-intern-api": "For an internal tool, built REST APIs."})
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            tailored = tailor_draft(build_draft(PROFILE, database, "en"), database, chat=chat)
            printer = FakePrinter()
            export_pdf(tailored, database, Path(directory) / "tailored.pdf", printer=printer)
            revise_fact(database, "fact-intern-api", text="Built REST APIs for two internal tools.")
            with self.assertRaisesRegex(CVError, "fact-intern-api"):
                export_pdf(tailored, database, Path(directory) / "stale.pdf", printer=FakePrinter())
        self.assertIn("For an internal tool, built REST APIs.", printer.html)

    def test_lines_go_out_under_stand_in_ids_never_their_fact_ids(self):
        # A fact ID can be made from its text, such as fact-example-university-coursework, and so name a school.
        chat = FakeChat({"fact-intern-api": "For an internal tool, built REST APIs."})
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            tailored = tailor_draft(build_draft(PROFILE, database, "en"), database, chat=chat)
        sent = chat.messages[-1]["content"]
        for fact in FACTS:
            self.assertNotIn(fact["id"], sent)
        self.assertEqual(line_for(tailored, "fact-intern-api")["text"], "For an internal tool, built REST APIs.")
        self.assertEqual(line_for(tailored, "fact-intern-tests")["text"], "Wrote unit tests for billing code.")

    def test_answers_that_skip_a_requested_line_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            draft = build_draft(PROFILE, database, "en")
            with self.assertRaisesRegex(CVError, "不一致"):
                tailor_draft(draft, database, chat=FakeChat(drop="fact-intern-tests"))


class CVApprovalTests(unittest.TestCase):
    def test_only_an_approved_draft_exports_without_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            draft = build_draft(PROFILE, database, "en")
            unapproved = export_pdf(draft, database, Path(directory) / "draft.pdf", printer=FakePrinter())
            printer = FakePrinter()
            final = export_pdf(
                approve_draft(draft, database), database, Path(directory) / "final.pdf", printer=printer
            )
        self.assertFalse(unapproved["final"])
        self.assertEqual(unapproved["watermark"], "DRAFT")
        self.assertTrue(final["final"])
        self.assertIsNone(final["watermark"])
        self.assertNotIn('class="watermark"', printer.html)

    def test_any_change_after_approval_blocks_the_final_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            approved = approve_draft(build_draft(PROFILE, database, "en"), database)
            edited = copy.deepcopy(approved)
            edited["header"]["name"] = "Alex Q. Example"
            with self.assertRaisesRegex(CVError, "批准后内容已改变"):
                export_pdf(edited, database, Path(directory) / "edited.pdf", printer=FakePrinter())
            revise_fact(database, "fact-intern-api", text="Built REST APIs for two internal tools.")
            with self.assertRaisesRegex(CVError, "fact-intern-api"):
                export_pdf(approved, database, Path(directory) / "stale.pdf", printer=FakePrinter())
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["workbench.db"])

    def test_stale_or_already_approved_drafts_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            draft = build_draft(PROFILE, database, "en")
            with self.assertRaisesRegex(CVError, "已经批准"):
                approve_draft(approve_draft(draft, database), database)
            revise_fact(database, "fact-intern-tests", text="Wrote unit tests for payment code.")
            with self.assertRaisesRegex(CVError, "fact-intern-tests"):
                approve_draft(draft, database)

    def test_approved_file_cannot_be_tailored_again(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            approved = approve_draft(build_draft(PROFILE, database, "en"), database)
            with self.assertRaisesRegex(CVError, "已批准"):
                tailor_draft(approved, database, chat=FakeChat())


class CVCommandLineTests(unittest.TestCase):
    def run_cli(self, arguments):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main([str(argument) for argument in arguments])
        self.assertEqual(exit_code, 0)
        return json.loads(stdout.getvalue())

    def test_cli_draft_then_pdf_create_new_files_and_warn_about_length(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = make_store(directory)
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(PROFILE, ensure_ascii=False), encoding="utf-8")
            with patch("cv.print_with_chrome", FakePrinter()):
                drafted = self.run_cli([
                    "draft", "--profile", profile_path, "--facts-db", database,
                    "--language", "zh", "--output", root / "draft.json",
                ])
                printed = self.run_cli([
                    "pdf", root / "draft.json", "--facts-db", database, "--output", root / "cv.pdf",
                ])
            self.assertTrue((root / "cv.pdf").exists())
        self.assertEqual((drafted["language"], drafted["paper"]), ("zh", "a4"))
        self.assertEqual(printed["pages"], 2)
        self.assertIn("2 页", printed["warning"])

    def test_cli_tailor_writes_a_new_draft_and_lists_rejected_lines(self):
        chat = FakeChat({"fact-intern-tests": "Wrote 40 unit tests for billing code."})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = make_store(directory)
            (root / "draft.json").write_text(
                json.dumps(build_draft(PROFILE, database, "en"), ensure_ascii=False), encoding="utf-8"
            )
            with patch("cv.chat_json", chat):
                summary = self.run_cli([
                    "tailor", root / "draft.json", "--facts-db", database,
                    "--output", root / "tailored.json",
                ])
            saved = json.loads((root / "tailored.json").read_text(encoding="utf-8"))
        self.assertEqual((summary["accepted"], summary["rejected"]), (3, 1))
        self.assertEqual(summary["rejected_lines"][0]["fact_id"], "fact-intern-tests")
        self.assertEqual(saved["tailoring"]["model"], "deepseek-flash")

    def test_cli_approve_lists_rewrites_then_pdf_is_final(self):
        chat = FakeChat({"fact-intern-api": "For an internal tool, built REST APIs."})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = make_store(directory)
            tailored = tailor_draft(build_draft(PROFILE, database, "en"), database, chat=chat)
            (root / "tailored.json").write_text(json.dumps(tailored, ensure_ascii=False), encoding="utf-8")
            with patch("cv.print_with_chrome", FakePrinter()):
                approved = self.run_cli([
                    "approve", root / "tailored.json", "--facts-db", database,
                    "--output", root / "approved.json",
                ])
                printed = self.run_cli([
                    "pdf", root / "approved.json", "--facts-db", database, "--output", root / "final.pdf",
                ])
        self.assertEqual(approved["rewritten_lines"], [{
            "fact_id": "fact-intern-api",
            "from": "Built REST APIs for an internal tool.",
            "to": "For an internal tool, built REST APIs.",
            "undone": False,
        }])
        self.assertTrue(printed["final"])

    def test_documented_example_profile_and_facts_work_together(self):
        examples = Path(__file__).parent / "examples"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "workbench.db"
            items = json.loads((examples / "synthetic_cv_facts.json").read_text(encoding="utf-8"))["facts"]
            import_facts(database, items)
            confirm_facts(database, [(item["id"], 1) for item in items])
            with patch("cv.print_with_chrome", FakePrinter()):
                for language in ("en", "zh"):
                    self.run_cli([
                        "draft", "--profile", examples / "synthetic_cv_profile.json",
                        "--facts-db", database, "--language", language,
                        "--output", root / f"draft-{language}.json",
                    ])
                    self.run_cli([
                        "pdf", root / f"draft-{language}.json", "--facts-db", database,
                        "--output", root / f"cv-{language}.pdf",
                    ])
            self.assertTrue((root / "cv-en.pdf").exists() and (root / "cv-zh.pdf").exists())



class PrivateLineTests(unittest.TestCase):
    def test_a_line_naming_the_employer_is_never_sent_whatever_extra_words_are_given(self):
        # The caller's words add to the draft's own; an empty list must not drop them.
        billing = {"id": "fact-intern-billing", "type": "experience", "text": "Built the Example Corp billing tool.", "tags": []}
        with tempfile.TemporaryDirectory() as directory:
            database = make_store(directory)
            import_facts(database, [billing])
            confirm_facts(database, [("fact-intern-billing", 1)])
            profile = copy.deepcopy(PROFILE)
            profile["sections"][1]["entries"][0]["facts"].append("fact-intern-billing")
            chat = FakeChat()
            tailored = tailor_draft(build_draft(profile, database, "en"), database, chat=chat, private=[])
        self.assertNotIn("Example Corp", chat.messages[-1]["content"])
        self.assertEqual(line_for(tailored, "fact-intern-billing")["text"], "Built the Example Corp billing tool.")


if __name__ == "__main__":
    unittest.main()
