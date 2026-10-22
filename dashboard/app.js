/* ChronoTrace war-room dashboard. Plain JS + SVG, no build step. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
const COLORS = ["#58a6ff", "#f0a040", "#c297ff", "#39d0b8", "#f0506e", "#8b98a8"];

const state = { cases: [], showcase: [], bundle: null, series: null, ev: "metrics", bench: null, metric: "AC@1", reqId: 0 };

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

/* ---------------- tabs ---------------- */
function activateTab(button) {
  $$(".tab").forEach((tab) => {
    const selected = tab === button;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  $$(".tabpane").forEach((panel) => {
    const selected = panel.id === button.getAttribute("aria-controls");
    panel.classList.toggle("active", selected);
  });
  if (button.dataset.tab === "benchmark") loadBenchmark();
}

function initTabs() {
  const tabs = $$(".tab");
  tabs.forEach((button, index) => {
    button.addEventListener("click", () => activateTab(button));
    button.addEventListener("keydown", (event) => {
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault();
      tabs[next].focus();
      activateTab(tabs[next]);
    });
  });
}


/* ---------------- case list ---------------- */
async function loadCases() {
  const data = await api("/api/cases");
  state.cases = data.cases;
  state.showcase = data.showcase.filter((s) => data.cases.some((c) => c.case === s.case));
  const ds = $("#ds");
  ds.innerHTML = `<option value="">All suites</option>` + data.datasets.map((d) => `<option>${esc(d)}</option>`).join("");
  ds.value = data.datasets.includes("RE2-OB") ? "RE2-OB" : "";
  ds.addEventListener("change", renderCaseList);
  $("#search").addEventListener("input", renderCaseList);
  $("#showcase").innerHTML = state.showcase
    .map((s) => `<li role="option" aria-selected="false" tabindex="-1" data-case="${esc(s.case)}"><b>${esc(s.case)}</b><span>${esc(s.why)}</span></li>`)
    .join("");
  wireCaseListKeyboard($("#showcase"));
  wireCaseListKeyboard($("#cases"));
  $("#showcase li").forEach((li) => li.addEventListener("click", () => selectCase(li.dataset.case)));
  renderCaseList();
  const fromHash = new URLSearchParams(location.hash.slice(1)).get("case");
  if (fromHash) selectCase(fromHash);
}

function renderCaseList() {
  const ds = $("#ds").value;
  const q = $("#search").value.trim().toLowerCase();
  const rows = state.cases.filter((c) => (!ds || c.dataset === ds) && (!q || c.case.toLowerCase().includes(q)));
  $("#count").textContent = `(${rows.length})`;
  $("#cases").innerHTML = rows
    .map((c) => `<li role="option" aria-selected="false" tabindex="-1" data-case="${esc(c.case)}"><span>${esc(c.case.replace(/^re\d\w\w_/, ""))}</span><span class="tag">${esc(c.dataset)}</span></li>`)
    .join("");
  $("#cases li").forEach((li) => li.addEventListener("click", () => selectCase(li.dataset.case)));
  markActive();
}

function wireCaseListKeyboard(list) {
  list.addEventListener("keydown", (event) => {
    const options = $("li[role=option]", list);
    const currentOption = event.target.closest('li[role="option"]');
    const current = options.indexOf(currentOption);
    if (current < 0) return;
    let next = current;
    if (event.key === "ArrowDown") next = Math.min(options.length - 1, current + 1);
    else if (event.key === "ArrowUp") next = Math.max(0, current - 1);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = options.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectCase(options[current].dataset.case);
      return;
    } else return;
    event.preventDefault();
    options[next]?.focus();
  });
}

function markActive() {
  const id = state.bundle?.case_id || state.pending;
  $(".caselist").forEach((list) => {
    const options = $('li[role="option"]', list);
    const selected = options.find((option) => option.dataset.case === id) || null;
    options.forEach((option, index) => {
      const isSelected = option === selected;
      option.classList.toggle("active", isSelected);
      option.setAttribute("aria-selected", String(isSelected));
      option.tabIndex = isSelected || (!selected && index === 0) ? 0 : -1;
    });
  });
}

/* ---------------- diagnose ---------------- */
async function selectCase(id, fresh = false) {
  const req = ++state.reqId;
  state.pending = id;
  history.replaceState(null, "", `#case=${encodeURIComponent(id)}`);
  markActive();
  $("#empty").hidden = true;
  $("#incident").hidden = false;
  $("#result").hidden = true;
  $("#loading").hidden = false;
  const meta = state.cases.find((c) => c.case === id);
  if (meta && $("#ds").value && $("#ds").value !== meta.dataset) { $("#ds").value = meta.dataset; renderCaseList(); }
  $("#inc-title").textContent = id;
  $("#inc-system").textContent = meta ? `${meta.dataset} · ${meta.system_name}` : "";
  $("#explain-out").innerHTML = "";
  $("#explain-status").textContent = "";
  try {
    const b = await api(`/api/diagnose/${encodeURIComponent(id)}${fresh ? "?fresh=1" : ""}`);
    if (req !== state.reqId) return;
    state.bundle = b;
    renderBundle();
    $("#loading").hidden = true;
    $("#result").hidden = false;
    loadSeries(b, req);
  } catch (e) {
    if (req !== state.reqId) return;
    $("#loading").hidden = true;
    $("#result").hidden = true;
    $("#empty").hidden = false;
    $("#empty").innerHTML = `<h2>Could not diagnose ${esc(id)}</h2><p>${esc(e.message)}</p>`;
  }
}

function renderBundle() {
  const b = state.bundle;
  const d = b.diagnosis;
  const reveal = $("#reveal").checked;
  const top = d.where_service_ranks[0];
  const confCls = d.ambiguous ? "lo" : "hi";
  $("#p-where").innerHTML = `${esc(d.root_cause)}<span class="conf ${confCls}">${d.ambiguous ? "ambiguous" : "confidence"} ${fmt(d.confidence)}</span>`;
  const second = d.where_service_ranks[1];
  $("#p-where-sub").textContent = second ? `next: ${second.service} (${fmt(second.score)}) · ${b.n_candidates} candidates` : "";
  $("#p-what").textContent = d.what.summary;
  const ind = d.what.primary_indicator;
  const me = d.what.metric_evidence[0];
  const lg = d.what.log_evidence;
  let sub = ind && me ? `${ind}: ${fmt(me.pre_median, 3)} → ${fmt(me.post_median, 3)}` : "";
  if (lg && lg.stack_trace) sub = `stack trace: ${lg.template.slice(0, 80)}`;
  $("#p-what-sub").textContent = sub;
  $("#p-why").innerHTML = d.why_causal_chain
    .map((c, i) => `<span class="hop ${i === 0 ? "origin" : ""}">${esc(c.service)}${c.onset_sec_after_inject != null ? ` +${c.onset_sec_after_inject}s` : ""}</span>`)
    .join(" ← ");
  const affected = d.where_service_ranks.filter((r) => r.role !== "NORMAL" && r.role !== "ROOT_CAUSE").map((r) => r.service);
  $("#p-why-sub").textContent =
    d.why_causal_chain.length > 1 ? "callers degraded only through the origin" : affected.length ? `also affected: ${affected.slice(0, 3).join(", ")}` : "no other service affected";
  $("#p-next").innerHTML = d.next_steps.commands.slice(0, 3).map((c) => `<li>${esc(c)}</li>`).join("");

  const gt = b.ground_truth;
  const ban = $("#gt-banner");
  if (reveal && gt) {
    const hit = gt.chronotrace_rank === 1;
    ban.hidden = false;
    ban.className = `gt-banner ${hit ? "hit" : "miss"}`;
    const br = b.baseline?.baro_rank_of_truth;
    ban.textContent = `Ground truth: ${gt.root_cause} (${gt.fault}). ChronoTrace rank ${gt.chronotrace_rank ?? "–"}${br ? ` · BARO rank ${br}` : ""}.`;
  } else ban.hidden = true;

  renderGraph(b);
  renderRanks(b, reveal);
  renderGate(b);
  renderVersus(b, reveal);
  renderReceipt(b);
}

function layoutGraph(services, edges) {
  const inE = {}, outE = {};
  services.forEach((s) => { inE[s] = []; outE[s] = []; });
  edges.forEach((e) => {
    if (e.caller === e.callee || !(e.caller in inE) || !(e.callee in inE)) return;
    outE[e.caller].push(e.callee);
    inE[e.callee].push(e.caller);
  });
  const linked = services.filter((s) => inE[s].length || outE[s].length);
  const depth = {};
  const roots = linked.filter((s) => !inE[s].length);
  const queue = (roots.length ? roots : linked.slice(0, 1)).map((s) => [s, 0]);
  while (queue.length) {
    const [s, dpt] = queue.shift();
    if (depth[s] !== undefined && depth[s] >= dpt) continue;
    if (dpt > 8) continue;
    depth[s] = dpt;
    outE[s].forEach((t) => queue.push([t, dpt + 1]));
  }
  linked.forEach((s) => { if (depth[s] === undefined) depth[s] = 0; });
  const layers = {};
  linked.forEach((s) => (layers[depth[s]] = layers[depth[s]] || []).push(s));
  const unlinked = services.filter((s) => !linked.includes(s));
  return { layers, unlinked };
}

function renderGraph(b) {
  const d = b.diagnosis;
  const ranks = Object.fromEntries(d.where_service_ranks.map((r) => [r.service, r]));
  let services = d.where_service_ranks.map((r) => r.service);
  const edges = d.call_graph;
  const shifted = new Set((d.what.trace_evidence?.shifted_edges) || []);
  // Large systems: keep the 18 highest-scoring services plus their direct neighbours of the root.
  if (services.length > 22) {
    const keep = new Set(services.slice(0, 18));
    edges.forEach((e) => { if (e.caller === d.root_cause) keep.add(e.callee); if (e.callee === d.root_cause) keep.add(e.caller); });
    services = services.filter((s) => keep.has(s));
  }
  const { layers, unlinked } = layoutGraph(services, edges);
  const W = 680, rowH = 96, padX = 50;
  const depths = Object.keys(layers).map(Number).sort((a, b) => a - b);
  const pos = {};
  depths.forEach((dp, i) => {
    const row = layers[dp].sort((a, c) => (ranks[c]?.score || 0) - (ranks[a]?.score || 0));
    const stagger = row.length > 4 ? 22 : 0;
    row.forEach((s, j) => (pos[s] = { x: padX + ((W - 2 * padX) * (j + 0.5)) / row.length, y: 40 + i * rowH + (j % 2) * stagger }));
  });
  const baseY = 40 + depths.length * rowH + (depths.length ? 10 : 0);
  const perRow = 6;
  unlinked.forEach((s, j) => (pos[s] = { x: padX + ((W - 2 * padX) * ((j % perRow) + 0.5)) / perRow, y: baseY + Math.floor(j / perRow) * 62 }));
  const H = Math.max(...Object.values(pos).map((p) => p.y), 60) + 46;
  const color = (s) => {
    const r = ranks[s];
    if (!r) return "var(--normal)";
    if (r.role === "ROOT_CAUSE") return "var(--root)";
    if (r.role !== "NORMAL") return "var(--victim)";
    return "var(--normal)";
  };
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(`Service call graph for ${d.root_cause}: ${services.length} services, ${edges.filter((edge) => services.includes(edge.caller) && services.includes(edge.callee) && edge.caller !== edge.callee).length} calls, ${shifted.size} slowed edges.`)}"><defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="#4b5563"/></marker><marker id="arrR" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="#f0506e"/></marker></defs>`;
  const maxCalls = Math.max(1, ...edges.map((e) => e.calls));
  edges.forEach((e) => {
    const a = pos[e.caller], c = pos[e.callee];
    if (!a || !c || e.caller === e.callee) return;
    const isS = shifted.has(`${e.caller}->${e.callee}`);
    const w = 0.8 + 2.2 * Math.sqrt(e.calls / maxCalls);
    const dx = c.x - a.x, dy = c.y - a.y, L = Math.hypot(dx, dy) || 1;
    const r1 = 16, r2 = 19;
    svg += `<line x1="${a.x + (dx / L) * r1}" y1="${a.y + (dy / L) * r1}" x2="${c.x - (dx / L) * r2}" y2="${c.y - (dy / L) * r2}" stroke="${isS ? "#f0506e" : "#3a4452"}" stroke-width="${isS ? w + 0.6 : w}" ${isS ? 'stroke-dasharray="5 3"' : ""} marker-end="url(#${isS ? "arrR" : "arr"})"><title>${esc(e.caller)} → ${esc(e.callee)}: ${e.calls} calls</title></line>`;
  });
  services.forEach((s) => {
    const p = pos[s]; if (!p) return;
    const r = ranks[s];
    const rad = 9 + 10 * Math.min(1, r?.score || 0);
    const isRoot = r?.role === "ROOT_CAUSE";
    svg += `<g><circle cx="${p.x}" cy="${p.y}" r="${rad}" fill="${color(s)}" fill-opacity="${isRoot ? 1 : 0.85}" ${isRoot ? 'stroke="#fff" stroke-width="2"' : ""}><title>${esc(s)} · score ${fmt(r?.score)} · ${esc(r?.role)}</title></circle>`;
    const label = s.length > 22 ? s.slice(0, 21) + "…" : s;
    svg += `<text class="node-label" x="${p.x}" y="${p.y + rad + 12}" text-anchor="middle">${esc(label)}</text>`;
    svg += `<text class="node-score" x="${p.x}" y="${p.y + rad + 23}" text-anchor="middle">${fmt(r?.score)}</text></g>`;
  });
  if (unlinked.length && depths.length) svg += `<text class="node-score" x="12" y="${baseY - 26}">no spans observed for:</text>`;
  svg += "</svg>";
  $("#graph").innerHTML = svg;
  const hidden = d.where_service_ranks.length - services.length;
  $("#graph-note").textContent = edges.length
    ? `${edges.length} call edges from traces${hidden > 0 ? ` · ${hidden} low-score services hidden` : ""}`
    : b.modalities.traces ? "traces present but no cross-service edges" : "no traces in this system — services shown without edges";
}

