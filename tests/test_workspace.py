import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace as workspace_module
from workspace import DATA_FORMAT, DATA_FORMAT_FILE, DataFormatError, Workspace, WorkspaceError, claim_data_format


REPO = Path(__file__).resolve().parent.parent
PROCESS_DIES = 9


def dies_before_renaming_into(name):
    return f"""
def replace(source, destination):
    if Path(destination).name == {name!r}:
        os._exit({PROCESS_DIES})
    real_replace(source, destination)
os.replace = replace
"""


def dies_after_renaming_into(name, times=1):
    """Dies just after ``name`` is renamed into place for the ``times``-th time."""
    return f"""
placed = []
def replace(source, destination):
    real_replace(source, destination)
    if Path(destination).name == {name!r}:
        placed.append(destination)
        if len(placed) == {times}:
            os._exit({PROCESS_DIES})
os.replace = replace
"""


def dies_moving(name, times=1):
    """Dies as ``name`` is moved (its ``times``-th move from anywhere, counting all moves)."""
    return f"""
moves = []
def rename(source, target):
    moves.append(source.name)
    if source.name == {name!r} and moves.count({name!r}) == {times}:
        os._exit({PROCESS_DIES})
    return real_rename(source, target)
Path.rename = rename
"""


def dies_halfway_through_writing(prefix):
    return f"""
class Half:
    def __init__(self, handle):
        self.handle = handle
    def __enter__(self):
        return self
    def __exit__(self, *exception):
        self.handle.close()
    def write(self, data):
        self.handle.write(data[: len(data) // 2])
        self.handle.flush()
        os._exit({PROCESS_DIES})
def opener(path, mode="r", *args, **kwargs):
    handle = real_open(path, mode, *args, **kwargs)
    return Half(handle) if path.name.startswith({prefix!r}) else handle
Path.open = opener
"""


