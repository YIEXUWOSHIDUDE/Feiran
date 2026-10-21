"""V2's shared multi-user store: one SQLite database in which every private row belongs to one user.

Account provisioning and sign-in are trusted operations: the identity adapter (identity.py) calls
``sign_in`` only with an identity it has verified, and the web layer builds a UserWorkspace only
from a server-side session. A browser never chooses a user_id, and knowing one is not being signed
in. Every workspace operation rechecks the account inside the same short transaction as its reads
and writes; model calls, page fetches and PDF rendering never run inside a transaction.

The database is the only authority for what is current: fact, profile, requirement, CV and gap
versions, approvals, final PDFs, tasks and quotas. Existing rule modules (cv, gaps, cv_plan,
requirement_flow, cv_import) keep producing their JSON documents; this store keeps them as
versioned payloads and never rewrites their rules. This store never opens or migrates the V1
database; v2_migrate.py is the explicit offline path.

Sponsor answers (sponsorship.py) are worked out when words are saved: a public board read, a
user's saved job, the user's explicit check or backfill_sponsorship. Reading a page only reads
them. Format 3 has grown only by such added tables, which a release from before them ignores;
upgrade_store adds them to a store that release made.

Model use is metered per transport call in model_calls (begin_call, settle_call, fail_call):
each call has its own ID, belongs to one task execution and one UTC day, holds a conservative
reservation while it runs and is charged what the provider reported (prompt plus completion
tokens) once it answers, or keeps its reservation when its use cannot be proven. The quota mode
is "legacy" (fixed units per task, reserved at submit) until the operator configures "tokens"
(daily token limits checked when each call starts); legacy units are never read as tokens.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import sponsorship
from cv import (CVError, approve_draft, build_draft_from_facts, content_fingerprint, is_final_approval,
                profile_fact_ids, profile_languages, verify_draft)
from cv_import import build_profile
from cv_plan import set_change
from facts import FactStoreError, normalize_fact_import
from gaps import accept_gap, decline_gap, write_line
from privacy import private_terms
from review import build_report


APPLICATION_ID = 0x46455632  # FEV2, distinct from the single-user fact database
# 1 was the unreleased first increment (facts, profiles, jobs, drafts only) and 2 the unreleased one
# without request keys; both are refused, never migrated (no V2 database was ever in use).
SCHEMA_VERSION = 3
MAX_JSON_BYTES = 2_000_000
MAX_RAW_CONTENT = 200_000
MAX_PDF_BYTES = 5_000_000
UPLOAD_KEEP = timedelta(hours=24)
LOGIN_KEEP = timedelta(minutes=10)
MAX_PENDING_LOGINS = 1000
SESSION_TOUCH = timedelta(seconds=60)
LANGUAGES = ("en", "zh")
JD_KEYS = frozenset({"text", "source", "captured_at", "title", "company", "location", "provider", "board",
                     "job_id", "posted_at", "updated_at", "raw_content", "requested_url", "extraction_method"})
NOTE_NAMES = ("cv-language", "cv-status-en", "cv-status-zh")
MATERIAL_STAGES = ("draft", "tailored", "planned")
# What a user agrees to before the matching model features are used for them. A new wording
# gets a new version, and an agreement to an older version no longer counts.
CONSENT_STAGES = {"upload_parsing": 1, "job_processing": 1}
TASK_STATUSES = ("queued", "running", "succeeded", "failed", "interrupted", "superseded", "cancelled")
ACTIVE_TASKS = ("queued", "running")
RETRYABLE_TASKS = ("failed", "interrupted", "cancelled", "superseded")
MAX_TASK_ATTEMPTS = 3
SETTING_KEYS = ("registration_open", "max_accounts", "tasks_enabled", "user_daily_units", "site_daily_units",
                "queue_limit", "quota_mode", "user_daily_tokens", "site_daily_tokens")
DEFAULT_SETTINGS = {"registration_open": "0", "max_accounts": "100", "tasks_enabled": "1", "queue_limit": "20"}
# No quota_mode setting means legacy: a store made before tokens existed keeps its unit limits as
# units. Tokens mode needs both token limits, set together with it (configure_quota).
QUOTA_MODES = ("legacy", "tokens")
UNIT_LIMITS = ("user_daily_units", "site_daily_units")
TOKEN_LIMITS = ("user_daily_tokens", "site_daily_tokens")
MAX_DAILY_TOKENS = 10 ** 12
# The smallest output budget a model call starts with in tokens mode: with less room left it is
# refused before anything is sent, since a shorter answer would most likely be cut off and wasted.
MIN_OUTPUT_TOKENS = 1024
# Model work is refused at submit when less is left than any one call could need (the shortest
# instructions here are near a kilobyte, so about MIN_OUTPUT_TOKENS of prompt plus that output).
MIN_START_TOKENS = 2 * MIN_OUTPUT_TOKENS
MAX_REPORTED_TOKENS = 10 ** 9  # a single call reporting more than this is malformed, not believed
RESETS = "It resets at 00:00 UTC."
DATABASE = "v2.db"  # in the data folder, beside the deletion ledger
DELETION_LEDGER = "deleted-accounts.jsonl"


class StoreError(Exception):
    """A V2 store operation was refused; no partial write was committed."""


class NotFound(StoreError):
    """Missing and other users' objects deliberately have the same public error."""


class Conflict(StoreError):
    """The caller's input version or idempotency key no longer matches."""


