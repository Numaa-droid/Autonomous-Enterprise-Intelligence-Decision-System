/* AEIDS Console: overview, documents, search, evaluation, traces (LangSmith-style), logs.
   Plain JS, no build step. Data comes from /api/v1/dashboard. Document text, trace inputs/outputs
   and log lines are untrusted: they are only ever inserted as text (textContent), never as HTML. */
"use strict";

const API = "/api/v1/dashboard";
const $ = (sel, root = document) => root.querySelector(sel);
const state = { tenant: null, defaultTenant: "default", tenants: [], timer: null, pageTimer: null, current: null, models: {} };

/* ---------------- icons (constant markup only) ---------------- */
const P = {
  overview: '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v10h13V10"/><path d="M10 20v-5.5h4V20"/>',
  documents: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  evaluation: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  traces: '<path d="M3 6h7M6 12h9M9 18h12"/><circle cx="12" cy="6" r="1.6"/><circle cx="17" cy="12" r="1.6"/>',
  logs: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  x: '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
  spinner: '<path d="M12 3a9 9 0 1 0 9 9"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
  download: '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
  lens: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9h18M9 9v12"/>',
  chunks: '<rect x="3" y="3" width="8" height="8" rx="2"/><rect x="13" y="3" width="8" height="8" rx="2"/><rect x="3" y="13" width="8" height="8" rx="2"/><rect x="13" y="13" width="8" height="8" rx="2"/>',
  embed: '<circle cx="6" cy="6" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="8" cy="18" r="2"/><circle cx="17" cy="17" r="2"/><path d="M8 7l8 0M7 8l1 8M10 18l5-1M17 9l0 6"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  sparkle: '<path d="M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2z"/><path d="M19 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
  coin: '<circle cx="12" cy="12" r="9"/><path d="M15 9.5c-.6-1-1.7-1.5-3-1.5-1.7 0-3 .9-3 2s1.3 1.7 3 2 3 .9 3 2-1.3 2-3 2c-1.3 0-2.5-.5-3-1.5M12 6v2M12 16v2"/>',
  bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  alert: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17.5v.5"/>',
  right: '<path d="m9 6 6 6-6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  back: '<path d="M15 6l-6 6 6 6"/>',
  workflows: '<rect x="2.5" y="4" width="6" height="5" rx="1.5"/><rect x="15.5" y="4" width="6" height="5" rx="1.5"/><rect x="9" y="15" width="6" height="5" rx="1.5"/><path d="M8.5 6.5h7M5.5 9v3.5h6.5V15M18.5 9v3.5H12"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
};
function icon(name) {
  const t = document.createElement("template");
  t.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ""}</svg>`;
  return t.content.firstChild;
}

/* ---------------- DOM helpers ---------------- */
function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (k === "vars") for (const [n, val] of Object.entries(v)) node.style.setProperty(n, val);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  append(node, kids);
  return node;
}
function append(node, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}
const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, ...kids) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid) node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return node;
}
function card(title, sub, ...body) {
  return el("div", { class: "card fade-in" },
    title ? el("div", { class: "card-head" }, el("h2", {}, title), sub ? el("span", { class: "sub" }, sub) : null) : null,
    ...body);
}
function empty(iconName, title, text) {
  return el("div", { class: "empty" }, icon(iconName), el("b", {}, title), el("div", {}, text));
}

/* ---------------- formatting ---------------- */
const nf = new Intl.NumberFormat();
const fmtInt = (n) => nf.format(Math.round(Number(n) || 0));
function fmtMs(ms) {
  if (ms == null) return "–";
  if (ms < 1) return "<1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}
function fmtCost(v) {
  const n = Number(v) || 0;
  if (n === 0) return "$0";
  if (n < 0.0001) return "<$0.0001";
  return "$" + n.toFixed(n < 0.01 ? 4 : n < 1 ? 3 : 2);
}
function fmtBytes(b) {
  if (!b) return "–";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; let n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}
function relTime(iso) {
  if (!iso) return "–";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const fmtDate = (iso) => iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "–";
const pct = (v) => v == null ? "–" : `${Math.round(v * 100)}%`;

/* ---------------- colour roles ---------------- */
const KIND = {
  retrieval: ["--s1", "Search"], embedding: ["--s2", "AI embedding"], vectordb: ["--s3", "Search index"],
  tool: ["--s4", "PDF reader"], llm: ["--s5", "AI model"], index: ["--s6", "Indexing"], job: ["--s7", "Background job"],
  parser: ["--neutral", "Reading PDF"], eval: ["--neutral", "Quality test"], chain: ["--neutral", "Step"],
};
const kindVar = (k) => `var(${(KIND[k] || KIND.chain)[0]})`;
const kindLabel = (k) => (KIND[k] || [null, k])[1];
const kindBadge = (k) => el("span", { class: "badge kind", vars: { "--c": kindVar(k) } }, kindLabel(k));
const STATUS = {
  pending: ["muted", "clock", "Waiting", "var(--neutral)"],
  parsing: ["progress", "spinner", "Reading", "var(--s1)"],
  parsed: ["muted", "check", "Read", "var(--s3)"],
  indexing: ["progress", "spinner", "Making searchable", "var(--s7)"],
  indexed: ["good", "check", "Ready", "var(--good)"],
  failed: ["critical", "x", "Failed", "var(--critical)"],
};
function statusTag(status) {
  const [cls, ic, label] = STATUS[status] || ["muted", "clock", status];
  return el("span", { class: `status ${cls}` }, icon(ic), label);
}
function runStatus(status) {
  return status === "error" ? el("span", { class: "status critical" }, icon("x"), "Error")
    : el("span", { class: "status good" }, icon("check"), "OK");
}

/* ---------------- tooltip ---------------- */
const tip = $("#tooltip");
function showTip(evt, title, rows) {
  tip.replaceChildren(el("div", { class: "t-title" }, title),
    ...rows.map(([k, v]) => el("div", { class: "t-row" }, el("span", {}, k), el("b", {}, v))));
  tip.style.opacity = "1";
  moveTip(evt);
}
function moveTip(evt) {
  const r = tip.getBoundingClientRect();
  let x = evt.clientX + 14, y = evt.clientY + 14;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - 14;
  tip.style.left = `${x}px`; tip.style.top = `${y}px`;
}
const hideTip = () => { tip.style.opacity = "0"; };
function hover(node, title, rows) {
  node.addEventListener("mouseenter", (e) => showTip(e, typeof title === "function" ? title() : title, rows()));
  node.addEventListener("mousemove", moveTip);
  node.addEventListener("mouseleave", hideTip);
  return node;
}

/* ---------------- API ---------------- */
function errorText(body, status) {
  const msg = body?.error?.message || body?.detail?.[0]?.msg || body?.detail || `HTTP ${status}`;
  return typeof msg === "string" ? msg : JSON.stringify(msg);
}
async function api(path, opts) {
  const res = await fetch(path.startsWith("/api/") ? path : API + path, opts); // dashboard paths are relative
  let body = null;
  try { body = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new Error(errorText(body, res.status));
  return body;
}
const tq = () => state.tenant ? `tenant=${encodeURIComponent(state.tenant)}` : "";
function toast(message) {
  const t = el("div", { class: "toast", role: "alert" }, el("b", {}, "Something went wrong"), el("div", {}, message));
  document.body.append(t);
  setTimeout(() => t.remove(), 6000);
}

/* ---------------- charts ---------------- */
function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
function topRounded(x, y, w, h, r = 4) {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}
function responsive(container, draw) {
  let last = 0;
  const ro = new ResizeObserver(() => {
    const w = Math.floor(container.clientWidth);
    if (w && w !== last) { last = w; container.replaceChildren(draw(w)); }
  });
  ro.observe(container);
}
/* columns over time: one series; hover tooltip per column */
function columnChart(container, data, { value, label, color = "var(--brand-mark)", height = 200, fmt = fmtInt, tipRows }) {
  responsive(container, (W) => {
    const m = { t: 10, r: 6, b: 24, l: 40 };
    const iw = W - m.l - m.r, ih = height - m.t - m.b;
    const values = data.map(value);
    let step = niceMax(Math.max(...values, 0) / 4);
    if (values.every(Number.isInteger)) step = Math.max(1, Math.ceil(step)); // counts: whole steps
    const max = step * 4;
    const y = (v) => m.t + ih - (v / max) * ih;
    const band = iw / data.length, bw = Math.max(4, Math.min(28, band - 6));
    const root = svg("svg", { viewBox: `0 0 ${W} ${height}`, height, role: "img" });
    for (let i = 0; i <= 4; i++) {
      const v = (max / 4) * i;
      root.append(svg("line", { class: i ? "gridline" : "baseline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }),
        svg("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" }, fmt(v)));
    }
    data.forEach((d, i) => {
      const x = m.l + band * i + (band - bw) / 2;
      const v = value(d);
      const mark = svg("path", { class: "mark", d: topRounded(x, y(v), bw, m.t + ih - y(v)), style: { fill: color } });
      const hit = svg("rect", { class: "hit", x: m.l + band * i, y: m.t, width: band, height: ih });
      hover(hit, label(d, true), () => tipRows(d));
      hit.addEventListener("mouseenter", () => mark.classList.add("hl"));
      hit.addEventListener("mouseleave", () => mark.classList.remove("hl"));
      root.append(mark, hit);
      if ((data.length - 1 - i) % Math.ceil(data.length / 7) === 0) // every few days, ending today
        root.append(svg("text", { x: m.l + band * i + band / 2, y: height - 6, "text-anchor": "middle" }, label(d, false)));
    });
    return root;
  });
}
/* grouped columns: groups x series, fixed 0..1 scale (scores) */
function groupedChart(container, groups, series, { height = 230 } = {}) {
  responsive(container, (W) => {
    const m = { t: 18, r: 6, b: 26, l: 40 };
    const iw = W - m.l - m.r, ih = height - m.t - m.b;
    const y = (v) => m.t + ih - v * ih;
    const band = iw / groups.length;
    const bw = Math.max(8, Math.min(34, (band - 28) / series.length - 2));
    const root = svg("svg", { viewBox: `0 0 ${W} ${height}`, height, role: "img" });
    for (let i = 0; i <= 4; i++) {
      root.append(svg("line", { class: i ? "gridline" : "baseline", x1: m.l, x2: W - m.r, y1: y(i / 4), y2: y(i / 4) }),
        svg("text", { x: m.l - 8, y: y(i / 4) + 4, "text-anchor": "end" }, `${i * 25}%`));
    }
    groups.forEach((g, gi) => {
      const total = series.length * bw + (series.length - 1) * 2;
      const x0 = m.l + band * gi + (band - total) / 2;
      series.forEach((s, si) => {
        const v = s.values[gi] ?? 0;
        const x = x0 + si * (bw + 2);
        const mark = svg("path", { class: "mark", d: topRounded(x, y(v), bw, m.t + ih - y(v)), style: { fill: s.color } });
        const hit = svg("rect", { class: "hit", x: x - 1, y: m.t, width: bw + 2, height: ih });
        hover(hit, `${s.name} · ${g}`, () => [["score", pct(v)]]);
        hit.addEventListener("mouseenter", () => mark.classList.add("hl"));
        hit.addEventListener("mouseleave", () => mark.classList.remove("hl"));
        root.append(mark, hit);
        if (s.label) root.append(svg("text", { x: x + bw / 2, y: y(v) - 5, "text-anchor": "middle",
          style: { fill: "var(--ink)", fontWeight: 600 } }, pct(v)));
      });
      root.append(svg("text", { x: m.l + band * gi + band / 2, y: height - 6, "text-anchor": "middle",
        style: { fill: "var(--ink-2)" } }, g));
    });
    return root;
  });
}
/* donut: part-to-whole of a handful of states, 2px surface gaps */
function donut(items, { size = 150, thickness = 22, centre, centreSub }) {
  const total = items.reduce((a, b) => a + b.value, 0);
  const r = size / 2 - 2, rin = r - thickness, c = size / 2;
  const root = svg("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: "img" });
  if (!total) root.append(svg("circle", { cx: c, cy: c, r: r - thickness / 2, fill: "none", stroke: "var(--surface-3)", "stroke-width": thickness }));
  let a0 = -Math.PI / 2;
  for (const it of items) {
    if (!it.value) continue;
    const a1 = a0 + (it.value / total) * Math.PI * 2;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const pt = (rad, a) => `${c + rad * Math.cos(a)},${c + rad * Math.sin(a)}`;
    const d = it.value === total
      ? `M${c},${c - r}A${r},${r} 0 1 1 ${c - 0.01},${c - r}L${c - 0.01},${c - rin}A${rin},${rin} 0 1 0 ${c},${c - rin}Z`
      : `M${pt(r, a0)}A${r},${r} 0 ${large} 1 ${pt(r, a1)}L${pt(rin, a1)}A${rin},${rin} 0 ${large} 0 ${pt(rin, a0)}Z`;
    const seg = svg("path", { d, style: { fill: it.color, stroke: "var(--surface)", strokeWidth: 2 } });
    hover(seg, it.label, () => [["versions", fmtInt(it.value)], ["share", pct(it.value / total)]]);
    root.append(seg);
    a0 = a1;
  }
  root.append(svg("text", { x: c, y: c + 2, "text-anchor": "middle", style: { fill: "var(--ink)", fontSize: "24px", fontWeight: 700 } }, centre),
    svg("text", { x: c, y: c + 20, "text-anchor": "middle", style: { fill: "var(--muted)", fontSize: "11px" } }, centreSub));
  return root;
}

/* ---------------- navigation ---------------- */
const PAGES = [
  ["overview", "Home", "What AEIDS does and how to start", "main"],
  ["workflows", "Ask a question", "Ask about the reports; every number is checked before you see it", "main"],
  ["documents", "Reports", "Upload company reports and see when each one is ready", "main"],
  ["search", "Search", "Find the passages in the reports that mention something", "main"],
  ["evaluation", "Search quality", "How often search finds the right page, on a fixed set of test questions", "tech"],
  ["traces", "Run history", "Every run, step by step: time taken, AI usage and cost", "tech"],
  ["logs", "System logs", "Raw output of the background services", "tech"],
];
function buildNav() {
  const firstTech = PAGES.findIndex((p) => p[3] === "tech");
  $("#nav").replaceChildren(...PAGES.flatMap(([id, label], i) => [
    i === firstTech ? el("div", { class: "nav-label" }, "For engineers") : null,
    el("a", { href: `#/${id}`, "data-page": id }, icon(id), label)].filter(Boolean)));
}
function route() {
  const [page = "overview", arg] = location.hash.replace(/^#\/?/, "").split("/");
  return { page: PAGES.some(([id]) => id === page) ? page : "overview", arg: arg ? decodeURIComponent(arg) : null };
}
async function render(soft = false) {
  if (!soft) { hideTip(); clearTimeout(state.pageTimer); } // a tooltip over a replaced mark would stay open
  const { page, arg } = route();
  const meta = PAGES.find(([id]) => id === page);
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  $("#page-title").textContent = arg && page === "traces" ? "Run details" : arg && page === "workflows" ? "Your question" : meta[1];
  $("#page-crumb").textContent = arg ? (page === "traces" ? `Run ${arg.slice(0, 12)}…` : "The answer, and how it was checked") : meta[2];
  if (soft && state.current?.refresh) { try { await state.current.refresh(); } catch (e) { /* keep page */ } return; }
  // quick clicks (page, tenant, theme) can start several renders: only the newest may touch the page
  const seq = (state.renderSeq = (state.renderSeq || 0) + 1);
  const view = $("#view");
  const holder = el("div", { style: { display: "contents" } });
  view.replaceChildren(holder);
  holder.append(el("div", { class: "skeleton" }), el("div", { class: "skeleton" }));
  const views = { overview: viewOverview, documents: viewDocuments, search: viewSearch, evaluation: viewEvaluation,
    traces: arg ? viewTrace : viewTraces, logs: viewLogs, workflows: arg ? viewWorkflow : viewWorkflows };
  try {
    const target = el("div", { style: { display: "contents" } });
    holder.append(target);
    const current = (await views[page](target, arg)) || null;
    if (seq !== state.renderSeq) return; // a newer render owns the page now
    holder.querySelectorAll(":scope > .skeleton").forEach((n) => n.remove());
    state.current = current;
  } catch (e) {
    if (seq === state.renderSeq) view.replaceChildren(card(null, null, empty("alert", "Could not load this page", e.message)));
  }
}

/* ---------------- overview ---------------- */
async function viewOverview(root) {
  const [ov, sv, asked] = await Promise.all([api(`/overview?${tq()}`), api("/services").catch(() => ({})),
    api(`/workflows?${tq()}`).catch(() => [])]);
  const d = ov.documents, t = ov.totals_7d;
  state.models = ov.models;
  $("#model-chip").replaceChildren(el("b", {}, "AI models"), `Search: ${ov.models.embedding}`, el("br"), `Answers: ${ov.models.llm}`);

  // plain health line: one sentence for everyone; the per-service pills sit in the technical section
  const svcNames = { database: "Database", qdrant: "Search index", redis: "Job queue", tablelens: "PDF reader", openrouter: "AI models" };
  const techNames = { database: "Postgres", qdrant: "Qdrant", redis: "Redis", tablelens: "TableLens", openrouter: "OpenRouter" };
  const down = Object.keys(svcNames).filter((k) => sv[k] && !sv[k].ok);
  const known = Object.keys(svcNames).some((k) => sv[k]);
  const health = !known ? null : down.length
    ? el("div", { class: "health critical", role: "status" }, icon("x"),
      `Not everything is running: ${down.map((k) => svcNames[k]).join(", ")}. Ask an engineer to run ./start.sh.`)
    : el("div", { class: "health good", role: "status" }, icon("check"), "Everything is running");
  const hero = el("div", { class: "card hero fade-in" },
    el("h2", {}, "Welcome to AEIDS"),
    el("p", {}, "Upload company reports, ask a question in plain English, and get an answer in which every number is ",
      "checked against the page it came from. AEIDS supports decisions; it does not replace a financial, legal or compliance review."),
    health);

  const ready = d.by_status.indexed || 0;
  const step = (n, title, text, href, label) => el("div", { class: "how-step" },
    el("div", { class: "num" }, String(n)), el("h3", {}, title), el("p", {}, text),
    href ? el("a", { class: n === 2 ? "btn primary" : "btn", href }, label) : null);
  const how = card("How it works", null, el("div", { class: "how-steps" },
    step(1, "Upload the reports", "Add annual or ESG reports as PDF files. AEIDS reads the text and the tables; this takes a few minutes per report.",
      "#/documents", d.documents ? "See the reports" : "Upload a report"),
    step(2, "Ask a question", "For example: “What was Apple's operating margin in 2025?” or “Compare revenue growth of Microsoft and Nvidia.”",
      "#/workflows", "Ask a question"),
    step(3, "Read the checked answer", "Each number shows the report and page it came from, and any sums are done by a calculator, not by the AI. Anything that could not be checked is left out, and the answer says so.",
      null, null)));

  const kpi = (ic, label, value, foot) => el("div", { class: "card kpi fade-in" },
    el("div", { class: "label" }, icon(ic), label), el("div", { class: "value tnum" }, value), el("div", { class: "foot" }, foot));
  const failedCount = d.by_status.failed || 0;
  const simple = el("div", { class: "grid g-kpi", style: { gridTemplateColumns: "repeat(4, minmax(0, 1fr))" } },
    kpi("documents", "Reports", fmtInt(d.documents), `from ${fmtInt(d.companies)} ${d.companies === 1 ? "company" : "companies"}`),
    kpi("check", "Ready to use", fmtInt(ready), d.versions ? `of ${fmtInt(d.versions)} uploaded file${d.versions === 1 ? "" : "s"}` : "nothing uploaded yet"),
    kpi("workflows", "Questions asked", fmtInt(asked.length), asked.length ? `last one ${relTime(asked[0].created_at)}` : "none yet"),
    kpi("alert", "Need attention", fmtInt(failedCount), failedCount ? "could not be read; see Reports" : "nothing to fix"));

  const failures = d.failures?.length ? card("Reports that could not be read", "newest first",
    d.failures.map((f) => el("div", { class: "error-box" }, el("b", {}, f.file), el("div", {}, f.error || "failed"),
      el("div", { class: "muted small" }, fmtDate(f.at))))) : null;

  // ---- technical details (folded): the engineer's view of the same tenant ----
  const pills = el("div", { class: "services" }, Object.entries(techNames).map(([k, name]) => {
    const s = sv[k];
    if (!s) return null;
    return el("span", { class: `pill ${s.ok ? "good" : "critical"}`, title: s.detail },
      icon(s.ok ? "check" : "x"), el("b", {}, name), s.ok ? s.detail : `down (${s.detail})`);
  }));
  const kinds = d.chunk_kinds || {};
  const kpis = el("div", { class: "grid g-kpi" },
    kpi("chunks", "Searchable chunks", fmtInt(d.chunks), `${fmtInt(kinds.table || 0)} tables · ${fmtInt(kinds.chart || 0)} charts`),
    kpi("traces", "Runs (7 days)", fmtInt(t.traces), `${fmtInt(t.spans)} steps · ${fmtInt(t.errors)} failed`),
    kpi("sparkle", "Model calls (7 days)", fmtInt(t.llm_calls + t.embedding_calls), `${fmtInt(t.tokens)} tokens`),
    kpi("coin", "Spend (7 days)", fmtCost(t.cost_usd), "OpenRouter, as reported"),
    kpi("database", "Indexed vectors", fmtInt(d.indexed_points), `${fmtInt(d.versions)} versions`));

  const parsed = (d.by_status.parsed || 0) + (d.by_status.indexing || 0) + (d.by_status.indexed || 0);
  const stages = [
    ["upload", "Upload", "PDF stored, checksum dedupe", fmtInt(d.versions)],
    ["lens", "TableLens", "Layout + fine-tuned table model", fmtInt(parsed)],
    ["chunks", "Normalise & chunk", "Units, scale, periods; sections", fmtInt(d.chunks)],
    ["embed", "Embed", (ov.models.embedding || "").split("/").pop(), fmtInt(t.embedding_calls) + " calls"],
    ["database", "Qdrant index", "Verified, then made current", fmtInt(d.indexed_points)],
    ["search", "Hybrid search", "Meaning + words, RRF", fmtInt(t.searches || 0) + " / 7d"],
  ];
  const pipeline = card("Ingestion pipeline", "documents flow left to right; counts for this workspace",
    el("div", { class: "pipeline" }, stages.map(([ic, name, what, count]) =>
      el("div", { class: "stage", vars: { "--c": "var(--accent)" } },
        el("div", { class: "orb" }, icon(ic)), el("div", { class: "name" }, name),
        el("div", { class: "what" }, what), el("div", { class: "count tnum" }, count)))));

  // activity: one measure at a time (no dual axis)
  const measures = {
    runs: ["Runs", (x) => x.traces, fmtInt], tokens: ["Tokens", (x) => x.tokens, (v) => v >= 1000 ? `${Math.round(v / 1000)}k` : fmtInt(v)],
    cost: ["Spend", (x) => Number(x.cost), (v) => v === 0 ? "$0" : `$${v.toFixed(v < 0.01 ? 4 : 2)}`],
  };
  let measure = "runs";
  const actBox = el("div", { class: "chart" });
  const seg = el("div", { class: "seg" });
  const drawActivity = () => {
    seg.replaceChildren(...Object.entries(measures).map(([k, [name]]) =>
      el("button", { class: k === measure ? "on" : "", type: "button", onclick: () => { measure = k; drawActivity(); } }, name)));
    const [, value, fmt] = measures[measure];
    actBox.replaceChildren();
    columnChart(actBox, ov.activity, {
      value, fmt, color: "var(--brand-mark)",
      label: (x, long) => long ? new Date(x.date + "T00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })
        : new Date(x.date + "T00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      tipRows: (x) => [["runs", fmtInt(x.traces)], ["steps", fmtInt(x.spans)], ["failed runs", fmtInt(x.errors)], ["tokens", fmtInt(x.tokens)], ["spend", fmtCost(x.cost)]],
    });
  };
  const activity = el("div", { class: "card fade-in" },
    el("div", { class: "card-head" }, el("h2", {}, "Activity"), el("span", { class: "sub" }, "last 14 days"), el("div", { class: "right" }, seg)),
    actBox);

  const maxP95 = Math.max(1, ...ov.latency.map((l) => l.p95));
  const latency = card("Time per step", "median bar, 95th percentile tick",
    ov.latency.length ? ov.latency.map((l) => hover(el("div", { class: "latency-row" },
      el("span", {}, kindBadge(l.kind)),
      el("div", { class: "track" }, el("div", { class: "bar", style: { width: `${(l.p50 / maxP95) * 100}%` } }),
        el("div", { class: "p95", style: { left: `calc(${(l.p95 / maxP95) * 100}% - 1px)` } })),
      el("span", { class: "tnum small muted", style: { textAlign: "right" } }, fmtMs(l.p50))),
      kindLabel(l.kind), () => [["median", fmtMs(l.p50)], ["95th pct", fmtMs(l.p95)], ["steps", fmtInt(l.count)]]))
      : empty("clock", "No runs yet", "Process a document or run a search."));

  const statusItems = Object.entries(STATUS).map(([k, [, ic, label, color]]) => ({ key: k, label, value: d.by_status[k] || 0, color, ic }));
  const chunkKinds = [["text", "Text", "var(--series-brand)"], ["table", "Tables", "var(--s1)"], ["chart", "Charts", "var(--s5)"]];
  const kindsTotal = Math.max(1, d.chunks);
  const status = card("Files by state", null,
    el("div", { class: "donut-wrap" },
      donut(statusItems, { centre: fmtInt(d.versions), centreSub: "files" }),
      el("div", { class: "legend" }, statusItems.filter((s) => s.value || ["indexed", "failed"].includes(s.key)).map((s) =>
        el("span", {}, el("i", { style: { background: s.color } }), statusTag(s.key), el("span", { class: "muted tnum" }, ` ${fmtInt(s.value)}`))))),
    el("div", { style: { marginTop: "18px" } },
      el("div", { class: "small muted", style: { marginBottom: "6px" } }, "Chunks by kind"),
      el("div", { class: "stack" }, chunkKinds.filter(([k]) => kinds[k]).map(([k, label, c]) =>
        hover(el("span", { style: { width: `${(kinds[k] / kindsTotal) * 100}%`, background: c } }), label, () => [["chunks", fmtInt(kinds[k])], ["share", pct(kinds[k] / kindsTotal)]]))),
      el("div", { class: "legend", style: { marginTop: "8px" } }, chunkKinds.map(([k, label, c]) =>
        el("span", {}, el("i", { style: { background: c } }), `${label} ${fmtInt(kinds[k] || 0)}`)))));

  const recent = card("Recent runs", null, traceTable(ov.recent_traces, true));

  let opened = false;
  try { opened = localStorage.getItem("aeids.techOpen") === "1"; } catch (e) { /* storage unavailable */ }
  const tech = el("details", { class: "card how", ...(opened ? { open: true } : {}),
    ontoggle: (e) => { try { localStorage.setItem("aeids.techOpen", e.target.open ? "1" : "0"); } catch (err) { /* ignore */ } } },
    el("summary", {}, el("b", {}, "Technical details"), el("span", { class: "muted small" }, " · services, pipeline, activity, speed and cost (for engineers)")),
    el("div", { class: "grid", style: { gap: "20px" } }, pills, kpis, pipeline,
      el("div", { class: "grid g-3" }, activity, latency, status), recent));

  append(root, [hero, how, simple, failures, tech]);
  drawActivity();
}

/* ---------------- documents ---------------- */
const IN_PROGRESS = ["pending", "parsing", "indexing"];
const PROCESSING_NOTE = {
  queued: ["info", "Uploaded. AEIDS is now reading it (text and tables) and making it searchable. This usually takes a few minutes; this page updates by itself."],
  already_queued: ["info", "Uploaded. It is already waiting its turn to be read."],
  done: ["good", "Uploaded and ready. You can ask questions about it now."],
  failed: ["critical", "Uploaded, but AEIDS could not read it. The reason is shown below; press Try again."],
  not_queued: ["warning", "Uploaded, but the background service that reads reports is not running, so nothing has started. Ask an engineer to run ./start.sh, then press Try again."],
  off: ["warning", "Uploaded. Automatic reading is turned off (AEIDS_JOBS_MODE=off); press Try again to start it."],
};
function notice(kind, ...text) {
  const ic = { good: "check", critical: "x", warning: "alert", info: "clock" }[kind] || "clock";
  return el("div", { class: `notice ${kind}`, role: kind === "critical" ? "alert" : "status" }, icon(ic), el("div", {}, ...text));
}
function uploadWithProgress(url, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(errorText(body, xhr.status)));
    };
    xhr.onerror = () => reject(new Error("The API did not answer. Is AEIDS running (./start.sh)?"));
    xhr.send(form);
  });
}
function uploadOutcome(out) {
  if (out.duplicate) {
    return notice("info", el("b", {}, "Already stored. "),
      `This exact file is already uploaded (version ${out.version.version_number} of ${out.document.company_name}'s ${out.document.filename}). Nothing new was added.`);
  }
  const [kind, text] = PROCESSING_NOTE[out.processing] || ["info", `Uploaded (${out.processing}).`];
  return notice(kind, el("b", {}, `${out.version.filename} · version ${out.version.version_number}. `), text);
}
function progressBar() {
  const fill = el("span");
  const label = el("span", { class: "small muted tnum" });
  const box = el("div", { class: "upload-progress", hidden: true }, el("div", { class: "bar" }, fill), label);
  return {
    box,
    set(frac, text) { box.hidden = false; fill.style.width = `${Math.round(frac * 100)}%`; label.textContent = text; },
    busy(text) { box.hidden = false; fill.style.width = "100%"; fill.classList.add("busy"); label.textContent = text; },
    hide() { box.hidden = true; fill.classList.remove("busy"); },
  };
}
const guessYear = (name) => (name.match(/(?:^|[^0-9])((?:19|20)\d\d)(?:[^0-9]|$)/) || [])[1] || "";

