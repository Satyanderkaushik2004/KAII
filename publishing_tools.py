"""publishing_tools — plugs the research/report/deck production layer into computer_tools' registry.

Same pattern as web_tools.py / computer_batch.py: import this once from app.py and register() wires the
tool defs + executors + prompt section into computer_tools, so Agent Mode gets them automatically.

DESTINATION rule (mirrors computer_run_terminal's existing convention): every tool below takes a
`destination`/`project` path that is either the literal word "workspace" (routed to the project workspace,
like a website build) or a real computer location (resolved exactly like every other computer_* tool). This
is not a new convention — computer_run_terminal's `working_directory` already accepts "workspace" alongside
real paths; publishing artifacts follow the same rule instead of inventing a third file-location model.

HONESTY BOUNDARY (spec section 26 — don't build a fake feature): this module does NOT drive a browser inside
Overleaf's website. The only browser capability anywhere in this codebase is Playwright used read-only for
render *checks* (site_tools) — there is no click/type/upload automation tool to build on. "Overleaf mode"
here means: produce a correct, compiling local LaTeX project (optionally zipped) that the user uploads to
Overleaf themselves, or pushes via Overleaf's git integration if they've set that up. If real browser-drive
tools are ever added to computer_tools, this is the module to extend — not to fake in the meantime.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import computer_tools as ct
import agent_core as ac
import publishing_agent as pa
import publishing_sources as ps

_S = ct._S
_obj = ct._obj
_PATH_HELP = ct._PATH_HELP + " Or the literal word 'workspace' to use the project workspace, like computer_run_terminal accepts."


def _resolve_dest(raw: str) -> Path:
    """'workspace' -> the real workspace dir; anything else -> the normal computer-path resolver."""
    s = (raw or "").strip()
    if s.lower() in ("workspace", "the workspace", "project", "the project"):
        ws = ct._workspace_dir()
        if ws is None:
            raise ct.PathError("No workspace is open.", "not_found")
        return ws
    return ct.resolve_computer_path(s)


def _project_paths(root: Path) -> dict:
    return {"root": root, "sources_json": root / ".publishing" / "sources.json", "main_tex": root / "main.tex", "bib": root / "references.bib"}


# ---------------------------------------------------------------------------------------------------------------------
# 1. classify — cheap, read-only, code-backed (never invents an answer the way a free-text guess could)
# ---------------------------------------------------------------------------------------------------------------------
def tool_classify_publishing_request(args: dict, ctx) -> dict:
    intent = pa.ArtifactDetector.detect(args.get("text", ""))
    out = {"ok": True, "is_publishing_task": intent.is_publishing, "artifact_type": intent.artifact_type,
          "venue": intent.venue, "platform": intent.platform, "suggested_format": intent.format,
          "is_conversion_from_existing_content": intent.is_conversion, "confidence": intent.confidence}
    if intent.ambiguous:
        out["ask_user"] = intent.ambiguous
    if intent.venue:
        spec, status = pa.FormatIntelligence().resolve(intent.venue)
        out["venue_template"] = {"label": spec.label, "official_docs": spec.official_docs, "notes": spec.notes, **status}
    return out


# ---------------------------------------------------------------------------------------------------------------------
# 2. create_research_project — real scaffold + main.tex, confirmed + verified like create_document
# ---------------------------------------------------------------------------------------------------------------------
def tool_create_research_project(args: dict, ctx) -> dict:
    dest_raw = (args.get("destination") or "").strip()
    if not dest_raw:
        return {"ok": False, "error": "A destination is required — a real computer path, or 'workspace'. Use ask_location if the user didn't say."}
    project_name = (args.get("project_name") or "").strip() or "research_paper"
    try:
        base = _resolve_dest(dest_raw)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    root = base / project_name
    ct._check_writable_target(root)
    exists = root.exists()
    venue = (args.get("venue") or "").strip().lower() or None
    fmt = pa.FormatIntelligence()
    spec, status = fmt.resolve(venue)

    content = pa.PaperContent(
        title=(args.get("title") or "Untitled").strip(), authors=list(args.get("authors") or []),
        affiliations=list(args.get("affiliations") or []), abstract=(args.get("abstract") or "").strip(),
        keywords=list(args.get("keywords") or []), sections=dict(args.get("sections") or {}),
        figures=list(args.get("figures") or []), tables=list(args.get("tables") or []))

    action = {"action": "create_document", "permission": ct.PERMISSIONS["create_document"], "icon": "📄",
              "title": "PROJECT ALREADY EXISTS" if exists else f"CREATE {spec.label.upper()} LATEX PROJECT",
              "danger": False, "path": str(root),
              "fields": [{"label": "TARGET", "value": str(root)}, {"label": "VENUE", "value": spec.label},
                         {"label": "STATUS", "value": "Real vendored template" if status["vendored"] else "Generic fallback — " + status["source"]}],
              "notes": ([f"'{root}' already exists — main.tex will be overwritten (references.bib and figures/ are left alone)."] if exists else [])}
    if exists:
        action["conflict"] = True
        opts = ct._conflict_options(False)     # same [cancel, overwrite, save_as_copy] convention as every other create tool
    else:
        opts = [ct._opt("cancel", "Cancel"), ct._opt("confirm", "Create Project", "primary")]

    def run(decision, value):
        target_root = ct.unique_path(root) if decision == "save_as_copy" else root
        target_root.mkdir(parents=True, exist_ok=True)
        mgr = pa.LatexProjectManager(target_root, fmt)
        scaffold = mgr.scaffold(venue)
        write = mgr.write_main_tex(content, venue, acm_format=args.get("acm_format") or "sigconf")
        ok = (target_root / "main.tex").is_file() and (target_root / "main.tex").stat().st_size > 0
        result = {"ok": ok, "verified": ok, "path": str(target_root), "main_tex": write["path"], "scaffold": scaffold,
                  "message": f"✓ {spec.label} project created — {target_root}" if ok else f"Verification failed: main.tex missing/empty at {target_root}"}
        if write.get("warning"):
            result["warning"] = write["warning"]
        if not status["vendored"]:
            result["template_notice"] = (f"No vendored/official {venue} template was available, so this used the generic article class instead. "
                                         f"Official template: {status['source']}")
        return result
    return ct._confirm_and_run(ctx, action, opts, f"creating the research project at {root}", run)


# ---------------------------------------------------------------------------------------------------------------------
# 3. compile — a FIXED, safe command (latexmk/pdflatex only, no shell text from the model), run inside a
#    project the user already confirmed creating, so this does not need its own confirmation card — same
#    trust level as run_file / check_problems, and much narrower than computer_run_terminal's arbitrary command.
# ---------------------------------------------------------------------------------------------------------------------
def tool_compile_research_project(args: dict, ctx) -> dict:
    proj = (args.get("project") or "").strip()
    if not proj:
        return {"ok": False, "error": "A project path is required (the path returned by create_research_project)."}
    try:
        root = _resolve_dest(proj)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    if not (root / "main.tex").is_file():
        return {"ok": False, "error": f"No main.tex in {root} — call create_research_project first."}
    mgr = pa.LatexProjectManager(root)
    comp = mgr.compile_with_retry(max_attempts=int(args.get("max_attempts") or 3))
    if comp["ok"]:
        ver = mgr.verify()
        comp["verify"] = ver
        comp["verified"] = ver["ok"]
    return comp


# ---------------------------------------------------------------------------------------------------------------------
# 4/5/6. sources + bibliography — no filesystem risk beyond a small JSON/.bib inside an already-approved project
# ---------------------------------------------------------------------------------------------------------------------
def tool_register_source(args: dict, ctx) -> dict:
    proj = (args.get("project") or "").strip()
    if not proj:
        return {"ok": False, "error": "A project path is required."}
    try:
        root = _resolve_dest(proj)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    p = _project_paths(root)
    sm = ps.SourceManager(p["sources_json"])
    res = sm.add({k: args.get(k) for k in ("title", "authors", "year", "doi", "url", "file", "publication",
                                           "publisher", "volume", "number", "pages", "type", "key", "notes")},
                provenance=args.get("provenance") or "user_provided", evidence=args.get("evidence") or "")
    return res


def tool_verify_source(args: dict, ctx) -> dict:
    proj, key = (args.get("project") or "").strip(), (args.get("key") or "").strip()
    if not proj or not key:
        return {"ok": False, "error": "project and key are both required."}
    try:
        root = _resolve_dest(proj)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    sm = ps.SourceManager(_project_paths(root)["sources_json"])
    if sm.get(key) is None:
        return {"ok": False, "error": f"No registered source '{key}' — call register_source first."}
    out = {}
    src = sm.get(key)
    if src.get("doi"):
        out["doi_check"] = sm.apply_doi_lookup(key)
    if src.get("url"):
        out["url_check"] = sm.check_url_reachable(key)
    if not out:
        return {"ok": False, "error": f"'{key}' has neither a DOI nor a URL to verify."}
    return {"ok": True, "key": key, **out, "source": sm.get(key)}


def tool_generate_bibliography(args: dict, ctx) -> dict:
    proj = (args.get("project") or "").strip()
    if not proj:
        return {"ok": False, "error": "A project path is required."}
    try:
        root = _resolve_dest(proj)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    p = _project_paths(root)
    sm = ps.SourceManager(p["sources_json"])
    if not sm.sources:
        return {"ok": False, "error": "No sources registered yet — call register_source for each reference first (never invent entries)."}
    bib = ps.sources_to_bib(sm.sources)
    p["bib"].write_text(bib, encoding="utf-8")
    ok = p["bib"].is_file() and p["bib"].stat().st_size > 0
    summary = sm.summary()
    result = {"ok": ok, "verified": ok, "path": str(p["bib"]), "entries": len(sm.sources), "summary": summary,
              "message": f"✓ references.bib written with {len(sm.sources)} entr{'y' if len(sm.sources)==1 else 'ies'} — {p['bib']}"}
    if summary["unverified_locator"]:
        result["warning"] = f"{summary['unverified_locator']} source(s) have no verified DOI/URL — consider verify_source before treating this as submission-ready."
    return result


def tool_validate_paper_bibliography(args: dict, ctx) -> dict:
    proj = (args.get("project") or "").strip()
    if not proj:
        return {"ok": False, "error": "A project path is required."}
    try:
        root = _resolve_dest(proj)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    p = _project_paths(root)
    if not p["main_tex"].is_file():
        return {"ok": False, "error": f"No main.tex in {root}."}
    tex_files = {f.name: f.read_text(encoding="utf-8", errors="replace") for f in root.glob("*.tex")}
    bib_text = p["bib"].read_text(encoding="utf-8", errors="replace") if p["bib"].exists() else ""
    sm = ps.SourceManager(p["sources_json"]) if p["sources_json"].exists() else None
    venue_style = None
    if args.get("venue"):
        spec, _ = pa.FormatIntelligence().resolve(args["venue"])
        venue_style = spec.bib_style
    res = ps.validate_bibliography(tex_files, bib_text, registry=sm, expected_style=venue_style)
    if sm is not None:
        sm.save()
    return {"ok": res["ok"], **res}


# ---------------------------------------------------------------------------------------------------------------------
# 7/8. Excel + PowerPoint — confirmed + on-disk-verified, same pattern as create_document
# ---------------------------------------------------------------------------------------------------------------------
def tool_create_excel_report(args: dict, ctx) -> dict:
    dest_raw = (args.get("path") or "").strip()
    if not dest_raw:
        return {"ok": False, "error": "A destination path (with filename) is required."}
    try:
        p = _resolve_dest(dest_raw) if dest_raw.lower().startswith(("workspace", "the workspace")) else ct.resolve_computer_path(dest_raw)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    if p.suffix.lower() != ".xlsx":
        p = p.with_suffix(".xlsx")
    rows = args.get("rows") or []
    if not rows:
        return {"ok": False, "error": "`rows` (a list of row objects) is required — the same shape create_document uses for xlsx."}
    ct._check_writable_target(p)
    exists = p.exists()
    action = {"action": "create_document", "permission": ct.PERMISSIONS["create_document"], "icon": "📊",
              "title": "FILE ALREADY EXISTS" if exists else "CREATE EXCEL REPORT", "danger": False, "path": str(p),
              "fields": [{"label": "TARGET", "value": str(p)}, {"label": "ROWS", "value": str(len(rows))}], "notes": []}
    if exists:
        action["conflict"] = True
        opts = ct._conflict_options(False)
    else:
        opts = [ct._opt("cancel", "Cancel"), ct._opt("confirm", "Create Report", "primary")]

    def run(decision, value):
        target = ct.unique_path(p) if decision == "save_as_copy" else p
        xm = pa.ExcelReportManager()
        res = xm.build(target, rows, title=args.get("title") or "Report", numeric_cols=args.get("numeric_cols"),
                       sheet_structure=args.get("sheet_structure"))
        if not res["ok"]:
            return res
        ver = xm.verify(target)
        res["verified"] = ver["ok"]
        res["message"] = f"✓ Excel report created and verified — {target}" if ver["ok"] else f"Written but verification failed: {target}"
        return res
    return ct._confirm_and_run(ctx, action, opts, f"creating {p}", run)


def tool_create_presentation(args: dict, ctx) -> dict:
    dest_raw = (args.get("path") or "").strip()
    if not dest_raw:
        return {"ok": False, "error": "A destination path (with filename) is required."}
    try:
        p = _resolve_dest(dest_raw) if dest_raw.lower().startswith(("workspace", "the workspace")) else ct.resolve_computer_path(dest_raw)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    if p.suffix.lower() != ".pptx":
        p = p.with_suffix(".pptx")
    slides = args.get("slides") or []
    if not slides:
        return {"ok": False, "error": "`slides` (a list of {title, bullets}) is required."}
    ct._check_writable_target(p)
    exists = p.exists()
    action = {"action": "create_document", "permission": ct.PERMISSIONS["create_document"], "icon": "📽",
              "title": "FILE ALREADY EXISTS" if exists else "CREATE PRESENTATION", "danger": False, "path": str(p),
              "fields": [{"label": "TARGET", "value": str(p)}, {"label": "SLIDES", "value": str(len(slides) + 1)}], "notes": []}
    if exists:
        action["conflict"] = True
        opts = ct._conflict_options(False)
    else:
        opts = [ct._opt("cancel", "Cancel"), ct._opt("confirm", "Create Presentation", "primary")]

    def run(decision, value):
        target = ct.unique_path(p) if decision == "save_as_copy" else p
        pm = pa.PresentationManager()
        res = pm.build(target, args.get("title") or "Presentation", args.get("subtitle") or "", slides)
        if not res["ok"]:
            return res
        ver = pm.verify(target, expected_slide_count=res["slide_count"])
        res["verified"] = ver["ok"]
        res["message"] = f"✓ Presentation created and verified — {target}" if ver["ok"] else f"Written but verification failed: {target}"
        return res
    return ct._confirm_and_run(ctx, action, opts, f"creating {p}", run)


# ---------------------------------------------------------------------------------------------------------------------
# 9. generic verifier for any produced artifact
# ---------------------------------------------------------------------------------------------------------------------
def tool_verify_publishing_artifact(args: dict, ctx) -> dict:
    raw = (args.get("path") or "").strip()
    if not raw:
        return {"ok": False, "error": "A path is required."}
    try:
        p = _resolve_dest(raw) if raw.lower().startswith(("workspace", "the workspace")) else ct.resolve_computer_path(raw)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    return pa.verify_artifact(p, args.get("format"), expected_slide_count=args.get("expected_slide_count"))


# ---------------------------------------------------------------------------------------------------------------------
# 10. Overleaf export — real zip of a real, already-compiling project. NOT browser automation (see module docstring).
# ---------------------------------------------------------------------------------------------------------------------
def tool_export_overleaf_zip(args: dict, ctx) -> dict:
    proj = (args.get("project") or "").strip()
    if not proj:
        return {"ok": False, "error": "A project path is required."}
    try:
        root = _resolve_dest(proj)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    if not (root / "main.tex").is_file():
        return {"ok": False, "error": f"No main.tex in {root} — nothing to export."}
    dest_raw = (args.get("destination") or "").strip() or str(root.parent)
    try:
        dest_folder = _resolve_dest(dest_raw)
    except ct.PathError as e:
        return {"ok": False, "error": str(e), "error_code": e.code}
    zip_path = dest_folder / f"{root.name}_overleaf.zip"
    ct._check_writable_target(zip_path)
    exists = zip_path.exists()
    action = {"action": "create_document", "permission": ct.PERMISSIONS["create_document"], "icon": "🗜",
              "title": "FILE ALREADY EXISTS" if exists else "EXPORT OVERLEAF-READY ZIP", "danger": False, "path": str(zip_path),
              "fields": [{"label": "SOURCE PROJECT", "value": str(root)}, {"label": "ZIP", "value": str(zip_path)}],
              "notes": ["This does NOT upload to Overleaf automatically — there is no browser-automation tool for that. "
                       "Upload the zip yourself at overleaf.com → New Project → Upload Project, or push these files to a git repo Overleaf is linked to."]}
    opts = (ct._conflict_options(False) if exists else [ct._opt("cancel", "Cancel"), ct._opt("confirm", "Create Zip", "primary")])
    if exists:
        action["conflict"] = True

    def run(decision, value):
        target = ct.unique_path(zip_path) if decision == "save_as_copy" else zip_path
        skip = {".git", "__pycache__", ".publishing"}
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in root.rglob("*"):
                if f.is_file() and not any(part in skip for part in f.relative_to(root).parts):
                    if f.suffix.lower() in (".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".synctex.gz"):
                        continue
                    zf.write(f, f.relative_to(root))
        ok = target.is_file() and target.stat().st_size > 0
        return {"ok": ok, "verified": ok, "path": str(target), "message": (f"✓ Overleaf-ready zip created — {target}. Upload it manually at overleaf.com (no browser-automation tool exists to do this step for you)."
                                                                            if ok else f"Verification failed: {target} missing/empty.")}
    return ct._confirm_and_run(ctx, action, opts, f"exporting {root} to a zip", run)


# ---------------------------------------------------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------------------------------------------------
_LOCATOR = {"type": "object", "properties": {
    "title": _S, "authors": {"type": "array", "items": _S}, "year": _S, "doi": _S, "url": _S, "file": _S,
    "publication": _S, "publisher": _S, "volume": _S, "number": _S, "pages": _S,
    "type": {"type": "string", "enum": list(ps.SOURCE_TYPES)}, "key": _S, "notes": _S,
}, "required": ["title"]}

TOOL_DEFS: list[dict] = [
    {"name": "classify_publishing_request",
     "description": ("Code-backed classification of a request into artifact type (research_paper, conference_paper, thesis, "
                     "literature_review, technical_report, business_report, excel_report, presentation, ...), venue (ieee/acm/springer/"
                     "elsevier/none), and platform (plain file, latex_project, overleaf_export). Call this FIRST for any 'make me a paper/"
                     "report/deck/spreadsheet' request before deciding how to build it — it also tells you which official template is "
                     "actually available versus a generic fallback, and flags anything genuinely ambiguous (e.g. which conference) that "
                     "you should ask about instead of guessing."),
     "parameters": _obj({"text": _S}, ["text"])},
    {"name": "create_research_project",
     "description": ("Scaffold a REAL LaTeX project (main.tex + references.bib + figures/tables/supplementary/output folders) for a "
                     "research/conference paper, using the REAL official class file for the venue when one is vendored (ieee -> IEEEtran, "
                     "acm -> acmart, springer -> llncs, elsevier -> elsarticle) or a plain article class ('generic'/omitted) otherwise — "
                     "never a hand-drawn imitation of a venue's layout. `sections` maps section keys (introduction, related_work, "
                     "methodology, experiments, results, discussion, limitations, conclusion, or any custom key) to plain text / light "
                     "markdown ('- ' bullets); embed real \\cite{key} tokens for citations you've already registered with register_source "
                     "— they pass through untouched, everything else in the text is escaped. `figures`/`tables` add real figure/table "
                     "blocks. Shows a confirmation card and verifies main.tex exists on disk before returning ok:true."),
     "parameters": _obj({
         "destination": {"type": "string", "description": _PATH_HELP},
         "project_name": {"type": "string", "description": "Folder name for the project (default 'research_paper')."},
         "venue": {"type": "string", "enum": list(pa.VENUE_SPECS), "description": "Omit or 'generic' for a plain article."},
         "acm_format": {"type": "string", "description": "acmart sub-format, default 'sigconf'."},
         "title": _S, "authors": {"type": "array", "items": _S}, "affiliations": {"type": "array", "items": _S},
         "abstract": _S, "keywords": {"type": "array", "items": _S},
         "sections": {"type": "object", "description": "e.g. {\"introduction\": \"...\", \"methodology\": \"...\"}"},
         "figures": {"type": "array", "items": _obj({"path": _S, "caption": _S, "label": _S}, ["path"])},
         "tables": {"type": "array", "items": _obj({"caption": _S, "label": _S, "rows": {"type": "array", "items": {"type": "array", "items": _S}}}, ["rows"])},
     }, ["destination", "title"])},
    {"name": "compile_research_project",
     "description": ("REALLY compile a LaTeX project created by create_research_project (latexmk -pdf, or pdflatex if latexmk isn't "
                     "installed; bibtex/biber runs automatically when latexmk is available). On failure returns a real diagnosis "
                     "(missing package/file, undefined citation, unbalanced braces, wrong environment order, ...) parsed from the actual "
                     "compiler log — fix the cause in main.tex/references.bib yourself and call this again; it retries automatically only "
                     "for the one case that's safe to retry blindly (needing another bibliography pass). On success also runs a real "
                     "verification pass (PDF actually opens, has pages, text extracts, no unresolved '??' references)."),
     "parameters": _obj({"project": {"type": "string", "description": _PATH_HELP}, "max_attempts": {"type": "integer", "description": "Default 3."}}, ["project"])},
    {"name": "register_source",
     "description": ("Register ONE real source (paper, book, dataset, webpage) for a project. REFUSES to register anything with no "
                     "locator — a doi, a url, or a local file — because a reference you can't point at isn't a reference; search for it "
                     "first if you only recall it from memory. Missing fields are left empty, never guessed. Detects and reuses "
                     "duplicates (same DOI/URL/title+year) instead of creating a second entry. Returns a citation `key` to use as "
                     "\\cite{key} in create_research_project's section text."),
     "parameters": _obj({"project": {"type": "string", "description": _PATH_HELP}, **_LOCATOR["properties"],
                         "provenance": {"type": "string", "enum": list(ps.PROVENANCES)}, "evidence": _S}, ["project", "title"])},
    {"name": "verify_source",
     "description": ("Actually check a registered source against a real DOI registry (Crossref) and/or fetch its URL with a real HTTP "
                     "request. Only a registry hit marks the DOI 'verified'; only a real 2xx/3xx response marks the URL 'reachable' — "
                     "nothing here is assumed. When the registry disagrees with what was registered (wrong title/year), that is reported "
                     "as a conflict, not silently overwritten."),
     "parameters": _obj({"project": {"type": "string", "description": _PATH_HELP}, "key": _S}, ["project", "key"])},
    {"name": "generate_bibliography",
     "description": "Write references.bib from every source registered so far via register_source for this project. Refuses if no sources are registered — never invents entries. Call this before compiling once you have real citations.",
     "parameters": _obj({"project": {"type": "string", "description": _PATH_HELP}}, ["project"])},
    {"name": "validate_paper_bibliography",
     "description": ("Cross-check every \\cite{...} in the project's .tex files against references.bib and the source registry: "
                     "undefined citations, duplicate keys/references, incomplete entries, placeholder-looking text, missing "
                     "\\bibliographystyle/\\bibliography commands, and (when sources are registered) which citations still have no "
                     "verified DOI/URL. Run this before declaring a paper's references done — a paper that compiles can still cite "
                     "garbage; this is the check that catches that."),
     "parameters": _obj({"project": {"type": "string", "description": _PATH_HELP}, "venue": {"type": "string", "enum": list(pa.VENUE_SPECS)}}, ["project"])},
    {"name": "create_excel_report",
     "description": ("Build a REAL multi-sheet Excel workbook (Raw Data / Clean Data / Analysis / Charts / Executive Summary by default, "
                     "override with `sheet_structure`) from `rows` (one object per row, like create_document's xlsx shape). Analysis sheet "
                     "uses REAL formulas (COUNT/SUM/AVERAGE/MIN/MAX/STDEV referencing the data sheet, not pasted-in numbers), Charts sheet "
                     "gets a real chart object, headers/freeze panes/auto-filter are set. Verifies the workbook actually reopens with real "
                     "sheets/formulas/data before returning ok:true."),
     "parameters": _obj({"path": {"type": "string", "description": _PATH_HELP + " Include the filename, e.g. 'Downloads\\\\report.xlsx'."},
                         "rows": {"type": "array", "items": {"type": "object"}}, "title": _S,
                         "numeric_cols": {"type": "array", "items": _S, "description": "Optional explicit list; auto-detected otherwise."},
                         "sheet_structure": {"type": "array", "items": _S, "description": "Optional custom sheet name list (default 5 sheets above)."}},
                        ["path", "rows"])},
    {"name": "create_presentation",
     "description": ("Build a REAL PowerPoint deck (title slide + one slide per `slides` entry: {title, bullets}). Flags slides that are "
                     "likely to overflow (too much text) instead of silently producing a broken-looking slide. Verifies the deck actually "
                     "reopens with the right slide count and no leftover placeholder text before returning ok:true."),
     "parameters": _obj({"path": {"type": "string", "description": _PATH_HELP + " Include the filename, e.g. 'Downloads\\\\deck.pptx'."},
                         "title": _S, "subtitle": _S,
                         "slides": {"type": "array", "items": _obj({"title": _S, "bullets": {"type": "array", "items": _S}}, ["title"])}},
                        ["path", "title", "slides"])},
    {"name": "verify_publishing_artifact",
     "description": "Re-check any produced document (pdf/xlsx/pptx/docx) actually exists and is structurally sound (opens, has pages/sheets/slides, text extracts). Use this as a final check before telling the user something is done, especially after any manual edits.",
     "parameters": _obj({"path": {"type": "string", "description": _PATH_HELP}, "format": _S, "expected_slide_count": {"type": "integer"}}, ["path"])},
    {"name": "export_overleaf_zip",
     "description": ("Zip a LaTeX project into an Overleaf-uploadable archive. This does NOT upload it to Overleaf automatically — no "
                     "browser-automation tool exists in this app for driving overleaf.com, so say so plainly and tell the user to upload "
                     "the zip themselves (overleaf.com -> New Project -> Upload Project) or push it via git if they've linked a repo."),
     "parameters": _obj({"project": {"type": "string", "description": _PATH_HELP}, "destination": {"type": "string", "description": _PATH_HELP + " Default: the project's parent folder."}}, ["project"])},
]

_EXECUTORS = {
    "classify_publishing_request": tool_classify_publishing_request,
    "create_research_project": tool_create_research_project,
    "compile_research_project": tool_compile_research_project,
    "register_source": tool_register_source,
    "verify_source": tool_verify_source,
    "generate_bibliography": tool_generate_bibliography,
    "validate_paper_bibliography": tool_validate_paper_bibliography,
    "create_excel_report": tool_create_excel_report,
    "create_presentation": tool_create_presentation,
    "verify_publishing_artifact": tool_verify_publishing_artifact,
    "export_overleaf_zip": tool_export_overleaf_zip,
}

_CONFIRMED_TOOLS = {"create_research_project", "create_excel_report", "create_presentation", "export_overleaf_zip"}
_NETWORK_TOOLS = {"verify_source"}          # hits Crossref / does a real HTTP HEAD/GET

PROMPT = """
PUBLISHING WORKFLOW — research papers, reports, spreadsheets, slide decks
Trigger on: "make/write/create a research paper / conference paper / thesis / technical report / literature "
review / white paper / case study / Excel report / PowerPoint / presentation", or "convert this paper into
slides/a report/a spreadsheet". Call classify_publishing_request FIRST — don't guess venue/platform/format
yourself when a code-backed answer exists.

  1. CLASSIFY. classify_publishing_request(text). If it returns ask_user items (e.g. "which conference?"),
     ask that ONE question before building anything with a wrong template.
  2. RESEARCH (when the topic needs real facts/sources, not just formatting): deep_search / web_search for
     the actual content. For every citation you intend to use, call register_source with the REAL doi/url it
     came from — never invent a reference, an author, a year, or a DOI. register_source refuses sources with
     no locator; that refusal is doing its job, not a bug to route around.
  3. BUILD.
       - LaTeX paper/thesis/conference paper: create_research_project (real official venue class file when
         one exists — IEEE/ACM/Springer/Elsevier — else a plain generic class, never a hand-drawn imitation).
         Write \\cite{key} for every registered source directly in the section text.
       - Excel report / data analysis / dashboard: create_excel_report with the real rows.
       - PowerPoint / slide deck: create_presentation, or extract the real structure from an existing
         paper/report first (read it) rather than starting blank when the request is "make a PPT from this".
       - Word doc / PDF report / other plain document: the existing create_document tool already covers this.
  4. BIBLIOGRAPHY (LaTeX only). generate_bibliography once sources are registered, then
     validate_paper_bibliography — fix every error it reports (undefined citation, placeholder text, missing
     \\bibliographystyle, ...) before compiling for real.
  5. COMPILE + DEBUG LOOP (LaTeX only). compile_research_project. On failure, read `diagnosis`, fix the ACTUAL
     cause in main.tex/references.bib (edit_file/computer_create_file as appropriate for where the project
     lives), and call compile_research_project again. Two attempts producing the same error means stop
     guessing and explain what's actually wrong instead of a third blind retry — same rule as any other debug
     loop in this app.
  6. VERIFY. compile_research_project's own verify step covers LaTeX; for other formats call
     verify_publishing_artifact. Never say "done" on the strength of a write call alone.
  7. OVERLEAF: there is no browser-automation tool for overleaf.com in this app. If the user explicitly asked
     for Overleaf, build the real local project as above, then export_overleaf_zip and tell them to upload it
     themselves — do not claim to have done that upload.
  8. Never claim a page/venue limit, mandatory package, or submission deadline you have not actually verified —
     classify_publishing_request's `official_docs` link is what to check or point the user to, not a fact to
     assert from memory.
"""


def register() -> None:
    have = {d["name"] for d in ct.TOOL_DEFS}
    for d in TOOL_DEFS:
        if d["name"] not in have:
            ct.TOOL_DEFS.append(d)
    ct._EXECUTORS.update(_EXECUTORS)
    ct.TOOL_NAMES.update(_EXECUTORS)
    ct.STREAMING_TOOL_NAMES.update(_CONFIRMED_TOOLS)
    ct.NETWORK_TOOL_NAMES.update(_NETWORK_TOOLS)
    ac.NETWORK_TOOLS.update(_NETWORK_TOOLS)     # so OFFLINE mode also hides it from the model's tool list, not just blocks execution
    for name in _CONFIRMED_TOOLS:
        ct.PERMISSIONS.setdefault(name, "filesystem_create")
    if PROMPT not in ct.PROMPT_EXTRAS:
        ct.PROMPT_EXTRAS.append(PROMPT)


register()
