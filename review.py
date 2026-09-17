"""Build a local, evidence-linked review draft from manually selected JD requirements."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def build_report(data: Any) -> dict[str, Any]:
    """Validate local input and return a draft; never assert semantic fit or approval."""
    if not isinstance(data, dict):
        raise ValueError("输入顶层必须是对象")

    jd = data.get("jd")
    if not isinstance(jd, dict):
        raise ValueError("jd 必须是对象")
    jd_text = _text(jd.get("text"), "jd.text")
    captured_at = _text(jd.get("captured_at"), "jd.captured_at")
    try:
        captured_time = datetime.fromisoformat(captured_at)
    except ValueError as exc:
        raise ValueError("jd.captured_at 必须是 ISO 8601 时间") from exc
    if captured_time.tzinfo is None or captured_time.utcoffset() is None:
        raise ValueError("jd.captured_at 必须包含时区")

    source = jd.get("source")
    if source is not None:
        source = _text(source, "jd.source")

    facts = data.get("facts")
    if not isinstance(facts, list):
        raise ValueError("facts 必须是数组")
    facts_by_id: dict[str, dict[str, Any]] = {}
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise ValueError(f"facts[{index}] 必须是对象")
        fact_id = _text(fact.get("id"), f"facts[{index}].id")
        fact_text = _text(fact.get("text"), f"facts[{index}].text")
        if not isinstance(fact.get("confirmed"), bool):
            raise ValueError(f"facts[{index}].confirmed 必须是布尔值")
        fact_version = fact.get("version")
        if fact_version is not None and (
            isinstance(fact_version, bool) or not isinstance(fact_version, int) or fact_version < 1
        ):
            raise ValueError(f"facts[{index}].version 必须是正整数")
        if fact_id in facts_by_id:
            raise ValueError(f"重复的事实 ID：{fact_id}")
        facts_by_id[fact_id] = {
            "text": fact_text,
            "confirmed": fact["confirmed"],
            "version": fact_version,
        }

    selections = data.get("selected_requirements")
    if not isinstance(selections, list):
        raise ValueError("selected_requirements 必须是数组")
    items: list[dict[str, Any]] = []
    seen_requirements: set[str] = set()
    for index, selection in enumerate(selections):
        if not isinstance(selection, dict):
            raise ValueError(f"selected_requirements[{index}] 必须是对象")
        requirement = _text(selection.get("text"), f"selected_requirements[{index}].text")
        if requirement not in jd_text:
            raise ValueError(f"要求不在 JD 原文中：{requirement}")
        if requirement in seen_requirements:
            raise ValueError(f"重复的岗位要求：{requirement}")
        seen_requirements.add(requirement)

        fact_id = selection.get("fact_id")
        if fact_id is None:
            items.append({
                "requirement_quote": requirement,
                "evidence_status": "未知",
                "fact_id": None,
                "fact_quote": None,
                "draft_suggestion": None,
            })
            continue
        fact_id = _text(fact_id, f"selected_requirements[{index}].fact_id")
        fact = facts_by_id.get(fact_id)
        if fact is None:
            raise ValueError(f"事实 ID 不存在：{fact_id}")
        if not fact["confirmed"]:
            raise ValueError(f"事实尚未确认：{fact_id}")
        selected_version = selection.get("fact_version")
        if fact["version"] is not None:
            if selected_version != fact["version"]:
                raise ValueError(f"事实版本不匹配：{fact_id}")
        elif selected_version is not None:
            raise ValueError(f"事实版本不匹配：{fact_id}")
        items.append({
            "requirement_quote": requirement,
            "evidence_status": "候选依据，语义待人工审核",
            "fact_id": fact_id,
            "fact_version": fact["version"],
            "fact_quote": fact["text"],
            "draft_suggestion": f"可考虑引用已确认事实：{fact['text']}",
        })

    return {
        "jd_source": source if source is not None else "未知",
        "jd_captured_at": captured_at,
        "official_post_status": "未知",
        "material_status": "待人工审核草稿" if items else "待选择岗位要求",
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="根据本地 JD 与事实生成待审核建议")
    parser.add_argument("input", type=Path, help="本地 JSON 输入文件")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        report = build_report(data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
