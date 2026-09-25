"""Company job boards the user follows, their open postings, and ranking by confirmed skills.

listings.db is a local copy of public postings. Deleting it loses only the company list
and downloaded postings; facts and work on a job live in other files.
"""

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from facts import load_search_terms, tag_finder
from job_search import SearchError, check_board, fetch_board


DEFAULT_DATABASE = Path(".local/listings.db")
STARTER_BOARDS = Path(__file__).parent / "starter_boards.json"
# The page reads a company again when its copy is older than this; there is no background job.
STALE_AFTER = timedelta(hours=24)
PAGE_SIZE = 50
SENIOR_TITLE = re.compile(
    r"\b(senior|sr|staff|principal|lead|director|manager|head|vp|vice president|distinguished|chief)\b",
    re.IGNORECASE,
)
# OpenAI, Anthropic and Cohere call every engineer this, new graduates included.
ANY_LEVEL_TITLE = re.compile(r"\bmember of (the )?technical staff\b", re.IGNORECASE)
MAX_PAGE_SIZE = 200
SCHEMA_VERSION = 1
SCHEMA = (
    """CREATE TABLE sources (
        provider TEXT NOT NULL,
        board TEXT NOT NULL,
        company TEXT NOT NULL,
        added_at TEXT NOT NULL,
        fetched_at TEXT,
        error TEXT,
        PRIMARY KEY (provider, board)
    )""",
    """CREATE TABLE listings (
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
        matched_key TEXT,
        matched_tags TEXT,
        PRIMARY KEY (provider, board, job_id),
        FOREIGN KEY (provider, board) REFERENCES sources (provider, board) ON DELETE CASCADE
    )""",
)

Fetch = Callable[..., list[dict[str, Any]]]


class ListingsError(Exception):
    """A company-list or listing request that cannot be carried out."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _open(path: Path, starter: Iterable[dict[str, str]] = ()) -> sqlite3.Connection:
    path = Path(path)
    if path.exists() and not path.is_file():
        raise ListingsError(f"岗位库路径不是文件：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with _transaction(connection):
                for statement in SCHEMA:
                    connection.execute(statement)
                _add_starter(connection, starter)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            raise ListingsError(f"不支持的岗位库版本：{version}")
    except BaseException:
        connection.close()
        raise
    return connection


def _add_starter(connection: sqlite3.Connection, starter: Iterable[dict[str, str]]) -> None:
    added_at = _now()
    for item in starter:
        try:
            provider, board = check_board(item["provider"], item["board"])
        except SearchError as exc:
            raise ListingsError(f"起始公司列表有误：{exc}") from exc
        connection.execute(
            "INSERT OR IGNORE INTO sources (provider, board, company, added_at) VALUES (?, ?, ?, ?)",
            (provider, board, item["company"], added_at),
        )


def load_starter(path: Path = STARTER_BOARDS) -> list[dict[str, str]]:
    """The companies a new listings database starts with; a missing file means none."""
    if not Path(path).exists():
        return []
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and all(isinstance(item.get(key), str) for key in ("provider", "board", "company"))
        for item in items
    ):
        raise ListingsError("起始公司列表必须是含 provider、board、company 的数组")
    return items


def initialize(path: Path, starter: Iterable[dict[str, str]] = ()) -> None:
    """Create the listings database; the starter companies are added only when it is new,
    so a company the user removed does not come back on the next start."""
    _open(path, starter).close()


def _store(connection: sqlite3.Connection, provider: str, board: str, jobs: list[dict[str, Any]]) -> dict[str, int]:
    """Replace one board's postings with the ones it lists now; keeps when each was first seen."""
    seen_at = _now()
    before = {row["job_id"] for row in connection.execute(
        "SELECT job_id FROM listings WHERE provider = ? AND board = ?", (provider, board)
    )}
    # Saved skill matches stay valid only while the title and text they were found in stay the same.
    connection.executemany(
        """INSERT INTO listings (provider, board, job_id, title, location, source, posted_at, text,
                                 text_hash, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (provider, board, job_id) DO UPDATE SET
               matched_key = CASE WHEN listings.title = excluded.title AND listings.text_hash = excluded.text_hash
                                  THEN listings.matched_key END,
               title = excluded.title, location = excluded.location, source = excluded.source,
               posted_at = excluded.posted_at, text = excluded.text, text_hash = excluded.text_hash,
               last_seen_at = excluded.last_seen_at""",
        [
            (provider, board, job["job_id"], job["title"], job.get("location"), job["source"],
             job.get("posted_at"), job["text"], hashlib.sha256(job["text"].encode("utf-8")).hexdigest(),
             seen_at, seen_at)
            for job in jobs
        ],
    )
    closed = connection.execute(
        "DELETE FROM listings WHERE provider = ? AND board = ? AND last_seen_at != ?",
        (provider, board, seen_at),
    ).rowcount
    listed = {job["job_id"] for job in jobs}
    return {"open": len(listed), "new": len(listed - before), "closed": closed}


