"""Conservative sponsorship signals from a supplied job-description snapshot.

This is a finite set of text rules, not an eligibility check or a claim that a
posting is current or authoritative. Unsupported wording stays unknown. Evidence
uses Python string offsets into the original input; whitespace is never rewritten.

RULE_VERSION names these rules. V2 stores each answer with the version that made it
(tenant_store); raise it whenever a change could give any text a different answer, so
answers of the old rules are shown as not yet checked instead of as current ones.
"""

import re
from typing import Any


RULE_VERSION = 1
STATUSES = ("supported", "not_supported", "conditional", "unknown", "conflicting")
_FLAGS = re.IGNORECASE
_SPONSOR = r"(?:(?:work\s+)?visa\s+|immigration\s+|employment\s+|h[-‐‑–]?1b\s+)?sponsorship"
_SUBJECT = r"(?:we|the\s+company|this\s+(?:company|employer|role|position))"
_VISA = r"(?:work\s+)?visas?|h[-‐‑–]?1b(?:\s+visas?)?"
_ZH_SUBJECT = r"(?:我们|我們|本公司|公司|雇主|本岗位|本崗位|本职位|本職位|该岗位|該崗位)"
_ZH_POLICY_SUBJECT = (rf"(?:^|[\n:：,，])\s*(?:[-*•]\s*)?{_ZH_SUBJECT}\s*(?:(?:将|將|可|可以|会|會)\s*)?"
                      r"(?:(?:为|為)(?:候选人|候選人|员工|員工|申请者|申請者)\s*)?")
_VISA_CONTEXT = re.compile(r"\b(?:visas?|immigration|h[-‐‑–]?1b)\b|签证|簽證", _FLAGS)
_FOLLOWUP_REFERENCE = re.compile(r"\b(?:it|this|that|these|such)\b|(?:该|該|此)(?:政策|支持|安排)", _FLAGS)
_FOLLOWUP_LIMIT = re.compile(
    r"\b(?:not|no|never|unable|cannot|only|limited|after|once|upon|unless|except)\b|"
    r"仅|僅|不|无法|無法|除非", _FLAGS,
)


def _patterns(*values):
    return tuple(re.compile(value, _FLAGS) for value in values)


