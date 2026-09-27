"""provider_router.py — the "universal AI router" core: deterministic task
detection, deterministic explicit-provider intent detection, and the
resolve_provider() priority chain.

Pure functions only: no I/O, no Flask, no credential access. Callers (app.py)
inject what they know — which providers exist, which are configured, which
support a given task — so this module is trivially unit-testable and never
needs to know how credentials are actually stored.

Priority chain (spec: "no silent provider substitution"):
    1. explicit provider in the user's message      ("Use Gemini to ...")
    2. explicit provider passed by the UI/action     (requested_provider=...)
    3. conversation-level override                   ("Use Claude for this chat")
    4. task-specific default
    5. global default
    6. none — caller falls back to its own existing AUTO/CLOUD/LOCAL/OFFLINE policy

An EXPLICIT request (1, 2, or 3) that can't be satisfied (not configured, or
configured but doesn't support the task) returns ok=False immediately and
NEVER falls through to a default or another provider. A DEFAULT that can't be
satisfied is allowed to keep falling through the chain.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Task / capability constants
# ---------------------------------------------------------------------------

TASK_CHAT = "chat"
TASK_CODING = "coding"
TASK_CODE_REVIEW = "code_review"
TASK_IMAGE = "image_generation"
TASK_VISION = "vision"
TASK_RESEARCH = "research"
TASK_DOCUMENT = "document_analysis"
TASK_LOCAL = "local_ai"
ALL_TASKS = (TASK_CHAT, TASK_CODING, TASK_CODE_REVIEW, TASK_IMAGE, TASK_VISION,
             TASK_RESEARCH, TASK_DOCUMENT, TASK_LOCAL)

# ---------------------------------------------------------------------------
# Explicit-provider intent detection
# ---------------------------------------------------------------------------

# provider_id -> aliases. Longest alias wins when several could match (so
# "google gemini" beats "gemini" beats a bare "google").
DEFAULT_ALIASES: dict[str, list[str]] = {
    "openai": ["open ai", "openai", "chat gpt", "chatgpt", "gpt-4o", "gpt4", "gpt"],
    "gemini": ["google gemini", "gemini", "google ai"],
    "anthropic": ["anthropic claude", "claude", "anthropic"],
    "deepseek": ["deep seek", "deepseek"],
    "openrouter": ["open router", "openrouter"],
    "huggingface": ["hugging face", "huggingface", "hf"],
    "grok": ["grok", "xai", "x.ai"],
    "groq": ["groq"],
    "ollama": ["local ollama", "ollama", "local ai"],
}

_TRIGGER = r"(?:use|via|with|through|on|switch(?:ing)? to|running on)"


def _build_pattern(aliases_by_provider: dict[str, list[str]]):
    pairs = [(alias, pid) for pid, names in aliases_by_provider.items() for alias in names]
    pairs.sort(key=lambda p: -len(p[0]))  # longest alias first
    alt = "|".join(re.escape(a) for a, _ in pairs)
    trigger_re = re.compile(rf"\b{_TRIGGER}\s+({alt})\b", re.IGNORECASE)
    lookup = {a.lower(): pid for a, pid in pairs}
    return trigger_re, lookup, alt


class ProviderIntentDetector:
    """Simple deterministic matching on purpose (spec: 'do not make this a
    complicated ML classifier'). Only fires on a clear trigger phrase, so a
    passing mention ('Gemini is a good model') never counts — see the spec's
    DO NOT OVER-DETECT PROVIDER NAMES section."""

    def __init__(self, aliases_by_provider: dict[str, list[str]] | None = None):
        self.aliases = aliases_by_provider or DEFAULT_ALIASES
        self._trigger_re, self._lookup, self._alt = _build_pattern(self.aliases)
        self._session_re = re.compile(
            rf"\b{_TRIGGER}\s+({self._alt})\b[^.!?\n]{{0,40}}?\b(for this (?:chat|conversation)|from now on)\b",
            re.IGNORECASE)
        self._session_re2 = re.compile(
            rf"\bfrom now on\b[^.!?\n]{{0,40}}?\b{_TRIGGER}\s+({self._alt})\b", re.IGNORECASE)

    def detect(self, message: str) -> dict:
        if not message:
            return {"provider": None, "explicit": False, "confidence": 0.0, "source": "default"}
        m = self._trigger_re.search(message)
        if not m:
            return {"provider": None, "explicit": False, "confidence": 0.0, "source": "default"}
        pid = self._lookup.get(m.group(1).lower())
        if not pid:
            return {"provider": None, "explicit": False, "confidence": 0.0, "source": "default"}
        return {"provider": pid, "explicit": True, "confidence": 1.0, "source": "user_message", "matched": m.group(0)}

    def detect_session_override(self, message: str) -> str | None:
        """'Use Claude for this chat' / 'from now on use Gemini'."""
        if not message:
            return None
        m = self._session_re.search(message) or self._session_re2.search(message)
        if not m:
            return None
        return self._lookup.get(m.group(1).lower())


# ---------------------------------------------------------------------------
# Task detection — deterministic keyword matching, most-specific task first.
# ---------------------------------------------------------------------------

_TASK_PATTERNS = [
    (TASK_IMAGE, re.compile(
        r"\b(image|photo|picture|illustration|logo|thumbnail|wallpaper|artwork|icon|drawing)s?\b"
        r"[^.!?\n]{0,25}\b(create|generate|make|draw|design|produce|render)\b"
        r"|\b(create|generate|make|draw|design|render)\b[^.!?\n]{0,25}"
        r"\b(image|photo|picture|illustration|logo|thumbnail|wallpaper|artwork|icon|drawing)s?\b",
        re.IGNORECASE)),
    (TASK_CODE_REVIEW, re.compile(
        r"\breview\b[^.!?\n]{0,25}\b(code|function|file|script|pr|pull request|patch|commit)\b"
        r"|\bcode review\b", re.IGNORECASE)),
    (TASK_VISION, re.compile(
        r"\b(analyz|analys)e?\s+this\s+(screenshot|image|photo|picture)"
        r"|\bwhat(?:'?s| is)\s+in\s+this\s+(image|screenshot|photo)\b", re.IGNORECASE)),
    (TASK_DOCUMENT, re.compile(
        r"\b(analyz|analys)e?\s+this\s+(pdf|document|doc|spreadsheet|report)\b"
        r"|\bsummarize\s+this\s+(pdf|document)\b", re.IGNORECASE)),
    (TASK_RESEARCH, re.compile(r"\bresearch\b|\block\s+into\b|\bfind out\b", re.IGNORECASE)),
    (TASK_CODING, re.compile(
        r"\b(write|build|implement|create|refactor|fix|debug)\b[^.!?\n]{0,30}"
        r"\b(code|function|backend|api|script|class|module|component|endpoint|bug|app|feature)\b"
        r"|\b(python|flask|javascript|typescript|react|django|node\.?js)\b", re.IGNORECASE)),
]


def detect_task(message: str, *, has_images: bool = False, default: str = TASK_CHAT) -> str:
    text = message or ""
    for task, pattern in _TASK_PATTERNS:
        if pattern.search(text):
            return task
    if has_images:
        return TASK_VISION
    return default


# ---------------------------------------------------------------------------
# The router itself
# ---------------------------------------------------------------------------

def resolve_provider(*, message: str, task: str | None, requested_provider: str | None,
                      detector: "ProviderIntentDetector", conversation_override: str | None,
                      task_default: str | None, global_default: str | None,
                      provider_exists, provider_configured, provider_supports, provider_label) -> dict:
    """Returns a route object:
        {"ok": True,  "provider": pid, "source": ..., "task": task, "capability": task}
        {"ok": False, "error": "...", "provider": pid|None, "source": ..., "task": task, "message": "..."}

    provider_exists(pid) -> bool
    provider_configured(pid) -> bool
    provider_supports(pid, task) -> bool
    provider_label(pid) -> str
    """
    task = task or detect_task(message)
    intent = detector.detect(message) if message else {"provider": None, "explicit": False}

    def make(pid, source):
        if not provider_exists(pid):
            return {"ok": False, "error": "unknown_provider", "provider": pid, "source": source, "task": task,
                    "message": f"{pid!r} isn't a recognized provider."}
        if not provider_configured(pid):
            return {"ok": False, "error": "provider_not_configured", "provider": pid, "source": source, "task": task,
                    "message": f"{provider_label(pid)} isn't connected yet. Add its API key in Settings, "
                               f"or explicitly ask me to use another provider."}
        if not provider_supports(pid, task):
            return {"ok": False, "error": "capability_unsupported", "provider": pid, "source": source, "task": task,
                    "message": f"{provider_label(pid)} is connected, but it doesn't support "
                               f"{task.replace('_', ' ')}."}
        return {"ok": True, "provider": pid, "source": source, "task": task, "capability": task}

    # 1 & 2: explicit — never falls through, whatever the result.
    if requested_provider:
        return make(requested_provider, "explicit_ui")
    if intent.get("explicit") and intent.get("provider"):
        return make(intent["provider"], "explicit_user")
    # 3: conversation override — also strict once set.
    if conversation_override:
        return make(conversation_override, "conversation_override")
    # 4 & 5: defaults — allowed to fall through to the next one.
    task_result = make(task_default, "task_default") if task_default else None
    if task_result and task_result["ok"]:
        return task_result
    if global_default:
        global_result = make(global_default, "global_default")
        if global_result["ok"]:
            return global_result
    if task_result:
        return task_result
    return {"ok": False, "error": "no_provider", "provider": None, "source": "none", "task": task,
            "message": "No provider is configured for this task."}