// Plain <textarea> SQL editor, used only if CodeMirror fails to load. Same interface as
// SqlEditor in editor.js.

export class TextareaEditor {
  constructor(host, { onRun, onChange, initialText = "" }) {
    this.kind = "textarea";
    this.host = host;
    const ta = document.createElement("textarea");
    ta.className = "editor-fallback";
    ta.spellcheck = false;
    ta.setAttribute("aria-label", "SQL editor");
    ta.setAttribute("data-testid", "sql-editor");
    ta.placeholder = "-- Write SQL here. Ctrl/Cmd+Enter runs the selection, or everything.";
    ta.value = initialText;
    ta.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); onRun(); }
      if (e.key === "Tab" && !e.shiftKey) {
        e.preventDefault();
        this.insertAtCursor("  ");
      }
    });
    ta.addEventListener("input", () => onChange && onChange(ta.value));
    host.append(ta);
    this.ta = ta;
  }

  getText() { return this.ta.value; }
  setText(text) { this.ta.value = text; }
  getRunText() {
    const { selectionStart: s, selectionEnd: e, value } = this.ta;
    return s === e ? value : value.slice(s, e);
  }
  insertAtCursor(text) { this.ta.focus(); this.ta.setRangeText(text, this.ta.selectionStart, this.ta.selectionEnd, "end"); this.ta.dispatchEvent(new Event("input")); }
  focus() { this.ta.focus(); }
  hasFocus() { return document.activeElement === this.ta; }
  setSchema() {}
  contains(node) { return node === this.host || this.host.contains(node); }
}
