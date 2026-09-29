"""test_app — the application on top of the delivery system: the pad's rules, the driver's wire format, and the
routes. Runs anywhere; only the overlay window and the driver itself need Windows.

    python -m pytest test_app.py -q
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))

import overlay  # noqa: E402
import pad  # noqa: E402
import vigem  # noqa: E402

W, H = 2736, 1824                                            # a Surface Pro screen


def centre(layout: dict, cid: str) -> tuple:
    c = next(c for c in layout["controls"] if c["id"] == cid)
    return c["x"] * W, c["y"] * H, c["s"] * min(W, H)


@pytest.fixture
def p() -> pad.Pad:
    return pad.Pad(pad.DEFAULT, W, H)


# --------------------------------------------------------------------------------- the pad's rules
def test_the_default_layout_places_every_control_once_and_in_range():
    lay = pad.clean_layout(pad.DEFAULT)
    ids = [c["id"] for c in lay["controls"]]
    assert sorted(ids) == sorted(pad.CATALOGUE), "every control in the catalogue, none twice"
    assert all(0 <= c["x"] <= 1 and 0 <= c["y"] <= 1 for c in lay["controls"])
    assert lay["id"] == "default"


def test_a_layout_is_cleaned_not_trusted():
    lay = pad.clean_layout({"name": "Mine!", "controls": [{"id": "a", "x": 7, "y": -1, "s": "big"},
                                                          {"id": "fake", "x": 0.5}, {"id": "menu", "on": False}]})
    a = next(c for c in lay["controls"] if c["id"] == "a")
    assert (a["x"], a["y"]) == (1.0, 0.0), "out-of-range positions are clamped"
    assert a["s"] == next(c for c in pad.DEFAULT["controls"] if c["id"] == "a")["s"], "nonsense sizes fall back"
    assert "fake" not in [c["id"] for c in lay["controls"]]
    assert next(c for c in lay["controls"] if c["id"] == "menu")["on"], "the menu handle cannot be switched off"
    assert lay["id"] == "mine" and lay["name"] == "Mine!"
    assert pad.clean_settings({"opacity": 3, "active": "My Pad", "snap": 2.4, "mirror": 0, "speed": 40, "idle": "x"}) == {
        "active": "my-pad", "active_screen": "default", "opacity": 1.0, "snap": 2.5, "mirror": False, "speed": 10,
        "idle": 1.0, "floating": False, "mode": "pad", "hold": 0.5}
    assert pad.clean_settings({"mode": "screen", "hold": 9})["mode"] == "screen"
    assert pad.clean_settings({"mode": "desktop", "hold": 9}) | {} == pad.clean_settings({"hold": 1.5}), "unknown modes fall back"
    assert pad.clean_settings({"snap": "off"})["snap"] == 2.5, "nonsense snaps fall back to the default step"


def test_the_default_layout_is_already_symmetric():
    """The editor's Mirror follows the left side with the right; a default that is not symmetric would jump
    the first time something is dragged."""
    lay = {c["id"]: c for c in pad.clean_layout(pad.DEFAULT)["controls"]}
    for left, right in pad.TWINS:
        assert lay[left]["x"] + lay[right]["x"] == pytest.approx(1.0, abs=1e-3), (left, right)
        assert (lay[left]["y"], lay[left]["s"], lay[left]["w"]) == (lay[right]["y"], lay[right]["s"], lay[right]["w"])
    cx = sum(lay[i]["x"] for i in pad.CLUSTER) / 4
    cy = sum(lay[i]["y"] for i in pad.CLUSTER) / 4
    assert lay["ls"]["x"] + cx == pytest.approx(1.0, abs=1e-3) and lay["ls"]["y"] == pytest.approx(cy, abs=1e-3)
    assert lay["dpad"]["x"] + lay["rs"]["x"] == pytest.approx(1.0, abs=1e-3) and lay["dpad"]["y"] == lay["rs"]["y"]
    assert len({lay[i]["s"] for i in pad.CLUSTER}) == 1, "the face buttons are one size"


def test_a_finger_on_a_button_presses_it_and_lifting_releases_it(p):
    x, y, _ = centre(p.layout, "a")
    assert p.down(1, x, y, 0.0) == {"a"}
    assert p.report()["buttons"] == pad.BITS["a"]
    assert p.up(1, 0.1) == ({"a"}, None)
    assert p.report() == pad.blank()


def test_a_finger_slides_from_one_button_to_the_next_without_lifting(p):
    ax, ay, _ = centre(p.layout, "a")
    bx, by, _ = centre(p.layout, "b")
    p.down(1, ax, ay, 0.0)
    assert p.move(1, bx, by) == {"a", "b"}
    assert p.report()["buttons"] == pad.BITS["b"]
    assert p.move(1, W / 2, H / 2) == {"b"}, "off every button, nothing is pressed"
    assert p.report()["buttons"] == 0


def test_a_stick_reports_the_offset_up_positive_and_clamped_to_the_rim(p):
    x, y, size = centre(p.layout, "ls")
    r = size / 2
    p.down(1, x, y, 0.0)
    assert p.report()["lx"] == 0 and p.report()["ly"] == 0
    p.move(1, x + r * pad.STICK_THROW / 2, y - r * pad.STICK_THROW / 2)
    rep = p.report()
    assert rep["lx"] == pytest.approx(16384, abs=2) and rep["ly"] == pytest.approx(16384, abs=2), "up is positive"
    p.move(1, x + 5 * r, y)                                  # far beyond the rim: still held, pinned at full
    assert p.report()["lx"] == 32767 and p.report()["ly"] == 0
    assert p.report()["rx"] == 0, "the other stick is untouched"
    assert p.up(1, 1.0)[0] == {"ls"}


def test_the_dpad_rolls_through_eight_directions_and_has_a_dead_centre(p):
    x, y, size = centre(p.layout, "dpad")
    p.down(1, x, y, 0.0)
    assert p.report()["buttons"] == 0, "the middle presses nothing"
    for dx, dy, want in ((1, 0, "right"), (1, -1, "right,up"), (0, -1, "up"), (-1, -1, "left,up"),
                         (-1, 0, "left"), (-1, 1, "left,down"), (0, 1, "down"), (1, 1, "right,down")):
        p.move(1, x + dx * size * 0.4, y + dy * size * 0.4)
        want_bits = sum(pad.BITS[n] for n in want.split(","))
        assert p.report()["buttons"] == want_bits, want


def test_three_fingers_at_once_are_three_inputs(p):
    ax, ay, _ = centre(p.layout, "a")
    lx, ly, ls = centre(p.layout, "ls")
    tx, ty, _ = centre(p.layout, "rt")
    p.down(1, ax, ay, 0.0)
    p.down(2, lx, ly, 0.0)
    p.move(2, lx, ly - ls)
    p.down(3, tx, ty, 0.0)
    rep = p.report()
    assert rep["buttons"] == pad.BITS["a"] and rep["ly"] == 32767 and rep["rt"] == 255 and rep["lt"] == 0
    p.up(2, 0.5)
    assert p.report()["ly"] == 0 and p.report()["buttons"] == pad.BITS["a"], "lifting one finger keeps the others"


def test_a_touch_off_every_control_belongs_to_the_game(p):
    assert p.down(1, W / 2, H / 2, 0.0) == set()
    assert p.fingers == {}


def test_the_menu_handle_hides_the_pad_on_a_tap_and_closes_it_on_a_hold(p):
    mx, my, _ = centre(p.layout, "menu")
    ax, ay, _ = centre(p.layout, "a")
    p.down(1, ax, ay, 0.0)
    p.down(2, mx, my, 0.0)
    assert p.up(2, 0.3) == ({"menu"}, "toggle")
    assert p.hidden and p.fingers == {}, "hiding the pad lets go of every finger"
    assert p.report() == pad.blank()
    assert p.down(3, ax, ay, 1.0) == set(), "a hidden button is not there"
    assert p.down(4, mx, my, 1.0) == {"menu"}
    assert p.held_menu_since() == 1.0
    assert p.up(4, 1.0 + pad.HOLD_TO_CLOSE) == ({"menu"}, "close")


def test_a_control_switched_off_is_neither_drawn_nor_touchable():
    lay = pad.clean_layout({"name": "no guide", "controls": [{"id": "guide", "x": 0.5, "y": 0.5, "s": 0.2, "on": False}]})
    p = pad.Pad(lay, W, H)
    assert "guide" not in [c.id for c in p.placed]
    assert p.down(1, W / 2, H / 2, 0.0) == set()


def test_a_layout_keeps_its_proportions_on_another_screen():
    a = pad.Placed(next(c for c in pad.DEFAULT["controls"] if c["id"] == "a"), 2736, 1824)
    b = pad.Placed(next(c for c in pad.DEFAULT["controls"] if c["id"] == "a"), 1368, 912)
    assert (a.cx, a.cy, a.h) == (2 * b.cx, 2 * b.cy, 2 * b.h)


KEY1 = {"id": "k1", "key": "Escape", "x": 0.40, "y": 0.24, "s": 0.065}
KEY2 = {"id": "k2", "key": "ControlLeft+KeyS", "x": 0.47, "y": 0.24, "s": 0.065}


def tp(**extra) -> pad.Pad:
    """A pad with the trackpad and the mouse buttons switched on, and two key buttons."""
    lay = pad.clean_layout({"name": "mouse", "controls": [{"id": i, "on": True, **DEF[i]} for i in
                                                          ("trackpad", "lmb", "rmb")] + [KEY1, KEY2]})
    return pad.Pad(lay, W, H, extra)


DEF = {c["id"]: {k: v for k, v in c.items() if k not in ("id", "on")} for c in pad.DEFAULT["controls"]}


def test_the_trackpad_moves_the_cursor_with_the_finger_faster_when_the_finger_is_fast():
    p = tp(speed=5)
    x, y, _ = centre(p.layout, "trackpad")
    p.down(1, x, y, 0.0)
    p.move(1, x + 2, y + 1)
    (kind, dx, dy), = p.take_events()
    assert kind == "move" and 4 <= dx <= 6 and 2 <= dy <= 3, "a slow finger: about 0.4 x speed pixels per pixel"
    p.move(1, x + 42, y + 1)
    (kind, fdx, _), = p.take_events()
    assert kind == "move" and fdx > 2 * 2 * 40, "a fast finger gets up to three times that"
    assert p.report() == pad.blank(), "the trackpad is not a gamepad control"
    p.up(1, 1.0)
    assert p.take_events() == [], "a finger that travelled did not tap"


def test_a_tap_clicks_and_a_quick_return_drags():
    p = tp()
    x, y, _ = centre(p.layout, "trackpad")
    p.down(1, x, y, 0.0)
    p.up(1, 0.1)
    assert p.take_events() == [("click", "left")]
    p.down(1, x, y, 0.2)                                       # back down within the window: hold the button
    p.move(1, x + 30, y)
    p.up(1, 0.9)
    ev = p.take_events()
    assert ev[0] == ("down", "left") and ev[-1] == ("up", "left") and any(e[0] == "move" for e in ev)
    p.down(1, x, y, 5.0)                                       # a plain touch much later, held too long
    p.up(1, 5.6)
    assert p.take_events() == [], "a long press is not a tap"


def test_two_fingers_tap_for_the_right_button_and_scroll_when_they_travel():
    p = tp()
    x, y, size = centre(p.layout, "trackpad")
    p.down(1, x - 20, y, 0.0)
    p.down(2, x + 20, y, 0.02)
    p.up(1, 0.1)
    p.up(2, 0.12)
    assert p.take_events() == [("click", "right")], "one right click for two fingers, not two"
    p.down(1, x - 20, y, 1.0)
    p.down(2, x + 20, y, 1.0)
    notch = pad.SCROLL_NOTCH * min(W, H)
    p.move(1, x - 20, y + notch * 2.5)                         # two fingers travelling together, one event each
    p.move(2, x + 20, y + notch * 2.5)
    wheel = lambda: sum(e[1] for e in p.take_events() if e[0] == "wheel")  # noqa: E731
    assert wheel() == 2, "finger down, content down; as far as the fingers went, not twice as far"
    p.move(1, x - 20, y - notch * 0.5)
    p.move(2, x + 20, y - notch * 0.5)
    assert wheel() == -2, "the half notch left over carries: +0.5 - 3 = -2.5"
    p.up(1, 2.0)
    p.up(2, 2.0)
    assert p.take_events() == [], "fingers that scrolled did not tap"


def test_the_mouse_buttons_and_the_keys_press_and_release():
    p = tp()
    lx, ly, _ = centre(p.layout, "lmb")
    kx, ky, _ = centre(p.layout, "k1")
    p.down(1, lx, ly, 0.0)
    p.down(2, kx, ky, 0.0)
    assert p.take_events() == [("down", "left"), ("keydown", "Escape")]
    assert p.report() == pad.blank(), "mouse buttons and keys are not gamepad buttons"
    p.up(2, 0.5)
    p.up(1, 0.6)
    assert p.take_events() == [("keyup", "Escape"), ("up", "left")]
    p.down(3, kx, ky, 1.0)
    p.take_events()
    p.toggle()                                                 # hiding the pad lets go in the game too
    assert p.take_events() == [("keyup", "Escape")]


def test_a_key_button_holds_a_key_or_a_combination_it_can_send():
    assert pad.clean_chord("KeyS+ControlLeft") == "ControlLeft+KeyS", "modifiers first, in a fixed order"
    assert pad.clean_chord("AltLeft+ShiftLeft+ControlLeft+Digit1") == "ControlLeft+ShiftLeft+AltLeft+Digit1"
    assert pad.clean_chord("ShiftRight") == "ShiftRight", "a modifier alone is a key too"
    assert pad.clean_chord("KeyA+KeyB") is None, "one key besides the modifiers"
    assert pad.clean_chord("MetaLeft") is None and pad.clean_chord("") is None and pad.clean_chord(7) is None
    assert pad.clean_chord("Ctrl+Up") == "ControlLeft+ArrowUp", "the key names of v0.3/v0.4 carry over"
    assert pad.chord_label("ControlLeft+ShiftLeft+KeyS") == "Ctrl+Shift+S"


def test_key_buttons_are_added_and_removed_and_old_unused_ones_are_dropped():
    lay = pad.clean_layout({"name": "k", "controls": [
        {"id": "k1", "key": "Escape", "on": False},                  # a v0.3 layout's unused key: dropped
        {"id": "k2", "key": "F5"},
        {"id": "k9", "key": "Nonsense", "label": "Hmm"},              # not sendable: Space, and its own label
        {"id": "k25", "key": "Space"},                                # beyond the 24 a layout can hold
        {"id": "k3", "key": "ControlLeft+KeyZ", "label": "Undo", "x": 0.2, "w": 2}]})
    keys = [c for c in lay["controls"] if c["id"].startswith("k")]
    assert [(c["id"], c["key"], c["label"]) for c in keys] == [("k2", "F5", "F5"), ("k3", "ControlLeft+KeyZ", "Undo"),
                                                               ("k9", "Space", "Space")]
    assert keys[1]["x"] == 0.2 and keys[1]["w"] == 2 and all(c["on"] for c in keys)
    p = pad.Pad(lay, W, H)
    k3 = p.by_id["k3"]
    assert (k3.kind, k3.shape, k3.label) == ("key", "rect", "Undo")


def test_a_combination_goes_down_modifiers_first_and_comes_up_in_reverse():
    """The overlay sends scan codes — the key's position — with the extended flag where the keyboard would."""
    recs = overlay.inputs_for([("keydown", "ControlLeft+KeyS"), ("keyup", "ControlLeft+KeyS"), ("keydown", "ArrowUp")])
    got = [(r.type, r.u.ki.wScan, r.u.ki.dwFlags) for r in recs]
    SC, UP, EXT = overlay.KEY_SCANCODE, overlay.KEY_UP, overlay.KEY_EXTENDED
    assert got == [(1, 0x1D, SC), (1, 0x1F, SC), (1, 0x1F, SC | UP), (1, 0x1D, SC | UP), (1, 0x48, SC | EXT)]
    assert (ctypes.sizeof(overlay.INPUT), ctypes.sizeof(overlay.MOUSEINPUT), ctypes.sizeof(overlay.KEYBDINPUT)) == \
        (40, 32, 24), "SendInput checks the record size: Windows x64's"


