"""Ask: AI-assisted SQL generation via OpenRouter's chat-completions API (SPEC §15.4).

What is sent: the user's question, view names, and column names/types of a few relevant
views, plus fixed instructions. What is never sent: any row data. This is structural —
`build_prompt` accepts only `str` plus `TableSchema` / `ColumnSchema` objects (names and
types, nothing else) and raises `TypeError` for anything else (DataFrames, dicts of rows,
query results). This module has no access to sessions or query results.

There is no user-chosen model: `generate` walks a fixed ordered chain of free models
(`MODEL_CHAIN`, with one dynamic slot filled from OpenRouter's public model list, cached
for 24h) and falls over to the next one on a timeout, connection error, or a status that
means "this model is unavailable" (429, 5xx, 404, or a 400/422 that says so), and on an
empty or non-SQL reply. A bad key (401/403) or no credits (402) stops immediately — those
are not per-model problems.

Generated SQL is never executed here: `extract_sql` pulls it out of the reply and
`validate_generated_sql` runs the same read-only / disallowed-function checks the query
path uses, returning warnings instead of raising.

No Qt, no web framework; HTTP uses stdlib `urllib` like `core/nessie.py`. Logs carry the
model, HTTP status, elapsed ms and prompt size only — never prompt text, response text, or
the API key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
import keyring.errors

from floe.core import diagnostics, paths
from floe.core.context import ColumnInfo, check_read_only
from floe.core.errors import FloeError, KeychainError

log = logging.getLogger("floe.ask")

KEYRING_SERVICE = "Floe"
KEYRING_USERNAME = "ask:openrouter_api_key"
API_KEY_ENV = "FLOE_OPENROUTER_API_KEY"
SETTINGS_FILENAME = "ask_settings.json"
MODEL_CACHE_FILENAME = "ask_models_cache.json"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_S = 90
DEFAULT_ATTEMPT_TIMEOUT_S = 25
DEFAULT_MAX_CHARS = 12_000
DEFAULT_FOCUS_CAP = 8
APP_TITLE = "Floe"
_PROVIDER_DETAIL_MAX = 200
_MODEL_CACHE_TTL_S = 24 * 3600
_MODELS_FETCH_TIMEOUT_S = 10

# Fixed, ordered fallback chain. Position -2 (before the always-last `openrouter/free`)
# is a dynamic slot filled from OpenRouter's public model list when that can be fetched
# (see `get_model_chain`); when it can't, or picks nothing, the chain just has these four.
MODEL_CHAIN: tuple[str, ...] = (
    "qwen/qwen3-coder:free",
    "openai/gpt-oss-120b:free",
    "openai/gpt-oss-20b:free",
    "openrouter/free",
)

# Orgs whose general chat/instruct free models are reasonable second choices for the
# dynamic slot, after anything that looks like a coder model.
_WELL_KNOWN_ORGS = (
    "openai/", "google/", "meta-llama/", "mistralai/", "qwen/", "deepseek/",
    "microsoft/", "nvidia/", "anthropic/",
)


# --------------------------------------------------------------------------- errors


class AskError(FloeError):
    """An Ask request failed. `message` is already redacted and safe to show.

    `attempts`, when set, is a tuple of `(model, outcome)` pairs — model ids and short,
    already-redacted reasons only, never response bodies.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        attempts: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.message = diagnostics.redact(message)
        self.status = status
        self.attempts = attempts
        super().__init__(self.message)

    def user_message(self) -> str:
        return self.message


class AskNotConfigured(AskError):
    """Ask is disabled, or has no API key configured."""


class AskAuthError(AskError):
    """OpenRouter rejected the API key (HTTP 401/403)."""


class AskQuotaError(AskError):
    """The account has no credits for this model (HTTP 402)."""


class AskRateLimited(AskError):
    """Rate limited by OpenRouter or the upstream provider (HTTP 429)."""


class AskUnavailable(AskError):
    """Timeout, connection failure, or a 5xx from OpenRouter / the provider."""


# --------------------------------------------------------------------------- schema types


