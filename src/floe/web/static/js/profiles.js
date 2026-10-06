// Profiles dialog (SPEC §8.2, §8.3, §4.3, §15.2): list + grouped form, New / Duplicate /
// Delete / Import .env / Test connection / Save. Secret inputs are never pre-filled: they
// show "(saved)" when a value exists; typing sets a new value, Clear removes it on Save.

import { api, authHeaders, enc, errorText, JobHandle } from "./api.js";
import { $, clear, confirmDialog, h, toast } from "./dom.js";

const GENERAL = "General";
const STORAGE = "Storage (ADLS)";
const NESSIE = "Nessie";
const TENANT = "Tenant scoping";
const DUCKDB = "DuckDB";
const SAFETY = "Safety";
const GROUPS = [GENERAL, STORAGE, NESSIE, TENANT, DUCKDB, SAFETY];
// Hidden in local mode. (Tenant scoping stays: local mode uses shared_namespaces and
// the registry table too.)
const REMOTE_ONLY = new Set([STORAGE, NESSIE]);

const isLocal = (v) => v.mode === "local";
const accountKey = (v) => v.adls_auth === "account_key";
const servicePrincipal = (v) => v.adls_auth === "service_principal";
const oauth2 = (v) => v.nessie_auth === "oauth2";

// kind: str | opt_str | int | opt_int | pos_int | enum | bool | list | dict | secret
export const FIELDS = [
  { name: "name", label: "Name", kind: "str", group: GENERAL, placeholder: "eg-my-profile", required: true },
  { name: "mode", label: "Mode", kind: "enum", group: GENERAL, choices: ["remote", "local"] },
  { name: "local_fixture_dir", label: "Local fixture directory", kind: "opt_str", group: GENERAL,
    placeholder: "/path/to/fixtures", when: isLocal },
  { name: "adls_account", label: "Storage account", kind: "str", group: STORAGE, placeholder: "myaccount" },
  { name: "adls_auth", label: "Auth mode", kind: "enum", group: STORAGE, choices: ["account_key", "service_principal"] },
  { name: "adls_account_key", label: "Account key", kind: "secret", group: STORAGE, when: accountKey },
  { name: "adls_tenant_id", label: "Tenant ID", kind: "str", group: STORAGE, when: servicePrincipal },
  { name: "adls_client_id", label: "Client ID", kind: "str", group: STORAGE, when: servicePrincipal },
  { name: "adls_client_secret", label: "Client secret", kind: "secret", group: STORAGE, when: servicePrincipal },
  { name: "adls_ca_cert_file", label: "CA cert file", kind: "opt_str", group: STORAGE,
    placeholder: "/path/to/ca-bundle.pem (optional)" },
  { name: "nessie_uri", label: "Nessie URI", kind: "str", group: NESSIE, placeholder: "https://nessie.example/api/v2" },
  { name: "nessie_auth", label: "Auth mode", kind: "enum", group: NESSIE, choices: ["oauth2", "none"] },
  { name: "nessie_token_endpoint", label: "Token endpoint", kind: "str", group: NESSIE,
    placeholder: "https://auth.example/oauth2/token", when: oauth2 },
  { name: "nessie_client_id", label: "Client ID", kind: "str", group: NESSIE, when: oauth2 },
  { name: "nessie_scope", label: "Scope", kind: "str", group: NESSIE, when: oauth2 },
  { name: "nessie_client_secret", label: "Client secret", kind: "secret", group: NESSIE, when: oauth2 },
  { name: "nessie_main_ref", label: "Main ref", kind: "str", group: NESSIE, placeholder: "main" },
  { name: "nessie_head_ttl_seconds", label: "Head TTL (seconds)", kind: "int", group: NESSIE },
  { name: "shared_containers", label: "Shared containers (one per line)", kind: "list", group: TENANT },
  { name: "shared_namespaces", label: "Shared namespaces (one per line)", kind: "list", group: TENANT },
  { name: "tenant_registry_table", label: "Tenant registry table", kind: "opt_str", group: TENANT,
    placeholder: "registry.tenants (optional)" },
  { name: "tenant_container_map", label: "Tenant → container (key=value per line)", kind: "dict", group: TENANT },
  { name: "tenant_data_source_map", label: "Tenant → data_source (key=value per line)", kind: "dict", group: TENANT },
  { name: "duckdb_memory_limit", label: "Memory limit", kind: "opt_str", group: DUCKDB, placeholder: "4GB (optional)" },
  { name: "duckdb_threads", label: "Threads", kind: "opt_int", group: DUCKDB, placeholder: "(optional)" },
  { name: "conn_cache_max", label: "Max cached connections", kind: "pos_int", group: DUCKDB },
  { name: "allow_export", label: "Allow export", kind: "bool", group: SAFETY },
  { name: "preview_row_limit", label: "Preview row limit", kind: "pos_int", group: SAFETY },
  { name: "restrict_file_access", label: "Restrict file access", kind: "bool", group: SAFETY,
    hint: "Restrict DuckDB file access to this branch's containers. Turn off only if legitimate reads fail with 'Access denied'." },
];
const SECRETS = FIELDS.filter((f) => f.kind === "secret").map((f) => f.name);

