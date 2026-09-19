/* Performance tab: KPIs, shadow-fill ledger with expandable rows, curves, breakdowns. */
"use strict";

$("fills-prev").addEventListener("click", () => { if (state.fillsPage > 1) { state.fillsPage--; refreshFills(); } });
$("fills-next").addEventListener("click", () => { state.fillsPage++; refreshFills(); });

async function refreshPerformance() {
  const [perf, fills, inv, risk] = await Promise.all([
    get("/api/performance"), get(`/api/fills?page=${state.fillsPage}`),
    get("/api/inventory").catch(() => null), get("/api/risk").catch(() => null),
  ]);

  $("perf-kpis").innerHTML =
    kpi("Quoted", fmtInt(perf.quoted)) +
    kpi("Shadow fills", fmtInt(perf.shadow_fills)) +
    kpi("Expected P&L", fmtMoney(perf.expected_pnl), perf.expected_pnl >= 0 ? "good" : "bad") +
    kpi("Realized P&L", fmtMoney(perf.realized_pnl), perf.realized_pnl >= 0 ? "good" : "bad") +
    kpi("Max downswing", fmtMoney(perf.max_downswing), "bad") +
    kpi("Max upswing", fmtMoney(perf.max_upswing), "good") +
    kpi("Assumed paper net notional", fmtMoney(perf.net_notional)) +
    kpi("Equity", inv ? fmtMoney(inv.equity) : "—");

  $("killswitch-badge").classList.toggle("hidden",
    !(risk && risk.kill_switch && risk.kill_switch.state === "engaged"));

  refreshFillsTable(fills);

  if (perf.curve && perf.curve.length) {
    lineChart("chart-realized", perf.curve, "realized_pnl", { color: "#3ddc84", fill: false });
    lineChart("chart-equity", perf.curve, "net_notional");
  } else {
    $("chart-equity").innerHTML = `<text x="300" y="90" text-anchor="middle">no data</text>`;
    $("chart-realized").innerHTML = `<text x="300" y="90" text-anchor="middle">no data</text>`;
  }

  const mkTable = (title, rows, key) => {
    if (!rows || !rows.length) return "";
    const body = rows.map((r) =>
      `<tr><td>${esc(r[key] ?? "—")}</td><td class="num">${fmtInt(r.shadow_fills)}</td>` +
      `<td class="num"><span class="${(r.expected_pnl ?? 0) >= 0 ? "pos" : "neg"}">${fmtMoney(r.expected_pnl)}</span></td>` +
      `<td class="num"><span class="${(r.realized_pnl ?? 0) >= 0 ? "pos" : "neg"}">${fmtMoney(r.realized_pnl)}</span></td></tr>`
    ).join("");
    return `<div class="chart"><h3>${esc(title)}</h3>
      <div class="table-wrap" style="max-height:none"><table>
      <thead><tr><th></th><th class="num">n</th><th class="num">Expected</th><th class="num">Realized</th></tr></thead><tbody>${body}</tbody>
      </table></div></div>`;
  };
  $("perf-breakdowns").innerHTML =
    mkTable("By family", perf.by_family, "family") +
    mkTable("By leg count", perf.by_legs, "n_legs") +
    mkTable("By game", (perf.by_game || []).slice(0, 12), "game") +
    mkTable("By market source", perf.by_market_source, "market_source");
}

async function refreshFills() {
  const fills = await get(`/api/fills?page=${state.fillsPage}`);
  refreshFillsTable(fills);
}

