from __future__ import annotations

import pytest

from forwin.canon_quality.chapter_review_form.form_builder import build_form
from forwin.canon_quality.chapter_review_form.form_schema import ChapterReviewAnswers
from forwin.narrative_obligations.types import NarrativeObligation

BODY = "林青取出父亲的遗书，信中写明铜钥匙是父亲临终托付给她的。"


def reviewed_fixture(
    *,
    body=BODY,
    value="fulfilled",
    subject_value="true",
    condition_value="fulfilled",
    quote=None,
):
    obligation = NarrativeObligation(
        id="promise",
        project_id="book",
        origin_chapter_number=1,
        status="active",
        obligation_type="custom_reader_promise",
        summary="交代林青铜钥匙的来源",
        subject_refs=["林青", "铜钥匙"],
        deadline_chapter=12,
        payoff_test="交代林青持有的铜钥匙来自何人",
        resolution_conditions=["以客观证据证实来源"],
    )
    form = build_form(
        project_id="book",
        chapter_number=11,
        chapter_text=body,
        obligations=[obligation],
    )
    answer = lambda v: {
        "value": v,
        "evidence_quote": body if quote is None else quote,
        "subject_of_quote": "林青",
        "confidence": 0.95,
        "explanation": "遗书证实父亲托付给林青。",
    }
    answers = ChapterReviewAnswers.model_validate(
        {
            "project_id": "book",
            "chapter_number": 11,
            "form_schema_version": form.form_schema_version,
            "reviewed_body_sha256": form.reviewed_body_sha256,
            "obligations": [
                {
                    "id": "promise",
                    "addressed": answer(value),
                    "payoff_evidence": answer(value),
                    "subject_matches": answer(subject_value),
                    "condition_results": [
                        {"condition": c, "assessment": answer(condition_value)}
                        for c in [
                            obligation.payoff_test,
                            *obligation.resolution_conditions,
                        ]
                    ],
                }
            ],
        }
    )
    return obligation, form, answers


def make_plan(obligation, form, answers, body=BODY):
    from forwin.narrative_obligations.resolution_evidence import build_resolution_plan

    return build_resolution_plan(
        obligations=[obligation],
        form=form,
        answers=answers,
        project_id="book",
        chapter_number=11,
        draft_id="draft-11",
        candidate_id="candidate-11",
        chapter_body=body,
    )


def test_review_form_includes_complete_contract_for_legal_early_payoff():
    _obligation, form, _ = reviewed_fixture()
    assert len(form.obligations) == 1
    ask = form.obligations[0]
    assert ask.subject_refs == ["林青", "铜钥匙"]
    assert ask.resolution_conditions == ["以客观证据证实来源"]
    assert ask.contract_fingerprint
    assert not ask.must_resolve_now


def test_complete_payoff_has_version_bound_exact_evidence():
    obligation, form, answers = reviewed_fixture()
    plan = make_plan(obligation, form, answers)
    assert plan.resolved_obligation_ids == ["promise"]
    assert plan.evidence[0].draft_id == "draft-11"
    assert plan.evidence[0].candidate_id == "candidate-11"
    assert plan.evidence[0].evidence_refs[0].endswith(f"#0:{len(BODY)}")
    with pytest.raises(ValueError):
        plan.evidence[0].draft_id = "other"


@pytest.mark.parametrize(
    "changes",
    [
        {
            "body": "林青说，明天她会解释铜钥匙的来源。",
            "value": "unaddressed",
            "condition_value": "unaddressed",
        },
        {"body": "王武的钥匙来自他的父亲。", "subject_value": "false"},
        {
            "body": "铜钥匙来源仍然不明，没有证据。",
            "value": "unknown",
            "condition_value": "unknown",
        },
        {"condition_value": "partial"},
        {"quote": "旧候选正文里的遗书"},
    ],
)
def test_unfulfilled_wrong_subject_or_unbound_evidence_cannot_resolve(changes):
    obligation, form, answers = reviewed_fixture(**changes)
    plan = make_plan(obligation, form, answers, changes.get("body", BODY))
    assert plan.resolved_obligation_ids == []


