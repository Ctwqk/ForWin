from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.observability.payloads import event_error_payload
from forwin.state.updater import StateUpdater
from forwin.world_model.compiler import WorldModelCompiler

from .arc_materializer import GenesisArcMaterializer
from .chapter_materializer import GenesisChapterMaterializer
from .commands import StartWritingCommand, StartWritingHandoffResult
from .map_bootstrap import GenesisMapBootstrap


class GenesisHandoffService:
    """Executes the explicit manual handoff from Genesis into writing runtime."""

    def __init__(self, owner) -> None:  # noqa: ANN001
        self.owner = owner
        self.arc_materializer = GenesisArcMaterializer(owner)
        self.chapter_materializer = GenesisChapterMaterializer(owner)
        self.map_bootstrap = GenesisMapBootstrap()

    def start_writing(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        command: StartWritingCommand,
    ) -> StartWritingHandoffResult:
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
        in_map_bootstrap = False
        try:
            decision = updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project.id,
                    scope="project",
                    event_family="business_event",
                    event_type=DecisionEventType.START_WRITING_REQUESTED,
                    actor_type="manual_ui",
                    summary="Genesis 已交接到写作流程。",
                    related_object_type="book_genesis_revision",
                    related_object_id=revision.id,
                )
            )
            decision_id = decision.id
            existing_arc_count = int(
                session.execute(
                    select(func.count(ArcPlanVersion.id)).where(ArcPlanVersion.project_id == project.id)
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
            existing_chapter_count = int(
                session.execute(
                    select(func.count(ChapterPlan.id)).where(ChapterPlan.arc_plan_id == active_arc.id)
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
            pack = self.owner.load_pack(revision)
            world = pack.get("world") if isinstance(pack.get("world"), dict) else {}
            world_bible = world.get("world_bible") if isinstance(world.get("world_bible"), dict) else {}
            if not str(project.setting_summary or "").strip():
                project.setting_summary = str(world_bible.get("overview", "") or "").strip()
            in_map_bootstrap = True
            map_summary = self.map_bootstrap.bootstrap_book_map_from_genesis(
                session=session,
                updater=updater,
                project=project,
                revision=revision,
                pack=pack,
                decision_event_id=decision.id,
            )
            in_map_bootstrap = False
            WorldModelCompiler(session).bootstrap_from_genesis(project.id)
            project.creation_status = "writing"
            session.add(project)
            active_chapter_count = int(
                session.execute(
                    select(func.count(ChapterPlan.id)).where(ChapterPlan.arc_plan_id == active_arc.id)
                ).scalar_one()
                or 0
            )
            session.flush()
            return StartWritingHandoffResult(
                project_id=project.id,
                active_arc_id=active_arc.id,
                active_arc_number=int(active_arc.arc_number or 0),
                created_arc_count=max(0, len(arcs) - existing_arc_count),
                created_chapter_plan_count=max(0, active_chapter_count - existing_chapter_count),
                active_chapter_plan_count=active_chapter_count,
                map_bootstrap_summary=map_summary,
                project_status="writing",
            )
        except ValueError as exc:
            failure_summary = str(exc) or "Genesis handoff failed."
            if (
                not in_map_bootstrap
                and "map" not in failure_summary.lower()
                and "BookMap" not in failure_summary
                and "地图" not in failure_summary
            ):
                raise
            session.rollback()
            failure_updater = StateUpdater(session)
            failure_updater.save_decision_event(
                DecisionEventInfo(
                    project_id=project_id_for_failure,
                    scope="project",
                    event_family="runtime_observation",
                    event_type=DecisionEventType.MAP_GENERATION_FAILED,
                    actor_type="system",
                    summary="Genesis map_atlas 生成 Scheme C BookMap 失败。",
                    reason=failure_summary,
                    payload=event_error_payload(
                        exc,
                        stage="map_generation",
                        failure_summary=failure_summary,
                    ),
                    related_object_type="book_genesis_revision",
                    related_object_id=revision_id_for_failure,
                    parent_event_id=decision_id,
                )
            )
            session.flush()
            raise
