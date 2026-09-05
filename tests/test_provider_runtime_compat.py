import asyncio
import unittest
from unittest.mock import patch

from oauth_plug_openai_codex import provider_runtime_compat


class ProviderRuntimeCompatTests(unittest.TestCase):
    def test_retry_forwards_rate_limit_policy_to_new_core(self):
        calls = []

        async def core_retry(
            label,
            request_factory,
            *,
            retry_rate_limits=True,
            max_attempts=None,
        ):
            calls.append((label, retry_rate_limits, max_attempts))
            return await request_factory()

        async def request():
            return "ok"

        with patch.object(
            provider_runtime_compat,
            "_core_retry_provider_request",
            core_retry,
        ):
            result = asyncio.run(
                provider_runtime_compat.retry_provider_request(
                    "provider",
                    request,
                    retry_rate_limits=False,
                    max_attempts=4,
                )
            )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [("provider", False, 4)])

    def test_retry_omits_unknown_policy_for_legacy_core(self):
        calls = []

        async def legacy_retry(label, request_factory, *, max_attempts=None):
            calls.append((label, max_attempts))
            return await request_factory()

        async def request():
            return "legacy-ok"

        with (
            patch.object(
                provider_runtime_compat,
                "_core_retry_provider_request",
                None,
            ),
            patch.object(
                provider_runtime_compat,
                "_legacy_retry_provider_request",
                legacy_retry,
            ),
        ):
            result = asyncio.run(
                provider_runtime_compat.retry_provider_request(
                    "provider",
                    request,
                    retry_rate_limits=False,
                    max_attempts=2,
                )
            )

        self.assertEqual(result, "legacy-ok")
        self.assertEqual(calls, [("provider", 2)])


if __name__ == "__main__":
    unittest.main()