@pytest.mark.parametrize(
    "changes",
    [
        {"subject_refs": ["王武"]},
        {"resolution_conditions": ["证实钥匙未被伪造"]},
        {"payoff_test": "证实钥匙打开密室"},
    ],
)
def test_reviewed_contract_cannot_be_reused_after_semantic_change(changes):
    obligation, form, answers = reviewed_fixture()
    with pytest.raises(ValueError, match="contract"):
        make_plan(obligation.model_copy(update=changes), form, answers)


def test_legacy_fulfilled_answer_without_semantic_assessments_stays_unknown():
    obligation, form, answers = reviewed_fixture()
    answers.obligations[0] = answers.obligations[0].model_copy(
        update={"subject_matches": None, "condition_results": []}
    )
    assert make_plan(obligation, form, answers).resolved_obligation_ids == []


# Actual PostgreSQL Canon transaction, with only the external semantic response
# supplied as a fixture. The compiler, candidate, preparation and history run.
from tests import test_canon_atomic_transaction as canon_fixtures

prepared_canon = canon_fixtures.prepared_canon


def _prepare_reviewed_canon(
    prepared, *, deadline=1, empty=False, status="active", priority="P1"
):
    from forwin.canon.preparation import CanonPreparationService
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.narrative_obligations.resolution_evidence import (
        build_resolution_plan,
        context_obligations,
    )

    with prepared.Session.begin() as session:
        row = session.get(NarrativeObligationRow, prepared.obligation_id)
        row.status = status
        row.priority = priority
        row.origin_chapter_number = 1 if status == "planned" else 0
        row.deadline_chapter = deadline
        row.subject_refs_json = '["Shen Linchuan"]'
        row.payoff_test = "Shen Linchuan enters the archive"
        row.resolution_conditions_json = '["Entry actually occurs in this chapter"]'
        session.flush()
        candidate = session.get(CandidateDraftRecord, prepared.candidate_id)
        draft = session.get(ChapterDraft, candidate.candidate_draft_id)
        obligations = context_obligations(
            session, prepared.project_id, 1, draft_id=draft.id, for_admission=True
        )
        form = build_form(
            project_id=prepared.project_id,
            chapter_number=1,
            chapter_text=draft.body_text,
            obligations=obligations,
        )
        assessment = {
            "value": "fulfilled",
            "confidence": 0.95,
            "evidence_quote": draft.body_text,
            "subject_of_quote": "Shen Linchuan",
            "explanation": "The narrative establishes his actual entry.",
        }
        answers = ChapterReviewAnswers.model_validate(
            {
                "project_id": prepared.project_id,
                "chapter_number": 1,
                "form_schema_version": form.form_schema_version,
                "reviewed_body_sha256": form.reviewed_body_sha256,
                "obligations": [
                    {
                        "id": row.id,
                        "addressed": assessment,
                        "payoff_evidence": assessment,
                        "subject_matches": {**assessment, "value": "true"},
                        "condition_results": [
                            {"condition": condition, "assessment": assessment}
                            for condition in [
                                row.payoff_test,
                                "Entry actually occurs in this chapter",
                            ]
                        ],
                    }
                ],
            }
        )
        resolution = build_resolution_plan(
            obligations=obligations,
            form=None if empty else form,
            answers=None if empty else answers,
            project_id=prepared.project_id,
            chapter_number=1,
            candidate_id=candidate.id,
            draft_id=draft.id,
            chapter_body=draft.body_text,
        )
        plan = prepared.plan
        result = CanonPreparationService().prepare_from_approved(
            session=session,
            candidate_id=candidate.id,
            approved_book_state_changes=plan.approved_book_state_changes,
            entity_admission_plan=plan.entity_admission_plan,
            acceptance_mode="normal",
            repair_attempt_count=0,
            residual_review_issues=[],
            canon_risk_level="low",
            obligation_resolution_plan=resolution,
        )
        return result.plan


