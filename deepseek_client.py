"""Minimal DeepSeek chat client for JSON answers; the key comes from env or macOS Keychain.

A caller that keeps a ledger of model use (V2's task runner) passes a ``meter`` (see chat_json):
then every transport call, each HTTP attempt included, is begun, settled with the usage the
answer reports, or ended unknown, here at the transport boundary."""

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
# A conservative reservation heuristic: serialized UTF-8 bytes plus a chat-template margin.
# This is not the provider's tokenizer or a guaranteed upper bound. Reported usage, including
# any overage, replaces the estimate after the response arrives.
PROMPT_MARGIN_TOKENS = 256
MESSAGE_MARGIN_TOKENS = 16
log = logging.getLogger("workbench.deepseek")


class DeepSeekError(Exception):
    """A DeepSeek request or answer could not be used safely.

    ``reason`` says what kind of failure it was, so a page can explain it and suggest what to
    do: missing_key, key_rejected, rate_limited, unreachable, request_failed or bad_response,
    or (CallRefused) what a meter refused.
    """

    def __init__(self, message: str, reason: str = "bad_response") -> None:
        super().__init__(message)
        self.reason = reason


class CallRefused(DeepSeekError):
    """The caller's meter would not let this call start (a daily allowance, new work paused), so
    nothing was sent. Code that falls back when the model cannot answer falls back here too, and
    says why; ``detail`` holds the numbers the refusal was based on."""

    def __init__(self, message: str, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, reason=reason)
        self.detail = detail or {}


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate a conservative prompt reservation (not a measured token count)."""
    body = json.dumps(messages, ensure_ascii=False).encode("utf-8")
    return len(body) + MESSAGE_MARGIN_TOKENS * len(messages) + PROMPT_MARGIN_TOKENS


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


def _begin(meter: Any, payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Start one metered call: (the call, the payload to send, with the output budget the meter
    allows). Without a meter nothing is recorded and the payload goes as it is."""
    if meter is None:
        return None, payload
    call, output = meter.begin(estimate_prompt_tokens(payload["messages"]), payload["max_tokens"])
    return call, {**payload, "max_tokens": output}


def _settle(meter: Any, call: Any, usage: Any) -> None:
    if meter is not None:
        meter.settle(call, usage)


def _fail(meter: Any, call: Any, reason: str) -> None:
    if meter is not None:
        meter.fail(call, reason)


