"""Company job boards the user follows, their open postings, and ranking by confirmed skills.

listings.db is a local copy of public postings. Deleting it loses only the company list
and downloaded postings; facts and work on a job live in other files.
"""

import hashlib
from collections import OrderedDict
from threading import Lock
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from feiran.facts import load_search_terms, tag_finder
from feiran.job_search import SearchError, check_board, fetch_board
from feiran.sponsorship import RULE_VERSION as SPONSOR_RULE_VERSION, STATUSES, assess_sponsorship, shown


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
SCHEMA_VERSION = 2
SPONSOR_CACHE_SCHEMA = """CREATE TABLE sponsorship_cache (
    text_hash TEXT NOT NULL, rule_version INTEGER NOT NULL, answer TEXT NOT NULL,
    PRIMARY KEY (text_hash, rule_version)
)"""
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
                connection.execute(SPONSOR_CACHE_SCHEMA)
                _add_starter(connection, starter)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version == 1:
            with _transaction(connection):
                # Another request may have completed the upgrade while this one waited.
                if connection.execute("PRAGMA user_version").fetchone()[0] == 1:
                    connection.execute(SPONSOR_CACHE_SCHEMA)
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


# Hiding senior roles also reads the JD for required experience of this many years or more.
# The wording rules below are a finite list (see requires_senior_experience), not language understanding.
SENIOR_YEARS = 5
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20}
_CHINESE_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUMBER = "|".join([r"\d{1,2}", *_NUMBER_WORDS])
_CHINESE_NUMBER = r"\d{1,2}|[一二两三四五六七八九十]{1,3}"
# Without a number of five or more shortly before "year" or 年, no rule below can apply.
# Looking from the year word back is quick; looking from every number forward is not.
_YEAR_WORD = re.compile(r"year|yrs|年")
_FIVE_OR_MORE = re.compile(r"(?<!\d)(?:0?[5-9]|[1-9]\d)|\b(?:five|six|seven|eight|nine|ten|eleven|twelve|fifteen|"
                           r"twenty)\b|[五六七八九十]")
# "at least 5 years of experience", "5 to 8 years of related industry experience", "five (5)+ years' ..."
_YEARS = re.compile(
    r"(?:\b(?:(?P<upper>up\s+to|less\s+than|fewer\s+than|under|no\s+more\s+than|(?:a\s+)?maximum(?:\s+of)?)"
    r"|at\s+least|(?:a\s+)?minimum(?:\s+of)?|min\.|no\s+less\s+than|more\s+than|over)\s+)?"
    rf"(?<![\w.])(?P<low>{_NUMBER})(?:\s*\(\d{{1,2}}\))?(?:\s*(?:-|–|—|to)\s*(?:{_NUMBER})(?:\s*\(\d{{1,2}}\))?)?"
    r"(?:\s*\+|\s+or\s+more)?\s*(?:years?|yrs?)\b['’]?"
    r"(?=(?:\s+(?!years?\b|yrs?\b)[\w/&+#'’-]+(?:\.[\w/&+#'’-]+)*){0,6}?\s+"
    r"(?:experience|experienced|working|industry)\b)",
    re.IGNORECASE)
# "5年以上工作经验", "五年以上", "5至8年Java开发经验"
_CHINESE_YEARS = re.compile(
    r"(?:(?P<upper>最多|不超过|少于|低于|小于)|至少|最少|不少于|不低于|超过|大于)?"
    rf"(?<!\d)(?P<low>{_CHINESE_NUMBER})\s*(?:[-~～—–至到]\s*(?:{_CHINESE_NUMBER})\s*)?年(?!以下|以内|内|制)"
    r"(?=[^，,；;。：:、\n]{0,15}?经验)")
# Years count only as the candidate's: opening a clause, after "and", "you have", "must have"...,
# or after "with" in a clause naming who is hired ("candidates with", "looking for someone with").
# "Our company has", "a mentor who has", "Senior engineers with" and "a team with" do not qualify.
_LEAD = re.compile(
    r"(?:^|\b(?:and|plus|including|requires?|required|requiring|needs?)"
    r"|(?:^|\b(?:you|you'll|you’ll|candidates?|applicants?|must|should|will|would)\s+)(?:have|has|bring|possess))$",
    re.IGNORECASE)
