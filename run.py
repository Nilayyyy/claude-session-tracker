"""Session Overlay: CLI entry point.

    python run.py                    start the overlay widget
    python run.py --demo             start it with sample sessions (visual smoke test)
    python run.py --install-hooks    add the status hooks to Claude Code settings
    python run.py --uninstall-hooks  remove them again
    python run.py --diagnose         show what the overlay can see
    python run.py --startup on|off   start the overlay at Windows login
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from config import BASE_DIR, VALID_CORNERS, Settings, load_settings
from src import cowork, installer, store

logger = logging.getLogger("session_overlay")

COWORK_REFRESH_SECONDS = 5
MUTEX_NAME = "Local\\SessionOverlaySingleton"


def setup_logging(log_dir: Path, verbose: bool) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"overlay_{time.strftime('%Y%m%d_%H%M%S')}.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    # Keep only the 10 most recent log files.
    for old in sorted(log_dir.glob("overlay_*.log"))[:-10]:
        try:
            old.unlink()
        except OSError:
            pass
    return log_file


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Always-on-top status widget for AI coding sessions.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--demo", action="store_true", help="show sample sessions instead of real ones")
    action.add_argument("--install-hooks", action="store_true", help="install Claude Code status hooks")
    action.add_argument("--uninstall-hooks", action="store_true", help="remove the overlay's Claude Code hooks")
    action.add_argument("--diagnose", action="store_true", help="print what the overlay can detect")
    action.add_argument("--startup", choices=("on", "off"), help="start the overlay at Windows login")
    parser.add_argument("--corner", choices=VALID_CORNERS, help="screen corner (overrides saved position)")
    parser.add_argument("--data-dir", type=Path, help="override DATA_DIR (state shared with hooks)")
    parser.add_argument("--config-dir", type=Path, action="append", help="Claude config dir to patch (repeatable)")
    parser.add_argument("--shell-form", action="store_true", help="install hooks without exec-form `args`")
    parser.add_argument("--at-login", action="store_true",
                        help="started by the login entry: wait for the desktop before creating windows")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    overrides: dict = {}
    if args.corner:
        overrides["CORNER"] = args.corner
    if args.data_dir:
        overrides["DATA_DIR"] = args.data_dir.resolve()
    if args.config_dir:
        overrides["CLAUDE_CONFIG_DIRS"] = tuple(path.expanduser().resolve() for path in args.config_dir)
    settings = replace(settings, **overrides) if overrides else settings
    settings.validate()
    return settings


def make_fetcher(settings: Settings):
    """Combine hook sessions (every poll) with Cowork sessions (cached for a few seconds)."""
    cache = {"at": 0.0, "sessions": []}

    def fetch() -> list[dict]:
        sessions = store.add_transcript_info(store.load_hook_sessions(settings.SESSIONS_DIR, settings.MAX_AGE_HOURS))
        if settings.COWORK_ENABLED:
            now = time.time()
            if now - cache["at"] > COWORK_REFRESH_SECONDS:
                cache["sessions"] = cowork.load_cowork_sessions(
                    list(settings.COWORK_DIRS), settings.COWORK_ACTIVE_SECONDS, settings.COWORK_SHOW_HOURS, now
                )
                cache["at"] = now
            sessions = sessions + cache["sessions"]
        return sessions

    return fetch


def demo_sessions() -> list[dict]:
    now = time.time()
    return [
        {"id": "d1", "agent": "claude", "status": "attention", "cwd": r"C:\code\api-gateway",
         "custom_title": "Gateway retries", "first_prompt": "Refactor the gateway retries", "source": "VS Code", "updated_at": now - 30,
         "attention_reason": "Claude needs your permission to use Bash"},
        {"id": "d2", "agent": "claude", "status": "running", "cwd": r"C:\code\claude-session-tracker",
         "ai_title": "Store test coverage", "first_prompt": "Write tests for the store", "source": "CLI", "updated_at": now - 5,
         "activity": "Run tests"},
        {"id": "d3", "agent": "claude", "status": "answered", "cwd": r"C:\code\blog",
         "first_prompt": "Fix the broken links in the docs", "source": "VS Code", "updated_at": now - 420},
        {"id": "d4", "agent": "cowork", "status": "running", "title": "Weekly report deck", "source": "Cowork",
         "updated_at": now - 12, "best_effort": True},
        {"id": "d5", "agent": "claude", "status": "error", "cwd": r"C:\code\data-pipeline",
         "first_prompt": "Migrate the ETL job", "source": "CLI", "updated_at": now - 90},
    ]


def acquire_single_instance():
    """Return a mutex handle, or None if another overlay is already running (Windows only)."""
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


LOGIN_SETTLE_SECONDS = 8
LOGIN_MAX_WAIT_SECONDS = 180


def wait_for_desktop() -> None:
    """At login, wait for the taskbar plus a few seconds before creating windows.

    Windows created while the shell is still starting can land in the wrong z-order band:
    flagged always-on-top yet stacked below ordinary windows, and SetWindowPos can't fix it.
    """
    if os.name != "nt":
        return
    import ctypes

    deadline = time.time() + LOGIN_MAX_WAIT_SECONDS
    while time.time() < deadline and not ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None):
        time.sleep(1)
    time.sleep(LOGIN_SETTLE_SECONDS)
    logger.info("Desktop ready; starting overlay")


def run_overlay(settings: Settings, demo: bool, force_corner: bool, at_login: bool = False) -> int:
    try:
        from src.overlay import Overlay
    except ImportError as error:
        logger.error("tkinter is unavailable (%s). Install Python from python.org with Tcl/Tk.", error)
        return 1

    mutex = acquire_single_instance()
    if mutex is None:
        logger.info("Overlay is already running; exiting.")
        return 0

    if at_login:
        wait_for_desktop()
    settings.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Starting overlay: corner=%s data_dir=%s demo=%s", settings.CORNER, settings.DATA_DIR, demo)
    fetch = demo_sessions if demo else make_fetcher(settings)
    on_clear = (lambda: 0) if demo else (lambda: store.clear_finished(settings.SESSIONS_DIR))
    started = time.time()
    Overlay(settings, fetch, on_clear, force_corner=force_corner).run()
    logger.info("Overlay closed after %.0fs", time.time() - started)
    return 0


def install_hooks(settings: Settings, shell_form: bool) -> int:
    python_exe = installer.windowless_python()
    handler = installer.build_handler(python_exe, settings.HOOK_SCRIPT, settings.SESSIONS_DIR, shell_form)
    logger.info("Hook command: %s %s", python_exe, settings.HOOK_SCRIPT)
    try:
        for line in installer.install(list(settings.CLAUDE_CONFIG_DIRS), handler):
            logger.info(line)
    except installer.InstallError as error:
        logger.error("%s", error)
        return 1
    logger.info("Done. Already-open Claude Code sessions pick up hooks after a restart (or run /hooks).")
    return 0


def uninstall_hooks(settings: Settings) -> int:
    try:
        for line in installer.uninstall(list(settings.CLAUDE_CONFIG_DIRS)):
            logger.info(line)
    except installer.InstallError as error:
        logger.error("%s", error)
        return 1
    return 0


def diagnose(settings: Settings) -> int:
    print(f"Project:        {BASE_DIR}")
    print(f"Python:         {sys.executable}")
    print(f"Data dir:       {settings.DATA_DIR}")
    for config_dir in settings.CLAUDE_CONFIG_DIRS:
        state = "installed" if installer.hooks_installed(config_dir) else "NOT installed"
        print(f"Hooks:          {state} in {config_dir / 'settings.json'}")
    print(f"Start at login: {'on' if installer.startup_enabled() else 'off'}")
    hook_log = settings.DATA_DIR / "hook_errors.log"
    if hook_log.exists():
        print(f"Hook errors:    see {hook_log}")
    sessions = store.add_transcript_info(store.load_hook_sessions(settings.SESSIONS_DIR, settings.MAX_AGE_HOURS))
    print(f"\nClaude Code sessions tracked: {len(sessions)}")
    for row in store.rows_from_sessions(sessions, 60, settings.STALE_RUNNING_MINUTES):
        print(f"  [{row.status:<9}] {row.name}  ({row.source}, {row.age})")
    print("\nCowork (best-effort):")
    for line in cowork.describe_cowork_files(list(settings.COWORK_DIRS)):
        print(f"  {line}")
    cowork_sessions = cowork.load_cowork_sessions(
        list(settings.COWORK_DIRS), settings.COWORK_ACTIVE_SECONDS, settings.COWORK_SHOW_HOURS
    )
    print(f"  Recent Cowork sessions shown: {len(cowork_sessions)}")
    return 0


def startup(enabled: bool) -> int:
    pythonw = installer.windowless_python()
    command = f'"{pythonw}" "{BASE_DIR / "run.py"}" --at-login'
    logger.info(installer.set_startup(enabled, command))
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    settings = apply_overrides(load_settings(), args)
    if args.diagnose:
        return diagnose(settings)
    log_file = setup_logging(settings.LOG_DIR, args.verbose)
    logger.info("Log file: %s", log_file)
    if args.install_hooks:
        return install_hooks(settings, args.shell_form)
    if args.uninstall_hooks:
        return uninstall_hooks(settings)
    if args.startup:
        return startup(args.startup == "on")
    return run_overlay(settings, args.demo, force_corner=bool(args.corner), at_login=args.at_login)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
