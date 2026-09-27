"""provider_runtime — the tool-calling loops for every provider kind, with real error handling.

Differences from the original loops in app.py:
  * failures are STRUCTURED (agent_core.classify_provider_error) and never dumped to the chat as raw JSON
  * 429 / 5xx / timeouts / network drops are retried with Retry-After-aware backoff, cancellable
  * on final failure the loop yields {"type": "provider_error", "error", "messages"} INSTEAD of "done", so a
    router can move the conversation to another provider WITHOUT losing the tool results gathered so far
  * tool results are compacted while staying valid JSON (the old code sliced JSON mid-string at 4000 chars)
  * Ollama is called through its native /api/chat so `num_ctx` can be set (the OpenAI-compatible route can't),
    which matters: a long agent system prompt + tool schemas overflows Ollama's small default context
  * OFFLINE is enforced below this layer (agent_core.install_offline_guard)
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Callable, Iterator

import requests

import agent_core as ac

MAX_TOOL_ITERATIONS = 20
RETRYABLE_KINDS = {"rate_limit", "timeout", "network", "http_408", "http_409", "http_500", "http_502", "http_503", "http_504"}
TOOL_RESULT_LIMIT = 6000

ToolRunner = Callable  # (name, args, cancel_event) -> generator returning result dict


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def compact_tool_result(result, limit: int = TOOL_RESULT_LIMIT) -> str:
    """JSON text for the model. Long strings/lists are shrunk with explicit markers so the JSON stays VALID and the
    status fields (ok / error / verified / paths) always survive."""
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "tool result was not serialisable"})
    if len(text) <= limit:
        return text

    def shrink(o, depth=0, str_cap=1200):
        if isinstance(o, str):
            return o if len(o) <= str_cap else o[: str_cap - 300] + f"…[+{len(o) - str_cap + 300} chars cut]"
        if isinstance(o, list):
            keep = 30 if depth < 2 else 12
            out = [shrink(x, depth + 1, str_cap) for x in o[:keep]]
            if len(o) > keep:
                out.append(f"…[+{len(o) - keep} more items not shown]")
            return out
        if isinstance(o, dict):
            return {k: shrink(v, depth + 1, str_cap) for k, v in o.items()}
        return o

    for cap in (1200, 500, 200):
        text = json.dumps(shrink(result, 0, cap), ensure_ascii=False, default=str)
        if len(text) <= limit:
            return text
    keep_keys = ("ok", "error", "error_code", "message", "path", "destination", "operation", "requested", "completed",
                 "failed", "skipped", "verified", "status", "url", "returncode")
    core = {k: result[k] for k in keep_keys if isinstance(result, dict) and k in result}
    core["_truncated"] = True
    return json.dumps(core, ensure_ascii=False, default=str)[:limit]


def public_error(err: dict) -> dict:
    return {k: v for k, v in err.items() if k not in ("detail",) or True}


def _sleep_cancellable(seconds: float, cancel_event) -> bool:
    """True if slept fully, False if cancelled."""
    end = time.time() + seconds
    while time.time() < end:
        if cancel_event.is_set():
            return False
        time.sleep(min(0.25, max(0.0, end - time.time())))
    return True


def _abortable_post(url, headers, payload, timeout, cancel_event):
    """POST in a worker thread and poll the cancel flag, so STOP takes effect within ~0.25 s even while a slow model is
    still generating. Returns None when cancelled (the abandoned request's result is discarded)."""
    box: dict = {}

    def work():
        try:
            box["resp"] = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except BaseException as e:  # noqa: BLE001 — re-raised in the caller's thread
            box["exc"] = e
    th = threading.Thread(target=work, daemon=True)
    th.start()
    while th.is_alive():
        th.join(0.25)
        if cancel_event.is_set():
            return None
    if "exc" in box:
        raise box["exc"]
    return box["resp"]


def _post(url: str, *, headers: dict | None, payload: dict, cancel_event, label: str, model: str, local: bool,
          retries: int = 2, timeout=(10, 120)) -> Iterator[dict]:
    """Generator -> (response | None, error | None). Retries recoverable failures; yields provider_notice events."""
    attempt = 0
    while True:
        if cancel_event.is_set():
            return None, {"kind": "cancelled"}
        try:
            resp = _abortable_post(url, headers, payload, timeout, cancel_event)
            if resp is None:
                return None, {"kind": "cancelled"}
            if resp.status_code == 200:
                return resp, None
            err = ac.classify_provider_error(resp.status_code, resp.text, provider=label, model=model,
                                             headers=dict(resp.headers), local=local)
        except requests.RequestException as e:
            err = ac.classify_provider_error(None, provider=label, model=model, exc=e, local=local)
        if err["kind"] in RETRYABLE_KINDS and attempt < retries:
            attempt += 1
            wait = err.get("retry_after")
            wait = min(wait if wait is not None else 2 ** attempt, 20)
            wait = max(wait, 1)
            yield {"type": "provider_notice", "level": "warn", "kind": err["kind"], "provider": label, "wait_s": wait,
                   "attempt": attempt, "of": retries,
                   "text": f"{err['title']} — {label} — retrying in {int(wait)}s (attempt {attempt}/{retries})"}
            if not _sleep_cancellable(wait, cancel_event):
                return None, {"kind": "cancelled"}
            continue
        err["attempts"] = attempt + 1
        return None, err


def _fail(err: dict, messages) -> dict:
    return {"type": "provider_error", "error": err, "messages": messages}


def _parse_args(raw) -> tuple[dict, str | None]:
    if isinstance(raw, dict):
        return raw, None
    try:
        v = json.loads(raw or "{}")
        return (v if isinstance(v, dict) else {}), (None if isinstance(v, dict) else "arguments were not a JSON object")
    except json.JSONDecodeError as e:
        return {}, f"arguments were not valid JSON ({e.msg})"


def _tool_defs_openai(tool_defs):
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in (tool_defs or [])]


