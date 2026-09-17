"""Extract exact JD requirement candidates and record explicit user decisions."""

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from review import build_report
from typesafe_classifier import DEFAULT_MODEL, TypeSafeError, classify_requirement_candidates


EXTRACTION_METHOD = "section-lines-v2"
MANUAL_METHOD = "manual-quote-v1"
TARGET_HEADINGS = {
    "requirements",
    "qualifications",
    "minimum qualifications",
    "preferred qualifications",
    "basic qualifications",
    "what we're looking for",
    "what you bring",
    "who you are",
    "about you",
    "skills and experience",
    "minimum requirements",
    "basic requirements",
    "required qualifications",
    "desired qualifications",
    "additional qualifications",
    "key qualifications",
    "requirements and qualifications",
    "qualifications and requirements",
    "skills and qualifications",
    "required skills",
    "preferred skills",
    "desired skills",
    "skills",
    "nice to have",
    "nice to haves",
    "nice-to-have",
    "nice-to-haves",
    "bonus points",
    "you have",
    "you should have",
    "what you'll need",
    "what you need",
    "what you'll bring",
    "who we're looking for",
    "must have",
    "must haves",
    "must-haves",
    "ideal candidate",
    "the ideal candidate",
    "your profile",
    "your qualifications",
    "eligibility requirements",
    "岗位要求",
    "任职要求",
    "职位要求",
    "任职资格",
    "岗位资格",
    "资格要求",
    "技能要求",
    "能力要求",
    "基本要求",
    "申请要求",
    "应聘要求",
    "岗位基本需求",
    "我们希望你",
    "你需要具备",
    "加分项",
    "优先条件",
    "具备以下条件者优先",
    "具备以下者优先",
}
STOP_HEADINGS = {
    "about the role",
    "about us",
    "about the company",
    "benefits",
    "compensation",
    "equal opportunity",
    "our mission",
    "our values",
    "responsibilities",
    "the role",
    "what you'll do",
    "what we offer",
    "what you can expect",
    "why you'll love working here",
    "past projects include",
    "hourly range for this internship is",
    "about the team",
    "about the job",
    "job description",
    "overview",
    "key responsibilities",
    "your responsibilities",
    "what you will do",
    "day to day",
    "perks",
    "perks and benefits",
    "benefits and perks",
    "salary",
    "pay range",
    "location",
    "how to apply",
    "application process",
    "who we are",
    "the team",
    "our team",
    "岗位职责",
    "工作职责",
    "职位描述",
    "岗位描述",
    "工作内容",
    "职责描述",
    "公司介绍",
    "公司简介",
    "关于我们",
    "团队介绍",
    "福利待遇",
    "薪资福利",
    "薪酬福利",
    "我们提供",
    "工作地点",
    "投递方式",
    "申请方式",
}
KNOWN_HEADINGS = TARGET_HEADINGS | STOP_HEADINGS
LIST_MARKER = re.compile(
    r"^(?:\d+[.)]\s+"
    r"|\d+\.(?=[^\x00-\x7f])"
    r"|\d+[、．]\s*"
    r"|[（(]\d+[）)]\s*"
    r"|[一二三四五六七八九十]+[、．]\s*)"
)
HEADING_NUMBER = re.compile(r"^(?:\d+|[一二三四五六七八九十]+)[.)）、．]\s*")
INLINE_HEADING = re.compile(r"^([^:：]{1,60})[:：](.*)$")


class RequirementError(Exception):
    """An invalid extraction input or decision."""


def _heading_key(line: str) -> str:
    """Normalize "**1. Requirements:**", "【岗位要求】" or "二、任职要求：" to a set key."""
    value = re.sub(r"^[\W_]+", "", line.strip())
    value = HEADING_NUMBER.sub("", value)
    value = re.sub(r"^[\W_]+|[\W_]+$", "", value)
    value = value.replace("’", "'").replace("&", "and")
    return re.sub(r"\s+", " ", value).casefold()


def _candidate_text(line: str) -> str:
    value = re.sub(r"^[\s>*•●▪◦-]+", "", line.strip())
    value = LIST_MARKER.sub("", value)
    return value.strip()


def _text_weight(text: str) -> int:
    # A Chinese requirement such as "本科及以上学历" is complete in seven characters.
    return sum(1 if character.isascii() else 2 for character in text)


def _candidate_id(text: str) -> str:
    digest = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:10]
    return f"req-{digest}"


def extract_requirement_candidates(jd_text: str) -> list[dict[str, Any]]:
    """Extract exact non-heading lines under known requirement sections."""
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise RequirementError("jd.text 必须是非空字符串")
    active_section: str | None = None
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for line in jd_text.splitlines():
        heading = _heading_key(line)
        if heading in KNOWN_HEADINGS:
            active_section = line.strip().rstrip(":：") if heading in TARGET_HEADINGS else None
            continue
        inline = INLINE_HEADING.match(line.strip())
        inline_heading = _heading_key(inline.group(1)) if inline else ""
        if inline_heading in KNOWN_HEADINGS:
            active_section = inline.group(1).strip() if inline_heading in TARGET_HEADINGS else None
            line = inline.group(2)
        if active_section is None:
            continue
        text = _candidate_text(line)
        if not text or _text_weight(text) < 8 or len(text) > 500:
            continue
        if text not in jd_text:
            raise RequirementError("提取结果不是 JD 原文片段")
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "id": _candidate_id(text),
            "text": text,
            "section": active_section,
            "status": "pending",
            "fact_id": None,
            "extraction_method": EXTRACTION_METHOD,
        })
    return candidates


