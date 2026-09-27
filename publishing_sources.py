"""publishing_sources — SourceManager + CitationManager for the publishing subsystem.

The rule this module exists to enforce: **a reference is only as real as its evidence.**

  * Every source must carry a locator (a DOI, a URL, or a local file) — you cannot cite something you cannot point at.
  * Metadata that was not provided or looked up is left EMPTY. Nothing is guessed, completed from memory, or invented.
  * A DOI is only marked `verified` when a real DOI registry (Crossref) answered for it. URLs are only marked
    `reachable` after a real HTTP request. Everything else stays `not_checked` / `unverified`, and the verifier reports it.
  * The registry remembers which URLs really appeared in tool results this task (`note_tool_result`), so a source that claims to
    come from a search but whose URL never appeared in any result is flagged `not_seen_in_results`.

BibTeX is generated only from registry fields that exist; nothing is padded.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import requests

CROSSREF_BASE = "https://api.crossref.org/works/"        # module-level so tests can point it at a local server
HTTP_TIMEOUT = 10
USER_AGENT = "AICodingWorkspace-PublishingAgent/1.0 (reference verification)"

_LOCK = threading.RLock()

SOURCE_TYPES = ("article", "inproceedings", "book", "incollection", "techreport", "thesis", "misc", "webpage", "dataset", "software")
PROVENANCES = ("search_result", "fetched_page", "user_provided", "doi_lookup", "file", "tool_result")

# fields a citation of each type should have to render sensibly (used for "incomplete" reporting, never for filling in)
REQUIRED_FIELDS = {
    "article": ("authors", "title", "publication", "year"),
    "inproceedings": ("authors", "title", "publication", "year"),
    "book": ("authors", "title", "publisher", "year"),
    "incollection": ("authors", "title", "publication", "year"),
    "techreport": ("authors", "title", "publisher", "year"),
    "thesis": ("authors", "title", "publisher", "year"),
    "misc": ("title",),
    "webpage": ("title", "url"),
    "dataset": ("title",),
    "software": ("title",),
}

_DOI_RE = re.compile(r"^10\.\d{4,9}/[^\s]+$", re.I)
_PLACEHOLDER_RE = re.compile(r"\b(?:todo|tbd|xxx+|lorem ipsum|author name|first ?name|last ?name|et al\.? ?et al|placeholder|unknown author|anonymous)\b|\bexample\.(?:com|org)\b|10\.xxxx|10\.1234/", re.I)


# ---------------------------------------------------------------------------------------------------------------------
# URLs actually seen in tool results this task (fed by app.py after every tool call)
# ---------------------------------------------------------------------------------------------------------------------
_SEEN: dict[str, float] = {}
_SEEN_MAX = 4000


def _norm_url(u: str) -> str:
    u = (u or "").strip()
    try:
        p = urlparse(u)
    except ValueError:
        return u.lower()
    host = (p.netloc or "").lower().removeprefix("www.")
    path = (p.path or "").rstrip("/")
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def note_tool_result(name: str, result) -> int:
    """Remember every URL that appeared in a real tool result. Returns how many were noted."""
    n = 0
    stack = [(result, 0)]
    while stack and n < 600:
        obj, depth = stack.pop()
        if depth > 5:
            continue
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and k in ("url", "final_url", "source_url", "link", "page_url", "image_url", "href") and v.startswith(("http://", "https://")):
                    with _LOCK:
                        _SEEN[_norm_url(v)] = time.time()
                        if len(_SEEN) > _SEEN_MAX:
                            for old in sorted(_SEEN, key=_SEEN.get)[:500]:
                                _SEEN.pop(old, None)
                    n += 1
                elif isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))
        elif isinstance(obj, list):
            for v in obj[:80]:
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))
    return n


def url_was_seen(url: str) -> bool:
    return _norm_url(url) in _SEEN


def reset_seen() -> None:
    with _LOCK:
        _SEEN.clear()


# ---------------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------------

def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = str(doi).strip()
    d = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", d, flags=re.I).strip()
    return d.lower() or None


def doi_syntax_ok(doi: str | None) -> bool:
    return bool(doi and _DOI_RE.match(doi.strip()))


def _ascii_slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


_STOP = {"a", "an", "the", "of", "on", "in", "for", "and", "to", "with", "from", "by", "at", "towards", "toward", "using", "via", "is", "are"}


def _first_content_word(title: str) -> str:
    for w in re.findall(r"[A-Za-z0-9]+", title or ""):
        if w.lower() not in _STOP:
            return _ascii_slug(w)
    return "ref"


def _family_name(author: str) -> str:
    a = (author or "").strip()
    if a.startswith("{") and a.endswith("}"):
        return _ascii_slug(a[1:-1].split()[0] if a[1:-1].split() else "org")
    if "," in a:
        return _ascii_slug(a.split(",")[0])
    parts = a.split()
    return _ascii_slug(parts[-1]) if parts else "anon"


def _clean_author(a: str) -> str:
    """Store authors as 'Family, Given' (or a braced organisation). Never reorders when it can't tell."""
    a = re.sub(r"\s+", " ", (a or "").strip())
    if not a or a.startswith("{") or "," in a:
        return a
    parts = a.split(" ")
    if len(parts) == 1:
        return "{" + a + "}"               # single token: treat as an organisation / mononym, protect it
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _tex_escape_bib(s: str) -> str:
    out = []
    for ch in s:
        if ch in "&%#_$":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _protect_title_caps(title: str) -> str:
    """Brace words BibTeX styles would otherwise lower-case (acronyms, camelCase like YOLOv8, words after a colon)."""
    def prot(m):
        w = m.group(0)
        if len(w) >= 2 and (w.isupper() or re.search(r"[a-z][A-Z]|[A-Za-z]\d|\d[A-Za-z]", w) or (w[0].islower() is False and any(c.isupper() for c in w[1:]))):
            return "{" + w + "}"
        return w
    return re.sub(r"[A-Za-z][A-Za-z0-9\-]*", prot, title)


