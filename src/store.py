"""Reads hook state files, prunes dead sessions, and turns sessions into display rows."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

STATUS_ORDER = {"attention": 0, "error": 1, "running": 2, "stale": 3, "answered": 4}


@dataclass(frozen=True)
class Row:
    id: str
    status: str
    name: str
    source: str
    age: str
    tooltip: str
    activity: str = ""  # Running rows: what it's doing right now.


def process_alive(pid: int, expected_image: str = "") -> bool | None:
    """True/False when liveness is known, None when it can't be determined."""
    if not pid or pid <= 0:
        return None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        return False if error == 87 else None  # 87 = no such process; 5 = access denied
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
            return False  # 259 = STILL_ACTIVE
        if expected_image:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                # PID reuse guard: a different executable means the session is gone.
                return os.path.basename(buffer.value).lower() == expected_image
        return True
    finally:
        kernel32.CloseHandle(handle)


def load_hook_sessions(
    sessions_dir: Path,
    max_age_hours: float,
    is_alive: Callable[[int, str], bool | None] = process_alive,
    now: float | None = None,
) -> list[dict]:
    """Load session files; delete ones whose process died or that are too old."""
    now = now or time.time()
    sessions: list[dict] = []
    if not sessions_dir.is_dir():
        return sessions
    for path in sessions_dir.glob("*.json"):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # Mid-write or corrupt; try again next poll.
        if not isinstance(session, dict) or "status" not in session:
            continue
        too_old = now - float(session.get("updated_at", 0)) > max_age_hours * 3600
        dead = is_alive(int(session.get("pid", 0) or 0), session.get("process_image", "")) is False
        if too_old or dead:
            try:
                path.unlink()
                logger.info("Pruned session %s (%s)", session.get("id"), "dead process" if dead else "too old")
            except OSError:
                pass
            continue
        sessions.append(session)
    return sessions


TITLE_TAIL_BYTES = 256 * 1024
INTERRUPT_PREFIX = "[Request interrupted by user"
SUBAGENT_TOOLS = {"Task", "Agent"}
# transcript path -> ((mtime, size), info); remembers the last titles seen.
_info_cache: dict[str, tuple[tuple[float, int], dict]] = {}


def _short_path(value: object) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").split("/")[-1]


