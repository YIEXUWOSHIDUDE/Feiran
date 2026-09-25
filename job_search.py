"""Read public Greenhouse, Lever or Ashby job boards, re-fetch a selected posting, or accept a pasted JD."""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from facts import FactStoreError, load_confirmed_fact_texts, load_search_terms, tag_pattern


API_ROOT = "https://boards-api.greenhouse.io/v1/boards"
LEVER_ROOT = "https://api.lever.co/v0/postings"
ASHBY_ROOT = "https://api.ashbyhq.com/posting-api/job-board"
PROVIDERS = ("greenhouse", "lever", "ashby")
BOARD_URLS = {
    "greenhouse": API_ROOT + "/{board}/jobs?content=true",
    "lever": LEVER_ROOT + "/{board}?mode=json",
    "ashby": ASHBY_ROOT + "/{board}",
}
# A whole board with full descriptions is large: OpenAI's Ashby board was 13.8 MB in 2026-09.
MAX_RESPONSE_BYTES = 50_000_000
MAX_SEARCH_RESULTS = 100
MAX_EVIDENCE_PER_JOB = 20
MAX_PASTED_CHARACTERS = 100_000
REQUEST_TIMEOUT_SECONDS = 20
FETCH_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1.0
RETRY_HTTP_CODES = {429, 500, 502, 503, 504}
BOARD_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
POSTING_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
BOARD_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}
BOARD_LINK_HINT = (
    "请粘贴公司招聘板链接，例如 https://boards.greenhouse.io/<公司>、"
    "https://jobs.lever.co/<公司> 或 https://jobs.ashbyhq.com/<公司>；"
    "公司官网若链接到这些网站，打开任一岗位即可看到"
)