# ---------------------------------------------------------------------------------------------------------------------
# Crossref (real DOI registry) + URL reachability
# ---------------------------------------------------------------------------------------------------------------------

def resolve_doi(doi: str) -> dict:
    """Ask the DOI registry about a DOI. Returns {ok, metadata{...}} or {ok:False, status, error}. Never invents anything."""
    d = normalize_doi(doi)
    if not doi_syntax_ok(d):
        return {"ok": False, "status": "invalid", "error": f"'{doi}' is not a syntactically valid DOI (expected 10.NNNN/suffix)."}
    try:
        r = requests.get(CROSSREF_BASE + requests.utils.quote(d, safe="/"), timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    except requests.RequestException as e:
        return {"ok": False, "status": "unreachable", "error": f"Could not reach the DOI registry: {e.__class__.__name__}. The DOI was NOT verified."}
    if r.status_code == 404:
        return {"ok": False, "status": "not_found", "error": f"The DOI registry has no record of {d}. Do not cite it as a DOI."}
    if r.status_code != 200:
        return {"ok": False, "status": "unreachable", "error": f"DOI registry answered HTTP {r.status_code}; the DOI was NOT verified."}
    try:
        msg = r.json()["message"]
    except (ValueError, KeyError):
        return {"ok": False, "status": "unreachable", "error": "DOI registry returned an unreadable response."}
    md: dict = {"doi": d}
    title = msg.get("title") or []
    if title:
        md["title"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title[0])).strip()
    auth = []
    for a in msg.get("author") or []:
        fam, giv = (a.get("family") or "").strip(), (a.get("given") or "").strip()
        if fam and giv:
            auth.append(f"{fam}, {giv}")
        elif fam or a.get("name"):
            auth.append("{" + (fam or a.get("name")) + "}" if not fam else fam)
    if auth:
        md["authors"] = auth
    ct = msg.get("container-title") or []
    if ct:
        md["publication"] = ct[0]
    for key in ("issued", "published-print", "published-online", "created"):
        dp = ((msg.get(key) or {}).get("date-parts") or [[None]])[0]
        if dp and dp[0]:
            md["year"] = str(dp[0])
            break
    for src, dst in (("volume", "volume"), ("issue", "number"), ("page", "pages"), ("publisher", "publisher")):
        if msg.get(src):
            md[dst] = str(msg[src])
    t = msg.get("type") or ""
    md["type"] = {"journal-article": "article", "proceedings-article": "inproceedings", "book-chapter": "incollection", "book": "book",
                  "report": "techreport", "dissertation": "thesis", "dataset": "dataset", "posted-content": "misc"}.get(t, "misc")
    if md["type"] == "article" and "publication" not in md:
        md["type"] = "misc"
    if msg.get("URL"):
        md["url"] = msg["URL"]
    return {"ok": True, "status": "verified", "metadata": md, "registry": "crossref"}


