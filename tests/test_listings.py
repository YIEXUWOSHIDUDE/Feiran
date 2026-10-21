import json
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
import sqlite3
from pathlib import Path

from facts import confirm_facts, import_facts
from job_search import SearchError
from listings import (
    ListingsError,
    add_source,
    initialize,
    list_sources,
    ranked_listings,
    refresh_source,
    remove_source,
)


FACTS = [
    {"id": "fact-python", "type": "skill", "text": "Built services in Python and SQL.", "tags": ["Python", "SQL"]},
    {"id": "fact-k8s", "type": "skill", "text": "Deployed with Kubernetes.", "tags": ["Kubernetes"]},
]
# Only the two headings and "5+ years of experience in software engineering" are quoted from
# MongoDB's public Dublin Solutions Architect posting (2026-10). The rest is made-up filler in the
# same shape: a 2+ years line the board's HTML wraps mid-list, then a preferred part. Germany is
# the wording on that role's page as the user supplied it.
MONGODB_DUBLIN = (
    "Required Qualifications\n5+ years of experience in software engineering or a similar hands-on technical job\n"
    "2+ years of everyday coding experience in a language such as Python, Go/\nRust\n, or SQL\n"
    "Preferred Qualifications\nFamiliarity with document databases"
)
MONGODB_GERMANY = (
    "As an ideal candidate, you will have:\n"
    "5 to 8 years of related industry experience, ideally 2 years of experience in a customer facing role"
)
# Wording that must hide a role, then wording that must not.
REQUIRES_YEARS = [
    "Minimum Qualifications\nAt least 5 years of professional software development experience.",
    "You have a minimum of five years of work experience in data engineering.",
    "Requirements:\n- 5-8 years of experience building backend services",
    "Requirements:\n- 6–10 years’ experience with distributed systems",
    "5 years of experience with software development in one or more programming languages.",
    "Five or more years of industry experience.",
    "We're looking for someone with 7+ years of experience in machine learning.",
    "We are looking for candidates with 5+ years of experience.",
    "Bachelor's degree in Computer Science or a related field and 5+ years of experience.",
    "Preferred Qualifications\nKnowledge of SQL.\nRequired Qualifications\n5+ years of professional experience.",
    "About Acme\nAcme builds payment software.\nRequirements\n5+ years of backend experience.",
    "五年以上工作经验\n岗位职责\n负责后台开发",
    "任职要求：\n1、5至8年Java开发经验；",
]
NO_REQUIRED_YEARS = [
    "Solutions Architect helping customers design their data platforms.",
    "You will work with senior engineers and engineering managers.",
    "Required Qualifications\n2+ years of professional experience.",
    "2-5 years of professional experience.",
    "Required Qualifications\n0-1 years experience; partner with senior engineers.",
    "Up to 5 years of experience.",
    "Ideally 5 years of software engineering experience.",
    "5 years of software engineering experience preferred.",
    "Preferred Qualifications\n5+ years of software engineering experience.\n"
    "Required Qualifications\n2+ years of professional experience.",
    "Preferred Qualifications\nRequired to travel\n5+ years of experience with Python.",
    "Nice to have:\n- 5+ years of experience with Kafka",
    "Preferred: 5+ years of experience in fintech",
    "A five-year degree in engineering.",
    "Our company has 10 years of experience building databases.",
    "Work with a mentor who has 10 years of experience.",
    "Work with a mentor with 10 years of experience.",
    "Senior engineers with 10+ years of experience will mentor you.",
    "About us\nWith 10 years of experience, Acme builds software.\nRequirements\n0-1 years of experience.",
    "About Acme\nOver 20 years of industry experience serving banks.\nRequirements\n0-1 years of experience.",
    "Bachelors degree and 5 years of experience, or a Masters degree and 2 years of experience.",
    "Bachelor's degree or 5+ years of equivalent work experience.",
    "Bachelor's degree AND 6+ years technical engineering experience\n"
    "OR Master's degree AND 4+ years technical engineering experience\nOR equivalent experience.",
    "两年以上工作经验\n岗位职责\n负责后台开发",
    "任职要求：\n有5年以上工作经验者优先",
]


