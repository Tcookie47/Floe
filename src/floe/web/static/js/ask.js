// Ask panel (SPEC §15.4): describe what you want → OpenRouter → SQL into the editor.
// The generated SQL is never run: it lands in the editor for review. "What will be sent"
// shows the exact request body (schema only — never row data).

import { api, errorText, JobHandle } from "./api.js";
import { $, clear, h, toast } from "./dom.js";

export class AskPanel {
  // ctx(): {profile, ref, tenantFilter, selectedView} ; editor: SqlEditor
  constructor({ ctx, editor, onVisibility }) {
    this.ctx = ctx;
    this.editor = editor;
    this.onVisibility = onVisibility;
    this.panel = $("#ask-panel");
    this.question = $("#ask-question");
    this.statusEl = $("#ask-status");
    this.warnings = $("#ask-warnings");
    this.preview = $("#ask-preview");
    this.settings = null;
    this.job = null;
    this.dialog = $("#ask-settings-dialog");

    $("#btn-ask-toggle").addEventListener("click", () => this.toggle());
    $("#btn-ask-settings").addEventListener("click", () => this.openSettings());
    $("#btn-ask-setup").addEventListener("click", () => this.openSettings());
    $("#btn-ask-preview").addEventListener("click", () => this.showPreview());
    $("#btn-ask-generate").addEventListener("click", () => this.generate());
    $("#btn-ask-cancel").addEventListener("click", () => this.cancel());
    this.question.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); e.stopPropagation(); this.generate(); }
    });
    this.question.addEventListener("input", () => this.updateButtons());
    $("#as-save").addEventListener("click", () => this.saveSettings());
    $("#as-api-key-clear").addEventListener("click", () => {
      this.clearKey = true;
      $("#as-api-key").value = "";
      $("#as-api-key").placeholder = "(cleared — removed on Save)";
    });
    $("#as-api-key").addEventListener("input", () => { if ($("#as-api-key").value) this.clearKey = false; });
    $("#ask-settings-form").addEventListener("submit", (e) => { e.preventDefault(); this.saveSettings(); });
  }

  get configured() {
    const s = this.settings;
    return Boolean(s && s.enabled && s.model && s.api_key && s.api_key.saved);
  }

  async loadSettings() {
    try {
      this.settings = await api.get("/api/ask/settings");
    } catch {
      this.settings = null;
    }
    this.updateButtons();
  }

  toggle(force = null) {
    const show = force === null ? this.panel.hidden : force;
    this.panel.hidden = !show;
    $("#btn-ask-toggle").setAttribute("aria-pressed", String(show));
    if (show) {
      this.loadSettings();
      this.question.focus();
    }
    if (this.onVisibility) this.onVisibility(show);
  }

  updateButtons() {
    const ok = this.configured;
    const running = Boolean(this.job);
    const hasCtx = Boolean(this.ctx().profile && this.ctx().ref);
    $("#ask-not-configured").hidden = ok || this.settings === null;
    this.question.disabled = !ok;
    $("#btn-ask-generate").disabled = !ok || running || !hasCtx || !this.question.value.trim();
    $("#btn-ask-preview").disabled = !hasCtx || running;
    $("#btn-ask-cancel").disabled = !running;
    for (const r of this.panel.querySelectorAll("input[name=ask-mode]")) r.disabled = !ok;
  }

  setStatus(text, error = false) {
    this.statusEl.textContent = text || "";
    this.statusEl.classList.toggle("error", error);
  }

  payload() {
    const c = this.ctx();
    return {
      profile: c.profile,
      ref: c.ref,
      tenant_filter: c.tenantFilter,
      question: this.question.value,
      selected_view: c.selectedView,
      editor_sql: this.editor.getText().slice(0, 100000),
    };
  }

  async showPreview() {
    const c = this.ctx();
    if (!c.profile || !c.ref) { this.setStatus("Pick a profile and branch first.", true); return; }
    this.setStatus("Building the request…");
    try {
      const data = await api.post("/api/ask/preview", this.payload());
      let pretty = data.body;
      try { pretty = JSON.stringify(JSON.parse(data.body), null, 2); } catch { /* keep text */ }
      this.preview.textContent = pretty;
      this.preview.hidden = false;
      this.setStatus(`This is exactly what will be sent (schema of ${data.focus_views.length} view(s); no rows, no API key).`);
    } catch (err) {
      this.setStatus(errorText(err), true);
    }
  }

  async generate() {
    if (!this.configured || this.job) return;
    const c = this.ctx();
    if (!c.profile || !c.ref) { this.setStatus("Pick a profile and branch first.", true); return; }
    if (!this.question.value.trim()) return;
    this.warnings.hidden = true;
    clear(this.warnings);
    this.setStatus("Asking the model…");
    try {
      this.job = await JobHandle.start({ kind: "ask", channel: "ask", ...this.payload() });
      this.updateButtons();
      const job = this.job;
      const done = await job.wait({
        onProgress: (j) => { if (j.status === "running") this.setStatus(`Asking the model… ${j.elapsed.toFixed(1)} s`); },
      });
      if (this.job !== job) return;
      if (done.status === "done") {
        const r = done.result;
        const mode = this.panel.querySelector("input[name=ask-mode]:checked").value;
        if (mode === "insert") this.editor.insertAtCursor(r.sql);
        else { this.editor.setText(r.sql); this.editor.focus(); }
        this.setStatus(`SQL from ${r.model} in ${(r.elapsed_ms / 1000).toFixed(1)} s is in the editor — review it, then click Run.`);
        if (r.warnings && r.warnings.length) {
          for (const w of r.warnings) this.warnings.append(h("li", { text: w }));
          this.warnings.hidden = false;
        }
        toast("Generated SQL inserted into the editor (not run).");
      } else if (done.status === "cancelled") {
        this.setStatus("Cancelled.");
      } else {
        this.setStatus(done.error ? done.error.message : "Ask failed.", true);
      }
    } catch (err) {
      this.setStatus(errorText(err), true);
    } finally {
      this.job = null;
      this.updateButtons();
    }
  }

  async cancel() {
    if (!this.job) return;
    const job = this.job;
    this.job = null;
    await job.cancel();
    this.setStatus("Cancelled.");
    this.updateButtons();
  }

  // ----- settings dialog ------------------------------------------------------------------
  async openSettings() {
    await this.loadSettings();
    const s = this.settings || { enabled: false, model: "", base_url: "", timeout_s: 60, api_key: { saved: false } };
    $("#as-error").hidden = true;
    $("#as-enabled").checked = s.enabled;
    $("#as-model").value = s.model;
    $("#as-base-url").value = s.base_url;
    $("#as-timeout").value = String(s.timeout_s);
    $("#as-api-key").value = "";
    $("#as-api-key").placeholder = s.api_key && s.api_key.saved ? "(saved)" : "";
    $("#as-api-key-hint").textContent = `Stored in the OS keyring, never shown again. Without a keyring, set ${s.api_key_env || "FLOE_OPENROUTER_API_KEY"} instead.`;
    this.clearKey = false;
    this.dialog.showModal();
    $("#as-model").focus();
  }

  async saveSettings() {
    const timeout = Number($("#as-timeout").value);
    const body = {
      enabled: $("#as-enabled").checked,
      model: $("#as-model").value.trim(),
      base_url: $("#as-base-url").value.trim(),
      timeout_s: Number.isInteger(timeout) ? timeout : 0,
    };
    const key = $("#as-api-key").value;
    if (key) body.api_key = key;
    else if (this.clearKey) body.api_key = "";
    const box = $("#as-error");
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > 600) {
      box.textContent = "Timeout must be a whole number of seconds from 1 to 600.";
      box.hidden = false;
      return;
    }
    try {
      this.settings = await api.put("/api/ask/settings", body);
      this.dialog.close();
      toast("Ask settings saved.");
      this.updateButtons();
    } catch (err) {
      let text = errorText(err);
      if (err.status === 503 || err.type === "KeychainError") {
        text += `\nNo usable OS keyring? Leave the key empty and set ${(this.settings && this.settings.api_key_env) || "FLOE_OPENROUTER_API_KEY"} before starting Floe.`;
      }
      box.textContent = text;
      box.hidden = false;
    }
  }
}
