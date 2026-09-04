from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from forwin.candidate_drafts import CandidateDraftRepository
from forwin.checker.rules import ContinuityChecker
from forwin.config import InfrastructureConfig
from forwin.models.draft import ChapterDraft
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.scene import SceneOutput
from forwin.protocol.state_change import TimeAdvance
from forwin.protocol.writer import WriterOutput
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


@pytest.fixture
def repair_runtime(tmp_path):
    policy_payload = RuntimePolicy.for_profile("standard").model_dump(mode="python")
    policy_payload["review"].update(max_rewrites=1, blocking_rewrites=1)
    pipeline = RuntimeContainer.from_config(
        InfrastructureConfig(
            database_url=postgres_test_url("repair_preserved_title"),
            artifact_root=str(tmp_path / "artifacts"),
            qdrant_url=":memory:",
            embedding_backend="hash",
            minimax_api_key="",
        ),
        policy=RuntimePolicy.model_validate(policy_payload),
        role="generation_worker",
    ).build_chapter_pipeline()
    try:
        with pipeline._SessionFactory() as session:
            project = StateUpdater(session).create_project(
                title="标题保留回归",
                premise="修复时保持契约。",
                genre="仙侠",
                runtime_policy=pipeline.policy,
            )
            arc = ArcPlanVersion(project_id=project.id, arc_synopsis="抵达津口")
            session.add(arc)
            session.flush()
            chapter_plan = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=10,
                title="第10章",
                one_line="在辰正二刻完成交割。",
            )
            session.add(chapter_plan)
            session.commit()
            yield pipeline, session, chapter_plan
    finally:
        pipeline.close()


def _run_title_repair(
    repair_runtime,
    *,
    must_preserve: list[str],
    design_patch: dict[str, object] | None = None,
    keep_body_error: bool = False,
):
    pipeline, session, chapter_plan = repair_runtime
    instruction = RepairInstruction(
        repair_scope="chapter_plan",
        failure_type="continuity",
        must_fix=["交割时间必须是辰正二刻。"],
        must_preserve=must_preserve,
        design_patch=design_patch or {},
    )
    original = WriterOutput(
        project_id=chapter_plan.project_id,
        chapter_number=10,
        title="第10章",
        body="巳初二刻完成交割。",
        end_of_chapter_summary="交割结束。",
    )
    rewritten = WriterOutput(
        project_id=chapter_plan.project_id,
        chapter_number=10,
        title="第10章 空税入泽",
        body="巳初二刻完成交割。" if keep_body_error else "辰正二刻完成交割。",
        end_of_chapter_summary="交割结束，准备离津。",
        scene_outputs=[
            SceneOutput(scene_no=1, scene_objective="完成交割", text="场景记录原样保留。")
        ],
        time_advance=TimeAdvance(
            new_time_label="辰正三刻", duration_description="交割后一刻"
        ),
        must_preserve_facts=["交割已完成"],
        generation_meta={"source": "repair-writer-fixture"},
    )
    seen_outputs: list[WriterOutput] = []

    def review_output(*, writer_output, **_kwargs):
        seen_outputs.append(writer_output.model_copy(deep=True))
        if len(seen_outputs) == 1 or keep_body_error:
            return ReviewVerdict(
                verdict="fail",
                issues=[
                    ContinuityIssue(
                        rule_name="form_invariant_drift",
                        issue_type="form_invariant_drift",
                        severity="error",
                        description="交割时间必须是辰正二刻。",
                        evidence_refs=["draft:body_head"],
                        blocking=True,
                    )
                ],
                repair_instruction=instruction,
            )
        return ReviewVerdict(verdict="pass", issues=[])

    execution = replace(
        pipeline.repair_execution,
        _plan_writer_output_entities=lambda *, writer_output, **_kwargs: writer_output,
        _review_current_output=review_output,
        _write_chapter_with_attention_fallback=lambda **_kwargs: rewritten,
    )
    output, review, forced_accept = pipeline.repair.review_candidate(
        execution=execution,
        session=session,
        repo=StateRepository(session),
        updater=StateUpdater(session),
        checker=ContinuityChecker(StateRepository(session)),
        project_id=chapter_plan.project_id,
        chapter_plan=chapter_plan,
        context=pipeline.retrieval_broker.build_chapter_context(
            StateRepository(session), chapter_plan.project_id, chapter_plan
        ),
        writer_output=original,
    )
    session.flush()
    candidate = CandidateDraftRepository(session).latest_for_chapter(
        project_id=chapter_plan.project_id, chapter_number=10
    )
    assert candidate is not None
    metadata = json.loads(candidate.metadata_json)
    assert metadata["title"] == output.title
    draft = session.execute(
        select(ChapterDraft).where(ChapterDraft.id == candidate.candidate_draft_id)
    ).scalar_one()
    assert draft.body_text == rewritten.body
    return output, review, forced_accept, seen_outputs, rewritten