function uploadPanel(companies, onUploaded) {
  let file = null;
  const fileInput = el("input", { type: "file", accept: "application/pdf,.pdf", hidden: true });
  const fileLabel = el("div", { class: "drop-text" }, el("b", {}, "Drag a PDF here"), " or click to choose one");
  const drop = el("label", { class: "dropzone", tabindex: "0", role: "button", "aria-label": "Choose a PDF to upload" },
    icon("upload"), fileLabel, fileInput);
  const company = el("input", { class: "input", list: "company-list", placeholder: "e.g. Apple Inc.", maxlength: "300", required: true, "aria-label": "Company" });
  const datalist = el("datalist", { id: "company-list" }, companies.map((c) => el("option", { value: c })));
  const type = el("select", { class: "select", "aria-label": "Document type" },
    [["annual_report", "Annual report"], ["esg_report", "ESG report"], ["brsr", "BRSR"], ["other", "Other"]]
      .map(([v, l]) => el("option", { value: v }, l)));
  const year = el("input", { class: "input", type: "number", min: "1900", max: "2100", placeholder: "e.g. 2025", "aria-label": "Year", style: { width: "120px" } });
  const submit = el("button", { class: "btn primary", type: "submit", disabled: true }, icon("upload"), "Upload");
  const progress = progressBar();
  const result = el("div");

  const ready = () => { submit.disabled = !(file && company.value.trim()); };
  const choose = (f) => {
    result.replaceChildren();
    if (!f) return;
    if (!/\.pdf$/i.test(f.name) && f.type !== "application/pdf") {
      file = null; fileLabel.replaceChildren(el("b", {}, "Not a PDF: "), f.name); drop.classList.add("bad"); ready(); return;
    }
    file = f;
    drop.classList.remove("bad"); drop.classList.add("has-file");
    fileLabel.replaceChildren(el("b", {}, f.name), ` · ${fmtBytes(f.size)} · click to change`);
    if (!year.value) year.value = guessYear(f.name);
    ready();
  };
  fileInput.addEventListener("change", () => choose(fileInput.files[0]));
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => choose(e.dataTransfer.files[0]));
  company.addEventListener("input", ready);

  const form = el("form", { class: "upload-form", onsubmit: async (e) => {
    e.preventDefault();
    if (!file || !company.value.trim()) return;
    const data = new FormData();
    data.append("file", file);
    data.append("company_name", company.value.trim());
    data.append("document_type", type.value);
    if (year.value) data.append("fiscal_year", year.value);
    submit.disabled = true;
    result.replaceChildren();
    try {
      const out = await uploadWithProgress("/api/v1/documents", data, (f) =>
        f < 1 ? progress.set(f, `Uploading ${Math.round(f * 100)}%`) : progress.busy("Saved; starting to read it…"));
      progress.hide();
      result.replaceChildren(uploadOutcome(out));
      file = null; fileInput.value = "";
      drop.classList.remove("has-file");
      fileLabel.replaceChildren(el("b", {}, "Drag another PDF here"), " or click to choose one");
      await onUploaded(out);
    } catch (err) {
      progress.hide();
      result.replaceChildren(notice("critical", el("b", {}, "Upload failed. "), err.message));
    } finally { ready(); }
  } },
    drop,
    el("div", { class: "upload-fields" },
      el("label", { class: "field grow" }, el("span", {}, "Company"), company, datalist),
      el("label", { class: "field" }, el("span", {}, "Document type"), type),
      el("label", { class: "field" }, el("span", {}, "Year (optional)"), year),
      el("div", { class: "field end" }, submit)),
    progress.box, result);

  const other = state.tenant && state.tenant !== state.defaultTenant;
  return el("div", { class: "card hero fade-in" },
    el("div", { class: "row" }, el("h2", { style: { margin: 0 } }, "Upload a report"),
      el("span", { class: "small muted" }, "A PDF annual or ESG report. AEIDS reads its text and tables and makes it searchable.")),
    other ? notice("info", `You are looking at the "${state.tenant}" workspace. Uploads go to your own workspace ("${state.defaultTenant}"); the page switches there after uploading.`) : null,
    form);
}

