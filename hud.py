"""Always-on-top Liquid Glass context HUD for Antigravity Desktop."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import ctypes.wintypes as wt
import json
from pathlib import Path
import socket
import sys
import threading
import tkinter as tk
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont

from core import ContextMonitorService, ContextSnapshot, ConversationSummary


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "hud_config.json"
POLL_INTERVAL_MS = 1500

# Enable crisp per-pixel rendering on Windows high-DPI displays before creating Tk root.
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Palette (Liquid Glass dark translucent theme)
STATUS_OK = "#34D399"
STATUS_WARN = "#FBBF24"
STATUS_HIGH = "#F87171"

MENU_BG = "#1E222D"
MENU_HOVER = "#2D3446"
MENU_FG = "#F1F5F9"

SEG_COLORS = [
    "#9AA0A6",  # system (prompt + tools + overhead)
    "#60A5FA",  # user messages
    "#3B82F6",  # agent responses
    "#F59E0B",  # tool calls
    "#C084FC",  # customizations
]

CATEGORY_LABELS_ZH = {
    "user": "用户消息",
    "agent": "Agent 回复",
    "tool": "工具调用",
    "system_prompt": "系统提示词",
    "system_tools": "内置工具",
    "system_overhead": "系统开销",
    "rules": "规则 (Rules)",
    "skills": "技能 (Skills)",
    "subagents": "子代理",
    "legacy_workflows": "旧工作流",
    "workflows": "工作流",
    "plugins": "插件",
    "mcp_tools": "MCP 工具",
    "system_messages": "系统消息",
}

CATEGORY_COLORS = {
    "user": "#60A5FA",
    "agent": "#3B82F6",
    "tool": "#F59E0B",
    "system_prompt": "#9AA0A6",
    "system_tools": "#9AA0A6",
    "system_overhead": "#9AA0A6",
    "system_messages": "#F87171",
    "rules": "#C084FC",
    "skills": "#C084FC",
    "subagents": "#C084FC",
    "legacy_workflows": "#C084FC",
    "workflows": "#C084FC",
    "plugins": "#C084FC",
    "mcp_tools": "#C084FC",
}

# (glass_alpha in 0..255, display name) - wide steps so each level is clearly distinct
GLASS_LEVELS: tuple[tuple[int, str], ...] = (
    (42, "极透"),
    (96, "通透"),
    (156, "半砂"),
    (216, "深邃"),
)

FONT_BOLD_CANDIDATES = (
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/consolab.ttf",
    "C:/Windows/Fonts/msyhbd.ttc",
)
FONT_ZH_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/segoeui.ttf",
)
FONT_ZH_BOLD_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/segoeuib.ttf",
)


# --- Win32 Structures for Per-Pixel Alpha Layered Window ---

if sys.platform == "win32":
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    _dwmapi = ctypes.windll.dwmapi

    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TOPMOST = 0x00000008
    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0x00
    AC_SRC_ALPHA = 0x01
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD),
            ("biWidth", wt.LONG),
            ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD),
            ("biBitCount", wt.WORD),
            ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG),
            ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD),
            ("biClrImportant", wt.DWORD),
        ]

    class _BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", ctypes.c_byte),
            ("BlendFlags", ctypes.c_byte),
            ("SourceConstantAlpha", ctypes.c_byte),
            ("AlphaFormat", ctypes.c_byte),
        ]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wt.LONG), ("y", wt.LONG)]

    class _SIZE(ctypes.Structure):
        _fields_ = [("cx", wt.LONG), ("cy", wt.LONG)]


def _load_font(candidates: tuple[str, ...], px_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, max(8, px_size))
            except OSError:
                continue
    return ImageFont.load_default()


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _draw_text_crisp(
    draw: ImageDraw.ImageDraw,
    pos: tuple[float, float],
    text: str,
    fill: tuple[int, int, int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    anchor: str,
    ss: int,
) -> None:
    """Draw text with a subtle soft drop halo so text stays legible across all glass levels."""
    x, y = pos
    sh_col = (4, 8, 14, 135)
    draw.text((x - 0.35 * ss, y + 0.55 * ss), text, fill=sh_col, font=font, anchor=anchor)
    draw.text((x + 0.35 * ss, y + 0.55 * ss), text, fill=sh_col, font=font, anchor=anchor)
    draw.text((x, y + 0.9 * ss), text, fill=(4, 8, 14, 150), font=font, anchor=anchor)
    draw.text((x, y), text, fill=fill, font=font, anchor=anchor)


def _draw_chevron(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    size: float,
    up: bool,
    color: tuple[int, int, int, int],
    width: int,
) -> None:
    half_w = size * 0.48
    half_h = size * 0.25
    if up:
        pts = [(cx - half_w, cy + half_h), (cx, cy - half_h), (cx + half_w, cy + half_h)]
    else:
        pts = [(cx - half_w, cy - half_h), (cx, cy + half_h), (cx + half_w, cy - half_h)]
    draw.line(pts, fill=color, width=width, joint="curve")


def _draw_small_ring(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    diameter: float,
    snap: ContextSnapshot | None,
    accent: str,
    stroke: int,
) -> None:
    box = (x, y, x + diameter, y + diameter)
    draw.ellipse(box, outline=(255, 255, 255, 44), width=stroke)
    if snap is None or snap.limit_tokens <= 0 or snap.used_tokens <= 0:
        return

    raw_items: list[tuple[float, str]] = []
    for seg_tok, color in zip(snap.grid_segments, SEG_COLORS):
        if seg_tok > 0:
            raw_items.append(((seg_tok / snap.limit_tokens) * 360.0, color))

    target_total = min(360.0, max(5.0, (snap.used_tokens / snap.limit_tokens) * 360.0))
    if not raw_items:
        draw.arc(box, start=-90.0, end=-90.0 + target_total, fill=accent, width=stroke)
        return

    # Give tiny non-zero segments at least 2.5 deg visibility while preserving total arc proportion
    angles = [max(2.5, deg) for deg, _ in raw_items]
    total_ang = sum(angles)
    if total_ang > target_total and total_ang > 0:
        scale_f = target_total / total_ang
        angles = [a * scale_f for a in angles]

    cur = -90.0
    for (_, color), deg in zip(raw_items, angles):
        if deg <= 0.4:
            continue
        draw.arc(box, start=cur, end=cur + deg, fill=color, width=stroke)
        cur += deg


def _build_liquid_glass_base(
    w: int,
    h: int,
    radius: int,
    glass_alpha: int,
    ss: int = 2,
) -> Image.Image:
    """Create a 2x-supersampled Liquid Glass panel with zero outer artifacts and crisp rim refraction."""
    sw, sh = w * ss, h * ss
    sr = min(radius * ss, min(sw, sh) // 2)

    # Smooth vertical crystal slate gradient so opacity levels look distinct even over dark themes
    col_strip = Image.new("RGBA", (1, sh), (0, 0, 0, 0))
    cpx = col_strip.load()
    for y in range(sh):
        ny = y / max(1, sh - 1)
        if ny < 0.45:
            t = ny / 0.45
            r_c = int(68 - 28 * t)
            g_c = int(84 - 34 * t)
            b_c = int(112 - 42 * t)
        else:
            t = (ny - 0.45) / 0.55
            r_c = int(40 - 16 * t)
            g_c = int(50 - 20 * t)
            b_c = int(70 - 26 * t)
        cpx[0, y] = (r_c, g_c, b_c, glass_alpha)
    base_fill = col_strip.resize((sw, sh), Image.Resampling.BILINEAR)

    # Top curved specular gloss & bottom caustic bounce light inside the lens
    sheen = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    top_band = max(11 * ss, min(int(sh * 0.48), 34 * ss))
    gloss_peak = 72 + int(glass_alpha * 0.12)
    for y in range(1, top_band):
        t = 1.0 - (y / top_band)
        a = int(gloss_peak * (t ** 2.2))
        if a > 0:
            sd.line((0, y, sw, y), fill=(240, 248, 255, a))

    bot_band = max(5 * ss, min(int(sh * 0.20), 12 * ss))
    bounce_peak = 24 + int(glass_alpha * 0.08)
    for i in range(bot_band):
        y = sh - 1 - i
        t = 1.0 - (i / bot_band)
        a = int(bounce_peak * (t ** 1.6))
        if a > 0:
            sd.line((0, y, sw, y), fill=(170, 215, 255, a))

    # Strict AA rounded pill mask: pixels outside pill remain strictly alpha=0
    mask = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, sw - 1, sh - 1), radius=sr, fill=255)

    glass_body = Image.alpha_composite(base_fill, sheen)
    img = Image.composite(glass_body, Image.new("RGBA", (sw, sh), (0, 0, 0, 0)), mask)

    # Directional Fresnel rim inside the pill contour (bright top edge -> soft mid -> cool bottom reflection)
    rim = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rim)
    inset = max(1, ss // 2)
    rd.rounded_rectangle(
        (inset, inset, sw - 1 - inset, sh - 1 - inset),
        radius=max(1, sr - inset),
        outline=(245, 250, 255, 255),
        width=max(2, ss),
    )
    grad = Image.new("L", (1, sh), 0)
    gpx = grad.load()
    top_rim = min(220, 145 + int(glass_alpha * 0.25))
    mid_rim = 58
    bot_rim = min(145, 86 + int(glass_alpha * 0.16))
    for y in range(sh):
        ny = y / max(1, sh - 1)
        if ny < 0.26:
            val = int(top_rim - (ny / 0.26) * (top_rim - mid_rim))
        elif ny < 0.78:
            val = int(mid_rim - ((ny - 0.26) / 0.52) * 18)
        else:
            val = int(40 + ((ny - 0.78) / 0.22) * (bot_rim - 40))
        gpx[0, y] = max(0, min(255, val))
    grad = grad.resize((sw, sh), Image.Resampling.BILINEAR)
    rim.putalpha(ImageChops.multiply(rim.getchannel("A"), ImageChops.multiply(grad, mask)))
    img = Image.alpha_composite(img, rim)
    return img


def get_window_hwnd(widget: tk.Tk) -> int:
    if sys.platform != "win32":
        return 0
    wid = widget.winfo_id()
    return _user32.GetParent(wid) or wid


def disable_dwm_frame_and_corners(hwnd: int) -> None:
    """Disable Win11 DWM border/corner masks so the per-pixel alpha ellipse renders cleanly."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        DWMWA_NCRENDERING_POLICY = 2
        DWMNCRP_DISABLED = 1
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_DONOTROUND = 1
        DWMWA_BORDER_COLOR = 34
        DWMWA_COLOR_NONE = 0xFFFFFFFE

        v_nc = ctypes.c_int(DWMNCRP_DISABLED)
        _dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_NCRENDERING_POLICY, ctypes.byref(v_nc), ctypes.sizeof(v_nc))
        v_cr = ctypes.c_int(DWMWCP_DONOTROUND)
        _dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(v_cr), ctypes.sizeof(v_cr))
        v_bd = ctypes.c_uint32(DWMWA_COLOR_NONE)
        _dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(v_bd), ctypes.sizeof(v_bd))
    except Exception:
        pass


