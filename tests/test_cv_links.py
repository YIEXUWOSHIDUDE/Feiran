"""Links read from a CV PDF stay with the line they were on: two lines of one entry that both
say "GitHub" keep their own URLs through import, the saved profile, the draft and the printed
HTML; a reworded line that lost its linked phrase keeps the link as a label on the same line;
an older profile with entry-level links renders as before. Synthetic CV text, no model."""
import tempfile
import unittest
from pathlib import Path

from feiran.cv import _build_draft, render_html
from feiran.cv_import import build_profile, structure_cv
from tests.test_cv_import import FakeStructurer

ALPHA, BETA, PAGE = "https://example.com/alpha", "https://example.com/beta", "https://example.com/page"
LINES = [["ALEX EXAMPLE"], ["alex@example.com"], ["PROJECTS"], ["Search Tools", "Python"],
         ["• Alpha indexer: code on GitHub."], ["• Beta crawler: code on GitHub, docs on the project page."]]
PDF_LINKS = [{"url": ALPHA, "pieces": ["GitHub"], "context": "• Alpha indexer: code on GitHub."},
             {"url": BETA, "pieces": ["GitHub"], "context": "• Beta crawler: code on GitHub, docs on the project page."},
             {"url": PAGE, "pieces": ["project page"],
              "context": "• Beta crawler: code on GitHub, docs on the project page."}]
ANSWER = {"sections": [{"kind": "projects", "heading": 3, "entries": [
    {"title": [4, 1], "location": [4, 2], "facts": [{"lines": [5], "tags": []}, {"lines": [6], "tags": []}]}]}]}


def imported():
    """The profile and fact list saved from the synthetic PDF, and its first entry's fact IDs."""
    proposal = structure_cv(LINES, FakeStructurer(ANSWER), links=PDF_LINKS)
    with tempfile.TemporaryDirectory() as directory:
        profile, items, _ = build_profile(proposal, proposal["private"], Path(directory) / "facts.db")
    profile["name"] = "Alex Example"
    return proposal, profile, {item["id"]: {**item, "version": 1} for item in items}


def links_in(html):
    import re
    return [link for link in re.findall(r'<a href="([^"]+)">([^<]+)</a>', html) if link[0].startswith("https://example.com/")]


class SameLabelTests(unittest.TestCase):
    def test_two_lines_of_one_entry_keep_their_own_github(self):
        proposal, profile, facts = imported()
        entry = proposal["sections"][0]["entries"][0]
        self.assertEqual([fact.get("links") for fact in entry["facts"]],
                         [{"GitHub": ALPHA}, {"GitHub": BETA, "project page": PAGE}])
        self.assertNotIn("GitHub", entry["links"])  # ambiguous for the entry as a whole
        self.assertEqual(entry["links"], {"project page": PAGE})
        saved = profile["sections"][0]["entries"][0]
        alpha_id, beta_id = saved["facts"]
        self.assertEqual(saved["fact_links"], {alpha_id: {"GitHub": ALPHA}, beta_id: {"GitHub": BETA, "project page": PAGE}})
        html = render_html(_build_draft(profile, lambda ids: facts), final=True)
        self.assertEqual(links_in(html), [(ALPHA, "GitHub"), (BETA, "GitHub"), (PAGE, "project page")])

    def test_a_reworded_line_keeps_its_own_link_and_never_another_lines(self):
        _proposal, profile, facts = imported()
        draft = _build_draft(profile, lambda ids: facts)
        alpha, beta = draft["sections"][0]["entries"][0]["lines"]
        beta["source_text"], beta["text"] = beta["text"], "Built a polite web crawler in Go."
        html = render_html(draft, final=True)
        self.assertIn('Built a polite web crawler in Go. <span class="source-links">('
                      f'<a href="{BETA}">GitHub</a> | <a href="{PAGE}">project page</a>)</span>', html)
        self.assertEqual(html.count(f'href="{ALPHA}"'), 1)  # only on the alpha line
        # A line left out (cut) shows none of its links anywhere.
        draft["sections"][0]["entries"][0]["lines"] = [beta]
        html = render_html(draft, final=True)
        self.assertNotIn(ALPHA, html)

    def test_an_older_profile_with_entry_links_renders_as_before(self):
        _proposal, profile, facts = imported()
        entry = profile["sections"][0]["entries"][0]
        entry.pop("fact_links")
        entry["links"] = {"GitHub": ALPHA}
        html = render_html(_build_draft(profile, lambda ids: facts), final=True)
        self.assertEqual([url for url, label in links_in(html) if label == "GitHub"], [ALPHA, ALPHA])
        # A stale fact ID (the fact moved out of the entry) links nothing and is no error.
        entry["fact_links"] = {"fact-not-here": {"GitHub": BETA}}
        self.assertNotIn(BETA, render_html(_build_draft(profile, lambda ids: facts), final=True))

    def test_a_line_without_a_link_never_borrows_another_lines_and_a_hidden_lines_link_stays_hidden(self):
        # Found in review by Codex: the second "GitHub" took the first line's URL, even with that line left out.
        lines = [["ALEX EXAMPLE"], ["alex@example.com"], ["PROJECTS"], ["Tools", "Python"],
                 ["• See GitHub for API."], ["• Uses GitHub Actions."]]
        links = [{"url": "https://example.com/api", "pieces": ["GitHub"], "context": "• See GitHub for API."}]
        answer = {"sections": [{"kind": "projects", "heading": 3, "entries": [
            {"title": [4, 1], "location": [4, 2], "facts": [{"lines": [5], "tags": []}, {"lines": [6], "tags": []}]}]}]}
        proposal = structure_cv(lines, FakeStructurer(answer), links=links)
        with tempfile.TemporaryDirectory() as directory:
            profile, items, _ = build_profile(proposal, proposal["private"], Path(directory) / "facts.db")
        profile["name"] = "Alex Example"
        facts = {item["id"]: {**item, "version": 1} for item in items}
        draft = _build_draft(profile, lambda ids: facts)
        html = render_html(draft, final=True)
        self.assertEqual(html.count('href="https://example.com/api"'), 1)
        self.assertIn("Uses GitHub Actions.", html)
        entry = draft["sections"][0]["entries"][0]
        entry["lines"] = entry["lines"][1:]  # the linked line left out, as a layout or fit does
        self.assertNotIn("https://example.com/api", render_html(draft, final=True))

    def test_an_unsafe_url_in_a_lines_links_is_refused(self):
        from feiran.cv import CVError
        _proposal, profile, facts = imported()
        entry = profile["sections"][0]["entries"][0]
        entry["fact_links"][entry["facts"][0]] = {"GitHub": "javascript:alert(1)"}
        with self.assertRaises(CVError):
            _build_draft(profile, lambda ids: facts)


if __name__ == "__main__":
    unittest.main()
