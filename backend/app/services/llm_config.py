"""Installation-wide provider settings; credentials never leave backend storage."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import settings


class LLMOptions(BaseModel):
    provider: Literal["ollama", "openai", "anthropic", "deepseek", "chatgpt"] = "ollama"
    api_base: str = "http://host.docker.internal:11434"
    api_key: str = ""
    model: str = "gemma4:e4b"
    timeout_seconds: float = Field(default=120, ge=5, le=1800)
    max_output_tokens: int | None = Field(default=None, ge=64, le=131072)
    temperature: float | None = Field(default=None, ge=0, le=2)
    native_tools: bool = True
    reasoning_effort: Literal["", "none", "minimal", "low", "medium", "high", "xhigh", "max"] = ""

    @field_validator("api_base")
    @classmethod
    def validate_base(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        # Accessing .port also rejects malformed/out-of-range port strings.
        if parsed.port == 0:
            raise ValueError("API_BASE must use a valid nonzero port")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("API_BASE must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "API_BASE must not contain credentials, query parameters or a fragment"
            )
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Choose a model first")
        return value.strip()

    @model_validator(mode="after")
    def normalize_provider(self) -> LLMOptions:
        endpoints = {
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com",
            "deepseek": "https://api.deepseek.com/v1",
            "chatgpt": "https://api.openai.com/v1",
        }
        if self.provider in endpoints:
            self.api_base = endpoints[self.provider]
        else:
            self.api_base = self.api_base.removesuffix("/v1")
        if self.provider == "chatgpt":
            self.api_key = ""
        if self.provider == "anthropic" and self.temperature is not None and self.temperature > 1:
            raise ValueError("Anthropic temperature must be between 0 and 1")
        return self


class RuntimeStore:
    @property
    def path(self) -> Path:
        return Path(settings.LLM_CONFIG_PATH)

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        # Fail closed on damaged/unreadable configuration instead of switching providers.
        data = self.path.read_bytes()
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
        return json.loads(Fernet(key).decrypt(data))

    def write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
        payload = Fernet(key).encrypt(json.dumps(data, ensure_ascii=False).encode())
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".llm-")
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @asynccontextmanager
    async def locked(self):
        # Deployments run on Linux; flock also serializes rotating-token refreshes
        # between Uvicorn workers. Acquisition never blocks the event loop.
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        deadline = asyncio.get_running_loop().time() + 90
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError("LLM configuration is busy; try again") from None
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)


runtime_store = RuntimeStore()


def environment_options(provider: str | None = None) -> LLMOptions:
    provider = provider or settings.LLM_PROVIDER
    defaults = {
        "ollama": (settings.OLLAMA_BASE_URL, "", settings.OLLAMA_MODEL),
        "openai": ("https://api.openai.com/v1", settings.OPENAI_API_KEY, settings.OPENAI_MODEL),
        "anthropic": ("https://api.anthropic.com", settings.ANTHROPIC_API_KEY, settings.ANTHROPIC_MODEL),
        "deepseek": ("https://api.deepseek.com/v1", settings.DEEPSEEK_API_KEY, settings.DEEPSEEK_MODEL),
        "chatgpt": ("https://api.openai.com/v1", "", settings.CHATGPT_MODEL),
    }
    if provider not in defaults:
        raise ValueError(f"Unsupported LLM provider: {provider}")
    api_base, api_key, model = defaults[provider]
    return LLMOptions(
        provider=provider,
        api_base=api_base,
        api_key=api_key,
        model=model,
        timeout_seconds=settings.LLM_REQUEST_TIMEOUT_SECONDS,
        max_output_tokens=settings.ANTHROPIC_MAX_TOKENS if provider == "anthropic" else None,
        reasoning_effort=settings.OPENAI_REASONING_EFFORT if provider == "openai" else "",
    )


def configured_options() -> LLMOptions:
    stored = runtime_store.read().get("options")
    if stored is not None:
        return LLMOptions.model_validate(stored)
    return environment_options()
