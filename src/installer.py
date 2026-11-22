"""Install/uninstall the overlay's Claude Code hooks and the optional login startup entry."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from src.hook import MARKER

logger = logging.getLogger(__name__)

# (event, matcher) pairs; None = match all occurrences.
HOOK_EVENTS: tuple[tuple[str, str | None], ...] = (
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("PreToolUse", "AskUserQuestion"),
    ("PermissionRequest", None),
    ("PostToolUse", None),
    ("Notification", None),
    ("Stop", None),
    ("StopFailure", None),
    ("SessionEnd", None),
)

STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "SessionOverlay"


class InstallError(RuntimeError):
    """Raised when a settings file cannot be safely modified."""


def windowless_python(executable: str | None = None) -> str:
    """Prefer pythonw.exe next to the current interpreter so hooks never open a console."""
    exe = Path(executable or sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if os.name == "nt" and candidate.exists():
        exe = candidate
    if "WindowsApps" in str(exe):
        logger.warning(
            "Python is the Microsoft Store alias (%s). Hooks may fail; install Python from python.org.", exe
        )
    return str(exe)


def build_handler(python_exe: str, hook_script: Path, state_dir: Path, shell_form: bool = False) -> dict:
    args = [str(hook_script), MARKER, "--agent", "claude", "--state-dir", str(state_dir)]
    if shell_form:
        # Experimental fallback for Claude Code versions without exec-form `args` support.
        # PowerShell doesn't wait for GUI-subsystem pythonw or forward stdin, so use python.exe + $input.
        console_python = python_exe.replace("pythonw.exe", "python.exe")
        quoted = " ".join(f"'{part}'" for part in args)
        return {
            "type": "command",
            "shell": "powershell",
            "command": f"$input | & '{console_python}' {quoted}",
            "async": True,
        }
    return {"type": "command", "command": python_exe, "args": args, "async": True, "timeout": 10}


def _is_ours(handler: dict) -> bool:
    return MARKER in handler.get("command", "") or MARKER in handler.get("args", [])


def strip_our_hooks(settings: dict) -> dict:
    """Remove only handlers carrying our marker; drop groups/events left empty."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = hooks[event] if isinstance(hooks[event], list) else []
        kept_groups = []
        for group in groups:
            handlers = [h for h in group.get("hooks", []) if not _is_ours(h)]
            if handlers:
                kept_groups.append({**group, "hooks": handlers})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings


def add_our_hooks(settings: dict, handler: dict) -> dict:
    settings = strip_our_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    for event, matcher in HOOK_EVENTS:
        group: dict = {"hooks": [dict(handler)]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    return settings


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as error:
        raise InstallError(f"{path} is not valid JSON ({error}); fix it before installing.") from error
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not contain a JSON object.")
    return data


def _write_settings(path: Path, data: dict) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        counter = 1
        while backup.exists():  # never overwrite an earlier backup from the same second
            backup = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
            counter += 1
        shutil.copy2(path, backup)
    tmp = path.with_suffix(".overlay.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def install(config_dirs: list[Path], handler: dict) -> list[str]:
    report = []
    for config_dir in config_dirs:
        path = Path(config_dir) / "settings.json"
        data = add_our_hooks(_load_settings(path), handler)
        backup = _write_settings(path, data)
        report.append(f"Installed hooks in {path}" + (f" (backup: {backup.name})" if backup else ""))
    return report


def uninstall(config_dirs: list[Path]) -> list[str]:
    report = []
    for config_dir in config_dirs:
        path = Path(config_dir) / "settings.json"
        if not path.exists():
            report.append(f"No settings file at {path}; nothing to remove")
            continue
        data = _load_settings(path)
        before = json.dumps(data, sort_keys=True)
        data = strip_our_hooks(data)
        if json.dumps(data, sort_keys=True) == before:
            report.append(f"No overlay hooks found in {path}")
            continue
        backup = _write_settings(path, data)
        report.append(f"Removed overlay hooks from {path} (backup: {backup.name if backup else '-'})")
    return report


def hooks_installed(config_dir: Path) -> bool:
    try:
        data = _load_settings(Path(config_dir) / "settings.json")
    except InstallError:
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    # Tolerate hand-edited settings: skip anything that isn't the expected shape.
    return any(
        isinstance(handler, dict) and _is_ours(handler)
        for groups in hooks.values() if isinstance(groups, list)
        for group in groups if isinstance(group, dict)
        for handler in (group.get("hooks") if isinstance(group.get("hooks"), list) else [])
    )


def set_startup(enabled: bool, command: str) -> str:
    """Add/remove an HKCU Run entry so the overlay starts at login (Windows only)."""
    if os.name != "nt":
        return "Startup at login is only supported on Windows."
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, command)
            return f"Overlay will start at login: {command}"
        try:
            winreg.DeleteValue(key, STARTUP_VALUE_NAME)
        except FileNotFoundError:
            return "Startup entry was not set."
        return "Startup entry removed."


def startup_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY) as key:
            winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
            return True
    except OSError:
        return False