def check_url(url: str) -> dict:
    """A real HTTP request. HEAD first, GET fallback (some servers reject HEAD)."""
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return {"ok": False, "status": "invalid", "error": "Not an http(s) URL."}
    hdr = {"User-Agent": USER_AGENT}
    try:
        r = requests.head(u, timeout=HTTP_TIMEOUT, headers=hdr, allow_redirects=True)
        if r.status_code in (403, 405, 400, 501) or r.status_code >= 500:
            r = requests.get(u, timeout=HTTP_TIMEOUT, headers=hdr, allow_redirects=True, stream=True)
            r.close()
    except requests.RequestException as e:
        return {"ok": False, "status": "unreachable", "error": f"{e.__class__.__name__}"}
    ok = r.status_code < 400
    return {"ok": ok, "status": "reachable" if ok else "http_error", "http_status": r.status_code, "final_url": r.url}


# ---------------------------------------------------------------------------------------------------------------------
# SourceManager
# ---------------------------------------------------------------------------------------------------------------------
class SourceManager:
    """Persistent registry of sources for one publishing workflow (JSON file)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.sources: list[dict] = []
        self._load()

    # -- persistence -----------------------------------------------------------------------------------------------
    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.sources = data.get("sources", []) if isinstance(data, dict) else []
            except (OSError, ValueError):
                self.sources = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sources": self.sources, "saved": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # -- lookup ----------------------------------------------------------------------------------------------------
    def get(self, key: str) -> dict | None:
        return next((s for s in self.sources if s["key"] == key), None)

    def _find_duplicate(self, s: dict) -> dict | None:
        doi = normalize_doi(s.get("doi"))
        nu = _norm_url(s["url"]) if s.get("url") else None
        nt = _ascii_slug(s.get("title", ""))
        for o in self.sources:
            if doi and normalize_doi(o.get("doi")) == doi:
                return o
            if nu and o.get("url") and _norm_url(o["url"]) == nu:
                return o
            if nt and len(nt) > 12 and _ascii_slug(o.get("title", "")) == nt and (not s.get("year") or not o.get("year") or s["year"] == o["year"]):
                return o
        return None

    def make_key(self, s: dict) -> str:
        fam = _family_name(s["authors"][0]) if s.get("authors") else _ascii_slug(urlparse(s.get("url") or "").netloc.split(".")[-2] if s.get("url") and "." in urlparse(s["url"]).netloc else "") or "anon"
        base = f"{fam}{s.get('year') or 'nd'}{_first_content_word(s.get('title', ''))}"
        key, i = base, 1
        taken = {x["key"] for x in self.sources}
        while key in taken:
            i += 1
            key = f"{base}{chr(96 + i) if i <= 26 else i}"
        return key

    # -- add / update ----------------------------------------------------------------------------------------------
    def add(self, fields: dict, *, provenance: str = "", evidence: str = "") -> dict:
        """Register a source. Returns {ok, source, duplicate_of?, warnings[]}. Refuses evidence-free sources."""
        title = re.sub(r"\s+", " ", str(fields.get("title") or "")).strip()
        doi = normalize_doi(fields.get("doi"))
        url = (fields.get("url") or "").strip() or None
        local = (fields.get("file") or "").strip() or None
        if not title:
            return {"ok": False, "error": "A source needs a title.", "error_code": "missing_title"}
        if not (doi or url or local):
            return {"ok": False, "error_code": "no_locator",
                    "error": "Refusing to register a source with no locator. Give a doi, a url, or a local file — a reference you cannot point at is not a reference. "
                             "If you only remember it from memory, search for it first and register what the search actually returned."}
        if doi and not doi_syntax_ok(doi):
            return {"ok": False, "error_code": "bad_doi", "error": f"'{fields.get('doi')}' is not a valid DOI. Leave the field out rather than guess."}
        stype = (fields.get("type") or "").lower() or ("article" if fields.get("publication") and not fields.get("booktitle") else "misc")
        if stype not in SOURCE_TYPES:
            stype = "misc"
        authors = fields.get("authors") or []
        if isinstance(authors, str):
            authors = [a.strip() for a in re.split(r"\s+and\s+|;", authors) if a.strip()]
        authors = [_clean_author(a) for a in authors if str(a).strip()]
        year = str(fields.get("year") or "").strip() or None
        if year and not re.fullmatch(r"(?:19|20)\d{2}[a-z]?", year):
            return {"ok": False, "error_code": "bad_year", "error": f"'{year}' is not a plausible publication year. Leave it out if unknown."}
        prov = provenance if provenance in PROVENANCES else ("doi_lookup" if fields.get("_doi_verified") else "tool_result")
        s = {"key": "", "type": stype, "title": title, "authors": authors, "publication": (fields.get("publication") or "").strip() or None,
             "year": year, "volume": fields.get("volume") or None, "number": fields.get("number") or None, "pages": fields.get("pages") or None,
             "publisher": fields.get("publisher") or None, "doi": doi, "url": url, "file": local,
             "accessed": fields.get("accessed") or time.strftime("%Y-%m-%d"), "source_type": fields.get("source_type") or stype,
             "used_in": [], "provenance": prov, "evidence": (evidence or "")[:300],
             "verification": {"doi": "verified" if fields.get("_doi_verified") else ("not_checked" if doi else "n/a"),
                              "url": "not_checked" if url else "n/a",
                              "seen_in_results": bool(url and url_was_seen(url)) or bool(fields.get("_doi_verified")),
                              "metadata": "registry" if fields.get("_doi_verified") else "as_provided"},
             "notes": (fields.get("notes") or "")[:300], "added": time.strftime("%Y-%m-%d %H:%M:%S")}
        warnings = []
        with _LOCK:
            dup = self._find_duplicate(s)
            if dup is not None:
                return {"ok": True, "duplicate_of": dup["key"], "source": dup, "warnings": [f"Already registered as '{dup['key']}' — reusing it (no duplicate created)."]}
            s["key"] = (fields.get("key") or "").strip() or self.make_key(s)
            if not re.fullmatch(r"[A-Za-z0-9_:\-\.]+", s["key"]):
                s["key"] = self.make_key(s)
            if self.get(s["key"]):
                s["key"] = self.make_key(s)
            missing = [f for f in REQUIRED_FIELDS.get(stype, ()) if not s.get(f)]
            s["incomplete"] = missing
            if missing:
                warnings.append(f"Missing fields left EMPTY (not guessed): {', '.join(missing)}.")
            if prov in ("search_result", "fetched_page") and not s["verification"]["seen_in_results"] and not doi:
                s["verification"]["seen_in_results"] = False
                warnings.append("This URL never appeared in a tool result this task — it will be flagged unverified. Search for it or fetch it first.")
            self.sources.append(s)
            self.save()
        return {"ok": True, "source": s, "warnings": warnings}

    def remove(self, key: str) -> bool:
        with _LOCK:
            n = len(self.sources)
            self.sources = [s for s in self.sources if s["key"] != key]
            if len(self.sources) != n:
                self.save()
                return True
        return False

    def mark_used(self, key: str, where: str) -> None:
        s = self.get(key)
        if s is not None and where not in s["used_in"]:
            s["used_in"].append(where)

    def apply_doi_lookup(self, key: str) -> dict:
        """Verify a registered DOI against the registry and fill ONLY empty fields from it."""
        s = self.get(key)
        if s is None:
            return {"ok": False, "error": f"No source '{key}'."}
        if not s.get("doi"):
            return {"ok": False, "error": f"Source '{key}' has no DOI to look up."}
        res = resolve_doi(s["doi"])
        with _LOCK:
            s["verification"]["doi"] = res["status"]
            if res["ok"]:
                md = res["metadata"]
                filled, conflicts = [], []
                for f in ("title", "authors", "publication", "year", "volume", "number", "pages", "publisher"):
                    if md.get(f) and not s.get(f):
                        s[f] = md[f]
                        filled.append(f)
                    elif md.get(f) and s.get(f) and f in ("title", "year") and _ascii_slug(str(md[f]))[:40] != _ascii_slug(str(s[f]))[:40]:
                        conflicts.append({"field": f, "registered": s[f], "registry": md[f]})
                if s.get("type") == "misc" and md.get("type") in ("article", "inproceedings"):
                    s["type"] = md["type"]
                s["verification"]["metadata"] = "registry"
                s["verification"]["seen_in_results"] = True
                s["incomplete"] = [f for f in REQUIRED_FIELDS.get(s["type"], ()) if not s.get(f)]
                self.save()
                out = {"ok": True, "key": key, "doi_status": "verified", "filled": filled, "source": s}
                if conflicts:
                    out["conflicts"] = conflicts
                    out["warning"] = "Registry metadata DISAGREES with what you registered for: " + ", ".join(c["field"] for c in conflicts) + ". Check that the DOI belongs to this paper."
                return out
            self.save()
        return {"ok": False, "key": key, "doi_status": res["status"], "error": res["error"]}

    def check_url_reachable(self, key: str) -> dict:
        s = self.get(key)
        if s is None or not s.get("url"):
            return {"ok": False, "error": f"Source '{key}' has no URL."}
        res = check_url(s["url"])
        with _LOCK:
            s["verification"]["url"] = res["status"]
            self.save()
        return {"key": key, **res}

    def summary(self) -> dict:
        return {"count": len(self.sources),
                "doi_verified": sum(1 for s in self.sources if s["verification"].get("doi") == "verified"),
                "unverified_locator": sum(1 for s in self.sources if not s["verification"].get("seen_in_results") and s["verification"].get("doi") != "verified"
                                          and s["verification"].get("url") != "reachable"),
                "incomplete": sum(1 for s in self.sources if s.get("incomplete"))}


# ---------------------------------------------------------------------------------------------------------------------
# CitationManager — BibTeX out, BibTeX in, citation cross-checking
# ---------------------------------------------------------------------------------------------------------------------
_BIB_TYPE = {"article": "article", "inproceedings": "inproceedings", "book": "book", "incollection": "incollection",
             "techreport": "techreport", "thesis": "phdthesis", "misc": "misc", "webpage": "misc", "dataset": "misc", "software": "misc"}


def _bib_field(name: str, value: str, *, protect: bool = False) -> str:
    v = _tex_escape_bib(str(value).strip())
    if protect:
        v = _protect_title_caps(v)
    return f"  {name} = {{{v}}}"


def source_to_bibtex(s: dict) -> str:
    """One BibTeX entry, containing ONLY fields the registry really has."""
    t = _BIB_TYPE.get(s.get("type", "misc"), "misc")
    if t == "article" and not s.get("publication"):
        t = "misc"
    f: list[str] = []
    if s.get("authors"):
        f.append("  author = {" + " and ".join(_tex_escape_bib(a) if not a.startswith("{") else a for a in s["authors"]) + "}")
    if s.get("title"):
        f.append(_bib_field("title", s["title"], protect=True))
    pub = s.get("publication")
    if pub:
        f.append(_bib_field({"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle"}.get(t, "howpublished"), pub))
    for name in ("volume", "number", "pages"):
        if s.get(name):
            val = str(s[name]).replace("--", "-").replace("-", "--") if name == "pages" else s[name]
            f.append(_bib_field(name, val))
    if s.get("publisher"):
        f.append(_bib_field("school" if t == "phdthesis" else "publisher", s["publisher"]))
    if s.get("year"):
        f.append(_bib_field("year", s["year"]))
    if s.get("doi"):
        f.append(f"  doi = {{{s['doi']}}}")
    if s.get("url"):
        f.append(f"  url = {{{s['url']}}}")
        if t == "misc" and not pub:
            f.append("  howpublished = {\\url{" + s["url"] + "}}")
        if s.get("accessed") and s.get("type") in ("webpage", "misc", "dataset", "software"):
            f.append(f"  note = {{Accessed: {s['accessed']}}}")
    return f"@{t}{{{s['key']},\n" + ",\n".join(f) + "\n}\n"


def sources_to_bib(sources: list[dict], only_keys: set[str] | None = None) -> str:
    out = ["% Generated from the publishing source registry. Fields that could not be verified are left out, not guessed.\n"]
    for s in sorted(sources, key=lambda x: x["key"]):
        if only_keys is None or s["key"] in only_keys:
            out.append(source_to_bibtex(s))
    return "\n".join(out)


def _skip_ws(t: str, i: int) -> int:
    while i < len(t) and t[i] in " \t\r\n":
        i += 1
    return i


def _read_braced(t: str, i: int) -> tuple[str, int]:
    """t[i] == '{' -> (inner text, index after the matching '}')."""
    depth, j = 0, i
    while j < len(t):
        c = t[j]
        if c == "\\" and j + 1 < len(t):
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return t[i + 1:j], j + 1
        j += 1
    return t[i + 1:], len(t)


def parse_bib(text: str) -> list[dict]:
    """Small, tolerant BibTeX parser -> [{type, key, fields{lowercase: value}, line}]."""
    entries, pos = [], 0
    head = re.compile(r"@\s*([A-Za-z]+)\s*([{(])")
    while True:
        m = head.search(text, pos)
        if not m:
            break
        typ = m.group(1).lower()
        line = text.count("\n", 0, m.start()) + 1
        if m.group(2) == "(":
            end = text.find(")", m.end())
            pos = end + 1 if end > 0 else len(text)
            continue
        body, after = _read_braced(text, m.end() - 1)
        pos = after
        if typ in ("comment", "preamble", "string"):
            continue
        if "," not in body:
            entries.append({"type": typ, "key": body.strip(), "fields": {}, "line": line})
            continue
        key, rest = body.split(",", 1)
        fields, i = {}, 0
        while i < len(rest):
            i = _skip_ws(rest, i)
            fm = re.compile(r"([A-Za-z][A-Za-z0-9_\-:]*)\s*=\s*").match(rest, i)
            if not fm:
                i += 1
                continue
            name = fm.group(1).lower()
            i = _skip_ws(rest, fm.end())
            if i >= len(rest):
                break
            if rest[i] == "{":
                val, i = _read_braced(rest, i)
            elif rest[i] == '"':
                j = i + 1
                while j < len(rest) and not (rest[j] == '"' and rest[j - 1] != "\\"):
                    j += 1
                val, i = rest[i + 1:j], j + 1
            else:
                mm = re.compile(r"[^,\s}]+").match(rest, i)
                val, i = (mm.group(0), mm.end()) if mm else ("", i + 1)
            # concatenations:  "a" # "b"
            i2 = _skip_ws(rest, i)
            while i2 < len(rest) and rest[i2] == "#":
                i2 = _skip_ws(rest, i2 + 1)
                if i2 < len(rest) and rest[i2] == "{":
                    extra, i2 = _read_braced(rest, i2)
                elif i2 < len(rest) and rest[i2] == '"':
                    j = rest.find('"', i2 + 1)
                    extra, i2 = rest[i2 + 1:j], j + 1
                else:
                    mm = re.compile(r"[^,\s}#]+").match(rest, i2)
                    extra, i2 = (mm.group(0), mm.end()) if mm else ("", i2 + 1)
                val += extra
                i2 = _skip_ws(rest, i2)
            i = i2
            fields[name] = re.sub(r"\s+", " ", val).strip()
        entries.append({"type": typ, "key": key.strip(), "fields": fields, "line": line})
    return entries


_CITE_RE = re.compile(r"\\([A-Za-z]*cite[A-Za-z]*|nocite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}")
_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.M)


def strip_tex_comments(tex: str) -> str:
    return _COMMENT_RE.sub("", tex)


def extract_citations(tex: str) -> dict:
    """-> {keys: {key: [line,...]}, nocite_all: bool}. Comments ignored."""
    clean = strip_tex_comments(tex)
    keys: dict[str, list[int]] = {}
    nocite_all = False
    for m in _CITE_RE.finditer(clean):
        line = clean.count("\n", 0, m.start()) + 1
        for k in m.group(2).split(","):
            k = k.strip()
            if not k:
                continue
            if k == "*" and m.group(1) == "nocite":
                nocite_all = True
            else:
                keys.setdefault(k, []).append(line)
    return {"keys": keys, "nocite_all": nocite_all}


_REQ_BIB = {"article": ("author", "title", "journal", "year"), "inproceedings": ("author", "title", "booktitle", "year"),
            "book": ("author", "title", "publisher", "year"), "incollection": ("author", "title", "booktitle", "year"),
            "techreport": ("author", "title", "institution", "year"), "phdthesis": ("author", "title", "school", "year"),
            "misc": ("title",)}


def validate_bibliography(tex_files: dict[str, str], bib_text: str, *, registry: SourceManager | None = None,
                          expected_style: str | None = None, bib_names: list[str] | None = None) -> dict:
    """Cross-check citations against a bibliography. `tex_files` = {relative name: text}. Pure — no network."""
    entries = parse_bib(bib_text)
    by_key: dict[str, list[dict]] = {}
    for e in entries:
        by_key.setdefault(e["key"], []).append(e)
    cited: dict[str, list[str]] = {}
    nocite_all = False
    for name, text in tex_files.items():
        ex = extract_citations(text)
        nocite_all = nocite_all or ex["nocite_all"]
        for k, lines in ex["keys"].items():
            cited.setdefault(k, []).extend(f"{name}:{ln}" for ln in lines)
    issues: list[dict] = []

    def add(level, code, msg, **kw):
        issues.append({"level": level, "code": code, "message": msg, **kw})

    keys_lower = {k.lower(): k for k in by_key}
    for k, where in sorted(cited.items()):
        if k not in by_key:
            close = difflib.get_close_matches(k, list(by_key), n=2, cutoff=0.7)
            case = keys_lower.get(k.lower())
            hint = f" Did you mean '{case}'?" if case else (f" Closest keys: {', '.join(close)}." if close else "")
            add("error", "undefined_citation", f"\\cite{{{k}}} has no entry in the bibliography.{hint}", key=k, where=where[:3], suggestion=case or (close[0] if close else None))
    for k, ents in by_key.items():
        if len(ents) > 1:
            add("error", "duplicate_key", f"Bibliography key '{k}' is defined {len(ents)} times (lines {', '.join(str(e['line']) for e in ents)}).", key=k)
    unused = [k for k in by_key if k not in cited and not nocite_all]
    for k in unused:
        add("warning", "unused_entry", f"Bibliography entry '{k}' is never cited (fine if intentional; otherwise remove it).", key=k)
    seen_doi: dict[str, str] = {}
    seen_title: dict[str, str] = {}
    for e in entries:
        f = e["fields"]
        req = _REQ_BIB.get(e["type"], ())
        miss = [r for r in req if not f.get(r) and not (r == "author" and f.get("editor"))]
        if miss and e["type"] != "misc":
            add("warning", "incomplete_entry", f"'{e['key']}' ({e['type']}) lacks: {', '.join(miss)}.", key=e["key"])
        if e["type"] == "misc" and not f.get("title"):
            add("warning", "incomplete_entry", f"'{e['key']}' has no title.", key=e["key"])
        doi = normalize_doi(f.get("doi"))
        if f.get("doi") and not doi_syntax_ok(doi):
            add("error", "bad_doi", f"'{e['key']}' has a malformed DOI '{f.get('doi')}'.", key=e["key"])
        if doi:
            if doi in seen_doi and seen_doi[doi] != e["key"]:
                add("warning", "duplicate_reference", f"'{e['key']}' and '{seen_doi[doi]}' have the same DOI — duplicate reference.", key=e["key"])
            seen_doi[doi] = e["key"]
        t = _ascii_slug(f.get("title", ""))
        if len(t) > 12:
            if t in seen_title and seen_title[t] != e["key"]:
                add("warning", "duplicate_reference", f"'{e['key']}' and '{seen_title[t]}' have the same title — duplicate reference.", key=e["key"])
            seen_title[t] = e["key"]
        joined = " ".join(f.values()) + " " + e["key"]
        if _PLACEHOLDER_RE.search(joined):
            add("error", "placeholder_content", f"'{e['key']}' contains placeholder-looking text ({_PLACEHOLDER_RE.search(joined).group(0)!r}). References must be real.", key=e["key"])
        if f.get("url") and not re.match(r"^https?://\S+$", f["url"].replace("\\_", "_")):
            add("warning", "bad_url", f"'{e['key']}' has a malformed URL.", key=e["key"])
        if registry is not None:
            src = registry.get(e["key"])
            if src is None:
                add("warning", "unregistered_reference", f"'{e['key']}' is in the .bib but not in the source registry — nothing records where it came from.", key=e["key"])
            else:
                v = src.get("verification", {})
                if src.get("doi") and v.get("doi") != "verified":
                    add("warning", "unverified_doi", f"'{e['key']}': DOI {src['doi']} was not verified against a DOI registry.", key=e["key"])
                if not src.get("doi") and not v.get("seen_in_results") and v.get("url") != "reachable":
                    add("warning", "unverified_source", f"'{e['key']}' has no verified locator (URL never seen in results and not checked).", key=e["key"])
    for k in cited:
        if registry is not None and registry.get(k) is not None:
            for where in cited[k][:1]:
                registry.mark_used(k, where)
    all_tex = "\n".join(strip_tex_comments(t) for t in tex_files.values())
    style = re.search(r"\\bibliographystyle\{([^}]*)\}", all_tex)
    biblatex = bool(re.search(r"\\usepackage(?:\[[^\]]*\])?\{biblatex\}", all_tex))
    if cited and not style and not biblatex:
        add("error", "no_bibliography_style", "The document cites sources but has neither \\bibliographystyle nor biblatex.")
    if expected_style and style and style.group(1).strip().lower() != expected_style.lower():
        add("warning", "style_mismatch", f"Bibliography style is '{style.group(1).strip()}' but the requested venue expects '{expected_style}'.")
    if cited and not re.search(r"\\bibliography\{|\\printbibliography|\\begin\{thebibliography\}", all_tex):
        add("error", "no_bibliography_command", "The document cites sources but never prints a bibliography (\\bibliography{...} / \\printbibliography).")
    if bib_names:
        for n in bib_names:
            pass
    errs = [i for i in issues if i["level"] == "error"]
    return {"ok": not errs, "citations": len(cited), "entries": len(entries), "errors": len(errs),
            "warnings": len([i for i in issues if i["level"] == "warning"]), "issues": issues,
            "undefined": [i["key"] for i in issues if i["code"] == "undefined_citation"],
            "unused": unused, "bibliography_style": style.group(1).strip() if style else ("biblatex" if biblatex else None)}