def test_a_floating_stick_centres_where_the_finger_lands():
    p = pad.Pad(pad.DEFAULT, W, H, {"floating": True})
    x, y, size = centre(p.layout, "ls")
    r = size / 2
    p.down(1, x + r * 0.9, y, 0.0)                             # near the rim, but that is the new centre
    assert p.report()["lx"] == 0
    p.move(1, x + r * 0.9 + r * pad.STICK_THROW / 2, y)
    assert p.report()["lx"] == pytest.approx(16384, abs=2)


# --------------------------------------------------------------------------------- full-screen touch
def screen(**extra) -> pad.Pad:
    """Full-screen touch, with one key button."""
    lay = pad.clean_layout({"name": "s", "mode": "screen", "controls": [KEY1]})
    return pad.Pad(lay, W, H, {"mode": "screen", **extra})


def test_the_full_screen_default_is_the_handle_alone():
    lay = pad.clean_layout(pad.TOUCH)
    assert [c["id"] for c in lay["controls"]] == ["menu"] and lay["mode"] == "screen"
    stray = pad.clean_layout({"name": "x", "mode": "screen", "controls": [{"id": "a"}, {"id": "ls"}]})
    assert [c["id"] for c in stray["controls"]] == ["menu"], "a full-screen layout has no gamepad controls"


