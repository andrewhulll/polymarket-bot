/* Engine tab: health, latency, decline reasons, risk events, draft quotes. */
"use strict";

async function refreshEngine() {
  const [eng, hist, risk] = await Promise.all([
    get("/api/engine"), get("/api/latency/histogram"), get("/api/risk"),
  ]);

  const ks = eng.kill_switch;
  $("killswitch-badge").classList.toggle("hidden", !(ks && ks.state === "tripped"));

  const h = eng.health || {};
  const hbAge = h.heartbeat_at ? (Date.now() - new Date(h.heartbeat_at).getTime()) / 1000 : null;
  const stale = hbAge != null && hbAge > 120;
  const uptime = h.started_at ? ageStr(h.started_at) : "—";

  $("engine-kpis").innerHTML =
    kpi("Uptime", esc(uptime)) +
    kpi("Messages", fmtInt(h.messages_processed)) +
    kpi("Errors", fmtInt(h.errors), h.errors ? "bad" : "") +
    kpi("Gateway", h.gateway_connected ? badge("CONNECTED", "q") : badge("DOWN", "no")) +
    kpi("Heartbeat age", hbAge != null ? Math.round(hbAge) + "s" : "—", stale ? "bad" : "") +
    kpi("Buffer drops", fmtInt(h.buffer_drops), h.buffer_drops ? "bad" : "") +
    kpi("Latency samples", fmtInt(eng.samples));

  let alerts = "";
  if (stale) alerts += `<div class="alert error">Stale heartbeat — last seen ${esc(h.heartbeat_at || "never")}. The capture process may be down.</div>`;
  if (ks && ks.state === "tripped") alerts += `<div class="alert error">Kill switch ENGAGED — ${esc(ks.trigger || "")} at ${esc(ks.ts || "")}.</div>`;
  $("engine-alerts").innerHTML = alerts;

  const budget = eng.budget_ms;
  $("latency-budget").textContent = `budget ${budget} ms (red buckets are over budget)`;
  histogram("hist-wait", hist.wait, budget);
  histogram("hist-compute", hist.compute, budget);

  const dist = (d) => d || {};
  const w = dist(eng.wait), c = dist(eng.compute);
  setRows("latency-table",
    `<tr><td>Wait (posted → engine)</td><td class="num">${fmtMs(w.p50)}</td><td class="num">${fmtMs(w.p95)}</td><td class="num">${fmtMs(w.max)}</td></tr>` +
    `<tr><td>Compute (engine → decision)</td><td class="num">${fmtMs(c.p50)}</td><td class="num">${fmtMs(c.p95)}</td><td class="num">${fmtMs(c.max)}</td></tr>`);

  const reasons = eng.reasons || [];
  const total = reasons.reduce((a, r) => a + (r.n || 0), 0) || 1;
  setRows("reasons-table", reasons.map((r) =>
    `<tr><td>${esc(r.reason_code || "—")}</td><td class="num">${fmtInt(r.n)}</td>` +
    `<td class="num">${(100 * r.n / total).toFixed(1)}%</td>` +
    `<td><div class="sharebar"><i style="width:${(100 * r.n / total).toFixed(1)}%"></i></div></td></tr>`
  ).join("") || `<tr><td colspan="4" class="dim">no declines recorded</td></tr>`);

  setRows("risk-table", (risk.events || []).map((e) =>
    `<tr><td class="dim">${esc((e.ts || "").slice(0, 19))}</td>` +
    `<td>${e.rfq_id ? shortId(e.rfq_id) : "—"}</td>` +
    `<td>${esc(e.action || "—")}</td><td>${esc(e.reason || "")}</td></tr>`
  ).join("") || `<tr><td colspan="4" class="dim">no risk events</td></tr>`);

  setRows("drafts-table", (eng.drafts || []).map((d) =>
    `<tr><td class="mono">${esc(d.quote_id || "")}</td><td>${shortId(d.rfq_id)}</td>` +
    `<td class="num">${fmtPrice(d.buy_price)}</td><td class="num">${fmtPrice(d.sell_price)}</td>` +
    `<td class="num">${esc(d.buy_qty_decimal || d.sell_qty_decimal || "")}</td>` +
    `<td class="dim">${esc((d.created_time || "").slice(0, 19))}</td></tr>`
  ).join("") || `<tr><td colspan="6" class="dim">no stored drafts</td></tr>`);
}
