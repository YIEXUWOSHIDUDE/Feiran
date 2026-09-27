import tempfile
import unittest
from pathlib import Path

from workspace import Workspace, WorkspaceError


INPUT = {
    "jd": {
        "text": "Requirements:\n- Experience with Python services",
        "title": "Backend Intern",
        "company": "Acme",
        "source": None,
        "captured_at": "2026-09-24T00:00:00+00:00",
        "provider": "manual",
    },
    "facts": [],
    "selected_requirements": [],
}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self.directory.name))
        self.job = self.workspace.create_job(INPUT)

    def tearDown(self):
        self.directory.cleanup()

    def test_new_job_is_listed_with_its_title_and_first_step(self):
        jobs = self.workspace.jobs()
        self.assertEqual(
            [(job["job_id"], job["title"], job["company"]) for job in jobs],
            [(self.job, "Backend Intern", "Acme")],
        )
        self.assertEqual(self.workspace.state(self.job), ["input"])
        self.assertEqual(self.workspace.read(self.job, "input"), INPUT)

    def test_redoing_a_step_archives_it_and_every_later_step(self):
        for step in ("candidates", "decided", "cv-draft-en"):
            self.workspace.write(self.job, step, {"version": 1})
        self.workspace.write(self.job, "candidates", {"version": 2})
        history = list((Path(self.directory.name) / self.job / "history").rglob("*.json"))
        self.assertEqual(self.workspace.state(self.job), ["input", "candidates"])
        self.assertEqual(self.workspace.read(self.job, "candidates"), {"version": 2})
        self.assertEqual(
            sorted(path.name for path in history),
            ["candidates.json", "cv-draft-en.json", "decided.json"],
        )

    def test_one_languages_cv_steps_leave_the_other_language_alone(self):
        self.workspace.write(self.job, "cv-draft-en", {"language": "en"})
        self.workspace.write(self.job, "cv-tailored-en", {"language": "en"})
        self.workspace.write(self.job, "cv-draft-zh", {"language": "zh"})
        self.workspace.write_bytes(self.job, "cv-final-zh", b"%PDF-1.4 zh")
        self.workspace.write(self.job, "cv-draft-en", {"language": "en", "version": 2})
        self.assertEqual(
            self.workspace.state(self.job), ["input", "cv-draft-en", "cv-draft-zh", "cv-final-zh"]
        )
        self.assertEqual(self.workspace.path(self.job, "cv-final-zh").read_bytes(), b"%PDF-1.4 zh")

    def test_talking_points_can_be_redone_without_touching_the_cv(self):
        for step in ("decided", "cv-draft-en", "cv-tailored-en", "cv-planned-en", "matches", "linked"):
            self.workspace.write(self.job, step, {"version": 1})
        self.workspace.write(self.job, "matches", {"version": 2})
        self.assertEqual(
            self.workspace.state(self.job),
            ["input", "decided", "matches", "cv-draft-en", "cv-tailored-en", "cv-planned-en"],
        )
        self.workspace.write(self.job, "cv-tailored-en", {"version": 2})
        self.assertEqual(self.workspace.state(self.job), ["input", "decided", "matches", "cv-draft-en", "cv-tailored-en"])

    def test_job_ids_and_step_names_cannot_leave_the_workspace(self):
        for job_id, step in (("../outside", "input"), (self.job, "../input"), (self.job, "notes")):
            with self.subTest(job_id=job_id, step=step):
                with self.assertRaises(WorkspaceError):
                    self.workspace.read(job_id, step)


if __name__ == "__main__":
    unittest.main()
