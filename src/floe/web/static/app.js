// Floe web UI entry point (SPEC §8, §15). Wires the top bar, table tree, Preview /
// Schema / SQL / Timeline tabs, results grids, profiles dialog, Ask panel, Help menu, keyboard
// shortcuts and UI prefs to the JSON API. Loaded as an external ES module (CSP).

import { api, authHeaders, enc, ensureApiKey, errorText, JobHandle, setUnauthorizedHandler } from "./js/api.js";
import { $, $$, clear, confirmDialog, copyText, debounce, fmtInt, fmtSecs, h, status, toast, wireDialogs } from "./js/dom.js";
import { ResultsView } from "./js/grid.js";
import { TableTree } from "./js/tree.js";
import { ProfilesDialog } from "./js/profiles.js";
import { AskPanel } from "./js/ask.js";
import { TIMELINE_CHANNELS, TimelineView } from "./js/timeline.js";
import { TextareaEditor } from "./js/fallback_editor.js";

const PAGE_SIZE = 500;
const TABS = ["preview", "schema", "sql", "timeline"];
const WORK_CHANNELS = ["preview", "schema", "sql", "ask", ...TIMELINE_CHANNELS];
const CATALOG = {
  ok: ["Catalog OK", "Nessie answered the last request."],
  stale: ["Catalog unreachable, showing last known state", "The last head refresh failed; Floe is using the last known head."],
  unknown: ["Catalog status unknown", "No catalog request has completed yet."],
  local: ["Local mode", "Local mode reads parquet files; there is no catalog."],
};
const TENANT_TIP = "Only show this branch's rows (WHERE data_source = <branch value>) in tenant tables.";
const NOT_ON_MAIN = "Not applicable on the main branch";
const EXPORT_OFF = "Export is disabled for this profile (Profiles… → Safety → Allow CSV export).";

const S = {
  profiles: [],
  profile: null, // name
  profileData: null, // non-secret profile dict
  branches: [],
  ref: null,
  head: null,
  tenantApplicable: false,
  selected: null, // TableInfo payload
  overrides: new Set(), // `${ref}\0${key}`: tenant filter off for this table's preview
  schemaCache: new Map(), // view → [column names] (editor completion)
  previewJob: null,
  schemaJob: null,
  sqlJob: null,
  sqlOverrideOnce: false,
  prefs: { last_profile: null, last_branch: {}, last_tab: null, editor_text: {} },
  tab: "preview",
  history: [],
  loadSeq: 0,
};

let editor = null;
let tree = null;
let ask = null;
let profilesDialog = null;
let previewView = null;
let schemaView = null;
let sqlView = null;
let timeline = null;

// ----- prefs ------------------------------------------------------------------------------
let pendingPrefs = {};
const flushPrefs = debounce(async () => {
  const patch = pendingPrefs;
  pendingPrefs = {};
  if (!Object.keys(patch).length) return;
  try { await api.put("/api/prefs", patch); } catch { /* prefs are best-effort */ }
}, 600);

// On page hide/unload: send what's pending right away; keepalive lets it outlive the page.
function flushPrefsNow() {
  const patch = pendingPrefs;
  pendingPrefs = {};
  if (!Object.keys(patch).length) return;
  try {
    fetch("/api/prefs", {
      method: "PUT", keepalive: true, credentials: "same-origin",
      headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(patch),
    }).catch(() => {});
  } catch { /* best effort */ }
}

function savePref(key, value, sub = null) {
  if (sub === null) pendingPrefs[key] = value;
  else pendingPrefs[key] = { ...(pendingPrefs[key] || {}), [sub]: value };
  if (sub === null) S.prefs[key] = value; else S.prefs[key] = { ...(S.prefs[key] || {}), [sub]: value };
  flushPrefs();
}

// ----- helpers ----------------------------------------------------------------------------
function tenantFilterFor(table) {
  if (table && S.overrides.has(`${S.ref}\u0000${table.key}`)) return false;
  return $("#tenant-filter").checked;
}

