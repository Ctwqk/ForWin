from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.models import Base, ChapterPlan, Project
from forwin.models.phase import BandExperiencePlan
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.planning.future_plan_audit import FuturePlanAuditor
from forwin.planning.future_plan_audit.signal_pre_audit import _plan_mentions_signal, select_stale_signal_targets
from forwin.protocol.experience import BandDelightSchedule


def _plan(number: int, *, one_line: str, goals: list[str] | None = None) -> ChapterPlan:
    return ChapterPlan(
        id=f"plan-{number}",
        project_id="project-1",
        arc_plan_id="arc-1",
        chapter_number=number,
        title=f"第{number}章",
        one_line=one_line,
        goals_json=json.dumps(goals or [], ensure_ascii=False),
        experience_plan_json="{}",
        status="planned",
    )


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_future_plan_auditor_binds_immutable_canon_rule_to_current_plan() -> None:
    plan = _plan(6, one_line="众人再次尝试通过门禁。")
    invariant = {
        "invariant_key": "book_state_rule:rule-transit-protocol",
        "kind": "active_rule",
        "label": "通行协议",
        "current_value": {"public_version": "三印同亮，门右移一格。"},
        "constraints": {"immutable_definition": True},
        "evidence_refs": ["chapter:5"],
    }
    auditor = FuturePlanAuditor()

    result = auditor.audit_plans(
        project_id="project-1",
        current_chapter=6,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={"invariant_constraints": [invariant]},
        obligations=[],
        target_total_chapters=60,
        include_current=True,
    )

    assert result.status == "warn"
    assert [issue.issue_type for issue in result.issues] == [
        "invariant_plan_binding_missing"
    ]
    assert result.issues[0].blocking is False
    patch = result.plan_patches[0]
    assert patch.patch_type == "invariant_plan_binding"
    assert patch.metadata["invariant_keys"] == [
        "book_state_rule:rule-transit-protocol"
    ]

    auditor.apply_plan_patch(plan, patch)

    anchors = json.loads(plan.experience_plan_json)["rule_anchors"]
    assert len(anchors) == 1
    assert "book_state_rule:rule-transit-protocol" in anchors[0]
    assert "三印同亮，门右移一格。" in anchors[0]
    assert "explicit canon bridge" in anchors[0]


def test_audit_and_apply_clears_blocker_after_successful_future_plan_patch() -> None:
    session = _session()
    project = Project(
        genre="玄幻",
        id="project-1",
        title="未来计划修补",
        premise="测试",
        target_total_chapters=36,
    )
    plan = _plan(32, one_line="角色继续寻找证据。")
    obligation = NarrativeObligation(
        id="obl-32",
        project_id="project-1",
        origin_chapter_number=31,
        obligation_type="motivation_gap",
        priority="P1",
        status="active",
        summary="解释角色动机。",
        hardness="design_debt",
        deadline_chapter=32,
        payoff_test="给出明确动机证据。",
    )
    session.add(project)
    session.add(plan)
    session.commit()

    result = FuturePlanAuditor().audit_and_apply(
        session=session,
        project_id="project-1",
        current_chapter=31,
        trigger_stage="post_acceptance",
        plans=[plan],
        canon_quality_context={},
        obligations=[obligation],
        target_total_chapters=36,
        include_current=False,
    )

    assert result.applied_plan_patch_ids
    assert result.plan_patches[0].patch_type == "obligation_plan_binding"
    assert "obl-32" in plan.goals_json
    assert obligation.payoff_test in json.loads(plan.experience_plan_json)["rule_anchors"]
    assert result.blocking_reasons == []
    assert result.status == "warn"