@pytest.mark.parametrize("deadline", [1, 4])
def test_canon_commits_due_or_early_reviewed_payoff_with_acceptance_and_audit(
    prepared_canon, deadline
):
    import json

    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.audit import DecisionEvent
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.models.project import ChapterPlan

    plan = _prepare_reviewed_canon(prepared_canon, deadline=deadline)
    service = CanonAdmissionService(session_factory=prepared_canon.Session)
    result = service.commit_plan(plan)
    assert not result.blocked, result.failure_reason
    with prepared_canon.Session() as session:
        row = session.get(NarrativeObligationRow, prepared_canon.obligation_id)
        assert row.status == "resolved"
        assert (
            session.get(ChapterPlan, prepared_canon.chapter_plan_id).active_commit_id
            == result.commit_id
        )
        metadata = json.loads(row.metadata_json)
        evidence = metadata["verifier_result"]["evidence"]
        assert evidence["candidate_id"] == prepared_canon.candidate_id
        assert evidence["draft_id"] == plan.obligation_resolution_plan.draft_id
        event = session.get(
            DecisionEvent, metadata["_canon_obligation_provenance"]["head_event_id"]
        )
        assert (
            json.loads(event.payload_json)["effect_acceptance_id"] == result.commit_id
        )
    assert service.commit_plan(plan).idempotent


def test_obligation_resolution_rolls_back_with_canon_after_resolution_injection(
    prepared_canon,
):
    from sqlalchemy import select

    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.models.project import ChapterPlan

    plan = _prepare_reviewed_canon(prepared_canon)

    def fail(stage):
        if stage == "obligation":
            raise RuntimeError("rollback reviewed resolution")

    result = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        plan, failure_injector=fail
    )
    assert result.blocked
    with prepared_canon.Session() as session:
        assert (
            session.get(NarrativeObligationRow, prepared_canon.obligation_id).status
            == "active"
        )
        assert (
            session.get(ChapterPlan, prepared_canon.chapter_plan_id).active_commit_id
            is None
        )
        assert session.scalar(select(CanonCommitRecord.id)) is None


@pytest.mark.parametrize(
    "change", ["condition", "subject", "deadline", "draft", "review_identity"]
)
def test_canon_rechecks_contract_candidate_and_review_under_lock(
    prepared_canon, change
):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft
    from forwin.models.narrative_obligation import NarrativeObligationRow

    plan = _prepare_reviewed_canon(prepared_canon)
    with prepared_canon.Session.begin() as session:
        row = session.get(NarrativeObligationRow, prepared_canon.obligation_id)
        if change == "condition":
            row.resolution_conditions_json = '["He must bring back a document"]'
        elif change == "subject":
            row.subject_refs_json = '["Another archivist"]'
        elif change == "deadline":
            row.deadline_chapter = 9
        elif change == "draft":
            candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
            session.get(
                ChapterDraft, candidate.candidate_draft_id
            ).body_text = "An old candidate with a different action."
        else:
            plan = plan.model_copy(
                update={
                    "obligation_resolution_plan": plan.obligation_resolution_plan.model_copy(
                        update={"review_identity": "forged"}
                    )
                }
            )
    result = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        plan
    )
    assert result.stale
    with prepared_canon.Session() as session:
        assert (
            session.get(NarrativeObligationRow, prepared_canon.obligation_id).status
            == "active"
        )


@pytest.mark.parametrize("deadline,blocked", [(1, True), (4, False)])
def test_empty_evidence_has_no_deadline_exemption_but_non_due_unknown_can_commit(
    prepared_canon, deadline, blocked
):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.narrative_obligation import NarrativeObligationRow

    plan = _prepare_reviewed_canon(prepared_canon, deadline=deadline, empty=True)
    result = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        plan
    )
    assert result.blocked is blocked
    with prepared_canon.Session() as session:
        assert (
            session.get(NarrativeObligationRow, prepared_canon.obligation_id).status
            == "active"
        )


def test_old_review_with_shared_payoff_quote_cannot_be_rebound_to_changed_body():
    obligation, form, answers = reviewed_fixture()
    with pytest.raises(ValueError, match="body"):
        make_plan(
            obligation, form, answers, BODY + "但新的段落揭示那封遗书其实是伪造的。"
        )