def push_layered_bitmap(hwnd: int, img: Image.Image, x: int, y: int) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False

    ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if not (ex & WS_EX_LAYERED):
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)

    w, h = img.size
    raw = bytearray(img.tobytes("raw", "BGRA"))
    for i in range(0, len(raw), 4):
        a = raw[i + 3]
        if a == 0:
            raw[i] = 0
            raw[i + 1] = 0
            raw[i + 2] = 0
        elif a < 255:
            raw[i] = (raw[i] * a) // 255
            raw[i + 1] = (raw[i + 1] * a) // 255
            raw[i + 2] = (raw[i + 2] * a) // 255

    hdc_screen = _user32.GetDC(0)
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h  # top-down DIB
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    if not hbmp:
        _gdi32.DeleteDC(hdc_mem)
        _user32.ReleaseDC(0, hdc_screen)
        return False

    ctypes.memmove(bits, bytes(raw), len(raw))
    old = _gdi32.SelectObject(hdc_mem, hbmp)

    pt_dst = _POINT(int(x), int(y))
    sz = _SIZE(w, h)
    pt_src = _POINT(0, 0)
    blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
    ok = bool(
        _user32.UpdateLayeredWindow(
            hwnd,
            hdc_screen,
            ctypes.byref(pt_dst),
            ctypes.byref(sz),
            hdc_mem,
            ctypes.byref(pt_src),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )
    )

    _gdi32.SelectObject(hdc_mem, old)
    _gdi32.DeleteObject(hbmp)
    _gdi32.DeleteDC(hdc_mem)
    _user32.ReleaseDC(0, hdc_screen)
    return ok


