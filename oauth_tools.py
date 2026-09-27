"""oauth_tools.py — real tool executors for the OAuth-connected services
(Google Drive/Gmail/Calendar, Dropbox, OneDrive). Every function here makes an
actual HTTP call to the real API using a live access token from
oauth_manager.get_valid_access_token() (which refreshes automatically) — none
of this returns synthesized data.

Same shape as extensions.py's adapters: fn(args, ctx, token) -> dict, so they
plug into the same get_active_tool_defs()/get_active_executors() machinery.
Write actions (create_event, create_draft, send_email) go through
computer_tools._confirm_and_run — the same confirm-card flow every other
mutating tool in this app already uses.
"""
from __future__ import annotations

import base64
import time

import requests

import computer_tools as ct
import oauth_manager as om

_S = ct._S
_obj = ct._obj


def _err_from_token(err: str) -> dict:
    return {"ok": False, "error": err}


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

def drive_search_files(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("google")
    if err:
        return _err_from_token(err)
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "A search query is required."}
    q = f"name contains '{query}' and trashed = false"
    try:
        r = requests.get("https://www.googleapis.com/drive/v3/files", headers={"Authorization": f"Bearer {token}"},
                          params={"q": q, "pageSize": min(max(int(args.get("count") or 10), 1), 25),
                                  "fields": "files(id,name,mimeType,webViewLink,modifiedTime,size)"}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Google Drive request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"Google Drive returned HTTP {r.status_code}: {r.text[:200]}"}
    files = r.json().get("files", [])
    return {"ok": True, "results": [{"id": f["id"], "name": f["name"], "type": f.get("mimeType"),
                                      "url": f.get("webViewLink"), "modified": f.get("modifiedTime")} for f in files]}


def drive_list_files(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("google")
    if err:
        return _err_from_token(err)
    folder_id = (args.get("folder_id") or "").strip()
    q = f"'{folder_id}' in parents and trashed = false" if folder_id else "trashed = false"
    try:
        r = requests.get("https://www.googleapis.com/drive/v3/files", headers={"Authorization": f"Bearer {token}"},
                          params={"q": q, "pageSize": min(max(int(args.get("count") or 20), 1), 50),
                                  "orderBy": "modifiedTime desc",
                                  "fields": "files(id,name,mimeType,webViewLink,modifiedTime,size)"}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Google Drive request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"Google Drive returned HTTP {r.status_code}: {r.text[:200]}"}
    files = r.json().get("files", [])
    return {"ok": True, "results": [{"id": f["id"], "name": f["name"], "type": f.get("mimeType"),
                                      "url": f.get("webViewLink"), "modified": f.get("modifiedTime")} for f in files]}


def drive_read_file(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("google")
    if err:
        return _err_from_token(err)
    file_id = (args.get("file_id") or "").strip()
    if not file_id:
        return {"ok": False, "error": "`file_id` is required (from drive_search_files/drive_list_files)."}
    try:
        meta = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}", headers={"Authorization": f"Bearer {token}"},
                             params={"fields": "name,mimeType"}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Google Drive request failed: {e}"}
    if not meta.ok:
        return {"ok": False, "error": f"Google Drive returned HTTP {meta.status_code}: {meta.text[:200]}"}
    mime = meta.json().get("mimeType", "")
    try:
        if mime.startswith("application/vnd.google-apps"):
            export_mime = "text/plain" if mime != "application/vnd.google-apps.spreadsheet" else "text/csv"
            r = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                              headers={"Authorization": f"Bearer {token}"}, params={"mimeType": export_mime}, timeout=20)
        else:
            r = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}", headers={"Authorization": f"Bearer {token}"},
                              params={"alt": "media"}, timeout=20)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Google Drive request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"Google Drive returned HTTP {r.status_code}: {r.text[:200]}"}
    try:
        content = r.content.decode("utf-8")
        return {"ok": True, "name": meta.json().get("name"), "content": content[:20000], "truncated": len(content) > 20000}
    except UnicodeDecodeError:
        return {"ok": True, "name": meta.json().get("name"), "binary": True, "note": "Binary file — not shown as text. Use computer_download_file with the webViewLink instead."}


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def gmail_search(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("google")
    if err:
        return _err_from_token(err)
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "A search query is required (Gmail search syntax, e.g. 'from:microsoft.com')."}
    try:
        r = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/messages", headers={"Authorization": f"Bearer {token}"},
                          params={"q": query, "maxResults": min(max(int(args.get("count") or 10), 1), 25)}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Gmail request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"Gmail returned HTTP {r.status_code}: {r.text[:200]}"}
    ids = [m["id"] for m in r.json().get("messages", [])]
    results = []
    for mid in ids:
        try:
            mr = requests.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}", headers={"Authorization": f"Bearer {token}"},
                               params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}, timeout=15)
        except requests.RequestException:
            continue
        if not mr.ok:
            continue
        headers = {h["name"]: h["value"] for h in mr.json().get("payload", {}).get("headers", [])}
        results.append({"id": mid, "from": headers.get("From"), "subject": headers.get("Subject"),
                         "date": headers.get("Date"), "snippet": mr.json().get("snippet")})
    return {"ok": True, "results": results}