def test_full_screen_a_tap_puts_the_cursor_there_and_clicks():
    p = screen()
    p.down(1, 1000, 700, 0.0)
    assert p.take_events() == [("to", 1000, 700)], "the cursor goes to the finger at once — the game shows its hover"
    p.move(1, 1004, 703)                                       # a finger's jitter is not a drag
    p.up(1, 0.2)
    assert p.take_events() == [("to", 1000, 700), ("click", "left")], "the click lands where the finger landed"
    assert p.report() == pad.blank()


def test_full_screen_a_touch_that_travels_drags_from_where_it_landed():
    p = screen()
    p.down(1, 1000, 700, 0.0)
    p.take_events()
    far = pad.SCREEN_SLOP * min(W, H) + 1
    p.move(1, 1000 + far, 700)
    assert p.take_events() == [("to", 1000, 700), ("down", "left"), ("to", 1000 + far, 700)]
    p.move(1, 1400, 900)
    assert p.take_events() == [("to", 1400, 900)]
    p.up(1, 3.0)
    assert p.take_events() == [("to", 1400, 900), ("up", "left")]


def test_full_screen_holding_still_arms_a_right_click_released_on_lift():
    p = screen(hold=0.5)
    p.down(1, 800, 600, 0.0)
    p.take_events()
    assert not p.tick(0.3) and p.armed() == []
    assert p.tick(0.5) and p.armed() == [(800, 600)], "armed at the hold time: the overlay draws a ring there"
    assert not p.tick(0.6), "armed once"
    p.up(1, 1.2)
    assert p.take_events() == [("to", 800, 600), ("click", "right")]


