import asyncio
import unittest

from oauth_plug_openai_codex.openai_oauth_sse import iter_json_sse_events, iter_json_sse_text


async def _lines(*values):
    for value in values:
        yield value


class ProviderSSETests(unittest.TestCase):
    def test_buffered_and_async_parsers_preserve_unicode_line_separators(self):
        wire = 'data: {"type":"response.completed","output_text":"before\u2028after"}'
        async def collect():
            return [event async for event in iter_json_sse_events(_lines(wire, ""))]
        self.assertEqual(list(iter_json_sse_text(wire)), asyncio.run(collect()))
        self.assertEqual(list(iter_json_sse_text(wire))[0]["output_text"], "before\u2028after")

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
