/* Performance tab: P&L, Brier, settlement check, and the merged fills + settlement ledger. */
"use strict";

$("fills-prev").addEventListener("click", () => { if (state.fillsPage > 1) { state.fillsPage--; refreshPerformance(); } });
$("fills-next").addEventListener("click", () => { state.fillsPage++; refreshPerformance(); });
$("settlement-run").addEventListener("click", runSettlementCheck);

let lastSettlements = null;

async function refreshPerformance() {
  const fillsUrl = `/api/fills?page=${state.fillsPage}${state.game ? `&game=${encodeURIComponent(state.game)}` : ""}`;
  const [perf, fills, settlements, source] = await Promise.all([
    get("/api/performance"), get(fillsUrl),
    get("/api/settlements?page=1").catch(() => null),
    get("/api/source/meta").catch(() => null),
  ]);
  lastSettlements = settlements;

  const m = source?.meta || {};
  $("source-meta").textContent = source
    ? `${source.label}${m.n_rfqs != null ? ` · ${m.n_rfqs} RFQs across ${m.n_games ?? "?"} games` : ""}` +
      `${m.data_vintage?.pull_date ? ` · data ${m.data_vintage.pull_date}` : ""}` +
      `${m.params_version ? ` · params ${m.params_version}` : ""}`
    : "";

  renderSettlementSummary(settlements);
  renderPerfKpis(perf, settlements);

  // P&L: realized vs expected, one shared scale
  const curve = perf.curve || [];
  if (curve.length) {
    linesChart("chart-realized", [
      { values: curve.map((c) => c.expected_pnl ?? 0), color: "#f2f2f2", dash: "4 3" },
      { values: curve.map((c) => c.realized_pnl), color: "#ff9f1c" },
    ], { zero: true });
    const last = curve[curve.length - 1];
    setFoot("pnl-foot", [
      `<span class="lg"><i style="background:#ff9f1c"></i>realized ${fmtMoney(last.realized_pnl)}</span>`,
      `<span class="lg"><i style="background:#f2f2f2"></i>expected ${fmtMoney(last.expected_pnl)}</span>`,
      `net notional ${fmtMoney0(perf.net_notional)}`]);
  } else {
    chartEmpty("chart-realized", "no fills yet"); setFoot("pnl-foot", []);
  }

  renderBrierChart(settlements);

  barList("perf-family", (perf.by_family || []).map((r) => ({
    label: r.family || "—", value: r.realized_pnl ?? 0, cls: (r.realized_pnl ?? 0) >= 0 ? "p" : "n",
    text: `${fmtMoney0(r.realized_pnl)} <span class="dim">· ${fmtInt(r.shadow_fills)}</span>`,
  })).sort((a, b) => b.value - a.value), "no fills yet");

  setRows("perf-games", (perf.by_game || []).map((r) => {
    const on = state.game && String(r.game || "").includes(state.game);
    return `<tr class="${on ? "selected" : ""}"><td>${gameLink(r.game)}</td><td class="num">${fmtInt(r.shadow_fills)}</td>` +
      `<td class="num">${fmtPnl(r.expected_pnl)}</td><td class="num">${fmtPnl(r.realized_pnl)}</td></tr>`;
  }).join("") || `<tr><td colspan="4" class="dim" style="padding:10px">no fills yet</td></tr>`);

  renderFillsTable(fills, settlements);
}

function renderPerfKpis(perf, s) {
  const settled = s?.settled ?? 0;
  setTabKpis(
    kpi("Shadow fills", fmtInt(perf.shadow_fills), "", `${fmtInt(perf.quoted)} quoted`) +
    kpi("Settled", fmtInt(settled), settled ? "good" : "", s ? `of ${fmtInt(s.eligible)} eligible` : "") +
    kpi("Expected P&L", fmtMoney0(perf.expected_pnl), perf.expected_pnl >= 0 ? "good" : "bad") +
    kpi("Hit rate", fmtPct(s?.hit_rate), "", "combo, settled") +
    kpi("Max downswing", fmtMoney0(perf.max_downswing), "bad") +
    kpi("Max upswing", fmtMoney0(perf.max_upswing), "good") +
    kpi("Model Brier", fmtScore(s?.model_brier)) +
    kpi("Brier lift", s?.brier_advantage == null ? "—" : fmtEdge(s.brier_advantage, 4), "", "naive − model"));
}