function renderRanks(b, reveal) {
  const d = b.diagnosis;
  const gt = b.ground_truth?.root_cause;
  const rows = d.where_service_ranks.slice(0, 10);
  const mb = (v, cls) => `<i class="${cls}" style="height:${Math.max(2, 18 * (v || 0))}px" title="${fmt(v)}"></i>`;
  $("#ranks").innerHTML =
    `<tr><th>#</th><th>service</th><th>score</th><th title="metric / log / trace">m·l·t</th><th>role</th><th>onset</th></tr>` +
    rows
      .map(
        (r) => `<tr class="${reveal && r.service === gt ? "gt" : ""}"><td>${r.rank}</td><td class="svc" title="${esc(r.service)}">${esc(r.service)}</td>
        <td><div class="bar"><i style="width:${100 * (r.score || 0)}%"></i></div></td>
        <td><div class="minibars">${mb(r.metric, "mb-m")}${mb(r.log, "mb-l")}${mb(r.trace, "mb-t")}</div></td>
        <td><span class="role ${esc(r.role)}">${esc(r.role.replace(/_/g, " ").toLowerCase())}</span></td>
        <td class="muted">${r.onset_sec_after_inject != null ? "+" + r.onset_sec_after_inject + "s" : "–"}</td></tr>`
      )
      .join("");
}

