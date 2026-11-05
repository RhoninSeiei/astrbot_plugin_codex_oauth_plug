import asyncio
import unittest

from oauth_plug_openai_codex.openai_oauth_audio import OpenAIOAuthAudioMixin


class PayloadParent:
    # Matches AstrBot v4.29.4's payload-builder signature. Capturing the received
    # values isolates the mixin's prompt normalization from message assembly.
    async def _prepare_chat_payload(
        self,
        prompt,
        image_urls=None,
        audio_urls=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        **kwargs,
    ):
        return (
            {
                "prompt": prompt,
                "image_urls": image_urls,
                "audio_urls": audio_urls,
                "contexts": contexts,
                "system_prompt": system_prompt,
                "tool_calls_result": tool_calls_result,
                "model": model,
                "extra_user_content_parts": extra_user_content_parts,
                **kwargs,
            },
            contexts,
        )


class AudioProvider(OpenAIOAuthAudioMixin, PayloadParent):
    pass


class AudioPromptContextTests(unittest.TestCase):
    def setUp(self):
        self.provider = AudioProvider()
        self.contexts = [{"role": "user", "content": "existing user message"}]
        self.extra_parts = [{"type": "text", "text": "attachment context"}]

    def prepare(self, *args, **kwargs):
        return asyncio.run(self.provider._prepare_chat_payload(*args, **kwargs))

    def test_extra_parts_do_not_create_new_turn_for_existing_context(self):
        payload, context = self.prepare(
            None,
            contexts=self.contexts,
            extra_user_content_parts=self.extra_parts,
        )
        self.assertIsNone(payload["prompt"])
        self.assertIs(context, self.contexts)
        self.assertIs(payload["extra_user_content_parts"], self.extra_parts)

    def test_existing_context_is_recognized_in_positional_arguments(self):
        payload, context = self.prepare(
            None,
            None,
            None,
            self.contexts,
            extra_user_content_parts=self.extra_parts,
        )
        self.assertIsNone(payload["prompt"])
        self.assertIs(context, self.contexts)

    def test_positional_extra_parts_can_start_attachment_only_turn(self):
        payload, _ = self.prepare(
            None, None, None, None, None, None, "gpt-6.1-sol", self.extra_parts
        )
        self.assertEqual(payload["prompt"], "")
        self.assertIs(payload["extra_user_content_parts"], self.extra_parts)
        self.assertEqual(payload["model"], "gpt-6.1-sol")

    def test_keyword_extra_parts_can_start_attachment_only_turn(self):
        for contexts in (None, []):
            with self.subTest(contexts=contexts):
                payload, _ = self.prepare(
                    None,
                    contexts=contexts,
                    extra_user_content_parts=self.extra_parts,
                )
                self.assertEqual(payload["prompt"], "")
                self.assertIs(payload["contexts"], contexts)

    def test_audio_starts_new_turn_even_when_context_already_exists(self):
        audio = ["voice.wav"]
        for positional in (False, True):
            with self.subTest(positional=positional):
                if positional:
                    payload, _ = self.prepare(None, None, audio, self.contexts)
                else:
                    payload, _ = self.prepare(
                        None, audio_urls=audio, contexts=self.contexts
                    )
                self.assertEqual(payload["prompt"], "")
                self.assertIs(payload["audio_urls"], audio)
                self.assertIs(payload["contexts"], self.contexts)

    def test_explicit_prompt_is_preserved_with_context_and_attachments(self):
        for prompt in ("new user message", ""):
            with self.subTest(prompt=prompt):
                payload, _ = self.prepare(
                    prompt,
                    contexts=self.contexts,
                    audio_urls=["voice.wav"],
                    extra_user_content_parts=self.extra_parts,
                )
                self.assertEqual(payload["prompt"], prompt)

    def test_no_attachments_leave_none_prompt_unchanged(self):
        for contexts in (None, [], self.contexts):
            with self.subTest(contexts=contexts):
                payload, _ = self.prepare(
                    None,
                    contexts=contexts,
                    audio_urls=[],
                    extra_user_content_parts=[],
                )
                self.assertIsNone(payload["prompt"])

    def test_positional_context_and_extra_parts_are_both_preserved(self):
        payload, _ = self.prepare(
            None,
            None,
            None,
            self.contexts,
            "system instruction",
            [],
            "gpt-6.1-sol",
            self.extra_parts,
        )
        self.assertIsNone(payload["prompt"])
        self.assertIs(payload["contexts"], self.contexts)
        self.assertIs(payload["extra_user_content_parts"], self.extra_parts)
        self.assertEqual(payload["system_prompt"], "system instruction")

    def test_duplicate_arguments_retain_parent_type_error(self):
        for args, kwargs in (
            ((None, None, ["positional.wav"]), {"audio_urls": None}),
            ((None, None, None, self.contexts), {"contexts": []}),
            (
                (None, None, None, None, None, None, None, self.extra_parts),
                {"extra_user_content_parts": []},
            ),
        ):
            with self.subTest(args=args, kwargs=kwargs):
                with self.assertRaises(TypeError):
                    self.prepare(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