_HIRED_WITH = re.compile(r"\b(?:candidates?|applicants?|someone|somebody|individuals?|degree|looking\s+for|seeking|"
                         r"hiring)\b.*\bwith$", re.IGNORECASE)
_OTHERS = re.compile(r"\b(?:mentor\w*|learn(?:ing)?\s+from|work(?:ing)?\s+(?:with|alongside)|alongside|"
                     r"colleagues|peers|teammates|team)\b", re.IGNORECASE)
_CHINESE_LEAD = re.compile(r"(?:需|需要|要求|应|须|必须)?(?:具有|具备|拥有|有)?")
_BULLET = re.compile(r"^[\s\-–—•*·▪◦●○■>]*(?:\(?\d{1,2}[).、]\s*)?")
_OPTIONAL_BEFORE = re.compile(r"\b(?:ideally|preferably|preferred|bonus|nice[\s-]to[\s-]have|optionally)\b|最好|优先",
                              re.IGNORECASE)
_OPTIONAL_AFTER = re.compile(r"\b(?:preferred|a\s+plus|nice[\s-]to[\s-]have|bonus|desired|desirable|optional|"
                             r"advantageous|an\s+advantage|helpful)\b|优先|加分|更佳|为佳", re.IGNORECASE)
# A heading (or the label before a colon) opens a part whose years do not count (preferred, or
# about the company) or a required one. "As an ideal candidate, you will have:" is a required
# one: "ideal" is not "ideally". A required heading has to end in its heading words, so a line
# like "Required to travel" is no heading.
_PREFERRED_HEADING = re.compile(r"\b(?:preferred|nice[\s-]to[\s-]haves?|bonus|desired|desirable|ideally|"
                                r"good\s+to\s+have|great\s+to\s+have|pluses|a\s+plus)\b|^(?:加分|优先)", re.IGNORECASE)
_ABOUT_HEADING = re.compile(r"^(?:about\s+(?!you\b|the\s+(?:role|job|position|opportunity|team)\b)\S.*|"
                            r"who\s+we\s+are|our\s+(?:company|story)|关于我们|公司(?:介绍|简介))$", re.IGNORECASE)
_REQUIRED_HEADING = re.compile(
    r"\b(?:requirements?|required|qualifications|must[\s-]haves?|looking\s+for|about\s+you|who\s+you\s+are|"
    r"you(?:'ll|’ll|\s+will)?\s+(?:have|need|bring)|ideal\s+candidate)\W*$|^(?:任职|岗位|职位|基本)?(?:要求|资格|条件)$",
    re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"[,;:!?()，；：。！？（）、]|\.(?=\s)")
_SENTENCE_BREAK = re.compile(r"[;!?；。！？]|\.(?=\s|$)")
# "Bachelor's and 5 years, or Master's and 2": an alternative that asks for fewer years.
_OR = re.compile(r"\bor\b(?!\s+(?:more|greater|above)\b)|或(?!以上)", re.IGNORECASE)


def _years(number: str) -> int:
    """5, five or 五 as a number; anything unreadable counts as 0, which hides nothing."""
    if number.isdecimal():
        return int(number)
    if number.casefold() in _NUMBER_WORDS:
        return _NUMBER_WORDS[number.casefold()]
    tens, ten, units = number.partition("十")
    if not ten:
        return _CHINESE_DIGITS.get(number, 0)
    return (_CHINESE_DIGITS.get(tens, 0) if tens else 1) * 10 + _CHINESE_DIGITS.get(units, 0)


def _units(text: str) -> list[str]:
    """The JD's lines (one per paragraph or list item); a line opening with "or" belongs to the
    one before, as in "Bachelor's AND 6+ years" / "OR Master's AND 4+ years"."""
    units: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if units and re.match(r"(?:or\b|或)", line, re.IGNORECASE):
            units[-1] += " " + line
        elif line:
            units.append(line)
    return units