def strip_images(messages: list[dict]) -> list[dict]:
    """Remove image parts (used when the conversation moves to a non-vision model)."""
    out = []
    for m in messages:
        m = dict(m)
        if isinstance(m.get("content"), list):
            m["content"] = "\n".join(p.get("text", "") for p in m["content"] if isinstance(p, dict) and p.get("type") == "text")
        m.pop("images", None)
        out.append(m)
    return out


# ---- OpenAI <-> Ollama message shapes (so a conversation can hop providers mid-task) ---------------------------

def openai_to_ollama(messages: list[dict]) -> list[dict]:
    out, names = [], {}
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                fn = c.get("function", {})
                args, _ = _parse_args(fn.get("arguments"))
                names[c.get("id")] = fn.get("name")
                calls.append({"function": {"name": fn.get("name"), "arguments": args}})
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            out.append({"role": "tool", "tool_name": names.get(m.get("tool_call_id")) or "tool", "content": m.get("content") or ""})
        elif isinstance(m.get("content"), list):
            text = "\n".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")
            imgs = []
            for p in m["content"]:
                if p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    if "base64," in url:
                        imgs.append(url.split("base64,", 1)[1])
            mm = {"role": role, "content": text}
            if imgs:
                mm["images"] = imgs
            out.append(mm)
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def ollama_to_openai(messages: list[dict]) -> list[dict]:
    out, pending = [], []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            pending = []
            for c in m["tool_calls"]:
                cid = f"call_{uuid.uuid4().hex[:10]}"
                fn = c.get("function", {})
                pending.append(cid)
                calls.append({"id": cid, "type": "function", "function": {"name": fn.get("name"), "arguments": json.dumps(fn.get("arguments") or {})}})
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": calls})
        elif role == "tool":
            cid = pending.pop(0) if pending else f"call_{uuid.uuid4().hex[:10]}"
            out.append({"role": "tool", "tool_call_id": cid, "content": m.get("content") or ""})
        elif m.get("images"):
            parts = [{"type": "text", "text": m.get("content") or ""}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in m["images"]]
            out.append({"role": role, "content": parts})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


