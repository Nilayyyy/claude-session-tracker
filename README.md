# Session Overlay

A small always-on-top pill at the top centre of your screen that expands into a detailed list when you hover it. It lists every running AI coding session, each with a coloured status dot, so you don't have to switch windows to check on them.

| Dot | Meaning | Triggered by |
|---|---|---|
| 🟡 yellow (pulsing) | **Needs you**: permission prompt, a question from Claude, or input requested | `PermissionRequest`, `PreToolUse(AskUserQuestion)`, `Notification` (permission / elicitation / agent_needs_input) |
| ⚪ grey | **Running** | `UserPromptSubmit`, `PostToolUse` |
| ◯ grey ring | Running, but no activity for `STALE_RUNNING_MINUTES` (probably interrupted) | transcript file not modified |
| 🟢 green | **Answered**: turn finished, waiting for your next prompt | `Stop`, `Notification(idle_prompt)`, `SessionStart` |
| 🔴 red | **Error** (rate limit, API error) | `StopFailure` |

A row disappears when the session ends (`SessionEnd`), when its Claude process exits, or after `MAX_AGE_HOURS`.

## What is tracked

- **Claude Code in VS Code, any terminal, and the Claude desktop app's Code tab.** Hooks fire in all three, whichever Claude account is signed in. Every config dir listed in `CLAUDE_CONFIG_DIRS` is patched.
- **Cowork (best-effort).** Local Cowork tasks run inside a VM, so hooks can't report to Windows. The overlay instead reads the desktop app's local session files (`%APPDATA%\Claude\local-agent-mode-sessions\**\local_*.json`, plus the MSIX path):
  - It shows grey if the task was active in the last `COWORK_ACTIVE_SECONDS`, and green otherwise.
  - It **cannot show yellow** for Cowork.
  - The file format is undocumented and may change.
  - Cowork tasks that run in the cloud are not visible on this PC at all.

No network calls are made, and no model is called. All data stays in local files.

## Requirements

- Windows 10/11
- Python 3.10+ from python.org. It includes tkinter and `pythonw.exe`. The Microsoft Store Python alias is not supported for hooks.
- A recent Claude Code version with exec-form hook support (`args`). For older versions, see `--shell-form` below.

No pip packages are needed (`requirements.txt` is intentionally empty).

## Setup

```powershell
cd $HOME\Documents\projects\session-overlay
copy .env.example .env          # optional: only if you want to change defaults
python run.py --demo            # 1. check the widget looks right (sample data); right-click > Quit
python run.py --install-hooks   # 2. add hooks to %USERPROFILE%\.claude\settings.json (a backup is made)
pythonw run.py                  # 3. start the widget with no console window
python run.py --startup on      # 4. start it at login (HKCU Run entry, uses pythonw, waits for the desktop)
```

Restart open Claude Code sessions (or VS Code) after installing hooks. You can check the installed hooks with `/hooks` inside Claude Code.

## Commands and overrides

| Command | Purpose |
|---|---|
| `python run.py` | Start the overlay (`pythonw` hides the console) |
| `python run.py --demo` | Smoke test with sample sessions; touches no files |
| `python run.py --install-hooks [--config-dir DIR ...] [--shell-form]` | Install hooks (idempotent; backs up `settings.json` first) |
| `python run.py --uninstall-hooks [--config-dir DIR ...]` | Remove only this overlay's hooks |
| `python run.py --diagnose` | Show hook status, tracked sessions, and Cowork files found (keys only, no content) |
| `python run.py --startup on\|off` | Add or remove the `HKCU\...\Run` login entry |
| `--corner top-center\|top-right\|top-left\|bottom-right\|bottom-left` | Position for this run (overrides a saved drag position) |
| `--data-dir PATH` | Override where session state is read and written |

`--shell-form` installs a PowerShell-based hook instead of exec form. It is **experimental**. Use it only if your Claude Code version rejects `args`.

## Using the widget

