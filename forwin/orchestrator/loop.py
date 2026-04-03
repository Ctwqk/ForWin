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
from typing import Any, Callable

from sqlalchemy.orm import Session

from forwin.checker.rules import ContinuityChecker
from forwin.config import Config
from forwin.director import ArcDirector
from forwin.models import ProvisionalChapterLedger, new_id
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ChapterPlan, Project
from forwin.protocol.review import ReviewVerdict
from forwin.orchestrator.phase3 import (
    PacingStrategist,
    ReplanGovernor,
    StageAnalyzer,
    save_stage_analysis,
)
from forwin.orchestrator.phase4 import (
    NPCIntentGenerator,
    WorldSimulator,
    save_npc_intents,
    save_world_turn,
)
from forwin.orchestrator.phase24 import ArcEnvelopeManager, ProvisionalBandPreview
from forwin.retrieval import RetrievalBroker, create_memory_index
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

    @property
    def status(self) -> str:
        if self.paused_chapters:
            return "needs_review"
        if self.failed_chapters and not self.completed_chapters:
            return "failed"
        if self.failed_chapters:
            return "partial_failed"
        return "completed"


class WritingOrchestrator:
    """Orchestrates the full chapter-generation pipeline."""

    def __init__(
        self,
        config: Config | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config or Config.from_env()
        self.progress_callback = progress_callback

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

            # Step 1: Plan arc -----------------------------------------------
            print(f"\n{'='*60}")
            print("正在规划故事大纲...")
            print(f"{'='*60}")
            arc_plan = self.arc_director.plan_arc(premise, genre, num_chapters)

            # Step 2: Create project + seed state ----------------------------
            title = arc_plan.get("arc_synopsis", premise[:30])[:60]
            setting_summary = arc_plan.get("setting_summary", "")
            project = updater.create_project(
                title=title,
                premise=premise,
                genre=genre,
                setting_summary=setting_summary,
            )
            project_id = project.id

            self._seed_state(updater, project_id, arc_plan, num_chapters)
            session.commit()
            self._emit_progress(
                "project_created",
                project_id=project_id,
                title=project.title,
            )

            self.arc_envelope_manager.ensure_active_arc_resolution(
                session=session,
                project_id=project_id,
                activation_chapter=1,
            )
            session.commit()

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

            return result

        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _emit_progress(self, event: str, **payload: Any) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, payload)
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring progress callback error.", exc_info=True)

    def continue_project(self, project_id: str) -> RunResult:
        session: Session = self._SessionFactory()
        try:
            repo, updater, checker = self._make_state_helpers(session)
            project = session.get(Project, project_id)
            if project is None:
                raise ValueError(f"项目不存在: {project_id}")

            chapter_plans = session.query(ChapterPlan).filter(
                ChapterPlan.project_id == project_id
            ).order_by(ChapterPlan.chapter_number).all()
            if not chapter_plans:
                raise ValueError(f"项目没有章节规划: {project_id}")

            waiting_review = [
                plan.chapter_number
                for plan in chapter_plans
                if plan.status == "needs_review"
            ]
            if waiting_review:
                waiting = ", ".join(str(number) for number in waiting_review)
                raise ValueError(f"仍有章节等待 review：{waiting}")

            chapter_numbers = [
                plan.chapter_number
                for plan in chapter_plans
                if plan.status in {"planned", "failed"}
            ]
            if not chapter_numbers:
                return RunResult(
                    project_id=project_id,
                    requested_chapters=len(chapter_plans),
                )

            self.arc_envelope_manager.ensure_active_arc_resolution(
                session=session,
                project_id=project_id,
                activation_chapter=min(chapter_numbers),
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
            session.close()

    def accept_review(self, project_id: str, chapter_number: int) -> dict[str, str]:
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

        for chapter_num in chapter_numbers:
            print(f"\n{'─'*60}")
            print(f"正在生成第 {chapter_num} 章...")
            print(f"{'─'*60}")

            chapter_plan = repo.get_chapter_plan(project_id, chapter_num)
            if chapter_plan is None:
                logger.error("Chapter plan %d not found, skipping.", chapter_num)
                failed_chapters.append(chapter_num)
                continue

            try:
                context = self.retrieval_broker.build_chapter_context(
                    repo, project_id, chapter_plan
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
                    session.commit()
                    break
                artifact_paths = self.artifact_store.save_writer_output(
                    project_id=project_id,
                    chapter_number=chapter_num,
                    writer_output=writer_output,
                )
                writer_output = artifact_paths["writer_output"].model_copy(
                    update={
                        "generation_meta": {
                            **writer_output.generation_meta,
                            "artifact_meta_path": artifact_paths["meta_path"],
                        },
                    }
                )

                verdict = checker.check(project_id, writer_output)

                draft = updater.save_draft(
                    chapter_plan_id=chapter_plan.id,
                    writer_output=writer_output,
                    raw_response=artifact_paths["meta_path"],
                    model_name=self.config.minimax_model,
                )
                updater.save_review(draft.id, verdict)
                updater.mark_chapter_status(project_id, chapter_num, "drafted")
                session.commit()

                if self.config.operation_mode == "checkpoint":
                    updater.mark_chapter_status(project_id, chapter_num, "needs_review")
                    session.commit()
                    paused_chapters.append(chapter_num)
                    break
                if self.config.operation_mode == "copilot" and verdict.verdict != "pass":
                    updater.mark_chapter_status(project_id, chapter_num, "needs_review")
                    session.commit()
                    paused_chapters.append(chapter_num)
                    break

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
                    repo, updater, checker = self._make_state_helpers(session)
                    break

                status = "accepted" if verdict.verdict != "fail" else "drafted"
                updater.mark_chapter_status(project_id, chapter_num, status)
                if status == "accepted":
                    self.retrieval_broker.memory_index.upsert_chapter(
                        project_id=project_id,
                        chapter_number=chapter_num,
                        title=writer_output.title,
                        summary=writer_output.end_of_chapter_summary,
                        body=writer_output.body,
                    )
                    self._run_phase3_pass(
                        session=session,
                        project_id=project_id,
                        chapter_number=chapter_num,
                    )
                session.commit()

            except Exception as exc:
                logger.exception("Chapter %d failed.", chapter_num)
                session.rollback()
                repo, updater, checker = self._make_state_helpers(session)
                updater.mark_chapter_status(project_id, chapter_num, "failed")
                session.commit()
                failed_chapters.append(chapter_num)
                print(f"  ✗ 第{chapter_num}章失败: {exc}")
                continue

            completed_chapters.append(chapter_num)

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
        for attempt in range(1, max_attempts + 1):
            try:
                return self.writer.write_chapter(context)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "Writer failed for chapter %d on attempt %d/%d: %s",
                    chapter_number,
                    attempt,
                    max_attempts,
                    exc,
                )
                if self._is_timeout_like(exc):
                    logger.warning(
                        "Writer timeout detected for chapter %d; skipping extra retries.",
                        chapter_number,
                    )
                    break
        if self.config.operation_mode == "blackbox":
            updater.mark_chapter_status(project_id, chapter_number, "needs_review")
            frozen_path = ""
            if self.config.freeze_failed_candidates:
                frozen_path = self.artifact_store.save_frozen_candidate(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    payload={
                        "reason": "writer-needs-attention",
                        "chapter_number": chapter_number,
                        "project_id": project_id,
                        "error": str(last_error) if last_error else "writer failed",
                    },
                )
            if frozen_path:
                frozen_artifacts.append(frozen_path)
            paused_chapters.append(chapter_number)
            return None
        raise last_error or RuntimeError("writer failed")

    @staticmethod
    def _is_timeout_like(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            token in message
            for token in ("timed out", "timeout", "read operation timed out")
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
                    retry_on_timeout=False,
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
        return any(
            token in text
            for token in (
                "timed out",
                "timeout",
                "read operation timed out",
                "json generation failed",
                "llmjsonparseerror",
                "connection reset",
            )
        )

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
        return ReviewVerdict.model_validate(
            {
                "verdict": review.verdict,
                "issues": json.loads(review.issues_json or "[]"),
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
