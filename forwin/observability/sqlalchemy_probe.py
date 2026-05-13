from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError

from .spans import current_span


_INSTALLED_ATTR = "_forwin_observability_sqlalchemy_probe_installed"


def install_sqlalchemy_query_probe(engine: Any) -> None:
    if engine is None or bool(getattr(engine, _INSTALLED_ATTR, False)):
        return
    try:
        event.listen(engine, "before_cursor_execute", _before_cursor_execute)
        event.listen(engine, "after_cursor_execute", _after_cursor_execute)
    except InvalidRequestError:
        return
    setattr(engine, _INSTALLED_ATTR, True)


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
    context._forwin_query_started_at = time.perf_counter()


def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
    span = current_span()
    if span is None:
        return
    started_at = getattr(context, "_forwin_query_started_at", None)
    if started_at is None:
        return
    duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))
    span.metric("db.query_count", int(span.metrics.get("db.query_count", 0) or 0) + 1)
    span.metric(
        "db.duration_ms",
        int(span.metrics.get("db.duration_ms", 0) or 0) + duration_ms,
    )
    slowest = int(span.metrics.get("db.slowest_query_ms", 0) or 0)
    if duration_ms >= slowest:
        span.metric("db.slowest_query_ms", duration_ms)
        span.tag("db.slowest_query_hash", _hash_sql(statement))
        span.tag("db.slowest_query_preview", _preview_sql(statement))


def _hash_sql(statement: str) -> str:
    normalized = _normalize_sql(statement)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _preview_sql(statement: str) -> str:
    normalized = _normalize_sql(statement)
    return normalized[:240]


def _normalize_sql(statement: str) -> str:
    return re.sub(r"\s+", " ", str(statement or "")).strip()
