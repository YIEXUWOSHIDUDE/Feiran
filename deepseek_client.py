"""Minimal DeepSeek chat client for JSON answers; the key comes from env or macOS Keychain."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable


API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_EFFORT = "low"
EFFORTS = ("none", "low", "high", "max")
KEYCHAIN_SERVICE = "deepseek-api-key"
MAX_TOKENS = 8192
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 120
HTTP_ATTEMPTS = 3
RETRY_HTTP_CODES = {429, 500, 502, 503}


class DeepSeekError(Exception):
    """A DeepSeek request or answer could not be used safely.

    ``reason`` says what kind of failure it was, so a page can explain it and suggest what to
    do: missing_key, key_rejected, rate_limited, unreachable, request_failed or bad_response.
    """

    def __init__(self, message: str, reason: str = "bad_response") -> None:
        super().__init__(message)
        self.reason = reason


def load_api_key() -> str:
    """Read DEEPSEEK_API_KEY, or the macOS Keychain item the user stored it in."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    if sys.platform == "darwin":
        try:
            found = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            found = None
        if found is not None and found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    raise DeepSeekError(
        f"缺少 DeepSeek API key：设置 DEEPSEEK_API_KEY，或存入钥匙串 service {KEYCHAIN_SERVICE}",
        reason="missing_key",
    )


def _post(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in RETRY_HTTP_CODES and attempt < HTTP_ATTEMPTS:
                time.sleep(2 ** attempt)
                continue
            reason = {401: "key_rejected", 403: "key_rejected", 429: "rate_limited"}.get(exc.code, "request_failed")
            raise DeepSeekError(f"DeepSeek API 返回 HTTP {exc.code}", reason=reason) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            if attempt < HTTP_ATTEMPTS:
                time.sleep(2 ** attempt)
                continue
            raise DeepSeekError("无法连接 DeepSeek API", reason="unreachable") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DeepSeekError("DeepSeek API 响应过大")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeepSeekError("DeepSeek API 返回了无效 JSON") from exc
    if not isinstance(parsed, dict):
        raise DeepSeekError("DeepSeek API 响应顶层必须是对象")
    return parsed


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise DeepSeekError("DeepSeek 响应缺少 usage")
    usage = {}
    for name in ("prompt_tokens", "completion_tokens"):
        count = value.get(name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise DeepSeekError(f"DeepSeek 响应中的 usage.{name} 无效")
        usage[name] = count
    return usage


def chat_json(
    messages: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    api_key: str | None = None,
    post: Callable[[dict[str, Any], str], dict[str, Any]] = _post,
    temperature: float = 0,
) -> dict[str, Any]:
    """Ask for one JSON object; retry once on the documented empty-content case.

    Temperature 0 by default: every use here is a structured task (picking lines, matching,
    planning, checked rewording), and the same question should get the same answer. At the
    API default the same job gave 11 gaps in one run and 4 in the next (2026-09).
    """
    if effort not in EFFORTS:
        raise DeepSeekError(f"reasoning effort 必须是：{', '.join(EFFORTS)}")
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "reasoning_effort": effort,
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    key = api_key if api_key is not None else load_api_key()
    for attempt in range(2):
        response = post(payload, key)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise DeepSeekError("DeepSeek 响应缺少 choices")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise DeepSeekError("DeepSeek 回答被截断")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            break
        if attempt == 1:
            raise DeepSeekError("DeepSeek 连续返回空内容")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DeepSeekError("DeepSeek 回答不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise DeepSeekError("DeepSeek 回答必须是 JSON 对象")
    resolved = response.get("model")
    return {
        "model": resolved if isinstance(resolved, str) and resolved else model,
        "content": parsed,
        "usage": _usage(response.get("usage")),
    }