def test_repair_keeps_exactly_protected_title_before_review_and_persistence(repair_runtime):
    output, review, forced_accept, seen_outputs, rewritten = _run_title_repair(
        repair_runtime, must_preserve=["第10章"]
    )

    assert output.title == "第10章"
    assert seen_outputs[1].title == "第10章"
    assert seen_outputs[1].model_dump(exclude={"title"}) == rewritten.model_dump(
        exclude={"title"}
    )
    assert rewritten.title == "第10章 空税入泽"
    assert review.verdict == "pass"
    assert review.repair_verification.preserved_all_must_preserve is True
    assert forced_accept is False


@pytest.mark.parametrize("must_preserve", [[], ["第1章"], ["保留第10章的交割事实"]])
def test_repair_keeps_writer_title_when_current_title_is_not_exactly_protected(
    repair_runtime, must_preserve
):
    output, review, forced_accept, seen_outputs, _ = _run_title_repair(
        repair_runtime, must_preserve=must_preserve
    )

    assert output.title == "第10章 空税入泽"
    assert seen_outputs[1].title == "第10章 空税入泽"
    assert review.verdict == "pass"
    assert forced_accept is False


@pytest.mark.parametrize("title_key", ["chapter_plan_title", "title"])
def test_explicit_plan_rename_is_not_overridden_and_preserve_conflict_still_blocks(
    repair_runtime, title_key
):
    output, review, forced_accept, seen_outputs, _ = _run_title_repair(
        repair_runtime,
        must_preserve=["第10章"],
        design_patch={title_key: "第10章 空税入泽"},
    )

    assert repair_runtime[2].title == "第10章 空税入泽"
    assert output.title == "第10章 空税入泽"
    assert seen_outputs[1].title == "第10章 空税入泽"
    assert review.verdict == "fail"
    assert review.repair_verification.preserved_all_must_preserve is False
    assert any(issue.rule_name == "repair_preserve_breach" for issue in review.issues)
    assert forced_accept is False


def test_preserving_title_does_not_accept_an_unfixed_body_error(repair_runtime):
    output, review, forced_accept, _, _ = _run_title_repair(
        repair_runtime, must_preserve=["第10章"], keep_body_error=True
    )

    assert output.title == "第10章"
    assert review.verdict == "fail"
    assert review.repair_verification.fixed_all_must_fix is False
    assert review.repair_verification.preserved_all_must_preserve is True
    assert any(issue.rule_name == "repair_unfixed" for issue in review.issues)
    assert review.final_residual_decision.decision == "manual_review_required"
    assert forced_accept is False


def test_repair_event_reports_unknown_coverage_without_claiming_contract_success(repair_runtime):
    from forwin.audit.events import DecisionEventType
    from forwin.models.audit import DecisionEvent

    _, review, _, _, _ = _run_title_repair(repair_runtime, must_preserve=["第10章"])
    assert review.repair_verification.fixed_all_must_fix is None
    _, session, chapter_plan = repair_runtime
    event = session.scalars(select(DecisionEvent).where(
        DecisionEvent.project_id == chapter_plan.project_id,
        DecisionEvent.event_type == DecisionEventType.REPAIR_SUCCEEDED,
    )).one()
    assert "未验证" in event.summary
    assert "已修复。" not in event.summary


