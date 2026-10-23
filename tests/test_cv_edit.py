import copy
import tempfile
import unittest

from feiran.cv import CVError, approve_draft, build_draft, content_fingerprint, tailor_draft, verify_draft
from feiran.cv_edit import (LOCAL_PREFIX, EditError, StaleEdit, add_line, adopt, adopted_sources, adoptions, carry_over,
                     change_line, edits_view, lines_view, mark_saved, profile_with_line, save_target, set_edit,
                     shown_text, without_edits)
from feiran.cv_layout import shown_sections
from feiran.cv_plan import plan_draft, set_change
from feiran.gaps import _entry_key
from tests.test_cv import PROFILE, FakeChat, make_store
from tests.test_cv_plan import CUT_API, FakePlanner
from tests.test_gaps import decided_job

SKILLS = "fact-skills-languages"
TESTS = "fact-intern-tests"
API = "fact-intern-api"
NEW_LINE = "Deployed the billing service on Kubernetes with zero-downtime releases."


def shown(draft):
    """What the CV shows, section by section: each entry's title and its lines' words."""
    return [(section["kind"], [(entry.get("title"), [line["text"] for line in entry["lines"]])
                               for entry in section["entries"]])
            for section in shown_sections(draft)]


def experience_key(draft):
    section = next(item for item in draft["sections"] if item["kind"] == "experience")
    return _entry_key("experience", section["entries"][0])


def bullet(draft, text=NEW_LINE):
    return {"kind": "bullet", "entry_key": experience_key(draft), "text": text, "tags": ["Kubernetes"]}


