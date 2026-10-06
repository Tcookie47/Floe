// "Save as image": renders the SQL and the first rows of a result onto a Canvas 2D surface
// (light, print-friendly style) and saves it as a PNG. Purely client-side: nothing is sent
// to the server and nothing is logged. Text is only ever drawn with fillText.
// The pure helpers (filename, wrapping, truncation) are exported so the e2e tests can
// exercise them.

import { cellText, NUMERIC } from "./grid.js";
import { fmtInt } from "./dom.js";

export const MAX_ROWS = 50;
export const MAX_SQL_LINES = 40;
export const MAX_IMAGE_WIDTH = 2400; // css px
const PAD = 24;
const MIN_IMAGE_WIDTH = 720;
const MIN_COL = 64;
const MAX_COL = 240;
const CELL_PAD = 10;
const ROW_H = 24;
const HEAD_H = 40;
const SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace';
const C = {
  bg: "#ffffff", fg: "#1f2328", muted: "#6b7280", dim: "#9ca3af", border: "#d0d7de",
  zebra: "#f6f8fa", head: "#eef1f4", box: "#f3f4f6",
};

const pad2 = (n) => String(n).padStart(2, "0");

export function dateParts(now = new Date()) {
  const date = `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
  return { date, time: `${pad2(now.getHours())}:${pad2(now.getMinutes())}`, hhmm: `${pad2(now.getHours())}${pad2(now.getMinutes())}` };
}

const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// The view name that appears first in the SQL (case-insensitive, whole identifier), or null.
export function firstViewName(sql, viewNames) {
  const names = Array.from(new Set((viewNames || []).filter(Boolean))).sort((a, b) => b.length - a.length);
  if (!names.length || !sql) return null;
  const re = new RegExp(`(?<![A-Za-z0-9_])(${names.map(escapeRegExp).join("|")})(?![A-Za-z0-9_])`, "i");
  const m = re.exec(sql);
  if (!m) return null;
  return names.find((n) => n.toLowerCase() === m[1].toLowerCase()) || m[1];
}

export function sanitizeSubject(text, max = 32) {
  const clean = String(text || "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "").slice(0, max).replace(/_+$/g, "");
  return clean || "query";
}

// floe_<subject>_<YYYY-MM-DD>_<HHMM>.png
export function snapshotFilename(sql, viewNames, now = new Date()) {
  const { date, hhmm } = dateParts(now);
  return `floe_${sanitizeSubject(firstViewName(sql, viewNames) || "query")}_${date}_${hhmm}.png`;
}

// Shorten `text` with a trailing "…" so measure(text) <= maxWidth.
export function fitText(text, maxWidth, measure) {
  if (measure(text) <= maxWidth) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (measure(`${text.slice(0, mid)}…`) <= maxWidth) lo = mid; else hi = mid - 1;
  }
  return `${text.slice(0, lo).trimEnd()}…`;
}

// Wrap `text` to maxWidth (explicit newlines kept, tabs become two spaces, long tokens are
// broken). With maxLines, the result is cut and ends with "… (SQL truncated)".
export function wrapLines(text, maxWidth, measure, maxLines = Infinity) {
  const out = [];
  for (const raw of String(text).replace(/\r\n?/g, "\n").replace(/\t/g, "  ").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (measure(line) <= maxWidth) { out.push(line); continue; }
    const indent = /^\s*/.exec(line)[0];
    let cur = indent;
    for (const word of line.slice(indent.length).split(/(?<=\s)/)) {
      if (measure(cur + word) <= maxWidth) { cur += word; continue; }
      if (cur.trim()) { out.push(cur.replace(/\s+$/, "")); cur = indent; }
      let rest = word;
      while (measure(cur + rest) > maxWidth && rest.length > 1) {
        let n = rest.length - 1;
        while (n > 1 && measure(cur + rest.slice(0, n)) > maxWidth) n--;
        out.push(cur + rest.slice(0, n));
        rest = rest.slice(n);
        cur = indent;
      }
      cur += rest;
    }
    out.push(cur.replace(/\s+$/, ""));
  }
  if (out.length > maxLines) {
    out.length = maxLines;
    out.push("… (SQL truncated)");
  }
  return out;
}

function version() {
  const meta = document.querySelector('meta[name="floe-version"]');
  return meta ? meta.content : "";
}

// meta: {sql, branch, elapsed, truncated, tenantFilter (true/false, or null when n/a), now}
// result: {columns: [{name, type}], rows (display order, already limited), total}
export function renderSnapshot({ columns, rows, total }, meta) {
  const probe = document.createElement("canvas").getContext("2d");
  const measure = (font) => (text) => { probe.font = font; return probe.measureText(text).width; };
  const fMono = `12px ${MONO}`;
  const fHead = `600 12px ${MONO}`;
  const fType = `10px ${SANS}`;
  const mMono = measure(fMono);
  const mHead = measure(fHead);
  const mType = measure(fType);
  const shown = rows.slice(0, MAX_ROWS);

  // Column widths, then as many columns as fit.
  const numeric = columns.map((c) => NUMERIC.test(c.type || ""));
  const widths = columns.map((col, i) => {
    let w = Math.max(mHead(col.name), mType(col.type || ""));
    for (const row of shown) w = Math.max(w, mMono(cellText(row[i])));
    // Round up (+1px slack) so text measured at width w still fits after rounding.
    return Math.ceil(Math.min(MAX_COL, Math.max(MIN_COL, w + 1 + CELL_PAD * 2)));
  });
  const avail = MAX_IMAGE_WIDTH - 2 * PAD;
  let fit = 0;
  let used = 0;
  while (fit < columns.length && (fit === 0 || used + widths[fit] <= avail)) used += widths[fit++];
  const hidden = columns.length - fit;
  const note = hidden ? `+${fmtInt(hidden)} more column${hidden === 1 ? "" : "s"} not shown` : "";
  const width = Math.max(MIN_IMAGE_WIDTH, used + 2 * PAD);
  const inner = width - 2 * PAD;

  const sqlLines = wrapLines(meta.sql || "", inner - 20, mMono, MAX_SQL_LINES);
  const sqlLineH = 16;
  const sqlBoxH = sqlLines.length * sqlLineH + 20;
  const hasFooterNote = total > shown.length || note;
  let y = PAD;
  const headerH = 50;
  const yHeader = y; y += headerH + 8;
  const ySql = y; y += sqlBoxH + 16;
  const yTable = y; y += HEAD_H + shown.length * ROW_H + (shown.length ? 0 : ROW_H);
  const yNote = y + 10; y += hasFooterNote ? 30 : 6;
  const yFoot = y + 8; y += 8 + 16 + PAD;
  const height = Math.ceil(y);

  const scale = Math.max(2, Math.min(3, Math.ceil(window.devicePixelRatio || 1)));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, width, height);
  ctx.textBaseline = "middle";
  const text = (s, x, ty, font, color, align = "left") => {
    ctx.font = font; ctx.fillStyle = color; ctx.textAlign = align; ctx.fillText(s, x, ty);
  };

  // Header
  const { date, time } = dateParts(meta.now || new Date());
  text("Floe", PAD, yHeader + 12, `700 20px ${SANS}`, C.fg);
  ctx.font = `700 20px ${SANS}`;
  const fw = ctx.measureText("Floe").width;
  const where = [meta.branch, `${date} ${time}`].filter(Boolean).join("  ·  ");
  text(where, PAD + fw + 14, yHeader + 13, `13px ${SANS}`, C.muted);
  const stats = [`${fmtInt(total)} rows`, `${Number(meta.elapsed ?? 0).toFixed(2)} s`];
  if (meta.truncated) stats.push("truncated at limit");
  if (meta.tenantFilter === true) stats.push("tenant filter on");
  if (meta.tenantFilter === false) stats.push("tenant filter off");
  text(stats.join(" · "), PAD, yHeader + 38, `13px ${SANS}`, C.fg);

  // SQL block
  ctx.fillStyle = C.box;
  ctx.fillRect(PAD, ySql, inner, sqlBoxH);
  ctx.strokeStyle = C.border;
  ctx.lineWidth = 1;
  ctx.strokeRect(PAD + 0.5, ySql + 0.5, inner - 1, sqlBoxH - 1);
  sqlLines.forEach((line, i) => text(line, PAD + 10, ySql + 10 + sqlLineH * (i + 0.5), fMono, C.fg));

  // Table
  const xs = [PAD];
  for (let i = 0; i < fit; i++) xs.push(xs[i] + widths[i]);
  ctx.fillStyle = C.head;
  ctx.fillRect(PAD, yTable, used, HEAD_H);
  for (let i = 0; i < fit; i++) {
    const col = columns[i];
    const w = widths[i] - CELL_PAD * 2;
    const right = numeric[i];
    const x = right ? xs[i + 1] - CELL_PAD : xs[i] + CELL_PAD;
    const align = right ? "right" : "left";
    text(fitText(col.name, w, mHead), x, yTable + 13, fHead, C.fg, align);
    if (col.type) text(fitText(col.type, w, mType), x, yTable + 29, fType, C.muted, align);
  }
  shown.forEach((row, r) => {
    const ry = yTable + HEAD_H + r * ROW_H;
    if (r % 2 === 1) { ctx.fillStyle = C.zebra; ctx.fillRect(PAD, ry, used, ROW_H); }
    for (let i = 0; i < fit; i++) {
      const isNull = row[i] === null || row[i] === undefined;
      const right = numeric[i] && !isNull;
      const x = right ? xs[i + 1] - CELL_PAD : xs[i] + CELL_PAD;
      text(fitText(cellText(row[i]), widths[i] - CELL_PAD * 2, mMono), x, ry + ROW_H / 2,
        isNull ? `italic 12px ${MONO}` : fMono, isNull ? C.dim : C.fg, right ? "right" : "left");
    }
  });
  if (!shown.length) text("No rows.", PAD + CELL_PAD, yTable + HEAD_H + ROW_H / 2, `13px ${SANS}`, C.muted);
  const tableH = HEAD_H + Math.max(shown.length, 1) * ROW_H;
  ctx.strokeStyle = C.border;
  ctx.beginPath();
  for (let r = 0; r <= Math.max(shown.length, 1) + 1; r++) {
    const ly = Math.round(yTable + (r === 0 ? 0 : HEAD_H + (r - 1) * ROW_H)) + 0.5;
    ctx.moveTo(PAD, ly); ctx.lineTo(PAD + used, ly);
  }
  for (const x of xs) { ctx.moveTo(Math.round(x) + 0.5, yTable); ctx.lineTo(Math.round(x) + 0.5, yTable + tableH); }
  ctx.stroke();

  // Notes + footer
  const notes = [];
  if (total > shown.length) notes.push(`Showing ${fmtInt(shown.length)} of ${fmtInt(total)} rows`);
  if (note) notes.push(note);
  if (notes.length) text(notes.join("  ·  "), PAD, yNote + 8, `italic 12px ${SANS}`, C.muted);
  text(`Generated by Floe ${version()}`.trim(), PAD, yFoot + 6, `11px ${SANS}`, C.dim);
  return canvas;
}

const toBlob = (canvas) => new Promise((resolve, reject) => {
  canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Couldn't create the image."))), "image/png");
});

// Saves the PNG made by `makeCanvas()`. Uses the File System Access save dialog when the
// browser has one (the picker is opened first, while the click's user activation is fresh);
// otherwise an <a download>. Returns {method: "picker"|"download"|"cancelled", name}.
export async function saveSnapshot(filename, makeCanvas) {
  if (typeof window.showSaveFilePicker === "function") {
    let handle;
    try {
      handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: [{ description: "PNG image", accept: { "image/png": [".png"] } }],
      });
    } catch (err) {
      if (err && err.name === "AbortError") return { method: "cancelled", name: filename };
      throw err;
    }
    const blob = await toBlob(await makeCanvas());
    const writable = await handle.createWritable();
    await writable.write(blob);
    await writable.close();
    return { method: "picker", name: handle.name || filename };
  }
  const blob = await toBlob(await makeCanvas());
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return { method: "download", name: filename };
}