# ---------------------------------------------------------------------------
# OpenAI-compatible (OpenAI, OpenRouter, DeepSeek, Grok, Hugging Face)
# ---------------------------------------------------------------------------

def call_openai(cfg, api_key, system_prompt, history, cancel_event, tool_defs, images, max_iterations, resume_messages, tool_runner):
    if resume_messages:
        messages = list(resume_messages)
        if messages and messages[0].get("role") == "system":
            messages[0] = {"role": "system", "content": system_prompt}
    else:
        messages = [{"role": "system", "content": system_prompt}] + [dict(m) for m in history]
        if images and messages:
            last = messages[-1]
            parts = [{"type": "text", "text": last["content"]}]
            for img in images:
                parts.append({"type": "image_url", "image_url": {"url": f"data:{img['mime']};base64,{img['data_b64']}"}})
            last["content"] = parts
    tools = _tool_defs_openai(tool_defs)
    label, model = cfg.get("label", "provider"), cfg["model"]
    for _ in range(max_iterations or MAX_TOOL_ITERATIONS):
        if cancel_event.is_set():
            yield {"type": "cancelled"}
            return
        payload = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        resp, err = yield from _post(f"{cfg['base_url']}/chat/completions", headers={"Authorization": f"Bearer {api_key}"},
                                     payload=payload, cancel_event=cancel_event, label=label, model=model, local=False,
                                     retries=cfg.get("retries", 2), timeout=(10, 90))
        if err:
            if err["kind"] == "cancelled":
                yield {"type": "cancelled"}
            else:
                yield _fail(err, messages)
            return
        try:
            msg = resp.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError):
            yield _fail(ac.classify_provider_error(502, resp.text[:300], provider=label, model=model), messages)
            return
        calls = msg.get("tool_calls") or []
        if not calls:
            yield {"type": "done", "reply": msg.get("content") or ""}
            return
        messages.append(msg)
        for call in calls:
            if cancel_event.is_set():
                yield {"type": "cancelled"}
                return
            name = call["function"]["name"]
            args, arg_err = _parse_args(call["function"].get("arguments"))
            yield {"type": "tool_start", "name": name, "args": args}
            t0 = time.time()
            if arg_err:
                result = {"ok": False, "error_code": "bad_arguments", "error": f"Tool call rejected: {arg_err}. Re-send it with valid JSON arguments.", "recoverable": True}
            else:
                result = yield from tool_runner(name, args, cancel_event)
            yield {"type": "tool", "name": name, "args": args, "result": result, "duration_ms": round((time.time() - t0) * 1000)}
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": compact_tool_result(result)})
    yield {"type": "done", "reply": "Reached the tool-call limit for this turn. Please continue.", "limit_reached": True}


# ---------------------------------------------------------------------------
# Ollama (native /api/chat — supports num_ctx and real capability errors)
# ---------------------------------------------------------------------------

