from __future__ import annotations

import json
from pathlib import Path

from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.reviewer.repair_scope_router import RepairScopeKind, route_review_repair_scopes


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repair_routing" / "chapter_18_fail_loop" / "signals.json"


def test_chapter18_fail_loop_routes_each_signal_to_repairable_layer() -> None:
    raw_signals = json.loads(FIXTURE.read_text(encoding="utf-8"))
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name=item["kind"],
                issue_type=item["kind"],
                severity=item["severity"],
                description=item["kind"],
                target_scope="chapter",
                blocking=True,
            )
            for item in raw_signals
        ],
    )

    scopes = route_review_repair_scopes(review)

    assert [scope.scope for scope in scopes] == [
        RepairScopeKind.OPERATOR,
        RepairScopeKind.ACTIVE_RULES,
        RepairScopeKind.SUBWORLD,
    ]
    assert RepairScopeKind.DRAFT not in {scope.scope for scope in scopes}
