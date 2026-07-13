from __future__ import annotations


_RETRYABLE_SQLSTATES = {
    "40001",
    "40P01",
    "55P03",
    "57014",
    "08000",
    "08001",
    "08003",
    "08006",
}

_RETRYABLE_MESSAGE_FRAGMENTS = (
    "database is locked",
    "database table is locked",
    "deadlock detected",
    "could not serialize access",
    "canceling statement due to lock timeout",
    "lock not available",
    "lock timeout",
    "connection refused",
    "connection not open",
    "server closed the connection",
    "terminating connection",
)


def is_retryable_database_error(exc: Exception) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = str(
        getattr(orig, "sqlstate", "") or getattr(orig, "pgcode", "") or ""
    ).strip()
    if sqlstate in _RETRYABLE_SQLSTATES:
        return True
    message = str(exc).lower()
    return any(fragment in message for fragment in _RETRYABLE_MESSAGE_FRAGMENTS)


__all__ = ["is_retryable_database_error"]
