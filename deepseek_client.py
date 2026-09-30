"""Minimal DeepSeek chat client for JSON answers; the key comes from env or macOS Keychain."""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import run_log


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
log = logging.getLogger("workbench.deepseek")


class DeepSeekError(Exception):
    """A DeepSeek request or answer could not be used safely.

    ``reason`` says what kind of failure it was, so a page can explain it and suggest what to
    do: missing_key, key_rejected, rate_limited, unreachable, request_failed or bad_response.
    """

    def __init__(self, message: str, reason: str = "bad_response") -> None:
        super().__init__(message)
        self.reason = reason


def _checked(key: str) -> str:
    """A key is printable ASCII without spaces; anything else would fail inside the HTTP
    library with the key in its message, so it is refused here without quoting it."""
    if not re.fullmatch(r"[\x21-\x7e]+", key):
        raise DeepSeekError("DeepSeek API key 格式无效（含空格、换行或非 ASCII 字符）", reason="key_rejected")
    return key


def load_api_key() -> str:
    """Read DEEPSEEK_API_KEY, the file DEEPSEEK_API_KEY_FILE names (how a server hands the key
    over without putting it in the environment), or the macOS Keychain item the user stored it
    in. A named file that cannot be read is an error, never a reason to look elsewhere."""
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return _checked(key)
    key_file = (os.environ.get("DEEPSEEK_API_KEY_FILE") or "").strip()
    if key_file:
        try:
            key = Path(key_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            key = ""
        if key:
            return _checked(key)
        raise DeepSeekError("DEEPSEEK_API_KEY_FILE 指定的文件不存在、无法读取或为空", reason="missing_key")
    if sys.platform == "darwin":
        try:
            found = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            found = None
        if found is not None and found.returncode == 0 and found.stdout.strip():
            return _checked(found.stdout.strip())
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
        except ValueError:  # an invalid header; its message would quote the key
            raise DeepSeekError("DeepSeek API key 格式无效", reason="key_rejected") from None
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in RETRY_HTTP_CODES and attempt < HTTP_ATTEMPTS:
                run_log.event(log, "deepseek_retry", level=logging.WARNING, attempt=attempt, reason=f"http_{exc.code}")
                time.sleep(2 ** attempt)
                continue
            reason = {401: "key_rejected", 403: "key_rejected", 429: "rate_limited"}.get(exc.code, "request_failed")
            raise DeepSeekError(f"DeepSeek API 返回 HTTP {exc.code}", reason=reason) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            if attempt < HTTP_ATTEMPTS:
                run_log.event(log, "deepseek_retry", level=logging.WARNING, attempt=attempt, reason="unreachable")
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
    """Ask for one JSON object; retry once on the documented empty-content case. Each call is
    logged with its time and token counts, or with the kind of failure; never what was asked
    or answered.

    Temperature 0 by default: every use here is a structured task (picking lines, matching,
    planning, checked rewording), and the same question should get the same answer. At the
    API default the same job gave 11 gaps in one run and 4 in the next (2026-09).
    """
    started = time.monotonic()
    try:
        result = _ask(messages, model, effort, api_key, post, temperature)
    except DeepSeekError as exc:
        run_log.event(log, "deepseek_failed", level=logging.WARNING, reason=exc.reason,
                      duration_ms=run_log.elapsed_ms(started))
        raise
    run_log.event(log, "deepseek_call", model=model, effort=effort,  # the model asked for, not the answer's word
                  duration_ms=run_log.elapsed_ms(started), **result["usage"])
    return result


def _ask(
    messages: list[dict[str, str]],
    model: str,
    effort: str,
    api_key: str | None,
    post: Callable[[dict[str, Any], str], dict[str, Any]],
    temperature: float,
) -> dict[str, Any]:
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
        run_log.event(log, "deepseek_retry", level=logging.WARNING, attempt=1, reason="empty_content")
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
