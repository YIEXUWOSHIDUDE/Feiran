"""What per-job DeepSeek requests must never carry: the user's name, contact details and the
names of their schools and employers.

The CV header and entry titles are never sent, but a line copied from an uploaded CV can still
hold them, such as an author list or a link to a paper. Every request built from CV lines masks
them; the CV itself keeps each line word for word.
"""

import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit


EMAIL = re.compile(r"(?:mailto:)?[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
WEB = re.compile(
    r"(?:https?://|www\.)[^\s|,;]+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|dev|ai|me|edu|co|app|site|page|xyz|tech|info"
    r"|cn|uk|de|us|ca|au|hk|sg|jp)\b(?:/[^\s|,;]*)?",
    re.ASCII,
)
PHONE = re.compile(r"\+?[\d\s().-]+")
PHONE_LIKE = re.compile(r"\+?\(?\d[\d\s().-]{8,}\d")
DATE_LIKE = re.compile(r"(?:19|20)\d\d\s*[./-]\s*\d{1,2}\b|\b(?:19|20)\d\d\b\D+\b(?:19|20)\d\d\b")
PRIVATE_SECTIONS = ("education", "experience")  # their entry titles are schools and employers
# Link labels that name a service or a kind of page rather than the user; any other label,
# such as a handle, is private.
GENERIC_LABELS = {
    "github", "gitlab", "bitbucket", "linkedin", "portfolio", "website", "personal website", "homepage",
    "home page", "blog", "google scholar", "scholar", "orcid", "researchgate", "kaggle", "leetcode", "medium",
    "twitter", "x", "stack overflow", "stackoverflow", "hugging face", "huggingface", "behance", "dribbble",
    "resume", "cv", "demo", "projects", "email", "phone", "个人主页", "主页", "博客", "领英",
}
URL_WORDS = {"in", "pub", "u", "user", "users", "profile", "people", "citations", "www", "hl", "en"}


def _handles(url: str) -> list[str]:
    """The parts of a personal link that name the user: github.com/octocat -> octocat."""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    words = [unquote(word) for word in parts.path.split("/")] + [value for _, value in parse_qsl(parts.query)]
    return [word for word in words if len(word) >= 2 and word.casefold() not in URL_WORDS]


def is_phone(text: str) -> bool:
    """10 to 15 digits in phone punctuation, and not a date range such as 2022.09 - 2026.06."""
    return bool(PHONE.fullmatch(text)) and 10 <= sum(c.isdigit() for c in text) <= 15 and not DATE_LIKE.search(text)


def phones(text: str) -> list[str]:
    """Phone numbers written anywhere in the text."""
    return [number for match in PHONE_LIKE.finditer(text) if is_phone(number := match.group(0).strip())]


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for text in value.values() if isinstance(text, str)]
    return []


def private_terms(cv: dict[str, Any]) -> list[str]:
    """The name (whole, and each word as written and in capitals), contact details and school
    and employer names of a CV draft or of a profile, longest first."""
    if "header" in cv:  # a draft: each field already in one language
        header = cv["header"]
        names = _texts(header.get("name"))
        contact = [item.get("text") for item in header.get("details", [])]
        links = [(item.get("text"), item.get("url")) for item in header.get("links", [])]
    else:
        names = _texts(cv.get("name"))
        details = cv.get("contact") if isinstance(cv.get("contact"), dict) else {}
        contact = [*_texts(details.get("location")), details.get("phone"), details.get("email")]
        links = [(item.get("label"), item.get("url")) for item in details.get("links") or [] if isinstance(item, dict)]
    for label, url in links:
        if isinstance(url, str) and url.strip():
            contact += [url, *_handles(url.strip())]
        if isinstance(label, str) and label.strip().casefold() not in GENERIC_LABELS:
            contact.append(label)
    titles = [
        text for section in cv.get("sections") or [] if isinstance(section, dict) and section.get("kind") in PRIVATE_SECTIONS
        for entry in section.get("entries") or [] if isinstance(entry, dict) for text in _texts(entry.get("title"))
    ]
    words = [variant for name in names for word in re.split(r"[\s,.]+", name) if len(word) >= 2
             for variant in (word, word.upper())]
    terms = {term.strip() for term in [*names, *words, *contact, *titles] if isinstance(term, str) and term.strip()}
    return sorted(terms, key=len, reverse=True)


def stand_ins(ids: Iterable[str], prefix: str = "L") -> tuple[dict[str, str], dict[str, str]]:
    """Short IDs to send in place of fact IDs, and the way back. A fact ID can be made from its
    text (fact-example-university-coursework), so it never goes out; an answer names the stand-in."""
    out = {real: f"{prefix}{number}" for number, real in enumerate(dict.fromkeys(ids), 1)}
    return out, {alias: real for real, alias in out.items()}


def mask(text: str, terms: Iterable[str] = ()) -> str:
    """The text with emails, web addresses, phone numbers and the given terms replaced. Terms
    match as written, so a name such as Will does not hide the word "will"."""
    text = WEB.sub("[link]", EMAIL.sub("[email]", text))
    for number in phones(text):
        text = text.replace(number, "[phone]")
    for term in terms:
        if term.isascii():
            text = re.sub(rf"(?<!\w){re.escape(term)}(?!\w)", "[private]", text)
        else:
            text = text.replace(term, "[private]")
    return text
