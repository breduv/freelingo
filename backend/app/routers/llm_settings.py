"""Administrator-only local provider configuration and ChatGPT account connection."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_redis, require_admin
from app.core.limiter import limiter
from app.models.user import User
from app.services import chatgpt_auth
from app.services.llm_adapter import LLMAdapter
from app.services.llm_config import LLMOptions, configured_options, environment_options, runtime_store


def prevent_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["llm-settings"], dependencies=[Depends(prevent_caching)])


class SettingsUpdate(LLMOptions):
    keep_api_key: bool = True


def resolve_options(update: SettingsUpdate) -> LLMOptions:
    data = update.model_dump(exclude={"keep_api_key"})
    previous = configured_options()
    if update.keep_api_key and not update.api_key:
        candidate = (
            previous
            if update.provider == previous.provider
            else environment_options(update.provider)
        )
        if update.api_base == candidate.api_base:
            data["api_key"] = candidate.api_key
    return LLMOptions.model_validate(data)


@router.get("/llm", include_in_schema=False)
@limiter.limit("60/minute")
async def settings_page(request: Request):
    return RedirectResponse("/settings/ai", headers={"Cache-Control": "no-store"})


@router.get("/api/admin/llm")
@limiter.limit("30/minute")
async def read_settings(request: Request, _admin: User = Depends(require_admin)):
    options = configured_options()
    return {
        "options": options.model_dump(exclude={"api_key"}),
        "has_api_key": bool(options.api_key),
        "chatgpt": chatgpt_auth.connection_status(),
        "source": "saved" if runtime_store.read().get("options") else "environment",
        "provider_defaults": {
            provider: {
                "options": defaults.model_dump(exclude={"api_key"}),
                "has_api_key": bool(defaults.api_key),
            }
            for provider in ("ollama", "openai", "anthropic", "deepseek", "chatgpt")
            for defaults in (environment_options(provider),)
        },
    }


@router.put("/api/admin/llm")
@limiter.limit("10/minute")
async def save_settings(
    request: Request, update: SettingsUpdate, _admin: User = Depends(require_admin)
):
    async with runtime_store.locked():
        options = resolve_options(update)
        if options.provider == "chatgpt" and not chatgpt_auth.connection_status()["plan_enabled"]:
            raise HTTPException(
                status_code=409, detail="Сначала подключи ChatGPT и разреши использование подписки."
            )
        stored = runtime_store.read()
        stored["options"] = options.model_dump()
        runtime_store.write(stored)
    return {"saved": True, "restart_required": False}


@router.post("/api/admin/llm/models")
@limiter.limit("20/minute")
async def list_models(
    request: Request, update: SettingsUpdate, _admin: User = Depends(require_admin)
):
    options = resolve_options(update)
    try:
        if options.provider == "chatgpt":
            return {"models": await chatgpt_auth.available_models()}
        adapter = LLMAdapter(options)
        try:
            if options.provider == "anthropic":
                page = await adapter._anthropic.models.list(limit=100, timeout=20)
                return {
                    "models": [
                        {"id": model.id, "name": model.display_name} for model in page.data
                    ]
                }
            page = await adapter.client.models.list(timeout=20)
            return {"models": [{"id": model.id, "name": model.id} for model in page.data]}
        finally:
            await adapter.close()
    except chatgpt_auth.ChatGPTAuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Не удалось получить модели. Проверь провайдера, его ключ и доступность сервера Ollama.",
        ) from None


@router.post("/api/admin/llm/test")
@limiter.limit("5/minute")
async def test_connection(
    request: Request, update: SettingsUpdate, _admin: User = Depends(require_admin)
):
    options = resolve_options(update)
    adapter = LLMAdapter(options)
    try:
        answer = await adapter.chat([{"role": "user", "content": "Reply with exactly READY."}])
        return {"ok": True, "model": options.model, "answer": answer}
    except chatgpt_auth.ChatGPTAuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Модель не ответила. Проверь адрес, ключ, имя модели, лимиты и подключение ChatGPT. Отключи неподдерживаемые возможности API.",
        ) from None
    finally:
        await adapter.close()


@router.post("/api/admin/llm/chatgpt/connect")
@limiter.limit("5/minute")
async def connect_chatgpt(
    request: Request, admin: User = Depends(require_admin), redis: Redis = Depends(get_redis)
):
    try:
        return {"url": await chatgpt_auth.prepare_sign_in(redis, admin.id)}
    except chatgpt_auth.ChatGPTAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/llm/auth/start", include_in_schema=False)
@limiter.limit("10/minute")
async def start_chatgpt_sign_in(request: Request, ticket: str, redis: Redis = Depends(get_redis)):
    try:
        url, cookie = await chatgpt_auth.begin_sign_in(redis, ticket)
    except chatgpt_auth.ChatGPTAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    response = RedirectResponse(
        url,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
    response.set_cookie(
        chatgpt_auth.COOKIE_NAME,
        cookie,
        httponly=True,
        samesite="lax",
        max_age=600,
        path="/auth/callback",
    )
    return response


@router.get("/auth/callback", include_in_schema=False)
@limiter.limit("10/minute")
async def chatgpt_callback(
    request: Request, redis: Redis = Depends(get_redis), db: AsyncSession = Depends(get_db)
):
    try:
        pending = await chatgpt_auth.consume_callback(
            redis,
            request.query_params.get("state", ""),
            request.cookies.get(chatgpt_auth.COOKIE_NAME, ""),
        )
        admin = await db.get(User, pending["owner_id"])
        if admin is None or not admin.is_active or admin.role != "admin":
            raise chatgpt_auth.ChatGPTAuthError(
                "The initiating administrator no longer has access."
            )
        if request.query_params.get("error"):
            raise chatgpt_auth.ChatGPTAuthError(
                "ChatGPT sign-in was declined. Return to AI settings to retry."
            )
        code = request.query_params.get("code")
        if not code:
            raise chatgpt_auth.ChatGPTAuthError("ChatGPT did not return an authorization code.")
        await chatgpt_auth.complete_sign_in(pending, code, request.query_params.get("client_id"))
        response = RedirectResponse(
            settings.APP_BASE_URL.rstrip("/") + "/settings/ai?chatgpt=connected", status_code=303
        )
    except chatgpt_auth.ChatGPTAuthError, httpx.HTTPError, ValueError:
        # The callback contains an ephemeral authorization code: never echo its URL or errors.
        response = HTMLResponse(
            '<!doctype html><html lang="ru"><meta charset="utf-8"><title>FreeLingo — ChatGPT</title><h1>Не удалось подключить ChatGPT</h1><p>Вход отменён, устарел или не прошёл проверку. Вернись в настройки ИИ и повтори вход. Если OpenAI не предоставляет подключение для аккаунта, выбери Ollama, OpenAI, Anthropic или DeepSeek.</p><a href="/settings/ai">Настройки ИИ</a></html>',
            status_code=400,
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.delete_cookie(chatgpt_auth.COOKIE_NAME, path="/auth/callback")
    return response


@router.post("/api/admin/llm/chatgpt/disconnect")
@limiter.limit("5/minute")
async def disconnect_chatgpt(request: Request, _admin: User = Depends(require_admin)):
    confirmed = await chatgpt_auth.disconnect()
    return {"disconnected": True, "remote_revocation_confirmed": confirmed}
