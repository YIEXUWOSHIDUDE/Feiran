"""Backups of V2's data folder: the database and the deletion ledger.

    python v2_backup.py create --data /data --out /backups/feiran-v2.tar.gz
    python v2_backup.py restore --archive /backups/feiran-v2.tar.gz --into /restored/data --ledger /data/deleted-accounts.jsonl
    python v2_backup.py verify --data /restored/data

Everything V2 keeps is in one SQLite database (uploads waiting for review, versions, approvals and
final PDFs included), so SQLite's online backup copies one consistent moment even while the app
runs; no pause of the task runner is needed for consistency. A task running at that moment is in
the copy as running and becomes "interrupted" when the copy is restored: it is never rerun or
published by itself. Sign-in sessions are left out of the copy.

A restore writes only into a missing or empty folder, after every file matches the manifest and
the database checks out. It then deletes again every account the deletion ledger lists (the one
in the archive and the live one given with --ledger), so an older backup never reopens a deleted
account. A V2 data folder always holds its ledger (the app creates it, empty until an account is
deleted, and rebuilds it from the database if it goes missing), so a live ledger that is not
there means a wrong path or a lost volume: the restore refuses. Only when the ledger is really
lost is --without-live-ledger the way on; accounts deleted after the backup then come back and
must be deleted again by hand. Output names files and counts, never CV text.
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import tenant_store
from cv import CVError, content_fingerprint, is_final_approval
from workspace import write_atomically

FORMAT = "feiran-v2-backup-1"
DATABASE = tenant_store.DATABASE
LEDGER = tenant_store.DELETION_LEDGER
MEMBERS = {f"data/{DATABASE}", f"data/{LEDGER}"}


class BackupError(Exception):
    """A backup cannot be made, or an archive cannot be restored safely."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def copy_database(source: Path, target: Path) -> None:
    """A consistent copy through SQLite's backup API; the source is opened read-only."""
    reader = sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
            writer.execute("PRAGMA journal_mode = DELETE")  # one self-contained file, no -wal beside it
        finally:
            writer.close()
    finally:
        reader.close()


