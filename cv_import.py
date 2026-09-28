"""Turn a CV PDF into pending facts and a CV profile, keeping contact details on this machine.

The PDF is read locally. The name line and the contact lines (email, phone, links) never leave
the machine: they only prefill a form the user checks. DeepSeek sees the other lines by number,
with the name, emails and web addresses masked, and answers which heading starts which section,
which part of which line is each entry's name, place, role and dates, and which lines make up
each fact. The program copies the text itself, so nothing can be invented, and every fact is
imported as pending for the user to read and confirm like any other.
"""

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from cv import PROFILE_VERSION, CVError, _header
from deepseek_client import DEFAULT_MODEL, DeepSeekError
from facts import list_facts, tag_pattern
from privacy import EMAIL, WEB, is_phone, mask, phones


class CVImportError(ValueError):
    """The PDF could not be read or turned into a CV."""


MAX_PDF_BYTES = 5_000_000
MAX_PAGES = 4
MAX_LINES = 300
MAX_FACT_LINES = 8
MAX_TAGS = 20
MAX_TAG_CHARACTERS = 60
HEADER_WINDOW = 8  # contact lines are looked for this far below the name
SECTION_FACT_TYPES = {
    "education": "education", "experience": "experience", "projects": "project",
    "skills": "skill", "publications": "achievement",
}
FIELDS = ("title", "location", "subtitle", "dates")
# Headings that end the name-and-contact block at the top of a resume.
HEADINGS = {
    "education", "experience", "work experience", "professional experience", "research experience",
    "employment", "projects", "personal projects", "academic projects", "skills", "technical skills",
    "publications", "selected publications", "summary", "profile", "objective", "awards", "honors",
    "certifications", "activities", "leadership", "教育背景", "教育经历", "实习经历", "工作经历",
    "项目经历", "专业技能", "技能", "个人技能", "论文发表", "发表论文", "科研经历", "获奖情况",
    "荣誉奖项", "自我评价", "个人简介",
}
DOCUMENT_TITLES = {"resume", "résumé", "curriculum vitae", "cv", "简历", "个人简历"}
SMALL_WORDS = {"a", "an", "and", "as", "at", "for", "in", "of", "on", "or", "the", "to", "&"}

COLUMN_GAP = re.compile(r"\s{3,}")
ICON = re.compile(r"[\ue000-\uf8ff]")  # icon-font glyphs such as a phone or envelope symbol
BULLET = re.compile(r"^(?:[•●◦▪‣∙·*]|[–—-](?=\s))\s*")
SEPARATOR = re.compile(r"\s*\|\s*|\s+[•·◦⋄♦]\s+")
LABEL = re.compile(r"^[^\W\d_][^:：/]{0,15}[:：]\s*(?!//)")

STRUCTURE_RULES = """You read the text lines of one resume and reply in json only with its structure.
Lines come numbered ("n"), each split into its left-to-right parts, numbered from 1. Refer to
lines and parts only by these numbers; never rewrite, merge or invent text. Masked words such as
[private], [email], [phone] or [link] are private and are never a field.
- Sections: give each section's heading line and its kind: "education", "experience" (jobs,
  internships, research positions), "projects", "skills" or "publications". Leave out sections
  of any other kind, such as awards or interests.
- Entries: in education, experience and projects, each school, job or project is one entry.
  Point at the parts holding its "title" (school, employer or project name), "location" (the
  place; for a project, the technologies listed beside its name), "subtitle" (degree or role)
  and "dates", each as [line, part]. Leave out what is not there. A part that only names a link,
  such as "GitHub" or "Demo", is not a field.
- Facts: every other line under an entry belongs to one fact: one bullet, or one plain line such
  as "Coursework: ...". When a bullet wraps onto the following lines, list all its lines in
  order. In skills and publications, use one entry with no fields, and each skills line or each
  publication is one fact.
- "tags": the skill words a fact names (programming languages, tools, frameworks, platforms,
  methods), copied exactly as written.
Reply as {"sections": [{"kind": "...", "heading": <n>, "entries": [{"title": [<n>, <part>],
"location": [<n>, <part>], "subtitle": [<n>, <part>], "dates": [<n>, <part>],
"facts": [{"lines": [<n>, ...], "tags": ["..."]}]}]}]}"""