export function secretEnvVar(profile, field) {
  const clean = (s) => s.replace(/[^A-Za-z0-9]/g, "_").toUpperCase();
  return `FLOE_SECRET__${clean(profile)}__${clean(field)}`;
}

const splitLines = (text) => text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);

function parseKv(text) {
  const out = {};
  for (const line of splitLines(text)) {
    const i = line.indexOf("=");
    if (i <= 0) continue;
    const key = line.slice(0, i).trim();
    if (key) out[key] = line.slice(i + 1).trim();
  }
  return out;
}

export class ProfilesDialog {
  constructor({ defaults, onChanged }) {
    this.dialog = $("#profiles-dialog");
    this.defaults = defaults;
    this.onChanged = onChanged;
    this.list = $("#profile-list");
    this.fieldsHost = $("#pf-fields");
    this.profiles = [];
    this.current = null; // saved name being edited, or null for a new profile
    this.secretSaved = {};
    this.secretCleared = {};
    this.importId = null;
    this.importedSecrets = new Set();
    this.requiredSecrets = new Set();
    this.keyringAvailable = true;
    this.dirty = false;
    this.testJob = null;
    this.inputs = {};
    this.buildForm();

    $("#pf-new").addEventListener("click", () => this.guard(() => this.newProfile()));
    $("#pf-duplicate").addEventListener("click", () => this.duplicate());
    $("#pf-delete").addEventListener("click", () => this.remove());
    $("#pf-import").addEventListener("click", () => $("#pf-import-file").click());
    $("#pf-import-file").addEventListener("change", (e) => this.importEnv(e.target));
    $("#pf-import-profile").addEventListener("click", () => $("#pf-import-profile-file").click());
    $("#pf-import-profile-file").addEventListener("change", (e) => this.importProfileFile(e.target));
    $("#pf-export").addEventListener("click", () => this.exportProfile());
    $("#pf-save").addEventListener("click", () => this.save());
    $("#pf-test-btn").addEventListener("click", () => this.testConnection());
    $("#profile-form").addEventListener("submit", (e) => { e.preventDefault(); this.save(); });
    this.dialog.addEventListener("close", () => { if (this.testJob) this.testJob.cancel(); });
    this.dialog.addEventListener("cancel", (e) => {
      if (this.dirty) { e.preventDefault(); this.guard(() => this.dialog.close()); }
    });
  }

