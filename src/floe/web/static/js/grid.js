// Results view: a status bar (status line, notices, pager, Export CSV) over a data grid
// with a sticky header, client-side sort of the loaded page, resizable columns, dimmed
// NULLs, rectangular cell selection and Ctrl/Cmd+C copy as TSV.

import { $$, clear, copyText, fmtInt, h, toast } from "./dom.js";

export const NUMERIC = /int|float|double|decimal|numeric|real|uint/i;

export function cellText(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function tsvText(value) {
  if (value === null || value === undefined) return "";
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return text.replace(/[\t\r\n]+/g, " ");
}

function compare(a, b) {
  const an = a === null || a === undefined;
  const bn = b === null || b === undefined;
  if (an || bn) return an === bn ? 0 : an ? 1 : -1; // NULLs last
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return cellText(a).localeCompare(cellText(b), undefined, { numeric: true });
}

export class ResultsView {
  constructor(host, { pageSize = 500, paged = true, exportable = true, onExport = null, onImage = null } = {}) {
    this.host = host;
    this.onImage = onImage;
    this.pageSize = pageSize;
    this.paged = paged;
    this.onExport = onExport;
    this.exportAllowed = false;
    this.exportTooltip = "";

    this.statusEl = h("span", { class: "results-status" });
    this.noticeEl = h("span", { class: "results-notice", hidden: true });
    this.pageLabel = h("span", { class: "page-label" });
    this.firstBtn = h("button", { type: "button", title: "First page", "aria-label": "First page", text: "⏮" });
    this.prevBtn = h("button", { type: "button", title: "Previous page", "aria-label": "Previous page", text: "‹" });
    this.nextBtn = h("button", { type: "button", title: "Next page", "aria-label": "Next page", text: "›" });
    this.lastBtn = h("button", { type: "button", title: "Last page", "aria-label": "Last page", text: "⏭" });
    this.pager = h("span", { class: "pager", hidden: true },
      this.firstBtn, this.prevBtn, this.pageLabel, this.nextBtn, this.lastBtn);
    this.exportBtn = h("button", { type: "button", class: "export-btn", text: "Export CSV…", disabled: true });
    this.imageBtn = h("button", { type: "button", class: "image-btn", text: "Save as image…", disabled: true });
    this.bar = h("div", { class: "results-bar" },
      this.statusEl, this.noticeEl, h("span", { class: "spacer" }), this.pager,
      exportable && onImage ? this.imageBtn : null,
      exportable ? this.exportBtn : null);
    this.body = h("div", { class: "grid-wrap", tabindex: "0" });
    clear(host).append(this.bar, this.body);

    this.firstBtn.addEventListener("click", () => this.goto(0));
    this.prevBtn.addEventListener("click", () => this.goto(this.offset - this.pageSize));
    this.nextBtn.addEventListener("click", () => this.goto(this.offset + this.pageSize));
    this.lastBtn.addEventListener("click", () =>
      this.goto(Math.floor(Math.max(0, this.total - 1) / this.pageSize) * this.pageSize));
    this.exportBtn.addEventListener("click", () => { if (this.onExport) this.onExport(this); });
    this.imageBtn.addEventListener("click", () => { if (this.onImage) this.onImage(this); });
    this.body.addEventListener("keydown", (e) => this.onKey(e));
    this.body.addEventListener("mousedown", (e) => this.onMouseDown(e));
    this.body.addEventListener("click", (e) => this.onClick(e));

    this.reset();
  }

  reset() {
    this.columns = [];
    this.rows = [];
    this.order = [];
    this.total = 0;
    this.offset = 0;
    this.fetchPage = null;
    this.sort = null;
    this.sel = null;
    this.cells = [];
    this.widths = new Map();
    this.hasResult = false;
    this.snapshotMeta = null;
    this.pager.hidden = true;
    this.updateExport();
  }

  // ----- states ------------------------------------------------------------
  setStatus(text) { this.statusEl.textContent = text || ""; }

  setNotice(text) {
    this.noticeEl.textContent = text || "";
    this.noticeEl.hidden = !text;
  }

  showMessage(text, { loading = false } = {}) {
    this.reset();
    this.setNotice("");
    clear(this.body).append(h("div", { class: `results-message${loading ? " loading" : ""}`, text }));
  }

  showInfo(text) {
    this.reset();
    clear(this.body).append(h("div", { class: "results-info", text }));
  }

  showError(message, actions = []) {
    this.reset();
    this.setNotice("");
    const box = h("div", { class: "results-error", role: "alert" }, h("div", { class: "error-text", text: message }));
    if (actions.length) {
      box.append(h("div", { class: "actions" },
        actions.map((a) => h("button", { type: "button", text: a.label, on: { click: a.onClick } }))));
    }
    clear(this.body).append(box);
  }

  setExportAllowed(allowed, tooltip) {
    this.exportAllowed = allowed;
    this.exportTooltip = tooltip || "";
    this.updateExport();
  }

  updateExport() {
    this.exportBtn.disabled = !(this.exportAllowed && this.hasResult);
    this.exportBtn.title = this.exportAllowed
      ? (this.hasResult ? "Export the rows of this result to a CSV file." : "Nothing to export yet.")
      : this.exportTooltip;
    this.imageBtn.disabled = !(this.exportAllowed && this.hasResult);
    this.imageBtn.title = this.exportAllowed
      ? (this.hasResult ? "Save the SQL and the first 50 rows as a PNG image (Ctrl/Cmd+Shift+S)." : "Nothing to save yet.")
      : this.exportTooltip;
  }

  // Rows in the current display order (client-side sort included) when the first page is
  // loaded; null when another page is showing.
  displayRows(limit) {
    if (this.offset !== 0) return null;
    return this.order.slice(0, limit).map((i) => this.rows[i]);
  }

  // result: {columns, rows, total_rows, offset}; fetchPage(offset, limit) → same shape
  showResult(result, fetchPage = null) {
    const keepWidths = this.widths;
    const sameColumns = this.columns.length === result.columns.length &&
      this.columns.every((c, i) => c.name === result.columns[i].name);
    this.columns = result.columns;
    this.rows = result.rows;
    this.total = result.total_rows ?? result.rows.length;
    this.offset = result.offset || 0;
    this.fetchPage = fetchPage;
    this.hasResult = true;
    if (!sameColumns) { this.widths = new Map(); this.sort = null; } else this.widths = keepWidths;
    this.sel = null;
    this.applySort();
    this.render();
    this.updatePager();
    this.updateExport();
  }

  async goto(offset) {
    if (!this.fetchPage) return;
    const maxOffset = Math.floor(Math.max(0, this.total - 1) / this.pageSize) * this.pageSize;
    offset = Math.max(0, Math.min(offset, maxOffset));
    if (offset === this.offset) return;
    this.setBusy(true);
    try {
      const page = await this.fetchPage(offset, this.pageSize);
      this.rows = page.rows;
      this.offset = page.offset;
      this.sel = null;
      this.applySort();
      this.render();
      this.updatePager();
    } catch (err) {
      toast(`Couldn't load that page: ${err.message}`);
    } finally {
      this.setBusy(false);
    }
  }

  setBusy(busy) {
    for (const b of [this.firstBtn, this.prevBtn, this.nextBtn, this.lastBtn]) b.disabled = busy;
    if (!busy) this.updatePager();
  }

  updatePager() {
    const multi = this.paged && this.total > this.pageSize;
    this.pager.hidden = !multi;
    if (!multi) return;
    const end = Math.min(this.total, this.offset + this.rows.length);
    this.pageLabel.textContent = `${fmtInt(this.offset + 1)}–${fmtInt(end)} of ${fmtInt(this.total)}`;
    this.firstBtn.disabled = this.prevBtn.disabled = this.offset === 0;
    this.nextBtn.disabled = this.lastBtn.disabled = end >= this.total;
  }

  // ----- sort ----------------------------------------------------------------
  applySort() {
    this.order = this.rows.map((_, i) => i);
    if (!this.sort) return;
    const { col, dir } = this.sort;
    this.order.sort((a, b) => {
      const c = compare(this.rows[a][col], this.rows[b][col]);
      return (dir === "asc" ? c : -c) || a - b;
    });
  }

  toggleSort(col) {
    if (!this.sort || this.sort.col !== col) this.sort = { col, dir: "asc" };
    else if (this.sort.dir === "asc") this.sort = { col, dir: "desc" };
    else this.sort = null;
    this.sel = null;
    this.applySort();
    this.render();
  }

  // ----- render ----------------------------------------------------------------
  defaultWidth(col, index) {
    let chars = Math.max(col.name.length, (col.type || "").length * 0.85);
    const sample = Math.min(this.rows.length, 60);
    for (let r = 0; r < sample; r++) chars = Math.max(chars, cellText(this.rows[r][index]).length);
    return Math.round(Math.min(380, Math.max(56, chars * 7.4 + 22)));
  }

  render() {
    const table = h("table", { class: "grid", role: "grid", "aria-rowcount": String(this.total) });
    const colgroup = h("colgroup");
    const numWidth = Math.max(40, String(this.offset + this.rows.length).length * 8 + 16);
    const rownumCol = h("col");
    rownumCol.style.width = `${numWidth}px`;
    colgroup.append(rownumCol);
    let totalWidth = numWidth;
    this.colEls = [];
    this.columns.forEach((col, i) => {
      if (!this.widths.has(col.name)) this.widths.set(col.name, this.defaultWidth(col, i));
      const width = this.widths.get(col.name);
      const el = h("col");
      el.style.width = `${width}px`;
      this.colEls.push(el);
      colgroup.append(el);
      totalWidth += width;
    });
    table.style.width = `${totalWidth}px`;
    this.table = table;

    const headRow = h("tr", {}, h("th", { class: "rownum", text: "#" }));
    this.columns.forEach((col, i) => {
      const sorted = this.sort && this.sort.col === i ? ` sort-${this.sort.dir}` : "";
      headRow.append(h("th", {
        class: `col-head${sorted}`, dataset: { col: String(i) }, title: `${col.name} (${col.type}) — click to sort this page`,
        scope: "col",
      },
      h("span", { class: "cname", text: col.name }),
      h("span", { class: "ctype", text: col.type || "" }),
      h("span", { class: "resizer", dataset: { col: String(i) }, "aria-hidden": "true" })));
    });
    const tbody = h("tbody");
    this.cells = [];
    const numeric = this.columns.map((c) => NUMERIC.test(c.type || ""));
    this.order.forEach((rowIndex, displayIndex) => {
      const row = this.rows[rowIndex];
      const tr = h("tr");
      tr.append(h("td", { class: "rownum", text: String(this.offset + rowIndex + 1), dataset: { r: String(displayIndex) } }));
      const rowCells = [];
      for (let c = 0; c < this.columns.length; c++) {
        const value = row[c];
        const isNull = value === null || value === undefined;
        const td = document.createElement("td");
        td.className = isNull ? "null" : numeric[c] ? "num" : "";
        td.textContent = cellText(value);
        td.dataset.r = String(displayIndex);
        td.dataset.c = String(c);
        rowCells.push(td);
        tr.append(td);
      }
      this.cells.push(rowCells);
      tbody.append(tr);
    });
    table.append(colgroup, h("thead", {}, headRow), tbody);
    clear(this.body).append(table);
    if (!this.rows.length) {
      this.body.append(h("div", { class: "results-message", text: "No rows." }));
    }
  }

  // ----- selection -----------------------------------------------------------------
  paintSelection(prev, next) {
    const each = (sel, fn) => {
      if (!sel) return;
      const [r0, r1] = [Math.min(sel.r0, sel.r1), Math.max(sel.r0, sel.r1)];
      const [c0, c1] = [Math.min(sel.c0, sel.c1), Math.max(sel.c0, sel.c1)];
      for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
        const td = this.cells[r] && this.cells[r][c];
        if (td) fn(td);
      }
    };
    each(prev, (td) => td.classList.remove("sel"));
    each(next, (td) => td.classList.add("sel"));
  }

  select(sel) {
    const prev = this.sel;
    this.sel = sel;
    this.paintSelection(prev, sel);
  }

  onMouseDown(e) {
    if (e.button !== 0) return;
    const resizer = e.target.closest(".resizer");
    if (resizer) { this.startResize(e, Number(resizer.dataset.col)); return; }
    const td = e.target.closest("td");
    if (!td || !this.cells.length) return;
    e.preventDefault();
    this.body.focus({ preventScroll: true });
    const r = Number(td.dataset.r);
    const lastCol = this.columns.length - 1;
    if (td.classList.contains("rownum")) {
      const anchor = e.shiftKey && this.sel ? this.sel.r0 : r;
      this.select({ r0: anchor, c0: 0, r1: r, c1: lastCol });
      return;
    }
    const c = Number(td.dataset.c);
    if (e.shiftKey && this.sel) this.select({ ...this.sel, r1: r, c1: c });
    else this.select({ r0: r, c0: c, r1: r, c1: c });
    const move = (ev) => {
      const over = document.elementFromPoint(ev.clientX, ev.clientY);
      const cell = over && over.closest && over.closest("td[data-c]");
      if (cell && this.body.contains(cell)) {
        this.select({ ...this.sel, r1: Number(cell.dataset.r), c1: Number(cell.dataset.c) });
      }
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  onClick(e) {
    if (e.target.closest(".resizer")) return;
    const th = e.target.closest("th.col-head");
    if (th && !this.justResized) this.toggleSort(Number(th.dataset.col));
    this.justResized = false;
  }

  startResize(e, col) {
    e.preventDefault();
    e.stopPropagation();
    const name = this.columns[col].name;
    const startX = e.clientX;
    const startWidth = this.widths.get(name);
    const handle = e.target;
    handle.classList.add("active");
    const tableStart = parseFloat(this.table.style.width) - startWidth;
    const move = (ev) => {
      const width = Math.max(40, Math.round(startWidth + ev.clientX - startX));
      this.widths.set(name, width);
      this.colEls[col].style.width = `${width}px`;
      this.table.style.width = `${tableStart + width}px`;
    };
    const up = () => {
      handle.classList.remove("active");
      this.justResized = true;
      setTimeout(() => { this.justResized = false; }, 0);
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  selectedTsv() {
    if (!this.sel) return "";
    const [r0, r1] = [Math.min(this.sel.r0, this.sel.r1), Math.max(this.sel.r0, this.sel.r1)];
    const [c0, c1] = [Math.min(this.sel.c0, this.sel.c1), Math.max(this.sel.c0, this.sel.c1)];
    const lines = [];
    const full = c0 === 0 && c1 === this.columns.length - 1 && r1 > r0;
    if (full) lines.push(this.columns.map((c) => tsvText(c.name)).join("\t"));
    for (let r = r0; r <= r1; r++) {
      const row = this.rows[this.order[r]];
      const parts = [];
      for (let c = c0; c <= c1; c++) parts.push(tsvText(row[c]));
      lines.push(parts.join("\t"));
    }
    return lines.join("\n");
  }

  async onKey(e) {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && (e.key === "c" || e.key === "C")) {
      if (!this.sel) return;
      e.preventDefault();
      const text = this.selectedTsv();
      const ok = await copyText(text);
      const count = text ? text.split("\n").length : 0;
      toast(ok ? `Copied ${fmtInt(count)} line(s) as TSV.` : "Couldn't copy to the clipboard.");
      return;
    }
    if (mod && (e.key === "a" || e.key === "A")) {
      if (!this.cells.length) return;
      e.preventDefault();
      this.select({ r0: 0, c0: 0, r1: this.cells.length - 1, c1: this.columns.length - 1 });
      return;
    }
    const moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    if (moves[e.key] && this.sel && !mod) {
      e.preventDefault();
      const [dr, dc] = moves[e.key];
      const clampR = (v) => Math.max(0, Math.min(this.cells.length - 1, v));
      const clampC = (v) => Math.max(0, Math.min(this.columns.length - 1, v));
      if (e.shiftKey) {
        this.select({ ...this.sel, r1: clampR(this.sel.r1 + dr), c1: clampC(this.sel.c1 + dc) });
      } else {
        const r = clampR(this.sel.r1 + dr);
        const c = clampC(this.sel.c1 + dc);
        this.select({ r0: r, c0: c, r1: r, c1: c });
      }
      const td = this.cells[this.sel.r1] && this.cells[this.sel.r1][this.sel.c1];
      if (td) td.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  visibleRowCount() { return $$("tbody tr", this.body).length; }
}