def describe_tool(name: str, tool_input: dict) -> str:
    """A short 'what it's doing' line for a tool call, e.g. 'Editing store.py'."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    target = _short_path(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path"))
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"Editing {target}" if target else "Editing files"
    if name == "Read":
        return f"Reading {target}" if target else "Reading files"
    if name in ("Grep", "Glob"):
        pattern = str(tool_input.get("pattern") or "")
        return f"Searching {pattern}" if pattern else "Searching files"
    if name in ("Bash", "PowerShell"):
        label = " ".join(str(tool_input.get("description") or tool_input.get("command") or "").split())
        return label or "Running a command"
    if name == "WebSearch":
        return "Searching the web"
    if name == "WebFetch":
        url = str(tool_input.get("url") or "")
        host = url.split("//", 1)[-1].split("/", 1)[0]
        return f"Reading {host}" if host else "Reading a web page"
    if name in SUBAGENT_TOOLS:
        label = str(tool_input.get("description") or "").strip()
        return f"Agent: {label}" if label else "Running an agent"
    if name == "AskUserQuestion":
        return "Asking you a question"
    if name in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        return "Updating its task list"
    if name.startswith("mcp__"):
        return f"Using {name.split('__')[-1].replace('_', ' ')}"
    return f"Using {name}"


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(block.get("text", "")) for block in content
                        if isinstance(block, dict) and block.get("type") == "text")
    return ""


def parse_transcript_tail(data: bytes) -> dict:
    """Pull titles and live details out of the tail of a Claude Code transcript (JSONL).

    Returns custom/ai titles plus: activity (the newest tool call still waiting for its
    result), subagents (open Task/Agent calls), context_tokens (prompt size of the last
    reply), last_text (Claude's last words) and interrupted (you pressed Esc).
    """
    info: dict = {}
    open_tools: dict[str, tuple[str, dict]] = {}
    for line in data.splitlines():
        if not (b'-title"' in line or b'"type":"assistant"' in line or b'"type":"user"' in line):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        kind = entry.get("type")
        if kind == "custom-title" and entry.get("customTitle"):
            info["custom"] = str(entry["customTitle"])
        elif kind == "ai-title" and entry.get("aiTitle"):
            info["ai"] = str(entry["aiTitle"])
        if kind not in ("assistant", "user") or entry.get("isSidechain"):
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
        if kind == "assistant":
            info["interrupted"] = False
            usage = message.get("usage") or {}
            tokens = sum(int(usage.get(key) or 0) for key in
                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            if tokens:
                info["context_tokens"] = tokens
            for block in blocks:
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    info["last_text"] = " ".join(str(block["text"]).split())
                elif block.get("type") == "tool_use":
                    open_tools[str(block.get("id"))] = (str(block.get("name", "")), block.get("input") or {})
            continue
        # User entries: tool results close calls; an interrupt marker ends the turn.
        for block in blocks:
            if block.get("type") == "tool_result":
                open_tools.pop(str(block.get("tool_use_id")), None)
        text = _text_of(content).strip()
        if text.startswith(INTERRUPT_PREFIX):
            info["interrupted"] = True
            open_tools.clear()
        elif text:
            info["interrupted"] = False
    if open_tools:
        name, tool_input = list(open_tools.values())[-1]
        info["activity"] = describe_tool(name, tool_input)
    info["subagents"] = sum(1 for name, _ in open_tools.values() if name in SUBAGENT_TOOLS)
    return info


def transcript_info(transcript_path: str, tail_bytes: int = TITLE_TAIL_BYTES) -> dict:
    """Titles and live details from a transcript's tail, cached on (mtime, size).

    Claude Code re-appends `custom-title` / `ai-title` lines throughout the file, so the
    tail is enough; a tail with no title line keeps the previously found titles.
    """
    if not transcript_path:
        return {}
    try:
        stat = os.stat(transcript_path)
    except OSError:
        return _info_cache.get(transcript_path, ((0, 0), {}))[1]
    key = (stat.st_mtime, stat.st_size)
    cached = _info_cache.get(transcript_path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(transcript_path, "rb") as handle:
            if stat.st_size > tail_bytes:
                handle.seek(stat.st_size - tail_bytes)
                handle.readline()  # Drop the partial first line.
            data = handle.read()
    except OSError:
        return cached[1] if cached else {}
    info = parse_transcript_tail(data)
    if cached:
        for title in ("custom", "ai"):
            if title not in info and title in cached[1]:
                info[title] = cached[1][title]
    _info_cache[transcript_path] = (key, info)
    return info


def transcript_titles(transcript_path: str, tail_bytes: int = TITLE_TAIL_BYTES) -> dict[str, str]:
    """Latest user-set (/rename) and auto-generated chat titles."""
    info = transcript_info(transcript_path, tail_bytes)
    return {key: info[key] for key in ("custom", "ai") if info.get(key)}


TRANSCRIPT_FIELDS = ("activity", "context_tokens", "last_text", "subagents", "interrupted")


def add_transcript_info(sessions: list[dict]) -> list[dict]:
    """Attach chat titles and live details from each Claude session's transcript (renames fire no hook)."""
    for session in sessions:
        if session.get("agent") == "cowork":
            continue
        info = transcript_info(str(session.get("transcript_path") or ""))
        if info.get("custom"):
            session["custom_title"] = info["custom"]
        if info.get("ai"):
            session["ai_title"] = info["ai"]
        for name in TRANSCRIPT_FIELDS:
            if info.get(name):
                session[name] = info[name]
    _prune_info_cache({str(session.get("transcript_path") or "") for session in sessions})
    return sessions


def _prune_info_cache(live_paths: set[str]) -> None:
    """Forget transcripts of sessions that are no longer tracked, so the cache can't grow forever."""
    for path in [path for path in _info_cache if path not in live_paths]:
        del _info_cache[path]




def effective_status(session: dict, stale_minutes: float, now: float) -> str:
    """Running sessions with no transcript activity for a while are shown as stale."""
    status = session.get("status", "running")
    if status != "running":
        return status
    if session.get("interrupted"):
        return "answered"  # Esc fires no Stop hook; the transcript records the interrupt.
    last_activity = float(session.get("updated_at", now))
    transcript = session.get("transcript_path")
    if transcript:
        try:
            last_activity = max(last_activity, os.path.getmtime(transcript))
        except OSError:
            pass
    return "stale" if now - last_activity > stale_minutes * 60 else "running"


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "now"  # Minute granularity keeps the widget from redrawing every second.
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def folder_name(session: dict) -> str:
    # Split on both separators so Windows and WSL/Linux paths both yield the folder name.
    return str(session.get("cwd") or "").rstrip("\\/").replace("\\", "/").split("/")[-1]


def display_name(session: dict, max_chars: int) -> str:
    """Chat title as shown in Claude Code: your /rename title, else the auto title, else the first prompt."""
    label = (
        session.get("custom_title") or session.get("ai_title") or session.get("title") or session.get("first_prompt")
        or ""
    )
    if session.get("agent") == "cowork":
        folder = folder_name(session)
        name = f"{folder} · {label}" if folder and label else folder or label
    else:
        name = label or folder_name(session)
    name = name or session.get("name") or session.get("session_id", "session")[:8]
    name = " ".join(str(name).split())
    return name if len(name) <= max_chars else name[: max_chars - 1] + "…"


STATUS_LABELS = {
    "answered": "Answered",
    "attention": "Needs you",
    "running": "Running",
    "error": "Error",
    "stale": "Running (no activity)",
}
PREVIEW_CHARS = 140
MARKDOWN_NOISE = re.compile(r"\*\*|`|^#+\s*|(?<=\s)-\s(?=\S)|^-\s")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "under a minute"
    if seconds < 3600:
        return f"{seconds // 60} min"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def format_tokens(tokens: int) -> str:
    return f"{tokens / 1_000_000:.1f}M" if tokens >= 1_000_000 else f"{round(tokens / 1000)}k"


def status_line(session: dict, status: str, now: float) -> str:
    """'Waiting on you · 4 min', 'Running · 6 min', 'Done · 12 min ago'."""
    since = now - float(session.get("status_since") or session.get("updated_at") or now)
    if status == "attention":
        text = f"Waiting on you · {format_duration(since)}"
    elif status == "running":
        text = f"Running · {format_duration(since)}"
    elif status == "stale":
        text = f"No activity · {format_duration(now - float(session.get('updated_at') or now))}"
    else:
        label = {"error": "Error", "answered": "Stopped" if session.get("interrupted") else "Done"}.get(status, status)
        text = f"{label} · {format_duration(now - float(session.get('updated_at') or now))} ago"
    return f"{text} · {session['source']}" if session.get("source") else text


def detail_lines(session: dict, status: str, now: float) -> list[str]:
    """Hover-card lines under the title. Only what's known and relevant, so it stays short."""
    lines = [status_line(session, status, now)]
    if status == "attention" and session.get("attention_reason"):
        lines.append(f"Waiting on: {session['attention_reason']}")
    elif status == "running":
        lines.append(session.get("activity") or "Thinking…")
    extras = []
    if session.get("context_tokens"):
        extras.append(f"{format_tokens(int(session['context_tokens']))} context")
    if status == "running" and session.get("subagents"):
        count = int(session["subagents"])
        extras.append(f"{count} agent{'s' if count != 1 else ''} working")
    if extras:
        lines.append(" · ".join(extras))
    if status != "running" and session.get("last_text"):
        text = " ".join(MARKDOWN_NOISE.sub("", str(session["last_text"])).split())
        text = text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1].rstrip() + "…"
        lines.append(f"“{text}”")
    if session.get("cwd"):
        lines.append(str(session["cwd"]))
    if session.get("best_effort"):
        lines.append("Cowork status is best-effort (no 'needs you' signal)")
    return lines


