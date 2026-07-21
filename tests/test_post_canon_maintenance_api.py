from __future__ import annotations

from contextlib import nullcontext
import json
from types import SimpleNamespace

from forwin.http.adapters.api_maintenance_routes import build_handlers
from forwin.maintenance.events import ORDER_CONTROLS_KEY, POST_CANON_STEP_NAMES
from forwin.runtime.policy import RuntimePolicy


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


def test_status_handler_uses_live_checkpoint_state() -> None:
    project = SimpleNamespace(
        id="project-1",
        runtime_policy_json=RuntimePolicy.for_profile("standard").model_dump_json(),
        runtime_policy_version=1,
    )
    commit = SimpleNamespace(
        id="canon-1",
        project_id="project-1",
        candidate_id="candidate-1",
        chapter_number=1,
        status="committed",
    )
    rows = [_run(step_name) for step_name in POST_CANON_STEP_NAMES]
    rows[-1].result_json = json.dumps(
        {
            ORDER_CONTROLS_KEY: {
                "status": "succeeded",
                "result": {
                    "blocking_reasons": [],
                    "checkpoint": {
                        "id": "checkpoint-1",
                        "status": "fail",
                    },
                },
            }
        },
        sort_keys=True,
    )
    checkpoint = SimpleNamespace(id="checkpoint-1", status="overridden")

    class FakeSession:
        def __init__(self):
            self.execute_count = 0

        def get(self, _model, identity):
            return {
                "project-1": project,
                "checkpoint-1": checkpoint,
            }.get(identity)

        def execute(self, _statement):
            self.execute_count += 1
            return _Result([commit] if self.execute_count == 1 else rows)

    session = FakeSession()
    handler = build_handlers(get_session=lambda: nullcontext(session))[
        "get_post_canon_maintenance_status"
    ]

    response = handler("project-1")

    assert response["ready"] is True
    assert response["chapters"][0]["checkpoint_status"] == "overridden"
    assert response["chapters"][0]["ready"] is True


def _run(step_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"run-{step_name}",
        canon_commit_id="canon-1",
        step_name=step_name,
        status="succeeded",
        attempts=1,
        worker_id="",
        lease_epoch=1,
        lease_expires_at=None,
        heartbeat_at=None,
        last_error="",
        result_json="{}",
        started_at=None,
        completed_at=None,
        updated_at=None,
    )
