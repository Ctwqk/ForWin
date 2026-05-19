from __future__ import annotations

from types import SimpleNamespace

from forwin.review_engine.cutover import (
    CutoverSelection,
    engine_live_enabled,
    select_cutover_pair,
)
from forwin.review_engine.types import Decision


def _config(*, enabled: bool, allowlist: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        review_engine_live_cutover_enabled=enabled,
        review_engine_live_cutover_project_allowlist=allowlist,
    )


def _decision(outcome: str, rule_id: str) -> Decision:
    return Decision(
        outcome=outcome,  # type: ignore[arg-type]
        reason=rule_id,
        rule_id=rule_id,
        missing_evidence=[],
        routed_from="fixture",
        sub_action={"rule_id": rule_id},
    )


def test_flag_off_never_enables_engine_live() -> None:
    assert engine_live_enabled(_config(enabled=False, allowlist=[]), "project-1") is False
    assert engine_live_enabled(_config(enabled=False, allowlist=["project-1"]), "project-1") is False


def test_flag_on_empty_allowlist_enables_global_engine_live() -> None:
    assert engine_live_enabled(_config(enabled=True, allowlist=[]), "project-1") is True


def test_flag_on_non_empty_allowlist_limits_engine_live_to_project() -> None:
    config = _config(enabled=True, allowlist=["project-1", "project-3"])

    assert engine_live_enabled(config, "project-1") is True
    assert engine_live_enabled(config, "project-2") is False


def test_select_cutover_pair_swaps_live_and_shadow_when_engine_is_enabled() -> None:
    legacy = _decision("manual_review", "legacy")
    engine = _decision("auto_approve", "engine")

    selection = select_cutover_pair(
        project_id="project-1",
        legacy_decision=legacy,
        engine_decision=engine,
        config=_config(enabled=True, allowlist=[]),
    )

    assert selection == CutoverSelection(
        live=engine,
        shadow=legacy,
        live_source="engine",
        shadow_source="legacy",
        engine_live=True,
    )
