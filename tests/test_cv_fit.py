"""Fitting a job's CV on one page by leaving out what helps the job least (cv_fit), with
synthetic CVs only. Page counts come from a stand-in printer, and from real Chrome when one is
configured (CHROME_PATH or on PATH), since that is what the final PDF is printed with."""
import copy
import math
import re
import unittest
from html import unescape
from pathlib import Path

from feiran.cv import CVError, _find_chrome, build_draft_from_facts, rendered_pages
from feiran.cv_edit import add_line, change_line, edits_view, set_edit, shown_text, without_edits
from feiran.cv_fit import FIT_REASON, MAX_RENDERS, FitError, candidates, fit_one_page, fit_outcome
from feiran.cv_layout import shown_sections
from feiran.cv_plan import set_change
from feiran.gaps import _entry_key
from tests.fit_fixture import ADDED_BULLET, FACTS, JOB, PROFILE

try:
    import pypdf
except ImportError:  # pragma: no cover - requirements.txt installs it
    pypdf = None


class RowPrinter:
    """Stands in for Chrome: lays the CV's text out in rows of ``width`` characters and writes a
    PDF stub with one page per ``rows`` rows, so leaving lines out really shortens it."""

    def __init__(self, rows=52, width=110):
        self.rows, self.width, self.calls = rows, width, 0

    def count(self, html):
        body = html.split("<body", 1)[1]
        body = re.sub(r'<div class="watermark">.*?</div>', "", body)
        blocks = re.findall(r"<(?:li|div class=\"line\"|div class=\"row\"|h1|h2|div class=\"contact\")[^>]*>(.*?)</(?:li|div|h1|h2)>", body)
        used = sum(max(1, math.ceil(len(unescape(re.sub(r"<[^>]+>", "", block))) / self.width)) for block in blocks)
        return max(1, math.ceil(used / self.rows))

    def __call__(self, html_path, pdf_path):
        self.calls += 1
        pages = self.count(Path(html_path).read_text(encoding="utf-8"))
        Path(pdf_path).write_bytes(b"%PDF-1.4\n" + b"".join(b"%d 0 obj << /Type /Page >>\n" % (n + 1) for n in range(pages))
                                   + b"%%EOF\n")


def long_cv():
    """The reported case: 20 lines worded long, plus one adopted suggestion."""
    draft = build_draft_from_facts(PROFILE, FACTS, "en", job=JOB)
    key = _entry_key("experience", draft["sections"][1]["entries"][0])
    draft, _ = add_line(draft, key, ADDED_BULLET, requirements=["r4"], origin="suggestion")
    return draft


def shown_ids(draft):
    return [line["fact_id"] for section in shown_sections(draft) for entry in section["entries"] for line in entry["lines"]]


def stored_ids(draft):
    return [line["fact_id"] for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]]