_RELEVANT = re.compile(
    r"\bsponsor(?:ship|ing|ed|s)?\b|(?<![A-Za-z0-9])h[-‐‑–]?1b(?![A-Za-z0-9])|签证|簽證|"
    r"\bwork\s+authori[sz]ation\b|\bauthori[sz]ed\s+to\s+work\b",
    _FLAGS,
)
# An explicit attempt to direct classification taints the supplied text, even
# when the desired answer is in the next sentence. Do not resolve it as policy.
_DIRECTIVE = _patterns(
    r"\b(?:ignore|disregard)\b[\s\S]{0,60}\binstructions?\b",
    r"(?:^|\n)\s*(?:system|assistant|user)\s*:",
    r"\b(?:classify|label|mark|output|return|report|pretend|assume)\b.{0,80}"
    r"(?:sponsor|supported|eligible)",
    r"(?:忽略|无视|無視).{0,30}(?:指令|提示)|(?:输出|輸出|返回|标记|標記|判定).{0,40}"
    r"(?:支持|赞助|贊助|sponsor)",
)
# Questionnaires and instructions describe something to answer, not employer policy.
_NON_POLICY = _patterns(
    # A policy expressly scoped to other roles says nothing about this role.
    r"\bfor\s+other\s+(?:positions|roles)\b",
    r"[?？]",
    r"\b(?:do|will|would|can|could)\s+you\b.*\bsponsor",
    r"\b(?:ignore|disregard)\b.*\binstructions?\b",
    r"\b(?:system|assistant|user)\s*:",
    r"\b(?:classify|label|mark|output|return|report|pretend|assume)\b.{0,80}"
    r"(?:sponsor|supported|eligible)",
    r"\b(?:example|sample|hypothetical|fictional|prompt)\b",
    r"(?:忽略|无视|無視).{0,30}(?:指令|提示)|(?:输出|輸出|返回|标记|標記|判定).{0,40}"
    r"(?:支持|赞助|贊助|sponsor)|示例|假设|假設|提示词|提示詞",
    r"\b(?:historically|previously|formerly|historical|history|last\s+year|in\s+the\s+past)\b",
    r"\b(?:has|have|had)\s+(?:been\s+)?sponsor(?:ed|ing)\b",
    r"\b(?:uscis|lca|h1bgrader|myvisajobs)\b|\baccording\s+to\b",
    r"(?:历史|歷史|曾经|曾經|过去|過去).{0,40}(?:签证|簽證|赞助|贊助|sponsor)",
    r"\b(?:event|conference|sports?|charity|festival|podcast|tournament)\b",
    r"\b(?:security\s+clearances?|professional\s+certifications?)\b",
    r"(?:活动|活動|赛事|賽事|大会|大會|会议|會議|公益).{0,25}(?:赞助|贊助|sponsor)",
    r"\b(?:incorrect|inaccurate|false\s+claim|not\s+true|no\s+longer\s+accurate)\b",
    r"\b(?:unclear|unsure|uncertain|unknown|whether|cannot\s+confirm|can't\s+confirm|"
    r"not\s+(?:clear|sure|known|confirmed|guaranteed))\b",
    r"(?:不确定|不確定|不一定|不保证|不保證|无法确认|無法確認|是否|尚未明确|尚未明確)",
)
_NEGATIVE = _patterns(
    rf"\bno\s+{_SPONSOR}(?:\s+(?:is|will\s+be))?\s*(?:available|provided|offered|supported)\b",
    rf"\bno\s+(?:visa\s+|immigration\s+|employment\s+|h[-‐‑–]?1b\s+)sponsorship\b"
    r"(?=\s*(?:[,.;!?。！？；]|$)|\s+(?:now\b|in\s+the\s+future\b|at\s+this\s+time\b|for\s+(?:this|the)\s+(?:role|position)\b))",
    rf"\b{_SPONSOR}\s+(?:(?:is|will\s+be)\s+)?not\s+(?:available|provided|offered|supported)\b",
    rf"\b{_SUBJECT}\s+(?:cannot|can't|do\s+not|don't|will\s+not|won't|"
    rf"are\s+unable\s+to|is\s+unable\s+to|are\s+not\s+able\s+to|is\s+not\s+able\s+to)\s+"
    rf"(?:(?:provide|offer|support)\s+{_SPONSOR}|sponsor(?:\s+(?:{_VISA}|candidates|applicants))?)\b",
    rf"\b(?:this\s+)?(?:role|position)\s+is\s+not\s+eligible\s+for\s+{_SPONSOR}\b",
    rf"\b{_SPONSOR}\b.{{0,100}}\bnot\s+(?:for\s+)?this\s+(?:role|position)\b",
    r"\b(?:candidates|applicants)\s+(?:who\s+)?(?:require|requiring|need|needing)\s+"
    rf"(?:current\s+or\s+future\s+)?{_SPONSOR}\s+(?:will\s+not\s+be|are\s+not)\s+"
    r"(?:considered|eligible|accepted)\b",
    r"\b(?:must|need\s+to|should)\s+be\s+authori[sz]ed\s+to\s+work\b.{0,100}"
    r"\bwithout\s+(?:(?:visa|employer|immigration)\s+)?sponsorship\b",
    r"(?:不|无法|無法|不能|不再)(?:会|會|予以)?(?:提供|支持|办理|辦理|接受).{0,8}"
    r"(?:签证|簽證)(?:支持|赞助|贊助)?",
    r"(?:本岗位|本崗位|本职位|本職位).{0,8}(?:不支持|不提供|无法提供|無法提供).{0,8}"
    r"(?:sponsor|赞助|贊助)",
)
_POSITIVE = _patterns(
    rf"\b{_SPONSOR}\s+(?:(?:is|will\s+be)\s+)?(?:available|provided|offered|supported)\b",
    rf"\b{_SUBJECT}\s+(?:(?:can|will|do)\s+|(?:are|is)\s+able\s+to\s+)?"
    rf"(?:provide|offer|support)\s+{_SPONSOR}\b",
    rf"\b{_SUBJECT}\s+(?:can\s+|will\s+)?sponsor\s+(?:{_VISA}|candidates|applicants)\b",
    rf"\b{_SPONSOR}\s*:\s*yes\b",
    rf"{_ZH_POLICY_SUBJECT}(?:提供|支持)(?:工作)?(?:签证|簽證)(?:支持|赞助|贊助)",
    rf"{_ZH_POLICY_SUBJECT}(?:办理|辦理)工作(?:签证|簽證)",
    r"(?:签证|簽證)(?:支持|赞助|贊助)\s*[:：]\s*(?:是|有|提供)",
)
_CONDITION = _patterns(
    r"\b(?:may|might|potentially|case[-\s]by[-\s]case|subject\s+to|"
    r"depends?\s+on|at\s+(?:our|the\s+employer's)\s+discretion)\b",
    r"\b(?:only|limited\s+to|if|provided\s+that|after|once|upon|already\s+holding)\b",
    r"(?:仅|僅|只限|视情况|視情況|酌情|可能|取决于|取決於|如果|条件|條件)",
)
# These need a sponsorship noun as well; a general conditional elsewhere is not
# evidence that the employer offers any sponsorship at all.
_CONDITIONAL_POLICY = _patterns(
    rf"\b{_SPONSOR}\s+(?:may|might|can)\s+be\s+(?:available|provided|offered|considered)\b",
    rf"\b{_SUBJECT}\s+(?:may|might|will|can)\s+consider\s+(?:offering\s+)?{_SPONSOR}\b",
    r"\bh[-‐‑–]?1b\s+transfers?\s+(?:only|(?:are\s+)?(?:accepted|supported|available|eligible))\b",
    r"\b(?:only|limited\s+to)\s+h[-‐‑–]?1b\s+transfers?\b",
    r"(?:仅|僅|只)(?:接受|支持|提供)?\s*h[-‐‑–]?1b\s*(?:转移|轉移|transfer)",
    rf"{_ZH_SUBJECT}\s*(?:可|可以|可能|视情况|視情況|酌情)(?:考虑|考慮)?(?:提供|支持|办理|辦理)"
    r".{0,8}(?:签证|簽證)",
)
_ABBREVIATION = re.compile(r"\b(?:[A-Za-z]\.){2,}$")
_FUTURE = re.compile(r"\b(?:future|later)\b|未来|未來", _FLAGS)
_BOTH_TIMES = re.compile(
    r"\b(?:now|currently|current|present)\b.{0,30}\bfuture\b|"
    r"(?:现在|現在|当前|當前).{0,15}(?:未来|未來)", _FLAGS,
)