def call_ollama(cfg, api_key, system_prompt, history, cancel_event, tool_defs, images, max_iterations, resume_messages, tool_runner):
    base = cfg["ollama_base"].rstrip("/")
    if resume_messages:
        messages = list(resume_messages)
        if messages and messages[0].get("role") == "system":
            messages[0] = {"role": "system", "content": system_prompt}
    else:
        messages = [{"role": "system", "content": system_prompt}] + [{"role": m["role"], "content": m["content"]} for m in history]
        if images and messages:
            messages[-1]["images"] = [i["data_b64"] for i in images]
    tools = _tool_defs_openai(tool_defs)
    label, model = cfg.get("label", "Ollama"), cfg["model"]
    for _ in range(max_iterations or MAX_TOOL_ITERATIONS):
        if cancel_event.is_set():
            yield {"type": "cancelled"}
            return
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": {"num_ctx": cfg.get("num_ctx") or 8192}, "keep_alive": "10m"}
        if tools:
            payload["tools"] = tools
        resp, err = yield from _post(f"{base}/api/chat", headers=None, payload=payload, cancel_event=cancel_event,
                                     label=label, model=model, local=True, retries=cfg.get("retries", 1), timeout=(5, 900))
        if err:
            if err["kind"] == "cancelled":
                yield {"type": "cancelled"}
            else:
                yield _fail(err, messages)
            return
        try:
            msg = resp.json()["message"]
        except (KeyError, ValueError):
            yield _fail(ac.classify_provider_error(502, resp.text[:300], provider=label, model=model, local=True), messages)
            return
        calls = msg.get("tool_calls") or []
        if not calls:
            yield {"type": "done", "reply": msg.get("content") or ""}
            return
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            if cancel_event.is_set():
                yield {"type": "cancelled"}
                return
            fn = call.get("function", {})
            name = fn.get("name", "")
            args, arg_err = _parse_args(fn.get("arguments"))
            yield {"type": "tool_start", "name": name, "args": args}
            t0 = time.time()
            if arg_err:
                result = {"ok": False, "error_code": "bad_arguments", "error": f"Tool call rejected: {arg_err}.", "recoverable": True}
            else:
                result = yield from tool_runner(name, args, cancel_event)
            yield {"type": "tool", "name": name, "args": args, "result": result, "duration_ms": round((time.time() - t0) * 1000)}
            messages.append({"role": "tool", "tool_name": name, "content": compact_tool_result(result)})
    yield {"type": "done", "reply": "Reached the tool-call limit for this turn. Please continue.", "limit_reached": True}


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def call_anthropic(cfg, api_key, system_prompt, history, cancel_event, tool_defs, images, max_iterations, resume_messages, tool_runner):
    messages = [dict(m) for m in history]
    if images and messages:
        last = dict(messages[-1])
        blocks = [{"type": "text", "text": last["content"]}]
        for img in images:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": img["mime"], "data": img["data_b64"]}})
        last["content"] = blocks
        messages[-1] = last
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in (tool_defs or [])]
    label, model = cfg.get("label", "Anthropic"), cfg["model"]
    for _ in range(max_iterations or MAX_TOOL_ITERATIONS):
        if cancel_event.is_set():
            yield {"type": "cancelled"}
            return
        payload = {"model": model, "max_tokens": 4096, "system": system_prompt, "messages": messages}
        if tools:
            payload["tools"] = tools
        resp, err = yield from _post(f"{cfg['base_url']}/messages", headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                                     payload=payload, cancel_event=cancel_event, label=label, model=model, local=False,
                                     retries=cfg.get("retries", 2), timeout=(10, 90))
        if err:
            if err["kind"] == "cancelled":
                yield {"type": "cancelled"}
            else:
                yield _fail(err, None)
            return
        content = resp.json().get("content", [])
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        if not tool_uses:
            yield {"type": "done", "reply": text}
            return
        messages.append({"role": "assistant", "content": content})
        result_blocks = []
        for block in tool_uses:
            if cancel_event.is_set():
                yield {"type": "cancelled"}
                return
            args = block.get("input", {})
            yield {"type": "tool_start", "name": block["name"], "args": args}
            t0 = time.time()
            result = yield from tool_runner(block["name"], args, cancel_event)
            yield {"type": "tool", "name": block["name"], "args": args, "result": result, "duration_ms": round((time.time() - t0) * 1000)}
            result_blocks.append({"type": "tool_result", "tool_use_id": block["id"], "content": compact_tool_result(result)})
        messages.append({"role": "user", "content": result_blocks})
    yield {"type": "done", "reply": "Reached the tool-call limit for this turn. Please continue.", "limit_reached": True}


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def call_gemini(cfg, api_key, system_prompt, history, cancel_event, tool_defs, images, max_iterations, resume_messages, tool_runner):
    def clean(schema):
        allowed = {"type", "properties", "required", "items", "description", "enum"}
        c = {k: v for k, v in schema.items() if k in allowed}
        if "properties" in c:
            c["properties"] = {k: clean(v) for k, v in c["properties"].items()}
        if "items" in c and isinstance(c["items"], dict):
            c["items"] = clean(c["items"])
        # Gemini's GenerateContentRequest validation (unlike OpenAI/Anthropic/OpenRouter)
        # REJECTS THE WHOLE REQUEST if any "type": "array" schema anywhere in the tool
        # list is missing "items" — one malformed tool breaks every tool call, silently,
        # for the entire turn. Every tool schema in this app is meant to keep "items"
        # explicit; this is a last-resort default so a future missing one degrades to a
        # loose (but valid) schema instead of taking down Gemini routing entirely.
        if c.get("type") == "array" and "items" not in c:
            c["items"] = {"type": "object"}
        return c

    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in history]
    if images and contents:
        for img in images:
            contents[-1]["parts"].append({"inlineData": {"mimeType": img["mime"], "data": img["data_b64"]}})
    tools = [{"functionDeclarations": [{"name": t["name"], "description": t["description"], "parameters": clean(t["parameters"])} for t in (tool_defs or [])]}] if tool_defs else None
    label, model = cfg.get("label", "Gemini"), cfg["model"]
    for _ in range(max_iterations or MAX_TOOL_ITERATIONS):
        if cancel_event.is_set():
            yield {"type": "cancelled"}
            return
        payload = {"systemInstruction": {"parts": [{"text": system_prompt}]}, "contents": contents}
        if tools:
            payload["tools"] = tools
        resp, err = yield from _post(f"{cfg['base_url']}/models/{model}:generateContent?key={api_key}", headers=None, payload=payload,
                                     cancel_event=cancel_event, label=label, model=model, local=False,
                                     retries=cfg.get("retries", 2), timeout=(10, 90))
        if err:
            if err["kind"] == "cancelled":
                yield {"type": "cancelled"}
            else:
                yield _fail(err, None)
            return
        candidates = resp.json().get("candidates") or []
        if not candidates:
            yield {"type": "done", "reply": "(the model returned no response)"}
            return
        parts = candidates[0].get("content", {}).get("parts", [])
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        text = "\n".join(p.get("text", "") for p in parts if "text" in p)
        if not calls:
            yield {"type": "done", "reply": text}
            return
        contents.append({"role": "model", "parts": parts})
        response_parts = []
        for fc in calls:
            if cancel_event.is_set():
                yield {"type": "cancelled"}
                return
            args = fc.get("args", {})
            yield {"type": "tool_start", "name": fc["name"], "args": args}
            t0 = time.time()
            result = yield from tool_runner(fc["name"], args, cancel_event)
            yield {"type": "tool", "name": fc["name"], "args": args, "result": result, "duration_ms": round((time.time() - t0) * 1000)}
            response_parts.append({"functionResponse": {"name": fc["name"], "response": {"result": json.loads(compact_tool_result(result))}}})
        contents.append({"role": "user", "parts": response_parts})
    yield {"type": "done", "reply": "Reached the tool-call limit for this turn. Please continue.", "limit_reached": True}


_KINDS = {"openai": call_openai, "ollama": call_ollama, "anthropic": call_anthropic, "gemini": call_gemini}


def run_raw(cfg, api_key, system_prompt, history, cancel_event, tool_defs=None, images=None, max_iterations=None,
            resume_messages=None, tool_runner: ToolRunner = None):
    """One provider, no fallback. May yield a `provider_error` event instead of `done`."""
    fn = _KINDS[cfg["kind"]]
    yield from fn(cfg, api_key, system_prompt, history, cancel_event, tool_defs, images, max_iterations, resume_messages, tool_runner)


def convert_resume(messages, from_kind: str, to_kind: str):
    """Carry an in-flight tool conversation to another provider. Returns None when the shapes can't be bridged
    (Anthropic/Gemini) — the caller then restarts from the plain chat history with a note."""
    if not messages:
        return None
    if from_kind == to_kind:
        return messages
    if from_kind == "openai" and to_kind == "ollama":
        return openai_to_ollama(messages)
    if from_kind == "ollama" and to_kind == "openai":
        return ollama_to_openai(messages)
    return None