def test_full_screen_a_finger_that_moves_after_arming_drags_instead():
    p = screen(hold=0.5)
    p.down(1, 800, 600, 0.0)
    p.tick(0.6)
    p.move(1, 900, 600)
    assert ("down", "left") in p.take_events() and p.armed() == []
    p.up(1, 1.0)
    assert p.take_events()[-1] == ("up", "left")


def test_full_screen_two_quick_taps_land_on_the_same_pixel():
    p = screen()
    p.down(1, 1000, 700, 0.0)
    p.up(1, 0.1)
    p.down(1, 1006, 695, 0.2)
    p.up(1, 0.3)
    clicks = [e for e in p.take_events() if e[0] == "to"]
    assert clicks[-1] == ("to", 1000, 700), "within reach of the first tap: exactly there, a double click"
    p.down(1, 1006, 695, 2.0)
    p.up(1, 2.1)
    assert p.take_events()[-2] == ("to", 1006, 695), "much later: where the finger is"


def test_full_screen_two_fingers_scroll_or_tap_for_a_right_click():
    p = screen()
    p.down(1, 900, 700, 0.0)
    p.down(2, 1100, 700, 0.02)
    p.up(1, 0.1)
    p.up(2, 0.12)
    assert p.take_events() == [("to", 900, 700), ("click", "right")], "one right click, and no left click"
    notch = pad.SCROLL_NOTCH * min(W, H)
    p.down(1, 900, 700, 1.0)
    p.down(2, 1100, 700, 1.0)
    p.take_events()
    for y in (700 + notch, 700 + notch * 2):
        p.move(1, 900, y)
        p.move(2, 1100, y)
    assert sum(e[1] for e in p.take_events() if e[0] == "wheel") == 2
    p.up(1, 2.0)
    p.up(2, 2.0)
    assert p.take_events() == [], "fingers that scrolled neither click nor drag"