function versionUploadButton(doc, onUploaded) {
  const input = el("input", { type: "file", accept: "application/pdf,.pdf", hidden: true });
  const progress = progressBar();
  const result = el("div");
  input.addEventListener("change", async () => {
    const f = input.files[0];
    if (!f) return;
    const data = new FormData();
    data.append("file", f);
    result.replaceChildren();
    try {
      const out = await uploadWithProgress(`/api/v1/documents/${encodeURIComponent(doc.id)}/versions`, data, (fr) =>
        fr < 1 ? progress.set(fr, `Uploading ${f.name} ${Math.round(fr * 100)}%`) : progress.busy("Saved; starting to read it…"));
      progress.hide();
      result.replaceChildren(uploadOutcome(out));
      await onUploaded(out);
    } catch (err) {
      progress.hide();
      result.replaceChildren(notice("critical", el("b", {}, "Upload failed. "), err.message));
    } finally { input.value = ""; }
  });
  const button = el("button", { class: "btn", type: "button", title: "The old version stays in use until the new one is ready",
    onclick: () => input.click() }, icon("upload"), "Upload a newer version");
  return { button, input, below: el("div", {}, progress.box, result) };
}

async function processAgain(doc, v, btn, out) {
  btn.disabled = true;
  try {
    const res = await api(`/api/v1/documents/${encodeURIComponent(doc.id)}/versions/${encodeURIComponent(v.id)}/process`, { method: "POST" });
    const [kind, text] = PROCESSING_NOTE[res.processing] || ["info", res.processing];
    out.replaceChildren(notice(kind, text));
  } catch (e) {
    out.replaceChildren(notice("critical", e.message));
  } finally { btn.disabled = false; }
}