  // ----- form construction --------------------------------------------------------------
  buildForm() {
    clear(this.fieldsHost);
    this.fieldsets = {};
    this.rows = {};
    for (const group of GROUPS) {
      const grid = h("div", { class: "form-grid" });
      const fs = h("fieldset", { dataset: { group } }, h("legend", { text: group }), grid);
      this.fieldsets[group] = fs;
      this.fieldsHost.append(fs);
      for (const f of FIELDS.filter((x) => x.group === group)) {
        const id = `pf-${f.name}`;
        let input;
        let control;
        if (f.kind === "enum") {
          input = h("select", { id, name: f.name }, f.choices.map((c) => h("option", { value: c, text: c })));
          control = input;
        } else if (f.kind === "bool") {
          input = h("input", { type: "checkbox", id, name: f.name });
          control = h("span", {}, input);
        } else if (f.kind === "list" || f.kind === "dict") {
          input = h("textarea", { id, name: f.name, rows: "3", spellcheck: "false" });
          control = input;
        } else if (f.kind === "secret") {
          input = h("input", { type: "password", id, name: f.name, autocomplete: "new-password", spellcheck: "false" });
          const clearBtn = h("button", { type: "button", class: "secret-clear", text: "Clear", dataset: { field: f.name } });
          clearBtn.addEventListener("click", () => this.clearSecret(f.name));
          control = h("span", { class: "secret-row" }, input, clearBtn,
            h("small", { class: "hint", id: `${id}-hint` }));
        } else {
          const numeric = ["int", "opt_int", "pos_int"].includes(f.kind);
          input = h("input", {
            type: numeric ? "number" : "text", id, name: f.name, placeholder: f.placeholder || null,
            min: numeric ? (f.kind === "int" ? "0" : "1") : null, step: numeric ? "1" : null, spellcheck: "false",
          });
          control = input;
        }
        const errorEl = h("span", { class: "field-error", id: `${id}-error`, hidden: true });
        const wrap = h("span", {}, control, f.hint ? h("small", { class: "hint", text: f.hint }) : null, errorEl);
        const label = h("label", { for: id, text: f.label });
        grid.append(label, wrap);
        this.inputs[f.name] = input;
        this.rows[f.name] = { label, wrap, errorEl };
        input.addEventListener("input", () => this.onInput(f));
        input.addEventListener("change", () => this.onInput(f));
      }
    }
  }

  onInput(field) {
    this.dirty = true;
    this.setFieldError(field.name, null);
    if (["mode", "adls_auth", "nessie_auth"].includes(field.name)) this.updateVisibility();
    if (field.kind === "secret") {
      if (this.inputs[field.name].value) this.secretCleared[field.name] = false;
      this.updateSecretHints();
    }
    if (field.name === "name") this.updateKeyringNote();
  }

  values() {
    const v = {};
    for (const f of FIELDS) {
      if (f.kind === "secret") continue;
      const input = this.inputs[f.name];
      if (f.kind === "bool") v[f.name] = input.checked;
      else v[f.name] = input.value;
    }
    return v;
  }

  updateVisibility() {
    const v = this.values();
    for (const group of GROUPS) this.fieldsets[group].hidden = REMOTE_ONLY.has(group) && isLocal(v);
    for (const f of FIELDS) {
      const visible = !f.when || f.when(v);
      this.rows[f.name].label.hidden = !visible;
      this.rows[f.name].wrap.hidden = !visible;
    }
    this.updateKeyringNote();
  }

  fill(profile) {
    for (const f of FIELDS) {
      const input = this.inputs[f.name];
      if (f.kind === "secret") { input.value = ""; continue; }
      const value = profile[f.name];
      if (f.kind === "bool") input.checked = Boolean(value);
      else if (f.kind === "list") input.value = (value || []).join("\n");
      else if (f.kind === "dict") input.value = Object.entries(value || {}).map(([k, x]) => `${k}=${x}`).join("\n");
      else input.value = value === null || value === undefined ? "" : String(value);
    }
    this.clearErrors();
    this.updateVisibility();
    this.updateSecretHints();
  }

  updateSecretHints() {
    for (const name of SECRETS) {
      const input = this.inputs[name];
      const hint = $(`#pf-${name}-hint`);
      const clearBtn = this.dialog.querySelector(`.secret-clear[data-field="${name}"]`);
      if (this.secretCleared[name]) {
        input.placeholder = "(cleared — removed on Save)";
      } else if (this.importedSecrets.has(name)) {
        input.placeholder = "(from .env — saved on Save)";
      } else {
        input.placeholder = this.secretSaved[name] ? "(saved)" : "";
      }
      clearBtn.disabled = !(this.secretSaved[name] || this.importedSecrets.has(name)) || this.secretCleared[name];
      const keyringHint = this.keyringAvailable ? "" :
        `No usable OS keyring: set ${secretEnvVar(this.inputs.name.value.trim() || "<profile>", name)} before starting Floe.`;
      const flagged = this.requiredSecrets.has(name) && !input.value;
      input.classList.toggle("needs-secret", flagged);
      hint.classList.toggle("needs-secret-hint", flagged);
      hint.textContent = flagged
        ? `Enter manually — not included in profile files.${keyringHint ? " " + keyringHint : ""}`
        : keyringHint;
    }
  }