def test_original_validation_rejection_cannot_be_lost_when_freezing_review():
    from forwin.canon_quality.chapter_review_form.evidence_validator import (
        RejectedAnswer,
        ValidationReport,
    )
    from forwin.narrative_obligations.resolution_evidence import build_resolution_plan

    obligation, form, answers = reviewed_fixture()
    report = ValidationReport(
        rejected=[
            RejectedAnswer(path="obligations[0].addressed", reason="subject_unverified")
        ]
    )
    plan = build_resolution_plan(
        obligations=[obligation],
        form=form,
        answers=answers,
        project_id="book",
        chapter_number=11,
        draft_id="draft-11",
        candidate_id="candidate-11",
        chapter_body=BODY,
        validation_report=report,
    )
    assert plan.resolved_obligation_ids == []


def test_pulp_nonfatal_due_unknown_keeps_existing_policy_semantics(prepared_canon):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.project import Project
    from forwin.runtime.policy import RuntimePolicy

    with prepared_canon.Session.begin() as session:
        session.get(
            Project, prepared_canon.project_id
        ).runtime_policy_json = RuntimePolicy.for_profile("pulp").model_dump_json()
    plan = _prepare_reviewed_canon(prepared_canon, deadline=1, empty=True)
    result = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        plan
    )
    assert not result.blocked, result.failure_reason


def test_prepare_chain_rejects_cached_answers_from_old_body_with_shared_quote(
    prepared_canon, monkeypatch
):
    from types import SimpleNamespace

    from forwin.candidate_drafts import (
        CandidateDraftRepository,
        candidate_plan_revision,
    )
    from forwin.canon import quality_preparation
    from forwin.canon.preparation import (
        CanonPreparationRequest,
        CanonPreparationService,
    )
    from forwin.canon_quality.chapter_review_form.form_schema import ChapterReviewForm
    from forwin.canon_quality.types import CanonQualityAnalysisResult
    from forwin.models.draft import ChapterDraft, ChapterReview
    from forwin.models.project import ChapterPlan
    from forwin.protocol.review import ReviewVerdict
    from forwin.protocol.writer import WriterOutput
    from forwin.runtime.policy import RuntimePolicy
    from forwin.state.updater import StateUpdater

    old = _prepare_reviewed_canon(prepared_canon)
    old_receipt = old.obligation_resolution_plan
    old_form = ChapterReviewForm.model_validate_json(old_receipt.form_json)
    old_answers = ChapterReviewAnswers.model_validate_json(old_receipt.answers_json)
    old_analysis = CanonQualityAnalysisResult(
        project_id=old.project_id, chapter_number=1, form=old_form, answers=old_answers
    )
    monkeypatch.setattr(
        quality_preparation, "analyze_writer_output_quality", lambda **_: old_analysis
    )
    monkeypatch.setattr(
        quality_preparation, "prepare_deferred_acceptance", lambda **_: []
    )
    with prepared_canon.Session.begin() as session:
        chapter = session.get(ChapterPlan, prepared_canon.chapter_plan_id)
        body = "Shen Linchuan enters the archive. The preceding scene was only an imagined plan."
        output = WriterOutput(
            project_id=old.project_id,
            chapter_number=1,
            title="Chapter one",
            body=body,
            end_of_chapter_summary="A revised candidate",
        )
        draft = ChapterDraft(
            chapter_plan_id=chapter.id, version=2, body_text=body, summary="Revised"
        )
        session.add(draft)
        session.flush()
        review = ChapterReview(draft_id=draft.id, verdict="pass")
        session.add(review)
        session.flush()
        candidate = CandidateDraftRepository(session).create_reviewed_version(
            project_id=old.project_id,
            chapter_plan=chapter,
            draft=draft,
            review=review,
            writer_output=output,
            plan_revision=candidate_plan_revision(chapter),
            policy_version=1,
        )
        result = CanonPreparationService().prepare(
            session=session,
            updater=StateUpdater(session),
            policy=RuntimePolicy.for_profile("standard"),
            llm_client=None,
            artifact_store=None,
            recorder=SimpleNamespace(record_event=lambda **_: None),
            request=CanonPreparationRequest(
                candidate_id=candidate.id,
                project_id=old.project_id,
                chapter_number=1,
                writer_output=output,
                verdict=ReviewVerdict(verdict="pass", issues=[]),
                acceptance_mode="normal",
                repair_attempt_count=0,
                residual_review_issues=[],
                canon_risk_level="low",
            ),
        )
        assert result.blocked
        assert "body identity" in result.blocked_path


