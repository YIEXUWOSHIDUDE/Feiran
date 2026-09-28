import copy
import json
import tempfile
import unittest

from cv import build_draft, tailor_draft
from cv_plan import plan_draft, set_change
from deepseek_client import DeepSeekError
from facts import add_fact, confirm_fact, list_facts, revise_fact
from gaps import EVIDENCE_RULES, SUGGEST_RULES, accept_gap, coverage, decline_gap, find_gaps, places, write_line
from requirement_flow import apply_requirement_decisions, propose_requirements
from test_cv import FACTS, PROFILE, FakeChat, make_store, stand_ins_for, swap_ids
from test_cv_plan import FakePlanner


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


def sent_lines(request):
    """Every line an evidence request gave an ID, by its text: each entry's role and lines, and
    the facts not on the CV."""
    lines = [line for entry in request["resume"] for line in [entry.get("role"), *entry["lines"]] if line]
    return {line["text"]: line["id"] for line in lines + request["other_facts"]}


def evidence(request):
    """Python is shown by the skills line; the years only in part, by the internship and its
    dates; integration tests only by a related line; nothing is about Docker."""
    lines = sent_lines(request)
    ids = {item["text"]: item["id"] for item in request["requirements"]}
    return [
        {"id": ids["Experience with Python"], "verdict": "supported", "sets": [[lines["Languages: Python, Java"]]]},
        {"id": ids["Hands-on Docker and Kubernetes"], "verdict": "none"},
        {"id": ids["3+ years of backend experience"], "verdict": "related",
         "lines": [lines["Software Intern (2025)"]], "missing": "3+ years"},
        {"id": ids["Writing integration tests for services"], "verdict": "related",
         "lines": [lines["Wrote unit tests for billing code."]], "missing": "integration tests"},
    ]


