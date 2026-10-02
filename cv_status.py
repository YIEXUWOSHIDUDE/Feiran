"""How a CV preparation went, in the words the page shows: each stage's outcome and what a
failure means for the user. Shared by the single-user app (web.py) and V2's task runner, so both
explain a stage the same way. No data is read or written here."""

import re

from cv import rewritten_lines
from deepseek_client import DeepSeekError


CANDIDATE_FIELDS = ("id", "text", "section", "strength", "status", "decided_by", "extraction_method")

FALLBACK_PATH = re.compile(r"sections\[(\d+)\](?:\.entries\[(\d+)\])?\.(title|subtitle|location|dates)")
FIELD_NAMES = {"title": "name", "subtitle": "role or degree", "location": "location", "dates": "dates"}


# What each kind of failure means and what to do about it. The page never quotes DeepSeek's raw
# error, so neither a key nor response text can reach it.
REASONS = {
    "missing_key": "No DeepSeek API key was found. Add it to the Keychain (service deepseek-api-key), "
                   "set DEEPSEEK_API_KEY, or on a server put it in the file DEEPSEEK_API_KEY_FILE names "
                   "(on AWS: the Secrets Manager secret the stack names), then try again.",
    "key_rejected": "DeepSeek refused the API key. Check or replace the key, then try again.",
    "rate_limited": "DeepSeek is busy right now. Try again in a minute.",
    "unreachable": "DeepSeek could not be reached. Check the internet connection, then try again.",
    "request_failed": "DeepSeek returned an error. Try again later.",
    "bad_response": "DeepSeek's answer could not be used. Try again.",
    "nothing_found": "DeepSeek found no requirement lines.",
    "no_profile": "There is no CV yet. Upload your CV on the Facts page.",
    "no_facts": "There are no facts yet. Upload your CV on the Facts page.",
    "facts_not_confirmed": "Some lines of your CV are not confirmed yet. Confirm them on the Facts page.",
    "facts_missing": "Your CV lists facts that are no longer stored. Upload your CV again on the Facts page.",
    "profile_unreadable": "Your CV layout file could not be read, so it is not clear what must stay private. "
                          "Nothing was sent to DeepSeek. Upload your CV again on the Facts page.",
    "backup_unreadable": "A backup of your CV layout ({file} in .local/profile-history) could not be read, so it "
                         "is not clear what must stay private. Nothing was sent to DeepSeek. Delete or fix that "
                         "file, then try again.",
}
STAGES = ("draft", "rewording", "layout")
STAGE_FAILED = {"draft": "Not prepared", "rewording": "Not reworded", "layout": "Not adjusted for this job"}
STAGE_KEPT = {"rewording": "Your confirmed wording is used.", "layout": "Your usual layout is used."}
STAGE_KEPT_EARLIER = {"rewording": "The earlier rewording below is kept.", "layout": "The earlier adjusted layout below is kept."}


def reason(exc: Exception) -> str:
    """The kind of failure: DeepSeek's own, or the one a domain error names."""
    for error in (exc, exc.__cause__):
        if isinstance(error, DeepSeekError):
            return error.reason
    return getattr(exc, "reason", "not_usable")


def stage_entry(stage: str, status: str, message: str, reason_code: str | None = None, output: bool = True) -> dict:
    return {"stage": stage, "status": status, "reason_code": reason_code, "message": message, "output_available": output}


def stage_failure(stage: str, exc: Exception, earlier: bool = False) -> dict:
    """A stage that did not work: a fallback when a result stays usable, else failed. ``earlier``
    says a retry failed while this stage's earlier result is still shown."""
    code = reason(exc)
    # Domain messages hold no key or response text.
    explanation = (REASONS.get(code) or str(exc)).replace("{file}", getattr(exc, "file", ""))
    if stage == "draft":
        return stage_entry(stage, "failed", f"{STAGE_FAILED[stage]}: {explanation}", code, output=False)
    kept = STAGE_KEPT_EARLIER[stage] if earlier else STAGE_KEPT[stage]
    return stage_entry(stage, "fallback", f"{STAGE_FAILED[stage]}{' again' if earlier else ''}: {explanation} {kept}", code)


def reworded(head: dict) -> dict:
    changed, kept = len(rewritten_lines(head)), head["tailoring"]["rejected"]
    note = f"; {kept} kept as confirmed because the rewording failed the fact check" if kept else ""
    return stage_entry("rewording", "done", f"Reworded for this job: {changed} line(s) changed{note}.")


def adjusted(planned: dict) -> dict:
    count = len(planned["plan"]["changes"])
    return stage_entry("layout", "done", f"Adjusted for this job: {count} change(s), listed below." if count
                       else "Adjusted for this job: your usual layout already fits.")


def explained(record: dict | None) -> dict | None:
    """A fallback record with its reason in plain words for the page."""
    if isinstance(record, dict) and record.get("fallback_code"):
        return {**record, "message": REASONS.get(record["fallback_code"]) or record.get("fallback_reason")}
    return record


def describe_fallbacks(draft: dict) -> list[str]:
    """Turn profile paths such as sections[1].entries[0].title into words the user can act on."""
    labels = []
    for path in draft["language_fallbacks"]:
        match = FALLBACK_PATH.fullmatch(path)
        if path == "name":
            labels.append(f"Name: {draft['header']['name']}")
        elif path == "contact.location":
            labels.append(f"Location: {draft['header']['details'][0]['text']}")
        elif match and match[2] is None:
            section = draft["sections"][int(match[1])]
            labels.append(f"{section['kind'].capitalize()} heading: {section['title']}")
        elif match:
            section = draft["sections"][int(match[1])]
            value = section["entries"][int(match[2])][match[3]]
            labels.append(f"{section['kind'].capitalize()} {FIELD_NAMES[match[3]]}: {value}")
        else:
            labels.append(path)
    return labels


def job_language(text: str) -> str:
    """The CV language a posting most likely wants: Chinese when it is mostly written in Chinese."""
    chinese = sum("\u4e00" <= character <= "\u9fff" for character in text)
    latin = sum(character.isascii() and character.isalpha() for character in text)
    return "zh" if chinese * 4 > latin else "en"
