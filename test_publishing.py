"""Tests for the publishing subsystem (publishing_agent / publishing_sources / publishing_tools).

Run from the project root:  pytest tests/test_publishing.py -v

These are real, not smoke tests: the LaTeX tests actually shell out to latexmk/pdflatex and check a real
PDF; the Excel tests actually reopen the workbook; the tool-dispatch tests run through the genuine
ActionContext/_confirm_and_run path used by the live app, not a mock. LaTeX/Excel/pptx tests skip
gracefully (not fail) when the corresponding binary/library isn't installed on the machine running them.
"""
from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import publishing_agent as pa
import publishing_sources as ps

HAVE_LATEX = shutil.which("latexmk") is not None or shutil.which("pdflatex") is not None
HAVE_PDFINFO = shutil.which("pdfinfo") is not None
needs_latex = pytest.mark.skipif(not HAVE_LATEX, reason="no LaTeX distribution installed")
needs_pdfinfo = pytest.mark.skipif(not HAVE_PDFINFO, reason="pdfinfo not installed")


# ----------------------------------------------------------------------------------------------------------------
# ArtifactDetector
# ----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected_type,expected_venue,expected_conv", [
    ("Make a research paper on AI-based traffic detection.", "research_paper", None, False),
    ("Make an IEEE paper on my project in Overleaf.", "conference_paper", "ieee", False),
    ("Create an Excel report from this dataset.", "excel_report", None, False),
    ("Make a PPT from this research paper.", "presentation", None, True),
    ("Create an ACM conference paper on federated learning", "conference_paper", "acm", False),
    ("write me a literature review on transformers", "literature_review", None, False),
])
def test_detector_classifies_correctly(text, expected_type, expected_venue, expected_conv):
    r = pa.ArtifactDetector.detect(text)
    assert r.is_publishing
    assert r.artifact_type == expected_type
    assert r.venue == expected_venue
    assert r.is_conversion == expected_conv


def test_detector_rejects_non_publishing_text():
    r = pa.ArtifactDetector.detect("what's the weather like today?")
    assert not r.is_publishing


def test_detector_flags_ambiguous_venue():
    r = pa.ArtifactDetector.detect("write a conference paper on graph neural networks")
    assert r.artifact_type == "conference_paper"
    assert r.venue is None
    assert r.ambiguous, "should ask which venue rather than silently pick one"


# ----------------------------------------------------------------------------------------------------------------
# SourceManager — the anti-fabrication guards
# ----------------------------------------------------------------------------------------------------------------
def test_source_manager_refuses_locatorless_sources(tmp_path):
    sm = ps.SourceManager(tmp_path / "sources.json")
    res = sm.add({"title": "Something I only remember"})
    assert not res["ok"]
    assert res["error_code"] == "no_locator"


def test_source_manager_rejects_malformed_doi(tmp_path):
    sm = ps.SourceManager(tmp_path / "sources.json")
    res = sm.add({"title": "X", "doi": "not-a-real-doi"})
    assert not res["ok"]
    assert res["error_code"] == "bad_doi"


def test_source_manager_deduplicates_by_url(tmp_path):
    sm = ps.SourceManager(tmp_path / "sources.json")
    r1 = sm.add({"title": "Paper A", "url": "https://arxiv.org/abs/1234.5678", "authors": ["A. Author"], "year": "2020"})
    r2 = sm.add({"title": "Paper A (slightly different casing)", "url": "https://arxiv.org/abs/1234.5678"})
    assert r1["ok"] and r2["ok"]
    assert r2.get("duplicate_of") == r1["source"]["key"]
    assert len(sm.sources) == 1


def test_bibtex_never_invents_missing_fields(tmp_path):
    sm = ps.SourceManager(tmp_path / "sources.json")
    r = sm.add({"title": "A Paper With No Venue", "url": "https://arxiv.org/abs/1999.00099"})
    bib = ps.sources_to_bib(sm.sources)
    assert "publisher" not in bib and "journal" not in bib and "booktitle" not in bib


