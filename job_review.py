"""Local evidence for deciding whether to prepare a job-specific CV.

This is a transparent keyword baseline, not a semantic match or eligibility decision.
All current resume entries can support a displayed skill hint. Reading a job calls no model,
assesses nothing, confirms no fact and changes no material or approval.
"""
from typing import Any

from facts import tag_finder, tag_pattern
from sponsorship import shown


def build_job_review(jd: dict[str, Any], facts: list[dict[str, Any]],
                     sponsorship: dict[str, Any] | None) -> dict[str, Any]:
    """Return the job's sponsorship statements and paired JD/fact skill-word evidence.

    Input facts must belong to the requesting user's snapshot. ``sponsorship`` is the answer
    already worked out for this job's words (V2 stores it when the job is saved; V1 works it
    out on reading), or None when there is none: it shows as not assessed, never as unknown.
    The source and capture time shown are this job's own. Unknown sponsorship stays unknown;
    none of these hints establishes the candidate's work permission.
    """
    tags = list(dict.fromkeys(tag for fact in facts for tag in fact.get("tags", [])))
    text = jd.get("text") or ""
    matches = tag_finder(tags)(text) if tags else []
    hints = []
    for tag in matches[:20]:
        find = tag_finder([tag])
        line = next((line for line in text.splitlines() if find(line)), "")
        occurrence = tag_pattern(tag).search(line)
        # Keep the literal surrounding text, never a model-written explanation.
        quote = line[max(0, occurrence.start() - 90):occurrence.end() + 180] if occurrence else line[:270]
        supporting = [fact for fact in facts if tag.casefold() in
                      {item.casefold() for item in fact.get("tags", [])}]
        hints.append({"tag": tag, "job_quote": quote,
                      "facts": [{key: fact[key] for key in ("id", "version", "text")}
                                for fact in supporting[:3]]})
    return {
        "sponsorship": shown(sponsorship, source=jd.get("source"), captured_at=jd.get("captured_at")),
        "skill_matches": hints,
        "skill_match_count": len(matches),
        "confirmed_fact_count": sum(fact.get("status") == "confirmed" for fact in facts),
        "pending_fact_count": sum(fact.get("status") != "confirmed" for fact in facts),
        "fact_count": len(facts),
    }