  updateKeyringNote() {
    const note = $("#pf-keyring-note");
    const local = isLocal(this.values());
    note.hidden = this.keyringAvailable || local;
    if (!note.hidden) {
      const name = this.inputs.name.value.trim() || "<profile>";
      note.textContent = "No usable OS keyring was found on this machine, so Floe can't save secrets. " +
        "Secrets are read from environment variables instead; set them before running `floe serve`:\n" +
        SECRETS.map((s) => `  ${secretEnvVar(name, s)}`).join("\n");
    }
    this.updateSecretHints();
  }

  clearSecret(name) {
    this.inputs[name].value = "";
    this.secretCleared[name] = true;
    this.importedSecrets.delete(name);
    this.dirty = true;
    this.updateSecretHints();
  }

  // Collect + client-side validate. Returns {profile, secrets} or null (errors shown).
  collect() {
    this.clearErrors();
    const out = {};
    let ok = true;
    const fail = (name, msg) => { this.setFieldError(name, msg); ok = false; };
    for (const f of FIELDS) {
      if (f.kind === "secret") continue;
      const input = this.inputs[f.name];
      const raw = f.kind === "bool" ? input.checked : input.value;
      switch (f.kind) {
        case "bool": out[f.name] = raw; break;
        case "enum": out[f.name] = raw; break;
        case "str": out[f.name] = raw.trim(); break;
        case "opt_str": out[f.name] = raw.trim() || null; break;
        case "list": out[f.name] = splitLines(raw); break;
        case "dict": out[f.name] = parseKv(raw); break;
        default: {
          const text = raw.trim();
          if (f.kind === "opt_int" && text === "") { out[f.name] = null; break; }
          const min = f.kind === "int" ? 0 : 1;
          if (!/^\d+$/.test(text) || Number(text) < min) { fail(f.name, `Enter a whole number ≥ ${min}.`); break; }
          out[f.name] = Number(text);
        }
      }
    }
    if (!out.name) fail("name", "Profile name is required.");
    else if (/[\x00-\x1f\x7f:]/.test(out.name)) fail("name", "The name may not contain ':' or control characters.");
    if (out.mode === "local" && !out.local_fixture_dir) fail("local_fixture_dir", "Local mode needs a local fixture directory.");
    const secrets = {};
    for (const name of SECRETS) {
      const value = this.inputs[name].value;
      if (value) secrets[name] = value;
      else if (this.secretCleared[name]) secrets[name] = "";
    }
    return ok ? { profile: out, secrets } : null;
  }

  clearErrors() {
    for (const name of Object.keys(this.rows)) this.setFieldError(name, null);
    const box = $("#pf-general-error");
    box.hidden = true;
    box.textContent = "";
  }

  setFieldError(name, message) {
    const row = this.rows[name];
    if (!row) return;
    row.errorEl.hidden = !message;
    row.errorEl.textContent = message || "";
    this.inputs[name].classList.toggle("invalid", Boolean(message));
    if (message) this.inputs[name].setAttribute("aria-invalid", "true");
    else this.inputs[name].removeAttribute("aria-invalid");
  }

  showError(err) {
    const message = errorText(err);
    // Map "<field> must be …" style server messages onto the field.
    const field = FIELDS.map((f) => f.name).find((n) => new RegExp(`(^|\\W)${n}(\\W|$)`).test(message));
    if (err && err.type === "ValidationError" && field) {
      const input = this.inputs[field];
      if (this.rows[field].wrap.hidden === false || input) {
        this.setFieldError(field, message);
        const fs = this.fieldsets[FIELDS.find((f) => f.name === field).group];
        if (!fs.hidden && !this.rows[field].wrap.hidden) { input.focus(); return; }
      }
    }
    if (err && err.type === "ValidationError" && /profile name|new name/i.test(message)) {
      this.setFieldError("name", message);
      this.inputs.name.focus();
      return;
    }
    const box = $("#pf-general-error");
    let text = message;
    if (err && (err.type === "KeychainError" || err.status === 503)) {
      text = `Keychain / keyring error: ${message}`;
      if (!this.keyringAvailable) {
        const name = this.inputs.name.value.trim() || "<profile>";
        text += "\n\nNo usable OS keyring exists here, so secrets can't be saved. Leave the secret fields empty " +
          "and set these environment variables before starting Floe instead:\n" +
          SECRETS.map((s) => `  ${secretEnvVar(name, s)}`).join("\n");
      }
    }
    box.textContent = text;
    box.hidden = false;
    box.scrollIntoView({ block: "nearest" });
  }

