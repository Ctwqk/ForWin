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
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable

from sqlalchemy.orm import Session

from forwin.checker.rules import ContinuityChecker
from forwin.config import Config
from forwin.director import ArcDirector
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
    evaluate_constraint_issues,
    evaluate_director_imbalance,
    evaluate_intra_band_consistency,
    evaluate_next_band_task_compatibility,
    evaluate_resource_closure_risk,
    evaluate_task_contract,
)
from forwin.models import ProvisionalBandExecution, ProvisionalChapterLedger, new_id
from forwin.models.governance import BandCheckpoint
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ChapterPlan, Project
from forwin.models.phase import ArcStructureDraft, BandExperiencePlan
from forwin.protocol.experience import ArcPayoffMap, BandDelightSchedule, ChapterExperiencePlan
from forwin.protocol.review import RepairInstruction, ReviewVerdict
from forwin.orchestrator.phase3 import (
    PacingStrategist,
    ReplanGovernor,
    StageAnalyzer,
    save_stage_analysis,
)
from forwin.orchestrator.feedback_aggregator import run_feedback_aggregation_pass
from forwin.orchestrator.phase4 import (
    NPCIntentGenerator,
    WorldSimulator,
    save_npc_intents,
    save_world_turn,
)
from forwin.orchestrator.phase24 import ArcEnvelopeManager, ProvisionalBandPreview
from forwin.retrieval import RetrievalBroker, create_memory_index
from forwin.reviewer import HistoricalReviewHub
from forwin.state.repo import StateRepository
from forwin.state.schema import KNOWN_STATE_FIELDS
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore
from forwin.protocol.writer import WriterOutput
from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.llm_client import LLMClient

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.config = config or Config.from_env()
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

        # Ensure the database directory exists.
        db_dir = Path(self.config.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        # Database setup.
        self.engine = get_engine(self.config.db_path)
        init_db(self.engine)
        self._SessionFactory = get_session_factory(self.engine)

        # LLM client + writer.
        self.llm_client = LLMClient(
            api_key=self.config.minimax_api_key,
            base_url=self.config.minimax_base_url,
            model=self.config.minimax_model,
            timeout_seconds=self.config.llm_timeout_seconds,
            retry_attempts=self.config.llm_retry_attempts,
            retry_initial_delay_seconds=self.config.llm_retry_initial_delay_seconds,
            retry_max_delay_seconds=self.config.llm_retry_max_delay_seconds,
            fallback_profiles=self.config.llm_fallback_profiles,
        )
        self.arc_director = ArcDirector(
            llm_client=self.llm_client,
            max_tokens=self.config.max_tokens,
        )
        self.retrieval_broker = RetrievalBroker(
            context_budget_chars=self.config.context_budget_chars,
            max_entities=self.config.retrieval_max_entities,
            max_threads=self.config.retrieval_max_threads,
            max_summaries=self.config.retrieval_max_summaries,
            memory_index=create_memory_index(
                backend=self.config.retrieval_backend,
                root_dir=self.config.retrieval_root,
                qdrant_url=self.config.qdrant_url,
                qdrant_collection=self.config.qdrant_collection,
                embedding_backend=self.config.embedding_backend,
                embedding_base_url=self.config.embedding_base_url,
                embedding_api_key=self.config.embedding_api_key,
                embedding_model=self.config.embedding_model,
                embedding_dims=self.config.embedding_dims,
            ),
        )
        self.artifact_store = ArtifactStore(
            self.config.artifact_root,
            backend=self.config.artifact_backend,
            minio_endpoint=self.config.minio_endpoint,
            minio_access_key=self.config.minio_access_key,
            minio_secret_key=self.config.minio_secret_key,
            minio_bucket=self.config.minio_bucket,
            minio_prefix=self.config.minio_prefix,
            minio_secure=self.config.minio_secure,
        )
        self.writer = ChapterWriter(
            llm_client=self.llm_client,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            writer_mode=self.config.writer_mode,
            default_scene_count=self.config.default_scene_count,
            max_scene_count=self.config.max_scene_count,
            min_chapter_chars=self.config.min_chapter_chars,
            max_chapter_chars=self.config.max_chapter_chars,
            target_chapter_chars=self.config.target_chapter_chars,
            single_call_timeout_seconds=self.config.llm_timeout_seconds,
            scene_call_timeout_seconds=self.config.scene_call_timeout_seconds,
        )
        provisional_target_chars = max(
            700,
            min(self.config.target_chapter_chars, 900),
        )
        provisional_min_chars = max(500, min(self.config.min_chapter_chars, provisional_target_chars))
        provisional_max_chars = max(
            provisional_target_chars,
            min(self.config.max_chapter_chars, 1000),
        )
        provisional_timeout_seconds = min(
            max(
                self.config.llm_timeout_seconds,
                self.config.scene_call_timeout_seconds,
                90.0,
            ),
            180.0,
        )
        self.provisional_writer = ChapterWriter(
            llm_client=self.llm_client,
            temperature=min(self.config.temperature, 0.7),
            max_tokens=min(self.config.max_tokens, 2400),
            writer_mode="single",
            default_scene_count=1,
            max_scene_count=1,
            min_chapter_chars=provisional_min_chars,
            max_chapter_chars=provisional_max_chars,
            target_chapter_chars=provisional_target_chars,
            single_call_timeout_seconds=provisional_timeout_seconds,
            scene_call_timeout_seconds=provisional_timeout_seconds,
        )
        self.stage_analyzer = StageAnalyzer()
        self.pacing_strategist = PacingStrategist(
            window_size=self.config.pacing_window_size,
            stale_thread_window=self.config.stale_thread_window,
            min_avg_chars=self.config.pacing_min_avg_chars,
            max_avg_chars=self.config.pacing_max_avg_chars,
            active_thread_limit=self.config.phase_active_thread_limit,
        )
        self.replan_governor = ReplanGovernor(
            cooldown_chapters=self.config.replan_cooldown_chapters
        )
        phase4_llm = (
            self.llm_client
            if self.config.phase4_use_llm and bool(self.config.minimax_api_key)
            else None
        )
        self.npc_intent_generator = NPCIntentGenerator(
            llm_client=phase4_llm,
            active_thread_limit=self.config.phase_active_thread_limit,
        )
        self.world_simulator = WorldSimulator(
            llm_client=phase4_llm,
            active_thread_limit=self.config.phase_active_thread_limit,
        )
        self.arc_envelope_manager = ArcEnvelopeManager(
            director=self.arc_director,
            provisional_executor=self._run_provisional_band_preview,
        )
        self.review_hub = HistoricalReviewHub(
            experience_review_enabled=self.config.experience_review_enabled,
            lint_review_enabled=self.config.lint_review_enabled,
            llm_client=self.llm_client if bool(self.config.minimax_api_key) else None,
            llm_enabled=bool(self.config.minimax_api_key),
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

            print(f"项目创建完成: {project.title}")
            print(f"项目ID: {project_id}")

            result = self._run_project_chapters(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_numbers=list(range(1, num_chapters + 1)),
                requested_chapters=num_chapters,
            )
            print(f"\n{'='*60}")
            if result.status == "needs_review":
                print(
                    "生成暂停："
                    f"第 {result.paused_chapters[0]} 章已进入人工检查点。"
                )
            elif result.status == "completed":
                print(f"生成完毕！共 {num_chapters} 章")
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
            print(f"数据库: {self.config.db_path}")
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

            result = self._run_project_chapters(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_numbers=list(range(1, num_chapters + 1)),
                requested_chapters=num_chapters,
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

    def _clear_governance_runtime(self) -> None:
        self._governance_runtime_project_id = ""
        self._governance_runtime_updater = None
        self._governance_stage_name = ""
        self._governance_stage_started_at = 0.0

    def _record_stage_transition(self, payload: dict[str, Any]) -> None:
        updater = self._governance_runtime_updater
        project_id = str(payload.get("project_id") or self._governance_runtime_project_id or "").strip()
        stage = str(payload.get("stage") or "").strip()
        if updater is None or not project_id or not stage:
            return
        now = time.perf_counter()
        chapter_number = int(payload.get("current_chapter") or 0)
        if self._governance_stage_name and self._governance_stage_name != stage:
            duration_ms = max(0, int((now - self._governance_stage_started_at) * 1000))
            stage_payload = {
                "stage": self._governance_stage_name,
                "next_stage": stage,
                "duration_ms": duration_ms,
            }
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_EXITED,
                scope="task",
                summary=f"阶段 {self._governance_stage_name} 已结束。",
                payload=stage_payload,
            )
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.STAGE_DURATION_SUMMARY,
                scope="task",
                summary=f"阶段 {self._governance_stage_name} 用时 {duration_ms}ms。",
                payload=stage_payload,
            )
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
        if previous_snapshot is not None and latest.id == previous_snapshot.id:
            return None
        if latest.aggregate_verdict == "fail" or latest.failure_count > 0:
            return latest
        return None

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
            self._record_decision_event(
                updater=updater,
                project_id=project_id,
                event_family="business_event",
                event_type=DecisionEventType.RUN_STARTED,
                scope="task",
                summary="已有项目生成 run 已启动。",
                related_object_type="project",
                related_object_id=project_id,
                payload={"requested_chapters": len(chapter_plans)},
            )
            session.commit()

            chapter_numbers = [
                plan.chapter_number
                for plan in chapter_plans
                if plan.status in {"planned", "failed"}
            ]
            if max_chapters is not None:
                chapter_numbers = chapter_numbers[: max(1, int(max_chapters or 1))]
            if not chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=len(chapter_plans),
                )

            self._emit_progress(
                "stage_changed",
                stage="resolving_arc_envelope",
                project_id=project_id,
                requested_chapters=len(chapter_plans),
                current_chapter=min(chapter_numbers) - 1,
            )
            if self._abort_requested():
                return self._cancelled_result(project_id, len(chapter_plans))
            previous_provisional = self._latest_provisional_gate_snapshot(
                session,
                project_id,
            )
            self.arc_envelope_manager.ensure_active_arc_resolution(
                session=session,
                project_id=project_id,
                activation_chapter=min(chapter_numbers),
            )
            session.commit()
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
                    requested_chapters=len(chapter_plans),
                    gate=failed_provisional,
                )

            return self._run_project_chapters(
                session=session,
                repo=repo,
                updater=updater,
                checker=checker,
                project_id=project_id,
                chapter_numbers=chapter_numbers,
                requested_chapters=len(chapter_plans),
            )
        finally:
            self._clear_governance_runtime()
            session.close()

    def accept_review(self, project_id: str, chapter_number: int, *, reason: str = "") -> dict[str, str]:
        session: Session = self._SessionFactory()
        try:
            repo, updater, _checker = self._make_state_helpers(session)
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

            updater.mark_chapter_status(project_id, chapter_number, "accepted")
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

    def _previous_band_row(
        self,
        session: Session,
        *,
        project_id: str,
        current_start: int,
    ) -> BandExperiencePlan | None:
        return (
            session.query(BandExperiencePlan)
            .filter(
                BandExperiencePlan.project_id == project_id,
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
        if latest_checkpoint is None:
            return (
                "band_checkpoint_pending",
                previous_band.band_id,
                chapter_blocking_message("band_checkpoint_pending", band_id=previous_band.band_id),
            )
        if latest_checkpoint.status in {"pass", "overridden"}:
            return "", "", ""
        code = {
            "pending": "band_checkpoint_pending",
            "warn": "band_checkpoint_warn",
            "fail": "band_checkpoint_fail",
            "error": "band_checkpoint_fail",
        }.get(str(latest_checkpoint.status or ""), "band_checkpoint_pending")
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
        return self.review_hub.review(
            project_id=project_id,
            repo=repo,
            context=context,
            writer_output=writer_output,
            continuity_checker=checker,
        )

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
        current_review = self._review_current_output(
            repo=repo,
            checker=checker,
            project_id=project_id,
            context=current_context,
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
            payload={
                "verdict": current_review.verdict,
                "issue_types": [
                    str(getattr(issue, "issue_type", getattr(issue, "rule_name", "")) or "")
                    for issue in current_review.issues
                ],
                "issue_groups": [
                    str(getattr(issue, "issue_group", "") or issue_group_for_issue(
                        issue_type=str(getattr(issue, "issue_type", "") or ""),
                        rule_name=str(getattr(issue, "rule_name", "") or ""),
                    ))
                    for issue in current_review.issues
                ],
                "forced_accept_applied": bool(current_review.forced_accept_applied),
            },
        )
        if current_review.verdict != "fail" or self.config.operation_mode == "checkpoint":
            return current_output, current_review, False

        max_attempts = max(1, min(3, int(self.config.review_fail_max_rewrites or 3)))
        initial_scope = (
            current_review.repair_instruction.repair_scope
            if current_review.repair_instruction is not None
            else "scene"
        )
        attempt_scopes = self._rewrite_scope_sequence(initial_scope, max_attempts)
        for attempt_no, repair_scope in enumerate(attempt_scopes[:max_attempts], start=1):
            repair_instruction = current_review.repair_instruction or self._default_repair_instruction(
                repair_scope=repair_scope,
                context=current_context,
                review=current_review,
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
                },
                parent_event_id=str(current_review_event.id or ""),
            )
            design_patch = self._apply_repair_patch(
                session=session,
                repo=repo,
                project_id=project_id,
                chapter_plan=chapter_plan,
                repair_scope=repair_scope,
                repair_instruction=repair_instruction,
            )
            session.flush()
            updated_context = self.retrieval_broker.build_chapter_context(repo, project_id, chapter_plan)
            try:
                rewritten_output = self._write_chapter_with_attention_fallback(
                    context=updated_context,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    updater=updater,
                    paused_chapters=[],
                    frozen_artifacts=[],
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
                    forced_accept_applied=(
                        self.config.operation_mode == "blackbox"
                        and attempt_no == max_attempts
                    ),
                )
                repair_failed_event = self._record_decision_event(
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
                    payload={"attempt_no": attempt_no, "repair_scope": repair_scope},
                    parent_event_id=str(repair_started_event.id or ""),
                )
                if self.config.operation_mode == "blackbox" and attempt_no == max_attempts:
                    current_review = current_review.model_copy(update={"forced_accept_applied": True})
                    current_review_row.review_meta_json = self._review_meta_json(current_review)
                    session.add(current_review_row)
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_plan.chapter_number,
                        event_family="audit_action",
                        event_type=DecisionEventType.FORCED_ACCEPT_APPLIED,
                        scope="chapter",
                        summary=f"第{chapter_plan.chapter_number}章应用 forced accept。",
                        related_object_type="chapter_review",
                        related_object_id=current_review_row.id,
                        parent_event_id=str(repair_failed_event.id or ""),
                    )
                    return current_output, current_review, True
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
                    forced_accept_applied=(
                        self.config.operation_mode == "blackbox"
                        and attempt_no == max_attempts
                    ),
                )
                repair_failed_event = self._record_decision_event(
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
                    payload={"attempt_no": attempt_no, "repair_scope": repair_scope},
                    parent_event_id=str(repair_started_event.id or ""),
                )
                if self.config.operation_mode == "blackbox" and attempt_no == max_attempts:
                    current_review = current_review.model_copy(update={"forced_accept_applied": True})
                    current_review_row.review_meta_json = self._review_meta_json(current_review)
                    session.add(current_review_row)
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_plan.chapter_number,
                        event_family="audit_action",
                        event_type=DecisionEventType.FORCED_ACCEPT_APPLIED,
                        scope="chapter",
                        summary=f"第{chapter_plan.chapter_number}章应用 forced accept。",
                        related_object_type="chapter_review",
                        related_object_id=current_review_row.id,
                        parent_event_id=str(repair_failed_event.id or ""),
                    )
                    return current_output, current_review, True
                continue
            rewritten_review = self._review_current_output(
                repo=repo,
                checker=checker,
                project_id=project_id,
                context=updated_context,
                writer_output=rewritten_output,
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
            forced_accept_applied = (
                self.config.operation_mode == "blackbox"
                and attempt_no == max_attempts
                and rewritten_review.verdict == "fail"
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
                forced_accept_applied=forced_accept_applied,
            )
            repair_result_event = self._record_decision_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.REPAIR_SUCCEEDED if rewritten_review.verdict != "fail" else DecisionEventType.REPAIR_FAILED,
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
            if forced_accept_applied:
                rewritten_review = rewritten_review.model_copy(update={"forced_accept_applied": True})
                rewritten_review_row.review_meta_json = self._review_meta_json(rewritten_review)
                session.add(rewritten_review_row)
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    event_family="audit_action",
                    event_type=DecisionEventType.FORCED_ACCEPT_APPLIED,
                    scope="chapter",
                    summary=f"第{chapter_plan.chapter_number}章应用 forced accept。",
                    related_object_type="chapter_review",
                    related_object_id=rewritten_review_row.id,
                    parent_event_id=str(repair_result_event.id or ""),
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
                payload={
                    "verdict": rewritten_review.verdict,
                    "issue_types": [
                        str(getattr(issue, "issue_type", getattr(issue, "rule_name", "")) or "")
                        for issue in rewritten_review.issues
                    ],
                    "issue_groups": [
                        str(getattr(issue, "issue_group", "") or issue_group_for_issue(
                            issue_type=str(getattr(issue, "issue_type", "") or ""),
                            rule_name=str(getattr(issue, "rule_name", "") or ""),
                        ))
                        for issue in rewritten_review.issues
                    ],
                    "forced_accept_applied": bool(rewritten_review.forced_accept_applied),
                },
                parent_event_id=str(repair_result_event.id or ""),
            )
            current_context = updated_context
            current_output = rewritten_output
            current_draft = rewritten_draft
            current_review = rewritten_review
            current_review_row = rewritten_review_row
            if rewritten_review.verdict != "fail":
                return rewritten_output, rewritten_review, False
        return current_output, current_review, bool(current_review.forced_accept_applied)

    @staticmethod
    def _rewrite_scope_sequence(initial_scope: str, max_attempts: int) -> list[str]:
        scope_order = ["scene", "band", "arc"]
        try:
            start = scope_order.index(initial_scope)
        except ValueError:
            start = 0
        sequence = scope_order[start:]
        while len(sequence) < max_attempts:
            sequence.append(sequence[-1] if sequence else "arc")
        return sequence[:max_attempts]

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
        repair_scope: str,
        repair_instruction: RepairInstruction,
    ) -> dict[str, object]:
        current_plan = repo.get_chapter_experience_plan(project_id, chapter_plan.chapter_number) or ChapterExperiencePlan()
        band_schedule = repo.get_band_experience_plan_for_chapter(project_id, chapter_plan.chapter_number)
        arc_structure = repo.get_latest_arc_structure_draft(project_id)
        patch = dict(repair_instruction.design_patch)
        patch["repair_scope"] = repair_scope

        if repair_scope == "scene":
            updated_plan = current_plan.model_copy(
                update=self._chapter_experience_patch_payload(current_plan, repair_instruction)
            )
            chapter_plan.experience_plan_json = json.dumps(
                updated_plan.model_dump(mode="json"),
                ensure_ascii=False,
            )
            session.add(chapter_plan)
            return updated_plan.model_dump(mode="json")

        if repair_scope == "band" and band_schedule is not None:
            updated_schedule = BandDelightSchedule.model_validate(
                self._band_schedule_patch_payload(band_schedule, repair_instruction)
            )
            self._replace_band_schedule(
                session=session,
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                schedule=updated_schedule,
                arc_structure=arc_structure,
            )
            return updated_schedule.model_dump(mode="json")

        if repair_scope == "arc" and arc_structure is not None:
            updated_arc_payoff = ArcPayoffMap.model_validate(
                self._arc_payoff_patch_payload(
                    ArcPayoffMap.model_validate(json.loads(arc_structure.arc_payoff_map_json or "{}") or {}),
                    repair_instruction,
                )
            )
            arc_structure.arc_payoff_map_json = json.dumps(
                updated_arc_payoff.model_dump(mode="json"),
                ensure_ascii=False,
            )
            session.add(arc_structure)
            current_schedule = band_schedule or BandDelightSchedule(
                band_id=f"band:{chapter_plan.chapter_number}:{chapter_plan.chapter_number}",
                chapter_start=chapter_plan.chapter_number,
                chapter_end=chapter_plan.chapter_number,
            )
            active_band = [
                repo.get_chapter_plan(project_id, number)
                for number in range(current_schedule.chapter_start, current_schedule.chapter_end + 1)
            ]
            active_band = [plan for plan in active_band if plan is not None]
            regenerated_schedule = self.arc_envelope_manager._derive_band_delight_schedule(
                band_id=current_schedule.band_id,
                chapter_start=current_schedule.chapter_start,
                chapter_end=current_schedule.chapter_end,
                structure=self._structure_data_from_row(arc_structure),
                active_band=active_band,
            )
            self._replace_band_schedule(
                session=session,
                repo=repo,
                project_id=project_id,
                chapter_number=chapter_plan.chapter_number,
                schedule=regenerated_schedule,
                arc_structure=arc_structure,
            )
            return updated_arc_payoff.model_dump(mode="json")

        updated_plan = current_plan.model_copy(
            update=self._chapter_experience_patch_payload(current_plan, repair_instruction)
        )
        chapter_plan.experience_plan_json = json.dumps(updated_plan.model_dump(mode="json"), ensure_ascii=False)
        session.add(chapter_plan)
        return updated_plan.model_dump(mode="json")

    def _replace_band_schedule(
        self,
        *,
        session: Session,
        repo: StateRepository,
        project_id: str,
        chapter_number: int,
        schedule: BandDelightSchedule,
        arc_structure: ArcStructureDraft | None,
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
    def _chapter_experience_patch_payload(
        current_plan: ChapterExperiencePlan,
        repair_instruction: RepairInstruction,
    ) -> dict[str, object]:
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
        if repair_instruction.failure_type == "stall" and not update["progress_markers"]:
            update["progress_markers"] = ["让主目标出现不可逆推进"]
        if repair_instruction.failure_type == "stall" and not update["question_hook"]:
            update["question_hook"] = "补出一个比当前更强的新问题"
        return update

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
                    updater.mark_chapter_status(project_id, chapter_num, "needs_review")
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
                    updater.mark_chapter_status(project_id, chapter_num, "needs_review")
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
                    updater.mark_chapter_status(project_id, chapter_num, "needs_review")
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
                    updater.mark_chapter_status(project_id, chapter_num, "needs_review")
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
                    updater.mark_chapter_status(project_id, chapter_num, "needs_review")
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
                    updater.mark_chapter_status(project_id, chapter_num, "needs_review")
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
                updater.mark_chapter_status(project_id, chapter_num, status)
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
                checkpoint_pause = False
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
                    if checkpoint_row is not None and checkpoint_row.status in {"warn", "fail", "error"}:
                        checkpoint_pause = True
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
                if checkpoint_pause or manual_after_accept is not None or manual_band_end is not None:
                    if checkpoint_pause and checkpoint_row is not None:
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
    ) -> WriterOutput | None:
        max_attempts = max(1, int(self.config.blackbox_writer_attention_retries))
        last_error: Exception | None = None
        saw_transient_error = False
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
                    payload={
                        "attempt_no": attempt,
                        "max_attempts": max_attempts,
                        "stage": "writing_chapter",
                        "model_profile_id": model_profile_id,
                        "model": model_name,
                    },
                )
                output = self.writer.write_chapter(context)
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
                    payload={
                        "attempt_no": attempt,
                        "max_attempts": max_attempts,
                        "stage": "writing_chapter",
                        "model_profile_id": model_profile_id,
                        "model": model_name,
                        "duration_ms": duration_ms,
                    },
                )
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.STAGE_DURATION_SUMMARY,
                    scope="chapter",
                    summary=f"第{chapter_number}章 writer 调用耗时 {duration_ms}ms。",
                    payload={
                        "stage": "writing_chapter",
                        "model_profile_id": model_profile_id,
                        "model": model_name,
                        "attempt_no": attempt,
                        "duration_ms": duration_ms,
                    },
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
                logger.warning(
                    "Writer failed for chapter %d on attempt %d/%d: %s",
                    chapter_number,
                    attempt,
                    max_attempts,
                    exc,
                )
                self._record_decision_event(
                    updater=updater,
                    project_id=project_id,
                    chapter_number=chapter_number,
                    event_family="runtime_observation",
                    event_type=DecisionEventType.LLM_REQUEST_FAILED,
                    scope="chapter",
                    summary=f"Writer 第 {attempt}/{max_attempts} 次调用失败：{exc}",
                    payload={
                        "attempt_no": attempt,
                        "max_attempts": max_attempts,
                        "is_transient": is_transient,
                        "model_profile_id": model_profile_id,
                        "model": model_name,
                        "duration_ms": duration_ms,
                        "error_class": exc.__class__.__name__,
                        "error_summary": str(exc),
                        "stage": "writing_chapter",
                    },
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
                        payload={
                            "attempt_no": attempt + 1,
                            "previous_attempt": attempt,
                            "delay_seconds": delay,
                            "stage": "writing_chapter",
                            "model_profile_id": model_profile_id,
                            "model": model_name,
                        },
                    )
                    time.sleep(delay)
        if last_error is not None:
            try:
                preview_output = self.writer.write_preview_chapter(
                    context,
                    timeout_seconds=self.writer.scene_call_timeout_seconds,
                    max_attempts=3 if saw_transient_error else 2,
                    retry_on_timeout=True,
                )
                preview_output.generation_meta.update(
                    {
                        "fallback_from_writer_error": True,
                        "writer_fallback_error": str(last_error),
                    }
                )
                logger.warning(
                    "Writer preview fallback succeeded for chapter %d after writer failure: %s",
                    chapter_number,
                    last_error,
                )
                return preview_output
            except Exception as preview_exc:  # noqa: BLE001
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
                payload={
                    "stage": parent_stage,
                    "model_profile_id": str(item.get("to_profile_id") or ""),
                    "model": str(item.get("to_model") or ""),
                    "error_summary": str(item.get("reason") or ""),
                    "from_model_profile_id": str(item.get("from_profile_id") or ""),
                    "from_model": str(item.get("from_model") or ""),
                },
            )

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
        try:
            filtered_state_changes = self._filter_supported_state_changes(
                writer_output.state_changes
            )
            filtered_events = self._filter_resolvable_events(
                repo,
                project_id,
                chapter_number,
                writer_output.new_events,
            )
            updater.apply_state_changes(
                project_id, chapter_number, filtered_state_changes
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
            return None
        except Exception:
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
            return frozen_path or None

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

    def _run_provisional_band_preview(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_plans: list[ChapterPlan],
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
                    max_attempts=2,
                    retry_on_timeout=True,
                )
                verdict = preview_checker.check(project_id, writer_output)
                verdict = self._normalize_provisional_verdict(writer_output, verdict)
                artifact_paths = self.artifact_store.save_writer_output(
                    project_id=project_id,
                    chapter_number=chapter_plan.chapter_number,
                    writer_output=writer_output,
                    namespace_root=namespace_root,
                )
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
                        artifact_meta_path=artifact_paths["meta_path"],
                        draft_blob_path=artifact_paths["writer_output"].draft_blob_path,
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
                        "artifact_meta_path": artifact_paths["meta_path"],
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
        # Arc plan version
        arc = updater.create_arc_plan(
            project_id=project_id,
            arc_synopsis=arc_plan.get("arc_synopsis", ""),
        )

        # Chapter plans
        chapters = arc_plan.get("chapters", [])
        for i in range(num_chapters):
            ch = chapters[i] if i < len(chapters) else {}
            updater.create_chapter_plan(
                project_id=project_id,
                arc_plan_id=arc.id,
                chapter_number=ch.get("chapter_number", i + 1),
                title=ch.get("title", f"第{i+1}章"),
                one_line=ch.get("one_line", ""),
                goals=ch.get("goals", []),
            )

        # Entities: characters
        entity_map: dict[str, str] = {}  # name -> entity_id
        for char_data in arc_plan.get("characters", []):
            entity = updater.create_entity(
                project_id=project_id,
                kind="character",
                name=char_data.get("name", "未命名"),
                description=char_data.get("description", ""),
                aliases=char_data.get("aliases", []),
                importance=char_data.get("importance", 5),
                chapter=0,
            )
            entity_map[entity.name] = entity.id
            initial_state = char_data.get("initial_state", {})
            if initial_state:
                updater.create_entity_state(entity.id, 0, initial_state)

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

        # Initial timeline
        initial_time = arc_plan.get("initial_time", {})
        if initial_time:
            updater.create_time_point(
                project_id=project_id,
                label=initial_time.get("label", "故事开始"),
                ordinal=0,
                description=initial_time.get("description", ""),
            )
