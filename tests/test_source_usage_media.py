import asyncio
import time
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_provider as provider_helpers

from oauth_plug_openai_codex.openai_oauth_transcription import OpenAIOAuthTranscriptionClient
from oauth_plug_openai_codex import openai_oauth_transcription as transcription
from oauth_plug_openai_codex import provider_runtime_compat as compat


class SourceUsageMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcription_missing_partial_and_zero_usage_are_distinct(self):
        class Resolver:
            def __init__(self, *args, **kwargs):
                pass

            @asynccontextmanager
            async def as_wav_path(self):
                yield types.SimpleNamespace(path="synthetic.wav", mime_type="audio/wav")

        for raw in (None, {}, {"input_tokens": 0, "output_tokens": 0}, {"input_tokens": 3}):
            with self.subTest(raw=raw):
                provider = provider_helpers.ProviderImageGenerationTests()._make_provider("unused")
                provider._ensure_fresh_oauth_token = AsyncMock()
                provider._record_provider_stat = AsyncMock()
                client = OpenAIOAuthTranscriptionClient(provider)
                client._post_transcription = AsyncMock(return_value=(200, {"text": "transcript", "usage": raw}, 0))
                with patch.object(transcription, "BoundedOAuthAudioResolver", Resolver), \
                     patch.object(Path, "stat", return_value=types.SimpleNamespace(st_size=3)), \
                     patch.object(Path, "read_bytes", return_value=b"wav"):
                    await client.transcribe_audio("synthetic.wav")
                recorded = provider._record_provider_stat.call_args.kwargs
                if not raw:
                    self.assertIsNone(recorded["usage"])
                else:
                    self.assertEqual(recorded["usage"].input_other, raw["input_tokens"])
                    self.assertEqual(recorded["usage"].output, raw.get("output_tokens", 0))
                    if hasattr(recorded["usage"], "is_partial"):
                        self.assertEqual(recorded["usage"].is_partial, "output_tokens" not in raw)
                await client.close()

    async def test_transcription_records_actual_usage_and_isolates_parent_ledger(self):
        provider = provider_helpers.ProviderImageGenerationTests()._make_provider("unused")
        provider._ensure_fresh_oauth_token = AsyncMock()
        records = []

        async def record(**kwargs):
            records.append((kwargs, compat.current_attempt_ledger.get()))

        provider._record_provider_stat = record
        client = OpenAIOAuthTranscriptionClient(provider)
        client._post_transcription = AsyncMock(return_value=(200, {
            "text": "transcript", "model": "reported-transcription",
            "usage": {"input_tokens": 0, "output_tokens": 7},
        }, 0))

        class Resolver:
            def __init__(self, *args, **kwargs):
                pass

            @asynccontextmanager
            async def as_wav_path(self):
                yield types.SimpleNamespace(path="synthetic.wav", mime_type="audio/wav")

        parent = compat.UsageAttemptLedger(provider_key=123)
        token = compat.current_attempt_ledger.set(parent)
        try:
            with patch.object(transcription, "BoundedOAuthAudioResolver", Resolver), \
                 patch.object(Path, "stat", return_value=types.SimpleNamespace(st_size=3)), \
                 patch.object(Path, "read_bytes", return_value=b"wav"):
                result = await client.transcribe_audio("synthetic.wav")
            self.assertIs(compat.current_attempt_ledger.get(), parent)
        finally:
            compat.current_attempt_ledger.reset(token)
            await client.close()
        self.assertEqual(result, "transcript")
        self.assertEqual(len(records), 1)
        recorded, active_ledger = records[0]
        self.assertIsNone(active_ledger)
        self.assertEqual(recorded["request_kind"], "transcription")
        self.assertEqual(recorded["model"], "reported-transcription")
        self.assertEqual(recorded["usage"].input_other, 0)
        self.assertEqual(recorded["usage"].output, 7)

    async def test_transcription_cancel_records_aborted_without_guessing_usage(self):
        provider = provider_helpers.ProviderImageGenerationTests()._make_provider("unused")
        provider._record_provider_stat = AsyncMock()
        client = OpenAIOAuthTranscriptionClient(provider)
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        client._transcribe_audio = blocked
        task = asyncio.create_task(client.transcribe_audio("synthetic.wav"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        recorded = provider._record_provider_stat.call_args.kwargs
        self.assertEqual(recorded["status"], "aborted")
        self.assertIsNone(recorded["usage"])
        await client.close()

    def realtime_session(self):
        from oauth_plug_openai_codex import openai_oauth_realtime as realtime

        provider = provider_helpers.ProviderImageGenerationTests()._make_provider("unused")
        provider._usage_recording_enabled = True
        provider._usage_recording_db = object()
        session = realtime.OpenAIOAuthRealtimeSession.__new__(realtime.OpenAIOAuthRealtimeSession)
        session._client = types.SimpleNamespace(oauth_provider=provider, event_queue_size=2, close_timeout=0.1)
        session._usage_context = object()
        session._usage_session_id = "synthetic-session"
        session._usage_tasks = set()
        session._usage_lock = asyncio.Lock()
        session._usage_unavailable_warned = False
        return realtime, session

    async def test_realtime_terminal_snapshots_use_raw_directions_and_stable_identity(self):
        realtime, session = self.realtime_session()
        recorder = AsyncMock(return_value=True)
        with patch.object(realtime, "record_provider_usage", recorder):
            await session._record_done_usage({"type": "response.done", "response": {
                "id": "upstream-response", "usage": {"input_tokens": 0}, "model": "actual-model",
            }})
            await asyncio.gather(*tuple(session._usage_tasks))
            await session._record_done_usage({"type": "turn.done", "turn": {
                "response_id": "upstream-response", "usage": {"output_tokens": 9}, "status": "cancelled",
            }})
            await asyncio.gather(*tuple(session._usage_tasks))
            # Anonymous and session-level totals do not become guessed calls.
            await session._record_done_usage({"type": "response.done", "response": {"usage": {"output_tokens": 99}}})
            await session._record_done_usage({"type": "session.done", "session": {"id": "total", "usage": {"output_tokens": 99}}})
        first, second = [call.kwargs for call in recorder.call_args_list]
        self.assertEqual(first["usage_snapshot_fields"], frozenset({"input", "model"}))
        self.assertEqual(second["usage_snapshot_fields"], frozenset({"output"}))
        self.assertEqual(first["usage"].input_other, 0)
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertNotIn("upstream-response", first["request_id"])
        self.assertIs(first["_context"], session._usage_context)
        self.assertEqual(second["status"], "aborted")
        self.assertTrue(first["replace_snapshot"])
        self.assertEqual(first["request_kind"], "realtime")

    async def test_realtime_write_queue_is_serial_and_bounded(self):
        realtime, session = self.realtime_session()
        started, release = asyncio.Event(), asyncio.Event()
        active = peak = calls = 0

        async def record(*args, **kwargs):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            started.set()
            try:
                await release.wait()
                return True
            finally:
                active -= 1

        with patch.object(realtime, "record_provider_usage", record):
            for index in range(3):
                await session._record_done_usage({"type": "response.done", "response": {
                    "id": f"response-{index}", "usage": {"input_tokens": 1, "output_tokens": 1},
                }})
            await started.wait()
            self.assertEqual(len(session._usage_tasks), 2)
            release.set()
            await asyncio.gather(*tuple(session._usage_tasks))
        self.assertEqual(calls, 2)
        self.assertEqual(peak, 1)

    async def test_realtime_close_cancels_bounded_usage_drain(self):
        from oauth_plug_openai_codex import openai_oauth_realtime as realtime

        provider = provider_helpers.ProviderImageGenerationTests()._make_provider("unused")
        provider._usage_recording_enabled = True
        provider._usage_recording_db = object()
        started, stopped = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        class Socket:
            async def recv(self):
                await asyncio.Event().wait()

            async def send(self, value):
                pass

            async def close(self, **kwargs):
                pass

        client = realtime.OpenAIOAuthRealtimeClient(provider, close_timeout=0.01)
        with patch.object(realtime, "record_provider_usage", blocked):
            session = realtime.OpenAIOAuthRealtimeSession(client, "rtc_synthetic", "v=0", Socket(), time.time() + 60)
            await session._record_done_usage({"type": "response.done", "response": {"id": "pending"}})
            await started.wait()
            await session.close()
        self.assertTrue(stopped.is_set())
        self.assertTrue(session.closed)