def test_full_screen_leaves_only_the_handle_and_the_keys_and_the_handle_pauses_it():
    p = screen()
    assert {c.id for c in p.visible()} == {"menu", "k1"}
    ax, ay = DEF["a"]["x"] * W, DEF["a"]["y"] * H
    p.down(1, ax, ay, 0.0)
    assert p.take_events() == [("to", ax, ay)], "where A would be is just screen"
    p.up(1, 0.1)
    p.take_events()
    kx, ky, _ = centre(p.layout, "k1")
    p.down(2, kx, ky, 1.0)
    p.up(2, 1.1)
    assert p.take_events() == [("keydown", "Escape"), ("keyup", "Escape")], "the key buttons still work"
    p.down(3, 1000, 700, 2.0)
    p.move(3, 1300, 700)
    p.take_events()
    p.toggle()                                                 # paused mid-drag: the button is let go
    assert p.take_events() == [("up", "left")]
    p.down(4, 1000, 700, 3.0)
    assert p.take_events() == [] and p.fingers == {}, "paused, the screen is the game's"


# --------------------------------------------------------------------------------- the driver's wire format
def test_the_structures_match_the_drivers_headers():
    """Sizes and codes as ViGEmBus defines them (BusShared.h); a byte off and the driver rejects the request."""
    assert ctypes.sizeof(vigem.PluginTarget) == 16
    assert ctypes.sizeof(vigem.SerialOnly) == 8
    assert ctypes.sizeof(vigem.XusbReport) == 12
    assert ctypes.sizeof(vigem.XusbSubmit) == 20
    assert vigem.IOCTL_PLUGIN == 0x2AA004
    assert vigem.IOCTL_UNPLUG == 0x2AA008
    assert vigem.IOCTL_CHECK_VERSION == 0x2AA00C
    assert vigem.IOCTL_WAIT_READY == 0x2AA010
    assert vigem.IOCTL_XUSB_SUBMIT == 0x2AA808
    assert bytes(vigem.BUS_GUID) == bytes.fromhex("222be496e9f5f842b043ed0f932f014f")