@dataclass(frozen=True)
class ColumnSchema:
    """A column's name and type. Nothing else — no values, no statistics."""

    name: str
    type: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not isinstance(self.type, str):
            raise TypeError("ColumnSchema name and type must be str")


@dataclass(frozen=True)
class TableSchema:
    """A view's name and its columns' names/types."""

    view_name: str
    columns: tuple[ColumnSchema, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.view_name, str):
            raise TypeError("TableSchema.view_name must be str")
        if not isinstance(self.columns, tuple) or not all(
            isinstance(c, ColumnSchema) for c in self.columns
        ):
            raise TypeError("TableSchema.columns must be a tuple of ColumnSchema")

    @classmethod
    def from_columns(cls, view_name: str, columns: Iterable[ColumnInfo]) -> TableSchema:
        """Copy only name/type from `ColumnInfo` objects (e.g. `FloeSession.schema(...)`)."""
        out: list[ColumnSchema] = []
        for col in columns:
            if not isinstance(col, (ColumnInfo, ColumnSchema)):
                raise TypeError(
                    f"TableSchema.from_columns expects ColumnInfo, got {type(col).__name__}"
                )
            out.append(ColumnSchema(str(col.name), str(col.type)))
        return cls(str(view_name), tuple(out))


# --------------------------------------------------------------------------- prompt

SYSTEM_PROMPT = """\
You write SQL for DuckDB (DuckDB SQL dialect).
Rules:
- Generate exactly one read-only statement: a single SELECT or WITH ... SELECT query.
  Never write INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, COPY, ATTACH, SET, PRAGMA, \
INSTALL or LOAD.
- Use only the views and columns listed below. Do not read files or URLs and do not call \
table functions such as read_parquet, read_csv or iceberg_scan.
- The views are already filtered to the current tenant. Never add filters on \
data_source (or any other tenant column) for tenant scoping.
- Quote identifiers with double quotes when needed (reserved words, mixed case, special \
characters).
- Answer with the SQL only, in one ```sql fenced code block. You may add at most one \
short SQL comment line (-- ...) inside the block. No other text."""


def _require_str_seq(value: object, what: str) -> None:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{what} must be a list or tuple, got {type(value).__name__}")


def _render_table(table: TableSchema, n_cols: int) -> str:
    head = f"- {table.view_name}"
    if not table.columns:
        return head
    shown = table.columns[:n_cols]
    cols = ", ".join(f"{c.name} {c.type}" for c in shown)
    omitted = len(table.columns) - len(shown)
    if not shown:
        return f"{head} ({omitted} columns omitted)"
    if omitted:
        cols += f", ... ({omitted} more columns omitted)"
    return f"{head}({cols})"


def _render_user(
    question: str,
    focus: Sequence[TableSchema],
    col_counts: Sequence[int],
    other_views: Sequence[str],
    n_other: int,
) -> str:
    lines = ["Question:", question.strip(), ""]
    if focus:
        lines.append("Views with columns (name type):")
        lines.extend(_render_table(t, n) for t, n in zip(focus, col_counts, strict=True))
    if other_views:
        if focus:
            lines.append("")
        lines.append("Other views (columns not listed):")
        shown = other_views[:n_other]
        if shown:
            lines.append(", ".join(shown))
        omitted = len(other_views) - len(shown)
        if omitted:
            lines.append(f"({omitted} more views omitted)")
    if not focus and not other_views:
        lines.append("(No views are available.)")
    truncated = n_other < len(other_views) or any(
        n < len(t.columns) for t, n in zip(focus, col_counts, strict=True)
    )
    if truncated:
        lines.append("")
        lines.append("Note: the schema listing was truncated to fit the size limit.")
    return "\n".join(lines)


