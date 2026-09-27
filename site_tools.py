"""
site_tools.py — website quality audit for Agent Mode (Sections 30-56).

`check_site(root, entry)` inspects a folder that contains a website and reports what is actually wrong with it,
so the agent can fix problems and re-check instead of declaring victory because an HTML file was written.

What it verifies STATICALLY (always available, no browser needed):
  * every <img>/<source>/<video poster>/<link>/<script>/<a> and CSS url() reference resolves to a real file
  * every local image is a REAL image (magic bytes) — not an HTML error page saved as .jpg — with true pixel size
  * placeholder images / lorem ipsum / "[IMAGE]" text / empty src / hotlinked remote images
  * likely empty gray image boxes, an unstyled page, missing viewport meta
  * design-quality signals: responsive rules, CSS variables, hover states, transitions, reduced-motion support,
    sticky/blurred nav, modern layout (grid/flex/clamp), one <h1>, alt text, in-page anchors that point nowhere

What it verifies ONLY IF a headless browser is installed (`render=True`): console errors, failed requests, images
that failed to decode, and horizontal overflow at desktop / tablet / mobile widths. When no browser is available
the result says plainly that the page was NOT rendered — "static checks passed" is never presented as "looks good".

It cannot judge taste. It catches the objective failures; the agent still has to look critically at the design.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from web_tools import image_dimensions, sniff_image

_SKIP_SCHEMES = ("data:", "mailto:", "tel:", "javascript:", "blob:", "sms:")
_PLACEHOLDER_URL = re.compile(r"placeholder|placehold\.|dummyimage|lorempixel|fakeimg|via\.placeholder|your[-_ ]?image|image[-_ ]?here|example\.com", re.I)
_PLACEHOLDER_TEXT = re.compile(r"lorem ipsum|\[\s*image[^\]]*\]|image goes here|your (?:text|image|content) here|insert image", re.I)
_GRAY_BG = re.compile(r"background(?:-color)?\s*:\s*(?:#(?:[c-e][0-9a-f]){3}|#[c-e]{3}|lightgr[ae]y|gr[ae]y|silver|#e0e0e0|#ddd|#eee)\b", re.I)
_CSS_URL = re.compile(r"url\(\s*['\"]?([^)'\"]+?)['\"]?\s*\)", re.I)


class _Site(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._in_style = False
        self.imgs: list[dict] = []
        self.refs: list[tuple[str, str]] = []          # (kind, url)
        self.ids: set[str] = set()
        self.anchors: list[str] = []
        self.h1 = 0
        self.sections = 0
        self.has_viewport = False
        self.has_nav = False
        self.style_blocks: list[str] = []
        self.inline_styles: list[str] = []
        self.text_parts: list[str] = []
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []
        self._skip_text = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if a.get("id"):
            self.ids.add(a["id"])
        if a.get("style"):
            self.inline_styles.append(a["style"])
        if tag == "title":
            self._in_title = True
        elif tag == "style":
            self._in_style = True
            self._skip_text += 1
        elif tag == "script":
            self._skip_text += 1
            if a.get("src"):
                self.scripts.append(a["src"]); self.refs.append(("script", a["src"]))
        elif tag == "meta" and a.get("name", "").lower() == "viewport":
            self.has_viewport = True
        elif tag == "link" and a.get("href"):
            rel = a.get("rel", "").lower()
            if "stylesheet" in rel:
                self.stylesheets.append(a["href"])
            if any(x in rel for x in ("stylesheet", "icon", "preload")):
                self.refs.append(("link", a["href"]))
        elif tag == "img":
            self.imgs.append({"src": a.get("src", ""), "alt": a.get("alt"), "srcset": a.get("srcset", ""), "class": a.get("class", ""),
                              "has_size": bool(a.get("width") and a.get("height")), "loading": a.get("loading", "")})
            if a.get("src"):
                self.refs.append(("img", a["src"]))
            for cand in re.split(r",\s*", a.get("srcset", "")):
                u = cand.strip().split(" ")[0]
                if u:
                    self.refs.append(("img", u))
        elif tag == "source":
            for k in ("src", "srcset"):
                for cand in re.split(r",\s*", a.get(k, "")):
                    u = cand.strip().split(" ")[0]
                    if u:
                        self.refs.append(("source", u))
        elif tag == "video" and a.get("poster"):
            self.refs.append(("img", a["poster"]))
        elif tag == "a" and a.get("href"):
            self.anchors.append(a["href"])
        elif tag == "h1":
            self.h1 += 1
        elif tag in ("section", "article"):
            self.sections += 1
        elif tag == "nav":
            self.has_nav = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("style", "script"):
            self._in_style = False
            self._skip_text = max(0, self._skip_text - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_style:
            self.style_blocks.append(data)
        elif not self._skip_text:
            self.text_parts.append(data)


def _is_remote(u: str) -> bool:
    return u.lower().startswith(("http://", "https://", "//"))


def _resolve_local(root: Path, base_dir: Path, ref: str) -> Path | None:
    ref = unquote(ref.split("#")[0].split("?")[0]).strip()
    if not ref:
        return None
    p = (root / ref.lstrip("/")) if ref.startswith("/") else (base_dir / ref)
    try:
        p = p.resolve()
        p.relative_to(root.resolve())        # never look outside the site folder
    except (ValueError, OSError):
        return None
    return p


def _issue(sev: str, code: str, msg: str, **extra) -> dict:
    return {"severity": sev, "code": code, "message": msg, **({"fix": FIX_HINTS[code]} if code in FIX_HINTS else {}), **extra}


def check_site(root: Path, entry: str = "index.html", render: bool = False) -> dict:
    root = Path(root)
    entry_path = (root / entry)
    if not entry_path.is_file():
        found = sorted(str(p.relative_to(root)) for p in root.rglob("*.html"))[:20]
        return {"ok": False, "error": f"No {entry} in this folder.", "html_files_found": found,
                "hint": "Pass entry=<one of html_files_found>, or create the page first."}
    html = entry_path.read_text(encoding="utf-8", errors="replace")
    site = _Site()
    site.feed(html)
    base = entry_path.parent
    issues: list[dict] = []
    images_report: list[dict] = []
    css_text = "\n".join(site.style_blocks) + "\n" + "\n".join(site.inline_styles)

    # ---- linked stylesheets (local) -> fold into CSS analysis + collect their url() refs
    css_refs: list[tuple[str, Path]] = []
    for href in site.stylesheets:
        if _is_remote(href):
            continue
        p = _resolve_local(root, base, href)
        if p and p.is_file():
            txt = p.read_text(encoding="utf-8", errors="replace")
            css_text += "\n" + txt
            for u in _CSS_URL.findall(txt):
                css_refs.append((u, p.parent))
    for u in _CSS_URL.findall(css_text):
        if not any(u == r[0] for r in css_refs):
            css_refs.append((u, base))

    # ---- references: existence + image validity
    checked: set[str] = set()

    def check_ref(kind: str, ref: str, from_dir: Path):
        ref = ref.strip()
        if not ref or ref.lower().startswith(_SKIP_SCHEMES) or ref.startswith("#"):
            return
        if _is_remote(ref):
            if kind == "img":
                issues.append(_issue("warning", "remote_image", "Image is hotlinked from another site — it can break or be blocked. "
                                     "Download it into assets/images and reference the local file.", ref=ref))
            return
        p = _resolve_local(root, from_dir, ref)
        key = str(p) if p else ref
        if key in checked:
            return
        checked.add(key)
        if p is None or not p.exists():
            issues.append(_issue("error", "missing_file" if kind != "img" else "broken_image",
                                 f"{'Image' if kind == 'img' else 'File'} not found: {ref}", ref=ref))
            return
        if kind == "img" or p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"):
            if p.suffix.lower() == ".svg":
                images_report.append({"path": ref, "ok": True, "format": "svg"})
                return
            head = p.read_bytes()[:262144]
            mime, ext = sniff_image(head)
            if not mime:
                what = "an HTML page" if head.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")) else "not a valid image"
                issues.append(_issue("error", "invalid_image", f"{ref} exists but is {what} (probably a failed download saved as an image).", ref=ref))
                images_report.append({"path": ref, "ok": False})
                return
            w, h = image_dimensions(head)
            images_report.append({"path": ref, "ok": True, "mime": mime, "width": w, "height": h, "bytes": p.stat().st_size})
            if w and w < 400:
                issues.append(_issue("warning", "low_resolution", f"{ref} is only {w}x{h}px — will look soft if shown large.", ref=ref))

    for kind, ref in site.refs:
        check_ref(kind, ref, base)
    for ref, from_dir in css_refs:
        if not ref.lower().split("?")[0].split("#")[0].endswith((".woff", ".woff2", ".ttf", ".otf", ".eot")):
            check_ref("img", ref, from_dir)

    # ---- placeholders / empty images / alt text
    for im in site.imgs:
        if not im["src"].strip():
            issues.append(_issue("error", "empty_img_src", "An <img> has an empty src."))
        elif _PLACEHOLDER_URL.search(im["src"]):
            issues.append(_issue("error", "placeholder_image", f"Placeholder image in use: {im['src']} — use the real image that was asked for.", ref=im["src"]))
        if im["alt"] is None:
            issues.append(_issue("warning", "missing_alt", f"<img src=\"{im['src'][:60]}\"> has no alt attribute."))
    body_text = " ".join(site.text_parts)
    if _PLACEHOLDER_TEXT.search(body_text):
        issues.append(_issue("error", "placeholder_text", "Placeholder copy found (lorem ipsum / [IMAGE] / 'your text here')."))
    for m in re.finditer(r"[^{}]*\{[^{}]*\}", css_text):
        rule = m.group(0)
        sel = rule.split("{")[0].lower()
        if _GRAY_BG.search(rule) and re.search(r"img|image|photo|thumb|placeholder|hero", sel):
            issues.append(_issue("warning", "gray_box", f"Possible empty gray image box: {sel.strip()[:70]}"))
            break

    # ---- anchors
    for href in site.anchors:
        if href.startswith("#") and len(href) > 1 and href[1:] not in site.ids:
            issues.append(_issue("warning", "broken_anchor", f"Link {href} points to an id that doesn't exist on the page.", ref=href))
        elif href.strip() in ("", "#") or href.lower().startswith("javascript:void"):
            continue
        elif not _is_remote(href) and not href.lower().startswith(_SKIP_SCHEMES) and not href.startswith("#"):
            p = _resolve_local(root, base, href)
            if p is None or not p.exists():
                issues.append(_issue("error", "broken_link", f"Link target not found: {href}", ref=href))

    # ---- structure / design signals
    css_l = css_text.lower()
    has_css = bool(site.style_blocks or site.stylesheets or site.inline_styles)
    signals = {
        "has_stylesheet": has_css,
        "viewport_meta": site.has_viewport,
        "responsive_rules": ("@media" in css_l) or ("clamp(" in css_l) or ("auto-fit" in css_l) or ("auto-fill" in css_l),
        "css_variables": "var(--" in css_l,
        "modern_layout": ("display:grid" in css_l.replace(" ", "")) or ("display:flex" in css_l.replace(" ", "")),
        "hover_states": ":hover" in css_l,
        "transitions_or_animations": ("transition" in css_l) or ("@keyframes" in css_l),
        "reduced_motion_respected": "prefers-reduced-motion" in css_l,
        "custom_font": bool(re.search(r"font-family\s*:", css_l)),
        "sticky_or_fixed_nav": bool(re.search(r"position\s*:\s*(?:sticky|fixed)", css_l)),
        "modern_effects": bool(re.search(r"backdrop-filter|linear-gradient|radial-gradient|mask|box-shadow|clip-path|aspect-ratio|object-fit", css_l)),
        "single_h1": site.h1 == 1,
        "has_nav": site.has_nav,
        "multiple_sections": site.sections >= 3,
    }
    if not has_css:
        issues.append(_issue("error", "unstyled_page", "The page has no CSS at all — it will render as a plain browser-default document."))
    if not signals["viewport_meta"]:
        issues.append(_issue("error", "no_viewport", "Missing <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> — mobile layout will be wrong."))
    if has_css and not signals["responsive_rules"]:
        issues.append(_issue("warning", "not_responsive", "No @media queries, clamp() or auto-fit grids found — check tablet/mobile layout."))
    if has_css and not signals["hover_states"]:
        issues.append(_issue("warning", "no_hover_states", "No :hover styles — buttons/cards will feel static."))
    if has_css and not signals["custom_font"]:
        issues.append(_issue("warning", "default_typography", "No font-family declared — the page will use the browser's default serif font."))
    if signals["transitions_or_animations"] and not signals["reduced_motion_respected"]:
        issues.append(_issue("info", "no_reduced_motion", "Animations present but no prefers-reduced-motion fallback."))
    if site.h1 != 1:
        issues.append(_issue("warning", "h1_count", f"Expected exactly one <h1>, found {site.h1}."))
    if not site.sections >= 3 and len(body_text.split()) < 400:
        issues.append(_issue("warning", "thin_page", "Very little structure/content (fewer than 3 sections) — a real page needs a hero, several content sections and a call to action."))

    out = {"ok": True, "entry": entry, "title": site.title.strip(), "images": images_report, "signals": signals,
           "issues": issues, "counts": {"errors": sum(i["severity"] == "error" for i in issues),
                                       "warnings": sum(i["severity"] == "warning" for i in issues)},
           "verified": {"static": True, "rendered": False}}
    if render:
        out["render"] = render_check(entry_path)
        out["verified"]["rendered"] = bool(out["render"].get("ok"))
        for vp in out["render"].get("viewports", []):
            for msg in vp.get("console_errors", [])[:3]:
                out["issues"].append(_issue("error", "console_error", f"[{vp['name']}] {msg}"))
            for u in vp.get("failed_requests", [])[:3]:
                out["issues"].append(_issue("error", "request_failed", f"[{vp['name']}] failed to load: {u}"))
            for u in vp.get("broken_images", [])[:3]:
                out["issues"].append(_issue("error", "image_failed_to_render", f"[{vp['name']}] image did not render: {u}"))
            if vp.get("horizontal_overflow"):
                out["issues"].append(_issue("error", "horizontal_scroll", f"[{vp['name']}] page scrolls sideways (content wider than the viewport by {vp['overflow_px']}px)."))
        out["counts"] = {"errors": sum(i["severity"] == "error" for i in out["issues"]), "warnings": sum(i["severity"] == "warning" for i in out["issues"])}
    if not out["verified"]["rendered"]:
        out["note"] = ("This was a STATIC check only — the page was not rendered, so layout, spacing and visual quality are NOT verified. "
                       + (out.get("render", {}).get("error", "") if render else "Pass render=true to try a headless-browser check.")
                       + " Tell the user honestly what was and wasn't verified.")
    out["clean"] = out["counts"]["errors"] == 0
    return out


def render_check(entry_path: Path) -> dict:
    """Optional: load the page in headless Chromium at three widths. Returns {ok:False, error} if no browser is available."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "Rendering needs Playwright (pip install playwright && playwright install chromium)."}
    viewports = [("desktop", 1440, 900), ("tablet", 820, 1180), ("mobile", 390, 844)]
    results = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for name, w, h in viewports:
                    page = browser.new_page(viewport={"width": w, "height": h})
                    errors, failed = [], []
                    page.on("console", lambda m, e=errors: e.append(m.text[:200]) if m.type == "error" else None)
                    page.on("pageerror", lambda ex, e=errors: e.append(str(ex)[:200]))
                    page.on("requestfailed", lambda r, f=failed: f.append(r.url[:160]))
                    page.goto(entry_path.resolve().as_uri(), wait_until="load", timeout=20000)
                    page.wait_for_timeout(600)
                    info = page.evaluate("""() => ({
                        broken: Array.from(document.images).filter(i => i.complete && i.naturalWidth === 0).map(i => i.currentSrc || i.src),
                        sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth })""")
                    results.append({"name": name, "width": w, "console_errors": errors, "failed_requests": failed,
                                    "broken_images": info["broken"], "horizontal_overflow": info["sw"] > info["cw"] + 1,
                                    "overflow_px": max(0, info["sw"] - info["cw"])})
                    page.close()
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 — no browser binary, sandboxed, timeout...
        return {"ok": False, "error": f"Couldn't render the page ({e.__class__.__name__}: {str(e)[:160]})."}
    return {"ok": True, "viewports": results}


