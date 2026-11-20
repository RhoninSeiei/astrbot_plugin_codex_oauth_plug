"""Keep one 401 recovery opportunity per logical Codex chat request."""

import asyncio
import json
import unittest
from collections import deque
from unittest.mock import AsyncMock, patch

from test_provider import ProviderOAuthPlugOpenAICodex

from oauth_plug_openai_codex import provider as provider_module
from oauth_plug_openai_codex import provider_runtime_compat


class AuthRetryBudgetTests(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        provider = ProviderOAuthPlugOpenAICodex(
            {
                "id": "auth-retry-test",
                "model": "gpt-6.1-sol",
                "oauth_access_token": "test-access",
                "oauth_refresh_token": "test-refresh",
                "oauth_account_id": "test-account",
            },
            {},
        )
        provider._ensure_fresh_oauth_token = AsyncMock()
        provider.get_model_context_metadata = AsyncMock(return_value={})
        provider._record_provider_stat = AsyncMock()
        return provider

    @staticmethod
    async def _retry_server_error_once(label, factory, *, max_attempts=None):
        try:
            return await factory()
        except provider_module.ProviderRequestError as error:
            if error.status_code != 500:
                raise
            return await factory()

    async def test_generic_retry_cannot_restore_spent_auth_recovery(self):
        provider = self.provider()
        provider._request_backend_once = AsyncMock(side_effect=[
            (401, "{}", 0),
            (500, "{}", 1),
            (401, "{}", 1),
            (200, json.dumps({"output_text": "Unwanted replay"}), 2),
        ])
        provider._refresh_after_auth_failure = AsyncMock(return_value=True)
        with patch.object(provider_runtime_compat, "_core_retry_provider_request", self._retry_server_error_once):
            with self.assertRaises(provider_module.ProviderRequestError) as error:
                await provider.text_chat(prompt="Hello")
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(provider._request_backend_once.await_count, 3)
        provider._refresh_after_auth_failure.assert_awaited_once_with(0)

    async def test_parent_retry_cannot_replay_after_using_already_rotated_credentials(self):
        provider = self.provider()
        original_version = provider._oauth_shared_state.version
        calls = 0

        async def request(payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                provider._oauth_shared_state.apply({"oauth_access_token": "rotated"})
                return 401, "{}", original_version
            if calls == 2:
                return 500, "{}", provider._oauth_shared_state.version
            return 401, "{}", provider._oauth_shared_state.version

        async def parent_text_chat(instance, **kwargs):
            payload = {"model": "gpt-6.1-sol", "messages": []}
            try:
                return await instance._query(payload, None)
            except provider_module.ProviderRequestError as error:
                if error.status_code != 500:
                    raise
                return await instance._query(payload, None)

        async def once(label, factory, *, max_attempts=None):
            return await factory()

        provider._request_backend_once = request
        provider._refresh_oauth_token = AsyncMock(return_value=False)
        with (
            patch.object(provider_module.ProviderOpenAIOfficial, "text_chat", parent_text_chat),
            patch.object(provider_runtime_compat, "_core_retry_provider_request", once),
        ):
            with self.assertRaises(provider_module.ProviderRequestError) as error:
                await provider.text_chat(prompt="Hello")
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(calls, 3)
        provider._refresh_oauth_token.assert_not_awaited()

    async def test_direct_query_stops_after_second_401_and_next_query_can_retry(self):
        provider = self.provider()
        provider._request_backend_once = AsyncMock(side_effect=[
            (401, "{}", 0),
            (401, "{}", 1),
            (401, "{}", 1),
            (200, json.dumps({"output_text": "Recovered"}), 2),
        ])
        provider._refresh_after_auth_failure = AsyncMock(return_value=True)
        with self.assertRaises(provider_module.ProviderRequestError) as error:
            await provider._query({"model": "gpt-6.1-sol", "messages": []}, None)
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(provider._request_backend_once.await_count, 2)
        provider._refresh_after_auth_failure.assert_awaited_once_with(0)

        response = await provider._query({"model": "gpt-6.1-sol", "messages": []}, None)
        self.assertEqual(response.completion_text, "Recovered")
        self.assertEqual(provider._refresh_after_auth_failure.await_count, 2)

    async def test_concurrent_queries_keep_independent_auth_recovery(self):
        provider = self.provider()
        responses = {
            model: deque([(401, "{}", 0), (200, json.dumps({"output_text": model}), 1)])
            for model in ("gpt-6.1-sol", "gpt-6.2-sol")
        }
        both_recovering = asyncio.Event()
        recovering = 0

        async def refresh(version):
            nonlocal recovering
            recovering += 1
            if recovering == 2:
                both_recovering.set()
            await asyncio.wait_for(both_recovering.wait(), timeout=2)
            return True

        async def request(payload):
            return responses[payload["model"]].popleft()

        provider._request_backend_once = request
        provider._refresh_after_auth_failure = refresh
        replies = await asyncio.gather(*(
            provider._query({"model": model, "messages": []}, None) for model in responses
        ))
        self.assertEqual([reply.completion_text for reply in replies], list(responses))
        self.assertEqual(recovering, 2)
        self.assertFalse(any(responses.values()))
