import asyncio
import unittest
from unittest.mock import AsyncMock

from oauth_plug_openai_codex.openai_oauth_audio import OpenAIOAuthAudioMixin


class _Parent:
    async def terminate(self):
        self.parent_closed = True


class _Provider(OpenAIOAuthAudioMixin, _Parent):
    def __init__(self):
        self.provider_config = {}


class ProviderAudioLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_still_closes_every_resource_and_parent_once(self):
        provider = _Provider()
        stream = AsyncMock()
        stream.aclose.side_effect = RuntimeError("stream close failed")
        provider._oauth_stream_clients = {stream}
        provider._oauth_transcription_client = AsyncMock()
        provider._oauth_realtime_client = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "stream close failed"):
            await provider.terminate()

        stream.aclose.assert_awaited_once()
        provider._oauth_transcription_client.close.assert_awaited_once()
        provider._oauth_realtime_client.close.assert_awaited_once()
        self.assertTrue(provider.parent_closed)
        with self.assertRaisesRegex(RuntimeError, "stream close failed"):
            await provider.terminate()
        stream.aclose.assert_awaited_once()

    async def test_cancelled_waiter_does_not_cancel_shared_cleanup(self):
        provider = _Provider()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_close():
            entered.set()
            await release.wait()

        stream = AsyncMock()
        stream.aclose.side_effect = blocking_close
        provider._oauth_stream_clients = {stream}
        provider._oauth_transcription_client = AsyncMock()
        provider._oauth_realtime_client = AsyncMock()

        waiter = asyncio.create_task(provider.terminate())
        await entered.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        release.set()
        await provider.terminate()

        stream.aclose.assert_awaited_once()
        provider._oauth_transcription_client.close.assert_awaited_once()
        provider._oauth_realtime_client.close.assert_awaited_once()
        self.assertTrue(provider.parent_closed)


if __name__ == "__main__":
    unittest.main()
