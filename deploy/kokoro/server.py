"""FreeLingo's local Kokoro service: stock languages plus Russian and German."""

import asyncio
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException, Response
from kokoro import KModel, KPipeline
from langid.langid import LanguageIdentifier
from langid.langid import model as language_model
from misaki.espeak import EspeakG2P
from pydantic import BaseModel, Field
from routing import PREFIXES, SUPPORTED, locale, split_languages, voice_for

from assets import GERMAN_REPO, RUSSIAN_REPO, STANDARD_REPO, prepare

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
SAMPLE_RATE = 24000
VOICE_FILE = Path(os.getenv("KOKORO_VOICES_FILE", "/etc/kokoro/voices.json"))


def configured_voices() -> dict[str, str]:
    voices = json.loads(VOICE_FILE.read_text(encoding="utf-8"))
    if (
        not isinstance(voices, dict)
        or set(voices) != SUPPORTED
        or any(not isinstance(v, str) or PREFIXES.get(v[:1]) != k for k, v in voices.items())
    ):
        raise ValueError("Invalid Kokoro language-to-voice mapping")
    return voices


class Engine:
    def __init__(self) -> None:
        self.models = {}
        self.pipelines = {}
        self.packs = {}
        self.device = os.getenv("KOKORO_DEVICE", "cuda")
        if self.device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("Kokoro CUDA was requested but no CUDA device is available")
        torch.set_num_threads(int(os.getenv("KOKORO_CPU_THREADS", "2")))
        self.standard, self.russian, self.german, self.ru_g2p = prepare()
        self.de_g2p = EspeakG2P(language="de")
        self.identifier = LanguageIdentifier.from_modelstring(language_model, norm_probs=True)
        self.identifier.set_languages(["en", "de", "es", "fr", "it", "pt"])
        configured_voices()
        # Validate all three checkpoints before readiness is reported.
        for family in ("standard", "ru", "de"):
            self.get_model(family)
        for language, voice in configured_voices().items():
            self.voice_pack(voice, language)

    def get_model(self, family):
        if family not in self.models:
            repo, directory, filename, config = {
                "standard": (
                    STANDARD_REPO,
                    self.standard,
                    "kokoro-v1_0.pth",
                    self.standard / "config.json",
                ),
                "ru": (
                    RUSSIAN_REPO,
                    self.russian,
                    "kokoro-ru-v2-base.pth",
                    self.russian / "config.json",
                ),
                "de": (
                    GERMAN_REPO,
                    self.german,
                    "kikiri_german_martin_ep10.pth",
                    self.standard / "config.json",
                ),
            }[family]
            model = KModel(repo_id=repo, config=str(config), model=str(directory / filename))
            if family == "de":
                # Kikiri uses torch.nn.utils.parametrizations.weight_norm. The pinned
                # Kokoro runtime uses legacy weight_g/weight_v; its permissive loader
                # otherwise leaves these layers uninitialized. Reload strictly.
                state = torch.load(directory / filename, map_location="cpu", weights_only=True)
                for name, parameters in state.items():
                    normalized = {
                        key.removeprefix("module.")
                        .replace(".parametrizations.weight.original0", ".weight_g")
                        .replace(".parametrizations.weight.original1", ".weight_v"): value
                        for key, value in parameters.items()
                    }
                    target = getattr(model, name)
                    # The legacy ONNX-compatible runtime uses affine InstanceNorm;
                    # Kikiri's non-affine normalization is exactly scale=1, bias=0.
                    for module_name, layer in target.named_modules():
                        if isinstance(layer, torch.nn.InstanceNorm1d) and layer.affine:
                            normalized.setdefault(
                                f"{module_name}.weight", torch.ones_like(layer.weight)
                            )
                            normalized.setdefault(
                                f"{module_name}.bias", torch.zeros_like(layer.bias)
                            )
                    target.load_state_dict(normalized, strict=True)
            self.models[family] = model.to(self.device).eval()
        return self.models[family]

    def voice_pack(self, voice, language):
        if voice not in self.packs:
            if language == "ru":
                path = (
                    self.russian
                    / "voices"
                    / ({"rf_masha": "masha.pt", "rf_sveta": "sveta.pt"}.get(voice, "invalid"))
                )
            elif language == "de":
                path = self.german / "voices/martin.pt" if voice == "dm_martin" else Path("invalid")
            else:
                path = self.standard / "voices" / f"{voice}.pt"
            if not path.is_file() or "/" in voice or "\\" in voice:
                raise ValueError(f"Unknown local voice: {voice}")
            self.packs[voice] = torch.load(path, map_location="cpu", weights_only=True)
        return self.packs[voice]

    def synthesize(self, text, primary, preferred, speed):
        voices = configured_voices()
        spans = split_languages(text, primary, self.identifier)
        output = []
        languages = []
        for language, part in spans:
            if not any(c.isalnum() for c in part):
                continue
            voice = voice_for(language, voices, preferred)
            pack = self.voice_pack(voice, language)
            languages.append(language)
            if language in {"ru", "de"}:
                phonemes, _ = (self.ru_g2p if language == "ru" else self.de_g2p)(part)
                model = self.get_model(language)
                # Split at word boundaries before the model's 510-phoneme limit.
                chunks = []
                for word in phonemes.split():
                    if len(word) > 400:
                        raise ValueError("A phonetic word exceeds Kokoro's context limit")
                    if chunks and len(chunks[-1]) + len(word) + 1 <= 400:
                        chunks[-1] += " " + word
                    else:
                        chunks.append(word)
                for chunk in chunks:
                    style = pack[min(len(chunk) - 1, pack.shape[0] - 1)].reshape(1, -1)
                    output.append(model(chunk, style, speed, return_output=True).audio.numpy())
            else:
                code = next(k for k, v in PREFIXES.items() if v == language)
                if code not in self.pipelines:
                    self.pipelines[code] = KPipeline(
                        lang_code=code, repo_id=STANDARD_REPO, model=self.get_model("standard")
                    )
                for result in self.pipelines[code](part, voice=pack, speed=speed):
                    if result.audio is not None:
                        output.append(result.audio.numpy())
        if not output:
            raise ValueError("No speakable text")
        # Encode once after concatenating PCM; concatenated MP3 files are not a valid join.
        audio = np.concatenate(output).astype(np.float32)
        return audio, languages


