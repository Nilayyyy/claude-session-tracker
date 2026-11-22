"""Always-on-top tkinter widget: a small top-centre pill that reveals a session list on hover.

Look and feel follows macOS dark UI: rounded windows (native Windows 11 DWM corners),
system-style colours, Segoe UI Variable type, rounded row highlights, a breathing
"needs you" dot, and fades for the panel and hover card.

Smoothness rule: windows are never resized or re-mapped while animating. The panel sits
exactly over the pill, pre-rendered, at opacity 0 and click-through; expanding is only an
opacity fade, which the compositor does without repainting anything.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from typing import Callable

from config import VALID_CORNERS, Settings
from src import focus
from src.store import Row, rows_from_sessions

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# macOS dark palette
BG = "#1c1c1e"
ROW_HOVER = "#2c2c2e"
ROW_PRESS = "#3a3a3c"
SEPARATOR = "#2e2e30"
BORDER = "#3a3a3c"
TEXT = "#f5f5f7"
SECONDARY = "#98989d"
TERTIARY = "#636366"
CARD_BG = "#2c2c2e"
ATTENTION_DIM = "#5c4d07"

# Font families, first available wins.
DISPLAY_FONTS = ("Segoe UI Variable Display Semib", "Segoe UI Semibold", "Segoe UI", "TkDefaultFont")
TEXT_FONTS = ("Segoe UI Variable Text", "Segoe UI", "TkDefaultFont")
SMALL_FONTS = ("Segoe UI Variable Small", "Segoe UI", "TkDefaultFont")

# Geometry, in logical pixels (scaled by DPI at runtime).
PANEL_WIDTH = 380
PILL_PAD_X = 16
PILL_HEIGHT = 34
PANEL_PAD = 6
ROW_HEIGHT = 30
ROW_RADIUS = 7

# Timing
FADE_IN_MS = 170
FADE_OUT_MS = 140
FRAME_MS = 10
PULSE_PERIOD_S = 1.6
PULSE_FRAME_MS = 50
TOOLTIP_DELAY_MS = 550
TOPMOST_MS = 5000
DRAG_THRESHOLD_PX = 4
HOVER_OPEN_MS = 200  # Pointer must rest this long before expanding, so passing by doesn't open it.
HOVER_CLOSE_MS = 350  # Grace period before collapsing after the pointer leaves.
URI_DELAY_MS = 250  # Let VS Code register the window focus before it routes the URI.
ALERT_SHOW_MS = 6000  # How long the alert banner stays (longer for "needs you").
ATTENTION_ALERT_SHOW_MS = 9000
ALERT_LINGER_MS = 1500  # After the pointer leaves the banner.
ALERT_GAP = 6
ALERT_TITLE_MAX = 260
ACTIVITY_MAX_SHARE = 0.42  # Of the row width.
ALERT_TEXT = {"attention": "needs you", "answered": "is done", "error": "hit an error"}


# --- Pure helpers (unit-tested) -------------------------------------------------------

def ease_out_cubic(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def mix_color(a: str, b: str, t: float) -> str:
    """Blend two #rrggbb colours; t=0 gives a, t=1 gives b."""
    t = min(max(t, 0.0), 1.0)
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def elide(text: str, max_px: int, measure: Callable[[str], int]) -> str:
    """Trim text with an ellipsis so it fits in max_px (binary search on the measured width)."""
    if measure(text) <= max_px:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(text[:mid].rstrip() + "…") <= max_px:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + "…" if low else "…"


def pulse_level(now: float, period: float = PULSE_PERIOD_S) -> float:
    """0..1 breathing curve (1 = full colour)."""
    return 0.5 + 0.5 * math.cos(2 * math.pi * (now % period) / period)


def rounded_rect_points(x1: float, y1: float, x2: float, y2: float, r: float) -> list[float]:
    """Polygon points for a smooth=True canvas polygon that renders as a rounded rectangle."""
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


# --- Win32 helpers (no-ops elsewhere) -------------------------------------------------

def enable_dpi_awareness() -> None:
    if not IS_WINDOWS:
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _hwnd(window: tk.Misc) -> int:
    import ctypes

    return ctypes.windll.user32.GetParent(window.winfo_id())


