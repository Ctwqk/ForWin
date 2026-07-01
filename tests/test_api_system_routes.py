from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from forwin.api_system_routes import _load_review_engine_breakdown, build_handlers


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


def _codex_health_handler(config: SimpleNamespace):
    return build_handlers(
        get_config=lambda: config,
        get_runtime_settings=lambda: None,
        get_publisher_manager=lambda: None,
        get_session=lambda: None,
        render_home_page=lambda **_kwargs: "",
        render_publishers_page=lambda **_kwargs: "",
        build_home_page_settings=lambda **_kwargs: {},
        build_runtime_config=lambda *_args, **_kwargs: None,
        copy_config=lambda *_args, **_kwargs: None,
        create_generation_task=lambda *_args, **_kwargs: "",
        serialize_task=lambda *_args, **_kwargs: {},
        get_generation_task_or_404=lambda _task_id: {},
        project_has_active_generation_task=lambda *_args, **_kwargs: False,
        generation_task_conflict_message=lambda _project_id: "",
        resolve_project_governance=lambda *_args, **_kwargs: None,
        governance_request_payload=lambda _req: {},
        serialize_llm_settings=lambda *_args, **_kwargs: {},
        active_generation_task_error_cls=RuntimeError,
    )["get_codex_bridge_status"]


def test_codex_health_rejects_ok_payload_without_bridge_identity() -> None:
    class WrongServiceClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def health(self) -> dict[str, object]:
            return {"status": "ok"}

        def close(self) -> None:
            pass

    config = SimpleNamespace(
        codex_enabled=True,
        codex_bridge_url="http://10.0.0.150:8897",
        codex_bridge_token="",
        codex_sync_timeout_seconds=90,
    )
    handler = _codex_health_handler(config)

    with patch("forwin.api_system_routes.CodexBridgeClient", WrongServiceClient):
        result = handler()

    assert result.healthy is False
    assert result.status == "wrong_backend"
    assert "Codex Bridge" in result.message