def test_quality_preparer_uses_selected_candidate_draft_even_with_newer_unaccepted_draft(
    prepared_canon, monkeypatch
):
    from types import SimpleNamespace

    from forwin.canon import quality_preparation
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft
    from forwin.protocol.review import ReviewVerdict
    from forwin.protocol.writer import WriterOutput
    from forwin.runtime.policy import RuntimePolicy
    from forwin.state.updater import StateUpdater

    captured = {}

    def analyze(**kwargs):
        captured["draft_id"] = kwargs["draft_id"]
        raise RuntimeError("selected draft captured")

    monkeypatch.setattr(quality_preparation, "analyze_writer_output_quality", analyze)
    with prepared_canon.Session.begin() as session:
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        selected = session.get(ChapterDraft, candidate.candidate_draft_id)
        session.add(
            ChapterDraft(
                chapter_plan_id=prepared_canon.chapter_plan_id,
                version=99,
                body_text="A later unaccepted candidate",
                summary="Later",
            )
        )
        session.flush()
        with pytest.raises(RuntimeError, match="selected draft captured"):
            quality_preparation.CanonQualityPreparer().evaluate(
                session=session,
                updater=StateUpdater(session),
                project_id=prepared_canon.project_id,
                chapter_number=1,
                candidate_id=candidate.id,
                writer_output=WriterOutput(
                    project_id=prepared_canon.project_id,
                    chapter_number=1,
                    title="Chapter one",
                    body=selected.body_text,
                    end_of_chapter_summary=selected.summary,
                ),
                verdict=ReviewVerdict(verdict="pass", issues=[]),
                policy=RuntimePolicy.for_profile("standard"),
                llm_client=None,
                artifact_store=None,
                recorder=SimpleNamespace(record_event=lambda **_: None),
            )
        assert captured["draft_id"] == selected.id


def test_review_cannot_resolve_planned_obligation_from_another_draft():
    obligation, form, answers = reviewed_fixture()
    obligation = obligation.model_copy(
        update={"status": "planned", "origin_draft_id": "different-draft"}
    )
    form = build_form(
        project_id="book",
        chapter_number=11,
        chapter_text=BODY,
        obligations=[obligation.model_copy(update={"must_resolve_now": True})],
    )
    with pytest.raises(ValueError, match="draft"):
        make_plan(obligation, form, answers)


def test_optional_future_obligations_can_be_pruned_without_making_them_mandatory():
    obligation, _, _ = reviewed_fixture()
    future = [obligation.model_copy(update={"id": f"future-{i}"}) for i in range(30)]
    form = build_form(
        project_id="book",
        chapter_number=11,
        chapter_text=BODY,
        obligations=future,
        token_budget_chars=3000,
    )
    assert len(form.obligations) < 30
    assert all(not ask.must_resolve_now for ask in form.obligations)


def test_current_draft_obligation_activation_and_fulfillment_share_canon_transaction(
    prepared_canon,
):
    import json

    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.narrative_obligation import NarrativeObligationRow

    plan = _prepare_reviewed_canon(prepared_canon, status="planned")
    assert plan.obligation_resolution_plan.resolved_obligation_ids == [
        prepared_canon.obligation_id
    ]
    outcome = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        plan
    )
    assert not outcome.blocked, outcome.failure_reason
    with prepared_canon.Session() as session:
        row = session.get(NarrativeObligationRow, prepared_canon.obligation_id)
        assert row.status == "resolved"
        assert (
            json.loads(row.metadata_json)["_canon_obligation_provenance"][
                "origin_acceptance_id"
            ]
            == outcome.commit_id
        )