  setStatus(text) { $("#pf-status").textContent = text || ""; }

  // ----- list ----------------------------------------------------------------------------
  async open(selectName = null) {
    // Populate the form before showing the dialog: showing it first let the user (or a
    // test) type into the form and then have the list load reset it to blank defaults.
    if (this.dialog.open) return;
    try {
      await this.reloadList();
    } catch (err) {
      this.newProfile();
      this.dialog.showModal();
      this.showError(err);
      return;
    }
    const target = selectName && this.profiles.some((p) => p.name === selectName)
      ? selectName : this.profiles.length ? this.profiles[0].name : null;
    if (target) await this.load(target); else this.newProfile();
    if (!this.dialog.open) this.dialog.showModal();
    if (target === null) this.inputs.name.focus();
  }

  async reloadList() {
    const data = await api.get("/api/profiles");
    this.profiles = data.profiles;
    this.keyringAvailable = data.keyring_available;
    this.renderList();
  }

  renderList() {
    clear(this.list);
    for (const p of this.profiles) {
      const li = h("li", { role: "option", text: p.name, dataset: { name: p.name },
        "aria-selected": String(p.name === this.current), class: p.name === this.current ? "selected" : "" });
      li.addEventListener("click", () => { if (p.name !== this.current) this.guard(() => this.load(p.name)); });
      this.list.append(li);
    }
    if (this.current === null) {
      this.list.append(h("li", { class: "new selected", role: "option", "aria-selected": "true", text: "(new profile)" }));
    }
    $("#pf-export").disabled = this.current === null;
    $("#pf-duplicate").disabled = this.current === null;
    $("#pf-delete").disabled = this.current === null;
  }

  async guard(fn) {
    if (this.dirty && !(await confirmDialog("Discard unsaved changes to this profile?", { title: "Unsaved changes", ok: "Discard" }))) return;
    fn();
  }

  resetTransient() {
    this.requiredSecrets = new Set();
    this.importId = null;
    this.importedSecrets = new Set();
    this.secretCleared = {};
    $("#pf-notes").hidden = true;
    $("#pf-test").hidden = true;
    this.setStatus("");
    if (this.testJob) { this.testJob.cancel(); this.testJob = null; }
  }

  async load(name) {
    this.resetTransient();
    try {
      const data = await api.get(`/api/profiles/${enc(name)}`);
      this.current = name;
      this.keyringAvailable = data.keyring_available;
      this.secretSaved = Object.fromEntries(Object.entries(data.secrets).map(([k, v]) => [k, v.saved]));
      this.fill(data.profile);
      this.dirty = false;
      this.renderList();
    } catch (err) {
      this.showError(err);
    }
  }

  newProfile(values = null) {
    this.resetTransient();
    this.current = null;
    this.secretSaved = {};
    this.fill({ ...this.defaults, name: "", ...(values || {}) });
    this.dirty = false;
    this.renderList();
    this.inputs.name.focus();
  }

  async duplicate() {
    if (this.current === null) return;
    const source = this.current;
    const newName = await confirmDialog(`Name for the copy of "${source}":`, {
      title: "Duplicate profile", ok: "Duplicate", input: `${source} copy`,
    });
    if (newName === null) return;
    try {
      const data = await api.post(`/api/profiles/${enc(source)}/duplicate`, { new_name: newName });
      await this.reloadList();
      await this.load(data.profile.name);
      toast(`Duplicated "${source}" as "${data.profile.name}".`);
      this.onChanged({ saved: data.profile.name });
    } catch (err) {
      this.showError(err);
    }
  }

  async remove() {
    if (this.current === null) return;
    const name = this.current;
    if (!(await confirmDialog(`Delete profile "${name}"? Its saved secrets and SQL history are deleted too.`,
      { title: "Delete profile", ok: "Delete" }))) return;
    try {
      await api.del(`/api/profiles/${enc(name)}`);
      this.dirty = false;
      await this.reloadList();
      if (this.profiles.length) await this.load(this.profiles[0].name); else this.newProfile();
      toast(`Deleted "${name}".`);
      this.onChanged({ deleted: name });
    } catch (err) {
      this.showError(err);
    }
  }