@pytest.mark.parametrize("keep_body_error", [False, True])
def test_persisted_repair_unknown_survives_reload_api_and_canon_eligibility(
    repair_runtime, keep_body_error
):
    from forwin.application.projects.reviews import get_chapter_review
    from forwin.canon.eligibility import candidate_ineligibility_reason
    from forwin.generation.pipeline_core.finalization import _load_review_verdict
    from forwin.models.draft import ChapterReview

    _, current_review, _, _, _ = _run_title_repair(
        repair_runtime,
        must_preserve=["保留交割事实"],
        keep_body_error=keep_body_error,
    )
    pipeline, session, chapter_plan = repair_runtime
    project_id, chapter_plan_id = chapter_plan.project_id, chapter_plan.id
    expected_fixed = False if keep_body_error else None
    assert current_review.repair_verification.fixed_all_must_fix is expected_fixed
    assert current_review.repair_verification.preserved_all_must_preserve is None
    # The error case exercises the final-residual metadata rewrite as well as save_review.
    assert current_review.repair_exhausted is keep_body_error
    session.commit()

    with pipeline._SessionFactory() as reloaded_session:
        persisted = reloaded_session.scalars(
            select(ChapterReview)
            .join(ChapterDraft, ChapterReview.draft_id == ChapterDraft.id)
            .where(ChapterDraft.chapter_plan_id == chapter_plan_id)
            .order_by(ChapterDraft.version.desc(), ChapterReview.created_at.desc())
        ).first()
        reloaded = _load_review_verdict(persisted)
        assert reloaded.repair_verification.fixed_all_must_fix is expected_fixed
        assert reloaded.repair_verification.preserved_all_must_preserve is None
        assert bool(candidate_ineligibility_reason(reloaded)) is keep_body_error

    api = get_chapter_review(
        project_id,
        10,
        get_session=pipeline._SessionFactory,
        decision_refs_for_chapter_review=lambda _session, **_kwargs: [],
    )
    assert api.repair_verification.fixed_all_must_fix is expected_fixed
    assert api.repair_verification.preserved_all_must_preserve is None
    assert api.repair_verification.checks[1].status == "unknown"
    assert api.rewrite_attempts[-1].verification.preserved_all_must_preserve is None
    assert api.repair_verification.model_dump()["preserved_all_must_preserve"] is None


def test_legacy_stored_review_missing_aggregate_fields_still_defaults_to_failure(
    repair_runtime,
):
    from forwin.application.projects.reviews import get_chapter_review
    from forwin.canon.eligibility import candidate_ineligibility_reason
    from forwin.generation.pipeline_core.finalization import _load_review_verdict
    from forwin.models.draft import ChapterReview

    pipeline, session, chapter_plan = repair_runtime
    project_id = chapter_plan.project_id
    draft = StateUpdater(session).save_draft(
        chapter_plan.id,
        WriterOutput(chapter_number=10, title="旧记录", body="旧稿正文。", end_of_chapter_summary=""),
        raw_response="",
    )
    row = ChapterReview(
        draft_id=draft.id,
        verdict="pass",
        issues_json="[]",
        review_meta_json='{"repair_verification":{"verifier_mode":"rule_only"}}',
    )
    session.add(row)
    session.flush()
    review_id = row.id
    session.commit()
    with pipeline._SessionFactory() as reloaded_session:
        reloaded = _load_review_verdict(reloaded_session.get(ChapterReview, review_id))
        assert reloaded.repair_verification.fixed_all_must_fix is False
        assert reloaded.repair_verification.preserved_all_must_preserve is False
        assert candidate_ineligibility_reason(reloaded)
    api = get_chapter_review(
        project_id, 10, get_session=pipeline._SessionFactory,
        decision_refs_for_chapter_review=lambda _session, **_kwargs: [],
    )
    assert api.repair_verification.fixed_all_must_fix is False
    assert api.repair_verification.preserved_all_must_preserve is False
