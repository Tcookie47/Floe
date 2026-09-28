// Table tree (SPEC §8.1, §5.4): layer → middle namespaces → table, with bronze / silver /
// gold first and other layers alphabetically; shared tables under "Reference (main)".
// The filter matches table names and SQL view names (case-insensitive) and keeps the
// parents of matches. Status icons carry the table's error message as a tooltip.
// Right-click (or Shift+F10 / the context-menu key) on a table opens a small menu of
// `menuItems` ({label, action(table)}), anchored under the row (no inline positioning).

import { clear, h } from "./dom.js";

export const REFERENCE_GROUP = "Reference (main)";
const PREFERRED = ["bronze", "silver", "gold"];

const STATUS = {
  not_found: { glyph: "?", label: "Not found" },
  corrupt: { glyph: "!", label: "Corrupt pointer" },
  collision: { glyph: "≠", label: "View name collision" },
  scope_error: { glyph: "✕", label: "Outside its container" },
  missing_data_source: { glyph: "i", label: "No data_source column" },
  error: { glyph: "✕", label: "Error" },
};

export function layerSortKey(name) {
  const lowered = name.toLowerCase();
  const i = PREFERRED.indexOf(lowered);
  return [i >= 0 ? i : PREFERRED.length, lowered];
}

function cmpKey(a, b) {
  const [ai, as] = layerSortKey(a);
  const [bi, bs] = layerSortKey(b);
  return ai - bi || as.localeCompare(bs);
}

export function displayStatus(table) {
  if (table.status === "error" && table.error && /collid|ambiguous/i.test(table.error)) return "collision";
  return table.status;
}

export function tableTooltip(table) {
  const lines = [`SQL view: ${table.view_name}`, `Key: ${table.key}`];
  if (table.shared) lines.push(`Resolved from ref: ${table.source_ref}`);
  const st = STATUS[displayStatus(table)];
  if (st) lines.push(table.error ? `${st.label}: ${table.error}` : st.label);
  return lines.join("\n");
}