def _clean(part: str) -> str:
    return " ".join(ICON.sub("", part).split()).strip(" |")


def _page_lines(text: str) -> list[list[str]]:
    lines = []
    for raw in text.splitlines():
        parts = [part for part in map(_clean, COLUMN_GAP.split(raw.strip())) if part]
        if len(parts) > 1 and len(parts[0]) == 1 and BULLET.match(parts[0] + " "):
            parts[:2] = [f"{parts[0]} {parts[1]}"]  # a bullet set apart from its text
        if parts:
            lines.append(parts)
    return lines


def _page_links(page: Any, page_number: int) -> list[dict[str, Any]]:
    """The page's web links, each with the text runs that start inside its box and the whole
    visual line it sits on (to tell apart two links with the same text)."""
    annotations = page.get("/Annots")
    if annotations is None:
        return []
    runs: list[tuple[float, float, str]] = []

    def visit(text: str, cm: list[float], tm: list[float], _font: Any, _size: Any) -> None:
        if text.strip():
            runs.append((tm[4] * cm[0] + tm[5] * cm[2] + cm[4], tm[4] * cm[1] + tm[5] * cm[3] + cm[5], text))

    page.extract_text(visitor_text=visit)
    links = []
    for annotation in annotations.get_object():
        annotation = annotation.get_object()
        action = annotation.get("/A")
        url = action.get_object().get("/URI") if action is not None and annotation.get("/Subtype") == "/Link" else None
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        rect = [float(value) for value in annotation["/Rect"]]
        left, right = sorted(rect[0::2])
        bottom, top = sorted(rect[1::2])
        inside = [(x, y, " ".join(text.split())) for x, y, text in runs if left - 2 <= x < right - 1 and bottom - 3 <= y <= top + 3]
        if any(piece for _, _, piece in inside):
            baseline = inside[0][1]
            context = " ".join(text for x, y, text in sorted(runs) if abs(y - baseline) <= 1.5)
            links.append({"url": str(url), "pieces": [piece for _, _, piece in inside if piece], "context": context,
                          "at": (page_number, -top, left)})
    return links


def read_pdf(data: bytes) -> dict[str, list[Any]]:
    """Each line as its left-to-right parts, and the web links with the text they cover.

    Layout mode keeps columns apart (a place or dates on the right) and reads letter-spaced
    words correctly where plain extraction gives "PyT orch".
    """
    from pypdf import PdfReader  # only uploads need pypdf (see requirements.txt)

    if len(data) > MAX_PDF_BYTES:
        raise CVImportError(f"PDF 不能超过 {MAX_PDF_BYTES // 1_000_000} MB")
    if not data.startswith(b"%PDF-"):
        raise CVImportError("这不是 PDF 文件")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise CVImportError("这个 PDF 有密码保护；请上传没有密码的版本")
        if len(reader.pages) > MAX_PAGES:
            raise CVImportError(f"简历最多 {MAX_PAGES} 页")
        lines: list[list[str]] = []
        links: list[dict[str, Any]] = []
        for number, page in enumerate(reader.pages):
            lines.extend(_page_lines(page.extract_text(extraction_mode="layout")))
            links.extend(_page_links(page, number))
    except CVImportError:
        raise
    except Exception as exc:  # pypdf raises many kinds of errors on damaged files
        raise CVImportError("无法读取这个 PDF") from exc
    if not lines:
        raise CVImportError("这个 PDF 里没有可读的文字（可能是扫描图片）")
    if len(lines) > MAX_LINES:
        raise CVImportError(f"简历最多 {MAX_LINES} 行")
    links.sort(key=lambda link: link["at"])
    return {"lines": lines, "links": [{key: link[key] for key in ("url", "pieces", "context")} for link in links]}