function refreshFillsTable(fills) {
  const pages = Math.max(1, Math.ceil(fills.total / fills.page_size));
  if (state.fillsPage > pages) { state.fillsPage = pages; return refreshFills(); }
  $("fills-page-label").textContent = `p ${fills.page}/${pages}`;
  $("fills-prev").disabled = fills.page <= 1;
  $("fills-next").disabled = fills.page >= pages;

  setRows("fills-table", fills.rows.map((r, i) =>
    `<tr class="clickable" data-i="${i}" data-rfq="${esc(r.rfq_id)}">` +
    `<td class="expander">▸</td>` +
    `<td class="dim">${esc((r.time || "").slice(11, 19))}${r.after_deadline ? ' <span class="badge warn" title="Quoted after the submission deadline">LATE</span>' : ""}</td>` +
    `<td>${shortId(r.rfq_id)}</td>` +
    `<td class="dim">${esc(r.game || "—")}</td>` +
    `<td>${esc(r.family || "—")}</td>` +
    `<td>${esc(r.response_action || r.side || "—")}</td>` +
    `<td class="num">${esc(r.size || "")}${r.capacity_limited ? ' <span class="badge warn" title="Paper fill reduced to stay within equity">CAPPED</span>' : ""}</td>` +
    `<td class="num">${fmtPrice(r.naive)}</td>` +
    `<td class="num">${fmtPrice(r.fair)}</td>` +
    `<td class="num"><b>${fmtPrice(r.our_price)}</b></td>` +
    `<td class="num">${fmtPrice(r.market_price)}${r.market_source ? ` <span class="dim" title="${esc(r.market_source)}">◉</span>` : ""}</td>` +
    `<td class="num">${fmtEdge(r.quote_edge)}</td>` +
    `<td class="num"><span class="${(r.expected_pnl ?? 0) >= 0 ? "pos" : "neg"}">${fmtMoney(r.expected_pnl)}</span></td>` +
    `<td class="num"><span class="${(r.realized_pnl ?? 0) >= 0 ? "pos" : "neg"}">${fmtMoney(r.realized_pnl)}</span></td>` +
    `<td class="dim">${r.settled_legs ?? "?"}/${r.total_legs ?? "?"}</td></tr>` +
    `<tr class="fill-detail hidden" id="fill-det-${i}"><td colspan="16"></td></tr>`
  ).join(""));
  fills._rows = fills.rows;
  document.querySelectorAll("#fills-table tbody tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => toggleFillDetail(tr, fills._rows[Number(tr.dataset.i)])));
}

function toggleFillDetail(tr, r) {
  const det = document.getElementById("fill-det-" + tr.dataset.i);
  const open = det.classList.toggle("hidden");
  tr.querySelector(".expander").textContent = open ? "▸" : "▾";
  if (open) return;
  det.querySelector("td").innerHTML = `
    <div class="kpis">
      ${kpi("Naive", fmtPrice(r.naive))}
      ${kpi("Model fair", fmtPrice(r.fair))}
      ${kpi("Our price", fmtPrice(r.our_price))}
      ${kpi("Market", fmtPrice(r.market_price))}
    </div>
    <dl class="kv">
      <dt>Quote edge</dt><dd>${fmtEdge(r.quote_edge)} <span class="dim">(our edge vs ${esc(r.market_source || "market")})</span></dd>
      <dt>Model edge</dt><dd>${fmtEdge(r.model_edge)} <span class="dim">(fair vs market)</span></dd>
      <dt>Expected P&amp;L</dt><dd>${fmtMoney(r.expected_pnl)}</dd>
      <dt>Realized P&amp;L</dt><dd>${fmtMoney(r.realized_pnl)}</dd>
      <dt>Net notional</dt><dd>${fmtMoney(r.net_notional)}</dd>
      <dt>Settlement</dt><dd>${r.settled_legs ?? "?"}/${r.total_legs ?? "?"} legs settled</dd>
      <dt>Size</dt><dd>${esc(r.size || "")} ${esc(r.size_unit || "")}</dd>
      ${r.capacity_limited ? `<dt>Original size</dt><dd>${esc(r.original_size)} ${esc(r.size_unit || "")} — reduced to stay within equity</dd>` : ""}
    </dl>
    <p><a href="#" onclick="event.preventDefault();openPricingDrawer('${esc(r.rfq_id)}')">Open full decision detail →</a></p>`;
}
