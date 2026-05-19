from __future__ import annotations

import json
from types import SimpleNamespace

from forwin.api_system_routes import _load_review_engine_breakdown


class _ScalarResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _ExecuteResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._rows)


class _Session:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.closed = False

    def execute(self, _stmt: object) -> _ExecuteResult:
        return _ExecuteResult(self.rows)

    def close(self) -> None:
        self.closed = True


def test_home_breakdown_loads_from_persisted_review_engine_events() -> None:
    rows = [
        SimpleNamespace(
            payload_json=json.dumps(
                {
                    "rule_id": "auto_approve_policy_disabled",
                    "outcome": "manual_review",
                    "reason": "policy disabled: review_engine.auto_approve_enabled=false",
                },
                ensure_ascii=False,
            ),
            reason="",
        )
    ]
    session = _Session(rows)

    breakdown = _load_review_engine_breakdown(lambda: session)

    assert session.closed is True
    assert breakdown[0]["rule_id"] == "auto_approve_policy_disabled"
    assert breakdown[0]["count"] == 1
