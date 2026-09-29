import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace import Workspace, WorkspaceError


class Crash(BaseException):
    """Stands in for the process dying: nothing after it runs, not even error handling."""


def crash_when_writing(name, written=False):
    """Kill the process as the file ``name`` is renamed into place: before the rename, or, with
    ``written``, just after it."""
    real = os.replace

    def replace(source, destination):
        if written:
            real(source, destination)
        if Path(destination).name == name:
            raise Crash()
        if not written:
            real(source, destination)

    return patch("workspace.os.replace", replace)


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
        self.root = Path(self.directory.name)
        self.workspace = Workspace(self.root)
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

    def files(self):
        return sorted(str(path.relative_to(self.root / self.job)) for path in (self.root / self.job).rglob("*"))

    def test_a_step_cut_short_by_a_crash_is_undone_at_the_next_start(self):
        for step in ("decided", "cv-draft-en", "cv-planned-en"):
            self.workspace.write(self.job, step, {"version": 1})
        before = self.files()
        with crash_when_writing("cv-draft-en.json"), self.assertRaises(Crash):
            self.workspace.write(self.job, "cv-draft-en", {"version": 2})
        restarted = Workspace(self.root)  # the next start
        self.assertEqual(restarted.recover(), [{"job_id": self.job, "step": "cv-draft-en"}])
        self.assertEqual(restarted.read(self.job, "cv-draft-en"), {"version": 1})
        self.assertEqual(restarted.read(self.job, "cv-planned-en"), {"version": 1})
        self.assertEqual(restarted.read_note(self.job, "interrupted")["step"], "cv-draft-en")
        self.assertEqual(self.files(), sorted(before + ["interrupted.json"]))  # nothing half-done is left
        restarted.write(self.job, "gaps", {"version": 1})  # other work keeps the note for the page
        self.assertEqual(restarted.read_note(self.job, "interrupted")["step"], "cv-draft-en")
        restarted.write(self.job, "decided", {"version": 2})  # redoing what it depends on clears it
        self.assertIsNone(restarted.read_note(self.job, "interrupted"))

    def test_a_crash_while_moving_the_old_steps_aside_is_undone(self):
        for step in ("cv-draft-en", "cv-tailored-en", "cv-planned-en"):
            self.workspace.write(self.job, step, {"version": 1})
        before = self.files()
        real = Path.rename

        def rename(source, target):
            if source.name == "cv-planned-en.json":  # the draft and its rewording are already aside
                raise Crash()
            return real(source, target)

        with patch.object(Path, "rename", rename), self.assertRaises(Crash):
            self.workspace.write(self.job, "cv-draft-en", {"version": 2})
        restarted = Workspace(self.root)
        self.assertEqual(restarted.recover(), [{"job_id": self.job, "step": "cv-draft-en"}])
        self.assertEqual([restarted.read(self.job, step) for step in ("cv-draft-en", "cv-tailored-en", "cv-planned-en")],
                         [{"version": 1}] * 3)
        self.assertEqual(self.files(), sorted(before + ["interrupted.json"]))

    def test_a_step_written_in_full_before_a_crash_stands(self):
        for step in ("cv-draft-en", "cv-planned-en"):
            self.workspace.write(self.job, step, {"version": 1})
        with crash_when_writing("cv-draft-en.json", written=True), self.assertRaises(Crash):
            self.workspace.write(self.job, "cv-draft-en", {"version": 2})
        restarted = Workspace(self.root)
        self.assertEqual(restarted.recover(), [])
        self.assertEqual(restarted.read(self.job, "cv-draft-en"), {"version": 2})
        self.assertIsNone(restarted.read(self.job, "cv-planned-en"))  # moved to history, as the change meant
        self.assertIsNone(restarted.read_note(self.job, "interrupted"))
        self.assertNotIn("change-in-progress.json", self.files())

    def test_a_step_that_cannot_be_written_leaves_the_job_as_it_was(self):
        for step in ("cv-draft-en", "cv-planned-en"):
            self.workspace.write(self.job, step, {"version": 1})
        before = self.files()
        real = os.replace

        def disk_full(source, destination):
            if Path(destination).name == "cv-draft-en.json":
                raise OSError(28, "No space left on device")
            real(source, destination)

        with patch("workspace.os.replace", disk_full), self.assertRaises(OSError):
            self.workspace.write(self.job, "cv-draft-en", {"version": 2})
        self.assertEqual(self.workspace.read(self.job, "cv-draft-en"), {"version": 1})
        self.assertEqual(self.workspace.read(self.job, "cv-planned-en"), {"version": 1})
        self.assertEqual(self.files(), before)
        self.assertEqual(Workspace(self.root).recover(), [])

    def test_a_job_whose_creation_was_cut_short_disappears(self):
        with crash_when_writing("input.json"), self.assertRaises(Crash):
            self.workspace.create_job(INPUT)
        restarted = Workspace(self.root)
        restarted.recover()
        self.assertEqual([job["job_id"] for job in restarted.jobs()], [self.job])

    def test_job_ids_and_step_names_cannot_leave_the_workspace(self):
        for job_id, step in (("../outside", "input"), (self.job, "../input"), (self.job, "notes")):
            with self.subTest(job_id=job_id, step=step):
                with self.assertRaises(WorkspaceError):
                    self.workspace.read(job_id, step)


if __name__ == "__main__":
    unittest.main()
