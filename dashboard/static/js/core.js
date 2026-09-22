/* Shared core: fetching, polling, tabs, drawer, formatting, svg helpers. */
"use strict";

const POLL_MS = 5000;
const SLOW_POLL_MS = 15000;

const state = {
  tab: "rfqs",
  timer: null, manualPaused: false,
  hidden: document.hidden,
  rfqPage: 1, rfqOnly: false, rfqScreen: "", rfqStatus: "", rfqGame: "", rfqSearch: "", rfqTotal: 0,
  pricingPage: 1, pricingTotal: 0,
  settlementPage: 1,
  fillsPage: 1, fillsTotal: 0,
  nflView: "overview", nflFilters: {},
};

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtPrice = (v, dp = 4) => v == null ? "—" : Number(v).toFixed(dp);
const fmtMs = (v) => v == null ? "—" : Number(v).toFixed(1);
const fmtInt = (v) => v == null ? "—" : Number(v).toLocaleString("en-US");
const fmtMoney = (v) => v == null ? "—"
  : (v < 0 ? "-$" : "$") + Math.abs(Number(v)).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (v, dp = 1) => v == null ? "—" : (Number(v) * 100).toFixed(dp) + "%";
const fmtEdge = (v, dp = 4) => v == null ? "—"
  : `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${Number(v).toFixed(dp)}</span>`;
const shortId = (id) => id && id.length > 14 ? `<span class="mono">${esc(id.slice(0, 12))}…</span>` : `<span class="mono">${esc(id || "—")}</span>`;

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
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return res.json();
}

/* Preserve scroll position while swapping table bodies. */
function setRows(tableId, html) {
  const table = $(tableId);
  const wrap = table.closest(".table-wrap");
  const top = wrap ? wrap.scrollTop : 0;
  table.querySelector("tbody").innerHTML = html;
  if (wrap) wrap.scrollTop = top;
}

function kpi(k, v, cls = "") {
  return `<div class="kpi"><div class="k">${esc(k)}</div><div class="v ${cls}">${v}</div></div>`;
}

function badge(text, kind) {
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

/* ---- tabs ---- */
function showTab(name) {
  state.tab = name;
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((s) =>
    s.classList.toggle("active", s.id === "tab-" + name));
  if (name === "nfl") ensureVega().then(() => refresh());
  else restartPolling();
}
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab)));

/* keyboard: 1–6 switch tabs, "/" focuses the RFQ search */
document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "");
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") {
    const s = $("rfq-search");
    if (s) { e.preventDefault(); showTab("rfqs"); s.focus(); }
    return;
  }
  const btns = [...document.querySelectorAll("#tabs button")];
  const n = parseInt(e.key, 10);
  if (n >= 1 && n <= btns.length) btns[n - 1].click();
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
  clearInterval(state.timer);
  state.timer = null;
  if (!state.manualPaused) refresh();
  if (!state.hidden && !state.manualPaused && state.tab !== "nfl") {
    const ms = state.tab === "performance" ? SLOW_POLL_MS : POLL_MS;
    state.timer = setInterval(refresh, ms);
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
    state.rfqPage = 1; state.pricingPage = 1; state.fillsPage = 1;
    restartPolling();
  } catch (e) { console.warn(e); }
}

