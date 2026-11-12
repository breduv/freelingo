"""Provider protocols, configuration isolation, and OAuth security."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import jwt
import pytest
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings
from app.services import chatgpt_auth
from app.services.llm_adapter import LLMAdapter, LLMResponseError, OpenAIResponsesStream
from app.services.llm_config import LLMOptions, configured_options, runtime_store


@pytest.mark.parametrize("url", ["http://localhost:wrong/v1", "http://localhost:99999/v1", "http://localhost:0/v1"])
def test_invalid_endpoint_ports_are_rejected_before_saving(url):
    with pytest.raises(ValueError):
        LLMOptions(api_base=url)


async def test_disconnect_invalidates_an_inflight_sign_in():
    pending = {"client_id": "client", "generation": 0}
    await chatgpt_auth.disconnect()
    with patch.object(chatgpt_auth, "_token_request", AsyncMock()) as exchange:
        with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="disconnected"):
            await chatgpt_auth.complete_sign_in(pending, "code", None)
    exchange.assert_not_awaited()


async def test_disconnect_also_invalidates_unopened_browser_tickets():
    redis = MemoryRedis()
    ticket_url = await chatgpt_auth.prepare_sign_in(redis, 42)
    ticket = parse_qs(urlsplit(ticket_url).query)["ticket"][0]
    await chatgpt_auth.disconnect()
    with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="expired"):
        await chatgpt_auth.begin_sign_in(redis, ticket)


@pytest.mark.parametrize("provider", ["ollama", "openai", "anthropic", "deepseek", "chatgpt"])
def test_original_providers_and_chatgpt_are_supported(provider):
    assert LLMOptions(provider=provider).provider == provider


def test_removed_generic_provider_is_rejected_for_new_settings():
    with pytest.raises(ValueError):
        LLMOptions(provider="openai_compatible")


def test_removed_generic_provider_is_rejected_in_saved_settings():
    runtime_store.write(
        {
            "options": {
                "provider": "openai_compatible",
                "api_base": "https://api.openai.com/v1",
                "model": "old-model",
                "api_key": "old-key",
            }
        }
    )
    with pytest.raises(ValueError):
        configured_options()


async def test_anthropic_snapshot_closes_its_native_client():
    from app.services import llm_adapter as llm

    native = SimpleNamespace(close=AsyncMock())
    with patch.object(llm._anthropic, "AsyncAnthropic", return_value=native) as create:
        adapter = LLMAdapter(LLMOptions(provider="anthropic", api_key="native-key", model="claude"))
        await adapter.close()
    create.assert_called_once_with(api_key="native-key", max_retries=0)
    native.close.assert_awaited_once()
    assert adapter.client is None


async def test_anthropic_model_discovery_uses_native_api(client, admin_user):
    _, headers = admin_user
    models = SimpleNamespace(
        list=AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(id="claude", display_name="Claude")]))
    )
    adapter = SimpleNamespace(_anthropic=SimpleNamespace(models=models), close=AsyncMock())
    with patch("app.routers.llm_settings.LLMAdapter", return_value=adapter):
        response = await client.post(
            "/api/admin/llm/models", headers=headers,
            json={"provider": "anthropic", "model": "claude"},
        )
    assert response.status_code == 200
    assert response.json() == {"models": [{"id": "claude", "name": "Claude"}]}
    models.list.assert_awaited_once_with(limit=100, timeout=20)
    adapter.close.assert_awaited_once()


async def test_provider_defaults_never_expose_environment_keys(client, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "private-anthropic-key")
    _, headers = admin_user
    response = await client.get("/api/admin/llm", headers=headers)
    assert response.status_code == 200
    defaults = response.json()["provider_defaults"]
    assert set(defaults) == {"ollama", "openai", "anthropic", "deepseek", "chatgpt"}
    assert defaults["anthropic"]["has_api_key"] is True
    assert "private-anthropic-key" not in response.text
    assert all("api_key" not in item["options"] for item in defaults.values())

@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LLM_CONFIG_PATH", str(tmp_path / "settings.json"))


class MemoryRedis:
    def __init__(self):
        self.values = {}

    async def setex(self, key, ttl, value):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def getdel(self, key):
        return self.values.pop(key, None)


def test_storage_encrypts_and_preserves_credentials():
    runtime_store.write(
        {
            "options": {"api_key": "secret-provider-key"},
            "chatgpt": {"refresh_token": "secret-refresh"},
        }
    )
    assert b"secret-provider-key" not in runtime_store.path.read_bytes()
    assert b"secret-refresh" not in runtime_store.path.read_bytes()
    assert runtime_store.read()["chatgpt"]["refresh_token"] == "secret-refresh"
    runtime_store.path.write_bytes(b"damaged")
    with pytest.raises(InvalidToken):
        configured_options()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:pass@example.com/v1",
        "https://example.com/v1?api_key=secret",
        "https://example.com/#secret",
    ],
)
def test_api_base_rejects_credential_urls_and_invalid_schemes(url):
    with pytest.raises(ValueError):
        LLMOptions(api_base=url)


async def test_ollama_uses_its_original_chat_completions_contract():
    requests = []

    async def endpoint(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "local",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "READY"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    from openai import AsyncOpenAI

    options = LLMOptions(
        provider="ollama",
        api_base="http://test/v1",
        model="local",
        native_tools=False,
    )
    adapter = LLMAdapter(options)
    await adapter.client.close()
    adapter.client = AsyncOpenAI(
        base_url=options.api_base + "/v1",
        api_key="local",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(endpoint)),
        max_retries=0,
    )
    try:
        assert await adapter.chat([{"role": "user", "content": "Hi"}]) == "READY"
        assert set(requests[0]) == {"model", "messages", "stream"}
    finally:
        await adapter.client.close()


async def test_truncated_chat_response_is_not_accepted():
    adapter = LLMAdapter(LLMOptions(provider="ollama"))
    try:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"partial":'), finish_reason="length"
                )
            ]
        )
        with patch.object(
            adapter.client.chat.completions, "create", AsyncMock(return_value=response)
        ):
            with pytest.raises(LLMResponseError, match="truncated"):
                await adapter._do_chat([])
    finally:
        await adapter.client.close()


async def test_chatgpt_requests_are_streamed_stateless_and_use_developer_messages():
    adapter = LLMAdapter(
        LLMOptions(
            provider="chatgpt", model="available-model", max_output_tokens=1234, temperature=0.3
        )
    )
    stream = AsyncMock()
    with (
        patch("app.services.chatgpt_auth.access_token", AsyncMock(return_value="oauth-secret")),
        patch.object(adapter.client.responses, "create", AsyncMock(return_value=stream)) as create,
    ):
        result = await adapter._do_chat([{"role": "system", "content": "Tutor rules"}], True)
        assert isinstance(result, OpenAIResponsesStream)
        kwargs = create.await_args.kwargs
        assert kwargs["store"] is False and kwargs["stream"] is True
        assert kwargs["input"][0]["role"] == "developer"
        assert "max_output_tokens" not in kwargs and "temperature" not in kwargs
        assert adapter.client.api_key == "oauth-secret"
    await adapter.client.close()


async def test_oauth_requires_same_browser_and_consumes_callback_once():
    redis = MemoryRedis()
    ticket_url = await chatgpt_auth.prepare_sign_in(redis, 42)
    ticket = parse_qs(urlsplit(ticket_url).query)["ticket"][0]
    url, cookie = await chatgpt_auth.begin_sign_in(redis, ticket)
    params = parse_qs(urlsplit(url).query)
    state = params["state"][0]
    assert params["client_id"] == ["dynamic_agent_client"]
    assert params["redirect_uri"][0].startswith("http://127.0.0.1:")
    with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="browser"):
        await chatgpt_auth.consume_callback(redis, state, "different-browser")
    pending = await chatgpt_auth.consume_callback(redis, state, cookie)
    assert pending["owner_id"] == 42
    with pytest.raises(chatgpt_auth.ChatGPTAuthError):
        await chatgpt_auth.consume_callback(redis, state, cookie)
    with pytest.raises(chatgpt_auth.ChatGPTAuthError):
        await chatgpt_auth.begin_sign_in(redis, ticket)


async def test_identity_signature_audience_and_nonce_are_verified():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="test-key", alg="RS256")
    now = int(time.time())
    claims = {
        "iss": chatgpt_auth.ISSUER,
        "sub": "account",
        "aud": "client",
        "iat": now,
        "exp": now + 300,
        "nonce": "expected",
    }
    token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test-key"})
    response = httpx.Response(
        200, json={"keys": [jwk]}, request=httpx.Request("GET", chatgpt_auth.JWKS_URL)
    )
    with patch("app.services.chatgpt_auth.httpx.AsyncClient") as client:
        client.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
        assert (await chatgpt_auth._verify_identity(token, "client", "expected"))[
            "sub"
        ] == "account"
        with pytest.raises(chatgpt_auth.ChatGPTAuthError):
            await chatgpt_auth._verify_identity(token, "other-client", "expected")
        with pytest.raises(chatgpt_auth.ChatGPTAuthError):
            await chatgpt_auth._verify_identity(token, "client", "wrong-nonce")


async def test_refresh_rotation_is_serialized_and_latest_token_is_saved():
    runtime_store.write(
        {
            "chatgpt": {
                "client_id": "client",
                "access_token": "old",
                "refresh_token": "old-refresh",
                "expires_at": 0,
                "scopes": [chatgpt_auth.PLAN_SCOPE],
            }
        }
    )
    with patch(
        "app.services.chatgpt_auth._token_request",
        AsyncMock(
            return_value={"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}
        ),
    ) as refresh:
        results = await asyncio.gather(chatgpt_auth.access_token(), chatgpt_auth.access_token())
    assert results == ["new", "new"]
    assert refresh.await_count == 1
    assert runtime_store.read()["chatgpt"]["refresh_token"] == "new-refresh"


async def test_identity_only_connection_cannot_consume_plan():
    runtime_store.write({"chatgpt": {"access_token": "token", "scopes": ["openid"]}})
    with pytest.raises(chatgpt_auth.ChatGPTAuthError, match="allow"):
        await chatgpt_auth.access_token()


async def test_settings_require_admin_and_do_not_expose_keys(client, test_user, admin_user):
    _, user_headers = test_user
    _, admin_headers = admin_user
    assert (await client.get("/api/admin/llm")).status_code == 401
    assert (await client.get("/api/admin/llm", headers=user_headers)).status_code == 403
    runtime_store.write({"options": LLMOptions(api_key="secret-value").model_dump()})
    response = await client.get("/api/admin/llm", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["has_api_key"] is True
    assert "secret-value" not in response.text
    assert "api_key" not in response.json()["options"]


async def test_changing_api_host_never_forwards_saved_key(client, admin_user):
    _, headers = admin_user
    runtime_store.write(
        {
            "options": LLMOptions(
                provider="ollama",
                api_base="https://first.example/v1",
                api_key="first-key",
            ).model_dump()
        }
    )
    response = await client.put(
        "/api/admin/llm",
        headers=headers,
        json={
            "provider": "ollama",
            "api_base": "https://second.example/v1",
            "model": "new",
        },
    )
    assert response.status_code == 200
    assert configured_options().api_key == ""


async def test_failed_subscription_does_not_switch_to_paid_api(client, admin_user):
    _, headers = admin_user
    response = await client.put(
        "/api/admin/llm", headers=headers, json={"provider": "chatgpt", "model": "available-model"}
    )
    assert response.status_code == 409
    assert configured_options().provider == "ollama"
    assert not runtime_store.read().get("options")
