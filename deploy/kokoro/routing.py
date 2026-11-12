"""Language boundaries and voice selection, without rewriting the spoken text."""

import re
import unicodedata

SUPPORTED = frozenset({"en-GB", "en-US", "es", "fr", "hi", "it", "ja", "pt", "zh", "ru", "de"})
LATIN = frozenset({"en", "es", "fr", "it", "pt", "de"})
PREFIXES = {
    "a": "en-US",
    "b": "en-GB",
    "e": "es",
    "f": "fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt",
    "z": "zh",
    "r": "ru",
    "d": "de",
}


def locale(value: str | None) -> str:
    value = (value or "en-GB").replace("_", "-").lower()
    result = (
        "en-US"
        if value == "en-us"
        else "en-GB" if value.split("-")[0] == "en" else value.split("-")[0]
    )
    if result not in SUPPORTED:
        raise ValueError(f"Local Kokoro does not support language: {value}")
    return result


def voice_for(language: str, voices: dict[str, str], preferred: str | None = None) -> str:
    # A stale OpenAI voice or a voice for another language must not change pronunciation.
    if preferred and PREFIXES.get(preferred[:1]) == language:
        return preferred
    return voices[language]


def _script(char: str, cjk: str) -> str | None:
    name = unicodedata.name(char, "")
    if "CYRILLIC" in name:
        return "ru"
    if "LATIN" in name:
        return "latin"
    if "HIRAGANA" in name or "KATAKANA" in name or "CJK" in name:
        return cjk
    if "DEVANAGARI" in name:
        return "hi"
    if "HANGUL" in name:
        raise ValueError("Local Kokoro does not support Korean")
    if char.isalpha():
        raise ValueError(f"Local Kokoro does not support script: {name}")
    return None


def split_languages(text: str, primary: str | None, identifier) -> list[tuple[str, str]]:
    """Keep ambiguous short Latin text in the resource's language; use scripts first."""
    context = locale(primary)
    cjk = "ja" if context == "ja" or re.search(r"[\u3040-\u30ff]", text) else "zh"
    spans: list[tuple[str, str]] = []

    def append(script: str, part: str) -> None:
        language = script
        if script == "latin":
            language = context if context.split("-")[0] in LATIN else "en-GB"
            # Single words/names are not enough evidence to override an owned language.
            if len(re.findall(r"[^\W\d_]+", part, re.UNICODE)) >= 3:
                ranked = identifier.rank(part)
                best, probability = ranked[0]
                if probability >= 0.95 and probability - ranked[1][1] >= 0.5:
                    language = (
                        (context if context.startswith("en") else "en-GB") if best == "en" else best
                    )
        if spans and spans[-1][0] == language:
            spans[-1] = (language, spans[-1][1] + part)
        else:
            spans.append((language, part))

    for sentence in re.split(r"(?<=[.!?。！？])(?=\s)", text):
        current = None
        start = 0
        for index, char in enumerate(sentence):
            script = _script(char, cjk)
            if script is not None and current is not None and script != current:
                append(current, sentence[start:index])
                start = index
            if script is not None:
                current = script
        if sentence[start:]:
            append(current or context, sentence[start:])
    return spans
