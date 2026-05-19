from __future__ import annotations

from forwin.book_state.query_interface import CountdownState
from forwin.planning.countdown_drift_pre_audit import select_countdown_drift_targets


class Query:
    def get_current_countdown_values(self, *, project_id: str, as_of_chapter: int):
        assert project_id == "p1"
        assert as_of_chapter == 17
        return {"main": CountdownState(key="main", remaining_minutes=57, status="active", chapter_number=17)}


def test_countdown_drift_targets_read_live_query_when_payload_is_stale_or_missing() -> None:
    targets = select_countdown_drift_targets(
        [
            {
                "signal_id": "sig1",
                "signal_type": "form_countdown_inconsistency",
                "subject_key": "main",
                "payload": {"plan_patchable": True},
            }
        ],
        project_id="p1",
        as_of_chapter=17,
        book_state_query=Query(),
    )

    assert targets[0]["prior_value_minutes"] == 57
    assert "57 分钟" in targets[0]["task"]
