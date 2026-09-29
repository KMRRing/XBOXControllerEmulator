"""overlay — the transparent pad drawn over the game. Win32 through ctypes, one thread, one window.

What makes it behave like a controller rather than like a window:

    WS_EX_NOACTIVATE + WM_POINTERACTIVATE/WM_MOUSEACTIVATE answered "no activate"
        touching the pad never takes focus from the game, so the game keeps running and keeps listening
    WS_EX_LAYERED with a colour key
        every pixel painted the key colour is not there at all — it is neither drawn nor hit-tested, so a
        touch anywhere off the controls lands on the game underneath; the controls themselves are drawn at
        the chosen opacity
    WM_POINTER messages
        each finger arrives with its own id, so a thumb on each stick and a third finger on a button are
        three independent inputs; touch pointers are captured by the window they went down on, so a thumb
        can wander off the stick's drawing without losing it
    WS_EX_TOPMOST, re-asserted every two seconds
        a borderless game that makes itself topmost does not bury the pad
    per-monitor DPI awareness on this thread
        coordinates are physical pixels, so at 200 % scaling on a Surface the pad lands where it is drawn

Exclusive fullscreen is the one thing no overlay can draw over: games run in borderless / windowed fullscreen.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
import urllib.request
from ctypes import wintypes

import pad as padlib
import vigem

WM_DESTROY, WM_CLOSE, WM_PAINT, WM_ERASEBKGND = 0x0002, 0x0010, 0x000F, 0x0014
WM_MOUSEACTIVATE, WM_DISPLAYCHANGE, WM_TIMER = 0x0021, 0x007E, 0x0113
WM_POINTERUPDATE, WM_POINTERDOWN, WM_POINTERUP = 0x0245, 0x0246, 0x0247
WM_POINTERACTIVATE, WM_POINTERCAPTURECHANGED = 0x024B, 0x024C
WM_TABLET_QUERYSYSTEMGESTURESTATUS, WM_DPICHANGED = 0x02CC, 0x02E0
WM_APP_RELOAD = 0x8000 + 1
MA_NOACTIVATE = PA_NOACTIVATE = 3
POINTER_FLAG_INCONTACT = 0x0004
PT_MOUSE = 4

KEY = 0x00FF00FF                                    # COLORREF of magenta: the pixels that are not there
WHITE_TEXT = 0x00E8E4E1


def rgb(r: int, g: int, b: int) -> int:
    return r | (g << 8) | (b << 16)


FILL, EDGE, KNOB = rgb(30, 32, 38), rgb(205, 210, 218), rgb(78, 82, 92)
LIVE, HOLD = rgb(16, 124, 16), rgb(200, 120, 20)    # pressed: Xbox green; the menu handle held: amber
TEXT = {"a": rgb(120, 200, 70), "b": rgb(235, 80, 70), "x": rgb(70, 140, 240), "y": rgb(245, 195, 30)}

LRESULT = ctypes.c_ssize_t
WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(   # CFUNCTYPE only so the module imports off Windows
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


_api = None


def api() -> dict:
    """The Win32 surface this file uses, typed. Missing optional calls (old Windows, Wine) come back None."""
    global _api
    if _api is not None:
        return _api
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    g32 = ctypes.WinDLL("gdi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    W, B, U, I, D, P = wintypes.HWND, wintypes.BOOL, wintypes.UINT, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p
    HDC = wintypes.HDC

    def bind(dll, name, res, *args, optional=False):
        try:
            f = getattr(dll, name)
        except AttributeError:
            if optional:
                return None
            raise
        f.restype, f.argtypes = res, list(args)
        return f

    _api = {
        "GetModuleHandleW": bind(k32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR),
        "RegisterClassExW": bind(u32, "RegisterClassExW", wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW)),
        "CreateWindowExW": bind(u32, "CreateWindowExW", W, D, wintypes.LPCWSTR, wintypes.LPCWSTR, D, I, I, I, I,
                                W, wintypes.HMENU, wintypes.HINSTANCE, P),
        "DefWindowProcW": bind(u32, "DefWindowProcW", LRESULT, W, U, wintypes.WPARAM, wintypes.LPARAM),
        "DestroyWindow": bind(u32, "DestroyWindow", B, W),
        "ShowWindow": bind(u32, "ShowWindow", B, W, I),
        "SetWindowPos": bind(u32, "SetWindowPos", B, W, W, I, I, I, I, U),
        "SetLayeredWindowAttributes": bind(u32, "SetLayeredWindowAttributes", B, W, wintypes.COLORREF, ctypes.c_ubyte, D),
        "GetMessageW": bind(u32, "GetMessageW", B, ctypes.POINTER(wintypes.MSG), W, U, U),
        "TranslateMessage": bind(u32, "TranslateMessage", B, ctypes.POINTER(wintypes.MSG)),
        "DispatchMessageW": bind(u32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG)),
        "PostMessageW": bind(u32, "PostMessageW", B, W, U, wintypes.WPARAM, wintypes.LPARAM),
        "PostQuitMessage": bind(u32, "PostQuitMessage", None, I),
        "SetTimer": bind(u32, "SetTimer", ctypes.c_size_t, W, ctypes.c_size_t, U, P),
        "KillTimer": bind(u32, "KillTimer", B, W, ctypes.c_size_t),
        "InvalidateRect": bind(u32, "InvalidateRect", B, W, ctypes.POINTER(wintypes.RECT), B),
        "BeginPaint": bind(u32, "BeginPaint", HDC, W, ctypes.POINTER(PAINTSTRUCT)),
        "EndPaint": bind(u32, "EndPaint", B, W, ctypes.POINTER(PAINTSTRUCT)),
        "GetDC": bind(u32, "GetDC", HDC, W),
        "ReleaseDC": bind(u32, "ReleaseDC", I, W, HDC),
        "FillRect": bind(u32, "FillRect", I, HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH),
        "DrawTextW": bind(u32, "DrawTextW", I, HDC, wintypes.LPCWSTR, I, ctypes.POINTER(wintypes.RECT), U),
        "MonitorFromPoint": bind(u32, "MonitorFromPoint", wintypes.HMONITOR, wintypes.POINT, D),
        "GetMonitorInfoW": bind(u32, "GetMonitorInfoW", B, wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)),
        "SetCapture": bind(u32, "SetCapture", W, W),
        "ReleaseCapture": bind(u32, "ReleaseCapture", B),
        "GetPointerType": bind(u32, "GetPointerType", B, wintypes.UINT, ctypes.POINTER(wintypes.DWORD), optional=True),
        "EnableMouseInPointer": bind(u32, "EnableMouseInPointer", B, B, optional=True),
        "SetWindowFeedbackSetting": bind(u32, "SetWindowFeedbackSetting", B, W, I, D, wintypes.UINT, P, optional=True),
        "SetThreadDpiAwarenessContext": bind(u32, "SetThreadDpiAwarenessContext", P, P, optional=True),
        "SendInput": bind(u32, "SendInput", U, U, P, I),
        "GetCursorPos": bind(u32, "GetCursorPos", B, ctypes.POINTER(wintypes.POINT)),
        "GetSystemMetrics": bind(u32, "GetSystemMetrics", I, I),
        "WindowFromPoint": bind(u32, "WindowFromPoint", W, wintypes.POINT),
        "MapVirtualKeyW": bind(u32, "MapVirtualKeyW", U, U, U),
        "GetWindowLongPtrW": bind(u32, "GetWindowLongPtrW" if ctypes.sizeof(P) == 8 else "GetWindowLongW", ctypes.c_ssize_t, W, I),
        "SetWindowLongPtrW": bind(u32, "SetWindowLongPtrW" if ctypes.sizeof(P) == 8 else "SetWindowLongW", ctypes.c_ssize_t, W, I, ctypes.c_ssize_t),
        "SetProcessDPIAware": bind(u32, "SetProcessDPIAware", B, optional=True),
        "CreateCompatibleDC": bind(g32, "CreateCompatibleDC", HDC, HDC),
        "CreateCompatibleBitmap": bind(g32, "CreateCompatibleBitmap", wintypes.HBITMAP, HDC, I, I),
        "SelectObject": bind(g32, "SelectObject", wintypes.HGDIOBJ, HDC, wintypes.HGDIOBJ),
        "DeleteObject": bind(g32, "DeleteObject", B, wintypes.HGDIOBJ),
        "DeleteDC": bind(g32, "DeleteDC", B, HDC),
        "BitBlt": bind(g32, "BitBlt", B, HDC, I, I, I, I, HDC, I, I, D),
        "CreateSolidBrush": bind(g32, "CreateSolidBrush", wintypes.HBRUSH, wintypes.COLORREF),
        "CreatePen": bind(g32, "CreatePen", wintypes.HPEN, I, I, wintypes.COLORREF),
        "Ellipse": bind(g32, "Ellipse", B, HDC, I, I, I, I),
        "RoundRect": bind(g32, "RoundRect", B, HDC, I, I, I, I, I, I),
        "Rectangle": bind(g32, "Rectangle", B, HDC, I, I, I, I),
        "SetBkMode": bind(g32, "SetBkMode", I, HDC, I),
        "SetTextColor": bind(g32, "SetTextColor", wintypes.COLORREF, HDC, wintypes.COLORREF),
        "CreateFontW": bind(g32, "CreateFontW", wintypes.HFONT, I, I, I, I, I, D, D, D, D, D, D, D, D, wintypes.LPCWSTR),
    }
    return _api


def _xy(lparam: int) -> tuple:
    return ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value


# --------------------------------------------------------------------------------- what the trackpad and the keys inject
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSE_MOVE, MOUSE_WHEEL = 0x0001, 0x0800
MOUSE_ABSOLUTE, MOUSE_VIRTUALDESK = 0x8000, 0x4000
MOUSE_DOWN = {"left": 0x0002, "right": 0x0008}
MOUSE_UP = {"left": 0x0004, "right": 0x0010}
KEY_EXTENDED, KEY_UP = 0x0001, 0x0002
WS_EX_TRANSPARENT, GWL_EXSTYLE = 0x00000020, -20


def mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    i = INPUT()
    i.type = INPUT_MOUSE
    i.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return i


def key_input(name: str, up: bool) -> INPUT:
    vk = padlib.KEYS[name]
    i = INPUT()
    i.type = INPUT_KEYBOARD
    scan = api()["MapVirtualKeyW"](vk, 0)                       # MAPVK_VK_TO_VSC: the key as a keyboard sends it
    i.u.ki = KEYBDINPUT(vk, scan, (KEY_EXTENDED if name in padlib.EXTENDED else 0) | (KEY_UP if up else 0), 0, 0)
    return i


def cursor_to(x: int, y: int) -> INPUT:
    """An absolute move, so Windows' own pointer acceleration does not double what the trackpad already did.
    Absolute coordinates run 0..65535 across the whole virtual desktop."""
    a = api()
    left, top = a["GetSystemMetrics"](76), a["GetSystemMetrics"](77)          # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN
    width, height = max(1, a["GetSystemMetrics"](78)), max(1, a["GetSystemMetrics"](79))
    x = min(left + width - 1, max(left, x))
    y = min(top + height - 1, max(top, y))
    return mouse_input(MOUSE_MOVE | MOUSE_ABSOLUTE | MOUSE_VIRTUALDESK,
                       (x - left) * 65535 // (width - 1 or 1), (y - top) * 65535 // (height - 1 or 1))


def inputs_for(events: list) -> list:
    """The pad's events as the INPUT records SendInput takes, in order. A click is a down and an up; the moves
    are added up and sent as one absolute position from where the cursor is now."""
    out = []
    dx = dy = 0
    for e in events:
        kind = e[0]
        if kind == "move":
            dx, dy = dx + e[1], dy + e[2]
            continue
        if dx or dy:
            pt = wintypes.POINT()
            api()["GetCursorPos"](ctypes.byref(pt))
            out.append(cursor_to(pt.x + dx, pt.y + dy))
            dx = dy = 0
        if kind == "wheel":
            out.append(mouse_input(MOUSE_WHEEL, data=e[1] * 120))
        elif kind == "down":
            out.append(mouse_input(MOUSE_DOWN[e[1]]))
        elif kind == "up":
            out.append(mouse_input(MOUSE_UP[e[1]]))
        elif kind == "click":
            out.append(mouse_input(MOUSE_DOWN[e[1]]))
            out.append(mouse_input(MOUSE_UP[e[1]]))
        elif kind == "keydown":
            out.append(key_input(e[1], False))
        elif kind == "keyup":
            out.append(key_input(e[1], True))
    if dx or dy:
        pt = wintypes.POINT()
        api()["GetCursorPos"](ctypes.byref(pt))
        out.append(cursor_to(pt.x + dx, pt.y + dy))
    return out


def send_inputs(records: list) -> None:
    if not records:
        return
    arr = (INPUT * len(records))(*records)
    api()["SendInput"](len(records), ctypes.byref(arr), ctypes.sizeof(INPUT))


class Painter:
    """GDI into an off-screen bitmap the size of the monitor. Brushes, pens and fonts are made once and kept."""

    def __init__(self, width: int, height: int):
        a = api()
        self.a, self.width, self.height = a, width, height
        screen = a["GetDC"](None)
        self.dc = a["CreateCompatibleDC"](screen)
        self.bitmap = a["CreateCompatibleBitmap"](screen, width, height)
        a["ReleaseDC"](None, screen)
        self.old = a["SelectObject"](self.dc, self.bitmap)
        a["SetBkMode"](self.dc, 1)                      # TRANSPARENT: text leaves the fill underneath alone
        self.brushes, self.pens, self.fonts = {}, {}, {}
        self.line = max(2, int(min(width, height) * 0.004))

    def brush(self, colour: int):
        if colour not in self.brushes:
            self.brushes[colour] = self.a["CreateSolidBrush"](colour)
        return self.brushes[colour]

    def pen(self, colour: int):
        if colour not in self.pens:
            self.pens[colour] = self.a["CreatePen"](0, self.line, colour)
        return self.pens[colour]

    def font(self, px: int):
        px = max(10, int(px))
        if px not in self.fonts:                        # 600 weight, NONANTIALIASED_QUALITY: no pink fringe
            self.fonts[px] = self.a["CreateFontW"](-px, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 3, 0, "Segoe UI")
        return self.fonts[px]

    def use(self, fill: int, edge: int = EDGE) -> None:
        self.a["SelectObject"](self.dc, self.brush(fill))
        self.a["SelectObject"](self.dc, self.pen(edge))

    def text(self, label: str, box: tuple, px: float, colour: int) -> None:
        if not label:
            return
        self.a["SelectObject"](self.dc, self.font(px))
        self.a["SetTextColor"](self.dc, colour)
        r = wintypes.RECT(*box)
        self.a["DrawTextW"](self.dc, label, -1, ctypes.byref(r), 0x01 | 0x04 | 0x20 | 0x100)  # centred, one line, no clip

    def clear(self) -> None:
        r = wintypes.RECT(0, 0, self.width, self.height)
        self.a["FillRect"](self.dc, ctypes.byref(r), self.brush(KEY))

    def close(self) -> None:
        a = self.a
        a["SelectObject"](self.dc, self.old)
        for h in [*self.brushes.values(), *self.pens.values(), *self.fonts.values(), self.bitmap]:
            a["DeleteObject"](h)
        a["DeleteDC"](self.dc)


def draw(p: Painter, pad: padlib.Pad, active_only: bool = False) -> None:
    """The whole pad, from scratch. GDI draws twenty shapes in well under a millisecond; only the pixels that
    changed are pushed to the screen. `active_only` is the top layer of the ghost mode: just the controls a
    finger is on, and the handle."""
    p.clear()
    pressed = pad.pressed()
    dpad = pad.dpad_state()
    holding = pad.held_menu_since() is not None
    for c in pad.visible():
        if active_only and c.id not in pressed and c.kind != "menu":
            continue
        l, t, r, b = c.box()
        live = c.id in pressed
        if c.kind == "stick":
            p.use(FILL)
            p.a["Ellipse"](p.dc, l, t, r, b)
            vx, vy = pad.stick_vector(c.id)
            travel, kr = c.r * 0.55, c.r * 0.42
            kx, ky = c.cx + vx * travel, c.cy + vy * travel
            p.use(LIVE if live else KNOB)
            p.a["Ellipse"](p.dc, int(kx - kr), int(ky - kr), int(kx + kr), int(ky + kr))
        elif c.kind == "dpad":
            arm = c.h / 3
            arms = {"up": (c.cx - arm / 2, t, c.cx + arm / 2, c.cy - arm / 2),
                    "down": (c.cx - arm / 2, c.cy + arm / 2, c.cx + arm / 2, b),
                    "left": (l, c.cy - arm / 2, c.cx - arm / 2, c.cy + arm / 2),
                    "right": (c.cx + arm / 2, c.cy - arm / 2, r, c.cy + arm / 2)}
            p.use(FILL)
            p.a["Rectangle"](p.dc, int(c.cx - arm / 2), int(c.cy - arm / 2), int(c.cx + arm / 2), int(c.cy + arm / 2))
            for name, (x0, y0, x1, y1) in arms.items():
                p.use(LIVE if dpad & padlib.BITS[name] else FILL)
                p.a["RoundRect"](p.dc, int(x0), int(y0), int(x1), int(y1), int(arm / 3), int(arm / 3))
        elif c.kind == "trackpad":
            p.use(FILL, LIVE if live else EDGE)
            corner = int(min(c.w, c.h) * 0.12)
            p.a["RoundRect"](p.dc, l, t, r, b, corner, corner)
            size = min(c.h, c.w) * 0.09
            p.text("trackpad", (l, b - int(size * 2.2), r - int(size * 1.2), b), size, EDGE)
        else:
            fill = (HOLD if holding else FILL) if c.kind == "menu" else (LIVE if live else FILL)
            p.use(fill)
            if c.shape == "circle":
                p.a["Ellipse"](p.dc, l, t, r, b)
            else:
                p.a["RoundRect"](p.dc, l, t, r, b, int(c.h * 0.5), int(c.h * 0.5))
            label = ("Pad" if pad.hidden else "Hide") if c.kind == "menu" else c.label
            size = c.h * (0.46 if len(label) <= 2 else 0.30 if len(label) <= 5 else 0.24)
            p.text(label, (l, t, r, b), size, WHITE_TEXT if live else TEXT.get(c.id, WHITE_TEXT))


class Overlay:
    """Start, stop, and read. Everything Win32 happens on the overlay's own thread; other threads only post
    messages to it, which is the one thing Windows lets any thread do to any window.

    Two windows when the idle setting is below 100 % (the ghost mode): the base window carries every control
    at the idle opacity — down to nearly invisible, but never absent, so it still takes the touches — and a
    second window on top of it draws only the controls a finger is on, at the full opacity. Both feed the same
    pad; a finger is captured by whichever it landed on."""

    def __init__(self, port: int):
        self.port = port
        self.thread = None
        self.hwnd = None                                  # the base window: every control, the timer
        self.top = None                                   # the ghost mode's top window, or None
        self.painters: dict = {}                          # hwnd -> Painter
        self.error = ""
        self.pad_error = ""
        self.report = padlib.blank()
        self.screen = None
        self.hidden = False
        self.started_at = 0.0
        self._pending = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive() and self.hwnd)

    def status(self) -> dict:
        return {"running": self.running, "error": self.error, "pad_error": self.pad_error,
                "hidden": self.hidden, "screen": self.screen, "report": dict(self.report)}

    def start(self, layout: dict, settings: dict) -> dict:
        if os.name != "nt":
            return {"ok": False, "error": "The overlay runs on Windows."}
        with self._lock:
            if self.running:
                self.reload(layout, settings)
                return {"ok": True, "already": True}
            self.error, self.pad_error, self.hwnd, self.top = "", "", None, None
            self._ready.clear()
            self._pending = (layout, padlib.clean_settings(settings))
            self.thread = threading.Thread(target=self._run, name="overlay", daemon=True)
            self.thread.start()
        self._ready.wait(10)
        return {"ok": self.running, "error": self.error, "pad_error": self.pad_error}

    def stop(self) -> dict:
        if self.hwnd:
            api()["PostMessageW"](self.hwnd, WM_CLOSE, 0, 0)
        if self.thread:
            self.thread.join(5)
        return {"ok": True}

    def reload(self, layout: dict, settings: dict) -> None:
        self._pending = (layout, padlib.clean_settings(settings))
        if self.hwnd:
            api()["PostMessageW"](self.hwnd, WM_APP_RELOAD, 0, 0)

    # ---- the overlay's thread
    def _keepalive(self) -> None:
        """While the pad is up the browser tab is in the background, and a background tab's heartbeat is
        throttled. The pad keeps the app alive itself, through the same /api/ping the page uses."""
        while self.running:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/ping", timeout=5).close()
            except Exception:                              # noqa: BLE001
                pass
            for _ in range(20):
                if not self.running:
                    return
                time.sleep(1)

    def _run(self) -> None:
        a = None
        self.controller = None
        try:
            a = api()
            if a["SetThreadDpiAwarenessContext"]:
                a["SetThreadDpiAwarenessContext"](ctypes.c_void_p(-4))    # PER_MONITOR_AWARE_V2
            elif a["SetProcessDPIAware"]:
                a["SetProcessDPIAware"]()
            if a["EnableMouseInPointer"]:
                a["EnableMouseInPointer"](True)          # a mouse drives the pad too — handy at a desk
            layout, self.settings = self._pending
            self.left, self.top_y, w, h = primary_monitor()
            self.screen = {"w": w, "h": h}
            self.pad = padlib.Pad(layout, w, h, self.settings)
            self._windows(w, h)
            self._render()
            try:
                self.controller = vigem.Controller()
                self.controller.connect()
            except Exception as e:                        # noqa: BLE001  the pad still draws; say why it is mute
                self.controller, self.pad_error = None, str(e)
            for hwnd in self._hwnds():
                a["ShowWindow"](hwnd, 4)                  # SW_SHOWNOACTIVATE
            self._topmost()
            a["SetTimer"](self.hwnd, 1, 100, None)
            self.started_at = time.time()
        except Exception as e:                            # noqa: BLE001
            self.error = f"could not open the overlay: {e}"
            self._cleanup()
            self._ready.set()
            return
        self._ready.set()
        threading.Thread(target=self._keepalive, name="overlay-keepalive", daemon=True).start()
        msg = wintypes.MSG()
        while a["GetMessageW"](ctypes.byref(msg), None, 0, 0) > 0:
            a["TranslateMessage"](ctypes.byref(msg))
            a["DispatchMessageW"](ctypes.byref(msg))
        self._cleanup()

    def _hwnds(self) -> list:
        return [h for h in (self.hwnd, self.top) if h]

    def _ghost(self) -> bool:
        return self.settings["idle"] < 1.0

    def _windows(self, w: int, h: int) -> None:
        a = api()
        self._proc = WNDPROC(self._wndproc)               # kept on self: collected, it would crash the process
        inst = a["GetModuleHandleW"](None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(cls)
        cls.lpfnWndProc = self._proc
        cls.hInstance = inst
        cls.lpszClassName = f"XBOXPadOverlay{id(self)}{int(time.time())}"
        if not a["RegisterClassExW"](ctypes.byref(cls)):
            raise OSError(f"RegisterClassEx failed ({ctypes.get_last_error()})")
        self._class, self._inst = cls.lpszClassName, inst
        self.hwnd = self._window(w, h, "XBOX pad")
        self.painters = {self.hwnd: Painter(w, h)}
        if self._ghost():
            self.top = self._window(w, h, "XBOX pad (active)")
            self.painters[self.top] = Painter(w, h)
        self._alpha()

    def _window(self, w: int, h: int, title: str):
        a = api()
        ex = 0x00080000 | 0x00000008 | 0x00000080 | 0x08000000   # LAYERED | TOPMOST | TOOLWINDOW | NOACTIVATE
        hwnd = a["CreateWindowExW"](ex, self._class, title, 0x80000000,   # WS_POPUP
                                    self.left, self.top_y, w, h, None, None, self._inst, None)
        if not hwnd:
            raise OSError(f"CreateWindowEx failed ({ctypes.get_last_error()})")
        if a["SetWindowFeedbackSetting"]:                 # no touch ripples, no press-and-hold right click
            off = wintypes.BOOL(False)
            for kind in range(1, 12):
                a["SetWindowFeedbackSetting"](hwnd, kind, 0, ctypes.sizeof(off), ctypes.byref(off))
        return hwnd

    def _alpha(self) -> None:
        """The base layer at the idle opacity — 1 at the least, because an alpha of 0 is not there to be
        touched — and the top layer, if there is one, at the full opacity."""
        full = int(round(255 * self.settings["opacity"]))
        base = max(1, int(round(full * self.settings["idle"]))) if self._ghost() else full
        api()["SetLayeredWindowAttributes"](self.hwnd, KEY, base, 0x1 | 0x2)
        if self.top:
            api()["SetLayeredWindowAttributes"](self.top, KEY, full, 0x1 | 0x2)

    def _topmost(self) -> None:                           # HWND_TOPMOST; NOMOVE | NOSIZE | NOACTIVATE | NOOWNERZORDER
        for hwnd in self._hwnds():                        # the top layer last, so it ends up on top
            api()["SetWindowPos"](hwnd, ctypes.c_void_p(-1).value, 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x200)

    def _refit(self) -> None:
        self.left, self.top_y, w, h = primary_monitor()
        self.screen = {"w": w, "h": h}
        for hwnd in self._hwnds():
            api()["SetWindowPos"](hwnd, ctypes.c_void_p(-1).value, self.left, self.top_y, w, h, 0x10)
            self.painters[hwnd].close()
            self.painters[hwnd] = Painter(w, h)
        self.pad.resize(w, h)
        self._redraw(None)

    def _render(self) -> None:
        draw(self.painters[self.hwnd], self.pad)
        if self.top:
            draw(self.painters[self.top], self.pad, active_only=True)

    def _redraw(self, ids) -> None:
        """Repaint the off-screen bitmaps, then invalidate only what moved — or everything, given None."""
        a = api()
        self._render()
        for hwnd in self._hwnds():
            if ids is None:
                a["InvalidateRect"](hwnd, None, False)
                continue
            grow = self.painters[hwnd].line * 2
            for c in self.pad.placed:
                if c.id in ids:
                    r = wintypes.RECT(*c.box(grow))
                    a["InvalidateRect"](hwnd, ctypes.byref(r), False)

    def _send(self) -> None:
        report = self.pad.report()
        if report != self.report:
            self.report = report
            if self.controller:
                self.controller.send(report)
        self._inject(self.pad.take_events())

    def _cursor_on_me(self) -> bool:
        a = api()
        pt = wintypes.POINT()
        a["GetCursorPos"](ctypes.byref(pt))
        return a["WindowFromPoint"](pt) in self._hwnds()

    def _inject(self, events: list) -> None:
        """Cursor moves go straight in. A button, a wheel or a key lands wherever the cursor is — and if that
        is one of these windows, the overlay steps aside for the moment: click-through for the injected input,
        then back. The wait is for the system to pick the input up before the window is solid again."""
        if not events:
            return
        moves = [e for e in events if e[0] == "move"]
        rest = [e for e in events if e[0] != "move"]
        send_inputs(inputs_for(moves))
        if not rest:
            return
        a = api()
        step_aside = any(e[0] != "keydown" and e[0] != "keyup" for e in rest) and self._cursor_on_me()
        if step_aside:
            for hwnd in self._hwnds():
                a["SetWindowLongPtrW"](hwnd, GWL_EXSTYLE, a["GetWindowLongPtrW"](hwnd, GWL_EXSTYLE) | WS_EX_TRANSPARENT)
        try:
            send_inputs(inputs_for(rest))
            if step_aside:
                time.sleep(0.03)
        finally:
            if step_aside:
                for hwnd in self._hwnds():
                    a["SetWindowLongPtrW"](hwnd, GWL_EXSTYLE, a["GetWindowLongPtrW"](hwnd, GWL_EXSTYLE) & ~WS_EX_TRANSPARENT)

    def _pointer(self, hwnd, msg: int, wparam: int, lparam: int) -> None:
        a = api()
        pid = wparam & 0xFFFF
        x, y = _xy(lparam)
        x, y = x - self.left, y - self.top_y
        now = time.time()
        before = self.pad.hidden
        action = None
        if msg == WM_POINTERDOWN:
            changed = self.pad.down(pid, x, y, now)
            if changed and a["GetPointerType"]:
                kind = wintypes.DWORD(0)
                if a["GetPointerType"](pid, ctypes.byref(kind)) and kind.value == PT_MOUSE:
                    a["SetCapture"](hwnd)
        elif msg == WM_POINTERUPDATE and (wparam >> 16) & POINTER_FLAG_INCONTACT:
            changed = self.pad.move(pid, x, y)
        else:                                             # up, capture lost, or an update no longer in contact
            changed, action = self.pad.up(pid, now)
            if not self.pad.fingers:
                a["ReleaseCapture"]()
        if action == "close":
            a["PostMessageW"](self.hwnd, WM_CLOSE, 0, 0)
        self.hidden = self.pad.hidden
        if changed or self.pad.hidden != before:
            self._redraw(None if self.pad.hidden != before else changed)
        self._send()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        a = api()
        try:
            if msg in (WM_POINTERDOWN, WM_POINTERUPDATE, WM_POINTERUP, WM_POINTERCAPTURECHANGED):
                self._pointer(hwnd, msg, wparam, lparam)
                return 0
            if msg == WM_POINTERACTIVATE:
                return PA_NOACTIVATE
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_TABLET_QUERYSYSTEMGESTURESTATUS:
                return 0x00000001 | 0x00000008 | 0x00000100 | 0x00010000 | 0x00080000   # press-and-hold, flicks, feedback off
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                dc = a["BeginPaint"](hwnd, ctypes.byref(ps))
                r = ps.rcPaint
                painter = self.painters.get(hwnd)
                if painter:
                    a["BitBlt"](dc, r.left, r.top, r.right - r.left, r.bottom - r.top, painter.dc,
                                r.left, r.top, 0x00CC0020)   # SRCCOPY
                a["EndPaint"](hwnd, ctypes.byref(ps))
                return 0
            if msg == WM_TIMER:
                self._tick()
                return 0
            if msg == WM_APP_RELOAD and self._pending:
                layout, settings = self._pending
                ghost_before = self._ghost()
                self.settings = settings
                self.pad.set_options(settings)
                self.pad.set_layout(layout)
                if self._ghost() != ghost_before:         # a layer appears or goes: simplest to start over
                    self._relayer()
                self._alpha()
                self._redraw(None)
                self._send()
                return 0
            if msg in (WM_DISPLAYCHANGE, WM_DPICHANGED):
                self._refit()
                return 0
            if msg == WM_CLOSE:
                self.pad.release_all()                    # nothing stays held in the game
                self._inject(self.pad.take_events())
                if self.top:
                    a["DestroyWindow"](self.top)
                a["DestroyWindow"](self.hwnd)
                return 0
            if msg == WM_DESTROY and hwnd == self.hwnd:
                a["KillTimer"](hwnd, 1)
                a["PostQuitMessage"](0)
                return 0
        except Exception as e:                            # noqa: BLE001  never let an exception cross into Windows
            self.error = f"{type(e).__name__}: {e}"
        return a["DefWindowProcW"](hwnd, msg, wparam, lparam)

    def _relayer(self) -> None:
        """The ghost mode switched on or off while the pad is up: make or drop the top layer."""
        a = api()
        if self.top and not self._ghost():
            self.painters.pop(self.top).close()
            a["DestroyWindow"](self.top)
            self.top = None
        elif self._ghost() and not self.top:
            w, h = self.screen["w"], self.screen["h"]
            self.top = self._window(w, h, "XBOX pad (active)")
            self.painters[self.top] = Painter(w, h)
            a["ShowWindow"](self.top, 4)
            self._topmost()

    def _tick(self) -> None:
        self._ticks = getattr(self, "_ticks", 0) + 1
        held = self.pad.held_menu_since()
        if held is not None and time.time() - held >= padlib.HOLD_TO_CLOSE:
            api()["PostMessageW"](self.hwnd, WM_CLOSE, 0, 0)
        if self._ticks % 20 == 0:
            self._topmost()

    def _cleanup(self) -> None:
        if self.controller:
            try:
                self.controller.close()
            except Exception:                              # noqa: BLE001
                pass
        for painter in self.painters.values():
            painter.close()
        self.controller, self.painters, self.hwnd, self.top = None, {}, None, None
        self.report = padlib.blank()
        self.hidden = False


def primary_monitor() -> tuple:
    """(left, top, width, height) of the primary monitor — the Surface's own screen — in the calling thread's
    DPI context, which is physical pixels once that thread is per-monitor aware."""
    a = api()
    mon = a["MonitorFromPoint"](wintypes.POINT(0, 0), 1)       # MONITOR_DEFAULTTOPRIMARY
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    a["GetMonitorInfoW"](mon, ctypes.byref(info))
    r = info.rcMonitor
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def screen_size() -> dict | None:
    """The primary monitor in physical pixels, without opening anything — what the layout editor draws to."""
    if os.name != "nt":
        return None
    try:
        a = api()
        if a["SetThreadDpiAwarenessContext"]:
            a["SetThreadDpiAwarenessContext"](ctypes.c_void_p(-4))
        _, _, w, h = primary_monitor()
        return {"w": w, "h": h}
    except Exception:                                    # noqa: BLE001
        return None