function renderBrierChart(s) {
  const rows = (s?.rows || []).filter((r) => r.status === "SETTLED" && r.brier != null && r.naive_brier != null).reverse();
  if (rows.length < 2) { chartEmpty("chart-brier", rows.length ? "need 2+ settled quotes" : "no settled quotes yet"); setFoot("brier-foot", []); return; }
  let m = 0, n = 0;
  const model = [], naive = [];
  rows.forEach((r, i) => { m += r.brier; n += r.naive_brier; model.push(m / (i + 1)); naive.push(n / (i + 1)); });
  linesChart("chart-brier", [
    { values: naive, color: "#f2f2f2", dash: "4 3" }, { values: model, color: "#ff9f1c" }]);
  setFoot("brier-foot", [
    `<span class="lg"><i style="background:#ff9f1c"></i>model ${fmtScore(model[model.length - 1])}</span>`,
    `<span class="lg"><i style="background:#f2f2f2"></i>naive ${fmtScore(naive[naive.length - 1])}</span>`,
    `n ${rows.length}`]);
}

function renderSettlementSummary(s) {
  if (!s) { $("settlement-last").textContent = "settlement ledger unavailable"; return; }
  const parts = [
    `${fmtInt(s.eligible)} eligible`, `${fmtInt(s.settled)} settled`,
    `${fmtInt((s.pending || 0) + (s.unscored || 0))} pending`,
    s.void ? `${fmtInt(s.void)} void` : "", s.unresolved ? `${fmtInt(s.unresolved)} unresolved` : ""].filter(Boolean);
  $("settlement-last").textContent = (s.available ? parts.join(" · ") : "waiting for the quote ledger") +
    (s.last_checked ? ` · last scored ${new Date(s.last_checked).toLocaleString()}` : (s.available ? " · not checked yet" : ""));
}