function summary(result, { tenantFilter, table = null, limitLabel = null }) {
  const parts = [`${fmtInt(result.total_rows)} rows`, fmtSecs(result.query_elapsed ?? 0)];
  if (result.truncated) parts.push(`Showing first ${fmtInt(result.total_rows)} rows (${limitLabel || "limit reached"})`);
  const filterable = S.tenantApplicable && !(table && table.shared);
  if (filterable && !tenantFilter) parts.push("tenant filter off");
  return parts.join(" · ");
}

async function cancelServerChannels(channels) {
  try { await api.post("/api/jobs/cancel-channels", { channels }); } catch { /* best effort */ }
}

function abandon(job) { if (job) job.abandon(); }

function setCatalog(statusName) {
  const pill = $("#catalog-pill");
  const [text, tip] = CATALOG[statusName] || CATALOG.unknown;
  pill.className = `pill pill-${CATALOG[statusName] ? statusName : "unknown"}`;
  pill.textContent = text;
  pill.title = tip;
  pill.dataset.status = statusName;
}

function setHead(head) {
  S.head = head;
  const btn = $("#head-hash");
  if (head) {
    btn.textContent = head.slice(0, 10);
    btn.title = `Head ${head} — click to copy`;
    btn.disabled = false;
  } else {
    btn.textContent = "—";
    btn.title = S.profileData && S.profileData.mode === "local" ? "Local mode: no branch head" : "No head";
    btn.disabled = true;
  }
}

function setTenantApplicable(applicable) {
  S.tenantApplicable = applicable;
  const cb = $("#tenant-filter");
  cb.disabled = !applicable;
  const label = $("#tenant-filter-label");
  label.classList.toggle("disabled", !applicable);
  label.title = applicable ? TENANT_TIP : S.ref ? NOT_ON_MAIN : "Pick a branch first";
}

function updateExportAllowed() {
  const allowed = Boolean(S.profileData && S.profileData.allow_export);
  for (const v of [previewView, sqlView]) v.setExportAllowed(allowed, EXPORT_OFF);
}

function setTab(name, { save = true } = {}) {
  if (!TABS.includes(name)) name = "preview";
  S.tab = name;
  for (const t of TABS) {
    const btn = $(`#tabbtn-${t}`);
    btn.setAttribute("aria-selected", String(t === name));
    btn.tabIndex = t === name ? 0 : -1;
    $(`#tab-${t}`).hidden = t !== name;
  }
  if (save) savePref("last_tab", name);
  if (name === "sql" && editor) setTimeout(() => editor.focus(), 0);
  if (name === "timeline" && timeline) timeline.activate();
}

function updateEditorSchema() {
  if (!editor || !tree) return;
  const views = {};
  for (const t of tree.tables) views[t.view_name] = S.schemaCache.get(t.view_name) || [];
  editor.setSchema(views);
}

// ----- profiles ---------------------------------------------------------------------------
async function loadProfiles() {
  const data = await api.get("/api/profiles");
  S.profiles = data.profiles;
  const select = $("#profile-select");
  clear(select);
  if (!S.profiles.length) {
    select.append(h("option", { value: "", text: "(no profiles)" }));
    select.disabled = true;
  } else {
    select.disabled = false;
    for (const p of S.profiles) {
      select.append(h("option", { value: p.name, text: `${p.name}${p.mode === "local" ? " (local)" : ""}` }));
    }
  }
  return S.profiles;
}

function resetWorkspace(message) {
  abandon(S.previewJob); abandon(S.schemaJob);
  S.previewJob = S.schemaJob = null;
  S.selected = null;
  S.schemaCache.clear();
  $("#preview-title").textContent = "Select a table in the tree.";
  $("#schema-title").textContent = "Select a table in the tree.";
  previewView.showMessage("");
  schemaView.showMessage("");
  tree.showMessage(message);
  if (timeline) timeline.reset();
}

