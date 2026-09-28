"""pad — what the touches mean. Pure Python, no Windows, so every rule in here is tested on any machine.

A layout is a list of controls placed on the screen. Positions are fractions of the screen (x of the width,
y of the height, the control's centre); sizes are fractions of the screen's short side, so a layout keeps its
proportions on any resolution. Rectangular controls carry `w`, their width as a multiple of their height.

    stick     a thumbstick: the finger's offset from the centre, clamped to the rim, is the stick
    dpad      one pad, eight directions by angle, so sliding round it rolls through the diagonals
    button    a digital button: A B X Y, LB RB, Back Start Guide, LS RS (the stick clicks)
    trigger   LT RT: pressed is fully pulled
    menu      the overlay's own handle — tap to hide/show the pad, hold to close the overlay

The report is the XInput gamepad state (XUSB_REPORT): 16 button bits, two 8-bit triggers, four signed
16-bit stick axes with up positive.
"""
from __future__ import annotations

import math
import re

# XUSB_BUTTON — the bits of wButtons, as ViGEmBus and XInput define them.
BITS = {
    "up": 0x0001, "down": 0x0002, "left": 0x0004, "right": 0x0008,
    "start": 0x0010, "back": 0x0020, "l3": 0x0040, "r3": 0x0080,
    "lb": 0x0100, "rb": 0x0200, "guide": 0x0400,
    "a": 0x1000, "b": 0x2000, "x": 0x4000, "y": 0x8000,
}

# Every control the pad knows: kind, label, and the shape it is drawn and hit-tested as.
CATALOGUE = {
    "ls": ("stick", "", "circle"), "rs": ("stick", "", "circle"),
    "dpad": ("dpad", "", "square"),
    "a": ("button", "A", "circle"), "b": ("button", "B", "circle"),
    "x": ("button", "X", "circle"), "y": ("button", "Y", "circle"),
    "lb": ("button", "LB", "rect"), "rb": ("button", "RB", "rect"),
    "lt": ("trigger", "LT", "rect"), "rt": ("trigger", "RT", "rect"),
    "back": ("button", "Back", "rect"), "start": ("button", "Start", "rect"),
    "guide": ("button", "Xbox", "circle"),
    "l3": ("button", "LS", "circle"), "r3": ("button", "RS", "circle"),
    "menu": ("menu", "Pad", "circle"),
}
ORDER = list(CATALOGUE)

HOLD_TO_CLOSE = 1.2          # seconds on the menu handle that close the overlay rather than hide the pad
STICK_REACH = 1.5            # a stick is grabbed anywhere within 1.5x its radius
STICK_THROW = 0.8            # full deflection at 80 % of the radius, so the rim is easy to reach
DPAD_DEAD = 0.12             # the middle of the D-pad, as a fraction of its size, presses nothing
SLOP = 1.15                  # buttons answer a little outside their drawn edge


def _c(id_, x, y, s, w=1.0, on=True):
    return {"id": id_, "x": x, "y": y, "s": s, "w": w, "on": on}


DEFAULT = {
    "id": "default",
    "name": "Default",
    "controls": [
        _c("lt", 0.075, 0.09, 0.075, 1.8), _c("lb", 0.075, 0.20, 0.075, 1.8),
        _c("rt", 0.925, 0.09, 0.075, 1.8), _c("rb", 0.925, 0.20, 0.075, 1.8),
        _c("back", 0.42, 0.07, 0.06, 1.7), _c("menu", 0.50, 0.07, 0.07), _c("start", 0.58, 0.07, 0.06, 1.7),
        _c("guide", 0.50, 0.93, 0.06, on=False),
        _c("ls", 0.14, 0.60, 0.26), _c("l3", 0.27, 0.40, 0.075),
        _c("dpad", 0.30, 0.82, 0.21),
        _c("rs", 0.68, 0.79, 0.22), _c("r3", 0.57, 0.63, 0.075),
        _c("y", 0.870, 0.44, 0.105), _c("x", 0.797, 0.56, 0.105),
        _c("b", 0.943, 0.56, 0.105), _c("a", 0.870, 0.68, 0.105),
    ],
}

