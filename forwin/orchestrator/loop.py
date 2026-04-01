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
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from forwin.checker.rules import ContinuityChecker
from forwin.config import Config
from forwin.director import ArcDirector
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.retrieval import RetrievalBroker
from forwin.state.repo import StateRepository
from forwin.state.schema import KNOWN_STATE_FIELDS
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore
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

    @property
    def status(self) -> str:
        if self.failed_chapters and not self.completed_chapters:
            return "failed"
        if self.failed_chapters:
            return "partial_failed"
        return "completed"


class WritingOrchestrator:
    """Orchestrates the full chapter-generation pipeline."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.from_env()

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
        )
        self.artifact_store = ArtifactStore(self.config.artifact_root)
        self.writer = ChapterWriter(
            llm_client=self.llm_client,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            writer_mode=self.config.writer_mode,
            default_scene_count=self.config.default_scene_count,
            max_scene_count=self.config.max_scene_count,
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

            print(f"项目创建完成: {project.title}")
            print(f"项目ID: {project_id}")

            # Step 3: Generate chapters --------------------------------------
            completed_chapters: list[int] = []
            failed_chapters: list[int] = []
            for chapter_num in range(1, num_chapters + 1):
                print(f"\n{'─'*60}")
                print(f"正在生成第 {chapter_num} 章...")
                print(f"{'─'*60}")

                chapter_plan = repo.get_chapter_plan(project_id, chapter_num)
                if chapter_plan is None:
                    logger.error("Chapter plan %d not found, skipping.", chapter_num)
                    failed_chapters.append(chapter_num)
                    continue

                try:
                    # 3a. Assemble context
                    context = self.retrieval_broker.build_chapter_context(
                        repo, project_id, chapter_plan
                    )

                    # 3b. Write chapter
                    writer_output = self.writer.write_chapter(context)
                    artifact_paths = self.artifact_store.save_writer_output(
                        project_id=project_id,
                        chapter_number=chapter_num,
                        writer_output=writer_output,
                    )
                    writer_output = writer_output.model_copy(
                        update={
                            "draft_blob_path": artifact_paths["draft_blob_path"],
                            "generation_meta": {
                                **writer_output.generation_meta,
                                "artifact_meta_path": artifact_paths["meta_path"],
                            },
                        }
                    )

                    # 3c. Continuity check
                    verdict = checker.check(project_id, writer_output)

                    # 3d. Save draft + review
                    draft = updater.save_draft(
                        chapter_plan_id=chapter_plan.id,
                        writer_output=writer_output,
                        raw_response=artifact_paths["meta_path"],
                        model_name=self.config.minimax_model,
                    )
                    updater.save_review(draft.id, verdict)
                    updater.mark_chapter_status(project_id, chapter_num, "drafted")
                    session.commit()

                    repo, updater, checker = self._make_state_helpers(session)

                    # 3e. Update canon state in a separate transaction so a dirty
                    # structured candidate cannot wipe out the saved draft.
                    try:
                        filtered_state_changes = self._filter_supported_state_changes(
                            writer_output.state_changes
                        )
                        updater.apply_state_changes(
                            project_id, chapter_num, filtered_state_changes
                        )
                        updater.apply_events(
                            project_id, chapter_num, writer_output.new_events
                        )
                        updater.apply_thread_beats(
                            project_id, chapter_num, writer_output.thread_beats
                        )
                        if writer_output.time_advance:
                            updater.apply_time_advance(
                                project_id, chapter_num, writer_output.time_advance
                            )
                    except Exception:
                        logger.exception(
                            "Canon update failed for chapter %d; keeping saved draft and review.",
                            chapter_num,
                        )
                        session.rollback()
                        repo, updater, checker = self._make_state_helpers(session)

                    # 3f. Mark chapter status
                    status = "accepted" if verdict.verdict != "fail" else "drafted"
                    updater.mark_chapter_status(project_id, chapter_num, status)
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

                # 3g. Print progress
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

            result = RunResult(
                project_id=project_id,
                requested_chapters=num_chapters,
                completed_chapters=completed_chapters,
                failed_chapters=failed_chapters,
            )
            print(f"\n{'='*60}")
            if result.status == "completed":
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
