"""Central configuration for the session overlay.

Every setting can be overridden through environment variables or a `.env`
file next to this module. Paths resolve relative to this project, never the
current shell directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Minimal stdlib .env loader: KEY=VALUE lines, existing env wins."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")


def _default_data_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "session-overlay"
    return Path.home() / ".session-overlay"


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_paths(name: str, default: list[Path]) -> list[Path]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return [Path(os.path.expandvars(os.path.expanduser(part.strip()))) for part in raw.split(";") if part.strip()]


VALID_CORNERS = ("top-center", "top-right", "top-left", "bottom-right", "bottom-left")
VALID_ALERT_SOUNDS = ("attention", "all", "off")  # Which alerts also play the Windows notification sound.

# Status colours (macOS dark system colours): answered / attention / running / error / stale
DEFAULT_COLORS = {
    "answered": "#30d158",
    "attention": "#ffd60a",
    "running": "#98989d",
    "error": "#ff453a",
    "stale": "#636366",
}


@dataclass(frozen=True)
class Settings:
    # Shared state written by hooks and read by the overlay
    DATA_DIR: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(_default_data_dir()))))
    LOG_DIR: Path = field(default_factory=lambda: Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs"))))

    # Claude Code config dirs to patch with hooks (";"-separated in .env)
    CLAUDE_CONFIG_DIRS: tuple[Path, ...] = field(
        default_factory=lambda: tuple(_env_paths("CLAUDE_CONFIG_DIRS", [Path.home() / ".claude"]))
    )

    # Widget placement and look
    CORNER: str = field(default_factory=lambda: os.getenv("CORNER", "top-center"))
    MARGIN_PX: int = field(default_factory=lambda: int(os.getenv("MARGIN_PX", "16")))
    OPACITY: float = field(default_factory=lambda: float(os.getenv("OPACITY", "0.85")))
    FONT_SIZE: int = field(default_factory=lambda: int(os.getenv("FONT_SIZE", "9")))
    NAME_MAX_CHARS: int = field(default_factory=lambda: int(os.getenv("NAME_MAX_CHARS", "42")))
    MAX_ROWS: int = field(default_factory=lambda: int(os.getenv("MAX_ROWS", "12")))
    BLINK_ATTENTION: bool = field(default_factory=lambda: _env_bool("BLINK_ATTENTION", True))
    COLORS: dict = field(default_factory=lambda: dict(DEFAULT_COLORS))

    # Alerts: a small banner under the pill when a session needs you, finishes or fails
    ALERTS: bool = field(default_factory=lambda: _env_bool("ALERTS", True))
    ALERT_SOUND: str = field(default_factory=lambda: os.getenv("ALERT_SOUND", "attention").strip().lower())
    ALERT_MIN_RUN_SECONDS: int = field(default_factory=lambda: int(os.getenv("ALERT_MIN_RUN_SECONDS", "30")))

    # Refresh and pruning
    POLL_MS: int = field(default_factory=lambda: int(os.getenv("POLL_MS", "1000")))
    STALE_RUNNING_MINUTES: int = field(default_factory=lambda: int(os.getenv("STALE_RUNNING_MINUTES", "15")))
    MAX_AGE_HOURS: int = field(default_factory=lambda: int(os.getenv("MAX_AGE_HOURS", "24")))

    # Cowork (best-effort, undocumented local files)
    COWORK_ENABLED: bool = field(default_factory=lambda: _env_bool("COWORK_ENABLED", True))
    COWORK_DIRS: tuple[Path, ...] = field(default_factory=lambda: tuple(_env_paths("COWORK_DIRS", [])))
    COWORK_ACTIVE_SECONDS: int = field(default_factory=lambda: int(os.getenv("COWORK_ACTIVE_SECONDS", "45")))
    COWORK_SHOW_HOURS: float = field(default_factory=lambda: float(os.getenv("COWORK_SHOW_HOURS", "3")))

    @property
    def SESSIONS_DIR(self) -> Path:
        return self.DATA_DIR / "sessions"

    @property
    def POSITION_FILE(self) -> Path:
        return self.DATA_DIR / "position.json"

    @property
    def HOOK_SCRIPT(self) -> Path:
        return BASE_DIR / "src" / "hook.py"

    def validate(self) -> None:
        if self.CORNER not in VALID_CORNERS:
            raise ValueError(f"CORNER must be one of {VALID_CORNERS}, got {self.CORNER!r}")
        if not 0.2 <= self.OPACITY <= 1.0:
            raise ValueError(f"OPACITY must be between 0.2 and 1.0, got {self.OPACITY}")
        if self.ALERT_SOUND not in VALID_ALERT_SOUNDS:
            raise ValueError(f"ALERT_SOUND must be one of {VALID_ALERT_SOUNDS}, got {self.ALERT_SOUND!r}")
        if self.POLL_MS < 200:
            raise ValueError(f"POLL_MS must be >= 200, got {self.POLL_MS}")


def load_settings() -> Settings:
    settings = Settings()
    settings.validate()
    return settings