SETTINGS = {"active": "default", "opacity": 0.6}


# --------------------------------------------------------------------------------- layouts
def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "layout"


def _num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(v) else min(hi, max(lo, v))


def clean_layout(raw) -> dict:
    """Whatever the page (or a synced file) sends, what comes back is a layout the overlay can draw: every
    known control exactly once, in range; unknown ids dropped; missing ones taken from the default."""
    raw = raw if isinstance(raw, dict) else {}
    given = {c.get("id"): c for c in raw.get("controls") or [] if isinstance(c, dict)}
    out = []
    for base in DEFAULT["controls"]:
        c = given.get(base["id"]) or {}
        out.append({"id": base["id"],
                    "x": round(_num(c.get("x"), 0.0, 1.0, base["x"]), 4),
                    "y": round(_num(c.get("y"), 0.0, 1.0, base["y"]), 4),
                    "s": round(_num(c.get("s"), 0.03, 0.6, base["s"]), 4),
                    "w": round(_num(c.get("w"), 1.0, 4.0, base["w"]), 3),
                    "on": True if base["id"] == "menu" else bool(c.get("on", base["on"]))})
    name = str(raw.get("name") or "").strip()[:60] or "Untitled"
    return {"id": slug(raw.get("id") or name), "name": name, "controls": out}


def clean_settings(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"active": slug(raw.get("active") or SETTINGS["active"]),
            "opacity": round(_num(raw.get("opacity"), 0.25, 1.0, SETTINGS["opacity"]), 2)}


# --------------------------------------------------------------------------------- geometry
class Placed:
    """A control on a real screen, in pixels."""

    def __init__(self, c: dict, width: int, height: int):
        self.id = c["id"]
        self.kind, self.label, self.shape = CATALOGUE[self.id]
        unit = min(width, height)
        self.cx, self.cy = c["x"] * width, c["y"] * height
        self.h = c["s"] * unit
        self.w = self.h * (c["w"] if self.shape == "rect" else 1.0)
        self.r = self.h / 2

    def box(self, pad: float = 0.0) -> tuple:
        """(left, top, right, bottom), grown by `pad` pixels — what gets repainted."""
        half_w, half_h = self.w / 2 + pad, self.h / 2 + pad
        return (int(self.cx - half_w), int(self.cy - half_h), int(self.cx + half_w) + 1, int(self.cy + half_h) + 1)

    def hit(self, x: float, y: float) -> float | None:
        """None if the point misses; otherwise how far it is from the centre, relative to the size — the
        nearest control wins where two overlap."""
        dx, dy = x - self.cx, y - self.cy
        if self.kind == "stick":
            d = math.hypot(dx, dy) / self.r
            return d if d <= STICK_REACH else None
        if self.shape == "circle":
            d = math.hypot(dx, dy) / self.r
            return d if d <= SLOP else None
        if abs(dx) <= self.w / 2 * SLOP and abs(dy) <= self.h / 2 * SLOP:
            return max(abs(dx) / (self.w / 2), abs(dy) / (self.h / 2))
        return None


def place(layout: dict, width: int, height: int) -> list:
    return [Placed(c, width, height) for c in layout["controls"] if c["on"]]


# --------------------------------------------------------------------------------- the pad
def blank() -> dict:
    return {"buttons": 0, "lt": 0, "rt": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}


def _axis(v: float) -> int:
    return int(round(max(-1.0, min(1.0, v)) * 32767))


