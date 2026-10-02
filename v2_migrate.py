"""Move one single-user workspace (V1) into the multi-user V2 database, for one named owner.

    python v2_migrate.py run --v1-data DIR --v2-db DIR/v2.db --owner-issuer ISSUER --owner-subject SUB [--dry-run]
    python v2_migrate.py rollback --data DIR --backup-out ARCHIVE

This is an offline tool, never a route of the app: stop the workbench first. The owner is named
explicitly by the Cognito identity (issuer and subject) that will sign in to V2; the data is
never given to "whoever registers first". The V1 files are only read (SQLite opened read-only)
and stay as they are, so the V1 folder and its backups remain the way back.

What moves: every fact version with its confirmation, each language's CV profile with its
history, each job's description, requirements, gap check, notes and the current CV chain (draft,
reworded, adjusted), the followed companies and the public postings. An approval moves only if
it still holds exactly as the single-user app would accept it (same content, current facts,
current profile); otherwise the CV waits for review in V2 and the final PDF is not carried over.
Nothing is ever approved or confirmed by the migration. Left behind, and listed in the report:
unsaved CV uploads, notices of unfinished actions, talking-point matches and each job's history
folder (they stay in the V1 folder). The report names IDs, files and counts, never CV text.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tenant_store
from cv import (CVError, LANGUAGES, _count_pages, _profile_hash, content_fingerprint, is_final_approval,
                profile_fact_ids, profile_languages, verify_draft)
from facts import FactStoreError, normalize_fact_import
from review import build_report
from tenant_store import FactSnapshot, StoreError, UserWorkspace
from workspace import DATA_FORMAT_FILE, JOB_ID, JOURNAL, UNREADABLE_JOURNAL, recorded_data_format

PROFILE = "cv-profile"
HISTORY_NAME = re.compile(r"(cv-profile(?:\.(?:en|zh))?)-(\d{8}T\d{12})\.json")
CHAIN = ("draft", "tailored", "planned")


class MigrationError(Exception):
    """The migration was refused; nothing was written."""


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _mtime(path: Path) -> str:
    return _stamp(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _v1_facts(data: Path) -> dict[str, dict[str, Any]]:
    """Every V1 fact with all its versions, read without changing the database."""
    path = data / "workbench.db"
    if not path.exists():
        return {}
    connection = _read_only(path)
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != 0 or \
                connection.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise MigrationError("workbench.db is not a single-user fact database in format 2; start the V1 app on "
                                 "this folder once (it updates the format), then migrate")
        facts = {}
        for row in connection.execute("SELECT fact_id, current_version, created_at FROM facts ORDER BY created_at, fact_id"):
            facts[row["fact_id"]] = {"current": row["current_version"], "created_at": row["created_at"], "versions": {}}
        for row in connection.execute("SELECT * FROM fact_versions ORDER BY fact_id, version"):
            tags = [tag["tag"] for tag in connection.execute(
                "SELECT tag FROM fact_tags WHERE fact_id = ? AND version = ? ORDER BY tag_key",
                (row["fact_id"], row["version"]))]
            facts[row["fact_id"]]["versions"][row["version"]] = {
                "text": row["text"], "fact_type": row["fact_type"], "tags": tags, "status": row["status"],
                "created_at": row["created_at"], "confirmed_at": row["confirmed_at"]}
        return facts
    except sqlite3.DatabaseError as exc:
        raise MigrationError("workbench.db cannot be read") from exc
    finally:
        connection.close()


def _profile_versions(data: Path, report: dict) -> dict[str, list[tuple[dict, str, str]]]:
    """Each language's profile versions, oldest first: (profile, created_at, file name). The
    current one is the file the single-user app reads for that language (web.py profile_file)."""
    found: dict[str, list[tuple[dict, str, str]]] = {language: [] for language in LANGUAGES}

    def read(path: Path) -> dict | None:
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report["skipped"].append({"file": path.name, "reason": "not readable"})
            return None
        return profile if isinstance(profile, dict) else None

    history = data / "profile-history"
    for path in sorted(history.glob("*.json") if history.is_dir() else [],
                       key=lambda item: (HISTORY_NAME.fullmatch(item.name) or [None, "", item.name])[2]):
        match = HISTORY_NAME.fullmatch(path.name)
        profile = read(path) if match else None
        if profile is None:
            continue
        moment = datetime.strptime(match[2], "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)
        for language in _languages_of(profile, match[1]):
            found[language].append((profile, _stamp(moment), f"profile-history/{path.name}"))
    base = data / f"{PROFILE}.json"
    for language in LANGUAGES:
        separate = data / f"{PROFILE}.{language}.json"
        current = separate if separate.exists() else base if base.exists() else None
        profile = read(current) if current else None
        if profile is not None and language in _languages_of(profile, current.stem):
            found[language].append((profile, _mtime(current), current.name))
    return found


def _languages_of(profile: dict, stem: str) -> list[str]:
    """The languages a profile file is the CV for: its own name's languages, and for a
    per-language file (cv-profile.en.json) that language only."""
    written = profile_languages(profile)
    if stem.startswith(f"{PROFILE}."):
        language = stem.split(".", 1)[1]
        return [language] if language in written else []
    return written


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _job_created_at(job_id: str, folder: Path) -> str:
    try:
        return _stamp(datetime.strptime(job_id[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc))
    except ValueError:
        return _mtime(folder)


def preflight(data: Path) -> None:
    """Refuse a V1 folder the migration cannot read as one consistent state."""
    data = Path(data)
    if not data.is_dir():
        raise MigrationError(f"no V1 data folder at {data}")
    if recorded_data_format(data) > 1:
        raise MigrationError(f"{DATA_FORMAT_FILE} says this folder is already in a newer format")
    jobs = data / "jobs"
    for folder in sorted(jobs.iterdir()) if jobs.is_dir() else []:
        if folder.is_dir() and ((folder / JOURNAL).exists() or (folder / UNREADABLE_JOURNAL).exists()):
            raise MigrationError(f"jobs/{folder.name} has an unfinished change; start the V1 app once so it finishes "
                                 "or undoes it, then migrate")


def migrate(v1_data: Path, v2_db: Path, *, issuer: str, subject: str) -> dict[str, Any]:
    """Migrate into ``v2_db`` (created if missing). Refused, with nothing written, when the owner
    identity already has a V2 account or the V1 folder is not in a consistent state."""
    v1_data, v2_db = Path(v1_data), Path(v2_db)
    preflight(v1_data)
    report: dict[str, Any] = {"skipped": [], "approvals_not_carried": [], "not_migrated": {}}
    facts = _v1_facts(v1_data)
    profiles = _profile_versions(v1_data, report)
    created = not v2_db.exists()
    tenant_store.initialize_store(v2_db)
    try:
        with tenant_store.transaction(v2_db) as connection:
            if connection.execute("SELECT 1 FROM users WHERE issuer = ? AND subject = ?", (issuer, subject)).fetchone():
                raise MigrationError("this owner already has a V2 account; nothing was migrated")
        owner = tenant_store.provision_user(v2_db, issuer=issuer, subject=subject)
        try:
            with tenant_store.transaction(v2_db, write=True) as connection:
                _write(connection, owner, v1_data, facts, profiles, report)
        except BaseException:
            # Nothing of the owner's data was committed; the empty account is removed again.
            with tenant_store.transaction(v2_db, write=True) as connection:
                connection.execute("DELETE FROM users WHERE user_id = ?", (owner,))
            raise
    except BaseException:
        if created:  # an empty new database must not look like a finished migration
            for suffix in ("", "-wal", "-shm"):
                Path(f"{v2_db}{suffix}").unlink(missing_ok=True)
        raise
    report["owner_user_id"] = owner
    report["verified"] = _verify(v2_db, owner, report)
    tenant_store.ensure_ledger(v2_db, v2_db.parent / tenant_store.DELETION_LEDGER)
    return report


def _write(connection: sqlite3.Connection, owner: str, data: Path, facts: dict, profiles: dict, report: dict) -> None:
    # Facts: every version as it was, confirmations included.
    for fact_id, fact in facts.items():
        connection.execute("INSERT INTO facts VALUES (?, ?, ?, ?)", (owner, fact_id, fact["current"], fact["created_at"]))
        for version, item in sorted(fact["versions"].items()):
            try:
                _, text, kind, tags = normalize_fact_import([{"id": fact_id, "text": item["text"],
                                                              "type": item["fact_type"], "tags": item["tags"]}])[0]
            except FactStoreError as exc:
                raise MigrationError(f"fact {fact_id} version {version} is not valid: {exc}") from exc
            payload = tenant_store._fact_payload(text, kind, tags)
            connection.execute("INSERT INTO fact_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (owner, fact_id, version, payload, item["status"], item["created_at"], item["confirmed_at"]))
    report["facts"] = len(facts)
    report["fact_versions"] = sum(len(fact["versions"]) for fact in facts.values())
    current_facts = FactSnapshot([
        {**fact["versions"][fact["current"]], "id": fact_id, "version": fact["current"]} for fact_id, fact in facts.items()])

    # Profiles: each language's versions, oldest first; a version whose facts are gone is left out.
    profile_version: dict[str, dict[str, int]] = {language: {} for language in LANGUAGES}
    report["profiles"] = {}
    for language, versions in profiles.items():
        number, last = 0, None
        for profile, created_at, name in versions:
            try:
                ids = profile_fact_ids(profile, language)
            except CVError:
                report["skipped"].append({"file": name, "language": language, "reason": "not a valid CV profile"})
                continue
            if any(fact_id not in facts for fact_id in ids):
                report["skipped"].append({"file": name, "language": language, "reason": "lists facts no longer stored"})
                continue
            payload = tenant_store._json(profile)
            if payload == last:
                continue
            number += 1
            if number == 1:
                connection.execute("INSERT INTO profiles VALUES (?, ?, 1)", (owner, language))
            else:
                connection.execute("UPDATE profiles SET current_version = ? WHERE user_id = ? AND language = ?",
                                   (number, owner, language))
            connection.execute("INSERT INTO profile_versions VALUES (?, ?, ?, ?, ?)",
                               (owner, language, number, payload, created_at))
            connection.executemany("INSERT INTO profile_facts VALUES (?, ?, ?, ?)",
                                   [(owner, language, number, fact_id) for fact_id in ids])
            # The newest version with this content: a CV made from it is current exactly when that is.
            profile_version[language][_profile_hash(json.loads(payload))] = number
            last = payload
        if number:
            report["profiles"][language] = number
    current_profile = {language: max(versions.values()) if versions else None
                       for language, versions in profile_version.items()}
    current_hash = {language: next((digest for digest, number in versions.items() if number == current_profile[language]), None)
                    for language, versions in profile_version.items()}

    counts = {"jobs": 0, "materials": 0, "approvals_carried": 0, "final_pdfs": 0, "gap_checks": 0}
    left = {"job_history_folders": 0, "talking_points": 0}
    jobs = data / "jobs"
    for folder in sorted(jobs.iterdir()) if jobs.is_dir() else []:
        if folder.is_symlink() or not folder.is_dir() or not JOB_ID.fullmatch(folder.name):
            continue
        job_id = folder.name
        review_input = _read_json(folder / "input.json")
        jd = review_input.get("jd") if isinstance(review_input, dict) else None
        try:
            snapshot = tenant_store._jd_snapshot({key: value for key, value in (jd or {}).items()
                                                  if key in tenant_store.JD_KEYS})
        except (StoreError, ValueError):
            report["skipped"].append({"job": job_id, "reason": "its job description cannot be read"})
            continue
        connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
                           (owner, job_id, f"v1:{job_id}", tenant_store._json(snapshot), _job_created_at(job_id, folder)))
        counts["jobs"] += 1
        left["job_history_folders"] += (folder / "history").is_dir()
        left["talking_points"] += (folder / "matches.json").exists()
        candidates, decided = _read_json(folder / "candidates.json"), _read_json(folder / "decided.json")
        requirements_version = None
        if isinstance(candidates, dict):
            try:
                if decided is not None:
                    build_report(decided)
                connection.execute("INSERT INTO job_requirements VALUES (?, ?, 1, ?, ?)",
                                   (owner, job_id, tenant_store._json({"candidates": candidates, "decided": decided}),
                                    _mtime(folder / "candidates.json")))
                requirements_version = 1
            except (ValueError, StoreError):
                report["skipped"].append({"job": job_id, "reason": "its requirements cannot be read"})
        chosen = _read_json(folder / "cv-language.json")
        if isinstance(chosen, dict) and chosen.get("language") in LANGUAGES:
            connection.execute("INSERT INTO job_notes VALUES (?, ?, 'cv-language', ?, ?)",
                               (owner, job_id, tenant_store._json({"language": chosen["language"]}), _mtime(folder / "cv-language.json")))
        gaps = _read_json(folder / "gaps.json")
        if isinstance(gaps, dict):
            connection.execute("INSERT INTO job_gaps VALUES (?, ?, 1, ?, ?)",
                               (owner, job_id, tenant_store._json(gaps), _mtime(folder / "gaps.json")))
            counts["gap_checks"] += 1
        for language in LANGUAGES:
            _move_cv(connection, owner, folder, language, facts, current_facts, profile_version[language],
                     current_hash[language], requirements_version, counts, report)
    report.update(counts)
    report["not_migrated"].update(left)
    uploads = data / "cv-uploads"
    report["not_migrated"]["unsaved_uploads"] = len(list(uploads.glob("*.json"))) if uploads.is_dir() else 0
    unfinished = data / "unfinished"
    report["not_migrated"]["unfinished_notices"] = len(list(unfinished.glob("*.json"))) if unfinished.is_dir() else 0
    _move_boards(connection, owner, data, report)


def _move_cv(connection, owner, folder, language, facts, current_facts, versions, current_hash,
             requirements_version, counts, report) -> None:
    """One job's current CV chain in one language, and its approval and PDF if they still hold."""
    documents = [(stage, _read_json(folder / f"cv-{stage}-{language}.json")) for stage in CHAIN]
    documents = [(stage, document) for stage, document in documents if isinstance(document, dict)]
    if not documents:
        return
    where = {"job": folder.name, "language": language}
    profile_number = versions.get(documents[0][1].get("profile_sha256"))
    if profile_number is None:
        report["skipped"].append({**where, "reason": "the CV was made from a profile version that is no longer kept; "
                                                     "prepare it again in V2"})
        return
    for _, document in documents:
        used = document.get("facts") or []
        if not all(isinstance(item, dict) and item.get("id") in facts
                   and item.get("version") in facts[item["id"]]["versions"] for item in used):
            report["skipped"].append({**where, "reason": "the CV lists fact versions that are not stored"})
            return
    parent = None
    for version, (stage, document) in enumerate(documents, 1):
        material_id = os.urandom(16).hex()
        connection.execute("INSERT INTO materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (owner, material_id, folder.name, language, version, stage, parent, profile_number,
                            requirements_version, f"v1:{folder.name}:{language}:{stage}",
                            tenant_store._json(["v1", folder.name, language, stage]), tenant_store._json(document),
                            content_fingerprint(document), document.get("created_at") or _mtime(folder)))
        connection.executemany("INSERT INTO material_facts VALUES (?, ?, ?, ?)",
                               [(owner, material_id, item["id"], item["version"]) for item in document["facts"]])
        if parent is None:
            root = material_id
        parent = material_id
        counts["materials"] += 1
    status = _read_json(folder / f"cv-status-{language}.json")
    if isinstance(status, dict) and status.get("draft_created_at") == documents[0][1].get("created_at"):
        connection.execute("INSERT INTO job_notes VALUES (?, ?, ?, ?, ?)",
                           (owner, folder.name, f"cv-status-{language}",
                            tenant_store._json({"draft_material_id": root, "stages": status.get("stages") or [],
                                                "updated_at": status.get("updated_at")}),
                            _mtime(folder / f"cv-status-{language}.json")))
    approved_path = folder / f"cv-approved-{language}.json"
    if not approved_path.exists():
        return
    approved = _read_json(approved_path)
    head = documents[-1][1]
    reason = None
    try:
        if not isinstance(approved, dict) or not is_final_approval(approved):
            reason = "the approval record is missing"
        elif content_fingerprint(approved) != content_fingerprint(head):
            reason = "the approved CV is not the CV's newest version"
        elif approved.get("profile_sha256") != current_hash:
            reason = "the CV profile changed after the approval"
        else:
            verify_draft(approved, current_facts)
    except CVError:
        reason = reason or "the approved CV no longer rests on the current confirmed facts"
    if reason:
        report["approvals_not_carried"].append({**where, "reason": reason})
        return
    approval_id = os.urandom(16).hex()
    connection.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (owner, approval_id, parent, owner, content_fingerprint(approved), tenant_store._json(approved),
                        approved["approval"]["approved_at"]))
    counts["approvals_carried"] += 1
    final = folder / f"cv-final-{language}.pdf"
    if final.exists():
        data = final.read_bytes()
        if data.startswith(b"%PDF"):
            connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (owner, os.urandom(16).hex(), approval_id, data, hashlib.sha256(data).hexdigest(),
                                _count_pages(data), _mtime(final)))
            counts["final_pdfs"] += 1
        else:
            report["skipped"].append({**where, "reason": "the final PDF is damaged"})