def gmail_read(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("google")
    if err:
        return _err_from_token(err)
    mid = (args.get("message_id") or "").strip()
    if not mid:
        return {"ok": False, "error": "`message_id` is required (from gmail_search)."}
    try:
        r = requests.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}", headers={"Authorization": f"Bearer {token}"},
                          params={"format": "full"}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Gmail request failed: {e}"}
    if r.status_code == 404:
        return {"ok": False, "error": "Message not found."}
    if not r.ok:
        return {"ok": False, "error": f"Gmail returned HTTP {r.status_code}: {r.text[:200]}"}
    msg = r.json()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body_text = _gmail_extract_text(msg.get("payload", {}))
    return {"ok": True, "from": headers.get("From"), "to": headers.get("To"), "subject": headers.get("Subject"),
            "date": headers.get("Date"), "body": body_text[:20000], "truncated": len(body_text) > 20000}


def _gmail_extract_text(payload: dict) -> str:
    if payload.get("mimeType", "").startswith("text/") and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        t = _gmail_extract_text(part)
        if t:
            return t
    return ""


def gmail_create_draft(args: dict, ctx, _unused_token: str) -> dict:
    to, subject, body = (args.get("to") or "").strip(), (args.get("subject") or "").strip(), (args.get("body") or "")
    if not to or not subject:
        return {"ok": False, "error": "`to` and `subject` are both required."}
    action = {"action": "extension_write", "permission": "extension_write", "icon": "📧", "title": "CREATE GMAIL DRAFT",
              "danger": False, "path": to,
              "fields": [{"label": "TO", "value": to}, {"label": "SUBJECT", "value": subject}, {"label": "BODY", "value": body[:300]}]}

    def run(decision, value):
        token, err = om.get_valid_access_token("google")
        if err:
            return _err_from_token(err)
        raw_msg = f"To: {to}\r\nSubject: {subject}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n{body}"
        raw_b64 = base64.urlsafe_b64encode(raw_msg.encode("utf-8")).decode()
        try:
            r = requests.post("https://gmail.googleapis.com/gmail/v1/users/me/drafts", headers={"Authorization": f"Bearer {token}"},
                               json={"message": {"raw": raw_b64}}, timeout=15)
        except requests.RequestException as e:
            return {"ok": False, "error": f"Gmail request failed: {e}"}
        if not r.ok:
            return {"ok": False, "error": f"Gmail returned HTTP {r.status_code}: {r.text[:200]}"}
        d = r.json()
        return {"ok": True, "verified": True, "draft_id": d.get("id"), "message": f"✓ Draft created (id {d.get('id')})"}
    return ct._confirm_and_run(ctx, action, [ct._opt("cancel", "Cancel"), ct._opt("confirm", "Create Draft", "primary")],
                               f"creating a Gmail draft to {to}", run)


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------