async function viewDocuments(root) {
  const list = el("div", { class: "grid", style: { gap: "20px" } });
  // kept across redraws (the list redraws every few seconds while something is processing):
  // a document's upload controls (progress, message) and each version's retry message
  const uploaders = new Map();
  const retryNotes = new Map();
  let panelShown = false;
  let docs = [];
  const load = async () => {
    try { docs = await api(`/documents?${tq()}`); }
    catch (e) { if (/no tenant/i.test(e.message)) docs = []; else throw e; }
    if (!panelShown) {
      root.prepend(uploadPanel([...new Set(docs.map((d) => d.company))], afterUpload));
      panelShown = true;
    }
    drawList();
    clearTimeout(state.pageTimer);
    const recent = (v) => Date.now() - new Date(v.uploaded_at).getTime() < 30 * 60 * 1000;
    if (docs.some((d) => d.versions.some((v) => IN_PROGRESS.includes(v.status) && recent(v))) && route().page === "documents")
      state.pageTimer = setTimeout(() => load().catch(() => {}), 3000); // follow processing until it settles
  };
  const afterUpload = async () => {
    if (state.tenant !== state.defaultTenant) {
      await loadTenants(state.defaultTenant);
      root.replaceChildren(list); panelShown = false; // tenant changed: redraw the panel without the note
    }
    await load();
  };
  const drawList = () => {
    if (!docs.length) {
      list.replaceChildren(card(null, null, empty("documents", "No reports yet", "Upload a PDF above. It shows up here, and you can follow it until it is ready.")));
      return;
    }
    const companies = [...new Set(docs.map((d) => d.company))];
    const order = ["pending", "parsing", "parsed", "indexing", "indexed"];
    const labels = ["Uploaded", "Reading the PDF", "Read", "Making it searchable", "Ready to use"];
    const own = state.tenant === state.defaultTenant;
    list.replaceChildren(...docs.map((doc) => {
      const c = "var(--accent)"; // brand teal avatar
      const words = doc.company.replace(/[^A-Za-z ]/g, " ").split(/\s+/)
        .filter((w) => w && !/^(inc|corp|corporation|co|ltd|limited|plc|llc|the)$/i.test(w));
      const initials = (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2)).toUpperCase();
      if (own && !uploaders.has(doc.id)) uploaders.set(doc.id, versionUploadButton(doc, load));
      const up = own ? uploaders.get(doc.id) : null;
      return el("div", { class: "card doc-card fade-in" },
        el("div", { class: "doc-head" }, el("div", { class: "doc-avatar", vars: { "--c": c } }, initials),
          el("div", { style: { minWidth: 0 } }, el("h3", {}, doc.company),
            el("div", { class: "meta" }, `${doc.filename} · ${doc.document_type.replace("_", " ")}${doc.fiscal_year ? ` · FY${doc.fiscal_year}` : ""} · ${doc.versions.length} version${doc.versions.length === 1 ? "" : "s"}`)),
          up ? el("div", { style: { marginLeft: "auto" } }, up.button, up.input) : null),
        up ? up.below : null,
        [...doc.versions].reverse().map((v) => {
          const idx = order.indexOf(v.status);
          const failedAt = v.status === "failed" ? (v.parsed_at ? 3 : 1) : -1;
          if (!retryNotes.has(v.id)) retryNotes.set(v.id, el("div"));
          const actionOut = retryNotes.get(v.id);
          const retry = v.status === "failed" && own
            ? el("button", { class: "btn", type: "button", onclick: (e) => processAgain(doc, v, e.currentTarget, actionOut).then(load) }, icon("refresh"), "Try again")
            : null;
          return el("div", { class: "version" },
            el("div", { class: "version-top" }, el("b", {}, `Version ${v.version_number}`),
              v.is_current ? el("span", { class: "badge current", title: "Questions and search use this version" }, icon("check"), "In use") : null,
              statusTag(v.status), el("span", { class: "muted small" }, `${v.filename} · uploaded ${relTime(v.uploaded_at)}`),
              retry ? el("span", { style: { marginLeft: "auto" } }, retry) : null),
            el("div", { class: "steps" }, labels.map((lab, i) => {
              let cls = "";
              if (failedAt >= 0) cls = i < failedAt ? "done" : i === failedAt ? "failed" : "";
              else if (i < idx || (v.status === "indexed" && i <= idx)) cls = "done";
              else if (i === idx) cls = ["parsing", "indexing"].includes(v.status) ? "active" : "done";
              return el("div", { class: `step ${cls}` }, lab);
            })),
            v.status === "pending" ? el("div", { class: "small muted", style: { marginBottom: "6px" } },
              "Waiting to start. If this does not change within a few minutes, the background services may be stopped (an engineer can run ./start.sh).") : null,
            el("div", { class: "facts", title: `Technical: ${fmtInt(v.chunks)} chunks · ${v.points == null ? "–" : fmtInt(v.points)} vectors · parsed ${v.parsed_at ? relTime(v.parsed_at) : "–"} · indexed ${v.indexed_at ? relTime(v.indexed_at) : "–"}` },
              el("span", {}, "Searchable sections ", el("b", { class: "tnum" }, fmtInt(v.chunks))),
              el("span", {}, "File size ", el("b", {}, fmtBytes(v.size_bytes))),
              el("span", {}, "Ready ", el("b", {}, v.indexed_at ? relTime(v.indexed_at) : "not yet"))),
            v.error ? el("div", { class: "error-box" }, el("b", {}, "What went wrong: "), v.error) : null,
            actionOut,
            v.warnings.length ? el("details", { class: "warn" }, el("summary", {}, `${v.warnings.length} note${v.warnings.length === 1 ? "" : "s"} from reading the PDF`),
              el("ul", {}, v.warnings.slice(0, 40).map((w) => el("li", {}, w)))) : null);
        }));
    }));
  };
  root.append(list);
  await load();
  return { refresh: load };
}

/* ---------------- workflows ---------------- */
const TASK_STATE = {
  CREATED: ["muted", "clock", "Created"], PLANNED: ["muted", "clock", "Planned"], QUEUED: ["progress", "spinner", "Queued"],
  RUNNING: ["progress", "spinner", "Running"], SUCCEEDED: ["progress", "spinner", "Succeeded"],
  VERIFYING: ["progress", "spinner", "Verifying"], VERIFIED: ["good", "check", "Verified"],
  REJECTED: ["critical", "x", "Rejected"], FAILED_RETRYABLE: ["warning", "alert", "Retrying"],
  FAILED_FINAL: ["critical", "x", "Failed"], BLOCKED: ["warning", "alert", "Blocked"],
  NEEDS_HUMAN_REVIEW: ["warning", "alert", "Needs review"], CANCELLED: ["muted", "x", "Cancelled"],
};
const WF_STATE = {
  CREATED: ["muted", "clock", "Created"], PLANNING: ["progress", "spinner", "Planning"], RUNNING: ["progress", "spinner", "Running"],
  NEEDS_HUMAN_REVIEW: ["warning", "alert", "Needs review"], REPORTING: ["progress", "spinner", "Reporting"],
  COMPLETED: ["good", "check", "Completed"], FAILED: ["critical", "x", "Failed"], CANCELLED: ["muted", "x", "Cancelled"],
};
const WF_TERMINAL = ["COMPLETED", "FAILED", "CANCELLED", "NEEDS_HUMAN_REVIEW"];
const AGENT_LABEL = { document_intelligence: "Document Intelligence", financial_analyst: "Financial Analyst", verification: "Verification" };
function stateTag(map, s) {
  const [cls, ic, label] = map[s] || ["muted", "clock", s];
  return el("span", { class: `status ${cls}` }, icon(ic), label);
}

async function viewWorkflows(root) {
  const input = el("input", { type: "search", placeholder: "e.g. Compare Apple's revenue and operating margin FY2023–FY2025", "aria-label": "Question" });
  const note = el("div");
  const list = el("div");
  const form = el("form", { class: "searchbar", onsubmit: async (e) => {
    e.preventDefault();
    const query = input.value.trim();
    if (query.length < 3) return;
    note.replaceChildren(notice("info", "Starting…"));
    try {
      const wf = await api("/workflows", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ tenant: state.tenant, query }) });
      location.hash = `#/workflows/${wf.id}`;
    } catch (err) { note.replaceChildren(notice("critical", el("b", {}, "Could not start: "), err.message)); }
  } }, icon("workflows"), input, el("button", { class: "btn primary", type: "submit" }, "Ask"));
  const load = async () => {
    const rows = await api(`/workflows?${tq()}`);
    list.replaceChildren(rows.length ? el("div", { class: "table-wrap" }, el("table", { class: "data" },
      el("thead", {}, el("tr", {}, el("th", {}, "Question"), el("th", {}, "Status"), el("th", { class: "num" }, "Asked"))),
      el("tbody", {}, rows.map((w) => el("tr", { class: "click", onclick: () => { location.hash = `#/workflows/${w.id}`; } },
        el("td", {}, el("b", {}, w.query)), el("td", {}, stateTag(WF_STATE, w.status)),
        el("td", { class: "num muted", title: fmtDate(w.created_at) }, relTime(w.created_at)))))))
      : empty("workflows", "No questions yet", "Ask one above."));
  };
  append(root, [el("div", { class: "card hero fade-in" },
    el("h2", {}, "Ask about the reports"),
    el("p", {}, "AEIDS finds the figures in the uploaded reports, does the maths, and checks every number against its page before showing it."),
    el("div", { style: { marginTop: "16px" } }, form), note),
    card("Questions asked so far", "click one to see its answer", list)]);
  await load();
  input.focus();
  return { refresh: load };
}