function renderGate(b) {
  const g = b.modality_gate;
  const col = { metric: "var(--metric)", log: "var(--log)", trace: "var(--trace)" };
  $("#gate").innerHTML = ["metric", "log", "trace"]
    .map((m) => {
      const x = g[m] || {};
      const off = !x.used;
      const why = !x.available ? (m === "trace" ? "no traces" : m === "log" ? "no logs" : "n/a") : `decisive ${fmt(x.decisiveness)} × base ${fmt(x.base_weight, 1)}`;
      return `<div class="gate-row ${off ? "off" : ""}"><span class="nm" style="color:${col[m]}">${m}</span>
        <div class="bar"><i style="width:${100 * (x.share || 0)}%;background:${col[m]}"></i></div>
        <span class="val" title="${esc(why)}">${off ? why : (100 * (x.share || 0)).toFixed(0) + "% share"}</span></div>`;
    })
    .join("");
}

function renderVersus(b, reveal) {
  const gt = reveal ? b.ground_truth?.root_cause : null;
  const ct = b.diagnosis.where_service_ranks.slice(0, 5).map((r) => r.service);
  const baro = b.baseline?.baro_top5 || [];
  const li = (s) => `<li class="${s === gt ? "hit" : ""}">${esc(s)}</li>`;
  $("#versus").innerHTML = `<div><h4>ChronoTrace</h4><ol>${ct.map(li).join("")}</ol></div><div><h4>BARO${b.baseline?.baro_top_metric ? ` <span class="muted">(top metric ${esc(b.baseline.baro_top_metric)})</span>` : ""}</h4><ol>${baro.map(li).join("") || "<li>n/a</li>"}</ol></div>`;
}

