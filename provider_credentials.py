"""provider_credentials.py — the multi-provider credential manager.

This replaces the "one active provider" model (apikey.json: {"provider": "...",
"api_key": "..."}) with N independently-connected providers, stored in
state/provider_credentials.json. Only this module reads/writes that file —
the router, provider_runtime, image_gen and the UI all go through the
functions below, so credentials never get duplicated across JSON files.

Migration: the first time this file doesn't exist yet, we import whatever
was already connected in the legacy stores this project grew over time:
  - apikey.json              (single chat provider — app.py)
  - image_keys.json          (OpenAI/Gemini image credentials — image_gen.py)
  - state/extensions.json    (chatgpt/claude/gemini "ask a second AI" connectors — extensions.py)
Nothing is deleted and no secret is ever logged/printed; the legacy files are
left exactly as they were so nothing downstream that still reads them breaks.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_STORE_FILE: Path | None = None
_APP_DIR: Path | None = None

# Every task/capability this app currently understands. "local_ai" defaults to
# ollama out of the box since that is the whole point of having it installed.
DEFAULT_TASK_DEFAULTS: dict[str, str | None] = {
    "chat": None,
    "coding": None,
    "code_review": None,
    "image_generation": None,
    "vision": None,
    "research": None,
    "document_analysis": None,
    "local_ai": "ollama",
}


def configure(app_dir: Path, state_dir: Path) -> None:
    """Call once at startup, after STATE_DIR exists. Triggers the one-time
    legacy migration if state/provider_credentials.json doesn't exist yet."""
    global _STORE_FILE, _APP_DIR
    _APP_DIR = app_dir
    _STORE_FILE = state_dir / "provider_credentials.json"
    if not _STORE_FILE.exists():
        _migrate_legacy()


def _empty_store() -> dict:
    return {"providers": {}, "defaults": dict(DEFAULT_TASK_DEFAULTS), "migrated_from": None, "migrated_at": None}


def _load() -> dict:
    if not _STORE_FILE or not _STORE_FILE.exists():
        return _empty_store()
    try:
        data = json.loads(_STORE_FILE.read_text())
        if not isinstance(data, dict):
            return _empty_store()
        data.setdefault("providers", {})
        data.setdefault("defaults", dict(DEFAULT_TASK_DEFAULTS))
        for k, v in DEFAULT_TASK_DEFAULTS.items():
            data["defaults"].setdefault(k, v)
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_store()


def _save(data: dict) -> None:
    if not _STORE_FILE:
        return
    _STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STORE_FILE.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(_STORE_FILE, 0o600)
    except OSError:
        pass


def _migrate_legacy() -> None:
    data = _empty_store()
    migrated_from: list[str] = []

    old_key_file = (_APP_DIR / "apikey.json") if _APP_DIR else None
    if old_key_file and old_key_file.exists():
        try:
            old = json.loads(old_key_file.read_text())
            pid, key = old.get("provider"), old.get("api_key")
            if pid and key:
                data["providers"][pid] = {"api_key": key, "enabled": True}
                data["defaults"]["chat"] = pid
                migrated_from.append("apikey.json")
        except (json.JSONDecodeError, OSError):
            pass

    old_image_file = (_APP_DIR / "image_keys.json") if _APP_DIR else None
    if old_image_file and old_image_file.exists():
        try:
            old = json.loads(old_image_file.read_text())
            for pid, cfg in (old.get("providers") or {}).items():
                key = cfg.get("api_key")
                if key and pid not in data["providers"]:
                    data["providers"][pid] = {"api_key": key, "enabled": True}
            if old.get("default") and not data["defaults"].get("image_generation"):
                data["defaults"]["image_generation"] = old["default"]
            migrated_from.append("image_keys.json")
        except (json.JSONDecodeError, OSError):
            pass

    old_ext_file = (_APP_DIR / "state" / "extensions.json") if _APP_DIR else None
    ext_to_provider = {"chatgpt": "openai", "claude": "anthropic", "gemini": "gemini"}
    if old_ext_file and old_ext_file.exists():
        try:
            old = json.loads(old_ext_file.read_text())
            for ext_id, pid in ext_to_provider.items():
                conn = (old.get("connections") or {}).get(ext_id)
                key = (conn or {}).get("api_key")
                if key and pid not in data["providers"]:
                    data["providers"][pid] = {"api_key": key, "enabled": True}
                    migrated_from.append(f"extensions.json:{ext_id}")
        except (json.JSONDecodeError, OSError):
            pass

    data["migrated_from"] = migrated_from or None
    data["migrated_at"] = time.time() if migrated_from else None
    _save(data)


# ---------------------------------------------------------------------------
# Credential manager API — the only place that knows where credentials live.
# ---------------------------------------------------------------------------

def get_provider_credentials(provider_id: str) -> dict | None:
    return _load()["providers"].get(provider_id)


def set_provider_credentials(provider_id: str, credentials: dict) -> None:
    """Adds/replaces one provider's credentials WITHOUT touching any other
    provider — this is the "no one active key" fix: connecting OpenAI never
    disconnects Gemini."""
    data = _load()
    existing = data["providers"].get(provider_id, {})
    merged = {**existing, **credentials}
    merged.setdefault("enabled", True)
    data["providers"][provider_id] = merged
    _save(data)


def remove_provider_credentials(provider_id: str) -> None:
    data = _load()
    data["providers"].pop(provider_id, None)
    for task, pid in list(data["defaults"].items()):
        if pid == provider_id:
            data["defaults"][task] = None
    _save(data)


def has_provider_credentials(provider_id: str) -> bool:
    c = get_provider_credentials(provider_id)
    if not c or not c.get("enabled", True):
        return False
    if provider_id == "ollama":
        return True  # ollama needs no api key, just to be reachable (checked elsewhere)
    return bool(c.get("api_key"))


def list_configured_providers() -> list[str]:
    return [pid for pid, c in _load()["providers"].items() if c.get("enabled", True)]


def mask_credentials(provider_id: str) -> str | None:
    c = get_provider_credentials(provider_id)
    if not c:
        return None
    if provider_id == "ollama":
        return c.get("base_url")
    key = c.get("api_key") or ""
    if not key:
        return None
    return key[:4] + "…" + key[-4:] if len(key) > 10 else "•" * len(key)


def get_defaults() -> dict:
    data = _load()
    return {**DEFAULT_TASK_DEFAULTS, **data.get("defaults", {})}


def set_default(task: str, provider_id: str | None) -> None:
    data = _load()
    data["defaults"][task] = provider_id
    _save(data)


def get_api_key(provider_id: str) -> str | None:
    c = get_provider_credentials(provider_id)
    return c.get("api_key") if c else None


def get_ollama_base_url(fallback: str) -> str:
    c = get_provider_credentials("ollama")
    return (c or {}).get("base_url") or fallback


def status_snapshot(known_provider_ids: list[str]) -> dict:
    """Masked, safe-to-return-to-the-frontend status for every known provider id."""
    data = _load()
    out = []
    for pid in known_provider_ids:
        c = data["providers"].get(pid)
        out.append({
            "id": pid,
            "configured": bool(c and c.get("enabled", True) and (pid == "ollama" or c.get("api_key"))),
            "masked": mask_credentials(pid) if c else None,
        })
    return {"providers": out, "defaults": get_defaults(), "migrated_from": data.get("migrated_from")}
