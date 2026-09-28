# Vendored frontend libraries

Everything the browser UI loads is served from this package (no CDN, works offline,
CSP `default-src 'self'`). No Node build step.

## CodeMirror 6 (SQL editor) — MIT

`codemirror/` holds the ES-module entry file of each package below, copied from the
npm registry tarballs by `scripts/vendor_codemirror.py`. The only change is that bare
import specifiers are rewritten to relative paths (`'@codemirror/state'` →
`'./state.js'`), because an import map would be an inline `<script>` that the CSP
forbids. Each package's licence is next to it as `LICENSE-<name>`.

| File | npm package | Version |
|---|---|---|
| `state.js` | `@codemirror/state` | 6.7.6 |
| `view.js` | `@codemirror/view` | 6.43.13 |
| `language.js` | `@codemirror/language` | 6.12.4 |
| `commands.js` | `@codemirror/commands` | 6.11.1 |
| `autocomplete.js` | `@codemirror/autocomplete` | 6.20.3 |
| `search.js` | `@codemirror/search` | 6.7.2 |
| `lang-sql.js` | `@codemirror/lang-sql` | 6.10.0 |
| `lezer-common.js` | `@lezer/common` | 1.5.3 |
| `lezer-highlight.js` | `@lezer/highlight` | 1.2.5 |
| `lezer-lr.js` | `@lezer/lr` | 1.4.10 |
| `style-mod.js` | `style-mod` | 4.1.4 |
| `w3c-keyname.js` | `w3c-keyname` | 2.2.8 |
| `crelt.js` | `crelt` | 1.0.7 |
| `find-cluster-break.js` | `@marijn/find-cluster-break` | 1.0.4 |

CSP note: `style-mod` injects a `<style>` element when mounted in a document (blocked by
the CSP), but uses constructable stylesheets (`adoptedStyleSheets`) inside a shadow root,
so `static/js/editor.js` mounts the editor in a shadow root. If CodeMirror fails to load,
the UI falls back to a plain `<textarea>` editor (`static/js/fallback_editor.js`).

To upgrade: bump the versions in `scripts/vendor_codemirror.py`, run it, update this
table, and run the web tests (including the `e2e` browser tests).

## htmx

Not used: the UI is a small set of vanilla ES modules over the JSON API.
