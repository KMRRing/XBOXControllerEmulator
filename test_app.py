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
    assert pad.clean_settings({"opacity": 3, "active": "My Pad", "snap": 2.4, "mirror": 0}) == {
        "active": "my-pad", "opacity": 1.0, "snap": 2.5, "mirror": False}
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
    assert [lay["id"] for lay in before["layouts"]] == ["default"]
    lay = dict(before["layouts"][0], name="Wide", id="Wide Pad!")
    lay["controls"][0]["x"] = 0.33
    status, saved = call("/api/layouts/save", {"layout": lay, "activate": True})
    assert status == 200 and saved["layout"]["id"] == "wide-pad" and saved["settings"]["active"] == "wide-pad"
    assert (data / "layouts" / "wide-pad.json").is_file(), "one file per layout, named by its id"
    default = dict(before["layouts"][0], name="Default")
    default["controls"][0]["y"] = 0.5
    call("/api/layouts/save", {"layout": default})
    _, after = call("/api/layouts")
    assert [lay["id"] for lay in after["layouts"]] == ["default", "wide-pad"]
    assert after["layouts"][0]["controls"][0]["y"] == 0.5, "default.json overrides the built-in default"
    assert after["settings"]["active"] == "wide-pad"


def test_deleting_a_layout_leaves_a_tombstone_the_sync_can_carry(app):
    call, data = app
    _, before = call("/api/layouts")
    call("/api/layouts/save", {"layout": dict(before["layouts"][0], id="gone", name="Gone"), "activate": True})
    status, body = call("/api/layouts/delete", {"id": "gone"})
    assert status == 200 and body["settings"]["active"] == "default", "deleting the active layout falls back"
    assert json.load(open(data / "layouts" / "gone.json")) == {"id": "gone", "deleted": True}
    assert [lay["id"] for lay in call("/api/layouts")[1]["layouts"]] == ["default"]
    assert call("/api/layouts/delete", {"id": "default"})[0] == 404, "the built-in default has nothing to delete"


def test_settings_are_checked(app):
    call, _ = app
    assert call("/api/settings", {"active": "nowhere"})[0] == 404
    status, body = call("/api/settings", {"opacity": 0.4, "snap": 5})
    assert status == 200 and body["settings"] == {"active": "default", "opacity": 0.4, "snap": 5.0, "mirror": True}
    assert call("/api/layouts")[1]["symmetry"]["cluster"] == list(pad.CLUSTER)
    assert call("/api/layouts/save", {"layout": {"controls": []}})[0] == 400, "a layout needs a name"


def test_off_windows_the_overlay_and_the_installer_refuse_politely(app):
    if os.name == "nt":
        pytest.skip("this asserts the non-Windows answer")
    call, _ = app
    status, body = call("/api/overlay/start", {})
    assert status == 409 and "Windows" in body["error"]
    assert call("/api/driver/install", {})[0] == 400
    assert call("/api/overlay/stop", {}) == (200, {"ok": True})
