"""Backups of the workbench's data folder, and checks that a folder is one the workbench can use.

    python backup.py create --data /data --out /backups/workbench.tar.gz   # with the app stopped
    python backup.py restore --archive /backups/workbench.tar.gz --into /restored/data
    python backup.py verify --data /restored/data

A backup is one .tar.gz: every file of the data folder under data/, and manifest.json with each
file's size and SHA-256, the application's revision and a few counts. The fact and listings
databases are copied through SQLite, read-only, so a copy is never torn; the rest is copied as it
is. The workbench must be stopped while a backup is made, so the databases and the job files
agree. Uploads not yet saved (they hold contact details and expire within a day) and a crash's
temporary files are left out.

Restoring writes only into a folder that is missing or empty, through a staging folder beside
it, and only after every file matches the manifest; an entry that is not a plain file inside
data/ refuses the whole archive. Output names files, jobs and counts, never a fact, a CV line
or contact details.
"""

import argparse
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cv import CVError, is_final_approval, verify_draft
from facts import FactStoreError, list_facts
from workspace import DATA_FORMAT, DATA_FORMAT_FILE, JOB_ID, JOURNAL, STEP_FILES, TEMPORARY, write_atomically


FORMAT = "workbench-backup-1"
LEFT_OUT = ("cv-uploads",)
DATABASES = ("workbench.db", "listings.db")
SQLITE_SIDE_FILES = ("-journal", "-wal", "-shm")


class BackupError(Exception):
    """A backup cannot be made, or an archive cannot be restored safely."""


def _kept(data: Path) -> list[PurePosixPath]:
    """The data folder's files a backup holds, as paths inside it."""
    kept = []
    for path in sorted(data.rglob("*")):
        relative = PurePosixPath(path.relative_to(data).as_posix())
        if relative.parts[0] in LEFT_OUT or TEMPORARY.fullmatch(path.name):
            continue
        if path.name.endswith(SQLITE_SIDE_FILES) and path.name.rsplit("-", 1)[0].endswith(".db"):
            continue  # the database copy is made through SQLite and complete by itself
        if path.is_symlink():
            raise BackupError(f"the data folder holds a link, which a backup does not follow: {relative}")
        if path.is_file():
            kept.append(relative)
    return kept


def _copy_database(source: Path, target: Path) -> None:
    """A consistent copy through SQLite, opened read-only so the data folder is never changed."""
    reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_backup(data: Path, archive: Path, revision: str | None = None) -> dict[str, Any]:
    """Pack the data folder into ``archive`` (written whole or not at all). Returns the
    manifest without its file list. Problems the copy has (see verify_data) are in it too: the
    archive is still the best copy there is, but it does not count as a backup that worked."""
    data, archive = Path(data), Path(archive)
    if not data.is_dir():
        raise BackupError(f"no data folder at {data}")
    with tempfile.TemporaryDirectory(prefix=".workbench-backup-", dir=archive.parent) as staging_name:
        staging = Path(staging_name)
        for relative in _kept(data):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative.name in DATABASES and len(relative.parts) == 1:
                _copy_database(data / relative, target)
            else:
                shutil.copyfile(data / relative, target)
        # Checked first: reading facts in an older layout updates the copy (as the app's next
        # start would), so the checksums must be of what the check leaves.
        checked = verify_data(staging)
        entries = [{"path": str(relative), "bytes": (staging / relative).stat().st_size,
                    "sha256": _sha256((staging / relative).read_bytes())} for relative in _kept(staging)]
        manifest = {"format": FORMAT, "data_format": DATA_FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                    "revision": revision or "unknown", "counts": checked["counts"], "problems": checked["problems"],
                    "files": entries}
        packed = io.BytesIO()
        with tarfile.open(fileobj=packed, mode="w:gz") as tar:
            encoded = json.dumps(manifest, indent=1).encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(encoded)
            tar.addfile(info, io.BytesIO(encoded))
            for entry in entries:
                tar.add(staging / entry["path"], arcname=f"data/{entry['path']}", recursive=False)
        write_atomically(archive, packed.getvalue())
    return {key: value for key, value in manifest.items() if key != "files"}


