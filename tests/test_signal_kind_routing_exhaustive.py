from __future__ import annotations

from forwin.canon_quality.signals import SignalKind
from forwin.reviewer.repair_scope_router import SIGNAL_KIND_TO_SCOPE, RepairScopeKind, route_signal_kind


def test_signal_kind_routing_table_is_exhaustive() -> None:
    missing = [kind.value for kind in SignalKind if kind.value not in SIGNAL_KIND_TO_SCOPE]

    assert missing == []


def test_unknown_signal_kind_never_falls_back_to_writer() -> None:
    assert route_signal_kind("not_yet_registered") == RepairScopeKind.OPERATOR