def die_while(root, action, crash_point):
    """Run ``action`` against the workspace in a new process that dies at ``crash_point`` the way
    a killed process does: no finally, no except, no temporary file removed."""
    code = "\n".join([
        "import os, sys", "from pathlib import Path", f"sys.path.insert(0, {str(REPO)!r})",
        "import workspace as module", f"workspace = module.Workspace(Path({str(root)!r}))",
        "real_replace, real_rename, real_open = os.replace, Path.rename, Path.open",
        crash_point, action,
    ])
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if result.returncode != PROCESS_DIES:
        raise AssertionError(f"the process did not die where planned ({result.returncode}): {result.stderr[-800:]}")


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

    def write_chain(self, steps=("cv-draft-en", "cv-tailored-en", "cv-planned-en")):
        for step in steps:
            self.workspace.write(self.job, step, {"version": 1, "step": step})

    def read_chain(self, workspace, steps=("cv-draft-en", "cv-tailored-en", "cv-planned-en")):
        return [workspace.read(self.job, step) for step in steps]

    def test_a_step_cut_short_by_a_crash_is_undone_at_the_next_start(self):
        self.write_chain(("decided", "cv-draft-en", "cv-tailored-en", "cv-planned-en"))
        before = self.files()
        die_while(self.root, f"workspace.write({self.job!r}, 'cv-draft-en', {{'version': 2}})",
                  dies_before_renaming_into("cv-draft-en.json"))
        self.assertIn("change-in-progress.json", self.files())  # what a killed process leaves
        restarted = Workspace(self.root)  # the next start
        self.assertEqual(restarted.recover(), [{"job_id": self.job, "step": "cv-draft-en"}])
        self.assertEqual(self.read_chain(restarted), [{"version": 1, "step": step}
                                                      for step in ("cv-draft-en", "cv-tailored-en", "cv-planned-en")])
        self.assertEqual(restarted.read_note(self.job, "interrupted")["step"], "cv-draft-en")
        self.assertEqual(self.files(), sorted(before + ["interrupted.json"]))  # its temporary file is gone too
        restarted.write(self.job, "gaps", {"version": 1})  # other work keeps the note for the page
        self.assertEqual(restarted.read_note(self.job, "interrupted")["step"], "cv-draft-en")
        restarted.write(self.job, "decided", {"version": 2})  # redoing what it depends on clears it
        self.assertIsNone(restarted.read_note(self.job, "interrupted"))

    def test_a_crash_while_moving_the_old_steps_aside_is_undone(self):
        self.write_chain()
        before = self.files()
        die_while(self.root, f"workspace.write({self.job!r}, 'cv-draft-en', {{'version': 2}})",
                  dies_moving("cv-planned-en.json"))  # the draft and its rewording are aside already
        restarted = Workspace(self.root)
        self.assertEqual(restarted.recover(), [{"job_id": self.job, "step": "cv-draft-en"}])
        self.assertEqual(self.read_chain(restarted), [{"version": 1, "step": step}
                                                      for step in ("cv-draft-en", "cv-tailored-en", "cv-planned-en")])
        self.assertEqual(self.files(), sorted(before + ["interrupted.json"]))

    def test_a_crash_while_the_new_file_is_half_written_leaves_the_old_one(self):
        self.write_chain(("cv-draft-en",))
        before = self.files()
        die_while(self.root, f"workspace.write({self.job!r}, 'cv-draft-en', {{'version': 2}})",
                  dies_halfway_through_writing(".workbench-cv-draft-en.json."))
        self.assertTrue(any(name.startswith(".workbench-cv-draft-en.json.") for name in self.files()))
        restarted = Workspace(self.root)
        restarted.recover()
        self.assertEqual(restarted.read(self.job, "cv-draft-en"), {"version": 1, "step": "cv-draft-en"})
        self.assertEqual(self.files(), sorted(before + ["interrupted.json"]))

    def test_a_step_written_in_full_before_a_crash_stands(self):
        self.write_chain(("cv-draft-en", "cv-planned-en"))
        die_while(self.root, f"workspace.write({self.job!r}, 'cv-draft-en', {{'version': 2}})",
                  dies_after_renaming_into("cv-draft-en.json"))
        restarted = Workspace(self.root)
        self.assertEqual(restarted.recover(), [])
        self.assertEqual(restarted.read(self.job, "cv-draft-en"), {"version": 2})
        self.assertIsNone(restarted.read(self.job, "cv-planned-en"))  # moved to history, as the change meant
        self.assertIsNone(restarted.read_note(self.job, "interrupted"))
        self.assertNotIn("change-in-progress.json", self.files())

    def test_a_rollback_cut_short_is_finished_at_the_next_start(self):
        # Rewriting a step with the very same bytes, cut short, then a crash while putting the old
        # files back: the next start must finish putting them back, not take the change as done.
        self.write_chain(("cv-draft-en", "cv-tailored-en", "cv-planned-en", "cv-approved-en"))
        before = self.files()
        same = {"version": 1, "step": "cv-draft-en"}
        die_while(self.root, f"workspace.write({self.job!r}, 'cv-draft-en', {same!r})",
                  dies_before_renaming_into("cv-draft-en.json"))
        die_while(self.root, "workspace.recover()", dies_moving("cv-tailored-en.json"))  # draft back, rewording not
        restarted = Workspace(self.root)
        restarted.recover()
        chain = ("cv-draft-en", "cv-tailored-en", "cv-planned-en", "cv-approved-en")
        self.assertEqual(self.read_chain(restarted, chain), [{"version": 1, "step": step} for step in chain])
        self.assertEqual(self.files(), sorted(before + ["interrupted.json"]))

    def test_the_note_is_not_lost_to_a_crash_right_after_it_is_written(self):
        self.write_chain()
        die_while(self.root, f"workspace.write({self.job!r}, 'cv-draft-en', {{'version': 2}})",
                  dies_before_renaming_into("cv-draft-en.json"))
        die_while(self.root, "workspace.recover()", dies_after_renaming_into("interrupted.json"))
        restarted = Workspace(self.root)
        restarted.recover()
        self.assertEqual(restarted.read_note(self.job, "interrupted")["step"], "cv-draft-en")
        self.assertNotIn("change-in-progress.json", self.files())

    def test_a_retry_that_lands_just_before_a_crash_settles_the_old_note(self):
        self.write_chain()
        die_while(self.root, f"workspace.write({self.job!r}, 'cv-draft-en', {{'version': 2}})",
                  dies_before_renaming_into("cv-draft-en.json"))
        Workspace(self.root).recover()  # marked interrupted
        die_while(self.root, f"workspace.write({self.job!r}, 'cv-draft-en', {{'version': 3}})",
                  dies_after_renaming_into("cv-draft-en.json"))  # the retry is in place, then the crash
        restarted = Workspace(self.root)
        self.assertEqual(restarted.recover(), [])
        self.assertEqual(restarted.read(self.job, "cv-draft-en"), {"version": 3})
        self.assertIsNone(restarted.read_note(self.job, "interrupted"))

    def record_disk_operations(self):
        """Patches that log each folder flush, move, placement, removal and new folder, by name."""
        events = []
        real_sync, real_rename, real_replace = workspace_module._sync_folder, Path.rename, os.replace
        real_unlink, real_mkdir = Path.unlink, Path.mkdir

        def sync(folder):
            events.append(("flush", Path(folder).name))
            real_sync(folder)

        def rename(source, target):
            events.append(("move", source.name))
            return real_rename(source, target)

        def replace(source, destination):
            events.append(("place", Path(destination).name))
            real_replace(source, destination)

        def unlink(path, missing_ok=False):
            events.append(("remove", path.name))
            return real_unlink(path, missing_ok=missing_ok)

        def mkdir(path, *args, **kwargs):
            events.append(("make", path.name))
            return real_mkdir(path, *args, **kwargs)

        patches = [patch("workspace._sync_folder", sync), patch.object(Path, "rename", rename),
                   patch("workspace.os.replace", replace), patch.object(Path, "unlink", unlink),
                   patch.object(Path, "mkdir", mkdir)]
        return events, patches

    def assert_before(self, events, earlier, later):
        """The first ``later`` event comes after an ``earlier`` one."""
        self.assertIn(earlier, events)
        self.assertIn(later, events)
        self.assertLess(events.index(earlier), events.index(later), events)

    def test_each_phase_of_a_change_is_on_disk_before_the_next_begins(self):
        self.write_chain()
        events, patches = self.record_disk_operations()
        for active in patches:
            active.start()
        try:
            self.workspace.write(self.job, "cv-draft-en", {"version": 2})
        finally:
            for active in patches:
                active.stop()
        (stamp,) = [folder.name for folder in (self.root / self.job / "history").iterdir()]
        moves = [index for index, event in enumerate(events) if event[0] == "move"]
        placed, removed = events.index(("place", "cv-draft-en.json")), events.index(("remove", "change-in-progress.json"))
        flushes = lambda name: [index for index, event in enumerate(events) if event == ("flush", name)]
        # The journal, and the new history folder's name in its parent, are on disk before anything moves.
        self.assertTrue(any(events.index(("place", "change-in-progress.json")) < index < moves[0] for index in flushes(self.job)))
        self.assertTrue(any(events.index(("make", stamp)) < index < moves[0] for index in flushes("history")))
        # Both sides of the moves are on disk before the new file is placed.
        self.assertTrue(any(moves[-1] < index < placed for index in flushes(stamp)))
        self.assertTrue(any(moves[-1] < index < placed for index in flushes(self.job)))
        # The new file is on disk before the journal goes, and the journal's removal is flushed too.
        self.assertTrue(any(placed < index < removed for index in flushes(self.job)))
        self.assertEqual(events[-1], ("flush", self.job))

    def test_each_phase_of_undoing_a_change_is_on_disk_before_the_next_begins(self):
        self.write_chain(("gaps",))
        self.write_chain(("gaps",))  # an earlier change's history stays, so history/ itself is not removed
        self.write_chain()
        die_while(self.root, f"workspace.write({self.job!r}, 'cv-draft-en', {{'version': 2}})",
                  dies_before_renaming_into("cv-draft-en.json"))
        events, patches = self.record_disk_operations()
        for active in patches:
            active.start()
        try:
            Workspace(self.root).recover()
        finally:
            for active in patches:
                active.stop()
        moves = [index for index, event in enumerate(events) if event[0] == "move"]
        noted, removed = events.index(("place", "interrupted.json")), events.index(("remove", "change-in-progress.json"))
        flushes = [index for index, event in enumerate(events) if event == ("flush", self.job)]
        self.assertEqual(len(moves), 3)
        # Everything is back and on disk before the note, and the note is on disk before the journal goes.
        self.assertTrue(any(moves[-1] < index < noted for index in flushes))
        self.assertTrue(any(noted < index < removed for index in flushes))
        self.assertEqual(events[-1], ("flush", self.job))

    def test_a_step_that_cannot_be_written_leaves_the_job_as_it_was(self):
        self.write_chain(("cv-draft-en", "cv-planned-en"))
        before = self.files()
        real = os.replace

        def disk_full(source, destination):
            if Path(destination).name == "cv-draft-en.json":
                raise OSError(28, "No space left on device")
            real(source, destination)

        with patch("workspace.os.replace", disk_full), self.assertRaises(OSError):
            self.workspace.write(self.job, "cv-draft-en", {"version": 2})
        self.assertEqual(self.read_chain(self.workspace, ("cv-draft-en", "cv-planned-en")),
                         [{"version": 1, "step": "cv-draft-en"}, {"version": 1, "step": "cv-planned-en"}])
        self.assertEqual(self.files(), before)  # no note: the page already showed the error
        self.assertEqual(Workspace(self.root).recover(), [])

    def test_a_job_whose_creation_was_cut_short_disappears(self):
        die_while(self.root, "workspace.create_job({'jd': {'text': 'x'}})", dies_before_renaming_into("input.json"))
        restarted = Workspace(self.root)
        restarted.recover()
        self.assertEqual([job["job_id"] for job in restarted.jobs()], [self.job])

    def test_recovery_never_puts_a_file_back_over_one_that_is_there(self):
        # Two changes stamped alike share a history folder; undoing the one that moved nothing
        # must not bring back the other one's old file.
        self.workspace.write(self.job, "cv-draft-en", {"version": 1})
        self.workspace.write(self.job, "cv-draft-en", {"version": 2})  # version 1 goes to history
        (stamp,) = [folder.name for folder in (self.root / self.job / "history").iterdir()]
        journal = {"step": "cv-draft-en", "history": stamp, "moved": ["cv-draft-en.json"],
                   "sha256": "0" * 64, "started_at": "2026-09-29T00:00:00+00:00"}
        (self.root / self.job / "change-in-progress.json").write_text(json.dumps(journal), encoding="utf-8")
        Workspace(self.root).recover()
        self.assertEqual(self.workspace.read(self.job, "cv-draft-en"), {"version": 2})

    def test_a_moved_file_found_nowhere_stops_recovery_for_that_job(self):
        self.write_chain(("cv-draft-en",))
        journal = {"step": "cv-draft-en", "history": "20260929T000000000000-abcd", "moved": ["cv-planned-en.json"],
                   "sha256": "0" * 64, "started_at": "2026-09-29T00:00:00+00:00"}
        (self.root / self.job / "change-in-progress.json").write_text(json.dumps(journal), encoding="utf-8")
        before = self.files()
        restarted = Workspace(self.root)
        self.assertEqual(restarted.recover(), [{"job_id": self.job, "step": "unknown"}])
        self.assertEqual(self.files(), sorted([name for name in before if name != "change-in-progress.json"]
                                              + ["change-in-progress.unreadable.json", "interrupted.json"]))

    def test_a_journal_that_cannot_be_read_neither_stops_the_start_nor_moves_anything(self):
        self.write_chain(("cv-draft-en", "cv-planned-en"))
        before = self.files()
        (self.root / self.job / "change-in-progress.json").write_text('{"step": "../../x", "moved": ["../../y"]}')
        restarted = Workspace(self.root)
        self.assertEqual(restarted.recover(), [{"job_id": self.job, "step": "unknown"}])
        self.assertEqual(self.files(), sorted(before + ["change-in-progress.unreadable.json", "interrupted.json"]))
        restarted.write(self.job, "gaps", {"version": 1})  # the page's own requirement check keeps the note
        self.assertEqual(restarted.read_note(self.job, "interrupted")["step"], "unknown")
        restarted.write(self.job, "cv-draft-en", {"version": 2})  # starting the CV over settles it
        self.assertEqual(restarted.read(self.job, "cv-draft-en"), {"version": 2})
        self.assertIsNone(restarted.read_note(self.job, "interrupted"))

    def test_a_journal_whose_parts_disagree_is_set_aside_without_stopping_the_start(self):
        self.write_chain(("cv-draft-en",))
        target = (self.root / self.job / "cv-draft-en.json").read_bytes()
        cases = {
            "files moved but no history folder": {"step": "cv-draft-en", "history": None, "moved": ["cv-draft-en.json"]},
            "a file outside the step's chain": {"step": "gaps", "history": "20260929T000000000000-abcd",
                                                "moved": ["cv-draft-en.json"]},
            "a digest that is not a hash": {"step": "cv-draft-en", "history": None, "moved": [], "sha256": "not-a-hash"},
        }
        for label, parts in cases.items():
            with self.subTest(label):
                journal = {"sha256": hashlib.sha256(target).hexdigest(), "started_at": "2026-09-29T00:00:00+00:00", **parts}
                (self.root / self.job / "change-in-progress.json").write_text(json.dumps(journal), encoding="utf-8")
                self.assertEqual(Workspace(self.root).recover(), [{"job_id": self.job, "step": "unknown"}])
                self.assertEqual(self.workspace.read(self.job, "cv-draft-en"), {"version": 1, "step": "cv-draft-en"})
                (self.root / self.job / "change-in-progress.unreadable.json").unlink()

    def test_a_job_folder_that_is_a_link_is_left_alone(self):
        elsewhere = Path(self.directory.name + "-elsewhere")
        elsewhere.mkdir()
        self.addCleanup(lambda: [path.unlink() for path in elsewhere.iterdir()] and None or elsewhere.rmdir())
        (elsewhere / "cv-draft-en.json").write_text("{}")
        (elsewhere / "change-in-progress.json").write_text('{"step": "cv-draft-en"}')
        linked = self.root / "20260101-000000-abcdef"
        linked.symlink_to(elsewhere, target_is_directory=True)
        Workspace(self.root).recover()
        self.assertEqual(sorted(path.name for path in elsewhere.iterdir()), ["change-in-progress.json", "cv-draft-en.json"])
        self.assertEqual([job["job_id"] for job in self.workspace.jobs()], [self.job])
        with self.assertRaises(WorkspaceError):
            self.workspace.read("20260101-000000-abcdef", "cv-draft-en")

    def assert_nothing_moves_through(self, link):
        """A change whose history is reached through a link: the job is marked, nothing moves."""
        self.write_chain(("cv-draft-en",))
        stamp = "20260929T000000000000-abcd"
        elsewhere = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (elsewhere / stamp).mkdir()
        (elsewhere / stamp / "cv-draft-en.json").write_text("{}")
        folder = self.root / self.job
        (folder / "cv-draft-en.json").unlink()  # as if moved into a history that is elsewhere
        link(folder / "history", elsewhere, stamp)
        journal = {"step": "cv-draft-en", "history": stamp, "moved": ["cv-draft-en.json"],
                   "sha256": hashlib.sha256(b"new").hexdigest(), "started_at": "2026-09-29T00:00:00+00:00"}
        (folder / "change-in-progress.json").write_text(json.dumps(journal), encoding="utf-8")
        self.assertEqual(Workspace(self.root).recover(), [{"job_id": self.job, "step": "unknown"}])
        self.assertEqual(sorted(path.name for path in (elsewhere / stamp).iterdir()), ["cv-draft-en.json"])
        self.assertFalse((folder / "cv-draft-en.json").exists())

    def test_a_history_folder_that_is_a_link_is_left_alone(self):
        self.assert_nothing_moves_through(lambda history, elsewhere, stamp: history.symlink_to(elsewhere))

    def test_a_change_in_history_that_is_a_link_is_left_alone(self):
        def link(history, elsewhere, stamp):
            history.mkdir()
            (history / stamp).symlink_to(elsewhere / stamp)
        self.assert_nothing_moves_through(link)

    def test_cleaning_up_after_a_crash_touches_only_its_own_temporary_files(self):
        folder = self.root / self.job
        (folder / ".workbench-cv-draft-en.json.0badc0de.tmp").write_text("{")  # one of ours
        (folder / ".editor.tmp").write_text("someone else's")
        (folder / ".workbench-notes.json.0badc0de.tmp").mkdir()  # a folder, whatever its name
        Workspace(self.root).recover()
        self.assertEqual(sorted(path.name for path in folder.iterdir() if path.name.startswith(".")),
                         [".editor.tmp", ".workbench-notes.json.0badc0de.tmp"])

    def test_job_ids_and_step_names_cannot_leave_the_workspace(self):
        for job_id, step in (("../outside", "input"), (self.job, "../input"), (self.job, "notes")):
            with self.subTest(job_id=job_id, step=step):
                with self.assertRaises(WorkspaceError):
                    self.workspace.read(job_id, step)


