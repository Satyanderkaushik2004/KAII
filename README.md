# KAIi

KAIi is a modular AI-assisted desktop development environment that combines
coding assistance, provider routing, tool execution and workspace automation
in a single local Flask app with a single-page frontend.

## Overview

KAIi runs entirely on your own machine. It gives you a Code Editor mode, a
General Assistant mode, and an autonomous Agent mode, all backed by either a
cloud LLM provider (via [OpenRouter](https://openrouter.ai), or direct
OpenAI/Anthropic/Gemini keys) or a local model served by
[Ollama](https://ollama.com). Agent mode can read and write files, run
commands, browse the web, and drive a small set of "computer tools" against a
workspace folder you control.

## Features

- **Code Editor, General Assistant, and Agent modes**, sharing one chat
  pipeline and one tool system.
- **Multi-provider routing** — OpenRouter, OpenAI, Anthropic, Gemini,
  HuggingFace, and local Ollama, connected independently through Extensions
  (`provider_credentials.py` / `provider_router.py` / `provider_runtime.py`).
- **Agent Mode** with configurable autonomy (safe / trusted / full), a
  step budget, and file/command/web tools (`computer_tools.py`,
  `computer_batch.py`, `web_tools.py`).
- **OAuth connectors** for Google Drive/Gmail/Calendar, Dropbox, Microsoft,
  Canva, and Pinterest (`oauth_manager.py`, `oauth_tools.py`).
- **Publishing pipeline** that can turn a conversation into a LaTeX paper
  (IEEE/ACM/Springer/Elsevier templates), an Excel report, or a PowerPoint
  deck (`publishing_agent.py`, `publishing_sources.py`, `publishing_tools.py`).
- **Kaii** — an optional voice layer (browser Web Speech API) with
  notifications, communication modes, and "where were we?" context recall
  (`kaii.py`); see `KAII_IMPLEMENTATION_REPORT.md` for details.
- **Local dev-server helper** for previewing static sites you're editing in
  the workspace (`devserver.py`).

## Architecture

A single Flask app (`app.py`) serves the frontend (`index.html`, a
self-contained single-page app) and exposes ~140 JSON/SSE API routes. Chat
requests are routed through `provider_router.py` to whichever provider is
configured; Agent Mode additionally drives tool calls through
`agent_core.py` and `computer_tools.py`. All persistent data — provider
credentials, OAuth tokens, conversation history, your profile photo,
settings, and notifications — lives under a local `state/` directory that is
created automatically on first run and is never committed to version
control (see **Security** below).

## Tech Stack

- **Backend:** Python, Flask
- **Frontend:** a single static `index.html` (no build step)
- **Local model runtime:** [Ollama](https://ollama.com)
- **Optional libraries:** `psutil` (dock CPU/RAM), `openpyxl` / `python-pptx`
  / `python-docx` (publishing pipeline), `playwright` (render checks in
  `check_website`)

## Project Structure

```
app.py                    Flask app, routes, chat/agent orchestration
kaii.py                   Voice/notification/context layer
agent_core.py             Agent-mode tool loop
computer_tools.py         File/command/system tools for Agent Mode
computer_batch.py         Batch file operations
web_tools.py              Web search / browsing tools
site_tools.py             Static-site helpers
devserver.py              Local dev-server for previewing sites
extensions.py             "Ask a second AI" connectors (ChatGPT/Claude/Gemini)
provider_credentials.py   Multi-provider credential store
provider_router.py        Routes a request to the right provider
provider_runtime.py       Per-provider request/response handling
oauth_manager.py          OAuth app config + token storage/refresh
oauth_tools.py            Tools built on top of connected OAuth accounts
publishing_agent.py       Detects and drives paper/report/deck generation
publishing_sources.py     Source/citation handling for papers
publishing_tools.py       Tool-facing entry points for publishing
image_gen.py              Image-generation provider integration
tasks.py                  Persistent record of long-running agent jobs
index.html                Frontend (single page, no build step)
test_publishing.py        Tests for the publishing subsystem
*.cls                     LaTeX class files used by the publishing pipeline
```

## Setup

```bash
git clone <this-repo>
cd kaii_project
pip install -r requirements.txt
python app.py
```

The app starts at `http://127.0.0.1:8123`. On first run it creates a local
`state/` directory (conversation history, settings, credentials) — nothing
is pre-populated, and nothing in `state/` is committed to this repository.

Optional:
- Install [Ollama](https://ollama.com) and pull a model to use KAIi fully
  offline/local.
- Install `playwright` and run `playwright install chromium` to enable
  render checks in the site-checking tools.
- A working LaTeX distribution (`latexmk`/`pdflatex`) is needed for the
  LaTeX-paper publishing tests/features.

## Configuration

All providers can be connected from the app itself (Extensions →
`<service>` → Configure) — this is the primary, recommended way to set
things up, and it's what writes to `state/provider_credentials.json` /
`state/oauth_apps.json` at runtime.

If you'd rather configure OAuth apps via environment variables instead,
copy `.env.example` to `.env` and fill in your own values:

```bash
cp .env.example .env
```

For the legacy single-provider chat key, copy `apikey.example.json` to
`apikey.json` and fill in one provider — this is only used the very first
time the app runs, to migrate you into the multi-provider store; it is not
required.

## Provider Configuration

Supported chat/coding providers: OpenRouter, OpenAI, Anthropic, Gemini,
HuggingFace, and local Ollama. Supported OAuth connectors: Google
(Drive/Gmail/Calendar), Dropbox, Microsoft (OneDrive/Graph), Canva, and
Pinterest. Each provider/connector can be enabled independently; connecting
one never disconnects another.

## Security

- No API credentials are included in this repository.
- Local credentials should be supplied through the Extensions UI or, if you
  prefer, environment variables (`.env`, gitignored).
- Runtime state — conversation history, your profile photo, settings,
  provider credentials, and OAuth tokens — is excluded from version control
  via `.gitignore` and is created locally on first launch.
- OAuth tokens and any other user-specific state must never be committed.
- Example configuration files (`.env.example`, `apikey.example.json`)
  contain placeholders only.
- This app is designed to run locally and exposes its API on
  `127.0.0.1` by default; it has not been hardened for exposure on a public
  network or multi-user use.

## Limitations

- Single-user, local-first design — no authentication/authorization layer,
  since it's meant to run on your own machine.
- Agent Mode's autonomy levels reduce but do not eliminate the risk of an
  unwanted file/command action; review what "trusted"/"full" autonomy does
  in `computer_tools.py` before enabling them.
- See `KAII_IMPLEMENTATION_REPORT.md` for the voice layer's specific
  known limitations (no wake word, no cross-session memory yet, etc.).

## Future Improvements

- Cross-session project memory for Kaii's context recall.
- Wake-word support for voice activation.
- Optional authentication for exposing the app beyond localhost.

## License

MIT — see [LICENSE](LICENSE). 
