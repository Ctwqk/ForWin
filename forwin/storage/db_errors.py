from __future__ import annotations


_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"

_RETRYABLE_SQLSTATES = {
    "40001",
    "40P01",
    _LOCK_NOT_AVAILABLE_SQLSTATE,
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


def _sqlstate(exc: Exception) -> str:
    orig = getattr(exc, "orig", None)
    return str(
        getattr(orig, "sqlstate", "") or getattr(orig, "pgcode", "") or ""
    ).strip()


def is_retryable_database_error(exc: Exception) -> bool:
    if _sqlstate(exc) in _RETRYABLE_SQLSTATES:
        return True
    message = str(exc).lower()
    return any(fragment in message for fragment in _RETRYABLE_MESSAGE_FRAGMENTS)


def is_lock_not_available_error(exc: Exception) -> bool:
    """Another transaction holds a row that a ``NOWAIT`` lock asked for."""
    return _sqlstate(exc) == _LOCK_NOT_AVAILABLE_SQLSTATE


__all__ = ["is_lock_not_available_error", "is_retryable_database_error"]