class SearchError(Exception):
    """An input, source, or response error that should stop this search."""


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "li", "div", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "li", "div", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _normalize_lines(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _html_text(html: Any) -> str:
    """One line per paragraph or list item of ordinary HTML; anything else becomes empty."""
    if not isinstance(html, str):
        return ""
    parser = _HTMLText()
    parser.feed(html)
    return _normalize_lines("".join(parser.parts))


def plain_text(content: str) -> str:
    """Make Greenhouse HTML searchable while preserving the original separately.

    Greenhouse escapes its HTML once more (&lt;p&gt;), so it is unescaped first.
    """
    return _html_text(unescape(content))


def _optional_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def prepare_pasted_jd(
    text: str,
    title: str,
    company: str | None = None,
    url: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Wrap user-pasted JD text in the same snapshot shape as a re-fetched posting."""
    normalized = _normalize_lines(text)
    if not normalized:
        raise SearchError("粘贴的 JD 内容为空")
    if len(text) > MAX_PASTED_CHARACTERS:
        raise SearchError(f"粘贴的 JD 不能超过 {MAX_PASTED_CHARACTERS} 个字符")
    title = _optional_text(title)
    if title is None:
        raise SearchError("粘贴 JD 时必须提供岗位标题")
    source = _optional_text(url)
    if source is not None and not source.startswith(("https://", "http://")):
        raise SearchError("岗位链接必须以 https:// 或 http:// 开头")
    return {
        "jd": {
            "text": normalized,
            "source": source,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "provider": "manual",
            "title": title,
            "company": _optional_text(company),
            "location": _optional_text(location),
            "raw_content": text,
        },
        "source_status": "用户粘贴；来源、发布时间和岗位是否仍开放均未核验",
    }


def _board_token(value: str) -> str:
    if not BOARD_TOKEN.fullmatch(value):
        raise SearchError("招聘板标识只能包含字母、数字、下划线和连字符")
    return value


def parse_board_link(link: str) -> tuple[str, str]:
    """Name the public board behind a pasted careers link as (provider, board token)."""
    value = link.strip()
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    provider = BOARD_HOSTS.get((parts.hostname or "").casefold()) if parts.scheme in ("http", "https") else None
    segments = [segment for segment in parts.path.split("/") if segment]
    board = None
    if provider == "greenhouse" and segments[:2] == ["embed", "job_board"]:
        board = parse_qs(parts.query).get("for", [None])[0]
    elif provider and segments:
        board = segments[0]
    if board is None or not BOARD_TOKEN.fullmatch(board):
        raise SearchError(f"无法识别招聘板。{BOARD_LINK_HINT}")
    return provider, board


def _provider(value: str) -> str:
    if value not in PROVIDERS:
        raise SearchError(f"招聘板类型必须是：{', '.join(PROVIDERS)}")
    return value


def check_board(provider: str, board: str) -> tuple[str, str]:
    """Validate a (provider, board token) pair before it is stored or put into a URL."""
    return _provider(provider), _board_token(board)


def _job_id(value: str, provider: str = "greenhouse") -> str:
    if provider == "greenhouse":
        if not value.isascii() or not value.isdigit():
            raise SearchError("Greenhouse 岗位 ID 必须是数字")
    elif not POSTING_ID.fullmatch(value):
        raise SearchError("岗位 ID 只能包含字母、数字和连字符")
    return value


def _fetch_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "job-fit-workbench/0.1"})
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except HTTPError as exc:
            exc.close()
            if exc.code in RETRY_HTTP_CODES and attempt < FETCH_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            hint = "；请检查招聘板标识或岗位 ID" if exc.code == 404 else ""
            raise SearchError(f"岗位接口返回 HTTP {exc.code}{hint}") from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt < FETCH_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            reason = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(reason, TimeoutError):
                raise SearchError(f"岗位接口读取超时（已尝试 {FETCH_ATTEMPTS} 次）") from exc
            raise SearchError(f"无法连接岗位接口：{reason}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SearchError("岗位接口响应过大")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SearchError("岗位接口返回无效 JSON") from exc


def _text_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _join_locations(values: list[Any]) -> str | None:
    names = [name for name in (_text_or_none(value) for value in values) if name]
    return "; ".join(dict.fromkeys(names)) or None


def _epoch_ms(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _iso_utc(value: Any) -> str | None:
    """A posting date as UTC text, so dates from boards in different time zones sort correctly."""
    try:
        moment = datetime.fromisoformat(_text_or_none(value) or "")
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _base_posting(
    provider: str, board: str, captured_at: str, job_id: Any, title: Any, source: Any
) -> dict[str, Any]:
    """Fields every provider must supply; a posting without them is refused, not guessed."""
    if not isinstance(job_id, (int, str)) or isinstance(job_id, bool):
        raise SearchError("岗位记录缺少有效 ID")
    job_id = _job_id(str(job_id), provider)
    if not isinstance(title, str) or not title.strip():
        raise SearchError(f"岗位 {job_id} 缺少标题")
    if not isinstance(source, str) or not source.startswith("https://"):
        raise SearchError(f"岗位 {job_id} 缺少 HTTPS 官方链接")
    return {
        "provider": provider,
        "board": board,
        "job_id": job_id,
        "title": title,
        "source": source,
        "captured_at": captured_at,
    }


def _posting(raw: Any, board: str, captured_at: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SearchError("岗位接口包含非对象记录")
    job = _base_posting("greenhouse", board, captured_at, raw.get("id"), raw.get("title"), raw.get("absolute_url"))
    content = raw.get("content")
    if not isinstance(content, str):
        raise SearchError(f"岗位 {job['job_id']} 缺少描述")
    location = raw.get("location")
    return {
        **job,
        "company": _text_or_none(raw.get("company_name")),
        "location": _text_or_none(location.get("name") if isinstance(location, dict) else None),
        "posted_at": _iso_utc(raw.get("first_published")),
        "raw_content": content,
        "text": plain_text(content),
    }


def _lever_posting(raw: Any, board: str, captured_at: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SearchError("岗位接口包含非对象记录")
    job = _base_posting("lever", board, captured_at, raw.get("id"), raw.get("text"), raw.get("hostedUrl"))
    categories = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
    locations = categories.get("allLocations")
    if not isinstance(locations, list) or not locations:
        locations = [categories.get("location")]
    # Lever keeps requirements in separate headed lists, outside the description.
    parts = [_html_text(raw.get("description"))]
    for item in raw.get("lists") if isinstance(raw.get("lists"), list) else []:
        if isinstance(item, dict):
            parts += [_text_or_none(item.get("text")) or "", _html_text(item.get("content"))]
    parts.append(_html_text(raw.get("additional")))
    return {
        **job,
        "company": None,
        "location": _join_locations(locations),
        "posted_at": _epoch_ms(raw.get("createdAt")),
        "raw_content": json.dumps(raw, ensure_ascii=False),
        "text": _normalize_lines("\n".join(parts)),
    }


def _ashby_posting(raw: Any, board: str, captured_at: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SearchError("岗位接口包含非对象记录")
    job = _base_posting("ashby", board, captured_at, raw.get("id"), raw.get("title"), raw.get("jobUrl"))
    secondary = raw.get("secondaryLocations") if isinstance(raw.get("secondaryLocations"), list) else []
    locations = [raw.get("location")] + [item.get("location") for item in secondary if isinstance(item, dict)]
    if raw.get("isRemote") is True and not any("remote" in (_text_or_none(name) or "").casefold() for name in locations):
        locations.append("Remote")
    html = raw.get("descriptionHtml")
    plain = raw.get("descriptionPlain")
    return {
        **job,
        "company": None,
        "location": _join_locations(locations),
        "posted_at": _iso_utc(raw.get("publishedAt")),
        "raw_content": json.dumps(raw, ensure_ascii=False),
        "text": _html_text(html) if isinstance(html, str) else _normalize_lines(plain if isinstance(plain, str) else ""),
    }


POSTING_PARSERS = {"greenhouse": _posting, "lever": _lever_posting, "ashby": _ashby_posting}


def normalize_board(
    data: Any, board: str, captured_at: str, provider: str = "greenhouse"
) -> list[dict[str, Any]]:
    """Normalize and deduplicate one public board response, or fail explicitly."""
    if provider == "lever":
        if not isinstance(data, list):
            raise SearchError("岗位接口缺少岗位数组")
        raws = data
    else:
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SearchError("岗位接口缺少 jobs 数组")
        raws = data["jobs"]
    seen: set[str] = set()
    jobs: list[dict[str, Any]] = []
    for raw in raws:
        if provider == "ashby" and isinstance(raw, dict) and raw.get("isListed") is False:
            continue
        job = POSTING_PARSERS[provider](raw, board, captured_at)
        if job["job_id"] not in seen:
            seen.add(job["job_id"])
            jobs.append(job)
    return jobs


def fetch_board(board: str, provider: str = "greenhouse") -> list[dict[str, Any]]:
    """Read current published listings from one public Greenhouse, Lever or Ashby board."""
    provider = _provider(provider)
    board = _board_token(board)
    data = _fetch_json(BOARD_URLS[provider].format(board=board))
    return normalize_board(data, board, datetime.now(timezone.utc).isoformat(), provider)


def fetch_selected(board: str, job_id: str, provider: str = "greenhouse") -> dict[str, Any]:
    """Re-fetch a chosen posting instead of trusting an earlier search result."""
    provider = _provider(provider)
    board = _board_token(board)
    job_id = _job_id(job_id, provider)
    captured_at = datetime.now(timezone.utc).isoformat()
    if provider == "greenhouse":
        job = _posting(_fetch_json(f"{API_ROOT}/{board}/jobs/{job_id}"), board, captured_at)
    elif provider == "lever":
        job = _lever_posting(_fetch_json(f"{LEVER_ROOT}/{board}/{job_id}"), board, captured_at)
    else:
        # Ashby's public API only lists whole boards, so the whole board is read again.
        listed = [job for job in fetch_board(board, provider) if job["job_id"] == job_id]
        if not listed:
            raise SearchError("该岗位已不在公司的公开招聘板上")
        job = listed[0]
    if job["job_id"] != job_id:
        raise SearchError("岗位接口返回了不同的岗位 ID")
    return {
        "jd": {key: job[key] for key in (
            "text", "source", "captured_at", "provider", "board", "job_id",
            "title", "company", "location", "posted_at", "raw_content",
        )},
        "source_status": "本次公开接口返回；投递前仍需核对官方页面",
    }


def load_profile_data(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read facts and provisional fact-to-term links from one local profile."""
    data = json.loads(path.read_text(encoding="utf-8"))
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, list):
        raise SearchError("搜索资料必须包含 facts 数组")
    normalized_facts: list[dict[str, Any]] = []
    terms: list[dict[str, Any]] = []
    fact_ids: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("id"), str):
            raise SearchError("事实缺少 ID")
        fact_id = fact["id"]
        if not fact_id.strip():
            raise SearchError("事实缺少 ID")
        if fact_id in fact_ids:
            raise SearchError(f"重复的事实 ID：{fact_id}")
        fact_ids.add(fact_id)
        text = fact.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SearchError("事实缺少原文")
        confirmed = fact.get("confirmed")
        if not isinstance(confirmed, bool):
            raise SearchError(f"事实 {fact_id} 的 confirmed 必须是布尔值")
        normalized_facts.append({"id": fact_id, "text": text, "confirmed": confirmed})
        keywords = fact.get("terms")
        if not isinstance(keywords, list):
            raise SearchError(f"事实 {fact_id} 缺少 terms 数组")
        for term in keywords:
            if not isinstance(term, str) or not term.strip() or term.casefold() not in text.casefold():
                raise SearchError(f"事实 {fact_id} 的词面线索必须出现在事实原文中")
            key = (fact_id, term.casefold())
            if confirmed and key not in seen:
                seen.add(key)
                terms.append({"term": term, "fact_id": fact_id, "fact_quote": text})
    return normalized_facts, terms


