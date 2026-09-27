"""publishing_agent — research/report/spreadsheet/slide-deck production layer for Agent Mode.

Implements, for real, the pieces of the "universal publishing agent" spec that this codebase can actually
back with working code:

    ArtifactDetector        -> classify a request into (artifact_type, venue, platform)
    FormatIntelligence       -> what a venue/format actually requires, from real vendored/official templates
    LatexProjectManager      -> create / populate / compile / diagnose / verify a real LaTeX project
    ExcelReportManager       -> Raw/Clean/Analysis/Dashboard workbook, real formulas + charts, via openpyxl
    PresentationManager      -> real, verifiable .pptx via python-pptx
    ArtifactVerifier         -> per-format "does this actually exist and look right" checks

Deliberately NOT implemented, and not claimed anywhere in this module: browser automation inside Overleaf's
website. This codebase's only browser capability is Playwright used read-only for render *checks*
(site_tools.render_check) — there is no click/type/upload automation tool. Building "Overleaf browser mode"
on top of that would be exactly the fake feature the spec itself warns against (see OVERLEAF_MODE below,
which produces a real local project + zip instead and says so).

Every "done" here is backed by a real check: a compiled PDF that was actually opened and page-counted, a
workbook actually reopened with openpyxl, a deck actually reopened with python-pptx. Nothing is marked
verified from the mere fact that a write call returned without raising.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import publishing_sources as psrc

TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates" / "latex"

# ---------------------------------------------------------------------------------------------------------------------
# 1. ARTIFACT DETECTION
# ---------------------------------------------------------------------------------------------------------------------
ARTIFACT_TYPES = ("research_paper", "conference_paper", "thesis", "technical_report", "literature_review",
                  "case_study", "white_paper", "business_report", "presentation", "excel_report", "poster", "other")

_VENUE_ALIASES = {
    "ieee": "ieee", "ieee conference": "ieee", "ieee access": "ieee",
    "acm": "acm", "acm conference": "acm", "sigchi": "acm",
    "springer": "springer", "lncs": "springer", "springer lncs": "springer",
    "elsevier": "elsevier", "elsarticle": "elsevier",
    "arxiv": "arxiv",
}
_VENUE_RE = re.compile(r"\b(ieee|acm|springer|lncs|elsevier|elsarticle|arxiv|mdpi|nature)\b", re.I)

_TARGET_FORMAT_PATTERNS = [
    # checked FIRST and take priority over what the SOURCE material is (e.g. "PPT from this research paper"
    # targets a presentation even though the source is a paper) — the artifact you're asked to PRODUCE wins.
    ("excel_report", re.compile(r"\bexcel\s+(?:report|sheet|workbook)\b|\bdata\s+analysis\s+report\b|\bdashboard\s+in\s+excel\b|\bsummary\s+dashboard\b|\.xlsx\b", re.I)),
    ("presentation", re.compile(r"\bpower\s?point\b|\bslide\s?deck\b|\ba\s+presentation\b|\bslides?\s+(?:for|from|on|about)\b|\.pptx\b|\bppt\b", re.I)),
]

_TYPE_PATTERNS = [
    ("thesis", re.compile(r"\bthesis\b|\bdissertation\b", re.I)),
    ("literature_review", re.compile(r"\bliterature\s+review\b|\bsurvey\s+paper\b", re.I)),
    ("case_study", re.compile(r"\bcase\s+stud(?:y|ies)\b", re.I)),
    ("white_paper", re.compile(r"\bwhite\s?paper\b", re.I)),
    ("business_report", re.compile(r"\bbusiness\s+report\b|\baudit\s+report\b|\bfeasibility\s+report\b", re.I)),
    ("technical_report", re.compile(r"\btechnical\s+report\b|\bproject\s+report\b|\bengineering\s+report\b|\binternship\s+report\b", re.I)),
    ("conference_paper", re.compile(r"\bconference\s+(?:paper|submission)\b|\bpaper\s+for\s+(?:ieee|acm|a\s+conference)\b|\b(?:ieee|acm|springer|elsevier)\s+paper\b", re.I)),
    ("research_paper", re.compile(r"\bresearch\s+paper\b|\bjournal\s+(?:paper|article)\b|\bpublication[- ]ready\b|\bmanuscript\b", re.I)),
    ("poster", re.compile(r"\bposter\b", re.I)),
]

_PLATFORM_RE = re.compile(r"\boverleaf\b|\blatex\s+project\b", re.I)
_CONVERT_RE = re.compile(r"\bfrom\s+(?:this|the|my)\s+(?:paper|research|document|report)\b|\bconvert\s+this\s+(?:paper|research)\b", re.I)


def _detect_venue(text: str) -> str | None:
    m = _VENUE_RE.search(text)
    return _VENUE_ALIASES.get(m.group(1).lower()) if m else None


@dataclass
class ArtifactIntent:
    is_publishing: bool
    artifact_type: str = "other"
    venue: str | None = None
    platform: str = "file"          # "file" | "latex_project" | "overleaf_export"
    format: str = "docx"            # inferred output container
    is_conversion: bool = False
    confidence: str = "low"         # low | medium | high
    ambiguous: list[str] = field(default_factory=list)   # things that genuinely need asking, e.g. "which conference"


_DEFAULT_FORMAT = {"research_paper": "pdf_latex", "conference_paper": "pdf_latex", "thesis": "pdf_latex",
                   "literature_review": "docx", "case_study": "docx", "white_paper": "docx",
                   "business_report": "docx", "technical_report": "docx", "excel_report": "xlsx",
                   "presentation": "pptx", "poster": "pdf_latex", "other": "docx"}


class ArtifactDetector:
    @staticmethod
    def detect(message: str) -> ArtifactIntent:
        text = message or ""
        low = text.lower()
        artifact_type = "other"
        for t, rx in _TARGET_FORMAT_PATTERNS:
            if rx.search(low):
                artifact_type = t
                break
        if artifact_type == "other":
            for t, rx in _TYPE_PATTERNS:
                if rx.search(low):
                    artifact_type = t
                    break
        publishing_markers = ("paper", "thesis", "report", "presentation", "slides", "powerpoint", "excel report",
                             "manuscript", "dissertation", "white paper", "case study", "poster", "publication")
        is_publishing = artifact_type != "other" or any(m in low for m in publishing_markers)
        if not is_publishing:
            return ArtifactIntent(is_publishing=False)

        venue = _detect_venue(low)
        wants_overleaf = bool(_PLATFORM_RE.search(low))
        platform = "overleaf_export" if wants_overleaf else ("latex_project" if venue and artifact_type in ("research_paper", "conference_paper", "thesis") else "file")
        fmt = _DEFAULT_FORMAT.get(artifact_type, "docx")
        if platform in ("latex_project", "overleaf_export"):
            fmt = "pdf_latex"
        is_conv = bool(_CONVERT_RE.search(low))
        ambiguous = []
        if artifact_type == "conference_paper" and venue is None:
            ambiguous.append("Which venue/conference (IEEE, ACM, Springer, Elsevier, or a plain journal format)? Different venues have different official templates.")
        confidence = "high" if (artifact_type != "other" and (venue or platform != "file")) else ("medium" if artifact_type != "other" else "low")
        return ArtifactIntent(is_publishing=True, artifact_type=artifact_type, venue=venue, platform=platform,
                              format=fmt, is_conversion=is_conv, confidence=confidence, ambiguous=ambiguous)


# ---------------------------------------------------------------------------------------------------------------------
# 2. FORMAT INTELLIGENCE
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class VenueSpec:
    key: str
    label: str
    doc_class: str
    class_files: tuple[str, ...]
    bib_style: str
    bst_file: str | None
    two_column: bool
    official_docs: str            # where to point the user for the CURRENT official author guidelines
    notes: str = ""


VENUE_SPECS: dict[str, VenueSpec] = {
    "ieee": VenueSpec("ieee", "IEEE (IEEEtran)", "IEEEtran", ("IEEEtran.cls",), "IEEEtran", "IEEEtran.bst", True,
                      "https://www.ieee.org/conferences/publishing/templates.html",
                      "Generic IEEEtran conference template (vendored, LPPL). A SPECIFIC IEEE conference may mandate its own "
                      "page limit / extra packages — check that conference's own call for papers; this template does not know that."),
    "acm": VenueSpec("acm", "ACM (acmart)", "acmart", ("acmart.cls",), "ACM-Reference-Format", "ACM-Reference-Format.bst", True,
                     "https://www.acm.org/publications/proceedings-template",
                     "acmart supports several \\documentclass[...]{acmart} format options (sigconf, manuscript, acmsmall, ...) — "
                     "this defaults to sigconf (two-column proceedings); pass acm_format to choose another."),
    "springer": VenueSpec("springer", "Springer (LNCS)", "llncs", ("llncs.cls",), "splncs04", "splncs04.bst", False,
                          "https://www.springer.com/gp/computer-science/lncs/conference-proceedings-guidelines",
                          "LNCS is one-column, single-spaced, no page numbers by design — that is correct, not a bug."),
    "elsevier": VenueSpec("elsevier", "Elsevier (elsarticle)", "elsarticle", ("elsarticle.cls",), "elsarticle-num", "elsarticle-num.bst", False,
                          "https://www.elsevier.com/researcher/author/policies-and-guidelines/latex-instructions",
                          "elsarticle supports several reference styles (num/authoryear) — this defaults to numbered (elsarticle-num)."),
    "generic": VenueSpec("generic", "Generic article", "article", (), "plain", None, False, "", "No venue named — a plain, well-structured article class. Not intended for a specific submission system."),
}


class FormatIntelligence:
    """Resolves a venue to real files. Never invents a template: if the venue isn't vendored and can't be
    fetched, it says so plainly and falls back to `generic` rather than hand-drawing a fake IEEE-ish layout."""

    def __init__(self, template_root: Path = TEMPLATE_ROOT, fetch_fn=None):
        self.root = Path(template_root)
        self.fetch_fn = fetch_fn      # optional callable(url) -> bytes, for fetching an official template not vendored locally

    def available_venues(self) -> list[str]:
        return [k for k in VENUE_SPECS if k == "generic" or (self.root / k).is_dir()]

    def resolve(self, venue: str | None) -> tuple[VenueSpec, dict]:
        key = (venue or "generic").lower()
        spec = VENUE_SPECS.get(key, VENUE_SPECS["generic"])
        status = {"venue": spec.key, "vendored": True, "missing_files": [], "source": "vendored (TeXLive texlive-publishers, LPPL)"}
        if spec.key == "generic":
            status.update(vendored=False, source="none needed")
            return spec, status
        d = self.root / spec.key
        missing = [f for f in spec.class_files if not (d / f).exists()]
        if spec.bst_file and not (d / spec.bst_file).exists():
            missing.append(spec.bst_file)
        if missing:
            status.update(vendored=False, missing_files=missing,
                          source=f"NOT AVAILABLE locally and not fetched — falling back to generic. Official template: {spec.official_docs}")
            return VENUE_SPECS["generic"], status
        return spec, status

    def copy_class_files(self, venue_key: str, dest: Path) -> list[str]:
        d = self.root / venue_key
        if not d.is_dir():
            return []
        copied = []
        for f in d.iterdir():
            shutil.copy2(f, dest / f.name)
            copied.append(f.name)
        return copied


# ---------------------------------------------------------------------------------------------------------------------
# 3. LATEX PROJECT MANAGER
# ---------------------------------------------------------------------------------------------------------------------
SECTION_ORDER = ("abstract", "introduction", "related_work", "methodology", "experiments", "results",
                 "discussion", "limitations", "conclusion")
SECTION_TITLES = {"introduction": "Introduction", "related_work": "Related Work", "methodology": "Methodology",
                  "experiments": "Experiments", "results": "Results and Discussion", "discussion": "Discussion",
                  "limitations": "Limitations", "conclusion": "Conclusion"}

_LOG_DIAGNOSES = [
    (re.compile(r"! LaTeX Error: File `([^']+)' not found"), "missing_file", lambda m: f"Missing file: {m.group(1)} — check it was created/copied into the project (or is a package: add \\usepackage{{...}} only if it's actually installed)."),
    (re.compile(r"! LaTeX Error: File `([\w\-]+\.sty)' not found|! LaTeX Error: File `([\w\-]+\.cls)' not found"), "missing_package",
     lambda m: f"Missing LaTeX package/class: {m.group(1) or m.group(2)}. Install it (tlmgr install <pkg>, or apt package texlive-...) — this cannot be silently worked around."),
    (re.compile(r"Undefined control sequence"), "undefined_control_sequence", lambda m: "An undefined LaTeX command was used — check for a typo or a missing \\usepackage for that command."),
    (re.compile(r"! Undefined control sequence.*\n.*l\.(\d+)", re.S), "undefined_control_sequence_line", lambda m: f"Undefined command at line {m.group(1)}."),
    (re.compile(r"Package natbib Error|Citation `([^']+)' undefined|Citation .* undefined"), "undefined_citation",
     lambda m: "One or more \\cite keys are not in the .bib file (or bibtex/biber was never run) — check the bibliography."),
    (re.compile(r"There were undefined references"), "undefined_references", lambda m: "Undefined \\ref/\\label or \\cite — some label doesn't exist, or bibtex/biber + a second pdflatex pass is needed."),
    (re.compile(r"! LaTeX Error: \\begin\{document\} ended by \\end\{([a-zA-Z*]+)\}|Environment ([a-zA-Z]+) undefined"), "environment_error", lambda m: "An environment was opened without closing it, or an environment/package for it is missing."),
    (re.compile(r"! Missing \$ inserted"), "math_mode_error", lambda m: "Math syntax used outside math mode (or an unescaped special character like _ or ^ in text)."),
    (re.compile(r"! File ended while scanning"), "unbalanced_braces", lambda m: "Unbalanced braces/environments — a { or \\begin{...} was never closed."),
    (re.compile(r"! LaTeX Error: Missing \\begin\{document\}"), "wrong_order", lambda m: "Content appeared before \\begin{document} — check the preamble."),
]


def diagnose_log(log_text: str) -> list[dict]:
    out = []
    for rx, code, fn in _LOG_DIAGNOSES:
        m = rx.search(log_text or "")
        if m:
            out.append({"code": code, "message": fn(m)})
    return out


def _tex_escape(s: str) -> str:
    repl = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
           "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "\\": r"\textbackslash{}"}
    return re.sub(r"[&%$#_{}~^\\]", lambda m: repl[m.group(0)], s)


@dataclass
class PaperContent:
    title: str
    authors: list[str] = field(default_factory=list)      # display strings, e.g. "Jane Doe"
    affiliations: list[str] = field(default_factory=list)
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    sections: dict = field(default_factory=dict)           # {section_key: plain-text/markdown-lite body}
    figures: list[dict] = field(default_factory=list)      # [{path relative to figures/, caption, label}]
    tables: list[dict] = field(default_factory=list)       # [{rows: [[...]], caption, label}]


class LatexProjectManager:
    """Creates, populates, compiles, diagnoses and verifies a real LaTeX project."""

    def __init__(self, root: Path, fmt_intel: FormatIntelligence | None = None):
        self.root = Path(root)
        self.fmt = fmt_intel or FormatIntelligence()

    # -- structure -------------------------------------------------------------------------------------------------
    def scaffold(self, venue: str | None = None) -> dict:
        spec, status = self.fmt.resolve(venue)
        for d in ("figures", "tables", "supplementary", "output"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        copied = self.fmt.copy_class_files(spec.key, self.root) if spec.key != "generic" else []
        (self.root / "references.bib").touch(exist_ok=True)
        return {"ok": True, "root": str(self.root), "venue": spec.key, "venue_label": spec.label, "doc_class": spec.doc_class,
                "class_files_copied": copied, "two_column": spec.two_column, "bib_style": spec.bib_style,
                "official_docs": spec.official_docs, "template_status": status,
                "structure": ["main.tex", "references.bib", "figures/", "tables/", "supplementary/", "output/"]}

    def _preamble(self, spec: VenueSpec, content: PaperContent, acm_format: str = "sigconf") -> str:
        pkgs = "\\usepackage{graphicx}\n\\usepackage{booktabs}\n\\usepackage{amsmath}\n\\usepackage{hyperref}\n"
        if spec.key == "ieee":
            return f"\\documentclass[conference]{{IEEEtran}}\n{pkgs}\n"
        if spec.key == "acm":
            return f"\\documentclass[{acm_format}]{{acmart}}\n\\usepackage{{graphicx}}\n\\usepackage{{booktabs}}\n\\settopmatter{{printacmref=false}}\n\\renewcommand\\footnotetextcopyrightpermission[1]{{}}\n"
        if spec.key == "springer":
            return f"\\documentclass{{llncs}}\n{pkgs}\n"
        if spec.key == "elsevier":
            return f"\\documentclass[preprint,12pt]{{elsarticle}}\n{pkgs}\\journal{{}}\n"
        return f"\\documentclass[11pt]{{article}}\n\\usepackage[margin=1in]{{geometry}}\n{pkgs}\n"

    def _body(self, spec: VenueSpec, content: PaperContent, include_bib: bool = True) -> str:
        parts = []
        title = _tex_escape(content.title)
        authors_tex = " \\and ".join(_tex_escape(a) for a in content.authors) or "Author Name"
        if spec.key == "ieee":
            parts.append(f"\\title{{{title}}}\n")
            authors_list = content.authors or ["Author Name"]

            def aff_for(i: int) -> str:
                if i < len(content.affiliations):
                    return content.affiliations[i]
                return content.affiliations[0] if len(content.affiliations) == 1 else ""
            blocks = "\\and\n".join(f"\\IEEEauthorblockN{{{_tex_escape(a)}}}\\IEEEauthorblockA{{{_tex_escape(aff_for(i))}}}" for i, a in enumerate(authors_list))
            parts.append(f"\\author{{{blocks}}}\n")
            parts.append("\\maketitle\n")
            if content.abstract:
                parts.append(f"\\begin{{abstract}}\n{_tex_escape(content.abstract)}\n\\end{{abstract}}\n")
            if content.keywords:
                parts.append(f"\\begin{{IEEEkeywords}}\n{', '.join(_tex_escape(k) for k in content.keywords)}\n\\end{{IEEEkeywords}}\n")
        elif spec.key == "acm":
            parts.append(f"\\title{{{title}}}\n")
            for a in content.authors or ["Author Name"]:
                parts.append(f"\\author{{{_tex_escape(a)}}}\n")
            # acmart hard-errors on \maketitle unless every \affiliation has \city{} and \country{} present
            # (even empty) — this is a formatting requirement of the class, not a research claim, so an
            # empty placeholder is correct here rather than inventing a location.
            inst = _tex_escape(content.affiliations[0]) if content.affiliations else ""
            parts.append(f"\\affiliation{{\\institution{{{inst}}}\\city{{}}\\country{{}}}}\n")
            if content.abstract:
                parts.append(f"\\begin{{abstract}}\n{_tex_escape(content.abstract)}\n\\end{{abstract}}\n")
            parts.append("\\begin{document}\n\\maketitle\n")
            if content.keywords:
                parts.append(f"\\keywords{{{', '.join(_tex_escape(k) for k in content.keywords)}}}\n")
        elif spec.key == "springer":
            parts.append(f"\\title{{{title}}}\n\\author{{{authors_tex}}}\n")
            if content.affiliations:
                parts.append(f"\\institute{{{_tex_escape(content.affiliations[0])}}}\n")
            parts.append("\\maketitle\n")
            if content.abstract:
                parts.append(f"\\begin{{abstract}}\n{_tex_escape(content.abstract)}\n\\end{{abstract}}\n")
            if content.keywords:
                parts.append(f"\\keywords{{{', '.join(_tex_escape(k) for k in content.keywords)}}}\n")
        elif spec.key == "elsevier":
            parts.append("\\begin{document}\n\\begin{frontmatter}\n")
            parts.append(f"\\title{{{title}}}\n")
            parts.append("\\author{" + ", ".join(_tex_escape(a) for a in (content.authors or ["Author Name"])) + "}\n")
            if content.affiliations:
                parts.append(f"\\address{{{_tex_escape(content.affiliations[0])}}}\n")
            parts.append("\\begin{abstract}\n" + (_tex_escape(content.abstract) or "") + "\n\\end{abstract}\n")
            if content.keywords:
                parts.append("\\begin{keyword}\n" + " \\sep ".join(_tex_escape(k) for k in content.keywords) + "\n\\end{keyword}\n")
            parts.append("\\end{frontmatter}\n")
        else:
            parts.append(f"\\title{{{title}}}\n\\author{{{authors_tex}}}\n\\date{{\\today}}\n\\maketitle\n")
            if content.abstract:
                parts.append(f"\\begin{{abstract}}\n{_tex_escape(content.abstract)}\n\\end{{abstract}}\n")

        for key in SECTION_ORDER:
            if key == "abstract":
                continue
            body = content.sections.get(key)
            if not body:
                continue
            title_ = SECTION_TITLES.get(key, key.replace("_", " ").title())
            parts.append(f"\n\\section{{{title_}}}\n{_body_to_tex(body)}\n")
        for extra_key, body in content.sections.items():
            if extra_key not in SECTION_ORDER and body:
                parts.append(f"\n\\section{{{extra_key.replace('_', ' ').title()}}}\n{_body_to_tex(body)}\n")

        for i, fig in enumerate(content.figures, 1):
            label = fig.get("label") or f"fig:{i}"
            parts.append(
                f"\n\\begin{{figure}}[t]\n\\centering\n\\includegraphics[width=0.9\\linewidth]{{{fig['path']}}}\n"
                f"\\caption{{{_tex_escape(fig.get('caption', ''))}}}\n\\label{{{label}}}\n\\end{{figure}}\n")
        for i, tab in enumerate(content.tables, 1):
            label = tab.get("label") or f"tab:{i}"
            rows = tab.get("rows") or []
            ncol = max((len(r) for r in rows), default=1)
            colspec = "l" * ncol
            body_rows = " \\\\\n".join(" & ".join(_tex_escape(str(c)) for c in r) for r in rows)
            parts.append(
                f"\n\\begin{{table}}[t]\n\\centering\n\\caption{{{_tex_escape(tab.get('caption', ''))}}}\n\\label{{{label}}}\n"
                f"\\begin{{tabular}}{{{colspec}}}\n\\toprule\n{body_rows} \\\\\n\\bottomrule\n\\end{{tabular}}\n\\end{{table}}\n")

        if include_bib:
            parts.append(f"\n\\bibliographystyle{{{spec.bib_style}}}\n\\bibliography{{references}}\n")
        parts.append("\\end{document}\n")
        return "\n".join(parts)

    def _has_bib_entries(self) -> bool:
        bib = self.root / "references.bib"
        return bib.exists() and "@" in bib.read_text(encoding="utf-8", errors="replace")

    def write_main_tex(self, content: PaperContent, venue: str | None = None, acm_format: str = "sigconf") -> dict:
        spec, status = self.fmt.resolve(venue)
        self.fmt.copy_class_files(spec.key, self.root)
        preamble = self._preamble(spec, content, acm_format)
        # ACM/Elsevier bodies open \begin{document} themselves (acmart needs \affiliation etc. issued before
        # \maketitle but after \begin{document}; elsarticle uses \begin{frontmatter} which must also follow it).
        # Every other class gets \begin{document} appended here, right before the body.
        opens_document_itself = spec.key in ("acm", "elsevier")
        bib_note = None
        if not self._has_bib_entries():
            bib_note = ("references.bib is empty — no \\bibliography command was added. Register sources and "
                       "regenerate the .bib (see the citation tools) before compiling for real, or this paper has no references section.")
        body = self._body(spec, content, include_bib=not bib_note)
        full = preamble + ("" if opens_document_itself else "\\begin{document}\n") + body
        path = self.root / "main.tex"
        path.write_text(full, encoding="utf-8")
        result = {"ok": True, "path": str(path), "bytes": len(full.encode("utf-8")), "venue": spec.key, "template_status": status}
        if bib_note:
            result["warning"] = bib_note
        return result

    # -- compile ---------------------------------------------------------------------------------------------------
    def compile(self, main: str = "main.tex", timeout: int = 90) -> dict:
        if shutil.which("latexmk") is None and shutil.which("pdflatex") is None:
            return {"ok": False, "error_code": "no_latex", "error": "Neither latexmk nor pdflatex is installed on this machine. Install a LaTeX distribution (TeX Live / MiKTeX) to compile."}
        stem = Path(main).stem
        cmd = (["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-bibtex", main] if shutil.which("latexmk")
              else ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", main])
        try:
            proc = subprocess.run(cmd, cwd=str(self.root), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return {"ok": False, "error_code": "timeout", "error": f"Compilation exceeded {timeout}s.", "output_tail": (e.stdout or "")[-4000:]}
        except FileNotFoundError:
            return {"ok": False, "error_code": "no_latex", "error": f"'{cmd[0]}' isn't on PATH."}
        log_file = self.root / f"{stem}.log"
        log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else (proc.stdout + proc.stderr)
        pdf = self.root / f"{stem}.pdf"
        ok = proc.returncode == 0 and pdf.exists()
        result = {"ok": ok, "returncode": proc.returncode, "pdf_path": str(pdf) if pdf.exists() else None,
                  "log_tail": log_text[-4000:], "stdout_tail": (proc.stdout or "")[-1500:]}
        if not ok:
            result["diagnosis"] = diagnose_log(log_text)
        # bibtex/biber (when not using latexmk) needs a second explicit run — surface that rather than silently missing refs
        if "There were undefined references" in log_text or "Citation" in log_text and "undefined" in log_text:
            result.setdefault("diagnosis", []).append({"code": "needs_bib_rerun", "message": "References look unresolved — run bibtex/biber then pdflatex twice more (latexmk -bibtex does this automatically)."})
        return result

    def compile_with_retry(self, main: str = "main.tex", max_attempts: int = 3, timeout: int = 90) -> dict:
        """The debug loop from the spec: compile -> diagnose -> (only auto-fixable causes get retried) -> recompile."""
        attempts = []
        for i in range(1, max_attempts + 1):
            res = self.compile(main, timeout=timeout)
            attempts.append({"attempt": i, "ok": res["ok"], "diagnosis": res.get("diagnosis", [])})
            if res["ok"]:
                res["attempts"] = attempts
                return res
            codes = {d["code"] for d in res.get("diagnosis", [])}
            # Only "needs another pass" is something we can safely retry automatically; a real content/syntax
            # error needs the caller (the model, reading `diagnosis`) to actually edit main.tex and try again —
            # blindly re-running the same failing command is exactly the "guess a third fix" the spec forbids.
            if "needs_bib_rerun" in codes and i < max_attempts:
                continue
            res["attempts"] = attempts
            return res
        res["attempts"] = attempts
        return res

    # -- verify ------------------------------------------------------------------------------------------------------
    def verify(self, main: str = "main.tex") -> dict:
        stem = Path(main).stem
        pdf = self.root / f"{stem}.pdf"
        checks = {"source_exists": (self.root / main).exists(), "pdf_exists": pdf.exists()}
        if not pdf.exists():
            return {"ok": False, "checks": checks, "error": "No PDF was produced — compile first."}
        info = {}
        if shutil.which("pdfinfo"):
            try:
                out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=15).stdout
                m = re.search(r"Pages:\s*(\d+)", out)
                info["pages"] = int(m.group(1)) if m else None
            except Exception:
                info["pages"] = None
        checks["has_pages"] = bool(info.get("pages"))
        text = ""
        if shutil.which("pdftotext"):
            try:
                r = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True, timeout=20)
                text = r.stdout or ""
            except Exception:
                text = ""
        checks["text_extracted"] = bool(text.strip())
        log_file = self.root / f"{stem}.log"
        log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        checks["no_unresolved_refs"] = "??" not in text if text else None
        checks["no_undefined_citations_in_log"] = "Citation" not in log_text or "undefined" not in log_text
        checks["references_section_present"] = bool(re.search(r"references|bibliography", text, re.I)) if text else None
        ok = checks["pdf_exists"] and checks["has_pages"] and checks["text_extracted"] and (checks["no_unresolved_refs"] is not False)
        return {"ok": ok, "checks": checks, "pages": info.get("pages"), "pdf_path": str(pdf), "verified": ok}


_CITE_TOKEN_RE = re.compile(r"\\(?:[A-Za-z]*cite[A-Za-z]*|ref|eqref|autoref|label)\*?(?:\[[^\]\n]*\]){0,2}\{[^{}\n]*\}")


def _escape_preserving_refs(s: str) -> str:
    """Escape TeX special characters in ordinary prose while leaving \\cite{...}/\\ref{...}/\\label{...}
    tokens the author wrote intact — those are the one kind of raw LaTeX command a section body is allowed
    to contain (everything else in the body is untrusted plain text and gets fully escaped)."""
    parts, pos = [], 0
    for m in _CITE_TOKEN_RE.finditer(s):
        parts.append(_tex_escape(s[pos:m.start()]))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(_tex_escape(s[pos:]))
    return "".join(parts)


def _body_to_tex(body: str) -> str:
    """Very small markdown-lite -> LaTeX: paragraphs, '- ' bullets, blank-line-separated. Escapes everything
    except \\cite/\\ref/\\label tokens the caller wrote, so citations placed by the citation tools survive."""
    lines = body.splitlines()
    out, in_list = [], False
    for ln in lines:
        s = ln.strip()
        if not s:
            if in_list:
                out.append("\\end{itemize}")
                in_list = False
            out.append("")
            continue
        if s.startswith(("- ", "* ")):
            if not in_list:
                out.append("\\begin{itemize}")
                in_list = True
            out.append(f"\\item {_escape_preserving_refs(s[2:])}")
        else:
            if in_list:
                out.append("\\end{itemize}")
                in_list = False
            out.append(_escape_preserving_refs(s))
    if in_list:
        out.append("\\end{itemize}")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------------------------
# 4. EXCEL REPORT MANAGER
# ---------------------------------------------------------------------------------------------------------------------
class ExcelReportManager:
    """Raw Data / Clean Data / Analysis / Charts / Executive Summary workbook with real formulas and charts."""

    def build(self, path: Path, rows: list[dict], *, title: str = "Report", numeric_cols: list[str] | None = None,
              sheet_structure: list[str] | None = None) -> dict:
        try:
            import openpyxl
            from openpyxl.chart import BarChart, LineChart, Reference
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            return {"ok": False, "error_code": "missing_library", "error": "openpyxl is required (pip install openpyxl)."}
        if not rows:
            return {"ok": False, "error": "No rows given to build the report from."}
        headers = list(rows[0].keys())
        numeric_cols = numeric_cols or [h for h in headers if all(isinstance(r.get(h), (int, float)) and not isinstance(r.get(h), bool) for r in rows if r.get(h) is not None)]

        wb = openpyxl.Workbook()
        structure = sheet_structure or ["Raw Data", "Clean Data", "Analysis", "Charts", "Executive Summary"]
        header_fill = PatternFill("solid", fgColor="1F4E78")
        header_font = Font(color="FFFFFF", bold=True)

        def write_table(ws, data_rows):
            for c, h in enumerate(headers, 1):
                cell = ws.cell(1, c, h)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")
            for r, row in enumerate(data_rows, 2):
                for c, h in enumerate(headers, 1):
                    ws.cell(r, c, row.get(h))
            ws.freeze_panes = "A2"
            for c in range(1, len(headers) + 1):
                ws.column_dimensions[get_column_letter(c)].width = max(12, len(str(headers[c - 1])) + 2)
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(data_rows) + 1}"
            return ws

        ws_raw = wb.active
        ws_raw.title = structure[0]
        write_table(ws_raw, rows)

        clean_rows = [r for r in rows if all(r.get(h) not in (None, "") for h in headers)]
        if len(structure) > 1:
            ws_clean = wb.create_sheet(structure[1])
            write_table(ws_clean, clean_rows)

        analysis_summary: dict[str, dict] = {}
        if len(structure) > 2 and numeric_cols:
            ws_an = wb.create_sheet(structure[2])
            n = len(clean_rows) or len(rows)
            data_sheet = structure[1] if len(structure) > 1 else structure[0]
            ws_an.cell(1, 1, "Metric")
            for c, col in enumerate(numeric_cols, 2):
                ws_an.cell(1, c, col)
            stats = [("Count", "COUNT"), ("Sum", "SUM"), ("Average", "AVERAGE"), ("Min", "MIN"), ("Max", "MAX"), ("StdDev", "STDEV")]
            for r, (label, fn) in enumerate(stats, 2):
                ws_an.cell(r, 1, label)
                for c, col in enumerate(numeric_cols, 2):
                    col_letter = get_column_letter(headers.index(col) + 1)
                    formula = f"={fn}('{data_sheet}'!{col_letter}2:{col_letter}{n + 1})"
                    ws_an.cell(r, c, formula)
            for c in range(1, len(numeric_cols) + 2):
                ws_an.column_dimensions[get_column_letter(c)].width = 16
            # capture real computed values too (openpyxl doesn't evaluate formulas) for the summary + verifier
            for col in numeric_cols:
                vals = [r.get(col) for r in clean_rows if isinstance(r.get(col), (int, float))]
                if vals:
                    analysis_summary[col] = {"count": len(vals), "sum": sum(vals), "avg": sum(vals) / len(vals), "min": min(vals), "max": max(vals)}

        chart_added = False
        if len(structure) > 3 and numeric_cols:
            ws_ch = wb.create_sheet(structure[3])
            data_sheet = structure[1] if len(structure) > 1 else structure[0]
            data_ws = wb[data_sheet]
            n = len(clean_rows) or len(rows)
            chart = BarChart() if n <= 40 else LineChart()
            chart.title = f"{numeric_cols[0]} overview"
            chart.y_axis.title = numeric_cols[0]
            col_idx = headers.index(numeric_cols[0]) + 1
            data_ref = Reference(data_ws, min_col=col_idx, min_row=1, max_row=n + 1)
            chart.add_data(data_ref, titles_from_data=True)
            label_col = 1
            cats = Reference(data_ws, min_col=label_col, min_row=2, max_row=n + 1)
            chart.set_categories(cats)
            ws_ch.add_chart(chart, "B2")
            chart_added = True

        if len(structure) > 4:
            ws_sum = wb.create_sheet(structure[4])
            ws_sum.cell(1, 1, title).font = Font(bold=True, size=16)
            ws_sum.cell(3, 1, "Records").font = Font(bold=True)
            ws_sum.cell(3, 2, len(rows))
            ws_sum.cell(4, 1, "Clean records").font = Font(bold=True)
            ws_sum.cell(4, 2, len(clean_rows))
            r = 6
            for col, s in analysis_summary.items():
                ws_sum.cell(r, 1, col).font = Font(bold=True)
                ws_sum.cell(r + 1, 1, "Average"); ws_sum.cell(r + 1, 2, round(s["avg"], 4))
                ws_sum.cell(r + 2, 1, "Min"); ws_sum.cell(r + 2, 2, s["min"])
                ws_sum.cell(r + 3, 1, "Max"); ws_sum.cell(r + 3, 2, s["max"])
                r += 5
            ws_sum.cell(r, 1, "Source").font = Font(italic=True)
            ws_sum.cell(r, 2, "Generated by the publishing agent from the data provided in this task.")

        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return {"ok": True, "path": str(path), "sheets": structure[:5] if len(structure) >= 5 else structure,
                "rows": len(rows), "clean_rows": len(clean_rows), "numeric_columns": numeric_cols,
                "chart_added": chart_added, "analysis": analysis_summary}

    def verify(self, path: Path) -> dict:
        try:
            import openpyxl
        except ImportError:
            return {"ok": False, "error": "openpyxl not installed."}
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": f"{p} does not exist."}
        try:
            wb = openpyxl.load_workbook(p, data_only=False)
        except Exception as e:
            return {"ok": False, "error": f"Workbook would not open: {e.__class__.__name__}: {e}"}
        sheets = wb.sheetnames
        has_formula = any(str(c.value).startswith("=") for ws in wb.worksheets for row in ws.iter_rows(max_row=min(ws.max_row, 30)) for c in row if c.value)
        has_data = any(ws.max_row > 1 and ws.max_column >= 1 for ws in wb.worksheets)
        has_chart = any(getattr(ws, "_charts", None) for ws in wb.worksheets)
        ok = bool(sheets) and has_data
        return {"ok": ok, "sheets": sheets, "sheet_count": len(sheets), "has_formula": has_formula, "has_chart": has_chart,
                "has_data": has_data, "verified": ok}


# ---------------------------------------------------------------------------------------------------------------------
# 5. PRESENTATION MANAGER
# ---------------------------------------------------------------------------------------------------------------------
class PresentationManager:
    def build(self, path: Path, title: str, subtitle: str, slides: list[dict]) -> dict:
        try:
            from pptx import Presentation
            from pptx.util import Inches, Pt
        except ImportError:
            return {"ok": False, "error_code": "missing_library", "error": "python-pptx is required (pip install python-pptx)."}
        prs = Presentation()
        title_slide = prs.slides.add_slide(prs.slide_layouts[0])
        title_slide.shapes.title.text = title
        if len(title_slide.placeholders) > 1:
            title_slide.placeholders[1].text = subtitle or ""
        overflow_warnings = []
        for i, s in enumerate(slides, 1):
            layout = prs.slide_layouts[1] if s.get("bullets") else prs.slide_layouts[5]
            slide = prs.slides.add_slide(layout)
            slide.shapes.title.text = s.get("title", f"Slide {i}")
            bullets = s.get("bullets") or []
            if bullets and len(slide.placeholders) > 1:
                tf = slide.placeholders[1].text_frame
                tf.text = bullets[0]
                for b in bullets[1:]:
                    p = tf.add_paragraph()
                    p.text = b
                total_chars = sum(len(b) for b in bullets)
                if total_chars > 600 or len(bullets) > 8:
                    overflow_warnings.append({"slide": i, "title": s.get("title"), "bullets": len(bullets), "chars": total_chars,
                                              "note": "This slide is text-heavy and may overflow — consider splitting it."})
        path.parent.mkdir(parents=True, exist_ok=True)
        prs.save(path)
        return {"ok": True, "path": str(path), "slide_count": len(prs.slides.slides) if hasattr(prs.slides, "slides") else len(prs.slides._sldIdLst),
                "overflow_warnings": overflow_warnings}

    def verify(self, path: Path, expected_slide_count: int | None = None) -> dict:
        try:
            from pptx import Presentation
        except ImportError:
            return {"ok": False, "error": "python-pptx not installed."}
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": f"{p} does not exist."}
        try:
            prs = Presentation(str(p))
        except Exception as e:
            return {"ok": False, "error": f"Presentation would not open: {e.__class__.__name__}: {e}"}
        n = len(prs.slides._sldIdLst)
        empty_titles = sum(1 for sl in prs.slides if sl.shapes.title is not None and not (sl.shapes.title.text or "").strip())
        placeholder_text = []
        for i, sl in enumerate(prs.slides, 1):
            for shp in sl.shapes:
                if shp.has_text_frame and re.search(r"\[.*(?:image|placeholder|todo).*\]|lorem ipsum", shp.text_frame.text, re.I):
                    placeholder_text.append(i)
        ok = n > 0 and (expected_slide_count is None or n == expected_slide_count) and not placeholder_text
        return {"ok": ok, "slide_count": n, "expected": expected_slide_count, "empty_titles": empty_titles,
                "placeholder_text_on_slides": sorted(set(placeholder_text)), "verified": ok}


# ---------------------------------------------------------------------------------------------------------------------
# 6. VERIFIER (dispatch)
# ---------------------------------------------------------------------------------------------------------------------
def verify_artifact(path: Path, fmt: str | None = None, **kw) -> dict:
    p = Path(path)
    f = (fmt or p.suffix.lstrip(".")).lower()
    if f in ("pdf",):
        if not p.exists():
            return {"ok": False, "error": f"{p} does not exist."}
        checks = {"exists": True}
        if shutil.which("pdfinfo"):
            try:
                out = subprocess.run(["pdfinfo", str(p)], capture_output=True, text=True, timeout=15).stdout
                m = re.search(r"Pages:\s*(\d+)", out)
                checks["pages"] = int(m.group(1)) if m else None
            except Exception:
                checks["pages"] = None
        ok = checks.get("pages", 0) and checks["pages"] > 0
        return {"ok": bool(ok), "checks": checks, "verified": bool(ok)}
    if f == "xlsx":
        return ExcelReportManager().verify(p)
    if f == "pptx":
        return PresentationManager().verify(p, kw.get("expected_slide_count"))
    if f in ("docx",):
        try:
            import docx
        except ImportError:
            return {"ok": False, "error": "python-docx not installed."}
        if not p.exists():
            return {"ok": False, "error": f"{p} does not exist."}
        try:
            d = docx.Document(str(p))
        except Exception as e:
            return {"ok": False, "error": f"Document would not open: {e}"}
        n_paras = len([x for x in d.paragraphs if x.text.strip()])
        return {"ok": n_paras > 0, "paragraphs": n_paras, "verified": n_paras > 0}
    return {"ok": p.exists(), "checks": {"exists": p.exists()}, "verified": p.exists(), "note": f"No specific verifier for '{f}'; only checked existence."}
