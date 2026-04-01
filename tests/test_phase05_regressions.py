from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import forwin.api as api_module
from forwin.config import Config
from forwin.director import ArcDirector
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.entity import EntityState
from forwin.models.draft import ChapterDraft
from forwin.models.event import CanonEvent, EventEntityLink
from forwin.models.publisher import (
    PublisherBrowserSession,
    PublisherExtensionClient,
    PublisherRawComment,
    PublisherUploadJob,
)
from forwin.models.project import ChapterPlan
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.protocol.context import (
    ChapterContextPack,
    EntitySnapshot,
    PlotThreadSnapshot,
    RelationSnapshot,
    TimelineSnapshot,
)
from forwin.protocol.state_change import EventCandidate, StateChangeCandidate
from forwin.protocol.writer import WriterOutput
from forwin.publishers import PublisherManager
from forwin.retrieval import RetrievalBroker
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore
from forwin.writer.chapter_writer import ChapterWriter


class Phase05RegressionTests(unittest.TestCase):
    def test_config_from_env_reads_shared_fields_once(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FORWIN_DB_PATH": "tmp/test.db",
                "TEMPERATURE": "0.42",
                "MAX_TOKENS": "4096",
            },
            clear=False,
        ):
            config = Config.from_env()

        self.assertEqual(config.db_path, "tmp/test.db")
        self.assertEqual(config.temperature, 0.42)
        self.assertEqual(config.max_tokens, 4096)

    def test_orchestrator_surfaces_partial_failures(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "orchestrator.db")
            orchestrator = WritingOrchestrator(
                Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "测试失败路径",
                        "setting_summary": "无",
                        "chapters": [
                            {
                                "chapter_number": 1,
                                "title": "第一章",
                                "one_line": "一",
                                "goals": ["g1"],
                            },
                            {
                                "chapter_number": 2,
                                "title": "第二章",
                                "one_line": "二",
                                "goals": ["g2"],
                            },
                        ],
                        "characters": [],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    if context.chapter_number == 2:
                        raise RuntimeError("simulated writer failure")
                    body = "正文" * 800
                    return WriterOutput(
                        chapter_number=1,
                        title="第一章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary="ok",
                        state_changes=[],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter

                result = orchestrator.run("p", "g", 2)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    statuses = [
                        (plan.chapter_number, plan.status)
                        for plan in session.execute(
                            select(ChapterPlan).order_by(ChapterPlan.chapter_number)
                        ).scalars()
                    ]
                    draft_count = session.execute(
                        select(func.count(ChapterDraft.id))
                    ).scalar_one()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "partial_failed")
            self.assertEqual(result.completed_chapters, [1])
            self.assertEqual(result.failed_chapters, [2])
            self.assertEqual(statuses, [(1, "accepted"), (2, "failed")])
            self.assertEqual(draft_count, 1)

    def test_orchestrator_keeps_draft_when_structured_state_is_dirty(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "dirty-state.db")
            orchestrator = WritingOrchestrator(
                Config(db_path=db_path, minimax_api_key="", minimax_model="fake-model")
            )
            try:
                def fake_plan_arc(premise: str, genre: str, num_chapters: int) -> dict:
                    return {
                        "arc_synopsis": "测试脏状态回退",
                        "setting_summary": "无",
                        "chapters": [
                            {
                                "chapter_number": 1,
                                "title": "第一章",
                                "one_line": "开场",
                                "goals": ["推进主线"],
                            }
                        ],
                        "characters": [
                            {
                                "name": "林夜",
                                "description": "主角",
                                "importance": 10,
                                "initial_state": {"location": "荒原", "status": "normal"},
                            }
                        ],
                        "locations": [],
                        "factions": [],
                        "relations": [],
                        "plot_threads": [],
                        "initial_time": {"label": "开始", "description": "开始"},
                    }

                def fake_write_chapter(context) -> WriterOutput:
                    body = "正文" * 800
                    return WriterOutput(
                        chapter_number=1,
                        title="第一章",
                        body=body,
                        char_count=len(body),
                        end_of_chapter_summary="ok",
                        state_changes=[
                            StateChangeCandidate(
                                entity_name="林夜",
                                entity_kind="character",
                                field="situation",
                                old_value="",
                                new_value="被盯上",
                                reason="测试脏字段",
                            )
                        ],
                        new_events=[],
                        thread_beats=[],
                        time_advance=None,
                    )

                orchestrator.arc_director.plan_arc = fake_plan_arc
                orchestrator.writer.write_chapter = fake_write_chapter

                result = orchestrator.run("p", "g", 1)

                engine = get_engine(db_path)
                session = get_session_factory(engine)()
                try:
                    statuses = [
                        (plan.chapter_number, plan.status)
                        for plan in session.execute(
                            select(ChapterPlan).order_by(ChapterPlan.chapter_number)
                        ).scalars()
                    ]
                    draft_count = session.execute(
                        select(func.count(ChapterDraft.id))
                    ).scalar_one()
                finally:
                    session.close()
                    engine.dispose()
            finally:
                orchestrator.llm_client.close()
                orchestrator.engine.dispose()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.completed_chapters, [1])
            self.assertEqual(result.failed_chapters, [])
            self.assertEqual(statuses, [(1, "accepted")])
            self.assertEqual(draft_count, 1)

    def test_apply_events_rejects_partial_event_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "events.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")

                with self.assertRaisesRegex(ValueError, "unknown entity"):
                    updater.apply_events(
                        project_id=project.id,
                        chapter_number=1,
                        events=[
                            EventCandidate(
                                summary="未知角色出现",
                                significance="major",
                                involved_entity_names=["不存在的人"],
                                roles=["protagonist"],
                            )
                        ],
                    )

                session.rollback()

                event_count = session.execute(
                    select(func.count(CanonEvent.id))
                ).scalar_one()
                link_count = session.execute(
                    select(func.count(EventEntityLink.id))
                ).scalar_one()
            finally:
                session.close()
                engine.dispose()

            self.assertEqual(event_count, 0)
            self.assertEqual(link_count, 0)

    def test_arc_director_is_independent_from_writer(self) -> None:
        class FakeLLMClient:
            def __init__(self) -> None:
                self.last_messages = None

            def chat(self, messages, temperature: float, max_tokens: int) -> str:
                self.last_messages = messages
                return (
                    '{"arc_synopsis":"弧线","setting_summary":"设定","chapters":[{"chapter_number":1,'
                    '"title":"第一章","one_line":"开场","goals":["目标"]}],"characters":[],'
                    '"locations":[],"factions":[],"relations":[],"plot_threads":[],"initial_time":'
                    '{"label":"开始","description":"开场"}}'
                )

        client = FakeLLMClient()
        director = ArcDirector(client, max_tokens=2048)

        plan = director.plan_arc("故事前提", "玄幻", 1)

        self.assertEqual(plan["arc_synopsis"], "弧线")
        self.assertEqual(plan["chapters"][0]["title"], "第一章")
        self.assertEqual(len(client.last_messages), 2)

    def test_typed_state_schema_rejects_unknown_new_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                entity = updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="林夜",
                    description="主角",
                )
                updater.create_entity_state(
                    entity_id=entity.id,
                    chapter=0,
                    state={"location": "黑井聚落", "status": "normal"},
                )
                session.commit()

                with self.assertRaisesRegex(ValueError, "Unsupported state field"):
                    updater.apply_state_changes(
                        project_id=project.id,
                        chapter_number=1,
                        changes=[
                            StateChangeCandidate(
                                entity_name="林夜",
                                entity_kind="character",
                                field="mystery_rank",
                                old_value="",
                                new_value="S",
                                reason="测试",
                            )
                        ],
                    )

                session.rollback()

                state_rows = session.execute(
                    select(EntityState).where(EntityState.entity_id == entity.id)
                ).scalars().all()
            finally:
                session.close()
                engine.dispose()

            self.assertEqual(len(state_rows), 1)

    def test_repo_resolves_entity_alias_without_full_entity_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "aliases.db")
            engine = get_engine(db_path)
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(title="书", premise="p", genre="g")
                entity = updater.create_entity(
                    project_id=project.id,
                    kind="character",
                    name="林夜",
                    description="主角",
                    aliases=["夜哥", "林哥"],
                )
                session.commit()

                repo = updater._repo
                resolved = repo.get_entity_by_name(project.id, "夜哥")
            finally:
                session.close()
                engine.dispose()

            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved.id, entity.id)

    def test_writer_marks_structured_extraction_as_degraded_after_retry_failure(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def chat(self, _messages, temperature: float, max_tokens: int, response_format=None) -> str:
                self.calls += 1
                if self.calls == 1:
                    return '{"title":"第一章","body":"%s"}' % ("正文" * 900)
                raise RuntimeError("structured extraction failed")

        writer = ChapterWriter(FakeClient(), writer_mode="single")
        context = ChapterContextPack(
            project_title="测试书",
            premise="前提",
            genre="玄幻",
            setting_summary="设定",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="开场",
            chapter_goals=["推进主线"],
        )

        output = writer.write_chapter(context)

        self.assertEqual(output.generation_meta["structured_extraction"], "degraded")
        self.assertEqual(output.new_events, [])
        self.assertEqual(output.state_changes, [])

    def test_retrieval_broker_applies_budget(self) -> None:
        broker = RetrievalBroker(
            context_budget_chars=1000,
            max_entities=2,
            max_threads=1,
            max_summaries=2,
        )

        class FakeRepo:
            def get_project(self, project_id):
                return SimpleNamespace(
                    title="书",
                    premise="设定" * 20,
                    genre="玄幻",
                    setting_summary="背景" * 20,
                )

            def get_active_entities(self, project_id):
                return [
                    EntitySnapshot(
                        entity_id="e1",
                        kind="character",
                        name="甲",
                        importance=9,
                        aliases=[],
                        description="主角" * 20,
                        current_state={"location": "A", "status": "normal"},
                    ),
                    EntitySnapshot(
                        entity_id="e2",
                        kind="character",
                        name="乙",
                        importance=8,
                        aliases=[],
                        description="配角" * 20,
                        current_state={"location": "B", "status": "normal"},
                    ),
                    EntitySnapshot(
                        entity_id="e3",
                        kind="character",
                        name="丙",
                        importance=1,
                        aliases=[],
                        description="路人" * 20,
                        current_state={"location": "C", "status": "normal"},
                    ),
                ]

            def get_active_relations(self, project_id):
                return [
                    RelationSnapshot(
                        source_name="甲",
                        target_name="乙",
                        relation_type="盟友",
                        description="一起行动",
                    ),
                    RelationSnapshot(
                        source_name="丙",
                        target_name="路人甲",
                        relation_type="路过",
                        description="无关",
                    ),
                ]

            def get_active_threads(self, project_id):
                return [
                    PlotThreadSnapshot(
                        thread_id="t1",
                        name="主线",
                        description="主线" * 20,
                        status="active",
                        priority=1,
                        recent_beats=["推进一", "推进二"],
                    ),
                    PlotThreadSnapshot(
                        thread_id="t2",
                        name="支线",
                        description="支线" * 20,
                        status="active",
                        priority=3,
                        recent_beats=["支线"],
                    ),
                ]

            def get_chapter_summaries(self, project_id, chapter_number):
                return ["摘要一" * 20, "摘要二" * 20, "摘要三" * 20]

            def get_current_timeline(self, project_id):
                return TimelineSnapshot(current_time_label="第一天", ordinal=1)

        chapter_plan = SimpleNamespace(
            chapter_number=3,
            title="第三章",
            one_line="推进主线",
            goals_json='["推进主线","保留悬念"]',
        )
        context = broker.build_chapter_context(FakeRepo(), "project-1", chapter_plan)

        self.assertLessEqual(len(context.active_entities), 2)
        self.assertLessEqual(len(context.active_threads), 1)
        self.assertLessEqual(len(context.previous_chapter_summaries), 2)
        self.assertTrue(all(rel.source_name != "丙" for rel in context.active_relations))

    def test_artifact_store_namespaces_by_project(self) -> None:
        with TemporaryDirectory() as tmp:
            store = ArtifactStore(tmp)
            writer_output = WriterOutput(
                chapter_number=1,
                title="第一章",
                body="正文内容",
                char_count=4,
                end_of_chapter_summary="摘要",
                generation_meta={"mode": "scene"},
            )
            paths = store.save_writer_output("project-xyz", 1, writer_output)

            self.assertIn("projects/project-xyz/chapters/1", paths["draft_blob_path"])
            self.assertTrue(Path(paths["draft_blob_path"]).exists())
            self.assertTrue(Path(paths["meta_path"]).exists())

    def test_scene_writer_flow(self) -> None:
        class FakeLLMClient:
            def __init__(self) -> None:
                self.responses = [
                    '{"scenes":[{"scene_no":1,"objective":"相遇","must_progress_points":["主角遇敌"],"time_hint":"清晨","location_hint":"荒原","involved_entities":["林夜"],"micro_hook":"敌人逼近","target_chars":700},{"scene_no":2,"objective":"逃离","must_progress_points":["主角脱身"],"time_hint":"午后","location_hint":"峡谷","involved_entities":["林夜"],"micro_hook":"更大危机","target_chars":700}]}',
                    '{"text":"scene one","micro_summary":"相遇总结","scene_time_point":"清晨","scene_location_id":"荒原","involved_entities":["林夜"]}',
                    '{"text":"scene two","micro_summary":"逃离总结","scene_time_point":"午后","scene_location_id":"峡谷","involved_entities":["林夜"]}',
                    '{"title":"拼接章","body":"scene one\\nscene two","end_of_chapter_summary":"本章完成逃离"}',
                    '{"state_changes":[{"entity_name":"林夜","entity_kind":"character","field":"location","old_value":"荒原","new_value":"峡谷","reason":"逃离成功"}],"new_events":[{"summary":"林夜逃离","significance":"major","involved_entity_names":["林夜"],"roles":["protagonist"]}],"thread_beats":[{"thread_name":"主线","beat_type":"escalation","description":"危机升级"}],"time_advance":{"new_time_label":"第二天","duration_description":"半日后"}}',
                ]

            def chat(self, messages, temperature: float, max_tokens: int) -> str:
                return self.responses.pop(0)

        writer = ChapterWriter(
            FakeLLMClient(),
            writer_mode="scene",
            default_scene_count=2,
            max_scene_count=3,
        )
        context = SimpleNamespace(
            project_title="书",
            premise="前提",
            genre="玄幻",
            setting_summary="背景",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="相遇并逃离",
            chapter_goals=["相遇", "逃离"],
            previous_chapter_summaries=[],
            active_entities=[],
            active_relations=[],
            active_threads=[],
            timeline=None,
        )

        output = writer.write_chapter(context)

        self.assertEqual(output.title, "拼接章")
        self.assertEqual(len(output.scene_outputs), 2)
        self.assertEqual(output.generation_meta["mode"], "scene")
        self.assertEqual(output.generation_meta["call_count"], 5)
        self.assertEqual(output.state_changes[0].field, "location")

    def _make_publisher_manager(self) -> tuple[TemporaryDirectory, object, PublisherManager]:
        tmp = TemporaryDirectory()
        db_path = str(Path(tmp.name) / "publisher.db")
        engine = get_engine(db_path)
        init_db(engine)
        session_factory = get_session_factory(engine)
        return tmp, engine, PublisherManager(session_factory, extension_api_key="secret")

    def test_publisher_manager_tracks_extension_heartbeat_and_stale_state(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertFalse(items["qidian"]["connected"])
            self.assertFalse(items["qidian"]["extension_online"])
            self.assertEqual(items["qidian"]["supported_login_methods"], ["scan"])

            manager.record_extension_heartbeat(
                client_id="client-1",
                extension_version="0.1.0",
                browser_name="Chrome",
                browser_version="123.0",
                backend_base_url="http://192.168.31.10:8899",
                platforms=[
                    {
                        "platform": "qidian",
                        "connected": True,
                        "login_method": "scan",
                        "last_error": "",
                    },
                    {
                        "platform": "fanqie",
                        "connected": False,
                        "login_method": "scan",
                        "last_error": "等待扫码",
                    },
                ],
            )

            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertTrue(items["qidian"]["connected"])
            self.assertTrue(items["qidian"]["extension_online"])
            self.assertEqual(items["fanqie"]["last_error"], "等待扫码")

            with manager.session_factory() as session:
                client = session.get(PublisherExtensionClient, "client-1")
                client.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=5)
                session.commit()

            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertFalse(items["qidian"]["extension_online"])
            self.assertTrue(items["qidian"]["connected"])

            manager.record_browser_session(
                client_id="client-1",
                platform="qidian",
                cookies=[
                    {
                        "name": "AppAuthToken",
                        "value": "token",
                        "domain": ".write.qq.com",
                        "path": "/",
                    },
                    {
                        "name": "pubtoken",
                        "value": "pub",
                        "domain": ".write.qq.com",
                        "path": "/",
                    },
                ],
            )
            items = {item["platform_id"]: item for item in manager.list_platforms()}
            self.assertTrue(items["qidian"]["connected"])
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_upload_jobs_and_comment_upsert(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
            job = manager.create_upload_job(
                platform="qidian",
                book_name="测试书",
                chapter_title="第一章",
                body="正文",
                upload_url=None,
                publish=True,
            )
            self.assertEqual(job["status"], "pending")

            claimed = manager.claim_next_upload_job(
                client_id="client-1",
                connected_platforms=["qidian"],
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["status"], "running")
            self.assertEqual(claimed["extension_client_id"], "client-1")

            running = manager.update_upload_job_result(
                job_id=job["job_id"],
                client_id="client-1",
                status="running",
                message="扩展已接管任务",
                current_url="https://write.qq.com/portal/dashboard",
                error="",
                result_payload={"phase": "claimed"},
            )
            self.assertEqual(running["status"], "running")

            completed = manager.update_upload_job_result(
                job_id=job["job_id"],
                client_id="client-1",
                status="succeeded",
                message="发布成功",
                current_url="https://write.qq.com/portal/dashboard",
                error="",
                result_payload={"mode": "publish"},
            )
            self.assertEqual(completed["status"], "succeeded")

            sync_job = manager.create_comment_sync_job(
                platform="fanqie",
                work_id="book-1",
                work_name="测试书",
                chapter_id="chapter-1",
                chapter_title="第一章",
                limit=50,
            )
            self.assertEqual(sync_job["status"], "pending")

            first_batch = manager.ingest_comments_batch(
                client_id="client-1",
                platform="fanqie",
                job_id=sync_job["job_id"],
                comments=[
                    {
                        "remote_comment_id": "comment-1",
                        "work_id": "book-1",
                        "work_name": "测试书",
                        "chapter_id": "chapter-1",
                        "chapter_title": "第一章",
                        "author_id": "user-1",
                        "author_name": "读者A",
                        "body": "催更",
                        "created_at": "2026-03-31T12:00:00Z",
                        "raw_payload": {"body": "催更"},
                    }
                ],
            )
            second_batch = manager.ingest_comments_batch(
                client_id="client-1",
                platform="fanqie",
                comments=[
                    {
                        "remote_comment_id": "comment-1",
                        "work_id": "book-1",
                        "work_name": "测试书",
                        "chapter_id": "chapter-1",
                        "chapter_title": "第一章",
                        "author_id": "user-1",
                        "author_name": "读者A",
                        "body": "二刷催更",
                        "created_at": "2026-03-31T12:01:00Z",
                        "raw_payload": {"body": "二刷催更"},
                    }
                ],
            )

            self.assertEqual(first_batch["inserted"], 1)
            self.assertEqual(second_batch["updated"], 1)

            with manager.session_factory() as session:
                comment_count = session.execute(select(func.count(PublisherRawComment.id))).scalar_one()
            self.assertEqual(comment_count, 1)

            session_sync = manager.record_browser_session(
                client_id="client-1",
                platform="qidian",
                cookies=[
                    {
                        "name": "AppAuthToken",
                        "value": "token-value",
                        "domain": ".write.qq.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "no_restriction",
                        "expirationDate": 1893456000,
                    },
                    {
                        "name": "pubtoken",
                        "value": "cookie-value",
                        "domain": ".write.qq.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "no_restriction",
                        "expirationDate": 1893456000,
                    }
                ],
            )
            self.assertTrue(session_sync["ok"])
            self.assertTrue(manager.has_browser_session("qidian"))
            stored = manager.get_browser_session("qidian")
            assert stored is not None
            self.assertEqual(stored["cookie_count"], 2)
            self.assertEqual(stored["cookies"][0]["sameSite"], "None")

            server_job = manager.create_upload_job(
                platform="qidian",
                book_name="测试书",
                chapter_title="后端接管章节",
                body="正文",
                upload_url=None,
                publish=False,
            )
            claimed = manager.claim_upload_job_for_server(
                job_id=server_job["job_id"],
                client_id=stored["client_id"],
            )
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed["status"], "running")
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publisher_manager_requeues_running_jobs_after_restart(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        try:
            job = manager.create_upload_job(
                platform="fanqie",
                book_name="测试书",
                chapter_title="重试章节",
                body="正文",
                upload_url=None,
                publish=False,
            )
            manager.update_upload_job_result(
                job_id=job["job_id"],
                client_id="client-1",
                status="running",
                message="后端接管中",
                current_url="https://fanqienovel.com/main/writer/",
                error="",
                result_payload={"phase": "starting"},
            )

            recovered_platforms = manager.requeue_interrupted_upload_jobs()
            self.assertEqual(recovered_platforms, ["fanqie"])

            with manager.session_factory() as session:
                stored_job = session.get(PublisherUploadJob, job["job_id"])
                assert stored_job is not None
                self.assertEqual(stored_job.status, "pending")
                self.assertIsNone(stored_job.started_at)
                self.assertEqual(stored_job.extension_client_id, "")
                self.assertEqual(stored_job.current_url, "")
                self.assertEqual(stored_job.result_message, "服务重启后，上传任务已重新排队。")
        finally:
            engine.dispose()
            tmp.cleanup()

    def test_publishers_page_and_extension_api_routes(self) -> None:
        tmp, engine, manager = self._make_publisher_manager()
        old_manager = api_module._publisher_manager
        try:
            with TestClient(api_module.app) as client:
                api_module._publisher_manager = manager

                page = client.get("/publishers")
                self.assertEqual(page.status_code, 200)
                self.assertIn("浏览器扩展", page.text)
                self.assertIn("如果你是用 macOS 浏览器访问这台 Linux 后端", page.text)
                self.assertIn("browser_extension/forwin-publisher", page.text)
                self.assertIn("execute-upload-job", page.text)
                self.assertIn("/api/publishers/extension-package", page.text)

                package = client.get("/api/publishers/extension-package")
                self.assertEqual(package.status_code, 200)
                self.assertEqual(package.headers["content-type"], "application/zip")
                self.assertIn("forwin-publisher-extension.zip", package.headers["content-disposition"])
                self.assertGreater(len(package.content), 0)
                self.assertIn(b"manifest.json", package.content)

                platforms = client.get("/api/publishers/platforms")
                self.assertEqual(platforms.status_code, 200)
                self.assertEqual(len(platforms.json()), 2)
                self.assertEqual(platforms.json()[0]["supported_login_methods"], ["scan"])

                with patch.object(api_module, "_start_backend_upload_thread", lambda _job_id: None):
                    created = client.post(
                        "/api/publishers/upload-jobs",
                        json={
                            "platform": "fanqie",
                            "book_name": "测试书",
                            "chapter_title": "第一章",
                            "body": "正文",
                            "publish": False,
                        },
                    )
                self.assertEqual(created.status_code, 200)
                job_id = created.json()["job_id"]
                self.assertEqual(created.json()["status"], "pending")

                fetched = client.get(f"/api/publishers/upload-jobs/{job_id}")
                self.assertEqual(fetched.status_code, 200)
                self.assertEqual(fetched.json()["job_id"], job_id)

                unauthorized = client.post(
                    "/api/publishers/extension/heartbeat",
                    json={
                        "client_id": "client-1",
                        "platforms": [],
                    },
                )
                self.assertEqual(unauthorized.status_code, 401)

                heartbeat = client.post(
                    "/api/publishers/extension/heartbeat",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "extension_version": "0.1.0",
                        "browser_name": "Chrome",
                        "browser_version": "123.0",
                        "backend_base_url": "http://192.168.31.10:8899",
                        "platforms": [
                            {
                                "platform": "fanqie",
                                "connected": True,
                                "login_method": "scan",
                                "cookies": [
                                    {
                                        "name": "sessionid",
                                        "value": "cookie-value",
                                        "domain": ".fanqienovel.com",
                                        "path": "/",
                                        "secure": True,
                                        "httpOnly": True,
                                        "sameSite": "lax",
                                    }
                                ],
                                "raw_state": {"source": "test"},
                            }
                        ],
                    },
                )
                self.assertEqual(heartbeat.status_code, 200)
                self.assertTrue(heartbeat.json()["ok"])

                with manager.session_factory() as session:
                    stored_session = session.get(PublisherBrowserSession, "fanqie")
                    self.assertIsNone(stored_session)

                session_sync = client.post(
                    "/api/publishers/extension/session-sync",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "platform": "qidian",
                        "cookies": [
                            {
                                "name": "AppAuthToken",
                                "value": "token-value",
                                "domain": ".write.qq.com",
                                "path": "/",
                                "secure": True,
                                "httpOnly": True,
                                "sameSite": "no_restriction",
                                "expirationDate": 1893456000,
                            },
                            {
                                "name": "pubtoken",
                                "value": "cookie-value",
                                "domain": ".write.qq.com",
                                "path": "/",
                                "secure": True,
                                "httpOnly": True,
                                "sameSite": "no_restriction",
                                "expirationDate": 1893456000,
                            }
                        ],
                    },
                )
                self.assertEqual(session_sync.status_code, 200)
                self.assertEqual(session_sync.json()["cookie_count"], 2)

                with patch.object(api_module, "_start_backend_upload_thread", lambda _job_id: None):
                    server_claim = client.post(
                        "/api/publishers/upload-jobs",
                        json={
                            "platform": "qidian",
                            "book_name": "测试书",
                            "chapter_title": "后端接管章节",
                            "body": "正文",
                            "publish": False,
                        },
                    )
                self.assertEqual(server_claim.status_code, 200)
                self.assertEqual(server_claim.json()["status"], "running")
                self.assertEqual(server_claim.json()["message"], "后端正在使用已同步会话执行上传。")

                updated = client.post(
                    f"/api/publishers/upload-jobs/{job_id}/result",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "status": "succeeded",
                        "message": "保存完成",
                        "current_url": "https://fanqienovel.com/main/writer/",
                        "result_payload": {"mode": "draft"},
                    },
                )
                self.assertEqual(updated.status_code, 200)
                self.assertEqual(updated.json()["status"], "succeeded")

                claimed_none = client.post(
                    "/api/publishers/extension/upload-jobs/claim",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "connected_platforms": ["fanqie"],
                    },
                )
                self.assertEqual(claimed_none.status_code, 200)
                self.assertFalse(claimed_none.json()["found"])

                comment_job = client.post(
                    "/api/publishers/comment-sync-jobs",
                    json={
                        "platform": "fanqie",
                        "work_id": "book-1",
                        "work_name": "测试书",
                    },
                )
                self.assertEqual(comment_job.status_code, 200)

                comments = client.post(
                    "/api/publishers/extension/comments/batch",
                    headers={"X-Forwin-Extension-Key": "secret"},
                    json={
                        "client_id": "client-1",
                        "platform": "fanqie",
                        "job_id": comment_job.json()["job_id"],
                        "comments": [
                            {
                                "remote_comment_id": "comment-1",
                                "work_id": "book-1",
                                "work_name": "测试书",
                                "body": "催更",
                                "raw_payload": {"body": "催更"},
                            }
                        ],
                    },
                )
                self.assertEqual(comments.status_code, 200)
                self.assertEqual(comments.json()["inserted"], 1)

                self.assertEqual(client.get("/publishers/qidian/auth").status_code, 404)
                self.assertEqual(client.post("/api/publishers/fanqie/login").status_code, 404)
                self.assertEqual(client.post("/api/publishers/upload", json={}).status_code, 404)
        finally:
            api_module._publisher_manager = old_manager
            engine.dispose()
            tmp.cleanup()

    def test_home_page_renders_minimax_defaults_and_apikey_field(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            try:
                with TestClient(api_module.app) as client:
                    api_module._config = Config(
                        db_path=":memory:",
                        runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                        minimax_api_key="",
                        minimax_base_url="https://api.minimaxi.com/v1",
                        minimax_model="MiniMax-M2.7",
                    )
                    api_module._runtime_settings = RuntimeSettingsStore(
                        api_module._config.runtime_settings_path,
                        default_api_key="",
                        default_base_url=api_module._config.minimax_base_url,
                        default_model=api_module._config.minimax_model,
                    )

                    page = client.get("/")

                    self.assertEqual(page.status_code, 200)
                    self.assertIn('id="api_key"', page.text)
                    self.assertIn("https://api.minimaxi.com/v1", page.text)
                    self.assertIn("MiniMax-M2.7", page.text)
                    self.assertIn("/publishers", page.text)
                    self.assertIn("保存默认配置", page.text)
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store

    def test_publishers_page_uses_extension_bridge_flow(self) -> None:
        with TestClient(api_module.app) as client:
            page = client.get("/publishers")

        self.assertEqual(page.status_code, 200)
        self.assertIn("forwin-publisher-extension", page.text)
        self.assertIn("open-login", page.text)
        self.assertIn("execute-upload-job", page.text)
        self.assertIn("浏览器扩展未响应", page.text)

    def test_generate_accepts_request_level_minimax_overrides(self) -> None:
        class FakeThread:
            created: list["FakeThread"] = []

            def __init__(self, target=None, args=None, daemon=None):
                self.target = target
                self.args = args or ()
                self.daemon = daemon
                self.started = False
                FakeThread.created.append(self)

            def start(self):
                self.started = True

        with TestClient(api_module.app) as client:
            old_config = api_module._config
            old_tasks = api_module._tasks
            try:
                api_module._config = Config(
                    db_path=":memory:",
                    minimax_api_key="",
                    minimax_base_url="https://api.minimaxi.com/v1",
                    minimax_model="MiniMax-M2.7",
                )
                api_module._tasks = {}

                with patch.object(api_module.threading, "Thread", FakeThread):
                    response = client.post(
                        "/api/generate",
                        json={
                            "premise": "一段前提",
                            "genre": "玄幻",
                            "num_chapters": 2,
                            "api_key": "sk-inline",
                            "base_url": "https://example.test/v1",
                            "model": "custom-model",
                        },
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(FakeThread.created), 1)
                thread = FakeThread.created[0]
                self.assertTrue(thread.started)
                self.assertIs(thread.target, api_module._run_generation_with_config)
                runtime_config = thread.args[4]
                self.assertEqual(runtime_config.minimax_api_key, "sk-inline")
                self.assertEqual(runtime_config.minimax_base_url, "https://example.test/v1")
                self.assertEqual(runtime_config.minimax_model, "custom-model")
            finally:
                api_module._config = old_config
                api_module._tasks = old_tasks

    def test_llm_settings_api_persists_runtime_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            try:
                with TestClient(api_module.app) as client:
                    api_module._config = Config(
                        db_path=":memory:",
                        runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                        minimax_api_key="",
                        minimax_base_url="https://api.minimaxi.com/v1",
                        minimax_model="MiniMax-M2.7",
                    )
                    api_module._runtime_settings = RuntimeSettingsStore(
                        api_module._config.runtime_settings_path,
                        default_api_key="",
                        default_base_url=api_module._config.minimax_base_url,
                        default_model=api_module._config.minimax_model,
                    )
                    saved = client.post(
                        "/api/settings/llm",
                        json={
                            "api_key": "sk-saved",
                            "base_url": "https://example.saved/v1",
                            "model": "saved-model",
                        },
                    )
                    current = client.get("/api/settings/llm")

                self.assertEqual(saved.status_code, 200)
                self.assertEqual(current.status_code, 200)
                self.assertEqual(current.json()["has_api_key"], True)
                self.assertEqual(current.json()["base_url"], "https://example.saved/v1")
                self.assertEqual(current.json()["model"], "saved-model")
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store

    def test_generate_uses_saved_runtime_settings_when_request_omits_key(self) -> None:
        class FakeThread:
            created: list["FakeThread"] = []

            def __init__(self, target=None, args=None, daemon=None):
                self.target = target
                self.args = args or ()
                self.daemon = daemon
                self.started = False
                FakeThread.created.append(self)

            def start(self):
                self.started = True

        with TemporaryDirectory() as tmp:
            old_config = api_module._config
            old_store = api_module._runtime_settings
            old_tasks = api_module._tasks
            try:
                with TestClient(api_module.app) as client:
                    api_module._config = Config(
                        db_path=":memory:",
                        runtime_settings_path=str(Path(tmp) / "runtime_settings.json"),
                        minimax_api_key="",
                        minimax_base_url="https://api.minimaxi.com/v1",
                        minimax_model="MiniMax-M2.7",
                    )
                    api_module._runtime_settings = RuntimeSettingsStore(
                        api_module._config.runtime_settings_path,
                        default_api_key="",
                        default_base_url=api_module._config.minimax_base_url,
                        default_model=api_module._config.minimax_model,
                    )
                    api_module._runtime_settings.save(
                        api_key="sk-from-store",
                        base_url="https://stored.example/v1",
                        model="stored-model",
                    )
                    api_module._tasks = {}
                    with patch.object(api_module.threading, "Thread", FakeThread):
                        response = client.post(
                            "/api/generate",
                            json={
                                "premise": "一段前提",
                                "genre": "玄幻",
                                "num_chapters": 1,
                                "api_key": "",
                                "base_url": "",
                                "model": "",
                            },
                        )

                self.assertEqual(response.status_code, 200)
                runtime_config = FakeThread.created[0].args[4]
                self.assertEqual(runtime_config.minimax_api_key, "sk-from-store")
                self.assertEqual(runtime_config.minimax_base_url, "https://stored.example/v1")
                self.assertEqual(runtime_config.minimax_model, "stored-model")
            finally:
                api_module._config = old_config
                api_module._runtime_settings = old_store
                api_module._tasks = old_tasks


if __name__ == "__main__":
    unittest.main()
