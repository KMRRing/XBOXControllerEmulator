"""pad — what the touches mean. Pure Python, no Windows, so every rule in here is tested on any machine.

A layout is a list of controls placed on the screen. Positions are fractions of the screen (x of the width,
y of the height, the control's centre); sizes are fractions of the screen's short side, so a layout keeps its
proportions on any resolution. Rectangular controls carry `w`, their width as a multiple of their height.

    stick      a thumbstick: the finger's offset from the centre, clamped to the rim, is the stick
    dpad       one pad, eight directions by angle, so sliding round it rolls through the diagonals
    button     a digital button: A B X Y, LB RB, Back Start Guide, LS RS (the stick clicks)
    trigger    LT RT: pressed is fully pulled
    trackpad   a laptop trackpad: drag moves the mouse cursor, tap clicks, two-finger tap right-clicks,
               tap-then-drag drags, two fingers up and down scroll
    click      the mouse's own buttons, for holding while the trackpad moves
    key        a keyboard key, chosen per control (K1..K4)
    menu       the overlay's own handle — tap to hide/show the pad, hold to close the overlay

Two things come out. The report is the XInput gamepad state (XUSB_REPORT: 16 button bits, two 8-bit
triggers, four signed 16-bit stick axes with up positive). The events are what the trackpad, the mouse
buttons and the keys ask the overlay to inject: ("move", dx, dy), ("down"|"up"|"click", "left"|"right"),
("wheel", notches), ("keydown"|"keyup", virtual key).
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
    "trackpad": ("trackpad", "", "rect"),
    "lmb": ("click", "LMB", "rect"), "rmb": ("click", "RMB", "rect"),
    "k1": ("key", "K1", "circle"), "k2": ("key", "K2", "circle"), "k3": ("key", "K3", "circle"), "k4": ("key", "K4", "circle"),
    "menu": ("menu", "Pad", "circle"),
}
ORDER = list(CATALOGUE)
MOUSE_BUTTON = {"lmb": "left", "rmb": "right"}

# The keys a key control can be: name -> Windows virtual-key code. Extended keys carry the flag the
# keyboard driver would; games reading scan codes tell the arrows from the numeric keypad by it.
KEYS = {"Escape": 0x1B, "Enter": 0x0D, "Space": 0x20, "Tab": 0x09, "Backspace": 0x08, "Shift": 0x10,
        "Ctrl": 0x11, "Alt": 0x12, "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
        "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22, "Insert": 0x2D, "Delete": 0x2E}
KEYS.update({f"F{n}": 0x6F + n for n in range(1, 13)})
KEYS.update({str(n): 0x30 + n for n in range(10)})
KEYS.update({chr(c): c for c in range(0x41, 0x5B)})
EXTENDED = {"Up", "Down", "Left", "Right", "Home", "End", "PageUp", "PageDown", "Insert", "Delete"}
DEFAULT_KEY = {"k1": "Escape", "k2": "Space", "k3": "Enter", "k4": "Tab"}

HOLD_TO_CLOSE = 1.2          # seconds on the menu handle that close the overlay rather than hide the pad
STICK_REACH = 1.5            # a stick is grabbed anywhere within 1.5x its radius
STICK_THROW = 0.8            # full deflection at 80 % of the radius, so the rim is easy to reach
DPAD_DEAD = 0.12             # the middle of the D-pad, as a fraction of its size, presses nothing
SLOP = 1.15                  # buttons answer a little outside their drawn edge
TAP_MAX = 0.25               # a touch shorter than this that did not travel is a tap
TAP_SLOP = 0.008             # ...where "did not travel" means less than this much of the short side
TAP_DRAG = 0.30              # a finger back down within this long after a tap holds the button and drags
SCROLL_NOTCH = 0.025         # two fingers travelling this much of the short side is one wheel notch
ACCEL_REF = 0.006            # per-event travel (of the short side) past which the cursor gains speed


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
        _c("ls", 0.135, 0.56, 0.26), _c("l3", 0.27, 0.36, 0.075),
        _c("dpad", 0.30, 0.82, 0.21),
        _c("rs", 0.70, 0.82, 0.22), _c("r3", 0.73, 0.36, 0.075),
        _c("y", 0.865, 0.44, 0.105), _c("x", 0.785, 0.56, 0.105),
        _c("b", 0.945, 0.56, 0.105), _c("a", 0.865, 0.68, 0.105),
        _c("trackpad", 0.50, 0.55, 0.30, 1.6, on=False),
        _c("lmb", 0.44, 0.80, 0.06, 1.6, on=False), _c("rmb", 0.56, 0.80, 0.06, 1.6, on=False),
        _c("k1", 0.40, 0.24, 0.065, on=False), _c("k2", 0.47, 0.24, 0.065, on=False),
        _c("k3", 0.53, 0.24, 0.065, on=False), _c("k4", 0.60, 0.24, 0.065, on=False),
    ],
}

# The editor's symmetry: each left control and the right one that follows it. TWINS match position, size and
# width; CENTRE_TWINS match position only. The face buttons move and size as one cluster; their centre is what
# mirrors the left stick, the way an Xbox pad is laid out (stick opposite the face buttons, D-pad opposite the
# right stick).
TWINS = (("lt", "rt"), ("lb", "rb"), ("back", "start"), ("l3", "r3"), ("lmb", "rmb"))
CLUSTER = ("a", "b", "x", "y")
CENTRE_TWINS = (("ls", "cluster"), ("dpad", "rs"))
SNAP_STEPS = (0.0, 1.0, 2.5, 5.0)                 # per cent of the screen's short side; 0 is off

SETTINGS = {"active": "default", "opacity": 0.6, "snap": 2.5, "mirror": True,
            "speed": 5, "idle": 1.0, "floating": False}


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
        clean = {"id": base["id"],
                 "x": round(_num(c.get("x"), 0.0, 1.0, base["x"]), 4),
                 "y": round(_num(c.get("y"), 0.0, 1.0, base["y"]), 4),
                 "s": round(_num(c.get("s"), 0.03, 0.6, base["s"]), 4),
                 "w": round(_num(c.get("w"), 1.0, 4.0, base["w"]), 3),
                 "on": True if base["id"] == "menu" else bool(c.get("on", base["on"]))}
        if base["id"] in DEFAULT_KEY:
            key = c.get("key")
            clean["key"] = key if key in KEYS else DEFAULT_KEY[base["id"]]
        out.append(clean)
    name = str(raw.get("name") or "").strip()[:60] or "Untitled"
    return {"id": slug(raw.get("id") or name), "name": name, "controls": out}


def clean_settings(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    snap = _num(raw.get("snap"), 0.0, 5.0, SETTINGS["snap"])
    return {"active": slug(raw.get("active") or SETTINGS["active"]),
            "opacity": round(_num(raw.get("opacity"), 0.25, 1.0, SETTINGS["opacity"]), 2),
            "snap": min(SNAP_STEPS, key=lambda step: abs(step - snap)),
            "mirror": bool(raw.get("mirror", SETTINGS["mirror"])),
            "speed": int(round(_num(raw.get("speed"), 1, 10, SETTINGS["speed"]))),
            "idle": round(_num(raw.get("idle"), 0.0, 1.0, SETTINGS["idle"]), 2),
            "floating": bool(raw.get("floating", SETTINGS["floating"]))}


# --------------------------------------------------------------------------------- geometry
class Placed:
    """A control on a real screen, in pixels."""

    def __init__(self, c: dict, width: int, height: int):
        self.id = c["id"]
        self.kind, self.label, self.shape = CATALOGUE[self.id]
        if self.kind == "key":
            self.key = c["key"]
            self.label = {"Escape": "Esc", "Backspace": "Bksp", "PageUp": "PgUp", "PageDown": "PgDn",
                          "Insert": "Ins", "Delete": "Del", "Space": "Spc"}.get(self.key, self.key)
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
        slop = 1.0 if self.kind == "trackpad" else SLOP
        if abs(dx) <= self.w / 2 * slop and abs(dy) <= self.h / 2 * slop:
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
    """Fingers in, report and events out. Each finger belongs to the control it went down on; a finger that
    went down on a button can slide onto a neighbouring button (A to B without lifting), a finger on a stick
    or the trackpad keeps it until it lifts wherever it wanders."""

    def __init__(self, layout: dict, width: int, height: int, options: dict | None = None):
        self.width, self.height = width, height
        self.hidden = False
        self.fingers: dict = {}                  # pointer id -> {"on": control id or None, "x", "y", "at", ...}
        self.events: list = []                   # what the overlay injects, in order
        self.last_tap = -1.0                     # when the trackpad was last tapped: a quick return drags
        self.scroll = 0.0                        # two-finger travel not yet turned into wheel notches
        self.carry = [0.0, 0.0]                  # sub-pixel cursor movement, saved for the next event
        self.set_options(options)
        self.set_layout(layout)

    def set_options(self, options: dict | None) -> None:
        o = clean_settings(options or {})
        self.speed, self.floating = o["speed"], o["floating"]

    def set_layout(self, layout: dict) -> None:
        self.layout = clean_layout(layout)
        self.placed = place(self.layout, self.width, self.height)
        self.by_id = {p.id: p for p in self.placed}
        self.release_all()

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.set_layout(self.layout)

    @property
    def unit(self) -> float:
        return min(self.width, self.height)

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

    def take_events(self) -> list:
        out, self.events = self.events, []
        return out

    # ---- the three things a finger does. Each returns the ids whose drawing changed.
    def down(self, pid: int, x: float, y: float, now: float) -> set:
        p = self._nearest(x, y)
        if p is None:
            return set()
        f = {"on": p.id, "x": x, "y": y, "at": now, "kind": p.kind, "x0": x, "y0": y, "moved": False}
        if p.kind == "stick":
            f["origin"] = (x, y) if self.floating else (p.cx, p.cy)
        elif p.kind == "trackpad":
            f["drag"] = now - self.last_tap <= TAP_DRAG and not self._trackpad_fingers()
            if f["drag"]:
                self.events.append(("down", "left"))
        elif p.kind == "click":
            self.events.append(("down", MOUSE_BUTTON[p.id]))
        elif p.kind == "key":
            self.events.append(("keydown", p.key))
        self.fingers[pid] = f
        return {p.id}

    def move(self, pid: int, x: float, y: float) -> set:
        f = self.fingers.get(pid)
        if f is None:
            return set()
        changed = set()
        dx, dy = x - f["x"], y - f["y"]
        f["x"], f["y"] = x, y
        if math.hypot(x - f["x0"], y - f["y0"]) > TAP_SLOP * self.unit:
            f["moved"] = True
        if f["kind"] == "button":                            # slide between buttons
            p = self._nearest(x, y, kinds=("button",))
            new = p.id if p else None
            if new != f["on"]:
                changed |= {i for i in (f["on"], new) if i}
                f["on"] = new
        elif f["kind"] == "trackpad":
            self._trackpad_move(f, dx, dy)
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
        if f["kind"] == "trackpad":
            self._trackpad_up(f, now)
        elif f["kind"] == "click":
            self.events.append(("up", MOUSE_BUTTON[f["on"]]))
        elif f["kind"] == "key":
            self.events.append(("keyup", self.by_id[f["on"]].key))
        return ({f["on"]} if f["on"] else set()), None

    # ---- the trackpad
    def _trackpad_fingers(self) -> list:
        return [f for f in self.fingers.values() if f["kind"] == "trackpad"]

    def _trackpad_move(self, f: dict, dx: float, dy: float) -> None:
        others = [g for g in self._trackpad_fingers() if g is not f]
        if others:                                           # two fingers: scroll, and nobody taps
            for g in others:
                g["moved"] = True
            self.scroll += dy
            notch = SCROLL_NOTCH * self.unit
            notches = int(self.scroll / notch)
            if notches:
                self.scroll -= notches * notch
                self.events.append(("wheel", notches))       # finger down, content down: natural scrolling
            return
        travel = math.hypot(dx, dy)
        gain = 0.4 * self.speed * (1.0 + min(2.0, travel / (ACCEL_REF * self.unit)))
        mx, my = dx * gain + self.carry[0], dy * gain + self.carry[1]
        ix, iy = int(mx), int(my)
        self.carry = [mx - ix, my - iy]
        if ix or iy:
            self.events.append(("move", ix, iy))

    def _trackpad_up(self, f: dict, now: float) -> None:
        if f.get("drag"):
            self.events.append(("up", "left"))
            return
        tapped = not f["moved"] and now - f["at"] <= TAP_MAX
        if not tapped:
            return
        others = self._trackpad_fingers()
        if others and all(not g["moved"] and now - g["at"] <= TAP_MAX + 0.2 for g in others):
            for g in others:                                 # the second finger's lift is the same tap
                g["moved"] = True
            self.events.append(("click", "right"))
            return
        if any(g.get("moved") for g in others):
            return
        self.events.append(("click", "left"))
        self.last_tap = now

    # ---- the menu handle
    def toggle(self) -> str:
        self.hidden = not self.hidden
        kept = {k: f for k, f in self.fingers.items() if f["kind"] == "menu"}
        self.fingers = {k: f for k, f in self.fingers.items() if f["kind"] != "menu"}
        self.release_all()
        self.fingers = kept
        return "toggle"

    def held_menu_since(self) -> float | None:
        times = [f["at"] for f in self.fingers.values() if f["kind"] == "menu"]
        return min(times) if times else None

    def release_all(self) -> None:
        """Let go of everything, and let go of anything held in the game with it."""
        for f in self.fingers.values():
            if f["kind"] == "click":
                self.events.append(("up", MOUSE_BUTTON[f["on"]]))
            elif f["kind"] == "key" and f["on"] in self.by_id:
                self.events.append(("keyup", self.by_id[f["on"]].key))
            elif f["kind"] == "trackpad" and f.get("drag"):
                self.events.append(("up", "left"))
        self.fingers = {}
        self.scroll = 0.0

    # ---- what the controller reports, and what each control looks like
    def pressed(self) -> set:
        return {f["on"] for f in self.fingers.values() if f["on"]}

    def stick_vector(self, sid: str) -> tuple:
        p = self.by_id.get(sid)
        for f in self.fingers.values():
            if f["on"] == sid and p is not None:
                ox, oy = f["origin"]
                dx, dy = (f["x"] - ox) / (p.r * STICK_THROW), (f["y"] - oy) / (p.r * STICK_THROW)
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
