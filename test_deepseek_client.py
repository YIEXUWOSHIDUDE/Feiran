import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from deepseek_client import API_URL, DeepSeekError, _post, chat_json


MESSAGES = [
    {"role": "system", "content": "Answer in json."},
    {"role": "user", "content": "{}"},
]


def reply(content, finish_reason="stop"):
    return {
        "model": "deepseek-flash",
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


class RecordingPost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, payload, api_key):
        self.calls.append((payload, api_key))
        return self.responses.pop(0)


class DeepSeekClientTests(unittest.TestCase):
    def test_request_uses_json_mode_and_returns_parsed_content_with_usage(self):
        post = RecordingPost(reply(json.dumps({"lines": []})))
        result = chat_json(MESSAGES, api_key="test-key", post=post)
        payload, api_key = post.calls[0]
        self.assertEqual(api_key, "test-key")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["reasoning_effort"], "low")
        # Structured tasks: the same question should get the same answer each time.
        self.assertEqual(payload["temperature"], 0)
        self.assertIn("max_tokens", payload)
        self.assertEqual(result["content"], {"lines": []})
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertEqual(result["usage"], {"prompt_tokens": 120, "completion_tokens": 30})

    def test_empty_content_is_retried_once_then_reported(self):
        recovered = chat_json(MESSAGES, api_key="k", post=RecordingPost(reply(""), reply('{"ok": true}')))
        self.assertEqual(recovered["content"], {"ok": True})
        with self.assertRaisesRegex(DeepSeekError, "空内容"):
            chat_json(MESSAGES, api_key="k", post=RecordingPost(reply(""), reply("  ")))

    def test_truncated_or_non_json_answers_fail(self):
        with self.assertRaisesRegex(DeepSeekError, "截断"):
            chat_json(MESSAGES, api_key="k", post=RecordingPost(reply('{"lines": [', "length")))
        with self.assertRaisesRegex(DeepSeekError, "JSON"):
            chat_json(MESSAGES, api_key="k", post=RecordingPost(reply("not json")))

    def test_missing_key_fails_before_any_request(self):
        post = RecordingPost()
        with patch.dict(os.environ, {}, clear=True), patch("deepseek_client.sys.platform", "linux"):
            with self.assertRaisesRegex(DeepSeekError, "DEEPSEEK_API_KEY"):
                chat_json(MESSAGES, post=post)
        self.assertEqual(post.calls, [])

    @patch("deepseek_client.time.sleep")
    @patch("deepseek_client.urllib.request.urlopen")
    def test_http_errors_are_retried_and_never_show_the_key(self, urlopen, _sleep):
        overloaded = HTTPError(API_URL, 429, "busy", None, None)
        unauthorized = HTTPError(API_URL, 401, "no", None, None)
        urlopen.side_effect = [overloaded, unauthorized]
        with self.assertRaises(DeepSeekError) as raised:
            _post({"model": "deepseek-flash"}, "sk-secret-test-key")
        self.assertIn("401", str(raised.exception))
        self.assertNotIn("sk-secret-test-key", str(raised.exception))
        self.assertEqual(urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