class Refused(StoreError):
    """A policy refusal the page can explain: ``code`` names it (quota_exhausted, queue_full,
    consent_required, registration_closed, account_disabled, ...)."""

    def __init__(self, message: str, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


SCHEMA = """
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    consents TEXT NOT NULL DEFAULT '{}',
    UNIQUE(issuer, subject),
    CHECK(deleted_at IS NULL OR active = 0)
);
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    touched_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    idle_seconds INTEGER NOT NULL CHECK(idle_seconds > 0)
);
CREATE INDEX sessions_by_user ON sessions(user_id);
CREATE TABLE login_attempts (
    state_hash TEXT PRIMARY KEY,
    binding_hash TEXT NOT NULL,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    return_to TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE facts (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    fact_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version > 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, fact_id)
);
CREATE TABLE fact_versions (
    user_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed')),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    PRIMARY KEY(user_id, fact_id, version),
    FOREIGN KEY(user_id, fact_id) REFERENCES facts(user_id, fact_id),
    CHECK((status = 'pending' AND confirmed_at IS NULL)
       OR (status = 'confirmed' AND confirmed_at IS NOT NULL))
);
CREATE TABLE profiles (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    language TEXT NOT NULL CHECK(language IN ('en', 'zh')),
    current_version INTEGER NOT NULL CHECK(current_version > 0),
    PRIMARY KEY(user_id, language)
);
CREATE TABLE profile_versions (
    user_id TEXT NOT NULL,
    language TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, language, version),
    FOREIGN KEY(user_id, language) REFERENCES profiles(user_id, language)
);
CREATE TABLE profile_facts (
    user_id TEXT NOT NULL,
    language TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY(user_id, language, profile_version, fact_id),
    FOREIGN KEY(user_id, language, profile_version)
        REFERENCES profile_versions(user_id, language, version),
    FOREIGN KEY(user_id, fact_id) REFERENCES facts(user_id, fact_id)
);
CREATE TABLE uploads (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    upload_id TEXT NOT NULL,
    language TEXT NOT NULL CHECK(language IN ('en', 'zh')),
    source_sha256 TEXT NOT NULL,
    proposal TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(user_id, upload_id)
);
CREATE TABLE jobs (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    job_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, job_id),
    UNIQUE(user_id, request_id)
);
CREATE TABLE job_requirements (
    user_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, job_id, version),
    FOREIGN KEY(user_id, job_id) REFERENCES jobs(user_id, job_id)
);
CREATE TABLE job_notes (
    user_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    name TEXT NOT NULL CHECK(name IN ('cv-language', 'cv-status-en', 'cv-status-zh')),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, job_id, name),
    FOREIGN KEY(user_id, job_id) REFERENCES jobs(user_id, job_id)
);
CREATE TABLE job_gaps (
    user_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, job_id, version),
    FOREIGN KEY(user_id, job_id) REFERENCES jobs(user_id, job_id)
);
CREATE TABLE materials (
    user_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    language TEXT NOT NULL CHECK(language IN ('en', 'zh')),
    version INTEGER NOT NULL CHECK(version > 0),
    stage TEXT NOT NULL CHECK(stage IN ('draft', 'tailored', 'planned')),
    parent_id TEXT,
    profile_version INTEGER NOT NULL,
    requirements_version INTEGER,
    request_id TEXT NOT NULL,
    request_payload TEXT NOT NULL,
    payload TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, material_id),
    UNIQUE(user_id, request_id),
    UNIQUE(user_id, job_id, language, version),
    FOREIGN KEY(user_id, job_id) REFERENCES jobs(user_id, job_id),
    FOREIGN KEY(user_id, language, profile_version)
        REFERENCES profile_versions(user_id, language, version),
    FOREIGN KEY(user_id, parent_id) REFERENCES materials(user_id, material_id),
    FOREIGN KEY(user_id, job_id, requirements_version)
        REFERENCES job_requirements(user_id, job_id, version)
);
CREATE TABLE material_facts (
    user_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    fact_version INTEGER NOT NULL,
    PRIMARY KEY(user_id, material_id, fact_id),
    FOREIGN KEY(user_id, material_id) REFERENCES materials(user_id, material_id),
    FOREIGN KEY(user_id, fact_id, fact_version)
        REFERENCES fact_versions(user_id, fact_id, version)
);
CREATE TABLE approvals (
    user_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL REFERENCES users(user_id),
    content_sha256 TEXT NOT NULL,
    payload TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    PRIMARY KEY(user_id, approval_id),
    UNIQUE(user_id, material_id),
    FOREIGN KEY(user_id, material_id) REFERENCES materials(user_id, material_id),
    CHECK(reviewer_id = user_id)
);
CREATE TABLE artifacts (
    user_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    data BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    pages INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, artifact_id),
    UNIQUE(user_id, approval_id),
    FOREIGN KEY(user_id, approval_id) REFERENCES approvals(user_id, approval_id)
);
CREATE TABLE public_boards (
    provider TEXT NOT NULL,
    board TEXT NOT NULL,
    company TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(provider, board)
);
CREATE TABLE public_postings (
    provider TEXT NOT NULL,
    board TEXT NOT NULL,
    job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    source TEXT NOT NULL,
    posted_at TEXT,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(provider, board, job_id),
    FOREIGN KEY(provider, board) REFERENCES public_boards(provider, board) ON DELETE CASCADE
);
CREATE TABLE followed_boards (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    provider TEXT NOT NULL,
    board TEXT NOT NULL,
    company TEXT NOT NULL,
    added_at TEXT NOT NULL,
    fetched_at TEXT,
    error TEXT,
    PRIMARY KEY(user_id, provider, board)
);
CREATE TABLE posting_matches (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    provider TEXT NOT NULL,
    board TEXT NOT NULL,
    job_id TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    terms_key TEXT NOT NULL,
    matched_tags TEXT NOT NULL,
    PRIMARY KEY(user_id, provider, board, job_id),
    FOREIGN KEY(provider, board, job_id) REFERENCES public_postings(provider, board, job_id) ON DELETE CASCADE
);
CREATE TABLE tasks (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    task_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_payload TEXT NOT NULL,
    job_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted',
                                          'superseded', 'cancelled')),
    stage TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    execution_token TEXT,
    heavy TEXT NOT NULL CHECK(heavy IN ('model', 'pdf')),
    units INTEGER NOT NULL CHECK(units >= 0),
    reserved_day TEXT,
    cost TEXT NOT NULL DEFAULT 'none' CHECK(cost IN ('none', 'known', 'unknown')),
    usage TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    error_code TEXT,
    error_message TEXT,
    dismissed INTEGER NOT NULL DEFAULT 0 CHECK(dismissed IN (0, 1)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    deadline_at TEXT,
    PRIMARY KEY(user_id, task_id),
    UNIQUE(task_id),
    UNIQUE(user_id, operation, idempotency_key),
    CHECK((status = 'running') = (execution_token IS NOT NULL))
);
CREATE INDEX tasks_by_status ON tasks(status, created_at);
CREATE TABLE quota_usage (
    scope TEXT NOT NULL,
    day TEXT NOT NULL,
    units INTEGER NOT NULL CHECK(units >= 0),
    PRIMARY KEY(scope, day)
);
-- The page's request keys (Idempotency-Key), each bound to what its first accepted request
-- asked, whatever that request led to (new work, work already there, an existing job).
CREATE TABLE request_keys (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    operation TEXT NOT NULL,
    request_key TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, operation, request_key)
);
"""
# Added after format 3 was in use, so only as new tables (upgrade_store). A change to an
# existing table would need a new SCHEMA_VERSION and an explicit migration instead.
SPONSOR_TABLES = ("public_sponsor_assessments", "user_sponsor_assessments")
SPONSOR_SCHEMA = (
    # What the rules of rule_version found in the words of public postings, shared by everyone
    # following a board that lists them: keyed by the exact words (their sha256, as
    # public_postings.text_hash), never by a link. Where and when each posting was read stays
    # with the posting.
    """CREATE TABLE IF NOT EXISTS public_sponsor_assessments (
    text_sha256 TEXT NOT NULL,
    rule_version INTEGER NOT NULL CHECK(rule_version > 0),
    status TEXT NOT NULL CHECK(status IN ('supported', 'not_supported', 'conditional', 'unknown', 'conflicting')),
    evidence TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    PRIMARY KEY(text_sha256, rule_version)
)""",
    # The same for the words of one user's saved jobs (pasted, or read for them): never shared.
    """CREATE TABLE IF NOT EXISTS user_sponsor_assessments (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    text_sha256 TEXT NOT NULL,
    rule_version INTEGER NOT NULL CHECK(rule_version > 0),
    status TEXT NOT NULL CHECK(status IN ('supported', 'not_supported', 'conditional', 'unknown', 'conflicting')),
    evidence TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    PRIMARY KEY(user_id, text_sha256, rule_version)
)""",
)
SPONSOR_BATCH = 50  # public texts assessed per short write transaction
# Where a backfill round stopped: the last public words (by their hash) or saved job it worked on.
_BACKFILL_POSITION = re.compile(r"public:([0-9a-f]{64})|private:([^:\s]+):([^:\s]+)")
# Model metering, added the same way. Its rows name their account (users) and nothing else, so a
# release from before them can still delete an account; what such a release leaves behind of a
# deleted account goes at the next upgrade_store.
METER_TABLES = ("model_calls", "quota_exemptions", "deleted_model_usage")
METER_SCHEMA = (
    # One row per transport call (each HTTP attempt): its own ID, the task execution it was made
    # for (the attempt and a hash of its execution token), the UTC day it was reserved on, the
    # reservation (prompt estimate plus output budget) and what the provider reported. pending:
    # under way; settled: the reported prompt and completion tokens are its charge; unknown: no
    # usable report (an HTTP error, no answer, malformed usage, a restart), so it stays charged at
    # least its reservation. Never a prompt or an answer.
    """CREATE TABLE IF NOT EXISTS model_calls (
    call_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK(attempt >= 0),
    execution_sha256 TEXT NOT NULL,
    day TEXT NOT NULL,
    reserved INTEGER NOT NULL CHECK(reserved > 0),
    max_output INTEGER NOT NULL CHECK(max_output > 0),
    exempt INTEGER NOT NULL CHECK(exempt IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN ('pending', 'settled', 'unknown')),
    prompt_tokens INTEGER CHECK(prompt_tokens >= 0),
    completion_tokens INTEGER CHECK(completion_tokens >= 0),
    cache_hit_tokens INTEGER CHECK(cache_hit_tokens >= 0 AND cache_hit_tokens <= prompt_tokens),
    cache_miss_tokens INTEGER CHECK(cache_miss_tokens >= 0 AND cache_miss_tokens <= prompt_tokens),
    error TEXT,
    late INTEGER NOT NULL DEFAULT 0 CHECK(late IN (0, 1)),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    CHECK(status != 'settled' OR (prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL))
)""",
    "CREATE INDEX IF NOT EXISTS model_calls_by_day ON model_calls(day, user_id)",
    "CREATE INDEX IF NOT EXISTS model_calls_by_task ON model_calls(user_id, task_id)",
    # Accounts the operator marked as test accounts with no daily model limit (v2_admin.py only).
    """CREATE TABLE IF NOT EXISTS quota_exemptions (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id),
    kind TEXT NOT NULL CHECK(kind = 'test_unlimited'),
    granted_at TEXT NOT NULL
)""",
    # What deleted accounts' calls charged each day, without saying whose, so deleting an account
    # never gives the site back tokens that were spent.
    """CREATE TABLE IF NOT EXISTS deleted_model_usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL CHECK(calls >= 0),
    input_tokens INTEGER NOT NULL CHECK(input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK(output_tokens >= 0),
    charged_tokens INTEGER NOT NULL CHECK(charged_tokens >= 0)
)""",
)
ADDED_TABLES = SPONSOR_TABLES + METER_TABLES
ADDED_INDEXES = ("model_calls_by_day", "model_calls_by_task")
ADDED_SCHEMA = SPONSOR_SCHEMA + METER_SCHEMA
# What one call counts against a limit: a settled call what was reported; any other at least its
# reservation, and more if a part that was reported says so.
_CHARGE = ("CASE WHEN status = 'settled' THEN prompt_tokens + completion_tokens "
           "ELSE MAX(reserved, COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)) END")
# Every table holding one user's rows, children before parents: deleting an account goes in this order.
USER_TABLES = ("artifacts", "approvals", "material_facts", "materials", "job_gaps", "job_notes",
               "job_requirements", "jobs", "profile_facts", "profile_versions", "profiles", "fact_versions",
               "facts", "uploads", "posting_matches", "user_sponsor_assessments", "followed_boards", "model_calls",
               "quota_exemptions", "tasks", "request_keys", "sessions")


def _clock() -> datetime:
    """The current time; tests replace this to move time forward."""
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    # One fixed-width UTC form, so stored times also compare correctly as text.
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _now() -> str:
    return _stamp(_clock())


def _today() -> str:
    return _clock().date().isoformat()


def _text(value: Any, label: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise StoreError(f"{label} must be nonempty text of at most {limit} characters")
    return value


def _version(value: Any, *, zero: bool = False) -> int:
    if type(value) is not int or value < (0 if zero else 1):
        raise StoreError("Invalid expected version")
    return value


def _language(value: Any) -> str:
    if value not in LANGUAGES:
        raise StoreError("Invalid CV language")
    return value


def _json(value: Any) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise StoreError("Invalid JSON data") from exc
    if len(result.encode('utf-8')) > MAX_JSON_BYTES:
        raise StoreError("Input is too large")
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def input_digest(value: Any) -> str:
    """The fingerprint a request key is bound to: what the request asked, as canonical JSON."""
    return _digest(value)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise StoreError("V2 store does not exist")
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=5,
                                 isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA secure_delete = ON')  # deleted CV text is overwritten, not left in free pages
    if (connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or
            connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
        connection.close()
        raise StoreError("Not a supported V2 store; no automatic migration was attempted")
    return connection


def initialize_store(path: Path) -> None:
    """Create a new private V2 database, or verify an existing V2 database and add the tables
    this release keeps beside it (upgrade_store).

    Refuse legacy/unknown schemas and links without changing them. This is deliberately
    explicit: opening a user workspace cannot create an empty store after a missing mount.
    New stores use write-ahead logging, so pages can be read while a short write commits.
    """
    path = Path(path)
    if path.is_symlink():
        raise StoreError("A V2 store cannot be a symbolic link")
    if path.exists():
        upgrade_store(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('PRAGMA foreign_keys = ON')
        settings = "".join(f"\nINSERT INTO settings VALUES ('{key}', '{value}');"
                           for key, value in DEFAULT_SETTINGS.items())
        added = "".join(f"\n{statement};" for statement in ADDED_SCHEMA)
        connection.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + added + settings +
                                 f'\nPRAGMA application_id = {APPLICATION_ID};'
                                 f'\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;')
    finally:
        connection.close()


def _add_tables(connection: sqlite3.Connection) -> None:
    for statement in ADDED_SCHEMA:
        connection.execute(statement)


# Rows of the added tables that an older release, not knowing them, left behind for accounts it deleted.
_ORPHANS = {table: f'''SELECT 1 FROM {table} t JOIN users u ON u.user_id = t.user_id
                       WHERE u.deleted_at IS NOT NULL LIMIT 1'''
            for table in ("user_sponsor_assessments", "model_calls", "quota_exemptions")}


def upgrade_store(path: Path) -> list[str]:
    """Bring a format-3 store an earlier release made up to this release, as often as it is run:
    add the tables (and their indexes) it lacks, never changing or rebuilding one it has, and drop
    what a release that did not know them left of deleted accounts: private sponsor answers,
    test-account marks, and model calls (their charge kept in the site's day totals). Returns the
    tables added. Nothing is assessed here: words saved before stay "not assessed" until a board
    refresh, the user's explicit check, or backfill_sponsorship. No setting changes: a store
    without quota_mode stays in legacy mode, its unit limits units."""
    with transaction(path) as connection:
        present = {row['name'] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
        orphaned = any(table in present and connection.execute(query).fetchone() is not None
                       for table, query in _ORPHANS.items())
    missing = [name for name in ADDED_TABLES if name not in present]
    if missing or orphaned or not set(ADDED_INDEXES) <= present:
        with transaction(path, write=True) as connection:
            _add_tables(connection)
            for row in connection.execute('''SELECT DISTINCT c.user_id FROM model_calls c JOIN users u
                                              ON u.user_id = c.user_id WHERE u.deleted_at IS NOT NULL''').fetchall():
                _fold_calls(connection, row['user_id'])
            for table in _ORPHANS:
                connection.execute(f'''DELETE FROM {table}
                    WHERE user_id IN (SELECT user_id FROM users WHERE deleted_at IS NOT NULL)''')
    return missing


@contextmanager
def transaction(path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    """One short transaction on the V2 store; anything raised inside rolls all of it back."""
    connection = None
    try:
        connection = _connect(path)
        connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            yield connection
        except BaseException:
            try:
                connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass  # SQLite already rolled back; the original error matters
            raise
        connection.execute('COMMIT')
    except sqlite3.Error as exc:
        raise StoreError("V2 storage operation failed") from exc
    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------------------------------------------
# Settings, accounts and their lifecycle (trusted operations: admin tool, identity adapter)

def _settings(connection: sqlite3.Connection) -> dict[str, str]:
    return {row['key']: row['value'] for row in connection.execute('SELECT key, value FROM settings')}


def _positive(value: str | None) -> int | None:
    try:
        number = int(value) if value is not None else None
    except ValueError:
        return None
    return number if number is not None and number > 0 else None


def get_settings(path: Path) -> dict[str, str]:
    with transaction(path) as connection:
        return _settings(connection)


def _quota_mode(settings: dict[str, str]) -> str:
    return "tokens" if settings.get("quota_mode") == "tokens" else "legacy"


def _daily_limits(settings: dict[str, str], mode: str) -> tuple[int | None, int | None]:
    """The (user, site) daily limits of a quota mode; None where one is not configured."""
    return tuple(_positive(settings.get(name)) for name in (TOKEN_LIMITS if mode == "tokens" else UNIT_LIMITS))


def _check_settings(settings: dict[str, str]) -> None:
    """Refuse settings that would leave work without finite limits: tokens mode without both
    token limits, or registration open without the active mode's limits and an account limit."""
    if settings.get("quota_mode") == "tokens" and None in _daily_limits(settings, "tokens"):
        raise StoreError("Set user_daily_tokens and site_daily_tokens before quota_mode tokens "
                         "(v2_admin.py quota tokens sets all three at once)")
    mode = _quota_mode(settings)
    if settings.get("registration_open") == "1" and (None in _daily_limits(settings, mode)
                                                      or _positive(settings.get("max_accounts")) is None):
        user, site = TOKEN_LIMITS if mode == "tokens" else UNIT_LIMITS
        raise StoreError(f"Set finite {user}, {site} and max_accounts before opening registration")


def _write_settings(connection: sqlite3.Connection, updates: dict[str, str]) -> dict[str, str]:
    previous = _settings(connection)
    settings = {**previous, **updates}
    _check_settings(settings)
    if settings.get('quota_mode', 'legacy') != previous.get('quota_mode', 'legacy'):
        if connection.execute("SELECT 1 FROM tasks WHERE status IN ('queued', 'running') LIMIT 1").fetchone():
            raise StoreError('Wait for queued and running tasks to finish before changing quota mode')
    for key, value in updates.items():
        connection.execute('INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                           (key, value))
    return settings


def set_setting(path: Path, key: str, value: str) -> None:
    """Change one site setting. Registration opens only with finite daily quotas and a finite
    account limit configured, so the site can never be open without spending limits; tokens mode
    needs its two token limits first."""
    if key not in SETTING_KEYS:
        raise StoreError(f"Unknown setting: {key}")
    if key in ("registration_open", "tasks_enabled"):
        if value not in ("0", "1"):
            raise StoreError(f"{key} must be 0 or 1")
    elif key == "quota_mode":
        if value not in QUOTA_MODES:
            raise StoreError("quota_mode must be legacy or tokens")
    elif _positive(value) is None or (key in TOKEN_LIMITS and int(value) > MAX_DAILY_TOKENS):
        raise StoreError(f"{key} must be a positive whole number" +
                         (f" of at most {MAX_DAILY_TOKENS}" if key in TOKEN_LIMITS else ""))
    with transaction(path, write=True) as connection:
        _write_settings(connection, {key: value})


def configure_quota(path: Path, mode: str, *, user_daily_tokens: int | None = None,
                    site_daily_tokens: int | None = None) -> dict[str, str]:
    """The operator's switch between quota modes, in one transaction. "tokens" sets both daily
    token limits with it; "legacy" goes back to the unit limits already set. Nothing is converted
    either way: units stay units and tokens tokens. Returns the settings."""
    if mode not in QUOTA_MODES:
        raise StoreError("The quota mode must be legacy or tokens")
    updates = {"quota_mode": mode}
    if mode == "tokens":
        for name, value in zip(TOKEN_LIMITS, (user_daily_tokens, site_daily_tokens)):
            if type(value) is not int or not 0 < value <= MAX_DAILY_TOKENS:
                raise StoreError(f"{name} must be a positive whole number of at most {MAX_DAILY_TOKENS}")
            updates[name] = str(value)
    elif user_daily_tokens is not None or site_daily_tokens is not None:
        raise StoreError("Token limits are set together with quota mode tokens")
    with transaction(path, write=True) as connection:
        return _write_settings(connection, updates)


def set_test_unlimited(path: Path, user_id: str, *, enabled: bool) -> dict[str, Any]:
    """Trusted operator change (v2_admin.py; no page or sign-in can make it): mark one account as
    a test account with no daily model limit, or end that. Such an account is never refused for
    its own or the site's daily limit, but every call it makes is still metered and counts in the
    site's total; the kill switch, the queue and its bounds, deadlines and disabling still apply,
    and a site without configured limits still starts no work."""
    if type(enabled) is not bool:
        raise StoreError("Invalid test account state")
    with transaction(path, write=True) as connection:
        if connection.execute('SELECT 1 FROM users WHERE user_id = ? AND deleted_at IS NULL',
                              (_text(user_id, 'user_id'),)).fetchone() is None:
            raise NotFound("Account unavailable")
        if enabled:
            connection.execute("INSERT OR IGNORE INTO quota_exemptions VALUES (?, 'test_unlimited', ?)", (user_id, _now()))
        else:
            connection.execute('DELETE FROM quota_exemptions WHERE user_id = ?', (user_id,))
    return {"user_id": user_id, "test_unlimited": enabled}


def _exempt(connection: sqlite3.Connection, user_id: str) -> bool:
    return connection.execute('SELECT 1 FROM quota_exemptions WHERE user_id = ?', (user_id,)).fetchone() is not None


def list_accounts(path: Path) -> list[dict[str, Any]]:
    """Every account, for the operator: internal ID, identity, state and dates; no content."""
    with transaction(path) as connection:
        return [{"user_id": row["user_id"], "issuer": row["issuer"], "subject": row["subject"],
                 "state": "deleted" if row["deleted_at"] else "active" if row["active"] else "disabled",
                 "created_at": row["created_at"], "deleted_at": row["deleted_at"]}
                for row in connection.execute("SELECT * FROM users ORDER BY created_at, user_id")]


def _resets_at(day: str) -> str:
    """When a UTC day's allowance starts again: the next midnight, UTC."""
    return (datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(days=1)).isoformat()


def _charged(connection: sqlite3.Connection, day: str, user_id: str | None = None) -> int:
    """The tokens charged on one day: one account's calls, or (no user) the whole site's,
    deleted accounts' included."""
    if user_id is not None:
        return connection.execute(f'SELECT COALESCE(SUM({_CHARGE}), 0) FROM model_calls WHERE day = ? AND user_id = ?',
                                  (day, user_id)).fetchone()[0]
    calls = connection.execute(f'SELECT COALESCE(SUM({_CHARGE}), 0) FROM model_calls WHERE day = ?', (day,)).fetchone()[0]
    gone = connection.execute('SELECT charged_tokens FROM deleted_model_usage WHERE day = ?', (day,)).fetchone()
    return calls + (gone[0] if gone else 0)


def _call_summary(connection: sqlite3.Connection, where: str, values: tuple) -> dict[str, Any]:
    """Model calls matching ``where``: how many, still under way or of unknown use; the input and
    output tokens reported by settled calls (cached input, a part of input, where it was reported);
    what is held beyond that for calls not settled; and the total charged."""
    row = connection.execute(f'''SELECT COUNT(*) AS calls, COALESCE(SUM(status = 'pending'), 0) AS pending,
        COALESCE(SUM(status = 'unknown'), 0) AS unknown,
        COALESCE(SUM(CASE WHEN status = 'settled' THEN prompt_tokens END), 0) AS input,
        COALESCE(SUM(CASE WHEN status = 'settled' THEN completion_tokens END), 0) AS output,
        SUM(CASE WHEN status = 'settled' THEN cache_hit_tokens END) AS cached,
        COALESCE(SUM({_CHARGE}), 0) AS charged FROM model_calls WHERE {where}''', values).fetchone()
    total = row['input'] + row['output']
    return {"calls": row['calls'], "pending_calls": row['pending'], "unknown_calls": row['unknown'],
            "input": row['input'], "output": row['output'], "total": total, "cached_input": row['cached'],
            "reserved": row['charged'] - total, "charged": row['charged']}


def _account_usage(connection: sqlite3.Connection, user_id: str, day: str) -> dict[str, Any]:
    """One account's day as its own page shows it: the active quota mode's measure (tokens charged,
    or legacy units reserved) against its limit, and the model calls metered either way. The
    site's numbers are never part of it: they would show other accounts' use."""
    settings = _settings(connection)
    mode = _quota_mode(settings)
    unlimited = _exempt(connection, user_id)
    tokens = _call_summary(connection, 'day = ? AND user_id = ?', (day, user_id))
    if mode == "tokens":
        used = tokens["charged"]
    else:
        row = connection.execute('SELECT units FROM quota_usage WHERE scope = ? AND day = ?', (user_id, day)).fetchone()
        used = row['units'] if row else 0
    limit = None if unlimited else _daily_limits(settings, mode)[0]
    return {"mode": mode, "unit": "tokens" if mode == "tokens" else "units", "day": day, "resets_at": _resets_at(day),
            "configured": None not in _daily_limits(settings, mode), "test_unlimited": unlimited, "used": used,
            "limit": limit, "remaining": None if limit is None else max(0, limit - used), "tokens": tokens}


def usage_on(path: Path, day: str | None = None) -> dict[str, Any]:
    """One day (UTC; today by default), for the operator: the legacy units reserved (the site's
    and each account's), the model calls metered (the site, deleted accounts' part included, and
    each account), the limits, the test accounts, and the token counts that tasks reported
    before calls were metered, which are incomplete (no day, no retries, no unusable answers)."""
    day = day or _today()
    with transaction(path) as connection:
        used = {row["scope"]: row["units"]
                for row in connection.execute("SELECT scope, units FROM quota_usage WHERE day = ?", (day,))}
        settings = _settings(connection)
        site = _call_summary(connection, 'day = ?', (day,))
        gone = connection.execute('SELECT * FROM deleted_model_usage WHERE day = ?', (day,)).fetchone()
        if gone:
            site = {**site, "calls": site["calls"] + gone["calls"], "input": site["input"] + gone["input_tokens"],
                    "output": site["output"] + gone["output_tokens"],
                    "total": site["total"] + gone["input_tokens"] + gone["output_tokens"],
                    "charged": site["charged"] + gone["charged_tokens"]}
            site["reserved"] = site["charged"] - site["total"]
        accounts = {row["user_id"]: _call_summary(connection, 'day = ? AND user_id = ?', (day, row["user_id"]))
                    for row in connection.execute('SELECT DISTINCT user_id FROM model_calls WHERE day = ? ORDER BY user_id',
                                                  (day,))}
        unlimited = [row["user_id"] for row in connection.execute('SELECT user_id FROM quota_exemptions ORDER BY user_id')]
        earlier = {"prompt_tokens": 0, "completion_tokens": 0}
        for row in connection.execute("SELECT usage FROM tasks"):
            for name, count in _usage_state(row["usage"])["tokens"].items():
                if name in earlier:
                    earlier[name] += count
    user_tokens, site_tokens = _daily_limits(settings, "tokens")
    return {"day": day, "mode": _quota_mode(settings), "site": used.pop("*", 0),
            "site_limit": _positive(settings.get("site_daily_units")),
            "user_limit": _positive(settings.get("user_daily_units")), "accounts": used,
            "tokens": {"site": site, "deleted_accounts_charged": gone["charged_tokens"] if gone else 0,
                       "site_limit": site_tokens, "user_limit": user_tokens, "accounts": accounts,
                       "test_unlimited": unlimited},
            "earlier_reported_tokens": {**earlier, "complete": False,
                                        "note": "reported by tasks before per-call metering, all days together"}}


def provision_user(path: Path, *, issuer: str, subject: str) -> str:
    """Trusted provisioning for an already verified identity; NOT an authentication API.

    Idempotent for the exact provider identity. Never uses email to own stored materials,
    and never reactivates a disabled or deleted user as a side effect of another login.
    Unlike ``sign_in`` it ignores the registration switch: the admin tool and tests use it.
    """
    issuer, subject = _text(issuer, 'issuer', 2048), _text(subject, 'subject')
    with transaction(path, write=True) as connection:
        user = connection.execute('SELECT user_id FROM users WHERE issuer = ? AND subject = ?',
                                  (issuer, subject)).fetchone()
        if user:
            return user['user_id']
        return _create_user(connection, issuer, subject)


def _create_user(connection: sqlite3.Connection, issuer: str, subject: str,
                 starter: Iterable[dict[str, str]] = ()) -> str:
    user_id = secrets.token_hex(16)
    now = _now()
    connection.execute('INSERT INTO users(user_id, issuer, subject, created_at) VALUES (?, ?, ?, ?)',
                       (user_id, issuer, subject, now))
    for item in starter:
        connection.execute('INSERT OR IGNORE INTO followed_boards(user_id, provider, board, company, added_at) '
                           'VALUES (?, ?, ?, ?, ?)', (user_id, item["provider"], item["board"], item["company"], now))
    return user_id


def sign_in(path: Path, *, issuer: str, subject: str, starter: Iterable[dict[str, str]] = ()) -> str:
    """The account of an identity the identity adapter has verified, created on first sign-in
    only while registration is open and below the account limit. A disabled or deleted account
    is refused and never reopened by signing in. ``starter`` lists the public job boards a new
    account follows at first, like a new single-user workspace."""
    issuer, subject = _text(issuer, 'issuer', 2048), _text(subject, 'subject')
    with transaction(path, write=True) as connection:
        user = connection.execute('SELECT user_id, active, deleted_at FROM users WHERE issuer = ? AND subject = ?',
                                  (issuer, subject)).fetchone()
        if user:
            if user['deleted_at']:
                raise Refused("This account was deleted", "account_deleted")
            if not user['active']:
                raise Refused("This account is disabled", "account_disabled")
            return user['user_id']
        settings = _settings(connection)
        if settings.get("registration_open") != "1":
            raise Refused("Registration is closed", "registration_closed")
        limit = _positive(settings.get("max_accounts"))
        count = connection.execute('SELECT COUNT(*) FROM users WHERE deleted_at IS NULL').fetchone()[0]
        if limit is None or count >= limit:
            raise Refused("The pilot is full", "registration_full")
        return _create_user(connection, issuer, subject, starter)


def _cancel_tasks(connection: sqlite3.Connection, user_id: str, message: str) -> None:
    """Queued work never starts and a running task's result can no longer be published: its
    execution token goes. A model call already under way may still cost money."""
    for row in connection.execute("SELECT task_id FROM tasks WHERE user_id = ? AND status IN ('queued', 'running')",
                                  (user_id,)).fetchall():
        _end_task(connection, user_id, row['task_id'], 'cancelled', error_code='account_unavailable', message=message)


def set_user_active(path: Path, user_id: str, *, active: bool) -> None:
    """Trusted lifecycle operation; workspace methods cannot administer other users.
    Disabling ends every session and stops the account's queued and running tasks."""
    if type(active) is not bool:
        raise StoreError("Invalid account state")
    with transaction(path, write=True) as connection:
        changed = connection.execute('UPDATE users SET active = ? WHERE user_id = ? AND deleted_at IS NULL',
                                     (active, user_id))
        if changed.rowcount != 1:
            raise NotFound("Account unavailable")
        if not active:
            connection.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
            _cancel_tasks(connection, user_id, "The account was disabled")


def delete_account(path: Path, user_id: str, ledger: Path | None = None) -> dict[str, str]:
    """Remove every private row of one account and keep only its tombstone: the identity, so the
    same sign-in can never reopen it, and the deletion time. With ``ledger`` (a file outside the
    database, kept beside it) the tombstone is also appended there first, so restoring an older
    backup can delete the account again (v2_backup.py). Backups made earlier still hold the data
    until they expire; that is stated to users, not hidden."""
    with transaction(path, write=True) as connection:
        user = connection.execute('SELECT * FROM users WHERE user_id = ? AND deleted_at IS NULL', (user_id,)).fetchone()
        if user is None:
            raise NotFound("Account unavailable")
        record = {"user_id": user_id, "issuer": user["issuer"], "subject": user["subject"], "deleted_at": _now()}
        if ledger is not None:
            append_deletion(ledger, record)
        _purge_user(connection, record)
        return record


def append_deletion(ledger: Path, record: dict[str, str]) -> None:
    """Append one tombstone to the deletion ledger and flush it to disk before the data goes."""
    line = json.dumps({key: record[key] for key in ("user_id", "issuer", "subject", "deleted_at")},
                      ensure_ascii=False, sort_keys=True) + "\n"
    descriptor = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_ledger(path: Path, ledger: Path) -> int:
    """Make sure the deletion ledger beside a V2 database exists and lists every account this
    database has deleted, so a data folder always has one: then a restore that cannot find the
    live ledger it was given knows the path is wrong or the ledger was lost, and refuses. A lost
    ledger is rebuilt from the database's tombstones. Returns how many records were added."""
    ledger = Path(ledger)
    known = {(record["user_id"], record["issuer"], record["subject"]) for record in read_deletions(ledger)}
    with transaction(path) as connection:
        tombstones = [dict(row) for row in connection.execute(
            "SELECT user_id, issuer, subject, deleted_at FROM users WHERE deleted_at IS NOT NULL ORDER BY deleted_at, user_id")]
    added = 0
    for record in tombstones:
        if (record["user_id"], record["issuer"], record["subject"]) not in known:
            append_deletion(ledger, record)
            added += 1
    if not ledger.exists():
        os.close(os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    return added


def read_deletions(ledger: Path) -> list[dict[str, str]]:
    """Every tombstone in a deletion ledger; a line that cannot be read stops the caller."""
    if not Path(ledger).exists():
        return []
    records = []
    for number, line in enumerate(Path(ledger).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise StoreError(f"Deletion ledger line {number} cannot be read") from exc
        if not isinstance(record, dict) or not all(isinstance(record.get(key), str) and record[key]
                                                   for key in ("user_id", "issuer", "subject", "deleted_at")):
            raise StoreError(f"Deletion ledger line {number} is incomplete")
        records.append(record)
    return records


def _fold_calls(connection: sqlite3.Connection, user_id: str) -> None:
    """Add one account's model calls, per day and without its ID, to what deleted accounts
    charged, before the calls themselves are deleted."""
    for row in connection.execute(f'''SELECT day, COUNT(*) AS calls,
            COALESCE(SUM(CASE WHEN status = 'settled' THEN prompt_tokens END), 0) AS input,
            COALESCE(SUM(CASE WHEN status = 'settled' THEN completion_tokens END), 0) AS output,
            COALESCE(SUM({_CHARGE}), 0) AS charged FROM model_calls WHERE user_id = ? GROUP BY day''',
                                  (user_id,)).fetchall():
        connection.execute('''INSERT INTO deleted_model_usage VALUES (?, ?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET
            calls = calls + excluded.calls, input_tokens = input_tokens + excluded.input_tokens,
            output_tokens = output_tokens + excluded.output_tokens,
            charged_tokens = charged_tokens + excluded.charged_tokens''',
                           (row['day'], row['calls'], row['input'], row['output'], row['charged']))


def _purge_user(connection: sqlite3.Connection, record: dict[str, str]) -> None:
    """Delete one account's private rows and leave its tombstone (inside the caller's transaction).
    What its model calls charged stays in the site's day totals, without its ID."""
    user_id = record["user_id"]
    _add_tables(connection)  # a store restored from before them has none to name yet
    _fold_calls(connection, user_id)
    for table in USER_TABLES:
        connection.execute(f'DELETE FROM {table} WHERE user_id = ?', (user_id,))
    connection.execute("DELETE FROM quota_usage WHERE scope = ?", (user_id,))
    existing = connection.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,)).fetchone()
    if existing:
        connection.execute("UPDATE users SET active = 0, deleted_at = ?, consents = '{}' WHERE user_id = ?",
                           (record["deleted_at"], user_id))
    else:
        connection.execute('INSERT OR IGNORE INTO users(user_id, issuer, subject, active, created_at, deleted_at) '
                           'VALUES (?, ?, ?, 0, ?, ?)', (user_id, record["issuer"], record["subject"],
                                                         record["deleted_at"], record["deleted_at"]))
    # The same identity under another internal ID (an older backup's row) is closed as well.
    connection.execute("""UPDATE users SET active = 0, deleted_at = COALESCE(deleted_at, ?)
        WHERE issuer = ? AND subject = ?""", (record["deleted_at"], record["issuer"], record["subject"]))


def apply_deletions(path: Path, records: Iterable[dict[str, str]]) -> int:
    """Delete again, in a restored database, every account the ledger says was deleted; returns
    how many accounts still held data. Used by restore before the app may open the database."""
    cleaned = 0
    with transaction(path, write=True) as connection:
        for record in records:
            for user in connection.execute('SELECT user_id FROM users WHERE (user_id = ? OR (issuer = ? AND subject = ?)) '
                                           'AND deleted_at IS NULL',
                                           (record["user_id"], record["issuer"], record["subject"])).fetchall():
                cleaned += 1
                _purge_user(connection, {**record, "user_id": user["user_id"]})
            _purge_user(connection, record)
    return cleaned


# ---------------------------------------------------------------------------------------------
# Sessions and sign-in attempts (called by identity.py and the web layer only)

@dataclass(frozen=True)
class Session:
    user_id: str
    csrf_token: str
    expires_at: str


def create_session(path: Path, user_id: str, *, lifetime: timedelta, idle: timedelta) -> tuple[str, Session]:
    """A new server-side session for an active account. Only the token's hash is stored; the
    token itself goes into the browser's cookie once. Expired sessions are cleared here too."""
    if lifetime <= timedelta(0) or idle <= timedelta(0):
        raise StoreError("Session lifetimes must be positive")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = _clock()
    with transaction(path, write=True) as connection:
        user = connection.execute('SELECT active FROM users WHERE user_id = ?', (user_id,)).fetchone()
        if not user or not user['active']:
            raise NotFound("Account unavailable")
        connection.execute('DELETE FROM sessions WHERE expires_at <= ? OR idle_expires_at <= ?',
                           (_stamp(now), _stamp(now)))
        expires = _stamp(now + lifetime)
        connection.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (_hash(token), user_id, csrf, _stamp(now), _stamp(now),
                            _stamp(min(now + idle, now + lifetime)), expires, int(idle.total_seconds())))
    return token, Session(user_id, csrf, expires)


def resolve_session(path: Path, token: str | None) -> Session | None:
    """The session a cookie names, if it is unexpired, not revoked and its account is active.
    Activity extends the idle limit (at most once a minute), never the absolute one."""
    if not isinstance(token, str) or not 20 <= len(token) <= 128:
        return None
    key, now = _hash(token), _clock()
    with transaction(path) as connection:
        row = connection.execute('''SELECT s.*, u.active FROM sessions s JOIN users u ON u.user_id = s.user_id
            WHERE s.token_hash = ?''', (key,)).fetchone()
    if row is None or not row['active']:
        return None
    if row['expires_at'] <= _stamp(now) or row['idle_expires_at'] <= _stamp(now):
        revoke_session(path, token)
        return None
    if datetime.fromisoformat(row['touched_at']) + SESSION_TOUCH <= now:
        idle_until = min(now + timedelta(seconds=row['idle_seconds']), datetime.fromisoformat(row['expires_at']))
        with transaction(path, write=True) as connection:
            connection.execute('UPDATE sessions SET touched_at = ?, idle_expires_at = ? WHERE token_hash = ?',
                               (_stamp(now), _stamp(idle_until), key))
    return Session(row['user_id'], row['csrf_token'], row['expires_at'])


def revoke_session(path: Path, token: str | None) -> None:
    if isinstance(token, str) and token:
        with transaction(path, write=True) as connection:
            connection.execute('DELETE FROM sessions WHERE token_hash = ?', (_hash(token),))


def save_login_attempt(path: Path, *, state: str, binding: str, nonce: str, code_verifier: str,
                       return_to: str) -> None:
    """Remember one sign-in started from this browser for ten minutes. The state and the browser
    binding are stored only as hashes; the nonce and verifier are needed again at the callback."""
    now = _clock()
    with transaction(path, write=True) as connection:
        connection.execute('DELETE FROM login_attempts WHERE expires_at <= ?', (_stamp(now),))
        if connection.execute('SELECT COUNT(*) FROM login_attempts').fetchone()[0] >= MAX_PENDING_LOGINS:
            raise Refused("Too many sign-ins are waiting; try again in a few minutes", "busy")
        connection.execute('INSERT INTO login_attempts VALUES (?, ?, ?, ?, ?, ?)',
                           (_hash(state), _hash(binding), nonce, code_verifier, return_to, _stamp(now + LOGIN_KEEP)))


def take_login_attempt(path: Path, *, state: str, binding: str) -> dict[str, str] | None:
    """The sign-in this callback finishes, used up by this call, but only in the browser that
    started it (its binding): a callback opened elsewhere is refused and cannot use up the real
    one, and a replayed callback finds nothing."""
    if not isinstance(state, str) or not state or not isinstance(binding, str) or not binding:
        return None
    with transaction(path, write=True) as connection:
        row = connection.execute('SELECT * FROM login_attempts WHERE state_hash = ?', (_hash(state),)).fetchone()
        if row is None or not secrets.compare_digest(row['binding_hash'], _hash(binding)):
            return None
        connection.execute('DELETE FROM login_attempts WHERE state_hash = ?', (_hash(state),))
    if row['expires_at'] <= _now():
        return None
    return {"nonce": row['nonce'], "code_verifier": row['code_verifier'], "return_to": row['return_to']}


# ---------------------------------------------------------------------------------------------
# Fact sources for the existing rule modules (see facts.FactFile)

class FactSnapshot:
    """One user's current facts read in one transaction, for rules that run while a model is
    asked: nothing here can write. Publication checks later that these versions are still current."""

    def __init__(self, facts: list[dict[str, Any]]) -> None:
        self._facts = {fact["id"]: fact for fact in facts}
        self._order = [fact["id"] for fact in facts]

    def available(self) -> bool:
        return True

    def current(self, fact_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        return {fact_id: self._facts[fact_id] for fact_id in dict.fromkeys(fact_ids) if fact_id in self._facts}

    def listed(self) -> list[dict[str, Any]]:
        return [self._facts[fact_id] for fact_id in self._order]

    def versions(self) -> list[list]:
        return sorted([fact_id, self._facts[fact_id]["version"], self._facts[fact_id]["status"]]
                      for fact_id in self._order)

    def revise_confirmed(self, *_args: Any) -> dict[str, Any]:
        raise StoreError("A fact snapshot cannot change facts")

    add_confirmed = revise_confirmed


class _TransactionFacts:
    """One user's facts inside an open write transaction: the gap and import rules read and
    write through it, and the caller commits their result in the same transaction."""

    def __init__(self, workspace: "UserWorkspace", connection: sqlite3.Connection) -> None:
        self.workspace, self.connection = workspace, connection

    def available(self) -> bool:
        return True

    def current(self, fact_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        found = {}
        for fact_id in dict.fromkeys(fact_ids):
            try:
                found[fact_id] = self.workspace._fact(self.connection, fact_id)
            except NotFound:
                continue
        return found

    def listed(self) -> list[dict[str, Any]]:
        return self.workspace._list_facts(self.connection)

    def revise_confirmed(self, fact_id: str, text: str, tags: Iterable[str]) -> dict[str, Any]:
        current = self.workspace._fact(self.connection, fact_id)
        _, text, kind, normalized = normalize_fact_import(
            [{'text': text, 'type': current['fact_type'], 'tags': list(tags)}])[0]
        payload = _fact_payload(text, kind, normalized)
        if _json({key: current[key] for key in ('text', 'fact_type', 'tags')}) != payload:
            version = current['version'] + 1
            self.workspace._write_fact(self.connection, fact_id, version, payload)
            self.connection.execute('UPDATE facts SET current_version = ? WHERE user_id = ? AND fact_id = ?',
                                    (version, self.workspace.user_id, fact_id))
        return self._confirm_current(fact_id)

    def add_confirmed(self, text: str, fact_type: str, tags: Iterable[str]) -> dict[str, Any]:
        _, text, kind, normalized = normalize_fact_import([{'text': text, 'type': fact_type, 'tags': list(tags)}])[0]
        payload = _fact_payload(text, kind, normalized)
        keys = [key for _, key in normalized]
        same = next((fact for fact in self.listed() if fact['text'] == text and fact['fact_type'] == kind
                     and [tag.casefold() for tag in fact['tags']] == keys), None)
        if same is None:
            fact_id = 'fact-' + secrets.token_hex(6)
            self.connection.execute('INSERT INTO facts VALUES (?, ?, 1, ?)', (self.workspace.user_id, fact_id, _now()))
            self.workspace._write_fact(self.connection, fact_id, 1, payload)
        else:
            fact_id = same['id']
        return self._confirm_current(fact_id)

    def _confirm_current(self, fact_id: str) -> dict[str, Any]:
        """The user has just said these exact words are true: confirm the current version."""
        self.connection.execute('''UPDATE fact_versions SET status = 'confirmed', confirmed_at = ?
            WHERE user_id = ? AND fact_id = ? AND status = 'pending'
              AND version = (SELECT current_version FROM facts WHERE user_id = ? AND fact_id = ?)''',
                                (_now(), self.workspace.user_id, fact_id, self.workspace.user_id, fact_id))
        return self.workspace._fact(self.connection, fact_id)


def _fact_payload(text: str, kind: str, tags: list[tuple[str, str]]) -> str:
    # Tag display case is preserved; imports with the same complete payload are idempotent.
    return _json({'text': text, 'fact_type': kind, 'tags': [tag for tag, _ in tags]})


def _jd_snapshot(jd: Any) -> dict[str, Any]:
    """The stored form of a JD: known text fields only. A page's whole HTML is provenance, not
    the description, and is left out when it is very large."""
    if not isinstance(jd, dict) or set(jd) - JD_KEYS:
        raise StoreError("Invalid JD snapshot")
    snapshot = {}
    for key, value in jd.items():
        if value is None:
            snapshot[key] = None
            continue
        if key == "raw_content" and isinstance(value, str) and len(value) > MAX_RAW_CONTENT:
            continue
        snapshot[key] = _text(value, 'JD field', 100_000 if key != "raw_content" else MAX_RAW_CONTENT)
    build_report({'jd': snapshot, 'facts': [], 'selected_requirements': []})
    return snapshot


@dataclass(frozen=True)
class UserWorkspace:
    """Only user-scoped data access is exposed. No model call happens here.

    Construct with a server-resolved user_id, never a client-selected identity. The object
    holds no connection or cached account status and may be used after a process restart.
    Every call checks the user's active state inside the same transaction as its reads/writes.
    Approval and fact confirmation only happen through methods named for the user's decision.
    """
    database: Path
    user_id: str

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        _text(self.user_id, 'user_id')
        with transaction(self.database, write=write) as connection:
            user = connection.execute('SELECT active FROM users WHERE user_id = ?', (self.user_id,)).fetchone()
            if not user or not user['active']:
                raise NotFound("Account unavailable")
            yield connection

    # -- facts -----------------------------------------------------------------------------

    def _fact(self, connection: sqlite3.Connection, fact_id: str, version: int | None = None) -> dict:
        row = connection.execute('''SELECT v.*, f.current_version FROM facts f JOIN fact_versions v
            ON v.user_id = f.user_id AND v.fact_id = f.fact_id
            WHERE f.user_id = ? AND f.fact_id = ? AND v.version = COALESCE(?, f.current_version)''',
                                 (self.user_id, fact_id, version)).fetchone()
        if row is None:
            raise NotFound("Fact not found")
        return {**json.loads(row['payload']), 'id': fact_id, 'version': row['version'],
                'current_version': row['current_version'], 'status': row['status'],
                'created_at': row['created_at'], 'confirmed_at': row['confirmed_at']}

    def _list_facts(self, connection: sqlite3.Connection) -> list[dict]:
        return [self._fact(connection, row['fact_id']) for row in connection.execute(
            'SELECT fact_id FROM facts WHERE user_id = ? ORDER BY created_at, fact_id', (self.user_id,))]

    def _write_fact(self, connection, fact_id, version, payload):
        connection.execute('''INSERT INTO fact_versions
            (user_id, fact_id, version, payload, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)''',
                           (self.user_id, fact_id, version, payload, _now()))

    def _import(self, connection: sqlite3.Connection, normalized: list[tuple]) -> list[dict]:
        result = []
        for fact_id, text, kind, tags in normalized:
            payload = _fact_payload(text, kind, tags)
            if fact_id is None:
                existing = connection.execute('''SELECT f.fact_id FROM facts f JOIN fact_versions v
                    ON v.user_id = f.user_id AND v.fact_id = f.fact_id AND v.version = f.current_version
                    WHERE f.user_id = ? AND v.payload = ? ORDER BY f.fact_id LIMIT 1''',
                                              (self.user_id, payload)).fetchone()
                fact_id = existing['fact_id'] if existing else 'fact-' + secrets.token_hex(6)
            existing = connection.execute('SELECT 1 FROM facts WHERE user_id = ? AND fact_id = ?',
                                          (self.user_id, fact_id)).fetchone()
            if existing:
                current = self._fact(connection, fact_id)
                if _json({key: current[key] for key in ('text', 'fact_type', 'tags')}) != payload:
                    raise Conflict("Fact exists with different content; explicitly revise its current version")
            else:
                connection.execute('INSERT INTO facts VALUES (?, ?, 1, ?)', (self.user_id, fact_id, _now()))
                self._write_fact(connection, fact_id, 1, payload)
            result.append(self._fact(connection, fact_id))
        return result

    def import_facts(self, items: Any) -> list[dict]:
        """Import a batch atomically as pending; a changed existing ID requires revise_fact.

        Explicit imports cannot confirm, overwrite history, or reset a later correction.
        Generated IDs and payload deduplication are local to this user.
        """
        normalized = normalize_fact_import(items)
        with self._transaction(write=True) as connection:
            return self._import(connection, normalized)

    def get_fact(self, fact_id: str, *, version: int | None = None) -> dict:
        if version is not None:
            _version(version)
        with self._transaction() as connection:
            return self._fact(connection, _text(fact_id, 'fact_id'), version)

    def list_facts(self) -> list[dict]:
        with self._transaction() as connection:
            return self._list_facts(connection)

    def fact_snapshot(self) -> FactSnapshot:
        with self._transaction() as connection:
            return FactSnapshot(self._list_facts(connection))

    def revise_fact(self, fact_id: str, *, expected_version: int, text: str,
                    fact_type: str | None = None, tags: list[str]) -> dict:
        """Compare-and-set the current fact; a changed payload always starts pending. Without
        ``fact_type`` the current type is kept."""
        _version(expected_version)
        with self._transaction(write=True) as connection:
            current = self._fact(connection, _text(fact_id, 'fact_id'))
            normalized = normalize_fact_import([{'id': fact_id, 'text': text, 'type': fact_type or current['fact_type'],
                                                 'tags': tags}])[0]
            payload = _fact_payload(*normalized[1:])
            if current['version'] != expected_version:
                raise Conflict("Fact version changed")
            if _json({key: current[key] for key in ('text', 'fact_type', 'tags')}) == payload:
                return current
            version = expected_version + 1
            self._write_fact(connection, fact_id, version, payload)
            connection.execute('UPDATE facts SET current_version = ? WHERE user_id = ? AND fact_id = ?',
                               (version, self.user_id, fact_id))
            return self._fact(connection, fact_id)

    def confirm_facts(self, refs: list[tuple[str, int]]) -> list[dict]:
        """The user's review: confirm exact current versions together; any missing/stale ref rolls all back."""
        refs = _refs(refs)
        if not refs:
            raise StoreError("Choose facts to confirm")
        with self._transaction(write=True) as connection:
            for fact_id, version in refs:
                fact = self._fact(connection, fact_id)
                if fact['version'] != version:
                    raise Conflict("Fact version changed")
                connection.execute('''UPDATE fact_versions SET status = 'confirmed', confirmed_at = ?
                    WHERE user_id = ? AND fact_id = ? AND version = ? AND status = 'pending' ''',
                                   (_now(), self.user_id, fact_id, version))
            return [self._fact(connection, fact_id) for fact_id, _ in refs]

    # -- profiles --------------------------------------------------------------------------

    def _save_profile(self, connection: sqlite3.Connection, language: str, profile: Any,
                      expected_version: int) -> dict:
        payload = _json(profile)
        profile = json.loads(payload)
        ids = profile_fact_ids(profile, language)
        if language not in profile_languages(profile):
            raise StoreError("Profile is not written in this language")
        row = connection.execute('SELECT current_version FROM profiles WHERE user_id = ? AND language = ?',
                                 (self.user_id, language)).fetchone()
        current = row['current_version'] if row else 0
        if current != expected_version:
            raise Conflict("Profile version changed")
        for fact_id in ids:
            self._fact(connection, fact_id)
        if current:
            previous = self._profile(connection, language, current)
            if _json(previous['profile']) == payload:
                return previous
            connection.execute('UPDATE profiles SET current_version = ? WHERE user_id = ? AND language = ?',
                               (current + 1, self.user_id, language))
        else:
            connection.execute('INSERT INTO profiles VALUES (?, ?, 1)', (self.user_id, language))
        connection.execute('INSERT INTO profile_versions VALUES (?, ?, ?, ?, ?)',
                           (self.user_id, language, current + 1, payload, _now()))
        connection.executemany('INSERT INTO profile_facts VALUES (?, ?, ?, ?)',
                               [(self.user_id, language, current + 1, fact_id) for fact_id in ids])
        return self._profile(connection, language, current + 1)

    def save_profile(self, language: str, profile: Any, *, expected_version: int) -> dict:
        """Save a language independently; 0 means it must not already exist.

        A profile may refer to pending facts for later review, but never another user's.
        Profile validation and references reuse the same rules as the legacy renderer.
        """
        language = _language(language)
        _version(expected_version, zero=True)
        with self._transaction(write=True) as connection:
            return self._save_profile(connection, language, profile, expected_version)

    def _profile(self, connection, language, version=None):
        row = connection.execute('''SELECT v.*, p.current_version FROM profiles p JOIN profile_versions v
            ON v.user_id = p.user_id AND v.language = p.language
            WHERE p.user_id = ? AND p.language = ? AND v.version = COALESCE(?, p.current_version)''',
                                 (self.user_id, language, version)).fetchone()
        if row is None:
            raise NotFound("Profile not found")
        return {'language': language, 'version': row['version'], 'current_version': row['current_version'],
                'profile': json.loads(row['payload']), 'created_at': row['created_at']}

    def get_profile(self, language: str, *, version: int | None = None) -> dict:
        if version is not None:
            _version(version)
        with self._transaction() as connection:
            return self._profile(connection, _language(language), version)

    def _languages(self, connection: sqlite3.Connection) -> list[str]:
        have = {row['language'] for row in connection.execute('SELECT language FROM profiles WHERE user_id = ?',
                                                              (self.user_id,))}
        return [language for language in LANGUAGES if language in have]

    def cv_languages(self) -> list[str]:
        with self._transaction() as connection:
            return self._languages(connection)

    def _private_terms(self, connection: sqlite3.Connection) -> list[str]:
        """What requests must mask: the name, contact details, schools and employers of every
        profile version in every language, since an older fact can still name a past employer."""
        terms: set[str] = set()
        for row in connection.execute('SELECT payload FROM profile_versions WHERE user_id = ?', (self.user_id,)):
            terms.update(private_terms(json.loads(row['payload'])))
        return sorted(terms, key=len, reverse=True)

    def private_terms(self) -> list[str]:
        with self._transaction() as connection:
            return self._private_terms(connection)

    # -- consent to model data flows -------------------------------------------------------

    def consents(self) -> dict[str, Any]:
        with self._transaction() as connection:
            return self._consents(connection)

    def _consents(self, connection: sqlite3.Connection) -> dict[str, Any]:
        stored = json.loads(connection.execute('SELECT consents FROM users WHERE user_id = ?',
                                               (self.user_id,)).fetchone()['consents'])
        return {stage: stored[stage] for stage, version in CONSENT_STAGES.items()
                if isinstance(stored.get(stage), dict) and stored[stage].get("version") == version}

    def give_consent(self, stage: str, version: int) -> dict[str, Any]:
        """This user agrees to the data flow the page showed for ``stage`` (its current version only)."""
        if CONSENT_STAGES.get(stage) != version:
            raise Conflict("This notice has changed; read it again")
        with self._transaction(write=True) as connection:
            stored = json.loads(connection.execute('SELECT consents FROM users WHERE user_id = ?',
                                                   (self.user_id,)).fetchone()['consents'])
            stored[stage] = {"version": version, "agreed_at": _now()}
            connection.execute('UPDATE users SET consents = ? WHERE user_id = ?', (_json(stored), self.user_id))
            return self._consents(connection)

    # -- CV uploads ------------------------------------------------------------------------

    def _upload(self, connection: sqlite3.Connection, upload_id: str) -> dict[str, Any]:
        connection.execute('DELETE FROM uploads WHERE user_id = ? AND expires_at <= ?', (self.user_id, _now()))
        row = connection.execute('SELECT * FROM uploads WHERE user_id = ? AND upload_id = ? AND expires_at > ?',
                                 (self.user_id, _text(upload_id, 'upload_id', 64), _now())).fetchone()
        if row is None:
            raise NotFound("Upload not found")
        return {**json.loads(row['proposal']), 'upload_id': row['upload_id'], 'language': row['language'],
                'source_sha256': row['source_sha256']}

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        """A proposal waiting for the user to check its contact details and save it; a day old,
        it is gone (it holds contact details)."""
        with self._transaction(write=True) as connection:
            return self._upload(connection, upload_id)

    def delete_upload(self, upload_id: str) -> None:
        with self._transaction(write=True) as connection:
            connection.execute('DELETE FROM uploads WHERE user_id = ? AND upload_id = ?',
                               (self.user_id, _text(upload_id, 'upload_id', 64)))

    def commit_upload(self, upload_id: str, contact: dict[str, Any]) -> dict[str, int]:
        """The user checked the details: import the CV's lines as pending facts and make it this
        language's CV layout, all in one transaction, so no half-saved CV can remain."""
        with self._transaction(write=True) as connection:
            proposal = self._upload(connection, upload_id)
            language = proposal['language']
            facts = _TransactionFacts(self, connection)
            try:
                profile, items, reused = build_profile(proposal, contact, facts)
            except (CVError, FactStoreError, ValueError) as exc:
                raise StoreError(str(exc)) from exc
            if items:
                self._import(connection, normalize_fact_import(items))
            profile["name"] = {language: profile["name"]}
            row = connection.execute('SELECT current_version FROM profiles WHERE user_id = ? AND language = ?',
                                     (self.user_id, language)).fetchone()
            self._save_profile(connection, language, profile, row['current_version'] if row else 0)
            connection.execute('DELETE FROM uploads WHERE user_id = ? AND upload_id = ?', (self.user_id, upload_id))
            return {"imported": len(items), "reused": reused}

    # -- jobs, requirements and notes ------------------------------------------------------

    def _new_job(self, connection: sqlite3.Connection, jd: Any, request_id: str, *,
                 first_capture_wins: bool = False) -> dict:
        """The job ``request_id`` names, made now if it is new. The same request again finds the
        job as first captured: when it was captured (``captured_at``, the server's clock) is not
        part of what was asked, so a resend is never a new capture. Different words under the
        same request ID are refused, unless ``first_capture_wins`` (a task re-reading a posting
        on a retry: the job it made the first time is its job). The saved words' sponsor wording
        is assessed here, once; a resend that finds them unassessed (the rules failed) tries again."""
        snapshot = _jd_snapshot(jd)
        payload = _json(snapshot)
        existing = connection.execute('SELECT * FROM jobs WHERE user_id = ? AND request_id = ?',
                                      (self.user_id, request_id)).fetchone()
        if existing:
            def asked(stored: dict) -> dict:
                return {key: value for key, value in stored.items() if key != 'captured_at'}
            if not first_capture_wins and asked(json.loads(existing['payload'])) != asked(snapshot):
                raise Conflict("Request key was used for a different JD")
            job = self._job(connection, existing['job_id'])
        else:
            job_id = secrets.token_hex(16)
            connection.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?)',
                               (self.user_id, job_id, request_id, payload, _now()))
            job = self._job(connection, job_id)
        self._assess_saved_text(connection, job['jd'].get('text'))
        return job

    def _assess_saved_text(self, connection: sqlite3.Connection, text: Any) -> str:
        """Make sure words of this user's saved jobs have a sponsor answer under the current rules,
        inside the caller's write transaction. Returns 'stored' (one was there), 'shared',
        'assessed' or 'failed' (nothing stored; a later check tries again).

        Words that a board this user follows lists right now are public words with one shared
        answer, which is copied (it is one the user can already see). When the board's own
        assessment has not run yet, the shared answer is made here from that stored public
        posting's words, never from what the user sent, so each public text is still assessed
        once however many followers save it meanwhile. Other words are assessed for this user
        alone, and nothing of theirs is written where another user could read it."""
        if not isinstance(text, str):
            return 'failed'
        digest, version = hashlib.sha256(text.encode('utf-8')).hexdigest(), sponsorship.RULE_VERSION
        if connection.execute('''SELECT 1 FROM user_sponsor_assessments WHERE user_id = ? AND text_sha256 = ?
                AND rule_version = ?''', (self.user_id, digest, version)).fetchone():
            return 'stored'
        public = connection.execute('''SELECT p.text FROM public_postings p JOIN followed_boards f
            ON f.provider = p.provider AND f.board = p.board WHERE f.user_id = ? AND p.text_hash = ? LIMIT 1''',
                                    (self.user_id, digest)).fetchone()
        if public is not None:
            shared = connection.execute('''SELECT status, evidence, assessed_at FROM public_sponsor_assessments
                WHERE text_sha256 = ? AND rule_version = ?''', (digest, version)).fetchone()
            outcome = 'shared' if shared is not None else 'assessed'
            answer = tuple(shared) if shared is not None else _answer_public_text(connection, digest, public['text'])
            if answer is None:
                return 'failed'
        else:
            made = _sponsor_answer(text)
            if made is None:
                return 'failed'
            answer, outcome = (*made, _now()), 'assessed'
        connection.execute('INSERT INTO user_sponsor_assessments VALUES (?, ?, ?, ?, ?, ?)',
                           (self.user_id, digest, version, *answer))
        connection.execute('''DELETE FROM user_sponsor_assessments WHERE user_id = ? AND text_sha256 = ?
            AND rule_version != ?''', (self.user_id, digest, version))  # older rules' answer, replaced
        return outcome

    def _saved_answer(self, connection: sqlite3.Connection, text: Any) -> dict[str, Any] | None:
        """This user's stored sponsor answer for saved words under the current rules, or None."""
        if not isinstance(text, str):
            return None
        return _answer(connection.execute('''SELECT status, evidence, rule_version, assessed_at
            FROM user_sponsor_assessments WHERE user_id = ? AND text_sha256 = ? AND rule_version = ?''',
                                          (self.user_id, hashlib.sha256(text.encode('utf-8')).hexdigest(),
                                           sponsorship.RULE_VERSION)).fetchone())

    def assess_job_sponsorship(self, job_id: str) -> str:
        """The user's explicit check of one saved job's sponsor wording: for a job saved before
        answers were kept, after a check that failed, or once the rules changed. Words that have
        an answer under the current rules are not assessed again, so repeating it changes
        nothing. Returns what _assess_saved_text did."""
        with self._transaction(write=True) as connection:
            job = self._job(connection, _text(job_id, 'job_id'))
            return self._assess_saved_text(connection, job['jd'].get('text'))

    def create_job(self, jd: Any, *, request_id: str) -> dict:
        """Save an immutable JD snapshot; retry only repeats the same user request.

        Unknown source stays None. captured_at must be supplied with a timezone; it is
        acquisition time, not a claim that a vacancy is currently open.
        """
        request_id = _text(request_id, 'request_id')
        with self._transaction(write=True) as connection:
            return self._new_job(connection, jd, request_id)

    def paste_job(self, jd: Any, *, request_id: str, key: str | None, input_sha256: str, units: int,
                  consent: str | None, review_only: bool = False) -> dict[str, Any]:
        """Save a job the user pasted and queue the work that finds its requirements, in one
        transaction. ``request_id`` names the pasted words: sending them again (a resend after a
        lost answer, or the same paste later) finds the same job as first captured, and the work
        already queued, running or done for it, never a second run. ``key`` is the page's request
        key, bound here to what was pasted (``input_sha256``) on every path a paste is accepted
        by: new work, the job's requirements already found, work already under way, or the job
        saved while its work cannot be queued. Used before for anything else, it refuses the whole
        request: no job, no task, no units. When the work cannot be queued now (quota, queue,
        consent) the job stays saved and the refusal comes back with it, as
        {"job", "task", "refused"}."""
        request_id = _text(request_id, 'request_id')
        with self._transaction(write=True) as connection:
            self._bind_request(connection, 'paste_job', key, input_sha256)
            job = self._new_job(connection, jd, request_id)
            job_id = job['job_id']
            if review_only:
                return {'job': job, 'task': None, 'refused': None}
            if self._requirements(connection, job_id) is not None:
                return {'job': job, 'task': None, 'refused': None}
            ongoing = connection.execute("""SELECT * FROM tasks WHERE user_id = ? AND operation = 'prepare_job'
                AND job_id = ? AND status IN ('queued', 'running', 'succeeded') ORDER BY created_at DESC LIMIT 1""",
                                         (self.user_id, job_id)).fetchone()
            if ongoing is not None:
                return {'job': job, 'task': _task_view(ongoing, connection), 'refused': None}
            try:
                task = self._submit_task(connection, 'prepare_job', key=key or f"prepare-job:{job_id}:0",
                                         request={'job_id': job_id}, context={'base_requirements_version': 0},
                                         units=units, heavy='model', job_id=job_id, consent=consent)
            except Refused as exc:  # refused before anything was written: the job alone is kept
                return {'job': job, 'task': None, 'refused': exc}
            return {'job': job, 'task': task, 'refused': None}

    def _job(self, connection, job_id):
        row = connection.execute('SELECT * FROM jobs WHERE user_id = ? AND job_id = ?',
                                 (self.user_id, job_id)).fetchone()
        if row is None:
            raise NotFound("Job not found")
        return {'job_id': row['job_id'], 'jd': json.loads(row['payload']), 'created_at': row['created_at']}

    def get_job(self, job_id: str) -> dict:
        with self._transaction() as connection:
            return self._job(connection, _text(job_id, 'job_id'))

    def list_jobs(self) -> list[dict]:
        with self._transaction() as connection:
            return [self._job(connection, row['job_id']) for row in connection.execute(
                'SELECT job_id FROM jobs WHERE user_id = ? ORDER BY created_at, job_id', (self.user_id,))]

    def find_job(self, *, source: str | None = None, url: str | None = None,
                 identity: tuple[str, str, str] | None = None) -> str | None:
        """This user's newest job made from the same posting: its board identity, the link the
        user gave, or its official link. Other users' jobs are never looked at."""
        with self._transaction() as connection:
            rows = connection.execute('SELECT job_id, payload FROM jobs WHERE user_id = ? ORDER BY created_at DESC, job_id',
                                      (self.user_id,)).fetchall()
        for row in rows:
            jd = json.loads(row['payload'])
            if ((identity and (jd.get("provider"), jd.get("board"), jd.get("job_id")) == tuple(identity))
                    or (url and url in (jd.get("requested_url"), jd.get("source")))
                    or (source and jd.get("source") == source)):
                return row['job_id']
        return None

    def _requirements(self, connection: sqlite3.Connection, job_id: str) -> dict | None:
        row = connection.execute('''SELECT * FROM job_requirements WHERE user_id = ? AND job_id = ?
            ORDER BY version DESC LIMIT 1''', (self.user_id, job_id)).fetchone()
        if row is None:
            return None
        return {**json.loads(row['payload']), 'version': row['version'], 'created_at': row['created_at']}

    def requirements(self, job_id: str) -> dict | None:
        """The job's current requirement candidates and decisions ({candidates, decided}), or None."""
        with self._transaction() as connection:
            self._job(connection, _text(job_id, 'job_id'))
            return self._requirements(connection, job_id)

    def _save_requirements(self, connection: sqlite3.Connection, job_id: str, candidates: dict,
                           decided: dict | None, expected_version: int) -> int:
        self._job(connection, job_id)
        current = self._requirements(connection, job_id)
        if (current['version'] if current else 0) != expected_version:
            raise Conflict("This job's requirements changed")
        if decided is not None:
            build_report(decided)
        version = expected_version + 1
        connection.execute('INSERT INTO job_requirements VALUES (?, ?, ?, ?, ?)',
                           (self.user_id, job_id, version, _json({'candidates': candidates, 'decided': decided}), _now()))
        return version

    def save_requirements(self, job_id: str, candidates: dict, decided: dict | None, *,
                          expected_version: int) -> int:
        """A new version of the job's requirements: the candidates found and what counts. The
        page's version must still be current, so a stale page cannot overwrite a newer decision."""
        _version(expected_version, zero=True)
        with self._transaction(write=True) as connection:
            return self._save_requirements(connection, _text(job_id, 'job_id'), candidates, decided, expected_version)

    def _note(self, connection: sqlite3.Connection, job_id: str, name: str) -> dict | None:
        row = connection.execute('SELECT payload FROM job_notes WHERE user_id = ? AND job_id = ? AND name = ?',
                                 (self.user_id, job_id, name)).fetchone()
        return json.loads(row['payload']) if row else None

    def _set_note(self, connection: sqlite3.Connection, job_id: str, name: str, payload: dict) -> None:
        if name not in NOTE_NAMES:
            raise StoreError("Unknown note")
        self._job(connection, job_id)
        connection.execute('''INSERT INTO job_notes VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, job_id, name)
            DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at''',
                           (self.user_id, job_id, name, _json(payload), _now()))

    def set_note(self, job_id: str, name: str, payload: dict) -> None:
        """Small status records replaced in place, as the single-user workspace's notes: the CV
        language chosen for a job and how its latest CV preparation went."""
        with self._transaction(write=True) as connection:
            self._set_note(connection, _text(job_id, 'job_id'), name, payload)

    def note(self, job_id: str, name: str) -> dict | None:
        with self._transaction() as connection:
            self._job(connection, _text(job_id, 'job_id'))
            return self._note(connection, job_id, name)

    # -- CV materials: versions, approvals and final PDFs ----------------------------------

    def _material_row(self, connection, material_id):
        row = connection.execute('SELECT * FROM materials WHERE user_id = ? AND material_id = ?',
                                 (self.user_id, material_id)).fetchone()
        if row is None:
            raise NotFound("Material not found")
        return row

    def _inputs_current(self, connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
        """Whether the profile, every fact and the requirements a CV was made from are still the
        current versions."""
        try:
            current = self._profile(connection, row['language'])['version'] == row['profile_version']
        except NotFound:
            return False
        if row['requirements_version'] is not None:
            requirements = self._requirements(connection, row['job_id'])
            current = current and requirements is not None and requirements['version'] == row['requirements_version']
        for ref in connection.execute('SELECT fact_id, fact_version FROM material_facts WHERE user_id = ? AND material_id = ?',
                                      (self.user_id, row['material_id'])):
            fact = self._fact(connection, ref['fact_id'])
            current = current and fact['version'] == ref['fact_version']
        return current

    def _material(self, connection, material_id):
        row = self._material_row(connection, material_id)
        return {'material_id': row['material_id'], 'job_id': row['job_id'], 'language': row['language'],
                'version': row['version'], 'stage': row['stage'], 'parent_id': row['parent_id'],
                'profile_version': row['profile_version'], 'requirements_version': row['requirements_version'],
                'content_sha256': row['content_sha256'], 'draft': json.loads(row['payload']),
                'inputs_current': self._inputs_current(connection, row), 'created_at': row['created_at']}

    def get_material(self, material_id: str) -> dict:
        """Historical content remains readable by its owner; stale inputs are explicit."""
        with self._transaction() as connection:
            return self._material(connection, _text(material_id, 'material_id'))

    def list_materials(self, job_id: str) -> list[dict]:
        with self._transaction() as connection:
            self._job(connection, _text(job_id, 'job_id'))
            return [self._material(connection, row['material_id']) for row in connection.execute(
                'SELECT material_id FROM materials WHERE user_id = ? AND job_id = ? ORDER BY language, version',
                (self.user_id, job_id))]

    def _head(self, connection: sqlite3.Connection, job_id: str, language: str) -> sqlite3.Row | None:
        return connection.execute('''SELECT * FROM materials WHERE user_id = ? AND job_id = ? AND language = ?
            ORDER BY version DESC LIMIT 1''', (self.user_id, job_id, language)).fetchone()

    def _insert_material(self, connection: sqlite3.Connection, *, job_id: str, language: str, stage: str,
                         parent_id: str | None, profile_version: int, requirements_version: int | None,
                         request_id: str, request_payload: str, document: dict,
                         facts: list[tuple[str, int]]) -> str:
        version = connection.execute('''SELECT COALESCE(MAX(version), 0) + 1 FROM materials
            WHERE user_id = ? AND job_id = ? AND language = ?''', (self.user_id, job_id, language)).fetchone()[0]
        material_id = secrets.token_hex(16)
        connection.execute('INSERT INTO materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (self.user_id, material_id, job_id, language, version, stage, parent_id, profile_version,
                            requirements_version, request_id, request_payload, _json(document),
                            content_fingerprint(document), _now()))
        connection.executemany('INSERT INTO material_facts VALUES (?, ?, ?, ?)',
                               [(self.user_id, material_id, fact_id, fact_version) for fact_id, fact_version in facts])
        return material_id

    def create_draft(self, job_id: str, language: str, *, request_id: str,
                     expected_profile_version: int, expected_facts: list[tuple[str, int]]) -> dict:
        """Store a DRAFT built by existing rules from exactly the caller's input versions.

        No model or printer is called. It cannot accept a supplied material payload, confirm
        facts, approve or export. The input check, fact snapshot and material publication are
        one short transaction, with no network work inside. The draft is not tailored to the
        job's requirements (see publish_cv for the task that is).
        """
        _text(job_id, 'job_id')
        _text(request_id, 'request_id')
        _language(language)
        _version(expected_profile_version)
        refs = _refs(expected_facts)
        request_payload = _json([job_id, language, expected_profile_version, refs])
        with self._transaction(write=True) as connection:
            previous = connection.execute('SELECT * FROM materials WHERE user_id = ? AND request_id = ?',
                                          (self.user_id, request_id)).fetchone()
            if previous:
                if previous['request_payload'] != request_payload:
                    raise Conflict("Request key was used for different inputs")
                return self._material(connection, previous['material_id'])
            job = self._job(connection, job_id)
            profile = self._profile(connection, language)
            if profile['version'] != expected_profile_version:
                raise Conflict("Profile version changed")
            needed = profile_fact_ids(profile['profile'], language)
            if set(needed) != {fact_id for fact_id, _ in refs}:
                raise Conflict("Expected facts must exactly match this profile")
            facts = {}
            for fact_id, version in refs:
                fact = self._fact(connection, fact_id)
                if fact['version'] != version:
                    raise Conflict("Fact version changed")
                facts[fact_id] = fact
            draft = build_draft_from_facts(profile['profile'], facts, language,
                                          job={'jd': job['jd'], 'facts': [], 'selected_requirements': []})
            material_id = self._insert_material(
                connection, job_id=job_id, language=language, stage='draft', parent_id=None,
                profile_version=profile['version'], requirements_version=None, request_id=request_id,
                request_payload=request_payload, document=draft, facts=refs)
            return self._material(connection, material_id)

    def cv_inputs(self, job_id: str, language: str) -> dict[str, Any]:
        """Everything a CV preparation reads, in one transaction: the profile and requirement
        versions, the facts, the private words to mask and the current CV. The task publishes
        against exactly these versions later."""
        with self._transaction() as connection:
            self._job(connection, _text(job_id, 'job_id'))
            language = _language(language)
            try:
                profile = self._profile(connection, language)
            except NotFound:
                profile = None
            head = self._head(connection, job_id, language)
            chain = []  # the current CV and every version it grew from, newest first
            row = head
            while row is not None:
                chain.append(self._material(connection, row['material_id']))
                row = self._material_row(connection, row['parent_id']) if row['parent_id'] else None
            return {
                'profile': profile, 'requirements': self._requirements(connection, job_id),
                'facts': FactSnapshot(self._list_facts(connection)), 'private': self._private_terms(connection),
                'head': chain[0] if chain else None, 'chain': chain,
                'status': self._note(connection, job_id, f'cv-status-{language}'),
            }

    def _made_from_current(self, connection: sqlite3.Connection, job_id: str, language: str,
                           documents: list[tuple[str, dict]], profile_version: int,
                           requirements_version: int | None) -> bool:
        """Whether CVs made from these inputs are still current: the language's profile and the
        job's requirements are the versions the task read, and every fact the CVs show is still
        that version."""
        try:
            if self._profile(connection, language)['version'] != profile_version:
                return False
        except NotFound:
            return False
        requirements = self._requirements(connection, job_id)
        if (requirements['version'] if requirements else None) != requirements_version:
            return False
        for fact_id, version in {(item['id'], item['version']) for _, document in documents for item in document['facts']}:
            try:
                fact = self._fact(connection, fact_id)
            except NotFound:
                return False
            if fact['version'] != version:
                return False
        return True

    def publish_cv(self, *, task_id: str, token: str, job_id: str, language: str, base_head: str | None,
                   documents: list[tuple[str, dict]], profile_version: int, requirements_version: int | None,
                   stages: list[dict] | None, draft_id: str | None = None, result: dict | None = None) -> dict:
        """Publish what one task produced: new CV versions for ``stage`` → document, each derived
        from the one before (the first from ``draft_id``, when a later stage is redone), and how
        the stages went. Only while the task still holds its execution token and its time, only
        if no other CV was published for this job and language since the task read its inputs,
        and only if those inputs (profile, requirements and every fact shown) are still current;
        otherwise nothing is kept and the task is marked superseded."""
        with self._transaction(write=True) as connection:
            _running_task(connection, self.user_id, task_id, token)
            head = self._head(connection, job_id, language)
            if (head['material_id'] if head else None) != base_head:
                _end_task(connection, self.user_id, task_id, 'superseded', error_code='superseded',
                          message="A newer CV was made while this one was being prepared")
                return {'superseded': True}
            if not self._made_from_current(connection, job_id, language, documents, profile_version,
                                           requirements_version):
                _end_task(connection, self.user_id, task_id, 'superseded', error_code='inputs_changed',
                          message="The facts, profile or requirements changed while this CV was being prepared")
                return {'superseded': True}
            parent, published = draft_id, []
            for stage, document in documents:
                facts = [(item['id'], item['version']) for item in document['facts']]
                parent = self._insert_material(
                    connection, job_id=job_id, language=language, stage=stage, parent_id=parent,
                    profile_version=profile_version, requirements_version=requirements_version,
                    request_id=f"{task_id}:{len(published)}", request_payload=_json([task_id, stage]),
                    document=document, facts=facts)
                published.append(parent)
            if stages is not None:
                root = published[0] if published and documents[0][0] == 'draft' else draft_id
                self._set_note(connection, job_id, f'cv-status-{language}',
                               {'draft_material_id': root, 'stages': stages, 'updated_at': _now()})
            outcome = {**(result or {}), 'materials': published}
            _end_task(connection, self.user_id, task_id, 'succeeded', result=outcome)
            return outcome

    def record_stages(self, *, task_id: str, token: str, job_id: str, language: str, draft_id: str | None,
                      stages: list[dict]) -> None:
        """How a preparation that produced nothing went (for example, a fact still pending)."""
        with self._transaction(write=True) as connection:
            _running_task(connection, self.user_id, task_id, token)
            self._set_note(connection, job_id, f'cv-status-{language}',
                           {'draft_material_id': draft_id, 'stages': stages, 'updated_at': _now()})

    def change_cv(self, job_id: str, language: str, change_id: str, undone: bool) -> dict:
        """Undo or redo one of the job adjustments the user sees listed: a new CV version, so the
        old one (and any approval of it) stays in history unchanged."""
        with self._transaction(write=True) as connection:
            head = self._head(connection, _text(job_id, 'job_id'), _language(language))
            if head is None or head['stage'] != 'planned':
                raise NotFound("There is no adjusted CV to change")
            document = json.loads(head['payload'])
            try:
                changed = set_change(document, change_id, undone)
            except CVError as exc:
                raise StoreError(str(exc)) from exc
            if content_fingerprint(changed) == head['content_sha256']:
                return self._material(connection, head['material_id'])
            facts = [(ref['fact_id'], ref['fact_version']) for ref in connection.execute(
                'SELECT fact_id, fact_version FROM material_facts WHERE user_id = ? AND material_id = ?',
                (self.user_id, head['material_id']))]
            material_id = self._insert_material(
                connection, job_id=job_id, language=language, stage='planned', parent_id=head['material_id'],
                profile_version=head['profile_version'], requirements_version=head['requirements_version'],
                request_id=f"change:{secrets.token_hex(8)}", request_payload=_json([change_id, undone]),
                document=changed, facts=facts)
            return self._material(connection, material_id)

    def _approval(self, connection: sqlite3.Connection, material_id: str) -> sqlite3.Row | None:
        return connection.execute('SELECT * FROM approvals WHERE user_id = ? AND material_id = ?',
                                  (self.user_id, material_id)).fetchone()

    def _valid_approval(self, connection: sqlite3.Connection, job_id: str, language: str) -> sqlite3.Row | None:
        """The approval of the current CV, if it still holds: the CV is still the newest version,
        its inputs are still current and its content is still what the reviewer read."""
        head = self._head(connection, job_id, language)
        if head is None:
            return None
        approval = self._approval(connection, head['material_id'])
        if approval is None or approval['content_sha256'] != head['content_sha256']:
            return None
        if not self._inputs_current(connection, head):
            return None
        try:
            if not is_final_approval(json.loads(approval['payload'])):
                return None
        except CVError:
            return None
        return approval

    def approve(self, job_id: str, language: str, expected_content_sha256: str) -> dict:
        """The user approves exactly the CV their page showed (its fingerprint). Refused if the
        CV changed since, or if its facts, profile or requirements are no longer current, so an
        old page can never approve a newer or outdated CV. The reviewer is the signed-in owner.
        Approving the same CV again changes nothing."""
        _text(expected_content_sha256, 'expected content', 128)
        with self._transaction(write=True) as connection:
            head = self._head(connection, _text(job_id, 'job_id'), _language(language))
            if head is None:
                raise NotFound("There is no CV to approve")
            if head['content_sha256'] != expected_content_sha256:
                raise Conflict("This CV changed after the page showed it")
            if not self._inputs_current(connection, head):
                raise Conflict("This CV's facts, profile or requirements changed; prepare it again")
            existing = self._approval(connection, head['material_id'])
            if existing:
                return dict(existing)
            document = json.loads(head['payload'])
            facts = FactSnapshot(self._list_facts(connection))
            try:
                approved = approve_draft(document, facts)
            except CVError as exc:
                raise Conflict(str(exc)) from exc
            approval_id = secrets.token_hex(16)
            connection.execute('INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?)',
                               (self.user_id, approval_id, head['material_id'], self.user_id,
                                head['content_sha256'], _json(approved), approved['approval']['approved_at']))
            return dict(self._approval(connection, head['material_id']))

    def export_inputs(self, job_id: str, language: str) -> dict[str, Any]:
        """The approved CV to print and the facts it rests on; refused without a valid approval."""
        with self._transaction() as connection:
            self._job(connection, _text(job_id, 'job_id'))
            approval = self._valid_approval(connection, job_id, _language(language))
            if approval is None:
                raise Conflict("Approve the current CV before creating its final PDF")
            return {'approval_id': approval['approval_id'], 'document': json.loads(approval['payload']),
                    'facts': FactSnapshot(self._list_facts(connection))}

    def publish_pdf(self, *, task_id: str, token: str, approval_id: str, data: bytes, pages: int | None) -> dict:
        """Register a printed PDF, written whole, as the final PDF of ``approval_id``, only while
        that approval is still valid and the task still holds its token. If the CV changed while
        it printed, nothing is kept and the task is superseded, so no final PDF of an outdated
        CV is ever offered."""
        if not isinstance(data, bytes) or not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
            raise StoreError("Not a usable PDF")
        with self._transaction(write=True) as connection:
            _running_task(connection, self.user_id, task_id, token)
            approval = connection.execute('SELECT * FROM approvals WHERE user_id = ? AND approval_id = ?',
                                          (self.user_id, approval_id)).fetchone()
            material = self._material_row(connection, approval['material_id']) if approval else None
            valid = material is not None and self._valid_approval(connection, material['job_id'], material['language'])
            if not valid or valid['approval_id'] != approval_id:
                _end_task(connection, self.user_id, task_id, 'superseded', error_code='superseded',
                          message="The CV changed while its PDF was printed; approve it again")
                return {'superseded': True}
            existing = connection.execute('SELECT artifact_id FROM artifacts WHERE user_id = ? AND approval_id = ?',
                                          (self.user_id, approval_id)).fetchone()
            artifact_id = existing['artifact_id'] if existing else secrets.token_hex(16)
            if not existing:
                connection.execute('INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)',
                                   (self.user_id, artifact_id, approval_id, data, hashlib.sha256(data).hexdigest(),
                                    pages, _now()))
            outcome = {'artifact_id': artifact_id, 'pages': pages}
            _end_task(connection, self.user_id, task_id, 'succeeded', result=outcome)
            return outcome

    def final_pdf(self, job_id: str, language: str) -> bytes:
        """The final PDF of the current, still valid approval; bytes are checked against the
        hash recorded when it was registered."""
        with self._transaction() as connection:
            approval = self._valid_approval(connection, _text(job_id, 'job_id'), _language(language))
            row = connection.execute('SELECT data, sha256 FROM artifacts WHERE user_id = ? AND approval_id = ?',
                                     (self.user_id, approval['approval_id'])).fetchone() if approval else None
        if row is None:
            raise NotFound("There is no final PDF of the current approved CV")
        data = bytes(row['data'])
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise StoreError("The stored PDF is damaged")
        return data

    def preview(self, job_id: str, language: str, expected_content_sha256: str | None = None) -> tuple[dict, bool]:
        """The current CV as it prints, and whether it prints as final (approved and still valid)."""
        with self._transaction() as connection:
            head = self._head(connection, _text(job_id, 'job_id'), _language(language))
            if head is None:
                raise NotFound("There is no CV yet")
            if expected_content_sha256 and expected_content_sha256 != head['content_sha256']:
                raise Conflict("This CV changed after the page showed it")
            approval = self._valid_approval(connection, job_id, language)
            if approval is not None:
                return json.loads(approval['payload']), True
            existing = self._approval(connection, head['material_id'])
            return json.loads(existing['payload'] if existing else head['payload']), False

    # -- gaps ------------------------------------------------------------------------------

    def _gaps(self, connection: sqlite3.Connection, job_id: str) -> dict | None:
        row = connection.execute('SELECT * FROM job_gaps WHERE user_id = ? AND job_id = ? ORDER BY version DESC LIMIT 1',
                                 (self.user_id, job_id)).fetchone()
        return {'version': row['version'], 'gaps': json.loads(row['payload'])} if row else None

    def gaps_inputs(self, job_id: str, language: str) -> dict[str, Any]:
        """What a gap check reads: requirements, the current CV, facts, private words, the last check.
        The check is published against the same CV and requirements (publish_gaps)."""
        with self._transaction() as connection:
            self._job(connection, _text(job_id, 'job_id'))
            head = self._head(connection, job_id, _language(language))
            return {'requirements': self._requirements(connection, job_id),
                    'head': self._material(connection, head['material_id']) if head else None,
                    'facts': FactSnapshot(self._list_facts(connection)), 'private': self._private_terms(connection),
                    'gaps': self._gaps(connection, job_id)}

    def publish_gaps(self, *, task_id: str, token: str, job_id: str, base_version: int, gaps: dict,
                     checked_head: str | None, requirements_version: int | None,
                     facts_read: list | None = None) -> dict:
        """Keep a new gap check unless another was saved after this one started, or the CV
        (``checked_head``), the requirements or the current material it used changed meanwhile;
        then nothing is kept and the task is superseded. A suggestion added to an earlier check,
        which may predate a line adopted since, names the facts it read in ``facts_read``."""
        with self._transaction(write=True) as connection:
            _running_task(connection, self.user_id, task_id, token)
            current = self._gaps(connection, job_id)
            if (current['version'] if current else 0) != base_version:
                _end_task(connection, self.user_id, task_id, 'superseded', error_code='superseded',
                          message="A newer check was saved first")
                return {'superseded': True}
            head = self._head(connection, job_id, _language(gaps.get('language')))
            requirements = self._requirements(connection, job_id)
            confirmed = sorted([fact['id'], fact['version']] for fact in self._list_facts(connection))
            if ((head['material_id'] if head else None) != checked_head
                    or (requirements['version'] if requirements else None) != requirements_version
                    or confirmed != (gaps.get('checked_facts') if facts_read is None else facts_read)):
                _end_task(connection, self.user_id, task_id, 'superseded', error_code='inputs_changed',
                          message="The CV, its facts or the requirements changed while they were checked")
                return {'superseded': True}
            connection.execute('INSERT INTO job_gaps VALUES (?, ?, ?, ?, ?)',
                               (self.user_id, job_id, base_version + 1, _json(gaps), _now()))
            outcome = {'gaps_version': base_version + 1}
            _end_task(connection, self.user_id, task_id, 'succeeded', result=outcome)
            return outcome

    def _change_gaps(self, job_id: str, change: Callable) -> dict:
        with self._transaction(write=True) as connection:
            self._job(connection, _text(job_id, 'job_id'))
            current = self._gaps(connection, job_id)
            if current is None:
                raise NotFound("Check this CV first")
            gaps = current['gaps']
            language = gaps.get('language')
            try:
                updated, profile = change(connection, gaps, language)
            except (ValueError, CVError, FactStoreError) as exc:
                raise StoreError(str(exc)) from exc
            if updated == gaps and profile is None:
                # The same line again (a second click, a tab left open): saved already.
                return {'gaps_version': current['version'], 'language': language, 'changed': False}
            if profile is not None:
                self._save_profile(connection, language, profile, self._profile(connection, language)['version'])
            connection.execute('INSERT INTO job_gaps VALUES (?, ?, ?, ?, ?)',
                               (self.user_id, job_id, current['version'] + 1, _json(updated), _now()))
            return {'gaps_version': current['version'] + 1, 'language': language, 'changed': True}

    def accept_gap(self, job_id: str, requirement_id: str) -> dict:
        """The user adopts a suggested line: it is saved with legacy status, the CV layout
        lists it and the gap is marked added, all in one transaction (see gaps.accept_gap)."""
        def change(connection, gaps, language):
            profile = self._profile(connection, language)['profile']
            return accept_gap(gaps, _text(requirement_id, 'requirement'), _TransactionFacts(self, connection), profile)
        return self._change_gaps(job_id, change)

    def write_gap_line(self, job_id: str, requirement_id: str, place: str, text: str) -> dict:
        """The user writes the line themselves (see gaps.write_line), in one transaction."""
        def change(connection, gaps, language):
            head = self._head(connection, job_id, language)
            if head is None:
                raise ValueError("Prepare this job's CV first")
            profile = self._profile(connection, language)['profile']
            return write_line(gaps, _text(requirement_id, 'requirement'), _text(place, 'place', 2000), text,
                              json.loads(head['payload']), _TransactionFacts(self, connection), profile)
        return self._change_gaps(job_id, change)

    def decline_gap(self, job_id: str, requirement_id: str) -> dict:
        return self._change_gaps(job_id, lambda _c, gaps, _l: (decline_gap(gaps, _text(requirement_id, 'requirement')), None))

    # -- everything one job's page shows, read together ------------------------------------

    def job_snapshot(self, job_id: str) -> dict[str, Any]:
        """The job, its requirements, notes, the current CV per language (with the draft it grew
        from, whether its inputs are current and its approval and final PDF, if still valid),
        the latest gap check, current saved material, the stored sponsor answer for the job's words
        (None when not assessed: nothing is assessed while reading) and this job's recent tasks,
        from one transaction, so they agree with each other."""
        with self._transaction() as connection:
            job = self._job(connection, _text(job_id, 'job_id'))
            cvs = {}
            for language in LANGUAGES:
                head = self._head(connection, job_id, language)
                if head is None:
                    cvs[language] = None
                    continue
                root = head
                while root['parent_id'] is not None:
                    root = self._material_row(connection, root['parent_id'])
                approval = self._valid_approval(connection, job_id, language)
                artifact = connection.execute('SELECT artifact_id, pages FROM artifacts WHERE user_id = ? AND approval_id = ?',
                                              (self.user_id, approval['approval_id'])).fetchone() if approval else None
                stale_approval = self._approval(connection, head['material_id'])
                cvs[language] = {
                    'material': self._material(connection, head['material_id']),
                    'draft': json.loads(root['payload']), 'draft_id': root['material_id'],
                    'approval': ({'approval_id': approval['approval_id'], 'approved_at': approval['approved_at'],
                                  'document': json.loads(approval['payload'])} if approval else None),
                    'approved_but_outdated': bool(stale_approval) and approval is None,
                    'final_pdf': bool(artifact), 'pages': artifact['pages'] if artifact else None,
                }
            return {
                'facts': self._list_facts(connection),
                'sponsorship': self._saved_answer(connection, job['jd'].get('text')),
                'job': job, 'requirements': self._requirements(connection, job_id),
                'notes': {name: self._note(connection, job_id, name) for name in NOTE_NAMES},
                'cv': cvs, 'cv_languages': self._languages(connection), 'gaps': self._gaps(connection, job_id),
                'confirmed': [(fact['id'], fact['version']) for fact in self._list_facts(connection)],
                'tasks': [_task_view(row, connection) for row in connection.execute(
                    '''SELECT * FROM tasks WHERE user_id = ? AND job_id = ? AND dismissed = 0
                       ORDER BY created_at DESC LIMIT 10''', (self.user_id, job_id)).fetchall()],
            }

    def job_summaries(self) -> list[dict[str, Any]]:
        """Every job, newest first, with what the jobs list shows: title, company, source, when
        it was captured and which steps exist (requirements decided, CV approved per language)."""
        with self._transaction() as connection:
            summaries = []
            for row in connection.execute('SELECT job_id FROM jobs WHERE user_id = ? ORDER BY created_at DESC, job_id',
                                          (self.user_id,)).fetchall():
                job = self._job(connection, row['job_id'])
                steps = ['input']
                requirements = self._requirements(connection, job['job_id'])
                if requirements:
                    steps.append('candidates')
                    if requirements.get('decided'):
                        steps.append('decided')
                for language in LANGUAGES:
                    head = self._head(connection, job['job_id'], language)
                    if head is not None:
                        steps.append(f"cv-{head['stage']}-{language}")
                        if self._valid_approval(connection, job['job_id'], language) is not None:
                            steps.append(f'cv-approved-{language}')
                jd = job['jd']
                summaries.append({'job_id': job['job_id'], 'title': jd.get('title'),
                                  'company': jd.get('company') or jd.get('board'), 'source': jd.get('source'),
                                  'captured_at': jd.get('captured_at'), 'steps': steps})
            return summaries

    # -- followed job boards and their public postings -------------------------------------

    def followed_boards(self) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            return self._boards(connection)

    def _boards(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute('''SELECT f.provider, f.board, f.company, f.added_at, f.fetched_at, f.error,
                   (SELECT COUNT(*) FROM public_postings p WHERE p.provider = f.provider AND p.board = f.board) AS open_count
            FROM followed_boards f WHERE f.user_id = ? ORDER BY f.company COLLATE NOCASE, f.provider, f.board''',
                                 (self.user_id,)).fetchall()
        return [dict(row) for row in rows]

    def follows(self, provider: str, board: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM followed_boards WHERE user_id = ? AND provider = ? AND board = ?',
                                     (self.user_id, provider, board)).fetchone()
            return dict(row) if row else None

    def follow_board(self, provider: str, board: str, company: str) -> None:
        with self._transaction(write=True) as connection:
            exists = connection.execute('SELECT company FROM followed_boards WHERE user_id = ? AND provider = ? AND board = ?',
                                        (self.user_id, provider, board)).fetchone()
            if exists is not None:
                raise Conflict(f"Already following {exists['company']}")
            connection.execute('INSERT INTO followed_boards VALUES (?, ?, ?, ?, ?, ?, NULL)',
                               (self.user_id, provider, board, _text(company, 'company', 500), _now(), _now()))

    def record_board_read(self, provider: str, board: str, error: str | None) -> None:
        """When this user last had this board read, and why it failed if it did."""
        with self._transaction(write=True) as connection:
            if error is None:
                connection.execute('''UPDATE followed_boards SET fetched_at = ?, error = NULL
                    WHERE user_id = ? AND provider = ? AND board = ?''', (_now(), self.user_id, provider, board))
            else:
                connection.execute('''UPDATE followed_boards SET error = ?
                    WHERE user_id = ? AND provider = ? AND board = ?''', (error[:500], self.user_id, provider, board))

    def unfollow_board(self, provider: str, board: str) -> None:
        with self._transaction(write=True) as connection:
            removed = connection.execute('DELETE FROM followed_boards WHERE user_id = ? AND provider = ? AND board = ?',
                                         (self.user_id, provider, board))
            if removed.rowcount != 1:
                raise NotFound("Not following this company")
            connection.execute('DELETE FROM posting_matches WHERE user_id = ? AND provider = ? AND board = ?',
                               (self.user_id, provider, board))

    def followed_posting(self, provider: str, board: str, job_id: str) -> dict[str, Any]:
        """One stored public posting of a board this user follows."""
        with self._transaction() as connection:
            row = connection.execute('''SELECT p.provider, p.board, p.job_id, p.title, p.source, f.company
                FROM public_postings p JOIN followed_boards f ON f.provider = p.provider AND f.board = p.board
                WHERE f.user_id = ? AND p.provider = ? AND p.board = ? AND p.job_id = ?''',
                                 (self.user_id, provider, board, job_id)).fetchone()
        if row is None:
            raise NotFound("This job is no longer in the downloaded list; refresh its company first")
        return dict(row)

    def ranking_rows(self, terms: list[str], find: Callable[[str], list[str]]) -> list[dict[str, Any]]:
        """Public postings of the boards this user follows, each with its stored text (for the
        listing filters to read), the user's saved skill words it mentions and the stored
        sponsor answer for its words (read only: one not yet assessed shows as such). Matches are
        this user's own cache: redone for a posting whose text changed or when the user's
        saved skills change; never shared with another user."""
        key = hashlib.sha256(json.dumps(terms, ensure_ascii=False).encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            rows = connection.execute('''SELECT p.provider, p.board, p.job_id, p.title, p.location, p.source,
                       p.posted_at, p.text, p.text_hash, p.last_seen_at, f.company,
                       m.terms_key, m.text_hash AS matched_hash, m.matched_tags,
                       s.status, s.evidence, s.rule_version, s.assessed_at
                FROM followed_boards f JOIN public_postings p ON p.provider = f.provider AND p.board = f.board
                LEFT JOIN posting_matches m ON m.user_id = f.user_id AND m.provider = p.provider
                     AND m.board = p.board AND m.job_id = p.job_id
                LEFT JOIN public_sponsor_assessments s ON s.text_sha256 = p.text_hash AND s.rule_version = ?
                WHERE f.user_id = ? ORDER BY p.provider, p.board, p.job_id''',
                                      (sponsorship.RULE_VERSION, self.user_id)).fetchall()
        # Matching runs outside any transaction; only the new matches are written, in one short one.
        result, found = [], []
        for row in rows:
            if row['terms_key'] == key and row['matched_hash'] == row['text_hash']:
                matched = json.loads(row['matched_tags'])
            else:
                matched = find(row['title'] + "\n" + row['text'])
                found.append((self.user_id, row['provider'], row['board'], row['job_id'], row['text_hash'], key,
                              json.dumps(matched, ensure_ascii=False)) + (row['provider'], row['board'],
                                                                          row['job_id'], row['text_hash']))
            result.append({**{name: row[name] for name in ('provider', 'board', 'job_id', 'title', 'location',
                                                            'source', 'posted_at', 'text', 'text_hash',
                                                            'last_seen_at', 'company')}, 'matched': matched,
                           'sponsorship': sponsorship.shown(_answer(row if row['status'] else None),
                                                            source=row['source'], captured_at=row['last_seen_at'])})
        if found:
            with self._transaction(write=True) as connection:
                # A posting replaced or closed meanwhile is skipped: its match would describe other text.
                connection.executemany('''INSERT INTO posting_matches SELECT ?, ?, ?, ?, ?, ?, ?
                    WHERE EXISTS (SELECT 1 FROM public_postings WHERE provider = ? AND board = ? AND job_id = ?
                                  AND text_hash = ?)
                    ON CONFLICT(user_id, provider, board, job_id) DO UPDATE SET text_hash = excluded.text_hash,
                    terms_key = excluded.terms_key, matched_tags = excluded.matched_tags''', found)
        return result

    # -- what tasks publish besides CVs, gap checks and PDFs --------------------------------

    def publish_upload(self, *, task_id: str, token: str, proposal: dict, language: str,
                       source_sha256: str) -> dict:
        """Keep a parsed CV upload for the user to check and save (a day at most)."""
        _language(language)
        _text(source_sha256, 'source hash', 64)
        with self._transaction(write=True) as connection:
            _running_task(connection, self.user_id, task_id, token)
            upload_id = secrets.token_hex(8)
            now = _clock()
            connection.execute('INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?, ?)',
                               (self.user_id, upload_id, language, source_sha256, _json(proposal), _stamp(now),
                                _stamp(now + UPLOAD_KEEP)))
            outcome = {'upload_id': upload_id}
            _end_task(connection, self.user_id, task_id, 'succeeded', result=outcome)
            _scrub_request(connection, self.user_id, task_id)
            return outcome

    def task_job(self, *, task_id: str, token: str, jd: Any) -> dict:
        """The job a task makes from a posting it read: one per task, however often it runs; a
        retry that reads the posting again keeps the job (and capture) of the first attempt."""
        with self._transaction(write=True) as connection:
            _running_task(connection, self.user_id, task_id, token)
            job = self._new_job(connection, jd, f"task:{task_id}", first_capture_wins=True)
            connection.execute('UPDATE tasks SET job_id = ? WHERE user_id = ? AND task_id = ?',
                               (job['job_id'], self.user_id, task_id))
            return job

    def task_requirements(self, *, task_id: str, token: str, job_id: str, base_version: int,
                          candidates: dict, decided: dict | None) -> int | None:
        """Save the requirements a task found, unless another version was saved since it read
        the job (then None: the task is superseded). A retry of the same task that already
        saved them finds its own version and keeps it. The task goes on to prepare the CV."""
        with self._transaction(write=True) as connection:
            _running_task(connection, self.user_id, task_id, token)
            current = self._requirements(connection, job_id)
            if current is not None and current.get('task_id') == task_id:
                return current['version']
            if (current['version'] if current else 0) != base_version:
                _end_task(connection, self.user_id, task_id, 'superseded', error_code='superseded',
                          message="The requirements were changed while they were being found")
                return None
            if decided is not None:
                build_report(decided)
            connection.execute('INSERT INTO job_requirements VALUES (?, ?, ?, ?, ?)',
                               (self.user_id, job_id, base_version + 1,
                                _json({'candidates': candidates, 'decided': decided, 'task_id': task_id}), _now()))
            return base_version + 1

    # -- tasks -----------------------------------------------------------------------------

    def _bind_request(self, connection: sqlite3.Connection, operation: str, key: str | None, digest: str) -> None:
        """Bind the page's request key to what this request asks (``digest``), in the transaction
        that accepts the request; a key already bound to anything else is refused before anything
        is written. A refused or failed request rolls its binding back with everything else."""
        if key is None:
            return
        _text(operation, 'operation', 64)
        _text(key, 'request key', 200)
        row = connection.execute('SELECT input_sha256 FROM request_keys WHERE user_id = ? AND operation = ? '
                                 'AND request_key = ?', (self.user_id, operation, key)).fetchone()
        if row is None:
            connection.execute('INSERT INTO request_keys VALUES (?, ?, ?, ?, ?)',
                               (self.user_id, operation, key, _text(digest, 'input digest', 64), _now()))
        elif not secrets.compare_digest(row['input_sha256'], digest):
            raise Conflict("This request key was already used for a different request")

    def bind_request(self, operation: str, key: str | None, digest: str) -> None:
        """Bind a request key for a request answered without new work (an existing job, say)."""
        with self._transaction(write=True) as connection:
            self._bind_request(connection, operation, key, digest)

    def submit_task(self, operation: str, *, key: str, request: dict, units: int, heavy: str,
                    job_id: str | None = None, consent: str | None = None, context: dict | None = None,
                    bind: bool = False, bind_operation: str | None = None) -> dict:
        """Queue heavy work (model calls, with any page read they need, or a PDF) for the task runner.

        ``request`` is what the client asked for and names the request together with the key;
        ``context`` is what the server worked out from its own state when the request came (the
        CV version to build on, say) and is not part of that identity. The same key with the same
        request returns the same task, however it ended (a double click, or a request resent
        after its answer was lost, even once the work is done and the state has moved on); with a
        different request it is a conflict. A task that failed, was interrupted, cancelled or
        superseded is queued again by the same request with the context of now, up to
        MAX_TASK_ATTEMPTS. Before queueing: new tasks must be switched on, the user must have
        agreed to the data flow (``consent``), each user has at most one queued task besides one
        running, the site's queue is bounded, and the day's quota is checked in this same
        transaction (_reserve): legacy units are reserved here, tokens when each call starts.
        ``units`` is the operation's legacy cost; 0, like ``heavy`` "pdf", also means it makes no
        model call. A refusal changes nothing.
        """
        with self._transaction(write=True) as connection:
            if bind:  # ``key`` is the page's request key: bound with the task; a refusal rolls both back
                self._bind_request(connection, bind_operation or operation, key, input_digest(request))
            return self._submit_task(connection, operation, key=key, request=request, units=units, heavy=heavy,
                                     job_id=job_id, consent=consent, context=context)

    def _submit_task(self, connection: sqlite3.Connection, operation: str, *, key: str, request: dict, units: int,
                     heavy: str, job_id: str | None = None, consent: str | None = None,
                     context: dict | None = None) -> dict:
        _text(operation, 'operation', 64)
        _text(key, 'idempotency key', 200)
        if heavy not in ('model', 'pdf') or type(units) is not int or units < 0:
            raise StoreError("Invalid task")
        payload = _json({"intent": request, "context": context or {}})
        if job_id is not None:
            self._job(connection, job_id)
        existing = connection.execute('SELECT * FROM tasks WHERE user_id = ? AND operation = ? AND idempotency_key = ?',
                                      (self.user_id, operation, key)).fetchone()
        if existing is not None:
            if json.loads(existing['request_payload']).get("intent") != json.loads(_json(request)):
                raise Conflict("This request key was already used for a different request")
            if existing['status'] not in RETRYABLE_TASKS:
                return _task_view(existing, connection)
            if existing['attempts'] >= MAX_TASK_ATTEMPTS:
                raise Refused("This was tried too many times; start over", "too_many_attempts")
        settings = _settings(connection)
        if settings.get("tasks_enabled") != "1":
            raise Refused("New work is paused on this site for now; try again later", "tasks_paused")
        if consent is not None and consent not in self._consents(connection):
            raise Refused("Agree to how your data is used first", "consent_required", {"stage": consent})
        queued = connection.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'queued'",
                                    (self.user_id,)).fetchone()[0]
        if queued >= 1:
            raise Refused("You already have work waiting; try again when it has started", "queue_full")
        limit = _positive(settings.get("queue_limit")) or 1
        if connection.execute("SELECT COUNT(*) FROM tasks WHERE status = 'queued'").fetchone()[0] >= limit:
            raise Refused("The site is busy; try again in a minute", "queue_full")
        day = _reserve(connection, self.user_id, units, settings, model=heavy == 'model' and units > 0)
        now = _now()
        if existing is not None:
            # A new attempt counts its own calls (for its units); what earlier attempts cost,
            # unknown calls included, stays with the task.
            accounting = {**_usage_state(existing['usage']), "attempt_calls": 0}
            connection.execute('''UPDATE tasks SET status = 'queued', stage = NULL, execution_token = NULL,
                request_payload = ?, reserved_day = ?, units = ?, usage = ?, result = NULL, error_code = NULL,
                error_message = NULL, dismissed = 0, created_at = ?, started_at = NULL, finished_at = NULL,
                deadline_at = NULL WHERE user_id = ? AND task_id = ?''',
                               (payload, day, units, _json(accounting), now, self.user_id, existing['task_id']))
            task_id = existing['task_id']
        else:
            task_id = secrets.token_hex(16)
            connection.execute('''INSERT INTO tasks(user_id, task_id, operation, idempotency_key, request_payload,
                job_id, status, heavy, units, reserved_day, created_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)''',
                               (self.user_id, task_id, operation, key, payload, job_id, heavy, units, day, now))
        return _task_view(connection.execute('SELECT * FROM tasks WHERE user_id = ? AND task_id = ?',
                                             (self.user_id, task_id)).fetchone(), connection)

    def get_task(self, task_id: str) -> dict:
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM tasks WHERE user_id = ? AND task_id = ?',
                                     (self.user_id, _text(task_id, 'task_id', 64))).fetchone()
            if row is None:
                raise NotFound("Task not found")
            return _task_view(row, connection)

    def task_request(self, task_id: str, token: str) -> dict:
        """What a running task was asked to do, for its runner (checked against its token)."""
        with self._transaction() as connection:
            row = _running_task(connection, self.user_id, task_id, token)
            return _request_of(row['request_payload'])

    def cancel_task(self, task_id: str) -> dict:
        """Stop a task: a queued one never starts (its quota is given back); a running one's
        result will not be published, though a model call under way may still cost money."""
        with self._transaction(write=True) as connection:
            row = connection.execute('SELECT * FROM tasks WHERE user_id = ? AND task_id = ?',
                                     (self.user_id, _text(task_id, 'task_id', 64))).fetchone()
            if row is None:
                raise NotFound("Task not found")
            if row['status'] in ACTIVE_TASKS:
                _end_task(connection, self.user_id, task_id, 'cancelled', error_code='cancelled',
                          message="Cancelled")
            return _task_view(connection.execute('SELECT * FROM tasks WHERE user_id = ? AND task_id = ?',
                                                 (self.user_id, task_id)).fetchone(), connection)

    def dismiss_task(self, task_id: str) -> None:
        """Hide a finished task's note from the job page."""
        with self._transaction(write=True) as connection:
            changed = connection.execute(f'''UPDATE tasks SET dismissed = 1 WHERE user_id = ? AND task_id = ?
                AND status NOT IN ('queued', 'running')''', (self.user_id, _text(task_id, 'task_id', 64)))
            if changed.rowcount != 1:
                raise NotFound("Task not found")

    def usage_today(self) -> dict[str, Any]:
        """This account's model use and quota today (UTC), as its page shows them (_account_usage).
        ``used`` and ``limit`` are in the active mode's measure: legacy units or tokens."""
        with self._transaction() as connection:
            return _account_usage(connection, self.user_id, _today())


def _short_of(scope: str, unit: str, needed: int, left: int, limit: int, day: str) -> Refused:
    """A refusal that says what the work needs against what is left (a site refusal names no
    site numbers: they would show other accounts' use)."""
    detail: dict[str, Any] = {"scope": scope, "unit": unit, "needed": needed, "resets_at": _resets_at(day)}
    model, about = ("model ", "about ") if unit == "tokens" else ("", "")
    if scope == "user":
        detail.update(remaining=max(0, left), limit=limit)
        return Refused(f"Not enough of today's {model}allowance (needed: {about}{needed:,} {unit}; "
                       f"left: {max(0, left):,}). {RESETS}", "quota_exhausted", detail)
    return Refused(f"The site's shared {model}allowance for today cannot cover this (needed: {about}{needed:,} {unit}). "
                   f"{RESETS}", "quota_exhausted", detail)


def _reserve(connection: sqlite3.Connection, user_id: str, units: int, settings: dict[str, str], *,
             model: bool) -> str | None:
    """Check today's quota for new work, or refuse; limits must be configured. Legacy mode
    reserves the work's units for the user and the site, returning the day reserved on. Tokens
    mode reserves nothing here (each model call reserves its own tokens as it starts), but
    refuses ``model`` work when the day could not start even one call. A test account
    (quota_exemptions) is never refused for a limit, and its units are still counted."""
    day = _today()
    if _quota_mode(settings) == "tokens":
        if model:
            user_limit, site_limit = _daily_limits(settings, "tokens")
            if user_limit is None or site_limit is None:
                raise Refused("Daily limits are not configured, so no work can start", "quota_unconfigured")
            if not _exempt(connection, user_id):
                own = user_limit - _charged(connection, day, user_id)
                site = site_limit - _charged(connection, day)
                if min(own, site) < MIN_START_TOKENS:
                    raise _short_of("user" if own <= site else "site", "tokens", MIN_START_TOKENS, own, user_limit, day)
        return None
    if units == 0:  # Public job reads use the bounded queue without reserving paid work.
        return day
    user_limit, site_limit = _daily_limits(settings, "legacy")
    if user_limit is None or site_limit is None:
        raise Refused("Daily limits are not configured, so no work can start", "quota_unconfigured")
    if not _exempt(connection, user_id):
        for scope, limit in ((user_id, user_limit), ("*", site_limit)):
            row = connection.execute('SELECT units FROM quota_usage WHERE scope = ? AND day = ?', (scope, day)).fetchone()
            used = row['units'] if row else 0
            if used + units > limit:
                raise _short_of("site" if scope == "*" else "user", "units", units, limit - used, limit, day)
    for scope in (user_id, "*"):
        connection.execute('''INSERT INTO quota_usage VALUES (?, ?, ?) ON CONFLICT(scope, day)
            DO UPDATE SET units = units + excluded.units''', (scope, day, units))
    return day


def _release(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Give back a task's reserved units, when it certainly cost nothing."""
    if row['reserved_day'] and row['units']:
        for scope in (row['user_id'], "*"):
            connection.execute('UPDATE quota_usage SET units = MAX(0, units - ?) WHERE scope = ? AND day = ?',
                               (row['units'], scope, row['reserved_day']))
    connection.execute('UPDATE tasks SET reserved_day = NULL WHERE user_id = ? AND task_id = ?',
                       (row['user_id'], row['task_id']))


class LateResult(StoreError):
    """A task's result arrived after it was cancelled, timed out, interrupted or superseded."""


def _running_task(connection: sqlite3.Connection, user_id: str, task_id: str, token: str) -> sqlite3.Row:
    """The task, if it is still running under this execution token and within its time limit;
    otherwise whatever it wants to do next (a model call, a result) is late. The deadline is
    checked here, in the same transaction as the call or the publication, so correctness never
    waits for expire_tasks, which only marks such tasks as timed out for the page."""
    row = connection.execute("SELECT * FROM tasks WHERE user_id = ? AND task_id = ? AND status = 'running'",
                             (user_id, task_id)).fetchone()
    if row is None or not isinstance(token, str) or not secrets.compare_digest(row['execution_token'], token):
        raise LateResult("This task no longer holds its execution; its result is not published")
    if row['deadline_at'] is not None and row['deadline_at'] <= _now():
        raise LateResult("This task ran past its time limit; its result is not published")
    return row


def _usage_state(text: str | None) -> dict[str, Any]:
    """A task's accounting, across all its attempts: the token counts reported (late answers
    included), the model calls it started, the calls whose cost is still unknown (failed,
    unanswered or answered without usage; kept through retries) and the answers that came for an
    execution that had already ended. ``attempt_calls`` counts only the current attempt's calls:
    it alone decides whether that attempt's reserved units can come back."""
    stored = json.loads(text or '{}')
    return {"tokens": dict(stored.get("tokens", {})), "calls": int(stored.get("calls", 0)),
            "open_calls": int(stored.get("open_calls", 0)), "attempt_calls": int(stored.get("attempt_calls", 0)),
            "late_calls": int(stored.get("late_calls", 0))}


def _task_cost(state: dict[str, Any]) -> str:
    """The whole task's cost: unknown while any call of any attempt has no reported usage, known
    once all have, none if it never called the model."""
    if state["open_calls"]:
        return 'unknown'
    return 'known' if state["calls"] else 'none'


def _end_task(connection: sqlite3.Connection, user_id: str, task_id: str, status: str, *,
              result: dict | None = None, error_code: str | None = None, message: str | None = None) -> None:
    """Finish a task. The units reserved for this attempt are given back when it did not succeed
    and started no model call: this attempt certainly cost nothing, whatever earlier attempts did."""
    row = connection.execute('SELECT * FROM tasks WHERE user_id = ? AND task_id = ?', (user_id, task_id)).fetchone()
    connection.execute('''UPDATE tasks SET status = ?, execution_token = NULL, finished_at = ?, result = ?,
        error_code = ?, error_message = ? WHERE user_id = ? AND task_id = ?''',
                       (status, _now(), _json(result) if result is not None else None, error_code,
                        message[:500] if message else None, user_id, task_id))
    if status != 'succeeded' and _usage_state(row['usage'])["attempt_calls"] == 0:
        _release(connection, row)


def _request_of(payload: str) -> dict[str, Any]:
    """A stored request as its runner reads it: what was asked, with the context worked out then."""
    stored = json.loads(payload)
    return {**stored.get("intent", {}), **stored.get("context", {})}


def _scrub_request(connection: sqlite3.Connection, user_id: str, task_id: str) -> None:
    """Drop a finished task's stored context (an upload's CV lines), keeping only its fingerprint
    and what was asked (a language and the PDF's hash), so the same request still finds the task."""
    row = connection.execute('SELECT request_payload FROM tasks WHERE user_id = ? AND task_id = ?',
                             (user_id, task_id)).fetchone()
    if row is None:
        return
    stored = json.loads(row['request_payload'])
    if "scrubbed" not in stored.get("context", {}):
        digest = hashlib.sha256(_json(stored.get("context", {})).encode("utf-8")).hexdigest()
        connection.execute('UPDATE tasks SET request_payload = ? WHERE user_id = ? AND task_id = ?',
                           (_json({"intent": stored.get("intent", {}), "context": {"scrubbed": digest}}),
                            user_id, task_id))


def _task_view(row: sqlite3.Row, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    """What the page may see of a task: never its stored input (it can hold CV lines). With the
    connection it was read on, also the model use of its metered calls, every attempt's
    (``model_use``), and what it reported before calls were metered (``earlier_reported``,
    incomplete: no retries or unusable answers)."""
    view = {'task_id': row['task_id'], 'operation': row['operation'], 'status': row['status'],
            'stage': row['stage'], 'job_id': row['job_id'], 'attempts': row['attempts'], 'cost': row['cost'],
            'result': json.loads(row['result']) if row['result'] else None, 'error_code': row['error_code'],
            'message': row['error_message'], 'created_at': row['created_at'], 'started_at': row['started_at'],
            'finished_at': row['finished_at']}
    if connection is not None:
        use = _call_summary(connection, 'user_id = ? AND task_id = ?', (row['user_id'], row['task_id']))
        earlier = {name: count for name, count in _usage_state(row['usage'])["tokens"].items()
                   if name in ("prompt_tokens", "completion_tokens")}
        view['model_use'] = {**use, **({"earlier_reported": earlier} if earlier else {})}
    return view


def _refs(values: Any) -> list[tuple[str, int]]:
    if not isinstance(values, (list, tuple)) or len(values) > 500:
        raise StoreError("Invalid fact references")
    result = []
    seen = set()
    for ref in values:
        if not isinstance(ref, (list, tuple)) or len(ref) != 2:
            raise StoreError("Invalid fact reference")
        fact_id, version = _text(ref[0], 'fact_id'), _version(ref[1])
        if fact_id in seen:
            raise StoreError("Duplicate fact reference")
        seen.add(fact_id)
        result.append((fact_id, version))
    return sorted(result)


# ---------------------------------------------------------------------------------------------
# Sponsor answers: worked out when words are saved (a user's own in _assess_saved_text), and
# only ever read by pages

def _answer(row: Any) -> dict[str, Any] | None:
    """A stored sponsor answer as sponsorship.shown takes it."""
    if row is None:
        return None
    return {"status": row['status'], "evidence": json.loads(row['evidence']), "rule_version": row['rule_version'],
            "assessed_at": row['assessed_at']}


def _sponsor_answer(text: Any) -> tuple[str, str] | None:
    """The current rules' (status, evidence JSON) for ``text``, or None if they failed or gave
    something that is not an answer: then nothing is stored, the words stay not assessed and a
    later refresh, explicit check or backfill tries again. A failure is never stored as one."""
    if not isinstance(text, str):
        return None
    try:
        status, evidence = sponsorship.checked(text, sponsorship.assess_sponsorship(text))
        return status, _json(evidence)
    except Exception:  # whatever went wrong in the rules, these words simply have no answer yet
        return None


def _answer_public_text(connection: sqlite3.Connection, digest: str, text: str) -> tuple[str, str, str] | None:
    """Assess the words of a stored public posting and save their shared answer, replacing any
    of older rules, in the caller's write transaction (which has found none under the current
    rules). Returns (status, evidence JSON, assessed_at), or None when the rules failed or the
    words are not the ones ``digest`` names: then nothing is saved."""
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != digest:
        return None  # an answer is only ever kept for the words it read
    made = _sponsor_answer(text)
    if made is None:
        return None
    answer, version = (*made, _now()), sponsorship.RULE_VERSION
    connection.execute('INSERT INTO public_sponsor_assessments VALUES (?, ?, ?, ?, ?)', (digest, version, *answer))
    connection.execute('DELETE FROM public_sponsor_assessments WHERE text_sha256 = ? AND rule_version != ?',
                       (digest, version))
    return answer


def _assess_public(path: Path, hashes: Iterable[str]) -> dict[str, int]:
    """Assess, under the current rules, the words of public postings named by ``hashes`` that
    have no answer yet. Those already answered are found by reading alone, so an unchanged board
    takes no write lock. The rest go in batches, each looked up again, assessed and saved inside
    one short write transaction, so two reads of a board at once (or of two boards listing the
    same words) assess each text once: the later waits, then finds the answer saved. Holding
    the write lock while assessing is right only for these local rules (well under a
    millisecond for a typical description); a paid model call must run outside any transaction,
    as tasks do."""
    version, wanted = sponsorship.RULE_VERSION, list(dict.fromkeys(hashes))
    answered: set[str] = set()
    with transaction(path) as connection:  # usually all are answered: then nothing takes the write lock
        for start in range(0, len(wanted), 500):
            part = wanted[start:start + 500]
            answered.update(row[0] for row in connection.execute(
                f'''SELECT text_sha256 FROM public_sponsor_assessments WHERE rule_version = ?
                    AND text_sha256 IN ({", ".join("?" * len(part))})''', (version, *part)))
    pending = [digest for digest in wanted if digest not in answered]
    counts = {"assessed": 0, "failed": 0}
    for start in range(0, len(pending), SPONSOR_BATCH):
        batch = pending[start:start + SPONSOR_BATCH]
        with transaction(path, write=True) as connection:
            rows = connection.execute(f'''SELECT text_hash, text FROM public_postings
                WHERE text_hash IN ({", ".join("?" * len(batch))}) AND text_hash NOT IN (
                    SELECT text_sha256 FROM public_sponsor_assessments WHERE rule_version = ?)
                GROUP BY text_hash''', (*batch, version)).fetchall()
            for row in rows:
                answered_now = _answer_public_text(connection, row['text_hash'], row['text']) is not None
                counts["assessed" if answered_now else "failed"] += 1
    return counts


def backfill_sponsorship(path: Path, *, limit: int, after: str | None = None) -> dict[str, Any]:
    """Work on at most ``limit`` saved texts that have no sponsor answer under the current rules,
    in one fixed order: public postings by their words' hash, then active accounts' saved jobs by
    account and job. For a store from before answers were kept, or after the rules changed.

    ``next`` in the result names where this round stopped while texts remain after it: given
    back as ``after``, the next round goes on from there, so a text that keeps failing never
    holds up those behind it. ``next`` is None once a round reaches the end; a round from the
    start (no ``after``) then tries the failed ones again. Every round stays within its limit,
    and an answer once stored is never worked out again. The rules are local: no model is
    called and nothing leaves the store. Returns counts and that position only."""
    if type(limit) is not int or limit < 0:
        raise StoreError("The limit must be a whole number of 0 or more")
    begin: tuple = ()
    if after is not None:
        position = _BACKFILL_POSITION.fullmatch(after) if isinstance(after, str) else None
        if position is None:
            raise StoreError("Not a position a sponsorship backfill gave")
        begin = (0, position[1]) if position[1] else (1, position[2], position[3])
    version = sponsorship.RULE_VERSION
    with transaction(path) as connection:
        public = [row[0] for row in connection.execute('''SELECT DISTINCT text_hash FROM public_postings
            WHERE text_hash NOT IN (SELECT text_sha256 FROM public_sponsor_assessments WHERE rule_version = ?)''',
                                                       (version,))]
        stored = {(row[0], row[1]) for row in connection.execute(
            'SELECT user_id, text_sha256 FROM user_sponsor_assessments WHERE rule_version = ?', (version,))}
        private: list[tuple[str, str]] = []
        for row in connection.execute('''SELECT j.user_id, j.job_id, json_extract(j.payload, '$.text') AS text
                FROM jobs j JOIN users u ON u.user_id = j.user_id WHERE u.active = 1
                ORDER BY j.user_id, j.job_id'''):
            if isinstance(row['text'], str):
                key = (row['user_id'], hashlib.sha256(row['text'].encode('utf-8')).hexdigest())
                if key not in stored:
                    stored.add(key)  # one job per user's words is enough
                    private.append((row['user_id'], row['job_id']))
    queue = sorted([(0, digest) for digest in public] + [(1, user_id, job_id) for user_id, job_id in private])
    waiting = [key for key in queue if key > begin]
    chosen = waiting[:limit]
    done = _assess_public(path, [key[1] for key in chosen if key[0] == 0])
    outcomes = {"assessed": 0, "shared": 0, "stored": 0, "failed": 0, "skipped": 0}
    for _, user_id, job_id in (key for key in chosen if key[0] == 1):
        try:
            outcomes[UserWorkspace(path, user_id).assess_job_sponsorship(job_id)] += 1
        except NotFound:  # the account was disabled or deleted meanwhile
            outcomes["skipped"] += 1
    last = chosen[-1] if chosen and len(waiting) > len(chosen) else None
    return {"rule_version": version,
            "public": {**done, "remaining": len(public) - done["assessed"]},
            "private": {"assessed": outcomes["assessed"], "shared": outcomes["shared"], "failed": outcomes["failed"],
                        "skipped": outcomes["skipped"],
                        "remaining": len(private) - outcomes["assessed"] - outcomes["shared"] - outcomes["stored"]},
            "next": None if last is None else f"public:{last[1]}" if last[0] == 0 else f"private:{last[1]}:{last[2]}"}


# ---------------------------------------------------------------------------------------------
# The shared public job-board cache (public data only; never user text)

def store_public_board(path: Path, provider: str, board: str, company: str, postings: list[dict[str, Any]]) -> dict:
    """Replace one public board's postings with what its public API lists now. Only postings
    read from a board's public API come here: never pasted text, CVs or anything of a user's.

    Then the sponsor wording of words without an answer under the current rules is assessed
    (_assess_public). Words read again unchanged keep their answer and when it was made; only
    the posting's last_seen_at moves. Changed words get an answer of their own, and answers for
    words no posting lists any more go."""
    now = _now()
    hashes = [hashlib.sha256(job["text"].encode("utf-8")).hexdigest() for job in postings]
    with transaction(path, write=True) as connection:
        connection.execute('''INSERT INTO public_boards VALUES (?, ?, ?, ?) ON CONFLICT(provider, board)
            DO UPDATE SET company = excluded.company, fetched_at = excluded.fetched_at''', (provider, board, company, now))
        before = {row['job_id']: row['text_hash'] for row in connection.execute(
            'SELECT job_id, text_hash FROM public_postings WHERE provider = ? AND board = ?', (provider, board))}
        for job, digest in zip(postings, hashes):
            connection.execute('''INSERT INTO public_postings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, board, job_id) DO UPDATE SET title = excluded.title, location = excluded.location,
                source = excluded.source, posted_at = excluded.posted_at, text = excluded.text,
                text_hash = excluded.text_hash, last_seen_at = excluded.last_seen_at''',
                               (provider, board, job["job_id"], job["title"], job.get("location"), job["source"],
                                job.get("posted_at"), job["text"], digest, now, now))
        listed = {job["job_id"] for job in postings}
        for job_id in before.keys() - listed:  # no longer on the board: closed (their matches go with them)
            connection.execute('DELETE FROM public_postings WHERE provider = ? AND board = ? AND job_id = ?',
                               (provider, board, job_id))
        dropped = sorted(set(before.values()) - set(hashes))
        for start in range(0, len(dropped), 500):
            part = dropped[start:start + 500]
            connection.execute(f'''DELETE FROM public_sponsor_assessments WHERE text_sha256 IN ({", ".join("?" * len(part))})
                AND text_sha256 NOT IN (SELECT text_hash FROM public_postings)''', part)
        counts = {"open": len(listed), "new": len(listed - before.keys()), "closed": len(before.keys() - listed)}
    return {**counts, "sponsorship": _assess_public(path, hashes)}


def assess_public_board(path: Path, provider: str, board: str) -> dict[str, int]:
    """Assess the words of one stored board's postings that have no answer under the current
    rules (an earlier failure, or older rules), without reading the board again: a refresh
    answered from the shared copy still retries them. Usually there are none and nothing is
    written."""
    with transaction(path) as connection:
        hashes = [row[0] for row in connection.execute('''SELECT DISTINCT text_hash FROM public_postings
            WHERE provider = ? AND board = ? AND text_hash NOT IN (
                SELECT text_sha256 FROM public_sponsor_assessments WHERE rule_version = ?)''',
                                                       (provider, board, sponsorship.RULE_VERSION))]
    return _assess_public(path, hashes)


def public_board_age(path: Path, provider: str, board: str) -> tuple[str | None, timedelta | None, int]:
    """The cached board's company name, how old its copy is (None when never read) and how many
    postings it lists."""
    with transaction(path) as connection:
        row = connection.execute(
            "SELECT company, fetched_at, (SELECT COUNT(*) FROM public_postings p WHERE p.provider = b.provider "
            "AND p.board = b.board) AS open_count FROM public_boards b WHERE provider = ? AND board = ?",
            (provider, board)).fetchone()
    if row is None:
        return None, None, 0
    return row['company'], _clock() - datetime.fromisoformat(row['fetched_at']), row['open_count']


# ---------------------------------------------------------------------------------------------
# The task runner's side of the queue (task_runner.py)

QUEUE_WAIT = timedelta(minutes=15)


def claim_tasks(path: Path, *, total: int, pdf: int, deadline: timedelta) -> list[dict[str, Any]]:
    """Start up to ``total`` queued tasks (at most ``pdf`` of them PDF printing), oldest first,
    one per user at a time and never for a disabled account. Each gets a new execution token:
    only the holder of that token can publish its result."""
    claimed: list[dict[str, Any]] = []
    if total <= 0:
        return claimed
    now = _clock()
    with transaction(path, write=True) as connection:
        busy = {row['user_id'] for row in connection.execute("SELECT user_id FROM tasks WHERE status = 'running'")}
        queued = connection.execute(
            "SELECT t.* FROM tasks t JOIN users u ON u.user_id = t.user_id "
            "WHERE t.status = 'queued' AND u.active = 1 ORDER BY t.created_at, t.task_id").fetchall()
        for row in queued:
            if len(claimed) >= total:
                break
            if row['user_id'] in busy or (row['heavy'] == 'pdf' and pdf <= 0):
                continue
            token = secrets.token_urlsafe(24)
            connection.execute("UPDATE tasks SET status = 'running', execution_token = ?, attempts = attempts + 1, "
                               "started_at = ?, deadline_at = ?, stage = 'started' WHERE user_id = ? AND task_id = ?",
                               (token, _stamp(now), _stamp(now + deadline), row['user_id'], row['task_id']))
            busy.add(row['user_id'])
            pdf -= row['heavy'] == 'pdf'
            claimed.append({'user_id': row['user_id'], 'task_id': row['task_id'], 'operation': row['operation'],
                            'heavy': row['heavy'], 'token': token, 'job_id': row['job_id'],
                            'request': _request_of(row['request_payload'])})
    return claimed


def begin_call(path: Path, user_id: str, task_id: str, token: str, *, prompt_tokens: int,
               max_output: int) -> tuple[str, int]:
    """Start one model call (one transport call) of a running task, in one write transaction,
    so concurrent calls never pass a limit together. Raises LateResult when the task no longer
    holds this execution or its time (no call may start after it was cancelled, timed out or
    retried), NotFound for a disabled account, and Refused when new work is paused or, in tokens
    mode, the day's tokens left to the account and to the site (deleted accounts' use counted)
    cannot cover ``prompt_tokens`` plus MIN_OUTPUT_TOKENS: nothing is recorded and nothing may be
    sent. The output budget is cut to what fits, up to ``max_output``; a test account's never is.
    Records the call as pending, reserving the prompt estimate plus that budget on today's date,
    and returns (call_id, output budget). The task's cost is unknown until the call settles."""
    for value in (prompt_tokens, max_output):
        if type(value) is not int or not 0 < value <= MAX_REPORTED_TOKENS:
            raise StoreError("Invalid call estimate")
    with transaction(path, write=True) as connection:
        row = _running_task(connection, user_id, task_id, token)
        account = connection.execute('SELECT active FROM users WHERE user_id = ?', (user_id,)).fetchone()
        if account is None or not account['active']:
            raise NotFound("Account unavailable")
        if row['heavy'] != 'model' or row['units'] == 0:
            raise StoreError("This task does not allow paid calls")
        settings = _settings(connection)
        if settings.get("tasks_enabled") != "1":
            raise Refused("New work was paused on this site; nothing more was sent to the model", "tasks_paused")
        exempt, day, output = _exempt(connection, user_id), _today(), max_output
        if _quota_mode(settings) == "tokens" and not exempt:
            user_limit, site_limit = _daily_limits(settings, "tokens")
            if user_limit is None or site_limit is None:
                raise Refused("Daily limits are not configured, so no work can start", "quota_unconfigured")
            own = user_limit - _charged(connection, day, user_id)
            site = site_limit - _charged(connection, day)
            room = min(own, site) - prompt_tokens
            if room < MIN_OUTPUT_TOKENS:
                raise _short_of("user" if own <= site else "site", "tokens", prompt_tokens + MIN_OUTPUT_TOKENS, own,
                                user_limit, day)
            output = min(max_output, room)
        call_id = secrets.token_hex(16)
        connection.execute('''INSERT INTO model_calls(call_id, user_id, task_id, attempt, execution_sha256, day, reserved,
            max_output, exempt, status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)''',
                           (call_id, user_id, task_id, row['attempts'], _hash(token), day, prompt_tokens + output, output,
                            int(exempt), _now()))
        state = _usage_state(row['usage'])
        state["calls"] += 1
        state["open_calls"] += 1
        state["attempt_calls"] += 1
        connection.execute('UPDATE tasks SET cost = ?, usage = ? WHERE user_id = ? AND task_id = ?',
                           (_task_cost(state), _json(state), user_id, task_id))
        return call_id, output


def _reported(usage: Any) -> dict[str, int | None]:
    """What a provider reported for one call, checked. prompt_tokens and completion_tokens are
    whole numbers from 0 (a missing or malformed one is None, never 0). DeepSeek's cache counts
    are optional parts of prompt_tokens (hit + miss = prompt): kept only while they fit it, None
    when absent or not fitting, and never added to the total."""
    def count(name: str) -> int | None:
        value = usage.get(name) if isinstance(usage, dict) else None
        return value if type(value) is int and 0 <= value <= MAX_REPORTED_TOKENS else None

    prompt, completion = count("prompt_tokens"), count("completion_tokens")
    hit, miss = count("prompt_cache_hit_tokens"), count("prompt_cache_miss_tokens")
    if prompt is None or (hit or 0) > prompt or (miss or 0) > prompt or (
            hit is not None and miss is not None and hit + miss != prompt):
        hit = miss = None
    return {"prompt": prompt, "completion": completion, "hit": hit, "miss": miss}


def _ended_late(connection: sqlite3.Connection, call: sqlite3.Row) -> tuple[sqlite3.Row | None, bool]:
    """The call's task, and whether the call ended after its execution did (cancelled, timed out,
    interrupted or retried under a new token)."""
    task = connection.execute('SELECT * FROM tasks WHERE user_id = ? AND task_id = ?',
                              (call['user_id'], call['task_id'])).fetchone()
    current = (task is not None and task['status'] == 'running' and task['execution_token'] is not None
               and secrets.compare_digest(_hash(task['execution_token']), call['execution_sha256']))
    return task, not current


def settle_call(path: Path, user_id: str, call_id: str, usage: Any) -> str:
    """Record what the provider reported for one call, whenever the answer comes: a late answer
    settles its own call, on the day that call was reserved, and never touches a later attempt.
    With both counts valid the call is settled at prompt plus completion tokens, even past its
    reservation (the overage is charged, not hidden, and the next call sees it); otherwise it is
    unknown, charged at least its reservation and any part that was reported. Only a pending call
    changes: a duplicate report changes nothing. Returns the call's status ("gone" once its
    account was deleted)."""
    reported = _reported(usage)
    with transaction(path, write=True) as connection:
        call = connection.execute('SELECT * FROM model_calls WHERE call_id = ? AND user_id = ?',
                                  (call_id, user_id)).fetchone()
        if call is None:
            return "gone"
        if call['status'] != 'pending':
            return call['status']
        settled = reported["prompt"] is not None and reported["completion"] is not None
        task, late = _ended_late(connection, call)
        connection.execute('''UPDATE model_calls SET status = ?, prompt_tokens = ?, completion_tokens = ?,
            cache_hit_tokens = ?, cache_miss_tokens = ?, error = ?, late = ?, ended_at = ? WHERE call_id = ?''',
                           ("settled" if settled else "unknown", reported["prompt"], reported["completion"],
                            reported["hit"], reported["miss"], None if settled else "bad_usage", int(late), _now(), call_id))
        if task is not None:
            state = _usage_state(task['usage'])
            if settled:
                state["open_calls"] = max(0, state["open_calls"] - 1)
            if late:
                state["late_calls"] += 1
            connection.execute('UPDATE tasks SET usage = ?, cost = ? WHERE user_id = ? AND task_id = ?',
                               (_json(state), _task_cost(state), user_id, call['task_id']))
        return "settled" if settled else "unknown"


def fail_call(path: Path, user_id: str, call_id: str, reason: str) -> str:
    """End a call that brought no usage back (an HTTP error, no answer, an unreadable body). It
    may still have been billed, so it is unknown: charged its reservation, its task's cost
    unknown. ``reason`` is a short code, never response text. Only a pending call changes."""
    reason = reason[:64] if isinstance(reason, str) and reason else "failed"
    with transaction(path, write=True) as connection:
        call = connection.execute('SELECT * FROM model_calls WHERE call_id = ? AND user_id = ?',
                                  (call_id, user_id)).fetchone()
        if call is None:
            return "gone"
        if call['status'] != 'pending':
            return call['status']
        _task, late = _ended_late(connection, call)
        connection.execute("UPDATE model_calls SET status = 'unknown', error = ?, late = ?, ended_at = ? WHERE call_id = ?",
                           (reason, int(late), _now(), call_id))
        return "unknown"


def mark_spending(path: Path, user_id: str, task_id: str, token: str, stage: str) -> None:
    """The task-level accounting from before calls were metered, kept for callers that use it
    (tests); TaskContext.chat meters each call instead (begin_call).

    Record, before a paid call starts, that the task may now cost money: the call stays open,
    and the task's cost unknown, until its usage is reported (record_usage), in this attempt or
    any later one. If the process stops or the answer is lost, it stays unknown and this
    attempt's units are not given back. A task that no longer holds its token or its time raises
    LateResult, so no call is made for it."""
    with transaction(path, write=True) as connection:
        row = _running_task(connection, user_id, task_id, token)
        if row['units'] == 0:
            raise StoreError("This task does not allow paid calls")
        state = _usage_state(row['usage'])
        state["calls"] += 1
        state["open_calls"] += 1
        state["attempt_calls"] += 1
        connection.execute("UPDATE tasks SET stage = ?, cost = ?, usage = ? WHERE user_id = ? AND task_id = ?",
                           (_text(stage, 'stage', 64), _task_cost(state), _json(state), user_id, task_id))


def record_usage(path: Path, user_id: str, task_id: str, token: str, usage: dict[str, int] | None) -> None:
    """mark_spending's counterpart, from before calls were metered (settle_call replaces it): its
    counts become the task's ``earlier_reported`` tokens, never part of a daily total.

    Add a call's reported token counts to the task. A call is settled only if it reported
    some; the task's cost is known only once every call of every attempt is settled, so a call
    that failed or reported nothing keeps it unknown, whatever this or a later attempt reports.
    An answer for an execution that has ended (cancelled, timed out, interrupted, or since
    retried under a new token) is counted as late: it settles its own call, but never touches the
    current attempt's count of calls, which decides whether that attempt's units come back."""
    counts = {name: count for name, count in (usage or {}).items()
              if isinstance(name, str) and isinstance(count, int) and not isinstance(count, bool) and count >= 0}
    with transaction(path, write=True) as connection:
        row = connection.execute('SELECT * FROM tasks WHERE user_id = ? AND task_id = ?', (user_id, task_id)).fetchone()
        if row is None:
            return
        state = _usage_state(row['usage'])
        for name, count in counts.items():
            state["tokens"][name] = state["tokens"].get(name, 0) + count
        if counts:
            state["open_calls"] = max(0, state["open_calls"] - 1)
        current = (row['status'] == 'running' and isinstance(token, str)
                   and secrets.compare_digest(row['execution_token'], token))
        if not current:
            state["late_calls"] += 1
        connection.execute('UPDATE tasks SET usage = ?, cost = ? WHERE user_id = ? AND task_id = ?',
                           (_json(state), _task_cost(state), user_id, task_id))


def set_stage(path: Path, user_id: str, task_id: str, token: str, stage: str) -> None:
    with transaction(path, write=True) as connection:
        _running_task(connection, user_id, task_id, token)
        connection.execute('UPDATE tasks SET stage = ? WHERE user_id = ? AND task_id = ?',
                           (_text(stage, 'stage', 64), user_id, task_id))


def finish_task(path: Path, user_id: str, task_id: str, token: str, status: str, *,
                result: dict | None = None, error_code: str | None = None, message: str | None = None) -> None:
    """End a running task that has nothing further to publish (failed, or succeeded without a
    record of its own). Raises LateResult if the task no longer holds this token."""
    if status not in ('succeeded', 'failed', 'superseded'):
        raise StoreError("Invalid task outcome")
    with transaction(path, write=True) as connection:
        _running_task(connection, user_id, task_id, token)
        _end_task(connection, user_id, task_id, status, result=result, error_code=error_code, message=message)


def recover_tasks(path: Path) -> int:
    """At start: a task still marked running belonged to a process that is gone, so it is
    interrupted, never silently rerun. Its cost stays as recorded (unknown if a call was under
    way); the user can run it again. A model call still pending can never be settled now (the
    process that waited for its answer is gone): it becomes unknown and keeps its reservation on
    its own day, through every later attempt."""
    with transaction(path, write=True) as connection:
        _add_tables(connection)  # a backup restored from before them has none yet
        rows = connection.execute("SELECT user_id, task_id FROM tasks WHERE status = 'running'").fetchall()
        for row in rows:
            _end_task(connection, row['user_id'], row['task_id'], 'interrupted', error_code='interrupted',
                      message="The service restarted while this was running")
        connection.execute("UPDATE model_calls SET status = 'unknown', error = 'interrupted', late = 1, ended_at = ? "
                           "WHERE status = 'pending'", (_now(),))
        return len(rows)


def expire_tasks(path: Path) -> list[tuple[str, str]]:
    """End running tasks past their deadline (their late results are refused) and queued tasks
    that waited too long, and drop the CV lines of upload tasks finished a day ago. Returns the
    (user, task) pairs ended."""
    now, waited = _now(), _stamp(_clock() - QUEUE_WAIT)
    ended = []
    with transaction(path, write=True) as connection:
        rows = connection.execute("SELECT user_id, task_id, status FROM tasks "
                                  "WHERE (status = 'running' AND deadline_at <= ?) OR (status = 'queued' AND created_at <= ?)",
                                  (now, waited)).fetchall()
        for row in rows:
            if row['status'] == 'running':
                _end_task(connection, row['user_id'], row['task_id'], 'failed', error_code='timeout',
                          message="This took too long and was stopped")
            else:
                _end_task(connection, row['user_id'], row['task_id'], 'failed', error_code='expired',
                          message="The site was too busy to start this in time; try again")
            ended.append((row['user_id'], row['task_id']))
        for row in connection.execute("SELECT user_id, task_id FROM tasks WHERE operation = 'upload_cv' "
                                      "AND status NOT IN ('queued', 'running') AND finished_at <= ?",
                                      (_stamp(_clock() - UPLOAD_KEEP),)).fetchall():
            _scrub_request(connection, row['user_id'], row['task_id'])
    return ended
