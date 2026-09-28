"""Retrieve bounded fact candidates for confirmed JD requirements and record human links."""

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

from deepseek_client import DEFAULT_MODEL as DEEPSEEK_MODEL, DeepSeekError
from facts import FactStoreError, find_confirmed_facts, list_facts, load_confirmed_fact
from review import build_report


MATCHING_METHOD = "versioned-tags-and-type-v1"
MODEL_MATCHING_METHOD = "deepseek-facts-v1"
MAX_MODEL_FACTS = 3
MATCH_RULES = """You match one job's requirements to an applicant's confirmed resume facts and reply in json only.
For each requirement, list the ids of facts whose own words show that the applicant meets it,
strongest first, at most 3. Being related is not enough. Years of experience, seniority or
leadership count only when the fact states them. Use an empty list when no fact shows it.
Reply as {"matches": [{"requirement": "<requirement id>", "facts": ["<fact id>", ...]}]}."""
REQUIREMENT_TYPE_PREFERENCES = {
    "skill_or_experience": ("project", "experience", "skill", "achievement"),
    "education_or_eligibility": ("education", "eligibility"),
    "availability": ("availability",),
    "responsibility": ("project", "experience", "skill"),
    "benefit_or_compensation": (),
    "application_process": (),
    "context_or_example": (),
}


class MatchingError(Exception):
    """A fact retrieval input or human matching decision is invalid."""


def _match_id(requirement_id: str, fact_id: str, fact_version: int) -> str:
    value = f"{requirement_id}\0{fact_id}\0{fact_version}"
    return "match-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _requirement_category(candidate: dict[str, Any]) -> str | None:
    judgment = candidate.get("semantic_judgment")
    category = judgment.get("category") if isinstance(judgment, dict) else None
    choice = category.get("choice") if isinstance(category, dict) else None
    return choice if choice in REQUIREMENT_TYPE_PREFERENCES else None


