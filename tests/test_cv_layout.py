import unittest

from cv_layout import describe_changes, effective_layout, guarded_layout, shown_sections


def line(fact_id):
    return {"fact_id": fact_id, "fact_version": 1, "text": f"Text of {fact_id}."}


def entry(title, *fact_ids):
    return {"title": title, "title_link": None, "location": None, "subtitle": None,
            "dates": None, "lines": [line(fact_id) for fact_id in fact_ids], "links": {}}


def draft():
    """Shaped like a real one-page CV: two schools, two jobs, two projects, skills, papers."""
    return {"sections": [
        {"kind": "education", "title": "Education", "entries": [entry("State University", "f-course"), entry("City College")]},
        {"kind": "experience", "title": "Experience", "entries": [
            entry("Acme", "f-acme-import", "f-acme-llm", "f-acme-api"),
            entry("Globex", "f-globex-refactor", "f-globex-api"),
        ]},
        {"kind": "projects", "title": "Projects", "entries": [
            entry("Reader", "f-reader-app", "f-reader-llm", "f-reader-ci"),
            entry("OCR", "f-ocr-buffer", "f-ocr-tests"),
        ]},
        {"kind": "skills", "title": "Skills", "entries": [entry(None, "f-skill-lang", "f-skill-frontend", "f-skill-ai")]},
        {"kind": "publications", "title": "Publications", "entries": [entry(None, "f-pub-gas", "f-pub-llm")]},
    ]}


PROPOSAL = {
    "sections": ["projects", "experience", "skills", "bogus"],
    "entries": [
        {"entry": "s2e1", "lines": ["f-ocr-buffer", "f-ocr-tests"]},
        {"entry": "s2e0", "lines": ["f-reader-llm", "f-reader-app", "f-acme-llm"]},
        {"entry": "s1e1", "lines": ["f-globex-api"]},
        {"entry": "s1e0", "lines": []},
        {"entry": "s3e0", "lines": ["f-skill-ai", "f-skill-lang", "f-skill-lang"]},
        {"entry": "s9e9", "lines": ["f-unknown"]},
    ],
}


def compact(layout):
    return [(section["kind"], [(item["entry"], item["lines"]) for item in section["entries"]]) for section in layout]


class LayoutTests(unittest.TestCase):
    def test_a_proposal_can_only_order_and_leave_out_within_the_guardrails(self):
        self.assertEqual(compact(guarded_layout(draft(), PROPOSAL)), [
            # Education is always kept, in its own order, even when the proposal leaves it out.
            ("education", [("s0e0", ["f-course"]), ("s0e1", [])]),
            ("projects", [("s2e1", ["f-ocr-buffer", "f-ocr-tests"]), ("s2e0", ["f-reader-llm", "f-reader-app"])]),
            # Jobs stay in date order and keep at least one bullet each.
            ("experience", [("s1e0", ["f-acme-import"]), ("s1e1", ["f-globex-api"])]),
            ("skills", [("s3e0", ["f-skill-ai", "f-skill-lang"])]),
        ])

    def test_education_keeps_its_place_in_the_profile(self):
        proposal = {"sections": ["projects", "experience", "education", "skills"], "entries": []}
        self.assertEqual([section["kind"] for section in guarded_layout(draft(), proposal)],
                         ["education", "projects", "experience", "skills"])

    def test_entries_are_cut_only_when_listed_empty_and_otherwise_stay_as_they_are(self):
        proposal = {"sections": ["education", "experience", "projects", "skills", "publications"],
                    "entries": [{"entry": "s2e0", "lines": []}, {"entry": "s1e1", "lines": []}]}
        layout = guarded_layout(draft(), proposal)
        self.assertEqual(compact(layout)[1:3], [
            ("experience", [("s1e0", ["f-acme-import", "f-acme-llm", "f-acme-api"]), ("s1e1", ["f-globex-refactor"])]),
            ("projects", [("s2e1", ["f-ocr-buffer", "f-ocr-tests"])]),
        ])
        self.assertEqual([item["id"] for item in describe_changes(draft(), layout)], ["cut:f-globex-api", "cut:s2e0"])

    def test_every_difference_from_the_usual_cv_is_listed(self):
        changes = describe_changes(draft(), guarded_layout(draft(), PROPOSAL))
        self.assertEqual({item["id"]: item["label"] for item in changes}, {
            "order:sections": "Section order: Education, Projects, Experience, Skills",
            "cut:publications": "Cut section: Publications",
            "order:s2": "Projects order: OCR, Reader",
            "order:s2e0": "Reader: bullets reordered",
            "cut:f-reader-ci": "Cut: Text of f-reader-ci.",
            "cut:f-acme-llm": "Cut: Text of f-acme-llm.",
            "cut:f-acme-api": "Cut: Text of f-acme-api.",
            "cut:f-globex-refactor": "Cut: Text of f-globex-refactor.",
            "order:s3e0": "Skills: lines reordered",
            "cut:f-skill-frontend": "Cut: Text of f-skill-frontend.",
        })

    def test_undoing_a_change_puts_back_exactly_that_part(self):
        layout = guarded_layout(draft(), PROPOSAL)
        self.assertEqual(
            compact(effective_layout(draft(), layout, {"cut:f-reader-ci", "order:s2e0", "cut:publications", "order:sections"})),
            [
                ("education", [("s0e0", ["f-course"]), ("s0e1", [])]),
                ("experience", [("s1e0", ["f-acme-import"]), ("s1e1", ["f-globex-api"])]),
                ("projects", [("s2e1", ["f-ocr-buffer", "f-ocr-tests"]), ("s2e0", ["f-reader-app", "f-reader-llm", "f-reader-ci"])]),
                ("skills", [("s3e0", ["f-skill-ai", "f-skill-lang"])]),
                ("publications", [("s4e0", ["f-pub-gas", "f-pub-llm"])]),
            ],
        )

    def test_the_cv_shows_the_plan_and_an_undone_rewrite_shows_the_fact_again(self):
        planned = draft()
        reader = planned["sections"][2]["entries"][0]
        reader["lines"][1].update(text="Integrated LLM APIs.", source_text="Text of f-reader-llm.",
                                  tailoring={"status": "accepted"})
        planned["plan"] = {"layout": guarded_layout(draft(), PROPOSAL), "undone": ["reword:f-reader-llm"]}
        sections = shown_sections(planned)
        self.assertEqual([section["kind"] for section in sections], ["education", "projects", "experience", "skills"])
        self.assertEqual(
            [item["text"] for item in sections[1]["entries"][1]["lines"]],
            ["Text of f-reader-llm.", "Text of f-reader-app."],
        )
        self.assertEqual(shown_sections(draft()), draft()["sections"])


if __name__ == "__main__":
    unittest.main()
