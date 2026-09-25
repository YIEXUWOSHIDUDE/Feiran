import tempfile
import unittest
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

    def test_jobs_mentioning_more_confirmed_skills_come_first(self):
        boards = FakeBoards([
            posting("1", "Designer", "Figma and user research."),
            posting("2", "Platform Engineer", "Kubernetes and Python."),
            posting("3", "Data Engineer", "Python pipelines with SQL."),
        ])
        add_source(self.listings_db, "greenhouse", "example", fetch=boards)
        self.assertEqual(self.titles(), [
            ("Data Engineer", ["Python", "SQL"]),
            ("Platform Engineer", ["Python"]),
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

    def test_newly_confirmed_skills_and_changed_postings_are_matched_again(self):
        boards = FakeBoards([posting("1", "Platform Engineer", "Kubernetes clusters."), posting("2", "Data Engineer", "Python.")])
        add_source(self.listings_db, "greenhouse", "example", fetch=boards)
        self.assertEqual(self.titles(), [("Data Engineer", ["Python"]), ("Platform Engineer", [])])
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
