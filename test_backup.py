import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backup import BackupError, create_backup, main, restore_backup, verify_data
from cv import approve_draft, build_draft
from facts import confirm_facts, list_facts, revise_fact
from listings import initialize
from test_cv import PROFILE, make_store
from workspace import DATA_FORMAT, Workspace


INPUT = {"jd": {"text": "Requirements:\n- Python", "title": "Backend Intern", "company": "Example Co",
                "source": None, "captured_at": "2026-09-29T00:00:00+00:00", "provider": "manual"}}


def data_folder(root: Path) -> tuple[Path, str]:
    """A folder like the data volume: facts, listings, a CV profile, and a job with an approved CV
    and its final PDF; also a pending upload and a crash's leftover, which a backup leaves out."""
    data = root / "data"
    data.mkdir()
    (data / ".workbench-data").write_text("")
    database = make_store(data)
    initialize(data / "listings.db", [])
    (data / "cv-profile.json").write_text(json.dumps(PROFILE, ensure_ascii=False), encoding="utf-8")
    workspace = Workspace(data / "jobs")
    job = workspace.create_job(INPUT)
    draft = build_draft(PROFILE, database, "en")
    workspace.write(job, "cv-draft-en", draft)
    workspace.write(job, "cv-approved-en", approve_draft(draft, database))
    workspace.write_bytes(job, "cv-final-en", b"%PDF-1.4 final\n%%EOF\n")
    (data / "cv-uploads").mkdir()
    (data / "cv-uploads" / "0123456789abcdef.json").write_text('{"private": {"email": "alex@example.com"}}')
    (data / ".workbench-cv-profile.json.0badc0de.tmp").write_text("{")
    return data, job


def files(folder: Path) -> list[str]:
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())


