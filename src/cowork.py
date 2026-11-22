"""Best-effort Cowork session reader.

Local Cowork tasks run inside a VM, so Claude Code hooks can't reach Windows.
The Claude desktop app keeps per-session metadata files on the host
(`local-agent-mode-sessions/**/local_*.json`). That format is undocumented and
may change, so every field is optional and any failure just yields no rows.
Status is inferred from recent activity only: running (grey) or answered
(green). A "needs you" state can't be detected, and cloud Cowork tasks are
not visible on this computer at all.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_GLOB = "local_*.json"


def default_cowork_dirs() -> list[Path]:
    dirs = []
    app_data = os.getenv("APPDATA")
    local_app_data = os.getenv("LOCALAPPDATA")
    if app_data:
        dirs.append(Path(app_data) / "Claude" / "local-agent-mode-sessions")
    if local_app_data:
        # MSIX / Store installs virtualise %APPDATA% under the package folder.
        packages = Path(local_app_data) / "Packages"
        if packages.is_dir():
            for package in packages.glob("Claude_*"):
                dirs.append(package / "LocalCache" / "Roaming" / "Claude" / "local-agent-mode-sessions")
    return [d for d in dirs if d.is_dir()]


def _to_epoch(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e12 else float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def parse_session_file(path: Path, now: float, active_seconds: float, show_hours: float) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        file_mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return None  # Unreadable, mid-write or deleted between listing and reading.
    if not isinstance(data, dict) or data.get("isArchived"):
        return None

    last_activity = max(
        filter(None, [_to_epoch(data.get("lastActivityAt")), _to_epoch(data.get("updatedAt")), file_mtime])
    )
    idle_seconds = now - last_activity
    if idle_seconds > show_hours * 3600:
        return None

    session_id = str(data.get("sessionId") or data.get("id") or path.stem)
    title = data.get("title") or data.get("initialMessage") or "Cowork task"
    return {
        "id": f"cowork:{session_id}",
        "agent": "cowork",
        "session_id": session_id,
        "status": "running" if idle_seconds <= active_seconds else "answered",
        "title": " ".join(str(title).split())[:120],
        "source": "Cowork",
        "updated_at": last_activity,
        "best_effort": True,
    }


def load_cowork_sessions(
    dirs: list[Path] | None, active_seconds: float, show_hours: float, now: float | None = None
) -> list[dict]:
    now = now or time.time()
    sessions = []
    for base in dirs if dirs else default_cowork_dirs():
        try:
            for path in Path(base).rglob(SESSION_GLOB):
                session = parse_session_file(path, now, active_seconds, show_hours)
                if session:
                    sessions.append(session)
        except OSError as error:
            logger.debug("Cowork dir %s unreadable: %s", base, error)
    return sessions


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0  # Deleted since it was listed.


def describe_cowork_files(dirs: list[Path] | None, limit: int = 3) -> list[str]:
    """Diagnostics: which files were found and which keys they carry (no values)."""
    lines = []
    found_dirs = dirs if dirs else default_cowork_dirs()
    if not found_dirs:
        return ["No Cowork session folder found (Cowork may not have run locally on this PC)."]
    for base in found_dirs:
        try:
            files = sorted(Path(base).rglob(SESSION_GLOB), key=_mtime_or_zero, reverse=True)
        except OSError as error:
            lines.append(f"{base}: unreadable ({error})")
            continue
        lines.append(f"{base}: {len(files)} session file(s)")
        for path in files[:limit]:
            try:
                keys = sorted(json.loads(path.read_text(encoding="utf-8")).keys())
            except (OSError, ValueError, AttributeError):
                keys = ["<unreadable>"]
            lines.append(f"  {path.name}: keys={keys}")
    return lines