def _section(unit: str, first_years: int) -> bool | None:
    """True if this line opens a part whose years do not count (preferred, or about the company),
    False if it opens a required one, None if it is no heading. A heading is a short line, or a
    label ending in a colon before any years."""
    label = re.match(r"([^:：]*)[:：]", unit) if ":" in unit or "：" in unit else None
    if label and label.end() <= first_years and len(label.group(1).split()) <= 8:
        head = label.group(1).strip()
    elif (first_years == len(unit) and len(unit) <= 80 and len(unit.split()) <= 5
          and not unit.endswith((".", "。"))):
        head = unit
    else:
        return None
    if _PREFERRED_HEADING.search(head) or _ABOUT_HEADING.match(head):
        return True
    return False if _REQUIRED_HEADING.search(head) else None


# Store only digests and booleans, not thousands of full descriptions. Public JD analysis
# is independent of the requesting user; process restart or changed wording recomputes it.
_SENIOR_CACHE: OrderedDict[tuple[int, str], bool] = OrderedDict()
_SENIOR_CACHE_LOCK = Lock()
_SENIOR_CACHE_LIMIT = 32768


def requires_senior_experience(text: str) -> bool:
    key = (SENIOR_YEARS, hashlib.sha256(text.encode("utf-8")).hexdigest())
    with _SENIOR_CACHE_LOCK:
        if key in _SENIOR_CACHE:
            _SENIOR_CACHE.move_to_end(key)
            return _SENIOR_CACHE[key]
    result = _parse_senior_experience(text)
    with _SENIOR_CACHE_LOCK:
        _SENIOR_CACHE[key] = result
        _SENIOR_CACHE.move_to_end(key)
        while len(_SENIOR_CACHE) > _SENIOR_CACHE_LIMIT:
            _SENIOR_CACHE.popitem(last=False)
    return result


def _parse_senior_experience(text: str) -> bool:
    """Whether the JD states that the role requires at least SENIOR_YEARS years of experience.

    It reads only this wording: "5+ years", "5 or more years", "at least / a minimum of five
    years", "5 years", ranges "5-8 / 5–8 / 5 to 8 years" (the lower end counts), or "5年以上 /
    五年以上 / 5至8年", followed within a few words by experience (经验). The years must open a
    line or clause, follow "and", "you have", "must have" and the like, or follow "with" in a
    clause naming who is hired ("candidates with"); so "Our company has 10 years of
    experience" and "Senior engineers with 10+ years of experience will mentor you" do not
    count. Years under a preferred heading (Preferred / Nice to have / 加分项) or an About us /
    About <company> heading, or marked ideally, preferably, preferred, a plus, 优先 in their own
    clause, do not count; a later required heading (Requirements, Required Qualifications, ...)
    counts again. An "or" offering fewer years ("Bachelor's and 5 years, or Master's and 2
    years", "a degree or 5+ years") keeps the role. Anything phrased otherwise leaves the role
    visible."""
    lowered = text.lower()
    if not any(_FIVE_OR_MORE.search(lowered, max(0, word.start() - 24), word.start())
               for word in _YEAR_WORD.finditer(lowered)):
        return False
    ignored = False  # inside a preferred part or one about the company
    for unit in _units(text):
        found = []
        if _YEAR_WORD.search(unit.lower()):
            found = sorted([(match, False) for match in _YEARS.finditer(unit)]
                           + [(match, True) for match in _CHINESE_YEARS.finditer(unit)],
                           key=lambda item: item[0].start())
        section = _section(unit, found[0][0].start() if found else len(unit))
        if section is not None:
            ignored = section
        if ignored:
            continue
        stated = []  # (match, lower bound, whether it reads as the candidate's requirement)
        for index, (match, chinese) in enumerate(found):
            following = found[index + 1][0].start() if index + 1 < len(found) else len(unit)
            stop = _SENTENCE_BREAK.search(unit, match.end(), following)
            start = max([0] + [item.end() for item in _CLAUSE_BREAK.finditer(unit, 0, match.start())])
            before = unit[start:match.start()]
            # A marker after the years reaches to the sentence end or the next years, so the
            # "ideally" in "5 to 8 years ..., ideally 2 years ..." belongs to the 2 years only.
            if (match["upper"] or _OPTIONAL_BEFORE.search(before)
                    or _OPTIONAL_AFTER.search(unit, match.end(), stop.start() if stop else following)):
                continue
            lead = _BULLET.sub("", before).strip()
            if chinese:
                required = bool(_CHINESE_LEAD.fullmatch(lead))
            else:
                required = bool(_LEAD.search(lead) or _HIRED_WITH.search(lead)) and not _OTHERS.search(lead)
            stated.append((match, _years(match["low"]), required))
        for match, years, required in stated:
            if required and years >= SENIOR_YEARS and not any(
                    fewer < SENIOR_YEARS
                    and _OR.search(unit[min(match.end(), other.end()):max(match.start(), other.start())])
                    for other, fewer, _ in stated):
                return True
    return False