async function viewWorkflow(root, id) {
  const graphBox = el("div", { class: "dag" });
  const parts = { head: el("div"), answer: el("div"), review: el("div"),
    graph: card("Task graph", "arrows: depends on · dashed: verifies", graphBox),
    findings: el("div"), timeline: el("div"), report: el("div"), techHead: el("div") };
  let opened = false;
  try { opened = localStorage.getItem("aeids.howOpen") === "1"; } catch (e) { /* storage unavailable */ }
  const how = el("details", { class: "card how", ...(opened ? { open: true } : {}),
    ontoggle: (e) => { try { localStorage.setItem("aeids.howOpen", e.target.open ? "1" : "0"); } catch (err) { /* ignore */ } } },
    el("summary", {}, el("b", {}, "How AEIDS got this answer"), el("span", { class: "muted small" }, " · every step, source and check (for auditors)")),
    parts.techHead, parts.graph,
    el("div", { class: "grid g-2", style: { alignItems: "start" } }, parts.findings, parts.timeline), parts.report);
  append(root, [parts.head, parts.review, parts.answer, how]);
  const load = async () => {
    const wf = await api(`/workflows/${encodeURIComponent(id)}?${tq()}`);
    drawHead(parts.head, wf);
    drawReview(parts.review, wf, load);
    drawAnswer(parts.answer, wf);
    drawTechHead(parts.techHead, wf);
    drawGraph(graphBox, wf.tasks);
    drawFindings(parts.findings, wf.findings);
    drawTimeline(parts.timeline, wf);
    drawReport(parts.report, wf.report);
    clearTimeout(state.pageTimer);
    if (!WF_TERMINAL.includes(wf.status) && route().page === "workflows") state.pageTimer = setTimeout(() => load().catch(() => {}), 2000);
  };
  await load();
  return { refresh: load };
}

const STEPS = [["extract", "Finding the figures in the reports"], ["verify:extract", "Checking them against their pages"],
  ["analyse", "Calculating"], ["verify:analyse", "Checking the calculations"]];

function latestTask(tasks, key) {
  return tasks.filter((t) => t.plan_key === key).sort((a, b) => b.rework_round - a.rework_round)[0];
}

function plainStatus(wf) {
  const a = wf.answer;
  if (wf.status === "COMPLETED") {
    if (a && (a.results.length || a.reported.length)) return ["good", "check", "Answered. Every number was checked against the report."];
    if (a && a.passages.length) return ["good", "check", "Answered with quotes from the reports. Each quote was checked against its source."];
    return ["warning", "alert", "No checked answer could be given. See the notes below."];
  }
  if (wf.status === "NEEDS_HUMAN_REVIEW") return ["warning", "alert", "Waiting for you: one step needs a person's decision."];
  if (wf.status === "FAILED") return ["critical", "x", "This question could not be answered."];
  if (wf.status === "CANCELLED") return ["muted", "x", "Cancelled."];
  return ["progress", "spinner", "Working on it… usually under a minute."];
}

function drawHead(box, wf) {
  const [cls, ic, text] = plainStatus(wf);
  const running = !WF_TERMINAL.includes(wf.status);
  const steps = running ? el("ol", { class: "wf-steps" }, STEPS.map(([key, label]) => {
    const t = latestTask(wf.tasks, key);
    const done = t && t.status === "VERIFIED";
    const active = t && !done && !["PLANNED", "CREATED"].includes(t.status);
    return el("li", { class: `wf-step${done ? " done" : active ? " active" : ""}` },
      el("span", { class: "dot" }, done ? icon("check") : active ? icon("spinner") : ""), label);
  })) : null;
  const took = wf.completed_at ? ` · took ${fmtMs(new Date(wf.completed_at) - new Date(wf.created_at))}` : "";
  box.replaceChildren(el("div", { class: "card hero fade-in" },
    el("div", { class: "trace-head" },
      el("a", { href: "#/workflows", class: "btn icon", title: "All questions", "aria-label": "All questions" }, icon("back")),
      el("h2", { style: { flex: "1 1 300px" } }, wf.query)),
    el("div", { class: `status ${cls}`, style: { fontSize: "15px", marginTop: "8px" } }, icon(ic), text,
      el("span", { class: "muted", style: { fontWeight: 400 } }, took)),
    steps));
}

function drawTechHead(box, wf) {
  const counts = wf.task_counts;
  box.replaceChildren(el("div", { class: "trace-stats", style: { margin: "12px 0" } },
    el("div", {}, el("b", {}, fmtInt(wf.tasks.length)), "tasks"),
    el("div", {}, el("b", {}, fmtInt(counts.VERIFIED || 0)), "verified"),
    el("div", {}, el("b", {}, fmtInt(wf.tasks.reduce((s, t) => s + Math.max(0, t.attempts - 1), 0))), "retries"),
    el("div", {}, el("b", {}, fmtInt(wf.findings.length)), "findings"),
    el("a", { class: "btn", href: `#/traces/${wf.trace_id}` }, icon("traces"), "Run details")));
}

function drawAnswer(box, wf) {
  const a = wf.answer;
  if (!a) { box.replaceChildren(); return; }
  const table = (head, rows) => el("div", { class: "table-wrap" }, el("table", { class: "data" },
    el("thead", {}, el("tr", {}, head.map(([h, cls]) => el("th", cls ? { class: cls } : {}, h)))),
    el("tbody", {}, rows)));
  const page = (s) => s.replace(/\.pdf/g, "").replace(/_/g, " ");
  const blocks = [];
  if (a.text) blocks.push(el("p", { class: "answer-text" }, a.text),
    el("div", { class: "small muted", style: { marginBottom: "18px" } }, a.text_source === "ai"
      ? "Written by AI from the checked figures below. Every number in it was checked." : "Summary of the checked figures below."));
  if (a.results.length) blocks.push(table([["Result"], ["Value", "num"], ["How"]], a.results.map((r) => el("tr", {},
    el("td", {}, el("b", {}, r.label)), el("td", { class: "num" }, el("b", { class: "big-value" }, r.value)),
    el("td", { class: "small muted" }, r.formula || "")))));
  if (a.reported.length) blocks.push(el("h3", { class: "answer-sub" }, a.results.length ? "Figures used, from the reports" : "From the reports"),
    table([["Figure"], ["Year"], ["Value", "num"], ["Where"]], a.reported.map((r) => el("tr", {},
      el("td", {}, r.label), el("td", {}, r.period || "–"),
      el("td", { class: "num" }, el("b", {}, r.value), r.units ? el("span", { class: "muted" }, ` ${r.units}`) : null),
      el("td", { class: "small muted" }, page(r.source))))));
  if (a.passages.length) blocks.push(el("h3", { class: "answer-sub", style: { marginTop: "0" } }, "What the reports say"), a.passages.map((p) => el("div", { class: "evidence" },
    el("div", { class: "small muted" }, page(p.source)),
    el("div", { class: "quote" }, p.quote.length > 320 ? p.quote.slice(0, 320) + "…" : p.quote))));
  if (a.limitations.length) blocks.push(el("div", { class: "answer-note" }, el("b", {}, "Note: "),
    el("ul", {}, a.limitations.map((l) => el("li", {}, l)))));
  blocks.push(el("div", { class: "small muted", style: { marginTop: "12px" } }, a.notice || ""));
  if (wf.status === "COMPLETED") blocks.push(el("div", { style: { marginTop: "14px" } },
    el("a", { class: "btn", href: `${API}/workflows/${wf.id}/report.md${tq() ? "?" + tq() : ""}`, download: "" },
      icon("download"), "Download the report (Markdown)"),
    el("span", { class: "small muted", style: { marginLeft: "10px" } }, "Answer, calculations, limitations, review and numbered sources, in one file.")));
  box.replaceChildren(card("Answer", null, blocks));
}

function drawReview(box, wf, reload) {
  const waiting = wf.tasks.filter((t) => t.status === "NEEDS_HUMAN_REVIEW");
  if (!waiting.length) { box.replaceChildren(); return; }
  box.replaceChildren(card("Your decision is needed", null,
    waiting.map((t) => {
      const note = el("input", { type: "text", placeholder: "Optional note", "aria-label": "Note" });
      const open = wf.findings.filter((f) => f.task_id === t.id && ["needs_review", "pending"].includes(f.verification_status));
      const send = async (decision) => {
        try {
          await api(`/workflows/${encodeURIComponent(wf.id)}/reviews`, { method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ tenant: state.tenant || null, task_id: t.id, decision, note: note.value }) });
          await reload();
        } catch (e) { toast(e.message); }
      };
      const reason = (t.last_error || "This step could not finish.").replace(/^./, (c) => c.toUpperCase());
      const buttons = open.length
        ? [el("button", { class: "btn primary", onclick: () => send("approve") }, icon("check"), "Looks right"),
           el("button", { class: "btn", onclick: () => send("reject") }, icon("x"), "Reject")]
        : [el("button", { class: "btn primary", onclick: () => send("rerun") }, icon("refresh"), "Try again"),
           el("button", { class: "btn", onclick: () => send("reject") }, icon("x"), "Stop here")];
      return el("div", { class: "version" },
        el("div", {}, open.length ? `AEIDS could not check ${open.length} figure(s) automatically. Please look at them:` : reason),
        open.length ? el("ul", { class: "small", style: { margin: "8px 0" } }, open.map((f) => el("li", {}, f.claim))) : null,
        el("div", { style: { marginTop: "12px" } }, note),
        el("div", { style: { display: "flex", gap: "8px", marginTop: "10px", flexWrap: "wrap" } }, buttons),
        open.length ? null : el("div", { class: "small muted", style: { marginTop: "8px" } },
          "Upload the missing report on the Documents page, then press “Try again”."));
    })));
}

