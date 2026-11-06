import json
import types
import unittest
from dataclasses import dataclass, field
from unittest.mock import patch

import test_provider as provider_helpers

from oauth_plug_openai_codex.rejected_tools import (
    LEGACY_REJECTION_NOTICE,
    REJECTION_NOTICE,
    apply_tool_rejections,
    parse_tool_arguments,
    rejected_tool_call,
)


class RejectedArgumentTests(unittest.TestCase):
    def test_invalid_arguments_do_not_become_executable_objects(self):
        cyclic = {}
        cyclic["loop"] = cyclic
        for value, code in (
            (None, "missing_arguments"),
            ('{"secret":"PRIVATE_VALUE', "invalid_json"),
            ("[]", "non_object_arguments"),
            ("null", "non_object_arguments"),
            ("42", "non_object_arguments"),
            ([], "non_object_arguments"),
            ('{"x":NaN}', "non_json_values"),
            ('{"x":Infinity}', "non_json_values"),
            ('{"x":-Infinity}', "non_json_values"),
            ({"x": float("nan")}, "non_json_values"),
            ({"x": (1, 2)}, "non_json_values"),
            ({1: "integer key"}, "non_json_values"),
            (cyclic, "non_json_values"),
        ):
            with self.subTest(code=code):
                self.assertEqual(parse_tool_arguments(value), (None, code))

    def test_explicit_empty_and_normal_objects_remain_valid(self):
        for value, expected in (("{}", {}), ({}, {}), ('{"query":"intended"}', {"query": "intended"})):
            with self.subTest(value=value):
                self.assertEqual(parse_tool_arguments(value), (expected, None))

    def test_rejection_identifiers_and_codes_are_safe(self):
        self.assertEqual(
            rejected_tool_call("tool\nPRIVATE", "PRIVATE" * 100, "PRIVATE"),
            {"name": "", "call_id": "", "code": "invalid_json"},
        )
        self.assertEqual(
            rejected_tool_call("safe_tool-1", "call.1", "missing_arguments"),
            {"name": "safe_tool-1", "call_id": "call.1", "code": "missing_arguments"},
        )

    def test_legacy_metadata_is_attached_without_constructor_changes(self):
        response = types.SimpleNamespace()
        text = apply_tool_rejections(
            response, [rejected_tool_call("tool", "bad", "invalid_json")],
            "Preserved text", has_valid_calls=True,
        )
        self.assertEqual(text, "Preserved text\n\n" + LEGACY_REJECTION_NOTICE)
        self.assertEqual(response.rejected_tool_calls[0]["code"], "invalid_json")
        self.assertNotIn("_raw_arguments", json.dumps(response.rejected_tool_calls))

    def test_native_metadata_preserves_text_and_empty_response_notice(self):
        @dataclass
        class NativeResponse:
            rejected_tool_calls: list = field(default_factory=list)

        rejection = [rejected_tool_call("tool", "bad", "non_object_arguments")]
        for text, valid, expected in (
            ("Preserved text", True, "Preserved text"),
            ("", True, ""),
            ("", False, REJECTION_NOTICE),
        ):
            response = NativeResponse()
            self.assertEqual(
                apply_tool_rejections(response, rejection, text, has_valid_calls=valid), expected
            )
            self.assertEqual(response.rejected_tool_calls, rejection)

    def test_legacy_slots_safely_fall_back_to_notice(self):
        class SlottedResponse:
            __slots__ = ()

        self.assertEqual(
            apply_tool_rejections(
                SlottedResponse(), [rejected_tool_call("tool", "bad", "invalid_json")],
                "", has_valid_calls=False,
            ),
            LEGACY_REJECTION_NOTICE,
        )


def _raw_completion(arguments, *, mixed=False):
    items = [{"type": "function_call", "name": "default_tool", "call_id": "bad", "arguments": arguments}]
    if mixed:
        items.append({"type": "function_call", "name": "default_tool", "call_id": "good", "arguments": '{"query":"intended"}'})
    items.append({"type": "message", "content": [{"type": "output_text", "text": "Preserved text"}]})
    return {"status": "completed", "output": items, "usage": {"input_tokens": 10, "output_tokens": 4}}


class RejectedCompletionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        return provider_helpers.ProviderImageGenerationTests()._make_provider("unused")

    async def test_parser_keeps_valid_text_calls_and_usage_without_raw_rejection(self):
        provider = self.provider()
        for arguments in ('{"secret":"PRIVATE_VALUE', None, "[]", '{"value":NaN}'):
            with self.subTest(arguments=arguments):
                response = await provider._parse_responses_completion(_raw_completion(arguments, mixed=True), None)
                self.assertIn("Preserved text", response.completion_text)
                self.assertEqual(response.tools_call_args, [{"query": "intended"}])
                self.assertEqual(response.tools_call_ids, ["good"])
                self.assertEqual(len(response.rejected_tool_calls), 1)
                self.assertNotIn("PRIVATE", json.dumps(response.rejected_tool_calls))
                self.assertIsNotNone(response.usage)
                for name in ("to_openai_tool_calls_model", "to_openai_tool_calls", "to_openai_to_calls_model"):
                    method = getattr(response, name, None)
                    if method is not None:
                        self.assertNotIn("bad", str(method()))
                        self.assertNotIn("PRIVATE", str(method()))

    async def test_stream_finalization_keeps_rejection_separate(self):
        provider = self.provider()

        async def events(_params):
            yield {"type": "response.output_text.delta", "delta": "Preserved text"}
            yield {"type": "response.completed", "response": _raw_completion('{"secret":"PRIVATE_VALUE', mixed=True)}

        provider._stream_backend_events = events
        results = [item async for item in provider._query_stream({"messages": []}, None)]
        self.assertEqual(results[-1].tools_call_ids, ["good"])
        self.assertEqual(results[-1].rejected_tool_calls[0]["code"], "invalid_json")
        self.assertNotIn("PRIVATE", str(results[-1].tools_call_args))
        self.assertEqual("".join(item.completion_text or "" for item in results if item.is_chunk), "Preserved text")

    async def test_missing_arguments_rejected_but_empty_object_executes(self):
        provider = self.provider()
        missing = _raw_completion(None)
        del missing["output"][0]["arguments"]
        result = await provider._parse_responses_completion(missing, None)
        self.assertEqual(result.tools_call_args, [])
        self.assertEqual(result.rejected_tool_calls[0]["code"], "missing_arguments")
        self.assertIn("Preserved text", result.completion_text)
        accepted = await provider._parse_responses_completion(_raw_completion("{}"), None)
        self.assertEqual(accepted.tools_call_args, [{}])

    async def test_real_runner_never_executes_rejected_default_call(self):
        try:
            from astrbot.core.agent.hooks import BaseAgentRunHooks
            from astrbot.core.agent.run_context import ContextWrapper
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
            from astrbot.core.agent.tool import FunctionTool, ToolSet
            from astrbot.core.provider.entities import LLMResponse, ProviderRequest
            from astrbot.core.provider.provider import Provider
            from mcp.types import CallToolResult, TextContent
        except ImportError:
            self.skipTest("Requires real AstrBot runner")

        for mixed, streaming, no_arguments in (
            (mixed, streaming, no_arguments)
            for mixed in (False, True)
            for streaming in (False, True)
            for no_arguments in (False, True)
        ):
            with self.subTest(mixed=mixed, streaming=streaming, no_arguments=no_arguments):
                raw = _raw_completion('{"secret":"PRIVATE_VALUE', mixed=mixed)
                if mixed and no_arguments:
                    raw["output"][1]["arguments"] = "{}"
                first = await self.provider()._parse_responses_completion(
                    raw, None
                )
                handler_calls = []

                if no_arguments:
                    async def handler():
                        handler_calls.append("NO_ARGUMENT_HANDLER")
                        return "ok"
                else:
                    async def handler(query="UNSAFE_DEFAULT"):
                        handler_calls.append(query)
                        return "ok"

                class SequenceProvider(Provider):
                    def __init__(self):
                        super().__init__({}, {})
                        self.call_count = 0

                    def get_current_key(self):
                        return "synthetic"

                    def set_key(self, key):
                        pass

                    async def get_models(self):
                        return ["synthetic"]

                    async def text_chat(self, **kwargs):
                        self.call_count += 1
                        return first if self.call_count == 1 else LLMResponse("assistant", completion_text="Final answer")

                    async def text_chat_stream(self, **kwargs):
                        yield await self.text_chat(**kwargs)

                class Executor:
                    async def execute(self, tool, run_context, **kwargs):
                        await tool.handler(**kwargs)
                        yield CallToolResult(content=[TextContent(type="text", text="ok")])

                tool = FunctionTool(
                    name="default_tool", description="Synthetic default-valued handler",
                    parameters={"type": "object", "properties": {} if no_arguments else {"query": {"type": "string"}}},
                    handler=handler,
                )
                runner = ToolLoopAgentRunner()
                sequence = SequenceProvider()
                await runner.reset(
                    provider=sequence,
                    request=ProviderRequest(prompt="synthetic test", func_tool=ToolSet(tools=[tool]), contexts=[]),
                    run_context=ContextWrapper(context=None), tool_executor=Executor(),
                    agent_hooks=BaseAgentRunHooks(), streaming=streaming,
                )
                with patch("astrbot.core.agent.runners.tool_loop_agent_runner.logger.info") as info:
                    async for _ in runner.step_until_done(3):
                        pass
                expected_calls = ["NO_ARGUMENT_HANDLER" if no_arguments else "intended"] if mixed else []
                self.assertEqual(handler_calls, expected_calls)
                self.assertNotIn("PRIVATE", str(info.call_args_list))
                self.assertLessEqual(sequence.call_count, 2)