def test_a_report_packs_as_the_driver_reads_it():
    r = vigem.pack_report({"buttons": 0x1009, "lt": 255, "rt": 0, "lx": -32768, "ly": 32767, "rx": 1, "ry": -1})
    assert bytes(r) == bytes.fromhex("0910ff000080ff7f0100ffff")


def test_off_windows_the_driver_is_reported_missing_not_crashing():
    if os.name == "nt":
        pytest.skip("this asserts the non-Windows answer")
    assert vigem.driver_status() == {"ok": False, "error": "Windows only"}


# --------------------------------------------------------------------------------- the routes
@pytest.fixture
def app(tmp_path):
    port = json.load(open(os.path.join(ROOT, "app", "appinfo.json"), encoding="utf-8"))["port"]
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=os.path.join(ROOT, "app"),
                            env={**os.environ, "APP_DATA_DIR": str(tmp_path / "data"), "DEVICE_NAME": "dev"})
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:                                  # noqa: BLE001
            time.sleep(0.25)
    else:
        proc.terminate()
        raise AssertionError("the app never answered")

    def call(path: str, body=None):
        req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    yield call, tmp_path / "data"
    proc.terminate()


def test_the_status_route_says_what_this_machine_can_do(app):
    call, _ = app
    status, body = call("/api/status")
    assert status == 200
    assert body["windows"] == (os.name == "nt")
    assert body["installer"], "the driver installer ships with the app"
    assert body["overlay"]["running"] is False