function renderReceipt(b) {
  const t = b.timing_ms;
  const rows = [
    ["engine", b.engine],
    ["input sha256", b.reproducibility.input_sha256],
    ["evidence sha256", b.reproducibility.evidence_sha256],
    ["LLM used in diagnosis", b.reproducibility.llm_used_in_diagnosis ? "yes" : "no"],
    ["analysis time", `${fmt(t.analysis_total, 0)} ms (metrics ${fmt(t.metrics, 0)} · logs ${fmt(t.logs, 0)} · traces ${fmt(t.traces, 0)} · fusion ${fmt(t.fusion, 1)})`],
    ["data load", `${fmt(t.load, 0)} ms`],
    ["candidates", b.n_candidates],
    ["modalities", Object.entries(b.modalities).filter(([, v]) => v).map(([k]) => k).join(", ")],
  ];
  $("#receipt").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  $("#hygiene").textContent = JSON.stringify(b.hygiene, null, 1);
  $("#raw").textContent = JSON.stringify(b, null, 1);
}

/* ---------------- evidence charts ---------------- */
async function loadSeries(b, req) {
  const svcs = b.diagnosis.where_service_ranks.slice(0, 3).map((r) => r.service);
  $("#chart").innerHTML = `<div class="loading"><div class="spinner"></div><span>loading series…</span></div>`;
  try {
    const s = await api(`/api/series/${encodeURIComponent(b.case_id)}?services=${encodeURIComponent(svcs.join(","))}`);
    if (req !== state.reqId) return;
    state.series = { ...s, services: svcs };
    renderChart();
  } catch (e) {
    $("#chart").innerHTML = `<p class="muted">${esc(e.message)}</p>`;
  }
}

