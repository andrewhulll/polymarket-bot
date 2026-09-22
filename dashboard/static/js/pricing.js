/* Pricing tab: every decision, both edges, model internals in the drawer. */
"use strict";

$("pricing-prev").addEventListener("click", () => { if (state.pricingPage > 1) { state.pricingPage--; refreshPricing(); } });
$("pricing-next").addEventListener("click", () => { state.pricingPage++; refreshPricing(); });
$("settlement-run").addEventListener("click", runSettlementCheck);
$("settlement-prev").addEventListener("click", () => { if (state.settlementPage > 1) { state.settlementPage--; refreshPricing(); } });
$("settlement-next").addEventListener("click", () => { state.settlementPage++; refreshPricing(); });

function decisionBadge(status) {
  if (status === "QUOTED") return badge("QUOTED", "q");
  if (!status || status === "PENDING") return badge("PENDING", "dim");
  return badge(status, "no");
}

async function refreshPricing() {
  const [data, settlements] = await Promise.all([
    get(`/api/pricing?page=${state.pricingPage}`), get(`/api/settlements?page=${state.settlementPage}`)
  ]);
  renderSettlementSummary(settlements);
  state.pricingTotal = data.total;
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  if (state.pricingPage > pages) { state.pricingPage = pages; return refreshPricing(); }
  $("pricing-page-label").textContent = `p ${data.page}/${pages}`;
  $("pricing-prev").disabled = data.page <= 1;
  $("pricing-next").disabled = data.page >= pages;
  $("pricing-count").textContent =
    `${data.total.toLocaleString()} priced RFQs · Market ref is the accepted Combo trade (◉) when observed; RFQs that never traded have no market ref`;

  const quoted = data.rows.filter((r) => r.status === "QUOTED");
  const edges = quoted.map((r) => r.edge_vs_market).filter((v) => v != null);
  const avgEdge = edges.length ? edges.reduce((a, b) => a + b, 0) / edges.length : null;
  $("pricing-kpis").innerHTML =
    kpi("Decisions (page)", fmtInt(data.rows.length)) +
    kpi("Quoted (page)", fmtInt(quoted.length)) +
    kpi("Avg quote edge", fmtEdge(avgEdge)) +
    kpi("Quote rate", fmtPct(quoted.length / Math.max(1, data.rows.length)));

  setRows("pricing-table", data.rows.map((r) =>
    `<tr class="clickable" data-rfq="${esc(r.rfq_id)}">` +
    `<td>${shortId(r.rfq_id)}</td>` +
    `<td class="dim">${esc((r.priced_at || "").slice(11, 19))}</td>` +
    `<td>${decisionBadge(r.status)}${r.after_deadline ? ' <span class="badge warn" title="Paper quote priced after exchange deadline">LATE</span>' : ""}</td>` +
    `<td class="dim">${esc(r.reason_code || "")}</td>` +
    `<td class="num"><b>${fmtPrice(r.response_price)}</b></td>` +
    `<td class="num">${fmtPrice(r.fair)}</td>` +
    `<td class="num">${fmtPrice(r.naive)}</td>` +
    `<td class="num">${fmtPrice(r.market_price)}${r.market_source ? ` <span class="dim" title="${esc(r.market_source)}">◉</span>` : ""}</td>` +
    `<td class="num">${fmtEdge(r.edge_vs_market)}</td>` +
    `<td class="num">${fmtEdge(r.model_edge)}</td>` +
    `<td class="num">${fmtMs(r.wait_ms)}</td>` +
    `<td class="num">${fmtMs(r.compute_ms)}</td></tr>`
  ).join(""));
  document.querySelectorAll("#pricing-table tbody tr").forEach((tr) =>
    tr.addEventListener("click", () => openPricingDrawer(tr.dataset.rfq)));
}