async function runSettlementCheck() {
  const button = $("settlement-run");
  const result = $("settlement-result");
  button.disabled = true;
  button.textContent = "Refreshing scores and settling…";
  result.innerHTML = '<div class="alert info">This may take a few minutes.</div>';
  try {
    const res = await post("/api/settlements/run", {});
    const pullWarning = res.pull.returncode === 0 ? ""
      : ` Score refresh exited ${res.pull.returncode}; cached scores were used.`;
    result.innerHTML = res.ok
      ? `<div class="alert good">Settlement check finished.${esc(pullWarning)}</div>`
      : `<div class="alert error">Settlement failed (exit ${res.settle.returncode}). ${esc(res.settle.output)}</div>`;
    await refreshPerformance();
  } catch (e) {
    result.innerHTML = `<div class="alert error">Settlement check failed: ${esc(e.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = "Check settlement for all priced RFQs";
  }
}

function outcomeCell(r) {
  if (r.settlement_status === "SETTLED" && r.combo_value != null) {
    return r.combo_value >= 0.5 ? `<span class="pos">HIT</span>` : `<span class="neg">MISS</span>`;
  }
  if (r.settlement_status === "VOID") return `<span class="dim">void</span>`;
  return `<span class="hi">live</span> <span class="dim">${r.settled_legs ?? "?"}/${r.total_legs ?? "?"}</span>`;
}

function renderFillsTable(fills, settlements) {
  const pages = Math.max(1, Math.ceil(fills.total / fills.page_size));
  if (state.fillsPage > pages) { state.fillsPage = pages; return refreshPerformance(); }
  $("fills-page-label").textContent = `${fills.page} / ${pages}`;
  $("fills-prev").disabled = fills.page <= 1;
  $("fills-next").disabled = fills.page >= pages;
  $("fills-meta").textContent = `${fmtInt(fills.total)} fills${state.game ? ` · ${state.game}` : ""} · prices at fill · outcome after final whistle`;

  const byRfq = {};
  (settlements?.rows || []).forEach((r) => { byRfq[r.rfq_id] = r; });
  fills._rows = fills.rows.map((r) => ({ ...r, _s: byRfq[r.rfq_id] || null }));

  setRows("fills-table", fills._rows.map((r, i) => {
    const s = r._s;
    const better = s && s.brier != null && s.naive_brier != null && s.brier <= s.naive_brier;
    return `<tr class="clickable" data-i="${i}" data-rfq="${esc(r.rfq_id)}">` +
      `<td class="expander">▸</td>` +
      `<td class="dim">${esc((r.time || "").slice(11, 19))}${r.after_deadline ? ' <span class="badge warn" title="Quoted after the submission deadline">LATE</span>' : ""}</td>` +
      `<td class="w">${shortId(r.rfq_id)}</td>` +
      `<td>${gameLink((r.game || "").split(", ")[0])}</td>` +
      `<td class="${(r.response_action || r.side) === "BUY" ? "pos" : "neg"}">${esc(r.response_action || r.side || "—")}</td>` +
      `<td class="num">${esc(r.size || "")}${r.capacity_limited ? ' <span class="badge warn" title="Paper fill reduced to stay within equity">CAPPED</span>' : ""}</td>` +
      `<td class="num dim">${fmtPrice(r.naive)}</td>` +
      `<td class="num">${fmtPrice(r.fair)}</td>` +
      `<td class="num w">${fmtPrice(r.our_price)}</td>` +
      `<td class="num">${fmtPrice(r.market_price)}${r.market_source ? ` <span class="dim" title="${esc(r.market_source)}">◉</span>` : ""}</td>` +
      `<td class="num">${fmtPnl(r.expected_pnl)}</td>` +
      `<td>${outcomeCell(r)}</td>` +
      `<td class="num ${s ? (better ? "pos" : "neg") : "dim"}">${fmtScore(s?.brier)}</td>` +
      `<td class="num dim">${fmtScore(s?.naive_brier)}</td>` +
      `<td class="num">${fmtPnl(r.realized_pnl)}</td></tr>` +
      `<tr class="fill-detail hidden" id="fill-det-${i}"><td colspan="15"></td></tr>`;
  }).join("") || `<tr><td colspan="15" class="dim" style="padding:12px">no fills${state.game ? ` for ${esc(state.game)}` : " yet"}</td></tr>`);
  document.querySelectorAll("#fills-table tbody tr.clickable").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      if (e.target.closest("[data-game]")) return;
      toggleFillDetail(tr, fills._rows[Number(tr.dataset.i)]);
    }));
}

function toggleFillDetail(tr, r) {
  const det = document.getElementById("fill-det-" + tr.dataset.i);
  const open = det.classList.toggle("hidden");
  tr.querySelector(".expander").textContent = open ? "▸" : "▾";
  if (open) return;
  const s = r._s;
  const legs = (r.settlement_legs || []).map((leg) => {
    const raw = leg.settlement_price;
    const side = leg.side || "YES";
    const won = raw == null ? "—" : ((side === "NO" ? Number(raw) === 0 : Number(raw) === 1) ? "won" : "lost");
    return `<tr><td class="mono">${esc(leg.position_id || leg.symbol || "—")}</td>` +
      `<td>${esc(side)}</td><td class="num">${raw == null ? "—" : esc(raw)}</td>` +
      `<td>${won}</td><td class="mono">${esc(leg.game_id || "—")}</td></tr>`;
  }).join("");
  det.querySelector("td").innerHTML = `
    <div class="kpis">
      ${kpi("Naive", fmtPrice(r.naive))}${kpi("Model fair", fmtPrice(r.fair))}
      ${kpi("Our price", fmtPrice(r.our_price), "hi")}${kpi("Market", fmtPrice(r.market_price))}
      ${s ? kpi("Hypo bid edge", fmtEdge(s.hypo_edge_bid)) + kpi("Hypo ask edge", fmtEdge(s.hypo_edge_ask)) : ""}
    </div>
    <dl class="kv" style="grid-template-columns:150px 1fr">
      <dt>Quote edge</dt><dd>${fmtEdge(r.quote_edge)} <span class="dim">(our edge vs ${esc(r.market_source || "market")})</span></dd>
      <dt>Model edge</dt><dd>${fmtEdge(r.model_edge)} <span class="dim">(fair vs market)</span></dd>
      <dt>Expected / realized</dt><dd>${fmtMoney(r.expected_pnl)} / ${fmtMoney(r.realized_pnl)}</dd>
      <dt>Net notional</dt><dd>${fmtMoney(r.net_notional)}</dd>
      <dt>Settlement</dt><dd>${esc(r.settlement_status || "—")} · ${r.settled_legs ?? "?"}/${r.total_legs ?? "?"} legs settled</dd>
      <dt>Combo YES value</dt><dd>${fmtPrice(r.combo_value)}</dd>
      <dt>${esc(r.side || "YES")} payout</dt><dd>${fmtPrice(r.settlement_value)}</dd>
      <dt>Size</dt><dd>${esc(r.size || "")} ${esc(r.size_unit || "")}</dd>
      ${s ? `<dt>Accepted quote</dt><dd>${fmtPrice(s.accepted_price)} × ${esc(s.accepted_size || "—")} ${esc(s.accepted_direction || "")}</dd>
             <dt>Model / params</dt><dd class="mono">${esc(s.model_version || "—")} · ${esc(s.params_version || "—")}</dd>` : ""}
      ${r.capacity_limited ? `<dt>Original size</dt><dd>${esc(r.original_size)} ${esc(r.size_unit || "")} — reduced to stay within equity</dd>` : ""}
    </dl>
    ${legs ? `<div class="table-wrap" style="max-height:none"><table>
      <thead><tr><th>Position</th><th>Side</th><th class="num">Settlement</th><th>Result</th><th>Game</th></tr></thead>
      <tbody>${legs}</tbody></table></div>` : ""}
    <p style="margin:8px 0 0"><a href="#" data-open-rfq="${esc(r.rfq_id)}">Open full decision detail →</a></p>`;
  det.querySelector("[data-open-rfq]").addEventListener("click", (e) => {
    e.preventDefault(); openRfqDrawer(e.currentTarget.dataset.openRfq);
  });
}
