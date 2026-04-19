from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import event


@contextmanager
def capture_select_statements(engine):
    select_statements: list[str] = []

    def record_selects(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = " ".join(str(statement or "").split()).lower()
        if normalized.startswith("select"):
            select_statements.append(normalized)

    event.listen(engine, "before_cursor_execute", record_selects)
    try:
        yield select_statements
    finally:
        try:
            event.remove(engine, "before_cursor_execute", record_selects)
        except Exception:  # noqa: BLE001
            pass


def count_matching_statements(select_statements: list[str], fragment: str) -> int:
    return sum(fragment in statement for statement in select_statements)