def _without_sessions(path: Path) -> None:
    """Sessions and sign-ins in progress are not data worth keeping and are ended by a restore anyway."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA secure_delete = ON")
        with connection:
            connection.execute("DELETE FROM sessions")
            connection.execute("DELETE FROM login_attempts")
        connection.execute("VACUUM")
    finally:
        connection.close()


def verify_database(path: Path) -> dict[str, Any]:
    """Whether V2 can use this database: whole, its links consistent, every stored PDF matching
    its hash and every approval matching the CV it approves. Problems name tables and IDs only."""
    problems: list[str] = []
    counts: dict[str, Any] = {}
    try:
        connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {"counts": counts, "problems": ["the database cannot be opened"]}
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return {"counts": counts, "problems": ["the database is damaged"]}
        if (connection.execute("PRAGMA application_id").fetchone()[0] != tenant_store.APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != tenant_store.SCHEMA_VERSION):
            return {"counts": counts, "problems": ["not a V2 database in this release's format"]}
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            problems.append("some rows point at rows that are not there")
        counts["accounts"] = connection.execute("SELECT COUNT(*) FROM users WHERE deleted_at IS NULL").fetchone()[0]
        counts["deleted_accounts"] = connection.execute("SELECT COUNT(*) FROM users WHERE deleted_at IS NOT NULL").fetchone()[0]
        for table in ("facts", "jobs", "materials", "approvals", "artifacts", "uploads"):
            counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        counts["tasks"] = {row[0]: row[1] for row in connection.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")}
        for row in connection.execute("SELECT artifact_id, data, sha256 FROM artifacts"):
            if _sha256(bytes(row["data"])) != row["sha256"]:
                problems.append(f"final PDF {row['artifact_id']} does not match its hash")
        for row in connection.execute("""SELECT a.approval_id, a.payload, a.content_sha256, m.content_sha256 AS material
                FROM approvals a JOIN materials m ON m.user_id = a.user_id AND m.material_id = a.material_id"""):
            try:
                document = json.loads(row["payload"])
                whole = is_final_approval(document) and content_fingerprint(document) == row["content_sha256"] == row["material"]
            except (CVError, ValueError):
                whole = False
            if not whole:
                problems.append(f"approval {row['approval_id']} does not match the CV it approves")
    except sqlite3.DatabaseError:
        problems.append("the database cannot be read")
    finally:
        connection.close()
    return {"counts": counts, "problems": problems}


def create_backup(data: Path, archive: Path, revision: str | None = None) -> dict[str, Any]:
    """Pack the V2 database and the deletion ledger into ``archive`` (written whole or not at
    all); returns the manifest without its file list."""
    data, archive = Path(data), Path(archive)
    if not (data / DATABASE).is_file():
        raise BackupError(f"no V2 database in {data}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".feiran-v2-backup-", dir=archive.parent) as staging_name:
        staging = Path(staging_name)
        copy_database(data / DATABASE, staging / DATABASE)
        _without_sessions(staging / DATABASE)
        names = [DATABASE]
        if (data / LEDGER).is_file():
            tenant_store.read_deletions(data / LEDGER)  # an unreadable ledger stops the backup
            shutil.copyfile(data / LEDGER, staging / LEDGER)
            names.append(LEDGER)
        checked = verify_database(staging / DATABASE)
        entries = [{"path": name, "bytes": (staging / name).stat().st_size, "sha256": _sha256((staging / name).read_bytes())}
                   for name in names]
        manifest = {"format": FORMAT, "schema_version": tenant_store.SCHEMA_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(), "revision": revision or "unknown",
                    "counts": checked["counts"], "problems": checked["problems"], "files": entries}
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


def _unpack(archive: Path, into: Path) -> dict[str, Any]:
    """Unpack into ``into`` (an empty folder) after checking every member against the manifest;
    anything but the expected plain files refuses the whole archive."""
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            members = tar.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise BackupError("the archive has no manifest or repeats a file")
            manifest = json.loads(tar.extractfile("manifest.json").read())
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise BackupError("this is not a V2 backup")
            if manifest.get("schema_version") != tenant_store.SCHEMA_VERSION:
                raise BackupError("this backup is in another database format; restore it with a release that reads it")
            expected = {f"data/{entry['path']}": entry for entry in manifest.get("files", [])}
            for member in members:
                if member.name == "manifest.json":
                    continue
                if (member.name not in MEMBERS or member.name not in expected or not member.isfile()
                        or PurePosixPath(member.name).is_absolute() or ".." in PurePosixPath(member.name).parts):
                    raise BackupError(f"unexpected entry in the archive: {member.name}")
                content = tar.extractfile(member).read()
                entry = expected.pop(member.name)
                if len(content) != entry["bytes"] or _sha256(content) != entry["sha256"]:
                    raise BackupError(f"{member.name} does not match the manifest")
                target = into / PurePosixPath(member.name).name
                with open(target, "xb") as handle:
                    handle.write(content)
            if expected:
                raise BackupError("the archive lacks files its manifest lists")
    except (tarfile.TarError, OSError, ValueError, KeyError, TypeError) as exc:
        raise BackupError("the archive cannot be read") from exc
    return manifest


def inspect_archive(archive: Path) -> dict[str, Any]:
    """Check an archive without restoring it: manifest, hashes and the database itself."""
    with tempfile.TemporaryDirectory(prefix="feiran-v2-inspect-") as scratch:
        manifest = _unpack(Path(archive), Path(scratch))
        checked = verify_database(Path(scratch) / DATABASE)
    return {"created_at": manifest.get("created_at"), "revision": manifest.get("revision"), **checked}


def _live_deletions(ledger: Path) -> list[dict[str, str]]:
    """The live ledger a restore was told to honour: it must be there and readable, since a
    missing one would silently reopen every account deleted after the backup was made."""
    if not ledger.is_file():
        raise BackupError(f"the live deletion ledger {ledger} is not there; give the right path, or "
                          "--without-live-ledger only if the ledger is really lost")
    try:
        return tenant_store.read_deletions(ledger)
    except (tenant_store.StoreError, OSError) as exc:
        raise BackupError(f"the live deletion ledger {ledger} cannot be read: {exc}") from exc


def restore_backup(archive: Path, into: Path, *, live_ledger: Path | None) -> dict[str, Any]:
    """Restore into a missing or empty folder. Accounts deleted according to the archive's or
    the live deletion ledger are deleted again; sessions are ended; tasks that were running are
    marked interrupted. The folder is then claimed for V2's format. ``live_ledger`` None is the
    explicit choice to restore without the live ledger (it is lost)."""
    archive, into = Path(archive), Path(into)
    if into.exists() and any(into.iterdir()):
        raise BackupError(f"{into} is not empty; restore into a new folder")
    live = _live_deletions(Path(live_ledger)) if live_ledger is not None else []
    into.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".feiran-v2-restore-", dir=into.parent))
    try:
        manifest = _unpack(archive, staging)
        checked = verify_database(staging / DATABASE)
        if checked["problems"]:
            raise BackupError("the restored database does not check out: " + "; ".join(checked["problems"]))
        records = tenant_store.read_deletions(staging / LEDGER)
        known = {(record["user_id"], record["issuer"], record["subject"]) for record in records}
        records += [record for record in live if (record["user_id"], record["issuer"], record["subject"]) not in known]
        deleted_again = tenant_store.apply_deletions(staging / DATABASE, records)
        (staging / LEDGER).unlink(missing_ok=True)
        for record in records:
            tenant_store.append_deletion(staging / LEDGER, record)
        tenant_store.ensure_ledger(staging / DATABASE, staging / LEDGER)  # there even with nothing deleted
        with tenant_store.transaction(staging / DATABASE, write=True) as connection:
            connection.execute("DELETE FROM sessions")
            connection.execute("DELETE FROM login_attempts")
        interrupted = tenant_store.recover_tasks(staging / DATABASE)
        connection = sqlite3.connect(staging / DATABASE)
        try:
            connection.execute("PRAGMA journal_mode = WAL")  # as a new V2 database runs
        finally:
            connection.close()
        write_atomically(staging / ".workbench-format", b"2\n")
        after = verify_database(staging / DATABASE)
        if after["problems"]:
            raise BackupError("the database does not check out after restoring: " + "; ".join(after["problems"]))
        if into.exists():
            into.rmdir()
        staging.rename(into)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"restored_from": manifest.get("created_at"), "revision": manifest.get("revision"),
            "deleted_again": deleted_again, "deletions_known": len(records), "interrupted_tasks": interrupted,
            "counts": after["counts"], "problems": after["problems"]}


def verify_data(data: Path) -> dict[str, Any]:
    data = Path(data)
    result = verify_database(data / DATABASE)
    if not (data / LEDGER).is_file():
        result["problems"].append("the deletion ledger is missing")
        return result
    try:
        result["counts"]["deletions_in_ledger"] = len(tenant_store.read_deletions(data / LEDGER))
    except (tenant_store.StoreError, OSError):
        result["problems"].append("the deletion ledger cannot be read")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="back up the V2 database (the app may keep running)")
    create.add_argument("--data", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    create.add_argument("--revision", default=os.environ.get("WORKBENCH_REVISION"))
    restore = commands.add_parser("restore", help="restore into a new, empty folder")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--into", type=Path, required=True)
    ledger = restore.add_mutually_exclusive_group(required=True)
    ledger.add_argument("--ledger", type=Path,
                        help="the live deletion ledger (DATA/deleted-accounts.jsonl); it must exist")
    ledger.add_argument("--without-live-ledger", action="store_true",
                        help="only when the live ledger is really lost: accounts deleted after this backup "
                             "would return and must be deleted again by hand")
    verify = commands.add_parser("verify", help="check that V2 can use a data folder")
    verify.add_argument("--data", type=Path, required=True)
    inspect = commands.add_parser("inspect", help="check an archive without restoring it")
    inspect.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_backup(args.data, args.out, args.revision)
        elif args.command == "restore":
            result = restore_backup(args.archive, args.into, live_ledger=None if args.without_live_ledger else args.ledger)
        elif args.command == "inspect":
            result = inspect_archive(args.archive)
        else:
            result = verify_data(args.data)
    except (BackupError, tenant_store.StoreError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1))
    return 1 if result.get("problems") else 0


if __name__ == "__main__":
    sys.exit(main())