def load_profile(path: Path) -> list[dict[str, Any]]:
    """Read provisional fact-to-term links; this is not an approval authority."""
    return load_profile_data(path)[1]


def prepare_review_input(selected: dict[str, Any]) -> dict[str, Any]:
    """Create a JD-only snapshot; fact retrieval belongs to the matching stage."""
    jd = selected.get("jd") if isinstance(selected, dict) else None
    if not isinstance(jd, dict):
        raise SearchError("选岗结果缺少 JD 快照")
    return {
        "jd": jd,
        "facts": [],
        "selected_requirements": [],
    }


def write_new_json(path: Path, data: dict[str, Any]) -> None:
    """Create a new review input without overwriting a user's edited version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")


def rank_jobs(
    jobs: list[dict[str, Any]],
    terms: list[dict[str, Any]],
    title_filter: str = "",
    location_filter: str = "",
) -> list[dict[str, Any]]:
    """Rank literal evidence candidates, without claiming qualification or probability."""
    patterns = [(item, tag_pattern(item["term"])) for item in terms]
    results: list[dict[str, Any]] = []
    for job in jobs:
        if title_filter.casefold() not in job["title"].casefold():
            continue
        location = job["location"]
        if location_filter and location is not None and location_filter.casefold() not in location.casefold():
            continue
        haystack = job["title"] + "\n" + job["text"]
        evidence = []
        for item, pattern in patterns:
            match = pattern.search(haystack)
            if match:
                start = max(0, match.start() - 60)
                end = min(len(haystack), match.end() + 60)
                item_evidence = {
                    "term": item["term"],
                    "jd_quote": haystack[start:end].strip(),
                    "fact_id": item["fact_id"],
                    "status": "词面候选依据，语义待审核",
                }
                if "fact_version" in item:
                    item_evidence["fact_version"] = item["fact_version"]
                if "fact_quote" in item:
                    item_evidence["fact_quote"] = item["fact_quote"]
                evidence.append(item_evidence)
        shown_evidence = evidence[:MAX_EVIDENCE_PER_JOB]
        results.append({
            "board": job["board"],
            "job_id": job["job_id"],
            "title": job["title"],
            "location": location if location is not None else "未知",
            "location_filter_status": "未知" if location_filter and location is None else "已检查",
            "source": job["source"],
            "captured_at": job["captured_at"],
            "matched_term_count": len({item["term"].casefold() for item in evidence}),
            "candidate_evidence_count": len(evidence),
            "candidate_evidence": shown_evidence,
            "candidate_evidence_truncated": len(evidence) > len(shown_evidence),
            "eligibility_status": "未核验",
        })
    results.sort(key=lambda item: (
        -item["matched_term_count"],
        -item["candidate_evidence_count"],
        item["location_filter_status"] == "未知",
        item["title"].casefold(),
        item["job_id"],
    ))
    return results


def attach_fact_quotes(
    jobs: list[dict[str, Any]],
    quotes: dict[tuple[str, int], str],
) -> None:
    """Attach exact local fact text after ranking, only for evidence being displayed."""
    for job in jobs:
        for evidence in job["candidate_evidence"]:
            fact_id = evidence["fact_id"]
            fact_version = evidence.get("fact_version")
            key = (fact_id, fact_version)
            if key not in quotes:
                raise SearchError(f"排序后的事实已不是当前已确认版本：{fact_id}")
            evidence["fact_quote"] = quotes[key]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按需搜索一个 Greenhouse、Lever 或 Ashby 招聘板")
    actions = parser.add_subparsers(dest="action", required=True)
    search = actions.add_parser("search", help="列出并排序公开岗位")
    search.add_argument("--board", required=True, help="招聘板标识，例如 stripe")
    search.add_argument("--provider", choices=PROVIDERS, default="greenhouse")
    search.add_argument("--profile", type=Path, help="本地事实词面线索 JSON；不会上传")
    search.add_argument("--facts-db", type=Path, help="使用事实库当前已确认版本的标签排序")
    search.add_argument("--title", default="", help="标题包含的文字")
    search.add_argument("--location", default="", help="地点包含的文字；地点未知会保留")
    search.add_argument("--limit", type=int, default=20, help="最多显示的岗位数")
    select = actions.add_parser("select", help="重新读取选中岗位的 JD")
    select.add_argument("--board", required=True)
    select.add_argument("--provider", choices=PROVIDERS, default="greenhouse")
    select.add_argument("--job-id", required=True)
    select.add_argument("--output", type=Path, help="新建可由 review.py 读取的 JSON；不覆盖已有文件")
    paste = actions.add_parser("paste", help="从文本文件或标准输入导入任意来源的 JD")
    paste.add_argument("--file", type=Path, required=True, help="JD 纯文本文件；- 表示标准输入")
    paste.add_argument("--title", required=True, help="岗位标题")
    paste.add_argument("--company", help="公司名称")
    paste.add_argument("--url", help="官方岗位链接；不提供时来源记为未知")
    paste.add_argument("--location", help="工作地点")
    paste.add_argument("--output", type=Path, required=True, help="新建审核输入 JSON；不覆盖已有文件")
    args = parser.parse_args(argv)
    try:
        if args.action == "search":
            if args.limit < 1 or args.limit > MAX_SEARCH_RESULTS:
                raise SearchError(f"--limit 必须在 1 到 {MAX_SEARCH_RESULTS} 之间")
            if args.profile and args.facts_db:
                raise SearchError("search 的 --profile 与 --facts-db 不能同时使用")
            if args.facts_db:
                terms = load_search_terms(args.facts_db)
                fact_confirmation_status = "来自事实库的当前已确认版本标签"
            elif args.profile:
                terms = load_profile(args.profile)
                fact_confirmation_status = "来自输入文件，未独立核验"
            else:
                terms = []
                fact_confirmation_status = "未提供事实检索来源"
            jobs = fetch_board(args.board, args.provider)
            ranked = rank_jobs(jobs, terms, args.title, args.location)
            displayed = ranked[:args.limit]
            if args.facts_db:
                displayed_fact_refs = {
                    (evidence["fact_id"], evidence["fact_version"])
                    for job in displayed
                    for evidence in job["candidate_evidence"]
                }
                attach_fact_quotes(
                    displayed,
                    load_confirmed_fact_texts(args.facts_db, displayed_fact_refs),
                )
            result = {
                "board": args.board,
                "retrieved_at": jobs[0]["captured_at"] if jobs else datetime.now(timezone.utc).isoformat(),
                "total_after_filter": len(ranked),
                "ranking_basis": "命中的不同标签数量（同一标签多条事实只算一次）；不是资格结论或录取概率",
                "fact_confirmation_status": fact_confirmation_status,
                "jobs": displayed,
            }
        elif args.action == "paste":
            if str(args.file) == "-":
                text = sys.stdin.read()
            else:
                text = args.file.read_text(encoding="utf-8")
            selected = prepare_pasted_jd(
                text, args.title, args.company, args.url, args.location
            )
            write_new_json(args.output, prepare_review_input(selected))
            jd = selected["jd"]
            result = {
                "saved_to": str(args.output),
                "title": jd["title"],
                "company": jd["company"] if jd["company"] is not None else "未知",
                "source": jd["source"] if jd["source"] is not None else "未知",
                "line_count": len(jd["text"].splitlines()),
                "source_status": selected["source_status"],
                "next_step": "运行 requirement_flow.py propose 提取待确认的 JD 要求",
            }
        else:
            result = fetch_selected(args.board, args.job_id, args.provider)
            if args.output:
                review_input = prepare_review_input(result)
                write_new_json(args.output, review_input)
                result = {
                    "saved_to": str(args.output),
                    "job_id": args.job_id,
                    "fact_count": 0,
                    "fact_confirmation_status": "事实将在要求确认后的匹配阶段按需检索",
                    "selected_requirement_count": 0,
                    "next_step": "运行 requirement_flow.py propose 提取待确认的 JD 要求",
                }
    except (OSError, FactStoreError, SearchError, ValueError, json.JSONDecodeError) as exc:
        print(f"搜索失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
