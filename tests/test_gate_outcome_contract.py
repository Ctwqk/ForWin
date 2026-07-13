from __future__ import annotations

import pytest
from pydantic import ValidationError

from forwin.audit.gate_outcome import (
    GateOutcome,
    attach_gate_outcome,
    parse_gate_outcome,
)


def test_gate_outcome_defaults_form_a_versioned_contract() -> None:
    outcome = GateOutcome(
        gate_id="hard_floor",
        responsibility_domain="draft_quality",
        scope="chapter",
        decision="pass",
    )

    assert outcome.model_dump(mode="json") == {
        "schema_version": 1,
        "gate_id": "hard_floor",
        "gate_version": "v1",
        "responsibility_domain": "draft_quality",
        "scope": "chapter",
        "candidate_id": "",
        "chapter_number": 0,
        "band_id": "",
        "policy_version": 0,
        "evaluated": True,
        "fired": False,
        "decision": "pass",
        "blocked": False,
        "overridden_by": "",
        "issue_keys": [],
        "issue_groups": [],
        "evidence_refs": [],
        "trace_ids": [],
    }


def test_gate_outcome_attach_and_parse_preserve_existing_payload() -> None:
    outcome = GateOutcome(
        gate_id="canon_quality",
        gate_version="v2",
        responsibility_domain="canon_admission",
        scope="chapter",
        candidate_id="candidate-7",
        chapter_number=7,
        policy_version=3,
        fired=True,
        decision="block",
        blocked=True,
        issue_keys=["continuity_gap"],
        issue_groups=["continuity"],
        evidence_refs=["review:7"],
        trace_ids=["trace-7"],
    )

    payload = attach_gate_outcome({"existing": "value"}, outcome)

    assert payload["existing"] == "value"
    assert payload["gate_outcome"]["gate_id"] == "canon_quality"
    assert parse_gate_outcome(payload) == outcome


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("scope", "scene"),
        ("decision", "skip"),
        ("overridden_by", "operator"),
    ],
)
def test_gate_outcome_rejects_values_outside_the_contract(
    field: str,
    value: object,
) -> None:
    values = {
        "gate_id": "future_plan_audit",
        "responsibility_domain": "future_plan",
        "scope": "band",
        "decision": "warn",
        field: value,
    }

    with pytest.raises(ValidationError):
        GateOutcome.model_validate(values)


def test_parse_gate_outcome_treats_legacy_or_invalid_payload_as_unknown() -> None:
    assert parse_gate_outcome({"legacy": True}) is None
    assert parse_gate_outcome({"gate_outcome": "invalid"}) is None
    assert parse_gate_outcome(
        {
            "gate_outcome": {
                "gate_id": "hard_floor",
                "responsibility_domain": "draft_quality",
                "scope": "chapter",
                "decision": "skip",
            }
        }
    ) is None