def test_future_plan_auditor_generates_band_binding_patch_for_band_obligation() -> None:
    schedule = BandDelightSchedule(
        band_id="arc-1:band:2",
        chapter_start=11,
        chapter_end=14,
        stall_guard_max_gap=1,
    )
    band_row = BandExperiencePlan(
        id="band-row-1",
        project_id="project-1",
        arc_id="arc-1",
        band_id="arc-1:band:2",
        chapter_start=11,
        chapter_end=14,
        schedule_json=schedule.model_dump_json(),
    )
    obligation = NarrativeObligation(
        id="obl-band",
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="reader_promise_payoff",
        priority="P1",
        status="active",
        summary="band 内兑现读者承诺。",
        hardness="design_debt",
        deadline_chapter=14,
        payoff_test="第14章前必须给出审计窗口真相证据。",
        linked_plan_patch_ids=["patch-old"],
        metadata={"minimum_scope": "band"},
    )

    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=10,
        trigger_stage="post_acceptance",
        plans=[],
        band_rows=[band_row],
        canon_quality_context={},
        obligations=[obligation],
        target_total_chapters=20,
        include_current=False,
    )

    assert result.status == "fail"
    assert result.issues[0].issue_type == "obligation_missing_from_band_plan"
    patch = result.plan_patches[0]
    assert patch.patch_type == "obligation_band_plan_binding"
    assert patch.target_scope == "band"
    assert patch.target_band_id == "arc-1:band:2"
    assert patch.affected_chapters == [11, 12, 13, 14]


def test_future_plan_auditor_skips_chapter_binding_when_band_contract_covers_obligation() -> None:
    schedule_payload = {
        "band_id": "arc-1:band:2",
        "chapter_start": 11,
        "chapter_end": 14,
        "stall_guard_max_gap": 1,
        "band_obligation_contract": {
            "open_obligations": ["obl-band"],
            "must_resolve_by_band_end": ["obl-band"],
            "allowed_carry_forward": [],
            "payoff_tests": {"obl-band": "第14章前必须给出审计窗口真相证据。"},
            "affected_chapters": {"obl-band": [11, 12, 13, 14]},
            "writer_context_injections": [],
            "reviewer_context_injections": [],
        },
    }
    band_row = BandExperiencePlan(
        id="band-row-1",
        project_id="project-1",
        arc_id="arc-1",
        band_id="arc-1:band:2",
        chapter_start=11,
        chapter_end=14,
        schedule_json=json.dumps(schedule_payload, ensure_ascii=False),
    )
    deadline_plan = _plan(14, one_line="兑现审计窗口。")
    obligation = NarrativeObligation(
        id="obl-band",
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="reader_promise_payoff",
        priority="P1",
        status="active",
        summary="band 内兑现读者承诺。",
        hardness="design_debt",
        deadline_chapter=14,
        payoff_test="第14章前必须给出审计窗口真相证据。",
        linked_plan_patch_ids=["patch-old"],
        metadata={"minimum_scope": "band"},
    )

    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=10,
        trigger_stage="post_acceptance",
        plans=[deadline_plan],
        band_rows=[band_row],
        canon_quality_context={},
        obligations=[obligation],
        target_total_chapters=20,
        include_current=False,
    )

    assert result.status == "pass"
    assert result.issues == []
    assert result.plan_patches == []


def test_future_plan_auditor_promotes_must_resolve_now_obligation_to_pre_write_patch() -> None:
    plan = _plan(12, one_line="陆明准备进入终端大厅。")
    obligation = NarrativeObligation(
        id="obl-now",
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="reader_promise_payoff",
        priority="P1",
        status="active",
        summary="偿还前文承诺。",
        hardness="canon_risk",
        deadline_chapter=12,
        payoff_test="第12章必须给出终端门禁来源证据。",
        must_resolve_now=True,
    )

    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=11,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={},
        obligations=[obligation],
        target_total_chapters=20,
        include_current=False,
    )

    assert result.status == "fail"
    assert [issue.issue_type for issue in result.issues] == ["obligation_pre_write_required"]
    assert result.plan_patches[0].patch_type == "obligation_pre_write"
    assert result.metadata["suppressed_prompt_constraint_keys"] == ["obligation:obl-now"]

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])
    serialized = json.dumps(
        {"goals": json.loads(plan.goals_json), "experience": json.loads(plan.experience_plan_json)},
        ensure_ascii=False,
    )
    assert "obl-now" in serialized
    assert "第12章必须给出终端门禁来源证据" in serialized