# ----------------------------------------------------------------------------------------------------------------
# Bibliography validation — must actually catch broken references, not just pass everything
# ----------------------------------------------------------------------------------------------------------------
def test_validate_bibliography_catches_undefined_citation():
    bib = "@article{smith2020, author={Smith, J.}, title={T}, journal={J}, year={2020}}"
    tex = r"See \cite{smith2020} and \cite{doesnotexist}."
    res = ps.validate_bibliography({"main.tex": tex}, bib)
    assert not res["ok"]
    assert "doesnotexist" in res["undefined"]


def test_validate_bibliography_catches_duplicate_keys():
    bib = ("@article{a2020, author={A}, title={T1}, journal={J}, year={2020}}\n"
          "@article{a2020, author={A}, title={T2}, journal={J}, year={2020}}")
    res = ps.validate_bibliography({"main.tex": r"\cite{a2020}"}, bib)
    assert any(i["code"] == "duplicate_key" for i in res["issues"])


def test_validate_bibliography_catches_placeholder_content():
    bib = '@misc{x2021, title={lorem ipsum dolor sit amet}, year={2021}}'
    res = ps.validate_bibliography({"main.tex": r"\cite{x2021}"}, bib)
    assert not res["ok"]
    assert any(i["code"] == "placeholder_content" for i in res["issues"])


def test_validate_bibliography_accepts_a_clean_bib():
    bib = '@article{doe2021, author={Doe, Jane}, title={A Real Paper}, journal={A Journal}, year={2021}, doi={10.1000/xyz123}}'
    tex = "\\cite{doe2021}\n\\bibliographystyle{plain}\n\\bibliography{references}"
    res = ps.validate_bibliography({"main.tex": tex}, bib)
    assert res["ok"], res["issues"]
    assert res["errors"] == 0


# ----------------------------------------------------------------------------------------------------------------
# LaTeX pipeline — real compilation, all vendored venues
# ----------------------------------------------------------------------------------------------------------------
def _sample_content(cite_key: str | None = None) -> pa.PaperContent:
    intro = "This is the introduction."
    if cite_key:
        intro += f" Related work exists \\cite{{{cite_key}}}."
    return pa.PaperContent(title="A Sample Paper on Testing", authors=["Jane Doe", "John Smith"],
                           affiliations=["Example University"], abstract="This is a test abstract.",
                           keywords=["testing", "latex"],
                           sections={"introduction": intro, "conclusion": "This concludes the paper."},
                           tables=[{"caption": "A table", "rows": [["A", "B"], ["1", "2"]]}])


@needs_latex
@needs_pdfinfo
@pytest.mark.parametrize("venue", ["ieee", "acm", "springer", "elsevier", "generic", None])
def test_latex_project_compiles_and_verifies_for_every_venue(tmp_path, venue):
    root = tmp_path / f"proj_{venue}"
    mgr = pa.LatexProjectManager(root)
    mgr.scaffold(venue)
    mgr.write_main_tex(_sample_content(), venue)
    comp = mgr.compile_with_retry()
    assert comp["ok"], comp.get("diagnosis")
    ver = mgr.verify()
    assert ver["ok"]
    assert ver["pages"] and ver["pages"] >= 1


@needs_latex
@needs_pdfinfo
def test_latex_citations_survive_escaping_and_resolve(tmp_path):
    sm = ps.SourceManager(tmp_path / "sources.json")
    r = sm.add({"title": "A Cited Work", "authors": ["A. Person"], "year": "2019", "url": "https://arxiv.org/abs/1901.00001"})
    key = r["source"]["key"]
    (tmp_path / "references.bib").write_text(ps.sources_to_bib(sm.sources), encoding="utf-8")

    mgr = pa.LatexProjectManager(tmp_path)
    mgr.scaffold("ieee")
    mgr.write_main_tex(_sample_content(cite_key=key), "ieee")
    comp = mgr.compile_with_retry()
    assert comp["ok"], comp.get("diagnosis")

    main_tex = (tmp_path / "main.tex").read_text(encoding="utf-8")
    assert f"\\cite{{{key}}}" in main_tex, "citation command must survive the escaping pass intact"
    bibval = ps.validate_bibliography({"main.tex": main_tex}, (tmp_path / "references.bib").read_text(encoding="utf-8"))
    assert bibval["ok"]
    assert key not in bibval["undefined"]


