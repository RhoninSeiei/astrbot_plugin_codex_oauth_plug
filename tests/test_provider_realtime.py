import asyncio
import types
import unittest

from oauth_plug_openai_codex.openai_oauth_realtime import OpenAIOAuthRealtimeClient


class ProviderRealtimeTests(unittest.TestCase):
    def _client(self):
        provider = types.SimpleNamespace(
            provider_config={},
            timeout=30,
        )
        return OpenAIOAuthRealtimeClient(provider)

    def test_realtime_rejects_non_audio_sdp_before_network(self):
        client = self._client()

        with self.assertRaisesRegex(ValueError, "audio-only"):
            asyncio.run(client.create_session("v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96"))

    def test_realtime_rejects_unsupported_model_before_network(self):
        client = self._client()
        offer = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111"

        with self.assertRaisesRegex(ValueError, "gpt-live-1-codex"):
            asyncio.run(client.create_session(offer, model="gpt-live-other"))


if __name__ == "__main__":
    unittest.main()