def test_future_plan_auditor_promotes_stale_open_signal_to_pre_write_patch() -> None:
    plan = _plan(14, one_line="陆明进入下一处证据点。")

    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=13,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={
            "open_signals": [
                {
                    "signal_id": "sig-1",
                    "signal_type": "placeholder_leakage",
                    "severity": "error",
                    "chapter_number": 10,
                    "subject_key": "placeholder",
                    "description": "第10章残留占位符，必须替换为具体证据。",
                }
            ]
        },
        obligations=[],
        target_total_chapters=20,
        include_current=False,
    )

    assert result.status == "fail"
    assert [issue.issue_type for issue in result.issues] == ["stale_open_signal_pre_write_required"]
    assert result.plan_patches[0].patch_type == "signal_pre_write"
    assert result.metadata["suppressed_prompt_constraint_keys"] == ["signal:sig-1"]

    FuturePlanAuditor().apply_plan_patch(plan, result.plan_patches[0])
    serialized = json.dumps(
        {"goals": json.loads(plan.goals_json), "experience": json.loads(plan.experience_plan_json)},
        ensure_ascii=False,
    )
    assert "sig-1" not in serialized
    assert "第10章残留占位符" in serialized
    assert result.plan_patches[0].metadata["signal_id"] == "sig-1"
    assert result.plan_patches[0].source_signal_ids == ["sig-1"]
    signal = {"signal_id": "sig-1", "description": "第10章残留占位符，必须替换为具体证据。",
              "signal_type": "placeholder_leakage", "severity": "error", "chapter_number": 10}
    assert _plan_mentions_signal(plan, signal_id="sig-1", description=signal["description"])
    assert select_stale_signal_targets(open_signals=[signal], plans=[plan], current_chapter=13, include_current=False) == []


def test_future_audit_has_no_countdown_or_custody_mixins() -> None:
    mixin_names = {cls.__name__ for cls in FuturePlanAuditor.__mro__}
    assert "FuturePlanCountdownMixin" not in mixin_names
    assert "FuturePlanCustodyMixin" not in mixin_names


@pytest.mark.parametrize("description", ['Placeholder "TODO" leaked', 'back\\slash leaked', 'Line1\nLine2'], ids=["quote", "backslash", "newline"])
def test_patched_signal_with_escaped_description_is_not_reselected(description: str) -> None:
    plan = _plan(14, one_line="Follow the evidence.")
    signal = {
        "signal_id": "sig-escaped", "description": description,
        "signal_type": "placeholder_leakage", "severity": "error", "chapter_number": 10,
    }
    auditor = FuturePlanAuditor()
    result = auditor.audit_plans(
        project_id="project-1", current_chapter=13, trigger_stage="pre_write", plans=[plan],
        canon_quality_context={"open_signals": [signal]}, obligations=[],
        target_total_chapters=20, include_current=False,
    )
    assert len(result.plan_patches) == 1
    auditor.apply_plan_patch(plan, result.plan_patches[0])
    assert "sig-escaped" not in plan.goals_json
    assert _plan_mentions_signal(plan, signal_id="sig-escaped", description=description)
    assert select_stale_signal_targets(
        open_signals=[signal], plans=[plan], current_chapter=13, include_current=False,
    ) == []


def test_empty_signal_description_does_not_match_unrelated_plan() -> None:
    assert not _plan_mentions_signal(_plan(14, one_line="Follow the evidence."), signal_id="", description="")


@pytest.mark.parametrize("field,value", [
    ("title", "covered-marker"),
    ("one_line", "covered-marker"),
    ("goals_json", '["covered-marker"]'),
    ("world_bindings_json", '[{"kind": "intent", "ref": "intent-1", "how": "covered-marker"}]'),
    ("intended_changes_json", '[{"description": "covered-marker"}]'),
    ("hook", "covered-marker"),
    ("experience_plan_json", '{"obligations_to_resolve": ["covered-marker"]}'),
])
def test_plan_text_coverage_is_shared_by_obligation_and_signal_audits(field, value) -> None:
    from forwin.planning.future_plan_audit.helpers import plan_text
    from forwin.planning.future_plan_audit.obligation_pre_audit import select_urgent_obligation_targets

    plan = _plan(2, one_line="Continue")
    setattr(plan, field, value)
    obligation = NarrativeObligation(
        id="covered-marker", project_id="project-1", status="active",
        origin_chapter_number=1, obligation_type="motivation_gap", payoff_test="Resolve the debt",
        must_resolve_now=True, deadline_chapter=2, summary="Resolve the debt",
    )

    assert "covered-marker" in plan_text(plan)
    assert select_urgent_obligation_targets(
        obligations=[obligation], plans=[plan], current_chapter=1, include_current=False,
    ) == []
    assert _plan_mentions_signal(plan, signal_id="covered-marker", description="")