function drawGraph(box, tasks) {
  const byId = new Map(tasks.map((t) => [String(t.id), t]));
  const depth = new Map();
  const d = (t) => {
    if (depth.has(t.id)) return depth.get(t.id);
    depth.set(t.id, 0);
    let v = 0;
    for (const dep of t.depends_on) if (byId.has(dep)) v = Math.max(v, d(byId.get(dep)) + 1);
    if (t.verifies && byId.has(String(t.verifies))) v = Math.max(v, d(byId.get(String(t.verifies))) + 1);
    depth.set(t.id, v);
    return v;
  };
  tasks.forEach(d);
  const cols = [];
  for (const t of tasks) (cols[depth.get(t.id)] ||= []).push(t);
  const nodes = new Map();
  const grid = el("div", { class: "dag-cols" }, cols.map((col) => el("div", { class: "dag-col" }, col.map((t) => {
    const [cls] = TASK_STATE[t.status] || ["muted"];
    const n = el("div", { class: `dag-node ${cls}`, tabindex: "0" },
      el("div", { class: "dag-top" }, el("b", {}, t.plan_key), t.rework_round ? el("span", { class: "badge" }, `rework ${t.rework_round}`) : null),
      el("div", { class: "small muted" }, AGENT_LABEL[t.agent] || t.agent),
      el("div", { class: "dag-foot" }, stateTag(TASK_STATE, t.status),
        el("span", { class: "small muted tnum" }, `attempt ${t.attempts}/${t.max_attempts}`)));
    hover(n, `${t.plan_key} · ${AGENT_LABEL[t.agent] || t.agent}`, () => [["status", t.status], ["attempts", `${t.attempts} of ${t.max_attempts}`],
      ...(t.last_error ? [["last error", t.last_error.slice(0, 120)]] : []),
      ...(t.output?.metrics?.hits ? [["passages", String(t.output.metrics.hits)]] : [])]);
    nodes.set(String(t.id), n);
    return n;
  }))));
  const svgLayer = svg("svg", { class: "dag-edges" });
  box.replaceChildren(svgLayer, grid);
  const drawEdges = () => {
    const base = box.getBoundingClientRect();
    svgLayer.setAttribute("width", base.width); svgLayer.setAttribute("height", base.height);
    svgLayer.replaceChildren(svg("defs", {}, svg("marker", { id: "arr", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      svg("path", { d: "M0,0 L10,5 L0,10 z", style: { fill: "var(--muted)" } }))));
    const edge = (from, to, dashed) => {
      const a = nodes.get(from)?.getBoundingClientRect(), b = nodes.get(to)?.getBoundingClientRect();
      if (!a || !b) return;
      const x1 = a.right - base.left, y1 = a.top + a.height / 2 - base.top, x2 = b.left - base.left - 2, y2 = b.top + b.height / 2 - base.top;
      const mx = (x1 + x2) / 2;
      svgLayer.append(svg("path", { d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, fill: "none",
        style: { stroke: dashed ? "var(--accent-ink)" : "var(--muted)", strokeWidth: 1.6, strokeDasharray: dashed ? "5 4" : "none" },
        "marker-end": "url(#arr)" }));
    };
    for (const t of tasks) {
      for (const dep of t.depends_on) edge(dep, String(t.id), false);
      if (t.verifies) edge(String(t.verifies), String(t.id), true);
    }
  };
  requestAnimationFrame(drawEdges);
  if (!box._ro) { box._ro = new ResizeObserver(() => requestAnimationFrame(drawEdges)); box._ro.observe(box); }
}

function drawFindings(box, findings) {
  box.replaceChildren(card("Findings", "each claim with its evidence and the verifier's decision",
    findings.length ? findings.map((f) => el("div", { class: "version" },
      el("div", { class: "version-top" }, stateTag({ verified: ["good", "check", "Verified"], rejected: ["critical", "x", "Rejected"],
        pending: ["progress", "spinner", "Pending"], needs_review: ["warning", "alert", "Needs review"] }, f.verification_status),
        el("span", { class: "badge" }, f.category)),
      el("div", { style: { margin: "8px 0", fontWeight: 600 } }, f.claim),
      f.value ? el("div", { class: "small" }, `Value: ${f.value} ${f.unit || ""} ${f.currency || ""} ${f.scale || ""} ${f.period || ""}`) : null,
      f.calculation ? el("div", { class: "small muted" }, `Calculation: ${f.calculation.function} · ${f.calculation.formula} · calc ${f.calculation.calc_version}`) : null,
      el("details", { class: "warn", open: true }, el("summary", {}, `${f.evidence.length} evidence passage(s)`),
        f.evidence.map((e) => el("div", { class: "evidence" },
          el("div", { class: "small" }, el("b", {}, e.file), ` · p. ${e.page}`, e.section ? ` · ${e.section}` : ""),
          el("div", { class: "quote" }, e.quote.length > 320 ? e.quote.slice(0, 320) + "…" : e.quote)))),
      f.verifications.map((v) => el("div", { class: "small muted", style: { marginTop: "6px" } },
        `Verification: ${v.decision} · ${v.reasons.join("; ")}`, v.rework_instructions ? el("div", {}, `Rework: ${v.rework_instructions}`) : null))))
      : empty("search", "No findings yet", "Findings appear when the Financial Analyst finishes.")));
}

function drawTimeline(box, wf) {
  const items = [];
  for (const e of wf.events) items.push({ at: e.at, who: "workflow", why: e.details.reason || "",
    text: e.details.to ? `${e.details.from} → ${e.details.to}` : e.action.replace("workflow.", "workflow ") });
  for (const t of wf.tasks) for (const tr of t.transitions)
    items.push({ at: tr.at, who: t.plan_key + (t.rework_round ? ` (rework ${t.rework_round})` : ""), text: `${tr.from} → ${tr.to}`, why: tr.reason, actor: tr.actor });
  items.sort((a, b) => new Date(a.at) - new Date(b.at));
  box.replaceChildren(card("Timeline", `${items.length} persisted state changes`,
    el("div", { class: "timeline" }, items.map((i) => el("div", { class: "tl-row" },
      el("span", { class: "tl-time tnum" }, new Date(i.at).toLocaleTimeString()),
      el("span", { class: "tl-who" }, i.who),
      el("span", {}, el("b", {}, i.text), i.why ? el("span", { class: "muted" }, ` · ${i.why}`) : null,
        i.actor && i.actor !== "supervisor" ? el("span", { class: "badge", style: { marginLeft: "6px" } }, i.actor) : null))))));
}

function drawReport(box, report) {
  if (!report) { box.replaceChildren(); return; }
  box.replaceChildren(card("Report", "only verified findings; everything else is a limitation",
    el("div", { class: "small" }, el("b", {}, "Verified findings: "), String(report.verified_findings.length)),
    report.verified_findings.map((f) => el("div", { class: "version" }, el("b", {}, f.claim),
      el("div", { class: "small muted" }, f.evidence.map((e) => `${e.file} p.${e.page}`).join(" · ")))),
    report.limitations.length ? el("div", { style: { marginTop: "10px" } }, el("b", { class: "small" }, "Limitations"),
      el("ul", { class: "small muted" }, report.limitations.map((l) => el("li", {}, l)))) : null,
    el("div", { class: "small muted", style: { marginTop: "10px" } }, report.notice)));
}

/* ---------------- search ---------------- */
async function viewSearch(root) {
  let mode = "hybrid", topK = 10, superseded = false;
  const input = el("input", { type: "search", placeholder: "Ask about a report, e.g. What were total net sales in 2025?", "aria-label": "Question" });
  const results = el("div", { class: "grid", style: { gap: "14px" } });
  const modeSeg = el("div", { class: "seg" });
  const modes = [["hybrid", "Best (both)"], ["dense", "By meaning"], ["keyword", "Exact words"]];
  const drawModes = () => modeSeg.replaceChildren(...modes.map(([k, n]) =>
    el("button", { type: "button", class: k === mode ? "on" : "", onclick: () => { mode = k; drawModes(); if (input.value.trim()) run(); } }, n)));
  drawModes();
  const chips = el("div", { class: "chips" });
  const run = async () => {
    const query = input.value.trim();
    if (!query) return;
    results.replaceChildren(el("div", { class: "skeleton" }), el("div", { class: "skeleton" }));
    try {
      const out = await api("/search", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ tenant: state.tenant, query, mode, top_k: topK, include_superseded: superseded }) });
      showResults(out, query);
    } catch (e) { results.replaceChildren(card(null, null, empty("alert", "Search failed", e.message))); }
  };
  const showResults = (out, query) => {
    const terms = [...new Set(query.toLowerCase().match(/[a-z0-9]{3,}/g) || [])]
      .filter((w) => !["what", "were", "was", "the", "how", "much", "did", "does", "which", "and", "for", "its", "with", "from", "this", "that"].includes(w));
    const maxScore = Math.max(...out.hits.map((h) => h.score), 1e-9);
    results.replaceChildren(
      el("div", { class: "row small muted" }, `${out.hits.length} results, best match first`,
        out.warnings.map((w) => el("span", { class: "status warning" }, icon("alert"), w)),
        el("span", { style: { marginLeft: "auto" } }, el("a", { href: `#/traces/${out.trace_id}` }, "Technical details of this search →"))),
      ...(out.hits.length ? out.hits.map((h, i) => el("div", { class: `card hit-card fade-in ${i === 0 ? "top" : ""}` },
        el("div", { class: "rank" }, String(i + 1)),
        el("div", { style: { minWidth: 0 } },
          el("div", { class: "hit-meta" }, el("span", { class: "co" }, h.company_name),
            el("span", {}, h.filename), el("span", { class: "badge" }, `page ${h.page_start}${h.page_end !== h.page_start ? `–${h.page_end}` : ""}`),
            el("span", { class: "badge kind", vars: { "--c": h.kind === "table" ? "var(--s3)" : h.kind === "chart" ? "var(--s2)" : "var(--s1)" } }, { table: "Table", chart: "Chart", text: "Text" }[h.kind] || h.kind),
            h.fiscal_year ? el("span", { class: "badge" }, `FY${h.fiscal_year}`) : null,
            h.is_current ? null : el("span", { class: "badge" }, `older version (v${h.version_number})`)),
          el("div", { class: "hit-section" }, h.section || "(no section)"),
          highlight(h.text.replace(/^Section: .*\n+/, ""), terms),
          el("div", { class: "hit-foot" },
            el("span", { class: "muted" }, "match"),
            el("div", { class: "scorebar", title: `Rank by meaning: ${h.dense_rank ? "#" + h.dense_rank : "–"} · by exact words: ${h.keyword_rank ? "#" + h.keyword_rank : "–"}` },
              el("span", { style: { width: `${(h.score / maxScore) * 100}%` } })),
            el("button", { class: "linkbtn", type: "button", onclick: (e) => { const t = e.target.closest(".hit-card").querySelector(".hit-text"); t.classList.toggle("open"); e.target.textContent = t.classList.contains("open") ? "Show less" : "Show all"; } }, "Show all")))))
        : [card(null, null, empty("search", "No results", "Nothing in the uploaded reports matches. Try other words."))]));
  };
  const form = el("form", { class: "searchbar", onsubmit: (e) => { e.preventDefault(); run(); } },
    icon("search"), input, el("button", { class: "btn primary", type: "submit" }, "Search"));
  const k = el("select", { class: "select", "aria-label": "Results", onchange: (e) => { topK = Number(e.target.value); } },
    [5, 10, 20].map((n) => el("option", { value: n, selected: n === topK }, `${n} results`)));
  append(root, [el("div", { class: "card hero fade-in" },
    el("h2", {}, "Search the reports"),
    el("p", {}, "Type a few words or a question. AEIDS shows the passages that match best, with the company, file and page. ",
      "To get a worked-out answer instead, use ", el("a", { href: "#/workflows" }, "Ask a question"), "."),
    el("div", { style: { marginTop: "16px" } }, form),
    el("div", { style: { marginTop: "12px" } }, chips),
    el("details", { class: "opts" }, el("summary", {}, "Search options"),
      el("div", { class: "row" }, modeSeg, k,
        el("label", { class: "toggle small" }, el("input", { type: "checkbox", onchange: (e) => { superseded = e.target.checked; } }), "Include older versions")),
      el("div", { class: "small muted", style: { marginTop: "8px" } },
        "Best (both) combines search by meaning (AI embeddings) with search for the exact words, and usually works best."))), results]);
  // example questions: from the latest benchmark run when there is one
  try {
    const runs = await api("/evals");
    const qs = runs.length ? (await api(`/evals/${encodeURIComponent(runs[0].name)}`)).questions.map((q) => q.question) : [];
    const picks = (qs.length ? [qs[0], qs[9], qs[13], qs[15], qs[22], qs[27]] : ["What were total revenues in 2025?", "What are the main risk factors?"]).filter(Boolean);
    chips.replaceChildren(el("span", { class: "small muted" }, "Try:"), ...picks.map((q) => el("button", { class: "chip", type: "button", onclick: () => { input.value = q; run(); } }, q)));
  } catch (e) { /* examples are optional */ }
  input.focus();
}
function highlight(text, terms) {
  const box = el("div", { class: "hit-text" });
  if (!terms.length) { box.textContent = text; return box; }
  const rx = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  for (const part of text.split(rx)) {
    if (!part) continue;
    box.append(rx.test(part) ? el("mark", {}, part) : document.createTextNode(part));
    rx.lastIndex = 0;
  }
  return box;
}

