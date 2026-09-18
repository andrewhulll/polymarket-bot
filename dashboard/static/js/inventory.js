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
    kpi("Realized PnL", invMoney(inv.realized_pnl),
      (inv.realized_pnl || 0) < 0 ? "bad" : (inv.realized_pnl || 0) > 0 ? "good" : "") +
    kpi("Kill switch", engaged ? badge("ENGAGED", "no") : badge("off", "q"));

  let alerts = "";
  if (engaged) alerts += `<div class="alert error">Kill switch ENGAGED — ` +
    `${esc(ks.trigger || "")} at ${esc(ks.ts || "")}. Paper quoting is halted.</div>`;
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
    `<td class="num">${net[g] != null ? esc(String(net[g])) : "—"}</td></tr>`
  ).join("") || `<tr><td colspan="5" class="dim">no exposure</td></tr>`);

  const mk = (obj) => Object.keys(obj || {}).sort((a, b) => obj[b] - obj[a]);
  setRows("inv-markets", mk(inv.markets).map((m) =>
    `<tr><td class="mono">${esc(m)}</td><td class="num">${invMoney(inv.markets[m])}</td></tr>`
  ).join("") || `<tr><td colspan="2" class="dim">no exposure</td></tr>`);
  setRows("inv-teams", mk(inv.teams).map((t) =>
    `<tr><td class="mono">${esc(t)}</td><td class="num">${invMoney(inv.teams[t])}</td></tr>`
  ).join("") || `<tr><td colspan="2" class="dim">no exposure</td></tr>`);

  setRows("inv-risk", (risk.events || []).map((e) =>
    `<tr><td class="dim">${esc((e.ts || "").slice(0, 19))}</td>` +
    `<td>${e.rfq_id ? shortId(e.rfq_id) : "—"}</td>` +
    `<td>${esc(e.action || "—")}</td><td>${esc(e.reason || "")}</td></tr>`
  ).join("") || `<tr><td colspan="4" class="dim">no risk events</td></tr>`);
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
