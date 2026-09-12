from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forwin.canon import quality_preparation
from forwin.canon_quality.chapter_review_form.form_builder import build_form
from forwin.canon_quality.chapter_review_form.form_schema import ChapterReviewAnswers
from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.signals import CanonQualitySignal
from forwin.canon_quality.types import CanonQualityAnalysisResult
from forwin.models.base import get_session_factory
from forwin.models.draft import CandidateDraftRecord
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.protocol.review import ReviewVerdict
from forwin.review.repair.service import _review_from_canon_gate_block
from tests.test_canon_repair_stage import (
    _build_pipeline,
    _one_chapter_arc,
    _writer_output,
    _isolate_canon_repair_from_hard_floor,  # noqa: F401 - imported pytest autouse fixture
)
from tests.postgres import postgres_test_url


def _obligation(project_id="p", **changes):
    return NarrativeObligation(
        id="promise",
        project_id=project_id,
        origin_chapter_number=0,
        status="active",
        obligation_type="reader_promise",
        priority="P0",
        summary="交代铜钥匙来源",
        subject_refs=["铜钥匙"],
        deadline_chapter=1,
        payoff_test="交代铜钥匙来自何人",
        resolution_conditions=["以客观证据证实来源"],
        linked_plan_patch_ids=["patch"],
    ).model_copy(update=changes)


def _patch(project_id="p"):
    return NarrativePlanPatch(
        id="patch",
        project_id=project_id,
        source_obligation_ids=["promise"],
        affected_chapters=[1],
        applied=True,
        validation_status="passed",
        must_preserve=["铜钥匙仍由原持有人保管"],
        must_not_change=["不得提前揭示密室位置"],
    )


def test_due_obligation_routes_concrete_contract_without_signals():
    gate = evaluate_canon_admission(
        project_id="p",
        chapter_number=1,
        signals=[],
        obligations=[_obligation()],
        plan_patches=[_patch()],
    )
    assert gate.required_repair_scope == "draft"
    blocker = gate.blocking_items[0]
    assert blocker.obligation_id == "promise"
    assert blocker.failure_domain == "content"
    assert blocker.unmet_conditions == ["交代铜钥匙来自何人", "以客观证据证实来源"]
    review = _review_from_canon_gate_block(gate)
    contract = review.repair_instruction
    assert "promise" in " ".join(contract.must_fix)
    assert "以客观证据证实来源" in " ".join(contract.must_fix)
    assert "铜钥匙仍由原持有人保管" in contract.must_preserve
    assert "不得提前揭示密室位置" in contract.must_preserve


@pytest.mark.parametrize("mode", ["strict", "pulp_fatal"])
def test_mixed_infrastructure_and_due_content_cannot_spend_rewrite(mode):
    gate = evaluate_canon_admission(
        project_id="p",
        chapter_number=1,
        mode=mode,
        signals=[
            CanonQualitySignal(
                signal_id="unavailable",
                project_id="p",
                chapter_number=1,
                signal_type="form_llm_unavailable",
                severity="error",
            )
        ],
        obligations=[_obligation()],
        plan_patches=[_patch()],
    )
    assert not gate.commit_allowed
    assert gate.required_repair_scope is None
    assert any(item.failure_domain == "infrastructure" for item in gate.blocking_items)


@pytest.mark.parametrize(
    "changes", [{"status": "expired"}, {"linked_plan_patch_ids": []}]
)
def test_unsupported_obligation_has_explicit_stop(changes):
    gate = evaluate_canon_admission(
        project_id="p",
        chapter_number=1,
        obligations=[_obligation(**changes)],
        plan_patches=[_patch()],
    )
    assert gate.required_repair_scope is None
    assert any(item.failure_domain == "unsupported" for item in gate.blocking_items)


