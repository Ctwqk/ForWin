from __future__ import annotations

import json
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace


from forwin.application.read_models import build_project_detail
from forwin.book_state import BookStateRepository
from forwin.canon_names import CanonNameAnchor, find_canon_name_violations
from forwin.checker.rules import ContinuityChecker
from forwin.context.assembler_core import assemble_context
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision
from forwin.map.models import MapRegionRow
from forwin.models.subworld import project_scoped_subworld_id
from forwin.generation.pipeline import ChapterPipeline
from forwin.protocol import (
    ChapterEntryTarget,
    ChapterExperiencePlan,
    ReviewVerdict,
    SubWorldSummary,
    WriterOutput,
)
from forwin.protocol.book_state import MapNode, WorldNode
from forwin.protocol.review import ContinuityIssue
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.world_view.types import ChapterWorldView, OnstageEntity
from tests.chapter_delta_helpers import chapter_delta
from tests.postgres import postgres_test_url
from forwin.writer.prompt_core import (
    _canon_name_anchor_section,
    build_single_chapter_draft_prompt,
)


class _BookStateQueryStub:
    def __init__(self, recent_beats: list[str]) -> None:
        self.recent_beats = recent_beats
        self.thread_reads: list[tuple[str, int]] = []

    def active_threads(
        self,
        project_id: str,
        *,
        as_of_chapter: int,
    ) -> list[object]:
        self.thread_reads.append((project_id, as_of_chapter))
        return [SimpleNamespace(description="", recent_beats=self.recent_beats)]

    def canon_name_anchors(self, project_id: str, *, as_of_chapter: int):
        return []

    def active_entities(
        self,
        _project_id: str,
        *,
        as_of_chapter: int,
    ) -> list[object]:
        return []


class _AnchoredContinuityChecker(ContinuityChecker):
    """ContinuityChecker with explicit canon name anchors, as BookState will supply them."""

    def __init__(self, anchors: list[CanonNameAnchor]) -> None:
        super().__init__(SimpleNamespace())
        self._anchors = anchors
        self.anchor_reads: list[tuple[str, int]] = []

    def _canon_name_anchors(
        self,
        project_id: str,
        *,
        as_of_chapter: int = 10**9,
    ) -> list[CanonNameAnchor]:
        self.anchor_reads.append((project_id, as_of_chapter))
        return list(self._anchors)


