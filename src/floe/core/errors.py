"""Typed exceptions (see SPEC §7).

Every exception carries structured, non-secret attributes only. Secrets
(tokens, keys, client secrets) must never be passed into these exceptions.
`user_message()` returns the text the UI should show, per the "UI treatment"
column of SPEC §7. Callers must still pass any user-facing text through
`floe.core.diagnostics.redact` before display, as a defense in depth.
"""

from __future__ import annotations

import sys


class FloeError(Exception):
    """Base class for all typed Floe exceptions."""

    def user_message(self) -> str:
        """Return the user-facing message for this error (SPEC §7)."""
        return str(self)


class NessieUnreachable(FloeError):
    """Connection refused / timeout to the Nessie host."""

    def __init__(self, uri: str, reason: str) -> None:
        self.uri = uri
        self.reason = reason
        super().__init__(f"Nessie unreachable at {uri}: {reason}")

    def user_message(self) -> str:
        return "Can't reach Nessie — are you on VPN?"


class NessieAuthError(FloeError):
    """Token request fails, or a 401 persists after one refresh."""

    def __init__(self, endpoint: str, status: int) -> None:
        self.endpoint = endpoint
        self.status = status
        super().__init__(f"Nessie auth failed at {endpoint} (status {status})")

    def user_message(self) -> str:
        return f"Authentication failed for {self.endpoint} (HTTP {self.status})"


class TableNotFound(FloeError):
    """404 when resolving a table pointer."""

    def __init__(self, ref: str, key: str) -> None:
        self.ref = ref
        self.key = key
        super().__init__(f"Table not found: {key} on {ref}")

    def user_message(self) -> str:
        return f"Table not found: {self.key} on {self.ref}"


class CorruptPointer(FloeError):
    """The catalog pointer for a table has snapshotId == -1."""

    def __init__(self, ref: str, key: str) -> None:
        self.ref = ref
        self.key = key
        super().__init__(f"Corrupt pointer for {key} on {ref} (snapshotId=-1)")

    def user_message(self) -> str:
        return f"{self.key} has no valid snapshot on {self.ref} (corrupt pointer)"


class TenantScopeError(FloeError):
    """A table's resolved location is outside its ref's expected container(s)."""

    def __init__(
        self,
        ref: str,
        key: str,
        location_container: str,
        expected_containers: list[str],
    ) -> None:
        self.ref = ref
        self.key = key
        self.location_container = location_container
        self.expected_containers = expected_containers
        super().__init__(
            f"{key} on {ref} resolves to container {location_container!r}, "
            f"expected one of {expected_containers!r}"
        )

    def user_message(self) -> str:
        return (
            f"{self.key} on {self.ref} points outside its expected container "
            f"({self.location_container!r} not in {self.expected_containers!r}); "
            "refusing to register this table."
        )


class MissingDataSourceColumn(FloeError):
    """Tenant filter is on, but the table has no data_source column."""

    def __init__(self, ref: str, key: str) -> None:
        self.ref = ref
        self.key = key
        super().__init__(f"{key} on {ref} has no data_source column")

    def user_message(self) -> str:
        return (
            f"{self.key} on {self.ref} has no data_source column; "
            "disable the tenant filter for this query to view it."
        )


class AdlsAuthError(FloeError):
    """ADLS rejected the configured credentials."""

    def __init__(self, account: str, auth_mode: str) -> None:
        self.account = account
        self.auth_mode = auth_mode
        super().__init__(f"ADLS auth failed for account {account} (mode {auth_mode})")

    def user_message(self) -> str:
        return f"Storage authentication failed for account {self.account} (mode: {self.auth_mode})"


class AdlsTlsError(FloeError):
    """The HTTPS certificate presented for the storage account could not be verified."""

    def __init__(self, account: str) -> None:
        self.account = account
        super().__init__(f"TLS certificate verification failed for storage account {account}")

    def user_message(self) -> str:
        return (
            "Couldn't verify the HTTPS certificate for the storage account "
            f"{self.account}. If your network inspects HTTPS (common on company laptops), "
            "set Storage → CA cert file to your company's root CA bundle (PEM), "
            "or try another network."
        )


