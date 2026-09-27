"""oauth_manager.py — real OAuth 2.0 for Google (Drive/Gmail/Calendar), Dropbox,
Microsoft (OneDrive/Graph), Canva and Pinterest.

Three different things, deliberately kept in three different places:

  1. CREDENTIALS   — the app registration the USER creates with each provider
                      (client_id/client_secret/redirect_uri + which permissions
                      to request). Entered once in the Extensions UI.
                      -> state/oauth_apps.json  (client_secret stays local; the
                         frontend only ever gets it back masked)

  2. AUTHENTICATION — the actual browser round-trip: /oauth/<id>/start builds a
                      real authorization URL (with a random `state` and, where
                      the provider uses it, a PKCE code_challenge) and redirects
                      the browser to the provider's OWN login/consent page.
                      Nothing is "connected" at this point — this is Anthropic's
                      exact three-step distinction from the request.
                      -> _PENDING (in-memory, short-lived, one-shot)

  3. ACCESS TOKENS  — what /oauth/<id>/callback gets back after the provider
                      verifies the user and the user approves. A token is only
                      ever stored, and the connection only ever marked
                      "connected", AFTER a real follow-up API call (get the
                      account identity, list a Drive/Calendar/etc, whatever
                      that provider's `verify` step does below) succeeds.
                      -> state/oauth_tokens.json

Nothing in this file — or in the JSON files it writes — is ever handed to the
frontend directly; app.py's routes below decide what's safe to expose (never
access_token/refresh_token/client_secret).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

STATE_DIR: Path | None = None
APPS_FILE: Path | None = None
TOKENS_FILE: Path | None = None

_PENDING: dict[str, dict] = {}          # state -> {provider, verifier, created_at, extra}
_PENDING_TTL = 600                       # 10 minutes to complete the browser round-trip


def configure(state_dir: Path) -> None:
    global STATE_DIR, APPS_FILE, TOKENS_FILE
    STATE_DIR = state_dir
    APPS_FILE = state_dir / "oauth_apps.json"       # credentials (incl. client_secret) — NEVER sent to frontend raw
    TOKENS_FILE = state_dir / "oauth_tokens.json"    # access/refresh tokens — NEVER sent to frontend at all


# ---------------------------------------------------------------------------
# Provider registry — real, current (checked against each provider's own
# documentation, not a tutorial) endpoints and scopes. No endpoint here is
# guessed.
# ---------------------------------------------------------------------------

GOOGLE_SERVICE_SCOPES = {
    "drive":    {"read": "https://www.googleapis.com/auth/drive.readonly", "write": "https://www.googleapis.com/auth/drive"},
    "gmail":    {"read": "https://www.googleapis.com/auth/gmail.readonly", "write": "https://www.googleapis.com/auth/gmail.modify"},
    # gmail.modify covers create/label/trash; sending a NEW draft/message additionally needs gmail.compose, added below when "create" is requested.
    "calendar": {"read": "https://www.googleapis.com/auth/calendar.readonly", "write": "https://www.googleapis.com/auth/calendar.events"},
}


def _google_verify(access_token: str) -> tuple[bool, str, str | None]:
    try:
        r = requests.get("https://www.googleapis.com/oauth2/v3/userinfo", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    except requests.RequestException as e:
        return False, "", f"Network error verifying the Google account: {e}"
    if not r.ok:
        return False, "", f"Google rejected the token while verifying the account (HTTP {r.status_code})."
    return True, r.json().get("email", "connected"), None


def _dropbox_verify(access_token: str) -> tuple[bool, str, str | None]:
    try:
        r = requests.post("https://api.dropboxapi.com/2/users/get_current_account", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    except requests.RequestException as e:
        return False, "", f"Network error verifying the Dropbox account: {e}"
    if not r.ok:
        return False, "", f"Dropbox rejected the token while verifying the account (HTTP {r.status_code})."
    body = r.json()
    return True, (body.get("email") or body.get("name", {}).get("display_name") or "connected"), None


def _microsoft_verify(access_token: str) -> tuple[bool, str, str | None]:
    try:
        r = requests.get("https://graph.microsoft.com/v1.0/me", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    except requests.RequestException as e:
        return False, "", f"Network error verifying the Microsoft account: {e}"
    if not r.ok:
        return False, "", f"Microsoft Graph rejected the token while verifying the account (HTTP {r.status_code})."
    body = r.json()
    return True, (body.get("mail") or body.get("userPrincipalName") or "connected"), None


def _canva_verify(access_token: str) -> tuple[bool, str, str | None]:
    try:
        r = requests.get("https://api.canva.com/rest/v1/users/me/profile", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    except requests.RequestException as e:
        return False, "", f"Network error verifying the Canva account: {e}"
    if not r.ok:
        return False, "", f"Canva rejected the token while verifying the account (HTTP {r.status_code})."
    body = r.json().get("profile", r.json())
    return True, (body.get("display_name") or "connected"), None


def _pinterest_verify(access_token: str) -> tuple[bool, str, str | None]:
    try:
        r = requests.get("https://api.pinterest.com/v5/user_account", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    except requests.RequestException as e:
        return False, "", f"Network error verifying the Pinterest account: {e}"
    if not r.ok:
        return False, "", f"Pinterest rejected the token while verifying the account (HTTP {r.status_code})."
    body = r.json()
    return True, (body.get("username") or "connected"), None


PROVIDERS: dict[str, dict] = {
    "google": {
        "label": "Google", "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token", "uses_pkce": True, "auth_style": "body",
        "scope_sep": " ", "extra_authorize_params": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"},
        "verify": _google_verify, "needs_tenant": False,
        "services": GOOGLE_SERVICE_SCOPES,
    },
    "dropbox": {
        "label": "Dropbox", "auth_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token", "uses_pkce": True, "auth_style": "body",
        "scope_sep": " ", "extra_authorize_params": {"token_access_type": "offline"},
        "verify": _dropbox_verify, "needs_tenant": False,
        "permission_scopes": {"read": "files.metadata.read files.content.read", "write": "files.metadata.write files.content.write"},
    },
    "microsoft": {
        "label": "Microsoft (OneDrive)", "auth_url_tmpl": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url_tmpl": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", "uses_pkce": True, "auth_style": "body",
        "scope_sep": " ", "extra_authorize_params": {},
        "verify": _microsoft_verify, "needs_tenant": True,
        "permission_scopes": {"read": "offline_access Files.Read Files.Read.All User.Read", "write": "offline_access Files.ReadWrite Files.ReadWrite.All User.Read"},
    },
    "canva": {
        "label": "Canva", "auth_url": "https://www.canva.com/api/oauth/authorize",
        "token_url": "https://api.canva.com/rest/v1/oauth/token", "uses_pkce": True, "auth_style": "basic",
        "scope_sep": " ", "extra_authorize_params": {},
        "verify": _canva_verify, "needs_tenant": False,
        "permission_scopes": {"read": "profile:read design:meta:read design:content:read folder:read", "write": "profile:read design:meta:read design:content:read design:content:write folder:read folder:write"},
    },
    "pinterest": {
        "label": "Pinterest", "auth_url": "https://www.pinterest.com/oauth/",
        "token_url": "https://api.pinterest.com/v5/oauth/token", "uses_pkce": True, "auth_style": "basic",
        "scope_sep": ",", "extra_authorize_params": {},
        "verify": _pinterest_verify, "needs_tenant": False,
        "permission_scopes": {"read": "boards:read,pins:read,user_accounts:read", "write": "boards:read,boards:write,pins:read,pins:write,user_accounts:read"},
    },
}


# ---------------------------------------------------------------------------
# App credentials (client id/secret/redirect_uri) — NOT tokens.
# ---------------------------------------------------------------------------

def _load_apps() -> dict:
    if not APPS_FILE or not APPS_FILE.exists():
        return {}
    try:
        return json.loads(APPS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_apps(data: dict) -> None:
    APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    APPS_FILE.write_text(json.dumps(data, indent=2))
    try:
        import os
        os.chmod(APPS_FILE, 0o600)
    except OSError:
        pass


def set_app_credentials(provider: str, *, client_id: str, client_secret: str, redirect_uri: str,
                         tenant_id: str | None = None, services: list[str] | None = None, permission: str = "read") -> dict:
    if provider not in PROVIDERS:
        return {"ok": False, "error": f"Unknown provider '{provider}'."}
    if not client_id or not client_secret or not redirect_uri:
        return {"ok": False, "error": "Client ID, Client Secret and Redirect URI are all required."}
    apps = _load_apps()
    apps[provider] = {
        "client_id": client_id.strip(), "client_secret": client_secret.strip(), "redirect_uri": redirect_uri.strip(),
        "tenant_id": (tenant_id or "common").strip() if PROVIDERS[provider].get("needs_tenant") else None,
        "services": services or [], "permission": permission if permission in ("read", "write") else "read",
        "saved_at": time.time(),
    }
    _save_apps(apps)
    return {"ok": True}


def get_app_config(provider: str) -> dict | None:
    saved = _load_apps().get(provider)
    if saved:
        return saved
    return _env_fallback(provider)


_ENV_VARS = {
    "google": {"client_id": "GOOGLE_CLIENT_ID", "client_secret": "GOOGLE_CLIENT_SECRET", "redirect_uri": "GOOGLE_REDIRECT_URI"},
    "dropbox": {"client_id": "DROPBOX_APP_KEY", "client_secret": "DROPBOX_APP_SECRET", "redirect_uri": "DROPBOX_REDIRECT_URI"},
    "microsoft": {"client_id": "MICROSOFT_CLIENT_ID", "client_secret": "MICROSOFT_CLIENT_SECRET", "redirect_uri": "MICROSOFT_REDIRECT_URI", "tenant_id": "MICROSOFT_TENANT_ID"},
    "canva": {"client_id": "CANVA_CLIENT_ID", "client_secret": "CANVA_CLIENT_SECRET", "redirect_uri": "CANVA_REDIRECT_URI"},
    "pinterest": {"client_id": "PINTEREST_CLIENT_ID", "client_secret": "PINTEREST_CLIENT_SECRET", "redirect_uri": "PINTEREST_REDIRECT_URI"},
}


def _env_fallback(provider: str) -> dict | None:
    """Optional fallback per .env.example — only used when nothing has been saved
    through the UI yet. The UI (Extensions → Configure) is the primary path."""
    import os
    names = _ENV_VARS.get(provider)
    if not names:
        return None
    client_id, client_secret = os.environ.get(names["client_id"]), os.environ.get(names["client_secret"])
    if not client_id or not client_secret:
        return None
    return {
        "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": os.environ.get(names["redirect_uri"], ""),
        "tenant_id": os.environ.get(names.get("tenant_id", ""), "common") if PROVIDERS[provider].get("needs_tenant") else None,
        "services": list(GOOGLE_SERVICE_SCOPES.keys()) if provider == "google" else [],
        "permission": "read", "from_env": True,
    }


def app_config_status(provider: str) -> dict:
    """Safe-for-frontend view of the saved app config — client_secret is masked, never returned in full."""
    cfg = get_app_config(provider)
    if not cfg:
        return {"configured": False}
    secret = cfg.get("client_secret", "")
    return {
        "configured": True,
        "client_id": cfg.get("client_id", ""),
        "client_secret_masked": (secret[:4] + "…" + secret[-4:]) if len(secret) > 10 else "•" * len(secret),
        "redirect_uri": cfg.get("redirect_uri", ""),
        "tenant_id": cfg.get("tenant_id"),
        "services": cfg.get("services", []),
        "permission": cfg.get("permission", "read"),
    }


def delete_app_credentials(provider: str) -> None:
    apps = _load_apps()
    apps.pop(provider, None)
    _save_apps(apps)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def _load_tokens() -> dict:
    if not TOKENS_FILE or not TOKENS_FILE.exists():
        return {}
    try:
        return json.loads(TOKENS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tokens(data: dict) -> None:
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(data, indent=2))
    try:
        import os
        os.chmod(TOKENS_FILE, 0o600)
    except OSError:
        pass


def is_connected(provider: str) -> bool:
    return provider in _load_tokens()


def connection_status(provider: str) -> dict | None:
    """Safe-for-frontend status — never includes access_token/refresh_token."""
    tok = _load_tokens().get(provider)
    if not tok:
        return None
    return {
        "connected": True, "account": tok.get("account"), "scope": tok.get("scope", ""),
        "connected_at": tok.get("connected_at"), "last_verified": tok.get("last_verified"),
        "expires_at": tok.get("expires_at"), "has_refresh_token": bool(tok.get("refresh_token")),
    }


def disconnect(provider: str) -> None:
    tokens = _load_tokens()
    tok = tokens.pop(provider, None)
    _save_tokens(tokens)
    # Best-effort revoke where the provider supports it — never fatal if it fails, the local token is gone either way.
    if not tok:
        return
    try:
        if provider == "google" and tok.get("access_token"):
            requests.post("https://oauth2.googleapis.com/revoke", params={"token": tok["access_token"]}, timeout=10)
        elif provider == "dropbox" and tok.get("access_token"):
            requests.post("https://api.dropboxapi.com/2/auth/token/revoke", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=10)
    except requests.RequestException:
        pass


def get_valid_access_token(provider: str) -> tuple[str | None, str | None]:
    """(access_token, error). Refreshes automatically if expired and a refresh_token is on file —
    the user should never have to log in again just because an hour passed."""
    tokens = _load_tokens()
    tok = tokens.get(provider)
    if not tok:
        return None, f"{PROVIDERS.get(provider, {}).get('label', provider)} isn't connected."
    if tok.get("expires_at") and time.time() < tok["expires_at"] - 60:
        return tok["access_token"], None
    if not tok.get("refresh_token"):
        return None, f"{PROVIDERS.get(provider, {}).get('label', provider)}'s access has expired and there's no refresh token — please reconnect."
    ok, err = _refresh(provider, tok)
    if not ok:
        return None, err
    return _load_tokens()[provider]["access_token"], None


def _refresh(provider: str, tok: dict) -> tuple[bool, str | None]:
    app = get_app_config(provider)
    if not app:
        return False, f"{provider} credentials are no longer configured."
    pconf = PROVIDERS[provider]
    token_url = pconf.get("token_url") or pconf["token_url_tmpl"].format(tenant=app.get("tenant_id") or "common")
    data = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "redirect_uri": app["redirect_uri"]}
    auth = None
    if pconf["auth_style"] == "basic":
        auth = (app["client_id"], app["client_secret"])
    else:
        data["client_id"], data["client_secret"] = app["client_id"], app["client_secret"]
    try:
        r = requests.post(token_url, data=data, auth=auth, headers={"Accept": "application/json"}, timeout=15)
    except requests.RequestException as e:
        return False, f"Network error refreshing {pconf['label']}'s token: {e}"
    if not r.ok:
        return False, _friendly_token_error(pconf["label"], r)
    body = r.json()
    tokens = _load_tokens()
    entry = tokens.get(provider, {})
    entry["access_token"] = body["access_token"]
    if body.get("refresh_token"):
        entry["refresh_token"] = body["refresh_token"]  # some providers rotate it
    entry["expires_at"] = time.time() + int(body.get("expires_in", 3600))
    tokens[provider] = entry
    _save_tokens(tokens)
    return True, None


def _friendly_token_error(label: str, r: requests.Response) -> str:
    try:
        body = r.json()
        code = body.get("error") or body.get("error_code") or ""
    except ValueError:
        code = ""
    if code in ("invalid_grant", "expired_token") or r.status_code in (400, 401):
        return f"{label} authorization expired or was revoked. Please connect {label} again."
    return f"{label} returned an error refreshing the connection (HTTP {r.status_code})."


# ---------------------------------------------------------------------------
# Authorization Code + PKCE flow
# ---------------------------------------------------------------------------

def _gc_pending():
    now = time.time()
    for k in [k for k, v in _PENDING.items() if now - v["created_at"] > _PENDING_TTL]:
        _PENDING.pop(k, None)


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(provider: str) -> tuple[str | None, str | None]:
    """(url, error). Never marks anything connected — this is step 2 of 3, the
    redirect to the provider's OWN login/consent screen."""
    if provider not in PROVIDERS:
        return None, f"Unknown provider '{provider}'."
    app = get_app_config(provider)
    if not app:
        return None, f"No credentials saved for {PROVIDERS[provider]['label']} yet. Add Client ID/Secret/Redirect URI first."
    pconf = PROVIDERS[provider]
    _gc_pending()
    state = secrets.token_urlsafe(24)
    verifier, challenge = (None, None)
    scope = _scopes_for(provider, app)
    params = {
        "client_id": app["client_id"], "redirect_uri": app["redirect_uri"], "response_type": "code",
        "scope": scope, "state": state,
    }
    params.update(pconf.get("extra_authorize_params", {}))
    if pconf["uses_pkce"]:
        verifier, challenge = _pkce_pair()
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    auth_url = pconf.get("auth_url") or pconf["auth_url_tmpl"].format(tenant=app.get("tenant_id") or "common")
    _PENDING[state] = {"provider": provider, "verifier": verifier, "created_at": time.time()}
    return f"{auth_url}?{urlencode(params)}", None