class SubWorldControlTests(unittest.TestCase):

    def test_project_detail_reads_book_state_characters(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("project-detail-book-state-characters"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                BookStateRepository(session).create_world_node(
                    WorldNode(
                        id="char_book",
                        project_id=project.id,
                        node_type="character",
                        name="陆沉",
                        description="BookState canon 角色",
                    ),
                    version_chapter=0,
                )

                detail = build_project_detail(
                    session=session,
                    project=project,
                    display_datetime=lambda _value: "",
                )
            finally:
                session.close()
                engine.dispose()

        self.assertEqual([item.id for item in detail.characters], ["char_book"])
        self.assertEqual(detail.characters[0].name, "陆沉")

    def test_assemble_context_uses_book_state_entities_and_active_subworlds(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("context"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                arc = updater.create_arc_plan(project.id, "弧线")
                updater.create_chapter_plan(
                    project_id=project.id,
                    arc_plan_id=arc.id,
                    chapter_number=1,
                    title="第一章",
                    one_line="开场",
                    goals=["推进"],
                )
                book_state = BookStateRepository(session)
                book_state.create_world_node(
                    WorldNode(
                        id="char_allowed",
                        project_id=project.id,
                        node_type="character",
                        name="阿青",
                        description="允许角色",
                        profile={
                            "personality_loadout": {
                                "dominant": {
                                    "skill": "trait-loyal-protector",
                                    "weight": 0.72,
                                },
                                "secondary": [],
                                "social_mask": [],
                                "stress_modes": [],
                                "relationship_patterns": [],
                                "overrides": {},
                            }
                        },
                    ),
                    version_chapter=0,
                )
                book_state.create_world_node(
                    WorldNode(
                        id="char_other_subworld",
                        project_id=project.id,
                        node_type="character",
                        name="小明",
                        description="其他子世界角色",
                        profile={
                            "personality_loadout": {
                                "dominant": {
                                    "skill": "trait-loyal-protector",
                                    "weight": 0.72,
                                },
                                "secondary": [],
                                "social_mask": [],
                                "stress_modes": [],
                                "relationship_patterns": [],
                                "overrides": {},
                            }
                        },
                    ),
                    version_chapter=0,
                )
                book_state.create_world_node(
                    WorldNode(
                        id="location_old_house",
                        project_id=project.id,
                        node_type="location",
                        name="旧宅",
                        description="地点",
                    ),
                    version_chapter=0,
                )
                global_core = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=arc.id,
                    parent_subworld_id=None,
                    name="global_core",
                    purpose="核心角色",
                    scope="global_core",
                    metadata={},
                )
                arc_local = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=arc.id,
                    parent_subworld_id=None,
                    name="江城线",
                    purpose="局部舞台",
                    scope="arc_local",
                    metadata={"chapter_window_hint": "1-3"},
                )
                book_state.create_map_node(MapNode(
                    id="map-old-house", project_id=project.id, subworld_id=global_core.id,
                    node_type="site", name="旧宅",
                ))
                plan = StateRepository(session).get_chapter_plan(project.id, 1)
                plan.location_refs_json = '["map-old-house"]'
                updater.update_chapter_experience_plan(
                    project.id,
                    1,
                    ChapterExperiencePlan(
                        active_subworld_ids=[arc_local.id],
                        entity_admission_rule="strict_named_character",
                    ),
                )
                revision = BookGenesisRevision(
                    project_id=project.id,
                    revision=1,
                    status="active",
                    pack_json=json.dumps(
                        {
                            "map_atlas": {
                                "overview": "主世界地图总览",
                                "submaps": [{"name": "主舞台总图"}],
                                "regions": [
                                    {
                                        "id": "region-main",
                                        "name": "主城核心区",
                                        "subworld_name": "主舞台总图",
                                        "parent_region_id": "",
                                        "level": 1,
                                    }
                                ],
                                "nodes": [],
                            }
                        },
                        ensure_ascii=False,
                    ),
                )
                session.add(revision)
                session.flush()
                project.active_genesis_revision_id = revision.id
                global_core.metadata_json = json.dumps(
                    {
                        "region_drafts": [
                            {
                                "name": "江城前哨区",
                                "level": 1,
                                "kind": "frontier_domain",
                            }
                        ],
                        "region_source": "runtime_generated",
                        "region_promotion_state": "draft",
                    },
                    ensure_ascii=False,
                )
                session.add(global_core)
                session.flush()
                repo = StateRepository(session)
                plan = repo.get_chapter_plan(project.id, 1)
                context = assemble_context(repo, project.id, plan)
            finally:
                session.close()
                engine.dispose()

        self.assertEqual([item.id for item in context.active_subworlds], [global_core.id])
        active_names = [item.name for item in context.active_entities]
        self.assertIn("阿青", active_names)
        self.assertIn("小明", active_names)
        self.assertIn("旧宅", active_names)
        self.assertEqual(set(context.allowed_entities), {"阿青", "小明"})
        self.assertEqual(context.entity_admission_rule, "strict_named_character")
        self.assertIn("Genesis 地区：主城核心区@主舞台总图·L1", context.genesis_map_overview)
        self.assertIn("运行时地区草案：江城前哨区@global_core·L1", context.genesis_map_overview)

    def test_active_subworld_region_drafts_read_current_region_source(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("region-source"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                global_core = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=None,
                    parent_subworld_id=None,
                    name="global_core",
                    purpose="核心舞台",
                    scope="global_core",
                    metadata={},
                )
                session.add(
                    MapRegionRow(
                        id="region-current-source",
                        project_id=project.id,
                        subworld_id=global_core.id,
                        region_type="local_region",
                        name="新舞台核心区",
                        description="地区",
                        metadata_json=json.dumps(
                            {"level": 1, "region_source": "runtime_generated"},
                            ensure_ascii=False,
                        ),
                    )
                )
                session.commit()

                arc = updater.create_arc_plan(project.id, "arc")
                plan = updater.create_chapter_plan(project.id, arc.id, 1, "title", "summary", [])
                BookStateRepository(session).create_map_node(MapNode(
                    id="map-region", project_id=project.id, subworld_id=global_core.id,
                    node_type="site", name="site",
                ))
                plan.location_refs_json = '["map-region"]'
                session.flush()
                drafts = StateRepository(session).get_active_subworld_region_drafts(project.id, 1)
            finally:
                session.close()
                engine.dispose()

        self.assertEqual(drafts[0]["name"], "新舞台核心区")
        self.assertEqual(drafts[0]["region_source"], "runtime_generated")

    def test_writer_prompt_includes_subworld_rules_and_entry_targets(self) -> None:
        context = SimpleNamespace(
            project_title="书",
            genre="玄幻",
            premise="前提",
            setting_summary="设定",
            chapter_number=1,
            chapter_plan_title="第一章",
            chapter_plan_one_line="开场",
            chapter_goals=["推进主线"],
            previous_chapter_summaries=[],
            active_entities=[SimpleNamespace(name="阿青", description="允许角色")],
            active_threads=[],
            active_relations=[],
            canon_name_anchors=[],
            timeline=None,
            audience_hints=None,
            reader_promise=None,
            arc_payoff_map=None,
            band_delight_schedule=None,
            chapter_experience_plan=None,
            active_subworlds=[SubWorldSummary(id="sw1", name="global_core", scope="global_core")],
            allowed_entities=["阿青", "未出场者"],
            chapter_entry_targets=[ChapterEntryTarget(chapter_hint=1, entity_name="沈知遥", subworld_id="sw2", role_hint="新盟友")],
            entity_admission_rule="strict_named_character",
            next_band_summary=None,
            retrieved_memories=[],
            world_view=ChapterWorldView(onstage=[OnstageEntity(ref="char-a", name="阿青")]),
        )

        prompt = build_single_chapter_draft_prompt(context)
        content = prompt[1]["content"]

        # The chapter's onstage list comes from the world view, not every active character.
        self.assertIn("本章出场名单：阿青", content)
        self.assertNotIn("未出场者", content)
        self.assertIn("沈知遥", content)
        self.assertIn("命名人物只能使用本章出场名单与【当前允许使用的专名】里的名字", content)

    def test_writer_prompt_includes_recent_thread_beats_without_deriving_name_anchors(self) -> None:
        context = SimpleNamespace(
            project_title="测试书",
            genre="都市悬疑",
            premise="陆明追查母亲留下的空白遗书。",
            setting_summary="临潮有回声账本。",
            chapter_number=3,
            chapter_plan_title="第二日",
            chapter_plan_one_line="提取回声残片",
            chapter_goals=["揭示母亲与回声账本有关"],
            previous_chapter_summaries=["陆明发现母亲留下的空白遗书。"],
            active_entities=[SimpleNamespace(name="陆明", description="主角")],
            active_threads=[
                SimpleNamespace(
                    name="空白遗书",
                    description="母亲线索",
                    recent_beats=["终端显示条目标题为“原型设计者：林若”，即母亲的名字。"],
                )
            ],
            active_relations=[],
            canon_name_anchors=[],
            timeline=None,
            audience_hints=None,
            reader_promise=None,
            arc_payoff_map=None,
            band_delight_schedule=None,
            chapter_experience_plan=None,
            active_subworlds=[],
            allowed_entities=["陆明"],
            chapter_entry_targets=[],
            entity_admission_rule="strict_named_character",
            next_band_summary=None,
            retrieved_memories=[],
        )

        prompt = build_single_chapter_draft_prompt(context)
        content = prompt[1]["content"]

        self.assertIn("原型设计者：林若", content)
        self.assertNotIn("Canon 命名锚点", content)
        self.assertNotIn("母亲姓名", content)
        self.assertIn("不得把前情中已经出现的姓名扩写、替换或另造别名", content)

    def test_canon_name_anchor_section_renders_explicit_anchors_for_any_role(self) -> None:
        section = _canon_name_anchor_section(
            SimpleNamespace(),
            anchors=[
                CanonNameAnchor(role_label="师父", canonical_name="甲乙"),
                CanonNameAnchor(role_label="母亲", canonical_name="丙丁"),
                CanonNameAnchor(role_label="", canonical_name="戊己"),
            ],
        )

        assert section is not None
        self.assertIn("【Canon 命名锚点】", section)
        self.assertIn("师父姓名：甲乙；必须逐字沿用，不得扩写、替换或另造别名。", section)
        self.assertIn("母亲姓名：丙丁", section)
        self.assertNotIn("戊己", section)
        self.assertIsNone(_canon_name_anchor_section(SimpleNamespace()))

    def test_writer_prompt_does_not_drop_active_thread_after_three_threads(self) -> None:
        context = SimpleNamespace(
            project_title="测试书",
            genre="都市悬疑",
            premise="陆明追查母亲留下的空白遗书。",
            setting_summary="临潮有回声账本。",
            chapter_number=3,
            chapter_plan_title="第二日",
            chapter_plan_one_line="提取回声残片",
            chapter_goals=["揭示母亲与回声账本有关"],
            previous_chapter_summaries=["陆明发现母亲留下的空白遗书。"],
            active_entities=[SimpleNamespace(name="陆明", description="主角")],
            active_threads=[
                SimpleNamespace(name="合作与危机", description="", status="resolved", priority=2, recent_beats=["合作线已暂时收束。"]),
                SimpleNamespace(name="母亲线索", description="", status="active", priority=2, recent_beats=["火灾录音确认是母亲的声音。"]),
                SimpleNamespace(name="神秘短信", description="", status="active", priority=2, recent_beats=["短信要求下午三点到旧港。"]),
                SimpleNamespace(name="空白遗书", description="", status="active", priority=2, recent_beats=["终端显示条目标题为“原型设计者：林若”，即母亲的名字。"]),
                SimpleNamespace(name="记忆删除", description="", status="active", priority=2, recent_beats=["公共记忆正在被系统性抹除。"]),
                SimpleNamespace(name="许安出现", description="", status="active", priority=2, recent_beats=["许安提供旧港火灾档案。"]),
                SimpleNamespace(name="陈伯伦消失", description="", status="active", priority=2, recent_beats=["陆明检索陈伯伦，发现记录消失。"]),
            ],
            active_relations=[],
            canon_name_anchors=[],
            timeline=None,
            audience_hints=None,
            reader_promise=None,
            arc_payoff_map=None,
            band_delight_schedule=None,
            chapter_experience_plan=None,
            active_subworlds=[],
            allowed_entities=["陆明"],
            chapter_entry_targets=[],
            entity_admission_rule="strict_named_character",
            next_band_summary=None,
            retrieved_memories=[],
        )

        prompt = build_single_chapter_draft_prompt(context)
        content = prompt[1]["content"]

        self.assertIn("原型设计者：林若", content)
        self.assertNotIn("母亲姓名", content)
        self.assertIn("陈伯伦", content)
        self.assertLess(content.index("母亲线索"), content.index("合作与危机"))

    def test_continuity_checker_warns_on_canon_name_drift_for_explicit_anchors(self) -> None:
        checker = _AnchoredContinuityChecker(
            [CanonNameAnchor(role_label="师父", canonical_name="甲乙")]
        )
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=3,
                title="第二日",
                body="山门前站着师父甲乙丙。他回头看了一眼，甲乙说：“走吧。”" * 30,
                end_of_chapter_summary="弟子认出师父甲乙丁。",
            ),
        )

        issues = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
        observed = {issue.entity_names[0] for issue in issues}
        self.assertEqual(verdict.verdict, "warn")
        self.assertEqual(verdict.recommended_action, "continue")
        self.assertTrue(all(issue.severity == "warning" for issue in issues))
        self.assertEqual(observed, {"甲乙丙", "甲乙丁"})
        self.assertTrue(all(issue.entity_names[1] == "甲乙" for issue in issues))
        self.assertTrue(all("师父" in issue.description for issue in issues))
        self.assertTrue(all("甲乙" in issue.suggested_fix for issue in issues))
        self.assertEqual(checker.anchor_reads, [("p1", 2)])

    def test_continuity_checker_warns_on_canon_name_drift_in_state_metadata(self) -> None:
        checker = _AnchoredContinuityChecker(
            [CanonNameAnchor(role_label="师父", canonical_name="甲乙")]
        )
        body = "师父甲乙留下了线索。" * 80
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=7,
                title="第六日",
                body=body,
                end_of_chapter_summary="弟子确认师父甲乙与旧事有关。",
                chapter_delta=chapter_delta(
                    body,
                    entity_changes=[
                        {
                            "ref": "char-disciple",
                            "field": "knowledge",
                            "new_value": "得知师父甲乙丁。",
                            "evidence": "师父甲乙留下了线索",
                        }
                    ],
                ),
            ),
        )

        issues = [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"]
        self.assertEqual(verdict.verdict, "warn")
        self.assertEqual([(issue.entity_names[0], issue.severity) for issue in issues], [("甲乙丁", "warning")])
        self.assertEqual(checker.anchor_reads, [("p1", 6)])

    def test_continuity_checker_has_no_canon_name_anchors_without_kinship_edges(self) -> None:
        book_state = _BookStateQueryStub(
            ["终端显示条目标题为“原型设计者：林若”，即母亲的名字。"]
        )
        checker = ContinuityChecker(
            SimpleNamespace(),
            book_state_query=book_state,
        )
        verdict = checker.check(
            "p1",
            WriterOutput(
                chapter_number=3,
                title="第二日",
                body="周岚说：“你母亲叫林婉清。”残片里又有人称她为林若水。" * 30,
                end_of_chapter_summary="陆明发现母亲林婉清是设计者。",
            ),
        )

        self.assertEqual(
            [issue for issue in verdict.issues if issue.rule_name == "canon_name_drift"],
            [],
        )
        self.assertEqual(book_state.thread_reads, [])

    def test_canon_name_violations_are_role_agnostic(self) -> None:
        for role in ("师父", "母亲", "盟友"):
            with self.subTest(role=role):
                violations = find_canon_name_violations(
                    [CanonNameAnchor(role_label=role, canonical_name="甲乙")],
                    "山门前站着甲乙丙。",
                )

                self.assertEqual(len(violations), 1)
                violation = violations[0]
                self.assertEqual(violation.role_label, role)
                self.assertEqual(violation.canonical_name, "甲乙")
                self.assertEqual(violation.observed_name, "甲乙丙")
                self.assertEqual(violation.reason, "expanded_canonical_name")
                self.assertIn("甲乙丙", violation.evidence)

    def test_canon_name_violations_cover_every_anchor_once_per_observed_name(self) -> None:
        violations = find_canon_name_violations(
            [
                CanonNameAnchor(role_label="师父", canonical_name="甲乙"),
                CanonNameAnchor(role_label="盟友", canonical_name="丙丁"),
            ],
            "师父甲乙丙。盟友丙丁戊。后来又提到师父甲乙丙。",
        )

        self.assertEqual(
            [(v.role_label, v.observed_name) for v in violations],
            [("师父", "甲乙丙"), ("盟友", "丙丁戊")],
        )

    def test_canon_name_violations_allow_the_canonical_name_in_ordinary_prose(self) -> None:
        violations = find_canon_name_violations(
            [CanonNameAnchor(role_label="师父", canonical_name="甲乙")],
            "弟子拜见师父甲乙，也找到了甲乙的剑。甲乙说：“进来。”这把剑是甲乙的。",
        )

        self.assertEqual(violations, [])

    def test_canon_name_violations_ignore_function_word_continuations(self) -> None:
        for text in (
            "说的就是甲乙了。",
            "他们只提到甲乙和。",
            "没人提起甲乙他。",
            "不会是甲乙不是。",
        ):
            with self.subTest(text=text):
                violations = find_canon_name_violations(
                    [CanonNameAnchor(role_label="师父", canonical_name="甲乙")],
                    text,
                )

                self.assertEqual(violations, [])

    def test_canon_name_violations_need_usable_anchors_and_text(self) -> None:
        anchor = CanonNameAnchor(role_label="师父", canonical_name="甲乙")

        self.assertEqual(find_canon_name_violations([], "师父甲乙丙。"), [])
        self.assertEqual(find_canon_name_violations([anchor], ""), [])
        self.assertEqual(
            find_canon_name_violations(
                [
                    CanonNameAnchor(role_label="", canonical_name="甲乙"),
                    CanonNameAnchor(role_label="师父", canonical_name=""),
                ],
                "师父甲乙丙。",
            ),
            [],
        )

    def test_canon_name_drift_autofix_replaces_observed_name_across_writer_output(self) -> None:
        body = "旧港是母亲最后出现的地方。陆明发现母亲林婉是原型设计者，报告撰写人是林婉。"
        output = WriterOutput(
            chapter_number=3,
            title="第二日",
            body=body,
            end_of_chapter_summary="陆明在记忆馆发现母亲林婉是回声账本原型设计者之一。",
            chapter_delta=chapter_delta(
                body,
                events=[
                    {
                        "summary": "陆明发现林婉留下技术报告",
                        "participant_refs": ["char-luming", "new:林婉"],
                        "significance": "major",
                        "visibility": "public",
                        "evidence": "陆明发现母亲林婉是原型设计者",
                    }
                ],
            ),
        )
        review = ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="母亲姓名漂移。",
                    entity_names=["林婉", "林若"],
                )
            ],
        )

        fixed = ChapterPipeline._apply_canon_name_drift_autofix(output, review)

        self.assertIsNotNone(fixed)
        assert fixed is not None
        serialized_content = fixed.model_dump_json(exclude={"generation_meta"})
        self.assertNotIn("林婉", serialized_content)
        self.assertIn("林若", fixed.body)
        self.assertIn("母亲最后出现的地方", fixed.body)
        self.assertEqual(fixed.char_count, len(fixed.body))
        self.assertEqual(fixed.generation_meta["canon_name_autofix"]["林婉"], "林若")

    def test_canon_name_drift_autofix_ignores_non_name_observed_terms(self) -> None:
        output = WriterOutput(
            chapter_number=13,
            title="旧港的余烬",
            body="许安说：“因为是我帮你母亲申请的工位。”这是他和母亲之间的暗号。录音结束。",
            end_of_chapter_summary="陆明和许安发现旧港线索。",
        )
        review = ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="误报的母亲姓名漂移。",
                    entity_names=["本人", "林若"],
                ),
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="误报的母亲姓名漂移。",
                    entity_names=["之间", "林若"],
                ),
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="误报的母亲姓名漂移。",
                    entity_names=["录音", "林若"],
                ),
            ],
        )

        fixed = ChapterPipeline._apply_canon_name_drift_autofix(output, review)

        self.assertIsNone(fixed)

    def test_canon_name_drift_autofix_ignores_expanded_canonical_phrase(self) -> None:
        output = WriterOutput(
            chapter_number=13,
            title="旧港的余烬",
            body="母亲林若警告他不要相信回声账本。",
            end_of_chapter_summary="陆明发现母亲留下警告。",
        )
        review = ReviewVerdict(
            verdict="fail",
            issues=[
                ContinuityIssue(
                    rule_name="canon_name_drift",
                    severity="error",
                    description="误报的母亲姓名扩展。",
                    entity_names=["林若警告", "林若"],
                )
            ],
        )

        fixed = ChapterPipeline._apply_canon_name_drift_autofix(output, review)

        self.assertIsNone(fixed)

    def test_project_detail_exposes_subworld_summaries(self) -> None:
        with TemporaryDirectory():
            engine = get_engine(postgres_test_url("detail"))
            init_db(engine)
            session = get_session_factory(engine)()
            try:
                updater = StateUpdater(session)
                project = updater.create_project(
                    title="书",
                    premise="p",
                    genre="g",
                    runtime_policy=RuntimePolicy.for_profile("standard"),
                )
                global_core = updater.create_subworld(
                    project_id=project.id,
                    origin_arc_id=None,
                    parent_subworld_id=None,
                    name="global_core",
                    purpose="核心",
                    scope="global_core",
                    metadata={},
                )
                detail = build_project_detail(
                    session=session,
                    project=project,
                    display_datetime=lambda dt: "",
                )
            finally:
                session.close()
                engine.dispose()

        self.assertTrue(detail.subworlds)
        self.assertEqual(detail.subworlds[0].name, "global_core")


if __name__ == "__main__":
    unittest.main()


def test_project_scoped_subworld_ids_are_stable_and_isolated() -> None:
    assert project_scoped_subworld_id("project-a", "local") == project_scoped_subworld_id("project-a", "local")
    assert project_scoped_subworld_id("project-a", "local") != project_scoped_subworld_id("project-b", "local")