- **Click a row** to jump to that chat. The overlay brings the session's window to the front (VS Code, terminal or Claude desktop). For sessions in the VS Code Claude extension it also opens the exact chat tab through the Claude extension (`vscode://anthropic.claude-code/open?session=<id>`). That link is only sent once the right VS Code window is confirmed in front, so it never opens the chat in the wrong workspace. For terminals, only the window is focused, not the tab. Cowork rows aren't clickable.
- **Drag** it anywhere (move more than 4 px; a shorter press counts as a click). The position is remembered in `%LOCALAPPDATA%\session-overlay\position.json`.
- **Collapsed by default:** a small pill with "Sessions" and the coloured counts, flush with the top edge. A yellow count gently pulses when a session needs you.
- **Hover** to reveal the full list: the panel fades in over the pill, and fades out shortly after the pointer leaves.
- **Look:** macOS-style dark panel with native rounded corners (Windows 11; a plain rectangle on Windows 10), Segoe UI Variable type, rounded row highlights with a `›` on clickable rows, and a details card that fades in below the panel when you rest on a row.
- **Running rows say what they're doing** on the right, e.g. "Editing store.py", "Run tests" or "Agent: Review PR". That's the newest tool call still waiting for its result, read from the transcript; "Thinking…" when no tool is running.
- **Alerts:** when a session needs you, finishes (after running at least 30 s) or fails, a small banner drops in under the pill for a few seconds: "● Gateway retries  needs you". Click it to jump to the chat. Hovering it keeps it up; right-click dismisses it. "Needs you" also plays the Windows notification sound. No banner when that chat's window is already in front (and no other tracked chat shares that window), or while the panel is open.
- **Right-click the header or background** for: Snap to position, Keep expanded, Clear finished, Quit.
- **Right-click a row** for: Open chat, Copy resume command (`claude --resume <id>`), Open folder, Hide until it changes.
- **Double-click the header** to toggle Keep expanded.
- **Hover a row** for details, showing only what's known:
  - status and for how long ("Waiting on you · 4 min", "Running · 6 min", "Done · 12 min ago")
  - what it's doing, or what it's waiting on
  - context size and how many subagents are working
  - Claude's last reply, trimmed (for finished or waiting chats)
  - the folder
- Row name = the chat's title as Claude Code shows it: your `/rename` title, else the auto-generated title, else the first prompt, else the folder name. The titles are read from the tail of the session transcript, since renaming fires no hook. The folder is in the hover tooltip.
- The widget never takes keyboard focus and doesn't appear in the taskbar or Alt-Tab.

## Configuration (`.env`)

See `.env.example`. Everything has a default.