def _largest_fitting(lo: int, hi: int, fits) -> int:
    """Largest n in [lo, hi] with fits(n) (fits is monotone: true then false); lo if none."""
    if fits(hi):
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def build_prompt(
    question: str,
    focus: Sequence[TableSchema],
    other_views: Sequence[str],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, str]]:
    """Build OpenAI-style chat messages from the question and schema objects only.

    Raises `TypeError` for anything but `str` / `TableSchema` / `str` view names — the
    structural guarantee that no row data can reach the prompt. `max_chars` caps the user
    message: other view names are dropped first, then columns of later focus tables.
    """
    if not isinstance(question, str):
        raise TypeError(f"question must be str, got {type(question).__name__}")
    _require_str_seq(focus, "focus")
    _require_str_seq(other_views, "other_views")
    for t in focus:
        if not isinstance(t, TableSchema):
            raise TypeError(f"focus items must be TableSchema, got {type(t).__name__}")
    for v in other_views:
        if not isinstance(v, str):
            raise TypeError(f"other_views items must be str, got {type(v).__name__}")
    if not question.strip():
        raise AskError("Type a question first.")
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive int")

    focus = list(focus)
    focus_names = {t.view_name for t in focus}
    others = [v for v in dict.fromkeys(other_views) if v not in focus_names]
    counts = [len(t.columns) for t in focus]

    def render(n_other: int) -> str:
        return _render_user(question, focus, counts, others, n_other)

    n_other = _largest_fitting(0, len(others), lambda n: len(render(n)) <= max_chars)
    # Then drop columns, starting from the last focus table.
    for i in range(len(focus) - 1, -1, -1):
        if len(render(n_other)) <= max_chars:
            break

        def fits(n: int, i: int = i) -> bool:
            trial = counts[:i] + [n] + counts[i + 1 :]
            return len(_render_user(question, focus, trial, others, n_other)) <= max_chars

        counts[i] = _largest_fitting(0, counts[i], fits)
    user = render(n_other)
    if len(user) > max_chars:
        raise AskError(
            "The question and view list are too long to send; shorten the question."
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


_WORD_CHARS = r"A-Za-z0-9_"


def pick_focus_views(
    question: str,
    editor_sql: str,
    selected_view: str | None,
    known_views: Iterable[str],
    *,
    cap: int = DEFAULT_FOCUS_CAP,
) -> list[str]:
    """Views whose columns go in the prompt: the selected view first, then known view
    names that appear as whole words (case-insensitive) in the question, then the editor
    text, in order of appearance. At most `cap` names."""
    known = list(dict.fromkeys(v for v in known_views if v))
    out: list[str] = []
    if selected_view and selected_view in known:
        out.append(selected_view)
    for text in (question or "", editor_sql or ""):
        hits: list[tuple[int, int, str]] = []
        for order, name in enumerate(known):
            pattern = rf"(?<![{_WORD_CHARS}]){re.escape(name)}(?![{_WORD_CHARS}])"
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                hits.append((m.start(), order, name))
        for _, _, name in sorted(hits):
            if name not in out:
                out.append(name)
    return out[: max(cap, 0)]


# --------------------------------------------------------------------------- settings


@dataclass
class AskSettings:
    """Global (not per-profile) Ask settings. The API key is never stored here.

    There is no `model` field: `generate` always walks the fixed fallback chain
    (`MODEL_CHAIN`, plus a dynamic slot). An old settings file that still has a `model`
    key from before this changed loads fine — the value is simply ignored.
    """

    enabled: bool = False
    base_url: str = DEFAULT_BASE_URL
    timeout_s: int = DEFAULT_TIMEOUT_S

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "base_url": str(self.base_url),
            "timeout_s": int(self.timeout_s),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AskSettings:
        s = cls()
        if isinstance(data.get("enabled"), bool):
            s.enabled = data["enabled"]
        if isinstance(data.get("base_url"), str) and data["base_url"].strip():
            s.base_url = data["base_url"].strip()
        timeout = data.get("timeout_s")
        if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
            s.timeout_s = timeout
        return s


def settings_path() -> Path:
    return paths.app_support_dir() / SETTINGS_FILENAME


def load_settings(path: Path | None = None) -> AskSettings:
    """Load settings; a missing or unreadable file yields the defaults (Ask disabled)."""
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AskSettings()
    return AskSettings.from_dict(data) if isinstance(data, dict) else AskSettings()


def save_settings(settings: AskSettings, path: Path | None = None) -> None:
    """Atomically write the settings JSON with mode 0600. Never contains the API key."""
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".ask-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
    os.chmod(path, 0o600)


def get_api_key() -> str | None:
    """The OpenRouter API key from the keyring, else `FLOE_OPENROUTER_API_KEY`.

    The value is registered with the redaction filter before it is returned.
    """
    value: str | None = None
    try:
        value = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring.errors.KeyringError as exc:
        log.warning("Keyring read failed for the Ask API key (%s)", type(exc).__name__)
        value = None
    if not value:
        value = os.environ.get(API_KEY_ENV) or None
    if value:
        diagnostics.register_secret(value)
    return value


def set_api_key(value: str | None) -> None:
    """Store the API key in the keyring; an empty value deletes it."""
    value = (value or "").strip()
    if value:
        diagnostics.register_secret(value)
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, value)
        except keyring.errors.KeyringError as exc:
            raise KeychainError("OpenRouter API key", "save") from exc
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError as exc:
        raise KeychainError("OpenRouter API key", "delete") from exc