def calendar_list_calendars(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("google")
    if err:
        return _err_from_token(err)
    try:
        r = requests.get("https://www.googleapis.com/calendar/v3/users/me/calendarList", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Google Calendar request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"Google Calendar returned HTTP {r.status_code}: {r.text[:200]}"}
    items = r.json().get("items", [])
    return {"ok": True, "calendars": [{"id": c["id"], "summary": c.get("summary"), "primary": c.get("primary", False)} for c in items]}


def calendar_list_events(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("google")
    if err:
        return _err_from_token(err)
    cal_id = (args.get("calendar_id") or "primary").strip()
    params = {"maxResults": min(max(int(args.get("count") or 10), 1), 25), "singleEvents": "true", "orderBy": "startTime",
              "timeMin": args.get("time_min") or time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())}
    if args.get("query"):
        params["q"] = args["query"]
    if args.get("time_max"):
        params["timeMax"] = args["time_max"]
    try:
        r = requests.get(f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events",
                          headers={"Authorization": f"Bearer {token}"}, params=params, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Google Calendar request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"Google Calendar returned HTTP {r.status_code}: {r.text[:200]}"}
    items = r.json().get("items", [])
    return {"ok": True, "events": [{"id": e["id"], "summary": e.get("summary"),
                                     "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
                                     "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
                                     "location": e.get("location")} for e in items]}


def calendar_create_event(args: dict, ctx, _unused_token: str) -> dict:
    summary, start, end = (args.get("summary") or "").strip(), args.get("start"), args.get("end")
    if not summary or not start or not end:
        return {"ok": False, "error": "`summary`, `start` and `end` (ISO 8601 datetimes) are all required."}
    cal_id = (args.get("calendar_id") or "primary").strip()
    action = {"action": "extension_write", "permission": "extension_write", "icon": "📅", "title": "CREATE CALENDAR EVENT",
              "danger": False, "path": cal_id,
              "fields": [{"label": "TITLE", "value": summary}, {"label": "START", "value": str(start)}, {"label": "END", "value": str(end)}]}

    def run(decision, value):
        token, err = om.get_valid_access_token("google")
        if err:
            return _err_from_token(err)
        body = {"summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}}
        if args.get("description"):
            body["description"] = args["description"]
        if args.get("location"):
            body["location"] = args["location"]
        try:
            r = requests.post(f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events",
                               headers={"Authorization": f"Bearer {token}"}, json=body, timeout=15)
        except requests.RequestException as e:
            return {"ok": False, "error": f"Google Calendar request failed: {e}"}
        if not r.ok:
            return {"ok": False, "error": f"Google Calendar returned HTTP {r.status_code}: {r.text[:200]}"}
        ev = r.json()
        return {"ok": True, "verified": True, "event_id": ev.get("id"), "url": ev.get("htmlLink"),
                "message": f"✓ Event created — {ev.get('htmlLink')}"}
    return ct._confirm_and_run(ctx, action, [ct._opt("cancel", "Cancel"), ct._opt("confirm", "Create Event", "primary")],
                               f"creating the calendar event '{summary}'", run)


# ---------------------------------------------------------------------------
# Dropbox
# ---------------------------------------------------------------------------

def dropbox_search_files(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("dropbox")
    if err:
        return _err_from_token(err)
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "A search query is required."}
    try:
        r = requests.post("https://api.dropboxapi.com/2/files/search_v2", headers={"Authorization": f"Bearer {token}"},
                           json={"query": query, "options": {"max_results": min(max(int(args.get("count") or 10), 1), 25)}}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Dropbox request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"Dropbox returned HTTP {r.status_code}: {r.text[:200]}"}
    matches = r.json().get("matches", [])
    results = []
    for m in matches:
        meta = m.get("metadata", {}).get("metadata", {})
        results.append({"name": meta.get("name"), "path": meta.get("path_display"), "type": meta.get(".tag")})
    return {"ok": True, "results": results}


def dropbox_list_folder(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("dropbox")
    if err:
        return _err_from_token(err)
    path = args.get("path") or ""
    try:
        r = requests.post("https://api.dropboxapi.com/2/files/list_folder", headers={"Authorization": f"Bearer {token}"},
                           json={"path": path, "limit": min(max(int(args.get("count") or 20), 1), 50)}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Dropbox request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"Dropbox returned HTTP {r.status_code}: {r.text[:200]}"}
    entries = r.json().get("entries", [])
    return {"ok": True, "entries": [{"name": e.get("name"), "path": e.get("path_display"), "type": e.get(".tag")} for e in entries]}


def dropbox_download_file(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("dropbox")
    if err:
        return _err_from_token(err)
    path = (args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "`path` is required (from dropbox_search_files/dropbox_list_folder)."}
    try:
        r = requests.post("https://content.dropboxapi.com/2/files/download",
                           headers={"Authorization": f"Bearer {token}", "Dropbox-API-Arg": ct.json.dumps({"path": path})}, timeout=20)
    except requests.RequestException as e:
        return {"ok": False, "error": f"Dropbox request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"Dropbox returned HTTP {r.status_code}: {r.text[:200]}"}
    try:
        content = r.content.decode("utf-8")
        return {"ok": True, "path": path, "content": content[:20000], "truncated": len(content) > 20000}
    except UnicodeDecodeError:
        return {"ok": True, "path": path, "binary": True, "note": "Binary file — not shown as text."}


# ---------------------------------------------------------------------------
# OneDrive (Microsoft Graph)
# ---------------------------------------------------------------------------

def onedrive_search_files(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("microsoft")
    if err:
        return _err_from_token(err)
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "A search query is required."}
    try:
        r = requests.get(f"https://graph.microsoft.com/v1.0/me/drive/root/search(q='{query}')",
                          headers={"Authorization": f"Bearer {token}"}, params={"$top": min(max(int(args.get("count") or 10), 1), 25)}, timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "error": f"OneDrive request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"OneDrive returned HTTP {r.status_code}: {r.text[:200]}"}
    items = r.json().get("value", [])
    return {"ok": True, "results": [{"id": it["id"], "name": it["name"], "url": it.get("webUrl"),
                                      "size": it.get("size"), "modified": it.get("lastModifiedDateTime")} for it in items]}


def onedrive_download_file(args: dict, ctx, _unused_token: str) -> dict:
    token, err = om.get_valid_access_token("microsoft")
    if err:
        return _err_from_token(err)
    item_id = (args.get("item_id") or "").strip()
    if not item_id:
        return {"ok": False, "error": "`item_id` is required (from onedrive_search_files)."}
    try:
        r = requests.get(f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}/content",
                          headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except requests.RequestException as e:
        return {"ok": False, "error": f"OneDrive request failed: {e}"}
    if not r.ok:
        return {"ok": False, "error": f"OneDrive returned HTTP {r.status_code}: {r.text[:200]}"}
    try:
        content = r.content.decode("utf-8")
        return {"ok": True, "content": content[:20000], "truncated": len(content) > 20000}
    except UnicodeDecodeError:
        return {"ok": True, "binary": True, "note": "Binary file — not shown as text."}


# ---------------------------------------------------------------------------
# Tool definitions, grouped by the underlying oauth_manager provider each needs.
# ---------------------------------------------------------------------------

GOOGLE_DRIVE_TOOLS = [
    {"name": "drive_search_files", "description": "Search the connected Google Drive by filename.",
     "parameters": _obj({"query": _S, "count": {"type": "integer"}}, ["query"])},
    {"name": "drive_list_files", "description": "List files in Google Drive (optionally inside one folder id), most recently modified first.",
     "parameters": _obj({"folder_id": _S, "count": {"type": "integer"}}, [])},
    {"name": "drive_read_file", "description": "Read a Drive file's text content (Google Docs/Sheets export as text/CSV; plain text files read directly).",
     "parameters": _obj({"file_id": _S}, ["file_id"])},
]
GOOGLE_DRIVE_EXECUTORS = {"drive_search_files": drive_search_files, "drive_list_files": drive_list_files, "drive_read_file": drive_read_file}
GOOGLE_DRIVE_WRITE_TOOLS: set[str] = set()

GMAIL_TOOLS = [
    {"name": "gmail_search", "description": "Search Gmail using Gmail's own search syntax (e.g. 'from:microsoft.com', 'subject:invoice').",
     "parameters": _obj({"query": _S, "count": {"type": "integer"}}, ["query"])},
    {"name": "gmail_read", "description": "Read the full body of one Gmail message.",
     "parameters": _obj({"message_id": _S}, ["message_id"])},
    {"name": "gmail_create_draft", "description": "Create a new Gmail draft. Shows a confirmation card first.",
     "parameters": _obj({"to": _S, "subject": _S, "body": _S}, ["to", "subject", "body"])},
]
GMAIL_EXECUTORS = {"gmail_search": gmail_search, "gmail_read": gmail_read, "gmail_create_draft": gmail_create_draft}
GMAIL_WRITE_TOOLS = {"gmail_create_draft"}

GOOGLE_CALENDAR_TOOLS = [
    {"name": "calendar_list_calendars", "description": "List the connected Google account's calendars.", "parameters": _obj({}, [])},
    {"name": "calendar_list_events", "description": "List/search upcoming events on a Google Calendar (default: the primary calendar, from today).",
     "parameters": _obj({"calendar_id": _S, "query": _S, "time_min": _S, "time_max": _S, "count": {"type": "integer"}}, [])},
    {"name": "calendar_create_event", "description": "Create a Google Calendar event. `start`/`end` must be ISO 8601 datetimes (e.g. '2026-10-01T14:00:00-07:00'). Shows a confirmation card first.",
     "parameters": _obj({"calendar_id": _S, "summary": _S, "start": _S, "end": _S, "description": _S, "location": _S}, ["summary", "start", "end"])},
]
GOOGLE_CALENDAR_EXECUTORS = {"calendar_list_calendars": calendar_list_calendars, "calendar_list_events": calendar_list_events, "calendar_create_event": calendar_create_event}
GOOGLE_CALENDAR_WRITE_TOOLS = {"calendar_create_event"}

DROPBOX_TOOLS = [
    {"name": "dropbox_search_files", "description": "Search the connected Dropbox account by filename/content.",
     "parameters": _obj({"query": _S, "count": {"type": "integer"}}, ["query"])},
    {"name": "dropbox_list_folder", "description": "List a Dropbox folder's contents ('' for the root).",
     "parameters": _obj({"path": _S, "count": {"type": "integer"}}, [])},
    {"name": "dropbox_download_file", "description": "Download and read a Dropbox file's text content.",
     "parameters": _obj({"path": _S}, ["path"])},
]
DROPBOX_EXECUTORS = {"dropbox_search_files": dropbox_search_files, "dropbox_list_folder": dropbox_list_folder, "dropbox_download_file": dropbox_download_file}
DROPBOX_WRITE_TOOLS: set[str] = set()

ONEDRIVE_TOOLS = [
    {"name": "onedrive_search_files", "description": "Search the connected OneDrive by filename.",
     "parameters": _obj({"query": _S, "count": {"type": "integer"}}, ["query"])},
    {"name": "onedrive_download_file", "description": "Download and read a OneDrive file's text content.",
     "parameters": _obj({"item_id": _S}, ["item_id"])},
]
ONEDRIVE_EXECUTORS = {"onedrive_search_files": onedrive_search_files, "onedrive_download_file": onedrive_download_file}
ONEDRIVE_WRITE_TOOLS: set[str] = set()
