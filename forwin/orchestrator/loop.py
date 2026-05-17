"""Writing orchestrator – the Phase 0.5 closed-loop pipeline.

Flow per run:
  1. Create project
  2. Plan arc (1 LLM call)
  3. Seed DB with initial state from arc plan
  4. For each chapter:
     a. Assemble context
     b. Write chapter (1 LLM call)
     c. Continuity check (rule-based)
     d. Save draft + review
     e. Update canon state
"""
from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.candidate_drafts import CandidateDraftRepository
from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.placeholder import extract_expected_protagonist_names
from forwin.canon_quality.repository import CanonQualityRepository
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.canon_quality.temporal_semantics import LLMTemporalReconciler
from forwin.canon_names import is_plausible_person_name
from forwin.checker.rules import ContinuityChecker
from forwin.config import Config
from forwin.context.assembler import _build_canon_quality_context
from forwin.governance import (
    BandCheckpointDetail,
    BandCheckpointIssueInfo,
    DecisionEventType,
    DecisionEventInfo,
    band_is_first_chapter,
    chapter_blocking_message,
    ensure_decision_event_type,
    issue_group_for_issue,
    new_project_governance,
    normalize_project_governance,
)
from forwin.governance_checks import (
    band_combined_text,
    evaluate_band_obligation_contract,
    evaluate_constraint_issues,
    evaluate_director_imbalance,
    evaluate_intra_band_consistency,
    evaluate_next_band_task_compatibility,
    evaluate_resource_closure_risk,
    evaluate_task_contract,
)
from forwin.models import BookGenesisRevision, ProvisionalBandExecution, ProvisionalChapterLedger, new_id
from forwin.models.governance import BandCheckpoint
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.phase import ArcStructureDraft, BandExperiencePlan, ChapterRewriteAttempt
from forwin.observability.context import OperationContext
from forwin.observability.payloads import attempt_group_ids, audit_payload, event_error_payload, safe_error_summary
from forwin.observability.ports import NullObservability
from forwin.observability.redaction import redact_payload
from forwin.observability.spans import SpanRecord, current_span
from forwin.book_state.extraction_contract import BookStateExtractionRequest
from forwin.book_state.review_gate_ext import BookStateDirectCommitService
from forwin.extractor.book_state_graph_delta import BookStateGraphDeltaExtractor
from forwin.generation.continue_workset import build_continue_generation_workset
from forwin.knowledge_system import KnowledgeProjectionRefresher
from forwin.planning.world_contracts import WorldContractRepository
from forwin.protocol.experience import ArcPayoffMap, BandDelightSchedule, ChapterExperiencePlan
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.orchestrator.phase3 import save_stage_analysis
from forwin.orchestrator.feedback_aggregator import run_feedback_aggregation_pass
from forwin.orchestrator.phase4 import (
    save_npc_intents,
    save_world_turn,
)
from forwin.planning.scenario_rehearsal_resolution import latest_blocking_scenario_rehearsal
from forwin.planning.future_plan_auditor import FuturePlanAuditor, FuturePlanAuditRun
from forwin.planning.band_plan_patcher import BandPlanPatcher
from forwin.planning.obligation_scope_router import BandScopeCandidate, ObligationScopeRouter
from forwin.orchestrator.phase24 import ProvisionalBandPreview
from forwin.retrieval import RetrievalBroker
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.transaction import DeferAcceptanceTransaction
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.reviewer.outcome import ReviewOutcomeRouter
from forwin.runtime.services import RuntimeServices
from forwin.state.repo import StateRepository
from forwin.state.schema import KNOWN_STATE_FIELDS
from forwin.state.updater import StateUpdater
from forwin.observability.llm_trace import (
    build_llm_decision_event_payloads,
    prepare_prompt_trace_payload,
)
from forwin.subworld_manager import SubWorldManager
from forwin.protocol.writer import WriterOutput
from forwin.world_v4_compat.compiler import WorldModelCompiler as WorldModelCompilerV4
from forwin.writer.chapter_writer import ChapterWriter
from forwin.world_model.compiler import WorldModelCompiler as LegacyWorldModelCompiler

logger = logging.getLogger(__name__)

RuntimeContainer: Any = None


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _priority_for_deferred_issue(issue_type: str) -> str:
    normalized = str(issue_type or "").strip()
    if normalized in {"style_repetition_pressure"}:
        return "P3"
    if normalized in {"foreshadowing_payoff", "transition_bridge_needed"}:
        return "P2"
    return "P1"


def _summary_for_deferred_issue(*, verdict: ReviewVerdict, issue_type: str, outcome_reason: str) -> str:
    for issue in verdict.issues:
        if str(getattr(issue, "issue_type", "") or getattr(issue, "rule_name", "") or "") == issue_type:
            return str(getattr(issue, "description", "") or outcome_reason or issue_type)
    return str(outcome_reason or issue_type)


def _payoff_test_for_deferred_issue(
    *,
    verdict: ReviewVerdict,
    issue_type: str,
    deadline_chapter: int,
    summary: str,
) -> str:
    for issue in verdict.issues:
        if str(getattr(issue, "issue_type", "") or getattr(issue, "rule_name", "") or "") != issue_type:
            continue
        suggested = str(getattr(issue, "suggested_fix", "") or "").strip()
        if suggested:
            return suggested
    return f"第{int(deadline_chapter or 0)}章前必须偿还：{summary}"


def _future_plan_audit_checkpoint_payload(
    result: FuturePlanAuditRun | None,
) -> dict[str, Any]:
    if result is None:
        return {
            "status": "not_run",
            "inspected_chapters": [],
            "issue_count": 0,
            "issue_types": [],
            "applied_plan_patch_ids": [],
            "blocking_reasons": [],
        }
    return {
        "run_id": result.id,
        "status": result.status,
        "inspected_chapters": list(result.inspected_chapters),
        "issue_count": len(result.issues),
        "issue_types": [issue.issue_type for issue in result.issues],
        "applied_plan_patch_ids": list(result.applied_plan_patch_ids),
        "blocking_reasons": list(result.blocking_reasons),
    }


@dataclass(slots=True)
class RunResult:
    """Summary for a single orchestrator run."""

    project_id: str
    requested_chapters: int
    completed_chapters: list[int] = field(default_factory=list)
    failed_chapters: list[int] = field(default_factory=list)
    paused_chapters: list[int] = field(default_factory=list)
    frozen_artifacts: list[str] = field(default_factory=list)
    cancelled: bool = False
    paused: bool = False

    @property
    def status(self) -> str:
        if self.paused:
            return "paused"
        if self.cancelled:
            return "cancelled"
        if self.paused_chapters:
            return "needs_review"
        if self.failed_chapters and not self.completed_chapters:
            return "failed"
        if self.failed_chapters:
            return "partial_failed"
        return "completed"


@dataclass(slots=True)
class ProvisionalGateSnapshot:
    """The latest persisted provisional execution used to gate canon writing."""

    id: str
    aggregate_verdict: str
    failure_count: int
    issue_count: int
    chapter_numbers: list[int]


