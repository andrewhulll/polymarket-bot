/* Shared core: state, fetching, polling, tabs, header (game chip + kill switch),
   KPI strip, docked drawer, formatting and SVG chart helpers. */
"use strict";

const POLL_MS = 5000;
const SLOW_POLL_MS = 15000;
const GLOBAL_POLL_MS = 15000;
const TABS = ["flow", "performance", "risk", "engine", "research"];

const state = {
  tab: "flow",
  timer: null, globalTimer: null, manualPaused: false,
  hidden: document.hidden,
  game: "",
  rfqPage: 1, rfqOnly: false, rfqScreen: "", rfqDecision: "", rfqStatus: "", rfqSearch: "", rfqTotal: 0,
  fillsPage: 1,
  selectedRfq: null,
  killSwitch: null,
  nflView: "overview", nflFilters: {},
};

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtPrice = (v, dp = 3) => v == null ? "—" : Number(v).toFixed(dp);
const fmtMs = (v) => v == null ? "—" : Number(v).toFixed(1);
const fmtInt = (v) => v == null ? "—" : Number(v).toLocaleString("en-US");
const fmtMoney = (v) => v == null ? "—"
  : (v < 0 ? "-$" : "$") + Math.abs(Number(v)).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtMoney0 = (v) => v == null ? "—"
  : (v < 0 ? "-$" : "$") + Math.abs(Number(v)).toLocaleString("en-US", { maximumFractionDigits: 0 });
const fmtPct = (v, dp = 1) => v == null ? "—" : (Number(v) * 100).toFixed(dp) + "%";
const fmtEdge = (v, dp = 3) => v == null ? "—"
  : `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : "−"}${Math.abs(Number(v)).toFixed(dp)}</span>`;
const fmtPnl = (v) => v == null ? "—" : `<span class="${v >= 0 ? "pos" : "neg"}">${fmtMoney(v)}</span>`;
const shortId = (id) => id && id.length > 14 ? `<span class="mono">${esc(id.slice(0, 12))}…</span>` : `<span class="mono">${esc(id || "—")}</span>`;
const fmtScore = (v) => v == null ? "—" : Number(v).toFixed(4);

