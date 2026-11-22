"""Claude Code hook handler: turns hook events into one small state file per session.

Invoked by Claude Code (exec form, async) as:
    pythonw.exe hook.py --session-overlay --agent claude --state-dir <dir>

Hook JSON arrives on stdin. This script is stdlib-only, writes nothing to
stdout and always exits 0, so it can never block or break a Claude session.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Captured first so out-of-order async hooks can be detected.
_SEQ = time.time_ns()

MARKER = "--session-overlay"

ATTENTION_NOTIFICATIONS = {
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
}
ANSWERED_NOTIFICATIONS = {"idle_prompt"}

DELETE = "__delete__"
AGENT_IMAGE_PREFIXES = ("claude", "node", "bun")


def status_for_event(payload: dict) -> str | None:
    """Map a hook payload to running/attention/answered/error, DELETE, or None (ignore)."""
    event = payload.get("hook_event_name", "")
    if event in ("UserPromptSubmit", "PostToolUse"):
        return "running"
    if event == "PermissionRequest":
        return "attention"
    if event == "PreToolUse":
        # Registered with matcher AskUserQuestion only; guard anyway.
        return "attention" if payload.get("tool_name") == "AskUserQuestion" else None
    if event == "Notification":
        kind = payload.get("notification_type", "")
        if kind in ATTENTION_NOTIFICATIONS:
            return "attention"
        if kind in ANSWERED_NOTIFICATIONS:
            return "answered"
        return None
    if event == "SessionStart":
        # Auto-compaction can fire SessionStart mid-turn; don't flip that to green.
        return None if payload.get("source") == "compact" else "answered"
    if event == "Stop":
        return "answered"
    if event == "StopFailure":
        return "error"
    if event == "SessionEnd":
        return DELETE
    return None


def _truncate(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parent_image(pid: int) -> str:
    """Executable basename of the parent (the claude process) on Windows; '' elsewhere."""
    if os.name != "nt" or pid <= 0:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return os.path.basename(buffer.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _tracked_pid(parent_pid: int, parent_image: str) -> int:
    """Only track the parent if it's the agent itself; a shell parent (shell-form hooks) exits at once."""
    if parent_image and not parent_image.startswith(AGENT_IMAGE_PREFIXES):
        return 0
    return parent_pid


def _source_label(env: dict) -> str:
    entrypoint = env.get("CLAUDE_CODE_ENTRYPOINT", "").lower()
    term = env.get("TERM_PROGRAM", "").lower()
    if "vscode" in entrypoint or "vscode" in term:
        return "VS Code"
    if "desktop" in entrypoint:
        return "Desktop"
    if entrypoint == "cli" or term:
        return "CLI"
    return "Claude Code"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # The overlay may be reading the file on Windows; retry briefly.
            time.sleep(0.02 * (attempt + 1))
    tmp.unlink(missing_ok=True)


def _safe_id(session_id: str) -> str:
    return "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")[:80]


def handle_event(
    payload: dict,
    state_dir: Path,
    agent: str,
    seq: int,
    parent_pid: int,
    env: dict,
    parent_image: str = "",
) -> Path | None:
    """Apply one hook event to the session's state file. Returns the file path touched."""
    session_id = _safe_id(str(payload.get("session_id", "")))
    status = status_for_event(payload)
    if not session_id or status is None:
        return None

    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{agent}-{session_id}.json"
    current = _read_json(path)
    if current.get("seq", 0) > seq:
        return None  # A newer event already landed (async hooks can finish out of order).

    if status == DELETE:
        path.unlink(missing_ok=True)
        return path

    state = dict(current)
    state.update(
        {
            "id": f"{agent}:{session_id}",
            "agent": agent,
            "session_id": session_id,
            "status": status,
            "last_event": payload.get("hook_event_name", ""),
            "seq": seq,
            "updated_at": seq / 1e9,
            "cwd": payload.get("cwd") or current.get("cwd", ""),
            "transcript_path": payload.get("transcript_path") or current.get("transcript_path", ""),
            "pid": _tracked_pid(parent_pid, parent_image),
            "process_image": parent_image or current.get("process_image", ""),
            "source": current.get("source") or _source_label(env),
        }
    )
    state.setdefault("started_at", seq / 1e9)
    if status != current.get("status") or "status_since" not in current:
        state["status_since"] = seq / 1e9  # When it started waiting on you / running this turn.
    if payload.get("session_title"):
        state["title"] = _truncate(payload["session_title"])
    if payload.get("hook_event_name") == "UserPromptSubmit" and not current.get("first_prompt"):
        state["first_prompt"] = _truncate(payload.get("prompt", ""))
    if status == "attention":
        state["attention_reason"] = _truncate(
            payload.get("message") or payload.get("tool_name") or payload.get("notification_type") or "", 80
        )
    else:
        state.pop("attention_reason", None)

    _atomic_write(path, state)
    return path


def _parse_args(argv: list[str]) -> tuple[str, Path | None]:
    agent, state_dir = "claude", None
    for index, arg in enumerate(argv):
        if arg == "--agent" and index + 1 < len(argv):
            agent = argv[index + 1]
        elif arg == "--state-dir" and index + 1 < len(argv):
            state_dir = Path(argv[index + 1])
    return agent, state_dir


def _log_error(state_dir: Path | None, message: str) -> None:
    try:
        log_dir = state_dir.parent if state_dir else Path.home()
        log_file = log_dir / "hook_errors.log"
        if log_file.exists() and log_file.stat().st_size > 512_000:
            log_file.unlink()
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def main(argv: list[str]) -> int:
    agent, state_dir = _parse_args(argv)
    try:
        if state_dir is None or sys.stdin is None:
            return 0
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        parent_pid = os.getppid()
        handle_event(payload, state_dir, agent, _SEQ, parent_pid, dict(os.environ), _parent_image(parent_pid))
    except Exception as error:  # Never surface errors to Claude Code.
        _log_error(state_dir, f"{type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