class LocalEditTests(unittest.TestCase):
    def test_redo_is_offered_only_after_its_previous_wording_is_restored(self):
        first, _ = change_line(self.draft, TESTS, "First edit.")
        second, _ = change_line(first, TESTS, "Second edit.")
        one, two = [edit["id"] for edit in second["edits"]]
        undone, _ = set_edit(second, two, True)
        undone, _ = set_edit(undone, one, True)
        rows = {row["id"]: row for row in edits_view(undone)}
        self.assertTrue(rows[one]["can_redo"])
        self.assertFalse(rows[two]["can_redo"])
        restored, _ = set_edit(undone, one, False)
        self.assertTrue(next(row for row in edits_view(restored) if row["id"] == two)["can_redo"])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = make_store(self.directory.name)
        self.draft = build_draft(PROFILE, self.database, "en")
        self.planned = plan_draft(self.draft, decided_job(), chat=FakePlanner(CUT_API))

    def tearDown(self):
        self.directory.cleanup()

    def test_an_adopted_line_goes_on_word_for_word_and_nothing_else_moves(self):
        before = shown(self.planned)
        edited, changed = adopt(self.planned, bullet(self.planned), "req-1", origin="suggestion", source="key-1")
        self.assertTrue(changed)
        block = edited["edits"][0]["block"]
        self.assertTrue(block.startswith(LOCAL_PREFIX))
        expected = copy.deepcopy(before)
        experience = next(entries for kind, entries in expected if kind == "experience")
        experience[0][1].append(NEW_LINE)
        self.assertEqual(shown(edited), expected)  # same order, same cuts, the new line last in its entry
        self.assertEqual(edited["plan"]["changes"], self.planned["plan"]["changes"])
        self.assertEqual(edited["plan"]["undone"], self.planned["plan"]["undone"])
        self.assertEqual(edited["facts"], self.planned["facts"])  # no saved entry is made or named
        self.assertEqual(adoptions(edited)["req-1"][0]["text"], NEW_LINE)
        self.assertEqual(adopted_sources(edited), {"key-1"})
        verify_draft(edited, self.database)  # a CV with a line of its own still verifies and approves
        self.assertNotEqual(content_fingerprint(edited), content_fingerprint(self.planned))
        approve_draft(edited, self.database)

    def test_skills_go_onto_the_skills_line_as_the_cv_shows_it(self):
        tailored = tailor_draft(self.draft, self.database, chat=FakeChat({SKILLS: "Languages: Python 3, Java 17"}))
        suggestion = {"kind": "skill", "fact_id": SKILLS, "items": ["Docker", "python 3", "Kubernetes"],
                      "where": "Languages: Python, Java", "new_text": "Languages: Python, Java, Docker, Kubernetes"}
        edited, _ = adopt(tailored, suggestion, "req-2", origin="suggestion")
        self.assertEqual(shown_text(edited, SKILLS), "Languages: Python 3, Java 17, Docker, Kubernetes")
        self.assertEqual(edited["edits"][0]["tags"], ["Docker", "Kubernetes"])
        undone, _ = set_edit(edited, edited["edits"][0]["id"], True)
        self.assertEqual(shown_text(undone, SKILLS), "Languages: Python 3, Java 17")  # the reworded line again

    def test_one_requirement_can_hold_several_lines_each_undone_on_its_own(self):
        first, _ = adopt(self.planned, bullet(self.planned), "req-1", origin="suggestion")
        second, _ = add_line(first, experience_key(first), "Wrote integration tests for the billing service.",
                             requirements=["req-1"])
        self.assertEqual([row["text"] for row in adoptions(second)["req-1"]],
                         ["Wrote integration tests for the billing service.", NEW_LINE])
        blocks = [edit["block"] for edit in second["edits"]]
        undone, _ = set_edit(second, second["edits"][0]["id"], True)
        self.assertEqual([row["text"] for row in adoptions(undone)["req-1"]],
                         ["Wrote integration tests for the billing service."])
        self.assertNotIn(NEW_LINE, str(shown(undone)))
        redone, _ = set_edit(undone, undone["edits"][0]["id"], False)
        self.assertEqual(shown(redone), shown(second))
        self.assertEqual([edit["block"] for edit in redone["edits"]], blocks)  # the same block IDs again

    def test_block_ids_stay_put_through_reordering_cuts_undos_and_edits(self):
        edited, _ = adopt(self.planned, bullet(self.planned), "req-1", origin="suggestion")
        block = edited["edits"][0]["block"]
        restored = set_change(set_change(edited, "cut:fact-intern-api", True), "order:sections", True)
        self.assertEqual(next(entries for kind, entries in shown(restored) if kind == "experience")[0][1],
                         ["Built REST APIs for an internal tool.", "Wrote unit tests for billing code.", NEW_LINE])
        reworded, _ = change_line(restored, block, "Deployed billing on Kubernetes.", expected_text=NEW_LINE)
        self.assertEqual(shown_text(reworded, block), "Deployed billing on Kubernetes.")
        self.assertEqual({line["block"] for line in lines_view(reworded)},
                         {line["block"] for line in lines_view(restored)})
        # Still the requirement's line after the user's own edit, which is undone first.
        self.assertEqual(adoptions(reworded)["req-1"][0]["block"], block)
        with self.assertRaises(StaleEdit):
            set_edit(reworded, reworded["edits"][0]["id"], True)
        back, _ = set_edit(reworded, reworded["edits"][1]["id"], True)
        self.assertEqual(shown_text(back, block), NEW_LINE)

    def test_an_old_page_cannot_overwrite_a_line_that_changed_since(self):
        edited, _ = change_line(self.planned, TESTS, "Wrote 200 unit tests for billing code.",
                                expected_text="Wrote unit tests for billing code.")
        with self.assertRaises(StaleEdit):
            change_line(edited, TESTS, "Tested billing code.", expected_text="Wrote unit tests for billing code.")
        with self.assertRaises(StaleEdit):  # a line that is gone, an entry that is gone
            change_line(edited, "fact-cv-0000000000000000", "Anything.")
        with self.assertRaises(StaleEdit):
            add_line(edited, '["experience", "Gone Corp", null, null, null]', "Anything.")

    def test_the_same_request_again_changes_nothing(self):
        once, changed = add_line(self.planned, experience_key(self.planned), NEW_LINE, request="key-1")
        again, changed_again = add_line(once, experience_key(once), NEW_LINE + " Again.", request="key-1")
        self.assertEqual((changed, changed_again), (True, False))
        self.assertIs(again, once)
        undone, _ = set_edit(once, once["edits"][0]["id"], True, request="key-2")
        self.assertEqual(add_line(undone, experience_key(undone), NEW_LINE, request="key-1"), (undone, False))
        self.assertEqual(set_edit(undone, undone["edits"][0]["id"], False, request="key-2"), (undone, False))
        # A second click with the same words names the requirement, never adds a second line.
        clicked, changed = add_line(once, experience_key(once), NEW_LINE, requirements=["req-9"])
        self.assertTrue(changed)
        self.assertEqual(len(clicked["edits"]), 1)
        self.assertEqual(add_line(clicked, experience_key(clicked), NEW_LINE, requirements=["req-9"]), (clicked, False))

    def test_hidden_or_unsuitable_places_are_refused_with_nothing_changed(self):
        cut = plan_draft(self.draft, decided_job(), chat=FakePlanner(
            {"sections": ["education", "skills"], "entries": []}))
        with self.assertRaisesRegex(EditError, "hidden"):
            add_line(cut, experience_key(cut), NEW_LINE)
        education = _entry_key("education", self.draft["sections"][0]["entries"][0])
        with self.assertRaisesRegex(EditError, "experience or project"):
            add_line(self.draft, education, NEW_LINE)
        with self.assertRaisesRegex(EditError, "already shows"):
            add_line(self.draft, experience_key(self.draft), "Built REST APIs for an internal tool.")
        for text in ("", "x" * 301, "Led [[team size]] engineers."):
            with self.assertRaises(EditError):
                add_line(self.draft, experience_key(self.draft), text)

    def test_changes_are_applied_again_after_the_cv_is_reworded_and_adjusted_again(self):
        edited, _ = adopt(self.planned, bullet(self.planned), "req-1", origin="suggestion")
        edited, _ = change_line(edited, TESTS, "Wrote 200 unit tests for billing code.")
        edited, _ = add_line(edited, experience_key(edited), "A line the user took back.")
        edited, _ = set_edit(edited, edited["edits"][2]["id"], True)
        base = without_edits(edited)
        self.assertEqual(shown(base), shown(self.planned))
        self.assertNotIn("edits", base)
        retailored = tailor_draft(self.draft, self.database, chat=FakeChat({TESTS: "Tested billing code end to end."}))
        replanned = plan_draft(retailored, decided_job(), chat=FakePlanner(CUT_API))
        carried = carry_over(edited, replanned)
        experience = next(entries for kind, entries in shown(carried) if kind == "experience")[0][1]
        self.assertEqual(experience, ["Wrote 200 unit tests for billing code.", NEW_LINE])
        self.assertEqual([edit["text"] for edit in carried["edits"]],
                         [NEW_LINE, "Wrote 200 unit tests for billing code."])  # the undone one is left behind
        undone, _ = set_edit(carried, carried["edits"][1]["id"], True)
        self.assertEqual(shown_text(undone, TESTS), "Tested billing code end to end.")  # what the model wrote now
        # An entry the new CV no longer has: the change is kept, not placed, and can be set aside.
        renamed = copy.deepcopy(replanned)
        renamed["sections"][1]["entries"][0]["title"] = "Example Corporation"
        lost = carry_over(edited, renamed)
        rows = {row["text"]: row for row in edits_view(lost)}
        self.assertFalse(rows[NEW_LINE]["placed"])
        self.assertTrue(rows["Wrote 200 unit tests for billing code."]["placed"])
        self.assertNotIn(NEW_LINE, str(shown(lost)))
        aside, _ = set_edit(lost, rows[NEW_LINE]["id"], True)
        self.assertNotIn(NEW_LINE, [edit["text"] for edit in carry_over(aside, replanned)["edits"]])

    def test_a_cv_without_changes_of_its_own_reads_as_before(self):
        self.assertIs(carry_over(self.planned, self.draft), self.draft)
        self.assertIs(carry_over(None, self.draft), self.draft)
        self.assertEqual(edits_view(self.planned), [])
        self.assertEqual(adoptions(self.planned), {})
        self.assertEqual(without_edits(self.planned), self.planned)
        self.assertEqual([line["block"] for line in lines_view(self.planned)],
                         ["fact-edu-coursework", SKILLS, TESTS])

    def test_saving_a_line_makes_it_a_saved_entry_with_the_same_block_id(self):
        edited, _ = adopt(self.planned, bullet(self.planned), "req-1", origin="suggestion")
        block = edited["edits"][0]["block"]
        target = save_target(edited, block)
        self.assertEqual({key: target[key] for key in ("text", "new", "fact_type", "tags")},
                         {"text": NEW_LINE, "new": True, "fact_type": "experience", "tags": ["Kubernetes"]})
        profile = profile_with_line(PROFILE, "en", target["entry_key"], block)
        self.assertEqual(profile["sections"][1]["entries"][0]["facts"], [API, TESTS, block])
        self.assertEqual(PROFILE["sections"][1]["entries"][0]["facts"], [API, TESTS])  # a copy
        saved = mark_saved(edited, block, 1, profile_sha256="0" * 64)
        self.assertEqual(saved["facts"][-1], {"id": block, "version": 1})
        self.assertEqual(saved["profile_sha256"], "0" * 64)
        self.assertFalse(any(line.get("local") for line in lines_view(saved)))
        self.assertEqual(adoptions(saved)["req-1"][0]["saved"], True)
        with self.assertRaises(StaleEdit):
            save_target(saved, block)
        with self.assertRaisesRegex(EditError, "saved to your profile"):
            set_edit(saved, saved["edits"][0]["id"], True)
        self.assertEqual(shown(without_edits(saved)), shown(saved))  # a saved entry is part of the base now
        with self.assertRaises(StaleEdit):
            save_target(self.planned, TESTS)  # nothing of this CV's own to save
        with self.assertRaisesRegex(EditError, "not in your profile"):
            profile_with_line(PROFILE, "en", '["experience", "Gone Corp", null, null, null]', block)

    def test_a_line_of_its_own_is_checked_for_its_format_only(self):
        edited, _ = add_line(self.draft, experience_key(self.draft), NEW_LINE)
        verify_draft(edited, self.database)
        block = edited["edits"][0]["block"]
        for change in ({"fact_version": 1}, {"text": "Led [[n]] people."}, {"text": "x" * 301}):
            broken = copy.deepcopy(edited)
            line = next(line for section in broken["sections"] for entry in section["entries"]
                        for line in entry["lines"] if line["fact_id"] == block)
            line.update(change)
            with self.subTest(change), self.assertRaises(CVError):
                verify_draft(broken, self.database)
        claimed = copy.deepcopy(edited)
        claimed["facts"].append({"id": block, "version": 0})
        with self.assertRaises(CVError):
            verify_draft(claimed, self.database)  # it never claims to be a saved entry


if __name__ == "__main__":
    unittest.main()
