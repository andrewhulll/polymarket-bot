/* Risk tab: paper positions, worst-case-loss exposure, risk events.
   The kill switch itself lives in the header (core.js). */
"use strict";

function invMoney(x) {
  if (x == null || Number.isNaN(Number(x))) return "—";
  const neg = Number(x) < 0;
  return (neg ? "−$" : "$") + Math.abs(Number(x)).toLocaleString("en-US",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
const invMoney0 = (x) => x == null ? "—" : fmtMoney0(x).replace("-", "−");

async function refreshRisk() {
  const [inv, risk, exposure] = await Promise.all([
    get("/api/inventory"), get("/api/risk"), get("/api/exposure")
  ]);

  renderKillSwitch(risk.kill_switch || inv.kill_switch_event);
  const ks = inv.kill_switch_event || risk.kill_switch || {};
  const engaged = killTripped(ks) || inv.kill_switch === true;

  const games = inv.exposures || {}, pending = inv.pending || {},
    executed = inv.executed || {}, net = inv.net_by_game || {};
  const gameKeys = Object.keys(games).sort((a, b) => games[b] - games[a]);
  const largest = gameKeys[0];
  const activity = inv.activity || {};
  const wclPct = inv.paper_wcl != null && inv.equity ? inv.paper_wcl / inv.equity : null;

  setTabKpis(
    kpi("Equity", invMoney0(inv.equity)) +
    kpi("Buying power", invMoney0(inv.buying_power)) +
    kpi("Paper WCL", invMoney0(inv.paper_wcl), wclPct > 0.8 ? "bad" : "", wclPct == null ? "" : fmtPct(wclPct, 0) + " of equity") +
    kpi("Net notional", invMoney0(inv.paper_net_notional)) +
    kpi("Realized P&L", invMoney0(inv.realized_pnl), (inv.realized_pnl || 0) < 0 ? "bad" : (inv.realized_pnl || 0) > 0 ? "good" : "") +
    kpi("Largest game", largest ? esc(largest) : "—", "", largest ? invMoney0(games[largest]) : "") +
    kpi("Paper quotes", fmtInt(activity.paper_quotes || 0)) +
    kpi("Recorded fills", fmtInt(activity.recorded_fills || 0)));

  let alerts = "";
  if (engaged) alerts += `<div class="alert error">Kill switch TRIPPED — ${esc(ks.trigger || "")} at ${esc(ks.ts || "")}. Paper quoting is halted.</div>`;
  if (!Object.keys(inv.exposures || {}).length && activity.paper_quotes) {
    alerts += `<div class="alert info">No recorded position exposure. Paper net notional is estimated from shadow fills; expired quotes release their reservation.</div>`;
  } else if (inv.paper_wcl) {
    alerts += `<div class="alert info">Paper fills are assumed when a quote is issued; an observed trade removes the fill if it beats our price. Market and team rows each receive the full combo loss, so their totals overlap.</div>`;
  }
  $("inv-alerts").innerHTML = alerts;

  setRows("inv-games", gameKeys.map((g) => {
    const on = state.game && g.includes(state.game);
    return `<tr class="${on ? "selected" : ""}"><td>${gameLink(g)}</td>` +
      `<td class="num">${invMoney0(pending[g])}</td>` +
      `<td class="num">${invMoney0(executed[g])}</td>` +
      `<td class="num w">${invMoney0(games[g])}</td>` +
      `<td class="num ${(net[g] || 0) >= 0 ? "pos" : "neg"}">${net[g] != null ? esc(Number(net[g]).toLocaleString("en-US", { maximumFractionDigits: 0 })) : "—"}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="dim" style="padding:10px">no exposure</td></tr>`);

  const series = exposure.series || [];
  const cap = inv.equity;
  if (series.length) {
    linesChart("chart-wcl", [
      { values: series.map((p) => p.pending_wcl ?? 0), color: "#9a9a9a", dash: "3 3" },
      { values: series.map((p) => p.executed_wcl ?? 0), color: "#ff9f1c" },
      { values: series.map((p) => p.total_wcl ?? 0), color: "#f2f2f2", fill: true },
    ], { ref: cap && cap <= Math.max(...series.map((p) => p.total_wcl ?? 0)) * 1.5 ? cap : null, lo: 0 });
    const last = series[series.length - 1];
    setFoot("wcl-foot", [
      `<span class="lg"><i style="background:#f2f2f2"></i>total ${invMoney0(last.total_wcl)}</span>`,
      `<span class="lg"><i style="background:#ff9f1c"></i>executed ${invMoney0(last.executed_wcl)}</span>`,
      `<span class="lg"><i style="background:#9a9a9a"></i>pending ${invMoney0(last.pending_wcl)}</span>`,
      cap ? `<span class="lg"><i style="background:#ff4d4d"></i>equity ${invMoney0(cap)}</span>` : ""]);
  } else {
    chartEmpty("chart-wcl", "no exposure snapshots yet"); setFoot("wcl-foot", []);
  }

  const toBars = (obj) => Object.keys(obj || {}).sort((a, b) => obj[b] - obj[a]).map((k) => ({
    label: k, value: obj[k], text: invMoney0(obj[k]),
  }));
  barList("risk-markets", toBars(inv.markets), "no exposure");
  barList("risk-teams", toBars(inv.teams).map((r) => ({ ...r, game: null })), "no exposure");

  const events = [...(risk.events || []), ...(inv.paper_events || [])]
    .filter((e) => !state.game || gameMatches(e.game_id))
    .sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || "")))
    .slice(0, 100);
  setRows("inv-risk", events.map((e) =>
    `<tr><td class="dim">${esc((e.ts || "").slice(11, 19))}</td>` +
    `<td>${e.rfq_id ? shortId(e.rfq_id) : "—"}</td>` +
    `<td>${gameLink(e.game_id)}</td>` +
    `<td class="${/block|reject|decline|trip/i.test(e.action || "") ? "neg" : /reduce|cap/i.test(e.action || "") ? "hi" : ""}">${esc(e.action || "—")}</td>` +
    `<td class="dim">${esc(e.reason || "")}${e.reason_detail ? `: ${esc(e.reason_detail)}` : ""}</td></tr>`
  ).join("") || `<tr><td colspan="5" class="dim" style="padding:10px">no risk or paper events${state.game ? ` for ${esc(state.game)}` : ""}</td></tr>`);
}
