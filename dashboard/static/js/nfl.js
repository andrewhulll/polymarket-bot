/* NFL correlation tab: filter panel + vega charts + tables + combo explorer + research runners. */
"use strict";

const NFL_VIEWS = ["overview", "combo_pricing", "calibration", "structure", "sensitivity", "explorer", "params"];
let nflMeta = null;
let nflGames = null;

function nflViewLabel(v) {
  return { overview: "Overview", combo_pricing: "Combo pricing", calibration: "Calibration",
    structure: "Correlation structure", sensitivity: "Sensitivity & P&L",
    explorer: "Combo explorer", params: "Params & data" }[v] || v;
}

function nflMetric(m) {
  const delta = m.delta ? ` <span class="dim">${esc(m.delta)}</span>` : "";
  return `<div class="kpi" ${m.help ? `title="${esc(m.help)}"` : ""}>` +
    `<div class="k">${esc(m.label)}</div><div class="v">${esc(m.value)}${delta}</div></div>`;
}

async function refreshNfl() {
  if (!nflMeta) {
    const meta = await get("/api/nfl/meta");
    if (!meta.has_results) {
      $("nfl-charts").innerHTML =
        `<div class="alert warn">No backtest results found. Point the research pipeline at data and re-run, or use Refresh params / Run backtest once results exist.</div>`;
      return;
    }
    nflMeta = meta;
    $("nfl-subtabs").innerHTML = NFL_VIEWS.map((v) =>
      `<button data-view="${v}" class="${v === state.nflView ? "active" : ""}">${nflViewLabel(v)}</button>`).join("");
    document.querySelectorAll("#nfl-subtabs button").forEach((b) =>
      b.addEventListener("click", () => {
        state.nflView = b.dataset.view;
        document.querySelectorAll("#nfl-subtabs button").forEach((x) =>
          x.classList.toggle("active", x === b));
        buildNflFilters();
        refreshNflView();
      }));
  }
  buildNflFilters();
  await refreshNflView();
}

function multiSelect(key, label, values, selected) {
  const opts = (values || []).map((v) =>
    `<option value="${esc(v)}" ${selected.includes(String(v)) ? "selected" : ""}>${esc(v)}</option>`).join("");
  return `<label>${esc(label)}<select multiple data-filter="${esc(key)}" size="${Math.min(6, Math.max(2, (values || []).length))}">${opts}</select></label>`;
}
function singleSelect(key, label, values, selected) {
  const opts = (values || []).map((v) =>
    `<option value="${esc(v)}" ${String(v) === String(selected) ? "selected" : ""}>${esc(v)}</option>`).join("");
  return `<label>${esc(label)}<select data-filter="${esc(key)}">${opts}</select></label>`;
}

function readNflFilters() {
  const out = {};
  document.querySelectorAll("#nfl-filters [data-filter]").forEach((el) => {
    if (el.multiple) {
      const sel = [...el.selectedOptions].map((o) => o.value);
      out[el.dataset.filter] = sel.join(",");
    } else if (el.type === "checkbox") {
      out[el.dataset.filter] = el.checked ? "1" : "0";
    } else if (el.value) {
      out[el.dataset.filter] = el.value;
    }
  });
  state.nflFilters = out;
  return out;
}