class FakeDeepSeek:
    """Answers the evidence check (``evidence``, or fails with ``error``) and gap suggestions."""

    def __init__(self, suggestions, evidence=evidence, error=None):
        self.suggestions = suggestions
        self.evidence = evidence
        self.error = error
        self.sent = []

    def __call__(self, messages, model, effort):
        request = json.loads(messages[-1]["content"])
        self.sent.append(messages[-1]["content"])
        if messages[0]["content"] == EVIDENCE_RULES:
            if self.error:
                raise self.error
            return {"model": "deepseek-flash", "content": {"requirements": self.evidence(request)}, "usage": {}}
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

    def test_each_requirement_gets_a_verdict_and_the_lines_behind_it(self):
        gaps, chat = self.gaps()
        items = gaps["items"]
        found = {item["text"]: item for item in gaps["requirements"]}
        self.assertEqual({text: item["evidence"] for text, item in found.items()}, {
            "Experience with Python": "supported", "Hands-on Docker and Kubernetes": "none",
            "3+ years of backend experience": "related", "Writing integration tests for services": "related"})
        self.assertEqual([[items[ref]["text"] for ref in group] for group in found["Experience with Python"]["sets"]],
                         [["Languages: Python, Java"]])
        years = found["3+ years of backend experience"]
        self.assertEqual(([items[ref]["text"] for ref in years["related"]], years["missing"]),
                         (["Software Intern (2025)"], "3+ years"))
        # Sent: every confirmed fact and each entry's role or degree with its dates, but no
        # names, schools, employers or fact IDs.
        sent = json.loads(chat.sent[0])
        self.assertEqual(sorted(sent_lines(sent)), sorted(
            [fact["text"] for fact in FACTS] + ["M.S. in Computer Science (2026 – 2028)", "Software Intern (2025)"]))
        self.assertEqual(sent["other_facts"], [])  # every confirmed fact is on this CV
        for private in ("Alex Example", "Example Corp", "Example University", "Los Angeles", *(fact["id"] for fact in FACTS)):
            self.assertNotIn(private, chat.sent[0])

    def test_what_nothing_shows_gets_an_honest_suggestion(self):
        gaps, chat = self.gaps()
        listed = {item["text"]: item["suggestion"] for item in gaps["requirements"]}
        self.assertIsNone(listed["Experience with Python"])  # shown, so nothing to add
        self.assertEqual({item["text"] for item in json.loads(chat.sent[-1])["gaps"]}, {
            "Hands-on Docker and Kubernetes", "3+ years of backend experience", "Writing integration tests for services"})
        self.assertEqual(listed["Hands-on Docker and Kubernetes"]["items"], ["Docker", "Kubernetes"])
        self.assertEqual(listed["Hands-on Docker and Kubernetes"]["where"], "Languages: Python, Java")
        self.assertIsNone(listed["3+ years of backend experience"])
        bullet = listed["Writing integration tests for services"]
        self.assertEqual((bullet["kind"], bullet["where"], bullet["tags"]),
                         ("bullet", "Example Corp", ["integration tests"]))
        # Shown with the lines already there, so a reworded one is plain to see.
        self.assertEqual(bullet["beside"], ["Built REST APIs for an internal tool.", "Wrote unit tests for billing code."])
        for private in ("Alex Example", "alex@example.com", "Example Corp", "Example University"):
            self.assertFalse(any(private in sent for sent in chat.sent))
        self.assertEqual({item["strength"] for item in gaps["requirements"]}, {"required"})
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
        self.assertEqual({item["text"]: item["suggestion"] for item in gaps["requirements"]}["Writing integration tests for services"], None)

    def test_a_new_line_that_copies_one_already_there_but_a_word_is_dropped(self):
        # Seen on a real posting: an existing bullet came back with only its first verb changed.
        def copying(ids):
            return [{"requirement": ids["Writing integration tests for services"], "kind": "bullet", "entry": "s1e0",
                     "text": "Created REST APIs for an internal tool.", "tags": ["REST APIs"]},
                    {"requirement": ids["Hands-on Docker and Kubernetes"], "kind": "bullet", "entry": "s1e0",
                     "text": "Documented REST APIs for the internal tool with OpenAPI.", "tags": ["OpenAPI"]}]
        gaps, _ = self.gaps(copying)
        suggestions = {item["text"]: item["suggestion"] for item in gaps["requirements"]}
        self.assertIsNone(suggestions["Writing integration tests for services"])
        # Found in review by Codex: in a short line one changed verb weighs too much for the ratio.
        def verb(ids):
            return [{"requirement": ids["Writing integration tests for services"], "kind": "bullet", "entry": "s1e0",
                     "text": "Added unit tests for billing code.", "tags": ["testing"]}]
        gaps, _ = self.gaps(verb)
        self.assertIsNone({item["text"]: item["suggestion"] for item in gaps["requirements"]}["Writing integration tests for services"])
        # Found in review by Codex: naming the same work while adding a tool is not a copy.
        self.assertEqual(suggestions["Hands-on Docker and Kubernetes"]["text"], "Documented REST APIs for the internal tool with OpenAPI.")

    def test_suggestions_repeating_what_the_cv_already_says_are_dropped(self):
        def repeating(ids):
            return [
                {"requirement": ids["Writing integration tests for services"], "kind": "bullet", "entry": "s1e0",
                 "text": "Built REST APIs for an internal tool.", "tags": ["REST APIs"]},
                {"requirement": ids["Hands-on Docker and Kubernetes"], "kind": "skill",
                 "line": "fact-skills-languages", "items": ["java", "REST APIs"]},
            ]
        gaps, _ = self.gaps(repeating)
        suggestions = {item["text"]: item["suggestion"] for item in gaps["requirements"]}
        self.assertIsNone(suggestions["Writing integration tests for services"])
        self.assertIsNone(suggestions["Hands-on Docker and Kubernetes"])

    def test_accepting_a_skill_confirms_a_new_version_of_that_skills_line(self):
        gaps, _ = self.gaps()
        requirement = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        updated, profile = accept_gap(gaps, requirement, self.database, PROFILE)
        skills = next(fact for fact in list_facts(self.database) if fact["id"] == "fact-skills-languages")
        self.assertEqual((skills["text"], skills["version"], skills["status"]),
                         ("Languages: Python, Java, Docker, Kubernetes", 2, "confirmed"))
        self.assertIsNone(profile)  # the same fact ID is already on the CV
        self.assertEqual(next(item for item in updated["requirements"] if item["requirement_id"] == requirement)["status"], "added")

    def test_accepting_a_bullet_adds_a_confirmed_fact_to_that_entry(self):
        gaps, _ = self.gaps()
        requirement = next(item["requirement_id"] for item in gaps["requirements"] if "integration" in item["text"])
        original = copy.deepcopy(PROFILE)
        _, profile = accept_gap(gaps, requirement, self.database, PROFILE)
        added = [fact for fact in list_facts(self.database) if fact["text"] == "Wrote integration tests for internal services."]
        self.assertEqual([(fact["status"], fact["fact_type"], fact["tags"]) for fact in added],
                         [("confirmed", "experience", ["integration tests"])])
        self.assertEqual(profile["sections"][1]["entries"][0]["facts"][-1], added[0]["id"])
        self.assertEqual(PROFILE, original)  # the caller's profile is not changed in place

    def test_a_new_line_goes_under_its_own_employer_even_after_the_layout_changes(self):
        gaps, _ = self.gaps()
        requirement = next(item["requirement_id"] for item in gaps["requirements"] if "integration" in item["text"])
        moved = copy.deepcopy(PROFILE)
        moved["sections"][1]["entries"].insert(0, {"title": "Other Co", "dates": "2024", "facts": []})
        _, profile = accept_gap(gaps, requirement, self.database, moved)
        entries = profile["sections"][1]["entries"]
        self.assertEqual(entries[0]["facts"], [])
        self.assertEqual(len(entries[1]["facts"]), 3)  # Example Corp, now second

    def test_a_suggestion_whose_entry_or_line_changed_since_is_refused(self):
        gaps, _ = self.gaps()
        bullet = next(item["requirement_id"] for item in gaps["requirements"] if "integration" in item["text"])
        skill = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
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
        bullet = next(item["requirement_id"] for item in gaps["requirements"] if "integration" in item["text"])
        skill = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        _, saved = accept_gap(gaps, bullet, self.database, PROFILE)
        accept_gap(gaps, skill, self.database, saved)
        updated, again = accept_gap(gaps, bullet, self.database, saved)
        updated, _ = accept_gap(updated, skill, self.database, saved)
        texts = [fact["text"] for fact in list_facts(self.database)]
        skills = next(fact for fact in list_facts(self.database) if fact["id"] == "fact-skills-languages")
        self.assertIsNone(again)  # the saved profile already lists the line
        self.assertEqual(texts.count("Wrote integration tests for internal services."), 1)
        self.assertEqual((skills["version"], skills["status"]), (2, "confirmed"))
        self.assertEqual({item["requirement_id"]: item["status"] for item in updated["requirements"]}[skill], "added")

    def test_accepting_never_confirms_a_version_the_user_has_not_seen(self):
        # Found in review by Codex: a pending version with the suggested text may carry other,
        # unreviewed tags, so it is left for the Facts page instead of being confirmed here.
        gaps, _ = self.gaps()
        skill = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
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
        bullet = next(item["requirement_id"] for item in gaps["requirements"] if "integration" in item["text"])
        _, saved = accept_gap(gaps, bullet, self.database, PROFILE)
        line = saved["sections"][1]["entries"][0]["facts"].pop()
        saved["sections"][1]["entries"].append({"title": "Other Co", "dates": "2024", "facts": [line]})
        with self.assertRaisesRegex(ValueError, "重新检查缺口"):
            accept_gap(gaps, bullet, self.database, saved)

    def test_a_refused_acceptance_changes_nothing(self):
        # Found in review by Codex: the pending fact was confirmed before the placement check
        # refused the acceptance.
        gaps, _ = self.gaps()
        bullet = next(item["requirement_id"] for item in gaps["requirements"] if "integration" in item["text"])
        pending, _ = add_fact(self.database, "Wrote integration tests for internal services.", "experience", ["integration tests"])
        moved = copy.deepcopy(PROFILE)
        moved["sections"][1]["entries"].append({"title": "Other Co", "dates": "2024", "facts": [pending["id"]]})
        with self.assertRaisesRegex(ValueError, "重新检查缺口"):
            accept_gap(gaps, bullet, self.database, moved)
        self.assertEqual(next(fact for fact in list_facts(self.database) if fact["id"] == pending["id"])["status"], "pending")

    def test_a_suggestion_declined_before_stays_declined_when_checked_again(self):
        gaps, _ = self.gaps()
        docker = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        declined = decline_gap(gaps, docker)
        again = find_gaps(decided_job(), self.draft, self.database, FakeDeepSeek(suggestions), previous=declined)
        self.assertEqual({item["text"]: item["status"] for item in again["requirements"] if item["suggestion"]}, {
            "Hands-on Docker and Kubernetes": "declined", "Writing integration tests for services": "open"})
        # A skill said to be untrue stays so even when the skills line it would join has changed.
        earlier = copy.deepcopy(declined)
        record = next(item for item in earlier["requirements"] if item["requirement_id"] == docker)
        record["suggestion"].update(where="Languages: Python", new_text="Languages: Python, Docker, Kubernetes")
        later = find_gaps(decided_job(), self.draft, self.database, FakeDeepSeek(suggestions), previous=earlier)
        self.assertEqual(next(item for item in later["requirements"] if item["requirement_id"] == docker)["status"], "declined")

    def test_a_declined_suggestion_stays_declined_after_a_check_that_failed(self):
        # Found in review by Codex: a failed check in between forgot what the user had declined.
        gaps, _ = self.gaps()
        docker = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        failed = find_gaps(decided_job(), self.draft, self.database, FakeDeepSeek(
            suggestions, error=DeepSeekError("测试中不联网", reason="unreachable")), previous=decline_gap(gaps, docker))
        again = find_gaps(decided_job(), self.draft, self.database, FakeDeepSeek(suggestions), previous=failed)
        self.assertEqual(next(item for item in again["requirements"] if item["requirement_id"] == docker)["status"], "declined")

    def test_suggestions_naming_lines_in_the_wrong_shape_are_dropped(self):
        def misshapen(ids):
            return [{"requirement": ids["Hands-on Docker and Kubernetes"], "kind": "skill", "line": ["L1"], "items": ["Docker"]},
                    {"requirement": ids["Writing integration tests for services"], "kind": "bullet", "entry": {"id": "s1e0"},
                     "text": "Wrote integration tests for internal services.", "tags": []}]
        gaps, _ = self.gaps(misshapen)
        self.assertEqual({item["text"]: item["suggestion"] for item in gaps["requirements"] if item["evidence"] != "supported"},
                         dict.fromkeys(["Hands-on Docker and Kubernetes", "3+ years of backend experience",
                                        "Writing integration tests for services"]))

    def test_the_user_can_write_the_line_for_a_requirement_nothing_shows(self):
        gaps, _ = self.gaps(lambda ids: [])  # no suggestion at all
        docker = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        entry = next(place for place in places(self.draft) if place["kind"] == "bullet")
        self.assertEqual(entry["where"], "Example Corp")
        text = "Deployed 3 services with Docker and Kubernetes."  # the user's own words, numbers included
        updated, profile = write_line(gaps, docker, entry["id"], text, self.draft, self.database, PROFILE)
        added = [fact for fact in list_facts(self.database) if fact["text"] == text]
        self.assertEqual([(fact["status"], fact["fact_type"]) for fact in added], [("confirmed", "experience")])
        self.assertEqual(profile["sections"][1]["entries"][0]["facts"][-1], added[0]["id"])
        gap = next(item for item in updated["requirements"] if item["requirement_id"] == docker)
        self.assertEqual((gap["status"], gap["suggestion"]["written"]), ("added", True))

    def test_the_user_can_add_skills_of_their_own_even_after_declining_the_suggestion(self):
        gaps, _ = self.gaps()
        docker = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        place = next(place for place in places(self.draft) if place["kind"] == "skill")
        updated, profile = write_line(decline_gap(gaps, docker), docker, place["id"], "Docker, java，Docker", self.draft,
                                      self.database, PROFILE)
        skills = next(fact for fact in list_facts(self.database) if fact["id"] == "fact-skills-languages")
        self.assertEqual((skills["text"], skills["version"], skills["status"]), ("Languages: Python, Java, Docker", 2, "confirmed"))
        self.assertIn("Docker", skills["tags"])
        self.assertIsNone(profile)
        self.assertEqual(next(item for item in updated["requirements"] if item["requirement_id"] == docker)["status"], "added")

    def test_a_line_of_ones_own_is_refused_when_empty_already_there_or_its_place_is_gone(self):
        gaps, _ = self.gaps(lambda ids: [])
        docker = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        entry = next(place for place in places(self.draft) if place["kind"] == "bullet")
        cases = [(entry["id"], "   ", "请写"), (entry["id"], "Built REST APIs for an internal tool.", "已经有"),
                 (entry["id"], "x" * 301, "最多"), ("skill:fact-skills-languages", "Python, java", "已有"),
                 ("entry:unknown", "Anything true.", "位置")]
        for place, text, message in cases:
            with self.subTest(text=text[:20]):
                with self.assertRaisesRegex(ValueError, message):
                    write_line(gaps, docker, place, text, self.draft, self.database, PROFILE)
        self.assertEqual(len(list_facts(self.database)), len(FACTS))  # nothing was stored

    def test_a_declined_or_unsuggested_gap_stays_off_the_cv(self):
        gaps, _ = self.gaps()
        years = next(item["requirement_id"] for item in gaps["requirements"] if "years" in item["text"])
        docker = next(item["requirement_id"] for item in gaps["requirements"] if "Docker" in item["text"])
        declined = decline_gap(gaps, docker)
        self.assertEqual(next(item for item in declined["requirements"] if item["requirement_id"] == docker)["status"], "declined")
        with self.assertRaisesRegex(ValueError, "没有可添加的建议"):
            accept_gap(gaps, years, self.database, PROFILE)
        with self.assertRaisesRegex(ValueError, "已标记为不属实"):
            accept_gap(declined, docker, self.database, PROFILE)