export class TableTree {
  constructor(treeEl, messageEl, filterEl, { onSelect, onActivate, menuItems = [] }) {
    this.el = treeEl;
    this.messageEl = messageEl;
    this.filterEl = filterEl;
    this.onSelect = onSelect;
    this.onActivate = onActivate;
    this.menuItems = menuItems;
    this.menuEl = null;
    this.rows = new Map(); // dotted key → {table, rowEl}
    this.collapsed = new Set(); // group paths the user collapsed
    this.selectedKey = null;
    this.cursorEl = null;
    this.filterText = "";

    filterEl.addEventListener("input", () => this.applyFilter(filterEl.value));
    filterEl.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); this.moveCursor(1, true); this.el.focus(); }
      if (e.key === "Escape" && filterEl.value) { e.stopPropagation(); filterEl.value = ""; this.applyFilter(""); }
    });
    treeEl.addEventListener("click", (e) => this.onClick(e));
    treeEl.addEventListener("dblclick", (e) => this.onDblClick(e));
    treeEl.addEventListener("keydown", (e) => this.onKey(e));
    treeEl.addEventListener("contextmenu", (e) => this.onContextMenu(e));
    document.addEventListener("click", (e) => { if (this.menuEl && !this.menuEl.contains(e.target)) this.closeMenu(); });
    treeEl.addEventListener("scroll", () => this.closeMenu());
    this.showMessage("No profile selected.");
  }

  showMessage(text, { error = false } = {}) {
    this.closeMenu();
    clear(this.el);
    this.rows.clear();
    this.el.hidden = true;
    this.messageEl.hidden = false;
    this.messageEl.textContent = text;
    this.messageEl.classList.toggle("error", error);
  }

  get tables() { return Array.from(this.rows.values(), (r) => r.table); }

  setTables(tables) {
    clear(this.el);
    this.rows.clear();
    this.cursorEl = null;
    if (!tables.length) { this.showMessage("No tables on this branch."); return; }
    this.el.hidden = false;
    this.messageEl.hidden = true;
    const own = tables.filter((t) => !t.shared);
    const shared = tables.filter((t) => t.shared);
    this.addGrouped(this.el, own, [], 0);
    if (shared.length) {
      const children = this.groupNode(this.el, REFERENCE_GROUP, ["\u0000ref"], 0, {
        reference: true,
        title: "Shared reference tables, read from the main ref's head. Their views work in SQL from any branch.",
      });
      this.addGrouped(children, shared, ["\u0000ref"], 1);
    }
    if (this.selectedKey && this.rows.has(this.selectedKey)) {
      this.rows.get(this.selectedKey).rowEl.classList.add("selected");
    } else {
      this.selectedKey = null;
    }
    this.applyFilter(this.filterEl.value);
  }

  addGrouped(parentEl, tables, path, depth) {
    const tree = { groups: new Map(), tables: [] };
    for (const t of tables) {
      let node = tree;
      for (const element of t.elements.slice(0, -1)) {
        if (!node.groups.has(element)) node.groups.set(element, { groups: new Map(), tables: [] });
        node = node.groups.get(element);
      }
      node.tables.push(t);
    }
    this.fill(parentEl, tree, path, depth, true);
  }

  fill(parentEl, node, path, depth, top) {
    const names = Array.from(node.groups.keys());
    names.sort(top ? cmpKey : (a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
    for (const name of names) {
      const children = this.groupNode(parentEl, name, [...path, name], depth, {});
      this.fill(children, node.groups.get(name), [...path, name], depth + 1, false);
    }
    const leaves = node.tables.slice().sort((a, b) =>
      a.elements[a.elements.length - 1].toLowerCase().localeCompare(b.elements[b.elements.length - 1].toLowerCase()));
    for (const t of leaves) this.tableNode(parentEl, t, depth);
  }

  groupNode(parentEl, name, path, depth, { reference = false, title = null }) {
    const pathKey = path.join("\u0001");
    const collapsed = this.collapsed.has(pathKey);
    const row = h("div", {
      class: `tree-row group depth-${Math.min(depth, 6)}${reference ? " reference" : ""}`,
      role: "treeitem", "aria-expanded": String(!collapsed), dataset: { path: pathKey }, title,
    }, h("span", { class: "caret", text: collapsed ? "▸" : "▾" }), h("span", { class: "label", text: name }));
    const children = h("div", { class: `tree-children${collapsed ? " collapsed" : ""}`, role: "group" });
    parentEl.append(h("div", { class: "tree-group" }, row, children));
    return children;
  }

  tableNode(parentEl, table, depth) {
    const st = displayStatus(table);
    const icon = STATUS[st]
      ? h("span", { class: `status-icon ${st}`, text: STATUS[st].glyph, "aria-label": STATUS[st].label })
      : h("span", { class: "status-icon table-icon", "aria-hidden": "true", text: "▦" });
    const name = table.elements[table.elements.length - 1];
    const row = h("div", {
      class: `tree-row table depth-${Math.min(depth, 6)}`, role: "treeitem",
      title: tableTooltip(table), dataset: { key: table.key, view: table.view_name, status: st },
    }, h("span", { class: "caret" }), icon, h("span", { class: "label", text: name }));
    parentEl.append(row);
    this.rows.set(table.key, { table, rowEl: row, search: `${name}\n${table.view_name}`.toLowerCase() });
  }

  updateStatus(key, status, error) {
    const entry = this.rows.get(key);
    if (!entry) return;
    if (entry.table.status === status && entry.table.error === error) return;
    const table = { ...entry.table, status, error };
    const parent = entry.rowEl.parentElement;
    const depth = Number((entry.rowEl.className.match(/depth-(\d)/) || [0, 0])[1]);
    const placeholder = h("div");
    parent.replaceChild(placeholder, entry.rowEl);
    this.tableNode(parent, table, depth);
    const fresh = this.rows.get(key).rowEl;
    parent.replaceChild(fresh, placeholder);
    if (this.selectedKey === key) fresh.classList.add("selected");
    this.applyFilter(this.filterEl.value);
  }

  // ----- filter ----------------------------------------------------------------
  applyFilter(text) {
    this.filterText = text.trim().toLowerCase();
    const q = this.filterText;
    for (const { rowEl, search } of this.rows.values()) rowEl.hidden = q !== "" && !search.includes(q);
    // Groups: visible if any descendant table is visible; expanded while filtering.
    const groups = Array.from(this.el.querySelectorAll(".tree-group")).reverse();
    for (const g of groups) {
      const children = g.lastElementChild;
      const anyVisible = Array.from(children.children).some((c) => !c.hidden);
      g.hidden = !anyVisible;
      const row = g.firstElementChild;
      const collapsed = q === "" && this.collapsed.has(row.dataset.path);
      children.classList.toggle("collapsed", collapsed);
      row.setAttribute("aria-expanded", String(!collapsed));
      row.firstElementChild.textContent = collapsed ? "▸" : "▾";
    }
    if (!this.el.hidden && q && !this.visibleTableRows().length) {
      this.messageEl.hidden = false;
      this.messageEl.textContent = "No tables match the filter.";
      this.messageEl.classList.remove("error");
    } else if (!this.el.hidden) {
      this.messageEl.hidden = true;
    }
  }

  visibleRows() {
    return Array.from(this.el.querySelectorAll(".tree-row")).filter((r) => r.offsetParent !== null);
  }

  visibleTableRows() {
    return Array.from(this.rows.values()).filter((r) => r.rowEl.offsetParent !== null).map((r) => r.table);
  }

  // ----- interaction -----------------------------------------------------------------
  toggleGroup(row) {
    const path = row.dataset.path;
    if (this.collapsed.has(path)) this.collapsed.delete(path); else this.collapsed.add(path);
    const children = row.nextElementSibling;
    const collapsed = this.collapsed.has(path) && this.filterText === "";
    children.classList.toggle("collapsed", collapsed);
    row.setAttribute("aria-expanded", String(!collapsed));
    row.firstElementChild.textContent = collapsed ? "▸" : "▾";
  }

  setCursor(row) {
    if (this.cursorEl) this.cursorEl.classList.remove("cursor");
    this.cursorEl = row;
    if (row) { row.classList.add("cursor"); row.scrollIntoView({ block: "nearest" }); }
  }

  select(key, { notify = true } = {}) {
    const entry = this.rows.get(key);
    if (!entry) return false;
    for (const r of this.el.querySelectorAll(".tree-row.selected")) r.classList.remove("selected");
    entry.rowEl.classList.add("selected");
    this.selectedKey = key;
    this.setCursor(entry.rowEl);
    if (notify) this.onSelect(entry.table);
    return true;
  }

  get selected() {
    const entry = this.selectedKey && this.rows.get(this.selectedKey);
    return entry ? entry.table : null;
  }

  onClick(e) {
    if (this.menuEl && this.menuEl.contains(e.target)) return;
    const row = e.target.closest(".tree-row");
    if (!row) return;
    if (row.classList.contains("group")) { this.toggleGroup(row); this.setCursor(row); return; }
    if (row.dataset.key !== this.selectedKey) this.select(row.dataset.key);
  }

  // ----- context menu ------------------------------------------------------------------
  onContextMenu(e) {
    const row = e.target.closest(".tree-row.table");
    if (!row || !this.menuItems.length) return;
    e.preventDefault();
    this.openMenu(row);
  }

  openMenu(row) {
    this.closeMenu();
    const table = this.rows.get(row.dataset.key).table;
    this.setCursor(row);
    const menu = h("div", { class: "menu-popup tree-menu", role: "menu", "aria-label": `${table.view_name} menu` },
      this.menuItems.map((item) => h("button", {
        type: "button", role: "menuitem", text: item.label,
        on: { click: (ev) => { ev.stopPropagation(); this.closeMenu(); item.action(table); } },
      })));
    menu.addEventListener("keydown", (ev) => {
      const buttons = Array.from(menu.querySelectorAll("button"));
      const i = buttons.indexOf(document.activeElement);
      if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); this.closeMenu(); this.el.focus(); }
      else if (ev.key === "ArrowDown") { ev.preventDefault(); buttons[(i + 1) % buttons.length].focus(); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); buttons[(i - 1 + buttons.length) % buttons.length].focus(); }
    });
    row.classList.add("menu-open");
    row.append(menu);
    this.menuEl = menu;
    menu.querySelector("button").focus();
  }

  closeMenu() {
    if (!this.menuEl) return;
    const row = this.menuEl.parentElement;
    if (row) row.classList.remove("menu-open");
    this.menuEl.remove();
    this.menuEl = null;
  }

  onDblClick(e) {
    const row = e.target.closest(".tree-row.table");
    if (row) this.onActivate(this.rows.get(row.dataset.key).table);
  }

  moveCursor(delta, fromTop = false) {
    const rows = this.visibleRows();
    if (!rows.length) return;
    let i = fromTop || !this.cursorEl ? -1 : rows.indexOf(this.cursorEl);
    i = Math.max(0, Math.min(rows.length - 1, i + delta));
    this.setCursor(rows[i]);
  }

  onKey(e) {
    const row = this.cursorEl;
    if (this.menuEl && this.menuEl.contains(e.target)) return;
    if ((e.key === "F10" && e.shiftKey) || e.key === "ContextMenu") {
      if (row && row.classList.contains("table") && this.menuItems.length) { e.preventDefault(); this.openMenu(row); }
      return;
    }
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); this.moveCursor(1); break;
      case "ArrowUp": e.preventDefault(); this.moveCursor(-1); break;
      case "ArrowRight":
      case "ArrowLeft":
        if (row && row.classList.contains("group")) {
          e.preventDefault();
          const expanded = row.getAttribute("aria-expanded") === "true";
          if ((e.key === "ArrowRight") !== expanded) this.toggleGroup(row);
        }
        break;
      case "Enter":
        if (!row) break;
        e.preventDefault();
        if (row.classList.contains("group")) this.toggleGroup(row);
        else this.select(row.dataset.key);
        break;
      default:
    }
  }
}