function miniChart(points, it, color, title, W = 640, H = 78) {
  if (!points || !points.length) return `<g role="img" aria-label="${esc(`${title}: no data available.`)}"><text x="8" y="20">${esc(title)}: no data</text></g>`;
  const xs = points.map((p) => p[0]), ys = points.map((p) => p[1] ?? 0);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 === y0) { y1 += 1; y0 -= 1; }
  const L = 46, R = 8, T = 16, B = 14;
  const X = (x) => L + ((x - x0) / (x1 - x0 || 1)) * (W - L - R);
  const Y = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1] ?? 0).toFixed(1)}`).join("");
  const ix = X(it);
  const chartLabel = `${title}. ${points.length} samples from ${shortNum(x0)} to ${shortNum(x1)}; values range from ${shortNum(Math.min(...ys))} to ${shortNum(Math.max(...ys))}; fault injected at ${shortNum(it)}.`;
  return `<g role="img" aria-label="${esc(chartLabel)}"><text x="${L}" y="11" style="fill:#c9d4e0">${esc(title)}</text>
    <line x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}" stroke="#2a3340"/>
    <text x="${L - 4}" y="${T + 4}" text-anchor="end">${esc(shortNum(y1))}</text><text x="${L - 4}" y="${H - B}" text-anchor="end">${esc(shortNum(y0))}</text>
    <rect x="${ix}" y="${T}" width="${W - R - ix}" height="${H - T - B}" fill="#f0506e" fill-opacity="0.06"/>
    <line x1="${ix}" x2="${ix}" y1="${T - 4}" y2="${H - B}" stroke="#f0506e" stroke-dasharray="3 3"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.4"/></g>`;
}

