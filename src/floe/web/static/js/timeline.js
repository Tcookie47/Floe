// Timeline tab (SPEC §15.7): data freshness per table, the branch's Nessie commit log and
// a table's Iceberg snapshot history with a small SVG chart. Metadata only — no rows.
// Everything is built with DOM APIs; commit messages and keys only ever go through
// textContent / attributes. Colours come from CSS classes (CSP: no inline styles).

import { errorText, JobHandle } from "./api.js";
import { $, clear, copyText, fmtInt, h, toast } from "./dom.js";

export const TIMELINE_CHANNELS = ["freshness", "timeline", "table_history"];
const SVG_NS = "http://www.w3.org/2000/svg";
const HISTORY_PAGE = 50;
const STATUS = {
  not_found: { glyph: "?", label: "Not found" },
  corrupt: { glyph: "!", label: "Corrupt pointer" },
  scope_error: { glyph: "✕", label: "Outside its container" },
  error: { glyph: "✕", label: "Error" },
};
const KNOWN_OPS = new Set(["append", "overwrite", "delete", "replace"]);

// ----- time formatting ------------------------------------------------------------------
export function ago(ms, now = Date.now()) {
  if (ms === null || ms === undefined) return "—";
  const secs = Math.round((now - ms) / 1000);
  if (secs < 0) return "in the future";
  if (secs < 45) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(secs / 3600);
  if (hours < 48) return `${hours} h ago`;
  const days = Math.round(secs / 86400);
  if (days < 60) return `${days} d ago`;
  const months = Math.round(days / 30.44);
  if (months < 24) return `${months} mo ago`;
  return `${Math.round(days / 365.25)} y ago`;
}

export function exactTime(ms) {
  if (ms === null || ms === undefined) return "";
  return new Date(ms).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
    timeZoneName: "short",
  });
}

function ageEl(ms, extra = "") {
  if (ms === null || ms === undefined) return h("span", { class: "muted", text: "—" });
  return h("span", {
    class: "age", dataset: { ms: String(ms) }, text: ago(ms),
    title: `${exactTime(ms)}${extra ? `\n${extra}` : ""}`,
  });
}

const num = (n) => (n === null || n === undefined ? "—" : fmtInt(n));
const firstLine = (text) => (text || "").split("\n").find((l) => l.trim()) || "";
const opClass = (op) => `op-${KNOWN_OPS.has(op) ? op : "other"}`;

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "text") el.textContent = v; else el.setAttribute(k, String(v));
  }
  for (const c of children) if (c) el.append(c);
  return el;
}

function statusIcon(status, error) {
  const st = STATUS[status];
  if (!st) return null;
  return h("span", {
    class: `status-icon ${status}`, text: st.glyph, "aria-label": st.label,
    title: error ? `${st.label}: ${error}` : st.label,
  });
}

