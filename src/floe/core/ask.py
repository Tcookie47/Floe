"""Ask: AI-assisted SQL generation via OpenRouter's chat-completions API (SPEC §15.4).

What is sent: the user's question, view names, and column names/types of a few relevant
views, plus fixed instructions. What is never sent: any row data. This is structural —
`build_prompt` accepts only `str` plus `TableSchema` / `ColumnSchema` objects (names and
types, nothing else) and raises `TypeError` for anything else (DataFrames, dicts of rows,
query results). This module has no access to sessions or query results.

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
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_S = 60
DEFAULT_MAX_CHARS = 12_000
DEFAULT_FOCUS_CAP = 8
APP_TITLE = "Floe"
_PROVIDER_DETAIL_MAX = 200


# --------------------------------------------------------------------------- errors


class AskError(FloeError):
    """An Ask request failed. `message` is already redacted and safe to show."""

    def __init__(self, message: str, status: int | None = None) -> None:
        self.message = diagnostics.redact(message)
        self.status = status
        super().__init__(self.message)

    def user_message(self) -> str:
        return self.message


class AskNotConfigured(AskError):
    """Ask is disabled, or has no model / API key configured."""


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
    """Global (not per-profile) Ask settings. The API key is never stored here."""

    enabled: bool = False
    model: str = ""
    base_url: str = DEFAULT_BASE_URL
    timeout_s: int = DEFAULT_TIMEOUT_S

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "model": str(self.model),
            "base_url": str(self.base_url),
            "timeout_s": int(self.timeout_s),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AskSettings:
        s = cls()
        if isinstance(data.get("enabled"), bool):
            s.enabled = data["enabled"]
        if isinstance(data.get("model"), str):
            s.model = data["model"].strip()
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
    """Raise `AskNotConfigured` unless Ask is enabled with a model and an API key."""
    if not settings.enabled:
        raise AskNotConfigured("Ask is turned off. Enable it in Settings → Ask.")
    if not settings.model.strip():
        raise AskNotConfigured(
            "No model is configured for Ask. Enter an OpenRouter model ID in "
            "Settings → Ask (see the README for suggestions)."
        )
    if not api_key:
        raise AskNotConfigured(
            "No OpenRouter API key is set. Add one in Settings → Ask "
            f"(or set {API_KEY_ENV})."
        )
    if not settings.base_url.strip():
        raise AskNotConfigured("The Ask base URL is empty.")


# --------------------------------------------------------------------------- client


@dataclass(frozen=True)
class AskResult:
    sql: str
    raw_text: str
    model: str
    elapsed_ms: int


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


def _request_body(messages: list[dict[str, str]], settings: AskSettings) -> dict[str, Any]:
    return {"model": settings.model, "messages": messages, "temperature": 0}


def preview_request(messages: list[dict[str, str]], settings: AskSettings) -> str:
    """The exact JSON body `OpenRouterClient.generate` would send (no API key in it)."""
    body = _request_body(_check_messages(messages), settings)
    return json.dumps(body, indent=2, ensure_ascii=False)


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

    def _post(self, data: bytes) -> tuple[int, bytes]:
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
            with self._opener.open(req, timeout=self._settings.timeout_s) as resp:
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
                    f"OpenRouter did not answer within {self._settings.timeout_s} s."
                ) from None
            detail = reason if isinstance(reason, str) else type(reason).__name__
            raise AskUnavailable(f"Can't reach OpenRouter ({detail}).") from None
        except TimeoutError:
            raise AskUnavailable(
                f"OpenRouter did not answer within {self._settings.timeout_s} s."
            ) from None
        except OSError as exc:
            raise AskUnavailable(f"Can't reach OpenRouter ({type(exc).__name__}).") from None

    def generate(self, messages: list[dict[str, str]]) -> AskResult:
        """Send the messages; return the reply and the SQL extracted from it. The SQL is
        never executed here."""
        body_text = preview_request(messages, self._settings)
        prompt_chars = sum(len(m["content"]) for m in messages)
        start = time.perf_counter()
        status: int | str = "ERR"
        try:
            status, raw = self._post(body_text.encode("utf-8"))
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                "ask model=%s status=%s elapsed_ms=%d prompt_chars=%d",
                self._settings.model, status, elapsed_ms, prompt_chars,
            )
        if not 200 <= status < 300:
            raise _error_for_status(status, raw)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise AskError("OpenRouter returned a response that isn't valid JSON.") from None
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            code = data["error"].get("code")
            if isinstance(code, int) and code >= 400:
                raise _error_for_status(code, raw)
            raise AskError(f"OpenRouter returned an error{_provider_detail(raw)}")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AskError("OpenRouter returned an unexpected response (no message).") from None
        if not isinstance(content, str) or not content.strip():
            raise AskError("The model returned an empty answer. Try rephrasing the question.")
        model = data.get("model") if isinstance(data.get("model"), str) else None
        return AskResult(
            sql=extract_sql(content),
            raw_text=content,
            model=model or self._settings.model,
            elapsed_ms=elapsed_ms,
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