async function selectProfile(name) {
  const seq = ++S.loadSeq;
  await cancelServerChannels(WORK_CHANNELS);
  if (S.sqlJob) finishSql(S.sqlJob, "Query cancelled (profile changed).");
  S.profile = name;
  S.profileData = S.profiles.find((p) => p.name === name) || null;
  S.ref = null;
  S.branches = [];
  S.overrides.clear();
  $("#profile-select").value = name || "";
  const branchSelect = $("#branch-select");
  clear(branchSelect);
  branchSelect.disabled = true;
  $("#btn-refresh").disabled = true;
  setHead(null);
  setTenantApplicable(false);
  setCatalog("unknown");
  updateExportAllowed();
  sqlView.showMessage("");
  if (!name) {
    resetWorkspace("No profiles yet. Click “Profiles…” to create one.");
    return;
  }
  savePref("last_profile", name);
  if (editor) editor.setText((S.prefs.editor_text || {})[name] || "");
  refreshHistory();
  resetWorkspace("Connecting…");
  let data;
  try {
    data = await api.get(`/api/profiles/${enc(name)}/branches`);
  } catch (err) {
    if (seq !== S.loadSeq) return;
    tree.showMessage(`Could not open the profile: ${errorText(err)}`, { error: true });
    status(`Could not list branches: ${errorText(err)}`, 10000);
    $("#btn-refresh").disabled = false;
    return;
  }
  if (seq !== S.loadSeq) return;
  setCatalog(data.catalog_status);
  S.branches = data.branches;
  if (!S.branches.length) { tree.showMessage("No branches found."); $("#btn-refresh").disabled = false; return; }
  for (const b of S.branches) {
    const inactive = b.active === false;
    branchSelect.append(h("option", {
      value: b.name, text: b.name + (inactive ? " (inactive)" : ""),
      class: inactive ? "inactive" : null, title: inactive ? "Inactive tenant (still selectable)" : null,
    }));
  }
  branchSelect.disabled = false;
  const wanted = (S.prefs.last_branch || {})[name];
  const ref = S.branches.some((b) => b.name === wanted) ? wanted : S.branches[0].name;
  await selectBranch(ref);
}

async function selectBranch(ref, { reselect = null } = {}) {
  const seq = ++S.loadSeq;
  if (S.ref !== null && S.ref !== ref) {
    await cancelServerChannels(WORK_CHANNELS);
    if (S.sqlJob) finishSql(S.sqlJob, "Query cancelled (branch changed).");
  }
  S.ref = ref;
  $("#branch-select").value = ref;
  savePref("last_branch", ref, S.profile);
  resetWorkspace("Loading tables…");
  setTenantApplicable(false);
  $("#btn-refresh").disabled = false;
  await loadTables(seq, () => api.get(`/api/profiles/${enc(S.profile)}/tables?ref=${enc(ref)}`), reselect);
  if (ask) ask.updateButtons();
}

async function loadTables(seq, fetcher, reselect) {
  let data;
  try {
    data = await fetcher();
  } catch (err) {
    if (seq !== S.loadSeq) return;
    tree.showMessage(`Could not list tables: ${errorText(err)}`, { error: true });
    status(`Could not list tables on ${S.ref}: ${errorText(err)}`, 10000);
    refreshCatalogStatus();
    return;
  }
  if (seq !== S.loadSeq) return;
  setCatalog(data.catalog_status);
  setHead(data.head);
  setTenantApplicable(data.tenant_filter_applicable);
  tree.setTables(data.tables);
  updateEditorSchema();
  status(`${data.tables.length} tables on ${S.ref}.`, 5000);
  if (timeline) timeline.invalidate();
  if (reselect && tree.select(reselect)) return;
}

async function refreshCatalogStatus() {
  if (!S.profile || !S.ref) return;
  try {
    const data = await api.get(`/api/profiles/${enc(S.profile)}/head?ref=${enc(S.ref)}`);
    setCatalog(data.catalog_status);
    setHead(data.head);
  } catch { /* keep the old state */ }
}

