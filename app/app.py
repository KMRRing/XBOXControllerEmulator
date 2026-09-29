"""XBOXControllerEmulator — a touch Xbox controller drawn over whatever is on the Surface's screen.

Three parts, three files:

    pad.py       what the touches mean — layouts, hit-testing, the XInput report. Pure Python, tested anywhere.
    vigem.py     the virtual Xbox 360 pad, spoken to the ViGEmBus driver directly (no DLL, any CPU).
    overlay.py   the transparent, never-focused, multi-touch window the pad is drawn in.

This file is the page's side: the driver, the overlay, and the layouts.

Data, under frame.DATA — one file per layout, because the sync is newest-wins per file:

    layouts/<id>.json         a controller layout; layouts/default.json, if present, overrides the built-in one
    touch-layouts/<id>.json   a full-screen touch layout, the same way — two folders, so the two kinds of
                              layout never share a name
    settings.json             the mode, the active layout of each mode, and the pad's other settings

A deleted layout is a tombstone {"deleted": true}, because the sync carries files, never deletions.
"""
from __future__ import annotations

import ctypes
import json
import os

import frame
import overlay as overlaylib
import pad as padlib
import vigem

make_server = frame.make_server                    # the iOS launcher runs the app inside its own process

ROOT = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.join(ROOT, "driver", "ViGEmBus_1.22.0_x64_x86_arm64.exe")
FOLDERS = {"pad": os.path.join(frame.DATA, "layouts"), "screen": os.path.join(frame.DATA, "touch-layouts")}
ACTIVE = {"pad": "active", "screen": "active_screen"}      # the settings key naming each mode's active layout
SETTINGS = os.path.join(frame.DATA, "settings.json")

OVERLAY = overlaylib.Overlay(frame.port())


# --------------------------------------------------------------------------------- storage
def _read(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def layouts(mode: str) -> dict:
    """Every layout of a mode by id: the built-in default, overridden by a saved one of the same id."""
    out = {"default": padlib.clean_layout(padlib.BUILT_IN[mode])}
    folder = FOLDERS[mode]
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json"):
                raw = _read(os.path.join(folder, name), None)
                if isinstance(raw, dict) and not raw.get("deleted"):
                    lay = padlib.clean_layout({**raw, "id": name[:-5], "mode": mode})
                    out[lay["id"]] = lay
    return out


def settings() -> dict:
    return padlib.clean_settings(_read(SETTINGS, {}))


def active() -> tuple:
    """The layout the overlay shows — the active one of the current mode — and the settings."""
    s = settings()
    every = layouts(s["mode"])
    return every.get(s[ACTIVE[s["mode"]]]) or every["default"], s


def _mode(value) -> str:
    return value if value in padlib.MODES else "pad"


def refresh_overlay() -> None:
    if OVERLAY.running:
        OVERLAY.reload(*active())


# --------------------------------------------------------------------------------- the driver
def launch_installer():
    """The ViGEmBus setup, elevated: Windows shows its UAC prompt, then the installer's own few clicks."""
    if os.name != "nt":
        return 400, {"error": "The driver installs on Windows."}
    if not os.path.isfile(INSTALLER):
        return 500, {"error": "The installer is missing from this copy of the app."}
    ctypes.WinDLL("ole32").CoInitializeEx(None, 0x2 | 0x4)   # apartment-threaded, no OLE1 DDE: what ShellExecute wants
    shell = ctypes.WinDLL("shell32")
    shell.ShellExecuteW.restype = ctypes.c_void_p
    shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    ctypes.c_wchar_p, ctypes.c_int]
    code = shell.ShellExecuteW(None, "runas", INSTALLER, None, os.path.dirname(INSTALLER), 1) or 0
    if code <= 32:
        return 500, {"error": "The installer was declined at the Windows prompt." if code == 5
                     else f"Windows did not start the installer (code {code})."}
    return {"ok": True}


# --------------------------------------------------------------------------------- routes
@frame.route("GET", "/api/status")
def status(request):
    return {"windows": os.name == "nt", "driver": vigem.driver_status(), "overlay": OVERLAY.status(),
            "screen": OVERLAY.screen or overlaylib.screen_size(), "installer": os.path.isfile(INSTALLER)}


@frame.route("GET", "/api/pad")
def pad_state(request):
    return {"running": OVERLAY.running, "hidden": OVERLAY.hidden, "report": dict(OVERLAY.report)}


@frame.route("POST", "/api/driver/install")
def driver_install(request):
    return launch_installer()


@frame.route("POST", "/api/overlay/start")
def overlay_start(request):
    result = OVERLAY.start(*active())
    return result if result.get("ok") else (409, result)


@frame.route("POST", "/api/overlay/stop")
def overlay_stop(request):
    return OVERLAY.stop()


@frame.route("GET", "/api/layouts")
def layouts_list(request):
    return {"layouts": {mode: list(layouts(mode).values()) for mode in padlib.MODES}, "settings": settings(),
            "catalogue": {k: {"kind": v[0], "label": v[1], "shape": v[2]} for k, v in padlib.CATALOGUE.items()},
            "symmetry": {"twins": padlib.TWINS, "cluster": padlib.CLUSTER, "centre_twins": padlib.CENTRE_TWINS,
                         "snap_steps": padlib.SNAP_STEPS},
            "codes": [[code, label] for code, (_, _, label) in padlib.CODES.items()],
            "max_keys": padlib.MAX_KEYS}


@frame.route("POST", "/api/layouts/save")
def layouts_save(request):
    body = request.json
    raw = body.get("layout")
    if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
        return 400, {"error": "A layout needs a name."}
    layout = padlib.clean_layout(raw)
    _write(os.path.join(FOLDERS[layout["mode"]], layout["id"] + ".json"), layout)
    if body.get("activate"):
        _write(SETTINGS, {**settings(), ACTIVE[layout["mode"]]: layout["id"]})
    refresh_overlay()
    return {"ok": True, "layout": layout, "settings": settings()}


@frame.route("POST", "/api/layouts/delete")
def layouts_delete(request):
    mode = _mode(request.json.get("mode"))
    lid = padlib.slug(request.json.get("id") or "")
    path = os.path.join(FOLDERS[mode], lid + ".json")
    if not os.path.isfile(path):
        return 404, {"error": "No saved layout by that name."}
    _write(path, {"id": lid, "deleted": True})         # a tombstone: the sync carries files, never deletions
    if settings()[ACTIVE[mode]] == lid and lid != "default":
        _write(SETTINGS, {**settings(), ACTIVE[mode]: "default"})
    refresh_overlay()
    return {"ok": True, "settings": settings()}


@frame.route("POST", "/api/settings")
def settings_save(request):
    s = padlib.clean_settings({**settings(), **request.json})
    for mode in padlib.MODES:
        if s[ACTIVE[mode]] not in layouts(mode):
            return 404, {"error": "No layout by that name."}
    _write(SETTINGS, s)
    refresh_overlay()
    return {"ok": True, "settings": s}


# ---------------------------------------------------------------------------------------------------------
# Routes go ABOVE this line. Anything written below it is never reached, because main() does not return
# until the app stops. test_system.py fails the build if a route ends up down here.
# ---------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    raise SystemExit(frame.main())
