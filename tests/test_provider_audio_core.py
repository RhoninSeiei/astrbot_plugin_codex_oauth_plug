# Adapted from AstrBot's production OAuth regression suite.
import asyncio
from unittest.mock import AsyncMock

import pytest

from oauth_plug_openai_codex.openai_oauth_audio import OpenAIOAuthAudioMixin


class Parent:
    async def _prepare_chat_payload(self, prompt, *args, **kwargs):
        return prompt, args, kwargs

    async def terminate(self):
        self.parent_closed = True


class AudioProvider(OpenAIOAuthAudioMixin, Parent):
    def __init__(self):
        self.provider_config = {}


@pytest.mark.asyncio
async def test_audio_only_turn_is_not_discarded():
    provider = AudioProvider()
    prompt, _, kwargs = await provider._prepare_chat_payload(
        None, audio_urls=["voice.wav"]
    )
    assert prompt == ""
    assert kwargs["audio_urls"] == ["voice.wav"]
    prompt, _, _ = await provider._prepare_chat_payload(None)
    assert prompt is None


@pytest.mark.asyncio
async def test_disabled_audio_has_actionable_error_and_no_request():
    provider = AudioProvider()
    provider.transcribe_audio = AsyncMock()
    with pytest.raises(ValueError, match="oauth_audio_transcription"):
        await provider._resolve_audio_part("voice.wav")
    provider.transcribe_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_audio_uses_transcript_content():
    provider = AudioProvider()
    provider.provider_config = {
        "oauth_audio_transcription": True,
        "oauth_transcription_model": "gpt-4o-transcribe",
    }
    provider.transcribe_audio = AsyncMock(return_value="测试转录")
    part = await provider._resolve_audio_part("voice.wav")
    assert part == {"type": "text", "text": "测试转录"}
    provider.transcribe_audio.assert_awaited_once_with(
        "voice.wav", model="gpt-4o-transcribe"
    )


@pytest.mark.asyncio
async def test_audio_error_is_not_replaced_with_empty_text():
    provider = AudioProvider()
    provider.provider_config = {"oauth_audio_transcription": True}
    provider.transcribe_audio = AsyncMock(side_effect=PermissionError("denied"))
    with pytest.raises(PermissionError):
        await provider._resolve_audio_part("voice.wav")


@pytest.mark.asyncio
async def test_provider_termination_closes_media_and_rejects_reuse():
    provider = AudioProvider()
    provider._oauth_transcription_client = AsyncMock()
    provider._oauth_realtime_client = AsyncMock()
    await provider.terminate()
    assert provider.parent_closed
    provider._oauth_transcription_client.close.assert_awaited_once()
    provider._oauth_realtime_client.close.assert_awaited_once()
    with pytest.raises(RuntimeError, match="closed"):
        await provider.transcribe_audio("voice.wav")
    with pytest.raises(RuntimeError, match="closed"):
        await provider.create_realtime_session("offer")


@pytest.mark.asyncio
async def test_termination_attempts_all_cleanup_after_one_client_fails():
    provider = AudioProvider()
    failing_stream = AsyncMock()
    failing_stream.aclose.side_effect = RuntimeError("stream close failed")
    provider._oauth_stream_clients = {failing_stream}
    provider._oauth_transcription_client = AsyncMock()
    provider._oauth_realtime_client = AsyncMock()

    with pytest.raises(RuntimeError, match="stream close failed"):
        await provider.terminate()

    failing_stream.aclose.assert_awaited_once()
    provider._oauth_transcription_client.close.assert_awaited_once()
    provider._oauth_realtime_client.close.assert_awaited_once()
    assert provider.parent_closed
    with pytest.raises(RuntimeError, match="stream close failed"):
        await provider.terminate()
    failing_stream.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_termination_waiter_does_not_cancel_shared_cleanup():
    provider = AudioProvider()
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
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    await provider.terminate()

    stream.aclose.assert_awaited_once()
    provider._oauth_transcription_client.close.assert_awaited_once()
    provider._oauth_realtime_client.close.assert_awaited_once()
    assert provider.parent_closed