async function refresh() {
  if (!S.profile) return;
  if (!S.ref) { await selectProfile(S.profile); return; }
  const seq = ++S.loadSeq;
  const reselect = S.selected ? S.selected.key : null;
  status("Refreshing…", 3000);
  await loadTables(seq, () => api.post(`/api/profiles/${enc(S.profile)}/refresh`, { ref: S.ref }), reselect);
}

// ----- preview / schema -------------------------------------------------------------------
function onTableSelected(table) {
  S.selected = table;
  runTableWork();
}

function showTableHistory(table) {
  setTab("timeline");
  timeline.showTable(table);
}

function onTableActivated(table) {
  if (!editor) return;
  setTab("sql");
  editor.insertAtCursor(table.view_name);
  status(`Inserted ${table.view_name} into the SQL editor.`, 4000);
}

function tableTitle(el, prefix, table, extra) {
  clear(el).append(`${prefix} `, h("strong", { text: table.view_name }),
    h("span", { class: "muted", text: `  ${table.key}${table.shared ? ` · from ${table.source_ref}` : ""}${extra || ""}` }));
}

function runTableWork() {
  const table = S.selected;
  if (!table || !S.profile || !S.ref) return;
  const tenantFilter = tenantFilterFor(table);
  const limit = S.profileData ? S.profileData.preview_row_limit : 1000;
  tableTitle($("#preview-title"), "Preview", table, ` · first ${fmtInt(limit)} rows`);
  tableTitle($("#schema-title"), "Schema", table, "");
  runPreview(table, tenantFilter);
  runSchema(table, tenantFilter);
}

function tableError(view, table, job, rerun) {
  const err = job.error || { message: "Failed." };
  if (err.table_status) tree.updateStatus(table.key, err.table_status, err.message);
  const actions = [];
  if (err.offer_disable_tenant_filter) {
    actions.push({
      label: "Show without tenant filter",
      onClick: () => {
        S.overrides.add(`${S.ref}\u0000${table.key}`);
        status(`Tenant filter disabled for ${table.view_name} on ${S.ref}.`, 5000);
        rerun();
      },
    });
  }
  view.showError(err.message, actions);
}

async function runPreview(table, tenantFilter) {
  abandon(S.previewJob);
  previewView.showMessage(`Loading preview of ${table.view_name}…`, { loading: true });
  previewView.setStatus("");
  let job;
  try {
    job = await JobHandle.start({
      kind: "preview", channel: "preview", profile: S.profile, ref: S.ref,
      view_name: table.view_name, tenant_filter: tenantFilter,
    });
  } catch (err) { previewView.showError(errorText(err)); return; }
  S.previewJob = job;
  const done = await job.wait({ pageSize: PAGE_SIZE }).catch((err) => ({ status: "error", error: { message: errorText(err) } }));
  if (S.previewJob !== job || done.abandoned) return;
  S.previewJob = null;
  if (done.status === "done") {
    const r = done.result;
    previewView.jobId = job.id;
    previewView.showResult(r, (offset, limit) => job.page(offset, limit).then((j) => j.result));
    previewView.setStatus(summary(r, { tenantFilter, table, limitLabel: "preview row limit" }));
    previewView.setNotice(r.reloaded ? "Catalog changed — reloaded and retried." : "");
    if (table.status !== "registered" && table.status !== "unknown") tree.updateStatus(table.key, "registered", null);
    refreshCatalogStatus();
  } else if (done.status === "cancelled") {
    previewView.showMessage("Preview cancelled.");
  } else {
    tableError(previewView, table, done, runTableWork);
    refreshCatalogStatus();
  }
}