def _compact(text: str) -> str:
    return "".join(text.split())


def _plain(text: str) -> str:
    """Text without spacing, column bars or bullets, for comparing a PDF line read two ways."""
    return "".join(character for character in text if not character.isspace() and character not in "|•●◦▪")


def _same_line(line: str, context: str) -> bool:
    return bool(line) and (line in context or context in line)


def _find_phrase(text: str, label: str) -> str | None:
    """The exact words of text that read as label, ignoring spacing (PDFs space words unevenly)."""
    kept = [index for index, character in enumerate(text) if not character.isspace()]
    compact = "".join(text[index] for index in kept)
    target = _compact(label)
    start = compact.find(target) if target else -1
    while start >= 0:
        first, last = kept[start], kept[start + len(target) - 1]
        before = text[first - 1] if first else " "
        after = text[last + 1] if last + 1 < len(text) else " "
        if not (before.isalnum() and text[first].isalnum()) and not (after.isalnum() and text[last].isalnum()):
            return text[first:last + 1]
        start = compact.find(target, start + 1)
    return None


class _Links:
    """The PDF's web links, each tied to the line its text is on and handed out once."""

    def __init__(self, links: Iterable[dict[str, Any]], lines: list[list[str]]):
        self.items = []
        for link in links:
            pieces = link["pieces"]
            # A box can also cover the start of the next words, so shorter readings are tried too.
            labels = [" ".join(pieces[:count]) for count in range(len(pieces), 0, -1)]
            self.items.append({"url": link["url"], "labels": labels, "used": False, "line": None,
                               "context": _plain(link.get("context") or "")})
        # Links and lines both run in reading order, so each link sits on the first line at or
        # after the previous link's line that shows its text, has not used up that text on other
        # links and, when the PDF tells, reads like the link's own visual line. Two links reading
        # "GitHub" then stay with their own project, past a plain "GitHub Actions" in between.
        start, placed = 0, {}
        for item in self.items:
            for index in range(start, len(lines)):
                if item["context"] and not _same_line(_plain(" ".join(lines[index])), item["context"]):
                    continue
                here = _compact(" ".join(lines[index]))
                after = _compact(" ".join(lines[index + 1])) if index + 1 < len(lines) else ""
                label = next((_compact(label) for label in item["labels"]
                              if here.count(_compact(label)) > placed.get((index, _compact(label)), 0)
                              or (_compact(label) not in here and _compact(label) in here + after
                                  and _compact(label) not in after)), None)
                if label is not None:  # on this line, or wrapping onto the next
                    placed[(index, label)] = placed.get((index, label), 0) + 1
                    item["line"], start = index + 1, index
                    break

    def take(self, text: str, line: int) -> str | None:
        """The URL of the unused link on this line whose text reads exactly as text."""
        for item in self.items:
            if (not item["used"] and item["line"] == line
                    and any(_compact(label) == _compact(text) for label in item["labels"])):
                item["used"] = True
                return item["url"]
        return None

    def phrases(self, text: str, lines: list[int]) -> dict[str, str]:
        """Unused links on these lines whose text appears in text, as {exact phrase: URL}."""
        found = {}
        for item in self.items:
            if item["used"] or item["line"] not in lines:
                continue
            for label in item["labels"]:
                phrase = _find_phrase(text, label)
                if phrase and phrase not in found:
                    found[phrase] = item["url"]
                    item["used"] = True
                    break
        return found

    def unused(self) -> list[str]:
        return [f"{item['labels'][-1]} ({item['url']})" for item in self.items if not item["used"]]


def _web_url(text: str) -> str:
    return text if text.startswith(("https://", "http://")) else f"https://{text}"


