import asyncio
import copy
import json
import types
import unittest

from test_provider import _install_fake_astrbot_runtime

_install_fake_astrbot_runtime()

from oauth_plug_openai_codex.provider import ProviderOAuthPlugOpenAICodex  # noqa: E402


class ProviderResponseValidationTests(unittest.TestCase):
    def setUp(self):
        self.provider = ProviderOAuthPlugOpenAICodex.__new__(
            ProviderOAuthPlugOpenAICodex
        )
        self.provider.provider_config = {"model": "gpt-6.1-sol"}
        self.provider.model_name = "gpt-6.1-sol"

    def parse_completion(self, response):
        return asyncio.run(
            self.provider._parse_responses_completion(response, None)
        )

    @staticmethod
    def sse(*events):
        return "".join(f"data: {json.dumps(event)}\n\n" for event in events)

    def test_response_format_maps_supported_chat_formats(self):
        schema = {
            "name": "answer",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        }
        for response_format, expected in (
            ({"type": "text"}, {"type": "text"}),
            ({"type": "json_object"}, {"type": "json_object"}),
            (
                {"type": "json_schema", "json_schema": schema},
                {"type": "json_schema", **schema},
            ),
        ):
            with self.subTest(response_format=response_format):
                payload = {
                    "model": "gpt-6.1-sol",
                    "messages": [],
                    "response_format": response_format,
                }
                original = copy.deepcopy(payload)
                params = self.provider._build_responses_params(payload, None)
                self.assertEqual(params["text"]["format"], expected)
                self.assertNotIn("response_format", params)
                self.assertEqual(payload, original)

    def test_request_response_format_preserves_other_text_settings(self):
        self.provider.provider_config["custom_extra_body"] = {
            "text": {"verbosity": "low", "format": {"type": "text"}}
        }
        original = copy.deepcopy(self.provider.provider_config)
        params = self.provider._build_responses_params(
            {"messages": [], "response_format": {"type": "json_object"}},
            None,
        )
        self.assertEqual(
            params["text"],
            {"verbosity": "low", "format": {"type": "json_object"}},
        )
        self.assertEqual(self.provider.provider_config, original)

    def test_nested_schema_type_cannot_override_response_format(self):
        params = self.provider._build_responses_params(
            {
                "messages": [],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "type": "json_object",
                        "name": "answer",
                        "schema": {"type": "object"},
                    },
                },
            },
            None,
        )
        self.assertEqual(params["text"]["format"]["type"], "json_schema")
        self.assertEqual(
            params["text"]["format"]["schema"], {"type": "object"}
        )

    def test_invalid_response_formats_fail_before_request(self):
        for response_format in (
            "json_object",
            [],
            {},
            {"type": "unsupported"},
            {"type": "json_schema"},
            {"type": "json_schema", "json_schema": {"schema": {}}},
            {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": []},
            },
            {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": {}, "strict": "true"},
            },
        ):
            with self.subTest(response_format=response_format):
                with self.assertRaises(ValueError):
                    self.provider._build_responses_params(
                        {"messages": [], "response_format": response_format},
                        None,
                    )

    def test_completed_null_output_is_successful_empty_completion(self):
        for response in (
            {"id": "resp_empty", "status": "completed", "output": None},
            types.SimpleNamespace(
                id="resp_empty", status="completed", output=None
            ),
        ):
            with self.subTest(response=response):
                result = self.parse_completion(response)
                self.assertEqual(result.completion_text, "")
                self.assertFalse(result.tools_call_args)

    def test_refusal_content_becomes_assistant_completion(self):
        result = self.parse_completion(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "refusal", "refusal": "Cannot assist."}
                        ],
                    }
                ],
            }
        )
        self.assertEqual(result.completion_text, "Cannot assist.")
        self.assertFalse(result.tools_call_args)

    def test_unsuccessful_status_rejects_even_nonempty_output(self):
        for status in ("failed", "incomplete", "cancelled", "in_progress"):
            with self.subTest(status=status):
                with self.assertRaises(Exception):
                    self.parse_completion(
                        {
                            "status": status,
                            "output_text": "partial response",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {"type": "output_text", "text": "partial"}
                                    ],
                                }
                            ],
                        }
                    )

    def test_legacy_response_without_status_keeps_nonempty_text(self):
        result = self.parse_completion(
            {"id": "resp_legacy", "output_text": "legacy reply", "output": []}
        )
        self.assertEqual(result.completion_text, "legacy reply")

    def test_valid_object_tool_arguments_are_preserved(self):
        for arguments in ('{"city":"Shanghai"}', {"city": "Shanghai"}, "{}"):
            with self.subTest(arguments=arguments):
                result = self.parse_completion(
                    {
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "name": "weather",
                                "call_id": "call_1",
                                "arguments": arguments,
                            }
                        ],
                    }
                )
                expected = (
                    json.loads(arguments)
                    if isinstance(arguments, str)
                    else arguments
                )
                self.assertEqual(result.tools_call_args, [expected])
                self.assertEqual(result.tools_call_name, ["weather"])
                self.assertEqual(result.tools_call_ids, ["call_1"])

    def test_invalid_tool_arguments_are_preserved_not_rejected(self):
        # 与原 reject 用例同一组输入矩阵，预期改为降级保留：
        # 坏参数不让整条 completion 失败，原始内容留给工具执行层裁决。
        missing = object()
        cases = (
            ('{"city":', {"_raw_arguments": '{"city":'}),
            ("[]", {"_raw_arguments": "[]"}),
            ("null", {"_raw_arguments": "null"}),
            ("42", {"_raw_arguments": "42"}),
            ('{"value":NaN}', {"_raw_arguments": '{"value":NaN}'}),
            ('{"value":Infinity}', {"_raw_arguments": '{"value":Infinity}'}),
            ('{"value":-Infinity}', {"_raw_arguments": '{"value":-Infinity}'}),
            (None, {}),
            ([], {"_value": []}),
            (missing, {}),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                item = {
                    "type": "function_call",
                    "name": "weather",
                    "call_id": "call_bad",
                }
                if arguments is not missing:
                    item["arguments"] = arguments
                result = self.parse_completion(
                    {"status": "completed", "output": [item]}
                )
                self.assertEqual(result.tools_call_args, [expected])
                self.assertEqual(result.tools_call_name, ["weather"])
                self.assertEqual(result.tools_call_ids, ["call_bad"])

    def test_backend_error_never_echoes_untrusted_error_fields(self):
        secret = "SECRET_PROMPT_ACCESS_TOKEN_8c3e"
        for body in (
            secret,
            json.dumps(secret),
            json.dumps({"message": secret, "code": secret, "type": secret}),
            json.dumps(
                {
                    "error": {"message": secret, "code": secret, "type": secret},
                    "instructions": secret,
                }
            ),
        ):
            with self.subTest(body=body):
                message = self.provider._format_backend_error(400, body)
                self.assertIn("400", message)
                self.assertNotIn(secret, message)

    def test_known_rate_limit_error_stays_diagnosable_without_message(self):
        secret = "SECRET_PROMPT_ACCESS_TOKEN_rate_limit"
        error = {"message": secret, "code": "rate_limit_exceeded"}
        message = self.provider._format_backend_error(
            429, json.dumps({"error": error})
        )
        self.assertIn("429", message)
        self.assertIn("rate_limit_exceeded", message)
        self.assertNotIn(secret, message)
        with self.assertRaises(Exception) as raised:
            self.provider._parse_backend_response(
                self.sse({"type": "error", "error": error})
            )
        self.assertEqual(getattr(raised.exception, "status_code", None), 429)
        self.assertIn("rate_limit_exceeded", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    def test_failed_completion_does_not_echo_response_identifiers_or_errors(self):
        secret = "SECRET_PROMPT_ACCESS_TOKEN_a912"
        with self.assertRaises(Exception) as raised:
            self.parse_completion(
                {
                    "id": secret,
                    "status": "failed",
                    "error": {"message": secret, "code": secret, "type": secret},
                    "instructions": secret,
                    "output": [{"type": secret}],
                }
            )
        self.assertNotIn(secret, str(raised.exception))

    def test_error_event_does_not_echo_untrusted_fields(self):
        secret = "SECRET_PROMPT_ACCESS_TOKEN_e03f"
        with self.assertRaises(Exception) as raised:
            self.provider._parse_backend_response(
                self.sse(
                    {
                        "type": "response.failed",
                        "response": {
                            "id": secret,
                            "error": {"message": secret, "code": secret},
                        },
                    }
                )
            )
        self.assertNotIn(secret, str(raised.exception))

    def test_malformed_sse_cannot_be_hidden_by_a_completed_event(self):
        completed = self.sse(
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": []},
            }
        )
        for malformed in ("{broken", "[]", "null", "42"):
            with self.subTest(malformed=malformed):
                with self.assertRaises(Exception):
                    self.provider._parse_backend_response(
                        f"data: {malformed}\n\n" + completed
                    )

    def test_failure_event_cannot_be_hidden_by_a_completed_event(self):
        for failure_type in (
            "error",
            "response.error",
            "response.failed",
            "response.incomplete",
        ):
            with self.subTest(failure_type=failure_type):
                with self.assertRaises(Exception):
                    self.provider._parse_backend_response(
                        self.sse(
                            {"type": failure_type},
                            {
                                "type": "response.completed",
                                "response": {"status": "completed", "output": []},
                            },
                        )
                    )

    def test_completed_envelope_cannot_hide_unsuccessful_response_status(self):
        for status in ("failed", "incomplete"):
            envelope = {
                "type": "response.completed",
                "response": {
                    "status": status,
                    "output": [{"type": "image_generation_call", "result": "aW1n"}],
                },
            }
            for body in (json.dumps(envelope), self.sse(envelope)):
                with self.subTest(status=status, body=body):
                    with self.assertRaises(Exception):
                        self.provider._parse_backend_response(body)

    def test_buffered_sse_accepts_bom_eof_and_done(self):
        event = {
            "type": "response.completed",
            "response": {
                "id": "resp_buffered",
                "status": "completed",
                "output": [],
                "output_text": "ok",
            },
        }
        data = f"data: {json.dumps(event)}"
        for body in (
            "\ufeff" + data + "\n\n",
            data,
            data + "\n\ndata: [DONE]\n\ndata: {bad ignored after DONE}",
        ):
            with self.subTest(body=body):
                response = self.provider._parse_backend_response(body)
                self.assertEqual(response["id"], "resp_buffered")
                self.assertEqual(response["output_text"], "ok")

    def test_valid_multiline_sse_is_joined_before_json_parsing(self):
        response = self.provider._parse_backend_response(
            ': keepalive\n'
            'data: {"type":"response.completed",\n'
            'data: "response":{"id":"resp_multiline","status":"completed",'
            '"output":[],"output_text":"ok"}}\n\n'
            'data: [DONE]\n\n'
        )
        self.assertEqual(response["id"], "resp_multiline")
        self.assertEqual(response["output_text"], "ok")


if __name__ == "__main__":
    unittest.main()
