from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import inspect, text

import forwin.api as api_module
from forwin.api_schemas import (
    BookGenesisPatchRequest,
    ProjectCreateRequest,
    WorldEditProposalReviewRequest,
    WorldModelExportRequest,
    WorldModelImportRequest,
)
from forwin.book_state.repository import BookStateRepository
from forwin.models.base import get_engine, get_session_factory, init_db, upgrade_db
from forwin.models.entity import EntityState
from forwin.models.event import CanonEvent
from forwin.models.phase4 import NPCIntentSnapshot, WorldSimulationTurn
from forwin.models.project import Project
from forwin.models.world_model import (
    WorldEditProposalRow,
    WorldModelCompileRunRow,
    WorldModelConflictRow,
    WorldModelPageRow,
    WorldModelSnapshotRow,
)
from forwin.protocol.context import WorldContextPack
from forwin.protocol.book_state import WorldNode
from forwin.runtime_settings import RuntimeSettingsStore
from forwin.state.updater import StateUpdater
from forwin.world_model.compiler import WorldModelCompiler
from forwin.world_model.exporter_obsidian import ObsidianWorldExporter
from forwin.world_model.importer_obsidian import ObsidianWorldImporter
from forwin.world_model.retriever import WorldModelRetriever
from forwin.obsidian.importer import ObsidianImporter
from forwin.config import Config
from forwin.context.assembler import assemble_context
from forwin.retrieval.broker import RetrievalBroker
from forwin.state.repo import StateRepository
from forwin.reviewer.context_builder import build_review_context_pack


class WorldModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = TemporaryDirectory()
        self.database_url = postgres_test_url("world_model")
        self.engine = get_engine(self.database_url)
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)
        self.old_session_factory = api_module._SessionFactory
        self.old_config = api_module._config
        self.old_runtime_settings = api_module._runtime_settings
        api_module._SessionFactory = self.session_factory
        api_module._config = Config(
            database_url=self.database_url,
            minimax_api_key="test-key",
            minimax_base_url="http://example.invalid",
            minimax_model="fake-model",
        )
        api_module._runtime_settings = RuntimeSettingsStore(
            str(Path(self.tmpdir.name) / "runtime_settings.json"),
            default_api_key="default-key",
            default_base_url="http://default.invalid",
            default_model="default-model",
        )

    def tearDown(self) -> None:
        api_module._SessionFactory = self.old_session_factory
        api_module._config = self.old_config
        api_module._runtime_settings = self.old_runtime_settings
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _create_genesis_project(self) -> str:
        created = api_module.create_project(
            ProjectCreateRequest.model_validate(
                {
                    "title": "WorldModel 测试书",
                    "premise": "一座旧城靠灵矿维持结界，主角发现矿脉正在说谎。",
                    "genre": "玄幻",
                    "target_total_chapters": 8,
                }
            )
        )
        api_module.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate(
                {
                    "book_brief": {
                        "title": "WorldModel 测试书",
                        "one_line": "旧城灵矿背后藏着失效结界。",
                        "promise": "主角会不断揭开灵矿真相。",
                    },
                    "world": {
                        "world_bible": {
                            "overview": "旧城、灵矿和边境宗门互相牵制。",
                            "axioms": ["灵矿结界需要代价", "宗门不得公开干预王都税权"],
                            "history_slice": "三十年前的矿难改变了边境秩序。",
                            "culture_profiles": [
                                {"id": "old-city", "name": "旧城民俗", "summary": "重契约。"}
                            ],
                        },
                        "map_atlas": {
                            "overview": "王都、旧城、北境灵矿构成主舞台。",
                            "regions": [
                                {"id": "old-city", "name": "旧城", "subworld_name": "旧城线"},
                                {"id": "north-mine", "name": "北境灵矿", "subworld_name": "矿脉线"},
                            ],
                            "nodes": [
                                {"id": "mine-gate", "name": "矿门", "parent_region_id": "north-mine"}
                            ],
                        },
                        "story_engine": {
                            "reader_promises": ["灵矿真相必须逐层揭露"],
                            "long_arcs": ["旧城结界失效", "宗门税权冲突"],
                            "core_cast": [
                                {"name": "林烬", "role": "主角"},
                                {"name": "韩砚", "role": "盟友"},
                            ],
                            "factions": [
                                {"name": "玄霜宗", "agenda": "控制北境灵矿"},
                                {"name": "王都皇室", "agenda": "征收灵矿税"},
                            ],
                        },
                        "institution_profiles": [
                            {"name": "宗门议事堂", "summary": "以长老令约束外门。"}
                        ],
                        "resource_economy_profiles": [
                            {"name": "灵矿税", "summary": "王都与宗门争夺的财政来源。"}
                        ],
                        "world_extensions": {
                            "secrets_codex": [
                                {"name": "矿难真相", "hidden_truth": "结界曾吞掉整支矿队。"}
                            ]
                        },
                    },
                }
            ),
        )
        for stage in ("brief", "world", "map", "story_engine", "book_blueprint", "bootstrap"):
            api_module.lock_project_genesis_stage(created.project_id, stage)
        return created.project_id

    def test_upgrade_backfills_projection_cache_columns_for_existing_world_model_pages_table(self) -> None:
        projection_columns = (
            "canon_status",
            "visibility_scope",
            "role_scope",
            "observer_id",
            "observer_type",
            "section_digest_json",
            "source_digest",
            "projection_version",
            "projection_kind",
        )
        with self.engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_world_model_pages_project_source_digest"))
            conn.execute(text("DROP INDEX IF EXISTS ix_world_model_pages_project_projection"))
            for column in projection_columns:
                conn.execute(text(f"ALTER TABLE world_model_pages DROP COLUMN IF EXISTS {column}"))

        upgrade_db(self.engine)

        columns = {column["name"] for column in inspect(self.engine).get_columns("world_model_pages")}
        assert set(projection_columns) <= columns

    def test_migration_creates_world_model_tables(self) -> None:
        with self.session_factory() as session:
            project = Project(
                title="迁移测试",
                premise="验证 world model 表存在。",
                genre="玄幻",
            )
            session.add(project)
            session.flush()
            session.add(
                WorldModelSnapshotRow(
                    project_id=project.id,
                    as_of_chapter=0,
                    version=1,
                    status="live",
                    snapshot_json="{}",
                    source_digest="digest",
                )
            )
            session.add(
                WorldModelCompileRunRow(
                    project_id=project.id,
                    trigger="test",
                    as_of_chapter=0,
                    status="succeeded",
                    source_digest="digest",
                )
            )
            session.commit()

            self.assertEqual(session.query(WorldModelSnapshotRow).count(), 1)
            self.assertEqual(session.query(WorldModelCompileRunRow).count(), 1)

    def test_bootstrap_from_genesis_creates_snapshot_pages_and_obsidian_index(self) -> None:
        project_id = self._create_genesis_project()
        vault_root = Path(self.tmpdir.name) / "vault"

        with self.session_factory() as session:
            snapshot = WorldModelCompiler(session).bootstrap_from_genesis(project_id)
            export = ObsidianWorldExporter(session).export_project(project_id, vault_root=vault_root)
            session.commit()

            pages = session.query(WorldModelPageRow).filter_by(project_id=project_id).all()

        self.assertEqual(snapshot.as_of_chapter, 0)
        self.assertEqual(snapshot.status, "live")
        self.assertTrue(any(page.page_type == "character" and page.title == "林烬" for page in pages))
        self.assertTrue(any(page.page_type == "faction" and page.title == "玄霜宗" for page in pages))
        self.assertTrue((vault_root / "00_Index.md").exists())
        self.assertTrue((vault_root / "canvas" / "arc_dependencies.canvas").exists())
        character_page = next(path for path in vault_root.rglob("*.md") if path.name == "林烬.md")
        text = character_page.read_text(encoding="utf-8")
        self.assertIn("forwin_id:", text)
        self.assertIn("## Canon Summary", text)
        self.assertIn("## Manual Notes", text)
        self.assertEqual(export.exported_count, len(pages))

    def test_project_delete_cleans_world_model_rows(self) -> None:
        project_id = self._create_genesis_project()
        with self.session_factory() as session:
            WorldModelCompiler(session).bootstrap_from_genesis(project_id)
            session.add(
                WorldEditProposalRow(
                    project_id=project_id,
                    source="obsidian",
                    target_page_key="world:index",
                    target_field="markdown",
                    proposed_patch_json="{}",
                )
            )
            session.add(
                WorldModelConflictRow(
                    project_id=project_id,
                    conflict_type="test_conflict",
                    subject_key="world:index",
                    description="测试删除清理。",
                )
            )
            session.commit()

        with self.session_factory() as session:
            api_module._delete_project(session, project_id)
            session.commit()
            counts = [
                session.query(WorldModelSnapshotRow).filter_by(project_id=project_id).count(),
                session.query(WorldModelPageRow).filter_by(project_id=project_id).count(),
                session.query(WorldModelCompileRunRow).filter_by(project_id=project_id).count(),
                session.query(WorldEditProposalRow).filter_by(project_id=project_id).count(),
                session.query(WorldModelConflictRow).filter_by(project_id=project_id).count(),
            ]

        self.assertEqual(counts, [0, 0, 0, 0, 0])

    def test_compile_after_chapter_is_idempotent_and_includes_runtime_world_state(self) -> None:
        project_id = self._create_genesis_project()
        with self.session_factory() as session:
            compiler = WorldModelCompiler(session)
            compiler.bootstrap_from_genesis(project_id)
            updater = StateUpdater(session)
            hero = updater.create_entity(
                project_id=project_id,
                kind="character",
                name="林烬",
                description="被矿脉选中的少年。",
                chapter=0,
            )
            updater.create_entity_state(
                hero.id,
                1,
                {"location": "北境灵矿", "alive": True, "goal": "查明矿难"},
            )
            session.add(
                CanonEvent(
                    project_id=project_id,
                    chapter_number=1,
                    summary="林烬抵达北境灵矿，发现矿门封印松动。",
                    significance="major",
                )
            )
            session.add(
                WorldSimulationTurn(
                    project_id=project_id,
                    chapter_number=1,
                    pressure_level="rising",
                    pressure_summary="灵矿封印不稳，宗门和皇室都开始加压。",
                    notable_shifts_json=json.dumps(["矿门戒严"], ensure_ascii=False),
                )
            )
            session.add(
                NPCIntentSnapshot(
                    project_id=project_id,
                    chapter_number=1,
                    entity_id=hero.id,
                    entity_name="林烬",
                    intent_kind="investigate",
                    objective="查清矿难真相",
                    urgency=3,
                )
            )
            session.flush()

            first = compiler.compile_after_chapter(project_id, 1)
            second = compiler.compile_after_chapter(project_id, 1)
            session.commit()
            rows = (
                session.query(WorldModelSnapshotRow)
                .filter_by(project_id=project_id, as_of_chapter=1, status="live")
                .all()
            )

        snapshot_payload = first.snapshot
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(rows), 1)
        self.assertIn("林烬抵达北境灵矿", json.dumps(snapshot_payload, ensure_ascii=False))
        self.assertIn("灵矿封印不稳", json.dumps(snapshot_payload, ensure_ascii=False))
        self.assertIn("查清矿难真相", json.dumps(snapshot_payload, ensure_ascii=False))

    def test_compile_prefers_runtime_actor_over_duplicate_genesis_actor(self) -> None:
        project_id = self._create_genesis_project()
        with self.session_factory() as session:
            compiler = WorldModelCompiler(session)
            updater = StateUpdater(session)
            updater.create_entity(
                project_id=project_id,
                kind="character",
                name="林烬",
                description="运行期主角档案。",
                chapter=0,
            )

            snapshot = compiler.bootstrap_from_genesis(project_id)

        characters = snapshot.snapshot["actor_model"]["characters"]
        matches = [item for item in characters if item.get("name") == "林烬"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["description"], "运行期主角档案。")

    def test_world_model_page_list_prefers_book_state_projection_for_duplicate_title(self) -> None:
        project_id = self._create_genesis_project()
        with self.session_factory() as session:
            WorldModelCompiler(session).bootstrap_from_genesis(project_id)
            session.add(
                WorldModelPageRow(
                    project_id=project_id,
                    page_key="character:char_runtime_linye",
                    page_type="character",
                    title="林烬",
                    vault_path="03_Actors/Characters/林烬_char_runtime_linye.md",
                    markdown="# 林烬\n\nBookState projection",
                    frontmatter_json=json.dumps(
                        {
                            "forwin_id": "character:char_runtime_linye",
                            "project_id": project_id,
                            "node_id": "char_runtime_linye",
                            "node_type": "character",
                            "status": "canon_live",
                            "as_of_chapter": 1,
                            "editable_fields": ["Manual Notes"],
                        },
                        ensure_ascii=False,
                    ),
                    content_hash="book-state-linye",
                    revision=1,
                    status="canon_live",
                    as_of_chapter=1,
                )
            )
            session.commit()

        pages = api_module.list_project_world_model_pages(project_id)
        linye_pages = [page for page in pages if page.page_type == "character" and page.title == "林烬"]

        self.assertEqual(len(linye_pages), 1)
        self.assertEqual(linye_pages[0].page_key, "character:char_runtime_linye")

    def test_import_obsidian_changes_creates_proposal_without_mutating_page(self) -> None:
        project_id = self._create_genesis_project()
        vault_root = Path(self.tmpdir.name) / "vault"
        with self.session_factory() as session:
            compiler = WorldModelCompiler(session)
            compiler.bootstrap_from_genesis(project_id)
            ObsidianWorldExporter(session).export_project(project_id, vault_root=vault_root)
            session.commit()

        page_path = next(path for path in vault_root.rglob("*.md") if path.name == "林烬.md")
        original_text = page_path.read_text(encoding="utf-8")
        page_path.write_text(original_text + "\n人工备注：怀疑他与矿难有关。\n", encoding="utf-8")

        with self.session_factory() as session:
            result = ObsidianWorldImporter(session).import_project(project_id, vault_root=vault_root)
            proposal = session.query(WorldEditProposalRow).filter_by(project_id=project_id).one()
            page = session.query(WorldModelPageRow).filter_by(project_id=project_id, title="林烬").one()
            session.commit()

        self.assertEqual(result.proposal_count, 1)
        self.assertEqual(proposal.status, "needs_resolution")
        self.assertIn("人工备注", proposal.proposed_patch_json)
        self.assertNotIn("人工备注", page.markdown)

    def test_book_state_obsidian_import_without_target_node_needs_resolution(self) -> None:
        project_id = self._create_genesis_project()
        vault_root = Path(self.tmpdir.name) / "legacy_vault"
        page_path = vault_root / "03_Actors" / "Characters" / "林烬.md"
        page_path.parent.mkdir(parents=True)
        page_path.write_text(
            "\n".join(
                [
                    "---",
                    f"project_id: {project_id}",
                    "forwin_id: legacy:character:lin",
                    "node_type: character",
                    "title: 林烬",
                    "---",
                    "## Manual Notes",
                    "需要人工确认这个旧 vault 页面对应哪个 BookState node。",
                ]
            ),
            encoding="utf-8",
        )

        with self.session_factory() as session:
            result = ObsidianImporter(session).import_project(project_id, vault_root=vault_root)
            proposal = session.query(WorldEditProposalRow).filter_by(project_id=project_id).one()
            session.commit()

        self.assertEqual(result.proposal_count, 1)
        self.assertEqual(proposal.status, "needs_resolution")
        self.assertEqual(proposal.target_node_id, "")
        self.assertIn("target_resolution_required", proposal.proposed_patch_json)

    def test_world_model_retriever_returns_context_pack_with_pages_and_conflicts(self) -> None:
        project_id = self._create_genesis_project()
        with self.session_factory() as session:
            compiler = WorldModelCompiler(session)
            compiler.bootstrap_from_genesis(project_id)
            context = WorldModelRetriever(session).build_context(
                project_id=project_id,
                chapter_number=1,
                query_terms=["林烬", "玄霜宗", "灵矿"],
                max_pages=4,
            )

        self.assertIsInstance(context, WorldContextPack)
        self.assertEqual(context.as_of_chapter, 0)
        self.assertTrue(context.world_model_refs)
        self.assertTrue(any(page.title == "林烬" for page in context.relevant_world_pages))
        self.assertTrue(context.world_model_digest)

    def test_assembled_chapter_context_includes_world_context_pack(self) -> None:
        project_id = self._create_genesis_project()
        with self.session_factory() as session:
            compiler = WorldModelCompiler(session)
            compiler.bootstrap_from_genesis(project_id)
            updater = StateUpdater(session)
            arc = updater.create_arc_plan(
                project_id=project_id,
                arc_synopsis="灵矿真相线",
                chapter_start=1,
                chapter_end=3,
            )
            plan = updater.create_chapter_plan(
                project_id=project_id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="矿门异响",
                one_line="林烬第一次接近北境灵矿。",
                goals=["让玄霜宗和灵矿规则进入当前章上下文"],
            )
            session.commit()

        with self.session_factory() as session:
            repo = StateRepository(session)
            plan = repo.get_chapter_plan(project_id, 1)
            context = assemble_context(repo, project_id, plan)

        self.assertEqual(context.world_context.as_of_chapter, 0)
        self.assertTrue(context.world_context.world_model_refs)
        self.assertTrue(any(page.title == "林烬" for page in context.world_context.relevant_world_pages))

        review_context = build_review_context_pack(repo=repo, context=context)
        self.assertEqual(review_context.world_context.snapshot_id, context.world_context.snapshot_id)

    def test_world_model_api_exports_imports_and_reviews_proposals(self) -> None:
        project_id = self._create_genesis_project()
        vault_root = Path(self.tmpdir.name) / "api-vault"

        export = api_module.export_project_world_model(
            project_id,
            WorldModelExportRequest.model_validate({"vault_root": str(vault_root)}),
        )
        self.assertTrue(export.ok)
        self.assertTrue((vault_root / "00_Index.md").exists())

        pages = api_module.list_project_world_model_pages(project_id)
        self.assertTrue(any(page.title == "林烬" for page in pages))
        latest = api_module.get_latest_project_world_model_snapshot(project_id, as_of_chapter=0)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.as_of_chapter, 0)

        page_path = next(path for path in vault_root.rglob("*.md") if path.name == "林烬.md")
        page_path.write_text(page_path.read_text(encoding="utf-8") + "\nAPI 修改。\n", encoding="utf-8")
        imported = api_module.import_project_world_model(
            project_id,
            WorldModelImportRequest.model_validate({"vault_root": str(vault_root)}),
        )
        self.assertEqual(imported.proposal_count, 1)
        proposals = api_module.list_project_world_model_proposals(project_id)
        self.assertEqual(len(proposals), 1)
        reviewed = api_module.review_project_world_model_proposal(
            project_id,
            proposals[0].id,
            WorldEditProposalReviewRequest.model_validate({"status": "accepted", "reason": "测试接受"}),
        )
        self.assertEqual(reviewed.status, "accepted")

    def test_world_model_store_upsert_supersedes_lower_rank_duplicate_pages(self) -> None:
        with self.session_factory() as session:
            project = Project(title="Canonical 页面测试", premise="同名人物只能有一个 live 页面。", genre="玄幻")
            session.add(project)
            session.flush()
            legacy = WorldModelPageRow(
                project_id=project.id,
                page_key="character:legacy_entity_1",
                page_type="character",
                title="林烬",
                vault_path="03_Actors/Characters/林烬.md",
                markdown="# 林烬\n\n## Canon Summary\nlegacy",
                frontmatter_json=json.dumps({"forwin_id": "character:legacy_entity_1"}, ensure_ascii=False),
                content_hash="legacy",
                status="canon_live",
                as_of_chapter=3,
            )
            session.add(legacy)
            session.flush()

            from forwin.world_model.store import WorldModelStore

            row = WorldModelStore(session).upsert_page(
                project_id=project.id,
                page_key="character:char_book_state",
                page_type="character",
                title="林烬",
                vault_path="04_Characters/林烬.md",
                markdown="# 林烬\n\n## Canon Summary\nbook state",
                frontmatter={
                    "forwin_id": "character:char_book_state",
                    "node_id": "char_book_state",
                    "node_type": "character",
                    "editable_fields": ["Manual Notes"],
                },
                as_of_chapter=4,
            )
            session.commit()

            rows = session.query(WorldModelPageRow).filter_by(project_id=project.id, title="林烬").all()

        live_rows = [item for item in rows if item.status == "canon_live"]
        superseded_rows = [item for item in rows if item.status == "superseded"]
        self.assertEqual([item.page_key for item in live_rows], [row.page_key])
        self.assertEqual([item.page_key for item in superseded_rows], ["character:legacy_entity_1"])

    def test_world_model_canonical_pages_are_shared_across_api_export_retriever_and_broker(self) -> None:
        vault_root = Path(self.tmpdir.name) / "canonical-vault"
        with self.session_factory() as session:
            project = Project(title="Canonical Consumer 测试", premise="所有 consumer 必须共享 canonical page。", genre="玄幻")
            session.add(project)
            session.flush()
            session.add(
                WorldModelSnapshotRow(
                    project_id=project.id,
                    as_of_chapter=5,
                    version=1,
                    status="live",
                    snapshot_json=json.dumps({"source_refs": []}, ensure_ascii=False),
                    source_digest="digest",
                )
            )
            pages = [
                WorldModelPageRow(
                    project_id=project.id,
                    page_key="world:index",
                    page_type="overview",
                    title="00_Index",
                    vault_path="00_Index.md",
                    markdown="# 00_Index\n\n## Canon Summary\nindex",
                    frontmatter_json=json.dumps({"forwin_id": "world:index"}, ensure_ascii=False),
                    content_hash="index",
                    status="canon_live",
                    as_of_chapter=5,
                ),
                WorldModelPageRow(
                    project_id=project.id,
                    page_key="character:genesis:character:林烬",
                    page_type="character",
                    title="林烬",
                    vault_path="03_Actors/Characters/林烬.md",
                    markdown="# 林烬\n\n## Canon Summary\ngenesis",
                    frontmatter_json=json.dumps({"forwin_id": "character:genesis:character:林烬"}, ensure_ascii=False),
                    content_hash="genesis",
                    status="canon_live",
                    as_of_chapter=0,
                ),
                WorldModelPageRow(
                    project_id=project.id,
                    page_key="character:legacy_entity_1",
                    page_type="character",
                    title="林烬",
                    vault_path="03_Actors/Characters/林烬-legacy.md",
                    markdown="# 林烬\n\n## Canon Summary\nlegacy",
                    frontmatter_json=json.dumps({"forwin_id": "character:legacy_entity_1"}, ensure_ascii=False),
                    content_hash="legacy",
                    status="canon_live",
                    as_of_chapter=5,
                ),
                WorldModelPageRow(
                    project_id=project.id,
                    page_key="character:char_book_state",
                    page_type="character",
                    title="林烬",
                    vault_path="04_Characters/林烬.md",
                    markdown="# 林烬\n\n## Canon Summary\nbook state",
                    frontmatter_json=json.dumps(
                        {
                            "forwin_id": "character:char_book_state",
                            "node_id": "char_book_state",
                            "node_type": "character",
                            "editable_fields": ["Manual Notes"],
                        },
                        ensure_ascii=False,
                    ),
                    content_hash="book",
                    status="canon_live",
                    as_of_chapter=5,
                ),
            ]
            session.add_all(pages)
            session.commit()
            project_id = project.id

        api_pages = api_module.list_project_world_model_pages(project_id)
        api_character_pages = [page for page in api_pages if page.page_type == "character" and page.title == "林烬"]
        self.assertEqual([page.page_key for page in api_character_pages], ["character:char_book_state"])

        with self.session_factory() as session:
            export = ObsidianWorldExporter(session).export_project(project_id, vault_root=vault_root)
            context = WorldModelRetriever(session).build_context(
                project_id=project_id,
                chapter_number=6,
                query_terms=["林烬"],
                max_pages=6,
            )
            broker_pages = RetrievalBroker()._load_obsidian_page_context(
                session,
                project_id,
                include_hidden_truth=True,
            )

        self.assertEqual(export.exported_count, 2)
        self.assertEqual(
            [page.page_key for page in context.relevant_world_pages if page.page_type == "character" and page.title == "林烬"],
            ["character:char_book_state"],
        )
        self.assertEqual(
            [page["page_key"] for page in broker_pages if page["page_type"] == "character" and page["title"] == "林烬"],
            ["character:char_book_state"],
        )

    def test_compile_for_book_state_project_ignores_legacy_and_genesis_character_sources(self) -> None:
        project_id = self._create_genesis_project()
        with self.session_factory() as session:
            repo = BookStateRepository(session)
            repo.create_world_node(
                WorldNode(
                    id="char_book_state",
                    project_id=project_id,
                    node_type="character",
                    name="陆沉",
                    summary="BookState canon 主角。",
                    description="只应从 BookState 进入 WorldModel 投影。",
                    source_refs=["book_state:delta:1"],
                )
            )
            repo.append_world_node_state(
                project_id=project_id,
                node_id="char_book_state",
                node_type="character",
                as_of_chapter=1,
                state={"goal": "查明灵矿真相"},
            )
            StateUpdater(session).create_entity(
                project_id=project_id,
                kind="character",
                name="旧影",
                description="legacy Entity mirror，不应进入 BookState 项目投影。",
                chapter=0,
            )
            snapshot = WorldModelCompiler(session).compile_after_chapter(project_id, 1)
            session.commit()
            pages = session.query(WorldModelPageRow).filter_by(project_id=project_id, page_type="character", status="canon_live").all()

        character_names = sorted(page.title for page in pages)
        snapshot_names = sorted(item["name"] for item in snapshot.snapshot["actor_model"]["characters"])
        self.assertEqual(character_names, ["陆沉"])
        self.assertEqual(snapshot_names, ["陆沉"])
        self.assertNotIn("旧影", json.dumps(snapshot.snapshot, ensure_ascii=False))
        self.assertNotIn("林烬", json.dumps(snapshot.snapshot, ensure_ascii=False))
        self.assertNotIn("韩砚", json.dumps(snapshot.snapshot, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