class ContextHUD:
    def __init__(self, start_expanded: bool = False) -> None:
        self.config = self._load_config()
        self.service = ContextMonitorService()

        self.root = tk.Tk()
        self.root.title("Antigravity Context HUD")
        self.root.overrideredirect(True)
        # Keep hidden until first per-pixel bitmap is pushed to avoid white flash
        self.root.withdraw()

        self.dpi_scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.topmost = bool(self.config.get("topmost", True))
        self.glass_alpha = self._normalize_glass_alpha(self.config.get("glass_alpha"))
        self.expanded = start_expanded
        self.pinned_conv_id: str | None = self.config.get("pinned_conversation_id")

        self.snapshot: ContextSnapshot | None = None
        self.recent_convs: list[ConversationSummary] = []
        self._error_msg: str | None = None

        self._last_render_sig: tuple[Any, ...] | None = None
        self._last_size: tuple[int, int, int] | None = None
        self._hover_id: str | None = None
        self._hit_boxes: list[tuple[str, tuple[int, int, int, int], Callable[[], None]]] = []

        self.win_x = 0
        self.win_y = 0
        self.win_w = 1
        self.win_h = 1
        self._hwnd = 0

        self._fetch_lock = threading.Lock()
        self._is_fetching = False
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._win_start_x = 0
        self._win_start_y = 0
        self._pressed_id: str | None = None
        self._dragging = False
        self._ipc_server: socket.socket | None = None

        self._bind_events()
        self._start_ipc_server()
        self._init_window()
        self._refresh_async(force=True)
        self.root.after(POLL_INTERVAL_MS, self._poll_loop)

    def _normalize_glass_alpha(self, raw: Any) -> int:
        if isinstance(raw, int):
            return min(GLASS_LEVELS, key=lambda lv: abs(lv[0] - raw))[0]
        return GLASS_LEVELS[1][0]

    def _glass_label(self) -> str:
        for alpha_val, name in GLASS_LEVELS:
            if alpha_val == self.glass_alpha:
                return name
        return GLASS_LEVELS[1][1]

    def _load_config(self) -> dict[str, Any]:
        try:
            if CONFIG_PATH.is_file():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _start_ipc_server(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(2)
            srv.settimeout(0.5)
            port = srv.getsockname()[1]
            self._ipc_server = srv
            self.config["ipc_port"] = port
            self._save_config()

            def _serve() -> None:
                while self._ipc_server is not None:
                    try:
                        conn, _ = srv.accept()
                        with conn:
                            msg = conn.recv(64)
                            if msg.startswith(b"WAKE"):
                                self.root.after(0, self._wake_window)
                    except socket.timeout:
                        continue
                    except OSError:
                        break

            threading.Thread(target=_serve, daemon=True).start()
        except OSError:
            self._ipc_server = None

    def _wake_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self._apply_topmost_flag()
        self._refresh_async(force=True)

    def _apply_topmost_flag(self) -> None:
        self.root.attributes("-topmost", self.topmost)
        if sys.platform == "win32" and self._hwnd:
            insert_after = HWND_TOPMOST if self.topmost else HWND_NOTOPMOST
            _user32.SetWindowPos(
                self._hwnd,
                insert_after,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )

    def _init_window(self) -> None:
        img, w, h, radius, hit_boxes = self._render_surface()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        saved_x = self.config.get("x")
        saved_y = self.config.get("y")
        if isinstance(saved_x, int) and isinstance(saved_y, int):
            self.win_x = max(4, min(saved_x, sw - w - 8))
            self.win_y = max(4, min(saved_y, sh - h - 48))
        else:
            self.win_x = max(12, sw - w - int(round(20 * self.dpi_scale)))
            self.win_y = int(round(10 * self.dpi_scale))

        self.root.geometry(f"{w}x{h}+{self.win_x}+{self.win_y}")
        self.root.update_idletasks()
        self._hwnd = get_window_hwnd(self.root)
        if sys.platform == "win32" and self._hwnd:
            ex = _user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
            _user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        self.root.deiconify()
        self.root.update_idletasks()
        self._hwnd = get_window_hwnd(self.root)
        disable_dwm_frame_and_corners(self._hwnd)
        self._apply_topmost_flag()
        self._commit_surface(img, w, h, radius, hit_boxes)
        self._last_render_sig = self._current_render_signature()

    def _bind_events(self) -> None:
        self.root.bind("<Motion>", self._on_mouse_move)
        self.root.bind("<Leave>", self._on_mouse_leave)
        self.root.bind("<ButtonPress-1>", self._on_press)
        self.root.bind("<B1-Motion>", self._on_drag_motion)
        self.root.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Double-Button-1>", self._on_double_click)
        self.root.bind("<Button-3>", self._show_context_menu)

    def _hit_test(self, x: int, y: int) -> tuple[str | None, Callable[[], None] | None]:
        for item_id, (x0, y0, x1, y1), cmd in self._hit_boxes:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return item_id, cmd
        return None, None

    def _on_mouse_move(self, event: tk.Event) -> None:
        if self._dragging:
            return
        hit_id, _ = self._hit_test(event.x, event.y)
        cursor = "hand2" if hit_id is not None else "arrow"
        if self.root.cget("cursor") != cursor:
            self.root.configure(cursor=cursor)
        if hit_id != self._hover_id:
            self._hover_id = hit_id
            self._render_if_needed(force=True)

    def _on_mouse_leave(self, _event: tk.Event) -> None:
        if self._hover_id is not None:
            self._hover_id = None
            self._render_if_needed(force=True)

    def _on_press(self, event: tk.Event) -> None:
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self._win_start_x = self.win_x
        self._win_start_y = self.win_y
        self._dragging = False
        hit_id, _ = self._hit_test(event.x, event.y)
        self._pressed_id = hit_id

    def _on_drag_motion(self, event: tk.Event) -> None:
        dx = event.x_root - self._drag_start_x
        dy = event.y_root - self._drag_start_y
        if not self._dragging and (abs(dx) > 4 or abs(dy) > 4):
            self._dragging = True
        if self._dragging:
            nx = self._win_start_x + dx
            ny = self._win_start_y + dy
            self.win_x = nx
            self.win_y = ny
            if sys.platform == "win32" and self._hwnd:
                _user32.SetWindowPos(
                    self._hwnd,
                    0,
                    nx,
                    ny,
                    0,
                    0,
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                )
            else:
                self.root.geometry(f"+{nx}+{ny}")

    def _on_release(self, event: tk.Event) -> None:
        was_dragging = self._dragging
        pressed = self._pressed_id
        self._dragging = False
        self._pressed_id = None
        if was_dragging:
            self.config["x"] = self.win_x
            self.config["y"] = self.win_y
            self._save_config()
            return
        if pressed is not None:
            hit_id, cmd = self._hit_test(event.x, event.y)
            if hit_id == pressed and cmd is not None:
                cmd()

    def _on_double_click(self, event: tk.Event) -> None:
        hit_id, _ = self._hit_test(event.x, event.y)
        # Avoid double-triggering if user rapidly double-clicked an explicit button
        if hit_id is None:
            self.toggle_expand()

    def _clamp_to_screen(self, w: int, h: int) -> None:
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.win_x = max(4, min(self.win_x, sw - w - 8))
        self.win_y = max(4, min(self.win_y, sh - h - 48))

    def toggle_expand(self) -> None:
        self.expanded = not self.expanded
        self._hover_id = None
        self._render_if_needed(force=True)

    def toggle_topmost(self) -> None:
        self.topmost = not self.topmost
        self._apply_topmost_flag()
        self.config["topmost"] = self.topmost
        self._save_config()
        self._render_if_needed(force=True)

    def cycle_glass_alpha(self) -> None:
        values = [lv[0] for lv in GLASS_LEVELS]
        try:
            idx = values.index(self.glass_alpha)
            self.glass_alpha = values[(idx + 1) % len(values)]
        except ValueError:
            self.glass_alpha = GLASS_LEVELS[1][0]
        self.config["glass_alpha"] = self.glass_alpha
        self._save_config()
        self._render_if_needed(force=True)

    def reset_position(self) -> None:
        self.config.pop("x", None)
        self.config.pop("y", None)
        self._save_config()
        sw = self.root.winfo_screenwidth()
        self.win_x = max(12, sw - self.win_w - int(round(20 * self.dpi_scale)))
        self.win_y = int(round(10 * self.dpi_scale))
        self._render_if_needed(force=True)

    def _select_conversation(self, conv_id: str | None) -> None:
        self.pinned_conv_id = conv_id
        self.config["pinned_conversation_id"] = conv_id
        self._save_config()
        self._last_render_sig = None
        self._refresh_async(force=True)

    def _build_session_menu(self) -> tk.Menu:
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=MENU_BG,
            fg=MENU_FG,
            activebackground=MENU_HOVER,
            activeforeground=MENU_FG,
            bd=1,
        )
        auto_prefix = "✓ " if self.pinned_conv_id is None else "   "
        menu.add_command(
            label=f"{auto_prefix}自动跟随最新活跃对话",
            command=lambda: self._select_conversation(None),
        )
        menu.add_separator()
        for conv in self.recent_convs:
            prefix = "✓ " if self.pinned_conv_id == conv.conversation_id else "   "
            label = f"{prefix}{conv.title}"
            menu.add_command(
                label=label,
                command=lambda cid=conv.conversation_id: self._select_conversation(cid),
            )
        return menu

    def _show_session_menu(self) -> None:
        self.recent_convs = self.service.list_recent_conversations(limit=8)
        menu = self._build_session_menu()
        bx, by = self.win_x + int(round(14 * self.dpi_scale)), self.win_y + self.win_h + 2
        for item_id, (x0, _, _, y1), _ in self._hit_boxes:
            if item_id == "session":
                bx = self.win_x + x0
                by = self.win_y + y1 + 4
                break
        menu.tk_popup(bx, by)

    def _show_context_menu(self, event: tk.Event) -> None:
        self.recent_convs = self.service.list_recent_conversations(limit=8)
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=MENU_BG,
            fg=MENU_FG,
            activebackground=MENU_HOVER,
            activeforeground=MENU_FG,
            bd=1,
        )
        menu.add_command(
            label="收起小条" if self.expanded else "展开详情",
            command=self.toggle_expand,
        )
        menu.add_command(label="立即刷新", command=lambda: self._refresh_async(force=True))
        menu.add_cascade(label="切换监控对话", menu=self._build_session_menu())
        menu.add_separator()
        menu.add_command(
            label=f"{'✓ ' if self.topmost else ''}始终置顶",
            command=self.toggle_topmost,
        )
        menu.add_command(
            label=f"切换玻璃浓度 ({self._glass_label()})",
            command=self.cycle_glass_alpha,
        )
        menu.add_command(label="复位到屏幕右上角", command=self.reset_position)
        menu.add_separator()
        menu.add_command(label="退出悬浮窗", command=self.quit_app)
        menu.tk_popup(event.x_root, event.y_root)

    def _poll_loop(self) -> None:
        self._refresh_async(force=False)
        self.root.after(POLL_INTERVAL_MS, self._poll_loop)

    def _refresh_async(self, force: bool = False) -> None:
        with self._fetch_lock:
            if self._is_fetching and not force:
                return
            self._is_fetching = True

        def _worker() -> None:
            try:
                snap = self.service.get_snapshot(self.pinned_conv_id)
                self.root.after(0, lambda s=snap: self._apply_snapshot(s))
            except Exception as exc:
                self.root.after(0, lambda e=str(exc): self._apply_error(e))
            finally:
                with self._fetch_lock:
                    self._is_fetching = False

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_snapshot(self, snap: ContextSnapshot | None) -> None:
        self._error_msg = None
        self.snapshot = snap
        self._render_if_needed(force=False)

    def _apply_error(self, message: str) -> None:
        self._error_msg = message
        self._render_if_needed(force=False)

    def _current_render_signature(self) -> tuple[Any, ...]:
        snap = self.snapshot
        snap_sig: tuple[Any, ...]
        if self._error_msg is not None:
            snap_sig = ("ERR", self._error_msg)
        elif snap is None:
            snap_sig = ("NONE",)
        else:
            snap_sig = (
                snap.conversation_id,
                snap.used_tokens,
                snap.limit_tokens,
                snap.compacted,
                snap.has_breakdown,
                tuple(snap.grid_segments),
                tuple((c["key"], c["tokens"], c["pct"]) for c in snap.categories),
                snap.checkpoint_view.get("active_number") if snap.checkpoint_view else None,
                snap.checkpoint_buffer,
            )
        return (
            snap_sig,
            self.expanded,
            self.topmost,
            self.glass_alpha,
            self.pinned_conv_id,
            self._hover_id,
        )

    def _render_if_needed(self, force: bool = False) -> None:
        sig = self._current_render_signature()
        if not force and sig == self._last_render_sig:
            return
        self._last_render_sig = sig

        img, w, h, radius, hit_boxes = self._render_surface()
        self._commit_surface(img, w, h, radius, hit_boxes)

    def _commit_surface(
        self,
        img: Image.Image,
        w: int,
        h: int,
        radius: int,
        hit_boxes: list[tuple[str, tuple[int, int, int, int], Callable[[], None]]],
    ) -> None:
        self._hit_boxes = hit_boxes
        self._clamp_to_screen(w, h)
        size_changed = (w, h, radius) != self._last_size
        self.win_w = w
        self.win_h = h

        if size_changed:
            self.root.geometry(f"{w}x{h}+{self.win_x}+{self.win_y}")
            self.root.update_idletasks()
            self._hwnd = get_window_hwnd(self.root)

        push_layered_bitmap(self._hwnd, img, self.win_x, self.win_y)

        if size_changed:
            disable_dwm_frame_and_corners(self._hwnd)
            self._last_size = (w, h, radius)

    def _accent_for_snapshot(self, snap: ContextSnapshot | None) -> str:
        if self._error_msg is not None:
            return STATUS_HIGH
        if snap is None:
            return "#94A3B8"
        if snap.compacted or snap.used_pct >= 80.0:
            return STATUS_HIGH
        if snap.used_pct >= 50.0:
            return STATUS_WARN
        return STATUS_OK

    def _header_texts(self, snap: ContextSnapshot | None) -> tuple[str, str, bool]:
        if self._error_msg is not None:
            return "读取失败", "", False
        if snap is None:
            return "等待对话", "", False
        approx = "~" if not snap.has_breakdown else ""
        main_txt = f"{approx}{snap.fmt_used} / {snap.fmt_limit}"
        pct_txt = f"{snap.used_pct:.1f}%"
        return main_txt, pct_txt, snap.compacted

    def _render_surface(
        self,
    ) -> tuple[Image.Image, int, int, int, list[tuple[str, tuple[int, int, int, int], Callable[[], None]]]]:
        if self.expanded:
            return self._render_expanded_card()
        return self._render_collapsed_pill()

    def _render_collapsed_pill(
        self,
    ) -> tuple[Image.Image, int, int, int, list[tuple[str, tuple[int, int, int, int], Callable[[], None]]]]:
        ss = 2
        s = self.dpi_scale
        snap = self.snapshot
        accent = self._accent_for_snapshot(snap)
        accent_rgba = (*_hex_to_rgb(accent), 255)
        main_txt, pct_txt, is_compacted = self._header_texts(snap)

        f_num = _load_font(FONT_BOLD_CANDIDATES, int(round(11.5 * s * ss)))
        f_zh = _load_font(FONT_ZH_BOLD_CANDIDATES, int(round(11 * s * ss)))
        f_tag = _load_font(FONT_ZH_CANDIDATES, int(round(8.5 * s * ss)))
        use_zh_main = self._error_msg is not None or snap is None
        main_font = f_zh if use_zh_main else f_num

        tmp = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        main_w = tmp.textlength(main_txt, font=main_font)
        pct_w = tmp.textlength(pct_txt, font=f_num) if pct_txt else 0.0
        tag_pad_x = int(round(5 * s * ss))
        tag_w = int(round(tmp.textlength("已压缩", font=f_tag) + tag_pad_x * 2)) if is_compacted else 0

        pad_l = int(round(10.5 * s * ss))
        pad_r = int(round(10 * s * ss))
        ring_d = int(round(14.5 * s * ss))
        stroke = max(3, int(round(2.4 * s * ss)))
        gap_ring = int(round(7.5 * s * ss))
        gap_pct = int(round(7 * s * ss)) if pct_txt else 0
        gap_tag = int(round(6 * s * ss)) if is_compacted else 0
        gap_chev = int(round(7.5 * s * ss))
        chev_w = int(round(8.5 * s * ss))

        sw = int(round(pad_l + ring_d + gap_ring + main_w + gap_pct + pct_w + gap_tag + tag_w + gap_chev + chev_w + pad_r))
        sh = int(round(27 * s * ss))
        w = (sw + ss - 1) // ss
        h = (sh + ss - 1) // ss
        radius = h // 2

        panel = _build_liquid_glass_base(w, h, radius=radius, glass_alpha=self.glass_alpha, ss=ss)
        d = ImageDraw.Draw(panel)
        sw, sh = panel.size
        cy = sh / 2.0

        # Tiny segmented ring
        rx = float(pad_l)
        ry = (sh - ring_d) / 2.0
        _draw_small_ring(d, rx, ry, float(ring_d), snap if self._error_msg is None else None, accent, stroke)

        # Primary numbers
        tx = rx + ring_d + gap_ring
        main_fill = (248, 250, 252, 255) if self._error_msg is None else (252, 165, 165, 255)
        _draw_text_crisp(d, (tx, cy), main_txt, main_fill, main_font, "lm", ss)
        cur_x = tx + main_w

        if pct_txt:
            cur_x += gap_pct
            _draw_text_crisp(d, (cur_x, cy), pct_txt, accent_rgba, f_num, "lm", ss)
            cur_x += pct_w

        if is_compacted:
            cur_x += gap_tag
            th = int(round(14 * s * ss))
            ty0 = (sh - th) // 2
            d.rounded_rectangle(
                (cur_x, ty0, cur_x + tag_w, ty0 + th),
                radius=th // 2,
                fill=(248, 113, 113, 46),
                outline=(248, 113, 113, 130),
                width=max(1, ss // 2),
            )
            _draw_text_crisp(d, (cur_x + tag_w / 2.0, cy), "已压缩", (254, 202, 202, 245), f_tag, "mm", ss)

        # Toggle chevron button with subtle hover pill highlight
        chev_cx = sw - pad_r - chev_w / 2.0
        if self._hover_id == "toggle":
            hr = int(round(9.5 * s * ss))
            d.ellipse(
                (chev_cx - hr, cy - hr, chev_cx + hr, cy + hr),
                fill=(255, 255, 255, 42),
            )
        chev_col = (248, 250, 252, 250) if self._hover_id == "toggle" else (218, 226, 238, 220)
        _draw_chevron(d, chev_cx, cy, float(chev_w), up=False, color=chev_col, width=max(2, int(round(1.5 * s * ss))))

        out_img = panel.resize((w, h), Image.Resampling.LANCZOS)
        hit_x0 = max(0, int((sw - pad_r - chev_w - int(round(10 * s * ss))) // ss))
        hits = [("toggle", (hit_x0, 0, w, h), self.toggle_expand)]
        return out_img, w, h, radius, hits

    def _render_expanded_card(
        self,
    ) -> tuple[Image.Image, int, int, int, list[tuple[str, tuple[int, int, int, int], Callable[[], None]]]]:
        ss = 2
        s = self.dpi_scale
        snap = self.snapshot
        accent = self._accent_for_snapshot(snap)
        accent_rgba = (*_hex_to_rgb(accent), 255)
        main_txt, pct_txt, is_compacted = self._header_texts(snap)

        f_num = _load_font(FONT_BOLD_CANDIDATES, int(round(11.5 * s * ss)))
        f_zh = _load_font(FONT_ZH_CANDIDATES, int(round(10 * s * ss)))
        f_zh_bold = _load_font(FONT_ZH_BOLD_CANDIDATES, int(round(10.5 * s * ss)))
        f_zh_sm = _load_font(FONT_ZH_CANDIDATES, int(round(8.8 * s * ss)))
        f_mono = _load_font(FONT_BOLD_CANDIDATES, int(round(9.8 * s * ss)))

        pad_x = int(round(13 * s * ss))
        pad_y = int(round(8 * s * ss))
        ring_d = int(round(14.5 * s * ss))
        stroke = max(3, int(round(2.4 * s * ss)))
        gap_ring = int(round(7.5 * s * ss))
        gap_pct = int(round(7 * s * ss)) if pct_txt else 0
        chev_w = int(round(8.5 * s * ss))

        header_h = int(round(28 * s * ss))
        row_h = int(round(19.5 * s * ss))
        footer_h = int(round(30 * s * ss))

        cr = self.service.cr
        rows: list[tuple[str, str, str, str, bool]] = []  # label, tok_str, pct_str, color, hollow
        note_line = ""

        if self._error_msg is not None:
            note_line = f"读取错误: {self._error_msg[:36]}"
        elif snap is None:
            note_line = "在 Antigravity 发起对话后自动显示"
        else:
            for cat in snap.categories:
                if cat["tokens"] <= 0 and len(snap.categories) > 4:
                    continue
                label_zh = CATEGORY_LABELS_ZH.get(cat["key"], cat["name"])
                col = CATEGORY_COLORS.get(cat["key"], "#CBD5E1")
                rows.append((label_zh, cr.fmt_tok(cat["tokens"]), f"{cat['pct']:.1f}%", col, False))
            free_pct = max(0.0, 100.0 - snap.used_pct)
            rows.append(("剩余空间", snap.fmt_free, f"{free_pct:.1f}%", "#94A3B8", True))

            if snap.checkpoint_view:
                ck = snap.checkpoint_view
                note_line = f"检查点 #{ck['active_number']} (共 {ck['count']}) · 缓冲 {cr.fmt_tok(snap.checkpoint_buffer)}"
            elif is_compacted:
                note_line = "已触发上下文压缩"

        note_h = int(round(17 * s * ss)) if note_line else 0
        mid_gap = int(round(8 * s * ss)) if rows or note_line else 0
        pre_footer_gap = int(round(7 * s * ss))

        w = int(round(248 * s))
        sh = pad_y + header_h + int(round(3 * s * ss)) + mid_gap + len(rows) * row_h + note_h + pre_footer_gap + footer_h + pad_y
        h = (sh + ss - 1) // ss
        radius = int(round(15 * s))

        panel = _build_liquid_glass_base(w, h, radius=radius, glass_alpha=self.glass_alpha, ss=ss)
        d = ImageDraw.Draw(panel)
        sw, sh = panel.size

        # --- Header row ---
        h_cy = pad_y + header_h / 2.0
        _draw_small_ring(
            d,
            float(pad_x),
            h_cy - ring_d / 2.0,
            float(ring_d),
            snap if self._error_msg is None else None,
            accent,
            stroke,
        )
        tx = pad_x + ring_d + gap_ring
        main_font = f_zh_bold if (self._error_msg is not None or snap is None) else f_num
        main_fill = (248, 250, 252, 255) if self._error_msg is None else (252, 165, 165, 255)
        _draw_text_crisp(d, (tx, h_cy), main_txt, main_fill, main_font, "lm", ss)
        m_w = d.textlength(main_txt, font=main_font)
        if pct_txt:
            _draw_text_crisp(d, (tx + m_w + gap_pct, h_cy), pct_txt, accent_rgba, f_num, "lm", ss)

        chev_cx = sw - pad_x - chev_w / 2.0
        if self._hover_id == "toggle":
            hr = int(round(10 * s * ss))
            d.ellipse((chev_cx - hr, h_cy - hr, chev_cx + hr, h_cy + hr), fill=(255, 255, 255, 42))
        chev_col = (248, 250, 252, 250) if self._hover_id == "toggle" else (218, 226, 238, 220)
        _draw_chevron(d, chev_cx, h_cy, float(chev_w), up=True, color=chev_col, width=max(2, int(round(1.5 * s * ss))))

        # Divider
        div_y = pad_y + header_h + int(round(2 * s * ss))
        d.line((pad_x, div_y, sw - pad_x, div_y), fill=(255, 255, 255, 38), width=max(1, ss // 2))

        # --- Breakdown rows ---
        cur_y = div_y + mid_gap
        dot_r = int(round(3.2 * s * ss))
        pct_col_right = sw - pad_x
        tok_col_right = sw - pad_x - int(round(44 * s * ss))

        for name, tok_s, pct_s, col_hex, hollow in rows:
            rmid = cur_y + row_h / 2.0
            rgb = _hex_to_rgb(col_hex)
            if hollow:
                d.ellipse(
                    (pad_x, rmid - dot_r, pad_x + dot_r * 2, rmid + dot_r),
                    outline=(*rgb, 210),
                    width=max(2, ss),
                )
            else:
                d.ellipse(
                    (pad_x, rmid - dot_r, pad_x + dot_r * 2, rmid + dot_r),
                    fill=(*rgb, 245),
                )
            label_col = (196, 208, 226, 235) if hollow else (238, 242, 250, 250)
            tok_col = (212, 222, 236, 245) if hollow else (248, 250, 252, 255)
            _draw_text_crisp(d, (pad_x + dot_r * 2 + int(round(8 * s * ss)), rmid), name, label_col, f_zh, "lm", ss)
            _draw_text_crisp(d, (tok_col_right, rmid), tok_s, tok_col, f_mono, "rm", ss)
            _draw_text_crisp(d, (pct_col_right, rmid), pct_s, (186, 198, 218, 240), f_mono, "rm", ss)
            cur_y += row_h

        if note_line:
            _draw_text_crisp(
                d,
                (pad_x, cur_y + note_h / 2.0),
                note_line,
                (196, 208, 226, 235),
                f_zh_sm,
                "lm",
                ss,
            )
            cur_y += note_h

        cur_y += pre_footer_gap

        # --- Footer glass chips ---
        hits: list[tuple[str, tuple[int, int, int, int], Callable[[], None]]] = [
            ("toggle", (max(0, int((sw - pad_x - int(round(26 * s * ss))) // ss)), 0, w, int((div_y) // ss)), self.toggle_expand),
        ]

        ch_h = int(round(22 * s * ss))
        cy0 = cur_y + (footer_h - ch_h) // 2

        def _draw_chip(
            item_id: str,
            x0: int,
            cw: int,
            label: str,
            cmd: Callable[[], None],
            active: bool = False,
            danger: bool = False,
        ) -> None:
            hovered = self._hover_id == item_id
            if danger:
                bg = (78, 24, 34, 195 if hovered else 155)
                bd = (248, 113, 113, 210 if hovered else 145)
                fg = (254, 214, 214, 255)
            elif active:
                bg = (14, 66, 54, 195 if hovered else 155)
                bd = (52, 211, 153, 215 if hovered else 145)
                fg = (176, 246, 214, 255)
            else:
                bg = (14, 18, 30, 188 if hovered else 142)
                bd = (255, 255, 255, 155 if hovered else 76)
                fg = (248, 250, 252, 255 if hovered else 242)

            d.rounded_rectangle(
                (x0, cy0, x0 + cw, cy0 + ch_h),
                radius=ch_h // 2,
                fill=bg,
                outline=bd,
                width=max(1, ss // 2),
            )
            _draw_text_crisp(d, (x0 + cw / 2.0, cy0 + ch_h / 2.0), label, fg, f_zh_sm, "mm", ss)
            hits.append((item_id, (x0 // ss, cy0 // ss, (x0 + cw) // ss, (cy0 + ch_h) // ss), cmd))

        sess_lbl = "锁定会话" if self.pinned_conv_id else "自动跟随"
        sess_w = int(round(d.textlength(sess_lbl, font=f_zh_sm) + 16 * s * ss))
        _draw_chip("session", pad_x, sess_w, sess_lbl, self._show_session_menu, active=bool(self.pinned_conv_id))

        rx_end = sw - pad_x
        right_chips = [
            ("quit", "退出", self.quit_app, False, True),
            ("glass", self._glass_label(), self.cycle_glass_alpha, False, False),
            ("topmost", "置顶" if self.topmost else "未置顶", self.toggle_topmost, self.topmost, False),
        ]
        chip_gap = int(round(5 * s * ss))
        for cid, lbl, cmd, is_act, is_dng in right_chips:
            cw = int(round(d.textlength(lbl, font=f_zh_sm) + 14 * s * ss))
            rx_end -= cw
            _draw_chip(cid, rx_end, cw, lbl, cmd, active=is_act, danger=is_dng)
            rx_end -= chip_gap

        out_img = panel.resize((w, h), Image.Resampling.LANCZOS)
        return out_img, w, h, radius, hits

    def quit_app(self) -> None:
        if self._ipc_server is not None:
            try:
                self._ipc_server.close()
            except OSError:
                pass
            self._ipc_server = None
        if "ipc_port" in self.config:
            self.config.pop("ipc_port", None)
            self._save_config()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def try_wake_existing_instance() -> bool:
    """If an instance is already running on its saved IPC port, wake it and return True."""
    try:
        if not CONFIG_PATH.is_file():
            return False
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        port = int(data.get("ipc_port") or 0)
        if not port:
            return False
        with socket.create_connection(("127.0.0.1", port), timeout=0.4) as sock:
            sock.sendall(b"WAKE")
        return True
    except Exception:
        return False


def main() -> None:
    if try_wake_existing_instance():
        return
    start_expanded = "--expanded" in sys.argv
    app = ContextHUD(start_expanded=start_expanded)
    app.run()


if __name__ == "__main__":
    main()