def _read_manifest(tar: tarfile.TarFile) -> tuple[dict[str, dict[str, Any]], int]:
    """The files the manifest lists, by path, and the data format the backup holds."""
    try:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    except (KeyError, AttributeError, ValueError) as exc:
        raise BackupError("the archive has no readable manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), list):
        raise BackupError("the archive is not a workbench backup this version can read")
    made_by = manifest.get("data_format", 1)  # backups from before the number existed are format 1
    if not isinstance(made_by, int) or made_by > DATA_FORMAT:
        raise BackupError(f"the archive holds data in format {made_by}, which this release (format {DATA_FORMAT}) "
                          "cannot read; restore it with the release that made it, or a later one")
    return {entry["path"]: entry for entry in manifest["files"]}, made_by


def restore_backup(archive: Path, into: Path) -> dict[str, Any]:
    """Unpack a backup into ``into``, which must be missing or empty. Nothing appears there
    unless every file matches the manifest. A backup made before the app recorded its data
    format gets the record the manifest names, so the restored data never looks new."""
    archive, into = Path(archive), Path(into)
    if into.exists() and (not into.is_dir() or any(into.iterdir())):
        raise BackupError(f"{into} is not empty; a backup is restored only into an empty folder")
    into.parent.mkdir(parents=True, exist_ok=True)
    staging = into.parent / f".workbench-restoring-{into.name}-{secrets.token_hex(4)}"
    try:
        with tarfile.open(archive, "r:gz") as tar:
            expected, made_by = _read_manifest(tar)
            seen = set()
            for member in tar.getmembers():
                if member.name == "manifest.json":
                    continue
                relative = PurePosixPath(member.name[len("data/"):]) if member.name.startswith("data/") else None
                if (not member.isfile() or relative is None or relative.is_absolute()
                        or not relative.parts or ".." in relative.parts):
                    raise BackupError(f"the archive holds an entry that is not a plain file inside data/: {member.name}")
                entry = expected.get(str(relative))
                content = tar.extractfile(member).read()
                if entry is None or entry["bytes"] != len(content) or entry["sha256"] != _sha256(content):
                    raise BackupError(f"{relative} does not match the manifest; the archive is damaged or was changed")
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                seen.add(str(relative))
            if seen != set(expected):
                raise BackupError("files the manifest lists are missing from the archive")
        if DATA_FORMAT_FILE not in seen:
            staging.mkdir(exist_ok=True)
            (staging / DATA_FORMAT_FILE).write_text(f"{made_by}\n", encoding="utf-8")
        if into.exists():
            into.rmdir()
        staging.rename(into)
    except (OSError, tarfile.TarError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise BackupError(f"the archive cannot be restored: {exc.__class__.__name__}") from exc
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"restored": len(seen), "into": str(into)}


def _database_is_whole(path: Path) -> bool:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


def verify_data(data: Path) -> dict[str, Any]:
    """Whether the workbench can use this data folder: whole databases, readable files, and
    approvals that still match their CV and the confirmed facts. Problems name files, never
    their contents; notes are things the next start takes care of."""
    data = Path(data)
    problems: list[str] = []
    notes: list[str] = []
    counts = {"facts": 0, "confirmed": 0, "jobs": 0, "approved": 0, "final_pdfs": 0}
    facts_db = data / "workbench.db"
    for name in DATABASES:
        if (data / name).exists() and not _database_is_whole(data / name):
            problems.append(f"{name}: the database is damaged")
    if facts_db.exists() and f"{facts_db.name}: the database is damaged" not in problems:
        try:
            facts = list_facts(facts_db)
            counts["facts"] = len(facts)
            counts["confirmed"] = sum(fact["status"] == "confirmed" for fact in facts)
        except (FactStoreError, sqlite3.DatabaseError):
            problems.append("workbench.db: the facts cannot be read")
    profile = data / "cv-profile.json"
    if profile.exists():
        try:
            if not isinstance(json.loads(profile.read_text(encoding="utf-8")), dict):
                problems.append("cv-profile.json: not a CV profile")
        except (OSError, ValueError):
            problems.append("cv-profile.json: not readable")
    jobs = data / "jobs"
    for job in sorted(jobs.iterdir()) if jobs.is_dir() else []:
        if not job.is_dir() or not JOB_ID.fullmatch(job.name):
            continue
        counts["jobs"] += 1
        if (job / JOURNAL).exists():
            notes.append(f"jobs/{job.name}: a change was cut short; the next start finishes or undoes it")
        for path in sorted(job.iterdir()):
            where = f"jobs/{job.name}/{path.name}"
            if path.name not in STEP_FILES:
                continue
            if path.suffix == ".pdf":
                if path.read_bytes()[:5] != b"%PDF-":
                    problems.append(f"{where}: not a PDF")
                else:
                    counts["final_pdfs"] += 1
                continue
            try:
                step = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                problems.append(f"{where}: not readable")
                continue
            if not path.name.startswith("cv-approved-"):
                continue
            counts["approved"] += 1
            try:
                if not is_final_approval(step):
                    problems.append(f"{where}: the approval is missing")
                    continue
            except CVError:
                problems.append(f"{where}: the approval does not match the CV")
                continue
            try:
                verify_draft(step, facts_db)
            except (CVError, FactStoreError, sqlite3.DatabaseError):
                problems.append(f"{where}: the approved CV no longer rests on the confirmed facts")
    return {"counts": counts, "problems": problems, "notes": notes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="pack the data folder (with the workbench stopped)")
    create.add_argument("--data", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    create.add_argument("--revision", default=os.environ.get("WORKBENCH_REVISION"))
    restore = commands.add_parser("restore", help="unpack a backup into an empty folder")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--into", type=Path, required=True)
    verify = commands.add_parser("verify", help="check that the workbench can use a data folder")
    verify.add_argument("--data", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_backup(args.data, args.out, args.revision)
        elif args.command == "restore":
            result = restore_backup(args.archive, args.into)
        else:
            result = verify_data(args.data)
    except BackupError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1))
    return 1 if result.get("problems") else 0


if __name__ == "__main__":
    sys.exit(main())