def _is_name(text: str) -> bool:
    return (0 < len(text) <= 50 and len(text.split()) <= 5 and "@" not in text
            and not any(character.isdigit() for character in text) and any(character.isalpha() for character in text)
            and text.casefold().rstrip(":：") not in HEADINGS)


def _name_case(name: str) -> str:
    """ALEX EXAMPLE -> Alex Example; names already in mixed case stay as written."""
    if any(character.isascii() and character.isalpha() for character in name) and name == name.upper():
        return " ".join(word[:1] + word[1:].lower() for word in name.split())
    return name


def _name_terms(name: str) -> list[str]:
    """The name and each of its words as written and in capitals, such as a surname in an
    author list, longest first."""
    words = {variant for word in re.split(r"[\s,.]+", name) if len(word) >= 2 for variant in (word, word.upper())}
    return sorted({name, *words} - {""}, key=len, reverse=True)


def _contact_tokens(text: str) -> list[tuple[str, str]]:
    """Emails, phone numbers and web addresses written anywhere in the text, in order."""
    emails = [(match.start(), match.end(), "email", match.group(0)) for match in EMAIL.finditer(text)]
    found = list(emails)
    found += [(match.start(), match.end(), "link", match.group(0)) for match in WEB.finditer(text)
              if not any(start <= match.start() < end for start, end, _, _ in emails)]
    found += [(text.find(number), 0, "phone", number) for number in phones(text)]
    return [(kind, value) for _, _, kind, value in sorted(found)]


def _file(kind: str, value: str, private: dict[str, Any]) -> None:
    if private[kind]:
        private["other"].append(value)
    else:
        private[kind] = value.removeprefix("mailto:")


def _contact(pieces: list[str], line: int, links: _Links, private: dict[str, Any]) -> None:
    """File what the contact pieces of one line hold under email, phone, links, location or
    other. A piece may hold several details, such as "Alex Example alex@example.com 213-555-0199"."""
    for piece in pieces:
        value = LABEL.sub("", piece).strip()
        rest = value
        for kind, token in _contact_tokens(value):
            rest = rest.replace(token, " ")
            if kind == "link":
                private["links"].append({"label": token, "url": links.take(token, line) or _web_url(token)})
            else:
                _file(kind, token, private)
        rest = " ".join(rest.split()).strip(" |•·,;:")
        if not rest or rest.casefold() == private["name"].casefold():
            continue
        if (url := links.take(rest, line)) is not None:
            private["links"].append({"label": rest, "url": url})
        elif not private["location"] and any(character.isalpha() for character in rest) and len(rest) <= 60:
            private["location"] = rest
        else:
            private["other"].append(rest)


def _split(lines: list[list[str]], links: _Links) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    private: dict[str, Any] = {"name": "", "location": "", "phone": "", "email": "", "links": [], "other": []}
    hidden: set[int] = set()
    start = 0
    while start < len(lines) and " ".join(lines[start]).casefold().rstrip(":：") in DOCUMENT_TITLES:
        start += 1
    contact_lines: list[tuple[int, list[str]]] = []
    if start < len(lines):
        # The name, or the name followed on the same line by contact details.
        first = " ".join(lines[start])
        tokens = _contact_tokens(first)
        name = first[:first.find(tokens[0][1])].strip(" |•·,;:") if tokens else lines[start][0]
        if _is_name(name):
            private["name"] = _name_case(name)
            hidden.add(start)
            if tokens:  # the details after the name
                contact_lines.append((start + 1, [first[first.find(tokens[0][1]):]]))
            elif lines[start][1:]:
                contact_lines.append((start + 1, lines[start][1:]))
            start += 1
    for index in range(start, min(len(lines), start + HEADER_WINDOW)):
        if len(lines[index]) == 1 and lines[index][0].casefold().rstrip(":：") in HEADINGS:
            break
        if _contact_tokens(" ".join(lines[index])):
            hidden.add(index)
            contact_lines.append((index + 1, lines[index]))
    for line, parts in contact_lines:
        _contact([piece for part in parts for piece in SEPARATOR.split(part)], line, links, private)
    terms = _name_terms(private["name"]) if private["name"] else []
    public = [
        {"n": index + 1, "parts": parts, "masked": [mask(part, terms) for part in parts]}
        for index, parts in enumerate(lines) if index not in hidden
    ]
    return public, private