def propose_requirements(data: Any) -> dict[str, Any]:
    """Add pending candidates to a fresh review input without selecting them."""
    if not isinstance(data, dict):
        raise RequirementError("输入顶层必须是对象")
    jd = data.get("jd")
    if not isinstance(jd, dict):
        raise RequirementError("jd 必须是对象")
    selections = data.get("selected_requirements")
    if selections != []:
        raise RequirementError("提取候选要求前，selected_requirements 必须为空数组")
    if "requirement_candidates" in data:
        raise RequirementError("输入已经包含 requirement_candidates")
    build_report(data)
    candidates = extract_requirement_candidates(jd.get("text"))
    result = copy.deepcopy(data)
    result["requirement_candidates"] = candidates
    result["requirement_extraction"] = {
        "method": EXTRACTION_METHOD,
        "status": "candidates_found" if candidates else "none_found",
        "candidate_count": len(candidates),
    }
    return result


def attach_semantic_judgments(
    proposal: dict[str, Any],
    classification: dict[str, Any],
) -> dict[str, Any]:
    """Attach advisory judgments while preserving every human decision as pending."""
    result = copy.deepcopy(proposal)
    candidates = result.get("requirement_candidates")
    judgments = classification.get("judgments")
    if not isinstance(candidates, list) or not isinstance(judgments, dict):
        raise RequirementError("TypeSafe 分类结果结构无效")
    candidate_ids = {candidate.get("id") for candidate in candidates if isinstance(candidate, dict)}
    if set(judgments) != candidate_ids:
        raise RequirementError("TypeSafe 分类结果与候选要求不一致")
    for candidate in candidates:
        candidate["semantic_judgment"] = judgments[candidate["id"]]
        candidate["status"] = "pending"
        candidate["fact_id"] = None
    extraction = result.get("requirement_extraction")
    if not isinstance(extraction, dict):
        raise RequirementError("输入缺少 requirement_extraction 对象")
    extraction["semantic_classifier"] = {
        key: classification.get(key)
        for key in ("provider", "requested_model", "resolved_model", "usage")
    }
    return result


def add_manual_requirements(data: Any, texts: list[str]) -> dict[str, Any]:
    """Add exact JD quotes that the section rules missed; they still start as pending."""
    if not isinstance(data, dict) or not isinstance(data.get("requirement_candidates"), list):
        raise RequirementError("输入缺少 requirement_candidates；请先运行 propose")
    if "match_candidates" in data or "fact_matching" in data:
        raise RequirementError("该文件已进入事实匹配阶段；请在 decide 输出、matching 之前添加要求")
    jd = data.get("jd")
    if not isinstance(jd, dict) or not isinstance(jd.get("text"), str):
        raise RequirementError("输入缺少 jd.text")
    result = copy.deepcopy(data)
    jd_text = jd["text"]
    existing_ids = {
        item.get("id") for item in result["requirement_candidates"] if isinstance(item, dict)
    }
    for value in texts:
        text = _candidate_text(value)
        if not text or text not in jd_text:
            raise RequirementError(f"手动要求不是 JD 原文片段：{value}")
        candidate_id = _candidate_id(text)
        if candidate_id in existing_ids:
            raise RequirementError(f"候选要求已存在：{candidate_id}")
        existing_ids.add(candidate_id)
        result["requirement_candidates"].append({
            "id": candidate_id,
            "text": text,
            "section": None,
            "status": "pending",
            "fact_id": None,
            "extraction_method": MANUAL_METHOD,
        })
    return result


def _parse_confirmations(values: list[str]) -> set[str]:
    confirmations: set[str] = set()
    for value in values:
        if not value or "=" in value:
            raise RequirementError("--confirm 只接受候选 ID；事实关联由 matching.py 处理")
        if value in confirmations:
            raise RequirementError(f"重复确认候选要求：{value}")
        confirmations.add(value)
    return confirmations