@needs_latex
def test_latex_compile_reports_diagnosis_on_real_syntax_error(tmp_path):
    mgr = pa.LatexProjectManager(tmp_path)
    mgr.scaffold("generic")
    (tmp_path / "main.tex").write_text(r"\documentclass{article}\begin{document}\section{Unterminated" + "\n\\end{document}", encoding="utf-8")
    comp = mgr.compile_with_retry(max_attempts=3)
    assert not comp["ok"]
    assert comp["diagnosis"], "a genuine syntax error must produce a diagnosis, not silence"
    assert len(comp["attempts"]) == 1, "a non-auto-fixable error must not be blindly retried"


def test_latex_special_characters_are_escaped_not_broken(tmp_path):
    mgr = pa.LatexProjectManager(tmp_path)
    mgr.scaffold("generic")
    content = pa.PaperContent(title="T", authors=["A"], sections={"introduction": "50% of $x$ & y_1 values"})
    mgr.write_main_tex(content, "generic")
    tex = (tmp_path / "main.tex").read_text(encoding="utf-8")
    assert "50\\% of \\$x\\$ \\& y\\_1 values" in tex


# ----------------------------------------------------------------------------------------------------------------
# ExcelReportManager
# ----------------------------------------------------------------------------------------------------------------
def _have(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _have("openpyxl"), reason="openpyxl not installed")
def test_excel_report_builds_real_formulas_and_verifies(tmp_path):
    rows = [{"Region": "N", "Revenue": 100}, {"Region": "S", "Revenue": 200}, {"Region": "E", "Revenue": 300}]
    xm = pa.ExcelReportManager()
    res = xm.build(tmp_path / "r.xlsx", rows, title="T")
    assert res["ok"]
    assert res["chart_added"]
    ver = xm.verify(tmp_path / "r.xlsx")
    assert ver["ok"] and ver["has_formula"] and ver["has_chart"]


@pytest.mark.skipif(not _have("openpyxl"), reason="openpyxl not installed")
def test_excel_report_separates_dirty_rows(tmp_path):
    rows = [{"A": 1, "B": 2}, {"A": None, "B": None}, {"A": 3, "B": 4}]
    xm = pa.ExcelReportManager()
    res = xm.build(tmp_path / "r.xlsx", rows, title="T")
    assert res["rows"] == 3
    assert res["clean_rows"] == 2


# ----------------------------------------------------------------------------------------------------------------
# PresentationManager
# ----------------------------------------------------------------------------------------------------------------
@pytest.mark.skipif(not _have("pptx"), reason="python-pptx not installed")
def test_presentation_builds_and_verifies(tmp_path):
    slides = [{"title": "Problem", "bullets": ["A", "B"]}, {"title": "Solution", "bullets": ["C"]}]
    pm = pa.PresentationManager()
    res = pm.build(tmp_path / "d.pptx", "Title", "Sub", slides)
    assert res["ok"]
    ver = pm.verify(tmp_path / "d.pptx", expected_slide_count=res["slide_count"])
    assert ver["ok"]
    assert ver["slide_count"] == res["slide_count"]


@pytest.mark.skipif(not _have("pptx"), reason="python-pptx not installed")
def test_presentation_flags_overflow_slides(tmp_path):
    slides = [{"title": "Wall of text", "bullets": ["x" * 100] * 9}]
    pm = pa.PresentationManager()
    res = pm.build(tmp_path / "d.pptx", "T", "", slides)
    assert res["overflow_warnings"]


# ----------------------------------------------------------------------------------------------------------------
# Real tool dispatch — through the app's actual ActionContext/_confirm_and_run path, not a mock
# ----------------------------------------------------------------------------------------------------------------
@pytest.fixture
def app_env(tmp_path, monkeypatch):
    import app
    import computer_tools as ct
    ws = tmp_path / "ws"
    appdir = tmp_path / "appdir"
    downloads = tmp_path / "downloads"
    ws.mkdir()
    appdir.mkdir()
    downloads.mkdir()
    app.WORKSPACE_DIR = ws
    ct.configure(app_dir=appdir, workspace_getter=lambda: app.WORKSPACE_DIR)
    ct._SPECIAL_OVERRIDE["downloads"] = downloads
    ct.set_autonomy("full")
    ctx = ct.ActionContext(lambda e: None, threading.Event())
    yield app, ct, ctx
    ct.set_autonomy("safe")


