"""JSON-safe conversion of query results and errors for the web API (SPEC §15).

Result cells are converted value by value: NaN / NaT / NA → null, dates and times →
ISO strings, decimals / UUIDs / intervals → strings, bytes → an escaped string, and
integers outside ±2**53 → strings (JavaScript numbers can't hold them exactly).

Error payloads carry only the redacted `user_message()` text (SPEC §7, §10).
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import uuid
from typing import Any

import numpy as np
import pandas as pd

from floe.core import diagnostics
from floe.core.errors import (
    CorruptPointer,
    FloeError,
    MissingDataSourceColumn,
    NessieUnreachable,
    TableNotFound,
    TenantScopeError,
    ViewNameCollision,
)

MAX_SAFE_INT = 2**53


def _bytes_text(value: bytes) -> str:
    return "".join(
        chr(b) if 32 <= b < 127 and b != 0x5C else f"\\x{b:02X}" for b in value
    )


def json_safe(value: Any) -> Any:
    """Convert one result cell (possibly nested) to a JSON-safe value."""
    if value is None:
        return None
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, int | np.integer):
        as_int = int(value)
        return as_int if -MAX_SAFE_INT <= as_int <= MAX_SAFE_INT else str(as_int)
    if isinstance(value, float | np.floating):
        f = float(value)
        if math.isnan(f):
            return None
        if math.isinf(f):
            return "Infinity" if f > 0 else "-Infinity"
        return f
    if isinstance(value, str):
        return value
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, pd.Timedelta | np.timedelta64 | dt.timedelta):
        return None if pd.isna(value) else str(value)
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else str(pd.Timestamp(value).isoformat())
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return None if value.is_nan() else str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return _bytes_text(bytes(value))
    if isinstance(value, dict):
        return {str(json_safe(k)): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | np.ndarray):
        return [json_safe(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def frame_columns(df: pd.DataFrame) -> list[dict[str, str]]:
    return [{"name": str(name), "type": str(dtype)} for name, dtype in df.dtypes.items()]


def frame_rows(df: pd.DataFrame, offset: int, limit: int) -> list[list[Any]]:
    """Rows `offset .. offset+limit` of `df`, JSON-safe, as lists in column order."""
    page = df.iloc[offset : offset + limit]
    columns = [page.iloc[:, i].tolist() for i in range(page.shape[1])]
    return [[json_safe(col[r]) for col in columns] for r in range(len(page))]


_TABLE_STATUS: tuple[tuple[type[FloeError], str], ...] = (
    (TableNotFound, "not_found"),
    (CorruptPointer, "corrupt"),
    (TenantScopeError, "scope_error"),
    (MissingDataSourceColumn, "missing_data_source"),
    (ViewNameCollision, "error"),
)


def error_payload(exc: BaseException) -> dict[str, Any]:
    """`{type, message}` for an error, redacted; plus UI hints for some error types."""
    if isinstance(exc, FloeError):
        message = exc.user_message()
        type_name = type(exc).__name__.lstrip("_")
    else:
        message = f"Unexpected error ({type(exc).__name__}): {exc}"
        type_name = "InternalError"
    payload: dict[str, Any] = {"type": type_name, "message": diagnostics.redact(message)}
    for cls, status in _TABLE_STATUS:
        if isinstance(exc, cls):
            payload["table_status"] = status
            break
    if isinstance(exc, MissingDataSourceColumn):
        payload["offer_disable_tenant_filter"] = True
        payload["table_key"] = exc.key
        payload["ref"] = exc.ref
    if isinstance(exc, NessieUnreachable):
        payload["unreachable"] = True
    return payload
