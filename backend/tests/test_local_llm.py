"""Provider protocols, configuration isolation, semantic review, and OAuth security."""

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
from app.schemas.lessons import ExerciseContent, LessonContent
from app.services import chatgpt_auth
from app.services.lesson_quality import LessonReview, review_issues
from app.services.llm_adapter import LLMAdapter, LLMResponseError, OpenAIResponsesStream
from app.services.llm_config import LLMOptions, configured_options, runtime_store


async def test_operation_keeps_configuration_until_all_steps_finish():
    from app.services.llm_config import llm_operation

    original = LLMOptions(api_base="https://first.example/v1", model="first", api_key="first-key")
    replacement = LLMOptions(api_base="https://second.example/v1", model="second")
    runtime_store.write({"options": original.model_dump()})
    async with llm_operation():
        runtime_store.write({"options": replacement.model_dump()})
        await asyncio.sleep(0)
        assert configured_options() == original
    assert configured_options() == replacement


async def test_configuration_snapshots_are_isolated_between_concurrent_operations():
    from app.services.llm_config import llm_operation

    entered = asyncio.Event()
    changed = asyncio.Event()
    runtime_store.write({"options": LLMOptions(model="first").model_dump()})

    async def first_operation():
        async with llm_operation():
            entered.set()
            await changed.wait()
            return configured_options().model

    async def second_operation():
        await entered.wait()
        runtime_store.write({"options": LLMOptions(model="second").model_dump()})
        async with llm_operation():
            changed.set()
            return configured_options().model

    assert await asyncio.gather(first_operation(), second_operation()) == ["first", "second"]


def test_explanation_size_is_bounded_before_exercises_are_generated():
    from app.services.lesson_quality import ExplanationContent

    with pytest.raises(ValueError):
        ExplanationContent(
            text="x" * 1201,
            key_points=["Short rule"],
            examples=[{"sentence": "I am here.", "note": "Present tense"}],
        )


@pytest.mark.parametrize("url", ["http://localhost:wrong/v1", "http://localhost:99999/v1", "http://localhost:0/v1"])
def test_invalid_endpoint_ports_are_rejected_before_saving(url):
    with pytest.raises(ValueError):
        LLMOptions(api_base=url)


async def test_disabled_review_still_generates_a_lesson_in_separate_tasks():
    from app.services import lesson_quality

    context = {"lesson_type": "grammar", "cefr_level": "A1", "unit_id": "unit"}
    with (
        patch.object(lesson_quality, "generate_lesson_parts", AsyncMock(return_value=sample_lesson())) as parts,
        patch.object(lesson_quality, "review_lesson", AsyncMock()) as review_call,
    ):
        await lesson_quality.generate_verified_lesson(
            LessonContent, target_language="en-GB", context=context
        )
    parts.assert_awaited_once()
    assert parts.await_args.args[0] == context
    review_call.assert_not_awaited()


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
    monkeypatch.setattr(settings, "LESSON_QUALITY_REVIEW", False)


class MemoryRedis:
    def __init__(self):
        self.values = {}

    async def setex(self, key, ttl, value):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def getdel(self, key):
        return self.values.pop(key, None)


def sample_lesson(question="___ a very good teacher.", options=None):
    return LessonContent(
        lesson_type="grammar",
        title="Be",
        cefr_level="A1",
        explanation={"text": "Be"},
        exercises=[
            ExerciseContent(
                type="multiple_choice",
                question=question,
                options=options or ["I am", "He is", "They are", "You is"],
                correct=(options or ["I am"])[0],
            )
        ],
    )


def review(valid_indices):
    return LessonReview(
        explanation_issues=[],
        exercises=[
            {
                "index": 0,
                "valid": True,
                "issues": [],
                "options": [
                    {
                        "index": index,
                        "valid": index in valid_indices,
                        "reason": "Independent judgment",
                    }
                    for index in range(4)
                ],
            }
        ],
    )


def test_screenshot_with_two_correct_answers_is_rejected():
    issues = review_issues(sample_lesson(), review([0, 1]))
    assert any("found 2" in issue for issue in issues)


def test_independent_reviewer_cannot_silently_change_the_answer_key():
    assert any("answer key" in issue for issue in review_issues(sample_lesson(), review([1])))


def test_complete_unique_correct_review_passes():
    assert (
        review_issues(sample_lesson("I ___ a teacher.", ["am", "is", "are", "be"]), review([0]))
        == []
    )


def test_incomplete_or_duplicate_review_fails_closed():
    judgment = review([0])
    judgment.exercises[0].options.pop()
    assert review_issues(sample_lesson(), judgment)
    judgment = review([0])
    judgment.exercises.append(judgment.exercises[0])
    assert review_issues(sample_lesson(), judgment)


def test_duplicate_answer_options_rejected():
    with pytest.raises(ValueError, match="distinct"):
        sample_lesson(options=["I am", " i  AM ", "He is", "You is"])


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


