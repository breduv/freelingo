"""Official public-client OAuth for a local, administrator-owned ChatGPT connection."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from redis.asyncio import Redis

from app.core.config import settings
from app.services.llm_config import runtime_store

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
COOKIE_NAME = "freelingo_chatgpt_oauth"


class ChatGPTAuthError(ValueError):
    pass


def _redirect_uri() -> str:
    uri = settings.CHATGPT_REDIRECT_URI
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.path != "/auth/callback"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ChatGPTAuthError(
            "ChatGPT requires an HTTP loopback callback at 127.0.0.1/auth/callback"
        )
    return uri


async def prepare_sign_in(redis: Redis, admin_id: int) -> str:
    redirect_uri = _redirect_uri()
    async with runtime_store.locked():
        stored = runtime_store.read()
        stored.setdefault("host_id", "urn:uuid:" + str(uuid.uuid4()))
        runtime_store.write(stored)
    ticket = secrets.token_urlsafe(32)
    await redis.setex(
        f"chatgpt:ticket:{ticket}",
        600,
        json.dumps({"owner_id": admin_id, "generation": stored.get("chatgpt_generation", 0)}),
    )
    parsed = urlsplit(redirect_uri)
    return f"http://{parsed.netloc}/llm/auth/start?" + urlencode({"ticket": ticket})


async def begin_sign_in(redis: Redis, ticket: str) -> tuple[str, str]:
    raw_ticket = await redis.getdel(f"chatgpt:ticket:{ticket}")
    if not raw_ticket:
        raise ChatGPTAuthError("The sign-in link expired. Start again from AI settings.")
    transaction = json.loads(raw_ticket)
    stored = runtime_store.read()
    if (
        not isinstance(transaction, dict)
        or stored.get("chatgpt_generation", 0) != transaction.get("generation")
    ):
        raise ChatGPTAuthError("The sign-in link expired. Start again from AI settings.")
    connection = stored.get("chatgpt", {})
    client_id = connection.get("client_id") or "dynamic_agent_client"
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    browser_secret = secrets.token_urlsafe(32)
    redirect_uri = _redirect_uri()
    params = {
        "client_id": client_id,
        "ext_agent_host_id": stored["host_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode(),
    }
    if client_id == "dynamic_agent_client":
        params["agent_name_hint"] = "FreeLingo Local"
    elif connection.get("email"):
        params["login_hint"] = connection["email"]
    if client_id != "dynamic_agent_client" and connection.get("id_token"):
        params["id_token_hint"] = connection["id_token"]
    pending = {
        "owner_id": transaction["owner_id"],
        "generation": stored.get("chatgpt_generation", 0),
        "client_id": client_id,
        "subject": connection.get("subject"),
        "nonce": nonce,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "browser_hash": hashlib.sha256(browser_secret.encode()).hexdigest(),
    }
    await redis.setex(f"chatgpt:state:{state}", 600, json.dumps(pending))
    return AUTHORIZE_URL + "?" + urlencode(params), browser_secret


async def consume_callback(redis: Redis, state: str, browser_secret: str) -> dict:
    # Read and verify the browser binding before consuming a valid transaction.
    raw = await redis.get(f"chatgpt:state:{state}")
    if not raw or not browser_secret:
        raise ChatGPTAuthError("Missing or expired sign-in state. Start again from AI settings.")
    pending = json.loads(raw)
    digest = hashlib.sha256(browser_secret.encode()).hexdigest()
    if not secrets.compare_digest(pending["browser_hash"], digest):
        raise ChatGPTAuthError("Sign-in browser does not match the initiating browser.")
    raw = await redis.getdel(f"chatgpt:state:{state}")
    if not raw:
        raise ChatGPTAuthError("This sign-in response has already been used.")
    return pending


async def _token_request(data: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(TOKEN_URL, data={**data, "resource": RESOURCE})
        if response.status_code != 200:
            # Do not propagate provider response bodies containing credentials or codes.
            raise ChatGPTAuthError(
                f"ChatGPT authorization failed (HTTP {response.status_code}). Reconnect your account."
            )
        tokens = response.json()
        if tokens.get("token_type", "").lower() != "bearer" or not tokens.get("access_token"):
            raise ChatGPTAuthError("ChatGPT did not return a valid access token.")
        return tokens
    except httpx.HTTPError:
        raise ChatGPTAuthError(
            "Cannot reach ChatGPT authorization. Check your connection and retry."
        ) from None


async def _verify_identity(token: str, client_id: str, nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ("RS256", "ES256") or not header.get("kid"):
            raise ChatGPTAuthError("Unsupported ChatGPT identity signature.")
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(JWKS_URL)
            response.raise_for_status()
        key_data = next(k for k in response.json()["keys"] if k.get("kid") == header["kid"])
        key = jwt.PyJWK.from_dict(key_data, algorithm=header["alg"]).key
        identity = jwt.decode(
            token,
            key,
            algorithms=[header["alg"]],
            issuer=ISSUER,
            audience=client_id,
            options={"require": ["sub", "exp", "iat", "nonce"]},
            leeway=5,
        )
        if not isinstance(identity.get("sub"), str) or not identity["sub"]:
            raise ChatGPTAuthError("ChatGPT identity has no subject.")
        if not secrets.compare_digest(str(identity.get("nonce", "")), nonce):
            raise ChatGPTAuthError("ChatGPT identity nonce does not match.")
        return identity
    except jwt.PyJWTError, httpx.HTTPError, KeyError, StopIteration, TypeError, ValueError:
        raise ChatGPTAuthError(
            "ChatGPT identity could not be verified. Start sign-in again."
        ) from None


def _credential_update(tokens: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    scopes = tokens.get("scope", " ".join(previous.get("scopes", []))).split()
    return {
        **previous,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", previous.get("refresh_token", "")),
        "id_token": tokens.get("id_token", previous.get("id_token", "")),
        "scopes": scopes,
        "expires_at": time.time() + float(tokens.get("expires_in", 3600)),
    }


async def complete_sign_in(pending: dict, code: str, returned_client_id: str | None) -> dict:
    client_id = pending["client_id"]
    if client_id == "dynamic_agent_client":
        if not returned_client_id or returned_client_id == "dynamic_agent_client":
            raise ChatGPTAuthError("ChatGPT did not issue a client registration.")
        client_id = returned_client_id
    elif returned_client_id and returned_client_id != client_id:
        raise ChatGPTAuthError("ChatGPT returned a different client registration.")
    # Retain a newly issued registration even if the short-lived code expires.
    async with runtime_store.locked():
        stored = runtime_store.read()
        if stored.get("chatgpt_generation", 0) != pending.get("generation", 0):
            raise ChatGPTAuthError("The ChatGPT connection was disconnected during sign-in.")
        previous = stored.get("chatgpt", {})
        if previous.get("client_id") and previous["client_id"] != client_id:
            raise ChatGPTAuthError("The ChatGPT connection changed during sign-in. Start again.")
        stored["chatgpt"] = {**previous, "client_id": client_id}
        runtime_store.write(stored)
    tokens = await _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": pending["verifier"],
            "redirect_uri": pending["redirect_uri"],
        }
    )
    if not tokens.get("id_token"):
        raise ChatGPTAuthError("ChatGPT did not return a verifiable identity.")
    identity = await _verify_identity(tokens["id_token"], client_id, pending["nonce"])
    if pending.get("subject") and identity["sub"] != pending["subject"]:
        raise ChatGPTAuthError("Sign in with the originally connected ChatGPT account.")
    record = _credential_update(tokens)
    record.update(
        client_id=client_id,
        subject=identity["sub"],
        email=identity.get("email", ""),
        owner_id=pending["owner_id"],
    )
    async with runtime_store.locked():
        stored = runtime_store.read()
        if stored.get("chatgpt_generation", 0) != pending.get("generation", 0):
            raise ChatGPTAuthError("The ChatGPT connection was disconnected during sign-in.")
        if stored.get("chatgpt", {}).get("client_id") != client_id:
            raise ChatGPTAuthError("The ChatGPT connection changed during sign-in.")
        stored["chatgpt"] = record
        runtime_store.write(stored)
    return connection_status()


def connection_status() -> dict:
    record = runtime_store.read().get("chatgpt", {})
    return {
        "connected": bool(record.get("access_token")),
        "plan_enabled": PLAN_SCOPE in record.get("scopes", []),
        "email": record.get("email", ""),
        "expires_at": record.get("expires_at"),
        "usage_url": "https://chatgpt.com/settings/usage",
    }


async def access_token() -> str:
    async with runtime_store.locked():
        stored = runtime_store.read()
        record = stored.get("chatgpt", {})
        if not record.get("access_token") or PLAN_SCOPE not in record.get("scopes", []):
            raise ChatGPTAuthError("Connect ChatGPT in AI settings and allow ChatGPT plan usage.")
        if record.get("expires_at", 0) > time.time() + 90:
            return record["access_token"]
        if not record.get("refresh_token"):
            raise ChatGPTAuthError("ChatGPT session expired. Reconnect in AI settings.")
        tokens = await _token_request(
            {
                "grant_type": "refresh_token",
                "client_id": record["client_id"],
                "refresh_token": record["refresh_token"],
            }
        )
        record = _credential_update(tokens, record)
        stored["chatgpt"] = record
        runtime_store.write(stored)
        if PLAN_SCOPE not in record["scopes"]:
            raise ChatGPTAuthError("ChatGPT plan permission was revoked. Reconnect in AI settings.")
        return record["access_token"]


async def disconnect() -> bool:
    confirmed = True
    async with runtime_store.locked():
        stored = runtime_store.read()
        record = stored.get("chatgpt", {})
        if record.get("refresh_token"):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    discovery = await client.get(ISSUER + "/.well-known/openid-configuration")
                    discovery.raise_for_status()
                    endpoint = discovery.json()["revocation_endpoint"]
                    parsed = urlsplit(endpoint)
                    if parsed.scheme != "https" or parsed.netloc != "auth.openai.com":
                        raise ChatGPTAuthError("Unexpected revocation endpoint")
                    response = await client.post(
                        endpoint,
                        data={
                            "token": record["refresh_token"],
                            "token_type_hint": "refresh_token",
                            "client_id": record["client_id"],
                        },
                    )
                    confirmed = response.status_code == 200
            except httpx.HTTPError, KeyError, ChatGPTAuthError, ValueError:
                confirmed = False
        stored["chatgpt"] = {
            key: record[key]
            for key in ("client_id", "subject", "email", "owner_id")
            if key in record
        }
        stored["chatgpt_generation"] = stored.get("chatgpt_generation", 0) + 1
        runtime_store.write(stored)
    return confirmed


async def available_models() -> list[dict]:
    token = await access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            RESOURCE + "/models", headers={"Authorization": "Bearer " + token}
        )
    if response.status_code != 200:
        raise ChatGPTAuthError(
            f"ChatGPT model list unavailable (HTTP {response.status_code}). Check account access and limits."
        )
    return [
        {"id": m["slug"], "name": m.get("display_name", m["slug"])}
        for m in response.json().get("models", [])
        if m.get("visibility") == "list" and m.get("slug")
    ]
