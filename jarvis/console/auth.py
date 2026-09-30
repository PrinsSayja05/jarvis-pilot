"""Console login: Keycloak (OIDC authorization code flow with PKCE) when configured and reachable,
otherwise demo mode, so nobody is locked out when Keycloak is down or not set up yet.

Configuration (.env):
    KEYCLOAK_URL            e.g. http://192.168.178.75:8080
    KEYCLOAK_REALM          e.g. wamocon
    KEYCLOAK_CLIENT_ID      e.g. jarvis-console (confidential client, standard flow)
    KEYCLOAK_CLIENT_SECRET
    CONSOLE_SESSION_SECRET  random string that signs the session cookie (sessions survive restarts)
    KEYCLOAK_ADMIN_ROLE     realm role that may use the demo switcher (default: jarvis-admin)

The session is a signed, HttpOnly cookie (8 h). The user is matched to Jira by e-mail.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

logger = logging.getLogger("jarvis.auth")
router = APIRouter()

SESSION_COOKIE = "jarvis_session"
LOGIN_COOKIE = "jarvis_login"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_SECONDS = 10 * 60
_MODE_CHECK_SECONDS = 60

_lock = threading.Lock()
_state: dict = {"mode": None, "reason": "", "checked": 0.0, "discovery": None}
_jwk_clients: dict[str, jwt.PyJWKClient] = {}
_fallback_secret = secrets.token_bytes(32)


def _settings() -> dict[str, str]:
    return {k: os.environ.get(k, "").strip() for k in
            ("KEYCLOAK_URL", "KEYCLOAK_REALM", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET", "CONSOLE_SESSION_SECRET", "KEYCLOAK_ADMIN_ROLE")}


def auth_mode() -> tuple[str, str]:
    """("keycloak", realm info) or ("demo", reason). Re-checked at most once a minute."""
    with _lock:
        if _state["mode"] and time.monotonic() - _state["checked"] < _MODE_CHECK_SECONDS:
            return _state["mode"], _state["reason"]
        s = _settings()
        if not (s["KEYCLOAK_URL"] and s["KEYCLOAK_REALM"] and s["KEYCLOAK_CLIENT_ID"] and s["KEYCLOAK_CLIENT_SECRET"]):
            mode, reason, discovery = "demo", "Keycloak is not configured (KEYCLOAK_* not set)", None
        else:
            url = f"{s['KEYCLOAK_URL'].rstrip('/')}/realms/{s['KEYCLOAK_REALM']}/.well-known/openid-configuration"
            try:
                r = httpx.get(url, timeout=3)
                r.raise_for_status()
                discovery = r.json()
                mode, reason = "keycloak", f"realm {s['KEYCLOAK_REALM']} at {s['KEYCLOAK_URL']}"
            except Exception as exc:
                mode, reason, discovery = "demo", f"Keycloak not reachable ({type(exc).__name__}), demo mode instead", None
        if (mode, reason) != (_state["mode"], _state["reason"]):
            (logger.info if mode == "keycloak" else logger.warning)("console auth: %s (%s)", mode, reason)
            if mode == "keycloak" and not s["CONSOLE_SESSION_SECRET"]:
                logger.warning("console auth: CONSOLE_SESSION_SECRET not set, logins are lost when the service restarts")
        _state.update(mode=mode, reason=reason, checked=time.monotonic(), discovery=discovery)
        return mode, reason


# ---------------------------------------------------------------- signed cookies

def _secret() -> bytes:
    configured = _settings()["CONSOLE_SESSION_SECRET"]
    return configured.encode() if configured else _fallback_secret


def _sign(data: dict, seconds: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({**data, "exp": int(time.time()) + seconds}).encode()).decode().rstrip("=")
    mac = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def _unsign(value: str | None) -> dict | None:
    if not value or "." not in value:
        return None
    payload, mac = value.rsplit(".", 1)
    if not hmac.compare_digest(mac, hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    return data if data.get("exp", 0) > time.time() else None


def current_user(request) -> dict | None:
    """The logged-in Keycloak user (from the session cookie), or None."""
    return _unsign(request.cookies.get(SESSION_COOKIE))


def _set_cookie(response: Response, request: Request, name: str, value: str, seconds: int) -> None:
    response.set_cookie(name, value, max_age=seconds, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")


def _base(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


# ---------------------------------------------------------------- routes

@router.get("/auth/login")
def login(request: Request, next: str = "/") -> Response:
    mode, _ = auth_mode()
    if mode != "keycloak":
        return RedirectResponse("/")
    d, s = _state["discovery"], _settings()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id": s["KEYCLOAK_CLIENT_ID"], "response_type": "code", "scope": "openid email profile",
              "redirect_uri": f"{_base(request)}/auth/callback", "state": state, "nonce": nonce,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    response = RedirectResponse(f"{d['authorization_endpoint']}?{urlencode(params)}")
    _set_cookie(response, request, LOGIN_COOKIE,
                _sign({"state": state, "nonce": nonce, "verifier": verifier, "next": _safe_next(next)}, LOGIN_SECONDS), LOGIN_SECONDS)
    return response


def _verify(token: str, discovery: dict, audience: str | None) -> dict:
    uri = discovery["jwks_uri"]
    client = _jwk_clients.setdefault(uri, jwt.PyJWKClient(uri, cache_keys=True))
    key = client.get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=["RS256", "ES256", "PS256"], issuer=discovery["issuer"],
                      audience=audience, options={"verify_aud": audience is not None}, leeway=30)


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
    pending = _unsign(request.cookies.get(LOGIN_COOKIE))
    mode, _ = auth_mode()
    if error or mode != "keycloak" or not pending or not code or not hmac.compare_digest(state, pending.get("state", "")):
        logger.warning("console auth: login callback rejected (error=%s, state ok=%s)", error or "-", bool(pending) and state == pending.get("state"))
        return RedirectResponse("/auth/login")
    d, s = _state["discovery"], _settings()
    r = httpx.post(d["token_endpoint"], auth=(s["KEYCLOAK_CLIENT_ID"], s["KEYCLOAK_CLIENT_SECRET"]), timeout=10,
                   data={"grant_type": "authorization_code", "code": code, "redirect_uri": f"{_base(request)}/auth/callback",
                         "code_verifier": pending["verifier"]})
    if r.status_code != 200:
        logger.warning("console auth: token exchange failed: HTTP %s", r.status_code)
        return RedirectResponse("/auth/login")
    tokens = r.json()
    try:
        claims = _verify(tokens["id_token"], d, s["KEYCLOAK_CLIENT_ID"])
        if claims.get("nonce") != pending["nonce"]:
            raise jwt.InvalidTokenError("nonce mismatch")
        access = _verify(tokens["access_token"], d, None)   # realm roles live in the access token
    except Exception as exc:
        logger.warning("console auth: token rejected: %s", exc)
        return RedirectResponse("/auth/login")
    roles = access.get("realm_access", {}).get("roles", [])
    user = {"sub": claims["sub"], "username": claims.get("preferred_username", ""), "email": claims.get("email", ""),
            "name": claims.get("name") or claims.get("preferred_username", ""),
            "admin": (s["KEYCLOAK_ADMIN_ROLE"] or "jarvis-admin") in roles}
    logger.info("console auth: login %s <%s> admin=%s", user["username"], user["email"], user["admin"])
    response = RedirectResponse(pending.get("next", "/"))
    _set_cookie(response, request, SESSION_COOKIE, _sign(user, SESSION_SECONDS), SESSION_SECONDS)
    response.delete_cookie(LOGIN_COOKIE, path="/")
    return response


@router.get("/auth/logout")
def logout(request: Request) -> Response:
    user = current_user(request)
    mode, _ = auth_mode()
    target = "/"
    if mode == "keycloak":
        params = {"client_id": _settings()["KEYCLOAK_CLIENT_ID"], "post_logout_redirect_uri": f"{_base(request)}/"}
        target = f"{_state['discovery']['end_session_endpoint']}?{urlencode(params)}"
    if user:
        logger.info("console auth: logout %s", user.get("username"))
    response = RedirectResponse(target)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