@asynccontextmanager
async def lifespan(app):
    app.state.engine = await asyncio.to_thread(Engine)
    app.state.lock = asyncio.Lock()
    logger.info("Kokoro ready: standard + Russian + German on %s", app.state.engine.device)
    yield


app = FastAPI(lifespan=lifespan)


class SpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=5000)
    voice: str | None = None
    language: str | None = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: str = "mp3"


@app.get("/v1/models")
async def models():
    return {
        "object": "list",
        "data": [{"id": "kokoro", "object": "model"}],
        "languages": sorted(SUPPORTED),
    }


@app.get("/v1/audio/voices")
async def voices():
    return {"voices": sorted({*configured_voices().values(), "rf_sveta"})}


def encode(audio, format):
    stream = io.BytesIO()
    sf.write(
        stream,
        audio,
        SAMPLE_RATE,
        format="MP3" if format == "mp3" else "WAV",
        subtype="MPEG_LAYER_III" if format == "mp3" else "PCM_16",
    )
    return stream.getvalue()


@app.post("/v1/audio/speech")
async def speech(body: SpeechRequest):
    if body.response_format not in {"mp3", "wav"}:
        raise HTTPException(422, "Supported output formats: mp3, wav")
    try:
        locale(body.language)
        async with app.state.lock:
            audio, languages = await asyncio.to_thread(
                app.state.engine.synthesize, body.input, body.language, body.voice, body.speed
            )
            data = await asyncio.to_thread(encode, audio, body.response_format)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(
        data,
        media_type="audio/mpeg" if body.response_format == "mp3" else "audio/wav",
        headers={"X-Kokoro-Languages": ",".join(languages)},
    )