async function runSchema(table, tenantFilter) {
  abandon(S.schemaJob);
  schemaView.showMessage(`Loading schema of ${table.view_name}…`, { loading: true });
  let job;
  try {
    job = await JobHandle.start({
      kind: "schema", channel: "schema", profile: S.profile, ref: S.ref,
      view_name: table.view_name, tenant_filter: tenantFilter,
    });
  } catch (err) { schemaView.showError(errorText(err)); return; }
  S.schemaJob = job;
  const done = await job.wait().catch((err) => ({ status: "error", error: { message: errorText(err) } }));
  if (S.schemaJob !== job || done.abandoned) return;
  S.schemaJob = null;
  if (done.status === "done") {
    const cols = done.result.columns;
    S.schemaCache.set(table.view_name, cols.map((c) => c.name));
    updateEditorSchema();
    schemaView.showResult({
      columns: [{ name: "column", type: "" }, { name: "type", type: "" }, { name: "nullable", type: "" }],
      rows: cols.map((c) => [c.name, c.type, c.nullable === null || c.nullable === undefined ? null : c.nullable ? "YES" : "NO"]),
      total_rows: cols.length, offset: 0,
    });
    schemaView.setStatus(`${fmtInt(cols.length)} columns`);
  } else if (done.status === "cancelled") {
    schemaView.showMessage("Cancelled.");
  } else {
    tableError(schemaView, table, done, runTableWork);
  }
}

// ----- SQL --------------------------------------------------------------------------------
let sqlTicker = null;

function setSqlRunning(running) {
  $("#btn-run").disabled = running;
  $("#btn-cancel").disabled = !running;
  clearInterval(sqlTicker);
  sqlTicker = null;
}

function finishSql(job, message) {
  if (S.sqlJob !== job) return;
  S.sqlJob = null;
  job.abandon();
  setSqlRunning(false);
  if (message) { sqlView.showMessage(message); sqlView.setStatus(""); status(message, 5000); }
}

async function runSql() {
  if (S.sqlJob) return;
  if (!editor) return;
  if (!S.profile || !S.ref) { sqlView.showError("Pick a profile and a branch first."); return; }
  const sql = editor.getRunText();
  if (!sql.trim()) return;
  const limitInput = $("#row-limit");
  const limit = Number(limitInput.value);
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000000) {
    limitInput.classList.add("invalid");
    sqlView.showError("Row limit must be a whole number from 1 to 1,000,000.");
    return;
  }
  limitInput.classList.remove("invalid");
  let tenantFilter = $("#tenant-filter").checked;
  if (S.sqlOverrideOnce) tenantFilter = false;
  S.sqlOverrideOnce = false;
  setTab("sql", { save: S.tab !== "sql" });
  setSqlRunning(true);
  sqlView.showMessage("Running…", { loading: true });
  let job;
  try {
    job = await JobHandle.start({
      kind: "query", channel: "sql", profile: S.profile, ref: S.ref, sql, limit, tenant_filter: tenantFilter,
    });
  } catch (err) {
    setSqlRunning(false);
    sqlView.showError(errorText(err));
    return;
  }
  S.sqlJob = job;
  const started = performance.now();
  sqlView.setStatus("Running… 0.0 s");
  sqlTicker = setInterval(() => {
    if (S.sqlJob === job) sqlView.setStatus(`Running… ${((performance.now() - started) / 1000).toFixed(1)} s`);
  }, 100);
  const done = await job.wait({ pageSize: PAGE_SIZE }).catch((err) => ({ status: "error", error: { message: errorText(err) } }));
  if (S.sqlJob !== job) return;
  S.sqlJob = null;
  setSqlRunning(false);
  if (done.status === "done") {
    const r = done.result;
    sqlView.jobId = job.id;
    sqlView.showResult(r, (offset, pageLimit) => job.page(offset, pageLimit).then((j) => j.result));
    sqlView.setStatus(summary(r, { tenantFilter, limitLabel: "limit reached" }));
    sqlView.setNotice(r.reloaded ? "Catalog changed — reloaded and retried." : "");
    if (r.reloaded) status("Catalog changed — reloaded and retried.", 8000);
    refreshHistory();
    refreshCatalogStatus();
  } else if (done.status === "cancelled") {
    sqlView.showMessage("Query cancelled.");
    sqlView.setStatus("");
  } else {
    const err = done.error || { message: "Query failed." };
    const actions = [];
    if (err.offer_disable_tenant_filter) {
      actions.push({ label: "Run without tenant filter", onClick: () => { S.sqlOverrideOnce = true; runSql(); } });
    }
    sqlView.showError(err.message, actions);
    sqlView.setStatus("Query failed");
    refreshCatalogStatus();
  }
}

