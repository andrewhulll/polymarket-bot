/* Flow tab: every RFQ from screen to decision to trade, plus the funnel, arrivals
   and quote-edge panels, and the docked RFQ detail drawer (screen, pricing, lifecycle). */
"use strict";

$("only-quotable").addEventListener("change", (e) => {
  state.rfqOnly = e.target.checked; state.rfqPage = 1; refreshFlow();
});
$("screen-filter").addEventListener("change", (e) => {
  state.rfqScreen = e.target.value; state.rfqPage = 1; refreshFlow();
});
$("decision-filter").addEventListener("change", (e) => {
  state.rfqDecision = e.target.value; state.rfqPage = 1; refreshFlow();
});
$("status-filter").addEventListener("change", (e) => {
  state.rfqStatus = e.target.value; state.rfqPage = 1; refreshFlow();
});
$("game-filter").addEventListener("change", (e) => setGame(e.target.value));
$("rfq-search").addEventListener("input", (e) => {
  clearTimeout($("rfq-search")._t);
  $("rfq-search")._t = setTimeout(() => {
    state.rfqSearch = e.target.value.trim(); state.rfqPage = 1; refreshFlow();
  }, 350);
});
$("rfq-prev").addEventListener("click", () => { if (state.rfqPage > 1) { state.rfqPage--; refreshFlow(); } });
$("rfq-next").addEventListener("click", () => { state.rfqPage++; refreshFlow(); });

function screenBadge(screen, quotable, status) {
  if (quotable) return badge("QUOTABLE", "q");
  if (screen === "QUOTABLE") return badge(status === "DECLINED" ? "DECLINED" : "PRICING", "warn");
  if (!screen) return badge("PENDING", "dim");
  return badge(screen, "no");
}

function decisionBadge(status) {
  if (status === "QUOTED") return badge("QUOTED", "q");
  if (!status || status === "PENDING") return badge("PENDING", "dim");
  return badge(status, "no");
}

async function refreshFlow() {
  const q = new URLSearchParams({
    only_quotable: state.rfqOnly ? "1" : "0",
    page: String(state.rfqPage),
    ...(state.rfqScreen ? { screen: state.rfqScreen } : {}),
    ...(state.rfqDecision ? { decision: state.rfqDecision } : {}),
    ...(state.rfqStatus ? { status: state.rfqStatus } : {}),
    ...(state.game ? { game: state.game } : {}),
    ...(state.rfqSearch ? { search: state.rfqSearch } : {}),
  });
  const summaryQuery = state.game ? `?game=${encodeURIComponent(state.game)}` : "";
  const [data, summary] = await Promise.all([
    get(`/api/rfqs?${q}`),
    get(`/api/flow/summary${summaryQuery}`).catch(() => null),
  ]);
  state.rfqTotal = data.total;
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  if (state.rfqPage > pages) { state.rfqPage = pages; return refreshFlow(); }

  // keep the screen filter options fresh from observed screens
  const sel = $("screen-filter");
  const seen = new Set([...sel.options].map((o) => o.value));
  data.rows.forEach((r) => {
    if (r.screen && !seen.has(r.screen)) {
      seen.add(r.screen);
      const o = document.createElement("option");
      o.value = o.textContent = r.screen;
      sel.appendChild(o);
    }
  });
  const syncOptions = (id, values, emptyLabel, current) => {
    const el = $(id);
    el.innerHTML = `<option value="">${emptyLabel}</option>` + (values || []).map((v) =>
      `<option value="${esc(v)}" ${v === current ? "selected" : ""}>${esc(v)}</option>`).join("");
  };
  syncOptions("status-filter", data.filter_options?.statuses, "all statuses", state.rfqStatus);
  syncOptions("game-filter", data.filter_options?.games, "all games", state.game);

  $("rfq-page-label").textContent = `${data.page} / ${pages}`;
  $("rfq-prev").disabled = data.page <= 1;
  $("rfq-next").disabled = data.page >= pages;
  $("rfq-count").textContent =
    `${data.total.toLocaleString()} RFQs${state.game ? ` · ${state.game}` : ""} · showing ${data.rows.length} · click a row for detail`;

  setRows("rfq-table", data.rows.map((r) => {
    const failed = Object.values(r.filters || {}).filter((v) => v === false).length;
    const priced = r.decision != null;
    const game = (r.game || "").split(", ")[0];
    return `<tr class="clickable${r.rfq_id === state.selectedRfq ? " selected" : ""}" data-rfq="${esc(r.rfq_id)}">` +
      `<td class="w">${shortId(r.rfq_id)}</td>` +
      `<td class="num dim">${ageStr(r.created_time)}</td>` +
      `<td>${screenBadge(r.screen, r.quotable, r.status)}${failed ? ` <span class="badge warn">${failed}✕</span>` : ""}</td>` +
      `<td class="num">${r.n_legs ?? "—"}</td>` +
      `<td>${gameLink(game)}</td>` +
      `<td class="${r.response_action === "BUY" ? "pos" : r.response_action === "SELL" ? "neg" : ""}">${esc(r.response_action || r.side || r.direction || "—")}</td>` +
      `<td class="num">${esc(r.qty_decimal || r.cash_order_qty || "—")}</td>` +
      `<td>${deadlineStr(r.submission_deadline)}</td>` +
      `<td>${priced ? decisionBadge(r.decision) : `<span class="dim">—</span>`}${r.after_deadline ? ' <span class="badge warn" title="Paper quote priced after the exchange deadline">LATE</span>' : ""}</td>` +
      `<td class="dim">${esc(r.reason_code || "")}</td>` +
      `<td class="num dim">${fmtPrice(r.naive)}</td>` +
      `<td class="num">${fmtPrice(r.fair)}</td>` +
      `<td class="num w">${fmtPrice(r.response_price)}</td>` +
      `<td class="num" title="our price vs the accepted trade price">${fmtEdge(r.edge_vs_market)}</td>` +
      `<td>${esc(r.status || "—")}</td>` +
      `<td class="num" title="${esc(r.trade_executed_at || "No confirmed trade observed")}">${fmtPrice(r.trade_price)}</td></tr>`;
  }).join("") || `<tr><td colspan="16" class="dim" style="padding:12px">no RFQs match these filters</td></tr>`);
  document.querySelectorAll("#rfq-table tbody tr[data-rfq]").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      if (e.target.closest("[data-game]")) return;   // a game link sets the global filter instead
      openRfqDrawer(tr.dataset.rfq);
    }));

  renderFlowSummary(summary, data.total);
}