def test_pulp_mandatory_due_unknown_cannot_obtain_payoff_exemption(prepared_canon):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.project import Project
    from forwin.runtime.policy import RuntimePolicy

    with prepared_canon.Session.begin() as session:
        session.get(
            Project, prepared_canon.project_id
        ).runtime_policy_json = RuntimePolicy.for_profile("pulp").model_dump_json()
    plan = _prepare_reviewed_canon(
        prepared_canon, deadline=1, empty=True, priority="P0"
    )
    result = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        plan
    )
    assert result.blocked
    assert "obligation_due_unresolved" in result.failure_reason


def test_canon_does_not_backfill_or_reset_legacy_resolved_evidence(prepared_canon):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.narrative_obligation import NarrativeObligationRow

    with prepared_canon.Session.begin() as session:
        legacy = NarrativeObligationRow(
            project_id=prepared_canon.project_id,
            origin_chapter_number=0,
            status="resolved",
            resolution_chapter=0,
            metadata_json='{"verifier_result":{"status":"pass"}}',
            resolution_evidence_refs_json='["legacy:keyword"]',
        )
        session.add(legacy)
        session.flush()
        legacy_id = legacy.id
    plan = _prepare_reviewed_canon(prepared_canon, deadline=4, empty=True)
    result = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        plan
    )
    assert not result.blocked, result.failure_reason
    with prepared_canon.Session() as session:
        legacy = session.get(NarrativeObligationRow, legacy_id)
        assert legacy.status == "resolved"
        assert legacy.metadata_json == '{"verifier_result":{"status":"pass"}}'
        assert legacy.resolution_evidence_refs_json == '["legacy:keyword"]'


def test_ambiguous_repeated_quote_needs_more_context_before_it_can_resolve():
    repeated_body = BODY + "此前那段只是梦境。" + BODY
    obligation, form, answers = reviewed_fixture(body=repeated_body, quote=BODY)
    assert (
        make_plan(obligation, form, answers, repeated_body).resolved_obligation_ids
        == []
    )