async def test_quality_retry_repairs_ambiguity_and_blinds_the_reviewer(monkeypatch):
    from app.services import lesson_quality

    monkeypatch.setattr(settings, "LESSON_QUALITY_REVIEW", True)
    bad = sample_lesson()
    good = sample_lesson("I ___ a teacher.", ["am", "is", "are", "be"])
    context = {"lesson_type": "grammar", "cefr_level": "A1", "unit_id": "unit"}
    with (
        patch.object(
            lesson_quality, "generate_lesson_parts", AsyncMock(side_effect=[bad, good])
        ) as parts,
        patch.object(
            lesson_quality.llm_adapter,
            "structured_output",
            AsyncMock(side_effect=[review([0, 1]), review([0])]),
        ) as generate,
    ):
        result = await lesson_quality.generate_verified_lesson(
            LessonContent, target_language="en-GB", context=context
        )
    assert result.exercises[0].question == "I ___ a teacher."
    review_input = json.loads(generate.await_args_list[0].args[0][1]["content"])
    assert "correct" not in review_input["lesson"]["exercises"][0]
    assert review_input["context"] == context
    assert any("found 2" in issue for issue in parts.await_args_list[1].kwargs["feedback"])
    deadlines = [
        call.kwargs["deadline"] for call in parts.await_args_list + generate.await_args_list
    ]
    assert len(set(deadlines)) == 1


async def test_persistently_ambiguous_lesson_is_not_returned(monkeypatch):
    from app.services import lesson_quality

    monkeypatch.setattr(settings, "LESSON_QUALITY_REVIEW", True)
    with (
        patch.object(
            lesson_quality, "generate_lesson_parts", AsyncMock(return_value=sample_lesson())
        ) as parts,
        patch.object(
            lesson_quality.llm_adapter,
            "structured_output",
            AsyncMock(side_effect=[review([0, 1]), review([0, 1])]),
        ),
    ):
        with pytest.raises(LLMResponseError, match="quality review"):
            await lesson_quality.generate_verified_lesson(
                LessonContent,
                target_language="en-GB",
                context={"lesson_type": "grammar", "cefr_level": "A1", "unit_id": "unit"},
            )
    assert parts.await_count == settings.LESSON_QUALITY_MAX_ATTEMPTS


async def test_split_generation_preserves_curriculum_and_uses_smaller_tasks():
    from app.services import lesson_quality

    outline = lesson_quality.LessonOutline(
        title="Be",
        explanation={
            "text": "Use am with I.",
            "key_points": ["I am"],
            "examples": [{"sentence": "I am a student.", "note": "Present-simple be."}],
        },
        vocabulary=[
            {"word": word, "definition": word, "example": word}
            for word in ("teacher", "student", "happy")
        ],
    )
    exercises = lesson_quality.LessonExercises(
        exercises=[sample_lesson("I ___ a teacher.", ["am", "is", "are", "be"]).exercises[0]] * 3
    )
    context = {
        "lesson_type": "grammar",
        "cefr_level": "A1",
        "unit_id": "a1-unit-1",
        "grammar_points": ["to-be"],
    }
    with patch.object(
        lesson_quality.llm_adapter, "structured_output", AsyncMock(side_effect=[outline, exercises])
    ) as generate:
        result = await lesson_quality.generate_lesson_parts(
            context, LessonContent, deadline=asyncio.get_running_loop().time() + 60
        )
    assert result.unit_id == "a1-unit-1" and result.cefr_level == "A1"
    assert generate.await_args_list[0].args[1] is lesson_quality.LessonOutline
    assert generate.await_args_list[1].args[1] is lesson_quality.LessonExercises


def test_native_outline_requires_translated_teaching_material():
    from app.services.lesson_quality import NativeLessonOutline

    with pytest.raises(ValueError, match="native_explanation"):
        NativeLessonOutline.model_validate(
            {
                "title": "Be",
                "explanation": {
                    "text": "Be",
                    "key_points": ["I am"],
                    "examples": [{"sentence": "I am here.", "note": "Present-simple be"}],
                },
                "vocabulary": [
                    {"word": word, "definition": word, "example": word}
                    for word in ("teacher", "student", "happy")
                ],
            }
        )


async def test_writing_task_uses_grading_criteria_instead_of_answer_choices():
    from app.services import lesson_quality

    outline = lesson_quality.LessonOutline(
        title="Be",
        explanation={
            "text": "Use be.",
            "key_points": ["I am"],
            "examples": [{"sentence": "I am here.", "note": "Present-simple be"}],
        },
        vocabulary=[
            {"word": word, "definition": word, "example": word}
            for word in ("teacher", "student", "happy")
        ],
    )
    choice = sample_lesson().exercises[0]
    incorrect_writing = ExerciseContent(
        type="free_write",
        question="Introduce yourself.",
        options=["I am a student.", "I is a student."],
        correct="I am a student.",
    )
    exercises = lesson_quality.LessonExercises(exercises=[choice, choice, incorrect_writing])
    writing = lesson_quality.WritingTask(
        question="Write one sentence about your profession using present-simple be.",
        criteria=[
            "Mention a profession",
            "Use present-simple be with the correct subject agreement",
        ],
        model_answer="I am a teacher.",
        explanation="Use am with I.",
    )
    with patch.object(
        lesson_quality.llm_adapter,
        "structured_output",
        AsyncMock(side_effect=[outline, exercises, writing]),
    ):
        result = await lesson_quality.generate_lesson_parts(
            {"lesson_type": "grammar", "cefr_level": "A1", "unit_id": "unit"},
            LessonContent,
            deadline=asyncio.get_running_loop().time() + 60,
        )
    assert result.exercises[2].options == writing.criteria
    assert result.exercises[2].correct == "I am a teacher."
    assert "I is a student." not in result.exercises[2].options