def rows_from_sessions(sessions: list[dict], max_chars: int, stale_minutes: float, now: float | None = None) -> list[Row]:
    now = now or time.time()
    rows = []
    for session in sessions:
        status = session.get("status") if session.get("agent") == "cowork" else effective_status(session, stale_minutes, now)
        age = now - float(session.get("updated_at", now))
        tooltip_lines = [display_name(session, 200), *detail_lines(session, status, now)]
        activity = ""
        if status == "running" and session.get("agent") != "cowork":
            activity = session.get("activity") or "Thinking…"
        rows.append(
            Row(
                id=str(session.get("id", "")),
                status=status,
                name=display_name(session, max_chars),
                source=str(session.get("source", "")),
                age=format_age(age),
                tooltip="\n".join(tooltip_lines),
                activity=activity,
            )
        )
    rows.sort(key=lambda row: (STATUS_ORDER.get(row.status, 9), _age_seconds(row.age)))
    return rows


def _age_seconds(age: str) -> int:
    if age == "now":
        return 0
    unit = {"m": 60, "h": 3600}.get(age[-1:], 1)
    try:
        return int(age[:-1]) * unit
    except ValueError:
        return 0


def clear_finished(sessions_dir: Path) -> int:
    """Remove answered/error state files (the menu's 'Clear finished')."""
    removed = 0
    for path in sessions_dir.glob("*.json"):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("status") in ("answered", "error"):
                path.unlink()
                removed += 1
        except (OSError, ValueError):
            continue
    return removed