  // ----- .env import ---------------------------------------------------------------------
  async importEnv(input) {
    const file = input.files && input.files[0];
    input.value = "";
    if (!file) return;
    if (file.size > 256 * 1024) { this.showError(new Error("The .env file is too large.")); return; }
    try {
      const text = await file.text();
      const data = await api.post("/api/profiles/import-env", { text });
      const current = this.values();
      const updates = { ...data.fields };
      if (Array.isArray(updates.shared_containers)) {
        const existing = current.shared_containers ? splitLines(current.shared_containers) : [];
        updates.shared_containers = [...existing, ...updates.shared_containers.filter((c) => !existing.includes(c))];
      }
      for (const [key, value] of Object.entries(updates)) {
        const f = FIELDS.find((x) => x.name === key);
        if (!f) continue;
        const el = this.inputs[key];
        if (f.kind === "bool") el.checked = Boolean(value);
        else if (f.kind === "list") el.value = (value || []).join("\n");
        else if (f.kind === "dict") el.value = Object.entries(value || {}).map(([k, x]) => `${k}=${x}`).join("\n");
        else el.value = value === null || value === undefined ? "" : String(value);
      }
      if (current.mode === "local" && Object.keys(updates).length) this.inputs.mode.value = "remote";
      this.importId = data.import_id;
      this.importedSecrets = new Set(data.secrets_present);
      for (const s of data.secrets_present) this.secretCleared[s] = false;
      this.dirty = true;
      this.updateVisibility();
      const notes = [...data.notes];
      notes.unshift(`Imported ${Object.keys(data.fields).length} field(s)` +
        (data.secrets_present.length ? ` and ${data.secrets_present.length} secret(s)` : "") +
        ` from ${file.name}. Nothing is saved until you click Save.`);
      const box = $("#pf-notes");
      box.textContent = notes.join("\n");
      box.hidden = false;
    } catch (err) {
      this.showError(err);
    }
  }

