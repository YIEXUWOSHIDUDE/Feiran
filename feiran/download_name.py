"""Build safe, distinguishable Content-Disposition values for CV PDF downloads."""

import re
import unicodedata
from urllib.parse import quote


_MAX_NAME_BYTES = 150
_ATTR_CHAR = "!#$&+-.^_`|~"


def _clean(value: object, *, default: str, limit: int) -> str:
    """Remove header controls and path separators while retaining ordinary Unicode text."""
    text = str(value or "")
    chars = []
    for char in text:
        if char in "/\\" or unicodedata.category(char).startswith("C"):
            chars.append(" ")
        else:
            chars.append(char)
    text = " ".join("".join(chars).split()).strip(" .")
    return text[:limit].strip(" .") or default


def _fit_utf8(value: str, byte_limit: int) -> str:
    """Trim only at Unicode character boundaries to fit a UTF-8 byte budget."""
    result = []
    used = 0
    for char in value:
        size = len(char.encode("utf-8"))
        if used + size > byte_limit:
            break
        result.append(char)
        used += size
    return "".join(result).strip(" .")


def _ascii_fallback(name: str) -> str:
    ascii_name = name.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_name)
    ascii_name = re.sub(r"-+", "-", ascii_name).strip("-.")
    return ascii_name or "CV-Job.pdf"


def pdf_disposition(*, company: object, title: object, language: object,
                    job_id: object, version: object) -> str:
    """Return an attachment disposition with a safe ASCII fallback and UTF-8 filename.

    The short job and content-version identifiers keep downloads distinct even when a user
    exports several jobs for the same company and role. The Unicode filename is bounded to
    150 UTF-8 bytes; the fallback contains only conservative ASCII filename characters.
    """
    name = pdf_filename(company=company, title=title, language=language, job_id=job_id, version=version)
    fallback = _ascii_fallback(name)
    extended = quote(name, safe=_ATTR_CHAR, encoding="utf-8", errors="strict")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{extended}'


def pdf_filename(*, company: object, title: object, language: object, job_id: object, version: object) -> str:
    """The Unicode file name of a CV PDF (pdf_disposition's): no path separator or control
    character, at most 150 UTF-8 bytes, ending in .pdf."""
    company_name = _clean(company, default="Job", limit=48)
    title_name = _clean(title, default="Job", limit=48)
    language_name = _clean(language, default="unknown", limit=8).upper()
    job_short = re.sub(r"[^A-Za-z0-9]", "", str(job_id or ""))[:8].lower() or "unknown"
    version_short = re.sub(r"[^A-Za-z0-9]", "", str(version or ""))[:8].lower() or "unknown"

    suffix = f"-{language_name}-{job_short}-v{version_short}.pdf"
    fixed = f"CV--{suffix}"
    remaining = max(0, _MAX_NAME_BYTES - len(fixed.encode("utf-8")))
    company_budget = min(len(company_name.encode("utf-8")), remaining // 2)
    company_name = _fit_utf8(company_name, company_budget) or "Job"
    remaining -= len(company_name.encode("utf-8"))
    title_name = _fit_utf8(title_name, min(len(title_name.encode("utf-8")), remaining)) or "Job"
    name = f"CV-{company_name}-{title_name}{suffix}"

    # Tiny budgets can be consumed by unusually long caller-provided language values.
    while len(name.encode("utf-8")) > _MAX_NAME_BYTES and title_name:
        title_name = title_name[:-1].rstrip(" .")
        name = f"CV-{company_name}-{title_name or 'Job'}{suffix}"
    return name


def folder_name(*, company: object, title: object, job_id: object) -> str:
    """A ZIP folder for one job's files: company, title and the whole job ID, cleaned like a
    file name (no separator, no control character, no leading dot), at most 154 UTF-8 bytes. The
    whole ID, not the short one in file names: a single-user job ID starts with its creation
    date, so two jobs of the same company and title made the same day would share a short ID."""
    company_name = _fit_utf8(_clean(company, default="Job", limit=48), 40) or "Job"
    title_name = _fit_utf8(_clean(title, default="Job", limit=48), 48) or "Job"
    job = re.sub(r"[^A-Za-z0-9_-]", "", str(job_id or ""))[:64] or "unknown"
    return f"{company_name}-{title_name}-{job}".lstrip(".")
