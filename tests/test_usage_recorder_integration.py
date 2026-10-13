"""Exercise the actual core recorder, mocking only transport and database IO."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_provider as provider_helpers
from oauth_plug_openai_codex import provider as provider_module

try:
    from astrbot.core.provider.usage_recorder import instrument_provider, usage_scope
except ImportError:
    instrument_provider = None
    usage_scope = None


def make_provider():
    provider = provider_helpers.ProviderImageGenerationTests()._make_provider("unused")
    provider.provider_config["id"] = "test-usage"
    provider._extract_generated_images = AsyncMock(return_value=["image"])
    return provider


class LegacyUsageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_aggregate_is_written_once(self):
        provider = make_provider()
        database = SimpleNamespace(insert_provider_stat=AsyncMock())
        provider._request_image_backend = AsyncMock(return_value={
            "usage": {"input_tokens": 3, "output_tokens": 4}, "output": [],
        })
        with patch.object(provider_module, "db_helper", database):
            await provider.generate_image("test", n=2)
        database.insert_provider_stat.assert_awaited_once()
        row = database.insert_provider_stat.await_args.kwargs
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["stats"]["token_usage"]["output"], 8)


@unittest.skipIf(instrument_provider is None, "Core does not have usage_recorder")
class CoreUsageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def instrument(self):
        provider = make_provider()
        database = SimpleNamespace(insert_provider_stat=AsyncMock())
        instrument_provider(provider, database)
        return provider, database

    async def test_partial_and_missing_usage_are_recorded_per_image(self):
        provider, database = self.instrument()
        provider._request_image_backend = AsyncMock(side_effect=[
            {"model": "gpt-image-2", "usage": {"input_tokens": 3}},
            {"model": "gpt-image-2", "usage": {}},
        ])
        with usage_scope(plugin_id="test-plugin", session_umo="test-session"):
            self.assertEqual(await provider.generate_image("test", n=2), ["image", "image"])
        rows = [call.kwargs for call in database.insert_provider_stat.await_args_list]
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["stats"]["usage_status"] for r in rows], ["partial", "missing"])
        self.assertEqual([r["status"] for r in rows], ["completed", "completed"])
        self.assertEqual([r["stats"]["request_kind"] for r in rows], ["image", "image"])
        self.assertTrue(all(r["stats"]["plugin_id"] == "test-plugin" for r in rows))

    async def test_second_failure_does_not_add_an_aggregate_row(self):
        provider, database = self.instrument()
        provider._request_image_backend = AsyncMock(side_effect=[
            {"usage": {"input_tokens": 3, "output_tokens": 4}},
            RuntimeError("test transport failure"),
        ])
        with self.assertRaisesRegex(RuntimeError, "test transport failure"):
            await provider.generate_image("test", n=2)
        rows = [call.kwargs for call in database.insert_provider_stat.await_args_list]
        self.assertEqual([r["status"] for r in rows], ["completed", "error"])
        self.assertEqual([r["stats"]["usage_status"] for r in rows], ["reported", "missing"])

    async def test_cancellation_records_one_aborted_row(self):
        provider, database = self.instrument()
        entered = asyncio.Event()

        async def pending(*args):
            entered.set()
            await asyncio.Event().wait()

        provider._request_image_backend = pending
        task = asyncio.create_task(provider.generate_image("test"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        database.insert_provider_stat.assert_awaited_once()
        row = database.insert_provider_stat.await_args.kwargs
        self.assertEqual(row["status"], "aborted")
        self.assertEqual(row["stats"]["usage_status"], "missing")