class FitTests(unittest.TestCase):
    def setUp(self):
        self.printer = RowPrinter()
        self.pages = lambda draft: rendered_pages(draft, self.printer)

    def test_two_pages_become_one_by_leaving_out_the_least_related_lines(self):
        draft = long_cv()
        original = copy.deepcopy(draft)
        self.assertEqual(self.pages(draft), 2)
        fitted, outcome = fit_one_page(draft, JOB, self.pages)
        self.assertEqual(draft, original)  # the CV it was given is not changed
        self.assertEqual((outcome["status"], outcome["pages_before"], outcome["pages"]), ("fitted", 2, 1))
        self.assertEqual(self.pages(fitted), 1)
        left_out = set(shown_ids(draft)) - set(shown_ids(fitted))
        self.assertEqual(len(left_out), outcome["left_out"])
        # Papers and coursework go before any job or project line that names what the job asks for.
        self.assertTrue(left_out & {"fact-pub-1", "fact-pub-2", "fact-be-course", "fact-ms-course"})
        for kept in ("fact-x1-api", "fact-x1-cache", "fact-x1-tests", "fact-x2-pipeline", "fact-p1-api"):
            self.assertIn(kept, shown_ids(fitted))
        # Nothing is deleted or reworded: every line is still stored, with the same words.
        self.assertEqual(stored_ids(fitted), stored_ids(draft))
        self.assertEqual(fitted["sections"], draft["sections"])
        self.assertEqual(fitted["edits"], draft["edits"])
        # Each new cut is listed, explained, and can be undone on its own.
        fit_changes = [change for change in fitted["plan"]["changes"] if change.get("fit")]
        self.assertEqual([change["id"] for change in fit_changes], outcome["changes"])
        self.assertTrue(all(change["reason"] == FIT_REASON and change["type"] == "cut" for change in fit_changes))
        restored = fitted
        for change in fit_changes:
            restored = set_change(restored, change["id"])
        self.assertEqual(shown_ids(restored), shown_ids(draft))
        self.assertEqual(set_change(restored, fit_changes[0]["id"], undone=False)["plan"]["undone"],
                         sorted(change["id"] for change in fit_changes[1:]))
        self.assertLessEqual(outcome["renders"], MAX_RENDERS)
        self.assertEqual(fitted["plan"]["fit"]["status"], "fitted")

    def test_a_cv_that_already_fits_is_left_as_it_is(self):
        draft = build_draft_from_facts(PROFILE, FACTS, "en", job=JOB)
        printer = RowPrinter(rows=500)
        fitted, outcome = fit_one_page(draft, JOB, lambda document: rendered_pages(document, printer))
        self.assertIs(fitted, draft)
        self.assertNotIn("plan", fitted)
        self.assertEqual((outcome["status"], outcome["pages"], outcome["left_out"], printer.calls), ("fits", 1, 0, 1))
        self.assertEqual(fit_outcome(draft, JOB, lambda document: rendered_pages(document, printer))[0], None)

    def test_the_cvs_own_lines_and_lines_put_back_are_never_left_out(self):
        draft = long_cv()
        added = draft["edits"][0]["block"]
        draft, _ = change_line(draft, "fact-pub-2", "J. Sample. Shorter paper title. 2024.",
                               expected_text=shown_text(draft, "fact-pub-2"))
        first, _ = fit_one_page(draft, JOB, self.pages)
        self.assertIn(added, shown_ids(first))
        self.assertIn("fact-pub-2", shown_ids(first))
        self.assertEqual(shown_text(first, "fact-pub-2"), "J. Sample. Shorter paper title. 2024.")
        # The user puts a left-out line back; fitting again keeps it and leaves out something else.
        cut = next(change["id"] for change in first["plan"]["changes"] if change.get("fit") and change["id"].startswith("cut:fact-"))
        put_back = set_change(first, cut)
        again, outcome = fit_one_page(put_back, JOB, self.pages)
        self.assertEqual(outcome["status"], "fitted")
        self.assertIn(cut.split(":", 1)[1], shown_ids(again))
        self.assertIn(cut, again["plan"]["undone"])
        self.assertEqual(self.pages(again), 1)
        # Undoing the job's own change still works on the fitted CV, and is not undone by fitting.
        edit = next(row for row in edits_view(again) if row["block"] == added)
        undone, changed = set_edit(again, edit["id"], True)
        self.assertTrue(changed)
        self.assertNotIn(added, shown_ids(undone))

    def test_every_job_and_the_skills_keep_a_line_and_education_stays(self):
        draft = long_cv()
        base = fit_one_page.__globals__["_with_plan"](draft)
        order, kept = candidates(base, JOB)
        experience = {"fact-x1-api", "fact-x1-cache", "fact-x1-tests", "fact-x1-docs", draft["edits"][0]["block"]}
        self.assertTrue(experience - set(order))  # the first job keeps lines
        self.assertTrue({"fact-x2-pipeline", "fact-x2-dashboard", "fact-x2-oncall"} - set(order))
        self.assertTrue({"fact-sk-lang", "fact-sk-backend", "fact-sk-data", "fact-sk-ml"} - set(order))
        self.assertEqual(kept["edited"], 1)
        # A line the job's requirements were matched to stays.
        job = copy.deepcopy(JOB)
        job["selected_requirements"][0]["fact_id"] = "fact-pub-1"
        self.assertNotIn("fact-pub-1", candidates(base, job)[0])

    def test_when_one_page_needs_too_much_nothing_changes_and_it_says_why(self):
        draft = long_cv()
        printer = RowPrinter(rows=12)
        with self.assertRaises(FitError) as caught:
            fit_one_page(draft, JOB, lambda document: rendered_pages(document, printer))
        outcome = caught.exception.outcome
        self.assertEqual(outcome["status"], "cannot_fit")
        self.assertGreater(outcome["pages"], 1)
        self.assertGreater(outcome["tried"], 0)
        self.assertEqual(outcome["kept"]["edited"], 1)
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(printer.calls, 2)  # as it is, then with every candidate left out
        self.assertEqual(fit_outcome(draft, JOB, lambda document: rendered_pages(document, printer)),
                         (None, {**outcome, "renders": 2}))
        # At most half of what the CV shows is ever left out.
        self.assertLessEqual(outcome["tried"], len(shown_ids(draft)) // 2)

    def test_the_search_is_bounded_and_only_keeps_a_cv_that_printed_on_one_page(self):
        draft = long_cv()
        calls = []

        def erratic(document):  # pages that do not fall steadily as lines go
            calls.append(document)
            shown = len(shown_ids(document))
            return 1 if shown <= 12 or shown == 16 else 2

        fitted, outcome = fit_one_page(draft, JOB, erratic, max_renders=4)
        self.assertLessEqual(len(calls), 4)
        self.assertEqual(erratic(fitted), 1)
        self.assertEqual(outcome["status"], "fitted")

    def test_a_cv_without_a_plan_gets_one_its_cuts_undo_on_their_own(self):
        draft = long_cv()
        self.assertNotIn("plan", draft)
        fitted, _ = fit_one_page(draft, JOB, self.pages)
        self.assertEqual(fitted["plan"]["provider"], "fit")
        self.assertTrue(all(change.get("fit") for change in fitted["plan"]["changes"]))
        # The model is never shown cuts: rewording or adjusting again starts without them.
        self.assertEqual(without_edits(fitted)["sections"], without_edits(draft)["sections"])

    def test_an_approved_cv_is_refused(self):
        with self.assertRaises(CVError):
            fit_one_page({**long_cv(), "approval": {}}, JOB, self.pages)


@unittest.skipUnless(_find_chrome(), "real page counts need Chrome (CHROME_PATH or on PATH)")
class RealChromeFitTests(unittest.TestCase):
    """What the final PDF is printed with decides, at the CV's normal type size."""

    def test_the_reported_two_page_cv_prints_on_one_page(self):
        draft = long_cv()
        before = rendered_pages(draft)
        self.assertEqual(before, 2)
        fitted, outcome = fit_one_page(draft, JOB, rendered_pages)
        self.assertEqual((outcome["status"], outcome["pages"]), ("fitted", 1))
        self.assertEqual(rendered_pages(fitted), 1)
        self.assertIn(draft["edits"][0]["block"], shown_ids(fitted))
        self.assertLessEqual(outcome["renders"], MAX_RENDERS)
        short = build_draft_from_facts(PROFILE, {key: FACTS[key] for key in FACTS}, "en", job=JOB)
        short["sections"] = short["sections"][:2]
        self.assertEqual(fit_one_page(short, JOB, rendered_pages)[1]["status"], "fits")


if __name__ == "__main__":
    unittest.main()