def has_api_key() -> bool:
    return bool(get_api_key())


def ensure_configured(settings: AskSettings, api_key: str | None) -> None:
    """Raise `AskNotConfigured` unless Ask is enabled with an API key. There is no model
    to check: `generate` always uses the fixed fallback chain."""
    if not settings.enabled:
        raise AskNotConfigured("Ask is turned off. Enable it in Settings → Ask.")
    if not api_key:
        raise AskNotConfigured(
            "No OpenRouter API key is set. Add one in Settings → Ask "
            f"(or set {API_KEY_ENV})."
        )
    if not settings.base_url.strip():
        raise AskNotConfigured("The Ask base URL is empty.")


# --------------------------------------------------------------------------- model chain


def _build_default_opener() -> Any:
    from floe.core.nessie import default_ssl_context

    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=default_ssl_context()))


def _models_cache_path() -> Path:
    return paths.app_support_dir() / MODEL_CACHE_FILENAME


_model_chain_lock = threading.Lock()
_model_chain_mem_cache: dict[str, dict[str, Any]] = {}


def _load_model_cache_disk(base_url: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_models_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("base_url") != base_url:
        return None
    if not isinstance(data.get("fetched_at"), (int, float)) or not isinstance(
        data.get("models"), list
    ):
        return None
    return data


def _save_model_cache_disk(base_url: str, fetched_at: float, models: list[dict[str, Any]]) -> None:
    path = _models_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".ask-models-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {"base_url": base_url, "fetched_at": fetched_at, "models": models}, f
                )
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        os.chmod(path, 0o600)
    except OSError as exc:  # a cache write failure must never break Ask
        log.info("Ask: could not write the model list cache (%s)", type(exc).__name__)


def _fetch_model_list(
    base_url: str, api_key: str | None, opener: Any
) -> list[dict[str, Any]] | None:
    """`GET {base_url}/models`; `None` on any failure (never raises)."""
    url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=_MODELS_FETCH_TIMEOUT_S) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001 - fetch failure just means "skip the slot"
        log.info("Ask: fetching the OpenRouter model list failed (%s)", type(exc).__name__)
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    return [m for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)]


def _get_model_list(base_url: str, api_key: str | None, opener: Any | None, now: float) -> (
    list[dict[str, Any]] | None
):
    """The cached (<=24h) model list for `base_url`, fetching if stale. `None` if nothing
    usable is available (fetch failed and there is no — even stale — cache)."""
    with _model_chain_lock:
        mem = _model_chain_mem_cache.get(base_url)
    if mem is not None and now - mem["fetched_at"] < _MODEL_CACHE_TTL_S:
        return mem["models"]
    disk = mem or _load_model_cache_disk(base_url)
    if disk is not None and now - disk["fetched_at"] < _MODEL_CACHE_TTL_S:
        with _model_chain_lock:
            _model_chain_mem_cache[base_url] = disk
        return disk["models"]
    models = _fetch_model_list(base_url, api_key, opener or _build_default_opener())
    if models is None:
        return disk["models"] if disk is not None else None
    entry = {"fetched_at": now, "models": models}
    with _model_chain_lock:
        _model_chain_mem_cache[base_url] = entry
    _save_model_cache_disk(base_url, now, models)
    return models


