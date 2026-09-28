"""Assemble a CV draft from a local profile and confirmed facts, and render it."""

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Callable, Iterable

from claims import check_rewrite
from cv_layout import shown_sections
from deepseek_client import DEFAULT_EFFORT, DEFAULT_MODEL, EFFORTS, DeepSeekError, chat_json
from facts import DEFAULT_DATABASE, FactStoreError, load_current_facts
from privacy import mask, private_terms, stand_ins
from review import build_report


PROFILE_VERSION = 1
DRAFT_VERSION = 1
APPROVAL_VERSION = 1
LANGUAGES = ("en", "zh")
PAPERS = ("letter", "a4")
DEFAULT_PAPER = {"en": "letter", "zh": "a4"}
SECTION_TITLES = {
    "education": {"en": "Education", "zh": "教育背景"},
    "experience": {"en": "Experience", "zh": "实习经历"},
    "projects": {"en": "Projects", "zh": "项目经历"},
    "skills": {"en": "Technical Skills", "zh": "专业技能"},
    "publications": {"en": "Selected Publications", "zh": "论文发表"},
}
PROFILE_KEYS = {"profile_version", "name", "contact", "sections"}
CONTACT_KEYS = {"location", "phone", "email", "links"}
SECTION_KEYS = {"kind", "title", "entries"}
ENTRY_KEYS = {"title", "title_link", "location", "subtitle", "dates", "facts", "links"}
PAGE_SIZES = {"letter": "Letter", "a4": "A4"}
WATERMARK = {"en": "DRAFT", "zh": "草稿"}
BULLET_KINDS = {"experience", "projects"}
LABEL_KINDS = {"education", "skills"}
STYLE = """
* { box-sizing: border-box; }
body { margin: 0; color: #111; font-size: 10.5pt; line-height: 1.2;
  font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans", "PingFang SC", "Hiragino Sans GB", "Heiti SC",
    "Noto Sans CJK SC", sans-serif; }
body.zh { line-height: 1.32; }
a { color: inherit; text-decoration: underline; text-decoration-thickness: 0.5pt; text-underline-offset: 1.5pt; }
header { text-align: center; margin-bottom: 4pt; }
h1 { font-size: 21pt; margin: 0 0 2pt; letter-spacing: 0.5pt; }
body.en h1 { text-transform: uppercase; }
.contact { font-size: 10pt; margin: 1pt 0; }
h2 { font-size: 11pt; color: #1f3864; margin: 7pt 0 3pt; padding-bottom: 1pt;
  border-bottom: 0.75pt solid #1f3864; letter-spacing: 0.3pt; break-after: avoid; }
body.en h2 { text-transform: uppercase; }
.entry { margin: 0 0 3pt; break-inside: avoid; }
.row { display: flex; justify-content: space-between; gap: 12pt; }
.row .right { white-space: nowrap; text-align: right; }
.strong { font-weight: bold; }
em, .italic { font-style: italic; }
body.zh em, body.zh .italic { font-style: normal; }
ul { margin: 1pt 0 0; padding-left: 14pt; }
li, .line { margin: 0 0 0.5pt; }
.watermark { position: fixed; top: 38%; left: 0; right: 0; z-index: 10; text-align: center;
  font-size: 110pt; font-weight: bold; color: rgba(190, 30, 30, 0.12); transform: rotate(-28deg); }
"""


class CVError(Exception):
    """The profile, draft, or export request cannot be used safely.

    ``reason`` names the problem when the page can suggest a fix (facts_not_confirmed,
    facts_missing, no_profile); otherwise it is "not_usable".
    """

    def __init__(self, message: str, reason: str = "not_usable") -> None:
        super().__init__(message)
        self.reason = reason