# ===========================================================================
# ASSET MANIFEST — every resource a page references, by kind, with real on-disk status
# ===========================================================================
_MEDIA_EXT = {
    "images": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg", ".ico"),
    "videos": (".mp4", ".webm", ".mov", ".m4v", ".ogv"),
    "audio": (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"),
    "fonts": (".woff", ".woff2", ".ttf", ".otf", ".eot"),
    "docs": (".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".csv"),
}
_JS_STRING_MEDIA = re.compile(r"""['"`]([^'"`\s]{1,240}\.(?:jpe?g|png|gif|webp|avif|svg|mp4|webm|mp3|wav|ogg|woff2?|ttf|otf))(?:\?[^'"`]*)?['"`]""", re.I)
_ASSET_DIR = {"images": "assets/images", "videos": "assets/videos", "audio": "assets/audio", "fonts": "assets/fonts", "icons": "assets/icons", "docs": "assets/docs"}


def _kind_of(ref: str) -> str:
    ext = "." + ref.lower().split("?")[0].split("#")[0].rsplit(".", 1)[-1] if "." in ref else ""
    for k, exts in _MEDIA_EXT.items():
        if ext in exts:
            return k
    return "other"


class _AssetParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[dict] = []
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self._in_style = False

    def _add(self, url, role, tag, alt=""):
        url = (url or "").strip()
        if url and not url.lower().startswith(_SKIP_SCHEMES) and not url.startswith("#"):
            self.found.append({"ref": url, "role": role, "tag": tag, "alt": alt})

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "img":
            self._add(a.get("src") or a.get("data-src"), "image", "img", a.get("alt", ""))
            for part in (a.get("srcset") or "").split(","):
                if part.strip():
                    self._add(part.strip().split()[0], "image", "img[srcset]")
        elif tag == "source":
            self._add(a.get("src"), "media_source", "source")
            for part in (a.get("srcset") or "").split(","):
                if part.strip():
                    self._add(part.strip().split()[0], "image", "source[srcset]")
        elif tag == "video":
            self._add(a.get("src"), "video", "video")
            self._add(a.get("poster"), "video_poster", "video")
        elif tag == "audio":
            self._add(a.get("src"), "audio", "audio")
        elif tag == "iframe":
            self._add(a.get("src"), "iframe", "iframe")
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "icon" in rel:
                self._add(a.get("href"), "favicon", "link")
            elif "stylesheet" in rel:
                self._add(a.get("href"), "stylesheet", "link")
                self.styles.append(a.get("href", ""))
            elif "preload" in rel and a.get("as") in ("font", "image"):
                self._add(a.get("href"), a.get("as"), "link")
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop in ("og:image", "twitter:image", "og:video"):
                self._add(a.get("content"), "og_image" if "image" in prop else "og_video", "meta")
        elif tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        if a.get("style"):
            for u in _CSS_URL.findall(a["style"]):
                self._add(u, "css_background", tag)
        elif tag == "style":
            self._in_style = True

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            for u in _CSS_URL.findall(data):
                self._add(u, "css_background", "style")


def asset_manifest(root: Path, entry: str = "index.html") -> dict:
    """Everything the page needs, grouped by kind, with exists/valid status and where it SHOULD live.

    {ok, entry, assets:{images:[{ref, role, path, exists, valid, width, height, remote}], videos, audio, fonts, docs, icons},
     missing:[...], remote:[...], needed_dirs:[...], existing_dirs:[...], summary}
    """
    root = Path(root)
    ep = root / entry
    if not ep.is_file():
        return {"ok": False, "error": f"No {entry} in this folder.", "html_files_found": sorted(str(p.relative_to(root)) for p in root.rglob("*.html"))[:20]}
    parser = _AssetParser()
    parser.feed(ep.read_text(encoding="utf-8", errors="replace"))
    found = list(parser.found)
    base = ep.parent
    # local stylesheets: url() references resolve relative to the stylesheet
    for href in parser.styles:
        if _is_remote(href):
            continue
        sp = _resolve_local(root, base, href)
        if sp and sp.is_file():
            for u in _CSS_URL.findall(sp.read_text(encoding="utf-8", errors="replace")):
                if u.strip() and not u.lower().startswith(_SKIP_SCHEMES):
                    rel = None
                    p2 = _resolve_local(root, sp.parent, u)
                    if p2:
                        try:
                            rel = str(p2.relative_to(base.resolve())).replace("\\", "/")
                        except ValueError:
                            rel = u
                    found.append({"ref": rel or u, "role": "css_background", "tag": "css", "alt": ""})
    # JS-loaded media: string literals in local scripts
    for src in parser.scripts:
        if _is_remote(src):
            continue
        sp = _resolve_local(root, base, src)
        if sp and sp.is_file():
            for m in _JS_STRING_MEDIA.finditer(sp.read_text(encoding="utf-8", errors="replace")[:400_000]):
                found.append({"ref": m.group(1), "role": "js_loaded", "tag": "script", "alt": ""})
    for m in _JS_STRING_MEDIA.finditer(ep.read_text(encoding="utf-8", errors="replace")):     # inline <script>
        if not any(f["ref"] == m.group(1) for f in found):
            found.append({"ref": m.group(1), "role": "js_loaded", "tag": "script", "alt": ""})

    assets: dict[str, list] = {k: [] for k in ("images", "videos", "audio", "fonts", "docs", "icons", "other")}
    seen, missing, remote = set(), [], []
    for f in found:
        ref = f["ref"]
        if ref in seen or f["role"] in ("stylesheet", "iframe") and _is_remote(ref):
            continue
        seen.add(ref)
        kind = "icons" if f["role"] == "favicon" else _kind_of(ref)
        if f["role"] in ("stylesheet",):
            continue
        row = {"ref": ref, "role": f["role"], "alt": f.get("alt", ""), "remote": _is_remote(ref)}
        if row["remote"]:
            remote.append(ref)
            row.update(exists=None, valid=None)
        else:
            p = _resolve_local(root, base, ref)
            row["path"] = str(p.relative_to(root.resolve())).replace("\\", "/") if p else None
            row["exists"] = bool(p and p.is_file())
            row["valid"] = None
            if row["exists"] and kind in ("images", "icons") and not ref.lower().endswith((".svg", ".ico")):
                head = p.read_bytes()[:262144]
                mime, _ext = sniff_image(head)
                row["valid"] = bool(mime)
                if mime:
                    row["width"], row["height"] = image_dimensions(head)
            elif row["exists"]:
                row["valid"] = True
            if not row["exists"] or row["valid"] is False:
                missing.append(ref)
        assets.setdefault(kind, []).append(row)
    assets = {k: v for k, v in assets.items() if v}
    needed = sorted({_ASSET_DIR[k] for k in assets if k in _ASSET_DIR})
    existing = [d for d in needed if (root / d).is_dir()]
    return {"ok": True, "entry": entry, "assets": assets, "missing": missing, "remote": remote,
            "needed_dirs": needed, "existing_dirs": existing, "missing_dirs": [d for d in needed if d not in existing],
            "summary": {k: len(v) for k, v in assets.items()} | {"missing": len(missing), "remote_hotlinks": len(remote)},
            "hint": "Create missing_dirs, download/copy the missing files into them, point the HTML at the local paths, then run check_website."}


# ===========================================================================
# HTTP AUDIT — verify a RUNNING site the way a browser's network tab would (no browser needed)
# ===========================================================================

def http_audit(base_url: str, entry_path: str = "/", max_assets: int = 120) -> dict:
    """GET the page from the live server, then GET every local asset it references. Reports HTTP failures and
    assets served with the wrong content type (e.g. an HTML error page delivered as an image)."""
    import requests
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urljoin
    url = urljoin(base_url.rstrip("/") + "/", entry_path.lstrip("/"))
    try:
        page = requests.get(url, timeout=6)
    except requests.RequestException as e:
        return {"ok": False, "error_code": "unreachable", "error": f"Could not load {url}: {e.__class__.__name__}", "url": url}
    if page.status_code >= 400:
        return {"ok": False, "error_code": "http_error", "status": page.status_code, "url": url, "error": f"The page itself returned HTTP {page.status_code}."}
    parser = _AssetParser()
    parser.feed(page.text)
    refs = [f for f in parser.found if not _is_remote(f["ref"])]
    for s in parser.scripts:
        if not _is_remote(s):
            refs.append({"ref": s, "role": "script", "tag": "script"})
    seen, uniq = set(), []
    for f in refs:
        if f["ref"] not in seen:
            seen.add(f["ref"]); uniq.append(f)
    uniq = uniq[:max_assets]

    def probe(f):
        u = urljoin(url, f["ref"])
        try:
            r = requests.get(u, timeout=8, stream=True)
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].lower()
            head = next(r.iter_content(64), b"")
            r.close()
            row = {"ref": f["ref"], "url": u, "status": r.status_code, "content_type": ctype, "role": f["role"], "ok": r.status_code < 400}
            k = _kind_of(f["ref"])
            if row["ok"] and k in ("images",) and not f["ref"].lower().endswith((".svg", ".ico")) and not sniff_image(head)[0]:
                row.update(ok=False, problem="served as an image but the bytes are not an image")
            elif row["ok"] and k == "images" and ctype.startswith("text/html"):
                row.update(ok=False, problem="server returned an HTML page instead of an image")
            return row
        except requests.RequestException as e:
            return {"ref": f["ref"], "url": u, "status": None, "ok": False, "problem": e.__class__.__name__, "role": f["role"]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(probe, uniq))
    bad = [r for r in rows if not r["ok"]]
    return {"ok": True, "clean": not bad, "url": url, "checked": len(rows), "failed": bad,
            "verified": {"http": True, "rendered": False},
            "message": f"{len(rows) - len(bad)}/{len(rows)} assets load over HTTP" + (f"; {len(bad)} broken" if bad else " — all OK")}


FIX_HINTS = {
    "broken_image": "Download/copy the image into assets/images and point the <img> at it, or remove the <img> if it isn't needed.",
    "invalid_image": "Delete the bad file and re-download from a different source (verify magic bytes after).",
    "remote_image": "Download the image into assets/images and reference the local path.",
    "placeholder_image": "Search for a real image (search_images), download it, replace the placeholder.",
    "missing_file": "Create or download the missing file, or fix the path.",
    "no_viewport": "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> to <head>.",
    "horizontal_scroll": "Find the element wider than the viewport (fixed widths, images without max-width:100%) and constrain it.",
    "broken_link": "Fix the href or create the target page.",
    "unstyled_page": "Add a stylesheet (link rel=stylesheet or <style>).",
}