def _scopes_for(provider: str, app: dict) -> str:
    pconf = PROVIDERS[provider]
    perm = app.get("permission", "read")
    if provider == "google":
        parts = []
        for svc in app.get("services") or []:
            svc_scopes = pconf["services"].get(svc)
            if not svc_scopes:
                continue
            parts.append(svc_scopes["write"] if perm == "write" else svc_scopes["read"])
            if svc == "gmail" and perm == "write":
                parts.append("https://www.googleapis.com/auth/gmail.compose")  # composing NEW drafts needs this in addition to .modify
        parts.append("openid")
        parts.append("https://www.googleapis.com/auth/userinfo.email")
        return pconf["scope_sep"].join(dict.fromkeys(parts))  # de-dupe, keep order
    return pconf["permission_scopes"]["write" if perm == "write" else "read"]


def handle_callback(provider: str, *, code: str | None, state: str | None, error: str | None) -> dict:
    """Steps 3-7 of the spec's flow, in order: exchange code -> get tokens ->
    verify the account with a REAL API call -> only THEN save + report connected.
    Returns {"ok": True, "account":..., "services": [...] } or {"ok": False, "error": "...", "denied": bool}."""
    if error:
        denied = error in ("access_denied", "user_cancelled", "consent_required")
        return {"ok": False, "denied": denied, "error": "Authorization was cancelled." if denied else f"{provider} returned an error: {error}"}
    _gc_pending()
    pending = _PENDING.pop(state, None) if state else None
    if not pending or pending["provider"] != provider:
        return {"ok": False, "denied": False, "error": "This authorization link is invalid or expired (state mismatch). Please try connecting again."}
    if not code:
        return {"ok": False, "denied": False, "error": "No authorization code was returned."}

    app = get_app_config(provider)
    if not app:
        return {"ok": False, "denied": False, "error": f"{PROVIDERS[provider]['label']} credentials are no longer configured."}
    pconf = PROVIDERS[provider]
    token_url = pconf.get("token_url") or pconf["token_url_tmpl"].format(tenant=app.get("tenant_id") or "common")
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": app["redirect_uri"]}
    if pending.get("verifier"):
        data["code_verifier"] = pending["verifier"]
    auth = None
    if pconf["auth_style"] == "basic":
        auth = (app["client_id"], app["client_secret"])
    else:
        data["client_id"], data["client_secret"] = app["client_id"], app["client_secret"]
    try:
        r = requests.post(token_url, data=data, auth=auth, headers={"Accept": "application/json"}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "denied": False, "error": f"Network error exchanging the authorization code: {e}"}
    if not r.ok:
        return {"ok": False, "denied": False, "error": _friendly_token_error(pconf["label"], r)}
    body = r.json()
    access_token = body.get("access_token")
    if not access_token:
        return {"ok": False, "denied": False, "error": f"{pconf['label']} didn't return an access token."}

    ok, account, verr = pconf["verify"](access_token)
    if not ok:
        return {"ok": False, "denied": False, "error": verr or "Could not verify the account after authorization."}

    tokens = _load_tokens()
    now = time.time()
    tokens[provider] = {
        "access_token": access_token, "refresh_token": body.get("refresh_token"),
        "expires_at": now + int(body.get("expires_in", 3600)), "account": account,
        "scope": body.get("scope", _scopes_for(provider, app)), "connected_at": now, "last_verified": now,
    }
    _save_tokens(tokens)
    services = app.get("services") or ([provider] if provider != "google" else [])
    return {"ok": True, "account": account, "services": services, "permission": app.get("permission", "read")}