// ----- chart ------------------------------------------------------------------------------
// Total rows over time as a step line, one marker per snapshot coloured by operation.
export function snapshotChart(snapshots) {
  const pts = snapshots
    .filter((s) => s.time_ms !== null && s.total_records !== null && s.total_records !== undefined)
    .slice()
    .sort((a, b) => a.time_ms - b.time_ms);
  if (!pts.length) return h("div", { class: "muted tl-empty", text: "No row totals in the snapshot summaries." });
  const W = 640, H = 170, L = 64, R = 14, T = 12, B = 28;
  const t0 = pts[0].time_ms, t1 = pts[pts.length - 1].time_ms;
  const maxY = Math.max(1, ...pts.map((p) => p.total_records));
  const x = (t) => (t1 === t0 ? L + (W - L - R) / 2 : L + ((t - t0) / (t1 - t0)) * (W - L - R));
  const y = (v) => T + (1 - v / maxY) * (H - T - B);
  const chart = svg("svg", {
    class: "tl-chart", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Total rows over time: ${pts.length} snapshot(s), up to ${fmtInt(maxY)} rows`,
    "data-testid": "snapshot-chart",
  });
  const axis = svg("g", { class: "tl-axis" });
  for (const v of [0, maxY]) {
    axis.append(svg("line", { class: "tl-grid", x1: L, x2: W - R, y1: y(v), y2: y(v) }));
    axis.append(svg("text", { x: L - 6, y: y(v) + 3, "text-anchor": "end", text: fmtInt(v) }));
  }
  const d0 = new Date(t0).toLocaleDateString();
  const d1 = new Date(t1).toLocaleDateString();
  axis.append(svg("text", { x: L, y: H - 8, "text-anchor": "start", text: d0 }));
  if (t1 !== t0) axis.append(svg("text", { x: W - R, y: H - 8, "text-anchor": "end", text: d1 }));
  chart.append(axis);
  const step = [];
  pts.forEach((p, i) => {
    if (i > 0) step.push(`${x(p.time_ms).toFixed(1)},${y(pts[i - 1].total_records).toFixed(1)}`);
    step.push(`${x(p.time_ms).toFixed(1)},${y(p.total_records).toFixed(1)}`);
  });
  chart.append(svg("polyline", { class: "tl-line", points: step.join(" ") }));
  for (const p of pts) {
    const tip = `${exactTime(p.time_ms)}\n${p.operation || "unknown"} · total ${fmtInt(p.total_records)} rows`
      + (p.added_records ? ` · +${fmtInt(p.added_records)}` : "")
      + (p.deleted_records ? ` · −${fmtInt(p.deleted_records)}` : "");
    chart.append(svg("circle", {
      class: `tl-pt ${opClass(p.operation)}`, cx: x(p.time_ms).toFixed(1), cy: y(p.total_records).toFixed(1), r: 4.5,
      "data-op": p.operation || "",
    }, svg("title", { text: tip })));
  }
  const ops = [...new Set(pts.map((p) => p.operation || "unknown"))];
  const legend = h("div", { class: "tl-legend" },
    ops.map((op) => h("span", { class: `tl-legend-item ${opClass(op === "unknown" ? null : op)}`, text: op })));
  return h("div", { class: "tl-chart-wrap" }, chart, legend);
}

// ----- view ---------------------------------------------------------------------------------
export class TimelineView {
  // ctx() → { profile, ref, head, isLocal }
  constructor({ ctx }) {
    this.ctx = ctx;
    this.loadedFor = null;
    this.rows = [];
    this.sort = { col: "last_write_time_ms", dir: -1 };
    this.commits = [];
    this.nextToken = null;
    this.table = null; // {elements, key}
    this.jobs = { fresh: null, commits: null, table: null };
    this.seq = 0;

    this.filterEl = $("#tl-filter");
    this.layerEl = $("#tl-layer");
    this.freshBody = $("#tl-fresh-body");
    this.freshStatus = $("#tl-fresh-status");
    this.commitsBody = $("#tl-commits-body");
    this.commitsStatus = $("#tl-commits-status");
    this.tableBody = $("#tl-table-body");
    this.tableTitle = $("#tl-table-title");
    this.messageEl = $("#tl-message");
    this.layoutEl = $("#tl-layout");

    this.filterEl.addEventListener("input", () => this.renderFreshness());
    this.layerEl.addEventListener("change", () => this.renderFreshness());
    $("#tl-reload").addEventListener("click", () => this.load());
    this.freshBody.addEventListener("click", (e) => {
      const th = e.target.closest("th[data-col]");
      if (th) { this.toggleSort(th.dataset.col); return; }
      const tr = e.target.closest("tr[data-key]");
      if (tr) this.openRow(tr);
    });
    this.freshBody.addEventListener("keydown", (e) => {
      const tr = e.target.closest("tr[data-key]");
      if (tr && e.key === "Enter") { e.preventDefault(); this.openRow(tr); }
    });
    setInterval(() => this.tick(), 30000);
    this.reset();
  }

  get visible() { return !$("#tab-timeline").hidden; }

  ctxKey() {
    const c = this.ctx();
    return c.profile && c.ref ? `${c.profile}\u0000${c.ref}\u0000${c.head || ""}` : null;
  }

  abandonAll() {
    for (const name of Object.keys(this.jobs)) {
      if (this.jobs[name]) this.jobs[name].abandon();
      this.jobs[name] = null;
    }
  }

  // Forget everything (profile / branch changed). Jobs server-side are cancelled by the
  // caller's channel cancel; here we just stop polling them.
  reset() {
    this.seq++;
    this.abandonAll();
    this.loadedFor = null;
    this.rows = [];
    this.commits = [];
    this.nextToken = null;
    this.table = null;
    clear(this.freshBody);
    clear(this.commitsBody);
    clear(this.tableBody).append(h("div", { class: "muted tl-empty", text: "Click a table in the freshness list or the timeline, or use “Show history” in the tree." }));
    this.tableTitle.textContent = "";
    this.freshStatus.textContent = "";
    this.commitsStatus.textContent = "";
    this.setMessage(null);
  }

  setMessage(text) {
    this.messageEl.hidden = !text;
    this.messageEl.textContent = text || "";
    this.layoutEl.hidden = Boolean(text);
  }

  // Tab shown (or context changed while shown): load if the branch/head changed.
  activate() {
    const c = this.ctx();
    if (!c.profile || !c.ref) { this.reset(); this.setMessage("Pick a profile and a branch first."); return; }
    if (c.isLocal) {
      this.reset();
      this.setMessage("The Timeline needs a Nessie catalog. Local mode reads plain parquet files, which have no snapshots or commit history.");
      this.loadedFor = this.ctxKey();
      return;
    }
    if (this.loadedFor === this.ctxKey()) return;
    this.load();
  }

  // Refresh pressed: reload now if visible, else on next activate.
  invalidate() {
    this.loadedFor = null;
    if (this.visible) this.activate();
  }

  load() {
    const c = this.ctx();
    if (!c.profile || !c.ref || c.isLocal) { this.activate(); return; }
    this.setMessage(null);
    this.loadedFor = this.ctxKey();
    this.seq++;
    this.loadFreshness();
    this.loadCommits(true);
    if (this.table) this.loadTable(this.table);
  }

  tick() {
    const now = Date.now();
    for (const el of document.querySelectorAll("#tab-timeline .age[data-ms]")) {
      el.textContent = ago(Number(el.dataset.ms), now);
    }
  }

  async runJob(slot, payload) {
    const c = this.ctx();
    if (this.jobs[slot]) this.jobs[slot].abandon();
    const seq = this.seq;
    let job;
    try {
      job = await JobHandle.start({ profile: c.profile, ref: c.ref, ...payload });
    } catch (err) {
      return { status: "error", error: { message: errorText(err) }, stale: seq !== this.seq };
    }
    if (seq !== this.seq) { job.abandon(); return { status: "cancelled", stale: true }; }
    this.jobs[slot] = job;
    const done = await job.wait().catch((err) => ({ status: "error", error: { message: errorText(err) } }));
    if (this.jobs[slot] !== job || done.abandoned || seq !== this.seq) return { ...done, stale: true };
    this.jobs[slot] = null;
    return done;
  }

  // ----- freshness -----------------------------------------------------------------------
  async loadFreshness() {
    clear(this.freshBody).append(h("div", { class: "results-message loading", text: "Reading table metadata…" }));
    this.freshStatus.textContent = "";
    const done = await this.runJob("fresh", { kind: "freshness", channel: "freshness" });
    if (done.stale) return;
    if (done.status !== "done") {
      clear(this.freshBody).append(done.status === "cancelled"
        ? h("div", { class: "results-message", text: "Cancelled." })
        : h("div", { class: "results-error", text: done.error ? done.error.message : "Failed." }));
      return;
    }
    this.rows = done.result.rows;
    const layers = [...new Set(this.rows.map((r) => r.layer))].sort();
    const current = this.layerEl.value;
    clear(this.layerEl).append(h("option", { value: "", text: "All layers" }),
      ...layers.map((l) => h("option", { value: l, text: l })));
    this.layerEl.value = layers.includes(current) ? current : "";
    this.renderFreshness();
  }

  toggleSort(col) {
    this.sort = this.sort.col === col ? { col, dir: -this.sort.dir } : { col, dir: col === "key" || col === "layer" || col === "operation" ? 1 : -1 };
    this.renderFreshness();
  }

  renderFreshness() {
    const q = this.filterEl.value.trim().toLowerCase();
    const layer = this.layerEl.value;
    const rows = this.rows.filter((r) => (!layer || r.layer === layer)
      && (!q || r.key.toLowerCase().includes(q) || r.view_name.toLowerCase().includes(q)));
    const { col, dir } = this.sort;
    rows.sort((a, b) => {
      const av = a[col], bv = b[col];
      const an = av === null || av === undefined, bn = bv === null || bv === undefined;
      if (an || bn) return an === bn ? a.key.localeCompare(b.key) : an ? 1 : -1; // blanks last
      const c = typeof av === "number" ? av - bv : String(av).localeCompare(String(bv));
      return c * dir || a.key.localeCompare(b.key);
    });
    const cols = [
      ["key", "Table"], ["layer", "Layer"], ["last_write_time_ms", "Last write"],
      ["operation", "Operation"], ["added_records", "Rows added"], ["total_records", "Total rows"],
      ["last_published_time_ms", "Last published"], ["last_commit_message", "Commit message"],
    ];
    const head = h("tr", {}, cols.map(([c, label]) => h("th", {
      dataset: { col: c }, class: c === col ? (dir > 0 ? "sort-asc" : "sort-desc") : null,
      "aria-sort": c === col ? (dir > 0 ? "ascending" : "descending") : "none", scope: "col",
    }, h("span", { class: "cname", text: label }))));
    const body = h("tbody", {}, rows.map((r) => {
      const name = r.elements[r.elements.length - 1];
      const icon = statusIcon(r.status, r.error);
      const statusText = STATUS[r.status] ? h("span", { class: `tl-status-text ${r.status}`, text: r.error || STATUS[r.status].label }) : null;
      return h("tr", {
        dataset: { key: r.key, status: r.status }, tabindex: "0",
        class: r.key === (this.table && this.table.key) ? "selected" : null,
        title: `${r.key}\nSQL view: ${r.view_name}${r.shared ? `\nResolved from ref: ${r.source_ref}` : ""}`,
      },
      h("td", { class: "tl-name" }, icon, h("span", { class: "label", text: name }), h("span", { class: "view", text: r.view_name })),
      h("td", { text: r.layer }),
      r.status === "ok"
        ? h("td", {}, ageEl(r.last_write_time_ms))
        : h("td", { colspan: "4" }, statusText),
      r.status === "ok" ? h("td", {}, r.operation ? h("span", { class: `tl-op ${opClass(r.operation)}`, text: r.operation }) : "—") : null,
      r.status === "ok" ? h("td", { class: "num", text: num(r.added_records) }) : null,
      r.status === "ok" ? h("td", { class: "num", text: num(r.total_records) }) : null,
      h("td", {}, ageEl(r.last_published_time_ms)),
      h("td", { class: "tl-cmsg", title: r.last_commit_message || "", text: firstLine(r.last_commit_message) || "—" }));
    }));
    const table = h("table", { class: "grid tl-grid-table", "data-testid": "freshness-table" }, h("thead", {}, head), body);
    clear(this.freshBody).append(rows.length ? table : h("div", { class: "results-message", text: this.rows.length ? "No tables match the filter." : "No tables on this branch." }));
    const bad = this.rows.filter((r) => r.status !== "ok").length;
    this.freshStatus.textContent = `${fmtInt(rows.length)} of ${fmtInt(this.rows.length)} tables${bad ? ` · ${bad} unavailable` : ""}`;
  }

  openRow(tr) {
    const row = this.rows.find((r) => r.key === tr.dataset.key);
    if (row) this.showTable(row);
  }

  // ----- branch timeline -------------------------------------------------------------------
  async loadCommits(first) {
    const moreBtn = this.commitsBody.querySelector(".tl-more");
    if (first) {
      this.commits = [];
      this.nextToken = null;
      clear(this.commitsBody).append(h("div", { class: "results-message loading", text: "Reading the commit log…" }));
    } else if (moreBtn) {
      moreBtn.disabled = true;
      moreBtn.textContent = "Loading…";
    }
    const payload = { kind: "history", channel: "timeline", max_records: HISTORY_PAGE };
    if (!first && this.nextToken) payload.page_token = this.nextToken;
    const done = await this.runJob("commits", payload);
    if (done.stale) return;
    if (done.status !== "done") {
      if (first) {
        clear(this.commitsBody).append(h("div", { class: "results-error", text: done.error ? done.error.message : "Cancelled." }));
      } else if (moreBtn) {
        moreBtn.disabled = false;
        moreBtn.textContent = "Load more";
        toast(done.error ? done.error.message : "Cancelled.", 5000);
      }
      return;
    }
    const r = done.result;
    this.commits = this.commits.concat(r.entries);
    this.nextToken = r.next_token;
    this.renderCommits();
  }

  renderCommits() {
    const c = this.ctx();
    const list = h("ol", { class: "tl-commits-list", "data-testid": "commit-list" }, this.commits.map((e) => this.commitItem(e)));
    clear(this.commitsBody).append(this.commits.length ? list : h("div", { class: "results-message", text: "No commits on this branch." }));
    if (this.nextToken) {
      this.commitsBody.append(h("button", { type: "button", class: "tl-more", text: "Load more", on: { click: () => this.loadCommits(false) } }));
    }
    this.commitsStatus.textContent = `${fmtInt(this.commits.length)} commit(s) on ${c.ref}${this.nextToken ? " · more available" : ""}`;
  }

  commitItem(e) {
    const who = e.author || e.committer || "unknown";
    const meta = h("div", { class: "tl-meta" },
      h("span", { class: "tl-author", text: who, title: e.committer && e.committer !== who ? `Committer: ${e.committer}` : null }),
      " · ", ageEl(e.time_ms), " · ",
      h("button", {
        type: "button", class: "hash", text: e.short_hash, title: `Commit ${e.hash} — click to copy`,
        on: { click: async () => toast((await copyText(e.hash)) ? `Copied ${e.hash}` : "Couldn't copy to the clipboard.") },
      }));
    const chips = e.touched && e.touched.length
      ? h("div", { class: "tl-chips" }, e.touched.map((t) => h("button", {
        type: "button", class: "chip", text: t.key, title: `Show the history of ${t.key} (${t.view_name})`,
        dataset: { key: t.key }, on: { click: () => this.showTable(t) },
      })))
      : null;
    return h("li", { class: "tl-commit", dataset: { hash: e.hash } },
      h("pre", { class: "tl-msg", text: e.message || "(no message)" }), meta, chips);
  }

  // ----- table history ---------------------------------------------------------------------
  // Show `t` ({key, elements, view_name?}) in the history panel; loads the tab if needed.
  showTable(t) {
    this.table = { key: t.key, elements: t.elements, view_name: t.view_name };
    for (const tr of this.freshBody.querySelectorAll("tr.selected")) tr.classList.remove("selected");
    const tr = [...this.freshBody.querySelectorAll("tr[data-key]")].find((el) => el.dataset.key === t.key);
    if (tr) tr.classList.add("selected");
    const c = this.ctx();
    if (!c.profile || !c.ref || c.isLocal) { this.activate(); return; }
    if (this.loadedFor !== this.ctxKey()) { this.load(); return; } // loads the table too
    this.loadTable(this.table);
  }

  async loadTable(t) {
    const c = this.ctx();
    if (c.isLocal) return;
    this.tableTitle.textContent = t.key;
    clear(this.tableBody).append(h("div", { class: "results-message loading", text: `Reading the metadata of ${t.key}…` }));
    const done = await this.runJob("table", { kind: "table_history", channel: "table_history", key: t.elements || t.key });
    if (done.stale) return;
    if (done.status !== "done") {
      const err = done.error || { message: "Cancelled." };
      clear(this.tableBody).append(h("div", { class: "results-error" }, statusIcon(err.table_status, null), " ", err.message));
      return;
    }
    const r = done.result;
    this.tableTitle.textContent = `${r.key} · ${r.view_name}${r.shared ? ` · from ${r.source_ref}` : ""}`;
    const snaps = r.snapshots;
    if (!snaps.length) { clear(this.tableBody).append(h("div", { class: "results-message", text: "This table has no snapshots yet." })); return; }
    const cols = [["Time", "Snapshot time"], ["Operation", "Snapshot operation"], ["+Rows", "Rows added"],
      ["−Rows", "Rows deleted"], ["+Files", "Data files added"], ["−Files", "Data files removed"],
      ["Total", "Total rows after the snapshot"], ["Snapshot", "Snapshot id"]];
    const table = h("table", { class: "grid tl-grid-table", "data-testid": "snapshot-table" },
      h("thead", {}, h("tr", {}, cols.map(([label, tip]) => h("th", { scope: "col", title: tip }, h("span", { class: "cname", text: label }))))),
      h("tbody", {}, snaps.map((s) => h("tr", { class: s.current ? "tl-current" : null, dataset: { snapshot: s.snapshot_id } },
        h("td", {}, ageEl(s.time_ms)),
        h("td", {}, s.operation ? h("span", { class: `tl-op ${opClass(s.operation)}`, text: s.operation }) : "—"),
        h("td", { class: "num", text: num(s.added_records) }),
        h("td", { class: "num", text: num(s.deleted_records) }),
        h("td", { class: "num", text: num(s.added_files) }),
        h("td", { class: "num", text: num(s.removed_files) }),
        h("td", { class: "num", text: num(s.total_records) }),
        h("td", { class: "mono", text: s.snapshot_id + (s.current ? " (current)" : ""), title: s.parent_id ? `Parent ${s.parent_id}` : "No parent" })))));
    clear(this.tableBody).append(snapshotChart(snaps), table);
  }
}