class DataFormatTests(unittest.TestCase):
    """The data folder records the newest format a release has stored it in, so it travels with
    the data (and its backups), and no older release opens it afterwards."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.record = self.folder / DATA_FORMAT_FILE

    def tearDown(self):
        self.directory.cleanup()

    def test_a_start_records_the_format_it_stores_data_in(self):
        claim_data_format(self.folder)
        self.assertEqual(self.record.read_text(encoding="utf-8"), f"{DATA_FORMAT}\n")
        claim_data_format(self.folder)  # and again, changing nothing
        self.assertEqual(self.record.read_text(encoding="utf-8"), f"{DATA_FORMAT}\n")

    def test_data_in_a_newer_format_is_refused_and_left_as_it_is(self):
        self.record.write_text(f"{DATA_FORMAT + 1}\n", encoding="utf-8")
        with self.assertRaisesRegex(DataFormatError, f"format {DATA_FORMAT + 1}"):
            claim_data_format(self.folder)
        self.assertEqual(self.record.read_text(encoding="utf-8"), f"{DATA_FORMAT + 1}\n")

    def test_a_record_that_cannot_be_read_is_refused(self):
        self.record.write_text("two", encoding="utf-8")
        with self.assertRaises(DataFormatError):
            claim_data_format(self.folder)
        self.assertEqual(self.record.read_text(encoding="utf-8"), "two")


if __name__ == "__main__":
    unittest.main()