function buildNflFilters() {
  const o = (nflMeta && nflMeta.filter_options) || {};
  const v = state.nflView;
  const saved = state.nflFilters || {};
  const one = (key, fallback) => saved[key] ?? fallback;
  const many = (key, fallback) => saved[key] ? String(saved[key]).split(",") : fallback;
  if (v === "explorer") { $("nfl-filters").innerHTML = ""; return; }
  let html =
    singleSelect("season_min", "Season from", o.seasons, one("season_min", o.season_default && o.season_default[0])) +
    singleSelect("season_max", "Season to", o.seasons, one("season_max", o.season_default && o.season_default[1])) +
    multiSelect("families", "Families", o.families, many("families", o.families || [])) +
    multiSelect("buckets", "Spread buckets", o.buckets, many("buckets", o.buckets || [])) +
    multiSelect("game_types", "Games", o.game_types, many("game_types", o.game_types || [])) +
    singleSelect("primary", "Model", o.models, one("primary", o.primary_model)) +
    singleSelect("sample", "Sample", o.samples, one("sample", o.sample_default)) +
    `<label class="toggle" style="flex-direction:row"><input type="checkbox" data-filter="include_nested" ${one("include_nested", "1") === "0" ? "" : "checked"}> include nested</label>`;
  if (v === "combo_pricing") html += singleSelect("heat_metric", "Heatmap metric", o.heatmap_metrics, o.heatmap_metrics && o.heatmap_metrics[0]);
  if (v === "calibration") html += singleSelect("width", "Bin width", o.cal_widths, one("width", 0.05)) +
    singleSelect("combo", "Combo", state.nflCalibrationOptions || ["All filtered"], one("combo", "All filtered"));
  if (v === "structure") html += singleSelect("param", "Parameter", o.param_history_params, "sigma_at_mean_points");
  if (v === "sensitivity") html +=
    `<label>Families (comma-sep)<input type="search" data-filter="sens_families" style="width:220px" value="${esc((o.sensitivity_families_default || []).join(", "))}"></label>` +
    `<label>Edge thr<input type="search" data-filter="thr" style="width:80px" value="${esc(o.edge_threshold_default ?? 0.01)}"></label>`;
  if (v === "params") {
    const chosen = one("params_file", (o.params_files || []).slice(-1)[0]);
    html += singleSelect("params_file", "Params file", o.params_files, chosen) +
      (chosen ? `<a class="button-link" href="/api/nfl/params/download?file=${encodeURIComponent(chosen)}" download="${esc(chosen)}">Download params JSON</a>` : "");
  }
  $("nfl-filters").innerHTML = html;
  document.querySelectorAll("#nfl-filters [data-filter]").forEach((el) =>
    el.addEventListener("change", refreshNflView));
}

async function refreshNflView() {
  if (state.nflView === "explorer") return renderExplorer();
  const q = new URLSearchParams(readNflFilters());
  $("nfl-charts").innerHTML = `<p class="caption">loading…</p>`;
  $("nfl-tables").innerHTML = "";
  const data = await get(`/api/nfl/${state.nflView}?${q}`);
  if (!data.has_results) {
    $("nfl-charts").innerHTML = `<p class="caption">no results for these filters</p>`;
    return;
  }
  const box = $("nfl-charts");
  box.innerHTML = "";
  $("nfl-tables").innerHTML = "";
  if (state.nflView === "calibration" && data.combo_options) {
    state.nflCalibrationOptions = data.combo_options;
    buildNflFilters();
  }
  if (state.nflView === "params") buildNflFilters();
  renderNflPayloadInto(data, box);
}

/* ---- combo explorer ---- */
function explorerLegRow() {
  const div = document.createElement("div");
  div.className = "toolbar";
  div.innerHTML =
    `<select class="leg-kind"><option value="ml">ML</option><option value="spread">Spread</option><option value="total">Total</option></select>` +
    `<select class="leg-side"><option value="team">team</option><option value="opp">opp</option></select>` +
    `<button class="leg-remove">✕</button>`;
  const kind = div.querySelector(".leg-kind"), side = div.querySelector(".leg-side");
  kind.addEventListener("change", () => {
    side.innerHTML = kind.value === "total"
      ? `<option value="over">over</option><option value="under">under</option>`
      : `<option value="team">team</option><option value="opp">opp</option>`;
  });
  div.querySelector(".leg-remove").addEventListener("click", () => div.remove());
  return div;
}