@pytest.mark.parametrize(
    "scenario",
    [
        "accept",
        "exhausted",
        "review_budget",
        "new_task_budget",
        "infra",
        "no_executor",
        "expired",
        "blocked",
        "invalid_patch",
    ],
)
def test_real_pipeline_due_without_signals_repairs_once_then_fresh_admission(
    monkeypatch, scenario
):
    repair_succeeds = scenario == "accept"
    repairs_expected = (
        0
        if scenario
        in {
            "new_task_budget",
            "infra",
            "no_executor",
            "expired",
            "blocked",
            "invalid_patch",
        }
        else 1
    )
    canon_repair_expected = scenario in {"accept", "exhausted"}
    from forwin.state.updater import StateUpdater

    create_project = StateUpdater.create_project

    def create_serial(updater, *args, **kwargs):
        project = create_project(updater, *args, **kwargs)
        project.automation_json = '{"primary_publish_platform":"qidian"}'
        return project

    monkeypatch.setattr(StateUpdater, "create_project", create_serial)
    pipeline = _build_pipeline(
        postgres_test_url("obligation-bounded-pipeline"), max_rewrites=1
    )
    calls = []
    writer_contexts = []
    gate_bodies = []
    from forwin.generation.pipeline_core import project_chapters

    original_gate = project_chapters.handle_chapter_review_gate

    def review_gate(*args, **kwargs):
        from dataclasses import replace

        gate_bodies.append(kwargs["writer_output"].body)
        outcome = original_gate(*args, **kwargs)
        return (
            replace(outcome, gate_approved=True) if len(gate_bodies) == 1 else outcome
        )

    monkeypatch.setattr(project_chapters, "handle_chapter_review_gate", review_gate)
    pipeline.arc_director.plan_arc = lambda *_: _one_chapter_arc("obligation repair")

    def write(context):
        writer_contexts.append(context)
        output = _writer_output(1)
        output.body = (
            "铜钥匙来自父亲。遗书证实父亲亲手将钥匙托付给持有人。fulfilled。"
            if len(writer_contexts) > 1 and repair_succeeds
            else "铜钥匙来源未知。unknown。"
        ) + output.body
        return output

    pipeline.writer.write_chapter = write
    review_calls = []

    def draft_review(**kwargs):
        review_calls.append(kwargs["writer_output"].body)
        if scenario == "review_budget" and len(review_calls) == 1:
            from tests.test_canon_repair_stage import _draft_blocking_review

            return _draft_blocking_review()
        return ReviewVerdict(verdict="pass", issues=[])

    pipeline.candidate_review.draft_review = SimpleNamespace(review=draft_review)
    if scenario == "no_executor":
        from dataclasses import replace

        pipeline.repair_execution = replace(
            pipeline.repair_execution, writer_execution=None
        )

    def analyze(**kwargs):
        repo = NarrativeObligationRepository(kwargs["session"])
        if not calls:
            repo.create_obligation(
                _obligation(
                    kwargs["project_id"],
                    **(
                        {"status": scenario}
                        if scenario in {"expired", "blocked"}
                        else {"linked_plan_patch_ids": []}
                        if scenario == "invalid_patch"
                        else {}
                    ),
                )
            )
            if scenario == "new_task_budget":
                plan = kwargs["session"].scalar(select(ChapterPlan))
                plan.repair_attempt_count = 1
                from forwin.generation.review_auto_retry import (
                    reset_chapter_for_auto_review_retry,
                )

                reset_chapter_for_auto_review_retry(
                    kwargs["session"],
                    project_id=kwargs["project_id"],
                    chapter_number=1,
                    plan=plan,
                    source="auto_continue_review_retry",
                    reason="retry",
                    summary="retry",
                )
            repo.create_plan_patch(_patch(kwargs["project_id"]))
        active = repo.list_active_for_context(kwargs["project_id"], chapter_number=1)
        obligation = _obligation(kwargs["project_id"])
        body = kwargs["writer_output"].body
        form = build_form(
            project_id=kwargs["project_id"],
            chapter_number=1,
            chapter_text=body,
            obligations=active,
        )
        value = "fulfilled" if "fulfilled" in body else "unknown"

        def answer(v):
            return dict(
                value=v,
                evidence_quote=body[:50],
                subject_of_quote="铜钥匙",
                confidence=0.95,
                explanation="以遗书核对铜钥匙来源及持有人。",
            )

        answers = ChapterReviewAnswers.model_validate(
            dict(
                project_id=kwargs["project_id"],
                chapter_number=1,
                form_schema_version=form.form_schema_version,
                reviewed_body_sha256=form.reviewed_body_sha256,
                obligations=[
                    dict(
                        id="promise",
                        addressed=answer(value),
                        payoff_evidence=answer(value),
                        subject_matches=answer("true"),
                        condition_results=[
                            dict(condition=c, assessment=answer(value))
                            for c in [
                                obligation.payoff_test,
                                *obligation.resolution_conditions,
                            ]
                        ],
                    )
                ]
                if active
                else [],
            )
        )
        calls.append(kwargs["draft_id"])
        from forwin.canon_quality.cache import persist_quality_projection
        from forwin.canon_quality.repository import CanonQualityRepository
        from forwin.canon_quality.types import QualityAnalysisCachePayload

        analysis = CanonQualityAnalysisResult(
            project_id=kwargs["project_id"],
            chapter_number=1,
            draft_id=kwargs["draft_id"],
            signals=(
                [
                    CanonQualitySignal(
                        signal_id="infra",
                        project_id=kwargs["project_id"],
                        chapter_number=1,
                        signal_type="form_llm_unavailable",
                        severity="error",
                    )
                ]
                if scenario == "infra"
                else []
            ),
            form=form,
            answers=answers,
        )
        persist_quality_projection(
            CanonQualityRepository(kwargs["session"]),
            project_id=kwargs["project_id"],
            chapter_number=1,
            payload=QualityAnalysisCachePayload(analysis=analysis),
        )
        return analysis

    monkeypatch.setattr(quality_preparation, "analyze_writer_output_quality", analyze)
    try:
        result = pipeline.run("p", "g", 1)
        with get_session_factory(pipeline.engine)() as session:
            attempts = session.scalars(select(ChapterRewriteAttempt)).all()
            candidates = session.scalars(
                select(CandidateDraftRecord).order_by(CandidateDraftRecord.version)
            ).all()
            plan = session.scalar(select(ChapterPlan))
            obligation = session.get(NarrativeObligationRow, "promise")
            assert len(attempts) == repairs_expected
            assert len(candidates) == repairs_expected + 1
            assert len(gate_bodies) == (2 if canon_repair_expected else 1)
            assert calls == [
                item.candidate_draft_id
                for item in (candidates if canon_repair_expected else candidates[-1:])
            ]
            if repairs_expected:
                assert candidates[0].review_id != candidates[1].review_id
                assert (
                    candidates[-1].repair_attempt_count
                    == plan.repair_attempt_count
                    == 1
                )
            assert result.status == (
                "completed" if repair_succeeds else "needs_review"
            ), result
            assert plan.status == ("accepted" if repair_succeeds else "needs_review")
            assert obligation.status == (
                "resolved"
                if repair_succeeds
                else scenario
                if scenario in {"expired", "blocked"}
                else "active"
            )
            assert obligation.deadline_chapter == 1
            assert not obligation.waive_reason
            assert not any(attempt.forced_accept_applied for attempt in attempts)
            if canon_repair_expected:
                contract = writer_contexts[1].repair_contract
                assert "promise" in " ".join(contract.must_fix)
                assert "以客观证据证实来源" in " ".join(contract.must_fix)
                assert "铜钥匙仍由原持有人保管" in contract.must_preserve
            if repair_succeeds:
                canon_plan = json.loads(candidates[-1].canon_commit_plan_json)
                assert canon_plan["acceptance_mode"] == "normal"
                evidence = canon_plan["obligation_resolution_plan"]
                assert evidence["candidate_id"] == candidates[-1].id
                assert evidence["draft_id"] == candidates[-1].candidate_draft_id
                assert (
                    json.loads(candidates[-1].eligibility_decision_json)["candidate_id"]
                    == candidates[-1].id
                )
                assert candidates[0].canon_commit_id == ""
            if scenario in {
                "infra",
                "no_executor",
                "expired",
                "blocked",
                "invalid_patch",
            }:
                assert result.system_block_chapters == [1]
    finally:
        pipeline.llm_client.close()
        pipeline.engine.dispose()


