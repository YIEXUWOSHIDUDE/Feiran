"""Optional TypeSafe/Jev judgments for exact JD requirement candidates."""

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable


API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
CATEGORIES = (
    "skill_or_experience",
    "education_or_eligibility",
    "availability",
    "responsibility",
    "benefit_or_compensation",
    "application_process",
    "context_or_example",
)
STRENGTHS = ("required", "preferred", "unclear_or_not_requirement")


class TypeSafeError(Exception):
    """A TypeSafe request or response could not be used safely."""


def build_request(
    jd_text: str,
    candidates: list[dict[str, Any]],
    model: str = DEFAULT_MODEL,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Build one parallel TypeSafe request and a local answer-ID mapping."""
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise TypeSafeError("JD 原文必须是非空字符串")
    if not isinstance(model, str) or not model.strip():
        raise TypeSafeError("TypeSafe model 必须是非空字符串")

    state_candidates: list[dict[str, str]] = []
    questions: dict[str, dict[str, Any]] = {}
    answer_ids: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise TypeSafeError("候选要求必须是对象")
        candidate_id = candidate.get("id")
        text = candidate.get("text")
        section = candidate.get("section")
        if not all(isinstance(value, str) and value for value in (candidate_id, text, section)):
            raise TypeSafeError("候选要求缺少 id、text 或 section")
        if candidate_id in seen:
            raise TypeSafeError(f"候选要求 ID 重复：{candidate_id}")
        if text not in jd_text:
            raise TypeSafeError(f"候选要求不是 JD 原文片段：{candidate_id}")
        seen.add(candidate_id)
        state_candidates.append({"id": candidate_id, "text": text, "section": section})

        path = f"candidate_requirements[{index}]"
        ids = {
            "is_requirement": f"candidate_{index}_is_requirement",
            "category": f"candidate_{index}_category",
            "strength": f"candidate_{index}_strength",
        }
        answer_ids[candidate_id] = ids
        questions.update({
            ids["is_requirement"]: {
                "type": "noul",
                "instructions": (
                    f"Judge whether `{path}.text`, in its JD context and section, states a condition "
                    "the applicant is expected to meet. Answer no for employer descriptions, job "
                    "responsibilities alone, benefits or compensation, application instructions, "
                    "dates alone, and examples of past company work."
                ),
            },
            ids["category"]: {
                "type": "choice",
                "instructions": f"Classify the primary role of `{path}.text` in this job description.",
                "criteria": {
                    "skill_or_experience": "Applicant skill, tool knowledge, domain knowledge, or prior experience.",
                    "education_or_eligibility": "Degree, enrollment, work authorization, location, or other eligibility condition.",
                    "availability": "Start date, schedule, duration, or time commitment expected from the applicant.",
                    "responsibility": "Work the hired person would perform, without itself stating an applicant qualification.",
                    "benefit_or_compensation": "Pay, perks, benefits, or what the employer provides.",
                    "application_process": "How or when to apply, application materials, or recruiting process details.",
                    "context_or_example": "Company context, project example, descriptive background, or anything not covered above.",
                },
            },
            ids["strength"]: {
                "type": "choice",
                "instructions": f"Classify the qualification strength expressed by `{path}.text` in context.",
                "criteria": {
                    "required": "The applicant must or is expected to meet it; includes minimum or basic qualifications.",
                    "preferred": "The employer marks it as preferred, desired, a plus, or nice to have.",
                    "unclear_or_not_requirement": "The strength is unclear, or the text is not an applicant qualification.",
                },
            },
        })

    return ({
        "state": {
            "job_description": jd_text,
            "candidate_requirements": state_candidates,
        },
        "model": model,
        "questions": questions,
    }, answer_ids)


def _post_json(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    raw: bytes | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 529} and attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise TypeSafeError(f"TypeSafe API 返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise TypeSafeError("无法连接 TypeSafe API") from exc
    if raw is None:
        raise TypeSafeError("TypeSafe API 请求未完成")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TypeSafeError("TypeSafe API 响应过大")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TypeSafeError("TypeSafe API 返回了无效 JSON") from exc
    if not isinstance(parsed, dict):
        raise TypeSafeError("TypeSafe API 响应顶层必须是对象")
    return parsed


def _probability(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise TypeSafeError(f"TypeSafe 响应中的 {label} 必须是 0 到 1 的数字")
    return float(value)


def _choice_answer(answer: Any, allowed: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise TypeSafeError(f"TypeSafe 响应缺少 {label} Choice")
    choice = answer.get("choice")
    if choice not in allowed:
        raise TypeSafeError(f"TypeSafe 响应中的 {label} 选项无效")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(allowed):
        raise TypeSafeError(f"TypeSafe 响应中的 {label} 概率分布无效")
    parsed_probabilities = {
        option: _probability(probabilities[option], f"{label}.{option}") for option in allowed
    }
    if abs(sum(parsed_probabilities.values()) - 1.0) > 1e-5:
        raise TypeSafeError(f"TypeSafe 响应中的 {label} 概率之和必须为 1")
    return {
        "choice": choice,
        "confidence": _probability(answer.get("confidence"), f"{label}.confidence"),
        "probabilities": parsed_probabilities,
    }


def classify_requirement_candidates(
    jd_text: str,
    candidates: list[dict[str, Any]],
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    post_json: Callable[[dict[str, Any], str], dict[str, Any]] = _post_json,
) -> dict[str, Any]:
    """Return typed semantic judgments without changing candidate decisions."""
    request_payload, answer_ids = build_request(jd_text, candidates, model)
    if not candidates:
        return {
            "provider": "typesafe",
            "requested_model": model,
            "resolved_model": None,
            "usage": None,
            "judgments": {},
        }
    resolved_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY")
    if not resolved_key:
        raise TypeSafeError("缺少 TYPESAFE_API_KEY 环境变量")

    response = post_json(request_payload, resolved_key)
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise TypeSafeError("TypeSafe 响应缺少 answers 对象")
    expected_answer_ids = {answer_id for ids in answer_ids.values() for answer_id in ids.values()}
    if set(answers) != expected_answer_ids:
        raise TypeSafeError("TypeSafe 响应的问题 ID 与请求不一致")
    resolved_model = response.get("model")
    if not isinstance(resolved_model, str) or not resolved_model:
        raise TypeSafeError("TypeSafe 响应缺少已解析 model")
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(
        isinstance(usage.get(name), bool)
        or not isinstance(usage.get(name), int)
        or usage[name] < 0
        for name in ("input_tokens", "output_tokens")
    ):
        raise TypeSafeError("TypeSafe 响应缺少有效 usage")

    judgments: dict[str, Any] = {}
    for candidate_id, ids in answer_ids.items():
        requirement = answers.get(ids["is_requirement"])
        if not isinstance(requirement, dict) or requirement.get("type") != "noul":
            raise TypeSafeError(f"TypeSafe 响应缺少候选 {candidate_id} 的 Noul")
        judgments[candidate_id] = {
            "is_requirement_probability": _probability(
                requirement.get("noul"), f"{candidate_id}.is_requirement"
            ),
            "category": _choice_answer(answers.get(ids["category"]), CATEGORIES, "category"),
            "strength": _choice_answer(answers.get(ids["strength"]), STRENGTHS, "strength"),
        }

    return {
        "provider": "typesafe",
        "requested_model": model,
        "resolved_model": resolved_model,
        "usage": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        },
        "judgments": judgments,
    }