function renderSettlementSummary(s) {
  const score = (v) => v == null ? "—" : Number(v).toFixed(4);
  $("settlement-kpis").innerHTML =
    kpi("Eligible quotes", fmtInt(s.eligible)) +
    kpi("Settled", fmtInt(s.settled), s.settled ? "good" : "") +
    kpi("Pending", fmtInt(s.pending + s.unscored)) +
    kpi("Void", fmtInt(s.void)) +
    kpi("Unresolved", fmtInt(s.unresolved)) +
    kpi("Model Brier", score(s.model_brier)) +
    kpi("Naive Brier", score(s.naive_brier)) +
    kpi("Combo hit rate", fmtPct(s.hit_rate)) +
    kpi("Naive − model", s.brier_advantage == null ? "—" : fmtEdge(s.brier_advantage));
  $("settlement-last").textContent = s.last_checked
    ? `last scored ${new Date(s.last_checked).toLocaleString()}`
    : (s.available ? "not checked yet" : "waiting for the quote ledger");

  const pages = Math.max(1, Math.ceil((s.total || 0) / (s.page_size || 500)));
  if (state.settlementPage > pages) { state.settlementPage = pages; return refreshPricing(); }
  $("settlement-page-label").textContent = `p ${s.page || 1}/${pages}`;
  $("settlement-prev").disabled = (s.page || 1) <= 1;
  $("settlement-next").disabled = (s.page || 1) >= pages;
  setRows("settlement-table", (s.rows || []).map((r, i) =>
    `<tr class="clickable" data-i="${i}"><td>${shortId(r.rfq_id)}</td>` +
    `<td class="dim">${esc(r.game || "—")}</td><td>${decisionBadge(r.status)}</td>` +
    `<td class="num">${fmtPrice(r.combo_value)}</td><td class="num">${fmtPrice(r.fair)}</td>` +
    `<td class="num">${fmtPrice(r.naive)}</td><td class="num">${score(r.brier)}</td>` +
    `<td class="num">${score(r.naive_brier)}</td><td class="num">${fmtEdge(r.hypo_edge_bid)}</td>` +
    `<td class="num">${fmtEdge(r.hypo_edge_ask)}</td><td class="num">${fmtMoney(r.realized_pnl)}</td></tr>`
  ).join("") || `<tr><td colspan="11" class="dim">no settlement rows yet</td></tr>`);
  (s.rows || []).forEach((r, i) => {
    const tr = document.querySelector(`#settlement-table tbody tr[data-i="${i}"]`);
    if (tr) tr.addEventListener("click", () => openSettlementDrawer(r));
  });
}

function openSettlementDrawer(r) {
  const legs = (r.legs || []).map((l) => `<tr><td class="mono">${esc(l.position_id || "")}</td>` +
    `<td>${esc(l.side || "—")}</td><td>${l.settlement_price == null ? "—" : esc(l.settlement_price)}</td>` +
    `<td class="mono">${esc(l.game_id || "—")}</td></tr>`).join("");
  openDrawer(shortId(r.rfq_id), `
    <p>${decisionBadge(r.status)} <span class="dim">${esc(r.reason_detail || "")}</span></p>
    <div class="kpis">${kpi("Combo value", fmtPrice(r.combo_value))}${kpi("Model Brier", r.brier == null ? "—" : Number(r.brier).toFixed(4))}${kpi("Naive Brier", r.naive_brier == null ? "—" : Number(r.naive_brier).toFixed(4))}${kpi("Accepted P&L", fmtMoney(r.realized_pnl))}</div>
    <dl class="kv"><dt>Trigger</dt><dd>${esc(r.trigger)}</dd><dt>Quoted bid / ask</dt><dd>${fmtPrice(r.bid)} / ${fmtPrice(r.ask)}</dd><dt>Fair / naive</dt><dd>${fmtPrice(r.fair)} / ${fmtPrice(r.naive)}</dd><dt>Hypothetical edges</dt><dd>bid ${fmtEdge(r.hypo_edge_bid)} · ask ${fmtEdge(r.hypo_edge_ask)}</dd><dt>Accepted quote</dt><dd>${fmtPrice(r.accepted_price)} × ${esc(r.accepted_size || "—")} ${esc(r.accepted_direction || "")}</dd><dt>Scores vintage</dt><dd>${esc(r.scores_vintage || "—")}</dd><dt>Model / params</dt><dd class="mono">${esc(r.model_version || "—")} · ${esc(r.params_version || "—")}</dd></dl>
    <h3>Settled legs (${r.n_legs_settled ?? 0}/${r.n_legs ?? 0})</h3><div class="table-wrap"><table><thead><tr><th>Position</th><th>Side</th><th>Settlement</th><th>Game</th></tr></thead><tbody>${legs || '<tr><td colspan="4" class="dim">no leg detail</td></tr>'}</tbody></table></div>`);
}