def test_expired_debt_cannot_rewrite_frozen_history_or_gain_exemption():
    from datetime import timedelta
    from forwin.models.draft import ChapterDraft
    from forwin.models.canon import CanonPublicationProtection
    from forwin.publisher_runtime.protection import require_revision_unprotected
    from forwin.review.repair.plan_patch import (
        RepairPlanPatchService,
        RepairPlanPatchRequest,
    )
    from forwin.state.repo import StateRepository
    from tests.test_canon_publisher_jobs import _fixture
    from tests.test_publisher_attempt_leases import NOW, _released_job

    fixture = _fixture("expired-obligation-frozen-history")
    try:
        _released_job(fixture)
        claim = fixture.runtime.attempts.claim(
            client_id="frozen-test", connected_platforms=["qidian"], now=NOW
        )
        fixture.runtime.attempts.transition(
            job_id=claim["job_id"],
            attempt_id=claim["attempt_id"],
            worker_id="frozen-test",
            lease_epoch=claim["lease_epoch"],
            phase="mutation_started",
            now=NOW + timedelta(seconds=1),
        )
        with fixture.runtime.session_factory.begin() as session:
            repo = NarrativeObligationRepository(session)
            candidate = session.get(CandidateDraftRecord, fixture.candidate_id)
            repo.create_obligation(
                _obligation(
                    fixture.project_id,
                    status="expired",
                    origin_chapter_number=1,
                    origin_draft_id=candidate.candidate_draft_id,
                )
            )
            repo.create_plan_patch(_patch(fixture.project_id))
            gate = evaluate_canon_admission(
                project_id=fixture.project_id,
                chapter_number=2,
                obligations=repo.list_for_admission(
                    fixture.project_id, chapter_number=2
                ),
                plan_patches=[_patch(fixture.project_id)],
            )
            assert gate.blocking_reasons == ["expired_obligation:promise"]
            assert gate.required_repair_scope is None
            assert _review_from_canon_gate_block(gate).repair_instruction is None
            with pytest.raises(ValueError, match="publication protection"):
                require_revision_unprotected(
                    session, project_id=fixture.project_id, from_chapter=1
                )
            from forwin.protocol.review import RepairInstruction

            service = RepairPlanPatchService(
                retrieval_broker=None, arc_envelope_manager=None
            )
            with pytest.raises(ValueError, match="accepted chapter plan"):
                service.apply(
                    RepairPlanPatchRequest(
                        session=session,
                        repo=StateRepository(session),
                        project_id=fixture.project_id,
                        chapter_plan=session.get(ChapterPlan, fixture.chapter_plan_id),
                        context=None,
                        repair_scope="chapter_plan",
                        instruction=RepairInstruction(
                            repair_scope="chapter_plan", failure_type="mixed"
                        ),
                    )
                )
            assert (
                session.get(ChapterDraft, candidate.candidate_draft_id).body_text
                == fixture.body
            )
            assert (
                session.get(ChapterPlan, fixture.chapter_plan_id).active_commit_id
                == fixture.canon_commit_id
            )
            assert (
                session.scalar(select(CanonPublicationProtection)).state == "reserved"
            )
            assert session.scalars(select(ChapterRewriteAttempt)).all() == []
            debt = session.get(NarrativeObligationRow, "promise")
            assert (debt.status, debt.deadline_chapter, debt.waive_reason) == (
                "expired",
                1,
                "",
            )
    finally:
        fixture.engine.dispose()
