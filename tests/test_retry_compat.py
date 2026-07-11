import asyncio
import importlib
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class RetryCompatibilityTests(unittest.TestCase):
    def _module(self):
        return importlib.import_module("oauth_plug_openai_codex.retry_compat")

    def test_module_loads_when_astrbot_request_retry_is_missing(self):
        module_path = (
            Path(__file__).resolve().parents[1]
            / "oauth_plug_openai_codex"
            / "retry_compat.py"
        )
        spec = importlib.util.spec_from_file_location(
            "oauth_plug_openai_codex_retry_compat_without_core",
            module_path,
        )
        module = importlib.util.module_from_spec(spec)

        with patch.dict(
            sys.modules,
            {"astrbot.core.provider.sources.request_retry": None},
        ):
            spec.loader.exec_module(module)

        self.assertIsNone(module._core_retry_provider_request)

    def test_fallback_executes_request_once_and_returns_result(self):
        retry_compat = self._module()
        calls = []

        async def request_factory():
            calls.append(True)
            return "result"

        with patch.object(retry_compat, "_core_retry_provider_request", None):
            result = asyncio.run(
                retry_compat.retry_provider_request(
                    "OpenAI OAuth",
                    request_factory,
                    max_attempts=7,
                )
            )

        self.assertEqual(result, "result")
        self.assertEqual(calls, [True])

    def test_fallback_propagates_error_without_retrying(self):
        retry_compat = self._module()
        calls = []

        async def request_factory():
            calls.append(True)
            raise RuntimeError("request failed")

        with patch.object(retry_compat, "_core_retry_provider_request", None):
            with self.assertRaisesRegex(RuntimeError, "request failed"):
                asyncio.run(
                    retry_compat.retry_provider_request(
                        "OpenAI OAuth",
                        request_factory,
                        max_attempts=7,
                    )
                )

        self.assertEqual(calls, [True])

    def test_core_retry_receives_max_attempts_when_available(self):
        retry_compat = self._module()
        calls = []

        async def core_retry(label, request_factory, *, max_attempts=None):
            calls.append((label, max_attempts))
            return await request_factory()

        async def request_factory():
            return "delegated"

        with patch.object(
            retry_compat,
            "_core_retry_provider_request",
            core_retry,
        ):
            result = asyncio.run(
                retry_compat.retry_provider_request(
                    "OpenAI OAuth",
                    request_factory,
                    max_attempts=4,
                )
            )

        self.assertEqual(result, "delegated")
        self.assertEqual(calls, [("OpenAI OAuth", 4)])