def posting(job_id, title, text, location="Remote", posted_at="2026-09-01T00:00:00+00:00"):
    return {
        "provider": "greenhouse",
        "board": "example",
        "job_id": job_id,
        "title": title,
        "company": "Example",
        "location": location,
        "source": f"https://job-boards.greenhouse.io/example/jobs/{job_id}",
        "posted_at": posted_at,
        "captured_at": "2026-09-24T12:00:00+00:00",
        "raw_content": text,
        "text": text,
    }


class FakeBoards:
    """Stands in for the public job-board API; nothing here touches the network."""

    def __init__(self, jobs):
        self.jobs = jobs
        self.calls = []

    def __call__(self, board, provider="greenhouse"):
        self.calls.append((provider, board))
        if isinstance(self.jobs, Exception):
            raise self.jobs
        return list(self.jobs)


class ListingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.facts_db = root / "workbench.db"
        self.listings_db = root / "listings.db"
        import_facts(self.facts_db, FACTS)
        confirm_facts(self.facts_db, [("fact-python", 1)])

    def tearDown(self):
        self.directory.cleanup()

    def titles(self, **filters):
        ranked = ranked_listings(self.listings_db, self.facts_db, **filters)
        return [(item["title"], item["matched"]) for item in ranked["listings"]]

    def test_seniority_reads_reuse_text_but_recheck_changed_description(self):
        import listings
        text = "Required: 7 years of software engineering experience. Unique cache test."
        with patch("listings._YEAR_WORD", wraps=listings._YEAR_WORD) as words:
            self.assertTrue(listings.requires_senior_experience(text))
            first = words.finditer.call_count
            listings.requires_senior_experience(text)
            self.assertEqual(words.finditer.call_count, first, "same JD must not be parsed twice")
            self.assertFalse(listings.requires_senior_experience(text.replace("7 years", "2 years")))
            self.assertGreater(words.finditer.call_count, first)

    def test_old_listing_database_upgrades_without_losing_jobs(self):
        add_source(self.listings_db, "greenhouse", "example", fetch=FakeBoards([
            posting("1", "Engineer", "We provide visa sponsorship.")]))
        with sqlite3.connect(self.listings_db) as connection:
            connection.execute("DROP TABLE sponsorship_cache")
            connection.execute("PRAGMA user_version = 1")
        result = ranked_listings(self.listings_db, self.facts_db)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["listings"][0]["sponsorship"]["status"], "supported")
        self.assertEqual(len(list_sources(self.listings_db)), 1)

    def test_sponsor_cache_survives_reads_refresh_and_rule_changes(self):
        from sponsorship import assess_sponsorship, RULE_VERSION
        boards = FakeBoards([posting("1", "Engineer", "We provide visa sponsorship.")])
        add_source(self.listings_db, "greenhouse", "example", fetch=boards)
        with patch("listings.assess_sponsorship", wraps=assess_sponsorship) as assess:
            first = ranked_listings(self.listings_db, self.facts_db)
            calls = assess.call_count
            ranked_listings(self.listings_db, self.facts_db, title="engineer")
            self.assertEqual(assess.call_count, calls, "unchanged list must reuse stored sponsor answer")
            refresh_source(self.listings_db, "greenhouse", "example", fetch=boards)
            ranked_listings(self.listings_db, self.facts_db)
            self.assertEqual(assess.call_count, calls)
            boards.jobs[0]["text"] = "We cannot sponsor visas."
            refresh_source(self.listings_db, "greenhouse", "example", fetch=boards)
            changed = ranked_listings(self.listings_db, self.facts_db)
            self.assertEqual(changed["listings"][0]["sponsorship"]["status"], "not_supported")
            self.assertEqual(assess.call_count, calls + 1)
            with patch("listings.SPONSOR_RULE_VERSION", RULE_VERSION + 1):
                ranked_listings(self.listings_db, self.facts_db)
            self.assertEqual(assess.call_count, calls + 2)
        self.assertEqual(first["listings"][0]["sponsorship"]["status"], "supported")

    def test_answer_cached_by_older_rules_is_worked_out_again(self):
        from sponsorship import RULE_VERSION
        text = ("We do sponsor visas! However, sponsorship is not available for every role "
                "and depends on eligibility.")
        add_source(self.listings_db, "greenhouse", "example", fetch=FakeBoards([posting("1", "Engineer", text)]))
        with closing(sqlite3.connect(self.listings_db)) as connection, connection:
            (text_hash,) = connection.execute("SELECT text_hash FROM listings").fetchone()
            connection.execute("DELETE FROM sponsorship_cache")
            # What the version before this one found in the same words.
            connection.execute("INSERT INTO sponsorship_cache VALUES (?, ?, ?)", (
                text_hash, RULE_VERSION - 1, json.dumps({"status": "conflicting", "evidence": [],
                                                         "rule_version": RULE_VERSION - 1})))
        result = ranked_listings(self.listings_db, self.facts_db)
        self.assertEqual(result["listings"][0]["sponsorship"]["status"], "conditional")
        self.assertEqual(result["listings"][0]["sponsorship"]["rule_version"], RULE_VERSION)
        with closing(sqlite3.connect(self.listings_db)) as connection:
            self.assertEqual(connection.execute("SELECT rule_version FROM sponsorship_cache").fetchall(),
                             [(RULE_VERSION,)])

    def test_jobs_mentioning_more_saved_skills_come_first(self):
        boards = FakeBoards([
            posting("1", "Designer", "Figma and user research."),
            posting("2", "Platform Engineer", "Kubernetes and Python."),
            posting("3", "Data Engineer", "Python pipelines with SQL."),
        ])
        add_source(self.listings_db, "greenhouse", "example", fetch=boards)
        self.assertEqual(self.titles(), [
            ("Data Engineer", ["Python", "SQL"]),
            ("Platform Engineer", ["Kubernetes", "Python"]),
            ("Designer", []),
        ])

    def test_one_role_posted_in_several_cities_is_one_row_and_filters_narrow_the_list(self):
        add_source(self.listings_db, "greenhouse", "example", fetch=FakeBoards([
            posting("1", "Backend Engineer", "Python and SQL.", location="New York, NY"),
            posting("2", "Backend Engineer", "Python and SQL.", location="Palo Alto, CA"),
            posting("3", "Backend Engineer", "Python and SQL, on call.", location="London"),
            posting("4", "Data Intern", "SQL.", location=None),
        ]))

        def rows(**filters):
            ranked = ranked_listings(self.listings_db, self.facts_db, **filters)
            return ranked["total"], [
                (item["title"], [posting["location"] for posting in item["postings"]])
                for item in ranked["listings"]
            ]

        self.assertEqual(rows(), (3, [
            ("Backend Engineer", ["New York, NY", "Palo Alto, CA"]),
            ("Backend Engineer", ["London"]),
            ("Data Intern", [None]),
        ]))
        self.assertEqual(rows(title="intern"), (1, [("Data Intern", [None])]))
        # Unknown is not "somewhere else": postings without a location stay in a location filter.
        self.assertEqual(rows(location="palo alto"), (2, [("Backend Engineer", ["Palo Alto, CA"]), ("Data Intern", [None])]))
        self.assertEqual(rows(limit=1, offset=1), (3, [("Backend Engineer", ["London"])]))

    def test_hiding_senior_roles_keeps_member_of_technical_staff(self):
        add_source(self.listings_db, "greenhouse", "example", fetch=FakeBoards([
            posting("1", "Senior Data Engineer", "Python."),
            posting("2", "Staff Engineer, Infra", "Python."),
            posting("3", "Engineering Manager", "Python."),
            posting("4", "Sr. Backend Engineer", "Python."),
            posting("5", "Member of Technical Staff", "Python."),
            posting("6", "Software Engineer, New Grad", "Python."),
            posting("7", "Staffing Coordinator", "Python."),
        ]))
        shown = ranked_listings(self.listings_db, self.facts_db, hide_senior=True)["listings"]
        self.assertEqual(
            sorted(item["title"] for item in shown),
            ["Member of Technical Staff", "Software Engineer, New Grad", "Staffing Coordinator"],
        )
        self.assertEqual(ranked_listings(self.listings_db, self.facts_db)["total"], 7)

    def test_hiding_senior_roles_also_reads_the_years_the_jd_requires(self):
        add_source(self.listings_db, "greenhouse", "example", fetch=FakeBoards([
            posting("1", "Solutions Architect", MONGODB_DUBLIN, location="Dublin", posted_at="2026-09-04T00:00:00+00:00"),
            posting("2", "Solutions Architect", MONGODB_DUBLIN, location="London", posted_at="2026-09-04T00:00:00+00:00"),
            posting("3", "Solutions Architect", MONGODB_GERMANY, location="Germany"),
            posting("4", "Member of Technical Staff", "Requirements\n5+ years of experience with Python."),
            posting("5", "Data Engineer", "Python.\n2+ years of experience.", posted_at="2026-09-03T00:00:00+00:00"),
            posting("6", "Platform Engineer", "Python.", posted_at="2026-09-02T00:00:00+00:00"),
        ]))

        def shown(**filters):
            ranked = ranked_listings(self.listings_db, self.facts_db, **filters)
            return ranked["total"], [[item["job_id"] for item in group["postings"]] for group in ranked["listings"]]

        # Off, nothing is hidden and the role posted in Dublin and London is one row.
        self.assertEqual(shown(), (5, [["1", "2"], ["5"], ["6"], ["4"], ["3"]]))
        self.assertEqual(shown(hide_senior=True), (2, [["5"], ["6"]]))
        # Hidden roles are left out before the page is cut.
        self.assertEqual(shown(hide_senior=True, limit=1, offset=1), (2, [["6"]]))

    def test_only_required_years_of_five_or_more_hide_a_role(self):
        jobs = [posting(f"hide-{index}", "Solutions Architect", text) for index, text in enumerate(REQUIRES_YEARS)]
        jobs += [posting(f"keep-{index}", "Solutions Architect", text) for index, text in enumerate(NO_REQUIRED_YEARS)]
        add_source(self.listings_db, "greenhouse", "example", fetch=FakeBoards(jobs))
        ranked = ranked_listings(self.listings_db, self.facts_db, hide_senior=True, limit=100)
        kept = {group["postings"][0]["job_id"] for group in ranked["listings"]}
        self.assertEqual(
            ([text for index, text in enumerate(REQUIRES_YEARS) if f"hide-{index}" in kept],
             [text for index, text in enumerate(NO_REQUIRED_YEARS) if f"keep-{index}" not in kept]),
            ([], []),
            "(still shown, wrongly hidden)",
        )
        self.assertEqual(ranked_listings(self.listings_db, self.facts_db, limit=100)["total"], len(jobs))

    def test_title_search_needs_every_word_anywhere_in_the_title(self):
        add_source(self.listings_db, "greenhouse", "example", fetch=FakeBoards([
            posting("1", "Software Engineer Intern", "Python."),
            posting("2", "Software Engineering Internship", "Python."),
            posting("3", "Senior Software Engineer, Internal Tools", "Python."),
            posting("4", "Software Engineer, Internal Platform", "Requirements\n5+ years of experience with Python."),
            posting("5", "Hardware Intern", "Python."),
            posting("6", "Software Engineer", "Python."),
        ]))

        def found(title, **filters):
            ranked = ranked_listings(self.listings_db, self.facts_db, title=title, **filters)
            return ranked["total"], sorted(group["postings"][0]["job_id"] for group in ranked["listings"])

        self.assertEqual(found("software inter"), (4, ["1", "2", "3", "4"]))
        self.assertEqual(found("  INTER   software "), (4, ["1", "2", "3", "4"]))
        self.assertEqual(found("software inter", hide_senior=True), (2, ["1", "2"]))
        self.assertEqual(found("intern"), (5, ["1", "2", "3", "4", "5"]))  # one word: contained anywhere, as before
        self.assertEqual(found("   "), (6, ["1", "2", "3", "4", "5", "6"]))
        self.assertEqual(found("software designer"), (0, []))

    def test_pending_skills_and_changed_postings_are_matched(self):
        boards = FakeBoards([posting("1", "Platform Engineer", "Kubernetes clusters."), posting("2", "Data Engineer", "Python.")])
        add_source(self.listings_db, "greenhouse", "example", fetch=boards)
        self.assertEqual(self.titles(), [("Data Engineer", ["Python"]), ("Platform Engineer", ["Kubernetes"])])
        confirm_facts(self.facts_db, [("fact-k8s", 1)])
        self.assertEqual(self.titles(), [("Data Engineer", ["Python"]), ("Platform Engineer", ["Kubernetes"])])
        boards.jobs = [posting("1", "Platform Engineer", "Kubernetes with Python and SQL."), posting("2", "Data Engineer", "Python.")]
        refresh_source(self.listings_db, "greenhouse", "example", fetch=boards)
        self.assertEqual(self.titles()[0], ("Platform Engineer", ["Kubernetes", "Python", "SQL"]))

    def test_refresh_follows_the_board_and_a_failed_read_keeps_the_last_copy(self):
        boards = FakeBoards([posting("1", "Data Engineer", "Python."), posting("2", "Designer", "Figma.")])
        add_source(self.listings_db, "greenhouse", "example", fetch=boards)
        boards.jobs = [posting("1", "Data Engineer II", "Python and SQL."), posting("3", "Analyst", "SQL.")]
        counts = refresh_source(self.listings_db, "greenhouse", "example", fetch=boards)
        self.assertEqual(counts, {"company": "Example", "open": 2, "new": 1, "closed": 1})
        self.assertEqual(self.titles(), [("Data Engineer II", ["Python", "SQL"]), ("Analyst", ["SQL"])])
        boards.jobs = SearchError("岗位接口返回 HTTP 503")
        with self.assertRaisesRegex(ListingsError, "Example.*HTTP 503"):
            refresh_source(self.listings_db, "greenhouse", "example", fetch=boards)
        with self.assertRaisesRegex(ListingsError, "不在公司列表"):
            refresh_source(self.listings_db, "lever", "unknown", fetch=boards)
        [source] = list_sources(self.listings_db)
        self.assertEqual(len(self.titles()), 2)
        self.assertEqual((source["company"], source["open_count"], source["stale"]), ("Example", 2, False))
        self.assertIn("HTTP 503", source["error"])

    def test_starter_companies_are_added_once_and_removing_one_drops_its_jobs(self):
        starter = [
            {"provider": "greenhouse", "board": "example", "company": "Example"},
            {"provider": "lever", "board": "other", "company": "Other Co"},
        ]
        initialize(self.listings_db, starter)
        refresh_source(self.listings_db, "greenhouse", "example", fetch=FakeBoards([posting("1", "Data Engineer", "Python.")]))
        self.assertEqual(len(self.titles()), 1)
        remove_source(self.listings_db, "greenhouse", "example")
        initialize(self.listings_db, starter)  # the next server start must not bring it back
        sources = [(source["company"], source["fetched_at"], source["stale"]) for source in list_sources(self.listings_db)]
        self.assertEqual(sources, [("Other Co", None, True)])
        self.assertEqual(self.titles(), [])
        with self.assertRaisesRegex(ListingsError, "招聘板标识"):
            initialize(self.directory.name + "/bad.db", [{"provider": "lever", "board": "../x", "company": "X"}])


if __name__ == "__main__":
    unittest.main()