def verdicts_for(requirement, *groups):
    """An evidence answer for one requirement: supported by each group of line texts."""
    def answer(request):
        lines = sent_lines(request)
        ids = {item["text"]: item["id"] for item in request["requirements"]}
        return [{"id": ids[requirement], "verdict": "supported", "sets": [[lines[text] for text in group] for group in groups]}]
    return answer


KEEP_ALL = {"sections": ["education", "experience", "skills"], "entries": []}
CUT_SKILLS = {"sections": ["education", "experience"], "entries": []}
PYTHON, TESTS = "Experience with Python", "Writing integration tests for services"


class CoverageTests(unittest.TestCase):
    """What this CV shows for each requirement, worked out from the CV as it is shown now."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = make_store(self.directory.name)
        self.draft = build_draft(PROFILE, self.database, "en")

    def tearDown(self):
        self.directory.cleanup()

    def check(self, cv, answer=evidence):
        return find_gaps(decided_job(), cv, self.database, FakeDeepSeek(lambda ids: [], answer))

    def shown(self, gaps, cv):
        confirmed = [(fact["id"], fact["version"]) for fact in list_facts(self.database) if fact["status"] == "confirmed"]
        return coverage(gaps, cv, confirmed)

    def requirement(self, gaps, cv, text):
        return next(item for item in self.shown(gaps, cv)["requirements"] if item["text"] == text)

    def test_each_requirement_gets_one_status(self):
        shown = self.shown(self.check(self.draft), self.draft)
        self.assertEqual({item["text"]: item["status"] for item in shown["requirements"]}, {
            PYTHON: "shown", "Hands-on Docker and Kubernetes": "none",
            "3+ years of backend experience": "related", TESTS: "related"})
        python = next(item for item in shown["requirements"] if item["text"] == PYTHON)
        self.assertEqual(python["evidence"], [{"text": "Languages: Python, Java", "shown": True, "why": None, "undo": []}])
        years = next(item for item in shown["requirements"] if "years" in item["text"])
        self.assertEqual((years["missing"], years["evidence"][0]["text"]), ("3+ years", "Software Intern (2025)"))

    def test_hiding_the_only_line_that_shows_it_changes_its_status_until_undone(self):
        gaps = self.check(self.draft)
        planned = plan_draft(self.draft, decided_job(), chat=FakePlanner(CUT_SKILLS))
        python = self.requirement(gaps, planned, PYTHON)
        self.assertEqual(python["status"], "not_shown")
        self.assertEqual(python["evidence"], [{"text": "Languages: Python, Java", "shown": False, "why": "cut", "undo": ["cut:skills"]}])
        self.assertEqual(self.requirement(gaps, set_change(planned, "cut:skills"), PYTHON)["status"], "shown")

    def test_lines_that_show_it_only_together_must_all_be_shown(self):
        gaps = self.check(self.draft, verdicts_for(TESTS, ["Built REST APIs for an internal tool.", "Wrote unit tests for billing code."]))
        planned = plan_draft(self.draft, decided_job(), chat=FakePlanner(
            {**KEEP_ALL, "entries": [{"entry": "s1e0", "lines": ["fact-intern-tests"]}]}))
        tests = self.requirement(gaps, planned, TESTS)
        self.assertEqual(tests["status"], "not_shown")
        self.assertEqual([(item["text"], item["shown"], item["undo"]) for item in tests["evidence"]], [
            ("Built REST APIs for an internal tool.", False, ["cut:fact-intern-api"]),
            ("Wrote unit tests for billing code.", True, [])])
        self.assertEqual(self.requirement(gaps, self.draft, TESTS)["status"], "shown")

    def test_a_rewording_that_no_longer_shows_it_says_so_and_can_be_undone(self):
        tailored = tailor_draft(self.draft, self.database, chat=FakeChat({"fact-intern-tests": "Wrote tests for billing code."}))
        planned = plan_draft(tailored, decided_job(), chat=FakePlanner(KEEP_ALL))
        gaps = self.check(planned, verdicts_for(TESTS, ["Wrote unit tests for billing code."]))
        tests = self.requirement(gaps, planned, TESTS)
        self.assertEqual((tests["status"], tests["evidence"]), ("not_shown", [
            {"text": "Wrote unit tests for billing code.", "shown": False, "why": "reworded", "undo": ["reword:fact-intern-tests"]}]))
        self.assertEqual(self.requirement(gaps, set_change(planned, "reword:fact-intern-tests"), TESTS)["status"], "shown")

    def test_a_change_already_undone_is_not_offered_again(self):
        tailored = tailor_draft(self.draft, self.database, chat=FakeChat({"fact-intern-tests": "Wrote tests for billing code."}))
        planned = set_change(plan_draft(tailored, decided_job(), chat=FakePlanner(KEEP_ALL)), "reword:fact-intern-tests")
        gaps = self.check(planned, verdicts_for(TESTS, ["Wrote tests for billing code."]))  # only the new words show it
        self.assertEqual(self.requirement(gaps, planned, TESTS)["evidence"], [
            {"text": "Wrote tests for billing code.", "shown": False, "why": "reworded", "undo": []}])

    def test_evidence_that_is_not_on_this_cv_is_named_as_such(self):
        docker, _ = add_fact(self.database, "Deployed services with Docker and Kubernetes.", "experience", ["Docker"])
        confirm_fact(self.database, docker["id"], 1)
        gaps = self.check(self.draft, verdicts_for("Hands-on Docker and Kubernetes", ["Deployed services with Docker and Kubernetes."]))
        self.assertEqual(self.requirement(gaps, self.draft, "Hands-on Docker and Kubernetes")["evidence"], [
            {"text": "Deployed services with Docker and Kubernetes.", "shown": False, "why": "not_on_cv", "undo": []}])

    def test_without_an_answer_nothing_is_shown_or_missing(self):
        # A matching skill word must never stand in for DeepSeek's verdict.
        chat = FakeDeepSeek(suggestions, error=DeepSeekError("DeepSeek API 返回 HTTP 503", reason="request_failed"))
        gaps = find_gaps(decided_job(), self.draft, self.database, chat)
        self.assertEqual({item["status"] for item in self.shown(gaps, self.draft)["requirements"]}, {"unchecked"})
        self.assertEqual((gaps["evidence_check"]["fallback_code"], len(chat.sent)), ("request_failed", 1))

    def test_an_answer_without_a_usable_line_leaves_that_requirement_unchecked(self):
        def sloppy(request):
            lines = sent_lines(request)
            ids = {item["text"]: item["id"] for item in request["requirements"]}
            python = lines["Languages: Python, Java"]
            return [
                {"id": ["not", "an", "id"], "verdict": "none"},
                {"id": ids[PYTHON], "verdict": "supported", "sets": [["E999"], [python, python, "E998"], [{"id": python}], "E1"]},
                {"id": ids[TESTS], "verdict": "related", "lines": ["E999", {"id": python}, [python]]},
                {"id": ids["Hands-on Docker and Kubernetes"], "verdict": "maybe"},
                {"id": ids["3+ years of backend experience"], "verdict": "supported",
                 "sets": [[python, lines["Software Intern (2025)"], lines["Wrote unit tests for billing code."],
                           lines["Coursework: Algorithms, Databases"]]]},
            ]
        statuses = {item["text"]: item["status"] for item in self.shown(self.check(self.draft, sloppy), self.draft)["requirements"]}
        self.assertEqual(set(statuses.values()), {"unchecked"})

    def test_each_line_goes_out_with_its_own_entry_so_no_line_borrows_other_dates(self):
        # Found in review by Codex: a flat list of lines let a bullet from one job take another
        # job's dates, so "3+ years of X" could look shown.
        profile = copy.deepcopy(PROFILE)
        profile["sections"][1]["entries"][0]["facts"] = ["fact-intern-api"]
        profile["sections"][1]["entries"].append(
            {"title": "Other Co", "subtitle": "Software Engineer", "dates": "2020 – 2024", "facts": ["fact-intern-tests"]})
        chat = FakeDeepSeek(lambda ids: [], lambda request: [])
        gaps = find_gaps(decided_job(), build_draft(profile, self.database, "en"), self.database, chat)
        entries = {entry["role"]["text"]: [line["text"] for line in entry["lines"]]
                   for entry in json.loads(chat.sent[0])["resume"] if entry.get("role")}
        self.assertEqual(entries["Software Intern (2025)"], ["Built REST APIs for an internal tool."])
        self.assertEqual(entries["Software Engineer (2020 – 2024)"], ["Wrote unit tests for billing code."])
        self.assertNotIn("Other Co", chat.sent[0])
        # Moving a line to the other job changes what it shows, so the check is out of date.
        moved = copy.deepcopy(profile)
        moved["sections"][1]["entries"][0]["facts"], moved["sections"][1]["entries"][1]["facts"] = \
            ["fact-intern-tests"], ["fact-intern-api"]
        self.assertTrue(self.shown(gaps, build_draft(moved, self.database, "en"))["stale"])

    def test_putting_back_a_reworded_line_also_restores_the_words_that_show_it(self):
        # Found in review by Codex: undoing the cut alone brought back the rewording, which does
        # not show the requirement.
        tailored = tailor_draft(self.draft, self.database, chat=FakeChat({"fact-intern-tests": "Wrote tests for billing code."}))
        planned = plan_draft(tailored, decided_job(), chat=FakePlanner(
            {**KEEP_ALL, "entries": [{"entry": "s1e0", "lines": ["fact-intern-api"]}]}))
        gaps = self.check(planned, verdicts_for(TESTS, ["Wrote unit tests for billing code."]))
        [line] = self.requirement(gaps, planned, TESTS)["evidence"]
        self.assertEqual((line["why"], line["undo"]), ("cut", ["cut:fact-intern-tests", "reword:fact-intern-tests"]))
        for change in line["undo"]:
            planned = set_change(planned, change)
        self.assertEqual(self.requirement(gaps, planned, TESTS)["status"], "shown")

    def test_a_check_is_out_of_date_when_evidence_changes_even_where_it_is_hidden(self):
        # Found in review by Codex: only what the CV showed was compared, so a cut line could be
        # reworded again, or a role removed, without the check noticing.
        def planned_with(rewording):
            tailored = tailor_draft(self.draft, self.database, chat=FakeChat({"fact-skills-languages": rewording}))
            return plan_draft(tailored, decided_job(), chat=FakePlanner(CUT_SKILLS))
        first = planned_with("Languages: Java, Python")
        gaps = self.check(first)
        self.assertFalse(self.shown(gaps, set_change(first, "cut:skills"))["stale"])  # undoing is not a change
        self.assertTrue(self.shown(gaps, planned_with("Languages: Python and Java"))["stale"])
        no_role = copy.deepcopy(PROFILE)
        del no_role["sections"][0]["entries"][0]["subtitle"]
        self.assertTrue(self.shown(gaps, build_draft(no_role, self.database, "en"))["stale"])

    def test_a_check_is_out_of_date_once_the_facts_or_the_wording_change(self):
        gaps = self.check(self.draft)
        planned = plan_draft(self.draft, decided_job(), chat=FakePlanner(CUT_SKILLS))
        self.assertFalse(self.shown(gaps, planned)["stale"])  # cuts and undos need no new check
        reworded = tailor_draft(self.draft, self.database, chat=FakeChat({"fact-intern-api": "For an internal tool, built REST APIs."}))
        self.assertTrue(self.shown(gaps, reworded)["stale"])
        revise_fact(self.database, "fact-intern-tests", text="Wrote unit tests for the billing code.")
        self.assertTrue(self.shown(gaps, self.draft)["stale"])


if __name__ == "__main__":
    unittest.main()