def _segments(text):
    """Yield original sentence/bullet spans, keeping wrapped lines intact."""
    start = 0
    for boundary in re.finditer(r"[。！？；!?;]|\.(?=\s|$)|\n[ \t]*\n|\n(?=[ \t]*[-*•]\s)", text):
        end = boundary.end()
        # Do not detach a condition after an abbreviation such as "U.S. roles only". Whether
        # one ends here depends only on the last five characters (and \b still sees the one
        # before the window), so the search never rescans the text before it.
        if text[end - 1] == "." and _ABBREVIATION.search(text, max(0, end - 8), end):
            continue
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            yield left, right
        start = end
    left, right = start, len(text)
    while left < right and text[left].isspace():
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    if left < right:
        yield left, right


def _signal(segment):
    if any(pattern.search(segment) for pattern in _NON_POLICY):
        return "unknown"
    negatives = [match for pattern in _NEGATIVE for match in pattern.finditer(segment)]
    positives = [
        match for pattern in _POSITIVE for match in pattern.finditer(segment)
        if not any(negative.start() < match.end() and match.start() < negative.end()
                   for negative in negatives)
        and (not re.search(r"\b(?:candidates|applicants)\b", match.group(), _FLAGS)
             or _VISA_CONTEXT.search(segment))
    ]
    conditional = any(pattern.search(segment) for pattern in _CONDITIONAL_POLICY)
    if negatives and positives:
        return "conflicting"
    if negatives:
        if re.search(r"\b(?:unless|except)\b|除非", segment, _FLAGS):
            return "conditional"
        if conditional:
            return "conditional" if re.search(r"\btransfers?\b|转移|轉移", segment, _FLAGS) else "conflicting"
        if _FUTURE.search(segment) and not _BOTH_TIMES.search(segment):
            return "conditional"
        return "not_supported"
    if positives:
        # A positive substring under an unrecognized negation is not affirmative
        # evidence. Prefer unknown even if the negation might be unrelated.
        if re.search(r"\b(?:no|not|never|cannot|unable|unlikely|without|deny|denies|reject|"
                     r"can['’]t|don['’]t|won['’]t|isn['’]t|aren['’]t)\b|不|无法|無法|未|并非|並非",
                     segment, _FLAGS):
            return "unknown"
        return "conditional" if any(pattern.search(segment) for pattern in _CONDITION) else "supported"
    return "conditional" if conditional else "unknown"


