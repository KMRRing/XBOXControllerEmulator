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
  lt: "LT", rt: "RT", back: "Back", start: "Start", guide: "Xbox", l3: "LS click", r3: "RS click", menu: "Hide / Pad" };
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

function paintStatus(s) {
  status = s;
  const o = s.overlay, drv = s.driver;
  const btn = $("#pad");
  btn.disabled = !s.windows;
  btn.textContent = o.running ? "Take the pad down" : "Show the pad";
  btn.classList.toggle("up", o.running);

  check("#c-driver", drv.ok ? "ok" : s.windows ? "bad" : "", drv.ok ? "ViGEmBus installed" : drv.error || "not installed");
  $("#install").hidden = drv.ok || !s.windows || !s.installer;
  check("#c-controller", o.running ? (o.pad_error ? "bad" : "ok") : "",
    o.running ? (o.pad_error ? o.pad_error : "Xbox 360 controller plugged in") : "plugged in while the pad is up");
  check("#c-screen", s.screen ? "ok" : "", s.screen ? `${s.screen.w} × ${s.screen.h}` : "not known");

  let line;
  if (!s.windows) line = "This runs on the Surface: the pad and the driver are Windows only.";
  else if (o.error) line = o.error;
  else if (o.running && o.pad_error) line = "The pad is up, but no controller reaches the game.";
  else if (o.running) line = o.hidden ? "The pad is folded away. Tap Pad on the screen to bring it back."
    : "The pad is up. The game sees an Xbox 360 controller.";
  else if (!drv.ok) line = "Install the driver once, then show the pad.";
  else line = "Ready. Start the game, then show the pad.";
  $("#headline").textContent = line;

  const aspect = s.screen ? `${s.screen.w} / ${s.screen.h}` : "3 / 2";
  if ($("#stage").style.aspectRatio !== aspect) { $("#stage").style.aspectRatio = aspect; drawStage(); }
}

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
function screen() { return status && status.screen ? status.screen : { w: 1500, h: 1000 }; }

function place(el, c) {
  const { w, h } = screen(), unit = Math.min(w, h), shape = catalogue[c.id].shape;
  el.style.left = `${c.x * 100}%`;
  el.style.top = `${c.y * 100}%`;
  el.style.width = `${(c.s * unit * (shape === "rect" ? c.w : 1)) / w * 100}%`;
  el.style.height = `${(c.s * unit) / h * 100}%`;
  el.classList.toggle("off", !c.on);
  el.classList.toggle("sel", sel === c.id);
}

function drawStage() {
  if (!edit) return;
  const stage = $("#stage");
  stage.innerHTML = "";
  for (const c of edit.controls) {
    const cat = catalogue[c.id], el = document.createElement("div");
    el.className = `ctl ${cat.shape}${cat.kind === "stick" ? " stick-base" : ""}`;
    el.dataset.id = c.id;
    el.textContent = cat.kind === "menu" ? "Hide" : cat.label;
    el.setAttribute("aria-label", NAMES[c.id]);
    place(el, c);
    el.addEventListener("pointerdown", (e) => grab(e, c, el));
    stage.appendChild(el);
  }
  inspect();
}

function grab(e, c, el) {
  e.preventDefault();
  sel = c.id;
  for (const other of document.querySelectorAll(".ctl")) other.classList.toggle("sel", other === el);
  inspect();
  const box = $("#stage").getBoundingClientRect();
  const dx = e.clientX - (box.left + c.x * box.width), dy = e.clientY - (box.top + c.y * box.height);
  el.setPointerCapture(e.pointerId);
  const move = (m) => {
    c.x = Math.round(Math.min(1, Math.max(0, (m.clientX - dx - box.left) / box.width)) * 1e4) / 1e4;
    c.y = Math.round(Math.min(1, Math.max(0, (m.clientY - dy - box.top) / box.height)) * 1e4) / 1e4;
    place(el, c);
    touched();
  };
  const done = () => { el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", done); el.removeEventListener("pointercancel", done); };
  el.addEventListener("pointermove", move);
  el.addEventListener("pointerup", done);
  el.addEventListener("pointercancel", done);
}

function inspect() {
  const c = edit && sel && edit.controls.find((k) => k.id === sel);
  $("#inspector").hidden = !c;
  if (!c) return;
  const shape = catalogue[c.id].shape;
  $("#i-name").textContent = NAMES[c.id];
  $("#i-size").value = c.s * 100;
  $("#i-wide").value = c.w;
  $("#i-wide-f").hidden = shape !== "rect";
  $("#i-on").checked = c.on;
  $("#i-on").disabled = c.id === "menu";
}

const current = () => edit.controls.find((k) => k.id === sel);
const replace = () => place(document.querySelector(`.ctl[data-id="${sel}"]`), current());
$("#i-size").oninput = (e) => { current().s = +e.target.value / 100; replace(); touched(); };
$("#i-wide").oninput = (e) => { current().w = +e.target.value; replace(); touched(); };
$("#i-on").onchange = (e) => { current().on = e.target.checked; replace(); touched(); };

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

async function loadLayouts(pick) {
  const j = await (await fetch("/api/layouts")).json();
  catalogue = j.catalogue;
  all = j.layouts;
  settings = j.settings;
  const want = pick || settings.active;
  $("#layout").innerHTML = all.map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("");
  $("#layout").value = all.some((l) => l.id === want) ? want : "default";
  $("#opacity").value = Math.round(settings.opacity * 100);
  $("#op-out").textContent = `${Math.round(settings.opacity * 100)} %`;
  settle(all.find((l) => l.id === $("#layout").value));
}

const note = (t) => { $("#editnote").textContent = t; };

$("#layout").onchange = async (e) => {
  if (dirty && !confirm("Drop the unsaved changes to this layout?")) { e.target.value = edit.id; return; }
  try { await post("/api/settings", { active: e.target.value }); } catch (err) { note(err.message); }
  sel = null;
  await loadLayouts(e.target.value);
  note("");
};

$("#opacity").oninput = (e) => { $("#op-out").textContent = `${e.target.value} %`; };
$("#opacity").onchange = async (e) => {
  try { await post("/api/settings", { opacity: +e.target.value / 100 }); } catch (err) { note(err.message); }
};

$("#save").onclick = async () => {
  try {
    const j = await post("/api/layouts/save", { layout: edit, activate: true });
    await loadLayouts(j.layout.id);
    note(`Saved ${j.layout.name}.${status && status.overlay.running ? " The pad on screen has it already." : ""}`);
  } catch (e) { note(e.message); }
};

$("#saveas").onclick = async () => {
  const name = $("#newname").value.trim();
  if (!name) { note("Type a name for the new layout first."); $("#newname").focus(); return; }
  if (all.some((l) => l.id === slug(name)) && !confirm(`A layout called ${name} exists. Replace it?`)) return;
  try {
    const j = await post("/api/layouts/save", { layout: { ...edit, id: slug(name), name }, activate: true });
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
    await post("/api/layouts/delete", { id: edit.id });
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