def test_layouts_are_one_file_each_and_a_saved_default_overrides_the_built_in(app):
    call, data = app
    _, before = call("/api/layouts")
    assert [lay["id"] for lay in before["layouts"]["pad"]] == ["default"]
    before["layouts"] = before["layouts"]["pad"]
    lay = dict(before["layouts"][0], name="Wide", id="Wide Pad!")
    lay["controls"][0]["x"] = 0.33
    status, saved = call("/api/layouts/save", {"layout": lay, "activate": True})
    assert status == 200 and saved["layout"]["id"] == "wide-pad" and saved["settings"]["active"] == "wide-pad"
    assert (data / "layouts" / "wide-pad.json").is_file(), "one file per layout, named by its id"
    default = dict(before["layouts"][0], name="Default")
    default["controls"][0]["y"] = 0.5
    call("/api/layouts/save", {"layout": default})
    _, after = call("/api/layouts")
    after["layouts"] = after["layouts"]["pad"]
    assert [lay["id"] for lay in after["layouts"]] == ["default", "wide-pad"]
    assert after["layouts"][0]["controls"][0]["y"] == 0.5, "default.json overrides the built-in default"
    assert after["settings"]["active"] == "wide-pad"


def test_deleting_a_layout_leaves_a_tombstone_the_sync_can_carry(app):
    call, data = app
    _, before = call("/api/layouts")
    before["layouts"] = before["layouts"]["pad"]
    call("/api/layouts/save", {"layout": dict(before["layouts"][0], id="gone", name="Gone"), "activate": True})
    status, body = call("/api/layouts/delete", {"id": "gone"})
    assert status == 200 and body["settings"]["active"] == "default", "deleting the active layout falls back"
    assert json.load(open(data / "layouts" / "gone.json")) == {"id": "gone", "deleted": True}
    assert [lay["id"] for lay in call("/api/layouts")[1]["layouts"]["pad"]] == ["default"]
    assert call("/api/layouts/delete", {"id": "default"})[0] == 404, "the built-in default has nothing to delete"


def test_full_screen_layouts_live_apart_from_controller_layouts(app):
    call, data = app
    touch = {"name": "Default", "mode": "screen", "controls": [{"id": "menu", "x": 0.9, "y": 0.1},
                                                               {"id": "k1", "key": "KeyE", "label": "End turn"}]}
    status, body = call("/api/layouts/save", {"layout": touch, "activate": True})
    assert status == 200 and body["settings"]["active_screen"] == "default" and body["settings"]["active"] == "default"
    assert (data / "touch-layouts" / "default.json").is_file() and not (data / "layouts" / "default.json").exists(), \
        "the full-screen Default is its own file: the controller's Default is untouched"
    listing = call("/api/layouts")[1]["layouts"]
    assert [c["id"] for c in listing["screen"][0]["controls"]] == ["menu", "k1"]
    assert "a" in [c["id"] for c in listing["pad"][0]["controls"]]
    call("/api/layouts/save", {"layout": {"name": "Spire", "mode": "screen", "controls": []}, "activate": True})
    assert call("/api/settings", {"mode": "screen"})[1]["settings"]["active_screen"] == "spire"
    assert call("/api/settings", {"active_screen": "nowhere"})[0] == 404
    status, body = call("/api/layouts/delete", {"id": "spire", "mode": "screen"})
    assert status == 200 and body["settings"]["active_screen"] == "default"


def test_settings_are_checked(app):
    call, _ = app
    assert call("/api/settings", {"active": "nowhere"})[0] == 404
    status, body = call("/api/settings", {"opacity": 0.4, "snap": 5})
    assert status == 200 and body["settings"] == {"active": "default", "active_screen": "default", "opacity": 0.4,
                                                  "snap": 5.0, "mirror": True, "speed": 5, "idle": 1.0,
                                                  "floating": False, "mode": "pad", "hold": 0.5}
    status, body = call("/api/settings", {"mode": "screen", "hold": 0.8})
    assert status == 200 and (body["settings"]["mode"], body["settings"]["hold"]) == ("screen", 0.8)
    listing = call("/api/layouts")[1]
    assert listing["symmetry"]["cluster"] == list(pad.CLUSTER) and ["Escape", "Esc"] in listing["codes"]
    assert call("/api/layouts/save", {"layout": {"controls": []}})[0] == 400, "a layout needs a name"


def test_off_windows_the_overlay_and_the_installer_refuse_politely(app):
    if os.name == "nt":
        pytest.skip("this asserts the non-Windows answer")
    call, _ = app
    status, body = call("/api/overlay/start", {})
    assert status == 409 and "Windows" in body["error"]
    assert call("/api/driver/install", {})[0] == 400
    assert call("/api/overlay/stop", {}) == (200, {"ok": True})
