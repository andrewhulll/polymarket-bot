/* Live dashboard frontend: polls small JSON endpoints and patches the DOM
   in place. The page itself never reloads, so scroll position and selection
   survive feed updates. Polling pauses while the browser tab is hidden. */
"use strict";

const POLL_MS = 5000;
const state = {
  tab: "rfqs",
  rfqPage: 1, rfqOnly: false, rfqTotal: 0,
  pricingPage: 1, pricingTotal: 0, selectedRfq: null,
  timer: null, hidden: document.hidden,
};

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtPrice = (v) => v == null ? "—" : Number(v).toFixed(4);
const fmtMs = (v) => v == null ? "—" : Number(v).toFixed(1);
const fmtMoney = (v) => (v < 0 ? "-$" : "$") + Math.abs(Number(v)).toLocaleString("en-US",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (v) => (Number(v) * 100).toFixed(1) + "%";
const check = (v) => v ? "✅" : "—";
const num = (v) => v == null ? "—" : `<span class="num">${esc(v)}</span>`;

async function get(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
  return res.json();
}

/* Keep the user's scroll position while we swap table contents. */
function setRows(tbodyId, html) {
  const tbody = $(tbodyId).querySelector("tbody");
  const wrap = $(tbodyId).closest(".table-wrap");
  const top = wrap ? wrap.scrollTop : 0;
  tbody.innerHTML = html;
  if (wrap) wrap.scrollTop = top;
}

function updated() {
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
}

/* ------------------------------------------------------------------ */
/* tabs                                                                */
/* ------------------------------------------------------------------ */
function showTab(name) {
  state.tab = name;
  document.querySelectorAll("#tabs button").forEach(
    (b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(
    (s) => s.classList.toggle("active", s.id === "tab-" + name));
  restartPolling();
}

document.querySelectorAll("#tabs button").forEach(
  (b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

document.addEventListener("visibilitychange", () => {
  state.hidden = document.hidden;
  $("paused").classList.toggle("hidden", !state.hidden);
  restartPolling();
});

function restartPolling() {
  clearInterval(state.timer);
  state.timer = null;
  refresh(); // immediate
  if (!state.hidden) state.timer = setInterval(refresh, POLL_MS);
}

async function refresh() {
  if (state.hidden) return;
  try {
    const health = await get("/api/health");
    $("health-dot").className = "dot " + (health.waiting ? "wait" : "ok");
    $("waiting").classList.toggle("hidden", !health.waiting);
    if (health.waiting) return;
    if (state.tab === "rfqs") await refreshRfqs();
    else if (state.tab === "pricing") await refreshPricing();
    else if (state.tab === "performance") await refreshPerformance();
    else if (state.tab === "engine") await refreshEngine();
    updated();
  } catch (e) { console.warn(e); }
}

/* ------------------------------------------------------------------ */
/* RFQs                                                                */
/* ------------------------------------------------------------------ */
$("only-quotable").addEventListener("change", (e) => {
  state.rfqOnly = e.target.checked; state.rfqPage = 1; refreshRfqs();
});
$("rfq-prev").addEventListener("click", () => { if (state.rfqPage > 1) { state.rfqPage--; refreshRfqs(); } });
$("rfq-next").addEventListener("click", () => { state.rfqPage++; refreshRfqs(); });

async function refreshRfqs() {
  const data = await get(`/api/rfqs?only_quotable=${state.rfqOnly ? 1 : 0}&page=${state.rfqPage}`);
  state.rfqTotal = data.total;
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  if (state.rfqPage > pages) { state.rfqPage = pages; return refreshRfqs(); }
  $("rfq-page-label").textContent = `page ${data.page} of ${pages}`;
  $("rfq-prev").disabled = data.page <= 1;
  $("rfq-next").disabled = data.page >= pages;
  $("rfq-count").textContent = `Showing ${data.rows.length.toLocaleString()} of ${data.total.toLocaleString()} matching RFQs`;
  setRows("rfq-table", data.rows.map((r) => {
    const sizeMin = Object.entries(r.filters || {}).find(([k]) => k.startsWith("at least "));
    return `<tr class="clickable" data-rfq="${esc(r.rfq_id)}">` +
      `<td>${esc(r.rfq_id.slice(0, 12))}…</td><td>${check(r.quotable)}</td>` +
      `<td>${esc(r.screen || "PENDING")}</td><td>${check(r.filters["known legs"])}</td>` +
      `<td>${check(r.filters["NFL same game"])}</td>` +
      `<td>${check(r.filters["no unsupported same game"])}</td>` +
      `<td>${sizeMin ? check(sizeMin[1]) : "—"}</td>` +
      `<td class="num">${r.n_legs ?? "—"}</td><td>${esc(r.qty_decimal || r.cash_order_qty || "—")}</td>` +
      `<td>${esc(r.created_time || "—")}</td></tr>`;
  }).join(""));
  document.querySelectorAll("#rfq-table tbody tr").forEach((tr) =>
    tr.addEventListener("click", () => selectRfq(tr.dataset.rfq)));
}

function selectRfq(rfqId) {
  state.selectedRfq = rfqId;
  showTab("pricing");
}

/* ------------------------------------------------------------------ */
/* Pricing                                                             */
/* ------------------------------------------------------------------ */
$("pricing-prev").addEventListener("click", () => { if (state.pricingPage > 1) { state.pricingPage--; refreshPricing(); } });
$("pricing-next").addEventListener("click", () => { state.pricingPage++; refreshPricing(); });

async function refreshPricing() {
  const data = await get(`/api/pricing?page=${state.pricingPage}`);
  state.pricingTotal = data.total;
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  if (state.pricingPage > pages) { state.pricingPage = pages; return refreshPricing(); }
  $("pricing-page-label").textContent = `page ${data.page} of ${pages}`;
  $("pricing-prev").disabled = data.page <= 1;
  $("pricing-next").disabled = data.page >= pages;
  $("pricing-count").textContent = `Showing ${data.rows.length.toLocaleString()} of ${data.total.toLocaleString()} filter-passing RFQs`;
  const ids = new Set(data.rows.map((r) => r.rfq_id));
  setRows("pricing-table", data.rows.map((r) =>
    `<tr class="clickable${r.rfq_id === state.selectedRfq ? " selected" : ""}" data-rfq="${esc(r.rfq_id)}">` +
    `<td>${esc(r.rfq_id.slice(0, 12))}…</td><td>${esc(r.status)}</td><td>${esc(r.reason_code)}</td>` +
    `<td class="num">${fmtPrice(r.response_price)}</td><td>${esc(r.size ?? "—")}</td>` +
    `<td class="num">${fmtPrice(r.market_price)}</td>` +
    `<td class="num">${fmtMs(r.wait_ms)}</td><td class="num">${fmtMs(r.compute_ms)}</td></tr>`
  ).join(""));
  document.querySelectorAll("#pricing-table tbody tr").forEach((tr) =>
    tr.addEventListener("click", () => { state.selectedRfq = tr.dataset.rfq; refreshPricingDetail(); }));
  await refreshPricingDetail(ids);
}

async function refreshPricingDetail(ids) {
  const box = $("pricing-detail");
  let item = null;
  if (state.selectedRfq && (!ids || ids.has(state.selectedRfq))) {
    try { item = await get(`/api/pricing/${encodeURIComponent(state.selectedRfq)}`); }
    catch (e) { /* fell off the page; fall through to first row */ }
  }
  if (!item) {
    const first = document.querySelector("#pricing-table tbody tr");
    if (!first) { box.classList.add("hidden"); return; }
    state.selectedRfq = first.dataset.rfq;
    item = await get(`/api/pricing/${encodeURIComponent(state.selectedRfq)}`);
  }
  box.classList.remove("hidden");
  const detail = item.detail || {};
  const keys = ["components", "explanations", "games", "legs", "corr_adjustment_bps", "spread_bps_total"];
  const slim = Object.fromEntries(keys.filter((k) => k in detail).map((k) => [k, detail[k]]));
  box.innerHTML =
    `<h3>RFQ ${esc(item.rfq_id)}</h3>` +
    `<div class="metrics">` +
    `<div class="card"><div class="k">Decision</div><div class="v">${esc(item.status)}</div></div>` +
    `<div class="card"><div class="k">Our quoted price</div><div class="v">${fmtPrice(item.response_price)}</div></div>` +
    `<div class="card"><div class="k">${esc(item.market_price != null ? item.market_source : "Market price")}</div>` +
    `<div class="v">${fmtPrice(item.market_price)}</div></div>` +
    `<div class="card"><div class="k">Edge vs market</div><div class="v">${item.edge_vs_market == null ? "—" : (item.edge_vs_market >= 0 ? "+" : "") + Number(item.edge_vs_market).toFixed(4)}</div></div>` +
    `</div>` +
    `<p>Reason: <strong>${esc(item.reason_code)}</strong> ${esc(item.reason_detail || "")}</p>` +
    `<p>Quote size: <strong>${esc(item.size ?? "—")} ${esc(item.size_unit ?? "")}</strong> · ` +
    `Model fair <strong>${item.fair ?? "—"}</strong> · Naive product <strong>${item.naive ?? "—"}</strong></p>` +
    `<p>Wait: <strong>${fmtMs(item.wait_ms)} ms</strong> · Compute: <strong>${fmtMs(item.compute_ms)} ms</strong></p>` +
    `<details><summary>Pricing adjustments and per-decision detail</summary>` +
    `<pre>${esc(JSON.stringify(slim, null, 2))}</pre></details>`;
}

/* ------------------------------------------------------------------ */
/* Performance                                                         */
/* ------------------------------------------------------------------ */
function lineChart(svgId, points, key) {
  const svg = $(svgId);
  const W = 600, H = 180, PAD = 8;
  if (!points.length) { svg.innerHTML = ""; return; }
  const vals = points.map((p) => Number(p[key] || 0));
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const span = hi - lo || 1;
  const X = (i) => PAD + (i / Math.max(1, points.length - 1)) * (W - 2 * PAD);
  const Y = (v) => PAD + (1 - (v - lo) / span) * (H - 2 * PAD);
  const d = vals.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ");
  const zeroY = Y(0).toFixed(1);
  svg.innerHTML =
    `<line x1="${PAD}" y1="${zeroY}" x2="${W - PAD}" y2="${zeroY}" stroke="#26334d"/>` +
    `<path class="area" d="${d} L${X(vals.length - 1).toFixed(1)},${H - PAD} L${PAD},${H - PAD} Z"/>` +
    `<path class="line" d="${d}"/>`;
}

async function refreshPerformance() {
  const p = await get("/api/performance");
  const cards = [
    ["Quotes", p.quoted.toLocaleString()], ["Shadow fills", p.shadow_fills.toLocaleString()],
    ["Win rate", fmtPct(p.win_rate)], ["Net notional", fmtMoney(p.net_notional)],
    ["Expected P&L", fmtMoney(p.expected_pnl)], ["Realized P&L", fmtMoney(p.realized_pnl)],
    ["Max downswing", fmtMoney(p.max_downswing)], ["Max upswing", fmtMoney(p.max_upswing)],
  ];
  $("perf-cards").innerHTML = cards.map(([k, v]) =>
    `<div class="card"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("");
  lineChart("chart-notional", p.curve, "net_notional");
  lineChart("chart-realized", p.curve, "realized_pnl");
  const breakdown = (id, rows, key) => setRows(id, rows.map((r) =>
    `<tr><td>${esc(r[key])}</td><td class="num">${r.shadow_fills}</td>` +
    `<td class="num">${fmtMoney(r.expected_pnl)}</td><td class="num">${fmtMoney(r.realized_pnl)}</td></tr>`
  ).join(""));
  breakdown("perf-family", p.by_family, "family");
  breakdown("perf-legs", p.by_legs, "n_legs");
  breakdown("perf-game", p.by_game, "game");
}

/* ------------------------------------------------------------------ */
/* Engine                                                              */
/* ------------------------------------------------------------------ */
async function refreshEngine() {
  const s = await get("/api/engine");
  $("latency-budget").textContent = s.budget_ms;
  const box = $("engine-health");
  const h = s.health;
  if (h) {
    const started = new Date(h.started_at.replace("Z", "+00:00"));
    const uptimeS = Math.max(0, Math.floor((Date.now() - started.getTime()) / 1000));
    const uptime = `${Math.floor(uptimeS / 3600)}h ${Math.floor(uptimeS % 3600 / 60)}m`;
    let hbWarn = "";
    try {
      const hbAgeS = (Date.now() - new Date(String(h.heartbeat_at).replace("Z", "+00:00")).getTime()) / 1000;
      if (hbAgeS > 1800) hbWarn =
        `<div class="alert warn">Capture heartbeat is stale — last beat ${Math.round(hbAgeS / 60)} min ago ` +
        `(threshold 30 min). The capture process may be down; see docs/always-on.md.</div>`;
    } catch (e) { /* unparsable heartbeat; skip */ }
    box.innerHTML =
      `<div class="cards">` +
      `<div class="card"><div class="k">Uptime</div><div class="v">${esc(uptime)}</div></div>` +
      `<div class="card"><div class="k">Messages processed</div><div class="v">${Number(h.messages_processed).toLocaleString()}</div></div>` +
      `<div class="card"><div class="k">Errors</div><div class="v">${h.errors}</div></div>` +
      `<div class="card"><div class="k">Gateway</div><div class="v">${h.gateway_connected ? "connected" : "disconnected"}</div></div>` +
      `</div>${hbWarn}` +
      `<p class="caption">Last heartbeat: ${esc(h.heartbeat_at)} · Buffer drops: ${esc(h.buffer_drops)}</p>`;
  } else {
    box.innerHTML = `<p class="caption">No engine heartbeat recorded yet.</p>`;
  }
  const w = s.wait, c = s.compute;
  if (s.samples) {
    setRows("latency-table",
      `<tr><td>Posted → engine started (wait)</td><td class="num">${fmtMs(w.p50)}</td>` +
      `<td class="num">${fmtMs(w.p95)}</td><td class="num">${fmtMs(w.max)}</td></tr>` +
      `<tr><td>Engine started → decision recorded (compute)</td><td class="num">${fmtMs(c.p50)}</td>` +
      `<td class="num">${fmtMs(c.p95)}</td><td class="num">${fmtMs(c.max)}</td></tr>`);
    $("latency-alert").innerHTML =
      (w.p50 != null && w.p50 > s.budget_ms)
        ? `<div class="alert error">p50 wait ${w.p50.toFixed(0)} ms exceeds the ${s.budget_ms} ms budget</div>`
        : "";
  } else {
    setRows("latency-table", "");
    $("latency-alert").innerHTML = `<p class="caption">No live timing samples yet.</p>`;
  }
  setRows("reasons-table", s.reasons.map((r) =>
    `<tr><td>${esc(r.reason_code)}</td><td class="num">${r.n}</td></tr>`).join(""));
  setRows("drafts-table", s.drafts.map((d) =>
    `<tr><td>${esc(d.quote_id)}</td><td>${esc(d.rfq_id.slice(0, 12))}…</td>` +
    `<td class="num">${fmtPrice(d.buy_price)}</td><td class="num">${fmtPrice(d.sell_price)}</td>` +
    `<td>${esc(d.buy_qty_decimal ?? "—")}</td><td>${esc(d.sell_qty_decimal ?? "—")}</td>` +
    `<td>${esc(d.created_time)}</td></tr>`).join(""));
}

/* ------------------------------------------------------------------ */
showTab("rfqs");