async function refresh() {
  if (state.hidden) return;
  try {
    const health = await get("/api/health");
    $("health-dot").className = "dot " + (health.waiting ? "wait" : "ok");
    $("waiting").classList.toggle("hidden", !health.waiting);
    if (health.waiting) $("waiting").textContent = health.capture_start_error || "Waiting for the headless capture database…";
    if (health.waiting) return;
    if (state.tab === "rfqs") await refreshRfqs();
    else if (state.tab === "pricing") await refreshPricing();
    else if (state.tab === "performance") await refreshPerformance();
    else if (state.tab === "engine") await refreshEngine();
    else if (state.tab === "inventory") await refreshInventory();
    else if (state.tab === "nfl") await refreshNfl();
    $("updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) { console.warn(e); }
}

/* ---- drawer ---- */
function openDrawer(title, html) {
  $("drawer-title").innerHTML = title;
  $("drawer-body").innerHTML = html;
  $("drawer").classList.remove("hidden");
  $("drawer-scrim").classList.remove("hidden");
}
function closeDrawer() {
  $("drawer").classList.add("hidden");
  $("drawer-scrim").classList.add("hidden");
}
$("drawer-close").addEventListener("click", closeDrawer);
$("drawer-scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

/* ---- svg charts ---- */
function lineChart(svgId, points, key, { color = "#4da3ff", fill = true } = {}) {
  const svg = $(svgId);
  const W = 600, H = 180, PAD = 10;
  const vals = points.map((p) => Number(p[key] ?? 0));
  if (!vals.length) { svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">no data</text>`; return; }
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1;
  const X = (i) => PAD + (i / Math.max(1, vals.length - 1)) * (W - 2 * PAD);
  const Y = (v) => PAD + (1 - (v - lo) / span) * (H - 2 * PAD);
  const d = vals.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
  const zy = Y(0).toFixed(1);
  svg.innerHTML =
    `<line x1="${PAD}" y1="${zy}" x2="${W - PAD}" y2="${zy}" stroke="#232d42"/>` +
    (fill ? `<path class="area" d="${d} L${X(vals.length - 1).toFixed(1)},${H - PAD} L${PAD},${H - PAD} Z"/>` : "") +
    `<path class="line" d="${d}" style="stroke:${color}"/>` +
    `<text x="${W - PAD}" y="${Y(vals[vals.length - 1]).toFixed(1) - 5}" text-anchor="end">${vals[vals.length - 1].toFixed(2)}</text>`;
}

function multiLineChart(svgId, points, series) {
  const svg = $(svgId), W = 600, H = 180, PAD = 10;
  if (!points || !points.length) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">no data</text>`;
    return;
  }
  const vals = points.flatMap((p) => series.map((s) => Number(p[s.key] ?? 0)));
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1;
  const X = (i) => PAD + (i / Math.max(1, points.length - 1)) * (W - 2 * PAD);
  const Y = (v) => PAD + (1 - (v - lo) / span) * (H - 2 * PAD);
  let html = `<line x1="${PAD}" y1="${Y(0)}" x2="${W - PAD}" y2="${Y(0)}" stroke="#232d42"/>`;
  series.forEach((s, si) => {
    const d = points.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(Number(p[s.key] ?? 0)).toFixed(1)}`).join(" ");
    html += `<path class="line" d="${d}" style="stroke:${s.color}"/>` +
      `<text x="${PAD + si * 150}" y="${PAD + 10}" fill="${s.color}">${esc(s.label)}</text>`;
  });
  svg.innerHTML = html;
}

function histogram(svgId, buckets, budgetMs) {
  const svg = $(svgId);
  const W = 600, H = 180, PAD = 10;
  if (!buckets || !buckets.length) { svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">no data</text>`; return; }
  const maxN = Math.max(...buckets.map((b) => b.n), 1);
  const hi = Math.max(...buckets.map((b) => b.hi));
  const bw = (W - 2 * PAD) / buckets.length;
  let html = "";
  buckets.forEach((b, i) => {
    const h = (b.n / maxN) * (H - 2 * PAD - 14);
    const x = PAD + i * bw + 1;
    const over = budgetMs != null && b.lo >= budgetMs;
    html += `<rect class="bar${over ? " over" : ""}" x="${x.toFixed(1)}" y="${(H - PAD - h).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${h.toFixed(1)}"><title>${b.lo.toFixed(0)}–${b.hi.toFixed(0)} ms: ${b.n}</title></rect>`;
  });
  if (budgetMs != null && hi > budgetMs) {
    const bx = PAD + (budgetMs / hi) * (W - 2 * PAD);
    html += `<line x1="${bx.toFixed(1)}" y1="${PAD}" x2="${bx.toFixed(1)}" y2="${H - PAD}" stroke="#ff6b6b" stroke-dasharray="4,3"/>` +
            `<text x="${bx.toFixed(1) + 4}" y="${PAD + 10}">budget ${budgetMs} ms</text>`;
  }
  const step = Math.max(1, Math.floor(buckets.length / 6));
  buckets.forEach((b, i) => {
    if (i % step === 0) html += `<text x="${(PAD + i * bw).toFixed(1)}" y="${H - 2}">${b.lo.toFixed(0)}</text>`;
  });
  svg.innerHTML = html;
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
loadSources();
restartPolling();