def _move_boards(connection: sqlite3.Connection, owner: str, data: Path, report: dict) -> None:
    """Followed companies become the owner's; their postings, public data, the shared cache."""
    path = data / "listings.db"
    report["boards_followed"] = report["public_postings"] = 0
    if not path.exists():
        return
    source = _read_only(path)
    try:
        for row in source.execute("SELECT * FROM sources"):
            connection.execute("INSERT OR IGNORE INTO public_boards VALUES (?, ?, ?, ?)",
                               (row["provider"], row["board"], row["company"], row["fetched_at"] or row["added_at"]))
            connection.execute("INSERT INTO followed_boards VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (owner, row["provider"], row["board"], row["company"], row["added_at"], row["fetched_at"],
                                row["error"]))
            report["boards_followed"] += 1
        for row in source.execute("SELECT * FROM listings"):
            connection.execute("INSERT OR IGNORE INTO public_postings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (row["provider"], row["board"], row["job_id"], row["title"], row["location"], row["source"],
                                row["posted_at"], row["text"], row["text_hash"], row["first_seen_at"], row["last_seen_at"]))
            report["public_postings"] += 1
    except sqlite3.DatabaseError as exc:
        raise MigrationError("listings.db cannot be read") from exc
    finally:
        source.close()


def _verify(v2_db: Path, owner: str, report: dict) -> dict[str, Any]:
    """Read everything back through the owner's workspace, as the app will."""
    owner_space = UserWorkspace(v2_db, owner)
    facts = owner_space.list_facts()
    jobs = owner_space.job_summaries()
    valid = 0
    for summary in jobs:
        snapshot = owner_space.job_snapshot(summary["job_id"])
        valid += sum(1 for language in LANGUAGES if snapshot["cv"][language] and snapshot["cv"][language]["approval"])
    with tenant_store.transaction(v2_db) as connection:
        problems = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    return {"facts": len(facts), "confirmed": sum(fact["status"] == "confirmed" for fact in facts),
            "jobs": len(jobs), "valid_approvals": valid, "foreign_key_problems": len(problems),
            "languages": owner_space.cv_languages()}