def repacked(archive: Path, change) -> bytes:
    """The archive with ``change(name, data)`` applied to each entry's bytes, entries kept."""
    out = io.BytesIO()
    with tarfile.open(archive, "r:gz") as source, tarfile.open(fileobj=out, mode="w:gz") as target:
        for member in source.getmembers():
            data = change(member.name, source.extractfile(member).read())
            member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    return out.getvalue()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data, self.job = data_folder(self.root)
        self.archive = self.root / "backups" / "workbench.tar.gz"
        self.archive.parent.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def test_a_backup_restored_into_an_empty_folder_is_the_same_workbench(self):
        made = create_backup(self.data, self.archive, revision="abc123")
        restored = self.root / "restored"
        restore_backup(self.archive, restored)
        report = verify_data(restored)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["counts"], {"facts": 4, "confirmed": 4, "jobs": 1, "approved": 1, "final_pdfs": 1})
        self.assertEqual((made["revision"], made["counts"], made["data_format"]), ("abc123", report["counts"], DATA_FORMAT))
        self.assertEqual(list_facts(restored / "workbench.db"), list_facts(self.data / "workbench.db"))
        kept = [name for name in files(self.data) if not name.startswith(("cv-uploads", ".workbench-cv-profile"))]
        self.assertEqual(files(restored), kept)
        for name in kept:
            if not name.endswith(".db"):  # databases are copied through SQLite, so compared by their facts above
                self.assertEqual((restored / name).read_bytes(), (self.data / name).read_bytes(), name)

    def test_a_backup_leaves_out_pending_uploads_and_a_crash_s_temporary_files(self):
        create_backup(self.data, self.archive)
        with tarfile.open(self.archive, "r:gz") as archive:
            names = archive.getnames()
            manifest = archive.extractfile("manifest.json").read().decode("utf-8")
        self.assertFalse([name for name in names if "cv-uploads" in name or name.endswith(".tmp")], names)
        self.assertNotIn("alex@example.com", manifest)  # the manifest names files and counts, nothing personal

    def test_restoring_never_writes_into_a_folder_that_holds_anything(self):
        create_backup(self.data, self.archive)
        occupied = self.root / "occupied"
        occupied.mkdir()
        (occupied / "workbench.db").write_text("someone's facts")
        with self.assertRaisesRegex(BackupError, "not empty"):
            restore_backup(self.archive, occupied)
        self.assertEqual(files(occupied), ["workbench.db"])
        self.assertEqual((occupied / "workbench.db").read_text(), "someone's facts")

    def test_a_changed_archive_is_refused_and_leaves_nothing_behind(self):
        create_backup(self.data, self.archive)
        self.archive.write_bytes(repacked(self.archive, lambda name, data: data.replace(b"Alex", b"Alix")
                                          if name == "data/cv-profile.json" else data))
        with self.assertRaisesRegex(BackupError, "does not match"):
            restore_backup(self.archive, self.root / "restored")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["backups", "data"])

    def test_an_archive_in_a_newer_data_format_is_refused(self):
        # Made by a release that stores data in a way this one cannot read.
        create_backup(self.data, self.archive)
        self.archive.write_bytes(repacked(self.archive, lambda name, data: json.dumps(
            {**json.loads(data), "data_format": DATA_FORMAT + 1}).encode("utf-8") if name == "manifest.json" else data))
        with self.assertRaisesRegex(BackupError, "cannot read"):
            restore_backup(self.archive, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_an_archive_missing_a_file_it_lists_is_refused(self):
        create_backup(self.data, self.archive)
        out = io.BytesIO()
        with tarfile.open(self.archive, "r:gz") as source, tarfile.open(fileobj=out, mode="w:gz") as target:
            for member in source.getmembers():
                if member.name != "data/cv-profile.json":
                    target.addfile(member, source.extractfile(member))
        self.archive.write_bytes(out.getvalue())
        with self.assertRaisesRegex(BackupError, "missing"):
            restore_backup(self.archive, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_an_archive_that_reaches_outside_its_folder_or_holds_a_link_is_refused(self):
        create_backup(self.data, self.archive)
        for label, entry in (("outside", tarfile.TarInfo("data/../escaped.json")),
                             ("link", tarfile.TarInfo("data/link.json"))):
            with self.subTest(label):
                out = io.BytesIO()
                with tarfile.open(self.archive, "r:gz") as source, tarfile.open(fileobj=out, mode="w:gz") as target:
                    for member in source.getmembers():
                        target.addfile(member, source.extractfile(member))
                    if label == "link":
                        entry.type, entry.linkname = tarfile.SYMTYPE, "/etc/passwd"
                        target.addfile(entry)
                    else:
                        entry.size = 2
                        target.addfile(entry, io.BytesIO(b"{}"))
                bad = self.root / f"{label}.tar.gz"
                bad.write_bytes(out.getvalue())
                with self.assertRaises(BackupError):
                    restore_backup(bad, self.root / f"restored-{label}")
                self.assertFalse((self.root / "escaped.json").exists())
                self.assertFalse((self.root / f"restored-{label}").exists())

    def test_verifying_finds_an_approval_whose_cv_changed_since(self):
        approved = self.data / "jobs" / self.job / "cv-approved-en.json"
        content = json.loads(approved.read_text(encoding="utf-8"))
        content["header"]["name"] = "Someone Else"
        approved.write_text(json.dumps(content), encoding="utf-8")
        report = verify_data(self.data)
        self.assertEqual(report["problems"], [f"jobs/{self.job}/cv-approved-en.json: the approval does not match the CV"])

    def test_verifying_finds_an_approved_cv_whose_facts_changed_since(self):
        revise_fact(self.data / "workbench.db", "fact-intern-api", text="Built REST APIs for a billing tool.")
        confirm_facts(self.data / "workbench.db", [("fact-intern-api", 2)])
        report = verify_data(self.data)
        self.assertEqual(report["problems"],
                         [f"jobs/{self.job}/cv-approved-en.json: the approved CV no longer rests on the confirmed facts"])
        self.assertNotIn("fact-intern-api", json.dumps(report))  # a fact ID can hold an employer's name

    def test_verifying_finds_a_damaged_database(self):
        (self.data / "listings.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
        self.assertEqual(verify_data(self.data)["problems"], ["listings.db: the database is damaged"])

    def test_a_change_a_crash_cut_short_is_noted_for_the_next_start(self):
        (self.data / "jobs" / self.job / "change-in-progress.json").write_text("{}")
        report = verify_data(self.data)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["notes"], [f"jobs/{self.job}: a change was cut short; the next start finishes or undoes it"])

    def test_the_command_line_prints_counts_and_fails_on_problems(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(main(["create", "--data", str(self.data), "--out", str(self.archive)]), 0)
            self.assertEqual(main(["restore", "--archive", str(self.archive), "--into", str(self.root / "restored")]), 0)
            self.assertEqual(main(["verify", "--data", str(self.root / "restored")]), 0)
        (self.root / "restored" / "cv-profile.json").write_text("{not json")
        with patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["verify", "--data", str(self.root / "restored")]), 1)
        self.assertNotIn("Alex", out.getvalue())

    def test_a_backup_of_data_with_problems_is_kept_but_does_not_count_as_one_that_worked(self):
        (self.data / "cv-profile.json").write_text("{not json")
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(main(["create", "--data", str(self.data), "--out", str(self.archive)]), 1)
        self.assertTrue(self.archive.exists())  # still the best copy there is
        self.assertIn("cv-profile.json: not readable", out.getvalue())
        with tarfile.open(self.archive, "r:gz") as archive:
            manifest = json.loads(archive.extractfile("manifest.json").read())
        self.assertEqual(manifest["problems"], ["cv-profile.json: not readable"])

    def test_a_facts_database_in_an_older_layout_restores(self):
        # Checking the copy updates an older layout, as the app's next start would; the archive
        # must hold the bytes its manifest describes, or it could never be restored.
        older = self.root / "older"
        older.mkdir()
        connection = sqlite3.connect(older / "workbench.db")
        connection.executescript("""
            CREATE TABLE facts (fact_id TEXT PRIMARY KEY, current_version INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE fact_versions (fact_id TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, confirmed_at TEXT, PRIMARY KEY (fact_id, version));
            INSERT INTO facts VALUES ('fact-legacy', 1, '2026-01-01T00:00:00+00:00');
            INSERT INTO fact_versions VALUES ('fact-legacy', 1, 'Built a parser', 'confirmed',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00');
            PRAGMA user_version = 1;
        """)
        connection.close()
        made = create_backup(older, self.archive)
        restore_backup(self.archive, self.root / "restored")
        self.assertEqual((made["counts"]["facts"], made["problems"]), (1, []))
        self.assertEqual([fact["text"] for fact in list_facts(self.root / "restored" / "workbench.db")], ["Built a parser"])


if __name__ == "__main__":
    unittest.main()