async function cancelSql() {
  const job = S.sqlJob;
  if (!job) return false;
  await job.cancel();
  finishSql(job, "Query cancelled.");
  return true;
}

// ----- history ----------------------------------------------------------------------------
async function refreshHistory() {
  const select = $("#history-select");
  if (!S.profile) { clear(select).append(h("option", { value: "", text: "(no history)" })); return; }
  try {
    const data = await api.get(`/api/profiles/${enc(S.profile)}/history`);
    S.history = data.entries;
  } catch { S.history = []; }
  clear(select);
  select.append(h("option", { value: "", text: S.history.length ? `(${S.history.length} recent queries)` : "(no history)" }));
  S.history.forEach((e, i) => {
    const when = e.timestamp ? new Date(e.timestamp * (e.timestamp < 1e12 ? 1000 : 1)) : null;
    const whenText = when && !Number.isNaN(when.getTime()) ? when.toLocaleString() : "";
    const first = e.sql.replace(/\s+/g, " ").trim();
    select.append(h("option", {
      value: String(i), title: e.sql,
      text: `${first.length > 70 ? `${first.slice(0, 70)}…` : first}  —  ${e.branch}${whenText ? ` · ${whenText}` : ""}`,
    }));
  });
  $("#btn-clear-history").disabled = !S.history.length;
}