class QueryCancelled(FloeError):
    """The user cancelled a running query."""

    def __init__(self) -> None:
        super().__init__("Query cancelled")

    def user_message(self) -> str:
        return "Query cancelled."


class QueryError(FloeError):
    """A SQL statement failed in DuckDB. `message` must already be redacted."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def user_message(self) -> str:
        return self.message


class ReadOnlyViolation(QueryError):
    """User SQL is not a single read-only query; the app is strictly read-only."""

    def __init__(self, statement_type: str) -> None:
        self.statement_type = statement_type
        super().__init__(
            f"Only read-only queries are allowed (got {statement_type}). "
            "Floe is read-only: use SELECT / WITH / DESCRIBE / SHOW / SUMMARIZE."
        )


class ExtensionUnavailable(FloeError):
    """A DuckDB extension could not be installed or loaded."""

    def __init__(self, extension: str, reason: str) -> None:
        self.extension = extension
        self.reason = reason
        super().__init__(f"DuckDB extension {extension!r} unavailable: {reason}")

    def user_message(self) -> str:
        return (
            f"Could not install or load the DuckDB {self.extension!r} extension "
            f"({self.reason}). Check the network connection and try again."
        )


class DisallowedFunction(QueryError):
    """User SQL reads files or settings directly (tenant scoping would be bypassed)."""

    def __init__(self, name: str, kind: str = "function") -> None:
        self.name = name
        self.kind = kind
        if kind == "file":
            detail = f"direct file or URL references are not allowed ({name!r})"
        else:
            detail = f"the {kind} {name!r} is not allowed"
        super().__init__(
            f"Query rejected: {detail}. Floe only reads the tables in the browser; "
            "query them by their view names (e.g. SELECT * FROM gold_summary)."
        )


class KeychainError(FloeError):
    """A keyring read/write failed (e.g. a Keychain/Credential Manager "Deny", a
    backend error), or no usable OS keyring exists at all.

    Never carries the secret value itself — only the field name, the action
    that failed, and (when there is no usable keyring) the env var to set.
    """

    def __init__(self, field: str, action: str, env_var: str | None = None) -> None:
        self.field = field
        self.action = action
        self.env_var = env_var
        super().__init__(f"Keychain {action} failed for field {field!r}")

    def user_message(self) -> str:
        if self.env_var is not None:
            return (
                f"No system keyring is available to store {self.field}. Set the "
                f"environment variable {self.env_var} instead and restart Floe — "
                "secret values are never written to disk."
            )
        hint = ""
        if sys.platform == "darwin":
            hint = " If you clicked Deny, try again and choose Always Allow."
        return f"Couldn't {self.action} {self.field} in the system keyring.{hint}"


class ViewNameCollision(FloeError):
    """Two or more table keys map to the same SQL view name."""

    def __init__(self, view_name: str, keys: list[str]) -> None:
        self.view_name = view_name
        self.keys = keys
        super().__init__(f"View name {view_name!r} is ambiguous: {keys!r}")

    def user_message(self) -> str:
        return (
            f"The tables {', '.join(self.keys)} all map to the SQL view name "
            f"{self.view_name!r}; Floe refuses to register any of them to avoid "
            "showing the wrong table."
        )


class TimelineUnavailable(FloeError):
    """The Timeline (snapshots / commit log) needs a Nessie catalog (not local mode)."""

    def __init__(self, reason: str = "local mode") -> None:
        self.reason = reason
        super().__init__(f"Timeline unavailable: {reason}")

    def user_message(self) -> str:
        return (
            "The Timeline needs a Nessie catalog. Local mode reads plain parquet files, "
            "which have no snapshots or commit history."
        )


class MetadataReadError(FloeError):
    """A table's metadata.json could not be read or parsed. Carries no path."""

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"Could not read the metadata of {key}: {reason}")

    def user_message(self) -> str:
        return f"Could not read the table metadata of {self.key} ({self.reason})."
