from __future__ import annotations

from forwin.planning.ledger_state_drift_pre_audit import select_ledger_state_drift_targets


def test_form_invariant_drift_creates_ledger_state_target() -> None:
    targets = select_ledger_state_drift_targets(
        [
            {
                "signal_id": "sig-deadline",
                "signal_type": "form_invariant_drift",
                "subject_key": "city_renovation_deadline",
                "payload": {
                    "plan_patchable": True,
                    "invariant_key": "city_renovation_deadline",
                    "invariant_kind": "deadline",
                    "expected": {"deadline_chapter": 26},
                    "observed": {"deadline_chapter": 28},
                    "allowed_bridges": ["explicit_extension_cost"],
                    "generic_suppression_key": "invariant:city_renovation_deadline",
                },
            }
        ]
    )

    assert len(targets) == 1
    target = targets[0]
    assert target.patch_kind == "ledger_state_drift"
    assert target.suppression_key == "invariant:city_renovation_deadline"
    assert target.invariant_key == "city_renovation_deadline"
    assert target.kind == "deadline"
    assert target.expected == {"deadline_chapter": 26}
    assert target.observed == {"deadline_chapter": 28}
    assert target.allowed_bridges == ["explicit_extension_cost"]
    assert target.source_signal_id == "sig-deadline"


def test_legacy_countdown_signal_adapts_to_generic_ledger_state_target() -> None:
    targets = select_ledger_state_drift_targets(
        [
            {
                "signal_id": "sig-countdown",
                "signal_type": "form_countdown_inconsistency",
                "subject_key": "main",
                "payload": {
                    "plan_patchable": True,
                    "prior_value_minutes": 12,
                    "new_value_minutes": 20,
                },
            }
        ]
    )

    assert len(targets) == 1
    target = targets[0]
    assert target.invariant_key == "countdown:main"
    assert target.kind == "monotonic_numeric"
    assert target.subject_key == "main"
    assert target.suppression_key == "invariant:countdown:main"
    assert target.expected == {"current_value": 12, "value_unit": "minutes"}
    assert target.observed == {"current_value": 20, "value_unit": "minutes"}
    assert "12 分钟" in target.task


def test_deadline_target_does_not_use_countdown_wording() -> None:
    targets = select_ledger_state_drift_targets(
        [
            {
                "signal_id": "sig-deadline",
                "signal_type": "form_invariant_drift",
                "subject_key": "city_renovation_deadline",
                "payload": {
                    "plan_patchable": True,
                    "invariant_key": "city_renovation_deadline",
                    "invariant_kind": "deadline",
                    "label": "城市改造期限",
                    "expected": {"deadline_chapter": 26},
                    "observed": {"deadline_chapter": 28},
                },
            }
        ]
    )

    task = targets[0].task
    assert "截止" in task
    assert "倒计时" not in task
    assert "分钟" not in task