def assess_sponsorship(text: str, *, source: str | None = None,
                       captured_at: str | None = None) -> dict:
    """Assess explicit job-text statements and return exact supporting spans.

    Questions and historical/company-database claims are not accepted as job
    policy. An explicit classification directive makes all text signals unknown.
    Work authorization alone proves no sponsorship policy. Immediate anaphoric
    restrictions are kept with their statement; this is not general coreference
    resolution. Conditions may include future-only restrictions or transfers only.
    Unrecognized relevant text remains visible as unknown evidence. source and
    captured_at are caller-supplied provenance, not independently verified here.
    """
    evidence = []
    directed = any(pattern.search(text) for pattern in _DIRECTIVE)
    spans = list(_segments(text))
    uncertain_restriction = False
    for index, (start, end) in enumerate(spans):
        segment = text[start:end]
        if _RELEVANT.search(segment):
            restricted = False
            if index + 1 < len(spans):
                next_start, next_end = spans[index + 1]
                following = text[next_start:next_end]
                if (not _RELEVANT.search(following)
                        and _FOLLOWUP_REFERENCE.search(following)
                        and _FOLLOWUP_LIMIT.search(following)):
                    # Keep a nearby "however ... offer it" / "this applies only"
                    # limitation instead of discarding it for lacking sponsor.
                    end = next_end
                    segment = text[start:end]
                    restricted = True
            signal = "unknown" if directed else _signal(segment)
            uncertain_restriction |= restricted and signal == "unknown"
            evidence.append({"text": segment, "start": start, "end": end,
                             "signal": signal})
    signals = {item["signal"] for item in evidence} - {"unknown"}
    transfer_exception = (
        signals == {"not_supported", "conditional"}
        and all(re.search(r"\btransfers?\b|转移|轉移", item["text"], _FLAGS)
                for item in evidence if item["signal"] == "conditional")
        and not any(re.search(r"\btransfers?\b|转移|轉移", item["text"], _FLAGS)
                    for item in evidence if item["signal"] == "not_supported")
    )
    if transfer_exception:
        status = "conditional"
    elif "conflicting" in signals or ("not_supported" in signals and len(signals) > 1):
        status = "conflicting"
    elif "conditional" in signals:
        status = "conditional"
    elif signals:
        status = next(iter(signals))
    else:
        status = "unknown"
    if status == "supported" and uncertain_restriction:
        status = "unknown"
    return {"status": status, "basis": "job_text", "rule_version": RULE_VERSION,
            "source": source, "captured_at": captured_at, "evidence": evidence}


def checked(text: str, result: Any) -> tuple[str, list[dict[str, Any]]]:
    """The status and evidence of ``result``, an assessment of ``text``, once it is shown to be
    an answer these rules can give: a known status, and evidence quoting exact spans of
    ``text``. Anything else raises ValueError, so it is never stored as an answer."""
    if not isinstance(result, dict) or result.get("status") not in STATUSES:
        raise ValueError("not a sponsorship status")
    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("sponsorship evidence must be a list")
    quotes = []
    for item in evidence:
        if not isinstance(item, dict) or item.get("signal") not in STATUSES:
            raise ValueError("sponsorship evidence must name a status")
        start, end, quote = item.get("start"), item.get("end"), item.get("text")
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or text[start:end] != quote):
            raise ValueError("sponsorship evidence must quote the text exactly")
        quotes.append({"text": quote, "start": start, "end": end, "signal": item["signal"]})
    return result["status"], quotes


def shown(answer: dict[str, Any] | None, *, source: str | None, captured_at: str | None) -> dict[str, Any]:
    """What a page shows for one saved text: ``answer`` (its status, evidence, rule version and,
    when stored, when it was assessed), or "not_assessed" when there is none under the current
    rules, which is never the same as "unknown" (the rules found no statement). ``source`` and
    ``captured_at`` belong to the posting or job the page shows, never to a shared answer."""
    if answer is None:
        return {"status": "not_assessed", "basis": "job_text", "rule_version": None, "assessed_at": None,
                "evidence": [], "source": source, "captured_at": captured_at}
    return {"status": answer["status"], "basis": "job_text", "rule_version": answer["rule_version"],
            "assessed_at": answer.get("assessed_at"), "evidence": answer["evidence"],
            "source": source, "captured_at": captured_at}