def apply_rounded_style(window: tk.Misc, border: str = BORDER) -> None:
    """Windows 11 native rounded corners + thin border; silently a plain rectangle on Windows 10."""
    if not IS_WINDOWS:
        return
    import ctypes

    try:
        hwnd = _hwnd(window)
        corner = ctypes.c_int(2)  # DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
        r, g, b = (int(border[i:i + 2], 16) for i in (1, 3, 5))
        colorref = ctypes.c_uint(r | (g << 8) | (b << 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(colorref), 4)  # DWMWA_BORDER_COLOR
    except Exception:
        logger.debug("Rounded window style unavailable", exc_info=True)


def set_click_through(window: tk.Misc, enabled: bool) -> None:
    """WS_EX_TRANSPARENT on a layered window: the mouse passes straight through it."""
    if not IS_WINDOWS:
        return
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = _hwnd(window)
    style = user32.GetWindowLongW(hwnd, -20)
    style = (style | 0x00000020 | 0x00080000) if enabled else (style & ~0x00000020)  # TRANSPARENT (+LAYERED)
    user32.SetWindowLongW(hwnd, -20, style)


def set_timer_resolution(fine: bool) -> None:
    """1 ms timer resolution while the widget runs, so animation frames are evenly spaced."""
    if not IS_WINDOWS:
        return
    import ctypes

    try:
        (ctypes.windll.winmm.timeBeginPeriod if fine else ctypes.windll.winmm.timeEndPeriod)(1)
    except Exception:
        pass


def make_tool_window(root: tk.Misc) -> None:
    """No taskbar/Alt-Tab entry and never take focus from the window you're working in."""
    if not IS_WINDOWS:
        return
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = _hwnd(root)
    style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    style = (style | 0x00000080 | 0x08000000) & ~0x00040000  # +TOOLWINDOW +NOACTIVATE -APPWINDOW
    user32.SetWindowLongW(hwnd, -20, style)
    reassert_topmost(root, frame_changed=True)


def reassert_topmost(root: tk.Misc, frame_changed: bool = False) -> None:
    if not IS_WINDOWS:
        root.attributes("-topmost", True)
        return
    import ctypes

    flags = 0x0001 | 0x0002 | 0x0010 | (0x0020 if frame_changed else 0)  # NOSIZE|NOMOVE|NOACTIVATE|FRAMECHANGED
    ctypes.windll.user32.SetWindowPos(_hwnd(root), -1, 0, 0, 0, 0, flags)  # HWND_TOPMOST


def below_normal_windows(window: tk.Misc) -> bool:
    """True if a visible, non-topmost window is stacked above this topmost window (a broken z-order band)."""
    if not IS_WINDOWS:
        return False
    import ctypes

    user32 = ctypes.windll.user32
    target = _hwnd(window)
    hwnd = user32.GetTopWindow(None)
    for _ in range(2000):
        if not hwnd or hwnd == target:
            return False
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and not (user32.GetWindowLongW(hwnd, -20) & 0x8):
            if user32.GetWindowTextLengthW(hwnd):  # A real, titled app window sits above us.
                return True
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    return False


def play_alert_sound() -> None:
    """The Windows 'asterisk' sound from the user's sound scheme (silent if they turned it off)."""
    if not IS_WINDOWS:
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        logger.debug("Alert sound failed", exc_info=True)


def work_area(root: tk.Tk) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary monitor, excluding the taskbar."""
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def virtual_screen(root: tk.Tk) -> tuple[int, int, int, int]:
    """Bounds spanning all monitors, used to keep a dragged widget reachable."""
    if IS_WINDOWS:
        import ctypes

        metrics = ctypes.windll.user32.GetSystemMetrics
        left, top = metrics(76), metrics(77)
        return left, top, left + metrics(78), top + metrics(79)
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def corner_position(corner: str, size: tuple[int, int], area: tuple[int, int, int, int], margin: int) -> tuple[int, int]:
    width, height = size
    left, top, right, bottom = area
    if corner == "top-center":
        return left + (right - left - width) // 2, top  # Centred and flush with the top edge.
    x = left + margin if corner.endswith("left") else right - width - margin
    y = top + margin if corner.startswith("top") else bottom - height - margin
    return x, y


def clamp_position(x: int, y: int, size: tuple[int, int], bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    width, height = size
    left, top, right, bottom = bounds
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))


def _pick_font(root: tk.Misc, candidates: tuple[str, ...]) -> str:
    families = set(tkfont.families(root))
    return next((name for name in candidates if name in families), candidates[-1])


# --- Row view ---------------------------------------------------------------------------

class RowView:
    """One session row drawn on a canvas: rounded highlight, status dot, title, meta, chevron."""

    def __init__(self, overlay: "Overlay", parent: tk.Misc, row: Row, clickable: bool):
        self.overlay = overlay
        self.row = row
        self.clickable = clickable
        s = overlay.px
        self.width = overlay.panel_width - 2 * s(PANEL_PAD)
        self.height = s(ROW_HEIGHT)
        self.canvas = tk.Canvas(parent, width=self.width, height=self.height, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(pady=(0, s(1)))
        inset = s(1)
        self.highlight = self.canvas.create_polygon(
            rounded_rect_points(inset, inset, self.width - inset, self.height - inset, s(ROW_RADIUS)),
            smooth=True, fill=BG, outline="",
        )
        mid = self.height / 2
        self.dot = self.canvas.create_text(s(14), mid, text="●", font=overlay.font_dot, anchor="center")
        self.title = self.canvas.create_text(s(28), mid, text="", font=overlay.font_row, fill=TEXT, anchor="w")
        self.meta = self.canvas.create_text(self.width - s(26), mid, text="", font=overlay.font_meta, fill=SECONDARY,
                                            anchor="e")
        self.chevron = self.canvas.create_text(self.width - s(12), mid - s(1), text="›", font=overlay.font_chevron,
                                               fill=BG, anchor="center")
        self.update(row)

        overlay._bind_drag(self.canvas)
        if clickable:
            self.canvas.configure(cursor="hand2")
            self.canvas.bind("<ButtonPress-1>", self._pressed, add="+")
            self.canvas.bind("<ButtonRelease-1>", lambda _e: self._set_fill(ROW_HOVER), add="+")
        self.canvas.bind("<Enter>", self._entered, add="+")
        self.canvas.bind("<Leave>", self._left, add="+")
        self.canvas.bind("<Button-3>", lambda event: overlay._show_row_menu(event, self.row.id))

    def update(self, row: Row) -> None:
        self.row = row
        o = self.overlay
        color = o.colors.get(row.status, SECONDARY)
        if row.status == "stale":
            self.canvas.itemconfigure(self.dot, text="○", fill=o.colors.get("running", SECONDARY))
        else:
            self.canvas.itemconfigure(self.dot, text="●", fill=color)
        if row.activity:
            # Running rows say what they're doing instead of where/when; capped so the title keeps room.
            meta = elide(row.activity, int(self.width * ACTIVITY_MAX_SHARE), o.font_meta_obj.measure)
        else:
            meta = f"{row.source} · {row.age}" if row.source else row.age
        self.canvas.itemconfigure(self.meta, text=meta)
        meta_width = o.font_meta_obj.measure(meta)
        available = self.width - o.px(28) - o.px(26) - meta_width - o.px(12)
        self.canvas.itemconfigure(self.title, text=elide(row.name, available, o.font_row_obj.measure))

    def set_dot_color(self, color: str) -> None:
        self.canvas.itemconfigure(self.dot, fill=color)

    def _set_fill(self, color: str) -> None:
        try:
            self.canvas.itemconfigure(self.highlight, fill=color)
            self.canvas.itemconfigure(self.chevron, fill=TERTIARY if self.clickable and color != BG else color)
        except tk.TclError:
            pass

    def _pressed(self, _event: tk.Event) -> None:
        self.overlay.pressed_row = self.row.id
        self._set_fill(ROW_PRESS)

    def _entered(self, event: tk.Event) -> None:
        self._set_fill(ROW_HOVER)
        self.overlay._schedule_tooltip(self, event)

    def _left(self, _event: tk.Event) -> None:
        self._set_fill(BG)
        self.overlay._cancel_tooltip()


# --- Widget ---------------------------------------------------------------------------

class Header:
    """'Sessions' + coloured counts, centred in a fixed-height strip. Updated in place, never rebuilt."""

    ORDER = ("attention", "error", "running", "stale", "answered")

    def __init__(self, overlay: "Overlay", parent: tk.Misc):
        self.overlay = overlay
        self.frame = tk.Frame(parent, bg=BG, height=overlay.px(PILL_HEIGHT))
        self.frame.pack_propagate(False)
        self.group = tk.Frame(self.frame, bg=BG)
        self.group.place(relx=0.5, rely=0.5, anchor="center")
        self.title = tk.Label(self.group, text="Sessions", bg=BG, fg=TEXT, font=overlay.font_title)
        self.title.pack(side="left")
        self.counts: dict[str, tk.Label] = {}
        for status in self.ORDER:
            glyph = "○" if status == "stale" else "●"
            color = overlay.colors["running"] if status == "stale" else overlay.colors[status]
            label = tk.Label(self.group, text=glyph, bg=BG, fg=color, font=overlay.font_count)
            label.glyph = glyph  # type: ignore[attr-defined]
            self.counts[status] = label
        self.visible: tuple[str, ...] = ()
        for widget in (self.frame, self.group, self.title, *self.counts.values()):
            overlay._bind_drag(widget)
            widget.bind("<Double-Button-1>", lambda _event: overlay.toggle_pinned())

    def update(self, counts: dict[str, int]) -> None:
        visible = tuple(status for status in self.ORDER if counts.get(status))
        if visible != self.visible:
            for label in self.counts.values():
                label.pack_forget()
            for status in visible:
                self.counts[status].pack(side="left", padx=(self.overlay.px(8), 0))
            self.visible = visible
        for status in visible:
            label = self.counts[status]
            text = f"{label.glyph} {counts[status]}"  # type: ignore[attr-defined]
            if label.cget("text") != text:
                label.configure(text=text)

    @property
    def attention_label(self) -> tk.Label | None:
        return self.counts["attention"] if "attention" in self.visible else None


class Overlay:
    """Four persistent windows: the pill (root), the panel, the hover card and the alert banner.

    Nothing is ever resized or re-mapped during an animation: hidden windows sit in place at
    alpha 0 and click-through, and showing/hiding only fades their opacity. Tk/Windows can't
    repaint a window that is being resized every frame without tearing and black flashes, but
    an opacity change is a pure compositor operation.
    """

    def __init__(
        self,
        settings: Settings,
        fetch_sessions: Callable[[], list[dict]],
        on_clear: Callable[[], int],
        force_corner: bool = False,
    ):
        self.settings = settings
        self.fetch_sessions = fetch_sessions
        self.on_clear = on_clear
        self.colors = settings.COLORS
        self.collapsed = True  # Small pill by default; expands while hovered.
        self.pinned = False  # "Keep expanded" from the menu or a header double-click.
        self.hovered = False
        self.hover_job: str | None = None
        self.tooltip_job: str | None = None
        self.card_row: str | None = None
        self.drag_offset: tuple[int, int] | None = None
        self.press_point: tuple[int, int] | None = None
        self.dragging = False
        self.pressed_row: str | None = None
        self.sessions_by_id: dict[str, dict] = {}
        self.row_views: dict[str, RowView] = {}
        self.row_ids: tuple[str, ...] = ()
        self.pulse_job: str | None = None
        self.fades: dict[str, dict] = {}  # window name -> fade state
        self.fade_job: str | None = None
        self.alpha = {"panel": 0.0, "card": 0.0, "toast": 0.0}
        self.prev_statuses: dict[str, str] | None = None  # None until the first load (no alerts at startup).
        self.prev_sessions: dict[str, dict] = {}
        self.dismissed: dict[str, str] = {}  # row id -> status it was hidden at; reappears when that changes.
        self.toast_row: str | None = None
        self.toast_job: str | None = None
        self.last_rows: tuple[Row, ...] | None = None
        self.pill_size = (1, 1)
        self.panel_size = (1, 1)
        # An explicit --corner beats a previously saved drag position.
        self.position = {"mode": "corner", "corner": settings.CORNER} if force_corner else self._load_position()

        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        self._make_fonts()
        self.panel_width = self.px(PANEL_WIDTH)

        self._style_window(self.root, "Session Overlay", settings.OPACITY)
        self.panel = tk.Toplevel(self.root)
        self.panel.withdraw()
        self._style_window(self.panel, "Session Overlay Panel", 0.0)
        self.card = tk.Toplevel(self.root)
        self.card.withdraw()
        self._style_window(self.card, "Session Overlay Card", 0.0, bg=CARD_BG)
        self.toast = tk.Toplevel(self.root)
        self.toast.withdraw()
        self._style_window(self.toast, "Session Overlay Alert", 0.0)

        # Pill: just the header.
        self.pill_header = Header(self, self.root)
        self.pill_header.frame.pack(fill="both", expand=True)
        # Panel: the same header in the same spot, then the rows.
        self.panel_header = Header(self, self.panel)
        self.panel_header.frame.pack(fill="x")
        self.separator = tk.Frame(self.panel, bg=SEPARATOR, height=max(1, self.px(1)))
        self.separator.pack(fill="x", padx=self.px(PANEL_PAD + 6))
        self.rows_frame = tk.Frame(self.panel, bg=BG)
        self.rows_frame.pack(fill="x", padx=self.px(PANEL_PAD), pady=(self.px(5), self.px(PANEL_PAD)))
        self.footer = tk.Label(self.rows_frame, bg=BG, fg=TERTIARY, font=self.font_meta)
        # Not on self.panel itself: a toplevel's bindings also fire for every child widget,
        # which would run the press/release handlers twice and lose the clicked row.
        for widget in (self.rows_frame, self.separator, self.footer):
            self._bind_drag(widget)
        self._build_card()
        self._build_toast()
        self._build_menu()
        # Children carry their toplevel in their bindtags, so these cover each whole window.
        for window in (self.root, self.panel):
            window.bind("<Enter>", self._pointer_entered, add="+")
            window.bind("<Leave>", self._pointer_left, add="+")

    def px(self, logical: float) -> int:
        return max(1, round(logical * self.scale))

    def _make_fonts(self) -> None:
        size = self.settings.FONT_SIZE
        display, text, small = (_pick_font(self.root, f) for f in (DISPLAY_FONTS, TEXT_FONTS, SMALL_FONTS))
        self.font_title = tkfont.Font(self.root, family=display, size=size + 1)
        self.font_count = tkfont.Font(self.root, family=small, size=size)
        self.font_row_obj = tkfont.Font(self.root, family=text, size=size + 1)
        self.font_meta_obj = tkfont.Font(self.root, family=small, size=size)
        self.font_row, self.font_meta = self.font_row_obj, self.font_meta_obj
        self.font_dot = tkfont.Font(self.root, family=text, size=size + 1)
        self.font_chevron = tkfont.Font(self.root, family=text, size=size + 3)
        self.font_card_title = tkfont.Font(self.root, family=display, size=size)
        self.font_card = tkfont.Font(self.root, family=small, size=size)

    @staticmethod
    def _style_window(window: tk.Misc, title: str, alpha: float, bg: str = BG) -> None:
        window.title(title)
        window.overrideredirect(True)
        window.configure(bg=bg)
        window.attributes("-topmost", True)
        window.attributes("-alpha", alpha)

    # Layout ----------------------------------------------------------------------------

    def _build_card(self) -> None:
        frame = tk.Frame(self.card, bg=CARD_BG, padx=self.px(12), pady=self.px(9))
        frame.pack()
        wrap = self.px(360)
        self.card_title = tk.Label(frame, bg=CARD_BG, fg=TEXT, font=self.font_card_title, justify="left",
                                   wraplength=wrap, anchor="w")
        self.card_title.pack(fill="x")
        self.card_body = tk.Label(frame, bg=CARD_BG, fg=SECONDARY, font=self.font_card, justify="left",
                                  wraplength=wrap, anchor="w")
        self.card_body.pack(fill="x", pady=(self.px(3), 0))

    def _build_toast(self) -> None:
        """The alert banner: '● Gateway retries  needs you', dropped just below the pill."""
        s = self.px
        self.toast_canvas = tk.Canvas(self.toast, bg=BG, highlightthickness=0, bd=0, width=s(200),
                                      height=s(PILL_HEIGHT), cursor="hand2")
        self.toast_canvas.pack()
        mid = s(PILL_HEIGHT) / 2
        self.toast_dot = self.toast_canvas.create_text(s(PILL_PAD_X), mid, text="●", font=self.font_dot, anchor="w")
        self.toast_title = self.toast_canvas.create_text(0, mid, text="", font=self.font_row, fill=TEXT, anchor="w")
        self.toast_sub = self.toast_canvas.create_text(0, mid, text="", font=self.font_meta, fill=SECONDARY,
                                                       anchor="w")
        self.toast_canvas.bind("<ButtonRelease-1>", self._toast_clicked)
        self.toast_canvas.bind("<Enter>", lambda _e: self._cancel_toast_job())
        self.toast_canvas.bind("<Leave>", lambda _e: self._schedule_toast_hide(ALERT_LINGER_MS))
        self.toast_canvas.bind("<Button-3>", lambda _e: self._hide_toast())

    def _build_menu(self) -> None:
        style = dict(tearoff=0, bg=CARD_BG, fg=TEXT, activebackground="#0a84ff", activeforeground="#ffffff",
                     bd=0, font=self.font_meta)
        self.menu_style = style
        self.row_menu = tk.Menu(self.root, **style)
        self.menu = tk.Menu(self.root, **style)
        self.corner_var = tk.StringVar(value=self.position.get("corner", self.settings.CORNER))
        snap = tk.Menu(self.menu, **style)
        for corner in VALID_CORNERS:
            snap.add_radiobutton(
                label=corner.replace("-", " ").title(), value=corner, variable=self.corner_var,
                command=lambda c=corner: self.snap_to(c),
            )
        self.menu.add_cascade(label="Snap to position", menu=snap)
        self.pinned_var = tk.BooleanVar(value=False)
        self.menu.add_checkbutton(label="Keep expanded", variable=self.pinned_var, command=self._pinned_from_menu)
        self.menu.add_command(label="Clear finished", command=self._clear)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.root.destroy)

    def _bind_drag(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)
        widget.bind("<ButtonRelease-1>", self._drag_end)
        widget.bind("<Button-3>", self._show_menu)

    def _render_rows(self, rows: list[Row]) -> None:
        """Update rows in place; rebuild only when the set or order of sessions changes."""
        shown = rows[: self.settings.MAX_ROWS]
        ids = tuple(row.id for row in shown)
        if ids != self.row_ids:
            self._hide_card()
            for view in self.row_views.values():
                view.canvas.destroy()
            self.row_views = {}
            self.footer.pack_forget()
            for row in shown:
                clickable = self._clickable(self.sessions_by_id.get(row.id))
                self.row_views[row.id] = RowView(self, self.rows_frame, row, clickable)
            self.row_ids = ids
        else:
            for row in shown:
                if self.row_views[row.id].row != row:
                    self.row_views[row.id].update(row)
        footer = ""
        if not rows:
            footer = "No active sessions"
        elif len(rows) > len(shown):
            footer = f"+{len(rows) - len(shown)} more"
        if footer != self.footer.cget("text") or bool(footer) != bool(self.footer.winfo_manager()):
            self.footer.configure(text=footer)
            if footer:
                self.footer.pack(pady=(self.px(4), self.px(2)))
            else:
                self.footer.pack_forget()

    def _render(self, rows: list[Row]) -> None:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        self.pill_header.update(counts)
        self.panel_header.update(counts)
        self._render_rows(rows)
        self._ensure_pulse()
        self.root.update_idletasks()
        pill_width = self.pill_header.group.winfo_reqwidth() + 2 * self.px(PILL_PAD_X)
        panel_width = max(self.panel_width, pill_width)
        if (panel_width - pill_width) % 2:
            pill_width += 1  # Same parity, so both windows centre on exactly the same pixel.
        self.pill_size = (pill_width, self.px(PILL_HEIGHT))
        self.panel_size = (panel_width, self.panel.winfo_reqheight())
        self._place_all()

    # Behaviour -------------------------------------------------------------------------

    def _load_rows(self) -> list[Row]:
        sessions = self.fetch_sessions()
        self._remember(sessions)
        # Elision happens by pixel width in RowView, so ask for full names here.
        rows = rows_from_sessions(sessions, 200, self.settings.STALE_RUNNING_MINUTES)
        try:
            self._check_alerts(rows)
        except Exception:
            logger.exception("Alert check failed")
        for row in rows:
            if row.id in self.dismissed and self.dismissed[row.id] != row.status:
                del self.dismissed[row.id]  # It changed since you hid it: show it again.
        return [row for row in rows if row.id not in self.dismissed]

    def refresh(self) -> None:
        try:
            if not self.fades and not self.dragging:  # Never re-layout mid-fade or mid-drag.
                rows = tuple(self._load_rows())
                if rows != self.last_rows:
                    self.last_rows = rows
                    self._render(list(rows))
        except Exception:
            logger.exception("Refresh failed; keeping last view")
        self.root.after(self.settings.POLL_MS, self.refresh)

    def refresh_now(self) -> None:
        rows = tuple(self._load_rows())
        self.last_rows = rows
        self._render(list(rows))

    # Alerts ----------------------------------------------------------------------------

    def _check_alerts(self, rows: list[Row]) -> None:
        """Compare with the previous poll and alert on: needs you, finished (after a real run), error."""
        statuses = {row.id: row.status for row in rows}
        previous, prev_sessions = self.prev_statuses, self.prev_sessions
        self.prev_statuses = statuses
        self.prev_sessions = {row_id: dict(session) for row_id, session in self.sessions_by_id.items()}
        if previous is None:
            return  # First load: everything is "new", nothing changed.
        now = time.time()
        alerts: list[tuple[str, Row, dict]] = []
        for row in rows:
            session = self.sessions_by_id.get(row.id)
            before = previous.get(row.id)
            if not session or session.get("agent") == "cowork" or before == row.status:
                continue
            if row.status == "attention":
                alerts.append(("attention", row, session))
            elif row.status in ("answered", "error") and before in ("running", "stale"):
                if session.get("interrupted"):
                    continue  # You stopped it yourself.
                prev = prev_sessions.get(row.id, {})
                ran = now - float(prev.get("status_since") or prev.get("updated_at") or now)
                if row.status == "error" or ran >= self.settings.ALERT_MIN_RUN_SECONDS:
                    alerts.append((row.status, row, session))
        if alerts:
            self._alert(alerts)

    def _alert(self, alerts: list[tuple[str, Row, dict]]) -> None:
        if not self.settings.ALERTS or not self.collapsed:
            return  # Panel open: you're already looking at it.
        others = list(self.sessions_by_id.values())
        alerts = [alert for alert in alerts if not focus.session_in_front(alert[2], others)]
        if not alerts:
            return
        priority = {"attention": 0, "error": 1, "answered": 2}
        alerts.sort(key=lambda alert: priority.get(alert[0], 9))
        kind, row, _session = alerts[0]
        sub = ALERT_TEXT.get(kind, kind)
        if len(alerts) > 1:
            sub += f"  +{len(alerts) - 1} more"
        logger.info("Alert: %s %s (%d total)", row.name, sub, len(alerts))
        sound = self.settings.ALERT_SOUND
        if sound == "all" or (sound == "attention" and kind == "attention"):
            play_alert_sound()
        self._show_toast(self.colors.get(kind, SECONDARY), row.name, sub, row.id,
                         ATTENTION_ALERT_SHOW_MS if kind == "attention" else ALERT_SHOW_MS)

    def _show_toast(self, color: str, title: str, sub: str, row_id: str, duration_ms: int) -> None:
        s = self.px
        c = self.toast_canvas
        title = elide(title, s(ALERT_TITLE_MAX), self.font_row_obj.measure)
        title_x = s(PILL_PAD_X) + self.font_dot.measure("●") + s(8)
        sub_x = title_x + self.font_row_obj.measure(title) + s(8)
        width = sub_x + self.font_meta_obj.measure(sub) + s(PILL_PAD_X)
        # Like the card: content and size change only while invisible.
        if self.alpha["toast"] > 0.01:
            self.fades.pop("toast", None)
            self.toast.attributes("-alpha", 0.0)
            self.alpha["toast"] = 0.0
        c.itemconfigure(self.toast_dot, fill=color)
        c.itemconfigure(self.toast_title, text=title)
        c.coords(self.toast_title, title_x, s(PILL_HEIGHT) / 2)
        c.itemconfigure(self.toast_sub, text=sub)
        c.coords(self.toast_sub, sub_x, s(PILL_HEIGHT) / 2)
        c.configure(width=width)
        cx, y = self._anchor()
        height = s(PILL_HEIGHT)
        below = y + self.pill_size[1] + s(ALERT_GAP)
        left, top, right, bottom = virtual_screen(self.root)
        ty = below if below + height <= bottom else y - height - s(ALERT_GAP)  # Pill at the bottom: show above.
        tx = min(max(left, cx - width // 2), right - width)
        self.toast.geometry(f"{width}x{height}+{tx}+{ty}")
        self.toast.update_idletasks()
        self.toast_row = row_id
        self._fade("toast", True)
        self._schedule_toast_hide(duration_ms)

    def _schedule_toast_hide(self, delay_ms: int) -> None:
        self._cancel_toast_job()
        if self.alpha["toast"] > 0.001 or "toast" in self.fades:
            self.toast_job = self.root.after(delay_ms, self._hide_toast)

    def _cancel_toast_job(self) -> None:
        if self.toast_job is not None:
            self.root.after_cancel(self.toast_job)
            self.toast_job = None

    def _hide_toast(self) -> None:
        self._cancel_toast_job()
        self._fade("toast", False)

    def _toast_clicked(self, _event: tk.Event) -> None:
        row_id = self.toast_row
        self._hide_toast()
        if row_id:
            self._open_session(row_id)

    # Row menu ------------------------------------------------------------------------------

    def _show_row_menu(self, event: tk.Event, row_id: str) -> None:
        session = self.sessions_by_id.get(row_id) or {}
        menu = self.row_menu
        menu.delete(0, "end")
        if self._clickable(session):
            menu.add_command(label="Open chat", command=lambda: self._open_session(row_id))
        session_id = str(session.get("session_id") or "")
        if session_id and session.get("agent") == "claude":
            menu.add_command(label="Copy resume command",
                             command=lambda: self._copy(f"claude --resume {session_id}"))
        cwd = str(session.get("cwd") or "")
        if cwd and os.path.isdir(cwd):
            menu.add_command(label="Open folder", command=lambda: self._open_folder(cwd))
        menu.add_command(label="Hide until it changes", command=lambda: self._dismiss(row_id))
        self._hide_card()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()  # Hand the text to the Windows clipboard now.

    @staticmethod
    def _open_folder(path: str) -> None:
        try:
            os.startfile(path)  # noqa: S606 - an existing local folder from the session's own cwd
        except (OSError, AttributeError):
            logger.exception("Could not open folder %s", path)

    def _dismiss(self, row_id: str) -> None:
        if self.prev_statuses and row_id in self.prev_statuses:
            self.dismissed[row_id] = self.prev_statuses[row_id]
            self.refresh_now()

    # Pulse ("needs you" breathing) -----------------------------------------------------

    def _attention_targets(self) -> tuple[list[RowView], list[tk.Label]]:
        views = [view for view in self.row_views.values() if view.row.status == "attention"]
        labels = [label for label in (self.pill_header.attention_label, self.panel_header.attention_label) if label]
        return views, labels

    def _ensure_pulse(self) -> None:
        views, labels = self._attention_targets()
        if self.settings.BLINK_ATTENTION and (views or labels) and self.pulse_job is None:
            self.pulse_job = self.root.after(PULSE_FRAME_MS, self._pulse)

    def _pulse(self) -> None:
        self.pulse_job = None
        views, labels = self._attention_targets()
        if not (views or labels):
            return  # Nothing needs attention: stop the loop until it does.
        color = mix_color(ATTENTION_DIM, self.colors["attention"], pulse_level(time.monotonic()))
        try:
            for view in views:
                view.set_dot_color(color)
            for label in labels:
                label.configure(fg=color)
        except tk.TclError:
            pass
        self.pulse_job = self.root.after(PULSE_FRAME_MS, self._pulse)

    # Placement -------------------------------------------------------------------------

    def _anchor(self) -> tuple[int, int]:
        """Top-centre point shared by the pill and the panel."""
        if self.position.get("mode") == "custom":
            center = int(self.position.get("cx", int(self.position.get("x", 0)) + self.pill_size[0] // 2))
            x, y = clamp_position(center - self.pill_size[0] // 2, int(self.position["y"]), self.pill_size,
                                  virtual_screen(self.root))
        else:
            corner = self.position.get("corner", self.settings.CORNER)
            x, y = corner_position(corner, self.pill_size, work_area(self.root), self.settings.MARGIN_PX)
        return x + self.pill_size[0] // 2, y

    def _place_all(self) -> None:
        cx, y = self._anchor()
        pw, ph = self.pill_size
        self.root.geometry(f"{pw}x{ph}+{cx - pw // 2}+{y}")
        w, h = self.panel_size
        x, py = clamp_position(cx - w // 2, y, (w, h), virtual_screen(self.root))
        self.panel.geometry(f"{w}x{h}+{x}+{py}")

    def snap_to(self, corner: str) -> None:
        self.position = {"mode": "corner", "corner": corner}
        self._save_position()
        self._place_all()

    def _keep_on_top(self) -> None:
        try:
            if below_normal_windows(self.root):
                self._repair_stacking()
            # Raise in stacking order so the pill never ends up above the panel or card.
            for window in self._windows():
                reassert_topmost(window)
        except Exception:
            logger.debug("Topmost reassert failed", exc_info=True)
        self.root.after(TOPMOST_MS, self._keep_on_top)

    def _repair_stacking(self) -> None:
        """Re-show the windows so Windows re-inserts them into the always-on-top band."""
        logger.warning("Overlay was stacked below normal windows; re-showing it")
        for window in self._windows():
            window.withdraw()
        self.root.update_idletasks()
        for window in self._windows():
            window.deiconify()
            window.attributes("-topmost", False)
            window.attributes("-topmost", True)
        self.root.update_idletasks()
        for window in self._windows():
            make_tool_window(window)
            apply_rounded_style(window)
        set_click_through(self.panel, self.alpha["panel"] < 0.01 or self.collapsed)
        set_click_through(self.card, True)
        set_click_through(self.toast, self.alpha["toast"] < 0.01)
        if below_normal_windows(self.root):
            logger.warning("Overlay still below normal windows after re-showing it")
        else:
            logger.info("Overlay stacking repaired")

    # Fades -----------------------------------------------------------------------------

    def _windows(self) -> tuple[tk.Misc, ...]:
        """All windows, bottom to top: raising them in this order keeps the stacking right."""
        return self.root, self.panel, self.card, self.toast

    def _window(self, name: str) -> tk.Misc:
        return {"panel": self.panel, "card": self.card, "toast": self.toast}[name]

    def _fade(self, name: str, visible: bool) -> None:
        """Fade a window in or out. Re-targeting mid-fade continues smoothly from the current opacity."""
        target = self.settings.OPACITY if visible else 0.0
        current = self.alpha[name]
        if abs(current - target) < 0.001 and name not in self.fades:
            return
        window = self._window(name)
        if visible:
            set_click_through(window, name == "card")  # The card is look-only; it never takes the mouse.
            reassert_topmost(window)
        else:
            set_click_through(window, True)  # Stop catching the mouse as soon as it starts hiding.
        duration = (FADE_IN_MS if visible else FADE_OUT_MS) / 1000
        span = abs(target - current) / max(self.settings.OPACITY, 0.01)
        self.fades[name] = {
            "from": current, "to": target, "start": time.perf_counter(), "duration": max(0.01, duration * span),
        }
        if self.fade_job is None:
            self._fade_tick()

    def _fade_tick(self) -> None:
        self.fade_job = None
        now = time.perf_counter()
        for name, fade in list(self.fades.items()):
            progress = min(1.0, (now - fade["start"]) / fade["duration"])
            eased = ease_out_cubic(progress) if fade["to"] > fade["from"] else ease_in_out_cubic(progress)
            alpha = fade["from"] + (fade["to"] - fade["from"]) * eased
            self.alpha[name] = alpha
            try:
                self._window(name).attributes("-alpha", alpha)
            except tk.TclError:
                pass
            if progress >= 1:
                del self.fades[name]
                if name == "panel" and fade["to"] > 0:
                    # Hide the pill under the fully shown panel, or its outline shows through the translucency.
                    self.root.attributes("-alpha", 0.0)
        if self.fades:
            self.fade_job = self.root.after(FRAME_MS, self._fade_tick)

    # Hover expand -----------------------------------------------------------------------

    def toggle_pinned(self) -> None:
        self.pinned = not self.pinned
        self.pinned_var.set(self.pinned)
        self._apply_expansion()

    def _pinned_from_menu(self) -> None:
        self.pinned = bool(self.pinned_var.get())
        self._apply_expansion()

    def _pointer_entered(self, _event: tk.Event) -> None:
        self._cancel_hover_job()
        if not self.hovered:
            self.hover_job = self.root.after(HOVER_OPEN_MS, self._hover_open)

    def _pointer_left(self, _event: tk.Event) -> None:
        # Leave also fires when moving between child widgets; the delayed check sorts that out.
        self._cancel_hover_job()
        self.hover_job = self.root.after(HOVER_CLOSE_MS, self._hover_close_check)

    def _cancel_hover_job(self) -> None:
        if self.hover_job is not None:
            self.root.after_cancel(self.hover_job)
            self.hover_job = None

    @staticmethod
    def _contains(window: tk.Misc, x: int, y: int) -> bool:
        left, top = window.winfo_rootx(), window.winfo_rooty()
        return left <= x < left + window.winfo_width() and top <= y < top + window.winfo_height()

    def _pointer_inside(self) -> bool:
        x, y = self.root.winfo_pointerxy()
        return self._contains(self.root, x, y) or (not self.collapsed and self._contains(self.panel, x, y))

    def _hover_open(self) -> None:
        self.hover_job = None
        logger.debug("Hover open: inside=%s pointer=%s", self._pointer_inside(), self.root.winfo_pointerxy())
        if self._pointer_inside():
            self.hovered = True
            self._apply_expansion()

    def _hover_close_check(self) -> None:
        self.hover_job = None
        if self.drag_offset or self._pointer_inside():
            if self.hovered or self.drag_offset:
                self.hover_job = self.root.after(HOVER_CLOSE_MS, self._hover_close_check)
            else:
                self._hover_open()
            return
        self.hovered = False
        self._apply_expansion()

    def _apply_expansion(self) -> None:
        collapsed = not (self.pinned or self.hovered)
        if collapsed == self.collapsed:
            return
        self.collapsed = collapsed
        if collapsed:
            self.root.attributes("-alpha", self.settings.OPACITY)  # Pill back underneath before the panel fades.
            self._hide_card()
            for view in self.row_views.values():
                view._set_fill(BG)
        else:
            self._hide_toast()  # The panel covers the banner's spot and shows the same news.
        self._fade("panel", not collapsed)

    def _clear(self) -> None:
        removed = self.on_clear()
        logger.info("Cleared %d finished session(s)", removed)
        self.refresh_now()

    def _show_menu(self, event: tk.Event) -> None:
        self._hide_card()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # Drag vs click -----------------------------------------------------------------------

    def _drag_start(self, event: tk.Event) -> None:
        self.drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())
        self.press_point = (event.x_root, event.y_root)
        self.dragging = False
        self.pressed_row = None  # Set right after this by a row's own press binding.

    def _drag_move(self, event: tk.Event) -> None:
        if not self.drag_offset:
            return
        if not self.dragging and self.press_point:
            moved = max(abs(event.x_root - self.press_point[0]), abs(event.y_root - self.press_point[1]))
            if moved < DRAG_THRESHOLD_PX:
                return  # Still a click, not a drag.
        self.dragging = True
        self._hide_card()
        # Move both windows together from the shared top-centre anchor.
        x, y = event.x_root - self.drag_offset[0], event.y_root - self.drag_offset[1]
        self.position = {"mode": "custom", "cx": x + self.pill_size[0] // 2, "y": y}
        self._place_all()

    def _drag_end(self, _event: tk.Event) -> None:
        if self.drag_offset and self.dragging:
            self._save_position()
        elif self.pressed_row:
            self._open_session(self.pressed_row)
        self.drag_offset = None
        self.press_point = None
        self.dragging = False
        self.pressed_row = None

    # Click to open -----------------------------------------------------------------------

    def _remember(self, sessions: list[dict]) -> None:
        self.sessions_by_id = {str(session.get("id", "")): session for session in sessions}

    @staticmethod
    def _clickable(session: dict | None) -> bool:
        return bool(session and session.get("agent") != "cowork" and session.get("pid"))

    def _open_session(self, row_id: str) -> None:
        """Bring the session's window forward; in VS Code also reveal its chat tab."""
        session = self.sessions_by_id.get(row_id)
        if not self._clickable(session):
            return
        self._hide_card()
        try:
            hwnd, in_extension = focus.focus_session(session)
        except Exception:
            logger.exception("Could not focus session %s", row_id)
            return
        # Focus changes land asynchronously, so the foreground check happens after the delay.
        if hwnd and in_extension:
            self.root.after(URI_DELAY_MS, lambda: self._reveal_vscode_chat(session, hwnd))

    def _reveal_vscode_chat(self, session: dict, hwnd: str) -> None:
        # The URI goes to the last-focused VS Code window; only send it if that's still ours.
        if not focus.is_foreground(hwnd):
            logger.info("Skipped chat reveal for %s: its window is no longer in front", session.get("session_id"))
            return
        try:
            focus.open_vscode_chat(str(session.get("session_id", "")))
            logger.info("Sent chat reveal for %s", session.get("session_id"))
        except Exception:
            logger.exception("Could not open VS Code chat %s", session.get("session_id"))

    # Hover card ----------------------------------------------------------------------------

    def _schedule_tooltip(self, view: RowView, _event: tk.Event) -> None:
        self._cancel_tooltip()
        if self.collapsed or self.dragging:
            return
        self.tooltip_job = self.root.after(TOOLTIP_DELAY_MS, lambda: self._show_card(view))

    def _show_card(self, view: RowView) -> None:
        self.tooltip_job = None
        if self.collapsed or self.dragging or self.row_views.get(view.row.id) is not view:
            return
        lines = view.row.tooltip.split("\n")
        if self.card_row != view.row.id or self.alpha["card"] < 0.01:
            # Content and size change only while the card is invisible, so nothing visibly repaints.
            self.card_title.configure(text=lines[0])
            self.card_body.configure(text="\n".join(lines[1:]))
            self.card.update_idletasks()
            width, height = self.card.winfo_reqwidth(), self.card.winfo_reqheight()
            left, _top, right, bottom = virtual_screen(self.root)
            panel_x, panel_y = self.panel.winfo_rootx(), self.panel.winfo_rooty()
            panel_w, panel_h = self.panel.winfo_width(), self.panel.winfo_height()
            x = min(max(left, panel_x + (panel_w - width) // 2), right - width)
            y = panel_y + panel_h + self.px(8)
            if y + height > bottom:
                y = panel_y - height - self.px(8)  # Panel near the bottom edge: show it above instead.
            if self.alpha["card"] > 0.01:
                self.card.attributes("-alpha", 0.0)
                self.alpha["card"] = 0.0
                self.fades.pop("card", None)
            self.card.geometry(f"{width}x{height}+{x}+{y}")
            self.card.update_idletasks()
        self.card_row = view.row.id
        self._fade("card", True)

    def _cancel_tooltip(self) -> None:
        if self.tooltip_job is not None:
            self.root.after_cancel(self.tooltip_job)
            self.tooltip_job = None
        self._fade("card", False)

    def _hide_card(self) -> None:
        self._cancel_tooltip()

    # Persistence -----------------------------------------------------------------------

    def _load_position(self) -> dict:
        try:
            data = json.loads(Path(self.settings.POSITION_FILE).read_text(encoding="utf-8"))
            if data.get("mode") in ("corner", "custom"):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return {"mode": "corner", "corner": self.settings.CORNER}

    def _save_position(self) -> None:
        try:
            path = Path(self.settings.POSITION_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.position), encoding="utf-8")
        except OSError:
            logger.warning("Could not save widget position", exc_info=True)

    def run(self) -> None:
        set_timer_resolution(True)  # 1 ms timers so fade frames land evenly.
        try:
            self.refresh_now()
            for window in self._windows():
                window.deiconify()
            self.root.update_idletasks()
            for window in self._windows():
                make_tool_window(window)
                apply_rounded_style(window)
            # Panel and card start invisible and click-through; they only ever fade.
            set_click_through(self.panel, True)
            set_click_through(self.card, True)
            set_click_through(self.toast, True)
            self._keep_on_top()
            self.root.after(self.settings.POLL_MS, self.refresh)
            self.root.mainloop()
        finally:
            set_timer_resolution(False)