def _model_tier(model_id: str) -> int:
    mid = model_id.lower()
    if "coder" in mid or "code" in mid:
        return 0
    if "instruct" in mid or any(mid.startswith(org) for org in _WELL_KNOWN_ORGS):
        return 1
    return 2


def _pick_dynamic_model(models: Sequence[dict[str, Any]], exclude: set[str]) -> str | None:
    """The best free model not already in `exclude`: coder/code ids first, then
    instruct/general-chat models from well-known orgs, then by larger context length."""
    candidates = [
        m for m in models
        if isinstance(m.get("id"), str) and m["id"].endswith(":free") and m["id"] not in exclude
    ]
    if not candidates:
        return None

    def sort_key(m: dict[str, Any]) -> tuple[int, int]:
        try:
            ctx = int(m.get("context_length") or 0)
        except (TypeError, ValueError):
            ctx = 0
        return (_model_tier(m["id"]), -ctx)

    candidates.sort(key=sort_key)
    return candidates[0]["id"]


def get_model_chain(
    settings: AskSettings,
    api_key: str | None,
    *,
    opener: Any | None = None,
    now: float | None = None,
) -> list[str]:
    """`MODEL_CHAIN` with its dynamic slot filled in (before the always-last
    `openrouter/free`), and any fixed id dropped that the fetched model list says no
    longer exists. Never raises: a failed fetch just leaves the chain at 4 models."""
    now = time.time() if now is None else now
    base_url = (settings.base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    fixed = list(MODEL_CHAIN[:-1])
    suffix = MODEL_CHAIN[-1]
    models = _get_model_list(base_url, api_key, opener, now)
    if models is not None:
        ids = {m["id"] for m in models}
        fixed = [m for m in fixed if m in ids]
        dynamic = _pick_dynamic_model(models, exclude=set(fixed) | {suffix})
        if dynamic:
            fixed.append(dynamic)
    chain = [m for m in fixed if m != suffix]
    chain.append(suffix)
    return chain


# --------------------------------------------------------------------------- client


@dataclass(frozen=True)
class AskResult:
    sql: str
    raw_text: str
    model: str
    elapsed_ms: int
    attempts: tuple[tuple[str, str], ...] = ()

    @property
    def fell_back(self) -> bool:
        return len(self.attempts) > 1


def _check_messages(messages: object) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        raise TypeError("messages must be a list of {role, content} dicts")
    for m in messages:
        if (
            not isinstance(m, dict)
            or set(m) != {"role", "content"}
            or not isinstance(m["role"], str)
            or not isinstance(m["content"], str)
        ):
            raise TypeError("messages must be a list of {role: str, content: str} dicts")
    return messages


def _request_body(messages: list[dict[str, str]], model: str) -> dict[str, Any]:
    return {"model": model, "messages": messages, "temperature": 0}


def preview_request(messages: list[dict[str, str]], model: str | None = None) -> str:
    """The exact JSON body the first attempt would send (no API key in it). `model`
    defaults to the first model in `MODEL_CHAIN`; the actual model used may change if
    that one is unavailable and Ask falls back to the next one in the chain."""
    body = _request_body(_check_messages(messages), model or MODEL_CHAIN[0])
    return json.dumps(body, indent=2, ensure_ascii=False)


def _model_unavailable_detail(body: bytes) -> bool:
    """Whether a 400/422 body says the model itself is the problem (vs. a bad request)."""
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
        msg = data.get("error", {}).get("message") if isinstance(data, dict) else None
    except (ValueError, AttributeError):
        msg = None
    if not isinstance(msg, str):
        return False
    msg = msg.lower()
    return any(
        kw in msg
        for kw in (
            "model not found", "not found", "no endpoints", "no allowed providers",
            "not a valid model", "does not exist", "not available", "unavailable",
            "unknown model",
        )
    )


def _provider_detail(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
        msg = data.get("error", {}).get("message") if isinstance(data, dict) else None
    except (ValueError, AttributeError):
        msg = None
    if not isinstance(msg, str) or not msg.strip():
        return ""
    msg = " ".join(diagnostics.redact(msg).split())
    if len(msg) > _PROVIDER_DETAIL_MAX:
        msg = msg[:_PROVIDER_DETAIL_MAX] + "…"
    return f": {msg}"


def _error_for_status(status: int, body: bytes) -> AskError:
    if status in (401, 403):
        return AskAuthError(
            f"OpenRouter rejected the API key (HTTP {status}). Check it in Settings → Ask.",
            status,
        )
    if status == 402:
        return AskQuotaError(
            "OpenRouter says the account has insufficient credits for this model "
            "(HTTP 402). Pick a free model or add credits.",
            status,
        )
    if status == 429:
        return AskRateLimited(
            "Rate limited by OpenRouter or the model provider (HTTP 429). "
            "Wait a moment and try again.",
            status,
        )
    if status >= 500:
        return AskUnavailable(
            f"OpenRouter or the model provider is unavailable (HTTP {status}). Try again later.",
            status,
        )
    return AskError(f"OpenRouter request failed (HTTP {status}){_provider_detail(body)}", status)


class OpenRouterClient:
    """Minimal OpenRouter chat-completions client (stdlib urllib)."""

    def __init__(
        self,
        settings: AskSettings,
        api_key: str | None,
        *,
        opener: Any = None,
    ) -> None:
        ensure_configured(settings, api_key)
        diagnostics.register_secret(api_key)
        self._settings = settings
        self._api_key = api_key
        if opener is None:
            from floe.core.nessie import default_ssl_context

            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=default_ssl_context())
            )
        self._opener = opener

    @property
    def endpoint(self) -> str:
        return self._settings.base_url.rstrip("/") + "/chat/completions"

    def _post(self, data: bytes, timeout_s: float) -> tuple[int, bytes]:
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Title": APP_TITLE,
            },
        )
        try:
            with self._opener.open(req, timeout=timeout_s) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            finally:
                exc.close()
            return exc.code, body
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise AskUnavailable(
                    f"OpenRouter did not answer within {timeout_s:.0f} s."
                ) from None
            detail = reason if isinstance(reason, str) else type(reason).__name__
            raise AskUnavailable(f"Can't reach OpenRouter ({detail}).") from None
        except TimeoutError:
            raise AskUnavailable(f"OpenRouter did not answer within {timeout_s:.0f} s.") from None
        except OSError as exc:
            raise AskUnavailable(f"Can't reach OpenRouter ({type(exc).__name__}).") from None

    def _attempt(
        self, model: str, messages: list[dict[str, str]], timeout_s: float, prompt_chars: int
    ) -> tuple[AskResult | None, str]:
        """One attempt against `model`. Returns `(result, outcome)`; `result` is `None`
        for a fallback-worthy failure, with `outcome` a short, redacted reason (never a
        response body). Raises `AskAuthError`/`AskQuotaError` immediately — those are not
        per-model problems, so there is no fallback for them."""
        body_text = preview_request(messages, model)
        start = time.perf_counter()
        status: int | str = "ERR"
        try:
            status, raw = self._post(body_text.encode("utf-8"), timeout_s)
        except AskUnavailable:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                "ask model=%s status=ERR elapsed_ms=%d prompt_chars=%d",
                model, elapsed_ms, prompt_chars,
            )
            return None, "timeout or connection error"
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "ask model=%s status=%s elapsed_ms=%d prompt_chars=%d",
            model, status, elapsed_ms, prompt_chars,
        )
        if not 200 <= status < 300:
            if status in (401, 402, 403):
                raise _error_for_status(status, raw)
            if status == 429:
                return None, "rate limited (HTTP 429)"
            if status >= 500:
                return None, f"server error (HTTP {status})"
            if status == 404:
                return None, "not found (HTTP 404)"
            if status in (400, 422) and _model_unavailable_detail(raw):
                return None, f"model unavailable (HTTP {status})"
            raise _error_for_status(status, raw)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "response wasn't valid JSON"
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            code = data["error"].get("code")
            if code in (401, 402, 403):
                raise _error_for_status(code, raw)
            if code == 429:
                return None, "rate limited (HTTP 429)"
            if isinstance(code, int) and code >= 500:
                return None, f"server error (HTTP {code})"
            if code in (400, 404, 422):
                return None, f"model unavailable (HTTP {code})"
            return None, "provider returned an error"
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None, "unexpected response (no message)"
        if not isinstance(content, str) or not content.strip():
            return None, "empty answer"
        sql = extract_sql(content)
        if not sql.strip():
            return None, "answer had no SQL in it"
        reported_model = data.get("model") if isinstance(data.get("model"), str) else None
        result = AskResult(
            sql=sql, raw_text=content, model=reported_model or model, elapsed_ms=elapsed_ms
        )
        return result, "ok"

    def generate(self, messages: list[dict[str, str]]) -> AskResult:
        """Try each model in the fallback chain in order; return the first one that
        answers with SQL. The SQL is never executed here.

        A bad key (401/403) or no credits (402) is raised immediately, with no fallback.
        Any other per-model failure (timeout, 429, 5xx, 404, a 400/422 that says the model
        is unavailable, or an empty/non-SQL reply) moves on to the next model. If every
        model in the chain fails this way, raises `AskUnavailable` with each model's
        outcome attached.
        """
        messages = _check_messages(messages)
        prompt_chars = sum(len(m["content"]) for m in messages)
        chain = get_model_chain(self._settings, self._api_key, opener=self._opener)
        budget = max(1, self._settings.timeout_s)
        deadline = time.perf_counter() + budget
        attempts: list[tuple[str, str]] = []
        for model in chain:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                attempts.append((model, "skipped (out of time)"))
                continue
            attempt_timeout = max(1.0, min(DEFAULT_ATTEMPT_TIMEOUT_S, remaining))
            result, outcome = self._attempt(model, messages, attempt_timeout, prompt_chars)
            if result is not None:
                return AskResult(
                    sql=result.sql,
                    raw_text=result.raw_text,
                    model=result.model,
                    elapsed_ms=result.elapsed_ms,
                    attempts=tuple([*attempts, (model, "ok")]),
                )
            attempts.append((model, outcome))
        log.warning("ask all models failed: %s", "; ".join(f"{m}={o}" for m, o in attempts))
        raise AskUnavailable(
            "All free models are busy or unavailable right now — try again in a minute.",
            attempts=tuple(attempts),
        )


