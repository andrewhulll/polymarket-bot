/* Engine tab: system health only — latency, decline reasons, feed health, draft quotes.
   Risk events live on the Risk tab; correlation-model lift lives on Research. */
"use strict";

async function refreshEngine() {
  const [eng, hist, flow] = await Promise.all([
    get("/api/engine"), get("/api/latency/histogram"),
    get("/api/flow/summary").catch(() => null),
  ]);

  renderKillSwitch(eng.kill_switch);
  const ks = eng.kill_switch;
  const h = eng.health || {};
  const hbAge = h.heartbeat_at ? (Date.now() - new Date(h.heartbeat_at).getTime()) / 1000 : null;
  const stale = hbAge != null && hbAge > 120;
  const uptime = h.started_at ? ageStr(h.started_at) : "—";
  const budget = eng.budget_ms;
  const total = (eng.wait?.p95 ?? 0) + (eng.compute?.p95 ?? 0);

  setTabKpis(
    kpi("Uptime", esc(uptime)) +
    kpi("Gateway", h.gateway_connected ? badge("CONNECTED", "good") : badge("DOWN", "no")) +
    kpi("Heartbeat age", hbAge != null ? Math.round(hbAge) + "s" : "—", stale ? "bad" : "") +
    kpi("Messages", fmtInt(h.messages_processed)) +
    kpi("Errors", fmtInt(h.errors), h.errors ? "bad" : "") +
    kpi("Buffer drops", fmtInt(h.buffer_drops), h.buffer_drops ? "bad" : "") +
    kpi("p95 wait + compute", eng.wait?.p95 == null ? "—" : `${total.toFixed(0)} ms`, total > budget ? "bad" : "good", `budget ${budget} ms`) +
    kpi("Latency samples", fmtInt(eng.samples)));

  let alerts = "";
  if (stale) alerts += `<div class="alert error">Stale heartbeat — last seen ${esc(h.heartbeat_at || "never")}. The capture process may be down.</div>`;
  else if (h.gateway_connected === false) alerts += `<div class="alert error">Gateway is disconnected. No new RFQs will arrive until it reconnects.</div>`;
  if (ks && killTripped(ks)) alerts += `<div class="alert warn">Kill switch is TRIPPED — ${esc(ks.trigger || "")} at ${esc(ks.ts || "")}. Reset it from the header.</div>`;
  if (!alerts) alerts = `<div class="alert good">Pipeline healthy · heartbeat ${hbAge != null ? Math.round(hbAge) + "s ago" : "n/a"} · ${h.buffer_drops ? fmtInt(h.buffer_drops) + " buffer drops" : "no buffer drops"} · p95 ${eng.wait?.p95 == null ? "—" : total.toFixed(0) + " ms"} against a ${budget} ms budget</div>`;
  $("engine-alerts").innerHTML = alerts;

  const w = eng.wait || {}, c = eng.compute || {};
  $("latency-budget").textContent = `budget ${budget} ms`;
  $("wait-meta").textContent = w.p95 == null ? "" : `p95 ${fmtMs(w.p95)} ms`;
  histogram("hist-wait", hist.wait, budget, "#f2f2f2");
  histogram("hist-compute", hist.compute, budget, "#ff9f1c");
  setFoot("wait-foot", [`p50 ${fmtMs(w.p50)} ms`, `p95 ${fmtMs(w.p95)} ms`, `max ${fmtMs(w.max)} ms`]);
  setFoot("compute-foot", [`p50 ${fmtMs(c.p50)} ms`, `p95 ${fmtMs(c.p95)} ms`,
    `<span class="${c.max > budget ? "neg" : ""}">max ${fmtMs(c.max)} ms</span>`]);

  const dist = (d) => d || {};
  const row = (label, d, cls) =>
    `<tr class="${cls || ""}"><td class="${cls ? "dim" : "w"}">${label}</td><td class="num">${fmtMs(d.p50)}</td>` +
    `<td class="num">${fmtMs(d.p95)}</td><td class="num ${d.max > budget ? "neg" : ""}">${fmtMs(d.max)}</td></tr>`;
  setRows("latency-table",
    row("Wait (posted → engine)", dist(eng.wait)) +
    row("&nbsp;&nbsp;delivery (network + clock)", dist(eng.delivery), "dim") +
    row("&nbsp;&nbsp;queue (our backlog)", dist(eng.queue), "dim") +
    row("Compute (engine → decision)", dist(eng.compute)) +
    row("&nbsp;&nbsp;book fetch (network)", dist(eng.fetch), "dim") +
    row("&nbsp;&nbsp;solve (model)", dist(eng.solve), "dim"));

  const reasons = eng.reasons || [];
  const sum = reasons.reduce((a, r) => a + (r.n || 0), 0) || 1;
  $("reasons-meta").textContent = `${fmtInt(sum === 1 && !reasons.length ? 0 : sum)} declined or skipped`;
  barList("reasons-list", reasons.map((r) => ({
    label: r.reason_code || "—", value: r.n || 0,
    text: `${fmtInt(r.n)} <span class="dim">· ${(100 * r.n / sum).toFixed(1)}%</span>`,
  })), "no declines recorded");

  if (flow) {
    const arrivals = flow.arrivals.map((a) => a.n);
    barsChart("feed-chart", arrivals, { color: "#f2f2f2", titles: flow.arrivals.map((a) => `${a.minute.slice(11)}: ${a.n}`) });
    const full = arrivals.slice(0, -1);   // the newest minute is still filling
    const mean = full.length ? full.reduce((a, b) => a + b, 0) / full.length : 0;
    setFoot("feed-foot", [`last full min ${fmtInt(full[full.length - 1] ?? 0)}`, `avg ${fmtInt(Math.round(mean))} / min`,
      `heartbeat ${hbAge != null ? Math.round(hbAge) + "s" : "—"}`, `drops ${fmtInt(h.buffer_drops ?? 0)}`]);
  } else { chartEmpty("feed-chart", "feed summary unavailable"); setFoot("feed-foot", []); }

  setRows("drafts-table", (eng.drafts || []).map((d) =>
    `<tr><td class="mono">${esc(d.quote_id || "")}</td><td>${shortId(d.rfq_id)}</td>` +
    `<td class="num">${fmtPrice(d.buy_price)}</td><td class="num">${fmtPrice(d.sell_price)}</td>` +
    `<td class="num">${esc(d.buy_qty_decimal || d.sell_qty_decimal || "")}</td>` +
    `<td class="dim">${esc((d.created_time || "").slice(11, 19))}</td></tr>`
  ).join("") || `<tr><td colspan="6" class="dim" style="padding:10px">no stored drafts</td></tr>`);
}