// ----- export -----------------------------------------------------------------------------
async function exportCsv(view) {
  if (!view.jobId || !S.profileData || !S.profileData.allow_export) return;
  const ok = await confirmDialog(
    `Export ${fmtInt(view.total)} row(s) to a CSV file? The file is saved by your browser; Floe doesn't keep a copy.`,
    { title: "Export CSV", ok: "Export" });
  if (!ok) return;
  let response;
  try {
    response = await fetch(`/api/jobs/${enc(view.jobId)}/csv`, { credentials: "same-origin", headers: authHeaders() });
  } catch {
    toast("Can't reach the Floe server.");
    return;
  }
  if (!response.ok) {
    let message = `Export failed (HTTP ${response.status}).`;
    try { message = (await response.json()).error.message || message; } catch { /* keep */ }
    toast(message, 6000);
    return;
  }
  const blob = await response.blob();
  const match = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") || "");
  const a = h("a", { href: URL.createObjectURL(blob), download: match ? match[1] : "export.csv" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  status(`Exported ${fmtInt(response.headers.get("x-row-count") || view.total)} rows.`, 5000);
}

// ----- help menu ----------------------------------------------------------------------------
function closeHelp() {
  $("#help-menu").hidden = true;
  $("#btn-help").setAttribute("aria-expanded", "false");
}

async function helpAction(action) {
  closeHelp();
  if (action === "about") {
    try {
      const about = await api.get("/api/about");
      $("#about-version").textContent = about.version;
      $("#about-commit").textContent = about.commit;
    } catch { /* keep the template's values */ }
    $("#about-dialog").showModal();
  } else if (action === "diagnostics") {
    try {
      const data = await api.get(`/api/diagnostics${S.profile ? `?profile=${enc(S.profile)}` : ""}`);
      toast((await copyText(data.text)) ? "Diagnostics copied to the clipboard." : "Couldn't copy to the clipboard.");
    } catch (err) {
      toast(errorText(err), 6000);
    }
  } else if (action === "shortcuts") {
    $("#shortcuts-dialog").showModal();
  } else if (action === "ask-settings") {
    ask.openSettings();
  }
}

// ----- keyboard -----------------------------------------------------------------------------
function anyDialogOpen() { return $$("dialog").some((d) => d.open); }

function inEditor(target) {
  return Boolean(editor && target && editor.contains(target));
}

function onKeyDown(e) {
  if (anyDialogOpen()) return;
  // Esc cancels a running query even when the editor also used it (e.g. to close
  // its autocomplete popup).
  if (e.key === "Escape" && S.sqlJob) { cancelSql(); return; }
  if (e.defaultPrevented) return;
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key === "Enter") {
    e.preventDefault();
    if (S.tab === "sql" || inEditor(e.target)) runSql();
    return;
  }
  if (mod && !e.shiftKey && !e.altKey && (e.key === "r" || e.key === "R")) {
    e.preventDefault();
    refresh();
    return;
  }
  if (mod && !e.shiftKey && !e.altKey && (e.key === "f" || e.key === "F")) {
    if (inEditor(e.target)) return;
    e.preventDefault();
    $("#tree-filter").focus();
    $("#tree-filter").select();
    return;
  }
  if (e.altKey && !mod && ["Digit1", "Digit2", "Digit3", "Digit4"].includes(e.code)) {
    e.preventDefault();
    setTab(TABS[Number(e.code.slice(-1)) - 1]);
    return;
  }
  if (e.key === "Escape") {
    if (!$("#help-menu").hidden) { closeHelp(); return; }
    if (S.previewJob && S.tab === "preview") {
      const job = S.previewJob;
      S.previewJob = null;
      job.cancel();
      previewView.showMessage("Preview cancelled.");
    }
  }
}

// ----- init -------------------------------------------------------------------------------
async function createEditor() {
  const opts = {
    onRun: () => runSql(),
    onChange: (text) => { if (S.profile) savePref("editor_text", text, S.profile); },
  };
  const host = $("#editor");
  try {
    const mod = await import("./js/editor.js");
    return new mod.SqlEditor(host, opts);
  } catch (err) {
    console.warn("CodeMirror failed to load; using a plain editor.", err);
    clear(host);
    return new TextareaEditor(host, opts);
  }
}

function wire() {
  wireDialogs();
  $("#profile-select").addEventListener("change", (e) => selectProfile(e.target.value));
  $("#branch-select").addEventListener("change", (e) => selectBranch(e.target.value));
  $("#btn-refresh").addEventListener("click", () => refresh());
  $("#btn-profiles").addEventListener("click", () => profilesDialog.open(S.profile));
  $("#head-hash").addEventListener("click", async () => {
    if (!S.head) return;
    toast((await copyText(S.head)) ? `Copied head ${S.head}` : "Couldn't copy to the clipboard.");
  });
  $("#tenant-filter").addEventListener("change", () => { if (S.selected) runTableWork(); });
  for (const t of TABS) $(`#tabbtn-${t}`).addEventListener("click", () => setTab(t));
  $(".tabs").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const i = TABS.indexOf(S.tab) + (e.key === "ArrowRight" ? 1 : -1);
    setTab(TABS[(i + TABS.length) % TABS.length]);
    $(`#tabbtn-${S.tab}`).focus();
  });
  $("#btn-run").addEventListener("click", () => runSql());
  $("#btn-cancel").addEventListener("click", () => cancelSql());
  $("#history-select").addEventListener("change", (e) => {
    const entry = S.history[Number(e.target.value)];
    e.target.value = "";
    if (entry && editor) { editor.setText(entry.sql); editor.focus(); }
  });
  $("#btn-clear-history").addEventListener("click", async () => {
    if (!S.profile) return;
    if (!(await confirmDialog(`Clear the SQL history of "${S.profile}"?`, { title: "Clear history", ok: "Clear" }))) return;
    try { await api.del(`/api/profiles/${enc(S.profile)}/history`); } catch (err) { toast(errorText(err)); }
    refreshHistory();
  });
  $("#btn-help").addEventListener("click", (e) => {
    e.stopPropagation();
    const menu = $("#help-menu");
    menu.hidden = !menu.hidden;
    $("#btn-help").setAttribute("aria-expanded", String(!menu.hidden));
  });
  $("#help-menu").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (btn) helpAction(btn.dataset.action);
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) closeHelp(); });
  document.addEventListener("keydown", onKeyDown);
  window.addEventListener("pagehide", flushPrefsNow);
  document.addEventListener("visibilitychange", () => { if (document.hidden) flushPrefsNow(); });
}