@pytest.mark.parametrize(
    "deadline,priority,judgment,blocked,extra_debts",
    [
        (1, "P0", "fulfilled", False, 0),
        (4, "P0", "fulfilled", False, 0),
        (1, "P0", "unknown", True, 0),
        (4, "P0", "unknown", False, 0),
        (1, "P1", "unknown", False, 0),
        (1, "P0", "fulfilled", False, 25),
    ],
)
def test_real_pulp_preparer_reviews_applicable_payoff_with_existing_form(
    prepared_canon,
    tmp_path,
    deadline,
    priority,
    judgment,
    blocked,
    extra_debts,
):
    import json

    from forwin.canon.admission import CanonAdmissionService
    from forwin.canon.preparation import CanonPreparationService
    from forwin.canon.quality_preparation import CanonQualityPreparer
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.models.project import Project
    from forwin.narrative_obligations.repository import NarrativeObligationRepository
    from forwin.narrative_obligations.types import NarrativePlanPatch
    from forwin.observability.pipeline_trace import (
        PipelineAuditContext,
        PipelineTraceRecorder,
    )
    from forwin.protocol.review import ReviewVerdict
    from forwin.protocol.writer import WriterOutput
    from forwin.runtime.policy import RuntimePolicy
    from forwin.state.updater import StateUpdater
    from forwin.storage import ArtifactStore

    class FormClient:
        calls = 0

        def complete_json(self, *, messages, **_kwargs):
            self.calls += 1
            content = messages[1]["content"]
            payload = json.loads(content[content.index("{") :])
            form = payload["form"]
            body = payload["chapter_body"]
            if extra_debts:
                assert len(form["obligations"]) == 1, (
                    "Optional P1 debt displaced required P0 evidence budget"
                )
                assert form["obligations"][0]["id"] == prepared_canon.obligation_id
                assert form["obligations"][0]["must_resolve_now"]
            assessment = {
                "value": judgment,
                "confidence": 0.95 if judgment == "fulfilled" else 0.3,
                "evidence_quote": body if judgment == "fulfilled" else "",
                "subject_of_quote": "Shen Linchuan",
                "explanation": "The narrative establishes his actual entry."
                if judgment == "fulfilled"
                else "Cannot establish the payoff.",
            }
            return {
                **{
                    key: form[key]
                    for key in ("project_id", "chapter_number", "form_schema_version")
                },
                "obligations": [
                    {
                        "id": ask["id"],
                        "addressed": assessment,
                        "payoff_evidence": assessment,
                        "subject_matches": {
                            **assessment,
                            "value": "true" if judgment == "fulfilled" else "unknown",
                        },
                        "condition_results": [
                            {"condition": condition, "assessment": assessment}
                            for condition in [
                                ask["payoff_test"],
                                *ask["resolution_conditions"],
                            ]
                        ],
                    }
                    for ask in form["obligations"]
                ],
            }

    client = FormClient()
    store = ArtifactStore(str(tmp_path))
    recorder = PipelineTraceRecorder(
        audit=PipelineAuditContext(), artifact_store=store, observability=None
    )
    policy = RuntimePolicy.for_profile("pulp")
    with prepared_canon.Session.begin() as session:
        session.get(
            Project, prepared_canon.project_id
        ).runtime_policy_json = policy.model_dump_json()
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        draft = session.get(ChapterDraft, candidate.candidate_draft_id)
        obligation = session.get(NarrativeObligationRow, prepared_canon.obligation_id)
        obligation.status = "active"
        obligation.origin_chapter_number = 0
        obligation.priority = priority
        obligation.deadline_chapter = deadline
        obligation.subject_refs_json = '["Shen Linchuan"]'
        obligation.payoff_test = "Shen Linchuan enters the archive"
        obligation.resolution_conditions_json = (
            '["Entry actually occurs in this chapter"]'
        )
        patch = NarrativeObligationRepository(session).create_plan_patch(
            NarrativePlanPatch(
                project_id=prepared_canon.project_id,
                source_obligation_ids=[obligation.id],
                validation_status="passed",
                applied=True,
            )
        )
        obligation.linked_plan_patch_ids_json = json.dumps([patch.id])
        for i in range(extra_debts):
            session.add(
                NarrativeObligationRow(
                    project_id=prepared_canon.project_id,
                    origin_chapter_number=0,
                    priority="P1",
                    status="active",
                    deadline_chapter=1,
                    obligation_type="custom_reader_promise",
                    summary=f"Optional debt {i}: " + "long promise " * 1000,
                    payoff_test="Pay off the optional promise",
                )
            )
        session.flush()
        result = CanonQualityPreparer().evaluate(
            session=session,
            updater=StateUpdater(session),
            policy=policy,
            llm_client=client,
            artifact_store=store,
            recorder=recorder,
            project_id=prepared_canon.project_id,
            chapter_number=1,
            candidate_id=candidate.id,
            writer_output=WriterOutput(
                project_id=prepared_canon.project_id,
                chapter_number=1,
                title="Chapter one",
                body=draft.body_text,
                end_of_chapter_summary=draft.summary,
            ),
            verdict=ReviewVerdict(verdict="pass"),
            policy_version=1,
        )
        assert result.blocked is blocked, result.gate_result.blocking_reasons
        if blocked:
            assert (
                f"obligation_due_unresolved:{obligation.id}"
                in result.gate_result.blocking_reasons
            )
        else:
            receipt = result.obligation_resolution_plan
            assert receipt.form_json and receipt.answers_json
            assert receipt.resolved_obligation_ids == (
                [obligation.id] if judgment == "fulfilled" else []
            )
            original = prepared_canon.plan
            canon = CanonPreparationService().prepare_from_approved(
                session=session,
                candidate_id=candidate.id,
                approved_book_state_changes=original.approved_book_state_changes,
                entity_admission_plan=original.entity_admission_plan,
                acceptance_mode="normal",
                repair_attempt_count=0,
                residual_review_issues=[],
                canon_risk_level="low",
                quality_admission_run_id=result.quality_admission_run_id,
                obligation_resolution_plan=receipt,
            )
        assert client.calls == 1
    if not blocked:
        committed = CanonAdmissionService(
            session_factory=prepared_canon.Session
        ).commit_plan(canon.plan)
        assert not committed.blocked, committed.failure_reason
        with prepared_canon.Session() as session:
            row = session.get(NarrativeObligationRow, prepared_canon.obligation_id)
            assert row.status == ("resolved" if judgment == "fulfilled" else "active")