/* ---------------- evaluation ---------------- */
async function viewEvaluation(root, _arg, chosen) {
  const runs = await api("/evals");
  if (!runs.length) {
    root.append(card(null, null, empty("evaluation", "No quality test has been run yet", "An engineer can run: .venv/bin/python eval/run_retrieval.py")));
    return;
  }
  const name = chosen || runs[0].name;
  const run = await api(`/evals/${encodeURIComponent(name)}`);
  const modes = [["dense", "By meaning", "var(--s1)"], ["keyword", "Exact words", "var(--s5)"], ["hybrid", "Best (both)", "var(--series-brand)"]]
    .filter(([m]) => run.scores[m]);
  const metrics = [["recall@5", "Found in top 5"], ["recall@10", "Found in top 10"], ["mrr@10", "Rank score (MRR)"]];
  const best = modes.reduce((a, b) => (run.scores[b[0]]["recall@5"] > run.scores[a[0]]["recall@5"] ? b : a));
  const picker = el("select", { class: "select", "aria-label": "Benchmark run", onchange: (e) => { root.replaceChildren(); viewEvaluation(root, null, e.target.value); } },
    runs.map((r) => el("option", { value: r.name, selected: r.name === name }, fmtDate(r.created_at))));

  const hero = el("div", { class: "card hero fade-in" },
    el("div", { class: "row" }, el("h2", { style: { margin: 0 } }, "How well does search find the right page?"), el("span", { style: { marginLeft: "auto" } }, picker)),
    el("p", { style: { marginTop: "6px" } }, `We ask ${run.questions.length} test questions about ${run.corpus.length} annual reports (${run.corpus.join(", ")}) for which we know the page that holds the answer. `,
      "A question passes when that page is among the first 5 (or 10) results. Higher is better."),
    el("p", { class: "small muted", style: { marginTop: "6px" } }, `Embeddings: ${run.embedding_model}. The rank score (MRR) is higher when the right page comes earlier in the list.`),
    el("div", { class: "grid g-kpi", style: { marginTop: "18px", gridTemplateColumns: "repeat(4, minmax(0,1fr))" } },
      el("div", {}, el("div", { class: "small muted" }, "Best search type"), el("div", { style: { fontSize: "22px", fontWeight: 700 } }, best[1])),
      ...metrics.map(([k, label]) => el("div", {}, el("div", { class: "small muted" }, label),
        el("div", { class: "tnum", style: { fontSize: "28px", fontWeight: 700 } }, pct(run.scores[best[0]][k]))))));

  const chartBox = el("div", { class: "chart" });
  const scores = card("Scores by search type", "higher is better",
    el("div", { class: "legend", style: { marginBottom: "10px" } }, modes.map(([, label, c]) => el("span", {}, el("i", { style: { background: c } }), label))),
    chartBox,
    el("div", { class: "table-wrap", style: { marginTop: "12px" } }, el("table", { class: "data" },
      el("thead", {}, el("tr", {}, el("th", {}, "Search type"), metrics.map(([, l]) => el("th", { class: "num" }, l)))),
      el("tbody", {}, modes.map(([m, label]) => el("tr", {}, el("td", {}, label), metrics.map(([k]) => el("td", { class: "num" }, pct(run.scores[m][k])))))))));

  const heatColor = (r) => r == null ? null : r === 1 ? 1 : r <= 3 ? 2 : r <= 5 ? 3 : 4;
  const heat = el("div", { class: "heat" },
    el("div", { class: "h" }, "id"), el("div", { class: "h" }, "Question"), modes.map(([m, label]) => el("div", { class: "h", style: { textAlign: "center" }, title: label }, { dense: "meaning", keyword: "words", hybrid: "both" }[m] || m)),
    run.questions.map((q) => [
      el("div", { class: "id" }, q.id), el("div", { class: "q", title: q.question }, q.question),
      modes.map(([m]) => {
        const r = q[m]?.rank;
        const b = heatColor(r);
        const cell = el("div", { class: `cell ${b ? "" : "miss"}`, style: b ? { background: `var(--heat-${b})`, color: `var(--heat-ink-${b})` } : {} }, r ? `#${r}` : "–");
        return hover(cell, `${q.id} · ${m}`, () => [["rank", r ? `#${r}` : "not in top 10"], ["gold pages", q.pages.join(", ")],
          ...((q[m]?.top || []).slice(0, 3).map((t, i) => [`top ${i + 1}`, `${t.doc.replace(".pdf", "")} p.${t.pages[0]}`]))]);
      })]));
  const legendScale = el("div", { class: "scale", style: { marginBottom: "10px" } }, "Position of the right page in the results:",
    [[1, "#1"], [2, "#2–3"], [3, "#4–5"], [4, "#6–10"]].map(([b, l]) => el("span", {}, el("span", { class: "sw", style: { background: `var(--heat-${b})` } }), " ", l)),
    el("span", {}, el("span", { class: "sw", style: { background: "repeating-linear-gradient(135deg, var(--surface-3) 0 4px, transparent 4px 8px)" } }), " not found"));
  const perQ = card("Every test question", "hover a cell for the answer pages and the top results", legendScale, heat);

  const history = runs.length > 1 ? card("Run history", null, el("div", { class: "table-wrap" }, el("table", { class: "data" },
    el("thead", {}, el("tr", {}, el("th", {}, "Run"), el("th", {}, "Model"), modes.map(([, label]) => el("th", { class: "num" }, `${label}: top 5`)))),
    el("tbody", {}, runs.map((r) => el("tr", {}, el("td", {}, fmtDate(r.created_at)), el("td", {}, r.embedding_model),
      modes.map(([m]) => el("td", { class: "num" }, pct(r.scores?.[m]?.["recall@5"]))))))))) : null;

  append(root, [hero, el("div", { class: "grid g-2", style: { alignItems: "start" } }, scores, perQ), history]);
  groupedChart(chartBox, metrics.map(([, l]) => l), modes.map(([m, label, c]) => ({
    name: label, color: c, label: m === "hybrid", values: metrics.map(([k]) => run.scores[m][k]) })));
}

/* ---------------- traces ---------------- */
function traceTable(rows, compact = false) {
  if (!rows.length) return empty("traces", "Nothing has run yet", "Upload a report or run a search; each run appears here.");
  const maxDur = Math.max(1, ...rows.map((r) => r.duration_ms || 0));
  return el("div", { class: "table-wrap" }, el("table", { class: "data" },
    el("thead", {}, el("tr", {}, el("th", {}, "Status"), el("th", {}, "What ran"), compact ? null : el("th", {}, "Input"),
      compact ? null : el("th", {}, "Workspace"), el("th", { class: "num" }, "Steps"), el("th", { class: "num" }, "AI tokens"),
      el("th", { class: "num" }, "Cost"), el("th", { class: "num" }, "Duration"), el("th", { class: "num" }, "Started"))),
    el("tbody", {}, rows.map((r) => el("tr", { class: "click", onclick: () => { location.hash = `#/traces/${r.trace_id}`; } },
      el("td", {}, runStatus(r.status)),
      el("td", {}, el("div", { class: "row", style: { gap: "8px", flexWrap: "nowrap" } }, kindBadge(r.kind), el("b", {}, r.name))),
      compact ? null : el("td", { class: "preview", title: r.preview }, r.preview),
      compact ? null : el("td", { class: "muted" }, r.tenant || "–"),
      el("td", { class: "num" }, fmtInt(r.spans)), el("td", { class: "num" }, r.tokens ? fmtInt(r.tokens) : "–"),
      el("td", { class: "num" }, Number(r.cost_usd) ? fmtCost(r.cost_usd) : "–"),
      el("td", { class: "num" }, el("div", { class: "dur" }, el("div", { class: "mini" }, el("span", { style: { width: `${((r.duration_ms || 0) / maxDur) * 100}%` } })), fmtMs(r.duration_ms))),
      el("td", { class: "num muted", title: fmtDate(r.started_at) }, relTime(r.started_at)))))));
}
async function viewTraces(root) {
  const f = { q: "", kind: "", status: "", limit: 50 };
  const tableBox = el("div");
  const count = el("span", { class: "sub" });
  const load = async () => {
    const qs = new URLSearchParams({ limit: f.limit });
    if (f.q) qs.set("q", f.q); if (f.kind) qs.set("kind", f.kind); if (f.status) qs.set("status", f.status);
    const out = await api(`/traces?${qs}`);
    count.textContent = `${fmtInt(out.total)} runs recorded`;
    tableBox.replaceChildren(traceTable(out.traces),
      out.traces.length >= f.limit ? el("div", { class: "row", style: { justifyContent: "center", marginTop: "12px" } },
        el("button", { class: "btn", type: "button", onclick: () => { f.limit += 50; load(); } }, "Load more")) : null);
  };
  let debounce;
  const filters = el("div", { class: "row" },
    el("input", { class: "input", type: "search", placeholder: "Filter by name", oninput: (e) => { clearTimeout(debounce); debounce = setTimeout(() => { f.q = e.target.value.trim(); load(); }, 250); } }),
    el("select", { class: "select", "aria-label": "Kind", onchange: (e) => { f.kind = e.target.value; load(); } },
      el("option", { value: "" }, "All kinds"), ["job", "retrieval", "eval", "chain"].map((k) => el("option", { value: k }, kindLabel(k)))),
    el("select", { class: "select", "aria-label": "Status", onchange: (e) => { f.status = e.target.value; load(); } },
      el("option", { value: "" }, "Any status"), el("option", { value: "ok" }, "OK"), el("option", { value: "error" }, "Error")));
  root.append(el("div", { class: "card fade-in" },
    el("div", { class: "card-head" }, el("h2", {}, "Run history"), count, el("div", { class: "right" }, filters)), tableBox));
  await load();
  return { refresh: load };
}
async function viewTrace(root, traceId) {
  const data = await api(`/traces/${encodeURIComponent(traceId)}`);
  const spans = data.spans;
  const byId = new Map(spans.map((s) => [s.span_id, { ...s, children: [] }]));
  const roots = [];
  for (const s of byId.values()) (s.parent_span_id && byId.get(s.parent_span_id) ? byId.get(s.parent_span_id).children : roots).push(s);
  const rootSpan = roots[0];
  const total = Math.max(1, ...spans.map((s) => s.offset_ms + (s.duration_ms || 0)));
  const tokens = spans.reduce((a, s) => a + (s.total_tokens || 0), 0);
  const cost = spans.reduce((a, s) => a + Number(s.cost_usd || 0), 0);
  const calls = spans.filter((s) => ["llm", "embedding"].includes(s.kind)).length;
  const collapsed = new Set();
  let selected = rootSpan.span_id;

  const head = el("div", { class: "card hero fade-in" },
    el("div", { class: "trace-head" },
      el("a", { href: "#/traces", class: "btn icon", title: "Run history", "aria-label": "Run history" }, icon("back")),
      kindBadge(rootSpan.kind), el("h2", {}, rootSpan.name), runStatus(rootSpan.status),
      el("button", { class: "btn", type: "button", style: { marginLeft: "auto" }, onclick: () => navigator.clipboard?.writeText(traceId) }, icon("copy"), traceId.slice(0, 12) + "…")),
    el("div", { class: "trace-stats" },
      el("div", {}, el("b", {}, fmtMs(rootSpan.duration_ms)), "total time"),
      el("div", {}, el("b", {}, fmtInt(spans.length)), "steps"),
      el("div", {}, el("b", {}, fmtInt(calls)), "model calls"),
      el("div", {}, el("b", {}, tokens ? fmtInt(tokens) : "–"), "tokens"),
      el("div", {}, el("b", {}, cost ? fmtCost(cost) : "–"), "cost"),
      el("div", {}, el("b", {}, rootSpan.tenant || "–"), "workspace"),
      el("div", {}, el("b", {}, fmtDate(data.started_at)), "started")));

  const tree = el("div");
  const inspector = el("div", { class: "card inspector fade-in" });
  const legend = el("div", { class: "legend", style: { marginBottom: "10px" } },
    [...new Set(spans.map((s) => s.kind))].map((k) => el("span", {}, el("i", { style: { background: kindVar(k) } }), kindLabel(k))));
  const ticks = el("div", { class: "ticks" }, [0, 0.25, 0.5, 0.75, 1].map((p) => el("span", { style: { left: `${p * 100}%` } }, p ? fmtMs(total * p) : "0")));
  const waterfall = card("Timeline", "click a step to inspect it", legend,
    el("div", { class: "wf-ruler" }, el("span", {}, "Step"), ticks), tree);

  const drawTree = () => {
    const rows = [];
    const walk = (s, depth) => {
      const kids = s.children.sort((a, b) => a.offset_ms - b.offset_ms);
      const left = (s.offset_ms / total) * 100, width = Math.max(0.25, ((s.duration_ms || 0) / total) * 100);
      const row = el("div", { class: `wf-row ${s.span_id === selected ? "sel" : ""}`, onclick: () => { selected = s.span_id; drawTree(); drawInspector(); } },
        el("div", { class: "wf-name", style: { paddingLeft: `${depth * 16}px` } },
          kids.length ? el("button", { class: "caret", type: "button", "aria-label": "Expand or collapse",
            onclick: (e) => { e.stopPropagation(); collapsed.has(s.span_id) ? collapsed.delete(s.span_id) : collapsed.add(s.span_id); drawTree(); } },
            icon(collapsed.has(s.span_id) ? "right" : "down")) : el("span", { class: "caret" }),
          el("span", { class: "dot", style: { background: kindVar(s.kind) } }),
          s.status === "error" ? el("span", { class: "err-ic" }, icon("x")) : null,
          el("span", { class: "nm", title: s.name }, s.name),
          kids.length && collapsed.has(s.span_id) ? el("span", { class: "muted small" }, `+${kids.length}`) : null),
        el("div", { class: "wf-track" },
          el("div", { class: `wf-bar ${s.status === "error" ? "err" : ""}`, style: { left: `${left}%`, width: `${width}%`, background: kindVar(s.kind) } }),
          el("span", durLabel(left, width), fmtMs(s.duration_ms))));
      rows.push(row);
      if (!collapsed.has(s.span_id)) kids.forEach((k) => walk(k, depth + 1));
    };
    roots.forEach((r) => walk(r, 0));
    tree.replaceChildren(...rows);
  };
  let tab = "inputs";
  const drawInspector = () => {
    const s = byId.get(selected);
    const tabs = [["inputs", "Inputs", s.inputs], ["outputs", "Outputs", s.outputs], ["attributes", "Attributes", s.attributes]];
    if (s.error) tabs.push(["error", "Error", s.error]);
    if (!tabs.some(([k]) => k === tab)) tab = "inputs";
    const current = tabs.find(([k]) => k === tab);
    inspector.replaceChildren(
      el("div", { class: "row" }, kindBadge(s.kind), runStatus(s.status)),
      el("h3", { style: { marginTop: "8px" } }, s.name),
      el("dl", { class: "kv" },
        el("dt", {}, "Duration"), el("dd", { class: "tnum" }, fmtMs(s.duration_ms)),
        el("dt", {}, "Starts at"), el("dd", { class: "tnum" }, `+${fmtMs(s.offset_ms)}`),
        s.model ? [el("dt", {}, "Model"), el("dd", {}, s.model)] : null,
        s.total_tokens != null ? [el("dt", {}, "Tokens"), el("dd", { class: "tnum" }, fmtInt(s.total_tokens))] : null,
        s.cost_usd != null ? [el("dt", {}, "Cost"), el("dd", { class: "tnum" }, fmtCost(s.cost_usd))] : null,
        el("dt", {}, "Span id"), el("dd", { class: "mono" }, s.span_id)),
      el("div", { class: "tabs", role: "tablist" }, tabs.map(([k, label]) =>
        el("button", { type: "button", role: "tab", class: k === tab ? "on" : "", onclick: () => { tab = k; drawInspector(); } }, label))),
      current[2] == null || (typeof current[2] === "object" && !Object.keys(current[2]).length)
        ? el("div", { class: "muted small" }, "Nothing recorded.") : jsonView(current[2]),
      el("div", { class: "muted small", style: { marginTop: "8px" } }, "Document text here is data from the reports, shown as plain text."));
  };
  append(root, [head, el("div", { class: "trace-body" }, waterfall, inspector)]);
  drawTree(); drawInspector();
}
/* where a step's duration label goes: after the bar, before it, or inside a long bar */
function durLabel(left, width) {
  if (left + width <= 82) return { class: "wf-dur", style: { left: `calc(${left + width}% + 6px)` } };
  if (left >= 18) return { class: "wf-dur", style: { right: `calc(${100 - left}% + 6px)` } };
  return { class: "wf-dur in", style: { right: `calc(${100 - left - width}% + 6px)` } };
}
function jsonView(value) {
  const pre = el("pre", { class: "json" });
  const put = (v, indent) => {
    const pad = "  ".repeat(indent);
    if (v === null) pre.append(el("span", { class: "b" }, "null"));
    else if (typeof v === "string") pre.append(el("span", { class: "s" }, JSON.stringify(v)));
    else if (typeof v === "number") pre.append(el("span", { class: "n" }, String(v)));
    else if (typeof v === "boolean") pre.append(el("span", { class: "b" }, String(v)));
    else if (Array.isArray(v)) {
      if (!v.length) { pre.append("[]"); return; }
      pre.append("[\n");
      v.forEach((x, i) => { pre.append(pad + "  "); put(x, indent + 1); pre.append(i < v.length - 1 ? ",\n" : "\n"); });
      pre.append(pad + "]");
    } else {
      const keys = Object.keys(v);
      if (!keys.length) { pre.append("{}"); return; }
      pre.append("{\n");
      keys.forEach((k, i) => { pre.append(pad + "  ", el("span", { class: "k" }, JSON.stringify(k)), ": "); put(v[k], indent + 1); pre.append(i < keys.length - 1 ? ",\n" : "\n"); });
      pre.append(pad + "}");
    }
  };
  put(value, 0);
  return pre;
}