function ageStr(iso) {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function deadlineStr(iso) {
  if (!iso) return "—";
  const numeric = Number(iso);
  const deadline = Number.isFinite(numeric) && numeric > 1e9
    ? new Date(numeric < 1e12 ? numeric * 1000 : numeric)
    : new Date(iso);
  if (Number.isNaN(deadline.getTime())) return "—";
  const s = (deadline.getTime() - Date.now()) / 1000;
  if (s <= 0) return `<span class="deadline-hot">expired</span>`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  const txt = h ? `${h}h ${m % 60}m` : m ? `${m}m ${Math.floor(s % 60)}s` : `${Math.floor(s)}s`;
  const cls = s < 60 ? "deadline-hot" : s < 300 ? "deadline-warm" : "";
  return `<span class="${cls}">${txt}</span>`;
}

async function get(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return res.json();
}
async function post(path, body) {
  const res = await fetch(path, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status} ${path}`);
  return data;
}

/* Preserve scroll position while swapping table bodies. */
function setRows(tableId, html) {
  const table = $(tableId);
  const wrap = table.closest(".table-wrap");
  const top = wrap ? wrap.scrollTop : 0;
  table.querySelector("tbody").innerHTML = html;
  if (wrap) wrap.scrollTop = top;
}

function kpi(k, v, cls = "", d = "") {
  return `<div class="kpi"><div class="k">${esc(k)}</div><div class="v ${cls}">${v}</div>${d ? `<div class="d">${d}</div>` : ""}</div>`;
}
function setTabKpis(html) { $("kpi-tab").innerHTML = html; }

function badge(text, kind) {
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

/* ---- global game filter ---- */
function gameLink(game) {
  if (!game) return `<span class="dim">—</span>`;
  return `<span class="gamelink" data-game="${esc(game)}" title="filter every tab to ${esc(game)}">${esc(game)}</span>`;
}
function renderGameChip() {
  const chip = $("game-chip");
  chip.className = "chip" + (state.game ? " set" : "");
  chip.innerHTML = state.game
    ? `GAME: <b>${esc(state.game)}</b> <button class="link" id="game-clear" title="clear game filter">×</button>`
    : "GAME: ALL";
  const clear = $("game-clear");
  if (clear) clear.addEventListener("click", (e) => { e.stopPropagation(); setGame(""); });
  const sel = $("game-filter");
  if (sel && [...sel.options].some((o) => o.value === state.game)) sel.value = state.game;
}
function setGame(game) {
  state.game = game || "";
  state.rfqPage = 1; state.fillsPage = 1;
  renderGameChip();
  refresh();
}
document.addEventListener("click", (e) => {
  const link = e.target.closest("[data-game]");
  if (link && link.dataset.game) { e.stopPropagation(); setGame(link.dataset.game); }
});
const gameMatches = (value) => !state.game || String(value || "").includes(state.game);

/* ---- tabs ---- */
function showTab(name) {
  if (!TABS.includes(name)) return;
  state.tab = name;
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((s) =>
    s.classList.toggle("active", s.id === "tab-" + name));
  setTabKpis("");
  if (name === "research") ensureVega().then(() => restartPolling());
  else restartPolling();
}
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab)));

/* keyboard: 1–5 switch tabs, "/" focuses RFQ search, "p" toggles polling, Esc closes overlays */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeDrawer(); closeKillPopover(); return; }
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "");
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") {
    e.preventDefault(); showTab("flow"); $("rfq-search").focus();
  } else if (e.key === "p" || e.key === "P") {
    $("poll-toggle").click();
  } else {
    const n = parseInt(e.key, 10);
    if (n >= 1 && n <= TABS.length) showTab(TABS[n - 1]);
  }
});

document.addEventListener("visibilitychange", () => {
  state.hidden = document.hidden;
  $("paused").classList.toggle("hidden", !state.hidden && !state.manualPaused);
  restartPolling();
});

$("poll-toggle").addEventListener("click", () => {
  state.manualPaused = !state.manualPaused;
  $("poll-toggle").textContent = state.manualPaused ? "Resume" : "Pause";
  $("paused").classList.toggle("hidden", !state.hidden && !state.manualPaused);
  restartPolling();
});

function restartPolling() {
  clearInterval(state.timer); clearInterval(state.globalTimer);
  state.timer = state.globalTimer = null;
  // stagger: the global tiles replay the paper ledger, so let the tab's own data paint first
  if (!state.manualPaused) { refresh(); setTimeout(refreshGlobal, 1200); }
  if (!state.hidden && !state.manualPaused) {
    if (state.tab !== "research") {
      state.timer = setInterval(refresh, state.tab === "performance" ? SLOW_POLL_MS : POLL_MS);
    }
    state.globalTimer = setInterval(refreshGlobal, GLOBAL_POLL_MS);
  }
}

/* ---- data sources (live capture vs week-1 backtest) ---- */
async function loadSources() {
  try {
    const data = await get("/api/sources");
    const sel = $("data-source");
    sel.innerHTML = data.sources.map((s) =>
      `<option value="${esc(s.id)}" ${s.id === data.active ? "selected" : ""}>${esc(s.label)}</option>`).join("");
    sel.onchange = () => switchSource(sel.value);
    state.source = data.active;
  } catch (e) { console.warn(e); }
}

async function switchSource(id) {
  try {
    await post("/api/sources/active", { id });
    state.source = id;
    closeDrawer();
    state.rfqPage = 1; state.fillsPage = 1;
    restartPolling();
  } catch (e) { console.warn(e); }
}

const TAB_REFRESH = {
  flow: () => refreshFlow(), performance: () => refreshPerformance(), risk: () => refreshRisk(),
  engine: () => refreshEngine(), research: () => refreshNfl(),
};

async function refresh() {
  if (state.hidden) return;
  try {
    const health = await get("/api/health");
    $("health-dot").className = "dot " + (health.waiting ? "wait" : "ok");
    $("waiting").classList.toggle("hidden", !health.waiting);
    if (health.waiting) $("waiting").textContent = health.capture_start_error || "Waiting for the headless capture database…";
    document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("hidden", health.waiting));
    if (health.waiting) return;
    refreshKillSwitch();
    await TAB_REFRESH[state.tab]();
    $("updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) { console.warn(e); }
}

/* ---- global KPI tiles (4): cached server-side, polled on their own timer ---- */
async function refreshGlobal() {
  if (state.hidden) return;
  try {
    const s = await get("/api/summary");
    const wclPct = s.wcl != null && s.equity ? s.wcl / s.equity : null;
    $("kpi-global").innerHTML =
      kpi("Quoted 1h", fmtInt(s.quoted_1h)) +
      kpi("Accept rate", fmtPct(s.accept_rate)) +
      kpi("Realized P&L", fmtMoney0(s.realized_pnl), s.realized_pnl == null ? "" : s.realized_pnl >= 0 ? "good" : "bad",
        s.expected_pnl != null ? `exp ${fmtMoney0(s.expected_pnl)}` : "") +
      kpi("WCL / equity", wclPct == null ? "—" : fmtPct(wclPct, 0), wclPct > 0.8 ? "bad" : "",
        s.wcl != null ? `${fmtMoney0(s.wcl)} of ${fmtMoney0(s.equity)}` : "");
  } catch (e) {
    $("kpi-global").innerHTML = kpi("Quoted 1h", "—") + kpi("Accept rate", "—") + kpi("Realized P&L", "—") + kpi("WCL / equity", "—");
    console.warn(e);
  }
}

/* ---- kill switch (header, every tab) ---- */
async function refreshKillSwitch() {
  try {
    const risk = await get("/api/risk");
    renderKillSwitch(risk.kill_switch);
  } catch (e) { console.warn(e); }
}
function killTripped(ks) { return !!ks && (ks.state === "tripped" || ks.state === "engaged"); }
function renderKillSwitch(ks) {
  state.killSwitch = ks || null;
  const tripped = killTripped(ks);
  $("ks").classList.toggle("tripped", tripped);
  $("ks-state").textContent = tripped ? "TRIPPED" : "OFF";
  const btn = $("ks-open");
  btn.disabled = false;
  btn.textContent = tripped ? "Reset" : "Trip";
}
function openKillPopover() {
  const tripped = killTripped(state.killSwitch);
  const pop = $("ks-popover");
  pop.classList.remove("hidden");
  pop.classList.toggle("reset", tripped);
  $("ks-title").textContent = tripped ? "Reset kill switch" : "Trip kill switch";
  $("ks-copy").textContent = tripped
    ? "Resumes paper quoting. New RFQs are priced again as soon as the engine reads the reset."
    : "Stops all new paper quotes immediately. Quotes already posted stay until they expire. The trip is logged as a risk event.";
  $("ks-confirm").textContent = tripped ? "Reset kill switch" : "Trip kill switch";
  $("ks-confirm").classList.toggle("danger", !tripped);
  $("ks-reason").value = "";
  $("ks-error").textContent = "";
  const ks = state.killSwitch;
  $("ks-last").textContent = ks && ks.ts ? `last event ${String(ks.ts).slice(11, 19)} · ${ks.state}${ks.trigger ? " · " + ks.trigger : ""}` : "no kill-switch events yet";
  $("ks-reason").focus();
}
function closeKillPopover() { $("ks-popover").classList.add("hidden"); }
$("ks-open").addEventListener("click", () =>
  $("ks-popover").classList.contains("hidden") ? openKillPopover() : closeKillPopover());
$("ks-cancel").addEventListener("click", closeKillPopover);
$("ks-confirm").addEventListener("click", async () => {
  const action = killTripped(state.killSwitch) ? "reset" : "trip";
  const btn = $("ks-confirm");
  btn.disabled = true;
  try {
    const res = await post("/api/risk/kill-switch", { action, reason: $("ks-reason").value });
    renderKillSwitch(res.kill_switch);
    closeKillPopover();
    refresh();
  } catch (err) {
    $("ks-error").textContent = `${action} failed: ${err.message}`;
  } finally { btn.disabled = false; }
});

/* ---- docked drawer ---- */
function openDrawer(title, html) {
  $("drawer-title").innerHTML = title;
  $("drawer-body").innerHTML = html;
  $("drawer").classList.remove("hidden");
}
function closeDrawer() {
  $("drawer").classList.add("hidden");
  state.selectedRfq = null;
  document.querySelectorAll("tr.selected").forEach((tr) => tr.classList.remove("selected"));
}
$("drawer-close").addEventListener("click", closeDrawer);
function markSelected(tableId, rfqId) {
  document.querySelectorAll(`#${tableId} tbody tr`).forEach((tr) =>
    tr.classList.toggle("selected", !!rfqId && tr.dataset.rfq === rfqId));
}

