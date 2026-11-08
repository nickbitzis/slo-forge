// SLO Forge UI: plain ES module, no build step. Hash routes:
// #/ overview, #/slo/<service>/<slo>[/<from>/<to>], #/alerts, #/gate
const app = document.getElementById("app");
const tip = document.getElementById("tip");

const STATUS = {
  ok: ["Healthy", "var(--good)"], at_risk: ["Budget under 25%", "var(--warning)"],
  exhausted: ["Budget spent", "var(--critical)"], burning: ["Page firing", "var(--burning)"],
};
const STRATS = [
  ["static", "Static threshold"], ["single_window", "Single-window burn rate"],
  ["multiwindow", "Multiwindow multi-burn-rate"],
];

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const get = (url) => fetch(url).then((r) => { if (!r.ok) throw new Error(url); return r.json(); });
const pct = (v, d = 1) => (v == null ? "n/a" : `${(v * 100).toFixed(d)}%`);
const fmtMin = (m) => (m == null ? "n/a" : m < 120 ? `${Math.round(m)} min` : `${(m / 60).toFixed(1)} h`);
const fmtDay = (d) => `day ${d.toFixed(d % 1 ? 2 : 0)}`;
const badge = (st) => `<span class="badge"><span class="dot" style="background:${STATUS[st][1]}"></span>${STATUS[st][0]}</span>`;

function bindTips(root) {
  root.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mousemove", (ev) => {
      tip.hidden = false;
      tip.innerHTML = el.dataset.tip;
      const x = Math.min(ev.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = `${Math.max(4, x)}px`;
      tip.style.top = `${ev.clientY + 12}px`;
    });
    el.addEventListener("mouseleave", () => { tip.hidden = true; });
  });
}

async function overview() {
  const d = await get("/api/slos");
  document.getElementById("clock").textContent = `Simulated now: ${fmtDay(d.day)} of 90`;
  const count = (st) => d.slos.filter((s) => s.status === st).length;
  const tiles = d.slos.map((s) => {
    const b = Math.max(0, Math.min(1, s.budget_remaining));
    return `<a class="tile st-${s.status}" href="#/slo/${s.slo_id}">
      <div class="name">${esc(s.service)}</div>
      <div class="meta">${esc(s.slo)}: ${s.objective}% over ${s.window}</div>
      <div class="big">${pct(s.budget_remaining)}</div>
      <div class="bar"><span style="width:${b * 100}%;background:${STATUS[s.status][1]}"></span></div>
      <div class="meta">budget left. 1h burn ${s.burn_rates["1h"].toFixed(1)}x</div>
      <div>${badge(s.status)}</div></a>`;
  }).join("");
  app.innerHTML = `
    <div class="card stats">
      <div class="stat"><span class="muted">SLOs</span><b>${d.slos.length}</b></div>
      <div class="stat"><span class="muted">Healthy</span><b>${count("ok")}</b></div>
      <div class="stat"><span class="muted">Budget under 25%</span><b>${count("at_risk")}</b></div>
      <div class="stat"><span class="muted">Budget spent</span><b>${count("exhausted")}</b></div>
      <div class="stat"><span class="muted">Page firing</span><b>${count("burning")}</b></div>
    </div>
    <h2>Error budget remaining <span class="muted">rolling window, at the end of the 90 simulated days</span></h2>
    <div class="legend">${Object.keys(STATUS).map(badge).join("")}</div>
    <div class="tiles">${tiles}</div>`;
}

