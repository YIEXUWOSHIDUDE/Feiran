import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from cv import build_draft
from cv_import import STRUCTURE_RULES, build_profile, split_private, structure_cv
from facts import add_fact, confirm_fact, confirm_facts, import_facts, list_facts, revise_fact

HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
if HAS_PYPDF:
    from cv_import import read_pdf


def minimal_pdf(lines, links=()):
    """A one-page PDF with (x, y, text) strings and (rectangle, URL) links, built by hand."""
    stream = "\n".join(f"BT /F1 11 Tf {x} {y} Td ({text}) Tj ET" for x, y, text in lines).encode()
    annotations = " ".join(f"{6 + index} 0 R" for index in range(len(links)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> /Annots [" + annotations.encode() + b"] >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        *(f"<< /Type /Annot /Subtype /Link /Rect [{' '.join(map(str, rect))}] /A << /S /URI /URI ({url}) >> >>".encode()
          for rect, url in links),
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    output += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return bytes(output)


LINES = [
    ["ALEX EXAMPLE"],
    ["Los Angeles, CA | 000-000-0000 | alex@example.com"],
    ["github.com/alex-example | linkedin.com/in/alex-example"],
    ["EDUCATION"],
    ["Example State University", "Los Angeles, CA"],
    ["Master of Science in Computer Science", "Aug 2026 – May 2028"],
    ["Coursework: Algorithms, Databases"],
    ["EXPERIENCE"],
    ["Example Corp", "Chengdu, China"],
    ["Software Intern", "Jun 2025 – Aug 2025"],
    ["• Built REST APIs for an internal tool using Python and"],
    ["FastAPI."],
    ["• Wrote unit tests for billing code."],
    ["TECHNICAL SKILLS"],
    ["Languages: Python, Java"],
    ["Hobbies: chess"],
]

ANSWER = {"sections": [
    {"kind": "education", "heading": 4, "entries": [
        {"title": [5, 1], "location": [5, 2], "subtitle": [6, 1], "dates": [6, 2],
         "facts": [{"lines": [7], "tags": ["Algorithms", "Databases", "Rust"]}]},
    ]},
    {"kind": "experience", "heading": 8, "entries": [
        {"title": [9, 1], "location": [9, 2], "subtitle": [10, 1], "dates": [10, 2], "facts": [
            {"lines": [11, 12, 13], "tags": []},
            {"lines": [11, 12], "tags": ["REST APIs", "FastAPI"]},
            {"lines": [13], "tags": ["unit tests"]},
            {"lines": [2], "tags": []},
            {"lines": [11], "tags": []},
        ]},
    ]},
    {"kind": "skills", "heading": 14, "entries": [{"facts": [{"lines": [15], "tags": ["Python", "Java"]}]}]},
    {"kind": "hobbies", "heading": 16, "entries": []},
]}


class FakeStructurer:
    def __init__(self, answer=ANSWER):
        self.answer = answer
        self.sent = None

    def __call__(self, messages, model, effort):
        assert messages[0]["content"] == STRUCTURE_RULES
        self.sent = messages[-1]["content"]
        return {"model": "deepseek-flash", "content": self.answer, "usage": {}}


class CVImportTests(unittest.TestCase):
    def test_name_contact_and_links_stay_local(self):
        public, private = split_private(LINES)
        self.assertEqual(private, {
            "name": "Alex Example", "location": "Los Angeles, CA", "phone": "000-000-0000",
            "email": "alex@example.com", "other": [],
            "links": [{"label": "github.com/alex-example", "url": "https://github.com/alex-example"},
                      {"label": "linkedin.com/in/alex-example", "url": "https://linkedin.com/in/alex-example"}],
        })
        self.assertEqual([line["n"] for line in public], list(range(4, 17)))

    def test_facts_are_copied_word_for_word_from_the_lines_deepseek_points_at(self):
        chat = FakeStructurer()
        proposal = structure_cv(LINES, chat)
        sections = {section["kind"]: section for section in proposal["sections"]}
        education = sections["education"]["entries"][0]
        experience = sections["experience"]["entries"][0]
        self.assertEqual(list(sections), ["education", "experience", "skills"])
        self.assertEqual((sections["education"]["title"], sections["skills"]["title"]), ("Education", "Technical Skills"))
        self.assertEqual((education["title"], education["location"], education["subtitle"], education["dates"]),
                         ("Example State University", "Los Angeles, CA", "Master of Science in Computer Science", "Aug 2026 – May 2028"))
        self.assertEqual(education["facts"], [{"text": "Coursework: Algorithms, Databases", "tags": ["Algorithms", "Databases"]}])
        self.assertEqual([fact["text"] for fact in experience["facts"]], [
            "Built REST APIs for an internal tool using Python and FastAPI.",
            "Wrote unit tests for billing code.",
        ])
        self.assertEqual(proposal["not_imported"], ["Hobbies: chess"])
        for private in ("ALEX", "alex@example.com", "000-000-0000", "github.com/alex-example"):
            self.assertNotIn(private, chat.sent)

    def test_the_profile_builds_a_cv_and_reuses_facts_already_stored(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            existing, _ = add_fact(database, "Wrote unit tests for billing code.", "experience", ["unit tests"])
            confirm_fact(database, existing["id"], 1)
            proposal = structure_cv(LINES, FakeStructurer())
            profile, items, reused = build_profile(proposal, proposal["private"], database)
            import_facts(database, items)
            confirm_facts(database, [(item["id"], 1) for item in items])
            draft = build_draft(profile, database, "en")
        self.assertEqual(reused, 1)
        self.assertEqual(profile["sections"][1]["entries"][0]["facts"][1], existing["id"])
        self.assertEqual(draft["header"]["name"], "Alex Example")
        self.assertEqual(
            [line["text"] for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]],
            ["Coursework: Algorithms, Databases", "Built REST APIs for an internal tool using Python and FastAPI.",
             "Wrote unit tests for billing code.", "Languages: Python, Java"],
        )

    def test_links_in_the_pdf_link_an_entry_name_and_a_phrase_but_never_reach_deepseek(self):
        lines = [["ALEX EXAMPLE"], ["alex@example.com"], ["PROJECTS"], ["Course Search", "GitHub", "Python"],
                 ["• Wrote the Search Guide for new users."], ["Docs: alex-example.github.io/search, help@example.com"]]
        links = [{"url": "https://github.com/alex-example/course-search", "pieces": ["GitHub"]},
                 {"url": "https://example.com/guide", "pieces": ["Search Gu ide"]}]  # runs can be spaced unevenly
        answer = {"sections": [{"kind": "projects", "heading": 3, "entries": [
            {"title": [4, 1], "location": [4, 3], "facts": [{"lines": [5], "tags": []}]}]}]}
        chat = FakeStructurer(answer)
        entry = structure_cv(lines, chat, links=links)["sections"][0]["entries"][0]
        self.assertEqual(entry["title_link"], {"label": "GitHub", "url": "https://github.com/alex-example/course-search"})
        self.assertEqual(entry["links"], {"Search Guide": "https://example.com/guide"})
        for private in ("https://", "github.io", "help@example.com"):
            self.assertNotIn(private, chat.sent)

    # The next three were found in review by Codex (gpt-6-astra, 2026-09-27).
    def test_a_name_and_contact_details_on_one_line_stay_local(self):
        lines = [["Alex Example alex@example.com 213-555-0199"], ["SKILLS"], ["Languages: Python"],
                 ["References: call 213-555-0199"]]
        answer = {"sections": [{"kind": "skills", "heading": 2, "entries": [{"facts": [{"lines": [3], "tags": ["Python"]}]}]}]}
        chat = FakeStructurer(answer)
        private = structure_cv(lines, chat)["private"]
        self.assertEqual((private["name"], private["email"], private["phone"]), ("Alex Example", "alex@example.com", "213-555-0199"))
        for text in ("Alex", "Example", "alex@", "213-555-0199"):
            self.assertNotIn(text, chat.sent)

    def test_links_with_the_same_text_stay_on_their_own_line(self):
        lines = [["ALEX EXAMPLE"], ["alex@example.com"], ["PROJECTS"], ["Alpha", "Python"], ["• See GitHub for source."],
                 ["Beta", "GitHub", "Go"], ["• Built a parser."]]
        links = [{"url": "https://example.com/first", "pieces": ["GitHub"]},
                 {"url": "https://example.com/second", "pieces": ["GitHub"]}]
        answer = {"sections": [{"kind": "projects", "heading": 3, "entries": [
            {"title": [4, 1], "location": [4, 2], "facts": [{"lines": [5], "tags": []}]},
            {"title": [6, 1], "location": [6, 3], "facts": [{"lines": [7], "tags": []}]}]}]}
        alpha, beta = structure_cv(lines, FakeStructurer(answer), links=links)["sections"][0]["entries"]
        self.assertEqual((alpha["title_link"], alpha["links"]), (None, {"GitHub": "https://example.com/first"}))
        self.assertEqual((beta["title_link"], beta["links"]), ({"label": "GitHub", "url": "https://example.com/second"}, {}))

    def test_a_link_skips_the_same_words_written_without_a_link(self):
        # Seen on a real CV: "GitHub Actions" in a bullet sat between two projects' GitHub links.
        lines = [["ALEX EXAMPLE"], ["alex@example.com"], ["PROJECTS"], ["Alpha |", "GitHub", "Python"],
                 ["• Added GitHub Actions to build it."], ["Beta |", "GitHub", "Go"], ["• Built a parser."]]
        links = [{"url": "https://example.com/alpha", "pieces": ["GitHub"], "context": "Alpha | GitHub Python"},
                 {"url": "https://example.com/beta", "pieces": ["GitHub"], "context": "Beta | GitHub Go"}]
        answer = {"sections": [{"kind": "projects", "heading": 3, "entries": [
            {"title": [4, 1], "location": [4, 3], "facts": [{"lines": [5], "tags": []}]},
            {"title": [6, 1], "location": [6, 3], "facts": [{"lines": [7], "tags": []}]}]}]}
        alpha, beta = structure_cv(lines, FakeStructurer(answer), links=links)["sections"][0]["entries"]
        self.assertEqual((alpha["title_link"]["url"], beta["title_link"]["url"]), ("https://example.com/alpha", "https://example.com/beta"))
        self.assertEqual(alpha["links"], {})

    def test_two_identical_linked_lines_keep_their_own_links(self):
        # Found in review of PR #2 by Codex: the second link went to the first line.
        lines = [["ALEX EXAMPLE"], ["alex@example.com"], ["PROJECTS"], ["Alpha", "Python"], ["• See GitHub for source."],
                 ["Beta", "Go"], ["• See GitHub for source."]]
        links = [{"url": "https://example.com/alpha", "pieces": ["GitHub"], "context": "• See GitHub for source."},
                 {"url": "https://example.com/beta", "pieces": ["GitHub"], "context": "• See GitHub for source."}]
        answer = {"sections": [{"kind": "projects", "heading": 3, "entries": [
            {"title": [4, 1], "location": [4, 2], "facts": [{"lines": [5], "tags": []}]},
            {"title": [6, 1], "location": [6, 2], "facts": [{"lines": [7], "tags": []}]}]}]}
        alpha, beta = structure_cv(lines, FakeStructurer(answer), links=links)["sections"][0]["entries"]
        self.assertEqual((alpha["links"], beta["links"]),
                         ({"GitHub": "https://example.com/alpha"}, {"GitHub": "https://example.com/beta"}))

    def test_uploading_an_old_cv_again_never_undoes_a_later_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workbench.db"
            proposal = structure_cv(LINES, FakeStructurer())
            _, items, _ = build_profile(proposal, proposal["private"], database)
            import_facts(database, items)
            built = items[1]
            revise_fact(database, built["id"], text="Helped build REST APIs for an internal tool.")
            confirm_fact(database, built["id"], 2)
            _, again, _ = build_profile(proposal, proposal["private"], database)
            import_facts(database, again)
            corrected = next(fact for fact in list_facts(database) if fact["id"] == built["id"])
        self.assertEqual((corrected["text"], corrected["version"], corrected["status"]),
                         ("Helped build REST APIs for an internal tool.", 2, "confirmed"))
        self.assertEqual([item["text"] for item in again], [built["text"]])  # a separate new fact to check
        self.assertNotEqual(again[0]["id"], built["id"])

    @unittest.skipUnless(HAS_PYPDF, "reading PDFs needs pypdf from requirements.txt")
    def test_a_pdf_is_read_as_columns_with_the_text_each_link_covers(self):
        data = minimal_pdf(
            [(72, 720, "Example Corp"), (430, 720, "Chengdu, China"), (72, 700, "Built REST APIs."), (72, 680, "GitHub")],
            links=[((70, 676, 110, 690), "https://github.com/alex-example")],
        )
        self.assertEqual(read_pdf(data), {
            "lines": [["Example Corp", "Chengdu, China"], ["Built REST APIs."], ["GitHub"]],
            "links": [{"url": "https://github.com/alex-example", "pieces": ["GitHub"], "context": "GitHub"}],
        })


if __name__ == "__main__":
    unittest.main()