function renderFlowSummary(s, total) {
  if (!s) {
    chartEmpty("flow-arrivals", "summary unavailable"); chartEmpty("flow-edge", "summary unavailable");
    $("flow-funnel").innerHTML = `<div class="panel-note">summary unavailable</div>`;
    setTabKpis(kpi("RFQs", fmtInt(total)));
    return;
  }
  const f = s.funnel;
  $("funnel-meta").textContent = s.game_scoped ? `last hour · ${s.game} · priced RFQs only` : "last hour";
  const stages = [["Received", f.received, "d"], ["Quotable", f.quotable, "f"], ["Priced", f.priced, "f"],
    ["Quoted", f.quoted, ""], ["Accepted", f.accepted, "p"]];
  $("flow-funnel").innerHTML = stages.map(([label, n, cls]) =>
    `<div class="bl"><span class="l">${label}</span><div><div class="b ${cls}" style="width:${f.received ? Math.max(1, (n / f.received) * 100).toFixed(1) : 0}%"></div></div><span class="v">${fmtInt(n)}</span></div>`).join("");

  const arrivals = s.arrivals.map((a) => a.n);
  barsChart("flow-arrivals", arrivals, { titles: s.arrivals.map((a) => `${a.minute.slice(11)}: ${a.n}`) });

  const edge = s.edge;
  if (edge.n) {
    barsChart("flow-edge", edge.buckets.map((b) => b.n),
      { color: (i) => (edge.buckets[i].lo + edge.buckets[i].hi) / 2 >= 0 ? "#3fd07f" : "#ff4d4d",
        titles: edge.buckets.map((b) => `${(b.lo).toFixed(1)}…${b.hi.toFixed(1)}c: ${b.n}`) });
    $("edge-meta").textContent = `median ${edge.median >= 0 ? "+" : "−"}${Math.abs(edge.median).toFixed(1)}c · n ${edge.n}`;
  } else {
    chartEmpty("flow-edge", "no accepted trades to compare");
    $("edge-meta").textContent = "vs accepted trades, cents";
  }

  const arrivalsNow = arrivals.length ? arrivals[arrivals.length - 2] ?? arrivals[arrivals.length - 1] : null;
  setTabKpis(
    kpi("RFQs (page set)", fmtInt(total), "", state.game ? esc(state.game) : "all games") +
    kpi("Received 1h", fmtInt(f.received)) +
    kpi("Quotable", fmtInt(f.quotable), "", f.received ? fmtPct(f.quotable / f.received) : "") +
    kpi("Priced", fmtInt(f.priced)) +
    kpi("Quoted", fmtInt(f.quoted), "", f.priced ? fmtPct(f.quoted / f.priced) + " of priced" : "") +
    kpi("Accepted", fmtInt(f.accepted), f.accepted ? "good" : "", f.quoted ? fmtPct(f.accepted / f.quoted) + " of quoted" : "") +
    kpi("Median edge", edge.n ? `${edge.median >= 0 ? "+" : "−"}${Math.abs(edge.median).toFixed(1)}c` : "—", edge.n ? (edge.median >= 0 ? "good" : "bad") : "") +
    kpi("Last full min", fmtInt(arrivalsNow), "", "RFQs / min"));
}

