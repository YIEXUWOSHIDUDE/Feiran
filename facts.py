"""Versioned candidate facts, classification metadata, and local retrieval."""

import argparse
import json
import re
import shlex
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DATABASE = Path(".local/workbench.db")
SCHEMA_VERSION = 2
MAX_FACT_LENGTH = 10_000
MAX_TAGS = 30
MAX_TAG_LENGTH = 100
MAX_IMPORT_FACTS = 500
FACT_TYPES = (
    "project", "experience", "skill", "education", "eligibility",
    "availability", "achievement", "other",
)


class FactStoreError(Exception):
    """The fact store could not safely perform the requested operation."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fact_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactStoreError("事实内容必须是非空字符串")
    text = value.strip()
    if len(text) > MAX_FACT_LENGTH:
        raise FactStoreError(f"事实内容不能超过 {MAX_FACT_LENGTH} 个字符")
    return text


def _fact_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("fact-") or len(value) > 64:
        raise FactStoreError("事实 ID 格式无效")
    suffix = value[5:]
    if not suffix or not suffix.isascii() or not all(character.isalnum() or character == "-" for character in suffix):
        raise FactStoreError("事实 ID 格式无效")
    return value


def _fact_type(value: Any) -> str:
    if value not in FACT_TYPES:
        raise FactStoreError(f"事实类型必须是：{', '.join(FACT_TYPES)}")
    return value


def _tags(values: Iterable[str]) -> list[tuple[str, str]]:
    if isinstance(values, (str, bytes)):
        raise FactStoreError("事实标签必须是字符串列表")
    normalized: dict[str, str] = {}
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise FactStoreError("事实标签必须是非空字符串")
        tag = value.strip()
        if len(tag) > MAX_TAG_LENGTH:
            raise FactStoreError(f"事实标签不能超过 {MAX_TAG_LENGTH} 个字符")
        normalized.setdefault(tag.casefold(), tag)
    if len(normalized) > MAX_TAGS:
        raise FactStoreError(f"每个事实版本最多包含 {MAX_TAGS} 个标签")
    return sorted(((tag, key) for key, tag in normalized.items()), key=lambda item: item[1])


def _connect(path: Path, create: bool) -> sqlite3.Connection:
    if path.exists() and not path.is_file():
        raise FactStoreError(f"数据库路径不是文件：{path}")
    if not path.exists():
        if not create:
            raise FactStoreError(f"事实数据库不存在：{path}")
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error as exc:
        raise FactStoreError("无法打开事实数据库") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _create_schema_v2(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE facts (
            fact_id TEXT PRIMARY KEY,
            current_version INTEGER NOT NULL CHECK (current_version >= 1),
            created_at TEXT NOT NULL
        );
        CREATE TABLE fact_versions (
            fact_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 1),
            text TEXT NOT NULL CHECK (length(trim(text)) > 0),
            fact_type TEXT NOT NULL CHECK (
                fact_type IN ('project', 'experience', 'skill', 'education',
                              'eligibility', 'availability', 'achievement', 'other')
            ),
            status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed')),
            created_at TEXT NOT NULL,
            confirmed_at TEXT,
            CHECK ((status = 'pending' AND confirmed_at IS NULL)
                OR (status = 'confirmed' AND confirmed_at IS NOT NULL)),
            PRIMARY KEY (fact_id, version),
            FOREIGN KEY (fact_id) REFERENCES facts(fact_id)
        );
        CREATE TABLE fact_tags (
            fact_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            tag TEXT NOT NULL CHECK (length(trim(tag)) > 0),
            tag_key TEXT NOT NULL CHECK (length(trim(tag_key)) > 0),
            PRIMARY KEY (fact_id, version, tag_key),
            FOREIGN KEY (fact_id, version)
                REFERENCES fact_versions(fact_id, version) ON DELETE CASCADE
        );
        CREATE INDEX fact_versions_status_type_idx ON fact_versions(status, fact_type);
        CREATE INDEX fact_tags_key_idx ON fact_tags(tag_key);
        PRAGMA user_version = 2;
        COMMIT;
        """
    )


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        BEGIN IMMEDIATE;
        ALTER TABLE fact_versions
            ADD COLUMN fact_type TEXT NOT NULL DEFAULT 'other' CHECK (
                fact_type IN ('project', 'experience', 'skill', 'education',
                              'eligibility', 'availability', 'achievement', 'other')
            );
        CREATE TABLE fact_tags (
            fact_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            tag TEXT NOT NULL CHECK (length(trim(tag)) > 0),
            tag_key TEXT NOT NULL CHECK (length(trim(tag_key)) > 0),
            PRIMARY KEY (fact_id, version, tag_key),
            FOREIGN KEY (fact_id, version)
                REFERENCES fact_versions(fact_id, version) ON DELETE CASCADE
        );
        DROP INDEX IF EXISTS fact_versions_status_idx;
        CREATE INDEX fact_versions_status_type_idx ON fact_versions(status, fact_type);
        CREATE INDEX fact_tags_key_idx ON fact_tags(tag_key);
        PRAGMA user_version = 2;
        COMMIT;
        """
    )


def _ensure_schema(connection: sqlite3.Connection) -> None:
    version = _schema_version(connection)
    if version == 0:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
        if existing is not None:
            raise FactStoreError("数据库包含未知表，不能初始化为事实库")
        _create_schema_v2(connection)
    elif version == 1:
        _migrate_v1_to_v2(connection)
    elif version != SCHEMA_VERSION:
        raise FactStoreError(f"不支持的事实数据库版本：{version}")
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('facts', 'fact_versions', 'fact_tags')"
        )
    }
    if tables != {"facts", "fact_versions", "fact_tags"}:
        raise FactStoreError("事实数据库缺少必要表")


def initialize_database(path: Path = DEFAULT_DATABASE) -> None:
    connection = _connect(path, create=True)
    try:
        with connection:
            _ensure_schema(connection)
    except sqlite3.Error as exc:
        raise FactStoreError("无法初始化或迁移事实数据库") from exc
    finally:
        connection.close()


def _open_store(path: Path) -> sqlite3.Connection:
    connection = _connect(path, create=False)
    try:
        with connection:
            _ensure_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _version_tags(connection: sqlite3.Connection, fact_id: str, version: int) -> list[str]:
    return [row["tag"] for row in connection.execute(
        "SELECT tag FROM fact_tags WHERE fact_id = ? AND version = ? ORDER BY tag_key",
        (fact_id, version),
    )]


def _row_to_fact(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    current_version: int | None = None,
) -> dict[str, Any]:
    result = {
        "id": row["fact_id"], "version": row["version"], "text": row["text"],
        "fact_type": row["fact_type"],
        "tags": _version_tags(connection, row["fact_id"], row["version"]),
        "status": row["status"], "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
    }
    if current_version is not None:
        result["is_current"] = row["version"] == current_version
    return result


def _insert_tags(
    connection: sqlite3.Connection,
    fact_id: str,
    version: int,
    tags: list[tuple[str, str]],
) -> None:
    connection.executemany(
        "INSERT INTO fact_tags(fact_id, version, tag, tag_key) VALUES (?, ?, ?, ?)",
        [(fact_id, version, tag, key) for tag, key in tags],
    )


def _add_in_transaction(
    connection: sqlite3.Connection,
    text: str,
    fact_type: str,
    tags: list[tuple[str, str]],
) -> tuple[dict[str, Any], bool]:
    rows = connection.execute(
        """SELECT v.fact_id, v.version, v.text, v.fact_type, v.status,
                  v.created_at, v.confirmed_at
           FROM facts AS f JOIN fact_versions AS v
             ON v.fact_id = f.fact_id AND v.version = f.current_version
           WHERE v.text = ? AND v.fact_type = ?""",
        (text, fact_type),
    ).fetchall()
    wanted_keys = [key for _, key in tags]
    for row in rows:
        existing_keys = [tag.casefold() for tag in _version_tags(
            connection, row["fact_id"], row["version"]
        )]
        if existing_keys == wanted_keys:
            return _row_to_fact(connection, row), False
    fact_id = f"fact-{uuid.uuid4().hex[:12]}"
    created_at = _now()
    connection.execute(
        "INSERT INTO facts(fact_id, current_version, created_at) VALUES (?, 1, ?)",
        (fact_id, created_at),
    )
    connection.execute(
        """INSERT INTO fact_versions(
               fact_id, version, text, fact_type, status, created_at, confirmed_at
           ) VALUES (?, 1, ?, ?, 'pending', ?, NULL)""",
        (fact_id, text, fact_type, created_at),
    )
    _insert_tags(connection, fact_id, 1, tags)
    row = connection.execute(
        """SELECT fact_id, version, text, fact_type, status, created_at, confirmed_at
           FROM fact_versions WHERE fact_id = ? AND version = 1""",
        (fact_id,),
    ).fetchone()
    return _row_to_fact(connection, row), True


def add_fact(
    path: Path,
    text: str,
    fact_type: str,
    tags: Iterable[str] = (),
) -> tuple[dict[str, Any], bool]:
    """Create one classified pending fact, deduplicating the complete payload."""
    normalized_text = _fact_text(text)
    normalized_type = _fact_type(fact_type)
    normalized_tags = _tags(tags)
    initialize_database(path)
    connection = _open_store(path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            return _add_in_transaction(
                connection, normalized_text, normalized_type, normalized_tags
            )
    except sqlite3.Error as exc:
        raise FactStoreError("无法添加事实") from exc
    finally:
        connection.close()


def import_facts(path: Path, items: Any) -> list[tuple[dict[str, Any], bool]]:
    """Validate every item first, then add all as pending facts in one transaction."""
    if not isinstance(items, list) or not items:
        raise FactStoreError("导入文件必须包含非空 facts 数组")
    if len(items) > MAX_IMPORT_FACTS:
        raise FactStoreError(f"一次最多导入 {MAX_IMPORT_FACTS} 条事实")
    normalized = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise FactStoreError(f"facts[{index}] 必须是对象")
        unknown = set(item) - {"text", "type", "tags"}
        if unknown:
            raise FactStoreError(f"facts[{index}] 包含未知字段：{', '.join(sorted(unknown))}")
        tags = item.get("tags", [])
        if not isinstance(tags, list):
            raise FactStoreError(f"facts[{index}].tags 必须是字符串数组")
        try:
            normalized.append((_fact_text(item.get("text")), _fact_type(item.get("type")), _tags(tags)))
        except FactStoreError as exc:
            raise FactStoreError(f"facts[{index}]：{exc}") from exc
    initialize_database(path)
    connection = _open_store(path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            return [_add_in_transaction(connection, *values) for values in normalized]
    except sqlite3.Error as exc:
        raise FactStoreError("无法导入事实") from exc
    finally:
        connection.close()


def revise_fact(
    path: Path,
    fact_id: str,
    text: str | None = None,
    fact_type: str | None = None,
    tags: Iterable[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create a pending version, carrying forward fields not changed by the caller."""
    fact_id = _fact_id(fact_id)
    if text is None and fact_type is None and tags is None:
        raise FactStoreError("修改事实时至少提供 --text、--type 或 --tag")
    connection = _open_store(path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT f.current_version, v.fact_id, v.version, v.text, v.fact_type,
                          v.status, v.created_at, v.confirmed_at
                   FROM facts AS f JOIN fact_versions AS v
                     ON v.fact_id = f.fact_id AND v.version = f.current_version
                   WHERE f.fact_id = ?""",
                (fact_id,),
            ).fetchone()
            if current is None:
                raise FactStoreError(f"事实不存在：{fact_id}")
            next_text = _fact_text(text) if text is not None else current["text"]
            next_type = _fact_type(fact_type) if fact_type is not None else current["fact_type"]
            current_tags = _version_tags(connection, fact_id, current["version"])
            next_tags = _tags(tags) if tags is not None else _tags(current_tags)
            if (next_text == current["text"] and next_type == current["fact_type"]
                    and [key for _, key in next_tags] == [tag.casefold() for tag in current_tags]):
                return _row_to_fact(connection, current), False
            version = current["current_version"] + 1
            created_at = _now()
            connection.execute(
                """INSERT INTO fact_versions(
                       fact_id, version, text, fact_type, status, created_at, confirmed_at
                   ) VALUES (?, ?, ?, ?, 'pending', ?, NULL)""",
                (fact_id, version, next_text, next_type, created_at),
            )
            _insert_tags(connection, fact_id, version, next_tags)
            connection.execute(
                "UPDATE facts SET current_version = ? WHERE fact_id = ?", (version, fact_id)
            )
            row = connection.execute(
                """SELECT fact_id, version, text, fact_type, status, created_at, confirmed_at
                   FROM fact_versions WHERE fact_id = ? AND version = ?""",
                (fact_id, version),
            ).fetchone()
            return _row_to_fact(connection, row), True
    except sqlite3.Error as exc:
        raise FactStoreError("无法创建事实新版本") from exc
    finally:
        connection.close()


def _confirm_in_transaction(
    connection: sqlite3.Connection,
    fact_id: str,
    version: int,
) -> tuple[dict[str, Any], bool]:
    fact = connection.execute(
        "SELECT current_version FROM facts WHERE fact_id = ?", (fact_id,)
    ).fetchone()
    if fact is None:
        raise FactStoreError(f"事实不存在：{fact_id}")
    if fact["current_version"] != version:
        raise FactStoreError(
            f"{fact_id} 只能确认当前版本 {fact['current_version']}，不能确认版本 {version}"
        )
    row = connection.execute(
        """SELECT fact_id, version, text, fact_type, status, created_at, confirmed_at
           FROM fact_versions WHERE fact_id = ? AND version = ?""",
        (fact_id, version),
    ).fetchone()
    if row["status"] == "confirmed":
        return _row_to_fact(connection, row), False
    confirmed_at = _now()
    connection.execute(
        """UPDATE fact_versions SET status = 'confirmed', confirmed_at = ?
           WHERE fact_id = ? AND version = ?""",
        (confirmed_at, fact_id, version),
    )
    row = connection.execute(
        """SELECT fact_id, version, text, fact_type, status, created_at, confirmed_at
           FROM fact_versions WHERE fact_id = ? AND version = ?""",
        (fact_id, version),
    ).fetchone()
    return _row_to_fact(connection, row), True


def confirm_facts(
    path: Path,
    refs: Iterable[tuple[str, int]],
) -> list[tuple[dict[str, Any], bool]]:
    """Confirm several exact current versions together; one stale ref confirms nothing."""
    validated: list[tuple[str, int]] = []
    for fact_id, version in refs:
        fact_id = _fact_id(fact_id)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise FactStoreError("事实版本必须是正整数")
        if any(fact_id == seen for seen, _ in validated):
            raise FactStoreError(f"重复确认事实：{fact_id}")
        validated.append((fact_id, version))
    if not validated:
        raise FactStoreError("至少需要确认一个事实")
    connection = _open_store(path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            return [
                _confirm_in_transaction(connection, fact_id, version)
                for fact_id, version in validated
            ]
    except sqlite3.Error as exc:
        raise FactStoreError("无法确认事实") from exc
    finally:
        connection.close()


def confirm_fact(path: Path, fact_id: str, version: int) -> tuple[dict[str, Any], bool]:
    """Confirm only the current complete version, including classification and tags."""
    return confirm_facts(path, [(fact_id, version)])[0]


def parse_fact_refs(values: list[str], version: int | None = None) -> list[tuple[str, int]]:
    """Read FACT_ID@VERSION refs, or one FACT_ID combined with --version."""
    refs: list[tuple[str, int]] = []
    for value in values:
        fact_id, separator, version_text = value.partition("@")
        if not separator:
            if version is None or len(values) != 1:
                raise FactStoreError("确认多个事实时请使用 FACT_ID@VERSION；单个事实也可用 --version")
            refs.append((fact_id, version))
            continue
        if version is not None:
            raise FactStoreError("使用 FACT_ID@VERSION 时不要再提供 --version")
        if not version_text.isascii() or not version_text.isdigit():
            raise FactStoreError(f"事实版本格式无效：{value}")
        refs.append((fact_id, int(version_text)))
    return refs


def list_facts(path: Path, history_for: str | None = None) -> list[dict[str, Any]]:
    """List current facts, or every version of one fact."""
    connection = _open_store(path)
    try:
        if history_for is not None:
            fact_id = _fact_id(history_for)
            fact = connection.execute(
                "SELECT current_version FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if fact is None:
                raise FactStoreError(f"事实不存在：{fact_id}")
            rows = connection.execute(
                """SELECT fact_id, version, text, fact_type, status, created_at, confirmed_at
                   FROM fact_versions WHERE fact_id = ? ORDER BY version""", (fact_id,)
            ).fetchall()
            return [_row_to_fact(connection, row, fact["current_version"]) for row in rows]
        rows = connection.execute(
            """SELECT v.fact_id, v.version, v.text, v.fact_type, v.status,
                      v.created_at, v.confirmed_at
               FROM facts AS f JOIN fact_versions AS v
                 ON v.fact_id = f.fact_id AND v.version = f.current_version
               ORDER BY f.created_at, f.fact_id"""
        ).fetchall()
        return [_row_to_fact(connection, row, row["version"]) for row in rows]
    except sqlite3.Error as exc:
        raise FactStoreError("无法读取事实") from exc
    finally:
        connection.close()


def tag_pattern(tag: str) -> re.Pattern[str]:
    """Literal tag matcher shared by job ranking and fact retrieval.

    Short tags such as Go, R or C are case-sensitive and may not touch "&"
    (single letters also not "-"), so "go to market", "R&D" and "C-suite" do
    not count. Boundaries only look at ASCII word characters, so "熟悉Python"
    still matches; non-ASCII tags such as "机器学习" match as substrings.
    """
    if not tag.isascii() or not any(character.isalnum() for character in tag):
        return re.compile(re.escape(tag), re.IGNORECASE)
    core_length = sum(character.isalnum() for character in tag)
    if core_length == 1:
        edge, flags = "A-Za-z0-9_&-", 0
    elif core_length == 2:
        edge, flags = "A-Za-z0-9_&", 0
    else:
        edge, flags = "A-Za-z0-9_", re.IGNORECASE
    return re.compile(f"(?<![{edge}]){re.escape(tag)}(?![{edge}])", flags)


def find_confirmed_facts(
    path: Path,
    query_text: str,
    preferred_types: Iterable[str] = (),
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Retrieve a bounded current-fact set using versioned tags and type preferences."""
    query = _fact_text(query_text)
    preferred = tuple(dict.fromkeys(_fact_type(value) for value in preferred_types))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
        raise FactStoreError("事实检索 limit 必须在 1 到 100 之间")
    connection = _open_store(path)
    try:
        tag_rows = connection.execute(
            """SELECT t.fact_id, t.tag
               FROM facts AS f JOIN fact_versions AS v
                 ON v.fact_id = f.fact_id AND v.version = f.current_version
               JOIN fact_tags AS t ON t.fact_id = v.fact_id AND t.version = v.version
               WHERE v.status = 'confirmed'"""
        ).fetchall()
        matched_by_id: dict[str, list[str]] = {}
        for row in tag_rows:
            tag = row["tag"]
            if tag_pattern(tag).search(query):
                matched_by_id.setdefault(row["fact_id"], []).append(tag)

        matched_ids = tuple(matched_by_id)
        if not matched_ids and not preferred:
            return []
        where_parts = []
        where_parameters: list[Any] = []
        if matched_ids:
            where_parts.append("v.fact_id IN (" + ",".join("?" for _ in matched_ids) + ")")
            where_parameters.extend(matched_ids)
        if preferred:
            where_parts.append("v.fact_type IN (" + ",".join("?" for _ in preferred) + ")")
            where_parameters.extend(preferred)
        matched_order = "1"
        order_parameters: list[Any] = []
        if matched_ids:
            matched_order = "CASE WHEN v.fact_id IN (" + ",".join("?" for _ in matched_ids) + ") THEN 0 ELSE 1 END"
            order_parameters.extend(matched_ids)
        preferred_order = "1"
        if preferred:
            preferred_order = "CASE WHEN v.fact_type IN (" + ",".join("?" for _ in preferred) + ") THEN 0 ELSE 1 END"
            order_parameters.extend(preferred)
        rows = connection.execute(
            f"""SELECT v.fact_id, v.version, v.text, v.fact_type, v.status,
                       v.created_at, v.confirmed_at
                FROM facts AS f JOIN fact_versions AS v
                  ON v.fact_id = f.fact_id AND v.version = f.current_version
                WHERE v.status = 'confirmed' AND ({' OR '.join(where_parts)})
                ORDER BY {matched_order}, {preferred_order}, v.confirmed_at DESC, v.fact_id
                LIMIT ?""",
            [*where_parameters, *order_parameters, limit],
        ).fetchall()
        results = []
        for row in rows:
            fact = _row_to_fact(connection, row)
            fact["retrieval_basis"] = {
                "matched_tags": matched_by_id.get(fact["id"], []),
                "preferred_type": fact["fact_type"] in preferred,
            }
            results.append(fact)
        return results
    except sqlite3.Error as exc:
        raise FactStoreError("无法检索事实") from exc
    finally:
        connection.close()


def load_confirmed_fact(path: Path, fact_id: str, version: int) -> dict[str, Any]:
    """Load one version only when it is still current and confirmed."""
    fact_id = _fact_id(fact_id)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise FactStoreError("事实版本必须是正整数")
    connection = _open_store(path)
    try:
        row = connection.execute(
            """SELECT v.fact_id, v.version, v.text, v.fact_type, v.status,
                      v.created_at, v.confirmed_at
               FROM facts AS f JOIN fact_versions AS v
                 ON v.fact_id = f.fact_id AND v.version = f.current_version
               WHERE v.fact_id = ? AND v.version = ? AND v.status = 'confirmed'""",
            (fact_id, version),
        ).fetchone()
        if row is None:
            raise FactStoreError(f"事实 {fact_id} 版本 {version} 已不是当前已确认版本")
        return _row_to_fact(connection, row)
    except sqlite3.Error as exc:
        raise FactStoreError("无法读取指定事实版本") from exc
    finally:
        connection.close()


def load_search_terms(path: Path) -> list[dict[str, Any]]:
    """Return only confirmed current tag metadata for local job-list ranking."""
    connection = _open_store(path)
    try:
        rows = connection.execute(
            """SELECT t.tag, v.fact_id, v.version
               FROM facts AS f JOIN fact_versions AS v
                 ON v.fact_id = f.fact_id AND v.version = f.current_version
               JOIN fact_tags AS t ON t.fact_id = v.fact_id AND t.version = v.version
               WHERE v.status = 'confirmed' ORDER BY t.tag_key, v.fact_id"""
        ).fetchall()
        return [
            {
                "term": row["tag"],
                "fact_id": row["fact_id"],
                "fact_version": row["version"],
            }
            for row in rows
        ]
    except sqlite3.Error as exc:
        raise FactStoreError("无法读取事实检索标签") from exc
    finally:
        connection.close()


def load_confirmed_fact_texts(
    path: Path,
    fact_refs: Iterable[tuple[str, int]],
) -> dict[tuple[str, int], str]:
    """Load text only when the selected fact version is still current and confirmed."""
    refs: dict[tuple[str, int], None] = {}
    for fact_id, version in fact_refs:
        fact_id = _fact_id(fact_id)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise FactStoreError("事实版本必须是正整数")
        refs[(fact_id, version)] = None
    if not refs:
        return {}
    ids = tuple(dict.fromkeys(fact_id for fact_id, _ in refs))
    connection = _open_store(path)
    try:
        rows = connection.execute(
            f"""SELECT v.fact_id, v.version, v.text
                FROM facts AS f JOIN fact_versions AS v
                  ON v.fact_id = f.fact_id AND v.version = f.current_version
                WHERE v.status = 'confirmed'
                  AND v.fact_id IN ({','.join('?' for _ in ids)})""",
            ids,
        ).fetchall()
        return {
            (row["fact_id"], row["version"]): row["text"]
            for row in rows
            if (row["fact_id"], row["version"]) in refs
        }
    except sqlite3.Error as exc:
        raise FactStoreError("无法读取已匹配的事实原文") from exc
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="管理本机候选人事实版本、分类、标签和人工确认")
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="初始化或迁移本机 SQLite 事实库")
    init.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    add = actions.add_parser("add", help="添加分类后的 pending 事实")
    add.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    add.add_argument("--text", required=True)
    add.add_argument("--type", required=True, choices=FACT_TYPES, dest="fact_type")
    add.add_argument("--tag", action="append", default=[], dest="tags")
    importing = actions.add_parser("import", help="从 JSON 文件批量添加 pending 事实")
    importing.add_argument("file", type=Path, help='格式：{"facts": [{"type": ..., "text": ..., "tags": [...]}]}')
    importing.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    revise = actions.add_parser("revise", help="修改文本、类型或标签并创建 pending 版本")
    revise.add_argument("fact_id")
    revise.add_argument("--text")
    revise.add_argument("--type", choices=FACT_TYPES, dest="fact_type")
    tag_change = revise.add_mutually_exclusive_group()
    tag_change.add_argument("--tag", action="append", dest="tags")
    tag_change.add_argument("--clear-tags", action="store_const", const=[], dest="tags")
    revise.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    confirm = actions.add_parser("confirm", help="确认一个或多个事实的当前完整版本")
    confirm.add_argument("refs", nargs="+", metavar="FACT_ID@VERSION")
    confirm.add_argument("--version", type=int, help="只确认一个 FACT_ID 时可用的旧写法")
    confirm.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    listing = actions.add_parser("list", help="查看当前事实或一个事实的版本历史")
    listing.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    listing.add_argument("--history", metavar="FACT_ID")
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            initialize_database(args.db)
            result = {"database": str(args.db), "schema_version": SCHEMA_VERSION}
        elif args.action == "add":
            fact, created = add_fact(args.db, args.text, args.fact_type, args.tags)
            result = {"created": created, "fact": fact}
        elif args.action == "import":
            data = json.loads(args.file.read_text(encoding="utf-8"))
            imported = import_facts(args.db, data.get("facts") if isinstance(data, dict) else None)
            pending_refs = list(dict.fromkeys(
                f"{fact['id']}@{fact['version']}"
                for fact, _ in imported if fact["status"] == "pending"
            ))
            command = ["python3", "facts.py", "confirm", *pending_refs]
            if args.db != DEFAULT_DATABASE:
                command += ["--db", str(args.db)]
            result = {
                "created_count": sum(created for _, created in imported),
                "existing_count": sum(not created for _, created in imported),
                "facts": [{"created": created, **fact} for fact, created in imported],
                "next_step": (
                    "逐条核对原文、分类和标签，只保留确实无误的 ID 后运行："
                    + shlex.join(command) if pending_refs else "没有待确认的事实"
                ),
            }
        elif args.action == "revise":
            fact, created = revise_fact(args.db, args.fact_id, args.text, args.fact_type, args.tags)
            result = {"created": created, "fact": fact}
        elif args.action == "confirm":
            confirmed = confirm_facts(args.db, parse_fact_refs(args.refs, args.version))
            result = {
                "changed_count": sum(changed for _, changed in confirmed),
                "facts": [{"changed": changed, **fact} for fact, changed in confirmed],
            }
        else:
            result = {"facts": list_facts(args.db, args.history)}
    except (FactStoreError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"事实库操作失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