def rollback(data: Path, backup_out: Path) -> dict[str, Any]:
    """Go back to the single-user app on ``data``: first a verified backup of the V2 database
    (so whatever V2 made since is kept), then the V2 database is set aside under a new name and
    the folder's format is set back to 1. Only with the V2 app stopped."""
    import v2_backup
    data = Path(data)
    database = data / "v2.db"
    if not database.exists():
        raise MigrationError("there is no V2 database in this folder")
    made = v2_backup.create_backup(data, backup_out)
    checked = v2_backup.inspect_archive(backup_out)
    if checked["problems"]:
        raise MigrationError("the V2 backup did not verify; nothing was changed")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    database.rename(data / f"v2.db.set-aside-{stamp}")
    for side in ("-wal", "-shm"):
        side_file = data / f"v2.db{side}"
        if side_file.exists():
            side_file.rename(data / f"v2.db.set-aside-{stamp}{side}")
    from workspace import write_atomically
    write_atomically(data / DATA_FORMAT_FILE, b"1\n")
    return {"backup": str(backup_out), "counts": made["counts"], "set_aside": f"v2.db.set-aside-{stamp}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="migrate a stopped V1 data folder for one named owner")
    run.add_argument("--v1-data", type=Path, required=True)
    run.add_argument("--v2-db", type=Path, required=True)
    run.add_argument("--owner-issuer", required=True)
    run.add_argument("--owner-subject", required=True)
    run.add_argument("--dry-run", action="store_true", help="migrate into a temporary copy and report only")
    back = commands.add_parser("rollback", help="back up V2, set it aside and return the folder to V1")
    back.add_argument("--data", type=Path, required=True)
    back.add_argument("--backup-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            if args.dry_run:
                with tempfile.TemporaryDirectory(prefix="v2-migrate-dry-") as scratch:
                    target = Path(scratch) / "v2.db"
                    if args.v2_db.exists():
                        import v2_backup
                        v2_backup.copy_database(args.v2_db, target)
                    result = migrate(args.v1_data, target, issuer=args.owner_issuer, subject=args.owner_subject)
                    result["dry_run"] = True
            else:
                result = migrate(args.v1_data, args.v2_db, issuer=args.owner_issuer, subject=args.owner_subject)
        else:
            result = rollback(args.data, args.backup_out)
    except (MigrationError, StoreError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