def _matched_count(tags: list[str]) -> int:
    return len({tag.casefold() for tag in tags})


def location_regions(location: str | None) -> set[str]:
    """Classify only explicit place names, never the JD language or company nationality.

    This deliberately finite vocabulary leaves unrecognized places unknown. A posting naming
    multiple regions can appear in both filters; Remote alone proves no country eligibility.
    """
    value = (location or "").casefold()
    mainland_city = re.search(
        r"\b(beijing|shanghai|shenzhen|guangzhou|hangzhou|suzhou|chengdu|wuhan|nanjing|"
        r"tianjin|xian|xi'an|chongqing|hefei|wuxi|xiamen|dongguan|zhuhai|changsha|ningbo)\b|"
        r"北京|上海|深圳|广州|杭州|苏州|成都|武汉|南京|天津|西安|重庆|合肥|无锡|厦门|东莞|珠海|长沙|宁波", value)
    other_china = re.search(r"\b(hong kong|hongkong|macau|macao|taiwan|taipei)\b|香港|澳门|澳門|台湾|臺灣|台北|臺北", value)
    mainland = mainland_city or (re.search(r"\b(china|prc)\b|中国|中國", value) and not other_china)
    other = other_china or re.search(
        r"\b(usa|united states|u\.s\.|united kingdom|uk|canada|germany|france|india|singapore|"
        r"japan|australia|netherlands|ireland|sweden|switzerland|brazil|mexico|spain|poland|"
        r"new york|san francisco|palo alto|mountain view|los angeles|seattle|boston|austin|"
        r"london|berlin|paris|tokyo|toronto|vancouver|sydney|bengaluru|bangalore|dublin)\b|"
        r"美国|英国|加拿大|德国|法国|印度|新加坡|日本|澳大利亚", value)
    result = ({"cn"} if mainland else set()) | ({"other"} if other else set())
    return result or {"unknown"}