def _company(connection: sqlite3.Connection, provider: str, board: str) -> str:
    row = connection.execute(
        "SELECT company FROM sources WHERE provider = ? AND board = ?", (provider, board)
    ).fetchone()
    if row is None:
        raise ListingsError(f"不在公司列表中：{provider}/{board}")
    return row["company"]


def add_source(
    path: Path, provider: str, board: str, company: str | None = None, fetch: Fetch = fetch_board
) -> dict[str, Any]:
    """Follow a company's public board; it is read once now, so a wrong link fails here."""
    connection = _open(path)
    try:
        jobs = fetch(board, provider)
        name = company or next((job["company"] for job in jobs if job.get("company")), None) or board
        with _transaction(connection):
            exists = connection.execute(
                "SELECT company FROM sources WHERE provider = ? AND board = ?", (provider, board)
            ).fetchone()
            if exists is not None:
                raise ListingsError(f"已在公司列表中：{exists['company']}")
            connection.execute(
                "INSERT INTO sources (provider, board, company, added_at, fetched_at) VALUES (?, ?, ?, ?, ?)",
                (provider, board, name, _now(), _now()),
            )
            counts = _store(connection, provider, board, jobs)
        return {"company": name, **counts}
    finally:
        connection.close()


def refresh_source(path: Path, provider: str, board: str, fetch: Fetch = fetch_board) -> dict[str, Any]:
    """Read one followed board again. If that fails, the last good copy stays and the error is kept."""
    connection = _open(path)
    try:
        company = _company(connection, provider, board)
        try:
            jobs = fetch(board, provider)
        except SearchError as exc:
            with _transaction(connection):
                connection.execute(
                    "UPDATE sources SET error = ? WHERE provider = ? AND board = ?", (str(exc), provider, board)
                )
            raise ListingsError(f"{company}：{exc}") from exc
        with _transaction(connection):
            _company(connection, provider, board)  # removed while it was being read
            counts = _store(connection, provider, board, jobs)
            connection.execute(
                "UPDATE sources SET fetched_at = ?, error = NULL WHERE provider = ? AND board = ?",
                (_now(), provider, board),
            )
        return {"company": company, **counts}
    finally:
        connection.close()


def remove_source(path: Path, provider: str, board: str) -> None:
    """Stop following a company; its downloaded postings go with it."""
    connection = _open(path)
    try:
        with _transaction(connection):
            _company(connection, provider, board)
            connection.execute("DELETE FROM sources WHERE provider = ? AND board = ?", (provider, board))
    finally:
        connection.close()


def list_sources(path: Path) -> list[dict[str, Any]]:
    """Followed companies, how many postings each lists now, and how the last read went."""
    connection = _open(path)
    try:
        rows = connection.execute(
            """SELECT s.provider, s.board, s.company, s.added_at, s.fetched_at, s.error,
                      COUNT(l.job_id) AS open_count
               FROM sources AS s LEFT JOIN listings AS l USING (provider, board)
               GROUP BY s.provider, s.board
               ORDER BY s.company COLLATE NOCASE, s.provider, s.board"""
        ).fetchall()
    finally:
        connection.close()
    stale_before = (datetime.now(timezone.utc) - STALE_AFTER).isoformat()
    return [{**row, "stale": row["fetched_at"] is None or row["fetched_at"] < stale_before} for row in map(dict, rows)]