class TransientLLMChapterFailure(RuntimeError):
    """Current chapter failed because the upstream LLM looked temporarily unavailable."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class WritingOrchestrator:
    """Orchestrates the full chapter-generation pipeline."""

    def __init__(
        self,
        config: Config | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
        should_pause: Callable[[], bool] | None = None,
        *,
        services: RuntimeServices | None = None,
    ) -> None:
        if services is None:
            container_cls = RuntimeContainer
            if container_cls is None:
                from forwin.runtime.container import RuntimeContainer as container_cls

            services = container_cls.from_config(config or Config.from_env()).services()
        self.services = services
        self.config = services.config
        self.progress_callback = progress_callback
        self.should_abort = should_abort
        self.should_pause = should_pause
        self._governance_task_id = str(getattr(self.config, "governance_task_id", "") or "").strip()
        self._governance_root_event_id = str(
            getattr(self.config, "governance_causal_root_id", "") or ""
        ).strip()
        self._governance_runtime_project_id = ""
        self._governance_runtime_updater: StateUpdater | None = None
        self._governance_stage_name = ""
        self._governance_stage_started_at = 0.0
        self._governance_stage_chapter_number = 0
        self._governance_stage_span: Any | None = None

        self.engine = services.engine
        self._SessionFactory = services.session_factory
        self.llm_client = services.llm_client
        self.skill_registry = services.skill_runtime.registry
        self.skill_router = services.skill_runtime.router
        self.skill_prompt_layer_builder = services.skill_runtime.prompt_layer_builder
        self.arc_director = services.arc_director
        self.book_genesis = services.book_genesis
        self.subworld_manager = services.subworld_manager
        self.retrieval_broker = services.retrieval_broker
        self.artifact_store = services.artifact_store
        self.observability = getattr(services, "observability", NullObservability())
        self.writer = services.writer
        self.provisional_writer = services.provisional_writer
        self.stage_analyzer = services.stage_analyzer
        self.pacing_strategist = services.pacing_strategist
        self.replan_governor = services.replan_governor
        self.npc_intent_generator = services.npc_intent_generator
        self.world_simulator = services.world_simulator
        self.arc_envelope_manager = services.arc_envelope_manager
        self.review_hub = services.review_hub
        self.repair_policy = services.repair_policy
        self.repair_verifier = services.repair_verifier
        self.final_acceptance_gate = services.final_acceptance_gate
        self._bind_orchestrator_runtime_hooks()

    def _bind_orchestrator_runtime_hooks(self) -> None:
        self.arc_envelope_manager.provisional_executor = self._run_provisional_band_preview
        self.arc_envelope_manager.scenario_progress_callback = (
            lambda **payload: self._emit_progress("stage_changed", **payload)
        )
        planning_services = getattr(self.arc_envelope_manager, "services", None)
        provisional_preview = getattr(planning_services, "provisional_preview", None)
        if provisional_preview is not None:
            provisional_preview.provisional_executor = self._run_provisional_band_preview
        scenario_rehearsal = getattr(planning_services, "scenario_rehearsal", None)
        if scenario_rehearsal is not None:
            scenario_rehearsal.progress_callback = (
                lambda **payload: self._emit_progress("stage_changed", **payload)
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        premise: str,
        genre: str = "玄幻",
        num_chapters: int = 3,
    ) -> RunResult:
        """Generate *num_chapters* chapters from a premise.

        Returns a run summary including the project ID and chapter outcomes.
        """
        session: Session = self._SessionFactory()
        try:
            repo, updater, checker = self._make_state_helpers(session)
            project_id = ""

            # Step 1: Plan arc -----------------------------------------------
            self._emit_progress(
                "stage_changed",
                stage="planning_arc",
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)
            print(f"\n{'='*60}")
            print("正在规划故事大纲...")
            print(f"{'='*60}")
            arc_plan = self.arc_director.plan_arc(premise, genre, num_chapters)

            # Step 2: Create project + seed state ----------------------------
            self._emit_progress(
                "stage_changed",
                stage="creating_project",
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)
            title = arc_plan.get("arc_synopsis", premise[:30])[:60]
            setting_summary = arc_plan.get("setting_summary", "")
            project = updater.create_project(
                title=title,
                premise=premise,
                genre=genre,
                setting_summary=setting_summary,
                target_total_chapters=num_chapters,
                governance=new_project_governance(
                    default_operation_mode=self.config.operation_mode,
                    review_interval_chapters=self.config.review_interval_chapters,
                ).model_copy(
                    update={
                        "progression_mode": self.config.progression_mode,
                        "auto_band_checkpoint": self.config.auto_band_checkpoint,
                        "band_warn_action": self.config.band_warn_action,
                        "manual_checkpoints_enabled": self.config.manual_checkpoints_enabled,
                        "future_constraints_enabled": self.config.future_constraints_enabled,
                    }
                ),
            )
            project_id = project.id
            self._bind_governance_runtime(project_id=project_id, updater=updater)

            self._seed_state(updater, project_id, arc_plan, num_chapters)
            session.commit()
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="business_event",
                event_type=DecisionEventType.RUN_STARTED,
                scope="task",
                summary="生成 run 已启动。",
                related_object_type="project",
                related_object_id=project_id,
                payload={"requested_chapters": num_chapters},
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="business_event",
                event_type=DecisionEventType.PROJECT_CREATED,
                scope="project",
                summary="项目已创建并写入初始规划。",
                related_object_type="project",
                related_object_id=project_id,
                payload={"requested_chapters": num_chapters},
            )
            session.commit()
            self._emit_progress(
                "project_created",
                project_id=project_id,
                title=project.title,
                requested_chapters=num_chapters,
            )

            self._emit_progress(
                "stage_changed",
                stage="resolving_arc_envelope",
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)
            previous_provisional = self._latest_provisional_gate_snapshot(
                session,
                project_id,
            )
            self.arc_envelope_manager.ensure_active_arc_resolution(
                session=session,
                project_id=project_id,
                activation_chapter=1,
            )
            session.commit()
            blocking_scenario = latest_blocking_scenario_rehearsal(session, project_id)
            if blocking_scenario is not None:
                return self._block_on_scenario_rehearsal(
                    project_id=project_id,
                    requested_chapters=num_chapters,
                    row=blocking_scenario,
                )
            failed_provisional = self._new_failed_provisional_gate(
                session,
                project_id=project_id,
                previous_snapshot=previous_provisional,
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="runtime_observation",
                event_type=DecisionEventType.PROVISIONAL_GATE_EVALUATED,
                scope="project",
                summary="provisional gate 已评估。",
                payload={
                    "blocked": failed_provisional is not None,
                    "gate_id": getattr(failed_provisional, "id", "") if failed_provisional is not None else "",
                },
            )
            session.commit()
            if failed_provisional is not None:
                return self._block_on_provisional_failure(
                    session=session,
                    updater=updater,
                    project_id=project_id,
                    requested_chapters=num_chapters,
                    gate=failed_provisional,
                )

            chapter_numbers = self._pending_chapter_numbers_for_active_arc(
                session=session,
                project_id=project_id,
            )
            if not chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=0,
                )

            print(f"项目创建完成: {project.title}")
            print(f"项目ID: {project_id}")

            result = self._run_project_chapters(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_numbers=chapter_numbers,
                requested_chapters=len(chapter_numbers),
            )
            print(f"\n{'='*60}")
            if result.status == "needs_review":
                print(
                    "生成暂停："
                    f"第 {result.paused_chapters[0]} 章被质量门阻断，需要修复或重试。"
                )
            elif result.status == "completed":
                print(f"生成完毕！本轮完成 {len(result.completed_chapters)} 章")
            else:
                print(
                    "生成结束："
                    f"成功 {len(result.completed_chapters)} 章，"
                    f"失败 {len(result.failed_chapters)} 章"
                )
                print(
                    "失败章节: "
                    + ", ".join(str(chapter) for chapter in result.failed_chapters)
                )
            print(f"项目ID: {project_id}")
            print(f"数据库: {self.engine.url.render_as_string(hide_password=True)}")
            print(f"{'='*60}\n")

            self._emit_progress(
                "stage_changed",
                stage="paused_for_review" if result.status == "needs_review" else (
                    "failed" if result.status in {"failed", "partial_failed"} else (
                        "cancelled" if result.status == "cancelled" else "completed"
                    )
                ),
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=result.completed_chapters[-1] if result.completed_chapters else 0,
                completed_chapters=result.completed_chapters,
                failed_chapters=result.failed_chapters,
                paused_chapters=result.paused_chapters,
                frozen_artifacts=result.frozen_artifacts,
            )
            return result

        except Exception:
            session.rollback()
            self._emit_progress("stage_changed", stage="failed")
            raise
        finally:
            self._clear_governance_runtime()
            session.close()

    def run_existing_project(
        self,
        project_id: str,
        *,
        num_chapters: int,
    ) -> RunResult:
        session: Session = self._SessionFactory()
        try:
            repo, updater, checker = self._make_state_helpers(session)
            project = session.get(Project, project_id)
            if project is None:
                raise ValueError(f"项目不存在: {project_id}")

            existing_plans = session.query(ChapterPlan).filter(
                ChapterPlan.project_id == project_id
            ).count()
            if existing_plans:
                session.close()
                return self.continue_project(project_id, max_chapters=num_chapters)

            premise = project.premise
            genre = project.genre or "玄幻"
            self._bind_governance_runtime(project_id=project_id, updater=updater)

            self._emit_progress(
                "stage_changed",
                stage="planning_arc",
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)

            print(f"\n{'='*60}")
            print(f"正在为项目《{project.title}》规划故事大纲...")
            print(f"{'='*60}")
            arc_plan = self.arc_director.plan_arc(premise, genre, num_chapters)

            self._emit_progress(
                "stage_changed",
                stage="creating_project",
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)

            setting_summary = arc_plan.get("setting_summary", "")
            if setting_summary:
                project.setting_summary = setting_summary
            project.target_total_chapters = max(1, int(num_chapters or 1))
            if not str(project.title or "").strip():
                project.title = (arc_plan.get("arc_synopsis", premise[:30]) or "未命名项目")[:60]
            session.add(project)

            self._seed_state(updater, project_id, arc_plan, num_chapters)
            session.commit()

            self._emit_progress(
                "stage_changed",
                stage="resolving_arc_envelope",
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=0,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, num_chapters)
            previous_provisional = self._latest_provisional_gate_snapshot(
                session,
                project_id,
            )
            self.arc_envelope_manager.ensure_active_arc_resolution(
                session=session,
                project_id=project_id,
                activation_chapter=1,
            )
            session.commit()
            blocking_scenario = latest_blocking_scenario_rehearsal(session, project_id)
            if blocking_scenario is not None:
                return self._block_on_scenario_rehearsal(
                    project_id=project_id,
                    requested_chapters=num_chapters,
                    row=blocking_scenario,
                )
            failed_provisional = self._new_failed_provisional_gate(
                session,
                project_id=project_id,
                previous_snapshot=previous_provisional,
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="runtime_observation",
                event_type=DecisionEventType.PROVISIONAL_GATE_EVALUATED,
                scope="project",
                summary="provisional gate 已评估。",
                payload={
                    "blocked": failed_provisional is not None,
                    "gate_id": getattr(failed_provisional, "id", "") if failed_provisional is not None else "",
                },
            )
            session.commit()
            if failed_provisional is not None:
                return self._block_on_provisional_failure(
                    session=session,
                    updater=updater,
                    project_id=project_id,
                    requested_chapters=num_chapters,
                    gate=failed_provisional,
                )

            chapter_numbers = self._pending_chapter_numbers_for_active_arc(
                session=session,
                project_id=project_id,
            )
            if not chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=0,
                )

            result = self._run_project_chapters(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_numbers=chapter_numbers,
                requested_chapters=len(chapter_numbers),
            )
            self._emit_progress(
                "stage_changed",
                stage="paused_for_review" if result.status == "needs_review" else (
                    "failed" if result.status in {"failed", "partial_failed"} else (
                        "cancelled" if result.status == "cancelled" else "completed"
                    )
                ),
                project_id=project_id,
                requested_chapters=num_chapters,
                current_chapter=result.completed_chapters[-1] if result.completed_chapters else 0,
                completed_chapters=result.completed_chapters,
                failed_chapters=result.failed_chapters,
                paused_chapters=result.paused_chapters,
                frozen_artifacts=result.frozen_artifacts,
            )
            return result
        except Exception:
            session.rollback()
            self._emit_progress("stage_changed", stage="failed", project_id=project_id)
            raise
        finally:
            self._clear_governance_runtime()
            session.close()

    def _emit_progress(self, event: str, **payload: Any) -> None:
        if event == "stage_changed":
            try:
                self._record_stage_transition(payload)
            except Exception:  # noqa: BLE001
                logger.debug("Ignoring stage transition tracking error.", exc_info=True)
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, payload)
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring progress callback error.", exc_info=True)

    def _bind_governance_runtime(
        self,
        *,
        project_id: str,
        updater: StateUpdater,
    ) -> None:
        self._governance_runtime_project_id = str(project_id or "").strip()
        self._governance_runtime_updater = updater
        self._governance_stage_name = ""
        self._governance_stage_started_at = 0.0
        self._governance_stage_chapter_number = 0
        self._governance_stage_span = None

    def _clear_governance_runtime(self) -> None:
        self._finish_governance_stage_span(next_stage="", chapter_number=0)
        self._governance_runtime_project_id = ""
        self._governance_runtime_updater = None
        self._governance_stage_name = ""
        self._governance_stage_started_at = 0.0
        self._governance_stage_chapter_number = 0
        self._governance_stage_span = None

    def _start_governance_stage_span(self, *, project_id: str, stage: str, chapter_number: int) -> None:
        if self._governance_stage_span is not None:
            return
        context = OperationContext(
            project_id=project_id,
            task_id=self._governance_task_id,
            chapter_number=int(chapter_number or 0),
            stage=stage,
            operation_id=self._audit_operation_id(),
        )
        span = self.observability.span(
            context,
            f"stage.{stage}",
            span_kind="stage",
            component="orchestrator",
            tags={"stage": stage},
        )
        span.__enter__()
        self._governance_stage_span = span

    def _finish_governance_stage_span(self, *, next_stage: str, chapter_number: int) -> None:
        span = self._governance_stage_span
        if span is None:
            return
        try:
            span.tag("next_stage", str(next_stage or ""))
            stage_chapter_number = int(
                getattr(self, "_governance_stage_chapter_number", 0)
                or chapter_number
                or 0
            )
            if stage_chapter_number:
                span.metric("chapter_number", stage_chapter_number)
            span.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring governance stage span close failure.", exc_info=True)
        finally:
            self._governance_stage_span = None

    def _record_stage_transition(self, payload: dict[str, Any]) -> None:
        updater = self._governance_runtime_updater
        project_id = str(payload.get("project_id") or self._governance_runtime_project_id or "").strip()
        stage = str(payload.get("stage") or "").strip()
        if updater is None or not project_id or not stage:
            return
        now = time.perf_counter()
        chapter_number = int(payload.get("current_chapter") or 0)
        if self._governance_stage_name and self._governance_stage_name != stage:
            stage_chapter_number = int(
                getattr(self, "_governance_stage_chapter_number", 0)
                or chapter_number
                or 0
            )
            duration_ms = max(0, int((now - self._governance_stage_started_at) * 1000))
            stage_payload = {
                "stage": self._governance_stage_name,
                "next_stage": stage,
                "duration_ms": duration_ms,
            }
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=stage_chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_EXITED,
                scope="task",
                summary=f"阶段 {self._governance_stage_name} 已结束。",
                payload=stage_payload,
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=stage_chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_DURATION_SUMMARY,
                scope="task",
                summary=f"阶段 {self._governance_stage_name} 用时 {duration_ms}ms。",
                payload=stage_payload,
            )
            self._finish_governance_stage_span(next_stage=stage, chapter_number=stage_chapter_number)
        if self._governance_stage_name != stage:
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_ENTERED,
                scope="task",
                summary=f"阶段 {stage} 已开始。",
                payload={"stage": stage},
            )
            self._governance_stage_name = stage
            self._governance_stage_started_at = now
            self._governance_stage_chapter_number = chapter_number
            self._start_governance_stage_span(
                project_id=project_id,
                stage=stage,
                chapter_number=chapter_number,
            )

    @staticmethod
    def _latest_provisional_gate_snapshot(
        session: Session,
        project_id: str,
    ) -> ProvisionalGateSnapshot | None:
        row = session.query(ProvisionalBandExecution).filter(
            ProvisionalBandExecution.project_id == project_id
        ).order_by(
            ProvisionalBandExecution.created_at.desc(),
            ProvisionalBandExecution.id.desc(),
        ).first()
        if row is None:
            return None
        try:
            raw_numbers = json.loads(row.chapter_numbers_json or "[]")
        except (json.JSONDecodeError, TypeError):
            raw_numbers = []
        chapter_numbers: list[int] = []
        if isinstance(raw_numbers, list):
            for item in raw_numbers:
                try:
                    chapter_number = int(item)
                except (TypeError, ValueError):
                    continue
                if chapter_number > 0:
                    chapter_numbers.append(chapter_number)
        return ProvisionalGateSnapshot(
            id=row.id,
            aggregate_verdict=str(row.aggregate_verdict or "").strip().lower(),
            failure_count=max(0, int(row.failure_count or 0)),
            issue_count=max(0, int(row.issue_count or 0)),
            chapter_numbers=chapter_numbers,
        )

    def _new_failed_provisional_gate(
        self,
        session: Session,
        *,
        project_id: str,
        previous_snapshot: ProvisionalGateSnapshot | None,
    ) -> ProvisionalGateSnapshot | None:
        latest = self._latest_provisional_gate_snapshot(session, project_id)
        if latest is None:
            return None
        if not bool(getattr(self.config, "legacy_provisional_blocking", False)):
            return None
        if previous_snapshot is not None and latest.id == previous_snapshot.id:
            return None
        if latest.aggregate_verdict == "fail" or latest.failure_count > 0:
            return latest
        return None

    def _block_on_scenario_rehearsal(
        self,
        *,
        project_id: str,
        requested_chapters: int,
        row,
    ) -> RunResult:
        try:
            payload = json.loads(row.report_json or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        chapter_numbers = [
            int(item)
            for item in (payload.get("chapter_numbers") or [])
            if str(item).strip().lstrip("-").isdigit()
        ]
        paused_chapters = chapter_numbers or [1]
        status = str(payload.get("resolution_status") or "manual_patch_required")
        self._emit_progress(
            "stage_changed",
            stage="paused_for_review",
            project_id=project_id,
            requested_chapters=requested_chapters,
            current_chapter=paused_chapters[0],
            completed_chapters=[],
            failed_chapters=[],
            paused_chapters=paused_chapters,
        )
        logger.warning(
            "Scenario rehearsal paused canon writing for project=%s status=%s band=%s",
            project_id,
            status,
            getattr(row, "band_id", ""),
        )
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            paused_chapters=paused_chapters,
            paused=True,
        )

    def _block_on_provisional_failure(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        requested_chapters: int,
        gate: ProvisionalGateSnapshot,
    ) -> RunResult:
        failed_chapters = gate.chapter_numbers or [1]
        for chapter_number in failed_chapters:
            updater.mark_chapter_status(project_id, chapter_number, "failed")
        session.commit()
        self._emit_progress(
            "stage_changed",
            stage="provisional_failed",
            project_id=project_id,
            requested_chapters=requested_chapters,
            current_chapter=failed_chapters[0],
            completed_chapters=[],
            failed_chapters=failed_chapters,
            paused_chapters=[],
        )
        logger.error(
            "Provisional preview blocked canon writing for project=%s verdict=%s failures=%d issues=%d chapters=%s",
            project_id,
            gate.aggregate_verdict,
            gate.failure_count,
            gate.issue_count,
            failed_chapters,
        )
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            failed_chapters=failed_chapters,
        )

    @staticmethod
    def _pending_chapter_numbers_for_active_arc(
        *,
        session: Session,
        project_id: str,
        max_chapters: int | None = None,
    ) -> list[int]:
        active_arc = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
            .order_by(ArcPlanVersion.created_at.desc(), ArcPlanVersion.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if active_arc is None:
            return []
        chapter_numbers = list(
            session.execute(
                select(ChapterPlan.chapter_number)
                .where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.arc_plan_id == active_arc.id,
                    ChapterPlan.status.in_(("planned", "failed")),
                )
                .order_by(ChapterPlan.chapter_number.asc())
            ).scalars().all()
        )
        if max_chapters is not None:
            chapter_numbers = chapter_numbers[: max(1, int(max_chapters or 1))]
        return chapter_numbers

    def _materialize_next_genesis_arc_if_needed(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project: Project,
    ) -> bool:
        if str(getattr(project, "creation_status", "") or "").strip() != "writing":
            return False
        if not str(getattr(project, "active_genesis_revision_id", "") or "").strip():
            return False
        revision = self.book_genesis.active_revision(session, project)
        if revision is None:
            return False
        remaining_pending = session.execute(
            select(ChapterPlan.chapter_number)
            .where(
                ChapterPlan.project_id == project.id,
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .limit(1)
        ).scalar_one_or_none()
        if remaining_pending is not None:
            return False
        promoted = self.book_genesis.promote_next_arc_if_needed(
            session=session,
            updater=updater,
            project=project,
            revision=revision,
        )
        if promoted:
            session.commit()
        return promoted

    def continue_project(self, project_id: str, max_chapters: int | None = None) -> RunResult:
        session: Session = self._SessionFactory()
        try:
            repo, updater, checker = self._make_state_helpers(session)
            self._bind_governance_runtime(project_id=project_id, updater=updater)
            project = session.get(Project, project_id)
            if project is None:
                raise ValueError(f"项目不存在: {project_id}")

            chapter_plans = session.query(ChapterPlan).filter(
                ChapterPlan.project_id == project_id
            ).order_by(ChapterPlan.chapter_number).all()
            if not chapter_plans:
                raise ValueError(f"项目没有章节规划: {project_id}")

            waiting_review: list[int] = []
            for plan in chapter_plans:
                if plan.status != "needs_review":
                    continue
                latest_draft = session.query(ChapterDraft).filter(
                    ChapterDraft.chapter_plan_id == plan.id
                ).order_by(ChapterDraft.version.desc()).first()
                if latest_draft is None:
                    logger.warning(
                        "Resetting orphan needs_review chapter back to planned for project=%s chapter=%d",
                        project_id,
                        plan.chapter_number,
                    )
                    plan.status = "planned"
                    session.add(plan)
                    continue
                waiting_review.append(plan.chapter_number)
            if waiting_review:
                waiting = ", ".join(str(number) for number in waiting_review)
                raise ValueError(f"仍有章节等待 review：{waiting}")
            session.commit()
            repo, updater, checker = self._make_state_helpers(session)
            workset = build_continue_generation_workset(
                session,
                project_id,
                max_chapters=max_chapters,
                source="orchestrator_continue",
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="business_event",
                event_type=DecisionEventType.RUN_STARTED,
                scope="task",
                summary="已有项目生成 run 已启动。",
                related_object_type="project",
                related_object_id=project_id,
                payload={
                    "resolved_workset_count": workset.requested_chapters,
                    "materialized_plan_count": workset.materialized_plan_count,
                    "workset_reason": workset.reason,
                },
            )
            session.commit()

            pending_chapter_numbers = list(workset.chapter_numbers)
            if workset.reason == "future_arc_materialization_required":
                if self._materialize_next_genesis_arc_if_needed(
                    session=session,
                    updater=updater,
                    project=project,
                ):
                    repo, updater, checker = self._make_state_helpers(session)
                    workset = build_continue_generation_workset(
                        session,
                        project_id,
                        max_chapters=max_chapters,
                        source="orchestrator_continue",
                    )
                    pending_chapter_numbers = list(workset.chapter_numbers)
            if not pending_chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=0,
                )

            self._emit_progress(
                "stage_changed",
                stage="resolving_arc_envelope",
                project_id=project_id,
                pending_chapter_count=len(pending_chapter_numbers),
                resolved_workset_count=workset.requested_chapters,
                current_chapter=min(pending_chapter_numbers) - 1,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, len(pending_chapter_numbers))
            previous_provisional = self._latest_provisional_gate_snapshot(
                session,
                project_id,
            )
            self.arc_envelope_manager.ensure_active_arc_resolution(
                session=session,
                project_id=project_id,
                activation_chapter=min(pending_chapter_numbers),
            )
            session.commit()
            blocking_scenario = latest_blocking_scenario_rehearsal(session, project_id)
            if blocking_scenario is not None:
                return self._block_on_scenario_rehearsal(
                    project_id=project_id,
                    requested_chapters=len(pending_chapter_numbers),
                    row=blocking_scenario,
                )
            failed_provisional = self._new_failed_provisional_gate(
                session,
                project_id=project_id,
                previous_snapshot=previous_provisional,
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="runtime_observation",
                event_type=DecisionEventType.PROVISIONAL_GATE_EVALUATED,
                scope="project",
                summary="provisional gate 已评估。",
                payload={
                    "blocked": failed_provisional is not None,
                    "gate_id": getattr(failed_provisional, "id", "") if failed_provisional is not None else "",
                },
            )
            session.commit()
            if failed_provisional is not None:
                return self._block_on_provisional_failure(
                    session=session,
                    updater=updater,
                    project_id=project_id,
                    requested_chapters=len(pending_chapter_numbers),
                    gate=failed_provisional,
                )

            workset = build_continue_generation_workset(
                session,
                project_id,
                max_chapters=max_chapters,
                source="orchestrator_continue",
            )
            chapter_numbers = list(workset.chapter_numbers)
            if not chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=0,
                )

            return self._run_project_chapters(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_numbers=chapter_numbers,
                requested_chapters=len(chapter_numbers),
            )
        finally:
            self._clear_governance_runtime()
            session.close()

    def accept_review(self, project_id: str, chapter_number: int, *, reason: str = "") -> dict[str, str]:
        session: Session = self._SessionFactory()
        try:
            repo, updater, _checker = self._make_state_helpers(session)
            project = repo.get_project(project_id)
            chapter_plan = repo.get_chapter_plan(project_id, chapter_number)
            if chapter_plan is None:
                raise ValueError(f"第{chapter_number}章不存在")

            latest_draft = session.query(ChapterDraft).filter(
                ChapterDraft.chapter_plan_id == chapter_plan.id
            ).order_by(ChapterDraft.version.desc()).first()
            if latest_draft is None:
                raise ValueError(f"第{chapter_number}章尚未生成 draft")

            latest_review = session.query(ChapterReview).filter(
                ChapterReview.draft_id == latest_draft.id
            ).order_by(ChapterReview.created_at.desc()).first()
            if latest_review is None:
                raise ValueError(f"第{chapter_number}章尚未生成 review")

            writer_output = self._load_writer_output_from_meta(latest_draft.llm_raw_response)
            verdict = self._load_review_verdict(latest_review)

            frozen_path = self._apply_canon_candidate(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
            )

            repair_attempt_count = len(repo.list_chapter_rewrite_attempts(project_id, chapter_number))
            if frozen_path:
                updater.mark_chapter_status(
                    project_id,
                    chapter_number,
                    "needs_review",
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=self._review_issue_payloads(verdict),
                    canon_risk_level="high",
                )
                session.commit()
                return {
                    "status": "needs_review",
                    "message": f"第{chapter_number}章 canon gate 阻止接受，已转为 needs_review。",
                    "frozen_artifact": frozen_path,
                }
            acceptance_mode = (
                "checkpoint_approved"
                if project is not None and self._project_governance(project).default_operation_mode == "checkpoint"
                else "human_approved"
            )
            updater.mark_chapter_status(
                project_id,
                chapter_number,
                "accepted",
                acceptance_mode=acceptance_mode,
                repair_attempt_count=repair_attempt_count,
                residual_review_issues=self._review_issue_payloads(verdict),
                canon_risk_level=(
                    "low" if verdict.verdict in {"pass", "warn"} else "high"
                ),
            )
            self.retrieval_broker.memory_index.upsert_chapter(
                project_id=project_id,
                chapter_number=chapter_number,
                title=writer_output.title,
                summary=writer_output.end_of_chapter_summary,
                body=writer_output.body,
            )
            self._run_phase3_pass(
                session=session,
                project_id=project_id,
                chapter_number=chapter_number,
            )
            self._audit_future_plans_after_acceptance(
                session=session,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                trigger_stage="manual_acceptance",
            )
            self._compile_world_model_after_acceptance(
                session=session,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="audit_action",
                event_type=DecisionEventType.REVIEW_APPROVED,
                scope="chapter",
                summary=f"第{chapter_number}章 review 已人工接受并写入 canon。",
                reason=str(reason or "").strip(),
                related_object_type="chapter_review",
                related_object_id=latest_review.id,
                payload={
                    "issue_types": [
                        str(getattr(issue, "issue_type", getattr(issue, "rule_name", "")) or "")
                        for issue in verdict.issues
                    ],
                    "issue_groups": [
                        str(getattr(issue, "issue_group", "") or issue_group_for_issue(
                            issue_type=str(getattr(issue, "issue_type", "") or ""),
                            rule_name=str(getattr(issue, "rule_name", "") or ""),
                        ))
                        for issue in verdict.issues
                    ],
                    "verdict": verdict.verdict,
                },
            )
            session.commit()
            return {
                "status": "accepted",
                "message": f"第{chapter_number}章已接受并写入 canon。",
                "frozen_artifact": frozen_path,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _project_governance(self, project: Project):
        return normalize_project_governance(
            getattr(project, "governance_json", "{}"),
            fallback_operation_mode=self.config.operation_mode,
            fallback_review_interval=self.config.review_interval_chapters,
            treat_empty_as_legacy=True,
        )

    def _record_decision_event(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        event_family: str,
        event_type: str,
        summary: str,
        reason: str = "",
        scope: str = "project",
        actor_type: str = "system",
        band_id: str = "",
        chapter_number: int = 0,
        task_id: str = "",
        related_object_type: str = "",
        related_object_id: str = "",
        payload: dict[str, Any] | None = None,
        parent_event_id: str = "",
        causal_root_id: str = "",
    ):
        row = updater.save_decision_event(
            DecisionEventInfo(
                project_id=project_id,
                task_id=task_id or self._governance_task_id,
                band_id=band_id,
                chapter_number=chapter_number,
                scope=scope,
                event_family=event_family,
                event_type=ensure_decision_event_type(event_type),
                actor_type=actor_type,
                summary=summary,
                reason=reason,
                payload=payload or {},
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                parent_event_id=parent_event_id,
                causal_root_id=causal_root_id or self._governance_root_event_id,
            )
        )
        if not self._governance_root_event_id:
            self._governance_root_event_id = str(row.causal_root_id or row.id or "")
        return row

    def _audit_current_plan_before_write(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_plan: ChapterPlan,
        context,
        trigger_stage: str,
    ):
        project = session.get(Project, project_id)
        target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
        chapter_number = int(chapter_plan.chapter_number or 0)
        canon_quality_context = dict(getattr(context, "canon_quality_context", {}) or {})
        obligation_repo = NarrativeObligationRepository(session)
        result = FuturePlanAuditor().audit_and_apply(
            session=session,
            project_id=project_id,
            current_chapter=chapter_number,
            trigger_stage=trigger_stage,
            plans=[chapter_plan],
            canon_quality_context=canon_quality_context,
            obligations=obligation_repo.list_active_for_context(project_id, chapter_number=chapter_number),
            target_total_chapters=target_total_chapters,
            include_current=True,
        )
        self._record_future_plan_audit_events(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            result=result,
        )
        if result.blocking_reasons:
            raise RuntimeError(
                "future_plan_audit_blocked:"
                + ";".join(str(reason) for reason in result.blocking_reasons)
            )
        if result.applied_plan_patch_ids:
            session.flush()
            return self.retrieval_broker.build_chapter_context(repo, project_id, chapter_plan)
        return context

    def _audit_future_plans_after_acceptance(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        trigger_stage: str = "post_acceptance",
    ) -> FuturePlanAuditRun | None:
        project = session.get(Project, project_id)
        target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
        plans = self._future_plan_audit_plans(
            session=session,
            project_id=project_id,
            current_chapter=chapter_number,
            include_current=False,
        )
        band_rows = self._future_plan_audit_band_rows(
            session=session,
            project_id=project_id,
            current_chapter=chapter_number,
        )
        if not plans and not band_rows:
            return None
        obligation_repo = NarrativeObligationRepository(session)
        obligations = [
            *obligation_repo.list_active_for_context(project_id, chapter_number=chapter_number + 1),
            *obligation_repo.list_planned_for_chapter(project_id, origin_chapter_number=chapter_number),
        ]
        canon_quality_context = _build_canon_quality_context(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number + 1,
            target_total_chapters=target_total_chapters,
        )
        result = FuturePlanAuditor().audit_and_apply(
            session=session,
            project_id=project_id,
            current_chapter=chapter_number,
            trigger_stage=trigger_stage,
            plans=plans,
            canon_quality_context=canon_quality_context,
            obligations=obligations,
            target_total_chapters=target_total_chapters,
            include_current=False,
            band_rows=band_rows,
        )
        self._record_future_plan_audit_events(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            result=result,
        )
        return result

    @staticmethod
    def _future_plan_audit_plans(
        *,
        session: Session,
        project_id: str,
        current_chapter: int,
        include_current: bool,
    ) -> list[ChapterPlan]:
        lower_bound = int(current_chapter or 0)
        predicate = ChapterPlan.chapter_number >= lower_bound if include_current else ChapterPlan.chapter_number > lower_bound
        return list(
            session.execute(
                select(ChapterPlan)
                .where(
                    ChapterPlan.project_id == project_id,
                    predicate,
                    ChapterPlan.status.in_(("planned", "failed")),
                )
                .order_by(ChapterPlan.chapter_number.asc())
            ).scalars().all()
        )

    @staticmethod
    def _future_plan_audit_band_rows(
        *,
        session: Session,
        project_id: str,
        current_chapter: int,
    ) -> list[BandExperiencePlan]:
        return list(
            session.execute(
                select(BandExperiencePlan)
                .where(
                    BandExperiencePlan.project_id == project_id,
                    BandExperiencePlan.chapter_end > int(current_chapter or 0),
                )
                .order_by(BandExperiencePlan.chapter_start.asc(), BandExperiencePlan.chapter_end.asc())
            ).scalars().all()
        )

    def _record_future_plan_audit_events(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        result: FuturePlanAuditRun,
    ) -> None:
        if not result.inspected_chapters:
            return
        event = self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.FUTURE_PLAN_AUDIT_RUN,
            scope="project",
            summary=f"future plan audit: {result.status}",
            related_object_type="future_plan_audit_run",
            related_object_id=result.id,
            payload=result.model_dump(mode="json", exclude={"plan_patches"}),
        )
        if result.applied_plan_patch_ids:
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="audit_action",
                event_type=DecisionEventType.FUTURE_PLAN_PATCH_APPLIED,
                scope="project",
                summary=f"已应用 {len(result.applied_plan_patch_ids)} 个 future plan patch。",
                related_object_type="future_plan_audit_run",
                related_object_id=result.id,
                parent_event_id=str(event.id or ""),
                payload={
                    "applied_plan_patch_ids": list(result.applied_plan_patch_ids),
                    "issue_types": [issue.issue_type for issue in result.issues],
                },
            )

    def _record_generation_audit_checkpoint_if_due(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        requested_chapters: int,
        last_requested_chapter: int,
        completed_chapters: list[int],
        failed_chapters: list[int],
        paused_chapters: list[int],
        future_plan_audit_result: FuturePlanAuditRun | None,
        governance,
    ) -> bool:
        interval = _positive_int(
            getattr(governance, "generation_audit_interval_chapters", 0)
        )
        if interval <= 0:
            return False
        chapter_number = int(chapter_number or 0)
        if chapter_number <= 0 or chapter_number % interval != 0:
            return False
        project_pause_enabled = bool(getattr(governance, "generation_audit_pause_enabled", False))
        runtime_pause_enabled = bool(getattr(self.config, "generation_audit_pause_enabled", False))
        pause_enabled = bool(project_pause_enabled and runtime_pause_enabled)
        has_next_requested = chapter_number != int(last_requested_chapter or 0)
        will_pause = bool(pause_enabled and has_next_requested)
        payload = self._generation_audit_checkpoint_payload(
            session=session,
            project_id=project_id,
            checkpoint_chapter=chapter_number,
            interval=interval,
            requested_chapters=requested_chapters,
            completed_chapters=[*completed_chapters, chapter_number],
            failed_chapters=failed_chapters,
            paused_chapters=paused_chapters,
            future_plan_audit_result=future_plan_audit_result,
            will_pause=will_pause,
            pause_enabled=pause_enabled,
            project_pause_enabled=project_pause_enabled,
            runtime_pause_enabled=runtime_pause_enabled,
            next_chapter=(chapter_number + 1 if has_next_requested else 0),
        )
        summary = (
            f"第{chapter_number}章命中 {interval} 章生成审计检查点，运行将暂停。"
            if will_pause
            else f"第{chapter_number}章命中 {interval} 章生成审计检查点，已记录摘要。"
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.GENERATION_AUDIT_CHECKPOINT_REACHED,
            scope="project",
            summary=summary,
            related_object_type="generation_audit_checkpoint",
            related_object_id=f"{project_id}:{chapter_number}",
            payload=payload,
        )
        return will_pause

    def _generation_audit_checkpoint_payload(
        self,
        *,
        session: Session,
        project_id: str,
        checkpoint_chapter: int,
        interval: int,
        requested_chapters: int,
        completed_chapters: list[int],
        failed_chapters: list[int],
        paused_chapters: list[int],
        future_plan_audit_result: FuturePlanAuditRun | None,
        will_pause: bool,
        pause_enabled: bool,
        project_pause_enabled: bool,
        runtime_pause_enabled: bool,
        next_chapter: int,
    ) -> dict[str, Any]:
        window_start = max(1, int(checkpoint_chapter or 0) - max(1, int(interval or 1)) + 1)
        window_end = int(checkpoint_chapter or 0)
        plans = (
            session.query(ChapterPlan)
            .filter(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number >= window_start,
                ChapterPlan.chapter_number <= window_end,
            )
            .order_by(ChapterPlan.chapter_number.asc())
            .all()
        )
        status_by_chapter = {
            str(int(plan.chapter_number or 0)): str(plan.status or "")
            for plan in plans
        }
        accepted_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.status or "") == "accepted"
        ]
        needs_review_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.status or "") == "needs_review"
        ]
        failed_window_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.status or "") == "failed"
        ]
        high_risk_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.canon_risk_level or "") == "high"
        ]
        repair_attempts_by_chapter = {
            str(int(plan.chapter_number or 0)): int(plan.repair_attempt_count or 0)
            for plan in plans
            if int(plan.repair_attempt_count or 0) > 0
        }
        residual_issue_count_by_chapter: dict[str, int] = {}
        for plan in plans:
            raw_issues = str(plan.residual_review_issues_json or "[]")
            try:
                parsed_issues = json.loads(raw_issues)
            except (json.JSONDecodeError, TypeError):
                parsed_issues = []
            if isinstance(parsed_issues, list) and parsed_issues:
                residual_issue_count_by_chapter[str(int(plan.chapter_number or 0))] = len(parsed_issues)
        obligation_repo = NarrativeObligationRepository(session)
        active_obligations = obligation_repo.list_active_for_context(
            project_id,
            chapter_number=window_end + 1,
        )
        return {
            "checkpoint_chapter": window_end,
            "checkpoint_interval": int(interval or 0),
            "window_start": window_start,
            "window_end": window_end,
            "requested_chapters": int(requested_chapters or 0),
            "completed_chapters": sorted({int(item) for item in completed_chapters}),
            "failed_chapters": sorted({int(item) for item in [*failed_chapters, *failed_window_chapters]}),
            "paused_chapters": sorted({int(item) for item in paused_chapters}),
            "accepted_chapters": accepted_chapters,
            "needs_review_chapters": needs_review_chapters,
            "status_by_chapter": status_by_chapter,
            "high_risk_chapters": high_risk_chapters,
            "repair_attempts_by_chapter": repair_attempts_by_chapter,
            "residual_issue_count_by_chapter": residual_issue_count_by_chapter,
            "open_obligation_ids": [item.id for item in active_obligations],
            "p0_p1_open_obligation_ids": [
                item.id for item in active_obligations if item.priority in {"P0", "P1"}
            ],
            "future_plan_audit": _future_plan_audit_checkpoint_payload(
                future_plan_audit_result
            ),
            "pause": {
                "enabled": pause_enabled,
                "project_enabled": project_pause_enabled,
                "runtime_enabled": runtime_pause_enabled,
                "will_pause": will_pause,
                "reason": "generation_audit_checkpoint" if will_pause else "",
            },
            "next_chapter": int(next_chapter or 0),
        }

    def _previous_band_row(
        self,
        session: Session,
        *,
        project_id: str,
        current_start: int,
    ) -> BandExperiencePlan | None:
        active_arc = StateRepository(session).get_active_arc_plan(project_id)
        if active_arc is None:
            return None
        return (
            session.query(BandExperiencePlan)
            .filter(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.arc_id == active_arc.id,
                BandExperiencePlan.chapter_end < current_start,
            )
            .order_by(BandExperiencePlan.chapter_end.desc(), BandExperiencePlan.created_at.desc())
            .first()
        )

    def _manual_boundary_checkpoint(
        self,
        session: Session,
        *,
        project_id: str,
        chapter_number: int,
        boundary_kind: str,
    ) -> BandCheckpoint | None:
        return (
            session.query(BandCheckpoint)
            .filter(
                BandCheckpoint.project_id == project_id,
                BandCheckpoint.trigger_source == "manual_boundary",
                BandCheckpoint.status == "pending",
                BandCheckpoint.boundary_kind == boundary_kind,
                BandCheckpoint.boundary_chapter == chapter_number,
            )
            .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
            .first()
        )

    def _strict_progression_block(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater | None = None,
        project: Project,
        chapter_number: int,
    ) -> tuple[str, str, str]:
        governance = self._project_governance(project)
        mode = str(governance.progression_mode or "legacy_relaxed")
        if mode == "legacy_relaxed":
            return "", "", ""
        if chapter_number > 1:
            previous_plan = repo.get_chapter_plan(project.id, chapter_number - 1)
            if previous_plan is not None and previous_plan.status != "accepted":
                return (
                    "chapter_not_canon",
                    "",
                    chapter_blocking_message("chapter_not_canon", chapter_number=chapter_number - 1),
                )
        if mode != "serial_canon_band_guard":
            return "", "", ""
        band_row = repo.get_band_row_for_chapter(project.id, chapter_number)
        if band_row is None or not band_is_first_chapter(band_row.chapter_start, chapter_number):
            return "", "", ""
        previous_band = self._previous_band_row(
            session,
                project_id=project.id,
                current_start=int(band_row.chapter_start or 0),
            )
        if previous_band is None:
            return "", "", ""
        latest_checkpoint = repo.get_latest_band_checkpoint(project.id, band_id=previous_band.band_id)
        if latest_checkpoint is None and bool(governance.auto_band_checkpoint) and updater is not None:
            latest_checkpoint = self._create_auto_band_checkpoint(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project.id,
                chapter_number=int(previous_band.chapter_end or 0),
            )
        if latest_checkpoint is None:
            return (
                "band_checkpoint_pending",
                previous_band.band_id,
                chapter_blocking_message("band_checkpoint_pending", band_id=previous_band.band_id),
            )
        checkpoint_status = str(latest_checkpoint.status or "")
        if checkpoint_status in {"pass", "overridden"}:
            return "", "", ""
        if checkpoint_status == "warn" and str(governance.band_warn_action or "") == "continue":
            return "", "", ""
        code = {
            "pending": "band_checkpoint_pending",
            "warn": "band_checkpoint_warn",
            "fail": "band_checkpoint_fail",
            "error": "band_checkpoint_fail",
        }.get(checkpoint_status, "band_checkpoint_pending")
        return (
            code,
            previous_band.band_id,
            chapter_blocking_message(code, band_id=previous_band.band_id),
        )

    def _create_auto_band_checkpoint(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
    ) -> BandCheckpoint | None:
        active_arc = repo.get_active_arc_plan(project_id)
        band_row = None
        if active_arc is not None:
            band_row = (
                session.query(BandExperiencePlan)
                .filter(
                    BandExperiencePlan.project_id == project_id,
                    BandExperiencePlan.arc_id == active_arc.id,
                    BandExperiencePlan.chapter_start <= chapter_number,
                    BandExperiencePlan.chapter_end == chapter_number,
                )
                .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
                .first()
            )
        if band_row is None:
            band_row = repo.get_band_row_for_chapter(project_id, chapter_number)
        if band_row is None or int(band_row.chapter_end or 0) != chapter_number:
            return None
        existing_boundary_checkpoint = (
            session.query(BandCheckpoint)
            .filter(
                BandCheckpoint.project_id == project_id,
                BandCheckpoint.band_id == band_row.band_id,
                BandCheckpoint.trigger_source == "auto_band_end",
                BandCheckpoint.boundary_kind == "band_end",
                BandCheckpoint.boundary_chapter == chapter_number,
            )
            .order_by(BandCheckpoint.created_at.desc(), BandCheckpoint.id.desc())
            .first()
        )
        if (
            existing_boundary_checkpoint is not None
            and str(existing_boundary_checkpoint.status or "") in {"pending", "warn", "fail", "error"}
        ):
            return existing_boundary_checkpoint
        band_plans = (
            session.query(ChapterPlan)
            .filter(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number >= int(band_row.chapter_start or 0),
                ChapterPlan.chapter_number <= int(band_row.chapter_end or 0),
            )
            .order_by(ChapterPlan.chapter_number.asc())
            .all()
        )
        unresolved = [
            row for row in repo.list_band_checkpoints(project_id, band_id=band_row.band_id)
            if row.status == "pending"
        ]
        constraints_enabled = (
            bool(repo.future_constraints_enabled(project_id))
            if hasattr(repo, "future_constraints_enabled")
            else True
        )
        issues: list[BandCheckpointIssueInfo] = []
        status = "pass"
        chapter_bodies: list[str] = []
        chapter_summaries: list[str] = []
        unresolved_review_chapters: list[int] = []
        review_fail_chapters: list[int] = []
        review_metas: list[dict[str, Any]] = []
        for plan in band_plans:
            if str(plan.status or "") == "needs_review":
                unresolved_review_chapters.append(int(plan.chapter_number or 0))
            latest_draft = (
                session.query(ChapterDraft)
                .filter(ChapterDraft.chapter_plan_id == plan.id)
                .order_by(ChapterDraft.version.desc())
                .first()
            )
            if latest_draft is None:
                continue
            chapter_bodies.append(str(latest_draft.body_text or ""))
            chapter_summaries.append(str(latest_draft.summary or ""))
            latest_review = (
                session.query(ChapterReview)
                .filter(ChapterReview.draft_id == latest_draft.id)
                .order_by(ChapterReview.created_at.desc(), ChapterReview.id.desc())
                .first()
            )
            if latest_review is not None and str(latest_review.verdict or "") == "fail":
                review_fail_chapters.append(int(plan.chapter_number or 0))
            if latest_review is not None:
                try:
                    review_meta = json.loads(latest_review.review_meta_json or "{}") or {}
                except (json.JSONDecodeError, TypeError):
                    review_meta = {}
                try:
                    review_issues = json.loads(latest_review.issues_json or "[]") or []
                except (json.JSONDecodeError, TypeError):
                    review_issues = []
                if isinstance(review_meta, dict):
                    review_meta["chapter_number"] = int(plan.chapter_number or 0)
                    review_meta["issue_types"] = [
                        str(item.get("issue_type") or item.get("rule_name") or "")
                        for item in review_issues
                        if isinstance(item, dict)
                    ]
                    review_metas.append(review_meta)
        latest_provisional = (
            session.query(ProvisionalBandExecution)
            .filter(
                ProvisionalBandExecution.project_id == project_id,
                ProvisionalBandExecution.arc_id == band_row.arc_id,
                ProvisionalBandExecution.band_id == band_row.band_id,
            )
            .order_by(ProvisionalBandExecution.created_at.desc(), ProvisionalBandExecution.id.desc())
            .first()
        )
        provisional_failed = bool(
            latest_provisional is not None
            and (
                str(latest_provisional.aggregate_verdict or "") == "fail"
                or int(latest_provisional.failure_count or 0) > 0
            )
        )
        if any(plan.status != "accepted" for plan in band_plans):
            status = "fail"
            issues.append(
                BandCheckpointIssueInfo(
                    code="band_not_fully_accepted",
                    severity="error",
                    issue_group=issue_group_for_issue(code="intra_band_consistency"),
                    description="band 内仍有章节未 accepted。",
                )
            )
        if unresolved:
            status = "warn" if status == "pass" else status
            issues.append(
                BandCheckpointIssueInfo(
                    code="pending_checkpoint_exists",
                    severity="warning",
                    issue_group=issue_group_for_issue(code="intra_band_consistency"),
                    description="同 band 仍存在未处理 checkpoint。",
                )
            )
        intra_band_issues = evaluate_intra_band_consistency(
            unresolved_review_chapters=unresolved_review_chapters,
            review_fail_chapters=review_fail_chapters,
            provisional_failed=provisional_failed,
            pending_checkpoint_count=len(unresolved),
            reviewer="governance",
            target_scope="band",
        )
        for issue in intra_band_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="intra_band_consistency",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        combined_text = band_combined_text(
            chapter_bodies=chapter_bodies,
            chapter_summaries=chapter_summaries,
        )
        band_task_issues = evaluate_task_contract(
            repo.get_band_task_contract_for_chapter(project_id, chapter_number),
            combined_text=combined_text,
            reviewer="governance",
            issue_type="band_task_completion",
            target_scope="band",
        )
        for issue in band_task_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="band_task_completion",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        try:
            schedule_payload = json.loads(band_row.schedule_json or "{}")
        except (TypeError, json.JSONDecodeError):
            schedule_payload = {}
        band_schedule = (
            BandDelightSchedule.model_validate(schedule_payload)
            if isinstance(schedule_payload, dict)
            else None
        )
        obligation_repo = NarrativeObligationRepository(session)
        band_obligations = [
            *obligation_repo.list_active_for_context(project_id, chapter_number=chapter_number + 1),
            *obligation_repo.list_planned_for_chapter(project_id, origin_chapter_number=chapter_number),
        ]
        band_obligation_issues = evaluate_band_obligation_contract(
            band_schedule,
            obligations=band_obligations,
            band_end_chapter=chapter_number,
            reviewer="governance",
            target_scope="band",
        )
        for issue in band_obligation_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="band_obligation_completion",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        director_issues = evaluate_director_imbalance(
            review_metas=review_metas,
            band_stall_guard=int(getattr(band_row, "stall_guard_max_gap", 0) or 0),
            reviewer="governance",
            target_scope="band",
        )
        for issue in director_issues:
            issues.append(
                BandCheckpointIssueInfo(
                    code="director_imbalance",
                    severity=issue.severity,
                    issue_group=issue.issue_group,
                    description=issue.description,
                    detail="; ".join(issue.evidence_refs),
                )
            )
        next_band_summary = repo.get_next_band_summary(project_id, chapter_number)
        constraint_chapter = (
            int(next_band_summary.chapter_start or 0)
            if next_band_summary is not None
            else chapter_number + 1
        )
        future_constraints = (
            repo.list_active_narrative_constraints(
                project_id,
                chapter_number=max(chapter_number, constraint_chapter),
            )
            if constraints_enabled
            else []
        )
        if constraints_enabled:
            compatibility_issues = evaluate_constraint_issues(
                future_constraints,
                combined_text=combined_text,
                state_changes=[],
                events=[],
                thread_beats=[],
                reviewer="governance",
                issue_type="next_band_compatibility",
                target_scope="band",
            )
            compatibility_issues.extend(
                evaluate_next_band_task_compatibility(
                    next_band_summary=next_band_summary,
                    combined_text=combined_text,
                    reviewer="governance",
                    target_scope="band",
                )
            )
            for issue in compatibility_issues:
                issues.append(
                    BandCheckpointIssueInfo(
                        code="next_band_compatibility" if issue.severity == "error" else "future_constraint",
                        severity=issue.severity,
                        issue_group=issue.issue_group,
                        description=issue.description,
                        detail="; ".join(issue.evidence_refs),
                    )
                )
            next_band_targets = [
                *[
                    task.target_name
                    for task in (next_band_summary.band_task_contract if next_band_summary is not None else [])
                    if str(task.target_name or "").strip()
                ],
                *[
                    constraint.subject_name
                    for constraint in future_constraints
                    if str(constraint.subject_name or "").strip()
                ],
            ]
            future_risk_issues = evaluate_resource_closure_risk(
                combined_text=combined_text,
                next_band_targets=list(dict.fromkeys(next_band_targets)),
                reviewer="governance",
                target_scope="band",
            )
            for issue in future_risk_issues:
                category = ""
                for ref in issue.evidence_refs:
                    text = str(ref or "")
                    if text.startswith("category="):
                        category = text.split("=", 1)[1].strip()
                        break
                issues.append(
                    BandCheckpointIssueInfo(
                        code="future_resource_preservation",
                        severity="warning",
                        category=category,
                        issue_group=issue.issue_group,
                        description=issue.description,
                        detail="; ".join(issue.evidence_refs),
                    )
                )
        if status != "fail" and any(issue.severity == "error" for issue in issues):
            status = "fail"
        elif status == "pass" and any(issue.severity == "warning" for issue in issues):
            status = "warn"
        summary = "band checkpoint 通过。" if status == "pass" else "band checkpoint 需要人工处理。"
        row = updater.save_band_checkpoint(
            BandCheckpointDetail(
                project_id=project_id,
                arc_id=band_row.arc_id,
                band_id=band_row.band_id,
                chapter_start=int(band_row.chapter_start or 0),
                chapter_end=int(band_row.chapter_end or 0),
                trigger_source="auto_band_end",
                boundary_kind="band_end",
                boundary_chapter=chapter_number,
                status=status,
                summary=summary,
                issues=issues,
            )
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            band_id=band_row.band_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.BAND_CHECKPOINT_CREATED,
            scope="band",
            summary=summary,
            related_object_type="band_checkpoint",
            related_object_id=row.id,
            payload={"status": status},
        )
        return row

    # ------------------------------------------------------------------
    # State seeding from arc plan
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_supported_state_changes(changes):
        filtered = []
        for change in changes:
            known_fields = KNOWN_STATE_FIELDS.get(change.entity_kind, set())
            if known_fields and change.field not in known_fields:
                logger.warning(
                    "Dropping unsupported state change field %r for entity kind %r.",
                    change.field,
                    change.entity_kind,
                )
                continue
            filtered.append(change)
        return filtered

    def _make_state_helpers(
        self,
        session: Session,
    ) -> tuple[StateRepository, StateUpdater, ContinuityChecker]:
        repo = StateRepository(session)
        updater = StateUpdater(session)
        checker = ContinuityChecker(
            repo,
            min_chars=self.config.min_chapter_chars,
            max_chars=self.config.max_chapter_chars,
        )
        return repo, updater, checker

    def _select_skill_layers(
        self,
        *,
        scope: str,
        stage_key: str,
        task_family: str,
    ) -> list[object]:
        selections = self.skill_router.select(
            scope=scope,
            stage_key=stage_key,
            task_family=task_family,
        )
        return self.skill_prompt_layer_builder.build(selections)

    @staticmethod
    def _filter_supported_kwargs(
        callable_obj: Callable[..., Any],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        target_callable = callable_obj
        side_effect = getattr(callable_obj, "side_effect", None)
        if callable(side_effect):
            target_callable = side_effect
        try:
            signature = inspect.signature(target_callable)
        except (TypeError, ValueError):
            return dict(kwargs)
        parameters = signature.parameters
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
            return dict(kwargs)
        return {
            key: value
            for key, value in kwargs.items()
            if key in parameters
        }

    def _call_with_compatible_kwargs(
        self,
        callable_obj: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return callable_obj(*args, **self._filter_supported_kwargs(callable_obj, kwargs))

    def _save_prompt_trace_payload(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        prompt_trace: dict[str, object] | None,
        parent_trace_id: str = "",
        decision_event_id: str = "",
    ) -> str:
        payload = prompt_trace if isinstance(prompt_trace, dict) else {}
        if not payload:
            return ""
        input_snapshot = payload.get("input_snapshot") if isinstance(payload.get("input_snapshot"), dict) else {}
        output_summary = payload.get("output_summary") if isinstance(payload.get("output_summary"), dict) else {}
        trace_chapter_number = int(
            (input_snapshot or {}).get("chapter_number")
            or (output_summary or {}).get("chapter_number")
            or 0
        )
        payload = prepare_prompt_trace_payload(
            payload,
            artifact_store=self.artifact_store,
            project_id=project_id,
            chapter_number=trace_chapter_number,
        )
        project = session.get(Project, project_id)
        row = updater.save_prompt_trace(
            project_id=project_id,
            genesis_revision_id=str(getattr(project, "active_genesis_revision_id", "") or ""),
            decision_event_id=str(decision_event_id or "").strip(),
            parent_trace_id=str(parent_trace_id or "").strip(),
            trace_scope=str(payload.get("trace_scope", "writer") or "writer"),
            stage_key=str(payload.get("stage_key", "") or ""),
            template_id=str(payload.get("template_id", "") or ""),
            template_version=str(payload.get("template_version", "v1") or "v1"),
            effective_system_prompt=str(payload.get("effective_system_prompt", "") or ""),
            prompt_layers_json=json.dumps(payload.get("prompt_layers", []), ensure_ascii=False),
            input_snapshot_json=json.dumps(payload.get("input_snapshot", {}), ensure_ascii=False),
            model_profile_json=json.dumps(payload.get("model_profile", {}), ensure_ascii=False),
            attempts_json=json.dumps(payload.get("attempts", []), ensure_ascii=False),
            output_summary_json=json.dumps(payload.get("output_summary", {}), ensure_ascii=False),
            backend=str(payload.get("backend", "") or ""),
            codex_job_id=str(payload.get("codex_job_id", "") or ""),
            permission_profile=str(payload.get("permission_profile", "") or ""),
            fallback_used=bool(payload.get("fallback_used", False)),
        )
        for event_payload in build_llm_decision_event_payloads(payload, prompt_trace_id=row.id):
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=trace_chapter_number,
                event_family=str(event_payload.get("event_family") or "runtime_observation"),
                event_type=str(event_payload.get("event_type") or DecisionEventType.LLM_REQUEST_FAILED),
                scope="chapter" if trace_chapter_number else "project",
                summary=str(event_payload.get("summary") or "LLM trace event."),
                payload=event_payload.get("payload") if isinstance(event_payload.get("payload"), dict) else {},
                related_object_type="prompt_trace",
                related_object_id=row.id,
                parent_event_id=str(decision_event_id or "").strip(),
            )
        self._record_prompt_trace_performance_spans(
            project_id=project_id,
            chapter_number=trace_chapter_number,
            prompt_trace_id=row.id,
            trace_payload=payload,
        )
        return row.id

    def _record_prompt_trace_performance_spans(
        self,
        *,
        project_id: str,
        chapter_number: int,
        prompt_trace_id: str,
        trace_payload: dict[str, object],
    ) -> None:
        attempts = trace_payload.get("attempts") if isinstance(trace_payload, dict) else []
        if not isinstance(attempts, list):
            return
        trace_scope = str(trace_payload.get("trace_scope") or "llm").strip() or "llm"
        fallback_stage = str(trace_payload.get("stage_key") or "").strip()
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            stage_key = str(attempt.get("stage_key") or fallback_stage or "").strip()
            try:
                duration_ms = max(0, int(attempt.get("duration_ms") or 0))
            except (TypeError, ValueError):
                duration_ms = 0
            tags = redact_payload(
                {
                    "prompt_trace_id": prompt_trace_id,
                    "trace_scope": trace_scope,
                    "stage_key": stage_key,
                    "profile_id": str(attempt.get("profile_id") or ""),
                    "profile_name": str(attempt.get("profile_name") or ""),
                    "model": str(attempt.get("model") or ""),
                    "llm_task_route": str(attempt.get("llm_task_route") or ""),
                    "http_status": int(attempt.get("http_status") or 0),
                    "attempt_no": int(attempt.get("attempt_no") or 0),
                    "attempt_group_id": str(attempt.get("attempt_group_id") or ""),
                    "retryable": bool(attempt.get("retryable", False)),
                    "fallback_eligible": bool(attempt.get("fallback_eligible", False)),
                    "final_failure": bool(attempt.get("final_failure", False)),
                    "parse_ok": bool(attempt.get("parse_ok", True)),
                    "schema_ok": bool(attempt.get("schema_ok", True)),
                }
            )
            metrics = {
                "input_chars": int(attempt.get("input_chars") or 0),
                "output_chars": int(attempt.get("output_chars") or 0),
                "sleep_ms": int(attempt.get("sleep_ms") or 0),
            }
            failed = bool(
                attempt.get("error_class")
                or attempt.get("final_failure")
                or attempt.get("parse_error")
            )
            error = {}
            if failed:
                error = redact_payload(
                    {
                        "error_class": str(attempt.get("error_class") or ""),
                        "error_message": str(
                            attempt.get("error_message")
                            or attempt.get("parse_error")
                            or attempt.get("error_category")
                            or ""
                        ),
                        "error_category": str(attempt.get("error_category") or ""),
                    }
                )
            context = OperationContext(
                project_id=project_id,
                task_id=self._governance_task_id,
                chapter_number=int(chapter_number or 0),
                stage=stage_key,
                operation_id=self._audit_operation_id(),
            )
            parent_span = current_span()
            record = SpanRecord(
                context=context,
                span_name="llm.request",
                span_kind="llm",
                component=trace_scope,
                tags=tags,
                metrics=metrics,
                status="failed" if failed else "ok",
                error=error,
                trace_id=str(getattr(parent_span, "trace_id", "") or prompt_trace_id),
                span_id=new_id(),
                parent_span_id=str(getattr(parent_span, "span_id", "") or ""),
                start_time_unix_ms=int(time.time() * 1000),
                duration_ms=duration_ms,
                self_duration_ms=duration_ms,
            )
            try:
                self.observability._record_span(record)
            except Exception:  # noqa: BLE001
                logger.debug("Ignoring prompt trace performance span failure.", exc_info=True)

    def _persist_draft_and_review(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        chapter_plan: ChapterPlan,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        review: ReviewVerdict,
    ) -> tuple[WriterOutput, ChapterDraft, ChapterReview]:
        artifact_paths = self.artifact_store.save_writer_output(
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output,
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.WRITER_OUTPUT_ARTIFACT_SAVED,
            scope="chapter",
            summary=f"第{chapter_number}章 writer output artifact 已保存。",
            payload={
                "draft_blob_path": artifact_paths.get("draft_blob_path", ""),
                "artifact_meta_path": artifact_paths.get("meta_path", ""),
                "char_count": int(getattr(writer_output, "char_count", 0) or 0),
            },
        )
        persisted_output = artifact_paths["writer_output"].model_copy(
            update={
                "generation_meta": {
                    **writer_output.generation_meta,
                    "artifact_meta_path": artifact_paths["meta_path"],
                },
            }
        )
        draft = updater.save_draft(
            chapter_plan_id=chapter_plan.id,
            writer_output=persisted_output,
            raw_response=artifact_paths["meta_path"],
            model_name=self.config.minimax_model,
        )
        review_row = updater.save_review(draft.id, review)
        repair_attempt_count = session.query(ChapterRewriteAttempt).filter(
            ChapterRewriteAttempt.project_id == project_id,
            ChapterRewriteAttempt.chapter_number == chapter_number,
        ).count()
        CandidateDraftRepository(session).upsert_from_review(
            project_id=project_id,
            chapter_plan=chapter_plan,
            draft=draft,
            review=review_row,
            writer_output=persisted_output,
            repair_attempt_count=repair_attempt_count,
        )
        updater.mark_chapter_status(project_id, chapter_number, "drafted")
        session.flush()
        return persisted_output, draft, review_row

    def _review_current_output(
        self,
        *,
        repo: StateRepository,
        checker: ContinuityChecker,
        project_id: str,
        context,
        writer_output: WriterOutput,
    ) -> ReviewVerdict:
        reviewer_skill_layers = self._select_skill_layers(
            scope="reviewer",
            stage_key="chapter_review",
            task_family="review_chapter",
        )
        return self._call_with_compatible_kwargs(
            self.review_hub.review,
            project_id=project_id,
            repo=repo,
            context=context,
            writer_output=writer_output,
            continuity_checker=checker,
            reviewer_skill_layers=reviewer_skill_layers,
        )

    @staticmethod
    def _apply_canon_name_drift_autofix(
        writer_output: WriterOutput,
        review: ReviewVerdict,
    ) -> WriterOutput | None:
        replacements: dict[str, str] = {}
        for issue in review.issues:
            if str(issue.rule_name or "") != "canon_name_drift":
                continue
            if str(issue.severity or "") != "error":
                continue
            entity_names = list(issue.entity_names or [])
            if len(entity_names) < 2:
                continue
            observed = str(entity_names[0] or "").strip()
            canonical = str(entity_names[1] or "").strip()
            if not observed or not canonical or observed == canonical:
                continue
            if observed.startswith(canonical):
                continue
            if not is_plausible_person_name(observed) or not is_plausible_person_name(canonical):
                continue
            replacements[observed] = canonical

        if not replacements:
            return None

        payload = WritingOrchestrator._replace_canon_name_strings(
            writer_output.model_dump(mode="python"),
            replacements,
        )
        payload["char_count"] = len(str(payload.get("body") or ""))
        generation_meta = dict(payload.get("generation_meta") or {})
        previous_autofix = generation_meta.get("canon_name_autofix")
        if isinstance(previous_autofix, dict):
            autofix_meta = {str(key): str(value) for key, value in previous_autofix.items()}
            autofix_meta.update(replacements)
        else:
            autofix_meta = replacements
        generation_meta["canon_name_autofix"] = autofix_meta
        payload["generation_meta"] = generation_meta
        return WriterOutput.model_validate(payload)

    @staticmethod
    def _apply_subworld_admission_autofix(
        writer_output: WriterOutput,
        review: ReviewVerdict,
        *,
        protected_names: set[str] | None = None,
    ) -> WriterOutput | None:
        replacements: dict[str, str] = {}
        body = str(writer_output.body or "")
        protected = {
            ContinuityChecker._normalize_character_reference(name)
            for name in (protected_names or set())
            if str(name or "").strip()
        }
        for issue in review.issues:
            if str(issue.rule_name or "") != "sub_world_unknown_named_entity":
                continue
            if str(issue.severity or "") != "error":
                continue
            entity_names = list(issue.entity_names or [])
            if not entity_names:
                continue
            observed = str(entity_names[0] or "").strip()
            normalized_observed = ContinuityChecker._normalize_character_reference(observed)
            if not observed or not WritingOrchestrator._looks_like_genericizable_unknown_reference(normalized_observed):
                continue
            if normalized_observed in protected:
                continue
            generic = WritingOrchestrator._generic_subworld_reference(body, observed)
            replacements[observed] = generic
            if len(observed) >= 2:
                replacements[f"{observed[0]}总"] = generic
            for title in WritingOrchestrator._subworld_role_titles():
                phrase = f"{title}{observed}"
                if phrase in body:
                    replacements[phrase] = title

        if not replacements:
            return None

        payload = WritingOrchestrator._replace_canon_name_strings(
            writer_output.model_dump(mode="python"),
            replacements,
        )
        payload["char_count"] = len(str(payload.get("body") or ""))
        generation_meta = dict(payload.get("generation_meta") or {})
        previous_autofix = generation_meta.get("subworld_admission_autofix")
        if isinstance(previous_autofix, dict):
            autofix_meta = {str(key): str(value) for key, value in previous_autofix.items()}
            autofix_meta.update(replacements)
        else:
            autofix_meta = replacements
        generation_meta["subworld_admission_autofix"] = autofix_meta
        payload["generation_meta"] = generation_meta
        return WriterOutput.model_validate(payload)

    @staticmethod
    def _apply_placeholder_leakage_autofix(
        writer_output: WriterOutput,
        review: ReviewVerdict,
    ) -> WriterOutput | None:
        body = str(writer_output.body or "")
        if "工作人员" not in body and "工作人员" not in str(writer_output.end_of_chapter_summary or ""):
            return None
        should_replace = any(
            str(issue.rule_name or "") == "bare_role_placeholder_leakage"
            and str(issue.severity or "") == "error"
            for issue in review.issues
        )
        if not should_replace:
            return None
        replacement = WritingOrchestrator._placeholder_role_replacement(body)
        replacements = {"工作人员": replacement}
        payload = WritingOrchestrator._replace_canon_name_strings(
            writer_output.model_dump(mode="python"),
            replacements,
        )
        payload["char_count"] = len(str(payload.get("body") or ""))
        generation_meta = dict(payload.get("generation_meta") or {})
        previous_autofix = generation_meta.get("placeholder_leakage_autofix")
        if isinstance(previous_autofix, dict):
            autofix_meta = {str(key): str(value) for key, value in previous_autofix.items()}
            autofix_meta.update(replacements)
        else:
            autofix_meta = replacements
        generation_meta["placeholder_leakage_autofix"] = autofix_meta
        payload["generation_meta"] = generation_meta
        return WriterOutput.model_validate(payload)

    @staticmethod
    def _placeholder_role_replacement(body: str) -> str:
        text = str(body or "")
        if "旧书摊" in text or "书摊" in text:
            return "旧书摊主"
        if "系统维护组" in text or "维护组" in text:
            return "系统维护员"
        if "分馆" in text or "地下三层" in text:
            return "地下分馆管理员"
        return "具体见证人"

    @staticmethod
    def _looks_like_genericizable_unknown_reference(name: str) -> bool:
        text = ContinuityChecker._normalize_character_reference(name)
        if not text:
            return False
        if is_plausible_person_name(text):
            return True
        if 2 <= len(text) <= 3 and text[0] in {"老", "小", "阿"}:
            return all("\u4e00" <= char <= "\u9fff" for char in text[1:])
        return False

    @staticmethod
    def _project_character_names(repo: StateRepository, project_id: str) -> set[str]:
        names: set[str] = set()
        try:
            project = repo.get_project(project_id)
        except Exception:  # noqa: BLE001
            project = None
        if project is not None:
            names.update(
                extract_expected_protagonist_names(
                    str(getattr(project, "premise", "") or ""),
                    str(getattr(project, "setting_summary", "") or ""),
                )
            )
        try:
            entities = repo.get_active_entities(project_id)
        except Exception:  # noqa: BLE001
            return names
        for entity in entities or []:
            if str(getattr(entity, "kind", "") or "") != "character":
                continue
            raw_names = [getattr(entity, "name", "") or "", *(getattr(entity, "aliases", []) or [])]
            for raw_name in raw_names:
                name = ContinuityChecker._normalize_character_reference(str(raw_name or ""))
                if name:
                    names.add(name)
        return names

    @staticmethod
    def _generic_subworld_reference(body: str, observed: str) -> str:
        if observed in body:
            index = body.find(observed)
            marker_window = body[max(0, index - 30) : index + len(observed) + 30]
        else:
            marker_window = body
        if any(marker in marker_window for marker in ("集团", "董事", "会议", "总监", "高管", "部门")):
            return "集团高管"
        return "馆员"

    @staticmethod
    def _subworld_role_titles() -> tuple[str, ...]:
        return (
            "首席运营官",
            "运营负责人",
            "财务总监",
            "财务负责人",
            "法务部负责人",
            "法务负责人",
            "部门总监",
            "部门负责人",
            "集团董事",
            "董事会成员",
            "安全主管",
            "安保主管",
            "项目负责人",
        )

    @staticmethod
    def _replace_canon_name_strings(value: Any, replacements: dict[str, str]) -> Any:
        if isinstance(value, str):
            result = value
            for observed, canonical in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
                result = result.replace(observed, canonical)
            return result
        if isinstance(value, list):
            return [
                WritingOrchestrator._replace_canon_name_strings(item, replacements)
                for item in value
            ]
        if isinstance(value, tuple):
            return tuple(
                WritingOrchestrator._replace_canon_name_strings(item, replacements)
                for item in value
            )
        if isinstance(value, dict):
            return {
                (
                    WritingOrchestrator._replace_canon_name_strings(key, replacements)
                    if isinstance(key, str)
                    else key
                ): WritingOrchestrator._replace_canon_name_strings(item, replacements)
                for key, item in value.items()
            }
        return value

    @staticmethod
    def _review_event_payload(review: ReviewVerdict) -> dict[str, object]:
        return {
            "verdict": review.verdict,
            "issue_types": [
                str(getattr(issue, "issue_type", getattr(issue, "rule_name", "")) or "")
                for issue in review.issues
            ],
            "issue_groups": [
                str(getattr(issue, "issue_group", "") or issue_group_for_issue(
                    issue_type=str(getattr(issue, "issue_type", "") or ""),
                    rule_name=str(getattr(issue, "rule_name", "") or ""),
                ))
                for issue in review.issues
            ],
            "forced_accept_applied": bool(review.forced_accept_applied),
        }

    @staticmethod
    def _review_issue_payloads(review: ReviewVerdict) -> list[dict[str, object]]:
        issues = review.residual_review_issues or review.issues
        return [issue.model_dump(mode="json") for issue in issues]

    def _record_map_movement_review_issues(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        review: ReviewVerdict,
        parent_event_id: str = "",
    ) -> None:
        issues = [
            issue
            for issue in review.issues
            if str(getattr(issue, "rule_name", "") or "").startswith("map_")
        ]
        if not issues:
            return
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.MAP_MOVEMENT_REVIEW_ISSUE,
            scope="chapter",
            summary=f"第{chapter_number}章 map movement reviewer 发现 {len(issues)} 个问题。",
            payload=audit_payload(
                stage="map_movement_review",
                status="issue",
                operation_id=self._audit_operation_id(),
                issue_count=len(issues),
                issues=[
                    {
                        "rule_name": str(issue.rule_name or ""),
                        "issue_type": str(issue.issue_type or ""),
                        "severity": str(issue.severity or ""),
                        "issue_group": str(issue.issue_group or ""),
                        "target_scope": str(issue.target_scope or ""),
                        "entity_names": list(issue.entity_names or []),
                        "evidence_refs": list(issue.evidence_refs or []),
                    }
                    for issue in issues
                ],
            ),
            parent_event_id=parent_event_id,
        )

    @staticmethod
    def _review_canon_risk(review: ReviewVerdict) -> str:
        if review.final_gate_decision is not None:
            return str(review.final_gate_decision.canon_risk or "")
        if review.forced_accept_applied:
            return "low"
        if review.verdict == "fail":
            return "high"
        return ""

    @staticmethod
    def _load_json_list(raw: str) -> list[object]:
        try:
            payload = json.loads(raw or "[]") or []
        except (json.JSONDecodeError, TypeError):
            return []
        return payload if isinstance(payload, list) else []

    def _chapter_plan_snapshot(
        self,
        *,
        repo: StateRepository,
        project_id: str,
        chapter_plan: ChapterPlan,
        experience_plan: ChapterExperiencePlan | None = None,
        transient_overlay: bool = False,
    ) -> dict[str, object]:
        live_experience_plan = experience_plan or repo.get_chapter_experience_plan(
            project_id,
            chapter_plan.chapter_number,
        )
        return {
            "chapter_number": int(chapter_plan.chapter_number or 0),
            "title": str(chapter_plan.title or ""),
            "one_line": str(chapter_plan.one_line or ""),
            "goals": self._load_json_list(getattr(chapter_plan, "goals_json", "[]")),
            "task_contract": self._load_json_list(getattr(chapter_plan, "task_contract_json", "[]")),
            "experience_plan": (
                live_experience_plan.model_dump(mode="json")
                if live_experience_plan is not None
                else {}
            ),
            "transient_overlay": bool(transient_overlay),
        }

    def _band_plan_snapshot(
        self,
        *,
        repo: StateRepository,
        project_id: str,
        chapter_number: int,
        schedule: BandDelightSchedule | None = None,
        transient_overlay: bool = False,
    ) -> dict[str, object]:
        row = repo.get_band_row_for_chapter(project_id, chapter_number)
        live_schedule = schedule or repo.get_band_experience_plan_for_chapter(project_id, chapter_number)
        if row is None and live_schedule is None:
            return {}
        return {
            "band_id": str(getattr(row, "band_id", getattr(live_schedule, "band_id", "")) or ""),
            "chapter_start": int(getattr(row, "chapter_start", getattr(live_schedule, "chapter_start", 0)) or 0),
            "chapter_end": int(getattr(row, "chapter_end", getattr(live_schedule, "chapter_end", 0)) or 0),
            "task_contract": self._load_json_list(getattr(row, "task_contract_json", "[]")),
            "schedule": live_schedule.model_dump(mode="json") if live_schedule is not None else {},
            "transient_overlay": bool(transient_overlay),
        }

    @staticmethod
    def _repair_verification_issue(
        *,
        rule_name: str,
        description: str,
        suggested_fix: str,
    ) -> ContinuityIssue:
        return ContinuityIssue(
            rule_name=rule_name,
            severity="error",
            description=description,
            reviewer="repair_verifier",
            issue_type="repair_verification",
            target_scope="chapter",
            evidence_refs=[],
            suggested_fix=suggested_fix,
        )

    def _review_with_repair_verification(
        self,
        *,
        original_output: WriterOutput,
        repaired_output: WriterOutput,
        before_review: ReviewVerdict,
        review: ReviewVerdict,
        repair_instruction: RepairInstruction,
    ) -> ReviewVerdict:
        verification = self.repair_verifier.verify(
            original_output=original_output,
            repaired_output=repaired_output,
            before_review=before_review,
            after_review=review,
            repair_instruction=repair_instruction,
        )
        merged_review = review.model_copy(update={"repair_verification": verification})
        if verification.fixed_all_must_fix and verification.preserved_all_must_preserve:
            return merged_review

        issues = list(merged_review.issues)
        for item in verification.unfixed:
            issues.append(
                self._repair_verification_issue(
                    rule_name="repair_unfixed",
                    description=f"repair 未真正修复：{item}",
                    suggested_fix="升级 repair scope，并继续针对 must_fix 重写。",
                )
            )
        for item in verification.broken_preserve_constraints:
            issues.append(
                self._repair_verification_issue(
                    rule_name="repair_preserve_breach",
                    description=f"repair 破坏了 must_preserve：{item}",
                    suggested_fix="保留既有约束后重新修复，不允许以修 A 伤 B。",
                )
            )
        summary_parts = [str(merged_review.review_summary or "").strip()]
        if verification.unfixed:
            summary_parts.append("repair verification: must_fix 仍未完全修复")
        if verification.broken_preserve_constraints:
            summary_parts.append("repair verification: must_preserve 被破坏")
        return merged_review.model_copy(
            update={
                "verdict": "fail",
                "recommended_action": "rewrite",
                "issues": issues,
                "review_summary": " | ".join(part for part in summary_parts if part),
                "repair_instruction": merged_review.repair_instruction or repair_instruction,
            }
        )

    @staticmethod
    def _repair_policy_requested_scope(review: ReviewVerdict) -> str:
        instruction = getattr(review, "repair_instruction", None)
        if instruction is None:
            return ""
        requested_scope = str(getattr(instruction, "repair_scope", "") or "").strip()
        if requested_scope in {"draft", "scene"} and WritingOrchestrator._review_has_structural_repair_issue(review):
            return ""
        return requested_scope

    @staticmethod
    def _review_has_structural_repair_issue(review: ReviewVerdict) -> bool:
        structural_issue_types = {
            "countdown_non_monotonic",
            "artifact_count_explanation",
            "artifact_ledger_conflict",
            "identity_conflict",
            "identity_ambiguity",
            "payoff_miss",
            "unpaid_promise_debt",
            "world_model_conflict",
            "cognition_conflict",
        }
        structural_target_scopes = {
            "ledger",
            "character",
            "band",
            "arc",
            "book",
            "world_model",
        }
        for issue in getattr(review, "issues", []) or []:
            issue_type = str(getattr(issue, "issue_type", "") or "").strip()
            target_scope = str(getattr(issue, "target_scope", "") or "").strip()
            if issue_type in structural_issue_types or target_scope in structural_target_scopes:
                return True
        return False

    def _review_and_maybe_rewrite(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        checker: ContinuityChecker,
        project_id: str,
        chapter_plan: ChapterPlan,
        context,
        writer_output: WriterOutput,
    ) -> tuple[WriterOutput, ReviewVerdict, bool]:
        current_context = context
        current_output = writer_output
        current_writer_trace_id = self._save_prompt_trace_payload(
            session=session,
            updater=updater,
            project_id=project_id,
            prompt_trace=(
                current_output.generation_meta.get("prompt_trace")
                if isinstance(current_output.generation_meta, dict)
                else {}
            ),
        )
        current_review = self._review_current_output(
            repo=repo,
            checker=checker,
            project_id=project_id,
            context=context,
            writer_output=current_output,
        )
        autofixed_output = self._apply_canon_name_drift_autofix(current_output, current_review)
        if autofixed_output is not None:
            current_output = autofixed_output
            current_review = self._review_current_output(
                repo=repo,
                checker=checker,
                project_id=project_id,
                context=context,
                writer_output=current_output,
            )
        protected_subworld_names = self._project_character_names(repo, project_id)
        autofixed_output = self._apply_subworld_admission_autofix(
            current_output,
            current_review,
            protected_names=protected_subworld_names,
        )
        if autofixed_output is not None:
            current_output = autofixed_output
            current_review = self._review_current_output(
                repo=repo,
                checker=checker,
                project_id=project_id,
                context=context,
                writer_output=current_output,
            )
        autofixed_output = self._apply_placeholder_leakage_autofix(current_output, current_review)
        if autofixed_output is not None:
            current_output = autofixed_output
            current_review = self._review_current_output(
                repo=repo,
                checker=checker,
                project_id=project_id,
                context=context,
                writer_output=current_output,
            )
        current_output, current_draft, current_review_row = self._persist_draft_and_review(
            session=session,
            updater=updater,
            chapter_plan=chapter_plan,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            writer_output=current_output,
            review=current_review,
        )
        current_review_event = self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
            scope="chapter",
            summary=f"第{chapter_plan.chapter_number}章 review verdict: {current_review.verdict}",
            related_object_type="chapter_review",
            related_object_id=current_review_row.id,
            payload=self._review_event_payload(current_review),
        )
        self._record_map_movement_review_issues(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            review=current_review,
            parent_event_id=str(current_review_event.id or ""),
        )
        current_review_trace_id = self._save_prompt_trace_payload(
            session=session,
            updater=updater,
            project_id=project_id,
            prompt_trace=current_review.prompt_trace,
            parent_trace_id=current_writer_trace_id,
            decision_event_id=str(current_review_event.id or ""),
        )
        if current_review.verdict != "fail" or self.config.operation_mode != "blackbox":
            return current_output, current_review, False

        while True:
            existing_attempts = repo.list_chapter_rewrite_attempts(project_id, chapter_plan.chapter_number)
            repair_decision = self.repair_policy.decide(
                verdict=current_review.verdict,
                operation_mode=self.config.operation_mode,
                attempts_completed=len(existing_attempts),
                requested_scope=self._repair_policy_requested_scope(current_review),
            )
            if repair_decision.kind != "repair":
                final_gate = self.final_acceptance_gate.evaluate(
                    operation_mode=self.config.operation_mode,
                    review=current_review,
                    verification=current_review.repair_verification,
                )
                current_review = current_review.model_copy(
                    update={
                        "repair_exhausted": True,
                        "final_gate_decision": final_gate,
                        "residual_review_issues": list(current_review.issues),
                        "forced_accept_applied": final_gate.decision == "force_accept",
                    }
                )
                current_review_row.review_meta_json = self._review_meta_json(current_review)
                session.add(current_review_row)
                if final_gate.decision == "force_accept":
                    if existing_attempts:
                        existing_attempts[-1].forced_accept_applied = True
                        session.add(existing_attempts[-1])
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_plan.chapter_number,
                        event_family="audit_action",
                        event_type=DecisionEventType.FORCED_ACCEPT_APPLIED,
                        scope="chapter",
                        summary=f"第{chapter_plan.chapter_number}章通过 final force-accept gate。",
                        related_object_type="chapter_review",
                        related_object_id=current_review_row.id,
                        parent_event_id=str(current_review_event.id or ""),
                        payload={"canon_risk": final_gate.canon_risk, "reason": final_gate.reason},
                    )
                    return current_output, current_review, True
                return current_output, current_review, False

            attempt_no = repair_decision.attempt_no
            repair_scope = repair_decision.scope
            repair_model_preference = {
                "preferred_provider_kind": repair_decision.preferred_provider_kind,
                "preferred_model": repair_decision.preferred_model,
            }
            repair_instruction = current_review.repair_instruction or self._default_repair_instruction(
                repair_scope=repair_scope,
                context=current_context,
                review=current_review,
            )
            source_chapter_plan = self._chapter_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
            )
            source_band_plan = self._band_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
            )
            repair_started_event = self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.REPAIR_STARTED,
                scope="chapter",
                summary=f"第{chapter_plan.chapter_number}章启动第 {attempt_no} 次 repair。",
                related_object_type="chapter_review",
                related_object_id=current_review_row.id,
                payload={
                    "attempt_no": attempt_no,
                    "repair_scope": repair_scope,
                    **repair_model_preference,
                },
                parent_event_id=str(current_review_event.id or ""),
            )
            (
                design_patch,
                updated_context,
                result_chapter_plan,
                result_band_plan,
                failure_reason,
            ) = self._apply_repair_patch(
                session=session,
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
                context=current_context,
                repair_scope=repair_scope,
                repair_instruction=repair_instruction,
            )
            if any(repair_model_preference.values()):
                design_patch = {
                    **design_patch,
                    "repair_model_preference": repair_model_preference,
                }
            if failure_reason:
                attempt_row = updater.save_chapter_rewrite_attempt(
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    attempt_no=attempt_no,
                    trigger_review_id=current_review_row.id,
                    repair_scope=repair_scope,
                    design_patch=design_patch,
                    source_draft_id=current_draft.id,
                    result_draft_id=current_draft.id,
                    result_verdict="fail",
                    result_review_id=current_review_row.id,
                    failure_reason=failure_reason,
                    verification={},
                    source_chapter_plan=source_chapter_plan,
                    result_chapter_plan=result_chapter_plan,
                    source_band_plan=source_band_plan,
                    result_band_plan=result_band_plan,
                    forced_accept_applied=False,
                )
                chapter_plan.repair_attempt_count = attempt_no
                session.add(chapter_plan)
                current_review_event = self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.REPAIR_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 失败。",
                    reason=failure_reason,
                    related_object_type="chapter_rewrite_attempt",
                    related_object_id=attempt_row.id,
                    payload={
                        "attempt_no": attempt_no,
                        "repair_scope": repair_scope,
                        **repair_model_preference,
                    },
                    parent_event_id=str(repair_started_event.id or ""),
                )
                continue

            self._emit_progress(
                "stage_changed",
                stage="repairing_chapter",
                project_id=project_id,
                current_chapter=chapter_plan.chapter_number,
            )
            try:
                self._emit_progress(
                    "stage_changed",
                    stage="repairing_chapter",
                    project_id=project_id,
                    current_chapter=chapter_plan.chapter_number,
                )
                rewritten_output = self._write_chapter_with_attention_fallback(
                    context=updated_context,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    updater=updater,
                    paused_chapters=[],
                    frozen_artifacts=[],
                    trace_stage_key="chapter_rewrite",
                    llm_preferred_provider_kind=repair_decision.preferred_provider_kind,
                    llm_preferred_model=repair_decision.preferred_model,
                )
            except Exception as exc:  # noqa: BLE001
                attempt_row = updater.save_chapter_rewrite_attempt(
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    attempt_no=attempt_no,
                    trigger_review_id=current_review_row.id,
                    repair_scope=repair_scope,
                    design_patch={**design_patch, "rewrite_error": str(exc)},
                    source_draft_id=current_draft.id,
                    result_draft_id=current_draft.id,
                    result_verdict="fail",
                    result_review_id=current_review_row.id,
                    failure_reason=str(exc),
                    verification={},
                    source_chapter_plan=source_chapter_plan,
                    result_chapter_plan=result_chapter_plan,
                    source_band_plan=source_band_plan,
                    result_band_plan=result_band_plan,
                    forced_accept_applied=False,
                )
                chapter_plan.repair_attempt_count = attempt_no
                session.add(chapter_plan)
                current_review_event = self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.REPAIR_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 失败。",
                    reason=str(exc),
                    related_object_type="chapter_rewrite_attempt",
                    related_object_id=attempt_row.id,
                    payload={
                        "attempt_no": attempt_no,
                        "repair_scope": repair_scope,
                        **repair_model_preference,
                    },
                    parent_event_id=str(repair_started_event.id or ""),
                )
                continue

            if rewritten_output is None:
                attempt_row = updater.save_chapter_rewrite_attempt(
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    attempt_no=attempt_no,
                    trigger_review_id=current_review_row.id,
                    repair_scope=repair_scope,
                    design_patch={**design_patch, "rewrite_error": "writer-returned-none"},
                    source_draft_id=current_draft.id,
                    result_draft_id=current_draft.id,
                    result_verdict="fail",
                    result_review_id=current_review_row.id,
                    failure_reason="writer-returned-none",
                    verification={},
                    source_chapter_plan=source_chapter_plan,
                    result_chapter_plan=result_chapter_plan,
                    source_band_plan=source_band_plan,
                    result_band_plan=result_band_plan,
                    forced_accept_applied=False,
                )
                chapter_plan.repair_attempt_count = attempt_no
                session.add(chapter_plan)
                current_review_event = self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.REPAIR_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 未产出正文。",
                    reason="writer-returned-none",
                    related_object_type="chapter_rewrite_attempt",
                    related_object_id=attempt_row.id,
                    payload={
                        "attempt_no": attempt_no,
                        "repair_scope": repair_scope,
                        **repair_model_preference,
                    },
                    parent_event_id=str(repair_started_event.id or ""),
                )
                continue
            rewritten_writer_trace_id = self._save_prompt_trace_payload(
                session=session,
                updater=updater,
                project_id=project_id,
                prompt_trace=(
                    rewritten_output.generation_meta.get("prompt_trace")
                    if isinstance(rewritten_output.generation_meta, dict)
                    else {}
                ),
                parent_trace_id=current_review_trace_id,
            )
            self._emit_progress(
                "stage_changed",
                stage="repair_review",
                project_id=project_id,
                current_chapter=chapter_plan.chapter_number,
            )
            rewritten_review = self._review_current_output(
                repo=repo,
                checker=checker,
                project_id=project_id,
                context=updated_context,
                writer_output=rewritten_output,
            )
            autofixed_rewritten_output = self._apply_canon_name_drift_autofix(
                rewritten_output,
                rewritten_review,
            )
            if autofixed_rewritten_output is not None:
                rewritten_output = autofixed_rewritten_output
                rewritten_review = self._review_current_output(
                    repo=repo,
                    checker=checker,
                    project_id=project_id,
                    context=updated_context,
                    writer_output=rewritten_output,
                )
            autofixed_rewritten_output = self._apply_subworld_admission_autofix(
                rewritten_output,
                rewritten_review,
                protected_names=self._project_character_names(repo, project_id),
            )
            if autofixed_rewritten_output is not None:
                rewritten_output = autofixed_rewritten_output
                rewritten_review = self._review_current_output(
                    repo=repo,
                    checker=checker,
                    project_id=project_id,
                    context=updated_context,
                    writer_output=rewritten_output,
                )
            autofixed_rewritten_output = self._apply_placeholder_leakage_autofix(
                rewritten_output,
                rewritten_review,
            )
            if autofixed_rewritten_output is not None:
                rewritten_output = autofixed_rewritten_output
                rewritten_review = self._review_current_output(
                    repo=repo,
                    checker=checker,
                    project_id=project_id,
                    context=updated_context,
                    writer_output=rewritten_output,
                )
            rewritten_review = self._review_with_repair_verification(
                original_output=current_output,
                repaired_output=rewritten_output,
                before_review=current_review,
                review=rewritten_review,
                repair_instruction=repair_instruction,
            )
            rewritten_output, rewritten_draft, rewritten_review_row = self._persist_draft_and_review(
                session=session,
                updater=updater,
                chapter_plan=chapter_plan,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                writer_output=rewritten_output,
                review=rewritten_review,
            )
            attempt_row = updater.save_chapter_rewrite_attempt(
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                attempt_no=attempt_no,
                trigger_review_id=current_review_row.id,
                repair_scope=repair_scope,
                design_patch=design_patch,
                source_draft_id=current_draft.id,
                result_draft_id=rewritten_draft.id,
                result_verdict=rewritten_review.verdict,
                result_review_id=rewritten_review_row.id,
                failure_reason="",
                verification=(
                    rewritten_review.repair_verification.model_dump(mode="json")
                    if rewritten_review.repair_verification is not None
                    else {}
                ),
                source_chapter_plan=source_chapter_plan,
                result_chapter_plan=result_chapter_plan,
                source_band_plan=source_band_plan,
                result_band_plan=result_band_plan,
                forced_accept_applied=False,
            )
            chapter_plan.repair_attempt_count = attempt_no
            session.add(chapter_plan)
            repair_result_event = self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                event_family="evaluation_verdict",
                event_type=(
                    DecisionEventType.REPAIR_SUCCEEDED
                    if rewritten_review.verdict != "fail"
                    else DecisionEventType.REPAIR_FAILED
                ),
                scope="chapter",
                summary=(
                    f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 已修复。"
                    if rewritten_review.verdict != "fail"
                    else f"第{chapter_plan.chapter_number}章第 {attempt_no} 次 repair 仍未通过。"
                ),
                related_object_type="chapter_rewrite_attempt",
                related_object_id=attempt_row.id,
                payload={
                    "attempt_no": attempt_no,
                    "repair_scope": repair_scope,
                    "verdict": rewritten_review.verdict,
                },
                parent_event_id=str(repair_started_event.id or ""),
            )
            current_review_event = self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
                scope="chapter",
                summary=f"第{chapter_plan.chapter_number}章 rewrite 后 verdict: {rewritten_review.verdict}",
                related_object_type="chapter_review",
                related_object_id=rewritten_review_row.id,
                payload=self._review_event_payload(rewritten_review),
                parent_event_id=str(repair_result_event.id or ""),
            )
            self._record_map_movement_review_issues(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                review=rewritten_review,
                parent_event_id=str(current_review_event.id or ""),
            )
            current_review_trace_id = self._save_prompt_trace_payload(
                session=session,
                updater=updater,
                project_id=project_id,
                prompt_trace=rewritten_review.prompt_trace,
                parent_trace_id=rewritten_writer_trace_id,
                decision_event_id=str(current_review_event.id or ""),
            )
            current_context = updated_context
            current_output = rewritten_output
            current_draft = rewritten_draft
            current_review = rewritten_review
            current_review_row = rewritten_review_row
            current_writer_trace_id = rewritten_writer_trace_id
            if rewritten_review.verdict != "fail":
                return rewritten_output, rewritten_review, False

    @staticmethod
    def _review_meta_json(review: ReviewVerdict) -> str:
        review_meta = review.model_dump(mode="json", exclude_none=True)
        review_meta.pop("verdict", None)
        review_meta.pop("issues", None)
        return json.dumps(review_meta, ensure_ascii=False)

    def _default_repair_instruction(
        self,
        *,
        repair_scope: str,
        context,
        review: ReviewVerdict,
    ) -> RepairInstruction:
        return RepairInstruction(
            repair_scope=repair_scope,  # type: ignore[arg-type]
            failure_type="mixed",
            must_fix=[issue.description for issue in review.issues if issue.severity == "error"],
            must_preserve=[
                context.chapter_plan_title,
                context.chapter_plan_one_line,
                *(context.chapter_goals[:2]),
            ],
            design_patch={},
            evidence_refs=[ref for issue in review.issues for ref in issue.evidence_refs],
        )

    def _apply_repair_patch(
        self,
        *,
        session: Session,
        repo: StateRepository,
        project_id: str,
        chapter_plan: ChapterPlan,
        context,
        repair_scope: str,
        repair_instruction: RepairInstruction,
    ) -> tuple[dict[str, object], Any, dict[str, object], dict[str, object], str]:
        current_plan = repo.get_chapter_experience_plan(project_id, chapter_plan.chapter_number) or ChapterExperiencePlan()
        band_schedule = repo.get_band_experience_plan_for_chapter(project_id, chapter_plan.chapter_number)
        arc_structure = repo.get_latest_arc_structure_draft(project_id)
        patch = dict(repair_instruction.design_patch)
        patch["repair_scope"] = repair_scope

        if repair_scope == "draft":
            updated_plan = current_plan.model_copy(
                update=self._chapter_experience_patch_payload(current_plan, repair_instruction)
            )
            updated_context = context.model_copy(
                update={"chapter_experience_plan": updated_plan}
            )
            return (
                updated_plan.model_dump(mode="json"),
                updated_context,
                self._chapter_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                    experience_plan=updated_plan,
                    transient_overlay=True,
                ),
                self._band_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    schedule=band_schedule,
                    transient_overlay=True,
                ),
                "",
            )

        if repair_scope == "chapter_plan":
            updated_plan = current_plan.model_copy(
                update=self._chapter_experience_patch_payload(current_plan, repair_instruction)
            )
            chapter_plan.experience_plan_json = json.dumps(
                updated_plan.model_dump(mode="json"),
                ensure_ascii=False,
            )
            if str(patch.get("chapter_plan_title") or patch.get("title") or "").strip():
                chapter_plan.title = str(patch.get("chapter_plan_title") or patch.get("title") or "").strip()
            if str(patch.get("chapter_plan_one_line") or patch.get("one_line") or "").strip():
                chapter_plan.one_line = str(
                    patch.get("chapter_plan_one_line") or patch.get("one_line") or ""
                ).strip()
            goal_patch = patch.get("chapter_goals")
            if not isinstance(goal_patch, list):
                goal_patch = patch.get("goals")
            if isinstance(goal_patch, list):
                chapter_plan.goals_json = json.dumps(goal_patch, ensure_ascii=False)
            task_contract_patch = patch.get("chapter_task_contract")
            if not isinstance(task_contract_patch, list):
                task_contract_patch = patch.get("task_contract")
            if isinstance(task_contract_patch, list):
                chapter_plan.task_contract_json = json.dumps(task_contract_patch, ensure_ascii=False)
            session.add(chapter_plan)
            session.flush()
            return (
                updated_plan.model_dump(mode="json"),
                self.retrieval_broker.build_chapter_context(repo, project_id, chapter_plan),
                self._chapter_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                ),
                self._band_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                ),
                "",
            )

        if band_schedule is not None:
            updated_schedule = BandDelightSchedule.model_validate(
                self._band_schedule_patch_payload(band_schedule, repair_instruction)
            )
            with session.begin_nested() as nested:
                self._replace_band_schedule(
                    session=session,
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    schedule=updated_schedule,
                    arc_structure=arc_structure,
                    repair_instruction=repair_instruction,
                )
                session.flush()
                transient_chapter_plan = self._chapter_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                    transient_overlay=True,
                )
                transient_band_plan = self._band_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    schedule=updated_schedule,
                    transient_overlay=True,
                )
                active_arc = repo.get_active_arc_plan(project_id)
                band_row = repo.get_band_row_for_chapter(project_id, chapter_plan.chapter_number)
                if active_arc is not None and band_row is not None:
                    preview_plans = [
                        repo.get_chapter_plan(project_id, number)
                        for number in range(band_row.chapter_start, band_row.chapter_end + 1)
                    ]
                    preview_plans = [
                        plan for plan in preview_plans
                        if plan is not None and str(plan.status or "") != "accepted"
                    ]
                    preview = self._run_provisional_band_preview(
                        session=session,
                        project_id=project_id,
                        arc_id=active_arc.id,
                        band_id=band_row.band_id,
                        chapter_plans=preview_plans,
                        persist_result=False,
                    )
                    if preview is not None and preview.aggregate_verdict in {"fail", "error"}:
                        nested.rollback()
                        session.expire_all()
                        return (
                            updated_schedule.model_dump(mode="json"),
                            context,
                            transient_chapter_plan,
                            transient_band_plan,
                            f"lightweight-provisional:{preview.aggregate_verdict}",
                        )
            return (
                updated_schedule.model_dump(mode="json"),
                self.retrieval_broker.build_chapter_context(repo, project_id, chapter_plan),
                self._chapter_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                ),
                self._band_plan_snapshot(
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                ),
                "",
            )

        updated_plan = current_plan.model_copy(
            update=self._chapter_experience_patch_payload(current_plan, repair_instruction)
        )
        updated_context = context.model_copy(update={"chapter_experience_plan": updated_plan})
        return (
            updated_plan.model_dump(mode="json"),
            updated_context,
            self._chapter_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
                experience_plan=updated_plan,
                transient_overlay=True,
            ),
            self._band_plan_snapshot(
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                schedule=band_schedule,
                transient_overlay=True,
            ),
            "",
        )

    def _replace_band_schedule(
        self,
        *,
        session: Session,
        repo: StateRepository,
        project_id: str,
        chapter_number: int,
        schedule: BandDelightSchedule,
        arc_structure: ArcStructureDraft | None,
        repair_instruction: RepairInstruction | None = None,
    ) -> None:
        active_arc = repo.get_active_arc_plan(project_id)
        if active_arc is None:
            return
        session.query(BandExperiencePlan).filter(
            BandExperiencePlan.project_id == project_id,
            BandExperiencePlan.arc_id == active_arc.id,
            BandExperiencePlan.band_id == schedule.band_id,
        ).delete(synchronize_session=False)
        session.add(
            BandExperiencePlan(
                id=new_id(),
                project_id=project_id,
                arc_id=active_arc.id,
                band_id=schedule.band_id,
                chapter_start=schedule.chapter_start,
                chapter_end=schedule.chapter_end,
                stall_guard_max_gap=schedule.stall_guard_max_gap,
                schedule_json=json.dumps(schedule.model_dump(mode="json"), ensure_ascii=False),
            )
        )
        structure_data = self._structure_data_from_row(arc_structure)
        for number in range(max(chapter_number, schedule.chapter_start), schedule.chapter_end + 1):
            plan = repo.get_chapter_plan(project_id, number)
            if plan is None:
                continue
            experience_plan = self.arc_envelope_manager._derive_chapter_experience_plan(
                chapter_number=number,
                structure=structure_data,
                schedule=schedule,
                chapter_plan=plan,
            )
            if number == chapter_number and repair_instruction is not None:
                experience_plan = self._current_chapter_repair_experience_plan(
                    experience_plan,
                    repair_instruction,
                )
            plan.experience_plan_json = json.dumps(experience_plan.model_dump(mode="json"), ensure_ascii=False)
            session.add(plan)

    @classmethod
    def _structure_data_from_row(cls, arc_structure: ArcStructureDraft | None):
        from forwin.orchestrator.phase24 import ArcStructureDraftData
        from forwin.protocol.experience import ReaderPromise

        if arc_structure is None:
            return ArcStructureDraftData(
                phase_layout=[],
                key_beats=[],
                thread_priorities=[],
                hotspot_candidates=[],
                compression_candidates=[],
                reader_promise=ReaderPromise(),
                arc_payoff_map=ArcPayoffMap(),
            )
        return ArcStructureDraftData(
            phase_layout=json.loads(arc_structure.phase_layout_json or "[]") or [],
            key_beats=json.loads(arc_structure.key_beats_json or "[]") or [],
            thread_priorities=json.loads(arc_structure.thread_priorities_json or "[]") or [],
            hotspot_candidates=json.loads(arc_structure.hotspot_candidates_json or "[]") or [],
            compression_candidates=json.loads(arc_structure.compression_candidates_json or "[]") or [],
            reader_promise=cls._reader_promise_from_row(arc_structure),
            arc_payoff_map=ArcPayoffMap.model_validate(json.loads(arc_structure.arc_payoff_map_json or "{}") or {}),
        )

    @staticmethod
    def _reader_promise_from_row(arc_structure: ArcStructureDraft):
        from forwin.protocol.experience import ReaderPromise

        return ReaderPromise.model_validate(json.loads(arc_structure.reader_promise_json or "{}") or {})

    @staticmethod
    def _current_chapter_repair_experience_plan(
        current_plan: ChapterExperiencePlan,
        repair_instruction: RepairInstruction,
    ) -> ChapterExperiencePlan:
        return current_plan.model_copy(
            update=WritingOrchestrator._chapter_experience_patch_payload(
                current_plan,
                repair_instruction,
            )
        )

    @staticmethod
    def _chapter_experience_patch_payload(
        current_plan: ChapterExperiencePlan,
        repair_instruction: RepairInstruction,
    ) -> dict[str, object]:
        repair_rule_anchors = WritingOrchestrator._countdown_repair_rule_anchors(repair_instruction.must_fix)
        repair_rule_anchors.extend([
            f"repair must fix: {item}"
            for item in repair_instruction.must_fix[:3]
            if str(item or "").strip()
        ])
        update: dict[str, object] = {
            "planned_reward_tags": list(
                repair_instruction.design_patch.get("planned_reward_tags")
                or current_plan.planned_reward_tags
                or ["mystery"]
            ),
            "selected_template_ids": list(
                repair_instruction.design_patch.get("selected_template_ids")
                or current_plan.selected_template_ids
            ),
            "hook_type": str(
                repair_instruction.design_patch.get("hook_type")
                or current_plan.hook_type
                or "cliffhanger_question"
            ),
            "question_hook": str(
                repair_instruction.design_patch.get("question_hook")
                or current_plan.question_hook
            ),
            "question_resolution": str(
                repair_instruction.design_patch.get("question_resolution")
                or current_plan.question_resolution
            ),
            "immersion_anchors": list(
                repair_instruction.design_patch.get("immersion_anchors")
                or current_plan.immersion_anchors
            ),
            "progress_markers": list(
                repair_instruction.design_patch.get("progress_markers")
                or current_plan.progress_markers
            ),
            "rule_anchors": list(
                repair_instruction.design_patch.get("rule_anchors")
                or current_plan.rule_anchors
            ),
            "relationship_or_status_shift": str(
                repair_instruction.design_patch.get("relationship_or_status_shift")
                or current_plan.relationship_or_status_shift
            ),
            "minimum_progress_channels": list(
                repair_instruction.design_patch.get("minimum_progress_channels")
                or current_plan.minimum_progress_channels
            ),
        }
        if repair_instruction.failure_type == "hook_failure" and "hook_type" not in repair_instruction.design_patch:
            update["hook_type"] = "hard_cliffhanger"
        if repair_instruction.failure_type == "immersion" and not update["immersion_anchors"]:
            update["immersion_anchors"] = ["补入感官锚点", "让角色即时反应落地"]
        if repair_instruction.failure_type == "immersion" and not update["rule_anchors"]:
            update["rule_anchors"] = ["补清规则边界或代价，防止作者强行感"]
        if repair_rule_anchors:
            existing_rule_anchors = [str(item) for item in update.get("rule_anchors", []) or []]
            update["rule_anchors"] = [*repair_rule_anchors, *existing_rule_anchors]
        if repair_instruction.failure_type == "stall" and not update["progress_markers"]:
            update["progress_markers"] = ["让主目标出现不可逆推进"]
        if repair_instruction.failure_type == "stall" and not update["question_hook"]:
            update["question_hook"] = "补出一个比当前更强的新问题"
        return update

    @staticmethod
    def _countdown_repair_rule_anchors(must_fix: list[str]) -> list[str]:
        anchors: list[str] = []
        for raw in must_fix:
            item = str(raw or "").strip()
            if not item:
                continue
            if "倒计时" not in item:
                continue
            stale_match = re.search(
                r"回溯旧倒计时为\s*([^，。,；;]+).*?([0-9]+)\s*分钟级别",
                item,
            )
            if stale_match:
                raw_target = str(stale_match.group(1) or "").strip()
                latest = int(stale_match.group(2))
                anchors.append(
                    "repair countdown hard constraint: 旧计划/旧摘要时间不得写成前文事实；"
                    f"{raw_target}必须删除，或明确改成公开伪数据/误导信息，"
                    f"同一记忆重置周期只能写小于等于{latest}分钟。"
                    "不得写“系统日志原本还有三天/七天/几小时”来解释当前倒计时。"
                )
                continue
            if not any(marker in item for marker in ("回升", "延长", "non_monotonic", "单调")):
                continue
            match = re.search(r"从\s*([0-9]+)\s*分钟(?:回升|延长)到\s*([^，。,；;]+)", item)
            if match:
                previous = int(match.group(1))
                raw_target = str(match.group(2) or "").strip()
                target_digit = re.search(r"([0-9]+)\s*分钟", raw_target)
                target_constraint = (
                    f"{int(target_digit.group(1))}分钟必须改成小于等于{previous}分钟"
                    if target_digit
                    else f"{raw_target}必须删除或改为小于等于{previous}分钟"
                )
                anchors.append(
                    "repair countdown hard constraint: 同一倒计时 ledger 在本章全文必须单调减少；"
                    f"{target_constraint}，"
                    "并同步修正文中所有相关倒计时、角色判断和摘要。除非正文明确 reset 或 branch clock，"
                    "不得在更小剩余时间之后再写更大的剩余时间。"
                )
                continue
            anchors.append(
                "repair countdown hard constraint: 同一倒计时 ledger 在本章全文必须单调减少；"
                "重写前先列出正文所有剩余时间，按出现顺序改成不增加序列。除非正文明确 reset 或 branch clock，"
                "不得在更小剩余时间之后再写更大的剩余时间。"
            )
        return anchors

    @staticmethod
    def _band_schedule_patch_payload(
        schedule: BandDelightSchedule,
        repair_instruction: RepairInstruction,
    ) -> dict[str, object]:
        payload = schedule.model_dump(mode="json")
        payload.update(repair_instruction.design_patch)
        if repair_instruction.failure_type == "stall":
            payload["stall_guard_max_gap"] = 1
        if repair_instruction.failure_type == "immersion" and not payload.get("immersion_anchor_scene_goal"):
            payload["immersion_anchor_scene_goal"] = "每章都落一个可感知现场锚点"
        if repair_instruction.failure_type == "stall" and not payload.get("curiosity_beats"):
            payload["curiosity_beats"] = [
                {
                    "chapter_hint": schedule.chapter_start,
                    "question_open": "当前局面真正危险在哪里",
                    "question_resolve": "先确认一个局部真相",
                    "escalated_question": "更大的幕后压力是什么",
                }
            ]
        return payload

    @staticmethod
    def _arc_payoff_patch_payload(
        payoff_map: ArcPayoffMap,
        repair_instruction: RepairInstruction,
    ) -> dict[str, object]:
        payload = payoff_map.model_dump(mode="json")
        patch = dict(repair_instruction.design_patch)
        if "macro_payoffs" in patch:
            payload["macro_payoffs"] = patch["macro_payoffs"]
        if "awe_kit" in patch:
            payload["awe_kit"] = patch["awe_kit"]
        if "revelation_layers" in patch:
            payload["revelation_layers"] = patch["revelation_layers"]
        if "ambiguity_constraints" in patch:
            payload["ambiguity_constraints"] = patch["ambiguity_constraints"]
        if repair_instruction.failure_type == "payoff_miss" and not payload.get("macro_payoffs"):
            payload["macro_payoffs"] = [
                {
                    "payoff_id": "repair-payoff-1",
                    "category": "mystery",
                    "template_id": "mystery-locked-clue",
                    "target_chapter_hint": "near-term",
                    "setup_requirement": "缩短 setup 到本 band 内",
                    "success_signal": "读者感到明确回报已经到账",
                }
            ]
        if repair_instruction.failure_type == "immersion" and not payload.get("ambiguity_constraints"):
            payload["ambiguity_constraints"] = ["关键翻盘必须回指既有规则或线索。"]
        return payload

    def _run_project_chapters(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        checker: ContinuityChecker,
        project_id: str,
        chapter_numbers: list[int],
        requested_chapters: int,
    ) -> RunResult:
        completed_chapters: list[int] = []
        failed_chapters: list[int] = []
        paused_chapters: list[int] = []
        frozen_artifacts: list[str] = []
        last_requested_chapter = max(chapter_numbers, default=0)
        project = repo.get_project(project_id)
        if project is None:
            raise ValueError(f"项目不存在: {project_id}")
        governance = self._project_governance(project)

        for chapter_num in chapter_numbers:
            if self._abort_requested():
                return self._cancelled_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=chapter_num,
                )
            if self._pause_requested():
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=max(0, chapter_num - 1),
                )
            print(f"\n{'─'*60}")
            print(f"正在生成第 {chapter_num} 章...")
            print(f"{'─'*60}")

            chapter_plan = repo.get_chapter_plan(project_id, chapter_num)
            if chapter_plan is None:
                logger.error("Chapter plan %d not found, skipping.", chapter_num)
                failed_chapters.append(chapter_num)
                continue
            block_code, block_band_id, block_message = self._strict_progression_block(
                session=session,
                repo=repo,
                updater=updater,
                project=project,
                chapter_number=chapter_num,
            )
            if block_code:
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    band_id=block_band_id,
                    chapter_number=chapter_num,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.HARD_GATE_HIT,
                    scope="chapter",
                    summary=block_message,
                    related_object_type="chapter_plan",
                    related_object_id=chapter_plan.id,
                    payload={"blocking_reason": block_code},
                )
                session.commit()
                paused_chapters.append(chapter_num)
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=max(0, chapter_num - 1),
                )
            manual_start_checkpoint = self._manual_boundary_checkpoint(
                session,
                project_id=project_id,
                chapter_number=chapter_num,
                boundary_kind="chapter_start",
            )
            if manual_start_checkpoint is not None:
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    band_id=manual_start_checkpoint.band_id,
                    chapter_number=chapter_num,
                    event_family="evaluation_verdict",
                    event_type=DecisionEventType.MANUAL_CHECKPOINT_HIT,
                    scope="chapter",
                    summary="命中 chapter_start manual checkpoint，运行已暂停。",
                    related_object_type="band_checkpoint",
                    related_object_id=manual_start_checkpoint.id,
                )
                session.commit()
                paused_chapters.append(chapter_num)
                return self._paused_result(
                    project_id,
                    requested_chapters,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                    current_chapter=max(0, chapter_num - 1),
                )

            try:
                self._emit_progress(
                    "stage_changed",
                    stage="assembling_context",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                context = self.retrieval_broker.build_chapter_context(
                    repo, project_id, chapter_plan
                )
                context = self._audit_current_plan_before_write(
                    session=session,
                    repo=repo,
                    updater=updater,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                    context=context,
                    trigger_stage="pre_write",
                )
                context_summary = dict(
                    getattr(self.retrieval_broker, "last_observability_summary", {}) or {}
                )
                if context_summary:
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.CONTEXT_ASSEMBLED,
                        scope="chapter",
                        summary=f"第{chapter_num}章 context 已组装。",
                        payload=context_summary,
                    )
                    if any(
                        int(context_summary.get(key) or 0) > 0
                        for key in ("pruned_entities", "pruned_threads", "pruned_relations")
                    ):
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            chapter_number=chapter_num,
                            event_family="runtime_observation",
                            event_type=DecisionEventType.CONTEXT_PRUNED,
                            scope="chapter",
                            summary=f"第{chapter_num}章 context 已按 budget 裁剪。",
                            payload=context_summary,
                        )

                if self._abort_requested():
                    return self._cancelled_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                if self._pause_requested():
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                self._emit_progress(
                    "stage_changed",
                    stage="writing_chapter",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                writer_output = self._write_chapter_with_attention_fallback(
                    context=context,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    updater=updater,
                    paused_chapters=paused_chapters,
                    frozen_artifacts=frozen_artifacts,
                )
                if writer_output is None:
                    if self._abort_requested():
                        return self._cancelled_result(
                            project_id,
                            requested_chapters,
                            completed_chapters=completed_chapters,
                            failed_chapters=failed_chapters,
                            paused_chapters=paused_chapters,
                            frozen_artifacts=frozen_artifacts,
                            current_chapter=chapter_num,
                        )
                    session.commit()
                    break
                if self._pause_requested():
                    session.commit()
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                self._emit_progress(
                    "stage_changed",
                    stage="continuity_review",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                writer_output, verdict, force_accept_applied = self._review_and_maybe_rewrite(
                    session=session,
                    repo=repo,
                    updater=updater,
                    checker=checker,
                    project_id=project_id,
                    chapter_plan=chapter_plan,
                    context=context,
                    writer_output=writer_output,
                )
                repair_attempt_count = len(
                    repo.list_chapter_rewrite_attempts(project_id, chapter_num)
                )
                residual_review_issues = self._review_issue_payloads(verdict)
                canon_risk_level = self._review_canon_risk(verdict)
                session.commit()
                if self._pause_requested():
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )

                if self.config.operation_mode == "checkpoint":
                    updater.mark_chapter_status(
                        project_id,
                        chapter_num,
                        "needs_review",
                        repair_attempt_count=repair_attempt_count,
                        residual_review_issues=residual_review_issues,
                        canon_risk_level=canon_risk_level,
                    )
                    session.commit()
                    paused_chapters.append(chapter_num)
                    self._emit_progress(
                        "stage_changed",
                        stage="paused_for_review",
                        project_id=project_id,
                        requested_chapters=requested_chapters,
                        current_chapter=chapter_num,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                    )
                    break
                if self.config.operation_mode == "copilot" and verdict.verdict != "pass":
                    updater.mark_chapter_status(
                        project_id,
                        chapter_num,
                        "needs_review",
                        repair_attempt_count=repair_attempt_count,
                        residual_review_issues=residual_review_issues,
                        canon_risk_level=canon_risk_level,
                    )
                    session.commit()
                    paused_chapters.append(chapter_num)
                    self._emit_progress(
                        "stage_changed",
                        stage="paused_for_review",
                        project_id=project_id,
                        requested_chapters=requested_chapters,
                        current_chapter=chapter_num,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                    )
                    break
                if (
                    self.config.operation_mode == "blackbox"
                    and verdict.verdict == "fail"
                    and not force_accept_applied
                ):
                    updater.mark_chapter_status(
                        project_id,
                        chapter_num,
                        "needs_review",
                        repair_attempt_count=repair_attempt_count,
                        residual_review_issues=residual_review_issues,
                        canon_risk_level=canon_risk_level,
                    )
                    session.commit()
                    paused_chapters.append(chapter_num)
                    self._emit_progress(
                        "stage_changed",
                        stage="paused_for_review",
                        project_id=project_id,
                        requested_chapters=requested_chapters,
                        current_chapter=chapter_num,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                    )
                    break

                should_apply_canon = (
                    verdict.verdict == "pass"
                    or (self.config.operation_mode == "blackbox" and verdict.verdict == "warn")
                    or force_accept_applied
                )
                if not should_apply_canon:
                    updater.mark_chapter_status(
                        project_id,
                        chapter_num,
                        "needs_review",
                        repair_attempt_count=repair_attempt_count,
                        residual_review_issues=residual_review_issues,
                        canon_risk_level=canon_risk_level,
                    )
                    session.commit()
                    paused_chapters.append(chapter_num)
                    self._emit_progress(
                        "stage_changed",
                        stage="paused_for_review",
                        project_id=project_id,
                        requested_chapters=requested_chapters,
                        current_chapter=chapter_num,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                    )
                    break

                review_interval = max(0, int(self.config.review_interval_chapters or 0))
                if review_interval and chapter_num % review_interval == 0 and chapter_num != last_requested_chapter:
                    updater.mark_chapter_status(
                        project_id,
                        chapter_num,
                        "needs_review",
                        repair_attempt_count=repair_attempt_count,
                        residual_review_issues=residual_review_issues,
                        canon_risk_level=canon_risk_level,
                    )
                    session.commit()
                    paused_chapters.append(chapter_num)
                    self._emit_progress(
                        "stage_changed",
                        stage="paused_for_review",
                        project_id=project_id,
                        requested_chapters=requested_chapters,
                        current_chapter=chapter_num,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                    )
                    break

                self._emit_progress(
                    "stage_changed",
                    stage="applying_canon",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                frozen_path = self._apply_canon_candidate(
                    session=session,
                    repo=repo,
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    writer_output=writer_output,
                    verdict=verdict,
                )
                if frozen_path:
                    frozen_artifacts.append(frozen_path)
                    updater.mark_chapter_status(
                        project_id,
                        chapter_num,
                        "needs_review",
                        repair_attempt_count=repair_attempt_count,
                        residual_review_issues=residual_review_issues,
                        canon_risk_level="high",
                    )
                    session.commit()
                    paused_chapters.append(chapter_num)
                    self._emit_progress(
                        "stage_changed",
                        stage="paused_for_review",
                        project_id=project_id,
                        requested_chapters=requested_chapters,
                        current_chapter=chapter_num,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                    )
                    repo, updater, checker = self._make_state_helpers(session)
                    break
                if self._pause_requested():
                    session.commit()
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )

                status = "accepted"
                updater.mark_chapter_status(
                    project_id,
                    chapter_num,
                    status,
                    acceptance_mode=(
                        "force_accept_after_repair" if force_accept_applied else "normal"
                    ),
                    repair_attempt_count=repair_attempt_count,
                    residual_review_issues=(
                        residual_review_issues if force_accept_applied else []
                    ),
                    canon_risk_level=canon_risk_level,
                )
                self._emit_progress(
                    "stage_changed",
                    stage="running_post_acceptance",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.MEMORY_INDEX_UPSERT_STARTED,
                    scope="chapter",
                    summary=f"第{chapter_num}章 memory index upsert 开始。",
                )
                try:
                    self.retrieval_broker.memory_index.upsert_chapter(
                        project_id=project_id,
                        chapter_number=chapter_num,
                        title=writer_output.title,
                        summary=writer_output.end_of_chapter_summary,
                        body=writer_output.body,
                    )
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.MEMORY_INDEX_UPSERT_SUCCEEDED,
                        scope="chapter",
                        summary=f"第{chapter_num}章 memory index upsert 完成。",
                    )
                except Exception as exc:
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.MEMORY_INDEX_UPSERT_FAILED,
                        scope="chapter",
                        summary=f"第{chapter_num}章 memory index upsert 失败。",
                        reason=str(exc),
                        payload={"error_class": exc.__class__.__name__, "error_summary": str(exc)},
                    )
                    raise
                self._run_phase3_pass(
                    session=session,
                    project_id=project_id,
                    chapter_number=chapter_num,
                )
                future_plan_audit_result = self._audit_future_plans_after_acceptance(
                    session=session,
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    trigger_stage="post_acceptance",
                )
                future_plan_audit_blocked = bool(
                    future_plan_audit_result is not None
                    and future_plan_audit_result.blocking_reasons
                )
                world_model_ok = self._compile_world_model_after_acceptance(
                    session=session,
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                )
                if not world_model_ok:
                    session.commit()
                    completed_with_current = [*completed_chapters, chapter_num]
                    paused_chapters.append(chapter_num)
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_with_current,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                generation_audit_pause = self._record_generation_audit_checkpoint_if_due(
                    session=session,
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    requested_chapters=requested_chapters,
                    last_requested_chapter=last_requested_chapter,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                    future_plan_audit_result=future_plan_audit_result,
                    governance=governance,
                )
                checkpoint_row = None
                checkpoint_pause = False
                checkpoint_warn_pause = False
                if bool(governance.auto_band_checkpoint):
                    try:
                        checkpoint_row = self._create_auto_band_checkpoint(
                            session=session,
                            repo=repo,
                            updater=updater,
                            project_id=project_id,
                            chapter_number=chapter_num,
                        )
                    except Exception as exc:
                        band_row = repo.get_band_row_for_chapter(project_id, chapter_num)
                        if band_row is None:
                            raise
                        checkpoint_row = updater.save_band_checkpoint(
                            BandCheckpointDetail(
                                project_id=project_id,
                                arc_id=band_row.arc_id,
                                band_id=band_row.band_id,
                                chapter_start=int(band_row.chapter_start or 0),
                                chapter_end=int(band_row.chapter_end or 0),
                                trigger_source="auto_band_end",
                                boundary_kind="band_end",
                                boundary_chapter=chapter_num,
                                status="error",
                                summary="band checkpoint evaluator 异常，运行已暂停。",
                                issues=[
                                    BandCheckpointIssueInfo(
                                        code="checkpoint_evaluator_error",
                                        severity="error",
                                        issue_group=issue_group_for_issue(code="runtime"),
                                        description="checkpoint evaluator 执行失败。",
                                        detail=f"{exc.__class__.__name__}: {exc}",
                                    )
                                ],
                            )
                        )
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            band_id=band_row.band_id,
                            chapter_number=chapter_num,
                            event_family="runtime_observation",
                            event_type=DecisionEventType.CHECKPOINT_EVALUATOR_ERROR,
                            scope="band",
                            summary="band checkpoint evaluator 异常。",
                            reason=str(exc),
                            related_object_type="band_checkpoint",
                            related_object_id=checkpoint_row.id,
                            payload={
                                "status": "error",
                                "error_class": exc.__class__.__name__,
                                "error_summary": str(exc),
                            },
                        )
                    if checkpoint_row is not None and checkpoint_row.status in {"fail", "error"}:
                        checkpoint_pause = True
                    if (
                        checkpoint_row is not None
                        and checkpoint_row.status == "warn"
                        and str(governance.band_warn_action or "") == "pause"
                    ):
                        checkpoint_warn_pause = True
                manual_after_accept = self._manual_boundary_checkpoint(
                    session,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    boundary_kind="chapter_accepted",
                )
                manual_band_end = self._manual_boundary_checkpoint(
                    session,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    boundary_kind="band_end",
                )
                session.commit()
                should_pause_for_checkpoint = checkpoint_pause or (
                    checkpoint_warn_pause and chapter_num != last_requested_chapter
                )
                if (
                    should_pause_for_checkpoint
                    or manual_after_accept is not None
                    or manual_band_end is not None
                    or future_plan_audit_blocked
                    or generation_audit_pause
                ):
                    if should_pause_for_checkpoint and checkpoint_row is not None:
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            band_id=checkpoint_row.band_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.BAND_CHECKPOINT_HIT,
                            scope="band",
                            summary="band checkpoint 命中阻断，运行已暂停。",
                            related_object_type="band_checkpoint",
                            related_object_id=checkpoint_row.id,
                            payload={"status": checkpoint_row.status},
                        )
                    if manual_after_accept is not None:
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            band_id=manual_after_accept.band_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.MANUAL_CHECKPOINT_HIT,
                            scope="chapter",
                            summary="命中 chapter_accepted manual checkpoint，运行已暂停。",
                            related_object_type="band_checkpoint",
                            related_object_id=manual_after_accept.id,
                        )
                    if manual_band_end is not None:
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            band_id=manual_band_end.band_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.MANUAL_CHECKPOINT_HIT,
                            scope="band",
                            summary="命中 band_end manual checkpoint，运行已暂停。",
                            related_object_type="band_checkpoint",
                            related_object_id=manual_band_end.id,
                        )
                    if future_plan_audit_blocked and future_plan_audit_result is not None:
                        self._record_decision_event(
                            updater=updater,
                            project_id=project_id,
                            chapter_number=chapter_num,
                            event_family="evaluation_verdict",
                            event_type=DecisionEventType.FUTURE_PLAN_AUDIT_RUN,
                            scope="project",
                            summary="future plan audit 存在未修复阻断，运行已暂停。",
                            related_object_type="future_plan_audit_run",
                            related_object_id=future_plan_audit_result.id,
                            payload={
                                "blocking_reasons": list(future_plan_audit_result.blocking_reasons),
                                "inspected_chapters": list(future_plan_audit_result.inspected_chapters),
                            },
                        )
                    session.commit()
                    completed_with_current = [*completed_chapters, chapter_num]
                    paused_chapters.append(chapter_num)
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_with_current,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )
                if self._pause_requested():
                    completed_chapters.append(chapter_num)
                    return self._paused_result(
                        project_id,
                        requested_chapters,
                        completed_chapters=completed_chapters,
                        failed_chapters=failed_chapters,
                        paused_chapters=paused_chapters,
                        frozen_artifacts=frozen_artifacts,
                        current_chapter=chapter_num,
                    )

            except Exception as exc:
                logger.exception("Chapter %d failed.", chapter_num)
                session.rollback()
                repo, updater, checker = self._make_state_helpers(session)
                updater.mark_chapter_status(project_id, chapter_num, "failed")
                session.commit()
                failed_chapters.append(chapter_num)
                self._emit_progress(
                    "stage_changed",
                    stage="chapter_failed",
                    project_id=project_id,
                    requested_chapters=requested_chapters,
                    current_chapter=chapter_num,
                    completed_chapters=completed_chapters,
                    failed_chapters=failed_chapters,
                    paused_chapters=paused_chapters,
                )
                print(f"  ✗ 第{chapter_num}章失败: {exc}")
                if isinstance(exc, TransientLLMChapterFailure) or self._is_transient_llm_like(exc):
                    logger.warning(
                        "Stopping run after transient LLM failure on chapter %d to avoid cascading failures.",
                        chapter_num,
                    )
                    break
                continue

            completed_chapters.append(chapter_num)
            self._emit_progress(
                "chapter_completed",
                project_id=project_id,
                requested_chapters=requested_chapters,
                current_chapter=chapter_num,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
                paused_chapters=paused_chapters,
            )

            issue_summary = ""
            if verdict.issues:
                issue_summary = " | 问题: " + "; ".join(
                    i.description for i in verdict.issues[:3]
                )
            print(
                f"  ✓ 第{chapter_num}章 《{writer_output.title}》 "
                f"({writer_output.char_count}字) "
                f"审查: {verdict.verdict}{issue_summary}"
            )

        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=completed_chapters,
            failed_chapters=failed_chapters,
            paused_chapters=paused_chapters,
            frozen_artifacts=frozen_artifacts,
        )

    def _write_chapter_with_attention_fallback(
        self,
        *,
        context,
        project_id: str,
        chapter_number: int,
        updater: StateUpdater,
        paused_chapters: list[int],
        frozen_artifacts: list[str],
        trace_stage_key: str = "chapter_draft",
        llm_preferred_provider_kind: str = "",
        llm_preferred_model: str = "",
    ) -> WriterOutput | None:
        max_attempts = max(1, int(self.config.blackbox_writer_attention_retries))
        last_error: Exception | None = None
        last_failure_event_id = ""
        last_failed_attempt = 0
        saw_transient_error = False
        writer_skill_layers = self._select_skill_layers(
            scope="writer",
            stage_key=trace_stage_key,
            task_family="write_chapter",
        )
        for attempt in range(1, max_attempts + 1):
            if self._abort_requested():
                return None
            started_at = time.perf_counter()
            model_profile_id, model_name = self._current_model_identity()
            try:
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LLM_REQUEST_STARTED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer 第 {attempt}/{max_attempts} 次调用开始。",
                    payload=audit_payload(
                        stage="writing_chapter",
                        status="started",
                        operation_id=self._audit_operation_id(),
                        attempt_no=attempt,
                        max_attempts=max_attempts,
                        model_profile_id=model_profile_id,
                        model=model_name,
                        preferred_provider_kind=str(llm_preferred_provider_kind or ""),
                        preferred_model=str(llm_preferred_model or ""),
                    ),
                )
                output = self._call_with_compatible_kwargs(
                    self.writer.write_chapter,
                    context,
                    skill_layers=writer_skill_layers,
                    trace_stage_key=trace_stage_key,
                    llm_preferred_provider_kind=llm_preferred_provider_kind,
                    llm_preferred_model=llm_preferred_model,
                )
                duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))
                self._record_model_fallback_payloads(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    parent_stage="writing_chapter",
                    events=output.generation_meta.get("model_fallbacks") or [],
                )
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LLM_REQUEST_SUCCEEDED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer 第 {attempt}/{max_attempts} 次调用成功。",
                    payload=audit_payload(
                        stage="writing_chapter",
                        status="succeeded",
                        operation_id=self._audit_operation_id(),
                        duration_ms=duration_ms,
                        attempt_no=attempt,
                        max_attempts=max_attempts,
                        model_profile_id=model_profile_id,
                        model=model_name,
                        preferred_provider_kind=str(llm_preferred_provider_kind or ""),
                        preferred_model=str(llm_preferred_model or ""),
                    ),
                )
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.STAGE_DURATION_SUMMARY,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer 调用耗时 {duration_ms}ms。",
                    payload=audit_payload(
                        stage="writing_chapter",
                        status="succeeded",
                        operation_id=self._audit_operation_id(),
                        duration_ms=duration_ms,
                        model_profile_id=model_profile_id,
                        model=model_name,
                        attempt_no=attempt,
                        preferred_provider_kind=str(llm_preferred_provider_kind or ""),
                        preferred_model=str(llm_preferred_model or ""),
                    ),
                )
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.WRITER_OUTPUT_BUILT,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer output 已生成。",
                    payload=audit_payload(
                        stage="writing_chapter",
                        status="succeeded",
                        operation_id=self._audit_operation_id(),
                        duration_ms=duration_ms,
                        char_count=int(getattr(output, "char_count", 0) or 0),
                        mode=str((getattr(output, "generation_meta", {}) or {}).get("mode") or ""),
                        scene_count=len(getattr(output, "scene_outputs", []) or []),
                        state_changes_count=len(getattr(output, "state_changes", []) or []),
                        events_count=len(getattr(output, "new_events", []) or []),
                        thread_beats_count=len(getattr(output, "thread_beats", []) or []),
                    ),
                )
                return output
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                is_transient = self._is_transient_llm_like(exc)
                saw_transient_error = saw_transient_error or is_transient
                duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))
                self._record_model_fallback_payloads(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    parent_stage="writing_chapter",
                    events=list(getattr(self.writer.llm_client, "drain_model_fallback_events", lambda: [])() or []),
                )
                llm_attempts = self._safe_prompt_trace_attempts(
                    self._drain_llm_attempt_events(),
                    fallback_attempt_no=attempt,
                    exc=exc,
                    duration_ms=duration_ms,
                )
                error_category = self._error_category_from_attempts(llm_attempts, exc)
                logger.warning(
                    "Writer failed for chapter %d on attempt %d/%d: %s",
                    chapter_number,
                    attempt,
                    max_attempts,
                    exc,
                )
                failure_event = self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LLM_REQUEST_FAILED,
                    scope="chapter",
                    summary=f"Writer 第 {attempt}/{max_attempts} 次调用失败：{safe_error_summary(exc)}",
                    payload=event_error_payload(
                        exc,
                        stage="writing_chapter",
                        operation_id=self._audit_operation_id(),
                        duration_ms=duration_ms,
                        error_category=error_category,
                        attempt_no=attempt,
                        max_attempts=max_attempts,
                        is_transient=is_transient,
                        model_profile_id=model_profile_id,
                        model=model_name,
                        attempt_count=len(llm_attempts),
                        attempt_group_ids=attempt_group_ids(llm_attempts),
                        preferred_provider_kind=str(llm_preferred_provider_kind or ""),
                        preferred_model=str(llm_preferred_model or ""),
                    ),
                )
                drain_attempts = getattr(self.writer.llm_client, "drain_llm_attempt_events", None)
                failed_attempts = drain_attempts() if callable(drain_attempts) else []
                if failed_attempts:
                    self._save_prompt_trace_payload(
                        session=updater.session,
                        updater=updater,
                        project_id=project_id,
                        prompt_trace={
                            "trace_scope": "writer",
                            "stage_key": trace_stage_key,
                            "template_id": "writer:failure",
                            "template_version": "v1",
                            "effective_system_prompt": "",
                            "prompt_layers": [],
                            "input_snapshot": {
                                "chapter_number": chapter_number,
                                "stage_key": trace_stage_key,
                                "failure_path": "writer_before_output",
                            },
                            "model_profile": {
                                "profile_id": model_profile_id,
                                "model": model_name,
                            },
                            "attempts": failed_attempts,
                            "output_summary": {
                                "status": "failed",
                                "chapter_number": chapter_number,
                                "error_class": exc.__class__.__name__,
                                "error_message": str(exc),
                            },
                        },
                        parent_trace_id="",
                        decision_event_id=str(getattr(failure_event, "id", "") or ""),
                    )
                last_failure_event_id = str(getattr(failure_event, "id", "") or "")
                last_failed_attempt = attempt
                self._record_failure_prompt_trace(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    context=context,
                    stage_key=trace_stage_key,
                    template_id="writer:failure",
                    source_event_id=last_failure_event_id,
                    exc=exc,
                    duration_ms=duration_ms,
                    attempts=llm_attempts,
                    skill_layers=writer_skill_layers,
                )
                if self._is_timeout_like(exc):
                    logger.warning(
                        "Writer timeout detected for chapter %d; skipping extra retries.",
                        chapter_number,
                    )
                    break
                if is_transient and attempt < max_attempts:
                    delay = self._transient_retry_delay(attempt)
                    logger.warning(
                        "Transient LLM failure detected for chapter %d; waiting %.1f s before writer retry %d/%d.",
                        chapter_number,
                        delay,
                        attempt + 1,
                        max_attempts,
                    )
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_number,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.RETRY_ATTEMPT,
                        scope="chapter",
                        summary=f"第{chapter_number}章准备进行 writer retry。",
                        payload=audit_payload(
                            stage="writing_chapter",
                            status="retry_scheduled",
                            operation_id=self._audit_operation_id(),
                            attempt_no=attempt + 1,
                            previous_attempt=attempt,
                            delay_seconds=delay,
                            model_profile_id=model_profile_id,
                            model=model_name,
                            preferred_provider_kind=str(llm_preferred_provider_kind or ""),
                            preferred_model=str(llm_preferred_model or ""),
                        ),
                    )
                    time.sleep(delay)
        if last_error is not None:
            preview_started_at = time.perf_counter()
            preview_max_attempts = 3 if saw_transient_error else 2
            preview_timeout_seconds = self.writer.single_call_timeout_seconds
            preview_started_event = self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.WRITER_PREVIEW_FALLBACK_STARTED,
                scope="chapter",
                summary=f"第{chapter_number}章 writer preview fallback 已开始。",
                parent_event_id=last_failure_event_id,
                payload=audit_payload(
                    stage="chapter_preview_fallback",
                    status="started",
                    operation_id=self._audit_operation_id(),
                    source_error_class=last_error.__class__.__name__,
                    source_error_message=safe_error_summary(last_error),
                    source_attempt_no=last_failed_attempt,
                    max_attempts=preview_max_attempts,
                    timeout_seconds=preview_timeout_seconds,
                ),
            )
            try:
                preview_output = self._call_with_compatible_kwargs(
                    self.writer.write_preview_chapter,
                    context,
                    skill_layers=writer_skill_layers,
                    trace_stage_key="writer_preview_fallback",
                    timeout_seconds=preview_timeout_seconds,
                    max_attempts=preview_max_attempts,
                    retry_on_timeout=False,
                )
                preview_output.generation_meta.update(
                    {
                        "fallback_from_writer_error": True,
                        "writer_fallback_error": str(last_error),
                    }
                )
                fallback_summary = self._prompt_trace_success_summary(preview_output)
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.WRITER_PREVIEW_FALLBACK_SUCCEEDED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer preview fallback 成功。",
                    parent_event_id=last_failure_event_id,
                    payload=audit_payload(
                        stage="chapter_preview_fallback",
                        status="succeeded",
                        operation_id=self._audit_operation_id(),
                        source_error_class=last_error.__class__.__name__,
                        source_error_message=safe_error_summary(last_error),
                        source_attempt_no=last_failed_attempt,
                        fallback_attempt_no=fallback_summary.get("successful_attempt_no", 0),
                        max_attempts=preview_max_attempts,
                        timeout_seconds=preview_timeout_seconds,
                        duration_ms=max(0, int((time.perf_counter() - preview_started_at) * 1000)),
                        char_count=int(getattr(preview_output, "char_count", 0) or 0),
                        **fallback_summary,
                    ),
                )
                logger.warning(
                    "Writer preview fallback succeeded for chapter %d after writer failure: %s",
                    chapter_number,
                    last_error,
                )
                return preview_output
            except Exception as preview_exc:  # noqa: BLE001
                preview_duration_ms = max(0, int((time.perf_counter() - preview_started_at) * 1000))
                preview_attempts = self._safe_prompt_trace_attempts(
                    self._drain_llm_attempt_events(),
                    fallback_attempt_no=0,
                    exc=preview_exc,
                    duration_ms=preview_duration_ms,
                )
                preview_error_category = self._error_category_from_attempts(preview_attempts, preview_exc)
                preview_failure_event = self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.WRITER_PREVIEW_FALLBACK_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer preview fallback 失败。",
                    parent_event_id=str(getattr(preview_started_event, "id", "") or last_failure_event_id),
                    payload=event_error_payload(
                        preview_exc,
                        stage="chapter_preview_fallback",
                        operation_id=self._audit_operation_id(),
                        duration_ms=preview_duration_ms,
                        error_category=preview_error_category,
                        source_error_class=last_error.__class__.__name__,
                        source_error_message=safe_error_summary(last_error),
                        source_attempt_no=last_failed_attempt,
                        max_attempts=preview_max_attempts,
                        timeout_seconds=preview_timeout_seconds,
                        attempt_count=len(preview_attempts),
                        attempt_group_ids=attempt_group_ids(preview_attempts),
                    ),
                )
                self._record_failure_prompt_trace(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    context=context,
                    stage_key=trace_stage_key,
                    template_id="writer:preview_failure",
                    source_event_id=str(getattr(preview_failure_event, "id", "") or ""),
                    exc=preview_exc,
                    duration_ms=preview_duration_ms,
                    attempts=preview_attempts,
                    skill_layers=writer_skill_layers,
                    fallback_stage="chapter_preview_fallback",
                )
                logger.warning(
                    "Writer preview fallback failed for chapter %d: %s",
                    chapter_number,
                    preview_exc,
                )
                last_error = preview_exc
        if self.config.operation_mode == "blackbox" and self.config.freeze_failed_candidates:
            frozen_path = self.artifact_store.save_frozen_candidate(
                project_id=project_id,
                chapter_number=chapter_number,
                payload={
                    "reason": "writer-failed-without-draft",
                    "chapter_number": chapter_number,
                    "project_id": project_id,
                    "error": str(last_error) if last_error else "writer failed",
                },
            )
            if frozen_path:
                frozen_artifacts.append(frozen_path)
        if last_error is not None and saw_transient_error:
            raise TransientLLMChapterFailure(str(last_error), cause=last_error) from last_error
        raise last_error or RuntimeError("writer failed")

    @staticmethod
    def _is_timeout_like(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            token in message
            for token in ("timed out", "timeout", "read operation timed out")
        )

    @staticmethod
    def _is_transient_llm_like(exc: Exception) -> bool:
        current: BaseException | None = exc
        while current is not None:
            message = str(current).lower()
            compact = " ".join(message.split())
            if any(
                token in compact
                for token in (
                    "http 529",
                    "status code 529",
                    "529 unknown status code",
                    "429",
                    "500",
                    "502",
                    "503",
                    "504",
                    "temporarily unavailable",
                    "service unavailable",
                    "rate limit",
                    "too many requests",
                    "overloaded",
                    "connection reset",
                    "connection refused",
                    "remoteprotocolerror",
                    "server disconnected",
                    "network error",
                    "timed out",
                    "timeout",
                    "read operation timed out",
                )
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _transient_retry_delay(attempt: int) -> float:
        return min(20.0, 3.0 * (2 ** max(0, attempt - 1)))

    def _current_model_identity(self) -> tuple[str, str]:
        profile_id = ""
        profile = getattr(self.config, "llm_fallback_profiles", None) or []
        primary = profile[0] if isinstance(profile, list) and profile else {}
        if isinstance(primary, dict):
            profile_id = str(primary.get("id", "")).strip()
        return profile_id, str(self.config.minimax_model or "").strip()

    def _audit_operation_id(self) -> str:
        return str(self._governance_task_id or self._governance_root_event_id or "").strip()

    def _drain_llm_attempt_events(self) -> list[dict[str, object]]:
        drain = getattr(getattr(self.writer, "llm_client", None), "drain_llm_attempt_events", None)
        if not callable(drain):
            return []
        events = drain()
        return [dict(item) for item in events if isinstance(item, dict)] if isinstance(events, list) else []

    @staticmethod
    def _safe_prompt_trace_attempts(
        attempts: list[dict[str, object]],
        *,
        fallback_attempt_no: int = 0,
        exc: BaseException | None = None,
        duration_ms: int = 0,
    ) -> list[dict[str, object]]:
        allowed_keys = {
            "attempt_group_id",
            "profile_id",
            "profile_name",
            "model",
            "preferred_provider_kind",
            "preferred_model",
            "base_url_host",
            "requested_temperature",
            "requested_max_tokens",
            "timeout_seconds",
            "attempt_no",
            "http_status",
            "provider_request_id",
            "duration_ms",
            "input_chars",
            "output_chars",
            "task_family",
            "stage_key",
            "llm_task_route",
            "retry_after",
            "sleep_ms",
            "error_class",
            "error_message",
            "error_category",
            "timeout_kind",
            "retryable",
            "fallback_eligible",
            "final_failure",
        }
        safe_attempts: list[dict[str, object]] = []
        for attempt in attempts:
            safe: dict[str, object] = {
                key: value
                for key, value in attempt.items()
                if key in allowed_keys and value is not None
            }
            if "error_message" in safe:
                safe["error_message"] = safe_error_summary(str(safe.get("error_message") or ""))
            safe_attempts.append(safe)
        if not safe_attempts and exc is not None:
            safe_attempts.append(
                {
                    "attempt_no": int(fallback_attempt_no or 0),
                    "duration_ms": max(0, int(duration_ms or 0)),
                    "error_class": exc.__class__.__name__,
                    "error_message": safe_error_summary(exc),
                    "error_category": "unknown",
                    "final_failure": True,
                }
            )
        return safe_attempts

    @staticmethod
    def _error_category_from_attempts(attempts: list[dict[str, object]], exc: BaseException) -> str:
        for attempt in reversed(attempts):
            category = str(attempt.get("error_category") or "").strip()
            if category and category != "unknown":
                return category
        message = str(exc).lower()
        if "timeout" in message or "timed out" in message:
            return "timeout"
        if "429" in message or "rate limit" in message:
            return "rate_limit"
        if any(token in message for token in ("529", "500", "502", "503", "504", "overload")):
            return "provider_overload"
        if "400" in message or "bad request" in message:
            return "bad_request"
        if "parse" in message or "json" in message or "schema" in message:
            return "parse_or_schema"
        return "unknown"

    @staticmethod
    def _diagnostic_kind_for_failure(exc: BaseException, error_category: str) -> str:
        message = str(exc).lower()
        if error_category == "bad_request" or "400" in message or "bad request" in message:
            return "provider_bad_request"
        if error_category == "parse_or_schema" or any(
            token in message for token in ("parse", "json", "schema")
        ):
            return "parse_or_schema_failure"
        return "writer_failure_without_draft"

    def _record_failure_prompt_trace(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        context,
        stage_key: str,
        template_id: str,
        source_event_id: str,
        exc: BaseException,
        duration_ms: int,
        attempts: list[dict[str, object]],
        skill_layers: list[object] | None,
        fallback_stage: str = "",
    ) -> str:
        if not isinstance(updater, StateUpdater):
            return ""
        fallback_attempt_no = 0
        if attempts:
            try:
                fallback_attempt_no = int(attempts[-1].get("attempt_no") or 0)
            except (TypeError, ValueError):
                fallback_attempt_no = 0
        safe_attempts = self._safe_prompt_trace_attempts(
            attempts,
            fallback_attempt_no=fallback_attempt_no,
            exc=exc,
            duration_ms=duration_ms,
        )
        error_category = self._error_category_from_attempts(safe_attempts, exc)
        selected_skills = ChapterWriter._selected_skills_from_layers(skill_layers)
        operation_id = self._audit_operation_id()
        model_profile_id, model_name = self._current_model_identity()
        trace_payload = {
            "trace_scope": "writer",
            "stage_key": stage_key,
            "template_id": template_id,
            "template_version": "v1",
            "effective_system_prompt": "",
            "prompt_layers": [],
            "input_snapshot": audit_payload(
                stage=stage_key,
                status="failed",
                operation_id=operation_id,
                chapter_number=chapter_number,
                writer_mode=str(getattr(self.writer, "writer_mode", "") or ""),
                selected_skills=selected_skills,
            ),
            "model_profile": {
                "profile_id": model_profile_id,
                "model": model_name,
                "base_url": str(getattr(self.llm_client, "base_url", "") or ""),
            },
            "attempts": safe_attempts,
            "output_summary": audit_payload(
                stage=stage_key,
                status="failed",
                operation_id=operation_id,
                duration_ms=duration_ms,
                error_category=error_category,
                chapter_number=chapter_number,
                context_chapter_number=int(getattr(context, "chapter_number", chapter_number) or chapter_number),
                error_class=exc.__class__.__name__,
                error_summary=safe_error_summary(exc),
                fallback_stage=fallback_stage,
                attempt_count=len(safe_attempts),
                attempt_group_ids=attempt_group_ids(safe_attempts),
            ),
        }
        trace_id = self._save_prompt_trace_payload(
            session=updater.session,
            updater=updater,
            project_id=project_id,
            prompt_trace=trace_payload,
            decision_event_id=source_event_id,
        )
        artifact_manifest: list[dict[str, object]] = []
        try:
            manifest = self.artifact_store.save_observability_diagnostic(
                project_id=project_id,
                chapter_number=chapter_number,
                kind=self._diagnostic_kind_for_failure(exc, error_category),
                source_event_id=source_event_id,
                trace_id=trace_id,
                payload={
                    "schema_version": "v4.5.1-audit",
                    "project_id": project_id,
                    "chapter_number": chapter_number,
                    "stage": stage_key,
                    "status": "failed",
                    "operation_id": operation_id,
                    "error_class": exc.__class__.__name__,
                    "error_summary": safe_error_summary(exc),
                    "error_category": error_category,
                    "attempts": safe_attempts,
                    "selected_skills": selected_skills,
                },
            )
            artifact_manifest.append(manifest)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to persist observability diagnostic artifact.", exc_info=True)
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.PROMPT_TRACE_RECORDED,
            scope="chapter",
            summary=f"第{chapter_number}章失败 prompt trace 已落盘。",
            parent_event_id=source_event_id,
            related_object_type="prompt_trace",
            related_object_id=trace_id,
            payload=audit_payload(
                stage=stage_key,
                status="failed",
                operation_id=operation_id,
                duration_ms=duration_ms,
                error_category=error_category,
                trace_id=trace_id,
                source_event_id=source_event_id,
                artifact_manifest=artifact_manifest,
            ),
        )
        return trace_id

    def _record_model_fallback_payloads(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        parent_stage: str,
        events: list[dict[str, Any]],
    ) -> None:
        for item in events:
            if not isinstance(item, dict):
                continue
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.FALLBACK_PROFILE_SWITCHED,
                scope="chapter",
                summary=(
                    f"writer fallback: {str(item.get('from_model') or '-')} -> "
                    f"{str(item.get('to_model') or '-')}"
                ),
                payload=audit_payload(
                    stage=parent_stage,
                    status="profile_switched",
                    operation_id=self._audit_operation_id(),
                    model_profile_id=str(item.get("to_profile_id") or ""),
                    model=str(item.get("to_model") or ""),
                    error_summary=safe_error_summary(str(item.get("reason") or "")),
                    from_model_profile_id=str(item.get("from_profile_id") or ""),
                    from_model=str(item.get("from_model") or ""),
                ),
            )

    def _apply_canon_quality_gate(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
    ) -> str:
        latest_draft, latest_review = self._latest_draft_and_review_for_chapter(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        )
        draft_id = str(getattr(latest_draft, "id", "") or "")
        review_id = str(getattr(latest_review, "id", "") or "")
        analysis = analyze_writer_output_quality(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output,
            draft_id=draft_id,
            persist=True,
            temporal_reconciler=LLMTemporalReconciler(self.llm_client) if self.llm_client is not None else None,
        )
        deferred_acceptance_errors = self._prepare_deferred_acceptance_if_needed(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            review_id=review_id,
            verdict=verdict,
            signals=analysis.signals,
            target_total_chapters=int(getattr(session.get(Project, project_id), "target_total_chapters", 0) or 0),
        )
        if deferred_acceptance_errors:
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
                scope="chapter",
                summary=f"第{chapter_number}章 deferred acceptance 计划补丁失败。",
                reason=";".join(deferred_acceptance_errors),
                payload={"deferred_acceptance_errors": deferred_acceptance_errors},
            )
            return "deferred-acceptance-blocked"
        obligation_repo = NarrativeObligationRepository(session)
        gate_obligations = [
            *obligation_repo.list_active_for_context(project_id, chapter_number=chapter_number),
            *obligation_repo.list_planned_for_chapter(project_id, origin_chapter_number=chapter_number),
        ]
        patch_ids = sorted(
            {
                patch_id
                for obligation in gate_obligations
                for patch_id in obligation.linked_plan_patch_ids
                if patch_id
            }
        )
        project = session.get(Project, project_id)
        target_total_chapters = int(getattr(project, "target_total_chapters", 0) or 0)
        gate_result = evaluate_canon_admission(
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            review_id=review_id,
            review_verdict=verdict.verdict,
            signals=analysis.signals,
            obligations=gate_obligations,
            plan_patches=obligation_repo.list_patches_by_ids(patch_ids),
            mode=str(getattr(self.config, "canon_quality_gate", "strict") or "strict"),
            is_final_chapter=bool(target_total_chapters and chapter_number >= target_total_chapters),
        )
        CanonQualityRepository(session).save_admission_run(gate_result, signals=analysis.signals)
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.REVIEW_VERDICT_RECORDED,
            scope="chapter",
            summary=f"第{chapter_number}章 canon quality gate: {gate_result.verdict}",
            related_object_type="canon_admission_run",
            payload=gate_result.model_dump(mode="json"),
        )
        if gate_result.commit_allowed:
            return ""
        frozen_path = ""
        if self.config.freeze_failed_candidates:
            frozen_path = self.artifact_store.save_frozen_candidate(
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
                },
            )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
            scope="chapter",
            summary=f"第{chapter_number}章 canon quality gate 阻止 canon 写入。",
            reason=gate_result.gate_summary,
            payload=gate_result.model_dump(mode="json"),
        )
        return frozen_path or "canon-quality-gate-blocked"

    def _prepare_deferred_acceptance_if_needed(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        draft_id: str,
        review_id: str,
        verdict: ReviewVerdict,
        signals: list[Any],
        target_total_chapters: int,
    ) -> list[str]:
        outcome = ReviewOutcomeRouter().route(
            review=verdict,
            signals=signals,
            current_chapter=chapter_number,
            target_total_chapters=target_total_chapters,
        )
        if outcome.action not in {"defer_with_chapter_plan_patch", "defer_with_band_plan_patch"}:
            return []
        issue_type = str(outcome.primary_issue_class or "").strip()
        if not issue_type:
            return []
        existing = session.execute(
            select(NarrativeObligationRow)
            .where(
                NarrativeObligationRow.project_id == project_id,
                NarrativeObligationRow.origin_chapter_number == int(chapter_number or 0),
                NarrativeObligationRow.obligation_type == issue_type,
                NarrativeObligationRow.status.in_(("planned", "active")),
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return []

        bands = self._band_scope_candidates(
            session=session,
            project_id=project_id,
            current_chapter=chapter_number,
        )
        scope_decision = ObligationScopeRouter().route(
            issue_type=issue_type,
            priority=_priority_for_deferred_issue(issue_type),
            current_chapter=chapter_number,
            target_total_chapters=target_total_chapters,
            bands=bands,
        )
        if scope_decision.action not in {"defer_with_chapter_plan_patch", "defer_with_band_plan_patch"}:
            return [scope_decision.reason or f"deferred_acceptance_scope_unavailable:{issue_type}"]

        obligation_id = new_id()
        summary = _summary_for_deferred_issue(verdict=verdict, issue_type=issue_type, outcome_reason=outcome.reason)
        payoff_test = _payoff_test_for_deferred_issue(
            verdict=verdict,
            issue_type=issue_type,
            deadline_chapter=scope_decision.deadline_chapter,
            summary=summary,
        )
        obligation = NarrativeObligation(
            id=obligation_id,
            project_id=project_id,
            origin_chapter_number=int(chapter_number or 0),
            origin_draft_id=draft_id,
            origin_review_id=review_id,
            origin_signal_ids=[
                str(getattr(signal, "signal_id", "") or "")
                for signal in signals
                if str(getattr(signal, "signal_type", "") or "") == issue_type
                and str(getattr(signal, "signal_id", "") or "")
            ],
            obligation_type=issue_type,
            priority=_priority_for_deferred_issue(issue_type),
            status="proposed",
            summary=summary,
            deferral_reason=scope_decision.reason or outcome.reason,
            hardness="design_debt",
            deadline_chapter=int(scope_decision.deadline_chapter or 0),
            payoff_test=payoff_test,
            evidence_refs=[f"review:{review_id}"] if review_id else [],
            metadata={"minimum_scope": scope_decision.target_scope},
        )

        if scope_decision.action == "defer_with_band_plan_patch":
            band_row = self._band_row_by_id(
                session=session,
                project_id=project_id,
                band_id=scope_decision.target_band_id,
            )
            if band_row is None:
                return [f"target_band_not_found:{scope_decision.target_band_id}"]
            plan_patch = BandPlanPatcher().build_obligation_patch(
                project_id=project_id,
                band_row=band_row,
                obligations=[obligation],
                current_chapter=chapter_number,
                patch_type="band_defer_acceptance",
            )
        else:
            target_plan = session.execute(
                select(ChapterPlan)
                .where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_number == int(scope_decision.deadline_chapter or 0),
                    ChapterPlan.status.in_(("planned", "failed")),
                )
                .limit(1)
            ).scalar_one_or_none()
            if target_plan is None:
                return [f"target_chapter_plan_not_found:{scope_decision.deadline_chapter}"]
            plan_patch = NarrativePlanPatch(
                id=new_id(),
                project_id=project_id,
                patch_type="defer_acceptance",
                target_scope="chapter",
                target_plan_id=str(target_plan.id or ""),
                target_arc_id=str(target_plan.arc_plan_id or ""),
                affected_chapters=[int(scope_decision.deadline_chapter or 0)],
                source_obligation_ids=[obligation_id],
                new_contract={
                    "obligations_to_resolve": [obligation_id],
                    "payoff_test": payoff_test,
                    "summary": summary,
                },
                diff_summary=f"Bind deferred obligation {obligation_id} to chapter {scope_decision.deadline_chapter}.",
                writer_context_injections=[
                    {
                        "type": "narrative_obligation",
                        "obligation_id": obligation_id,
                        "priority": obligation.priority,
                        "summary": summary,
                        "payoff_test": payoff_test,
                        "deadline_chapter": obligation.deadline_chapter,
                    }
                ],
                reviewer_context_injections=[
                    {
                        "type": "narrative_obligation",
                        "obligation_id": obligation_id,
                        "payoff_test": payoff_test,
                        "must_resolve_now": True,
                    }
                ],
                expected_resolution_tests=[payoff_test],
            )
        result = DeferAcceptanceTransaction(session).run(
            obligation=obligation,
            plan_patch=plan_patch,
            current_chapter=chapter_number,
            target_total_chapters=target_total_chapters,
        )
        return [] if result.success else list(result.errors)

    @staticmethod
    def _band_scope_candidates(
        *,
        session: Session,
        project_id: str,
        current_chapter: int,
    ) -> list[BandScopeCandidate]:
        rows = session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.chapter_end > int(current_chapter or 0),
            )
            .order_by(BandExperiencePlan.chapter_start.asc(), BandExperiencePlan.chapter_end.asc())
        ).scalars().all()
        if not rows:
            return []
        plans = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number > int(current_chapter or 0),
                ChapterPlan.status.in_(("planned", "failed")),
            )
        ).scalars().all()
        planned_numbers = [int(plan.chapter_number or 0) for plan in plans]
        result: list[BandScopeCandidate] = []
        for row in rows:
            start = int(row.chapter_start or 0)
            end = int(row.chapter_end or 0)
            result.append(
                BandScopeCandidate(
                    band_id=str(row.band_id or ""),
                    arc_id=str(row.arc_id or ""),
                    chapter_start=start,
                    chapter_end=end,
                    planned_chapters=[number for number in planned_numbers if start <= number <= end],
                )
            )
        return result

    @staticmethod
    def _band_row_by_id(
        *,
        session: Session,
        project_id: str,
        band_id: str,
    ) -> BandExperiencePlan | None:
        return session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.band_id == band_id,
            )
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _latest_draft_and_review_for_chapter(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
    ) -> tuple[ChapterDraft | None, ChapterReview | None]:
        chapter_plan = session.execute(
            select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
        ).scalar_one_or_none()
        if chapter_plan is None:
            return None, None
        latest_draft = session.execute(
            select(ChapterDraft)
            .where(ChapterDraft.chapter_plan_id == chapter_plan.id)
            .order_by(ChapterDraft.version.desc(), ChapterDraft.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest_draft is None:
            return None, None
        latest_review = session.execute(
            select(ChapterReview)
            .where(ChapterReview.draft_id == latest_draft.id)
            .order_by(ChapterReview.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return latest_draft, latest_review

    def _apply_canon_candidate(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
    ) -> str | None:
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.CANON_COMMIT_STARTED,
            scope="chapter",
            summary=f"第{chapter_number}章 canon 写入开始。",
            payload={
                "state_changes_count": len(getattr(writer_output, "state_changes", []) or []),
                "events_count": len(getattr(writer_output, "new_events", []) or []),
                "thread_beats_count": len(getattr(writer_output, "thread_beats", []) or []),
            },
        )
        try:
            quality_blocked_path = self._apply_canon_quality_gate(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
            )
            if quality_blocked_path:
                return quality_blocked_path
            v4_blocked_path = self._apply_world_v4_gate(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
            )
            if v4_blocked_path:
                return v4_blocked_path
            self._validate_subworld_admission(
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
            )
            self._ensure_genesis_canon_seed_entities(
                session=session,
                repo=repo,
                updater=updater,
                project_id=project_id,
            )
            filtered_state_changes = self._filter_supported_state_changes(
                writer_output.state_changes
            )
            filtered_state_changes = self._filter_resolvable_state_changes(
                repo,
                project_id,
                chapter_number,
                filtered_state_changes,
            )
            updater.apply_state_changes(
                project_id, chapter_number, filtered_state_changes
            )
            filtered_events = self._filter_resolvable_events(
                repo,
                project_id,
                chapter_number,
                writer_output.new_events,
            )
            updater.apply_events(
                project_id, chapter_number, filtered_events
            )
            updater.apply_thread_beats(
                project_id, chapter_number, writer_output.thread_beats
            )
            if writer_output.time_advance:
                updater.apply_time_advance(
                    project_id, chapter_number, writer_output.time_advance
                )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="business_event",
                event_type=DecisionEventType.CANON_COMMIT,
                scope="chapter",
                summary=f"第{chapter_number}章 canon 写入成功。",
                payload={"issue_count": len(verdict.issues)},
            )
            NarrativeObligationRepository(session).activate_planned_for_chapter(
                project_id,
                origin_chapter_number=chapter_number,
            )
            CandidateDraftRepository(session).mark_canon_committed(
                project_id=project_id,
                chapter_number=chapter_number,
            )
            return None
        except Exception as exc:
            logger.exception(
                "Canon update failed for chapter %d; keeping saved draft and review.",
                chapter_number,
            )
            frozen_path = ""
            if self.config.freeze_failed_candidates:
                frozen_path = self.artifact_store.save_frozen_candidate(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    payload={
                        "reason": "canon-update-failed",
                        "chapter_number": chapter_number,
                        "writer_output": writer_output.model_dump(mode="json"),
                        "review_verdict": verdict.model_dump(mode="json"),
                    },
                )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.CANON_COMMIT_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 canon 写入失败。",
            )
            session.rollback()
            CandidateDraftRepository(session).mark_canon_failed(
                project_id=project_id,
                chapter_number=chapter_number,
                failure_reason=str(exc),
                canon_artifact_path=frozen_path,
            )
            return frozen_path or None

    @staticmethod
    def _prompt_trace_success_summary(writer_output: WriterOutput) -> dict[str, object]:
        generation_meta = getattr(writer_output, "generation_meta", {}) or {}
        prompt_trace = generation_meta.get("prompt_trace") if isinstance(generation_meta, dict) else {}
        attempts = prompt_trace.get("attempts", []) if isinstance(prompt_trace, dict) else []
        if not isinstance(attempts, list):
            attempts = []
        successful = None
        for item in attempts:
            if not isinstance(item, dict):
                continue
            if int(item.get("output_chars") or 0) > 0 and not str(item.get("error_class") or ""):
                successful = item
        if successful is None and attempts:
            successful = next((item for item in reversed(attempts) if isinstance(item, dict)), None)
        if not isinstance(successful, dict):
            return {
                "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
                "effective_model": "",
                "effective_profile_id": "",
                "successful_attempt_no": 0,
                "attempt_group_id": "",
                "output_chars": int(getattr(writer_output, "char_count", 0) or 0),
                "fallback_chain": generation_meta.get("model_fallbacks", []),
            }
        return {
            "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
            "effective_model": str(successful.get("model") or ""),
            "effective_profile_id": str(successful.get("profile_id") or ""),
            "effective_profile_name": str(successful.get("profile_name") or ""),
            "successful_attempt_no": int(successful.get("attempt_no") or 0),
            "attempt_group_id": str(successful.get("attempt_group_id") or ""),
            "output_chars": int(successful.get("output_chars") or getattr(writer_output, "char_count", 0) or 0),
            "fallback_chain": generation_meta.get("model_fallbacks", []),
        }

    def _apply_world_v4_gate(
        self,
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
    ) -> str | None:
        chapter_intent = WorldContractRepository(session).get_chapter_intent(
            project_id,
            chapter_number,
        )
        writer_output_for_direct = writer_output.model_copy(update={"project_id": project_id})
        broker = RetrievalBroker()
        writer_pack = broker.build_world_model_pack(
            repo,
            project_id,
            chapter_number,
            "writing",
        )
        review_pack = broker.build_world_model_pack(
            repo,
            project_id,
            chapter_number,
            "review",
        )
        compiler_pack = broker.build_world_model_pack(
            repo,
            project_id,
            chapter_number,
            "compiler",
        )
        retrieval_pack_payload = {
            "writing": writer_pack.model_dump(mode="json"),
            "review": review_pack.model_dump(mode="json"),
            "compiler": compiler_pack.model_dump(mode="json"),
        }
        extraction = BookStateGraphDeltaExtractor().extract(
            BookStateExtractionRequest(
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output_for_direct,
                chapter_intent=chapter_intent,
                review_verdict_id=f"book_state_direct_review_{project_id}_{chapter_number}",
            )
        )
        gate_verdict = extraction.compatibility_gate_verdict
        if not extraction.accepted or extraction.changes is None:
            frozen_path = ""
            if self.config.freeze_failed_candidates:
                frozen_path = self.artifact_store.save_frozen_candidate(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    payload={
                        "reason": "book-state-direct-extraction-blocked",
                        "chapter_number": chapter_number,
                        "writer_output": writer_output.model_dump(mode="json"),
                        "review_verdict": verdict.model_dump(mode="json"),
                        "book_state_extraction": extraction.model_dump(mode="json"),
                        "v4_retrieval_packs": retrieval_pack_payload,
                    },
                )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.CANON_COMMIT_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 BookState direct extraction 阻止 canon 写入。",
                payload={
                    "book_state_extraction_issues": [
                        issue.model_dump(mode="json") for issue in extraction.issues
                    ],
                    "extraction_path": "book_state_direct",
                },
            )
            return frozen_path or "book-state-direct-extraction-blocked"

        book_state_changes = extraction.changes
        if not book_state_changes.graph_deltas:
            return None
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.BOOK_STATE_REVIEW_STARTED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState review gate 开始。",
            payload=audit_payload(
                stage="book_state_review",
                status="started",
                operation_id=self._audit_operation_id(),
                graph_delta_count=len(book_state_changes.graph_deltas),
                extraction_path="book_state_direct",
            ),
        )
        commit_service = BookStateDirectCommitService(session)
        book_state_verdict = commit_service.review(book_state_changes)
        if not book_state_verdict.accepted or book_state_verdict.approved_changes is None:
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.BOOK_STATE_REVIEW_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 BookState review gate 未通过。",
                payload=audit_payload(
                    stage="book_state_review",
                    status="failed",
                    operation_id=self._audit_operation_id(),
                    issue_count=len(book_state_verdict.issues),
                    issues=[issue.model_dump(mode="json") for issue in book_state_verdict.issues],
                    extraction_path="book_state_direct",
                ),
            )
            frozen_path = ""
            if self.config.freeze_failed_candidates:
                frozen_path = self.artifact_store.save_frozen_candidate(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    payload={
                        "reason": "book-state-review-gate-blocked",
                        "chapter_number": chapter_number,
                        "writer_output": writer_output.model_dump(mode="json"),
                        "book_state_review": book_state_verdict.model_dump(mode="json"),
                        "book_state_extraction": extraction.model_dump(mode="json"),
                    },
                )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.CANON_COMMIT_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 BookState review gate 阻止 canon 写入。",
                payload={
                    "book_state_review_issues": [
                        issue.model_dump(mode="json") for issue in book_state_verdict.issues
                    ],
                },
            )
            return frozen_path or "book-state-review-gate-blocked"

        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.BOOK_STATE_REVIEW_SUCCEEDED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState review gate 通过。",
            payload=audit_payload(
                stage="book_state_review",
                status="succeeded",
                operation_id=self._audit_operation_id(),
                issue_count=len(book_state_verdict.issues),
                extraction_path="book_state_direct",
            ),
        )

        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.BOOK_STATE_COMPILE_STARTED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState compile 开始。",
            payload=audit_payload(
                stage="book_state_compile",
                status="started",
                operation_id=self._audit_operation_id(),
                graph_delta_count=len(book_state_verdict.approved_changes.graph_deltas),
                extraction_path="book_state_direct",
            ),
        )
        try:
            book_state_result = commit_service.compile_approved(
                book_state_verdict.approved_changes,
                compiler_run_id=f"book_state_compile_{project_id}_{chapter_number}",
            )
        except Exception as exc:
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.BOOK_STATE_COMPILE_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 BookState compile 异常失败。",
                reason=str(exc),
                payload=event_error_payload(
                    exc,
                    stage="book_state_compile",
                    operation_id=self._audit_operation_id(),
                ),
            )
            raise
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=(
                DecisionEventType.BOOK_STATE_COMPILE_SUCCEEDED
                if book_state_result.committed
                else DecisionEventType.BOOK_STATE_COMPILE_FAILED
            ),
            scope="chapter",
            summary=(
                f"第{chapter_number}章 BookState compile 完成。"
                if book_state_result.committed
                else f"第{chapter_number}章 BookState compile 未提交。"
            ),
            payload=audit_payload(
                stage="book_state_compile",
                status="succeeded" if book_state_result.committed else "failed",
                operation_id=self._audit_operation_id(),
                result=book_state_result.model_dump(mode="json"),
                extraction_path="book_state_direct",
            ),
        )
        if not book_state_result.committed:
            frozen_path = ""
            if self.config.freeze_failed_candidates:
                frozen_path = self.artifact_store.save_frozen_candidate(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    payload={
                        "reason": "book-state-compile-blocked",
                        "chapter_number": chapter_number,
                        "writer_output": writer_output.model_dump(mode="json"),
                        "book_state_review": book_state_verdict.model_dump(mode="json"),
                        "book_state_result": book_state_result.model_dump(mode="json"),
                        "book_state_extraction": extraction.model_dump(mode="json"),
                    },
                )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.CANON_COMMIT_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 BookState compile 阻止 canon 写入。",
                payload={"book_state_blocked_reasons": list(book_state_result.blocked_reasons)},
            )
            return frozen_path or "book-state-compile-blocked"

        if self.config.world_v4_compat_write_enabled and gate_verdict is not None:
            legacy_nested = session.begin_nested()
            try:
                compiler_result = WorldModelCompilerV4(session).compile_gate_verdict(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    verdict=gate_verdict,
                    compiler_run_id=f"compile_{project_id}_{chapter_number}",
                    retrieval_pack_payload=retrieval_pack_payload,
                )
                if compiler_result.committed:
                    legacy_nested.commit()
                else:
                    legacy_nested.rollback()
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_number,
                        event_family="runtime_observation",
                        event_type=DecisionEventType.LEGACY_PROJECTION_FAILED,
                        scope="chapter",
                        summary=f"第{chapter_number}章 world_model_v4 compatibility projection 未提交，BookState canon 已保留。",
                        payload=audit_payload(
                            stage="legacy_projection",
                            status="failed",
                            operation_id=self._audit_operation_id(),
                            result=compiler_result.model_dump(mode="json"),
                        ),
                    )
            except Exception as exc:
                legacy_nested.rollback()
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LEGACY_PROJECTION_FAILED,
                    scope="chapter",
                    summary=f"第{chapter_number}章 world_model_v4 compatibility projection 失败，BookState canon 已保留。",
                    reason=str(exc),
                    payload=event_error_payload(
                        exc,
                        stage="legacy_projection",
                        operation_id=self._audit_operation_id(),
                    ),
                )

        projection_refresh = KnowledgeProjectionRefresher(
            session,
            qdrant_url=self.config.qdrant_url,
            qdrant_collection=self.config.llm_kb_qdrant_collection,
        ).refresh(
            project_id,
            as_of_chapter=chapter_number,
            trigger="chapter_accepted",
        )
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.KNOWLEDGE_PROJECTION_REFRESHED,
            scope="chapter",
            summary=f"第{chapter_number}章 BookState projection refresh 完成。",
            payload=projection_refresh.as_dict(),
        )
        return None

    @staticmethod
    def _filter_resolvable_events(
        repo: StateRepository,
        project_id: str,
        chapter_number: int,
        events: list[EventCandidate],
    ) -> list[EventCandidate]:
        entity_lookup = repo.get_entities_by_names(
            project_id,
            [
                name
                for event in events
                for name in event.involved_entity_names
            ],
        )
        filtered: list[EventCandidate] = []
        for event in events:
            unknown_names = [
                name
                for name in event.involved_entity_names
                if entity_lookup.get(name) is None
            ]
            if unknown_names:
                logger.warning(
                    "Dropping event %r in chapter %d because entities are unknown: %s",
                    event.summary,
                    chapter_number,
                    ", ".join(unknown_names),
                )
                continue
            filtered.append(event)
        return filtered

    @staticmethod
    def _filter_resolvable_state_changes(
        repo: StateRepository,
        project_id: str,
        chapter_number: int,
        changes: list,
    ) -> list:
        character_names = [
            str(change.entity_name or "").strip()
            for change in changes
            if str(getattr(change, "entity_kind", "") or "") == "character"
            and str(change.entity_name or "").strip()
        ]
        entity_lookup = repo.get_entities_by_names(project_id, character_names)
        filtered: list = []
        for change in changes:
            entity_name = str(change.entity_name or "").strip()
            if str(getattr(change, "entity_kind", "") or "") == "character" and entity_name:
                if entity_lookup.get(entity_name) is None:
                    logger.warning(
                        "Dropping state change for unknown character %r in chapter %d.",
                        entity_name,
                        chapter_number,
                    )
                    continue
            filtered.append(change)
        return filtered

    @staticmethod
    def _ensure_genesis_canon_seed_entities(
        *,
        session: Session,
        repo: StateRepository,
        updater: StateUpdater,
        project_id: str,
    ) -> None:
        project = session.get(Project, project_id)
        if project is None:
            return
        revision_id = str(getattr(project, "active_genesis_revision_id", "") or "").strip()
        revision = session.get(BookGenesisRevision, revision_id) if revision_id else None
        if revision is None:
            return
        try:
            pack = json.loads(str(getattr(revision, "pack_json", "") or "{}"))
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(pack, dict):
            return
        world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
        story_engine = world.get("story_engine") if isinstance(world.get("story_engine"), dict) else {}
        seed_specs: list[tuple[str, str, dict]] = []
        for collection_key, entity_kind in (
            ("core_cast", "character"),
            ("characters", "character"),
            ("factions", "faction"),
            ("opposition", "character"),
        ):
            for item in story_engine.get(collection_key) or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("id") or "").strip()
                if not name or len(name) > 40:
                    continue
                seed_specs.append((entity_kind, name, item))
        for anchor in ContinuityChecker(repo)._canon_name_anchors(project_id):
            name = str(getattr(anchor, "canonical_name", "") or "").strip()
            role_label = str(getattr(anchor, "role_label", "") or "").strip()
            if not name or len(name) > 40:
                continue
            seed_specs.append(
                (
                    "character",
                    name,
                    {
                        "role": f"{role_label} canon name anchor" if role_label else "canon name anchor",
                        "aliases": [role_label] if role_label else [],
                    },
                )
            )
        if not seed_specs:
            return

        changed = False
        seen: set[tuple[str, str]] = set()
        for entity_kind, name, payload in seed_specs:
            key = (entity_kind, name)
            if key in seen:
                continue
            seen.add(key)
            existing = repo.get_entities_by_names(project_id, [name]).get(name)
            if existing is not None:
                continue
            aliases = [
                str(alias).strip()
                for alias in (payload.get("aliases") or [])
                if str(alias).strip()
            ]
            if "/" in name:
                aliases.extend(part.strip() for part in name.split("/") if part.strip() and part.strip() != name)
            description_parts = [
                str(payload.get(key_name) or "").strip()
                for key_name in ("role", "desire", "fear", "secret", "goal", "leverage")
                if str(payload.get(key_name) or "").strip()
            ]
            updater.create_entity(
                project_id=project_id,
                kind=entity_kind,
                name=name,
                description="；".join(description_parts),
                aliases=list(dict.fromkeys(aliases)),
                importance=8 if entity_kind == "character" else 7,
                chapter=0,
            )
            changed = True
        if changed:
            SubWorldManager().ensure_registry(session, project_id)

    @staticmethod
    def _collect_subworld_candidate_names(
        repo: StateRepository,
        project_id: str,
        writer_output: WriterOutput,
    ) -> set[str]:
        names: set[str] = set()
        maybe_event_names: set[str] = set()
        maybe_state_change_names: set[str] = set()
        absence_only_names = {
            name
            for change in writer_output.state_changes
            if change.entity_kind == "character"
            and ContinuityChecker._is_absence_only_state_change(change)
            for name in [ContinuityChecker._candidate_character_name(change.entity_name)]
            if name
        }
        for mention in getattr(writer_output, "entity_mentions", []):
            if (
                getattr(mention, "entity_kind", "") == "character"
                and bool(getattr(mention, "is_named", False))
                and bool(getattr(mention, "is_on_stage", True))
            ):
                entity_name = ContinuityChecker._candidate_character_name(
                    getattr(mention, "entity_name", "")
                )
                if entity_name and entity_name not in absence_only_names:
                    names.add(entity_name)
        for change in writer_output.state_changes:
            if (
                change.entity_kind == "character"
                and not ContinuityChecker._is_absence_only_state_change(change)
            ):
                entity_name = ContinuityChecker._candidate_character_name(change.entity_name)
                if not entity_name:
                    continue
                maybe_state_change_names.add(entity_name)
        for event in writer_output.new_events:
            for entity_name in event.involved_entity_names:
                normalized = ContinuityChecker._candidate_character_name(entity_name)
                if normalized and normalized not in absence_only_names:
                    maybe_event_names.add(normalized)
        for scene in writer_output.scene_outputs:
            for entity_name in scene.involved_entities:
                normalized = ContinuityChecker._candidate_character_name(entity_name)
                if normalized and normalized not in absence_only_names:
                    names.add(normalized)
        if maybe_event_names:
            resolved = repo.get_entities_by_names(project_id, sorted(maybe_event_names))
            for entity_name in maybe_event_names:
                entity = resolved.get(entity_name)
                if entity is not None and entity.kind == "character":
                    names.add(entity_name)
        if maybe_state_change_names:
            resolved = repo.get_entities_by_names(project_id, sorted(maybe_state_change_names))
            for entity_name in maybe_state_change_names:
                entity = resolved.get(entity_name)
                if entity is not None and entity.kind == "character":
                    names.add(entity_name)
        return {name for name in names if len(name) <= 12}

    def _validate_subworld_admission(
        self,
        *,
        repo: StateRepository,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
    ) -> None:
        allowed_names = {
            ContinuityChecker._normalize_character_reference(name)
            for name in repo.get_allowed_entity_names(project_id, chapter_number)
        }
        allowed_names.update(
            ContinuityChecker._normalize_character_reference(anchor.canonical_name)
            for anchor in ContinuityChecker(repo)._canon_name_anchors(project_id)
        )
        allowed_names.update(
            ContinuityChecker._normalize_character_reference(name)
            for name in self._project_character_names(repo, project_id)
        )
        if not allowed_names:
            return
        unknown = sorted(
            name
            for name in self._collect_subworld_candidate_names(repo, project_id, writer_output)
            if name not in allowed_names
        )
        if unknown:
            raise ValueError(
                "Subworld admission rejected chapter "
                f"{chapter_number}: {', '.join(unknown)}"
            )

    def _run_phase3_pass(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
    ) -> None:
        stage = self.stage_analyzer.analyze(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        )
        pacing = self.pacing_strategist.analyze(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        )
        save_stage_analysis(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            stage=stage,
            pacing=pacing,
        )
        self.replan_governor.apply_if_needed(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            stage=stage,
            pacing=pacing,
        )
        self.arc_envelope_manager.ensure_active_arc_resolution(
            session=session,
            project_id=project_id,
            activation_chapter=chapter_number + 1,
        )
        self.arc_envelope_manager.record_provisional_promotion(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            reason="accepted-into-canon",
        )
        intents = self.npc_intent_generator.generate(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        )
        self._flush_background_llm_trace(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            stage_key="npc_intents",
            trace_scope="phase4",
        )
        save_npc_intents(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            intents=intents,
        )
        world_turn = self.world_simulator.simulate(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        )
        self._flush_background_llm_trace(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            stage_key="world_pressure",
            trace_scope="phase4",
        )
        save_world_turn(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            turn=world_turn,
        )
        # Phase B: windowed signal aggregation + cooldown filter
        run_feedback_aggregation_pass(
            session,
            project_id,
            chapter_number,
            cooldown_chapters=self.config.feedback_cooldown_chapters,
            comment_to_reader_ratio=self.config.comment_to_reader_ratio,
        )

    def _flush_background_llm_trace(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        stage_key: str,
        trace_scope: str,
    ) -> str:
        drain_attempts = getattr(self.llm_client, "drain_llm_attempt_events", None)
        attempts = drain_attempts() if callable(drain_attempts) else []
        if not attempts:
            return ""
        return self._save_prompt_trace_payload(
            session=session,
            updater=StateUpdater(session),
            project_id=project_id,
            prompt_trace={
                "trace_scope": trace_scope,
                "stage_key": stage_key,
                "template_id": f"{trace_scope}:{stage_key}",
                "template_version": "v1",
                "effective_system_prompt": "",
                "prompt_layers": [],
                "input_snapshot": {
                    "project_id": project_id,
                    "chapter_number": chapter_number,
                    "stage_key": stage_key,
                },
                "model_profile": {
                    "profile_id": getattr(self.llm_client, "profile_id", ""),
                    "profile_name": getattr(self.llm_client, "profile_name", ""),
                    "model": getattr(self.llm_client, "model", ""),
                    "base_url": getattr(self.llm_client, "base_url", ""),
                },
                "attempts": attempts,
                "output_summary": {
                    "status": "recorded",
                    "chapter_number": chapter_number,
                },
            },
        )

    def _compile_world_model_after_acceptance(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
    ) -> bool:
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.WORLD_MODEL_COMPILE_STARTED,
            scope="chapter",
            summary=f"第{chapter_number}章 WorldModel compile 开始。",
        )
        try:
            snapshot = LegacyWorldModelCompiler(session).compile_after_chapter(project_id, chapter_number)
        except Exception as exc:
            logger.exception("WorldModel compile failed for chapter %d.", chapter_number)
            try:
                LegacyWorldModelCompiler(session).record_failed_compile(
                    project_id=project_id,
                    as_of_chapter=chapter_number,
                    trigger="chapter_accepted",
                    error=f"{exc.__class__.__name__}: {exc}",
                )
            except Exception:
                logger.warning("Failed to record WorldModel failed compile run.", exc_info=True)
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.WORLD_MODEL_COMPILE_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 legacy WorldModel projection 失败，BookState canon 已保留。",
                reason=str(exc),
                payload=event_error_payload(
                    exc,
                    stage="world_model_compile",
                    operation_id=self._audit_operation_id(),
                ),
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.LEGACY_PROJECTION_FAILED,
                scope="chapter",
                summary=f"第{chapter_number}章 legacy world_model_v4 projection 失败，BookState canon 不回滚。",
                reason=str(exc),
                payload=event_error_payload(
                    exc,
                    stage="legacy_projection",
                    operation_id=self._audit_operation_id(),
                ),
            )
            return True
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.WORLD_MODEL_COMPILE_SUCCEEDED,
            scope="chapter",
            summary=f"第{chapter_number}章 WorldModel compile 完成。",
            related_object_type="world_model_snapshot",
            related_object_id=snapshot.id,
            payload=audit_payload(
                stage="world_model_compile",
                status="succeeded",
                operation_id=self._audit_operation_id(),
                snapshot_id=snapshot.id,
                as_of_chapter=snapshot.as_of_chapter,
                source_digest=snapshot.source_digest,
            ),
        )
        return True

    def _run_provisional_band_preview(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_plans: list[ChapterPlan],
        persist_result: bool = True,
    ) -> ProvisionalBandPreview | None:
        if not chapter_plans or not self.config.minimax_api_key.strip():
            return None
        self._emit_progress(
            "stage_changed",
            stage="running_provisional_preview",
            project_id=project_id,
            current_chapter=chapter_plans[0].chapter_number if chapter_plans else 0,
        )

        repo, _updater, _checker = self._make_state_helpers(session)
        preview_checker = ContinuityChecker(
            repo,
            min_chars=self.provisional_writer.min_chapter_chars,
            max_chars=self.provisional_writer.max_chapter_chars,
        )
        safe_band = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_"
            for ch in band_id
        ).strip("_") or "band"
        namespace_root = (
            f"projects/{project_id}/arcs/{arc_id}/provisional/{safe_band}"
        )
        if persist_result:
            session.query(ProvisionalChapterLedger).filter(
                ProvisionalChapterLedger.project_id == project_id,
                ProvisionalChapterLedger.arc_id == arc_id,
                ProvisionalChapterLedger.band_id == band_id,
            ).delete(synchronize_session=False)
        summaries: list[str] = []
        chapter_payloads: list[dict[str, object]] = []
        chapter_numbers: list[int] = []
        total_char_count = 0
        issue_count = 0
        failure_count = 0
        aggregate_verdict = "pass"

        for chapter_plan in chapter_plans:
            timeline_before = repo.get_current_timeline(project_id)
            current_time_label = (
                timeline_before.current_time_label
                if timeline_before is not None
                else ""
            )
            context = self.retrieval_broker.build_chapter_context(
                repo, project_id, chapter_plan
            )
            if summaries:
                previous = (
                    list(context.previous_chapter_summaries)
                    + summaries[-2:]
                )[-3:]
                context = context.model_copy(
                    update={"previous_chapter_summaries": previous}
                )
            try:
                writer_output = self.provisional_writer.write_preview_chapter(
                    context,
                    trace_stage_key="provisional_preview",
                    max_attempts=2,
                    retry_on_timeout=True,
                )
                verdict = preview_checker.check(project_id, writer_output)
                verdict = self._normalize_provisional_verdict(writer_output, verdict)
                artifact_meta_path = ""
                draft_blob_path = ""
                if persist_result:
                    artifact_paths = self.artifact_store.save_writer_output(
                        project_id=project_id,
                        chapter_number=chapter_plan.chapter_number,
                        writer_output=writer_output,
                        namespace_root=namespace_root,
                    )
                    artifact_meta_path = str(artifact_paths["meta_path"] or "")
                    draft_blob_path = str(artifact_paths["writer_output"].draft_blob_path or "")
                projected_time_label = (
                    writer_output.time_advance.new_time_label
                    if writer_output.time_advance is not None
                    else current_time_label
                )
                total_char_count += writer_output.char_count or len(writer_output.body)
                issue_count += len(verdict.issues)
                chapter_numbers.append(chapter_plan.chapter_number)
                summaries.append(
                    writer_output.end_of_chapter_summary or writer_output.title
                )
                if persist_result:
                    session.add(
                        ProvisionalChapterLedger(
                            id=new_id(),
                            project_id=project_id,
                            arc_id=arc_id,
                            band_id=band_id,
                            chapter_number=chapter_plan.chapter_number,
                            title=writer_output.title,
                            summary=writer_output.end_of_chapter_summary,
                            verdict=verdict.verdict,
                            char_count=writer_output.char_count,
                            artifact_meta_path=artifact_meta_path,
                            draft_blob_path=draft_blob_path,
                            current_time_label=current_time_label,
                            projected_time_label=projected_time_label,
                            state_changes_json=json.dumps(
                                [
                                    change.model_dump(mode="json")
                                    for change in writer_output.state_changes
                                ],
                                ensure_ascii=False,
                            ),
                            events_json=json.dumps(
                                [
                                    event.model_dump(mode="json")
                                    for event in writer_output.new_events
                                ],
                                ensure_ascii=False,
                            ),
                            thread_beats_json=json.dumps(
                                [
                                    beat.model_dump(mode="json")
                                    for beat in writer_output.thread_beats
                                ],
                                ensure_ascii=False,
                            ),
                            time_advance_json=json.dumps(
                                writer_output.time_advance.model_dump(mode="json")
                                if writer_output.time_advance is not None
                                else {},
                                ensure_ascii=False,
                            ),
                            issues_json=json.dumps(
                                [
                                    issue.model_dump(mode="json")
                                    for issue in verdict.issues
                                ],
                                ensure_ascii=False,
                            ),
                        )
                    )
                chapter_payloads.append(
                    {
                        "chapter_number": chapter_plan.chapter_number,
                        "title": writer_output.title,
                        "summary": writer_output.end_of_chapter_summary,
                        "char_count": writer_output.char_count,
                        "verdict": verdict.verdict,
                        "current_time_label": current_time_label,
                        "projected_time_label": projected_time_label,
                        "state_changes": [
                            change.model_dump(mode="json")
                            for change in writer_output.state_changes
                        ],
                        "events": [
                            event.model_dump(mode="json")
                            for event in writer_output.new_events
                        ],
                        "thread_beats": [
                            beat.model_dump(mode="json")
                            for beat in writer_output.thread_beats
                        ],
                        "time_advance": (
                            writer_output.time_advance.model_dump(mode="json")
                            if writer_output.time_advance is not None
                            else {}
                        ),
                        "artifact_meta_path": artifact_meta_path,
                        "issues": [
                            issue.model_dump(mode="json")
                            for issue in verdict.issues
                        ],
                    }
                )
                if verdict.verdict == "fail":
                    aggregate_verdict = "fail"
                elif verdict.verdict == "warn" and aggregate_verdict == "pass":
                    aggregate_verdict = "warn"
            except Exception as exc:  # noqa: BLE001
                if self._should_degrade_provisional_preview(exc):
                    fallback = self._build_provisional_fallback(
                        chapter_plan=chapter_plan,
                        current_time_label=current_time_label,
                        error_text=str(exc),
                        issue_description="当前章节预演生成失败，已降级为计划级影子草案。",
                    )
                    issue_count += len(fallback["issues"])
                    total_char_count += int(fallback["char_count"])
                    chapter_numbers.append(chapter_plan.chapter_number)
                    summaries.append(str(fallback["summary"]))
                    chapter_payloads.append(fallback)
                    if persist_result:
                        session.add(
                            ProvisionalChapterLedger(
                                id=new_id(),
                                project_id=project_id,
                                arc_id=arc_id,
                                band_id=band_id,
                                chapter_number=chapter_plan.chapter_number,
                                title=str(fallback["title"]),
                                summary=str(fallback["summary"]),
                                verdict=str(fallback["verdict"]),
                                char_count=int(fallback["char_count"]),
                                artifact_meta_path="",
                                draft_blob_path="",
                                current_time_label=current_time_label,
                                projected_time_label=str(fallback["projected_time_label"]),
                                state_changes_json="[]",
                                events_json="[]",
                                thread_beats_json="[]",
                                time_advance_json="{}",
                                issues_json=json.dumps(fallback["issues"], ensure_ascii=False),
                                error_text=str(fallback["error"]),
                            )
                        )
                    if aggregate_verdict == "pass":
                        aggregate_verdict = "warn"
                    continue
                failure_count += 1
                aggregate_verdict = "fail"
                chapter_payloads.append(
                    {
                        "chapter_number": chapter_plan.chapter_number,
                        "title": chapter_plan.title,
                        "summary": "",
                        "char_count": 0,
                        "verdict": "fail",
                        "error": str(exc),
                        "issues": [],
                    }
                )
                if persist_result:
                    session.add(
                        ProvisionalChapterLedger(
                            id=new_id(),
                            project_id=project_id,
                            arc_id=arc_id,
                            band_id=band_id,
                            chapter_number=chapter_plan.chapter_number,
                            title=chapter_plan.title,
                            summary="",
                            verdict="fail",
                            char_count=0,
                            artifact_meta_path="",
                            draft_blob_path="",
                            current_time_label=current_time_label,
                            projected_time_label=current_time_label,
                            state_changes_json="[]",
                            events_json="[]",
                            thread_beats_json="[]",
                            time_advance_json="{}",
                            issues_json="[]",
                            error_text=str(exc),
                        )
                    )
                break

        artifact_path = ""
        if persist_result:
            artifact_path = self.artifact_store.save_provisional_band(
                project_id=project_id,
                arc_id=arc_id,
                band_id=band_id,
                payload={
                    "project_id": project_id,
                    "arc_id": arc_id,
                    "band_id": band_id,
                    "aggregate_verdict": aggregate_verdict,
                    "preview_chapter_count": len(chapter_payloads),
                    "total_char_count": total_char_count,
                    "issue_count": issue_count,
                    "failure_count": failure_count,
                    "chapters": chapter_payloads,
                },
            )
        return ProvisionalBandPreview(
            band_id=band_id,
            artifact_path=artifact_path,
            aggregate_verdict=aggregate_verdict,
            preview_chapter_count=len(chapter_payloads),
            total_char_count=total_char_count,
            issue_count=issue_count,
            failure_count=failure_count,
            chapter_numbers=chapter_numbers,
            summary_lines=summaries,
        )

    def _abort_requested(self) -> bool:
        try:
            return bool(self.should_abort and self.should_abort())
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring abort predicate failure.", exc_info=True)
            return False

    def _pause_requested(self) -> bool:
        try:
            return bool(self.should_pause and self.should_pause())
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring pause predicate failure.", exc_info=True)
            return False

    def _paused_result(
        self,
        project_id: str,
        requested_chapters: int,
        *,
        completed_chapters: list[int] | None = None,
        failed_chapters: list[int] | None = None,
        paused_chapters: list[int] | None = None,
        frozen_artifacts: list[str] | None = None,
        current_chapter: int = 0,
    ) -> RunResult:
        self._emit_progress(
            "stage_changed",
            stage="paused",
            project_id=project_id,
            requested_chapters=requested_chapters,
            current_chapter=current_chapter,
            completed_chapters=completed_chapters or [],
            failed_chapters=failed_chapters or [],
            paused_chapters=paused_chapters or [],
            frozen_artifacts=frozen_artifacts or [],
        )
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=list(completed_chapters or []),
            failed_chapters=list(failed_chapters or []),
            paused_chapters=list(paused_chapters or []),
            frozen_artifacts=list(frozen_artifacts or []),
            paused=True,
        )

    def _cancelled_result(
        self,
        project_id: str,
        requested_chapters: int,
        *,
        completed_chapters: list[int] | None = None,
        failed_chapters: list[int] | None = None,
        paused_chapters: list[int] | None = None,
        frozen_artifacts: list[str] | None = None,
        current_chapter: int = 0,
    ) -> RunResult:
        self._emit_progress(
            "stage_changed",
            stage="cancelled",
            project_id=project_id,
            requested_chapters=requested_chapters,
            current_chapter=current_chapter,
            completed_chapters=completed_chapters or [],
            failed_chapters=failed_chapters or [],
            paused_chapters=paused_chapters or [],
            frozen_artifacts=frozen_artifacts or [],
        )
        return RunResult(
            project_id=project_id,
            requested_chapters=requested_chapters,
            completed_chapters=list(completed_chapters or []),
            failed_chapters=list(failed_chapters or []),
            paused_chapters=list(paused_chapters or []),
            frozen_artifacts=list(frozen_artifacts or []),
            cancelled=True,
        )

    @staticmethod
    def _normalize_provisional_verdict(
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
    ) -> ReviewVerdict:
        usable_body = len((writer_output.body or "").strip()) >= 300
        filtered_issues = [
            issue for issue in verdict.issues if issue.rule_name != "char_count_low"
        ]
        if verdict.verdict != "fail":
            if len(filtered_issues) == len(verdict.issues):
                return verdict
            if not filtered_issues:
                return ReviewVerdict(verdict="pass", issues=[])
            severities = {issue.severity for issue in filtered_issues}
            next_verdict = "fail" if "error" in severities else "warn"
            return ReviewVerdict(verdict=next_verdict, issues=filtered_issues)
        if not usable_body:
            return ReviewVerdict(verdict=verdict.verdict, issues=filtered_issues)
        softened_issues = [
            issue.model_copy(update={"severity": "warning"})
            for issue in filtered_issues
        ]
        softened_issues.append(
            softened_issues[0].model_copy(
                update={
                    "rule_name": "provisional_softened_fail",
                    "description": "预演正文可用，已将严格失败降级为预演警告。",
                    "entity_names": [],
                }
            )
            if softened_issues
            else None
        )
        softened_issues = [issue for issue in softened_issues if issue is not None]
        return ReviewVerdict(
            verdict="warn" if softened_issues else "pass",
            issues=softened_issues,
        )

    @staticmethod
    def _should_degrade_provisional_preview(exc: Exception) -> bool:
        text = str(exc).lower()
        if any(
            token in text
            for token in (
                "timed out",
                "timeout",
                "read operation timed out",
                "json generation failed",
                "llmjsonparseerror",
                "preview generation failed",
                "preview response body is empty",
                "connection reset",
            )
        ):
            return True
        return WritingOrchestrator._is_transient_llm_like(exc)

    @staticmethod
    def _build_provisional_fallback(
        *,
        chapter_plan: ChapterPlan,
        current_time_label: str,
        error_text: str,
        issue_description: str,
    ) -> dict[str, Any]:
        try:
            goals = json.loads(chapter_plan.goals_json or "[]") or []
        except (json.JSONDecodeError, TypeError):
            goals = []
        summary = chapter_plan.one_line.strip() or chapter_plan.title.strip() or f"第{chapter_plan.chapter_number}章"
        estimated_char_count = max(
            360,
            min(1200, 260 + len(summary) * 8 + sum(len(str(goal)) for goal in goals) * 4),
        )
        issues = [
            {
                "rule_name": "provisional_fallback",
                "severity": "warning",
                "description": issue_description,
            }
        ]
        return {
            "chapter_number": chapter_plan.chapter_number,
            "title": chapter_plan.title,
            "summary": summary,
            "char_count": estimated_char_count,
            "verdict": "warn",
            "current_time_label": current_time_label,
            "projected_time_label": current_time_label,
            "state_changes": [],
            "events": [],
            "thread_beats": [],
            "time_advance": {},
            "artifact_meta_path": "",
            "issues": issues,
            "error": error_text,
            "fallback_mode": "plan_shadow",
        }

    def _load_writer_output_from_meta(self, meta_path: str) -> WriterOutput:
        payload = self.artifact_store.read_json(meta_path)
        return WriterOutput.model_validate(payload)

    @staticmethod
    def _load_review_verdict(review: ChapterReview) -> ReviewVerdict:
        meta = json.loads(review.review_meta_json or "{}") if getattr(review, "review_meta_json", "") else {}
        if not isinstance(meta, dict):
            meta = {}
        return ReviewVerdict.model_validate(
            {
                "verdict": review.verdict,
                "issues": json.loads(review.issues_json or "[]"),
                **meta,
            }
        )

    def _seed_state(
        self,
        updater: StateUpdater,
        project_id: str,
        arc_plan: dict,
        num_chapters: int,
    ) -> None:
        """Seed the database with initial state from the arc plan."""
        chapters = arc_plan.get("chapters", [])
        raw_outlines = arc_plan.get("arc_outlines") or []
        normalized_outlines: list[dict[str, int | str]] = []
        cursor = 1
        for index, raw in enumerate(raw_outlines, start=1):
            if cursor > num_chapters or not isinstance(raw, dict):
                break
            raw_count = int(raw.get("chapter_count", 0) or 0)
            if raw_count <= 0:
                continue
            remaining = num_chapters - cursor + 1
            chapter_count = min(raw_count, remaining)
            if chapter_count <= 0:
                break
            chapter_start = cursor
            chapter_end = cursor + chapter_count - 1
            normalized_outlines.append(
                {
                    "arc_number": index,
                    "chapter_start": chapter_start,
                    "chapter_end": chapter_end,
                    "chapter_count": chapter_count,
                    "arc_synopsis": str(raw.get("arc_synopsis", "")).strip() or str(arc_plan.get("arc_synopsis", "")).strip(),
                }
            )
            cursor = chapter_end + 1
        if not normalized_outlines:
            normalized_outlines = [
                {
                    "arc_number": 1,
                    "chapter_start": 1,
                    "chapter_end": num_chapters,
                    "chapter_count": num_chapters,
                    "arc_synopsis": str(arc_plan.get("arc_synopsis", "")).strip(),
                }
            ]
        elif cursor <= num_chapters:
            normalized_outlines.append(
                {
                    "arc_number": len(normalized_outlines) + 1,
                    "chapter_start": cursor,
                    "chapter_end": num_chapters,
                    "chapter_count": num_chapters - cursor + 1,
                    "arc_synopsis": f"后续弧线：第{cursor}章至第{num_chapters}章",
                }
            )

        first_arc = None
        for outline in normalized_outlines:
            chapter_start = int(outline.get("chapter_start", 1) or 1)
            chapter_end = int(outline.get("chapter_end", chapter_start) or chapter_start)
            chapter_count = max(1, int(outline.get("chapter_count", chapter_end - chapter_start + 1) or 1))
            arc = updater.create_arc_plan(
                project_id=project_id,
                arc_synopsis=str(outline.get("arc_synopsis", "") or ""),
                version=1,
                status="active" if first_arc is None else "planned",
                arc_number=int(outline.get("arc_number", 1) or 1),
                chapter_start=chapter_start,
                chapter_end=chapter_end,
                planned_target_size=chapter_count,
                planned_soft_min=max(1, int(round(chapter_count * 0.85))),
                planned_soft_max=max(chapter_count, int(round(chapter_count * 1.20))),
            )
            if first_arc is None:
                first_arc = arc
            for chapter_number in range(chapter_start, chapter_end + 1):
                ch = chapters[chapter_number - 1] if chapter_number - 1 < len(chapters) else {}
                updater.create_chapter_plan(
                    project_id=project_id,
                    arc_plan_id=arc.id,
                    chapter_number=ch.get("chapter_number", chapter_number),
                    title=ch.get("title", f"第{chapter_number}章"),
                    one_line=ch.get("one_line", ""),
                    goals=ch.get("goals", []),
                )

        # Entities: characters
        from forwin.characters.creation import CharacterCreationHelper
        from forwin.characters.models import CharacterCreationRequest

        character_helper = CharacterCreationHelper(updater.session)
        entity_map: dict[str, str] = {}  # name -> entity_id
        for char_data in arc_plan.get("characters", []):
            initial_state = char_data.get("initial_state", {})
            result = character_helper.create_character(
                CharacterCreationRequest(
                    project_id=project_id,
                    source="arc_plan_seed",
                    source_ref=str(char_data.get("source_ref") or ""),
                    name=char_data.get("name", "未命名"),
                    description=char_data.get("description", ""),
                    aliases=char_data.get("aliases", []),
                    importance=char_data.get("importance", 5),
                    created_at_chapter=0,
                    profile={
                        "role_hint": str(char_data.get("role_hint") or ""),
                        "role_archetype": str(char_data.get("role_archetype") or char_data.get("role_hint") or ""),
                        "narrative_role": str(char_data.get("narrative_role") or ""),
                        "public_identity": str(char_data.get("public_identity") or ""),
                    },
                    state=initial_state if isinstance(initial_state, dict) else {},
                    personality_tags=list(char_data.get("personality_tags") or []),
                    audit_reason="arc plan seed character",
                )
            )
            entity_map[result.character_name] = result.legacy_entity_id or result.character_id

        # Entities: locations
        for loc_data in arc_plan.get("locations", []):
            entity = updater.create_entity(
                project_id=project_id,
                kind="location",
                name=loc_data.get("name", "未命名"),
                description=loc_data.get("description", ""),
                aliases=loc_data.get("aliases", []),
                importance=loc_data.get("importance", 5),
                chapter=0,
            )
            entity_map[entity.name] = entity.id
            initial_state = loc_data.get("initial_state", {})
            if initial_state:
                updater.create_entity_state(entity.id, 0, initial_state)

        # Entities: factions
        for fac_data in arc_plan.get("factions", []):
            entity = updater.create_entity(
                project_id=project_id,
                kind="faction",
                name=fac_data.get("name", "未命名"),
                description=fac_data.get("description", ""),
                aliases=fac_data.get("aliases", []),
                importance=fac_data.get("importance", 5),
                chapter=0,
            )
            entity_map[entity.name] = entity.id
            initial_state = fac_data.get("initial_state", {})
            if initial_state:
                updater.create_entity_state(entity.id, 0, initial_state)

        # Relations
        for rel_data in arc_plan.get("relations", []):
            source_name = rel_data.get("source_name", "")
            target_name = rel_data.get("target_name", "")
            source_id = entity_map.get(source_name)
            target_id = entity_map.get(target_name)
            if source_id and target_id:
                updater.create_relation(
                    project_id=project_id,
                    source_id=source_id,
                    target_id=target_id,
                    relation_type=rel_data.get("relation_type", "unknown"),
                    description=rel_data.get("description", ""),
                    chapter=0,
                )
            else:
                logger.warning(
                    "Skipping relation %s -> %s: entity not found.",
                    source_name,
                    target_name,
                )

        # Plot threads
        for thread_data in arc_plan.get("plot_threads", []):
            updater.create_thread(
                project_id=project_id,
                name=thread_data.get("name", ""),
                description=thread_data.get("description", ""),
                priority=thread_data.get("priority", 2),
                chapter=0,
            )

        self.subworld_manager.apply_initial_arc_plan(
            session=updater.session,
            updater=updater,
            project_id=project_id,
            arc_id=first_arc.id if first_arc is not None else "",
            arc_plan=arc_plan,
            entity_map=entity_map,
        )

        # Initial timeline
        initial_time = arc_plan.get("initial_time", {})
        if initial_time:
            updater.create_time_point(
                project_id=project_id,
                label=initial_time.get("label", "故事开始"),
                ordinal=0,
                description=initial_time.get("description", ""),
            )