async function runSettlementCheck() {
  const button = $("settlement-run");
  const result = $("settlement-result");
  button.disabled = true;
  button.textContent = "Refreshing scores and settling…";
  result.innerHTML = '<div class="alert">This may take a few minutes.</div>';
  try {
    const res = await post("/api/settlements/run", {});
    renderSettlementSummary(await get(`/api/settlements?page=${state.settlementPage}`));
    const pullWarning = res.pull.returncode === 0 ? ""
      : ` Score refresh exited ${res.pull.returncode}; cached scores were used.`;
    result.innerHTML = res.ok
      ? `<div class="alert good">Settlement check finished.${esc(pullWarning)}</div>`
      : `<div class="alert error">Settlement failed (exit ${res.settle.returncode}). ${esc(res.settle.output)}</div>`;
  } catch (e) {
    result.innerHTML = `<div class="alert error">Settlement check failed: ${esc(e.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = "Check settlement for all priced RFQs";
  }
}

async function openPricingDrawer(rfqId) {
  openDrawer(shortId(rfqId), `<p class="caption">loading…</p>`);
  const r = await get(`/api/pricing/${encodeURIComponent(rfqId)}`);
  const detail = r.detail || {};
  const comps = detail.components || {};
  const compRows = Object.entries(comps).map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td class="num">${typeof v === "number" ? v.toFixed(1) : esc(v)}</td></tr>`).join("");
  const spreadTotal = Object.values(comps).filter((v) => typeof v === "number")
    .reduce((a, b) => a + b, 0);
  const explanations = (detail.explanations || []).map((e) => `<li>${esc(e)}</li>`).join("");
  const legs = (detail.legs || []).map((l) =>
    `<tr><td class="mono">${esc(l.label || l.symbol || "")}</td><td>${esc(l.book_source || "—")}</td>` +
    `<td class="num">${fmtPrice(l.bid)}</td><td class="num">${fmtPrice(l.ask)}</td>` +
    `<td class="num">${fmtPrice(l.q_market ?? l.q ?? l.mark)}</td></tr>`).join("");

  openDrawer(shortId(rfqId), `
    <p>${decisionBadge(r.status)}${r.after_deadline ? ' <span class="badge warn">LATE</span>' : ""} <span class="dim">${esc(r.reason_code || "")} ${esc(r.reason_detail || "")}</span></p>
    ${r.after_deadline ? '<p class="caption">Paper price computed after the exchange submission deadline; it could not have been submitted for this RFQ.</p>' : ""}
    <div class="kpis">
      ${kpi("Our price", fmtPrice(r.response_price))}
      ${kpi("Model fair", fmtPrice(r.fair))}
      ${kpi("Naive", fmtPrice(r.naive))}
      ${kpi("Market ref", fmtPrice(r.market_price))}
    </div>
    <dl class="kv">
      <dt>Quote edge</dt><dd>${fmtEdge(r.edge_vs_market)} <span class="dim">(our price vs ${esc(r.market_source || "market")})</span></dd>
      <dt>Model edge</dt><dd>${fmtEdge(r.model_edge)} <span class="dim">(fair vs market)</span></dd>
      <dt>Action / size</dt><dd>${esc(r.response_action || "—")} ${esc(r.size || "")} ${esc(r.size_unit || "")}</dd>
      <dt>Spread</dt><dd>${compRows ? spreadTotal.toFixed(1) + " bps total" : "—"}</dd>
      <dt>Corr adjustment</dt><dd>${detail.corr_adjustment_bps != null ? Number(detail.corr_adjustment_bps).toFixed(1) + " bps" : "—"}</dd>
      <dt>Wait / compute</dt><dd>${fmtMs(r.wait_ms)} ms / ${fmtMs(r.compute_ms)} ms</dd>
      <dt>Model</dt><dd class="mono">${esc(r.model_version || "—")} · params ${esc(r.params_version || "—")} · ${esc(r.decided_by || "")}</dd>
      <dt>Priced at</dt><dd>${esc(r.priced_at || "—")}</dd>
    </dl>
    ${explanations ? `<h3>Why</h3><ul>${explanations}</ul>` : ""}
    ${compRows ? `<h3>Spread components (bps)</h3>
      <div class="table-wrap"><table><thead><tr><th>Component</th><th class="num">bps</th></tr></thead><tbody>${compRows}</tbody></table></div>` : ""}
    ${legs ? `<h3>Legs</h3>
      <div class="table-wrap"><table><thead><tr><th>Leg</th><th>Book</th><th class="num">Bid</th><th class="num">Ask</th><th class="num">Mark</th></tr></thead><tbody>${legs}</tbody></table></div>` : ""}
    <details><summary>Full decision JSON</summary><pre class="json">${esc(JSON.stringify(detail, null, 2))}</pre></details>
  `);
}