def _validated_requirements(data: dict[str, Any]) -> list[dict[str, Any]]:
    selections = data.get("selected_requirements")
    candidates = data.get("requirement_candidates")
    if not isinstance(selections, list) or not isinstance(candidates, list):
        raise MatchingError("输入必须先完成 requirement_flow.py decide")
    candidates_by_id = {
        item.get("id"): item for item in candidates
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if len(candidates_by_id) != len(candidates):
        raise MatchingError("候选要求 ID 缺失或重复")
    requirements = []
    seen: set[str] = set()
    for selection in selections:
        if not isinstance(selection, dict):
            raise MatchingError("selected_requirements 必须包含对象")
        requirement_id = selection.get("id")
        text = selection.get("text")
        if not isinstance(requirement_id, str) or not isinstance(text, str) or not text:
            raise MatchingError("已确认要求缺少 id 或 text")
        if requirement_id in seen:
            raise MatchingError(f"重复的已确认要求：{requirement_id}")
        seen.add(requirement_id)
        candidate = candidates_by_id.get(requirement_id)
        if candidate is None or candidate.get("status") != "confirmed" or candidate.get("text") != text:
            raise MatchingError(f"要求与已确认候选不一致：{requirement_id}")
        if selection.get("fact_id") is not None:
            raise MatchingError(f"要求已经关联事实：{requirement_id}")
        requirements.append({
            "id": requirement_id,
            "text": text,
            "category": _requirement_category(candidate),
        })
    return requirements


def _model_choices(
    requirements: list[dict[str, Any]], facts_db: Path, chat: Callable[..., dict], effort: str
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Ask DeepSeek which confirmed facts show each requirement is met.

    Only requirement texts and confirmed fact texts are sent. Unknown or pending fact IDs
    and unknown requirements in the answer are dropped, so the model can only point at
    facts the user confirmed.
    """
    confirmed = {fact["id"]: fact for fact in list_facts(facts_db) if fact["status"] == "confirmed"}
    request = {
        "requirements": [{"id": item["id"], "text": item["text"]} for item in requirements],
        "facts": [{"id": fact["id"], "text": fact["text"]} for fact in confirmed.values()],
    }
    reply = chat(
        [{"role": "system", "content": MATCH_RULES}, {"role": "user", "content": json.dumps(request, ensure_ascii=False)}],
        model=DEEPSEEK_MODEL,
        effort=effort,
    )
    picks = reply["content"].get("matches")
    if not isinstance(picks, list):
        raise MatchingError("DeepSeek 的回答缺少 matches 数组")
    wanted = {item["id"] for item in requirements}
    choices: dict[str, list[dict[str, Any]]] = {}
    for pick in picks:
        requirement_id = pick.get("requirement") if isinstance(pick, dict) else None
        fact_ids = pick.get("facts") if isinstance(pick, dict) else None
        if requirement_id not in wanted or requirement_id in choices or not isinstance(fact_ids, list):
            continue
        known = [fact_id for fact_id in dict.fromkeys(fact_ids) if isinstance(fact_id, str) and fact_id in confirmed]
        choices[requirement_id] = [
            {**confirmed[fact_id], "retrieval_basis": {"method": "deepseek", "rank": rank}}
            for rank, fact_id in enumerate(known[:MAX_MODEL_FACTS], 1)
        ]
    if requirements and not choices:
        raise MatchingError("DeepSeek 的回答没有对应任何要求")
    return choices, {"model": reply.get("model"), "usage": reply.get("usage")}


def propose_matches(
    data: Any,
    facts_db: Path,
    limit: int = 10,
    chat: Callable[..., dict] | None = None,
    effort: str = "none",
) -> dict[str, Any]:
    """Retrieve bounded local candidates without copying unrelated facts.

    With ``chat`` DeepSeek chooses up to three confirmed facts per requirement; if it fails,
    facts sharing tags with the requirement are offered instead and the reason is recorded.
    """
    if not isinstance(data, dict):
        raise MatchingError("输入顶层必须是对象")
    if data.get("facts") != []:
        raise MatchingError("匹配前 facts 必须为空；事实应由本阶段按需检索")
    if "match_candidates" in data or "fact_matching" in data:
        raise MatchingError("输入已经包含事实匹配结果")
    requirements = _validated_requirements(data)
    result = copy.deepcopy(data)
    choices: dict[str, list[dict[str, Any]]] | None = None
    details: dict[str, Any] = {}
    if chat is not None:
        try:
            choices, details = _model_choices(requirements, facts_db, chat, effort)
        except (DeepSeekError, MatchingError) as exc:
            details = {"fallback_reason": str(exc), "fallback_code": getattr(exc, "reason", "bad_response")}
    match_candidates = []
    requirement_summaries = []
    for requirement in requirements:
        if choices is not None:
            facts = choices.get(requirement["id"], [])
        else:
            preferred_types = REQUIREMENT_TYPE_PREFERENCES.get(requirement["category"], ())
            facts = find_confirmed_facts(
                facts_db, requirement["text"], preferred_types=preferred_types, limit=limit
            )
        requirement_summaries.append({
            "requirement_id": requirement["id"],
            "category_signal": requirement["category"],
            "candidate_count": len(facts),
            "status": "pending" if facts else "no_candidate_found",
        })
        for fact in facts:
            match_candidates.append({
                "id": _match_id(requirement["id"], fact["id"], fact["version"]),
                "requirement_id": requirement["id"],
                "requirement_text": requirement["text"],
                "fact_id": fact["id"],
                "fact_version": fact["version"],
                "fact_text": fact["text"],
                "fact_type": fact["fact_type"],
                "fact_tags": fact["tags"],
                "retrieval_basis": fact["retrieval_basis"],
                "status": "pending",
            })
    result["match_candidates"] = match_candidates
    result["fact_matching"] = {
        "method": MODEL_MATCHING_METHOD if choices is not None else MATCHING_METHOD,
        **details,
        "candidate_limit_per_requirement": MAX_MODEL_FACTS if choices is not None else limit,
        "requirements": requirement_summaries,
        "candidate_count": len(match_candidates),
    }
    return result


def _parse_links(values: list[str]) -> dict[str, str]:
    links: dict[str, str] = {}
    for value in values:
        requirement_id, separator, fact_id = value.partition("=")
        if not separator or not requirement_id or not fact_id:
            raise MatchingError("--link 格式应为 REQUIREMENT_ID=FACT_ID")
        if requirement_id in links:
            raise MatchingError(f"重复的要求匹配决定：{requirement_id}")
        links[requirement_id] = fact_id
    return links


def first_candidates(data: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    """The best-ranked proposed fact for each requirement, or no match when none was found."""
    links: dict[str, str] = {}
    no_matches: set[str] = set()
    for summary in data["fact_matching"]["requirements"]:
        requirement_id = summary["requirement_id"]
        first = next(
            (item for item in data["match_candidates"] if item["requirement_id"] == requirement_id), None
        )
        if first is None:
            no_matches.add(requirement_id)
        else:
            links[requirement_id] = first["fact_id"]
    return links, no_matches


def apply_match_decisions(
    data: Any,
    facts_db: Path,
    links: dict[str, str],
    no_matches: set[str],
    decided_by: str = "user",
) -> dict[str, Any]:
    """Bind decisions to the exact fact versions proposed earlier.

    ``decided_by`` records whether the user chose or the first candidate was taken ("auto").
    """
    if not isinstance(data, dict) or not isinstance(data.get("match_candidates"), list):
        raise MatchingError("输入缺少 match_candidates")
    if not links and not no_matches:
        raise MatchingError("至少需要一个 --link 或 --no-match")
    overlap = set(links) & no_matches
    if overlap:
        raise MatchingError(f"同一要求不能同时关联事实和标记无匹配：{sorted(overlap)[0]}")

    result = copy.deepcopy(data)
    selections = result.get("selected_requirements")
    if not isinstance(selections, list):
        raise MatchingError("输入缺少 selected_requirements")
    selections_by_id = {
        item.get("id"): item for item in selections
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    decision_ids = set(links) | no_matches
    unknown = decision_ids - set(selections_by_id)
    if unknown:
        raise MatchingError(f"已确认要求不存在：{sorted(unknown)[0]}")

    candidate_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in result["match_candidates"]:
        if not isinstance(candidate, dict):
            raise MatchingError("事实候选结构无效")
        key = (candidate.get("requirement_id"), candidate.get("fact_id"))
        if not all(isinstance(value, str) for value in key) or key in candidate_pairs:
            raise MatchingError("事实候选要求/事实组合无效或重复")
        candidate_pairs[key] = candidate

    for requirement_id, fact_id in links.items():
        candidate = candidate_pairs.get((requirement_id, fact_id))
        if candidate is None:
            raise MatchingError(f"事实不是该要求的候选：{requirement_id}={fact_id}")
        for item in result["match_candidates"]:
            if item.get("requirement_id") == requirement_id:
                item["status"] = "selected" if item.get("fact_id") == fact_id else "not_selected"

    for requirement_id in no_matches:
        selection = selections_by_id[requirement_id]
        selection["fact_id"] = None
        selection.pop("fact_version", None)
        for item in result["match_candidates"]:
            if item.get("requirement_id") == requirement_id:
                item["status"] = "not_selected"

    decisions = result.get("match_decisions", {})
    if not isinstance(decisions, dict) or not set(decisions).issubset(selections_by_id):
        raise MatchingError("已有 match_decisions 结构无效")
    for requirement_id, fact_id in links.items():
        decisions[requirement_id] = {"status": "linked", "fact_id": fact_id, "decided_by": decided_by}
    for requirement_id in no_matches:
        decisions[requirement_id] = {"status": "no_match", "fact_id": None, "decided_by": decided_by}
    result["match_decisions"] = decisions

    selected_facts: dict[tuple[str, int], dict[str, Any]] = {}
    for requirement_id, selection in selections_by_id.items():
        decision = decisions.get(requirement_id)
        if decision is None:
            selection["fact_id"] = None
            selection.pop("fact_version", None)
            continue
        if not isinstance(decision, dict) or decision.get("status") not in {"linked", "no_match"}:
            raise MatchingError(f"要求的匹配决定无效：{requirement_id}")
        if decision["status"] == "no_match":
            selection["fact_id"] = None
            selection.pop("fact_version", None)
            continue
        fact_id = decision.get("fact_id")
        candidate = candidate_pairs.get((requirement_id, fact_id))
        if candidate is None:
            raise MatchingError(f"已有匹配决定不再对应候选：{requirement_id}")
        fresh = load_confirmed_fact(facts_db, fact_id, candidate.get("fact_version"))
        if (
            fresh["text"] != candidate.get("fact_text")
            or fresh["fact_type"] != candidate.get("fact_type")
            or fresh["tags"] != candidate.get("fact_tags")
        ):
            raise MatchingError(f"事实候选内容已变化，请重新运行 propose：{fact_id}")
        selection["fact_id"] = fact_id
        selection["fact_version"] = fresh["version"]
        selected_facts[(fact_id, fresh["version"])] = {
            "id": fact_id,
            "version": fresh["version"],
            "text": fresh["text"],
            "fact_type": fresh["fact_type"],
            "tags": fresh["tags"],
            "confirmed": True,
        }
    result["facts"] = list(selected_facts.values())
    result["fact_matching"]["decision_counts"] = {
        "linked": sum(item.get("status") == "linked" for item in decisions.values()),
        "no_match": sum(item.get("status") == "no_match" for item in decisions.values()),
        "pending": len(selections_by_id) - len(decisions),
    }
    build_report(result)
    return result


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_new_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="为已确认 JD 要求检索事实候选并记录人工匹配")
    actions = parser.add_subparsers(dest="action", required=True)
    propose = actions.add_parser("propose", help="按分类和标签检索事实候选")
    propose.add_argument("input", type=Path)
    propose.add_argument("--facts-db", type=Path, required=True)
    propose.add_argument("--limit", type=int, default=10)
    propose.add_argument("--output", type=Path, required=True)
    decide = actions.add_parser("decide", help="关联事实或明确记录无匹配")
    decide.add_argument("input", type=Path)
    decide.add_argument("--facts-db", type=Path, required=True)
    decide.add_argument("--link", action="append", default=[], metavar="REQUIREMENT_ID=FACT_ID")
    decide.add_argument("--no-match", action="append", default=[], metavar="REQUIREMENT_ID")
    decide.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = _read_json(args.input)
        if args.action == "propose":
            result = propose_matches(data, args.facts_db, args.limit)
            summary = {
                "saved_to": str(args.output),
                "requirement_count": len(result["fact_matching"]["requirements"]),
                "candidate_count": len(result["match_candidates"]),
                "requirements": result["fact_matching"]["requirements"],
            }
        else:
            result = apply_match_decisions(
                data, args.facts_db, _parse_links(args.link), set(args.no_match)
            )
            summary = {
                "saved_to": str(args.output),
                "decision_counts": result["fact_matching"]["decision_counts"],
                "selected_fact_count": len(result["facts"]),
                "next_step": "运行 review.py 检查双侧原文和未知项",
            }
        _write_new_json(args.output, result)
    except (OSError, json.JSONDecodeError, FactStoreError, MatchingError, ValueError) as exc:
        print(f"事实匹配失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
