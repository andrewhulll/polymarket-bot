/* Inventory tab: paper positions, worst-case-loss exposure, kill switch. */
"use strict";

function invMoney(x) {
  if (x == null || Number.isNaN(Number(x))) return "—";
  const neg = Number(x) < 0;
  return (neg ? "−$" : "$") + Math.abs(Number(x)).toLocaleString("en-US",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

async function refreshInventory() {
  const [inv, risk] = await Promise.all([get("/api/inventory"), get("/api/risk")]);

  const ks = inv.kill_switch_event || {};
  const engaged = inv.kill_switch === true || ks.state === "tripped";
  $("killswitch-badge").classList.toggle("hidden", !engaged);

  $("inv-kpis").innerHTML =
    kpi("Equity", invMoney(inv.equity)) +
    kpi("Buying power", invMoney(inv.buying_power)) +
    kpi("Assumed paper net notional", invMoney(inv.paper_net_notional)) +
    kpi("Paper WCL", invMoney(inv.paper_wcl)) +
    kpi("Realized PnL", invMoney(inv.realized_pnl),
      (inv.realized_pnl || 0) < 0 ? "bad" : (inv.realized_pnl || 0) > 0 ? "good" : "") +
    kpi("Kill switch", engaged ? badge("ENGAGED", "no") : badge("off", "q"));

  const activity = inv.activity || {};
  $("inv-activity-kpis").innerHTML =
    kpi("Paper quotes", fmtInt(activity.paper_quotes || 0)) +
    kpi("Recorded fills", fmtInt(activity.recorded_fills || 0));
  setRows("inv-quotes", (activity.recent_quotes || []).map((q) =>
    `<tr><td>${shortId(q.rfq_id)}</td><td class="dim">${esc((q.created_time || "").slice(0, 19))}</td>` +
    `<td>${esc(q.state || "—")}${q.reason_code ? ` · ${esc(q.reason_code)}` : ""}</td>` +
    `<td class="num">${fmtPrice(q.buy_price)}</td>` +
    `<td class="num">${fmtPrice(q.sell_price)}</td>` +
    `<td class="num">${fmtInt(q.buy_qty_decimal)}</td>` +
    `<td class="num">${fmtInt(q.sell_qty_decimal)}</td></tr>`
  ).join("") || `<tr><td colspan="7" class="dim">No paper quotes recorded for this source.</td></tr>`);

  let alerts = "";
  if (engaged) alerts += `<div class="alert error">Kill switch ENGAGED — ` +
    `${esc(ks.trigger || "")} at ${esc(ks.ts || "")}. Paper quoting is halted.</div>`;
  if (!Object.keys(inv.exposures || {}).length && activity.paper_quotes) {
    alerts += `<div class="alert">No recorded position exposure. Paper net notional is estimated from shadow fills; expired quotes release their reservation.</div>`;
  } else if (inv.paper_wcl) {
    alerts += `<div class="alert">Paper fills are assumed when a quote is issued; an observed trade removes the fill if it beats our price. Market and team rows each receive the full combo loss, so their totals overlap.</div>`;
  }
  $("inv-alerts").innerHTML = alerts;
  $("ks-trip").disabled = engaged;
  $("ks-reset").disabled = !engaged;

  const games = inv.exposures || {}, pending = inv.pending || {},
    executed = inv.executed || {}, net = inv.net_by_game || {};
  const gameKeys = Object.keys(games).sort((a, b) => games[b] - games[a]);
  setRows("inv-games", gameKeys.map((g) =>
    `<tr><td class="mono">${esc(g)}</td>` +
    `<td class="num">${invMoney(pending[g])}</td>` +
    `<td class="num">${invMoney(executed[g])}</td>` +
    `<td class="num">${invMoney(games[g])}</td>` +
    `<td class="num">${net[g] != null ? esc(Number(net[g]).toLocaleString("en-US", {maximumFractionDigits: 2})) : "—"}</td></tr>`
  ).join("") || `<tr><td colspan="5" class="dim">no exposure</td></tr>`);

  const mk = (obj) => Object.keys(obj || {}).sort((a, b) => obj[b] - obj[a]);
  setRows("inv-markets", mk(inv.markets).map((m) =>
    `<tr><td class="mono">${esc(m)}</td><td class="num">${invMoney(inv.markets[m])}</td></tr>`
  ).join("") || `<tr><td colspan="2" class="dim">no exposure</td></tr>`);
  setRows("inv-teams", mk(inv.teams).map((t) =>
    `<tr><td class="mono">${esc(t)}</td><td class="num">${invMoney(inv.teams[t])}</td></tr>`
  ).join("") || `<tr><td colspan="2" class="dim">no exposure</td></tr>`);

  const events = [...(risk.events || []), ...(inv.paper_events || [])]
    .sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || "")))
    .slice(0, 100);
  setRows("inv-risk", events.map((e) =>
    `<tr><td class="dim">${esc((e.ts || "").slice(0, 19))}</td>` +
    `<td>${e.rfq_id ? shortId(e.rfq_id) : "—"}</td>` +
    `<td class="mono">${esc(e.game_id || "—")}</td>` +
    `<td>${esc(e.action || "—")}</td>` +
    `<td>${esc(e.reason || "")}${e.reason_detail ? `: ${esc(e.reason_detail)}` : ""}</td></tr>`
  ).join("") || `<tr><td colspan="5" class="dim">no risk or paper events</td></tr>`);
}

async function killSwitch(action) {
  const reason = ($("ks-reason") || {}).value || "";
  const btn = action === "trip" ? $("ks-trip") : $("ks-reset");
  btn.disabled = true;
  try {
    await post("/api/risk/kill-switch", { action, reason });
    $("ks-reason").value = "";
    await refreshInventory();
  } catch (err) {
    $("inv-alerts").innerHTML =
      `<div class="alert error">Kill-switch ${esc(action)} failed: ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("ks-trip").addEventListener("click", () => killSwitch("trip"));
  $("ks-reset").addEventListener("click", () => killSwitch("reset"));
});