def split_private(
    lines: list[list[str]], links: Iterable[dict[str, Any]] = ()
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The lines DeepSeek may see, numbered from 1 as in the PDF, and what stays on this machine:
    the name, location, phone, email and links found above the first section, for the form."""
    return _split(lines, _Links(links, lines))


def _number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _joined(texts: list[str]) -> str:
    text = BULLET.sub("", texts[0], count=1)
    for more in texts[1:]:
        text = text + more if text.endswith("-") and more[:1].islower() else f"{text} {more}"
    return " ".join(text.split())


def _tags(value: Any, text: str) -> list[str]:
    tags: list[str] = []
    for tag in value if isinstance(value, list) else []:
        if isinstance(tag, str):
            tag = " ".join(tag.split())
            if (tag and len(tag) <= MAX_TAG_CHARACTERS and tag_pattern(tag).search(text)
                    and tag.casefold() not in {known.casefold() for known in tags}):
                tags.append(tag)
    return tags[:MAX_TAGS]


def _section_title(text: str) -> str:
    """The resume's own heading, in title case when written in capitals."""
    text = text.strip().rstrip(":：").strip()
    if any(character.isascii() and character.isalpha() for character in text) and text == text.upper():
        words = text.lower().split()
        text = " ".join(word if index and word in SMALL_WORDS else word[:1].upper() + word[1:]
                        for index, word in enumerate(words))
    return text


def _first_line(entry: dict[str, Any]) -> float:
    numbers = [value[0] for field in FIELDS if isinstance(value := entry.get(field), list) and value and _number(value[0])]
    numbers += [
        fact["lines"][0] for fact in entry.get("facts") or []
        if isinstance(fact, dict) and isinstance(fact.get("lines"), list) and fact["lines"] and _number(fact["lines"][0])
    ]
    return min(numbers, default=float("inf"))


class _Reader:
    """Checks DeepSeek's answer against the lines and copies the text it points at."""

    def __init__(self, public: list[dict[str, Any]]):
        self.lines = {line["n"]: line for line in public}
        self.order = [line["n"] for line in public]
        self.whole: set[int] = set()  # heading and fact lines
        self.parts: dict[int, set[int]] = {}  # entry header lines -> the parts used

    def free(self, number: int) -> bool:
        return number in self.lines and number not in self.whole and number not in self.parts

    def part(self, value: Any, taken: list[tuple[int, int]]) -> tuple[int, int] | None:
        if _number(value):
            value = [value, 1]
        if not (isinstance(value, list) and len(value) == 2 and all(map(_number, value))):
            return None
        number, part = value[0], value[1] - 1
        line = self.lines.get(number)
        if (line is None or number in self.whole or not 0 <= part < len(line["parts"])
                or part in self.parts.get(number, set()) or (number, part) in taken
                or line["masked"][part] in ("[email]", "[link]", "[phone]")):
            return None
        return number, part

    def fact(self, value: Any, header: set[int]) -> dict[str, Any] | None:
        numbers = value.get("lines") if isinstance(value, dict) else None
        if not isinstance(numbers, list) or not 0 < len(numbers) <= MAX_FACT_LINES or not all(map(_number, numbers)):
            return None
        if not all(self.free(number) and number not in header for number in numbers):
            return None
        first = self.order.index(numbers[0])
        if numbers != self.order[first:first + len(numbers)]:
            return None  # a fact is one run of consecutive lines
        texts = [" ".join(self.lines[number]["parts"]) for number in numbers]
        if any(BULLET.match(text) for text in texts[1:]):
            return None  # two bullets are two facts
        self.whole.update(numbers)
        text = _joined(texts)
        return {"text": text, "tags": _tags(value.get("tags"), text), "lines": numbers}


def _sections(content: Any, public: list[dict[str, Any]], links: _Links) -> tuple[list[dict[str, Any]], list[str]]:
    raw_sections = content.get("sections") if isinstance(content, dict) else None
    if not isinstance(raw_sections, list):
        raise CVImportError("DeepSeek 没有给出简历结构，请重试")
    reader = _Reader(public)
    sections: dict[str, dict[str, Any]] = {}
    raw_sections = sorted(
        (section for section in raw_sections if isinstance(section, dict) and section.get("kind") in SECTION_FACT_TYPES),
        key=lambda section: section["heading"] if _number(section.get("heading")) else float("inf"),
    )
    for raw in raw_sections:
        heading = raw.get("heading") if _number(raw.get("heading")) and reader.free(raw["heading"]) else None
        if heading is not None:
            reader.whole.add(heading)
        section = sections.get(raw["kind"])
        is_new = section is None
        if is_new:
            title = _section_title(" ".join(reader.lines[heading]["parts"])) if heading is not None else None
            section = {"kind": raw["kind"], "title": title, "entries": []}
        entries = raw.get("entries") if isinstance(raw.get("entries"), list) else []
        added = 0
        for entry in sorted((entry for entry in entries if isinstance(entry, dict)), key=_first_line):
            taken: list[tuple[int, int]] = []
            fields: dict[str, Any] = {}
            for field in FIELDS:
                ref = reader.part(entry.get(field), taken)
                if ref is not None:
                    taken.append(ref)
                    fields[field] = reader.lines[ref[0]]["parts"][ref[1]]
            header = {number for number, _ in taken}
            facts = [fact for value in entry.get("facts") or [] if (fact := reader.fact(value, header))]
            if not fields and not facts:
                continue
            for number, part in taken:
                reader.parts.setdefault(number, set()).add(part)
            section["entries"].append({
                **{field: fields.get(field) for field in FIELDS},
                "title_link": None, "facts": facts, "links": {}, "header": sorted(header),
            })
            added += 1
        if is_new and added:
            sections[raw["kind"]] = section
        elif heading is not None and (not is_new or not added):
            reader.whole.discard(heading)  # its title is not shown, so the line is reported below
    ordered = list(sections.values())
    for section in ordered:
        for entry in section["entries"]:
            # A link beside an entry's name, such as "GitHub", links the entry.
            for number in entry.pop("header"):
                line = reader.lines[number]
                for part, text in enumerate(line["parts"]):
                    if entry["title_link"] or part in reader.parts[number]:
                        continue
                    url = links.take(text, number) or (_web_url(text) if WEB.fullmatch(text) else None)
                    if url:
                        entry["title_link"] = {"label": text, "url": url}
                        reader.parts[number].add(part)
    for section in ordered:
        for entry in section["entries"]:
            for fact in entry["facts"]:
                entry["links"].update(links.phrases(fact["text"], fact.pop("lines")))
    not_imported = []
    for line in public:
        if line["n"] in reader.whole:
            continue
        used = reader.parts.get(line["n"], set())
        unused = [text for part, text in enumerate(line["parts"]) if part not in used]
        if unused:
            not_imported.append(" ".join(unused))
    return ordered, not_imported + links.unused()


def structure_cv(
    lines: list[list[str]],
    chat: Callable[..., dict[str, Any]],
    links: Iterable[dict[str, Any]] = (),
    effort: str = "none",
) -> dict[str, Any]:
    """Sections, entries and facts copied from the lines DeepSeek points at, the private details
    kept for the form, and every line or link that was not placed anywhere."""
    pool = _Links(links, lines)
    public, private = _split(lines, pool)
    if not public:
        raise CVImportError("除了姓名和联系方式，没有读到简历内容")
    request = {"lines": [{"n": line["n"], "parts": line["masked"]} for line in public]}
    messages = [
        {"role": "system", "content": STRUCTURE_RULES},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]
    try:
        answer = chat(messages, model=DEFAULT_MODEL, effort=effort)
    except DeepSeekError as exc:
        raise CVImportError(f"DeepSeek 暂时无法整理这份简历：{exc}") from exc
    sections, not_imported = _sections(answer.get("content"), public, pool)
    if not sections:
        raise CVImportError("没能从这份简历里认出教育、经历、项目、技能或论文部分")
    return {
        "private": private,
        "sections": sections,
        "not_imported": not_imported,
        "structuring": {"model": answer.get("model"), "usage": answer.get("usage")},
    }


def _new_id(fact_type: str, text: str, taken: set[str]) -> str:
    """A readable ID from the text, never one already stored: re-uploading an old CV must add a
    separate pending fact, not a new version that undoes a later correction."""
    base = f"fact-cv-{fact_type}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]}"
    fact_id, number = base, 2
    while fact_id in taken:
        fact_id, number = f"{base}-{number}", number + 1
    return fact_id


def build_profile(
    proposal: dict[str, Any], contact: dict[str, Any], facts_db: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """The CV profile for a reviewed upload, the new facts to import as pending, and how many
    lines reuse a fact already stored with exactly the same text."""
    known: dict[str, str] = {}
    stored: set[str] = set()
    if Path(facts_db).exists():
        for fact in list_facts(facts_db):
            known.setdefault(fact["text"], fact["id"])
            stored.add(fact["id"])
    items: list[dict[str, Any]] = []
    placed: set[str] = set()
    reused = 0
    sections = []
    for section in proposal["sections"]:
        fact_type = SECTION_FACT_TYPES[section["kind"]]
        entries = []
        for entry in section["entries"]:
            ids = []
            for fact in entry["facts"]:
                fact_id = known.get(fact["text"]) or next(
                    (item["id"] for item in items if item["text"] == fact["text"]), None
                ) or _new_id(fact_type, fact["text"], stored | {item["id"] for item in items})
                if fact_id in placed:
                    continue  # the same line twice; a CV lists each fact once
                placed.add(fact_id)
                ids.append(fact_id)
                if fact["text"] in known:
                    reused += 1
                else:
                    items.append({"id": fact_id, "text": fact["text"], "type": fact_type, "tags": fact["tags"]})
            item = {key: entry[key] for key in (*FIELDS, "title_link") if entry.get(key)}
            if ids:
                item["facts"] = ids
            if entry.get("links"):
                item["links"] = entry["links"]
            if item:
                entries.append(item)
        if entries:
            sections.append({"kind": section["kind"], **({"title": section["title"]} if section.get("title") else {}),
                             "entries": entries})
    if not sections:
        raise CVImportError("这份简历没有可以导入的内容")
    details = {key: " ".join(str(contact.get(key) or "").split()) for key in ("name", "location", "phone", "email")}
    if not details["name"]:
        raise CVImportError("请填写姓名")
    links = [
        {"label": " ".join(str(link.get("label") or "").split()), "url": _web_url(str(link.get("url") or "").strip())}
        for link in contact.get("links") or [] if isinstance(link, dict) and str(link.get("url") or "").strip()
    ]
    profile = {
        "profile_version": PROFILE_VERSION,
        "name": details["name"],
        "contact": {
            **{key: details[key] for key in ("location", "phone", "email") if details[key]},
            "links": [{"label": link["label"] or link["url"], "url": link["url"]} for link in links],
        },
        "sections": sections,
    }
    try:
        _header(profile, "en", [])
    except CVError as exc:
        raise CVImportError(str(exc)) from exc
    return profile, items, reused