/* ---- svg charts (fixed 400x100 viewBox, stretched; labels live in HTML footers) ---- */
const VB_W = 400, VB_H = 100, PAD = 2;

function chartEmpty(svgId, message) {
  const svg = $(svgId);
  svg.innerHTML = "";
  const host = svg.parentElement;
  let note = host.querySelector(".empty");
  if (message) {
    if (!note) { note = document.createElement("div"); note.className = "empty"; host.appendChild(note); }
    note.textContent = message;
  } else if (note) { note.remove(); }
}

/* series: [{ values: number[], color, dash, fill }] sharing one y scale. */
function linesChart(svgId, series, { ref = null, zero = false, lo = null, hi = null } = {}) {
  const usable = series.filter((s) => s.values && s.values.length);
  if (!usable.length) { chartEmpty(svgId, "no data"); return null; }
  chartEmpty(svgId, "");
  const all = usable.flatMap((s) => s.values).concat(ref != null ? [ref] : []);
  if (zero) all.push(0);
  const min = lo ?? Math.min(...all), max = hi ?? Math.max(...all), span = max - min || 1;
  const Y = (v) => (VB_H - PAD - ((v - min) / span) * (VB_H - 2 * PAD)).toFixed(1);
  let html = [25, 50, 75].map((y) => `<line class="grid" x1="0" x2="${VB_W}" y1="${y}" y2="${y}"/>`).join("");
  if (zero && min < 0 && max > 0) html += `<line class="grid" style="stroke:#555" x1="0" x2="${VB_W}" y1="${Y(0)}" y2="${Y(0)}"/>`;
  if (ref != null) html += `<line class="ref" x1="0" x2="${VB_W}" y1="${Y(ref)}" y2="${Y(ref)}"/>`;
  usable.forEach((s) => {
    const pts = s.values.map((v, i) =>
      `${(s.values.length === 1 ? VB_W : (i / (s.values.length - 1)) * VB_W).toFixed(1)},${Y(v)}`).join(" ");
    if (s.fill) html += `<polygon points="${pts} ${VB_W},${VB_H} 0,${VB_H}" fill="${s.color}" fill-opacity=".12"/>`;
    html += `<polyline class="ln" points="${pts}" style="stroke:${s.color}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""}/>`;
  });
  $(svgId).innerHTML = html;
  return { min, max };
}