function shortNum(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1) + "G";
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "k";
  if (a >= 10) return v.toFixed(0);
  if (a >= 1) return v.toFixed(1);
  return v.toFixed(3);
}

function renderChart() {
  const s = state.series;
  const b = state.bundle;
  if (!s) return;
  const mode = state.ev;
  const W = 640, H = 78;
  let parts = [], legend = "";
  if (mode === "logs") {
    const lg = b.diagnosis.what.log_evidence;
    const svcWithLogs = s.services.filter((x) => s.logs[x]);
    svcWithLogs.forEach((svc, i) => {
      parts.push(`<g transform="translate(0,${i * (H + 6)})">${miniChart(s.logs[svc], s.inject_time, COLORS[i], `${svc} · error lines per 10 s`)}</g>`);
    });
    const n = svcWithLogs.length;
    let html = n ? `<svg viewBox="0 0 ${W} ${n * (H + 6)}">${parts.join("")}</svg>` : `<p class="muted">${b.modalities.logs ? "No error-level log lines for the top services." : "This suite has no logs (RE1 is metrics only)."}</p>`;
    if (lg && lg.sample) html += `<div class="muted mt">Root-cause log evidence (${lg.stack_trace ? "stack trace" : lg.novel ? "new template" : "burst"}, ${lg.pre_count} before / ${lg.post_count} after; PII masked):</div><pre class="pre">${esc(lg.sample)}</pre>`;
    $("#chart").innerHTML = html;
    $("#chart-legend").innerHTML = "";
    return;
  }
  const src = mode === "metrics" ? s.metrics : s.traces;
  const svcs = s.services.filter((x) => src[x]);
  svcs.forEach((svc, i) => {
    const pts = mode === "metrics" ? src[svc].points : src[svc];
    const title = mode === "metrics" ? `${svc} · ${src[svc].column}` : `${svc} · p90 exclusive time (ms)`;
    parts.push(`<g transform="translate(0,${i * (H + 6)})">${miniChart(pts, s.inject_time, COLORS[i], title)}</g>`);
  });
  $("#chart").innerHTML = svcs.length
    ? `<svg viewBox="0 0 ${W} ${svcs.length * (H + 6)}">${parts.join("")}</svg>`
    : `<p class="muted">${mode === "traces" ? "No traces for these services (Sock Shop and RE1 have none)." : "No series."}</p>`;
  $("#chart-legend").innerHTML = `<span><i style="background:#f0506e"></i>fault injection (shaded = faulty window)</span>`;
}