/* ---------------- logs ---------------- */
async function viewLogs(root) {
  const f = { source: "all", q: "", levels: new Set(["INFO", "WARNING", "ERROR", "CRITICAL", "DEBUG"]), lines: 400 };
  const srcColor = { tablelens: "var(--s4)", api: "var(--s1)", worker: "var(--s3)", start: "var(--s7)" };
  const term = el("div", { class: "terminal", role: "log", "aria-label": "Logs" });
  const info = el("span", { class: "sub" });
  const sources = [["all", "All"], ["tablelens", "PDF reader"], ["api", "API"], ["worker", "Worker"], ["start", "Launcher"]];
  const srcSeg = el("div", { class: "seg" });
  const drawSrc = () => srcSeg.replaceChildren(...sources.map(([k, n]) =>
    el("button", { type: "button", class: k === f.source ? "on" : "", onclick: () => { f.source = k; drawSrc(); load(true); } }, n)));
  drawSrc();
  const lvlSeg = el("div", { class: "seg" });
  const drawLvl = () => lvlSeg.replaceChildren(...[["INFO", "Info"], ["WARNING", "Warnings"], ["ERROR", "Errors"]].map(([k, n]) =>
    el("button", { type: "button", class: f.levels.has(k) ? "on" : "", onclick: () => {
      const keys = k === "ERROR" ? ["ERROR", "CRITICAL"] : k === "INFO" ? ["INFO", "DEBUG"] : [k];
      keys.forEach((x) => (f.levels.has(x) ? f.levels.delete(x) : f.levels.add(x))); drawLvl(); load(true);
    } }, n)));
  drawLvl();
  let debounce;
  const search = el("input", { class: "input", type: "search", placeholder: "Search logs", oninput: (e) => { clearTimeout(debounce); debounce = setTimeout(() => { f.q = e.target.value.trim(); load(true); }, 250); } });
  const load = async (jump = false) => {
    const qs = new URLSearchParams({ source: f.source, lines: f.lines });
    if (f.q) qs.set("q", f.q);
    const out = await api(`/logs?${qs}`);
    const atBottom = term.scrollTop + term.clientHeight >= term.scrollHeight - 30;
    const entries = out.entries.filter((e) => f.levels.has(e.level));
    info.textContent = out.sources.length ? `${fmtInt(entries.length)} lines from ${out.sources.join(", ")}` : "";
    if (!out.sources.length) {
      term.replaceChildren(el("div", { style: { padding: "20px", color: "#8d8c85" } }, out.hint || "No logs."));
      return;
    }
    term.replaceChildren(...entries.map((e) => el("div", { class: `log-line ${e.level}` },
      el("span", { class: "ts" }, e.ts ? new Date(e.ts).toLocaleTimeString() : ""),
      el("span", { class: "src", style: { color: srcColor[e.source] } }, e.source),
      el("span", { class: `lvl ${e.level}` }, e.level),
      el("span", { class: "txt" }, e.text))));
    if (jump || atBottom) term.scrollTop = term.scrollHeight;
  };
  root.append(el("div", { class: "card fade-in" },
    el("div", { class: "card-head" }, el("h2", {}, "System logs"), info,
      el("div", { class: "right" }, el("span", { class: "small muted" }, "Turn on Auto-update (top right) to follow"))),
    el("div", { class: "row", style: { marginBottom: "12px" } }, srcSeg, lvlSeg, search), term));
  await load(true);
  return { refresh: () => load(false) };
}

/* ---------------- shell ---------------- */
async function loadTenants(select) {
  try {
    const t = await api("/tenants");
    state.defaultTenant = t.default;
    if (t.models) $("#model-chip").replaceChildren(el("b", {}, "AI models"), `Search: ${t.models.embedding}`, el("br"), `Answers: ${t.models.llm}`);
    const names = t.tenants.includes(t.default) ? t.tenants : [t.default, ...t.tenants]; // own tenant always listed
    state.tenants = names;
    let saved = select || null;
    if (!saved) { try { saved = localStorage.getItem("aeids-tenant"); } catch (e) { /* storage blocked */ } }
    state.tenant = names.includes(saved) ? saved : (t.tenants.includes(t.default) || !t.tenants.length ? t.default : t.tenants[0]);
    try { localStorage.setItem("aeids-tenant", state.tenant); } catch (e) { /* storage blocked */ }
    $("#tenant-select").replaceChildren(...names.map((n) => el("option", { value: n, selected: n === state.tenant }, n)));
  } catch (e) { toast(e.message); }
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("aeids-theme", theme); } catch (e) { /* storage blocked */ }
  const btn = $("#theme-btn");
  btn.replaceChildren(icon(theme === "light" ? "moon" : "sun"), theme === "light" ? "Dark theme" : "Light theme");
}
function setLive(on) {
  clearInterval(state.timer);
  $("#live-dot").hidden = !on;
  if (on) state.timer = setInterval(() => { if (!["search", "evaluation", "workflows"].includes(route().page)) render(true); }, route().page === "logs" ? 3000 : 5000);
}
async function main() {
  buildNav();
  $("#refresh-btn").append(icon("refresh"));
  $("#refresh-btn").addEventListener("click", () => render());
  setTheme(document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  $("#theme-btn").addEventListener("click", () => {
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    render(); // charts read the theme's colours when drawn
  });
  $("#tenant-select").addEventListener("change", (e) => {
    state.tenant = e.target.value;
    try { localStorage.setItem("aeids-tenant", state.tenant); } catch (err) { /* storage blocked */ }
    render();
  });
  $("#auto-refresh").addEventListener("change", (e) => setLive(e.target.checked));
  window.addEventListener("hashchange", () => { render(); if ($("#auto-refresh").checked) setLive(true); });
  await loadTenants();
  render();
}
main();