| Setting | Default | Notes |
|---|---|---|
| `DATA_DIR` | `%LOCALAPPDATA%\session-overlay` | Re-run `--install-hooks` after changing it (the path is baked into the hook) |
| `CLAUDE_CONFIG_DIRS` | `%USERPROFILE%\.claude` | `;`-separated; add the dir of any other account/profile (`CLAUDE_CONFIG_DIR`) |
| `CORNER`, `MARGIN_PX`, `OPACITY`, `FONT_SIZE` | `top-center`, 16, 0.85, 9 | Look and placement (`top-center` ignores the margin and sits flush with the top) |
| `NAME_MAX_CHARS`, `MAX_ROWS` | 42, 12 | Row width and count |
| `BLINK_ATTENTION` | true | Pulse ("breathe") yellow dots; false keeps them static |
| `ALERTS` | true | Banner under the pill on needs you / done / error |
| `ALERT_SOUND` | `attention` | `attention`, `all` or `off`: which alerts also play the Windows notification sound |
| `ALERT_MIN_RUN_SECONDS` | 30 | Only announce "done" for turns that ran at least this long (quick replies you're watching anyway) |
| `POLL_MS` | 1000 | Refresh interval |
| `STALE_RUNNING_MINUTES` | 15 | Grey ring after this long with no activity |
| `MAX_AGE_HOURS` | 24 | Forget sessions older than this |
| `COWORK_ENABLED`, `COWORK_ACTIVE_SECONDS`, `COWORK_SHOW_HOURS`, `COWORK_DIRS` | true, 45, 3, auto | Cowork adapter |

## How it works

```text
Claude Code ──hook (async, pythonw, exec form)──> src/hook.py ──> %LOCALAPPDATA%\session-overlay\sessions\claude-<id>.json
Claude desktop ──local_*.json (Cowork)───────────────────────────────────────────────┐
                                                                                       ▼
                                  run.py ──> src/store.py + src/cowork.py ──> src/overlay.py (tkinter)
```

Each part:

- **`src/hook.py`**
  - Uses the standard library only.
  - Never writes to stdout and always exits 0.
  - Writes atomically (temp file + `os.replace`, retried while the overlay is reading).
  - Async hooks can finish out of order, so each write carries a nanosecond `seq`, and an older event never overwrites a newer one.
  - Errors go to `%LOCALAPPDATA%\session-overlay\hook_errors.log`.
- **`src/store.py`**
  - Loads state files and skips half-written ones.
  - Prunes sessions whose Claude process has exited. It checks the PID with `OpenProcess`/`GetExitCodeProcess` and compares the executable name to guard against PID reuse.
  - Builds sorted display rows: needs you → error → running → stale → answered.
- **`src/focus.py`**
  - Finds a session's window by walking up the Claude process's parent chain to the first ancestor with a visible window. If that ancestor owns several windows (VS Code), it picks the one whose title names the session's folder, and does nothing if none does.
- **`src/installer.py`**
  - Merges hooks into `settings.json` without touching your own hooks.
  - Our entries carry a `--session-overlay` marker, so uninstall removes only those.
  - Aborts if `settings.json` isn't valid JSON.
- **`src/overlay.py`**
  - Plain tkinter with Win32 styles: DPI-aware, tool window, no-activate, topmost re-asserted every 5 s, DWM rounded corners.
  - Four persistent windows: the pill, the panel, the hover card and the alert banner. Hidden ones stay in place at opacity 0 and click-through, so showing or hiding is only an opacity fade. Windows are never resized mid-animation, because a Tk window resized every frame tears and flashes black.
  - The panel's header is identical to the pill and centred on the same pixel, so the fade reads as the pill opening up.
  - Positioned in the work area, above the taskbar.

## Failure behaviour

| Situation | What happens |
|---|---|
| Hook gets bad or empty input, or can't write | Exits 0 silently (logged to `hook_errors.log`); Claude is never affected |
| `settings.json` is invalid JSON | `--install-hooks` aborts without writing and tells you which file |
| A state file is mid-write or corrupt | Skipped this poll, retried on the next |
| Refresh throws | Error logged to `logs\overlay_*.log`; the widget keeps the last view |
| Cowork files missing or changed format | No Cowork rows; everything else works |
| Second overlay started | Exits immediately (named mutex) |
| Windows stacks the overlay below normal windows (seen when launched during sign-in) | `--at-login` waits for the taskbar plus 8 s before creating windows; a 5 s self-check also detects it and re-shows the windows |

## Tests

```powershell
python -m compileall -q src
python -m unittest discover -s tests -t .
```

The tests cover:

- The event → status table, lifecycle and out-of-order events.
- Malformed stdin and an unwritable directory, by running `hook.py` as a real subprocess.
- Installer merge, idempotence, backup, invalid JSON, and uninstall that touches only our hooks.
- Pruning, row sorting and naming, and stale detection.
- The Cowork parser, using fixture files.
- Corner, top-centre and multi-monitor placement maths.
- Title priority (`/rename` > auto title > first prompt) and reading titles from the transcript tail.
- Live details from the transcript tail: the open tool call as the activity line, context size, open subagents, the last reply, interrupts, and the hover-card text.
- Click-to-open window matching: the parent walk stops at `explorer.exe`, VS Code windows are picked by folder, and the chat-tab link is sent only for extension-hosted sessions.

No live Claude sessions are needed.

**Manual check after installing:**

- Start a session in VS Code and in a terminal; each should appear grey while working and green when done.
- Trigger a permission prompt (yellow, blinking). Approve it: the row goes grey when the tool finishes.
- Press Esc mid-turn: the row turns green within a second or two (the transcript records the interrupt).
- Close the session: the row disappears.

## Known limitations

- **No "permission approved" event exists.** A row stays yellow until the approved tool *finishes*. For a long-running command this can take a while.
- **Interrupting with Esc doesn't fire `Stop`.** The overlay spots the interrupt marker in the transcript instead, and that format is undocumented. If it changes, the row falls back to turning green about 60 s later (`idle_prompt`).
- **Activity, context size and the reply preview come from the transcript**, which is an undocumented format. If it changes, those lines just disappear; status still comes from hooks.
- **Cowork has no yellow state, and cloud Cowork is invisible locally** (see above).
- **Already-open sessions don't pick up newly installed hooks** until they restart.
- **With `--shell-form`, the Claude process can't be identified.** Closed sessions linger until `MAX_AGE_HOURS` or "Clear finished".
- **Two hooks firing within the same instant can race.** The later one wins, and the next event corrects it.
