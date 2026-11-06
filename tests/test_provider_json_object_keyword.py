import copy
import unittest

from test_provider import _install_fake_astrbot_runtime

_install_fake_astrbot_runtime()

from oauth_plug_openai_codex.provider import ProviderOAuthPlugOpenAICodex  # noqa: E402

NOTE = "Output format: json."


def _all_text(items):
    texts = []
    for item in items:
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    return texts


class ProviderJsonObjectKeywordTests(unittest.TestCase):
    def setUp(self):
        self.provider = ProviderOAuthPlugOpenAICodex.__new__(
            ProviderOAuthPlugOpenAICodex
        )
        self.provider.provider_config = {"model": "gpt-6.1-sol"}
        self.provider.model_name = "gpt-6.1-sol"

    def build(self, messages, format_type="json_object"):
        payload = {
            "model": "gpt-6.1-sol",
            "messages": messages,
            "response_format": {"type": format_type},
        }
        original = copy.deepcopy(payload)
        params = self.provider._build_responses_params(payload, None)
        self.assertEqual(payload, original)
        return params

    def test_appends_note_to_last_user_message_without_json(self):
        params = self.build(
            [
                {"role": "system", "content": "你是一个复盘助手。"},
                {"role": "user", "content": "按给定结构输出复盘结果"},
            ]
        )
        self.assertEqual(params["text"]["format"], {"type": "json_object"})
        user_items = [
            item
            for item in params["input"]
            if item.get("type") == "message" and item.get("role") == "user"
        ]
        self.assertEqual(len(user_items), 1)
        self.assertIn(NOTE, user_items[0]["content"])
        self.assertIn("json", params["instructions"].lower())

    def test_appends_text_part_to_list_content(self):
        params = self.build(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "第一段"},
                        {"type": "text", "text": "第二段"},
                    ],
                }
            ]
        )
        content = params["input"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[-1], {"type": "input_text", "text": NOTE})

    def test_no_append_when_message_already_mentions_json(self):
        params = self.build(
            [{"role": "user", "content": "请以 json 格式输出结果"}]
        )
        texts = _all_text(params["input"])
        self.assertEqual(texts, ["请以 json 格式输出结果"])
        self.assertNotIn(NOTE, params["instructions"])

    def test_no_append_when_instructions_mention_json_in_message_check(self):
        # instructions 提到 json 但消息正文没有时，消息仍需兜底，
        # 因为后端只扫描 input 消息条目。
        params = self.build(
            [
                {"role": "system", "content": "Always answer with json."},
                {"role": "user", "content": "给出复盘结果"},
            ]
        )
        self.assertEqual(params["instructions"], "Always answer with json.")
        texts = _all_text(params["input"])
        self.assertTrue(any(NOTE in text for text in texts))

    def test_creates_user_message_when_no_messages(self):
        params = self.build([])
        self.assertEqual(len(params["input"]), 1)
        item = params["input"][0]
        self.assertEqual(item["type"], "message")
        self.assertEqual(item["role"], "user")
        self.assertEqual(item["content"], [{"type": "input_text", "text": NOTE}])
        self.assertEqual(params["instructions"], NOTE)

    def test_falls_back_to_last_message_when_no_user_message(self):
        params = self.build([{"role": "assistant", "content": "上次的结果"}])
        self.assertEqual(len(params["input"]), 1)
        self.assertIn(NOTE, params["input"][0]["content"])

    def test_text_format_untouched(self):
        params = self.build(
            [{"role": "user", "content": "随便聊聊"}], format_type="text"
        )
        texts = _all_text(params["input"])
        self.assertEqual(texts, ["随便聊聊"])
        self.assertEqual(params["instructions"], "")


if __name__ == "__main__":
    unittest.main()