/* values: numbers; opts.color(i, v) -> css color. */
function barsChart(svgId, values, { color = "#ff9f1c", titles = null } = {}) {
  if (!values || !values.length || Math.max(...values) <= 0) { chartEmpty(svgId, "no data"); return; }
  chartEmpty(svgId, "");
  const max = Math.max(...values), bw = VB_W / values.length;
  $(svgId).innerHTML = values.map((v, i) => {
    const h = (v / max) * (VB_H - 6);
    const fill = typeof color === "function" ? color(i, v) : color;
    return `<rect x="${(i * bw + 0.5).toFixed(1)}" y="${(VB_H - 2 - h).toFixed(1)}" width="${Math.max(0.5, bw - 1).toFixed(1)}" height="${h.toFixed(1)}" fill="${fill}">${titles ? `<title>${esc(titles[i])}</title>` : ""}</rect>`;
  }).join("");
}

/* latency buckets [{lo, hi, n}]; buckets at/over the budget are red, with a budget marker. */
function histogram(svgId, buckets, budgetMs, barColor = "#f2f2f2") {
  if (!buckets || !buckets.length) { chartEmpty(svgId, "no data"); return; }
  chartEmpty(svgId, "");
  const maxN = Math.max(...buckets.map((b) => b.n), 1);
  const hi = Math.max(...buckets.map((b) => b.hi));
  const bw = VB_W / buckets.length;
  let html = buckets.map((b, i) => {
    const h = (b.n / maxN) * (VB_H - 6);
    const over = budgetMs != null && b.lo >= budgetMs;
    return `<rect x="${(i * bw + 0.5).toFixed(1)}" y="${(VB_H - 2 - h).toFixed(1)}" width="${Math.max(0.5, bw - 1).toFixed(1)}" height="${h.toFixed(1)}" fill="${over ? "#ff4d4d" : barColor}"><title>${b.lo.toFixed(0)}–${b.hi.toFixed(0)} ms: ${b.n}</title></rect>`;
  }).join("");
  if (budgetMs != null && hi > budgetMs) {
    const x = ((budgetMs / hi) * VB_W).toFixed(1);
    html += `<line class="ref" x1="${x}" x2="${x}" y1="0" y2="${VB_H}"/>`;
  }
  $(svgId).innerHTML = html;
}

/* rows: [{ label, value, text, cls, game }]; bar width is value / max(value). */
function barList(containerId, rows, empty = "no data") {
  const max = Math.max(...rows.map((r) => Math.abs(r.value)), 0);
  $(containerId).innerHTML = rows.length && max > 0
    ? rows.map((r) =>
      `<div class="bl${r.game ? " click" : ""}" ${r.game ? `data-game="${esc(r.game)}"` : ""}>` +
      `<span class="l ${r.lcls || "w"}" title="${esc(r.label)}">${esc(r.label)}</span>` +
      `<div><div class="b ${r.cls || ""}" style="width:${Math.max(1, (Math.abs(r.value) / max) * 100).toFixed(1)}%"></div></div>` +
      `<span class="v">${r.text ?? ""}</span></div>`).join("")
    : `<div class="panel-note">${esc(empty)}</div>`;
}

function setFoot(id, parts) {
  $(id).innerHTML = parts.filter(Boolean).map((p) => `<span>${p}</span>`).join("");
}

function ensureVega() {
  if (window.vegaEmbed) return Promise.resolve();
  const load = (src) => new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = src; s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
  return load("/static/vendor/vega.min.js")
    .then(() => load("/static/vendor/vega-lite.min.js"))
    .then(() => load("/static/vendor/vega-embed.min.js"));
}

/* ---- init ---- */
renderGameChip();
loadSources();
// every tab script has executed by DOMContentLoaded, so refreshFlow() etc. exist
document.addEventListener("DOMContentLoaded", restartPolling);