def get_listing(path: Path, provider: str, board: str, job_id: str) -> dict[str, Any]:
    """One stored posting with its company name, as last read from the board."""
    connection = _open(path)
    try:
        row = connection.execute(
            """SELECT l.provider, l.board, l.job_id, l.title, l.source, s.company
               FROM listings AS l JOIN sources AS s USING (provider, board)
               WHERE l.provider = ? AND l.board = ? AND l.job_id = ?""",
            (provider, board, job_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ListingsError("这个岗位已不在已下载的列表中，请刷新该公司后再试")
    return dict(row)


def is_senior_title(title: str) -> bool:
    return bool(SENIOR_TITLE.search(ANY_LEVEL_TITLE.sub("", title)))


def _matched_count(tags: list[str]) -> int:
    return len({tag.casefold() for tag in tags})


def ranked_listings(
    path: Path,
    facts_db: Path,
    title: str = "",
    location: str = "",
    limit: int = PAGE_SIZE,
    offset: int = 0,
    hide_senior: bool = False,
) -> dict[str, Any]:
    """Open postings, most confirmed skills mentioned first; a count of words, not a fit score.

    The same role posted in several cities (same title and text) is one row. Ties go to the
    newest posting. A location filter keeps postings whose location is unknown.
    """
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE or offset < 0:
        raise ListingsError(f"每页岗位数必须在 1 到 {MAX_PAGE_SIZE} 之间")
    terms = load_search_terms(facts_db) if Path(facts_db).exists() else []
    tags = list(dict.fromkeys(item["term"] for item in terms))
    # Matching thousands of postings takes seconds, so matches are saved per posting and
    # redone only for postings that changed or when the confirmed skills change.
    key = hashlib.sha256(json.dumps(tags, ensure_ascii=False).encode("utf-8")).hexdigest()
    connection = _open(path)
    try:
        with _transaction(connection):
            stale = connection.execute(
                "SELECT provider, board, job_id, title, text FROM listings WHERE matched_key IS NOT ?", (key,)
            ).fetchall()
            if stale:
                find = tag_finder(tags)
                connection.executemany(
                    """UPDATE listings SET matched_key = ?, matched_tags = ?
                       WHERE provider = ? AND board = ? AND job_id = ?""",
                    [
                        (key, json.dumps(find(row["title"] + "\n" + row["text"]), ensure_ascii=False),
                         row["provider"], row["board"], row["job_id"])
                        for row in stale
                    ],
                )
            rows = connection.execute(
                """SELECT l.provider, l.board, l.job_id, l.title, l.location, l.source, l.posted_at,
                          l.text_hash, l.matched_tags, s.company
                   FROM listings AS l JOIN sources AS s USING (provider, board)
                   ORDER BY l.provider, l.board, l.job_id"""
            ).fetchall()
    finally:
        connection.close()
    title_key, location_key = title.strip().casefold(), location.strip().casefold()
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        if title_key not in row["title"].casefold() or (hide_senior and is_senior_title(row["title"])):
            continue
        if location_key and row["location"] and location_key not in row["location"].casefold():
            continue
        role = (row["provider"], row["board"], row["title"].casefold(), row["text_hash"])
        if role not in groups:
            groups[role] = {
                "provider": row["provider"],
                "board": row["board"],
                "company": row["company"],
                "title": row["title"],
                "matched": json.loads(row["matched_tags"]),
                "postings": [],
            }
        groups[role]["postings"].append({
            field: row[field] for field in ("job_id", "location", "source", "posted_at")
        })
    ranked = list(groups.values())
    for group in ranked:
        group["posted_at"] = max((item["posted_at"] for item in group["postings"] if item["posted_at"]), default=None)
    ranked.sort(key=lambda group: (group["company"].casefold(), group["title"].casefold(), group["postings"][0]["job_id"]))
    ranked.sort(key=lambda group: group["posted_at"] or "", reverse=True)
    ranked.sort(key=lambda group: _matched_count(group["matched"]), reverse=True)
    return {
        "total": len(ranked),
        "skill_count": len({tag.casefold() for tag in tags}),
        "listings": ranked[offset:offset + limit],
    }
