// Small DOM helpers. Elements are built node by node; all data goes through textContent.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// h("div", {class: "x", title: "t", dataset: {a: 1}, on: {click: fn}}, child, "text")
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "on") for (const [ev, fn] of Object.entries(value)) el.addEventListener(ev, fn);
    else if (key === "text") el.textContent = value;
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

let toastTimer = null;
export function toast(message, ms = 3000) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

let statusTimer = null;
export function status(message, ms = 6000) {
  const el = $("#statusbar");
  el.textContent = message;
  clearTimeout(statusTimer);
  if (ms) statusTimer = setTimeout(() => { if (el.textContent === message) el.textContent = ""; }, ms);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = h("textarea", { class: "offscreen" });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

export function debounce(fn, ms) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  wrapped.flush = (...args) => { clearTimeout(timer); fn(...args); };
  return wrapped;
}

// Wire [data-close] buttons of every <dialog>.
export function wireDialogs() {
  for (const dialog of $$("dialog")) {
    for (const btn of $$("[data-close]", dialog)) btn.addEventListener("click", () => dialog.close());
  }
}

// Promise-based confirm / prompt on the shared #confirm-dialog.
export function confirmDialog(message, { title = "Confirm", ok = "OK", input = null } = {}) {
  const dialog = $("#confirm-dialog");
  const field = $("#confirm-input");
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  $("#confirm-ok").textContent = ok;
  field.hidden = input === null;
  field.value = input === null ? "" : input;
  return new Promise((resolve) => {
    const done = (value) => {
      cleanup();
      if (dialog.open) dialog.close();
      resolve(value);
    };
    const onOk = () => done(input === null ? true : field.value);
    const onCancel = () => done(input === null ? false : null);
    const onKey = (e) => { if (e.key === "Enter" && input !== null) { e.preventDefault(); onOk(); } };
    const onClose = () => done(input === null ? false : null);
    const cleanup = () => {
      $("#confirm-ok").removeEventListener("click", onOk);
      $("#confirm-cancel").removeEventListener("click", onCancel);
      field.removeEventListener("keydown", onKey);
      dialog.removeEventListener("close", onClose);
    };
    $("#confirm-ok").addEventListener("click", onOk);
    $("#confirm-cancel").addEventListener("click", onCancel);
    field.addEventListener("keydown", onKey);
    dialog.addEventListener("close", onClose);
    dialog.showModal();
    if (input !== null) { field.focus(); field.select(); } else $("#confirm-ok").focus();
  });
}

export const fmtInt = (n) => Number(n).toLocaleString("en-US");
export const fmtSecs = (s) => `${Number(s).toFixed(2)} s`;
export const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