def test_real_pulp_preparer_without_obligations_keeps_model_unused(
    prepared_canon, tmp_path
):
    from forwin.canon.quality_preparation import CanonQualityPreparer
    from forwin.models.draft import CandidateDraftRecord, ChapterDraft
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.observability.pipeline_trace import (
        PipelineAuditContext,
        PipelineTraceRecorder,
    )
    from forwin.protocol.review import ReviewVerdict
    from forwin.protocol.writer import WriterOutput
    from forwin.runtime.policy import RuntimePolicy
    from forwin.state.updater import StateUpdater
    from forwin.storage import ArtifactStore

    class ForbiddenClient:
        def __getattr__(self, name):
            pytest.fail(f"No-obligation pulp path accessed the model: {name}")

    store = ArtifactStore(str(tmp_path))
    recorder = PipelineTraceRecorder(
        audit=PipelineAuditContext(), artifact_store=store, observability=None
    )
    with prepared_canon.Session.begin() as session:
        session.delete(
            session.get(NarrativeObligationRow, prepared_canon.obligation_id)
        )
        session.flush()
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        draft = session.get(ChapterDraft, candidate.candidate_draft_id)
        result = CanonQualityPreparer().evaluate(
            session=session,
            updater=StateUpdater(session),
            policy=RuntimePolicy.for_profile("pulp"),
            llm_client=ForbiddenClient(),
            artifact_store=store,
            recorder=recorder,
            project_id=prepared_canon.project_id,
            chapter_number=1,
            candidate_id=candidate.id,
            writer_output=WriterOutput(
                project_id=prepared_canon.project_id,
                chapter_number=1,
                title="Chapter one",
                body=draft.body_text,
                end_of_chapter_summary=draft.summary,
            ),
            verdict=ReviewVerdict(verdict="pass"),
        )
        assert not result.blocked
        assert result.obligation_resolution_plan.form_json == ""
        assert result.obligation_resolution_plan.evidence == ()


@pytest.mark.parametrize("status", ["expired", "blocked"])
def test_locked_admission_rejects_newly_expired_or_blocked_debt(prepared_canon, status):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.models.project import ChapterPlan

    plan = _prepare_reviewed_canon(prepared_canon)
    with prepared_canon.Session.begin() as session:
        session.get(
            NarrativeObligationRow, prepared_canon.obligation_id
        ).status = status
    result = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        plan
    )
    assert result.blocked
    assert status in result.failure_reason
    with prepared_canon.Session() as session:
        assert (
            session.get(NarrativeObligationRow, prepared_canon.obligation_id).status
            == status
        )
        assert (
            session.get(ChapterPlan, prepared_canon.chapter_plan_id).active_commit_id
            is None
        )


@pytest.mark.parametrize("status", ["expired", "blocked"])
def test_old_fulfillment_answers_do_not_resolve_stopped_debt(status):
    obligation, form, answers = reviewed_fixture()
    plan = make_plan(obligation.model_copy(update={"status": status}), form, answers)
    assert plan.resolved_obligation_ids == []


@pytest.mark.parametrize("status", ["expired", "blocked"])
def test_pulp_optional_stopped_debt_remains_stopped_without_blocking_fresh_admission(
    prepared_canon, status
):
    from forwin.canon.admission import CanonAdmissionService
    from forwin.models.project import Project
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.runtime.policy import RuntimePolicy

    with prepared_canon.Session.begin() as session:
        session.get(
            Project, prepared_canon.project_id
        ).runtime_policy_json = RuntimePolicy.for_profile("pulp").model_dump_json()
    plan = _prepare_reviewed_canon(
        prepared_canon, status=status, priority="P1", empty=True
    )
    result = CanonAdmissionService(session_factory=prepared_canon.Session).commit_plan(
        plan
    )
    assert not result.blocked, result.failure_reason
    with prepared_canon.Session() as session:
        assert (
            session.get(NarrativeObligationRow, prepared_canon.obligation_id).status
            == status
        )
