"""Small, model-free contract checks; real pronunciation is checked separately."""

import asyncio
import unittest
from types import SimpleNamespace

import httpx
import numpy as np
from routing import locale, split_languages, voice_for
from server import app


class Identifier:
    def rank(self, text):
        language = "de" if "Guten" in text else "en"
        return [(language, 0.99), ("fr", 0.01)]


class RoutingTests(unittest.TestCase):
    def test_russian_english_switch_preserves_every_character(self):
        text = 'Скажите "I am a student", затем назовите имя.'
        spans = split_languages(text, "en-GB", Identifier())
        self.assertEqual([language for language, _ in spans], ["ru", "en-GB", "ru"])
        self.assertEqual("".join(part for _, part in spans), text)

    def test_latin_switch_uses_sentences(self):
        text = "Guten Tag, ich heiße Alex. What is your name?"
        spans = split_languages(text, "de-DE", Identifier())
        self.assertEqual([language for language, _ in spans], ["de", "en-GB"])
        self.assertEqual("".join(part for _, part in spans), text)

    def test_ambiguous_word_keeps_resource_language(self):
        self.assertEqual(split_languages("Student", "de-DE", Identifier()), [("de", "Student")])

    def test_japanese_kanji_kana_and_english(self):
        text = "私は Alex です。"
        spans = split_languages(text, "ja-JP", Identifier())
        self.assertEqual([language for language, _ in spans], ["ja", "en-GB", "ja"])
        self.assertEqual("".join(part for _, part in spans), text)

    def test_unsupported_language_has_no_english_fallback(self):
        with self.assertRaises(ValueError):
            locale("pl-PL")
        with self.assertRaises(ValueError):
            split_languages("안녕하세요", "en-GB", Identifier())

    def test_regional_english_and_stale_cloud_voice(self):
        mapping = {"en-GB": "bf_emma", "en-US": "af_heart", "ru": "rf_masha"}
        self.assertEqual(voice_for(locale(None), mapping, "af_heart"), "bf_emma")
        self.assertEqual(voice_for(locale("en-US"), mapping, "nova"), "af_heart")
        self.assertEqual(voice_for(locale("ru-RU"), mapping, "nova"), "rf_masha")


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_and_output_contract(self):
        calls = []

        def synthesize(*args: object) -> tuple[np.ndarray, list[str]]:
            calls.append(args)
            return np.zeros(2400, dtype=np.float32), ["ru", "en-GB"]

        app.state.engine = SimpleNamespace(synthesize=synthesize)
        app.state.lock = asyncio.Lock()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            text = "Привет, Alex!"
            response = await client.post(
                "/v1/audio/speech", json={"input": text, "language": "ru-RU"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "audio/mpeg")
            self.assertEqual(response.headers["x-kokoro-languages"], "ru,en-GB")
            self.assertGreater(len(response.content), 100)
            self.assertEqual(calls[0][0], text)
            for body in (
                {"input": text, "language": "ko-KR"},
                {"input": "x" * 5001},
                {"input": text, "response_format": "flac"},
            ):
                rejected = await client.post("/v1/audio/speech", json=body)
                self.assertEqual(rejected.status_code, 422)
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