def dpad_bits(dx: float, dy: float, size: float) -> int:
    if math.hypot(dx, dy) < size * DPAD_DEAD:
        return 0
    sector = int(((math.degrees(math.atan2(-dy, dx)) + 22.5) % 360) // 45)   # 0 = right, counter-clockwise
    return (BITS["right"], BITS["right"] | BITS["up"], BITS["up"], BITS["up"] | BITS["left"],
            BITS["left"], BITS["left"] | BITS["down"], BITS["down"], BITS["down"] | BITS["right"])[sector]


class Pad:
    """Fingers in, report out. Each finger belongs to the control it went down on; a finger that went down on
    a button can slide onto a neighbouring button (A to B without lifting), a finger on a stick keeps the
    stick until it lifts wherever it wanders."""

    def __init__(self, layout: dict, width: int, height: int):
        self.width, self.height = width, height
        self.hidden = False
        self.fingers: dict = {}                  # pointer id -> {"on": control id or None, "x", "y", "at"}
        self.set_layout(layout)

    def set_layout(self, layout: dict) -> None:
        self.layout = clean_layout(layout)
        self.placed = place(self.layout, self.width, self.height)
        self.by_id = {p.id: p for p in self.placed}
        self.fingers.clear()

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.set_layout(self.layout)

    def visible(self) -> list:
        return [p for p in self.placed if p.kind == "menu"] if self.hidden else self.placed

    def _nearest(self, x: float, y: float, kinds=None):
        best, best_d = None, None
        for p in self.visible():
            if kinds and p.kind not in kinds:
                continue
            d = p.hit(x, y)
            if d is not None and (best_d is None or d < best_d):
                best, best_d = p, d
        return best

    # ---- the three things a finger does. Each returns the ids whose drawing changed.
    def down(self, pid: int, x: float, y: float, now: float) -> set:
        p = self._nearest(x, y)
        if p is None:
            return set()
        self.fingers[pid] = {"on": p.id, "x": x, "y": y, "at": now, "kind": p.kind}
        return {p.id}

    def move(self, pid: int, x: float, y: float) -> set:
        f = self.fingers.get(pid)
        if f is None:
            return set()
        changed = set()
        f["x"], f["y"] = x, y
        if f["kind"] == "button":                            # slide between buttons
            p = self._nearest(x, y, kinds=("button",))
            new = p.id if p else None
            if new != f["on"]:
                changed |= {i for i in (f["on"], new) if i}
                f["on"] = new
        elif f["on"]:
            changed.add(f["on"])
        return changed

    def up(self, pid: int, now: float) -> tuple:
        """(changed ids, what the menu handle asks for: None, "toggle" or "close")."""
        f = self.fingers.pop(pid, None)
        if f is None:
            return set(), None
        if f["kind"] == "menu":
            return {"menu"}, "close" if now - f["at"] >= HOLD_TO_CLOSE else self.toggle()
        return ({f["on"]} if f["on"] else set()), None

    def toggle(self) -> str:
        self.hidden = not self.hidden
        self.fingers = {k: f for k, f in self.fingers.items() if f["kind"] == "menu"}
        return "toggle"

    def held_menu_since(self) -> float | None:
        times = [f["at"] for f in self.fingers.values() if f["kind"] == "menu"]
        return min(times) if times else None

    def release_all(self) -> None:
        self.fingers.clear()

    # ---- what the controller reports, and what each control looks like
    def pressed(self) -> set:
        return {f["on"] for f in self.fingers.values() if f["on"]}

    def stick_vector(self, sid: str) -> tuple:
        p = self.by_id.get(sid)
        for f in self.fingers.values():
            if f["on"] == sid and p is not None:
                dx, dy = (f["x"] - p.cx) / (p.r * STICK_THROW), (f["y"] - p.cy) / (p.r * STICK_THROW)
                n = math.hypot(dx, dy)
                return (dx / n, dy / n) if n > 1 else (dx, dy)
        return 0.0, 0.0

    def dpad_state(self) -> int:
        p = self.by_id.get("dpad")
        bits = 0
        for f in self.fingers.values():
            if f["on"] == "dpad" and p is not None:
                bits |= dpad_bits(f["x"] - p.cx, f["y"] - p.cy, p.h)
        return bits

    def report(self) -> dict:
        r = blank()
        for cid in self.pressed():
            if cid in BITS:
                r["buttons"] |= BITS[cid]
        r["buttons"] |= self.dpad_state()
        r["lt"] = 255 if "lt" in self.pressed() else 0
        r["rt"] = 255 if "rt" in self.pressed() else 0
        lx, ly = self.stick_vector("ls")
        rx, ry = self.stick_vector("rs")
        r["lx"], r["ly"], r["rx"], r["ry"] = _axis(lx), _axis(-ly), _axis(rx), _axis(-ry)   # up is positive
        return r
