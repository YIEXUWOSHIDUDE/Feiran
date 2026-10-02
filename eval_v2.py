"""B6 evaluation on synthetic, hand-labelled samples (examples/v2_eval_samples.json).

    python eval_v2.py offline       # the word-level claim checks on the rewrite samples; no model
    python eval_v2.py live --max-calls 9 --confirm-paid --out report.json   # paid; needs authorization

``offline`` is a regression gate: every overclaim the rules are built to catch must be flagged
and every supported line must pass; overclaims they cannot see are reported as known gaps, never
as passes. ``live`` asks the model to judge the evidence samples through gaps.find_gaps (the
app's own prompt and answer checks), one call per sample under a hard limit; the suggestion step
is stopped locally so it never reaches the model. It reports agreement with the labels next to a
keyword baseline, with the model, prompt fingerprint and sample version. A run's numbers describe
its synthetic samples only, not real CVs, and do not make the model's judgments guaranteed.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from claims import check_rewrite

SAMPLES = Path(__file__).parent / "examples" / "v2_eval_samples.json"
MAX_LIVE_CALLS = 100
GENERIC_WORDS = {"with", "experience", "years", "year", "proficiency", "knowledge", "familiarity", "strong",
                 "hands", "ability", "skills", "writing"}


def load(path: Path = SAMPLES) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def offline(samples: dict[str, Any]) -> dict[str, Any]:
    """The claim checks on every labelled rewrite."""
    results = []
    for case in samples["rewrites"]:
        reasons = check_rewrite(case["rewrite"], case["fact"], case["tags"], case.get("vocabulary", case["tags"]))
        results.append({"id": case["id"], "label": case["label"], "rules": case["rules"],
                        "category": case["category"], "flagged": bool(reasons)})
    regressions = [item["id"] for item in results
                   if item["rules"] == "must_catch" and item["flagged"] != (item["label"] == "overclaim")]
    by_category: dict[str, dict[str, int]] = {}
    for item in results:
        counts = by_category.setdefault(item["category"], {"cases": 0, "flagged": 0})
        counts["cases"] += 1
        counts["flagged"] += item["flagged"]
    return {"sample_version": samples["version"], "cases": len(results), "regressions": regressions,
            "known_gaps_missed": [item["id"] for item in results if item["rules"] == "known_gap" and not item["flagged"]],
            "known_gaps_caught": [item["id"] for item in results if item["rules"] == "known_gap" and item["flagged"]],
            "by_category": by_category}


class EvidenceOnly:
    """Passes only the evidence check to ``chat``, at most ``limit`` times. Any other request
    (the suggestion step) fails here, before the network."""

    def __init__(self, chat: Callable[..., dict[str, Any]], limit: int) -> None:
        self.chat, self.limit, self.calls = chat, limit, 0
        self.usage: dict[str, int] = {}
        self.models: set[str] = set()

    def __call__(self, messages: list[dict[str, str]], model: str, effort: str) -> dict[str, Any]:
        from deepseek_client import DeepSeekError
        from gaps import EVIDENCE_RULES
        if messages[0]["content"] != EVIDENCE_RULES:
            raise DeepSeekError("not part of this evaluation", reason="request_failed")
        if self.calls >= self.limit:
            raise DeepSeekError("call limit reached", reason="request_failed")
        self.calls += 1
        answer = self.chat(messages, model=model, effort=effort)
        for key, value in (answer.get("usage") or {}).items():
            if isinstance(value, int):
                self.usage[key] = self.usage.get(key, 0) + value
        if answer.get("model"):
            self.models.add(str(answer["model"]))
        return answer


def evidence_case(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """One requirement and a one-entry CV for an evidence sample, shaped as the app builds them."""
    from tenant_store import FactSnapshot
    facts = [{"id": f"fact-eval-{number}", "version": 1, "text": line, "fact_type": "experience", "tags": [],
              "status": "confirmed"} for number, line in enumerate(case["lines"], 1)]
    cv = {"language": "zh" if re.search(r"[一-鿿]", case["requirement"]) else "en",
          "header": {"name": "Sample Applicant", "details": [], "links": []},
          "sections": [{"kind": "experience", "title": "Experience", "entries": [
              {"title": "Example Company", "subtitle": "Software Engineer Intern", "location": None,
               "dates": case["dates"],
               "lines": [{"fact_id": fact["id"], "fact_version": 1, "text": fact["text"]} for fact in facts]}]}]}
    decided = {"jd": {"title": None},
               "selected_requirements": [{"id": "R1", "text": case["requirement"], "strength": "required"}]}
    return decided, cv, FactSnapshot(facts)


def baseline(case: dict[str, Any]) -> str:
    """The naive reading the model must beat: a shared word means the requirement is met."""
    words = {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z+#.]*", case["requirement"])
             if len(word) > 2 and word.casefold() not in GENERIC_WORDS}
    words |= {run[i:i + 2] for run in re.findall(r"[一-鿿]+", case["requirement"]) for i in range(len(run) - 1)}
    text = " ".join(case["lines"]).casefold()
    return "supported" if any(re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text) for word in words) else "none"


def live(samples: dict[str, Any], limit: int, chat: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """The evidence samples judged by the model; costs money with the real client."""
    from deepseek_client import DEFAULT_MODEL
    from gaps import EVIDENCE_RULES, find_gaps
    limited = EvidenceOnly(chat, limit)
    rows = []
    for case in samples["evidence"]:
        if limited.calls >= limit:
            verdict = "not_run"
        else:
            decided, cv, facts = evidence_case(case)
            verdict = find_gaps(decided, cv, facts, limited)["requirements"][0]["evidence"]
        rows.append({"id": case["id"], "category": case["category"], "expected": case["expected"],
                     "model": verdict, "baseline": baseline(case)})
    judged = [row for row in rows if row["model"] not in ("not_run", "unchecked")]
    return {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sample_version": samples["version"], "requested_model": DEFAULT_MODEL,
            "answered_by": sorted(limited.models),
            "prompt_sha256": hashlib.sha256(EVIDENCE_RULES.encode("utf-8")).hexdigest()[:16],
            "calls": limited.calls, "usage": limited.usage, "samples": len(rows), "judged": len(judged),
            "unchecked": [row["id"] for row in rows if row["model"] == "unchecked"],
            "model_agrees": sum(row["model"] == row["expected"] for row in judged),
            "baseline_agrees": sum(row["baseline"] == row["expected"] for row in judged),
            # the costly error: support claimed where the labels say there is none
            "model_overcredits": [row["id"] for row in judged
                                  if row["model"] == "supported" and row["expected"] != "supported"],
            "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("offline", help="the claim checks on the labelled rewrites (no model)")
    paid = commands.add_parser("live", help="the model on the evidence samples (paid; needs --confirm-paid)")
    paid.add_argument("--max-calls", type=int, required=True)
    paid.add_argument("--confirm-paid", action="store_true")
    paid.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    samples = load()
    if args.command == "offline":
        report = offline(samples)
        print(json.dumps(report, indent=1, ensure_ascii=False))
        return 1 if report["regressions"] else 0
    if not args.confirm_paid or not 0 < args.max_calls <= MAX_LIVE_CALLS:
        print(f"refused: a live run sends the synthetic samples to DeepSeek and is billed; it needs an authorized "
              f"budget, --confirm-paid and --max-calls from 1 to {MAX_LIVE_CALLS}", file=sys.stderr)
        return 2
    from deepseek_client import chat_json
    report = live(samples, args.max_calls, chat_json)
    args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
