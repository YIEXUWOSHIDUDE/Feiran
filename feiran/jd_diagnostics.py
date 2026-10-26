"""What can be said about a saved job description, shown beside it on the job page: where it came
from, when it was captured and (only when the source said so) posted, how it was read, how long
it is, and whether duties and qualifications were detected. A pure rule over the saved snapshot,
shared by both apps: nothing is fetched again and no model is asked.

It never says more than the text shows. "Detected" means a duties or a qualifications heading
with text under it, or such a clause where it can belong (a "Testing ..." line under Requirements
is a skill asked for, not a duty); it is not proof that the posting is complete, that its source
is official or that the role is still open. A missing field stays unknown: the capture
time is never offered as the posting date.
"""

import re
from typing import Any
from urllib.parse import urlsplit

from feiran.job_url import QUALIFICATION, has_job_details

# A heading line: the whole line, or its start before a colon ("Requirements: Python"). What
# follows it, up to the next heading, belongs to that section; text before any heading is unheaded.
DUTY_HEADINGS = (r"(?:key |your |main |primary |core |job )?(?:responsibilities|duties)|what you.ll do|"
                 r"what you will do|the role|role overview|岗位职责|工作职责|职责描述|工作内容")
QUALIFICATION_HEADINGS = (r"(?:minimum |basic |preferred |required |desired )?(?:qualifications|requirements|skills)|"
                          r"what you.ll bring|what you will bring|what we.re looking for|what we look for|about you|"
                          r"岗位要求|任职要求|任职资格|职位要求|任职条件")
OTHER_HEADINGS = (r"benefits|perks|about us|about the (?:company|team)|compensation|salary|how to apply|"
                  r"福利待遇|福利|公司介绍|关于我们")
HEADING = re.compile(rf"^\s*(?:#+\s*)?(?:(?P<duty>{DUTY_HEADINGS})|(?P<qualification>{QUALIFICATION_HEADINGS})|"
                     rf"(?P<other>{OTHER_HEADINGS}))\s*(?:$|[:：]\s*(?P<rest>.*)$)", re.I)
# A duty clause: one of these verbs (any common form, as a whole word, after "you", "we" or
# "you'll" at most) opening a line, a bullet or a sentence, and not naming a skill ("Design skills", "Testing experience"); or 负责/参与 there.
CLAUSE_START = r"(?:^|[.!?。；;，,]\s*)\s*(?:[-•*·]\s*|\d+[.、)]\s*)?"
DUTY_CLAUSE = re.compile(rf"{CLAUSE_START}(?:(?:you|we)(?:.ll| will)?\s+|will\s+)?"
                         r"(?:(?:build|develop|maintain|design|implement|test)(?:s|ed|ing)?|collaborat(?:e|es|ed|ing))\b"
                         r"(?!\s+(?:skills?|experience|knowledge|background|principles|patterns|tools?|frameworks?)\b)"
                         rf"|{CLAUSE_START}(?:负责|参与)", re.I)
TRUNCATED = re.compile(r"(?:\.\.\.|…)\s*$|(?:read|see) more[.!]?\s*$|查看全文\s*$", re.I)
SHORT_CHARACTERS = 400  # below this a posting is usually a summary: a hint to look, never a refusal
READERS = {"jobposting-jsonld": "structured", "jobposting-jsonld+html-text": "structured_and_page",
           "html-text": "page_text"}


def _web_url(value: Any) -> str | None:
    """An http(s) URL with a host, or None: anything else is not shown as a link."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    return value.strip() if parts.scheme in ("http", "https") and parts.hostname else None


def _when(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _sections(text: str) -> tuple[bool, bool]:
    """Whether duties and qualifications are found, each only where it can belong: a duties
    heading with text under it, or a duty clause outside the qualifications and other sections
    (so "Testing with pytest" under Requirements is no duty); likewise a qualifications heading
    with text, or a qualification phrase outside the duties and other sections."""
    section, duties, qualifications = None, False, False
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            section = next(name for name in ("duty", "qualification", "other") if heading.group(name))
            line = heading.group("rest") or ""
        if not line.strip():
            continue
        if section == "duty":
            duties = True
        elif section == "qualification":
            qualifications = True
        if section in (None, "duty") and QUALIFICATION.search(line):
            qualifications = True
        if section in (None, "duty") and DUTY_CLAUSE.search(line):
            duties = True
    return duties, qualifications


def jd_diagnostics(jd: Any) -> dict[str, Any]:
    """The facts and hints about one saved JD snapshot (``jd`` as the job holds it). Notes are
    codes the page words in both languages: no_source, unsafe_source, no_posted_at,
    no_captured_at, no_duties, no_qualifications, short, truncated, web_summary (a web page's
    summary that the generation gate refuses)."""
    jd = jd if isinstance(jd, dict) else {}
    text = jd.get("text") if isinstance(jd.get("text"), str) else ""
    provider = jd.get("provider") if isinstance(jd.get("provider"), str) else None
    raw_source = jd.get("source")
    source = _web_url(raw_source)
    method = jd.get("extraction_method") if isinstance(jd.get("extraction_method"), str) else None
    if provider == "manual":
        reader = "pasted"
    elif provider == "web":
        reader = READERS.get(method, "page")
    elif provider:
        reader = "job_board"  # a public board's own API (Greenhouse, Lever, Ashby, Tencent)
    else:
        reader = "unknown"
    duties, qualifications = _sections(text)
    notes = []
    if raw_source in (None, "") or (isinstance(raw_source, str) and not raw_source.strip()):
        notes.append("no_source")
    elif source is None:
        notes.append("unsafe_source")
    if _when(jd.get("posted_at")) is None:
        notes.append("no_posted_at")
    if _when(jd.get("captured_at")) is None:
        notes.append("no_captured_at")
    if not duties:
        notes.append("no_duties")
    if not qualifications:
        notes.append("no_qualifications")
    if len(text) < SHORT_CHARACTERS:
        notes.append("short")
    if TRUNCATED.search(text):
        notes.append("truncated")
    if provider == "web" and not has_job_details(text):
        notes.append("web_summary")
    return {
        "source": source, "requested_url": _web_url(jd.get("requested_url")),
        "captured_at": _when(jd.get("captured_at")), "posted_at": _when(jd.get("posted_at")),
        "updated_at": _when(jd.get("updated_at")), "provider": provider, "reader": reader, "method": method,
        "characters": len(text), "lines": len([line for line in text.splitlines() if line.strip()]),
        "duties_detected": duties, "qualifications_detected": qualifications, "notes": notes,
    }