def ranked_listings(
    path: Path,
    facts_db: Path,
    title: str = "",
    location: str = "",
    limit: int = PAGE_SIZE,
    offset: int = 0,
    hide_senior: bool = False,
    region: str = "all",
    sponsorship: str = "all",
) -> dict[str, Any]:
    """Open postings, most confirmed skills mentioned first; a count of words, not a fit score.

    The same role posted in several cities (same title and text) is one row. Ties go to the
    newest posting. A location filter keeps postings whose location is unknown. Filters work
    as group_postings describes.

    Sponsor answers are persisted by text digest and rule version, shared across identical
    descriptions. Changed text or rules get a new answer; reads never call a paid model.
    """
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE or offset < 0:
        raise ListingsError(f"每页岗位数必须在 1 到 {MAX_PAGE_SIZE} 之间")
    if region not in {"all", "cn", "other", "unknown"}:
        raise ListingsError("地区筛选无效")
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
                          l.text, l.text_hash, l.matched_tags, l.last_seen_at, s.company
                   FROM listings AS l JOIN sources AS s USING (provider, board)
                   ORDER BY l.provider, l.board, l.job_id"""
            ).fetchall()
            answers = {row["text_hash"]: json.loads(row["answer"]) for row in connection.execute(
                "SELECT text_hash, answer FROM sponsorship_cache WHERE rule_version = ?",
                (SPONSOR_RULE_VERSION,))}
            for row in rows:
                if row["text_hash"] not in answers:
                    answer = {**assess_sponsorship(row["text"]), "assessed_at": _now()}
                    answers[row["text_hash"]] = answer
                    connection.execute("INSERT OR REPLACE INTO sponsorship_cache VALUES (?, ?, ?)",
                                       (row["text_hash"], SPONSOR_RULE_VERSION, json.dumps(answer)))
            # The cache contains only current descriptions under the current rules.
            connection.execute("DELETE FROM sponsorship_cache WHERE rule_version != ? OR text_hash NOT IN "
                               "(SELECT text_hash FROM listings)", (SPONSOR_RULE_VERSION,))
    finally:
        connection.close()
    postings = [{**dict(row), "matched": json.loads(row["matched_tags"]),
                 "sponsorship": shown(answers[row["text_hash"]], source=row["source"],
                                      captured_at=row["last_seen_at"])} for row in rows]
    return group_postings(postings, len({tag.casefold() for tag in tags}), title, location, limit, offset,
                          hide_senior, region, sponsorship)


def group_postings(
    postings: list[dict[str, Any]],
    skill_count: int,
    title: str = "",
    location: str = "",
    limit: int = PAGE_SIZE,
    offset: int = 0,
    hide_senior: bool = False,
    region: str = "all",
    sponsorship: str = "all",
) -> dict[str, Any]:
    """Filter and rank postings (each with its stored JD ``text``, its ``matched`` skill words and
    the ``sponsorship`` answer its caller read or worked out; none shows as not assessed), most
    skills mentioned first. The same role posted in several cities (same title and text) is one
    row; ties go to the newest posting; a location filter keeps postings whose location is unknown.

    The title filter keeps titles containing every word of it, in any order ("software inter"
    finds "Software Engineering Internship"). hide_senior drops senior titles and JDs that
    requires_senior_experience reads as asking for five or more years; the rest stay. The text
    is only read here: no listing returned carries it."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE or offset < 0:
        raise ListingsError(f"每页岗位数必须在 1 到 {MAX_PAGE_SIZE} 之间")
    if region not in {"all", "cn", "other", "unknown"}:
        raise ListingsError("地区筛选无效")
    if sponsorship not in {"all", "not_assessed", *STATUSES}:
        raise ListingsError("Sponsor filter is invalid")
    title_words, location_key = title.casefold().split(), location.strip().casefold()
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in postings:
        if region != "all" and region not in location_regions(row["location"]):
            continue
        name = row["title"].casefold()
        if not all(word in name for word in title_words) or (hide_senior and is_senior_title(row["title"])):
            continue
        if location_key and row["location"] and location_key not in row["location"].casefold():
            continue
        assessment = row.get("sponsorship") or shown(None, source=row["source"], captured_at=row["last_seen_at"])
        if sponsorship != "all" and assessment["status"] != sponsorship:
            continue
        # Reading the JD costs the most, so it comes after every other filter.
        if hide_senior and requires_senior_experience(row.get("text") or ""):
            continue
        role = (row["provider"], row["board"], row["title"].casefold(), row["text_hash"])
        if role not in groups:
            groups[role] = {
                "provider": row["provider"],
                "board": row["board"],
                "company": row["company"],
                "title": row["title"],
                "matched": row["matched"],
                "sponsorship": assessment,
                "postings": [],
            }
        groups[role]["postings"].append({
            **{field: row[field] for field in ("job_id", "location", "source", "posted_at", "last_seen_at")},
            "sponsorship": assessment,
        })
    ranked = list(groups.values())
    for group in ranked:
        group["posted_at"] = max((item["posted_at"] for item in group["postings"] if item["posted_at"]), default=None)
    ranked.sort(key=lambda group: (group["company"].casefold(), group["title"].casefold(), group["postings"][0]["job_id"]))
    ranked.sort(key=lambda group: group["posted_at"] or "", reverse=True)
    ranked.sort(key=lambda group: _matched_count(group["matched"]), reverse=True)
    return {
        "total": len(ranked),
        "skill_count": skill_count,
        "listings": ranked[offset:offset + limit],
    }
