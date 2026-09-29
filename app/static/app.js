const $ = (s) => document.querySelector(s);
const esc = (t) => String(t).replace(/[<&>"]/g, (c) => ({ "<": "&lt;", "&": "&amp;", ">": "&gt;", '"': "&quot;" }[c]));
const post = async (url, body) => {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `${r.status}`);
  return j;
};
const slug = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "layout";
const copy = (o) => JSON.parse(JSON.stringify(o));

const NAMES = { ls: "Left stick", rs: "Right stick", dpad: "D-pad", a: "A", b: "B", x: "X", y: "Y", lb: "LB", rb: "RB",
  lt: "LT", rt: "RT", back: "Back", start: "Start", guide: "Xbox", l3: "LS click", r3: "RS click", menu: "On/Off handle",
  trackpad: "Trackpad", lmb: "Left mouse button", rmb: "Right mouse button" };
const KEY = { kind: "key", label: "", shape: "rect" };           // key buttons are added: ids k1..k24
const MODIFIERS = ["ControlLeft", "ControlRight", "ShiftLeft", "ShiftRight", "AltLeft", "AltRight"];
let codes = [], LABEL = {}, maxKeys = 24;
const BITS = [["a", 0x1000, "A"], ["b", 0x2000, "B"], ["x", 0x4000, "X"], ["y", 0x8000, "Y"], ["lb", 0x100, "LB"],
  ["rb", 0x200, "RB"], ["lt", 0, "LT"], ["rt", 0, "RT"], ["back", 0x20, "Back"], ["start", 0x10, "Start"],
  ["guide", 0x400, "Xbox"], ["l3", 0x40, "LS"], ["r3", 0x80, "RS"], ["up", 1, "↑"], ["down", 2, "↓"],
  ["left", 4, "←"], ["right", 8, "→"]];

let status = null, catalogue = {}, all = [], settings = {}, saved = null, edit = null, sel = null, dirty = false;

// --------------------------------------------------------------------------------- the deck
function check(id, cls, say) {
  const li = $(id);
  li.className = cls;
  li.querySelector(".say").textContent = say;
}

const mode = () => (settings && settings.mode) || "pad";

function paintStatus(s) {
  status = s;
  const o = s.overlay, drv = s.driver, touch = mode() === "screen";
  const btn = $("#pad");
  btn.disabled = !s.windows;
  btn.textContent = touch ? (o.running ? "Stop full-screen touch" : "Start full-screen touch")
    : (o.running ? "Take the pad down" : "Show the pad");
  btn.classList.toggle("up", o.running);

  check("#c-driver", drv.ok ? "ok" : s.windows && !touch ? "bad" : "",
    drv.ok ? "ViGEmBus installed" : touch ? "not needed for full-screen touch" : drv.error || "not installed");
  $("#install").hidden = drv.ok || !s.windows || !s.installer;
  if (touch) check("#c-controller", "", "unplugged: full-screen touch is mouse input");
  else check("#c-controller", o.running ? (o.pad_error ? "bad" : "ok") : "",
    o.running ? (o.pad_error ? o.pad_error : "Xbox 360 controller plugged in") : "plugged in while the pad is up");
  check("#c-screen", s.screen ? "ok" : "", s.screen ? `${s.screen.w} × ${s.screen.h}` : "not known");

  let line;
  if (!s.windows) line = "This runs on the Surface: the pad and the driver are Windows only.";
  else if (o.error) line = o.error;
  else if (touch) line = !o.running ? "Ready. Start the game, then start full-screen touch."
    : o.hidden ? "Full-screen touch is paused. Tap On on the screen to take it back."
      : "Full-screen touch is on: a tap clicks where you tap.";
  else if (o.running && o.pad_error) line = "The pad is up, but no controller reaches the game.";
  else if (o.running) line = o.hidden ? "The pad is folded away. Tap Pad on the screen to bring it back."
    : "The pad is up. The game sees an Xbox 360 controller.";
  else if (!drv.ok) line = "Install the driver once, then show the pad.";
  else line = "Ready. Start the game, then show the pad.";
  $("#headline").textContent = line;

  const aspect = s.screen ? `${s.screen.w} / ${s.screen.h}` : "3 / 2";
  if ($("#stage").style.aspectRatio !== aspect) { $("#stage").style.aspectRatio = aspect; drawStage(); }
}

function paintMode() {
  const touch = mode() === "screen";
  for (const el of document.querySelectorAll(".pad-only")) el.hidden = touch;
  for (const el of document.querySelectorAll(".screen-only")) el.hidden = !touch;
  $("#edit-title").textContent = touch ? "Full-screen touch layout" : "Controller layout";
  for (const r of document.querySelectorAll('input[name="mode"]')) r.checked = r.value === mode();
  $("#hold-f").hidden = !touch;
  $("#hint-pad").hidden = touch;
  $("#hint-screen").hidden = !touch;
  $("#hold").value = settings.hold;
  $("#hold-out").textContent = `${Number(settings.hold).toFixed(1)} s`;
  if (status) paintStatus(status);
}

for (const r of document.querySelectorAll('input[name="mode"]')) {
  r.onchange = async () => {
    if (dirty && !confirm("Drop the unsaved changes to this layout?")) { paintMode(); return; }
    await setting({ mode: r.value });
    sel = null;
    await loadLayouts();                                           // the other mode's layouts, and its active one
  };
}
$("#hold").oninput = (e) => { $("#hold-out").textContent = `${Number(e.target.value).toFixed(1)} s`; };
$("#hold").onchange = (e) => setting({ hold: +e.target.value });

async function refresh() {
  try { paintStatus(await (await fetch("/api/status")).json()); }
  catch { $("#headline").textContent = "The app stopped. Close this tab."; }
}

$("#pad").onclick = async () => {
  const b = $("#pad");
  b.disabled = true;
  try {
    if (status && status.overlay.running) await post("/api/overlay/stop");
    else await post("/api/overlay/start");
  } catch (e) { $("#headline").textContent = e.message; }
  await refresh();
};

$("#install").onclick = async () => {
  try {
    await post("/api/driver/install");
    check("#c-driver", "warn", "installer open — approve the prompt, then Next and Install");
  } catch (e) { check("#c-driver", "bad", e.message); }
};

// --------------------------------------------------------------------------------- what the game sees
$("#pills").innerHTML = BITS.map(([k, , label]) => `<span data-b="${k}">${label}</span>`).join("");

function paintPad(r) {
  for (const [k, bit] of BITS) {
    const on = k === "lt" ? r.lt > 0 : k === "rt" ? r.rt > 0 : (r.buttons & bit) !== 0;
    $(`#pills [data-b="${k}"]`).classList.toggle("on", on);
  }
  for (const [s, x, y] of [["l", r.lx, r.ly], ["r", r.rx, r.ry]]) {
    const el = $(`.stick[data-s="${s}"]`);
    el.classList.toggle("on", x !== 0 || y !== 0);
    el.querySelector("i").style.transform = `translate(${(x / 32767) * 1.45}rem, ${(-y / 32767) * 1.45}rem)`;
  }
}

async function pollPad() {
  if (status && status.overlay.running && !document.hidden) {
    try {
      const p = await (await fetch("/api/pad")).json();
      paintPad(p.report);
      if (!p.running) refresh();
    } catch { /* the next status poll says why */ }
    setTimeout(pollPad, 100);
  } else {
    setTimeout(pollPad, 700);
  }
}

// --------------------------------------------------------------------------------- the layout editor
// Positions are fractions of the screen and sizes fractions of its short side ("unit"). The editor works in
// pixels of the real screen, so a grid step is the same distance up and down and across, then stores fractions.
let symmetry = { twins: [], cluster: [], centre_twins: [], snap_steps: [0, 1, 2.5, 5] };

function screen() { return status && status.screen ? status.screen : { w: 1500, h: 1000 }; }
const cat = (id) => catalogue[id] || KEY;
const nameOf = (c) => (cat(c.id).kind === "key" ? `Key ${c.label}` : NAMES[c.id] || c.id);
const unit = () => Math.min(screen().w, screen().h);
const gridPx = () => (settings.snap || 0) / 100 * unit();
const toPx = (c) => ({ x: c.x * screen().w, y: c.y * screen().h });
const clamp01 = (v) => Math.min(1, Math.max(0, v));
const round4 = (v) => Math.round(v * 1e4) / 1e4;
function setPx(c, x, y) {
  c.x = round4(clamp01(x / screen().w));
  c.y = round4(clamp01(y / screen().h));
}
function snapPx(v) { const g = gridPx(); return g ? Math.round(v / g) * g : v; }
const ctl = (id) => edit.controls.find((k) => k.id === id);
const inCluster = (id) => symmetry.cluster.includes(id);
const movingSet = (id) => (inCluster(id) ? symmetry.cluster : [id]).map(ctl);

function clusterCentre() {
  const pts = symmetry.cluster.map((id) => toPx(ctl(id)));
  return { x: pts.reduce((a, p) => a + p.x, 0) / pts.length, y: pts.reduce((a, p) => a + p.y, 0) / pts.length };
}

function clusterSpread() {
  const c = clusterCentre();
  return symmetry.cluster.map((id) => toPx(ctl(id))).reduce((a, p) => a + Math.hypot(p.x - c.x, p.y - c.y), 0)
    / symmetry.cluster.length / unit() * 100;
}

const DIAMOND = { y: [0, -1], a: [0, 1], x: [-1, 0], b: [1, 0] };

function setClusterSpread(pct) {
  const c = clusterCentre(), d = pct / 100 * unit();
  for (const id of symmetry.cluster) {
    const p = toPx(ctl(id)), len = Math.hypot(p.x - c.x, p.y - c.y);
    const dir = len > 1 ? [(p.x - c.x) / len, (p.y - c.y) / len] : DIAMOND[id];
    setPx(ctl(id), c.x + dir[0] * d, c.y + dir[1] * d);
  }
}

function moveCluster(dx, dy) {
  for (const id of symmetry.cluster) { const p = toPx(ctl(id)); setPx(ctl(id), p.x + dx, p.y + dy); }
}

// After `id` changed, bring its twin along. Twins match in place, size and width; centre twins in place only.
function follow(id) {
  if (!settings.mirror) return;
  const mirrorX = (x) => round4(1 - x);
  for (const [left, right] of symmetry.twins) {
    if (id !== left && id !== right) continue;
    const from = ctl(id), to = ctl(id === left ? right : left);
    Object.assign(to, { x: mirrorX(from.x), y: from.y, s: from.s, w: from.w, on: from.on });
  }
  for (const [left, right] of symmetry.centre_twins) {
    const leftIs = id === left, rightIs = right === "cluster" ? inCluster(id) : id === right;
    if (!leftIs && !rightIs) continue;
    if (right === "cluster") {
      if (leftIs) {                                                // the stick moved: carry the cluster
        const c = clusterCentre(), want = toPx({ x: mirrorX(ctl(left).x), y: ctl(left).y });
        moveCluster(want.x - c.x, want.y - c.y);
      } else {                                                     // the cluster moved: place the stick
        const c = clusterCentre();
        ctl(left).x = mirrorX(c.x / screen().w);
        ctl(left).y = round4(c.y / screen().h);
      }
    } else {
      const from = ctl(id), to = ctl(leftIs ? right : left);
      to.x = mirrorX(from.x);
      to.y = from.y;
    }
  }
}

function place(el, c) {
  const { w, h } = screen(), shape = cat(c.id).shape;
  el.style.left = `${c.x * 100}%`;
  el.style.top = `${c.y * 100}%`;
  el.style.width = `${(c.s * unit() * (shape === "rect" ? c.w : 1)) / w * 100}%`;
  el.style.height = `${(c.s * unit()) / h * 100}%`;
  el.classList.toggle("off", !c.on);
  el.classList.toggle("sel", sel === c.id || (sel && inCluster(sel) && inCluster(c.id)));
}

function placeAll() {
  for (const el of document.querySelectorAll(".ctl")) place(el, ctl(el.dataset.id));
}

function paintGrid() {
  const stage = $("#stage"), g = gridPx();
  stage.classList.toggle("grid", g > 0);
  stage.style.backgroundSize = g ? `${g / screen().w * 100}% ${g / screen().h * 100}%` : "";
}

function drawStage() {
  if (!edit) return;
  const stage = $("#stage");
  stage.innerHTML = "";
  for (const c of edit.controls) {
    const k = cat(c.id), el = document.createElement("div");
    el.className = `ctl ${k.shape}${k.kind === "stick" ? " stick-base" : ""}`;
    el.dataset.id = c.id;
    el.textContent = k.kind === "menu" ? (mode() === "screen" ? "Off" : "Hide") : k.kind === "key" ? c.label
      : k.kind === "trackpad" ? "trackpad" : k.label;
    el.setAttribute("aria-label", nameOf(c));
    if (k.kind === "key" && c.label.length > 3) el.style.fontSize = "0.8em";
    place(el, c);
    el.addEventListener("pointerdown", (e) => grab(e, c, el));
    stage.appendChild(el);
  }
  paintGrid();
  inspect();
}

function select(id) {
  if (id !== sel) stopRecording();                               // a recording belongs to the key it started on
  sel = id;
  placeAll();
  inspect();
}

function grab(e, c, el) {
  e.preventDefault();
  $("#stage").focus({ preventScroll: true });                    // so the arrow keys nudge, not a number box
  select(c.id);
  const box = $("#stage").getBoundingClientRect();
  const scale = { x: screen().w / box.width, y: screen().h / box.height };     // page pixels → screen pixels
  const group = movingSet(c.id), start = group.map(toPx), from = toPx(c);
  const grabAt = { x: e.clientX, y: e.clientY };
  el.setPointerCapture(e.pointerId);
  const move = (m) => {
    const want = { x: snapPx(from.x + (m.clientX - grabAt.x) * scale.x), y: snapPx(from.y + (m.clientY - grabAt.y) * scale.y) };
    const dx = want.x - from.x, dy = want.y - from.y;
    group.forEach((k, i) => setPx(k, start[i].x + dx, start[i].y + dy));
    follow(c.id);
    placeAll();
    inspect();
    touched();
  };
  const done = () => {
    el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", done); el.removeEventListener("pointercancel", done);
  };
  el.addEventListener("pointermove", move);
  el.addEventListener("pointerup", done);
  el.addEventListener("pointercancel", done);
}

function inspect() {
  const c = edit && sel && ctl(sel);
  $("#inspector").hidden = !c;
  if (!c) { stopRecording(); return; }
  const k = cat(c.id), cluster = inCluster(c.id), key = k.kind === "key";
  $("#i-name").textContent = cluster ? "A B X Y" : key ? "Key button" : nameOf(c);
  $("#i-size").value = $("#i-size-n").value = Math.round(c.s * 200) / 2;
  $("#i-wide").value = $("#i-wide-n").value = c.w;
  $("#i-wide-f").hidden = k.shape !== "rect";
  $("#i-spread-f").hidden = !cluster;
  if (cluster) $("#i-spread").value = $("#i-spread-n").value = Math.round(clusterSpread() * 2) / 2;
  $("#i-x").value = Math.round(c.x * 1000) / 10;
  $("#i-y").value = Math.round(c.y * 1000) / 10;
  $("#i-on-f").hidden = key || c.id === "menu";
  $("#i-on").checked = c.on;
  $("#i-key-f").hidden = !key;
  if (key) {
    const parts = c.key.split("+"), main = parts.filter((p) => !MODIFIERS.includes(p));
    $("#i-chord").textContent = chordLabel(c.key);
    $("#i-key").value = main[0] || parts[parts.length - 1];
    $("#i-ctrl").checked = parts.includes("ControlLeft");
    $("#i-shift").checked = parts.includes("ShiftLeft");
    $("#i-alt").checked = parts.includes("AltLeft");
    if (document.activeElement !== $("#i-label")) $("#i-label").value = c.label;
  }
}

function changed() {
  follow(sel);
  placeAll();
  inspect();
  touched();
}

function bindPair(slider, number, apply) {
  const on = (e) => { const v = +e.target.value; if (Number.isFinite(v)) { apply(v); } };
  $(slider).oninput = on;
  $(number).onchange = on;
}
bindPair("#i-size", "#i-size-n", (v) => { for (const k of movingSet(sel)) k.s = Math.min(0.4, Math.max(0.03, v / 100)); changed(); });
bindPair("#i-wide", "#i-wide-n", (v) => { ctl(sel).w = Math.min(4, Math.max(1, v)); changed(); });
bindPair("#i-spread", "#i-spread-n", (v) => { setClusterSpread(Math.min(30, Math.max(5, v))); changed(); });
$("#i-x").onchange = (e) => { const p = toPx(ctl(sel)); nudgeTo(clamp01(+e.target.value / 100) * screen().w, p.y); };
$("#i-y").onchange = (e) => { const p = toPx(ctl(sel)); nudgeTo(p.x, clamp01(+e.target.value / 100) * screen().h); };
$("#i-on").onchange = (e) => { for (const k of movingSet(sel)) k.on = e.target.checked; changed(); };

// ---- key buttons: a key or a combination, recorded from the keyboard or picked
const chordLabel = (chord) => chord.split("+").map((p) => LABEL[p] || p).join("+");

// A button wide enough for its label: grown when a longer label arrives, never shrunk behind the user's back.
const fitWidth = (c) => { c.w = Math.max(c.w, Math.min(4, Math.round((0.35 + 0.2 * c.label.length) * 10) / 10)); };

function relabel(c) {
  const el = document.querySelector(`.ctl[data-id="${c.id}"]`);
  el.textContent = c.label;
  el.style.fontSize = c.label.length > 3 ? "0.8em" : "";
}

function bind(chord, label) {
  const c = ctl(sel);
  if (!c || cat(c.id).kind !== "key") return;
  c.key = chord;
  c.label = (label || chordLabel(chord)).slice(0, 16);
  fitWidth(c);
  relabel(c);
  stopRecording();
  changed();
}

function pickedChord() {
  const main = $("#i-key").value;
  if (MODIFIERS.includes(main)) return main;
  return [["#i-ctrl", "ControlLeft"], ["#i-shift", "ShiftLeft"], ["#i-alt", "AltLeft"]]
    .filter(([box]) => $(box).checked).map(([, code]) => code).concat(main).join("+");
}
for (const id of ["#i-key", "#i-ctrl", "#i-shift", "#i-alt"]) $(id).onchange = () => bind(pickedChord());
$("#i-label").oninput = (e) => {
  const c = ctl(sel);
  c.label = e.target.value.trim().slice(0, 16) || chordLabel(c.key);
  fitWidth(c);
  relabel(c);
  changed();
};

let recording = false, sawKey = false;
function startRecording() {
  recording = true;
  sawKey = false;
  $("#i-rec").textContent = "Press the keys…";
  $("#i-rec").classList.add("rec");
}
function stopRecording() {
  recording = false;
  $("#i-rec").textContent = "Record";
  $("#i-rec").classList.remove("rec");
}
$("#i-rec").onclick = () => (recording ? stopRecording() : startRecording());

// While recording, every key is the answer — Escape and Tab included; the button stops it. The key's position
// (e.code) is what gets sent back; its letter on this keyboard (e.key) is what the button is labelled with.
document.addEventListener("keydown", (e) => {
  if (!recording) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  if (MODIFIERS.includes(e.code)) return;                         // a modifier alone is decided when it comes up
  if (!LABEL[e.code]) { note(`${e.code} is not a key the pad can send. Pick one from the list instead.`); return; }
  sawKey = true;
  const mods = [[e.ctrlKey, "ControlLeft"], [e.shiftKey, "ShiftLeft"], [e.altKey, "AltLeft"]].filter(([on]) => on).map(([, c]) => c);
  const plain = e.key.length === 1 && e.key !== " " && !e.ctrlKey && !e.shiftKey && !e.altKey;
  const main = plain ? e.key.toUpperCase() : LABEL[e.code];
  bind([...mods, e.code].join("+"), [...mods.map((m) => LABEL[m]), main].join("+"));
  note("");
}, true);
document.addEventListener("keyup", (e) => {
  if (!recording) return;
  e.preventDefault();
  if (MODIFIERS.includes(e.code) && !sawKey) bind(e.code);
}, true);

// Somewhere near the middle that no control is on yet: outward from the centre along a row, then the next row.
function freeSpot(size) {
  const { w, h } = screen(), taken = edit.controls.filter((c) => c.on)
    .map((c) => ({ ...toPx(c), r: (c.s * unit() * (cat(c.id).shape === "rect" ? c.w : 1)) / 2 }));
  for (const row of [0, 1, -1, 2, -2]) {
    for (const col of [0, 1, -1, 2, -2, 3, -3, 4, -4]) {
      const x = snapPx(w / 2 + col * size * 1.3), y = snapPx(h / 2 + row * size * 1.3);
      if (x < size || x > w - size || y < size || y > h - size) continue;
      if (taken.every((t) => Math.hypot(t.x - x, t.y - y) > t.r + size * 0.6)) return { x, y };
    }
  }
  return { x: w / 2, y: h / 2 };
}

function freeKeyId() {
  const used = new Set(edit.controls.map((c) => c.id));
  for (let n = 1; n <= maxKeys; n += 1) if (!used.has(`k${n}`)) return `k${n}`;
  return null;
}

$("#addkey").onclick = () => {
  const id = freeKeyId();
  if (!id) { note(`A layout holds ${maxKeys} keys at most.`); return; }
  const spot = freeSpot(0.08 * unit());
  edit.controls.push({ id, x: round4(spot.x / screen().w), y: round4(spot.y / screen().h), s: 0.08, w: 1, on: true,
    key: "Space", label: "Space" });
  sel = id;
  drawStage();
  touched();
  startRecording();
  $("#stage").focus({ preventScroll: true });
  note("Press the key to bind — or a combination like Ctrl+S — or pick it below.");
};

$("#i-remove").onclick = () => {
  edit.controls = edit.controls.filter((c) => c.id !== sel);
  sel = null;
  stopRecording();
  drawStage();
  touched();
};

function nudgeTo(x, y) {
  const c = ctl(sel), from = toPx(c), dx = x - from.x, dy = y - from.y;
  for (const k of movingSet(sel)) { const p = toPx(k); setPx(k, p.x + dx, p.y + dy); }
  changed();
}

document.addEventListener("keydown", (e) => {
  if (recording || !sel || !edit || /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
  const step = (gridPx() || unit() / 200) * (e.shiftKey ? 4 : 1);
  const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
  if (!d) return;
  e.preventDefault();
  const p = toPx(ctl(sel));
  nudgeTo(snapPx(p.x + d[0]), snapPx(p.y + d[1]));
});

function touched() {
  dirty = true;
  $("#save").disabled = false;
  $("#revert").disabled = false;
}

function settle(layout) {
  saved = copy(layout);
  edit = copy(layout);
  dirty = false;
  $("#save").disabled = true;
  $("#revert").disabled = true;
  $("#delete").textContent = layout.id === "default" ? "Reset default" : "Delete";
  drawStage();
}

const ACTIVE = { pad: "active", screen: "active_screen" };
const layoutsOf = () => (all && all[mode()]) || [];

async function loadLayouts(pick) {
  const j = await (await fetch("/api/layouts")).json();
  catalogue = j.catalogue;
  symmetry = j.symmetry;
  codes = j.codes;
  maxKeys = j.max_keys;
  LABEL = Object.fromEntries(codes);
  $("#i-key").innerHTML = codes.map(([code, label]) => `<option value="${esc(code)}">${esc(label)}</option>`).join("");
  all = j.layouts;
  settings = j.settings;
  const want = pick || settings[ACTIVE[mode()]];
  $("#layout").innerHTML = layoutsOf().map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("");
  $("#layout").value = layoutsOf().some((l) => l.id === want) ? want : "default";
  $("#opacity").value = Math.round(settings.opacity * 100);
  $("#op-out").textContent = `${Math.round(settings.opacity * 100)} %`;
  $("#idle").value = Math.round(settings.idle * 100);
  $("#idle-out").textContent = `${Math.round(settings.idle * 100)} %`;
  $("#speed").value = settings.speed;
  $("#speed-out").textContent = settings.speed;
  $("#floating").checked = settings.floating;
  $("#snap").innerHTML = symmetry.snap_steps.map((s) => `<option value="${s}">${s ? `${s} %` : "Off"}</option>`).join("");
  $("#snap").value = String(settings.snap);
  $("#mirror").checked = settings.mirror;
  paintMode();
  settle(layoutsOf().find((l) => l.id === $("#layout").value));
}

const note = (t) => { $("#editnote").textContent = t; };

async function setting(patch) {
  try {
    settings = (await post("/api/settings", patch)).settings;
  } catch (err) { note(err.message); }
}

$("#layout").onchange = async (e) => {
  if (dirty && !confirm("Drop the unsaved changes to this layout?")) { e.target.value = edit.id; return; }
  await setting({ [ACTIVE[mode()]]: e.target.value });
  sel = null;
  await loadLayouts(e.target.value);
  note("");
};

$("#opacity").oninput = (e) => { $("#op-out").textContent = `${e.target.value} %`; };
$("#opacity").onchange = (e) => setting({ opacity: +e.target.value / 100 });
$("#idle").oninput = (e) => { $("#idle-out").textContent = `${e.target.value} %`; };
$("#idle").onchange = (e) => setting({ idle: +e.target.value / 100 });
$("#speed").oninput = (e) => { $("#speed-out").textContent = e.target.value; };
$("#speed").onchange = (e) => setting({ speed: +e.target.value });
$("#floating").onchange = (e) => setting({ floating: e.target.checked });
$("#snap").onchange = async (e) => { await setting({ snap: +e.target.value }); paintGrid(); };
$("#mirror").onchange = async (e) => {
  await setting({ mirror: e.target.checked });
  if (settings.mirror && sel) changed();                          // switching it on lines the right side up now
};

$("#save").onclick = async () => {
  try {
    const j = await post("/api/layouts/save", { layout: { ...edit, mode: mode() }, activate: true });
    await loadLayouts(j.layout.id);
    note(`Saved ${j.layout.name}.${status && status.overlay.running ? " The pad on screen has it already." : ""}`);
  } catch (e) { note(e.message); }
};

$("#saveas").onclick = async () => {
  const name = $("#newname").value.trim();
  if (!name) { note("Type a name for the new layout first."); $("#newname").focus(); return; }
  if (layoutsOf().some((l) => l.id === slug(name)) && !confirm(`A layout called ${name} exists. Replace it?`)) return;
  try {
    const j = await post("/api/layouts/save", { layout: { ...edit, id: slug(name), name, mode: mode() }, activate: true });
    $("#newname").value = "";
    await loadLayouts(j.layout.id);
    note(`Saved as ${j.layout.name}, and it is the active layout now.`);
  } catch (e) { note(e.message); }
};

$("#revert").onclick = () => { settle(saved); note(""); };

$("#delete").onclick = async () => {
  const isDefault = edit.id === "default";
  if (!confirm(isDefault ? "Put the default layout back as it came?" : `Delete ${edit.name}?`)) return;
  try {
    await post("/api/layouts/delete", { id: edit.id, mode: mode() });
    sel = null;
    await loadLayouts(isDefault ? "default" : undefined);
    note(isDefault ? "The default layout is back as it came." : "Deleted.");
  } catch (e) { note(isDefault ? "The default layout has not been changed." : e.message); }
};

// --------------------------------------------------------------------------------- the delivery system
async function facts() {
  try {
    const s = await (await fetch("/api/state")).json();
    $("#facts").innerHTML = [["Device", s.device], ["Version", `${s.version} (${s.transport})`], ["Data folder", s.data],
      ["Repository", s.repo ? s.repo + (s.linked ? "" : " (no token on this device)") : "not linked"]]
      .map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  } catch { /* the status poll reports a stopped app */ }
}

$("#sync").onclick = async () => {
  const b = $("#sync");
  b.disabled = true; b.textContent = "Syncing";
  try {
    const up = await post("/api/sync/push"), down = await post("/api/sync/pull");
    $("#syncnote").textContent = up.error || down.error || `Sent ${up.pushed.length}, received ${down.applied.length}.`;
    await loadLayouts();
  } catch (e) { $("#syncnote").textContent = e.message || "Could not reach GitHub."; }
  b.disabled = false; b.textContent = "Sync now";
};

$("#stop").onclick = async () => {
  $("#stop").disabled = true;
  await fetch("/api/quit", { method: "POST" }).catch(() => {});
  $("#headline").textContent = "Stopped. The launcher is pushing the data.";
};

// The heartbeat: when it stops, the app stops and the data is pushed. While the pad is up, the pad keeps the
// app alive itself, because a browser throttles the timers of a tab that is behind a game.
setInterval(() => fetch("/api/ping").catch(() => {}), 30000);
setInterval(refresh, 2000);

(async () => {
  await refresh();
  await loadLayouts();
  facts();
  pollPad();
})();