/* ---------------- explanation ---------------- */
async function runExplain() {
  const b = state.bundle;
  if (!b) return;
  const btn = $("#explain");
  btn.disabled = true;
  $("#explain-status").textContent = "asking the explanation layer…";
  $("#explain-out").innerHTML = "";
  try {
    const e = await api(`/api/explain/${encodeURIComponent(b.case_id)}`, { method: "POST" });
    const r = e.report;
    const chips = [
      `<span class="chip">${esc(e.provider)}</span>`,
      `<span class="chip">${esc(e.model)}</span>`,
      `<span class="chip ${e.verified ? "ok" : "warn"}">${e.verified ? "verified against evidence" : "unverified"}</span>`,
      e.latency_ms ? `<span class="chip">${e.latency_ms} ms</span>` : "",
      `<span class="chip" title="the bundle the model was given">evidence ${esc(e.evidence_sha256.slice(0, 12))}…</span>`,
    ].join("");
    const rej = (e.attempts || [])
      .map((a) => `<div class="rejected">${esc(a.provider)}${a.model ? " (" + esc(a.model) + ")" : ""}: ${esc(a.error || "rejected — " + (a.rejected || []).join("; "))}</div>`)
      .join("");
    $("#explain-out").innerHTML = `<div class="exp-meta">${chips}</div>${rej}
      <div class="exp-block"><b>Summary</b>${esc(r.executive_summary)}</div>
      <div class="exp-block"><b>Reasoning</b>${esc(r.root_cause_reasoning)}</div>
      <div class="exp-block"><b>Causal chain</b>${r.causal_chain.map(esc).join(" ← ")}</div>
      <div class="exp-block"><b>Runbook</b><ol class="cmds">${r.remediation_steps.map((s) => `<li><code>${esc(s.command)}</code><div class="muted">${esc(s.rationale)}</div></li>`).join("")}</ol></div>
      <div class="exp-block"><b>Before acting</b>${esc(r.risk_notes)}</div>`;
    $("#explain-status").textContent = "";
  } catch (err) {
    $("#explain-status").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- benchmark ---------------- */
const SHOW = [
  ["chronotrace", "ChronoTrace", "main"],
  ["baro", "BARO (official)", "base"],
  ["nsigma", "N-Sigma (official)", "base"],
  ["no_traces", "– without traces", "abl"],
  ["no_logs", "– without logs", "abl"],
  ["no_new_behaviour", "– without new-template signal", "abl"],
  ["no_trace_gate", "– without trace gate", "abl"],
  ["metric_only", "– metrics only", "abl"],
  ["metric_no_novelty", "– metrics only, no novelty filter", "abl"],
];
const COLS = ["AC@1", "AC@3", "AC@5", "Avg@5", "Retrieval@all", "Rerank@1", "Chance@5", "Lift@5"];

async function loadBenchmark() {
  if (!state.bench) {
    try {
      state.bench = await api("/api/benchmark");
    } catch (e) {
      $("#bench-tables").innerHTML = `<p class="muted">No benchmark results yet. Run <code>python scripts/run_benchmark.py</code>.</p>`;
      return;
    }
  }
  renderBench();
}

function renderBench() {
  const rep = state.bench;
  const t = rep.summary.datasets;
  const m = state.metric;
  $("#bench-proto").textContent = rep.protocol + ` · generated ${rep.generated_at}`;
  $("#bars-title").textContent = `${m} by suite`;
  const dsList = Object.keys(t).filter((d) => d !== "ALL");
  const order = ["ALL", ...dsList];
  const maxV = m === "Lift@5" ? Math.max(...order.flatMap((d) => ["chronotrace", "baro", "nsigma"].map((k) => t[d][k]?.[m] || 0))) : 1;
  $("#bars").setAttribute("role", "img");
  $("#bars").setAttribute("aria-label", `${m} benchmark comparison across ${order.length} suites, including ChronoTrace, BARO and N-Sigma; values are shown for each suite.`);
  $("#bars").innerHTML =
    `<div class="bars-legend"><span><i style="background:var(--accent)"></i>ChronoTrace</span><span><i style="background:#8b98a8"></i>BARO</span><span><i style="background:#4b5563"></i>N-Sigma</span></div>` +
    order
      .map((d) => {
        const lines = ["chronotrace", "baro", "nsigma"]
          .map((k) => {
            const v = t[d][k]?.[m];
            return `<div class="bar-line"><div class="b ${k}" style="width:${Math.max(0.5, (100 * (v || 0)) / (maxV || 1))}%"></div><span class="v">${fmt(v)}</span></div>`;
          })
          .join("");
        return `<div class="bar-group"><span class="ds">${esc(d)}<br><span class="muted">${t[d].chronotrace.cases} cases</span></span><div class="bar-lines">${lines}</div></div>`;
      })
      .join("");

  $("#bench-tables").innerHTML = order
    .map((d) => {
      const best = {};
      COLS.forEach((c) => (best[c] = Math.max(...SHOW.filter(([k]) => k !== "metric_no_novelty").map(([k]) => (c === "Chance@5" ? -1 : t[d][k]?.[c] ?? -1)))));
      const rows = SHOW.filter(([k]) => t[d][k])
        .map(([k, name, cls]) => `<tr class="${cls}"><td>${esc(name)}</td>${COLS.map((c) => `<td class="${cls === "main" && t[d][k][c] === best[c] && c !== "Chance@5" ? "best" : ""}">${fmt(t[d][k][c], 3)}</td>`).join("")}</tr>`)
        .join("");
      return `<table class="bt"><caption>${esc(d)} · ${t[d].chronotrace.cases} cases</caption><tr><th>method</th>${COLS.map((c) => `<th>${c}</th>`).join("")}</tr>${rows}</table>`;
    })
    .join("");

  const bf = rep.summary.by_fault;
  $("#by-fault").innerHTML =
    `<table class="bt"><tr><th>fault</th><th>cases</th><th>CT AC@1</th><th>BARO AC@1</th><th>CT Avg@5</th><th>BARO Avg@5</th></tr>` +
    Object.entries(bf)
      .map(([f, v]) => `<tr><td>${esc(f)}</td><td>${v.chronotrace.cases}</td><td>${fmt(v.chronotrace["AC@1"], 3)}</td><td>${fmt(v.baro["AC@1"], 3)}</td><td>${fmt(v.chronotrace["Avg@5"], 3)}</td><td>${fmt(v.baro["Avg@5"], 3)}</td></tr>`)
      .join("") +
    "</table>";
  const lat = rep.summary.latency_ms;
  const notes = [
    `Analysis time per case (excluding file load): median ${lat.median} ms, p90 ${lat.p90} ms.`,
    `Chance@5 is computed over each method's own candidate set; ChronoTrace folds datastores into their owning service, so its pool is slightly smaller. Compare Lift@5 for a pool-independent view.`,
    rep.skipped_unusable_cases.length ? `Skipped (broken in the dataset: empty normal or faulty window): ${rep.skipped_unusable_cases.join(", ")}.` : "",
    rep.not_downloaded_cases ? `${rep.not_downloaded_cases} cases of the selected suites were not downloaded.` : "",
    `RE2-TT (2 GB of traces) was not part of this run.`,
  ].filter(Boolean);
  $("#bench-notes").innerHTML = `<ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`;
}

/* ---------------- boot ---------------- */
function boot() {
  initTabs();
  $("#reveal").addEventListener("change", () => state.bundle && renderBundle());
  $("#rerun").addEventListener("click", () => state.bundle && selectCase(state.bundle.case_id, true));
  $("#explain").addEventListener("click", runExplain);
  $$("#ev-seg button").forEach((b) =>
    b.addEventListener("click", () => {
      $$("#ev-seg button").forEach((x) => x.classList.toggle("active", x === b));
      state.ev = b.dataset.ev;
      renderChart();
    })
  );
  $$("#metric-seg button").forEach((b) =>
    b.addEventListener("click", () => {
      $$("#metric-seg button").forEach((x) => x.classList.toggle("active", x === b));
      state.metric = b.dataset.m;
      renderBench();
    })
  );
  loadCases().catch((e) => ($("#empty").innerHTML = `<h2>API not reachable</h2><p>${esc(e.message)}</p>`));
  const tab = new URLSearchParams(location.hash.slice(1)).get("tab");
  if (tab) $(`.tab[data-tab="${tab}"]`)?.click();
}
boot();
