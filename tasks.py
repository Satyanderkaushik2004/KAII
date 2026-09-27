"""tasks — persistent record of what the agent did, so long jobs can be inspected, resumed and retried.

Every agent turn is a Task. The recorder watches the SAME stream events the UI sees (tool_start / tool / done ...), so
nothing is self-reported by the model: steps, files touched, downloads, errors and checkpoints come from real tool
results. Checkpoints are derived from verified results only (a download counts once files really landed on disk).

Secrets are never stored: tool arguments are reduced to a short summary and known secret keys are dropped.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.RLock()
_SECRET_KEYS = re.compile(r"key|token|secret|password|passwd|authorization|credential|cookie", re.I)
MAX_TASKS = 200
MAX_STEPS = 400

CHECKPOINTS = [
    ("inspected", "Project / target inspected"),
    ("assets_ready", "Assets downloaded & verified"),
    ("files_updated", "Files created or updated"),
    ("server_running", "Local server running & healthy"),
    ("sources_registered", "Sources researched & registered"),
    ("bibliography_ready", "Bibliography generated & validated"),
    ("compiled", "Document compiled"),
    ("verified", "Verification passed"),
]


def _safe_args(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if _SECRET_KEYS.search(str(k)):
            out[k] = "«redacted»"
        elif isinstance(v, str):
            out[k] = v if len(v) <= 160 else v[:140] + f"…(+{len(v) - 140} chars)"
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list):
            out[k] = f"[{len(v)} items]"
        else:
            out[k] = "…"
    return out


class TaskStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.tasks: list[dict] = []
        self._load()

    # ---- persistence -------------------------------------------------------------------------------------
    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.tasks = data[-MAX_TASKS:]
                    for t in self.tasks:                       # a task still 'running' after a restart was interrupted
                        if t.get("status") == "running":
                            t["status"] = "interrupted"
            except (OSError, ValueError):
                self.tasks = []

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.tasks[-MAX_TASKS:], indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # ---- lifecycle -----------------------------------------------------------------------------------------
    def start(self, command: str, *, session_id: str = "main", provider: str = "", model: str = "", mode: str = "", autonomy: str = "safe") -> dict:
        t = {"id": "t_" + uuid.uuid4().hex[:8], "command": command[:500], "session_id": session_id, "started": time.time(),
             "started_text": time.strftime("%Y-%m-%d %H:%M:%S"), "ended": None, "status": "running", "provider": provider, "model": model,
             "mode": mode, "autonomy": autonomy, "steps": [], "files_touched": [], "downloads": [], "errors": [], "checkpoints": [],
             "providers_used": [provider] if provider else [], "state": {"goal": command[:300], "decisions": [], "remaining": [], "notes": ""},
             "final_reply": "", "verification": None, "paused": False}
        with _LOCK:
            self.tasks.append(t)
            del self.tasks[:-MAX_TASKS]
            self.save()
        return t

    def get(self, task_id: str) -> dict | None:
        with _LOCK:
            return next((t for t in self.tasks if t["id"] == task_id), None)

    def latest(self, session_id: str | None = None, statuses=("running", "partial", "interrupted", "paused", "failed", "blocked", "completed_unverified")) -> dict | None:
        with _LOCK:
            for t in reversed(self.tasks):
                if (session_id is None or t["session_id"] == session_id) and t["status"] in statuses:
                    return t
        return None

    def summary(self, t: dict) -> dict:
        return {"id": t["id"], "command": t["command"], "status": t["status"], "started": t["started_text"], "provider": t["provider"],
                "model": t["model"], "steps": len(t["steps"]), "errors": len(t["errors"]), "files": len(t["files_touched"]),
                "downloads": len(t["downloads"]), "checkpoints": [c["id"] for c in t["checkpoints"]],
                "duration_s": round((t["ended"] or time.time()) - t["started"])}

    def list(self, limit: int = 50) -> list[dict]:
        with _LOCK:
            return [self.summary(t) for t in reversed(self.tasks[-limit:])]

    # ---- event recording -------------------------------------------------------------------------------------
    def record_tool(self, t: dict, name: str, args: dict, result: dict, duration_ms: int) -> list[dict]:
        """Record a finished tool call. Returns any NEW checkpoint events (for the stream)."""
        result = result if isinstance(result, dict) else {"ok": False, "error": "non-dict result"}
        ok = bool(result.get("ok")) or bool(result.get("requires_confirmation"))
        step = {"i": len(t["steps"]) + 1, "tool": name, "args": _safe_args(args), "ok": bool(result.get("ok")), "ms": duration_ms,
                "verified": result.get("verified"), "error": (result.get("error") or "")[:240] or None,
                "error_code": result.get("error_code"), "message": (result.get("message") or "")[:160] or None}
        with _LOCK:
            if len(t["steps"]) < MAX_STEPS:
                t["steps"].append(step)
            if not result.get("ok") and not result.get("cancelled") and step["error"]:
                t["errors"].append({"step": step["i"], "tool": name, "error": step["error"], "code": result.get("error_code")})
                del t["errors"][:-60]
            for pth in self._paths(name, args, result):
                if pth not in t["files_touched"]:
                    t["files_touched"].append(pth)
            del t["files_touched"][:-300]
            for d in result.get("downloaded", []) or []:
                t["downloads"].append({"path": d.get("path"), "url": d.get("url"), "bytes": d.get("bytes")})
            for m in result.get("mapping", []) or []:
                if m.get("local_path") and not any(x["path"] == m["local_path"] for x in t["downloads"]):
                    t["downloads"].append({"path": m["local_path"], "url": m.get("url"), "bytes": m.get("bytes")})
            del t["downloads"][:-300]
            if name == "provider_fallback" and result.get("now_using"):
                t["providers_used"].append(result["now_using"])
            new = self._checkpoints(t, name, args, result)
            self.save()
        return new

    @staticmethod
    def _paths(name, args, result) -> list[str]:
        out = []
        if name in ("create_file", "edit_file", "delete_file", "rename_file", "copy_file", "write_project_manifest"):
            for k in ("path", "new_path", "destination_path"):
                if args.get(k):
                    out.append(str(args[k]))
        for k in ("path", "destination"):
            if result.get("ok") and isinstance(result.get(k), str) and name.startswith(("computer_", "create_document")):
                out.append(result[k])
        for r in result.get("results", []) or []:
            if isinstance(r, dict) and r.get("ok") and not r.get("skipped"):
                for k in ("destination", "path"):
                    if isinstance(r.get(k), str):
                        out.append(r[k]); break
        return out

    def _checkpoints(self, t, name, args, result) -> list[dict]:
        hits = []
        ok = bool(result.get("ok"))
        if ok and name in ("get_project_map", "get_project_structure", "list_files", "computer_browse_directory", "computer_search_files", "site_asset_manifest", "read_project_manifest"):
            hits.append("inspected")
        if ok and (name in ("computer_download_files", "computer_download_file", "download_search_results", "computer_download_media") and result.get("downloaded") or result.get("mapping")):
            hits.append("assets_ready")
        if ok and name in ("create_file", "edit_file", "computer_create_file", "computer_create_files", "create_document", "computer_copy_files",
                          "computer_move_files", "create_research_project", "create_excel_report", "create_presentation"):
            hits.append("files_updated")
        if ok and name in ("start_dev_server", "restart_dev_server", "run_file") and (result.get("health") or {}).get("ok") is not False and (result.get("url") or result.get("preview_url")):
            hits.append("server_running")
        if ok and name == "register_source":
            hits.append("sources_registered")
        if ok and name in ("generate_bibliography", "validate_paper_bibliography") and (name == "generate_bibliography" or result.get("ok")):
            hits.append("bibliography_ready")
        if ok and name == "compile_research_project":
            hits.append("compiled")
        if ok and ((name in ("check_website", "check_running_site") and (result.get("clean") is True)) or name == "computer_verify_paths"
                   or (name in ("compile_research_project", "verify_publishing_artifact") and result.get("verified") is True)
                   or (name in ("create_excel_report", "create_presentation") and result.get("verified") is True)):
            hits.append("verified")
        events = []
        have = {c["id"] for c in t["checkpoints"]}
        for h in hits:
            if h not in have:
                label = dict(CHECKPOINTS)[h]
                cp = {"id": h, "label": label, "step": len(t["steps"]), "at": time.strftime("%H:%M:%S")}
                t["checkpoints"].append(cp)
                events.append({"type": "checkpoint", **cp, "task_id": t["id"]})
                have.add(h)
        return events

    def update_state(self, t: dict, **fields) -> dict:
        with _LOCK:
            st = t["state"]
            for k in ("goal", "current_step", "notes", "project"):
                if fields.get(k):
                    st[k] = str(fields[k])[:500]
            for k in ("decisions", "remaining"):
                if isinstance(fields.get(k), list):
                    st[k] = [str(x)[:200] for x in fields[k]][:30]
            self.save()
            return dict(st)

    def finish(self, t: dict, status: str, reply: str = "", verification=None) -> None:
        with _LOCK:
            t["ended"] = time.time()
            t["status"] = status
            t["final_reply"] = (reply or "")[:2000]
            if verification is not None:
                t["verification"] = verification
            self.save()

    # ---- prompts -----------------------------------------------------------------------------------------------
    def state_block(self, t: dict) -> str:
        """Compact TASK STATE for the model (prevents rediscovering the same facts every turn)."""
        st = t["state"]
        recent = t["steps"][-8:]
        lines = ["TASK STATE (from real recorded tool results — trust it, don't re-discover it)",
                 f"  Goal: {st.get('goal', t['command'])}", f"  Status: {t['status']}",
                 f"  Checkpoints reached: {', '.join(c['id'] for c in t['checkpoints']) or 'none'}"]
        if st.get("project"):
            lines.append(f"  Project: {st['project']}")
        if t["files_touched"]:
            lines.append("  Files changed: " + "; ".join(t["files_touched"][-10:]))
        if t["downloads"]:
            lines.append("  Downloaded: " + "; ".join(str(d['path']) for d in t["downloads"][-10:]))
        if t["errors"]:
            lines.append("  Open errors: " + "; ".join(f"{e['tool']}: {e['error'][:100]}" for e in t["errors"][-4:]))
        if st.get("decisions"):
            lines.append("  Decisions: " + "; ".join(st["decisions"][-6:]))
        if st.get("remaining"):
            lines.append("  Remaining: " + "; ".join(st["remaining"][:8]))
        if st.get("current_step"):
            lines.append(f"  Current step: {st['current_step']}")
        lines.append("  Last actions: " + " | ".join(f"{s['tool']}{'✓' if s['ok'] else '✗'}" for s in recent))
        return "\n".join(lines)

    def resume_prompt(self, t: dict, mode: str = "continue") -> str:
        last_cp = t["checkpoints"][-1]["label"] if t["checkpoints"] else "none yet"
        failed = next((s for s in reversed(t["steps"]) if not s["ok"] and s.get("error")), None)
        head = f"Continue the previous task: \"{t['command']}\"."
        if mode == "retry" and failed:
            head += f" Retry the failed step: {failed['tool']} — it failed with: {failed['error']}. Diagnose why, fix the cause, then continue."
        else:
            head += f" Resume from the last checkpoint ({last_cp}). Do not redo work that is already verified."
        return head + "\n\n" + self.state_block(t)
