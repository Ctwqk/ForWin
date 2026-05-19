from __future__ import annotations

from forwin.config import Config
from forwin.review_engine.rules.repair_v2 import compare_repair_v2_shadow


def test_config_disables_repair_v2_by_default() -> None:
    assert Config().review_engine_repair_v2_enabled is False


def test_repair_v2_shadow_records_old_and_new_scope_when_flag_off() -> None:
    result = compare_repair_v2_shadow(
        old_scope="draft",
        new_scope="arc_plan",
        enabled=False,
    )

    assert result.live_scope == "draft"
    assert result.shadow_scope == "arc_plan"
    assert result.enabled is False


def test_repair_v2_shadow_promotes_new_scope_when_flag_on() -> None:
    result = compare_repair_v2_shadow(
        old_scope="draft",
        new_scope="arc_plan",
        enabled=True,
    )

    assert result.live_scope == "arc_plan"
    assert result.shadow_scope == "arc_plan"
    assert result.enabled is True
