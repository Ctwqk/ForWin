from __future__ import annotations

from dataclasses import asdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
)
from forwin.canon_quality.rule_provenance import build_rule_handoff_summary
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.observability.payloads import event_error_payload
from forwin.state.updater import StateUpdater

from .arc_materializer import GenesisArcMaterializer
from .chapter_materializer import GenesisChapterMaterializer
from .commands import HandoffStageError, StartWritingCommand, StartWritingHandoffResult
from .map_bootstrap import GenesisMapBootstrap, GenesisMapBootstrapError
from .world_seed_committer import WorldSeedCommitError, WorldSeedCommitter


class GenesisHandoffService:
    """Executes the explicit manual handoff from Genesis into writing runtime."""

    def __init__(self, owner) -> None:  # noqa: ANN001
        self.owner = owner
        self.arc_materializer = GenesisArcMaterializer(owner)
        self.chapter_materializer = GenesisChapterMaterializer(owner)
        self.map_bootstrap = GenesisMapBootstrap()
        self.world_seed_committer = WorldSeedCommitter()

    def start_writing(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        command: StartWritingCommand,
    ) -> StartWritingHandoffResult:
        """Arcs, then the book map, then the chapter-0 world seed, then arc-1 chapter plans.

        Only flushes. On a ``HandoffStageError`` the session is rolled back and holds
        just the stage's failure event, flushed, when the error is re-raised.
        """
        actor_type = str(command.actor_type or "").strip()
        if actor_type != "manual_ui":
            raise ValueError("start-writing 必须由 manual_ui 显式触发。")
        project = session.get(Project, command.project_id)
        if project is None:
            raise ValueError("项目不存在")
        if str(project.creation_status or "") != "genesis_ready":
            raise ValueError("Genesis 尚未完成锁定，不能启动写作。")
        revision = self.owner.active_revision(session, project)
        if revision is None:
            raise ValueError("Genesis revision 不存在。")

        project_id_for_failure = project.id
        revision_id_for_failure = revision.id
        decision_id = ""
        try:
            decision = updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project.id,
                    scope="project",
                    event_family="business_event",
                    event_type=DecisionEventType.START_WRITING_REQUESTED,
                    actor_type="manual_ui",
                    summary="Genesis 已交接到写作流程。",
                    payload={
                        "rule_provenance_handoff": build_rule_handoff_summary(
                            session,
                            project_id=project.id,
                        )
                    },
                    related_object_type="book_genesis_revision",
                    related_object_id=revision.id,
                )
            )
            decision_id = decision.id
            existing_arc_count = int(
                session.execute(
                    select(func.count(ArcPlanVersion.id)).where(
                        ArcPlanVersion.project_id == project.id
                    )
                ).scalar_one()
                or 0
            )
            arcs = self.arc_materializer.materialize_book_arcs(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
            )
            active_arc = next((row for row in arcs if row.status == "active"), None)
            if active_arc is None:
                raise ValueError("Genesis blueprint 缺少 active arc。")
            pack = self.owner.load_pack(revision)
            map_summary = self.map_bootstrap.bootstrap_book_map_from_genesis(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                pack=pack,
                decision_event_id=decision.id,
            )
            # The seed places actors on map nodes, and arc-1 planning reads the seeded BookState.
            seed_summary = self.world_seed_committer.commit(
                session=session,
                project=project,
                revision=revision,
                pack=pack,
            )
            existing_chapter_count = int(
                session.execute(
                    select(func.count(ChapterPlan.id)).where(
                        ChapterPlan.arc_plan_id == active_arc.id
                    )
                ).scalar_one()
                or 0
            )
            self.chapter_materializer.materialize_arc_chapter_plans(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                arc_number=active_arc.arc_number,
                decision_event_id=decision.id,
                ensure_arc_map=False,
            )
            world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
            world_bible = (
                world.get("world_bible")
                if isinstance(world.get("world_bible"), dict)
                else {}
            )
            if not str(project.setting_summary or "").strip():
                project.setting_summary = str(
                    world_bible.get("overview", "") or ""
                ).strip()
            revision.status = "locked"
            session.add(revision)
            project.creation_status = "writing"
            session.add(project)
            active_chapter_count = int(
                session.execute(
                    select(func.count(ChapterPlan.id)).where(
                        ChapterPlan.arc_plan_id == active_arc.id
                    )
                ).scalar_one()
                or 0
            )
            session.flush()
            return StartWritingHandoffResult(
                project_id=project.id,
                active_arc_id=active_arc.id,
                active_arc_number=int(active_arc.arc_number or 0),
                created_arc_count=max(0, len(arcs) - existing_arc_count),
                created_chapter_plan_count=max(
                    0, active_chapter_count - existing_chapter_count
                ),
                active_chapter_plan_count=active_chapter_count,
                map_bootstrap_summary=map_summary,
                world_seed_summary=asdict(seed_summary),
                project_status="writing",
            )
        except HandoffStageError as exc:
            session.rollback()
            StateUpdater(session).save_decision_event(
                _stage_failure_event(
                    exc,
                    project_id=project_id_for_failure,
                    revision_id=revision_id_for_failure,
                    parent_event_id=decision_id,
                )
            )
            session.flush()
            raise


def _stage_failure_event(
    exc: HandoffStageError,
    *,
    project_id: str,
    revision_id: str,
    parent_event_id: str,
) -> DecisionEventInfo:
    if isinstance(exc, WorldSeedCommitError):
        event_type = DecisionEventType.WORLD_SEED_COMMIT_FAILED
        stage = "world_seed_commit"
        summary = "Genesis world_seed 写入第 0 章 BookState 失败。"
    elif isinstance(exc, GenesisMapBootstrapError):
        event_type = DecisionEventType.MAP_GENERATION_FAILED
        stage = "map_generation"
        summary = "Genesis map_atlas 生成 Scheme C BookMap 失败。"
    else:
        raise TypeError(f"no failure event for {type(exc).__name__}") from exc
    failure_summary = str(exc) or summary
    return DecisionEventInfo(
        project_id=project_id,
        scope="project",
        event_family="runtime_observation",
        event_type=event_type,
        actor_type="system",
        summary=summary,
        reason=failure_summary,
        payload=event_error_payload(
            exc,
            stage=stage,
            failure_summary=failure_summary,
            issues=list(exc.issues),
        ),
        related_object_type="book_genesis_revision",
        related_object_id=revision_id,
        parent_event_id=parent_event_id,
    )
