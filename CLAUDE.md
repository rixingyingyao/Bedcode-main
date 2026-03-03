# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BedCode is a remote control system for Claude Code on Windows via Telegram Bot + Web API + Mobile App. It allows sending text/images/voice/files to Claude Code terminals, taking non-intrusive screenshots, monitoring Claude's thinking/idle state, and managing multiple Claude windows — all from Telegram, a web PWA, or a React Native mobile client.

**Platform**: Windows-only (Win32 APIs, Git Bash, pywinauto UIA)
**Language**: Python 3.10+ (backend), TypeScript/React Native (mobile)

## Running

```bash
# Backend — starts Telegram bot + FastAPI server on port 8080
pip install -r requirements.txt
cp .env.example .env   # then fill in TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS
python bot.py

# Mobile client (Expo)
cd mobile && npm install && npx expo start --tunnel

# Web UI is served automatically by FastAPI at http://localhost:8080/
```

Required `.env` variables: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`. Optional: `PROXY_URL` (defaults to `http://127.0.0.1:7897` for China mainland), `WORK_DIR`, `GIT_BASH_PATH`, `SCREENSHOT_DELAY`, `SHELL_TIMEOUT`, `BEDCODE_API_TOKEN` (auto-generated if empty).

No test suite or linter is configured.

## Architecture

### Startup flow (`bot.py:run_all`)
1. Scan for Claude Code terminal windows via pywinauto
2. Build and start Telegram bot (non-blocking polling)
3. Start passive monitor (background Claude state detection)
4. Start FastAPI + uvicorn (port 8080, serves REST API + WebSocket + Web UI)
5. Optionally start Cloudflare Tunnel for remote access

### Core modules

| Module | Role |
|---|---|
| `bot.py` | Orchestrator — wires everything together, entry point `main()` |
| `config.py` | All env vars, logging, global `state` dict (shared mutable state) |
| `handlers.py` | Telegram command/message/callback handlers (~1300 lines, one handler per command) |
| `monitor.py` | Polling loop (3-4s) — detects state changes, processes message queue, forwards results |
| `claude_detect.py` | Window scanning (`find_claude_windows`), state detection via title spinner chars, UIA terminal text reading |
| `win32_api.py` | Low-level Win32: `PrintWindow` screenshots, `SendInput` key injection, clipboard ops |
| `stream_mode.py` | `claude -p --output-format stream-json` subprocess management, real-time JSON event parsing |
| `core/events.py` | `EventBus` — async queue-based pub/sub broadcasting events to WebSocket clients |
| `api/app.py` | FastAPI setup, CORS, Bearer token auth middleware |
| `api/routes.py` | REST endpoints under `/api/*` |
| `api/ws.py` | WebSocket handler at `/ws` |
| `utils.py` | Text splitting, file saving, template/panel/alias/state persistence |

### Key patterns

**Shared mutable state**: A global `state` dict in `config.py` holds all runtime state (target window handle, message queue, monitor tasks, labels, etc.). All modules import and mutate it directly.

**Claude state detection**: Window title's first character is checked — Braille spinner chars (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏) = "thinking", idle markers (✳·•∙) = "idle". The monitor polls this every 3-4 seconds.

**Message queue**: When Claude is thinking, incoming messages are queued (`collections.deque`, max 50). On thinking→idle transition, queued messages are injected sequentially.

**EventBus** (`core/events.py`): Monitor emits events (screenshot, completion, prompt) → WebSocket subscribers receive them in real-time.

**Interactive prompt detection**: `monitor.py:_detect_interactive_prompt` scans terminal text for y/n, ❯, ◯/◉, ☐/☑ patterns and generates inline keyboard buttons in Telegram.

**Screenshot fallback chain** (`win32_api.py:capture_window_screenshot`):
1. `PrintWindow(PW_RENDERFULLCONTENT)` — preferred, captures off-screen content
2. `PrintWindow(0)` — simpler mode fallback
3. `BitBlt` — direct DC copy
4. `ImageGrab.grab(bbox=...)` — screen-region capture (requires window visible)
5. Black-image detection (`_is_mostly_black`) triggers step 4 automatically
6. `activate_first=True` parameter activates the window before capture — needed for Windows Terminal tabs that share one container window

**Window detection** (`claude_detect.py`): Terminal windows (`CASCADIA_HOSTING_WINDOW_CLASS`, `ConsoleWindowClass`, `mintty`) are identified by title-character heuristics. Stale handles are filtered via `win32gui.IsWindow()`. Results are sorted: Claude windows before Windsurf, idle before thinking.

### Dual-channel input

Both Telegram handlers and REST API endpoints write to the same shared `state` and inject text into the same target Claude window. The Web UI connects via WebSocket for real-time updates.

### Persistent state files

`window_labels.json`, `state.json`, `templates.json`, `panel.json`, `aliases.json`, `recent_dirs.json` — all in project root, loaded at startup and saved on changes.

## Mobile app (`mobile/`)

Expo Router app with Zustand state management. Key structure:
- `app/` — Expo Router pages (login, chat, settings, windows)
- `components/` — TG-style chat UI components
- `hooks/` — `useApi`, `useWebSocket`, `useAutoMonitor`
- `stores/` — Zustand `chatStore`

## Important notes for development

- All code comments and UI text are in Chinese
- `.windsurfrules` contains CodeChat binary integration rules for Windsurf IDE — not relevant to Claude Code
- The `codechat/` directory contains third-party binary executables, do not modify
- Win32 API calls in `win32_api.py` use raw ctypes — handle with care, incorrect struct definitions can crash the process
- Windows Terminal tabs share a single HWND container; `capture_window_screenshot(handle, activate_first=True)` is needed for per-tab screenshots (used in `/windows`), while passive monitoring uses `activate_first=False` to avoid disrupting Claude
- `handlers.py` is the largest file; each Telegram command is a separate `cmd_*` async function
- Auth is enforced at two levels: Telegram user ID whitelist (`ALLOWED_USER_IDS`) and Bearer token for REST/WebSocket API