  // ----- profile export / import ---------------------------------------------------------
  async exportProfile() {
    if (this.current === null) return;
    const name = this.current;
    const ok = await confirmDialog(
      "Settings only — no keys, secrets or passwords are included. The file does contain your " +
      "Nessie address, storage account, containers and branch names, so share it only with " +
      "people who should have them.",
      { title: `Export profile "${name}"`, ok: "Export" });
    if (!ok) return;
    try {
      const response = await fetch(`/api/profiles/${enc(name)}/export`, {
        headers: authHeaders({ Accept: "application/json" }), credentials: "same-origin",
      });
      if (!response.ok) {
        let payload = null;
        try { payload = (await response.json()).error; } catch { payload = null; }
        throw Object.assign(new Error((payload && payload.message) || `Export failed (HTTP ${response.status}).`),
          { type: payload && payload.type, status: response.status });
      }
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = /filename="([^"]+)"/.exec(disposition);
      const filename = match ? match[1] : "profile.floe-profile.json";
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = h("a", { href: url, download: filename });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      toast(`Exported "${name}" to ${filename}.`);
    } catch (err) {
      this.showError(err);
    }
  }

  uniqueName(base) {
    const taken = new Set(this.profiles.map((p) => p.name));
    if (!taken.has(base)) return base;
    let candidate = `${base} (imported)`;
    for (let n = 2; taken.has(candidate); n += 1) candidate = `${base} (imported ${n})`;
    return candidate;
  }

  async importProfileFile(input) {
    const file = input.files && input.files[0];
    input.value = "";
    if (!file) return;
    if (this.dirty && !(await confirmDialog("Discard unsaved changes to this profile?",
      { title: "Unsaved changes", ok: "Discard" }))) return;
    if (file.size > 256 * 1024) { this.showError(new Error("The profile file is too large.")); return; }
    try {
      const text = await file.text();
      const data = await api.post("/api/profiles/import-profile", { text });
      const fields = { ...data.fields };
      const original = String(fields.name || "");
      fields.name = this.uniqueName(original);
      this.newProfile(fields);
      this.dirty = true;
      this.requiredSecrets = new Set(data.missing_secrets);
      this.updateSecretHints();
      const notes = [`Imported profile settings from ${file.name}. Nothing is saved until you click Save.`];
      if (fields.name !== original) notes.push(`A profile named "${original}" already exists, so this one is named "${fields.name}".`);
      if (data.missing_secrets.length) {
        const labels = data.missing_secrets.map((s) => FIELDS.find((f) => f.name === s).label);
        notes.push(`Enter manually — not included in profile files: ${labels.join(", ")}.`);
      }
      notes.push(...data.notes);
      const box = $("#pf-notes");
      box.textContent = notes.join("\n");
      box.hidden = false;
    } catch (err) {
      this.showError(err);
    }
  }

  // ----- save ------------------------------------------------------------------------------
  async save() {
    const collected = this.collect();
    if (!collected) { this.setStatus("Fix the highlighted fields."); return; }
    const { profile, secrets } = collected;
    const body = { profile, secrets };
    if (this.importId) body.import_id = this.importId;
    $("#pf-save").disabled = true;
    this.setStatus("Saving…");
    let renamedFrom = null;
    try {
      let data;
      if (this.current === null) {
        data = await api.post("/api/profiles", body);
      } else {
        if (profile.name !== this.current) {
          await api.post(`/api/profiles/${enc(this.current)}/rename`, { new_name: profile.name });
          renamedFrom = this.current;
          this.current = profile.name;
        }
        data = await api.put(`/api/profiles/${enc(profile.name)}`, body);
      }
      this.current = data.profile.name;
      this.secretSaved = Object.fromEntries(Object.entries(data.secrets).map(([k, v]) => [k, v.saved]));
      this.keyringAvailable = data.keyring_available;
      this.importId = null;
      this.importedSecrets = new Set();
      this.requiredSecrets = new Set();
      this.secretCleared = {};
      this.fill(data.profile);
      this.dirty = false;
      await this.reloadList();
      this.setStatus(`Saved "${data.profile.name}".`);
      this.onChanged({ saved: data.profile.name, renamedFrom });
    } catch (err) {
      this.setStatus("");
      if (renamedFrom) { await this.reloadList(); this.onChanged({ saved: this.current, renamedFrom }); }
      if (err.type === "ImportExpired") this.importId = null;
      this.showError(err);
    } finally {
      $("#pf-save").disabled = false;
    }
  }

  // ----- test connection -----------------------------------------------------------------
  async testConnection() {
    const collected = this.collect();
    if (!collected) { this.setStatus("Fix the highlighted fields."); return; }
    const { profile, secrets } = collected;
    const payload = {
      kind: "test_connection",
      profile: this.current || profile.name,
      profile_form: profile,
      secrets,
      channel: "test_connection",
    };
    if (this.importId) payload.import_id = this.importId;
    const box = $("#pf-test");
    const list = $("#pf-test-steps");
    const summary = $("#pf-test-summary");
    box.hidden = false;
    clear(list);
    summary.className = "";
    summary.textContent = "Running…";
    const btn = $("#pf-test-btn");
    btn.disabled = true;
    if (this.testJob) this.testJob.cancel();
    const render = (steps) => {
      clear(list);
      for (const s of steps) {
        list.append(h("li", { class: s.ok ? "ok" : "fail" },
          h("span", { class: s.ok ? "step-ok" : "step-fail", text: s.ok ? "✓ " : "✗ " }),
          h("strong", { text: s.name }), " ",
          h("span", { class: "step-msg", text: `${s.message} (${Math.round(s.elapsed_ms)} ms)` })));
      }
    };
    try {
      const job = await JobHandle.start(payload);
      this.testJob = job;
      const done = await job.wait({ onProgress: (j) => render(j.steps || []) });
      if (this.testJob !== job) return;
      render(done.steps || (done.result && done.result.steps) || []);
      if (done.status === "done") {
        const ok = done.result.ok;
        summary.className = ok ? "test-summary-ok" : "test-summary-fail";
        const failed = (done.result.steps || []).find((s) => !s.ok);
        summary.textContent = ok ? "All checks passed." : `Failed at "${failed ? failed.name : "?"}": ${failed ? failed.message : ""}`;
      } else if (done.status === "cancelled") {
        summary.className = "";
        summary.textContent = "Cancelled.";
      } else {
        summary.className = "test-summary-fail";
        summary.textContent = done.error ? done.error.message : "Test failed.";
      }
    } catch (err) {
      summary.className = "test-summary-fail";
      summary.textContent = errorText(err);
    } finally {
      btn.disabled = false;
    }
  }
}
