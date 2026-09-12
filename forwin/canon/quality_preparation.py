from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.canon.types import CanonQualityGateOutcome
from forwin.canon_quality.chapter_review_form.pruning import select_obligations_to_ask
from forwin.canon_quality.continuity_adapter import signals_from_continuity_issues
from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.canon_quality.signals import CanonAdmissionGateResult, dedupe_signals
from forwin.model_adapter import ModelAdapter
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.resolution_evidence import build_resolution_plan
from forwin.observability.llm_trace import safe_prompt_trace_attempts
from forwin.observability.pipeline_trace import PipelineTraceRecorder
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.issue_groups import issue_group_for_issue
from forwin.review.queries import latest_draft_and_review_for_chapter
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore

from .deferred_acceptance import prepare_deferred_acceptance

logger = logging.getLogger(__name__)


def _canon_quality_gate_outcome(
    gate_result: CanonAdmissionGateResult,
    *,
    candidate_id: str,
    policy_version: int,
    signal_types: list[str],
    evidence_refs: list[str],
    trace_ids: list[str] | None = None,
) -> GateOutcome:
    issue_keys = list(
        dict.fromkeys(
            str(item)
            for item in [*signal_types, *gate_result.blocking_reasons]
            if str(item)
        )
    )
    decision = (
        "block"
        if not gate_result.commit_allowed
        else "warn"
        if gate_result.verdict == "warn" or issue_keys
        else "pass"
    )
    return GateOutcome(
        gate_id="canon_quality",
        responsibility_domain="canon_admission",
        scope="chapter",
        candidate_id=candidate_id,
        chapter_number=int(gate_result.chapter_number or 0),
        policy_version=policy_version,
        fired=bool(issue_keys or gate_result.verdict != "pass"),
        decision=decision,
        blocked=not gate_result.commit_allowed,
        issue_keys=issue_keys,
        issue_groups=list(
            dict.fromkeys(
                issue_group_for_issue(issue_type=issue_key) for issue_key in issue_keys
            )
        ),
        evidence_refs=list(
            dict.fromkeys(str(ref) for ref in evidence_refs if str(ref))
        ),
        trace_ids=list(
            dict.fromkeys(str(value) for value in trace_ids or [] if str(value))
        ),
    )


def persist_canon_quality_attempt_trace(
    *,
    llm_client: ModelAdapter,
    recorder: PipelineTraceRecorder,
    session: Session,
    updater: StateUpdater,
    project_id: str,
    chapter_number: int,
    candidate_id: str,
    error: BaseException | None = None,
) -> str:
    drain = getattr(llm_client, "drain_llm_attempt_events", None)
    events = drain() if callable(drain) else []
    attempts = (
        [dict(item) for item in events if isinstance(item, dict)]
        if isinstance(events, list)
        else []
    )
    save_prompt_trace = recorder.save_prompt_trace
    if not attempts or not callable(save_prompt_trace):
        return ""
    safe_attempts = safe_prompt_trace_attempts(attempts, exc=error)
    try:
        return save_prompt_trace(
            session=session,
            updater=updater,
            project_id=project_id,
            prompt_trace={
                "trace_scope": "canon_quality",
                "stage_key": "chapter_review_form",
                "template_id": "canon_quality:chapter_review_form",
                "template_version": "v2",
                "effective_system_prompt": "",
                "prompt_layers": [],
                "input_snapshot": {
                    "project_id": project_id,
                    "chapter_number": chapter_number,
                    "candidate_id": candidate_id,
                    "gate_id": "canon_quality",
                },
                "attempts": safe_attempts,
                "output_summary": {
                    "status": "failed" if error is not None else "completed",
                    "chapter_number": chapter_number,
                    "candidate_id": candidate_id,
                    "gate_id": "canon_quality",
                    "error_class": error.__class__.__name__
                    if error is not None
                    else "",
                },
            },
        )
    except Exception:
        logger.warning(
            "Failed to persist canon-quality LLM attempt trace for chapter %d.",
            chapter_number,
            exc_info=True,
        )
        return ""