def test_tool_registration_is_complete():
    import app
    names = {t["name"] for t in app.get_agent_tools()}
    for n in ("classify_publishing_request", "create_research_project", "compile_research_project",
             "register_source", "verify_source", "generate_bibliography", "validate_paper_bibliography",
             "create_excel_report", "create_presentation", "verify_publishing_artifact", "export_overleaf_zip"):
        assert n in names, f"{n} not registered as an agent tool"
    health = app.tool_health_report()
    assert health["ok"] and not health["broken"]


@needs_latex
@needs_pdfinfo
def test_full_dispatch_pipeline_through_real_action_context(app_env):
    app, ct, ctx = app_env
    r1 = ct.execute("create_research_project", {"destination": "workspace", "project_name": "paper",
                                                  "venue": "ieee", "title": "T", "sections": {"introduction": "Intro."}}, ctx)
    assert r1["ok"] and r1["verified"]

    r2 = ct.execute("compile_research_project", {"project": r1["path"]}, ctx)
    assert r2["ok"] and r2["verified"]

    r3 = ct.execute("register_source", {"project": r1["path"], "title": "A Source",
                                        "url": "https://arxiv.org/abs/2003.00002", "year": "2020"}, ctx)
    assert r3["ok"]
    key = r3["source"]["key"]

    # recreate with a citation to the registered key (also exercises the exists-conflict path under FULL autonomy)
    r4 = ct.execute("create_research_project", {"destination": "workspace", "project_name": "paper", "venue": "ieee",
                                                 "title": "T", "sections": {"introduction": f"See \\cite{{{key}}}."}}, ctx)
    assert r4["ok"]

    r5 = ct.execute("generate_bibliography", {"project": r1["path"]}, ctx)
    assert r5["ok"] and r5["entries"] == 1

    r6 = ct.execute("compile_research_project", {"project": r1["path"]}, ctx)
    assert r6["ok"] and r6["verified"]

    r7 = ct.execute("validate_paper_bibliography", {"project": r1["path"], "venue": "ieee"}, ctx)
    assert r7["ok"] and r7["errors"] == 0

    r8 = ct.execute("export_overleaf_zip", {"project": r1["path"]}, ctx)
    assert r8["ok"] and Path(r8["path"]).is_file()


def test_safe_autonomy_requires_real_confirmation_not_silent_bypass(app_env):
    app, ct, ctx = app_env
    ct.set_autonomy("safe")
    short_ctx = ct.ActionContext(lambda e: None, threading.Event(), wait_timeout=0.5)
    r = ct.execute("create_research_project", {"destination": "workspace", "project_name": "p", "title": "T"}, short_ctx)
    assert not r["ok"]
    assert r.get("declined") and r.get("timed_out"), "SAFE mode must not silently write files without a real confirmation"
    ct.set_autonomy("full")


def test_offline_mode_blocks_verify_source(app_env):
    app, ct, ctx = app_env
    r1 = ct.execute("create_research_project", {"destination": "workspace", "project_name": "p", "title": "T"}, ctx)
    ct.execute("register_source", {"project": r1["path"], "title": "X", "url": "https://arxiv.org/abs/2004.00003"}, ctx)
    ct.set_network_allowed(False)
    try:
        r = ct.execute("verify_source", {"project": r1["path"], "key": "anything"}, ctx)
        assert not r["ok"] and r["error_code"] == "offline_mode"
    finally:
        ct.set_network_allowed(True)


def test_register_source_refuses_at_tool_layer_too(app_env):
    app, ct, ctx = app_env
    r1 = ct.execute("create_research_project", {"destination": "workspace", "project_name": "p", "title": "T"}, ctx)
    r = ct.execute("register_source", {"project": r1["path"], "title": "No locator here"}, ctx)
    assert not r["ok"] and r["error_code"] == "no_locator"
