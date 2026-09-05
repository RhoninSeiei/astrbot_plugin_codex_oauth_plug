import asyncio
import unittest

from oauth_plug_openai_codex.openai_oauth_sse import iter_json_sse_events


async def _lines(*values):
    for value in values:
        yield value


class ProviderSSETests(unittest.TestCase):
    def test_parser_joins_multiline_data_and_stops_at_done(self):
        async def collect():
            return [
                event
                async for event in iter_json_sse_events(
                    _lines(
                        ': keepalive',
                        'data: {"type":',
                        'data: "response.completed"}',
                        '',
                        'data: [DONE]',
                        '',
                        'data: {"ignored": true}',
                        '',
                    )
                )
            ]

        self.assertEqual(
            asyncio.run(collect()),
            [{"type": "response.completed"}],
        )

    def test_parser_rejects_oversized_event(self):
        async def collect():
            return [
                event
                async for event in iter_json_sse_events(
                    _lines('data: {"value":"too large"}', ''),
                    max_event_bytes=8,
                )
            ]

        with self.assertRaisesRegex(ValueError, "大小上限"):
            asyncio.run(collect())


if __name__ == "__main__":
    unittest.main()