async function onProfilesChanged({ saved = null, deleted = null, renamedFrom = null }) {
  await loadProfiles();
  const names = S.profiles.map((p) => p.name);
  if (deleted && deleted === S.profile) {
    await selectProfile(names[0] || null);
  } else if (saved && (saved === S.profile || renamedFrom === S.profile || !S.profile)) {
    // Saving drops the server-side session: reconnect with the new settings.
    await selectProfile(saved);
  } else {
    $("#profile-select").value = S.profile || "";
    S.profileData = S.profiles.find((p) => p.name === S.profile) || null;
    updateExportAllowed();
  }
}

// Shown when this tab has no (valid) API key: a new tab, or a restarted server.
function showSignedOut() {
  if (document.body.dataset.signedOut) return;
  document.body.dataset.signedOut = "true";
  document.body.replaceChildren(h("main", { class: "signed-out", "data-testid": "signed-out" },
    h("h1", { text: "Floe" }),
    h("p", { text: "This tab isn't signed in. Open Floe from the link printed in the terminal where you ran `floe serve`." }),
    h("p", { text: "That link works once. If it was already used, keep using the Floe tab you opened with it, or run `floe show --open` in a terminal for a fresh link while Floe is running." })));
}

async function main() {
  setUnauthorizedHandler(showSignedOut);
  if (!(await ensureApiKey())) {
    showSignedOut();
    return;
  }
  const defaults = JSON.parse(document.body.dataset.profileDefaults || "{}");
  previewView = new ResultsView($("#preview-results"), { pageSize: PAGE_SIZE, onExport: exportCsv });
  schemaView = new ResultsView($("#schema-results"), { paged: false, exportable: false });
  sqlView = new ResultsView($("#sql-results"), { pageSize: PAGE_SIZE, onExport: exportCsv });
  tree = new TableTree($("#tree"), $("#tree-message"), $("#tree-filter"), {
    onSelect: onTableSelected, onActivate: onTableActivated,
    menuItems: [{ label: "Show history", action: showTableHistory }],
  });
  timeline = new TimelineView({
    ctx: () => ({
      profile: S.profile, ref: S.ref, head: S.head,
      isLocal: Boolean(S.profileData && S.profileData.mode === "local"),
    }),
  });
  profilesDialog = new ProfilesDialog({ defaults, onChanged: onProfilesChanged });
  wire();
  try { S.prefs = { ...S.prefs, ...(await api.get("/api/prefs")) }; } catch { /* defaults */ }
  editor = await createEditor();
  document.body.dataset.editor = editor.kind;
  ask = new AskPanel({
    ctx: () => ({ profile: S.profile, ref: S.ref, tenantFilter: $("#tenant-filter").checked, selectedView: S.selected ? S.selected.view_name : null }),
    editor,
  });
  setTab(S.prefs.last_tab || "preview", { save: false });
  sqlView.showMessage("Write a query and press Run (Ctrl/Cmd+Enter).");
  let profiles = [];
  try {
    profiles = await loadProfiles();
  } catch (err) {
    tree.showMessage(`Could not load profiles: ${errorText(err)}`, { error: true });
    return;
  }
  const names = profiles.map((p) => p.name);
  const initial = names.includes(S.prefs.last_profile) ? S.prefs.last_profile : names[0] || null;
  document.body.dataset.ready = "true";
  await selectProfile(initial);
  if (!initial) profilesDialog.open(null);
  ask.loadSettings();
}

main();
