"""Actual command/tool method bodies share the authorized quota refresh path."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from oauth_plug_openai_codex.quota import QuotaReader
from oauth_plug_openai_codex.usage_format import format_usage
from oauth_plug_openai_codex.usage_tool import UsageToolService


def entrypoints():
    path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = [node for cls in tree.body if isinstance(cls, ast.ClassDef) for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name in {"codex_oauth_usage", "command_usage"}]
    for method in methods:
        method.decorator_list = []
        method.returns = None
        for argument in method.args.args:
            argument.annotation = None
    namespace = {"json": json, "format_usage": format_usage}
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), str(path), "exec"), namespace)
    return namespace


class QuotaEntryAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_and_tool_refresh_and_keep_permissions(self):
        entries = entrypoints()
        for entry in ("command_usage", "codex_oauth_usage"):
            for admin, private, allowed in ((True, True, False), (False, True, False), (True, False, False), (True, False, True)):
                with self.subTest(entry=entry, admin=admin, private=private, allowed=allowed):
                    version = [0]
                    calls = []
                    def headers():
                        return {"Authorization": f"Bearer token-{version[0]}", "chatgpt-account-id": "synthetic-account"}, version[0]
                    async def refresh(attempted):
                        self.assertEqual(attempted, 0)
                        version[0] = 1
                        return True
                    provider = SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex", provider_config={"type": "oauth_plug_openai_codex_chat_completion"}, _build_backend_headers=lambda: headers()[0], _build_backend_headers_with_version=headers, _refresh_after_auth_failure=AsyncMock(side_effect=refresh))
                    async def handler(request):
                        calls.append(request)
                        return httpx.Response(401 if len(calls) == 1 else 200, json={"plan_type": "pro"})
                    reader = QuotaReader(client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                    origin = "test:GroupMessage:42"
                    context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
                    config = {"usage": {"enabled": True, "group_allowlist": [origin] if allowed else []}}
                    service = UsageToolService(context, config, reader)
                    event = SimpleNamespace(is_admin=lambda: admin, is_private_chat=lambda: private, unified_msg_origin=origin, stop_event=lambda: None, plain_result=lambda value: value)
                    owner = SimpleNamespace(usage_service=service)
                    if entry == "command_usage":
                        result = [value async for value in entries[entry](owner, event)][0]
                    else:
                        result = json.loads(await entries[entry](owner, event))["status"]
                    if admin and (private or allowed):
                        self.assertEqual(len(calls), 2)
                        self.assertEqual(calls[-1].headers["Authorization"], "Bearer token-1")
                        provider._refresh_after_auth_failure.assert_awaited_once()
                        if entry == "codex_oauth_usage":
                            self.assertEqual(result, "success")
                        else:
                            self.assertIn("套餐：pro", result)
                    else:
                        self.assertEqual(calls, [])
                        provider._refresh_after_auth_failure.assert_not_awaited()
                        context.get_using_provider_async.assert_not_awaited()
                    await service.close()
