import copy
import json
import tempfile
import unittest

from cv import build_draft
from facts import add_fact, list_facts, revise_fact
from gaps import SUGGEST_RULES, accept_gap, decline_gap, find_gaps
from matching import MATCH_RULES
from requirement_flow import apply_requirement_decisions, propose_requirements
from test_cv import FACTS, PROFILE, make_store, stand_ins_for, swap_ids


JD = """Requirements:
- Experience with Python
- Hands-on Docker and Kubernetes
- 3+ years of backend experience
- Writing integration tests for services"""


def decided_job():
    data = {"jd": {"text": JD, "title": "Backend Intern", "source": None,
                   "captured_at": "2026-09-26T00:00:00+00:00"}, "facts": [], "selected_requirements": []}
    proposed = propose_requirements(data)
    return apply_requirement_decisions(proposed, {item["id"] for item in proposed["requirement_candidates"]}, set())


class FakeDeepSeek:
    """Answers matching (only the Python requirement is covered) and gap suggestions."""

    def __init__(self, suggestions):
        self.suggestions = suggestions
        self.sent = []

    def __call__(self, messages, model, effort):
        request = json.loads(messages[-1]["content"])
        self.sent.append(messages[-1]["content"])
        if messages[0]["content"] == MATCH_RULES:
            python = stand_ins_for(request["facts"])["fact-skills-languages"]
            matches = [{"requirement": item["id"], "facts": [python] if "Python" in item["text"] else []}
                       for item in request["requirements"]]
            return {"model": "deepseek-flash", "content": {"matches": matches}, "usage": {}}
        assert messages[0]["content"] == SUGGEST_RULES
        ids = {item["text"]: item["id"] for item in request["gaps"]}
        return {"model": "deepseek-flash", "usage": {}, "content": {"suggestions": resume_ids(self.suggestions(ids), request)}}


def resume_ids(value, request):
    """``value`` naming CV lines by fact ID, turned into the IDs the request gave them."""
    lines = [line for section in request["resume"] for entry in section["entries"] for line in entry["lines"]]
    return swap_ids(value, stand_ins_for(lines))


def suggestions(ids):
    return [
        {"requirement": ids["Hands-on Docker and Kubernetes"], "kind": "skill",
         "line": "fact-skills-languages", "items": ["Docker", "Kubernetes", "Python"]},
        {"requirement": ids["3+ years of backend experience"], "kind": "none"},
        {"requirement": ids["Writing integration tests for services"], "kind": "bullet", "entry": "s1e0",
         "text": "Wrote integration tests for internal services.", "tags": ["integration tests", "Go"]},
    ]


class GapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = make_store(self.directory.name)
        self.draft = build_draft(PROFILE, self.database, "en")

    def tearDown(self):
        self.directory.cleanup()

    def gaps(self, suggest=suggestions):
        chat = FakeDeepSeek(suggest)
        return find_gaps(decided_job(), self.draft, self.database, chat), chat

    def test_only_uncovered_requirements_are_listed_each_with_an_honest_suggestion(self):
        gaps, chat = self.gaps()
        listed = {item["text"]: item["suggestion"] for item in gaps["gaps"]}
        self.assertEqual(set(listed), {"Hands-on Docker and Kubernetes", "3+ years of backend experience",
                                       "Writing integration tests for services"})
        self.assertEqual(listed["Hands-on Docker and Kubernetes"]["items"], ["Docker", "Kubernetes"])
        self.assertEqual(listed["Hands-on Docker and Kubernetes"]["where"], "Languages: Python, Java")
        self.assertIsNone(listed["3+ years of backend experience"])
        bullet = listed["Writing integration tests for services"]
        self.assertEqual((bullet["kind"], bullet["where"], bullet["tags"]),
                         ("bullet", "Example Corp", ["integration tests"]))
        for private in ("Alex Example", "alex@example.com", "Example Corp", "Example University"):
            self.assertFalse(any(private in sent for sent in chat.sent))
        self.assertEqual({item["strength"] for item in gaps["gaps"]}, {"required"})
        self.assertEqual({item["strength"] for item in json.loads(chat.sent[-1])["gaps"]}, {"required"})
        for fact in FACTS:
            self.assertNotIn(fact["id"], chat.sent[-1])
        self.assertEqual(listed["Hands-on Docker and Kubernetes"]["fact_id"], "fact-skills-languages")

    def test_suggestions_that_claim_numbers_or_leadership_are_dropped(self):
        def overclaiming(ids):
            requirement = ids["Writing integration tests for services"]
            return [{"requirement": requirement, "kind": "bullet", "entry": "s1e0",
                     "text": "Led a team of 5 writing integration tests.", "tags": []}]
        gaps, _ = self.gaps(overclaiming)
        self.assertEqual({item["text"]: item["suggestion"] for item in gaps["gaps"]}["Writing integration tests for services"], None)

    def test_suggestions_repeating_what_the_cv_already_says_are_dropped(self):
        def repeating(ids):
            return [
                {"requirement": ids["Writing integration tests for services"], "kind": "bullet", "entry": "s1e0",
                 "text": "Built REST APIs for an internal tool.", "tags": ["REST APIs"]},
                {"requirement": ids["Hands-on Docker and Kubernetes"], "kind": "skill",
                 "line": "fact-skills-languages", "items": ["java", "REST APIs"]},
            ]
        gaps, _ = self.gaps(repeating)
        suggestions = {item["text"]: item["suggestion"] for item in gaps["gaps"]}
        self.assertIsNone(suggestions["Writing integration tests for services"])
        self.assertIsNone(suggestions["Hands-on Docker and Kubernetes"])

    def test_accepting_a_skill_confirms_a_new_version_of_that_skills_line(self):
        gaps, _ = self.gaps()
        requirement = next(item["requirement_id"] for item in gaps["gaps"] if "Docker" in item["text"])
        updated, profile = accept_gap(gaps, requirement, self.database, PROFILE)
        skills = next(fact for fact in list_facts(self.database) if fact["id"] == "fact-skills-languages")
        self.assertEqual((skills["text"], skills["version"], skills["status"]),
                         ("Languages: Python, Java, Docker, Kubernetes", 2, "confirmed"))
        self.assertIsNone(profile)  # the same fact ID is already on the CV
        self.assertEqual(next(item for item in updated["gaps"] if item["requirement_id"] == requirement)["status"], "added")

    def test_accepting_a_bullet_adds_a_confirmed_fact_to_that_entry(self):
        gaps, _ = self.gaps()
        requirement = next(item["requirement_id"] for item in gaps["gaps"] if "integration" in item["text"])
        original = copy.deepcopy(PROFILE)
        _, profile = accept_gap(gaps, requirement, self.database, PROFILE)
        added = [fact for fact in list_facts(self.database) if fact["text"] == "Wrote integration tests for internal services."]
        self.assertEqual([(fact["status"], fact["fact_type"], fact["tags"]) for fact in added],
                         [("confirmed", "experience", ["integration tests"])])
        self.assertEqual(profile["sections"][1]["entries"][0]["facts"][-1], added[0]["id"])
        self.assertEqual(PROFILE, original)  # the caller's profile is not changed in place

    def test_a_new_line_goes_under_its_own_employer_even_after_the_layout_changes(self):
        gaps, _ = self.gaps()
        requirement = next(item["requirement_id"] for item in gaps["gaps"] if "integration" in item["text"])
        moved = copy.deepcopy(PROFILE)
        moved["sections"][1]["entries"].insert(0, {"title": "Other Co", "dates": "2024", "facts": []})
        _, profile = accept_gap(gaps, requirement, self.database, moved)
        entries = profile["sections"][1]["entries"]
        self.assertEqual(entries[0]["facts"], [])
        self.assertEqual(len(entries[1]["facts"]), 3)  # Example Corp, now second

    def test_a_suggestion_whose_entry_or_line_changed_since_is_refused(self):
        gaps, _ = self.gaps()
        bullet = next(item["requirement_id"] for item in gaps["gaps"] if "integration" in item["text"])
        skill = next(item["requirement_id"] for item in gaps["gaps"] if "Docker" in item["text"])
        renamed = copy.deepcopy(PROFILE)
        renamed["sections"][1]["entries"][0]["title"] = "Example Corporation"
        with self.assertRaisesRegex(ValueError, "重新检查缺口"):
            accept_gap(gaps, bullet, self.database, renamed)
        twice = copy.deepcopy(PROFILE)  # two entries that look the same: no way to tell which
        twice["sections"][1]["entries"].append({**twice["sections"][1]["entries"][0], "facts": []})
        with self.assertRaisesRegex(ValueError, "重新检查缺口"):
            accept_gap(gaps, bullet, self.database, twice)
        revise_fact(self.database, "fact-skills-languages", text="Languages: Python, Java, Go")
        with self.assertRaisesRegex(ValueError, "重新检查缺口"):
            accept_gap(gaps, skill, self.database, PROFILE)
        self.assertEqual([fact["text"] for fact in list_facts(self.database)
                          if fact["text"] == "Wrote integration tests for internal services."], [])

    def test_accepting_again_after_an_interrupted_save_adds_nothing_twice(self):
        # Each step can be repeated: if saving stops before the gap is marked added, the gap
        # stays open and accepting it again finishes the job without duplicates.
        gaps, _ = self.gaps()
        bullet = next(item["requirement_id"] for item in gaps["gaps"] if "integration" in item["text"])
        skill = next(item["requirement_id"] for item in gaps["gaps"] if "Docker" in item["text"])
        _, saved = accept_gap(gaps, bullet, self.database, PROFILE)
        accept_gap(gaps, skill, self.database, saved)
        updated, again = accept_gap(gaps, bullet, self.database, saved)
        updated, _ = accept_gap(updated, skill, self.database, saved)
        texts = [fact["text"] for fact in list_facts(self.database)]
        skills = next(fact for fact in list_facts(self.database) if fact["id"] == "fact-skills-languages")
        self.assertIsNone(again)  # the saved profile already lists the line
        self.assertEqual(texts.count("Wrote integration tests for internal services."), 1)
        self.assertEqual((skills["version"], skills["status"]), (2, "confirmed"))
        self.assertEqual({item["requirement_id"]: item["status"] for item in updated["gaps"]}[skill], "added")

    def test_accepting_never_confirms_a_version_the_user_has_not_seen(self):
        # Found in review by Codex: a pending version with the suggested text may carry other,
        # unreviewed tags, so it is left for the Facts page instead of being confirmed here.
        gaps, _ = self.gaps()
        skill = next(item["requirement_id"] for item in gaps["gaps"] if "Docker" in item["text"])
        revise_fact(self.database, "fact-skills-languages", text="Languages: Python, Java, Docker, Kubernetes",
                    tags=["Python", "Java", "Docker", "Kubernetes", "AWS"])
        with self.assertRaisesRegex(ValueError, "Facts"):
            accept_gap(gaps, skill, self.database, PROFILE)
        skills = next(fact for fact in list_facts(self.database) if fact["id"] == "fact-skills-languages")
        self.assertEqual(skills["status"], "pending")

    def test_a_line_found_under_another_entry_is_not_counted_as_added(self):
        # Found in review by Codex: after an interrupted save the line was moved elsewhere, so
        # the entry the suggestion was for still lacks it.
        gaps, _ = self.gaps()
        bullet = next(item["requirement_id"] for item in gaps["gaps"] if "integration" in item["text"])
        _, saved = accept_gap(gaps, bullet, self.database, PROFILE)
        line = saved["sections"][1]["entries"][0]["facts"].pop()
        saved["sections"][1]["entries"].append({"title": "Other Co", "dates": "2024", "facts": [line]})
        with self.assertRaisesRegex(ValueError, "重新检查缺口"):
            accept_gap(gaps, bullet, self.database, saved)

    def test_a_refused_acceptance_changes_nothing(self):
        # Found in review by Codex: the pending fact was confirmed before the placement check
        # refused the acceptance.
        gaps, _ = self.gaps()
        bullet = next(item["requirement_id"] for item in gaps["gaps"] if "integration" in item["text"])
        pending, _ = add_fact(self.database, "Wrote integration tests for internal services.", "experience", ["integration tests"])
        moved = copy.deepcopy(PROFILE)
        moved["sections"][1]["entries"].append({"title": "Other Co", "dates": "2024", "facts": [pending["id"]]})
        with self.assertRaisesRegex(ValueError, "重新检查缺口"):
            accept_gap(gaps, bullet, self.database, moved)
        self.assertEqual(next(fact for fact in list_facts(self.database) if fact["id"] == pending["id"])["status"], "pending")

    def test_a_declined_or_unsuggested_gap_stays_off_the_cv(self):
        gaps, _ = self.gaps()
        years = next(item["requirement_id"] for item in gaps["gaps"] if "years" in item["text"])
        docker = next(item["requirement_id"] for item in gaps["gaps"] if "Docker" in item["text"])
        declined = decline_gap(gaps, docker)
        self.assertEqual(next(item for item in declined["gaps"] if item["requirement_id"] == docker)["status"], "declined")
        with self.assertRaisesRegex(ValueError, "没有可添加的建议"):
            accept_gap(gaps, years, self.database, PROFILE)
        with self.assertRaisesRegex(ValueError, "已标记为不属实"):
            accept_gap(declined, docker, self.database, PROFILE)


if __name__ == "__main__":
    unittest.main()
