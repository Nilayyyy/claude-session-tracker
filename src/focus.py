"""Bring a session's host window (VS Code, terminal, Claude desktop) to the front.

Windows-only; every function degrades to a no-op elsewhere. The host window is
found by walking up the Claude process's parent chain until an ancestor owns a
visible top-level window. When one process owns several windows (VS Code),
the one whose title names the session's folder wins.

For VS Code sessions the Claude extension's URI handler then reveals the exact
chat tab. The URI is only sent once the right window is confirmed in the
foreground, because VS Code delivers it to the last-focused window and opening
a session in the wrong workspace would be worse than doing nothing.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
VSCODE_URI = "vscode://anthropic.claude-code/open?session={session_id}"
_UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


# --- Process tree ---------------------------------------------------------------------

SHELL_IMAGES = {"explorer.exe"}  # The desktop shell: never a session's host window.


def _parent_map() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, lowercase exe name) for every running process (Toolhelp snapshot)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    parents: dict[int, tuple[int, str]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def ancestors(pid: int, parents: dict[int, tuple[int, str]], limit: int = 12) -> list[int]:
    """pid and its ancestors, nearest first.

    Stops at cycles, missing parents and the desktop shell: if a terminal's host isn't an
    ancestor, walking on into explorer.exe would focus the desktop or a File Explorer window.
    """
    chain: list[int] = []
    while pid and pid in parents and pid not in chain and len(chain) < limit:
        parent, image = parents[pid]
        if image in SHELL_IMAGES:
            break
        chain.append(pid)
        pid = parent
    return chain


def hosted_by_vscode_extension(chain: list[int], parents: dict[int, tuple[int, str]]) -> bool:
    """True when Claude's direct parent is VS Code itself (the extension), not a terminal shell.

    A CLI session in VS Code's integrated terminal has a shell as parent; opening its id
    through the extension would start a second copy of a still-running session.
    """
    return len(chain) > 1 and parents.get(chain[1], (0, ""))[1] in {"code.exe", "code - insiders.exe"}


# --- Windows --------------------------------------------------------------------------

def _visible_windows() -> list[tuple[int, int, str]]:
    """(hwnd, owning pid, title) for visible, titled, unowned top-level windows."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER: skip dialogs/tooltips
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((int(hwnd), pid.value, buffer.value))
        return True

    user32.EnumWindows(collect, 0)
    return found


def folder_names(cwd: str) -> list[str]:
    """The cwd's folder and its parents' names, nearest first (a session may run in a subfolder)."""
    parts = [part for part in cwd.replace("\\", "/").split("/") if part and not part.endswith(":")]
    return list(reversed(parts))


def pick_window(windows: list[tuple[int, int, str]], chain: list[int], cwd: str) -> int | None:
    """Choose the window of the nearest ancestor that owns one; break ties by folder name in the title."""
    names = [name.lower() for name in folder_names(cwd)]
    for pid in chain:
        owned = [(hwnd, title) for hwnd, owner, title in windows if owner == pid]
        if not owned:
            continue
        if len(owned) == 1:
            return owned[0][0]
        for name in names:
            # VS Code titles look like "<editor> - <folder> - Visual Studio Code".
            for hwnd, title in owned:
                if f" - {name} - " in f" - {title.lower()} - ":
                    return hwnd
        return None  # Several windows and none names the folder: don't guess.
    return None


def _bring_to_front(hwnd: int) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if not user32.SetForegroundWindow(hwnd):
        # Foreground lock fallback: a synthetic Alt press lets this process set the foreground.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 0x0002, 0)
        user32.SetForegroundWindow(hwnd)


# --- Public API -----------------------------------------------------------------------

def focus_session(session: dict) -> tuple[str, bool]:
    """Bring the session's window forward. Returns (hwnd or "", hosted by the VS Code extension)."""
    if not IS_WINDOWS:
        return "", False
    pid = int(session.get("pid") or 0)
    if not pid:
        return "", False
    parents = _parent_map()
    chain = ancestors(pid, parents)
    hwnd = pick_window(_visible_windows(), chain, str(session.get("cwd") or ""))
    if hwnd is None:
        logger.info("No window found for session %s (pid %s)", session.get("session_id"), pid)
        return "", False
    _bring_to_front(hwnd)  # Focus lands asynchronously; callers check is_foreground() later.
    in_extension = hosted_by_vscode_extension(chain, parents)
    logger.info("Focus session %s -> hwnd %s (vscode extension=%s)", session.get("session_id"), hwnd, in_extension)
    return str(hwnd), in_extension


def session_in_front(session: dict, others: list[dict]) -> bool:
    """True when the session's window is in front and no other tracked session shares it.

    One VS Code window can hold several chat tabs; if another session lives in the same
    window, being in that window doesn't mean you can see this chat.
    """
    if not IS_WINDOWS or not session.get("pid"):
        return False
    import ctypes

    foreground = ctypes.windll.user32.GetForegroundWindow()
    if not foreground:
        return False
    parents, windows = _parent_map(), _visible_windows()

    def window_of(item: dict) -> int | None:
        chain = ancestors(int(item.get("pid") or 0), parents)
        return pick_window(windows, chain, str(item.get("cwd") or ""))

    if window_of(session) != foreground:
        return False
    return not any(window_of(other) == foreground for other in others
                   if other is not session and other.get("pid") and other.get("agent") != "cowork")


def is_foreground(hwnd: str) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    import ctypes

    return ctypes.windll.user32.GetForegroundWindow() == int(hwnd)


def open_vscode_chat(session_id: str) -> None:
    """Ask the Claude VS Code extension to reveal (or resume) the chat tab."""
    if not IS_WINDOWS or not _UUID.match(session_id or ""):
        return
    os.startfile(VSCODE_URI.format(session_id=session_id))  # noqa: S606 - fixed scheme, validated id