// One chart per series, each with a single y-axis (small multiples share the x range).
function lineChart(title, t, ys, opts = {}) {
  const W = opts.w || 560, H = opts.h || 160, L = 46, R = 10, T = 8, B = 22;
  const t0 = t[0], t1 = t[t.length - 1] || t0 + 1;
  const vals = ys.filter((v) => v != null && isFinite(v));
  let lo = opts.lo ?? Math.min(0, ...vals), hi = opts.hi ?? Math.max(...vals, ...(opts.lines || []).map((l) => l.y));
  if (opts.cap && hi > opts.cap) hi = opts.cap;
  if (hi - lo < 1e-9) hi = lo + 1;
  const clipped = vals.some((v) => v > hi);
  const ticks = [0, 0.5, 1].map((f) => lo + f * (hi - lo));
  hi += (hi - lo) * 0.06;
  const x = (v) => L + ((v - t0) / (t1 - t0 || 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const fmt = opts.fmt || ((v) => v.toFixed(Math.abs(hi - lo) < 3 ? 1 : 0));
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/>
    <text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text>`).join("");
  const bands = (opts.bands || []).map((b) => {
    const a = Math.max(x(b.from), L), z = Math.min(x(b.to), W - R);
    return z > L && a < W - R ? `<rect x="${a}" y="${T}" width="${Math.max(1.5, z - a)}" height="${H - T - B}"
      fill="${b.significant ? "var(--band)" : "var(--band-minor)"}" data-tip="${esc(b.tip)}"/>` : "";
  }).join("");
  const lines = (opts.lines || []).map((l) => `<line x1="${L}" x2="${W - R}" y1="${y(l.y)}" y2="${y(l.y)}"
    stroke="${l.color}" stroke-dasharray="4 3"/><text x="${W - R - 2}" y="${y(l.y) - 3}" text-anchor="end"
    style="fill:${l.color}">${esc(l.label)}</text>`).join("");
  let path = "";
  ys.forEach((v, i) => {
    if (v == null || !isFinite(v)) return;
    path += `${path ? "L" : "M"}${x(t[i]).toFixed(1)},${y(Math.min(hi, Math.max(lo, v))).toFixed(1)}`;
  });
  const xt = [t0, (t0 + t1) / 2, t1].map((v, i) => `<text x="${x(v)}" y="${H - 6}"
    text-anchor="${["start", "middle", "end"][i]}">${fmtDay(v)}</text>`).join("");
  return `<div class="card chart"><h3>${title}${clipped ? ' <span class="muted">(peaks clipped)</span>' : ""}</h3>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${grid}${bands}${lines}
    <path d="${path}" fill="none" stroke="var(--series)" stroke-width="1.5"/>${xt}</svg></div>`;
}

// Lanes: one row per strategy, a bar for each alert episode, incidents shaded behind.
function timeline(t0, t1, incidents, alerts, rows) {
  const W = 1000, L = 190, R = 10, laneH = 24, T = 6, H = T + rows.length * laneH + 22;
  const x = (v) => L + ((v - t0) / (t1 - t0)) * (W - L - R);
  const bands = incidents.map((i) => {
    const a = Math.max(x(i.start_day), L), z = Math.min(x(i.end_day), W - R);
    const tipText = `${esc(i.slo_id)}<br>${i.kind}, ${fmtMin(i.minutes)}, ${pct(i.budget_consumed, 2)} of budget${i.significant ? "" : " (minor)"}`;
    return z >= L && a <= W - R ? `<rect x="${a}" y="${T}" width="${Math.max(2, z - a)}" height="${rows.length * laneH}"
      fill="${i.significant ? "var(--band)" : "var(--band-minor)"}" data-tip="${tipText}"/>` : "";
  }).join("");
  const lanes = rows.map((r, k) => {
    const y0 = T + k * laneH;
    const eps = alerts.filter((e) => e.strategy === r.strategy && r.severities.includes(e.severity));
    const marks = eps.map((e) => {
      const a = Math.max(x(e.start_day), L), z = Math.min(x(e.end_day), W - R);
      const color = e.true ? (e.severity === "ticket" ? "var(--ticket)" : "var(--good)") : "var(--critical)";
      const tipText = `${esc(r.label)}<br>${esc(e.slo_id)} ${e.severity}<br>${fmtDay(e.start_day)}, fired ${fmtMin(e.minutes)}<br>${e.true ? "true page" : e.incident ? "false: minor incident" : "false: no incident"}`;
      return `<rect x="${a}" y="${y0 + 5}" width="${Math.max(2, z - a)}" height="${laneH - 10}" fill="${color}" data-tip="${tipText}"/>`;
    }).join("");
    return `<line x1="${L}" x2="${W - R}" y1="${y0 + laneH}" y2="${y0 + laneH}" stroke="var(--grid)"/>
      <text x="4" y="${y0 + laneH / 2 + 4}" style="font-size:12px">${esc(r.short)}</text>${marks}`;
  }).join("");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => t0 + f * (t1 - t0)).map((v, i) =>
    `<text x="${x(v)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${fmtDay(v)}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="alert timeline">${bands}${lanes}${ticks}</svg>`;
}

const LANES = [
  { strategy: "static", severities: ["page"], short: "Static threshold", label: "Static error rate threshold" },
  { strategy: "single_window", severities: ["page"], short: "Single window 1h", label: "Single-window burn rate" },
  { strategy: "multiwindow", severities: ["page"], short: "MWMBR pages", label: "Multiwindow multi-burn-rate" },
  { strategy: "multiwindow", severities: ["ticket"], short: "MWMBR tickets", label: "Multiwindow multi-burn-rate" },
];
const timelineLegend = `<div class="legend">
  <span class="badge"><span class="dot" style="background:var(--good)"></span>True page</span>
  <span class="badge"><span class="dot" style="background:var(--ticket)"></span>True ticket</span>
  <span class="badge"><span class="dot" style="background:var(--critical)"></span>False page or ticket</span>
  <span class="badge"><span class="dot" style="background:var(--band)"></span>Budget-threatening incident</span>
  <span class="badge"><span class="dot" style="background:var(--band-minor)"></span>Minor incident (blip)</span></div>`;

async function sloPage(service, slo, from, to) {
  const q = from != null ? `?from_day=${from}&to_day=${to}` : "";
  const d = await get(`/api/slos/${service}/${slo}${q}`);
  document.getElementById("clock").textContent = `Showing ${fmtDay(d.from_day)} to ${fmtDay(d.to_day)}`;
  const bands = d.incidents.map((i) => ({ from: i.start_day, to: i.end_day, significant: i.significant,
    tip: `${i.kind}, ${fmtMin(i.minutes)}, ${pct(i.budget_consumed, 2)} of budget` }));
  const budget = lineChart("Error budget remaining (rolling " + d.window + ")", d.t, d.budget_series.map((v) => v * 100),
    { w: 1000, h: 220, lo: Math.min(0, ...d.budget_series.map((v) => v * 100)), hi: 100, fmt: (v) => `${v.toFixed(0)}%`,
      bands, lines: [{ y: 25, color: "var(--warning)", label: "25%: feature freeze" }] });
  const burn = Object.entries(d.burn_rate_max).map(([w, ys]) => {
    const seen = new Set();
    const lines = d.thresholds[w].filter((th) => {
      const k = th.burn_rate; if (seen.has(k)) return false; seen.add(k); return true;
    }).map((th) => ({ y: th.burn_rate, color: "var(--threshold)", label: `${th.burn_rate}x ${th.strategy === "static" ? "static" : th.severity}` }));
    const step = d.step_minutes > 1 ? `, max per ${fmtMin(d.step_minutes)}` : "";
    return lineChart(`Burn rate over ${w}${step}`, d.t, ys, { bands, lines, cap: Math.max(...lines.map((l) => l.y), 1) * 2.5, fmt: (v) => `${v.toFixed(v < 10 ? 1 : 0)}x` });
  }).join("");
  const rows = d.incidents.map((i) => {
    const pad = Math.max(0.25, (i.end_day - i.start_day) * 0.5);
    const z0 = Math.max(0, i.start_day - pad).toFixed(3), z1 = Math.min(90, i.end_day + pad).toFixed(3);
    const det = STRATS.map(([k]) => {
      const e = d.alerts.find((a) => a.strategy === k && a.incident === i.id);
      return `<td class="num">${e ? fmtMin((e.start_day - i.start_day) * 1440) : "missed"}</td>`;
    }).join("");
    return `<tr class="${i.significant ? "" : "hl"}"><td>${fmtDay(i.start_day)}</td><td>${i.kind}</td>
      <td class="num">${fmtMin(i.minutes)}</td><td class="num">${pct(i.budget_consumed, 2)}</td>
      <td>${i.significant ? "yes" : "no"}</td>${det}<td><a href="#/slo/${d.slo_id}/${z0}/${z1}">zoom</a></td></tr>`;
  }).join("");
  const zoomed = from != null ? ` <a href="#/slo/${d.slo_id}">show all 90 days</a>` : "";
  app.innerHTML = `
    <h1>${esc(d.service)} / ${esc(d.slo)}</h1>
    <div class="row muted">${d.sli_type} SLO, ${d.objective}% over ${d.window}, owner ${esc(d.owner)}, tier ${d.tier},
      deployed strategy ${d.deployed_strategy}. ${badge(d.status)}${zoomed}</div>
    <h2>Budget burn-down</h2>${budget}
    <h2>Burn rate per window <span class="muted">1x spends the budget exactly over the SLO window. Dashed lines are alert thresholds.</span></h2>
    <div class="charts">${burn}</div>
    <h2>When each strategy fired</h2>
    <div class="card">${timelineLegend}${timeline(d.from_day, d.to_day, d.incidents, d.alerts, LANES)}</div>
    <h2>Incidents (ground truth) <span class="muted">time to first page or ticket per strategy</span></h2>
    <div class="card scroll"><table><tr><th>Start</th><th>Kind</th><th class="num">Duration</th>
      <th class="num">Budget spent</th><th>Threatening</th><th class="num">Static</th><th class="num">Single window</th>
      <th class="num">MWMBR</th><th></th></tr>${rows || `<tr><td colspan="9" class="muted">No incidents in range</td></tr>`}</table></div>`;
  bindTips(app);
}

async function alertsPage() {
  const [ev, tl] = await Promise.all([get("/api/evaluation"), get("/api/timeline")]);
  document.getElementById("clock").textContent = `${ev.slos} SLOs, ${ev.days} days, seed ${ev.seed}`;
  const rows = ev.rows.map((r) => `<tr><td>${esc(r.label)}</td><td class="num">${r.notifications}</td>
    <td class="num">${r.false}</td><td class="num">${pct(r.precision, 0)}</td>
    <td class="num">${pct(r.recall, 0)} (${r.detected}/${r.significant_incidents})</td>
    <td class="num">${fmtMin(r.median_detection_min)}</td><td class="num">${fmtMin(r.median_reset_min)}</td>
    <td class="num">${r.per_week}</td></tr>`).join("");
  const keys = ["static+page", "single_window+page", "multiwindow+page", "multiwindow+page+ticket"];
  const kinds = ev.kinds.map((k) => `<tr><td>${k.kind}</td><td class="num">${k.count}</td><td class="num">${k.significant}</td>
    <td class="num">${pct(k.median_budget_consumed, 2)}</td>${keys.map((key) => {
      const b = k.by_row[key];
      return `<td class="num">${b.detected}/${b.of}${b.median_detection_min != null ? `, ${fmtMin(b.median_detection_min)}` : ""}<br><span class="muted">${b.notifications} notified</span></td>`;
    }).join("")}</tr>`).join("");
  const alerts = Object.values(tl.alerts).flat();
  app.innerHTML = `
    <h1>Alert strategy comparison</h1>
    <p class="muted">Every strategy replays the same 90 days for ${ev.slos} SLOs at 1 minute resolution. A notification is true
      when it starts during a budget-threatening incident (2% or more of the budget) or within an hour after it.
      Re-fires within an hour count as the same notification.</p>
    <div class="card scroll"><table><tr><th>Strategy</th><th class="num">Notifications</th><th class="num">False</th>
      <th class="num">Precision</th><th class="num">Recall</th><th class="num">Median detect</th>
      <th class="num">Median reset</th><th class="num">Per week</th></tr>${rows}</table></div>
    <h2>Detection by incident kind <span class="muted">detected / budget-threatening, median time to detect</span></h2>
    <div class="card scroll"><table><tr><th>Kind</th><th class="num">Count</th><th class="num">Threatening</th>
      <th class="num">Median budget spent</th><th class="num">Static</th><th class="num">Single window</th>
      <th class="num">MWMBR pages</th><th class="num">MWMBR pages + tickets</th></tr>${kinds}</table></div>
    <h2>Timeline <span class="muted">all SLOs, every notification over 90 days. Hover for details.</span></h2>
    <div class="card">${timelineLegend}${timeline(0, tl.days, tl.incidents, alerts, LANES)}</div>`;
  bindTips(app);
}

async function gatePage() {
  const d = await get("/api/slos");
  const services = [...new Set(d.slos.map((s) => s.service))];
  app.innerHTML = `
    <h1>Deploy gate</h1>
    <p class="muted">CI calls <code>GET /api/deploy-gate?service=...&change=feature</code> (or <code>sloforge gate</code>)
      before a deploy. The error budget policy in <code>examples/policy.yaml</code> decides.</p>
    <div class="card"><div class="row">
      <label>Service <select id="svc">${services.map((s) => `<option>${s}</option>`).join("")}</select></label>
      <label>Change <select id="chg"><option>feature</option><option>config</option><option>fix</option><option>rollback</option></select></label>
      <label>At day <input id="day" type="number" min="0" max="90" step="0.5" value="90" style="width:6em"></label>
      <button id="go">Check</button></div>
      <div id="out" style="margin-top:12px"></div></div>
    <h2>Every service at day 90 <span class="muted">feature deploys</span></h2>
    <div class="card scroll" id="all"></div>`;
  const run = async () => {
    const svc = document.getElementById("svc").value, chg = document.getElementById("chg").value;
    const day = document.getElementById("day").value;
    const r = await get(`/api/deploy-gate?service=${svc}&change=${chg}&day=${day}`);
    document.getElementById("out").innerHTML = `
      <div class="verdict ${r.allowed ? "allowed" : "blocked"}">${r.allowed ? "Allowed" : "Blocked"}</div>
      ${r.reasons.map((x) => `<div>${esc(x.policy)}: ${esc(x.slo_id)} has ${pct(x.budget_remaining)} budget left. ${esc(x.description)}</div>`).join("")}
      <div class="muted">${r.slos.map((s) => `${esc(s.slo_id)}: ${pct(s.budget_remaining)} left${s.page_firing ? ", page firing" : ""}`).join("; ")}</div>`;
  };
  document.getElementById("go").addEventListener("click", run);
  run();
  const all = await Promise.all(services.map((s) => get(`/api/deploy-gate?service=${s}&change=feature`)));
  document.getElementById("all").innerHTML = `<table><tr><th>Service</th><th>Feature deploy</th><th>Why</th></tr>
    ${all.map((r) => `<tr><td>${r.service}</td><td class="${r.allowed ? "allowed" : "blocked"}">${r.allowed ? "allowed" : "blocked"}</td>
    <td>${r.reasons.map((x) => `${esc(x.policy)} (${esc(x.slo_id)}, ${pct(x.budget_remaining)} left)`).join("; ")}</td></tr>`).join("")}</table>`;
}

async function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const name = parts[0] || "overview";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active",
    a.dataset.route === (name === "slo" ? "overview" : name)));
  tip.hidden = true;
  try {
    if (name === "slo") await sloPage(parts[1], parts[2], parts[3], parts[4]);
    else if (name === "alerts") await alertsPage();
    else if (name === "gate") await gatePage();
    else await overview();
  } catch (e) {
    app.innerHTML = `<p class="blocked">Could not load: ${esc(e.message)}</p>`;
  }
}

window.addEventListener("hashchange", route);
route();