async function renderExplorer() {
  $("nfl-notes").innerHTML = "";
  $("nfl-tables").innerHTML = "";
  if (!nflGames) nflGames = await get("/api/nfl/games").catch(() => ({ games: [] }));
  const o = (nflMeta && nflMeta.filter_options) || {};
  const games = nflGames.games || [];
  const box = $("nfl-charts");
  box.innerHTML = `
    <div class="chart"><h3>Price a same-game combo</h3>
      <div class="filterbar">
        <label>Game<select id="exp-game">
          <option value="">hypothetical…</option>
          ${games.map((g) => `<option value="${esc(g.game_id)}">${esc(g.label)}</option>`).join("")}
        </select></label>
        <label>Home<input type="search" id="exp-home" style="width:70px" value="KC"></label>
        <label>Away<input type="search" id="exp-away" style="width:70px" value="BUF"></label>
        <label>Spread (home)<input type="search" id="exp-spread" style="width:70px" value="-3.5"></label>
        <label>Total<input type="search" id="exp-total" style="width:70px" value="47.5"></label>
        <label>Team<select id="exp-team"></select></label>
        <label>Model<select id="exp-model">${(o.models || []).map((m) => `<option>${esc(m)}</option>`).join("")}</select></label>
        <label>Params<select id="exp-params">${(o.params_files || []).map((p, i, a) => `<option ${i === a.length - 1 ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></label>
        <label>Corr scale<input type="search" id="exp-corr" style="width:60px" value="1.0"></label>
        <label class="toggle" style="flex-direction:row"><input type="checkbox" id="exp-three"> all 3-leg combos</label>
      </div>
      <h3>Market prices (de-vigged)</h3>
      <div class="filterbar">
        <label>Team cover P<input type="number" id="exp-p-cover" min="0.01" max="0.99" step="0.005" value="0.5"></label>
        <label>Over P<input type="number" id="exp-p-over" min="0.01" max="0.99" step="0.005" value="0.5"></label>
        <label class="toggle" style="flex-direction:row"><input type="checkbox" id="exp-use-ml"> use market moneyline</label>
        <label>Team win P<input type="number" id="exp-p-ml" min="0.01" max="0.99" step="0.005" value="0.5" disabled></label>
      </div>
      <h3>Model overrides</h3>
      <div class="filterbar">
        <label class="toggle" style="flex-direction:row"><input type="checkbox" id="exp-override"> override σ / ρ</label>
        <label>σ home<input type="number" id="exp-sig-home" min="5" max="16" step="0.1" value="9.5" disabled></label>
        <label>σ away<input type="number" id="exp-sig-away" min="5" max="16" step="0.1" value="9.0" disabled></label>
        <label>ρ<input type="number" id="exp-rho" min="-0.5" max="0.5" step="0.01" value="0.05" disabled></label>
      </div>
      <h3>Legs</h3><div id="exp-legs"></div>
      <div class="toolbar"><button id="exp-add">+ add leg</button><span class="spacer"></span><button id="exp-price">Price combo</button></div>
    </div>
    <div id="exp-result"></div>`;
  const legsBox = $("exp-legs");
  legsBox.appendChild(explorerLegRow());
  legsBox.appendChild(explorerLegRow());
  $("exp-add").addEventListener("click", () => legsBox.appendChild(explorerLegRow()));
  const syncMarket = () => {
    const g = games.find((x) => x.game_id === $("exp-game").value);
    const teamIsHome = $("exp-team").value === (g?.home || $("exp-home").value);
    $("exp-p-cover").value = g?.p_home_cover == null ? "0.5" : String(teamIsHome ? g.p_home_cover : 1 - g.p_home_cover);
    $("exp-p-over").value = g?.p_over == null ? "0.5" : String(g.p_over);
    const ml = g?.p_home_ml == null ? null : (teamIsHome ? g.p_home_ml : 1 - g.p_home_ml);
    $("exp-use-ml").checked = ml != null;
    $("exp-p-ml").disabled = ml == null;
    $("exp-p-ml").value = String(ml ?? 0.5);
  };
  $("exp-game").addEventListener("change", (e) => {
    const g = games.find((x) => x.game_id === e.target.value);
    const team = $("exp-team");
    if (g) {
      team.innerHTML = `<option>${esc(g.home)}</option><option>${esc(g.away)}</option>`;
      $("exp-home").value = g.home; $("exp-away").value = g.away;
      $("exp-spread").value = g.spread_home; $("exp-total").value = g.total;
      ["exp-home", "exp-away", "exp-spread", "exp-total"].forEach((id) => $(id).disabled = true);
    } else {
      team.innerHTML = `<option>KC</option><option>BUF</option>`;
      ["exp-home", "exp-away", "exp-spread", "exp-total"].forEach((id) => $(id).disabled = false);
    }
    syncMarket();
  });
  $("exp-team").innerHTML = `<option>KC</option><option>BUF</option>`;
  $("exp-team").addEventListener("change", syncMarket);
  $("exp-use-ml").addEventListener("change", () => $("exp-p-ml").disabled = !$("exp-use-ml").checked);
  $("exp-override").addEventListener("change", () => {
    ["exp-sig-home", "exp-sig-away", "exp-rho"].forEach((id) => $(id).disabled = !$("exp-override").checked);
  });
  $("exp-price").addEventListener("click", async () => {
    const payload = {
      legs: [...legsBox.children].map((row) => ({
        kind: row.querySelector(".leg-kind").value,
        side: row.querySelector(".leg-side").value,
      })),
      team: $("exp-team").value,
      model: $("exp-model").value,
      params_file: $("exp-params").value || null,
      corr_scale: Number.isFinite(parseFloat($("exp-corr").value)) ? parseFloat($("exp-corr").value) : 1.0,
      p_team_cover: parseFloat($("exp-p-cover").value),
      p_over: parseFloat($("exp-p-over").value),
      use_ml: $("exp-use-ml").checked,
      p_team_ml: parseFloat($("exp-p-ml").value),
      three_leg: $("exp-three").checked,
    };
    if ($("exp-override").checked) payload.override = {
      sigma_home: parseFloat($("exp-sig-home").value),
      sigma_away: parseFloat($("exp-sig-away").value),
      rho: parseFloat($("exp-rho").value),
    };
    const gid = $("exp-game").value;
    if (gid) payload.game_id = gid;
    else {
      payload.home = $("exp-home").value || "KC";
      payload.away = $("exp-away").value || "BUF";
      payload.spread_home = parseFloat($("exp-spread").value);
      payload.total = parseFloat($("exp-total").value);
    }
    $("exp-result").innerHTML = `<p class="caption">pricing…</p>`;
    try {
      const data = await post("/api/nfl/explorer/price", payload);
      if (data.error) { $("exp-result").innerHTML = `<div class="alert error">${esc(data.error)}</div>`; return; }
      const target = $("exp-result");
      target.innerHTML = "";
      renderNflPayloadInto(data, target);
    } catch (e) {
      $("exp-result").innerHTML = `<div class="alert error">failed: ${esc(e.message)}</div>`;
    }
  });
}

function renderNflPayloadInto(data, root) {
  const notes = document.createElement("div");
  notes.innerHTML = (data.notes || []).map((n) => `<p class="caption">${esc(n)}</p>`).join("");
  root.appendChild(notes);
  const metrics = data.metrics || [];
  if (metrics.length || data.combo) {
    const kpis = document.createElement("div");
    kpis.className = "kpis";
    kpis.innerHTML = metrics.map(nflMetric).join("");
    if (data.combo) {
      const c = data.combo;
      kpis.innerHTML += kpi("Model P", fmtPct(c.model, 2)) + kpi("Naive P", fmtPct(c.naive, 2)) +
        kpi("Corr adj", (c.adj_bps >= 0 ? "+" : "") + Number(c.adj_bps).toFixed(0) + " bps") +
        (c.realized ? kpi("Realized", esc(c.realized).toUpperCase()) : "");
    }
    root.appendChild(kpis);
  }
  (data.specs || []).forEach((s) => {
    const div = document.createElement("div");
    div.className = "vega-chart";
    div.innerHTML = `<h3>${esc(s.title || s.id || "")}</h3>`;
    const t = document.createElement("div");
    div.appendChild(t);
    root.appendChild(div);
    vegaEmbed(t, s.spec, { actions: true, theme: "dark" }).catch((e) => {
      t.innerHTML = `<p class="caption">chart failed: ${esc(e.message)}</p>`;
    });
  });
  Object.entries(data.tables || {}).forEach(([name, rows]) => {
    if (!rows || !rows.length) return;
    const cols = Object.keys(rows[0]);
    const div = document.createElement("div");
    div.className = "chart";
    div.innerHTML = `<h3>${esc(name)} (${rows.length})</h3>
      <div class="table-wrap" style="max-height:none"><table>
      <thead><tr>${cols.map((c) => `<th class="${typeof rows[0][c] === "number" ? "num" : ""}">${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${cols.map((c) => {
        const val = r[c];
        return `<td class="${typeof val === "number" ? "num" : ""}">${typeof val === "number" ? val.toFixed(4) : esc(val)}</td>`;
      }).join("")}</tr>`).join("")}</tbody></table></div>`;
    root.appendChild(div);
  });
}

$("nfl-run-params").addEventListener("click", () => nflRun("refresh_params"));
$("nfl-pull-data").addEventListener("click", () => nflRun("pull_data"));
$("nfl-run-backtest").addEventListener("click", () => nflRun("run_backtest", false, {
  first_season: parseInt($("nfl-first-season").value, 10),
  last_season: parseInt($("nfl-last-season").value, 10),
}));
$("nfl-run-week1").addEventListener("click", () => nflRun("run_week_backtest", true));

async function nflRun(script, switchToWeek1 = false, options = {}) {
  const out = $("nfl-run-output");
  out.classList.remove("hidden");
  out.textContent = `running ${script}…`;
  try {
    const res = await post("/api/nfl/run", { script, options });
    out.textContent = `$ ${script} → exit ${res.returncode}\n\n${res.output || res.stdout || ""}`;
    nflMeta = null;
    if (switchToWeek1 && res.returncode === 0) {
      await loadSources();
      await switchSource("week1_backtest.db");
      out.textContent += "\n\nswitched the live tabs to the Week 1 backtest database.";
    }
    if (state.tab === "nfl") await refreshNfl();
  } catch (e) {
    out.textContent = `failed: ${e.message}`;
  }
}
