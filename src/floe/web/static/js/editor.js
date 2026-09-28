// SQL editor: CodeMirror 6 (vendored, see static/vendor/README.md), mounted inside a
// shadow root. CodeMirror's style-mod injects a <style> element into a document (which
// the page's CSP forbids) but uses constructable stylesheets (adoptedStyleSheets) in a
// shadow root, so the editor lives in one. Colours come from CSS variables on :root,
// which inherit into the shadow tree, so light/dark follows the page.

import { EditorState, Compartment, Prec } from "../vendor/codemirror/state.js";
import {
  EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter,
  drawSelection, placeholder, highlightSpecialChars,
} from "../vendor/codemirror/view.js";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "../vendor/codemirror/commands.js";
import {
  syntaxHighlighting, HighlightStyle, bracketMatching, indentOnInput,
} from "../vendor/codemirror/language.js";
import {
  autocompletion, completionKeymap, closeBrackets, closeBracketsKeymap,
} from "../vendor/codemirror/autocomplete.js";
import { searchKeymap, highlightSelectionMatches } from "../vendor/codemirror/search.js";
import { sql, PostgreSQL } from "../vendor/codemirror/lang-sql.js";
import { tags as t } from "../vendor/codemirror/lezer-highlight.js";

const highlight = HighlightStyle.define([
  { tag: [t.keyword, t.operatorKeyword, t.modifier], color: "var(--syn-keyword)", fontWeight: "600" },
  { tag: [t.string, t.special(t.string)], color: "var(--syn-string)" },
  { tag: [t.number, t.bool, t.null], color: "var(--syn-number)" },
  { tag: [t.lineComment, t.blockComment, t.comment], color: "var(--syn-comment)", fontStyle: "italic" },
  { tag: [t.typeName, t.standard(t.name)], color: "var(--syn-type)" },
  { tag: [t.operator, t.punctuation], color: "var(--syn-operator)" },
  { tag: [t.name, t.special(t.name)], color: "var(--syn-name)" },
]);

const theme = EditorView.theme({
  "&": {
    height: "100%", fontSize: "13px", color: "var(--fg)", backgroundColor: "var(--bg)",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
    lineHeight: "1.5",
  },
  ".cm-content": { caretColor: "var(--cursor)", padding: "6px 0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--cursor)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection":
    { backgroundColor: "var(--editor-sel)" },
  ".cm-gutters": {
    backgroundColor: "var(--bg-alt)", color: "var(--fg-dim)", borderRight: "1px solid var(--border)",
  },
  ".cm-activeLine": { backgroundColor: "var(--active-line)" },
  ".cm-activeLineGutter": { backgroundColor: "var(--bg-hover)" },
  ".cm-placeholder": { color: "var(--fg-dim)" },
  ".cm-tooltip": {
    backgroundColor: "var(--bg)", color: "var(--fg)", border: "1px solid var(--border)",
  },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
    backgroundColor: "var(--bg-sel)", color: "var(--fg)",
  },
  ".cm-panels": { backgroundColor: "var(--bg-alt)", color: "var(--fg)" },
  ".cm-searchMatch": { backgroundColor: "var(--warn-bg)" },
  ".cm-matchingBracket": { backgroundColor: "var(--bg-sel)", outline: "none" },
});

export class SqlEditor {
  // host: an element; onRun(): Ctrl/Cmd+Enter; onChange(text)
  constructor(host, { onRun, onChange, initialText = "" }) {
    this.kind = "codemirror";
    const shadow = host.attachShadow({ mode: "open" });
    const mount = document.createElement("div");
    mount.className = "cm-mount";
    shadow.append(mount);
    this.host = host;
    this.language = new Compartment();
    const run = () => { onRun(); return true; };
    this.view = new EditorView({
      parent: mount,
      state: EditorState.create({
        doc: initialText,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          highlightSpecialChars(),
          history(),
          drawSelection(),
          indentOnInput(),
          bracketMatching(),
          closeBrackets(),
          autocompletion({ activateOnTyping: true }),
          highlightActiveLine(),
          highlightSelectionMatches(),
          syntaxHighlighting(highlight),
          placeholder("-- Write SQL here. Ctrl/Cmd+Enter runs the selection, or everything."),
          this.language.of(sql({ dialect: PostgreSQL, upperCaseKeywords: true })),
          Prec.highest(keymap.of([{ key: "Mod-Enter", run, preventDefault: true }])),
          keymap.of([
            ...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap,
            ...completionKeymap, indentWithTab,
          ]),
          theme,
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ "aria-label": "SQL editor", "data-testid": "sql-editor" }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged && onChange) onChange(this.getText());
          }),
        ],
      }),
    });
    // mount must fill the host
    mount.style.height = "100%";
  }

  getText() { return this.view.state.doc.toString(); }

  setText(text) {
    this.view.dispatch({
      changes: { from: 0, to: this.view.state.doc.length, insert: text },
      selection: { anchor: text.length },
    });
  }

  // The selected text if any, else the whole document.
  getRunText() {
    const { from, to } = this.view.state.selection.main;
    return from === to ? this.getText() : this.view.state.sliceDoc(from, to);
  }

  insertAtCursor(text) {
    const { from, to } = this.view.state.selection.main;
    this.view.dispatch({
      changes: { from, to, insert: text },
      selection: { anchor: from + text.length },
      scrollIntoView: true,
    });
    this.focus();
  }

  focus() { this.view.focus(); }

  hasFocus() { return this.view.hasFocus; }

  // views: {view_name: [column, ...]} for completion
  setSchema(views) {
    this.view.dispatch({
      effects: this.language.reconfigure(sql({ dialect: PostgreSQL, upperCaseKeywords: true, schema: views })),
    });
  }

  contains(node) { return node === this.host || this.host.contains(node); }
}