# --------------------------------------------------------------------------- output handling

_SQL_FENCE_RE = re.compile(r"```[ \t]*sql[ \t]*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)
_ANY_FENCE_RE = re.compile(r"```[^\n`]*\r?\n(.*?)```", re.DOTALL)
_OPEN_FENCE_RE = re.compile(r"```[^\n`]*\r?\n(.*)\Z", re.DOTALL)


def _strip_sql(text: str) -> str:
    text = text.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def extract_sql(text: str) -> str:
    """Pull the SQL out of a model reply: the first ```sql block, else the first fenced
    block, else (an unterminated fence's body, or) the whole text. Trailing `;` stripped."""
    if not isinstance(text, str):
        raise TypeError("extract_sql expects str")
    for pattern in (_SQL_FENCE_RE, _ANY_FENCE_RE, _OPEN_FENCE_RE):
        m = pattern.search(text)
        if m:
            return _strip_sql(m.group(1))
    return _strip_sql(text.replace("```", ""))


def validate_generated_sql(sql: str) -> list[str]:
    """Warnings for generated SQL that the query path would reject. Never executes it
    (parses only, via `check_read_only`)."""
    if not sql or not sql.strip():
        return ["The answer contained no SQL."]
    try:
        check_read_only(sql)
    except FloeError as exc:
        return [diagnostics.redact(exc.user_message())]
    except Exception as exc:  # parser surprises must never escape as a crash
        return [diagnostics.redact(f"Could not check this SQL ({type(exc).__name__}).")]
    return []