def _check_keys(value: Any, allowed: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CVError(f"{path} 必须是对象")
    unknown = set(value) - allowed
    if unknown:
        raise CVError(f"{path} 包含未知字段：{', '.join(sorted(unknown))}")
    return value


def _localized(value: Any, language: str, path: str, fallbacks: list[str]) -> str | None:
    """Pick the requested language; fall back to the other one and record where."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            raise CVError(f"{path} 不能为空字符串")
        return value.strip()
    if not isinstance(value, dict) or not value or set(value) - set(LANGUAGES):
        raise CVError(f'{path} 必须是字符串或 {{"en": ..., "zh": ...}}')
    options = {}
    for key, text in value.items():
        if not isinstance(text, str):
            raise CVError(f"{path}.{key} 必须是字符串")
        if text.strip():
            options[key] = text.strip()
    if language in options:
        return options[language]
    for key in LANGUAGES:
        if key in options:
            fallbacks.append(path)
            return options[key]
    raise CVError(f"{path} 至少需要一种语言的文字")


def profile_languages(profile: Any) -> list[str]:
    """The CV languages a profile is written in, judged by the name: a resume given in one
    language gets CVs in that language only, instead of machine-filled gaps in another."""
    name = profile.get("name") if isinstance(profile, dict) else None
    if isinstance(name, str):
        return ["zh" if any("\u4e00" <= character <= "\u9fff" for character in name) else "en"]
    if isinstance(name, dict):
        written = [language for language in LANGUAGES if isinstance(name.get(language), str) and name[language].strip()]
        if written:
            return written
    return ["en"]


def _url(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        raise CVError(f"{path} 必须以 https:// 或 http:// 开头")
    return value


def _plain(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CVError(f"{path} 必须是非空字符串")
    return value.strip()


def _link(value: Any, path: str) -> dict[str, str] | None:
    if value is None:
        return None
    _check_keys(value, {"label", "url"}, path)
    label = _plain(value.get("label"), f"{path}.label")
    if label is None:
        raise CVError(f"{path}.label 必须是非空字符串")
    return {"text": label, "url": _url(value.get("url"), f"{path}.url")}


def _phrase_links(value: Any, path: str) -> dict[str, str]:
    """Links for exact phrases inside a line, such as a project or paper title."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CVError(f"{path} 必须是 {{短语: 链接}} 对象")
    links = {}
    for phrase, url in value.items():
        if not phrase.strip():
            raise CVError(f"{path} 的短语不能为空")
        links[phrase] = _url(url, f"{path}.{phrase}")
    return links


def _header(profile: dict[str, Any], language: str, fallbacks: list[str]) -> dict[str, Any]:
    name = _localized(profile.get("name"), language, "name", fallbacks)
    if name is None:
        raise CVError("profile 缺少 name")
    contact = _check_keys(profile.get("contact", {}), CONTACT_KEYS, "contact")
    details = []
    location = _localized(contact.get("location"), language, "contact.location", fallbacks)
    if location:
        details.append({"text": location, "url": None})
    phone = _plain(contact.get("phone"), "contact.phone")
    if phone:
        details.append({"text": phone, "url": None})
    email = _plain(contact.get("email"), "contact.email")
    if email:
        if "@" not in email or any(character.isspace() for character in email):
            raise CVError("contact.email 格式无效")
        details.append({"text": email, "url": f"mailto:{email}"})
    links = contact.get("links", [])
    if not isinstance(links, list):
        raise CVError("contact.links 必须是数组")
    link_items = [
        _link(link, f"contact.links[{index}]") for index, link in enumerate(links)
    ]
    return {"name": name, "details": details, "links": link_items}


def _fact_ids(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CVError(f"{path} 必须是事实 ID 数组")
    return value


def _profile_hash(profile: dict[str, Any]) -> str:
    canonical = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _job_summary(job: Any) -> dict[str, Any] | None:
    if job is None:
        return None
    try:
        build_report(job)
    except ValueError as exc:
        raise CVError(f"岗位文件无效：{exc}") from exc
    jd = job["jd"]
    return {
        "title": jd.get("title"),
        "company": jd.get("company") or jd.get("board"),
        "source": jd.get("source"),
        "captured_at": jd["captured_at"],
        "matched_fact_ids": list(dict.fromkeys(
            item["fact_id"] for item in job["selected_requirements"] if item.get("fact_id")
        )),
    }


def requirement_briefs(job: dict[str, Any]) -> list[dict[str, str]]:
    """Each counted requirement as a model sees it: its words and whether it is required,
    preferred or unclear. Requirements saved before strengths were kept count as unclear."""
    return [{"text": item["text"], "strength": item.get("strength") or "unclear"} for item in job["selected_requirements"]]


def build_draft(
    profile: Any,
    facts_db: Path,
    language: str = "en",
    paper: str | None = None,
    job: Any = None,
) -> dict[str, Any]:
    """Resolve the profile for one language and quote the current confirmed facts it lists.

    With a matching.py decide output as ``job``, facts linked to that job's
    requirements are listed first inside their entry; nothing else changes.
    """
    job_summary = _job_summary(job)
    matched = set(job_summary["matched_fact_ids"]) if job_summary else set()
    if language not in LANGUAGES:
        raise CVError(f"语言必须是：{', '.join(LANGUAGES)}")
    paper = paper or DEFAULT_PAPER[language]
    if paper not in PAPERS:
        raise CVError(f"纸张必须是：{', '.join(PAPERS)}")
    _check_keys(profile, PROFILE_KEYS, "profile")
    if profile.get("profile_version") != PROFILE_VERSION:
        raise CVError(f"profile_version 必须是 {PROFILE_VERSION}")
    raw_sections = profile.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise CVError("profile.sections 必须是非空数组")
    fallbacks: list[str] = []
    header = _header(profile, language, fallbacks)

    # Pass 1 validates the whole profile, so layout mistakes surface before fact checks.
    referenced: list[str] = []
    sections = []
    for index, section in enumerate(raw_sections):
        _check_keys(section, SECTION_KEYS, f"sections[{index}]")
        kind = section.get("kind")
        if kind not in SECTION_TITLES:
            raise CVError(f"sections[{index}].kind 必须是：{', '.join(SECTION_TITLES)}")
        raw_entries = section.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise CVError(f"sections[{index}].entries 必须是非空数组")
        title = _localized(section.get("title"), language, f"sections[{index}].title", fallbacks)
        entries = []
        for entry_index, entry in enumerate(raw_entries):
            path = f"sections[{index}].entries[{entry_index}]"
            _check_keys(entry, ENTRY_KEYS, path)
            fact_ids = _fact_ids(entry.get("facts"), f"{path}.facts")
            for fact_id in fact_ids:
                if fact_id in referenced:
                    raise CVError(f"事实被重复引用：{fact_id}")
                referenced.append(fact_id)
            entries.append({
                "title": _localized(entry.get("title"), language, f"{path}.title", fallbacks),
                "title_link": _link(entry.get("title_link"), f"{path}.title_link"),
                "location": _localized(entry.get("location"), language, f"{path}.location", fallbacks),
                "subtitle": _localized(entry.get("subtitle"), language, f"{path}.subtitle", fallbacks),
                "dates": _localized(entry.get("dates"), language, f"{path}.dates", fallbacks),
                "lines": fact_ids,
                "links": _phrase_links(entry.get("links"), f"{path}.links"),
            })
        sections.append({
            "kind": kind,
            "title": title or SECTION_TITLES[kind][language],
            "entries": entries,
        })

    facts = load_current_facts(facts_db, referenced)
    missing = [fact_id for fact_id in referenced if fact_id not in facts]
    if missing:
        raise CVError(f"简历引用的事实不存在：{', '.join(missing)}", reason="facts_missing")
    pending = [
        f"{fact_id}@{facts[fact_id]['version']}"
        for fact_id in referenced if facts[fact_id]["status"] != "confirmed"
    ]
    if pending:
        raise CVError(
            "简历只能使用已确认的事实；以下事实的当前版本尚未确认，逐条核对后运行："
            "python3 facts.py confirm " + " ".join(pending),
            reason="facts_not_confirmed",
        )

    # Pass 2 quotes each confirmed fact word for word; job-matched facts go first.
    for section in sections:
        for entry in section["entries"]:
            entry["lines"] = sorted(
                (
                    {
                        "fact_id": fact_id,
                        "fact_version": facts[fact_id]["version"],
                        "text": facts[fact_id]["text"],
                    }
                    for fact_id in entry["lines"]
                ),
                key=lambda line: line["fact_id"] not in matched,
            )
    return {
        "cv_draft_version": DRAFT_VERSION,
        "status": "draft",
        "language": language,
        "paper": paper,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile_sha256": _profile_hash(profile),
        "job": job_summary,
        "header": header,
        "sections": sections,
        "facts": [{"id": fact_id, "version": facts[fact_id]["version"]} for fact_id in referenced],
        "language_fallbacks": fallbacks,
    }


def _linked(text: str, links: dict[str, str]) -> str:
    """Escape text and link each listed phrase, preferring longer phrases on overlap."""
    spans: list[tuple[int, int, str]] = []
    for phrase in sorted(links, key=len, reverse=True):
        start = text.find(phrase)
        while start != -1:
            end = start + len(phrase)
            if all(end <= other_start or start >= other_end for other_start, other_end, _ in spans):
                spans.append((start, end, links[phrase]))
            start = text.find(phrase, end)
    parts, position = [], 0
    for start, end, url in sorted(spans):
        parts.append(escape(text[position:start]))
        parts.append(f'<a href="{escape(url, quote=True)}">{escape(text[start:end])}</a>')
        position = end
    parts.append(escape(text[position:]))
    return "".join(parts)


def _labelled(text: str, links: dict[str, str]) -> str:
    """Bold a short leading label such as "Languages:" in skill and coursework lines."""
    for separator in (": ", "："):
        label, found, rest = text.partition(separator)
        if found and 0 < len(label) <= 40:
            space = " " if found == ": " else ""
            return f"<strong>{escape(label + found.strip())}</strong>{space}{_linked(rest, links)}"
    return _linked(text, links)


def _items(items: list[dict[str, Any]]) -> str:
    return " | ".join(
        f'<a href="{escape(item["url"], quote=True)}">{escape(item["text"])}</a>'
        if item["url"] else escape(item["text"])
        for item in items
    )


def _entry_html(kind: str, entry: dict[str, Any]) -> str:
    rows = []
    if entry["title"] or entry["location"]:
        left = f"<strong>{escape(entry['title'])}</strong>" if entry["title"] else ""
        if entry["title_link"]:
            link = entry["title_link"]
            left += f' | <a href="{escape(link["url"], quote=True)}">{escape(link["text"])}</a>'
        right = ""
        if entry["location"]:
            style = "italic" if kind == "projects" else "strong"
            right = f'<span class="right {style}">{escape(entry["location"])}</span>'
        rows.append(f'<div class="row"><span class="left">{left}</span>{right}</div>')
    if entry["subtitle"] or entry["dates"]:
        rows.append(
            f'<div class="row"><span class="left"><em>{escape(entry["subtitle"] or "")}</em></span>'
            f'<span class="right"><em>{escape(entry["dates"] or "")}</em></span></div>'
        )
    lines = entry["lines"]
    if lines and kind in BULLET_KINDS:
        rows.append("<ul>" + "".join(
            f"<li>{_linked(line['text'], entry['links'])}</li>" for line in lines
        ) + "</ul>")
    elif lines:
        render = _labelled if kind in LABEL_KINDS else _linked
        rows.extend(f'<div class="line">{render(line["text"], entry["links"])}</div>' for line in lines)
    return f'<div class="entry">{"".join(rows)}</div>'


def render_html(draft: dict[str, Any], final: bool = False) -> str:
    """Render a draft as self-contained HTML; anything not final carries a watermark."""
    language = draft["language"]
    header = draft["header"]
    sections = "".join(
        f'<section><h2>{escape(section["title"])}</h2>'
        + "".join(_entry_html(section["kind"], entry) for entry in section["entries"])
        + "</section>"
        for section in shown_sections(draft)
    )
    links = f'<div class="contact">{_items(header["links"])}</div>' if header["links"] else ""
    watermark = "" if final else f'<div class="watermark">{WATERMARK[language]}</div>'
    return f"""<!doctype html>
<html lang="{'zh-CN' if language == 'zh' else 'en'}">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{escape(header["name"])}</title>
<style>@page {{ size: {PAGE_SIZES[draft["paper"]]}; margin: 0.4in 0.6in; }}{STYLE}</style>
</head>
<body class="{language}">
{watermark}
<header><h1>{escape(header["name"])}</h1>
<div class="contact">{_items(header["details"])}</div>
{links}
</header>
{sections}
</body>
</html>
"""


CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
CHROME_TIMEOUT_SECONDS = 60


def _find_chrome() -> str | None:
    configured = os.environ.get("CHROME_PATH")
    if configured:
        return configured
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _pdf_complete(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 1024))
            return b"%%EOF" in handle.read()
    except FileNotFoundError:
        return False


def print_with_chrome(html_path: Path, pdf_path: Path) -> None:
    """Print local HTML to PDF with headless Chrome in a throwaway profile.

    Chrome may keep running after writing the file (observed inside sandboxes), so
    the file ending in %%EOF counts as done and Chrome is then stopped.
    """
    chrome = _find_chrome()
    if chrome is None:
        raise CVError("找不到 Chrome；请安装 Google Chrome 或设置 CHROME_PATH")
    with tempfile.TemporaryDirectory(prefix="cv-chrome-") as profile:
        process = subprocess.Popen(
            [
                chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", "--use-mock-keychain", "--disable-extensions",
                "--disable-background-networking", f"--user-data-dir={profile}",
                "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", Path(html_path).as_uri(),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + CHROME_TIMEOUT_SECONDS
            while not _pdf_complete(pdf_path):
                if process.poll() is not None and not _pdf_complete(pdf_path):
                    raise CVError(f"Chrome 没有生成 PDF（退出码 {process.returncode}）")
                if time.monotonic() > deadline:
                    raise CVError("Chrome 生成 PDF 超时")
                time.sleep(0.2)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def _line_supported(line: dict[str, Any], fact: dict[str, Any], vocabulary: list[str]) -> bool:
    """A plain line quotes its fact; an accepted rewrite still passes every claim check."""
    tailoring = line.get("tailoring")
    status = tailoring.get("status") if isinstance(tailoring, dict) else None
    if tailoring is None or status == "rejected":
        return line["text"] == fact["text"]
    if status == "accepted":
        return (
            line.get("source_text") == fact["text"]
            and not check_rewrite(line["text"], fact["text"], fact["tags"], vocabulary)
        )
    return False


def _verify_draft(draft: Any, facts_db: Path) -> dict[str, dict[str, Any]]:
    """Every line must still rest on the current confirmed version of its fact.

    Returns those current facts, keyed by ID, for callers that need their tags.
    """
    if not isinstance(draft, dict) or draft.get("cv_draft_version") != DRAFT_VERSION:
        raise CVError("草稿文件版本无效")
    if draft.get("language") not in LANGUAGES or draft.get("paper") not in PAPERS:
        raise CVError("草稿的语言或纸张无效")
    used = draft.get("facts")
    if not isinstance(used, list) or not all(
        isinstance(item, dict) and isinstance(item.get("id"), str)
        and isinstance(item.get("version"), int) for item in used
    ):
        raise CVError("草稿 facts 结构无效")
    versions = {item["id"]: item["version"] for item in used}
    current = load_current_facts(facts_db, versions)
    problems = [
        fact_id for fact_id, version in versions.items()
        if fact_id not in current or current[fact_id]["status"] != "confirmed"
        or current[fact_id]["version"] != version
    ]
    vocabulary = [tag for fact in current.values() for tag in fact["tags"]]
    try:
        for section in draft["sections"]:
            for entry in section["entries"]:
                for line in entry["lines"]:
                    fact = current.get(line["fact_id"])
                    if (fact is None or versions.get(line["fact_id"]) != line["fact_version"]
                            or not _line_supported(line, fact, vocabulary)):
                        problems.append(line["fact_id"])
    except (KeyError, TypeError, AttributeError) as exc:
        raise CVError("草稿文件结构无效") from exc
    if problems:
        raise CVError(
            "草稿与当前已确认事实不一致，请重新生成草稿：" + ", ".join(dict.fromkeys(problems))
        )
    return current


def _count_pages(data: bytes) -> int | None:
    count = len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", data))
    return count or None


def _content_hash(draft: dict[str, Any]) -> str:
    content = {key: value for key, value in draft.items() if key != "approval"}
    return _profile_hash(content)


def approve_draft(draft: Any, facts_db: Path) -> dict[str, Any]:
    """Stamp a verified draft with the fingerprint of exactly what the user reviewed."""
    if isinstance(draft, dict) and "approval" in draft:
        raise CVError("这个文件已经批准过；如内容需要修改，请重新生成草稿再批准")
    _verify_draft(draft, facts_db)
    approved = copy.deepcopy(draft)
    approved["approval"] = {
        "approval_version": APPROVAL_VERSION,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "content_sha256": _content_hash(draft),
    }
    return approved


def is_final_approval(draft: dict[str, Any]) -> bool:
    """An approval counts only while the content still matches its fingerprint."""
    approval = draft.get("approval")
    if approval is None:
        return False
    if (not isinstance(approval, dict) or approval.get("approval_version") != APPROVAL_VERSION
            or not isinstance(approval.get("content_sha256"), str)):
        raise CVError("批准记录结构无效")
    if approval["content_sha256"] != _content_hash(draft):
        raise CVError("批准后内容已改变；请重新生成草稿并重新批准")
    return True


def export_pdf(
    draft: Any,
    facts_db: Path,
    output: Path,
    html_output: Path | None = None,
    printer: Callable[[Path, Path], None] = print_with_chrome,
) -> dict[str, Any]:
    """Re-check a draft against the fact store, then print it to a new PDF.

    Only an approved draft whose content is unchanged prints without the watermark.
    """
    _verify_draft(draft, facts_db)
    final = is_final_approval(draft)
    output = Path(output)
    for path in (output, html_output):
        if path is not None and Path(path).exists():
            raise CVError(f"输出文件已存在，不会覆盖：{path}")
    html = render_html(draft, final=final)
    with tempfile.TemporaryDirectory(prefix="cv-export-") as directory:
        html_path = Path(directory) / "cv.html"
        pdf_path = Path(directory) / "cv.pdf"
        html_path.write_text(html, encoding="utf-8")
        printer(html_path, pdf_path)
        data = pdf_path.read_bytes() if pdf_path.exists() else b""
    if not data.startswith(b"%PDF"):
        raise CVError("没有生成有效的 PDF")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle:
        handle.write(data)
    if html_output is not None:
        with Path(html_output).open("x", encoding="utf-8") as handle:
            handle.write(html)
    return {
        "saved_to": str(output),
        "pages": _count_pages(data),
        "final": final,
        "watermark": None if final else WATERMARK[draft["language"]],
    }


TAILOR_KINDS = {"education", "experience", "projects", "skills"}
LANGUAGE_NAMES = {"en": "English", "zh": "Simplified Chinese (简体中文)"}
TAILOR_RULES = """You rewrite resume lines for one job application and reply in json only.
Rules:
1. Rewrite each input line into exactly one line in {language}. Use only what that line states:
   add no numbers, metrics, technologies, tools, team sizes, results or impact.
2. Keep the strength of every claim. "Developed", "implemented" or "participated" must never
   become "led", "owned", "managed", "spearheaded" or "responsible for"; in Chinese do not use
   主导、带领、领导、牵头、负责 or 统筹 unless the line itself says so. Keep every qualifier and
   negation ("prototype", "course project", "helped", "in progress", "internal", "not",
   "without") and add no scale or audience words such as "production", "customers" or "million".
3. Keep technology names, product names and numbers exactly as written; do not translate them.
4. You may reorder and reword so the parts relevant to the job requirements come first, using
   only content already in the line.
5. One sentence per line, no line breaks, about the same length or shorter.
6. For "Label: items" lines keep the items exactly; you may translate only the label.
Reply with this json shape, exactly one entry per input line, reusing each fact_id:
{{"lines": [{{"fact_id": "L1", "text": "rewritten line"}}]}}"""


def _rewrites(content: Any, expected: set[str]) -> dict[str, str]:
    lines = content.get("lines") if isinstance(content, dict) else None
    if not isinstance(lines, list):
        raise CVError("DeepSeek 返回的行与请求不一致：缺少 lines")
    rewrites: dict[str, str] = {}
    for item in lines:
        if (not isinstance(item, dict) or not isinstance(item.get("fact_id"), str)
                or not isinstance(item.get("text"), str) or item["fact_id"] in rewrites):
            raise CVError("DeepSeek 返回的行与请求不一致")
        rewrites[item["fact_id"]] = item["text"].strip()
    if set(rewrites) != expected:
        raise CVError("DeepSeek 返回的行与请求不一致")
    return rewrites


def tailor_draft(
    draft: Any,
    facts_db: Path,
    job: Any = None,
    chat: Callable[..., dict[str, Any]] = chat_json,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    private: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Rewrite bullet, skill and coursework lines for one job, keeping only checked rewrites.

    Only line text (under stand-in IDs), the job title and its confirmed requirements are sent:
    never the name, contact details, entry titles, publications or fact IDs. A line that itself holds any of them (such
    as a link or an employer's name) is not sent and stays as confirmed; ``private`` adds words
    to mask to the draft's own (its language only). A rewrite that fails
    check_rewrite is recorded with its reasons while the line keeps the confirmed fact word
    for word.
    """
    if isinstance(draft, dict) and "approval" in draft:
        raise CVError("已批准的文件不能再改写；请从 cv.py draft 生成的原始草稿开始")
    if isinstance(draft, dict) and "tailoring" in draft:
        raise CVError("草稿已经改写过；请从 cv.py draft 生成的原始草稿开始")
    current = _verify_draft(draft, facts_db)
    job_summary = _job_summary(job)
    private = sorted({*private_terms(draft), *(private or ())}, key=len, reverse=True)
    sendable = [
        (section["kind"], line) for section in draft["sections"] if section["kind"] in TAILOR_KINDS
        for entry in section["entries"] for line in entry["lines"] if mask(line["text"], private) == line["text"]
    ]
    if not sendable:
        raise CVError("草稿中没有可改写的行")
    out, back = stand_ins(line["fact_id"] for _, line in sendable)
    requested = [{"fact_id": out[line["fact_id"]], "section": kind, "text": line["text"]} for kind, line in sendable]
    language = LANGUAGE_NAMES[draft["language"]]
    request = {
        "target_language": language,
        "job_title": job_summary["title"] if job_summary else None,
        "job_requirements": requirement_briefs(job) if job_summary else [],
        "lines": requested,
    }
    messages = [
        {"role": "system", "content": TAILOR_RULES.format(language=language)},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]
    try:
        answer = chat(messages, model=model, effort=effort)
    except DeepSeekError as exc:
        raise CVError(f"DeepSeek 改写失败：{exc}") from exc
    rewrites = {back[alias]: text for alias, text in _rewrites(answer.get("content"), set(back)).items()}
    vocabulary = [tag for fact in current.values() for tag in fact["tags"]]
    result = copy.deepcopy(draft)
    counts = {"accepted": 0, "rejected": 0}
    for section in result["sections"]:
        if section["kind"] not in TAILOR_KINDS:
            continue
        for entry in section["entries"]:
            for line in entry["lines"]:
                if line["fact_id"] not in rewrites:
                    continue  # it holds private details, so it was not sent and stays as confirmed
                fact = current[line["fact_id"]]
                rewrite = rewrites[line["fact_id"]]
                reasons = check_rewrite(rewrite, fact["text"], fact["tags"], vocabulary)
                if reasons:
                    line["tailoring"] = {
                        "status": "rejected", "rejected_text": rewrite, "reasons": reasons,
                    }
                    counts["rejected"] += 1
                else:
                    line["source_text"] = line["text"]
                    line["text"] = rewrite
                    line["tailoring"] = {"status": "accepted"}
                    counts["accepted"] += 1
    result["tailoring"] = {
        "provider": "deepseek",
        "requested_model": model,
        "model": answer.get("model"),
        "effort": effort,
        "usage": answer.get("usage"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "job": job_summary,
        **counts,
    }
    return result


def _shown_lines(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """The stored lines (with any rewrite) of every line the CV shows, in CV order."""
    stored = {
        line["fact_id"]: line
        for section in draft["sections"] for entry in section["entries"] for line in entry["lines"]
    }
    return [
        stored[line["fact_id"]]
        for section in shown_sections(draft) for entry in section["entries"] for line in entry["lines"]
    ]


def rejected_lines(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """Rewrites the claim checks refused, with their reasons; those lines kept the fact."""
    return [
        {"fact_id": line["fact_id"], "rejected_text": line["tailoring"]["rejected_text"],
         "reasons": line["tailoring"]["reasons"]}
        for line in _shown_lines(draft)
        if line.get("tailoring", {}).get("status") == "rejected"
    ]


def rewritten_lines(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """Every shown line whose wording DeepSeek changed, as original → rewrite, for review.

    ``undone`` marks a rewrite the user turned back to the confirmed fact's own words.
    """
    undone = set((draft.get("plan") or {}).get("undone", []))
    return [
        {"fact_id": line["fact_id"], "from": line["source_text"], "to": line["text"],
         "undone": f"reword:{line['fact_id']}" in undone}
        for line in _shown_lines(draft)
        if line.get("tailoring", {}).get("status") == "accepted" and line["text"] != line["source_text"]
    ]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_new_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用本机简历资料和已确认事实生成简历草稿与 PDF")
    actions = parser.add_subparsers(dest="action", required=True)
    drafting = actions.add_parser("draft", help="生成简历草稿 JSON（只使用已确认事实）")
    drafting.add_argument("--profile", type=Path, required=True, help="本机 cv-profile.json")
    drafting.add_argument("--facts-db", type=Path, default=DEFAULT_DATABASE)
    drafting.add_argument("--job", type=Path, help="matching.py decide 的输出；相关事实排在前面")
    drafting.add_argument("--language", choices=LANGUAGES, default="en")
    drafting.add_argument("--paper", choices=PAPERS, help="默认英文 letter、中文 a4")
    drafting.add_argument("--output", type=Path, required=True, help="新建草稿 JSON；不覆盖已有文件")
    tailoring = actions.add_parser("tailor", help="用 DeepSeek 按岗位改写草稿；未通过检查的行保留原文")
    tailoring.add_argument("draft", type=Path, help="cv.py draft 生成的原始草稿")
    tailoring.add_argument("--job", type=Path, help="matching.py decide 的输出；不提供则只翻译和润色")
    tailoring.add_argument("--facts-db", type=Path, default=DEFAULT_DATABASE)
    tailoring.add_argument("--model", default=DEFAULT_MODEL)
    tailoring.add_argument("--effort", choices=EFFORTS, default=DEFAULT_EFFORT)
    tailoring.add_argument("--output", type=Path, required=True, help="新建改写后的草稿；不覆盖已有文件")
    approving = actions.add_parser("approve", help="逐行核对后批准草稿；只有批准且未改动的文件能导出无水印 PDF")
    approving.add_argument("draft", type=Path)
    approving.add_argument("--facts-db", type=Path, default=DEFAULT_DATABASE)
    approving.add_argument("--output", type=Path, required=True, help="新建已批准文件；不覆盖已有文件")
    printing = actions.add_parser("pdf", help="重新核对事实后生成 PDF；未批准的带草稿水印")
    printing.add_argument("draft", type=Path)
    printing.add_argument("--facts-db", type=Path, default=DEFAULT_DATABASE)
    printing.add_argument("--output", type=Path, required=True, help="新建 PDF；不覆盖已有文件")
    printing.add_argument("--html", type=Path, help="同时保存 HTML 预览；不覆盖已有文件")
    args = parser.parse_args(argv)
    try:
        if args.action == "draft":
            job = _read_json(args.job) if args.job else None
            draft = build_draft(_read_json(args.profile), args.facts_db, args.language, args.paper, job)
            _write_new_json(args.output, draft)
            summary = {
                "saved_to": str(args.output),
                "language": draft["language"],
                "paper": draft["paper"],
                "fact_count": len(draft["facts"]),
                "language_fallbacks": draft["language_fallbacks"],
                "next_step": "运行 cv.py pdf 生成带草稿水印的 PDF 并逐行检查",
            }
        elif args.action == "tailor":
            job = _read_json(args.job) if args.job else None
            tailored = tailor_draft(
                _read_json(args.draft), args.facts_db, job,
                chat=chat_json, model=args.model, effort=args.effort,
            )
            _write_new_json(args.output, tailored)
            summary = {
                "saved_to": str(args.output),
                "model": tailored["tailoring"]["model"],
                "usage": tailored["tailoring"]["usage"],
                "accepted": tailored["tailoring"]["accepted"],
                "rejected": tailored["tailoring"]["rejected"],
                "rejected_lines": rejected_lines(tailored),
                "rewritten_lines": rewritten_lines(tailored),
                "next_step": "运行 cv.py pdf 生成 PDF，并逐行核对改写后的内容",
            }
        elif args.action == "approve":
            approved = approve_draft(_read_json(args.draft), args.facts_db)
            _write_new_json(args.output, approved)
            summary = {
                "saved_to": str(args.output),
                "approved_at": approved["approval"]["approved_at"],
                "language": approved["language"],
                "job": (approved.get("tailoring") or {}).get("job") or approved.get("job"),
                "line_count": sum(
                    len(entry["lines"]) for section in approved["sections"] for entry in section["entries"]
                ),
                "rewritten_lines": rewritten_lines(approved),
                "next_step": "运行 cv.py pdf 生成无水印的最终 PDF；内容或事实变化后需重新批准",
            }
        else:
            summary = export_pdf(
                _read_json(args.draft), args.facts_db, args.output, args.html,
                printer=print_with_chrome,
            )
            if summary["pages"] and summary["pages"] > 1:
                summary["warning"] = f"简历共 {summary['pages']} 页；如需一页，请在 profile 中减少条目或事实"
    except (CVError, FactStoreError, OSError, ValueError) as exc:
        print(f"简历处理失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
