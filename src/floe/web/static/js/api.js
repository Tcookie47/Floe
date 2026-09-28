// JSON API client and job polling for the Floe web UI.
// Every request is same-origin; fetch sends the Origin header the server checks on
// state-changing methods. Authentication is the HttpOnly session cookie *plus* the
// per-launch API key in the X-Floe-Auth header (browsers send the cookie to every
// 127.0.0.1 port; the key they don't). The server delivers the key once, in the URL
// fragment of the post-sign-in refresh (`/#k=…`); it's kept in sessionStorage, which is
// isolated per origin including the port, and the fragment is removed from the URL.

const KEY_NAME = "floe-api-key";
const KEY_HEADER = "X-Floe-Auth";
let memoryKey = null;

function storedKey() {
  try { return sessionStorage.getItem(KEY_NAME); } catch { return null; }
}

function rememberKey(key) {
  memoryKey = key;
  try { sessionStorage.setItem(KEY_NAME, key); } catch { /* memory only */ }
}

(function captureKeyFromFragment() {
  const m = /^#k=([A-Za-z0-9_-]+)$/.exec(window.location.hash || "");
  if (!m) return;
  rememberKey(m[1]);
  history.replaceState(null, "", window.location.pathname + window.location.search);
})();

export function apiKey() { return memoryKey || storedKey(); }

export function authHeaders(extra = {}) {
  const key = apiKey();
  return key ? { ...extra, [KEY_HEADER]: key } : { ...extra };
}

// A tab opened later (new tab, bookmark) has no key in its sessionStorage. It asks the
// other Floe tabs of the same origin (BroadcastChannel is same-origin, port included);
// if none answers, the page shows "open Floe from the terminal link".
const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("floe-auth") : null;
let keyWaiter = null;
if (channel) {
  channel.onmessage = (event) => {
    const data = event.data || {};
    if (data.type === "need-key" && apiKey()) {
      channel.postMessage({ type: "key", key: apiKey() });
    } else if (data.type === "key" && typeof data.key === "string" && !apiKey()) {
      rememberKey(data.key);
      if (keyWaiter) keyWaiter(true);
    }
  };
}

export function ensureApiKey(timeoutMs = 600) {
  if (apiKey()) return Promise.resolve(true);
  if (!channel) return Promise.resolve(false);
  return new Promise((resolve) => {
    const done = (ok) => { keyWaiter = null; resolve(ok); };
    keyWaiter = done;
    channel.postMessage({ type: "need-key" });
    setTimeout(() => done(Boolean(apiKey())), timeoutMs);
  });
}

let onUnauthorized = null;
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

export class ApiError extends Error {
  constructor(status, payload) {
    const p = payload || {};
    super(p.message || `Request failed (HTTP ${status}).`);
    this.status = status;
    this.type = p.type || "HTTPError";
    this.payload = p;
  }
}

async function parse(response) {
  const text = await response.text();
  let body = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = null; }
  }
  if (response.status === 401 && onUnauthorized) onUnauthorized();
  if (!response.ok) {
    const payload = body && body.error ? body.error : { message: `Request failed (HTTP ${response.status}).` };
    throw new ApiError(response.status, payload);
  }
  return body;
}

export async function request(method, url, body) {
  const init = { method, headers: authHeaders({ Accept: "application/json" }), credentials: "same-origin" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiError(0, { type: "NetworkError", message: "Can't reach the Floe server. Is `floe serve` still running?" });
  }
  return parse(response);
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body === undefined ? {} : body),
  put: (url, body) => request("PUT", url, body),
  del: (url) => request("DELETE", url),
};

export const enc = encodeURIComponent;

const FINISHED = new Set(["done", "error", "cancelled"]);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// A submitted server-side job. `wait()` polls until it finishes; `cancel()` asks the
// server to cancel it (and makes `wait()` resolve with status "cancelled" promptly).
export class JobHandle {
  constructor(job) {
    this.id = job.id;
    this.job = job;
    this.cancelled = false;
    this.abandoned = false;
    this.started = performance.now();
  }

  static async start(payload) {
    return new JobHandle(await api.post("/api/jobs", payload));
  }

  async wait({ onProgress, pageSize = 500 } = {}) {
    let delay = 80;
    for (;;) {
      if (this.abandoned) return { ...this.job, status: "cancelled", abandoned: true };
      const job = await api.get(`/api/jobs/${enc(this.id)}?offset=0&limit=${pageSize}`);
      this.job = job;
      if (onProgress) onProgress(job);
      if (FINISHED.has(job.status)) return job;
      await sleep(delay);
      delay = Math.min(400, Math.round(delay * 1.4));
    }
  }

  async page(offset, limit) {
    return api.get(`/api/jobs/${enc(this.id)}?offset=${offset}&limit=${limit}`);
  }

  async cancel() {
    this.cancelled = true;
    try {
      await api.post(`/api/jobs/${enc(this.id)}/cancel`);
    } catch {
      /* already finished or expired */
    }
  }

  // Stop polling without telling the server (it was superseded server-side).
  abandon() { this.abandoned = true; }
}

export function errorText(err) {
  if (err instanceof ApiError) return err.message;
  if (err && err.message) return `Unexpected error: ${err.message}`;
  return "Unexpected error.";
}