class CanonQualityPreparer:
    """Prepare quality evidence in the caller transaction, without Canon writes."""

    def evaluate(
        self,
        *,
        policy: RuntimePolicy,
        llm_client: ModelAdapter,
        artifact_store: ArtifactStore,
        recorder: PipelineTraceRecorder,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
        candidate_id: str = "",
        policy_version: int = 0,
    ) -> CanonQualityGateOutcome:
        if candidate_id:
            candidate = session.get(CandidateDraftRecord, candidate_id)
            if candidate is None or (
                candidate.project_id,
                candidate.chapter_number,
            ) != (project_id, chapter_number):
                raise ValueError("quality review candidate identity mismatch")
            latest_draft = session.get(ChapterDraft, candidate.candidate_draft_id)
            latest_review = session.get(ChapterReview, candidate.review_id)
            if (
                latest_draft is None
                or latest_review is None
                or latest_review.draft_id != latest_draft.id
                or latest_draft.body_text != writer_output.body
            ):
                raise ValueError(
                    "quality review candidate draft/body identity mismatch"
                )
        else:
            latest_draft, latest_review = latest_draft_and_review_for_chapter(
                session=session, project_id=project_id, chapter_number=chapter_number
            )
        draft_id = str(getattr(latest_draft, "id", "") or "")
        review_id = str(getattr(latest_review, "id", "") or "")
        gate_mode = policy.canon.quality_gate
        deterministic_gate_mode = gate_mode == "pulp_fatal"
        needs_obligation_form = False
        if deterministic_gate_mode:
            obligation_repo = NarrativeObligationRepository(session)
            # Pulp has no other semantic reviewer. Use the existing form only
            # when its selection rules include an applicable obligation; keep
            # the no-obligation path deterministic and preserve its cache.
            review_obligations = [
                *obligation_repo.list_active_for_context(
                    project_id, chapter_number=chapter_number
                ),
                *[
                    item
                    for item in obligation_repo.list_planned_for_chapter(
                        project_id, origin_chapter_number=chapter_number
                    )
                    if draft_id and item.origin_draft_id == draft_id
                ],
            ]
            needs_obligation_form = bool(
                select_obligations_to_ask(
                    obligations=review_obligations, chapter_number=chapter_number
                )
            )
        use_form = not deterministic_gate_mode or needs_obligation_form
        gate_llm_client = llm_client if use_form else None
        analysis_mode = "primary" if use_form else "off"
        gate_trace_id = ""
        analysis_error: BaseException | None = None
        try:
            analysis = analyze_writer_output_quality(
                session=session,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                draft_id=draft_id,
                persist=True,
                mode=analysis_mode,
                llm_client=gate_llm_client,
                return_raw_analyzer_results=True,
                obligation_gate_mode=gate_mode,
            )
        except BaseException as exc:
            analysis_error = exc
            raise
        finally:
            if gate_llm_client is not None:
                gate_trace_id = persist_canon_quality_attempt_trace(
                    llm_client=llm_client,
                    recorder=recorder,
                    session=session,
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    candidate_id=candidate_id,
                    error=analysis_error,
                )
        continuity_signals = signals_from_continuity_issues(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            issues=list(getattr(verdict, "issues", []) or []),
        )
        gate_signals = dedupe_signals([*analysis.signals, *continuity_signals])
        if continuity_signals:
            CanonQualityRepository(session).save_signals(continuity_signals)
        project = session.get(Project, project_id)
        target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
        deferred_acceptance_errors = prepare_deferred_acceptance(
            policy=policy,
            recorder=recorder,
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            review_id=review_id,
            verdict=verdict,
            signals=gate_signals,
            target_total_chapters=target_total_chapters,
        )
        if deferred_acceptance_errors:
            recorder.record_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
                scope="chapter",
                summary=f"第{chapter_number}章 deferred acceptance 计划补丁失败。",
                reason=";".join(deferred_acceptance_errors),
                payload=attach_gate_outcome(
                    {"deferred_acceptance_errors": deferred_acceptance_errors},
                    GateOutcome(
                        gate_id="canon_quality",
                        responsibility_domain="canon_admission",
                        scope="chapter",
                        candidate_id=candidate_id,
                        chapter_number=chapter_number,
                        policy_version=policy_version,
                        fired=True,
                        decision="error",
                        blocked=True,
                        issue_keys=list(deferred_acceptance_errors),
                        issue_groups=["fact_conflict"],
                        trace_ids=[gate_trace_id] if gate_trace_id else [],
                    ),
                ),
            )
            return CanonQualityGateOutcome(blocked_path="deferred-acceptance-blocked")
        obligation_repo = NarrativeObligationRepository(session)
        gate_obligations = [
            *obligation_repo.list_active_for_context(
                project_id, chapter_number=chapter_number
            ),
            *[
                item
                for item in obligation_repo.list_planned_for_chapter(
                    project_id, origin_chapter_number=chapter_number
                )
                if draft_id and item.origin_draft_id == draft_id
            ],
        ]
        resolution_plan = build_resolution_plan(
            obligations=gate_obligations,
            form=getattr(analysis, "form", None),
            answers=getattr(analysis, "answers", None),
            validation_report=getattr(analysis, "validation_report", None),
            project_id=project_id,
            chapter_number=chapter_number,
            candidate_id=candidate_id,
            draft_id=draft_id,
            chapter_body=str(getattr(writer_output, "body", "") or ""),
        )
        patch_ids = sorted(
            {
                patch_id
                for obligation in gate_obligations
                for patch_id in obligation.linked_plan_patch_ids
                if patch_id
            }
        )
        gate_analyzer_results = [
            item for item in analysis.raw_analyzer_results if isinstance(item, dict)
        ]
        gate_result = evaluate_canon_admission(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            review_id=review_id,
            review_verdict=verdict.verdict,
            signals=gate_signals,
            obligations=gate_obligations,
            plan_patches=obligation_repo.list_patches_by_ids(patch_ids),
            mode=gate_mode,
            is_final_chapter=bool(
                target_total_chapters and chapter_number >= target_total_chapters
            ),
            analyzer_results=gate_analyzer_results,
            min_blocking_confidence=0.8,
            require_evidence_for_block=True,
            resolved_obligation_ids=resolution_plan.resolved_obligation_ids,
        )
        admission_run = CanonQualityRepository(session).save_admission_run(
            gate_result, signals=gate_signals
        )
        gate_outcome = _canon_quality_gate_outcome(
            gate_result,
            candidate_id=candidate_id,
            policy_version=policy_version,
            signal_types=[signal.signal_type for signal in gate_signals],
            evidence_refs=[
                ref for signal in gate_signals for ref in signal.evidence_refs
            ],
            trace_ids=[gate_trace_id] if gate_trace_id else [],
        )
        recorder.record_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
            scope="chapter",
            summary=f"第{chapter_number}章 canon quality gate: {gate_result.verdict}",
            related_object_type="canon_admission_run",
            payload=attach_gate_outcome(
                gate_result.model_dump(mode="json"),
                gate_outcome,
            ),
        )
        if gate_result.commit_allowed:
            return CanonQualityGateOutcome(
                gate_result=gate_result, quality_admission_run_id=admission_run.id,
                obligation_resolution_plan=resolution_plan
            )
        frozen_path = ""
        if policy.canon.hard_floor:
            frozen_path = artifact_store.save_frozen_candidate(
                project_id=project_id,
                chapter_number=chapter_number,
                payload={
                    "reason": "canon-quality-gate-blocked",
                    "chapter_number": chapter_number,
                    "writer_output": writer_output.model_dump(mode="json"),
                    "review_verdict": verdict.model_dump(mode="json"),
                    "canon_quality_gate": gate_result.model_dump(mode="json"),
                    "canon_quality_signals": [
                        signal.model_dump(mode="json") for signal in analysis.signals
                    ],
                    "chapter_review_form_results": gate_analyzer_results,
                },
            )
        recorder.record_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
            scope="chapter",
            summary=f"第{chapter_number}章 canon quality gate 阻止 canon 写入。",
            reason=gate_result.gate_summary,
            payload=attach_gate_outcome(
                gate_result.model_dump(mode="json"),
                gate_outcome,
            ),
        )
        return CanonQualityGateOutcome(
            blocked_path=frozen_path or "canon-quality-gate-blocked",
            gate_result=gate_result,
        )