def _post(payload: dict[str, Any], api_key: str, meter: Any = None) -> dict[str, Any]:
    """Send one request, trying again (HTTP_ATTEMPTS in all) while the API is busy or unreachable.
    With ``meter`` (see chat_json) each attempt is its own call: begun just before it is sent,
    settled with the answer's reported usage before anything reads the answer, or ended unknown
    when no usage came back (an HTTP error, no answer, an unreadable body), since such an attempt
    may still have been billed. A refused begin stops here with nothing sent."""
    _checked(api_key)
    # Reject locally unserializable input before reserving a potentially billed attempt.
    json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        call, sent = _begin(meter, payload)
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(sent, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except ValueError:  # an invalid header; its message would quote the key
            _fail(meter, call, "invalid_request")
            raise DeepSeekError("DeepSeek API key 格式无效", reason="key_rejected") from None
        except urllib.error.HTTPError as exc:
            exc.close()
            _fail(meter, call, f"http_{exc.code}")
            if exc.code in RETRY_HTTP_CODES and attempt < HTTP_ATTEMPTS:
                run_log.event(log, "deepseek_retry", level=logging.WARNING, attempt=attempt, reason=f"http_{exc.code}")
                time.sleep(2 ** attempt)
                continue
            reason = {401: "key_rejected", 403: "key_rejected", 429: "rate_limited"}.get(exc.code, "request_failed")
            raise DeepSeekError(f"DeepSeek API 返回 HTTP {exc.code}", reason=reason) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            _fail(meter, call, "unreachable")
            if attempt < HTTP_ATTEMPTS:
                run_log.event(log, "deepseek_retry", level=logging.WARNING, attempt=attempt, reason="unreachable")
                time.sleep(2 ** attempt)
                continue
            raise DeepSeekError("无法连接 DeepSeek API", reason="unreachable") from exc
        except BaseException:  # anything else while sending or reading: the attempt may have been billed
            _fail(meter, call, "transport_error")
            raise
    if len(raw) > MAX_RESPONSE_BYTES:
        _fail(meter, call, "too_large")
        raise DeepSeekError("DeepSeek API 响应过大")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(meter, call, "unreadable")
        raise DeepSeekError("DeepSeek API 返回了无效 JSON") from exc
    if not isinstance(parsed, dict):
        _fail(meter, call, "unreadable")
        raise DeepSeekError("DeepSeek API 响应顶层必须是对象")
    _settle(meter, call, parsed.get("usage"))
    return parsed


_post.meters_calls = True  # takes ``meter`` and meters each HTTP attempt itself


def _metered(post: Callable[[dict[str, Any], str], dict[str, Any]], meter: Any) -> Callable[[dict[str, Any], str], dict[str, Any]]:
    """An injected transport (tests, offline evaluations), metered as one call per request."""

    def send(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        call, sent = _begin(meter, payload)
        try:
            response = post(sent, api_key)
        except BaseException as exc:
            _fail(meter, call, getattr(exc, "reason", "transport_error"))
            raise
        _settle(meter, call, response.get("usage") if isinstance(response, dict) else None)
        return response

    return send


def _transport(post: Callable[..., dict[str, Any]], meter: Any) -> Callable[[dict[str, Any], str], dict[str, Any]]:
    """How each request is sent: as it is without a meter; the built-in transport meters its own
    attempts (``meters_calls``); any other is metered once per request it answers."""
    if meter is None:
        return post
    if getattr(post, "meters_calls", False) is True:
        return lambda payload, key: post(payload, key, meter=meter)
    return _metered(post, meter)


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
    post: Callable[..., dict[str, Any]] = _post,
    temperature: float = 0,
    meter: Any = None,
) -> dict[str, Any]:
    """Ask for one JSON object; retry once on the documented empty-content case. Each call is
    logged with its time and token counts, or with the kind of failure; never what was asked
    or answered.

    Temperature 0 by default: every use here is a structured task (picking lines, matching,
    planning, checked rewording), and the same question should get the same answer. At the
    API default the same job gave 11 gaps in one run and 4 in the next (2026-09).

    ``meter`` records every transport call the request makes, once the key and the request have
    been checked here (a missing or malformed key makes none): ``meter.begin(prompt_tokens,
    max_output)`` just before a call is sent returns ``(call, output budget)`` (sent as
    max_tokens), or raises CallRefused and nothing is sent; ``meter.settle(call, usage)`` receives
    the answer's ``usage`` as reported, before the answer is read, so an answer that is then empty,
    cut off or not JSON is still counted; ``meter.fail(call, reason)`` ends a call that brought no
    usage back. Each HTTP attempt of the built-in transport is one call, as is each answer of an
    injected ``post``.
    """
    started = time.monotonic()
    try:
        result = _ask(messages, model, effort, api_key, post, temperature, meter)
    except DeepSeekError as exc:
        run_log.event(log, "deepseek_failed", level=logging.WARNING, reason=exc.reason,
                      duration_ms=run_log.elapsed_ms(started))
        raise
    run_log.event(log, "deepseek_call", model=model, effort=effort,  # the model asked for, not the answer's word
                  duration_ms=run_log.elapsed_ms(started), **result["usage"])
    return result


chat_json.meters_calls = True  # takes ``meter`` (task_runner.TaskContext passes one)


def _ask(
    messages: list[dict[str, str]],
    model: str,
    effort: str,
    api_key: str | None,
    post: Callable[..., dict[str, Any]],
    temperature: float,
    meter: Any = None,
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
    key = _checked(api_key) if api_key is not None else load_api_key()
    send = _transport(post, meter)
    for attempt in range(2):
        response = send(payload, key)
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
