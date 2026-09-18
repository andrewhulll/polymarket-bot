/* RFQs tab: dense feed + drill-down drawer. */
"use strict";

let screenOptionsLoaded = false;

$("only-quotable").addEventListener("change", (e) => {
  state.rfqOnly = e.target.checked; state.rfqPage = 1; refreshRfqs();
});
$("screen-filter").addEventListener("change", (e) => {
  state.rfqScreen = e.target.value; state.rfqPage = 1; refreshRfqs();
});
$("rfq-search").addEventListener("input", (e) => {
  clearTimeout($("rfq-search")._t);
  $("rfq-search")._t = setTimeout(() => {
    state.rfqSearch = e.target.value.trim(); state.rfqPage = 1; refreshRfqs();
  }, 350);
});
$("rfq-prev").addEventListener("click", () => { if (state.rfqPage > 1) { state.rfqPage--; refreshRfqs(); } });
$("rfq-next").addEventListener("click", () => { state.rfqPage++; refreshRfqs(); });

function screenBadge(screen, quotable) {
  if (quotable) return badge("QUOTABLE", "q");
  if (!screen) return badge("PENDING", "dim");
  return badge(screen, "no");
}

async function refreshRfqs() {
  if (!screenOptionsLoaded) {
    // populate screen filter from a cheap aggregate via the rfqs endpoint is overkill;
    // use fixed known screens plus whatever the first page shows.
    screenOptionsLoaded = true;
  }
  const q = new URLSearchParams({
    only_quotable: state.rfqOnly ? "1" : "0",
    page: String(state.rfqPage),
    ...(state.rfqScreen ? { screen: state.rfqScreen } : {}),
    ...(state.rfqSearch ? { search: state.rfqSearch } : {}),
  });
  const data = await get(`/api/rfqs?${q}`);
  state.rfqTotal = data.total;
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  if (state.rfqPage > pages) { state.rfqPage = pages; return refreshRfqs(); }

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

  $("rfq-page-label").textContent = `p ${data.page}/${pages}`;
  $("rfq-prev").disabled = data.page <= 1;
  $("rfq-next").disabled = data.page >= pages;
  $("rfq-count").textContent =
    `${data.total.toLocaleString()} RFQs · showing ${data.rows.length} · click a row for full detail`;

  setRows("rfq-table", data.rows.map((r) => {
    const failed = Object.values(r.filters || {}).filter((v) => v === false).length;
    return `<tr class="clickable" data-rfq="${esc(r.rfq_id)}">` +
      `<td>${shortId(r.rfq_id)}</td>` +
      `<td class="num">${ageStr(r.created_time)}</td>` +
      `<td>${screenBadge(r.screen, r.quotable)}${failed ? ` <span class="badge warn">${failed}✕</span>` : ""}</td>` +
      `<td class="num">${r.n_legs ?? "—"}${r.n_nfl_legs ? ` <span class="dim">(${r.n_nfl_legs} NFL)</span>` : ""}</td>` +
      `<td>${esc(r.side || r.direction || "—")}</td>` +
      `<td class="num">${esc(r.qty_decimal || r.cash_order_qty || "—")}</td>` +
      `<td>${deadlineStr(r.submission_deadline)}</td>` +
      `<td>${esc(r.status || "—")}</td>` +
      `<td class="num" title="${esc(r.trade_executed_at || "No confirmed trade observed")}">${fmtPrice(r.trade_price)}</td>` +
      `<td class="dim">${esc((r.created_time || "").slice(11, 19))}</td></tr>`;
  }).join(""));
  document.querySelectorAll("#rfq-table tbody tr").forEach((tr) =>
    tr.addEventListener("click", () => openRfqDrawer(tr.dataset.rfq)));
}

async function openRfqDrawer(rfqId) {
  openDrawer(shortId(rfqId), `<p class="caption">loading…</p>`);
  const d = await get(`/api/rfqs/${encodeURIComponent(rfqId)}`);
  const r = d.rfq || {}, s = d.screen || {};
  const checks = s.checks || s.filters || {};
  const checkRows = Object.entries(checks).map(([k, v]) =>
    `<div class="check"><span>${v ? "✅" : "❌"}</span><span>${esc(k)}</span></div>`).join("");

  const legRows = (d.legs || []).map((l) =>
    `<tr><td class="mono">${esc(l.symbol)}</td><td>${esc(l.side || "—")}</td>` +
    `<td class="num">${fmtPrice(l.bid)}</td><td class="num">${fmtPrice(l.ask)}</td>` +
    `<td>${l.settlement_price != null ? (l.settlement_price >= 0.5 ? "won" : "lost") : "—"}</td></tr>`).join("");

  const events = (d.events || []).map((e) =>
    `<div class="ev"><div>${esc(e.event_type)}${e.client_derived ? ' <span class="badge dim">derived</span>' : ""}</div>` +
    `<div class="t">${esc(e.recorded_at || "")} · ${esc(e.source || "")}</div></div>`).join("");

  const p = d.pricing;
  const pricingHtml = p
    ? `<dl class="kv">
        <dt>Decision</dt><dd>${esc(p.status)} <span class="dim">${esc(p.reason_code || "")}</span></dd>
        <dt>Our price</dt><dd>${fmtPrice(p.response_price)} ${esc(p.response_action || "")}</dd>
        <dt>Model fair</dt><dd>${fmtPrice(p.fair)}</dd>
        <dt>Naive</dt><dd>${fmtPrice(p.naive)}</dd>
        <dt>Size</dt><dd>${esc(p.size || "")} ${esc(p.size_unit || "")}</dd>
        <dt>Priced at</dt><dd>${esc(p.priced_at || "—")}</dd>
      </dl>
      <p><a href="#" onclick="event.preventDefault();openPricingDrawer('${esc(rfqId)}')">Open full pricing detail →</a></p>`
    : `<p class="caption">Not priced yet.</p>`;

  openDrawer(shortId(rfqId), `
    <p>${screenBadge(s.screen, r.quotable ?? s.screen === "QUOTABLE")}
       <span class="dim">${esc(r.status || "")} · ${esc(r.symbol || "")}</span></p>
    <dl class="kv">
      <dt>Posted</dt><dd>${esc(r.created_time || "—")} (${ageStr(r.created_time)} ago)</dd>
      <dt>Updated</dt><dd>${esc(r.updated_time || "—")}</dd>
      <dt>Size</dt><dd>${esc(r.qty_decimal || r.cash_order_qty || "—")}</dd>
      <dt>Side</dt><dd>${esc(s.side || r.side || "—")} ${esc(s.direction || "")}</dd>
      <dt>Deadline</dt><dd>${deadlineStr(s.submission_deadline)}</dd>
      <dt>Legs</dt><dd>${s.n_legs ?? "—"} known, ${s.n_nfl_legs ?? "—"} NFL</dd>
      <dt>Accepted trade</dt><dd>${fmtPrice(d.trade?.price)}${d.trade ? ` · ${esc(d.trade.size)} shares · ${esc(d.trade.executed_at || "")}` : " · none observed"}</dd>
    </dl>
    <h3>Screen checks</h3>${checkRows || '<p class="caption">none recorded</p>'}
    <h3>Legs</h3>
    <div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Side</th><th class="num">Bid</th><th class="num">Ask</th><th>Settled</th></tr></thead>
    <tbody>${legRows || '<tr><td colspan="5" class="dim">no legs</td></tr>'}</tbody></table></div>
    <h3>Pricing decision</h3>${pricingHtml}
    <h3>Lifecycle</h3><div class="timeline">${events || '<p class="caption">no events</p>'}</div>
  `);
}