/* ---- RFQ detail: screen checks, legs, pricing decision, lifecycle ---- */
async function openRfqDrawer(rfqId) {
  state.selectedRfq = rfqId;
  markSelected("rfq-table", rfqId);
  openDrawer(`RFQ detail <span class="dim">${esc(rfqId.slice(0, 14))}</span>`, `<p class="caption">loading…</p>`);
  let d, p = null;
  try {
    [d, p] = await Promise.all([
      get(`/api/rfqs/${encodeURIComponent(rfqId)}`),
      get(`/api/pricing/${encodeURIComponent(rfqId)}`).catch(() => null),
    ]);
  } catch (err) {
    $("drawer-body").innerHTML = `<div class="alert error">Could not load ${esc(rfqId)}: ${esc(err.message)}</div>`;
    return;
  }
  if (state.selectedRfq !== rfqId) return;   // user moved on while loading
  const r = d.rfq || {}, s = d.screen || {};
  const checks = s.checks || s.filters || {};
  const checkRows = Object.entries(checks).map(([k, v]) =>
    `<div class="check"><span class="${v ? "pos" : "neg"}">${v ? "✓" : "✕"}</span><span>${esc(k)}</span></div>`).join("");

  const legRows = (d.legs || []).map((l) =>
    `<tr><td class="mono">${esc(l.symbol)}</td><td>${esc(l.side || "—")}</td>` +
    `<td class="num">${fmtPrice(l.bid)}</td><td class="num">${fmtPrice(l.ask)}</td>` +
    `<td>${l.settlement_price != null ? (l.settlement_price >= 0.5 ? "won" : "lost") : "—"}</td></tr>`).join("");

  const events = (d.events || []).map((e) =>
    `<div class="ev"><div>${esc(e.event_type)}${e.client_derived ? ' <span class="badge dim">derived</span>' : ""}` +
    `<div class="t">${esc((e.recorded_at || "").slice(11, 23))} · ${esc(e.source || "")}</div></div></div>`).join("");

  let pricingHtml;
  if (p && !p.error) {
    const detail = p.detail || {}, comps = detail.components || {};
    const compRows = Object.entries(comps).map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td class="num">${typeof v === "number" ? v.toFixed(1) : esc(v)}</td></tr>`).join("");
    const spreadTotal = Object.values(comps).filter((v) => typeof v === "number").reduce((a, b) => a + b, 0);
    const explanations = (detail.explanations || []).map((e) => `<li>${esc(e)}</li>`).join("");
    const pricedLegs = (detail.legs || []).map((l) =>
      `<tr><td class="mono">${esc(l.label || l.symbol || "")}</td><td>${esc(l.book_source || "—")}</td>` +
      `<td class="num">${fmtPrice(l.bid)}</td><td class="num">${fmtPrice(l.ask)}</td>` +
      `<td class="num">${fmtPrice(l.q_market ?? l.q ?? l.mark)}</td></tr>`).join("");
    pricingHtml = `
      <h3>Pricing decision</h3>
      <p>${decisionBadge(p.status)}${p.after_deadline ? ' <span class="badge warn">LATE</span>' : ""} <span class="dim">${esc(p.reason_code || "")} ${esc(p.reason_detail || "")}</span></p>
      ${p.after_deadline ? '<p class="caption" style="padding:0 8px 8px">Paper price computed after the exchange deadline; it could not have been submitted for this RFQ.</p>' : ""}
      <div class="kpis">${kpi("Naive", fmtPrice(p.naive))}${kpi("Fair", fmtPrice(p.fair))}${kpi("Our", fmtPrice(p.response_price), "hi")}${kpi("Market", fmtPrice(p.market_price))}</div>
      <dl class="kv" style="margin-top:0">
        <dt>Quote edge</dt><dd>${fmtEdge(p.edge_vs_market)} <span class="dim">(our price vs ${esc(p.market_source || "market")})</span></dd>
        <dt>Model edge</dt><dd>${fmtEdge(p.model_edge)} <span class="dim">(fair vs market)</span></dd>
        <dt>Action / size</dt><dd>${esc(p.response_action || "—")} ${esc(p.size || "")} ${esc(p.size_unit || "")}</dd>
        <dt>Spread</dt><dd>${compRows ? spreadTotal.toFixed(1) + " bps total" : "—"}</dd>
        <dt>Corr adjustment</dt><dd>${detail.corr_adjustment_bps != null ? Number(detail.corr_adjustment_bps).toFixed(1) + " bps" : "—"}</dd>
        <dt>Wait / compute</dt><dd>${fmtMs(p.wait_ms)} ms / ${fmtMs(p.compute_ms)} ms</dd>
        <dt>Model</dt><dd class="mono">${esc(p.model_version || "—")} · params ${esc(p.params_version || "—")}</dd>
        <dt>Priced at</dt><dd>${esc(p.priced_at || "—")}</dd>
      </dl>
      ${explanations ? `<h3>Why</h3><ul style="margin:0;padding:0 8px 8px 24px">${explanations}</ul>` : ""}
      ${compRows ? `<h3>Spread components (bps)</h3><div class="table-wrap"><table><thead><tr><th>Component</th><th class="num">bps</th></tr></thead><tbody>${compRows}</tbody></table></div>` : ""}
      ${pricedLegs ? `<h3>Priced legs</h3><div class="table-wrap"><table><thead><tr><th>Leg</th><th>Book</th><th class="num">Bid</th><th class="num">Ask</th><th class="num">Mark</th></tr></thead><tbody>${pricedLegs}</tbody></table></div>` : ""}
      <details><summary>Full decision JSON</summary><pre class="json">${esc(JSON.stringify(detail, null, 2))}</pre></details>`;
  } else {
    pricingHtml = `<h3>Pricing decision</h3><p class="caption">${s.screen === "QUOTABLE" ? "Not priced yet." : "Rejected by the screener; no quote created."}</p>`;
  }

  const game = ((p?.detail?.games || [])[0] || {}).game || ((p?.detail?.games || [])[0] || {}).label || "";
  openDrawer(`RFQ detail <span class="dim">${esc(rfqId.slice(0, 14))}</span>`, `
    <div class="dh"><span class="big">${game ? gameLink(game) + " · " : ""}${s.n_legs ?? "?"} legs · ${esc(s.side || r.side || "")} ${esc(r.qty_decimal || r.cash_order_qty || "")}</span>
      <div style="margin-top:6px">${screenBadge(s.screen, r.quotable ?? s.screen === "QUOTABLE", r.status)}
      <span class="dim">${esc(r.status || "")} · deadline ${deadlineStr(s.submission_deadline)}</span></div></div>
    ${r.quotable === false ? '<p class="caption">Session only. This RFQ is not stored and disappears when the logger restarts.</p>' : ""}
    <dl class="kv">
      <dt>Posted</dt><dd>${esc(r.created_time || "—")} (${ageStr(r.created_time)} ago)</dd>
      <dt>Requester</dt><dd>${esc(r.creator_user_id || "—")}</dd>
      <dt>Legs</dt><dd>${s.n_legs ?? "—"} known, ${s.n_nfl_legs ?? "—"} NFL</dd>
      <dt>Accepted trade</dt><dd>${fmtPrice(d.trade?.price)}${d.trade ? ` · ${esc(d.trade.size)} shares · ${esc(d.trade.executed_at || "")}` : " · none observed"}</dd>
    </dl>
    <h3>Screen checks</h3>${checkRows || '<p class="caption">none recorded</p>'}
    <h3>Legs</h3>
    <div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Side</th><th class="num">Bid</th><th class="num">Ask</th><th>Settled</th></tr></thead>
    <tbody>${legRows || '<tr><td colspan="5" class="dim">no legs</td></tr>'}</tbody></table></div>
    ${pricingHtml}
    <h3>Lifecycle</h3><div class="timeline">${events || '<p class="caption">no events</p>'}</div>
    <div class="drawer-actions">
      ${game ? `<button class="btn" data-game="${esc(game)}">Filter to game</button>` : ""}
      <button class="btn ghost" id="copy-rfq">Copy RFQ id</button>
      <button class="btn ghost" id="open-risk">Open in Risk</button>
    </div>`);
  $("copy-rfq").addEventListener("click", () => navigator.clipboard?.writeText(rfqId));
  $("open-risk").addEventListener("click", () => { if (game) setGame(game); showTab("risk"); });
}