def apply_requirement_decisions(
    data: Any,
    confirmations: set[str],
    exclusions: set[str],
) -> dict[str, Any]:
    """Apply explicit decisions and rebuild selected requirements from confirmed candidates."""
    if not isinstance(data, dict) or not isinstance(data.get("requirement_candidates"), list):
        raise RequirementError("输入缺少 requirement_candidates 数组")
    if not confirmations and not exclusions:
        raise RequirementError("至少需要一个 --confirm 或 --exclude")
    overlap = set(confirmations) & exclusions
    if overlap:
        raise RequirementError(f"同一候选要求不能同时确认和排除：{sorted(overlap)[0]}")

    result = copy.deepcopy(data)
    candidates = result["requirement_candidates"]
    candidates_by_id: dict[str, dict[str, Any]] = {}
    jd_text = result.get("jd", {}).get("text") if isinstance(result.get("jd"), dict) else None
    if not isinstance(jd_text, str):
        raise RequirementError("输入缺少 jd.text")
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
            raise RequirementError("候选要求缺少 ID")
        candidate_id = candidate["id"]
        if candidate_id in candidates_by_id:
            raise RequirementError(f"重复的候选要求 ID：{candidate_id}")
        if not isinstance(candidate.get("text"), str) or candidate["text"] not in jd_text:
            raise RequirementError(f"候选要求不是 JD 原文片段：{candidate_id}")
        if candidate_id != _candidate_id(candidate["text"]):
            raise RequirementError(f"候选要求 ID 与原文不匹配：{candidate_id}")
        if candidate.get("status") not in {"pending", "confirmed", "excluded"}:
            raise RequirementError(f"候选要求状态无效：{candidate_id}")
        candidates_by_id[candidate_id] = candidate

    unknown = (set(confirmations) | exclusions) - set(candidates_by_id)
    if unknown:
        raise RequirementError(f"候选要求 ID 不存在：{sorted(unknown)[0]}")
    for candidate_id in confirmations:
        candidate = candidates_by_id[candidate_id]
        candidate["status"] = "confirmed"
        candidate["fact_id"] = None
    for candidate_id in exclusions:
        candidate = candidates_by_id[candidate_id]
        candidate["status"] = "excluded"
        candidate["fact_id"] = None

    result["selected_requirements"] = [
        {"id": candidate["id"], "text": candidate["text"], "fact_id": None}
        for candidate in candidates
        if candidate.get("status") == "confirmed"
    ]
    extraction = result.get("requirement_extraction")
    if not isinstance(extraction, dict):
        raise RequirementError("输入缺少 requirement_extraction 对象")
    extraction["decision_counts"] = {
        "confirmed": sum(candidate.get("status") == "confirmed" for candidate in candidates),
        "excluded": sum(candidate.get("status") == "excluded" for candidate in candidates),
        "pending": sum(candidate.get("status") == "pending" for candidate in candidates),
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
    parser = argparse.ArgumentParser(description="从 JD 原文提取候选要求并记录人工决定")
    actions = parser.add_subparsers(dest="action", required=True)
    propose = actions.add_parser("propose", help="提取待确认的候选要求")
    propose.add_argument("input", type=Path)
    propose.add_argument("--output", type=Path, required=True)
    propose.add_argument("--typesafe", action="store_true", help="用 TypeSafe/Jev 标注候选要求")
    propose.add_argument("--typesafe-model", default=DEFAULT_MODEL, help="TypeSafe 模型或别名")
    add = actions.add_parser("add", help="手动添加规则漏掉的 JD 原文要求（仍为 pending）")
    add.add_argument("input", type=Path)
    add.add_argument("--text", action="append", required=True, metavar="QUOTE", help="JD 中的确切原文，可重复")
    add.add_argument("--output", type=Path, required=True)
    decide = actions.add_parser("decide", help="确认或排除候选要求")
    decide.add_argument("input", type=Path)
    decide.add_argument("--confirm", action="append", default=[], metavar="ID")
    decide.add_argument("--exclude", action="append", default=[], metavar="ID")
    decide.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = _read_json(args.input)
        if args.action == "propose":
            result = propose_requirements(data)
            if args.typesafe:
                classification = classify_requirement_candidates(
                    result["jd"]["text"],
                    result["requirement_candidates"],
                    model=args.typesafe_model,
                )
                result = attach_semantic_judgments(result, classification)
            summary = {
                "saved_to": str(args.output),
                "extraction_status": result["requirement_extraction"]["status"],
                "candidate_count": len(result["requirement_candidates"]),
                "candidates": [
                    {
                        "id": item["id"],
                        "section": item["section"],
                        "text": item["text"],
                        **({"semantic_judgment": item["semantic_judgment"]} if "semantic_judgment" in item else {}),
                    }
                    for item in result["requirement_candidates"]
                ],
            }
        elif args.action == "add":
            result = add_manual_requirements(data, args.text)
            added = result["requirement_candidates"][len(data["requirement_candidates"]):]
            summary = {
                "saved_to": str(args.output),
                "added": [{"id": item["id"], "text": item["text"]} for item in added],
                "candidate_count": len(result["requirement_candidates"]),
                "next_step": "运行 requirement_flow.py decide 确认或排除这些候选",
            }
        else:
            confirmations = _parse_confirmations(args.confirm)
            result = apply_requirement_decisions(data, confirmations, set(args.exclude))
            summary = {
                "saved_to": str(args.output),
                "decision_counts": result["requirement_extraction"]["decision_counts"],
                "selected_requirement_count": len(result["selected_requirements"]),
                "next_step": "运行 matching.py 为已确认要求检索候选事实",
            }
        _write_new_json(args.output, result)
    except (OSError, json.JSONDecodeError, RequirementError, TypeSafeError, ValueError) as exc:
        print(f"要求处理失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
