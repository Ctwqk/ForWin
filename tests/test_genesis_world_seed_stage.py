from __future__ import annotations

import json
import re
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from forwin.api_schema import BookGenesisPatchRequest, ProjectCreateRequest
from forwin.cli import build_parser, cmd_generate
from forwin.config import InfrastructureConfig
from forwin.genesis import GENESIS_STAGE_ORDER, BookGenesisService
from forwin.genesis.messages import (
    _build_stage_generation_messages,
    _build_stage_refine_messages,
    _world_seed_output_requirement,
)
from forwin.genesis.names_paths import GenesisStageInvalidError
from forwin.genesis.world_seed import WORLD_SEED_KEYS, fallback_world_seed
from forwin.mcp.models import STAGE_KEY_ORDER, GenesisView, StageKey, StageStateView
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision
from forwin.models.project import Project
from forwin.runtime.container import RuntimeContainer
from forwin.state.updater import StateUpdater
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url


REPO_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_STAGES = GENESIS_STAGE_ORDER[:-1]

STORY_ENGINE = {
    "core_cast": [
        {"id": "cast-1", "name": "甲", "secrets": ["甲隐瞒的往事"]},
        {"id": "cast-2", "name": "乙", "secrets": []},
    ],
    "opposition": [{"id": "opp-1", "name": "丙"}],
    "factions": [{"id": "faction-1", "name": "丁"}],
    "relationship_axes": [],
    "reader_promises": [],
    "long_arcs": [],
}
MAP_ATLAS = {
    "overview": "一城一渡口。",
    "topology_rules": [],
    "submaps": [{"id": "subworld-1", "name": "主舞台"}],
    "regions": [{"id": "region-1", "name": "主城", "subworld_name": "主舞台", "level": 1}],
    "nodes": [
        {"id": "node-1", "name": "旧城", "kind": "city", "parent_subworld": "subworld-1", "parent_region_id": "region-1"},
        {"id": "node-2", "name": "渡口", "kind": "port", "parent_subworld": "subworld-1", "parent_region_id": "region-1"},
    ],
    "edges": [],
}
BLUEPRINT = {
    "summary": "两卷蓝图",
    "arcs": [
        {"arc_number": 1, "title": "第一卷", "arc_synopsis": "卷一梗概", "chapter_start": 1, "chapter_end": 3, "chapter_count": 3},
        {"arc_number": 2, "title": "第二卷", "arc_synopsis": "卷二梗概", "chapter_start": 4, "chapter_end": 6, "chapter_count": 3},
    ],
}
UPSTREAM_PATCH = {
    "world": {
        "world_bible": {"overview": "世界概览"},
        "map_atlas": MAP_ATLAS,
        "story_engine": STORY_ENGINE,
    },
    "book_arc_blueprint": BLUEPRINT,
}


def valid_seed() -> dict:
    return deepcopy({
        "calendar": {"epoch_label": "开篇纪元", "opening_minute": 0, "units": [{"name": "日", "minutes": 1440}]},
        "lexicon": {},
        "actors": [
            {
                "ref": "cast-1", "actor_tier": "major", "location_ref": "node-1",
                "agendas": [{"id": "agenda-1", "goal": "目标", "priority": 3}],
                "intents": [{
                    "id": "intent-1", "agenda_id": "agenda-1", "action_type": "move",
                    "location_ref": "node-2", "scheduled_in": {"amount": 1, "unit": "日"},
                    "impact_level": "minor",
                }],
            },
            {"ref": "cast-2", "actor_tier": "minor", "location_ref": "node-2"},
            {"ref": "opp-1", "actor_tier": "minor", "location_ref": "node-2"},
            {"ref": "faction-1", "actor_tier": "background", "location_ref": "node-1"},
        ],
        "relations": [{"source": "cast-1", "target": "faction-1", "edge_type": "member_of", "note": "", "visibility": "public"}],
        "facts": [{"id": "fact-1", "proposition": "往事", "truth_value": True, "sensitivity": "secret", "related_refs": ["cast-1"]}],
        "knowledge": [{"observer_ref": "cast-1", "fact_id": "fact-1", "state": "knows", "channel": "seeded"}],
        "rules": [],
        "clocks": [],
        "reader_promises": [],
        "reveal_candidates": [{"fact_id": "fact-1", "arc_number": 2, "note": ""}],
    })


class _StubLLM:
    api_key = "test-key"
    codex_enabled = False
    profile_id = ""
    profile_name = ""
    model = "stub-model"
    base_url = ""
    last_call_result = None

    def __init__(self, reply: str = "{}") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"messages": messages, **kwargs})
        return self.reply


def _codes(issues) -> set[str]:
    return {issue["code"] if isinstance(issue, dict) else issue.code for issue in issues}


def test_world_seed_generation_messages_define_appellation_direction():
    messages = _build_stage_generation_messages(
        None, project=Project(id="p", title="t", premise="p", genre="g"),
        pack=deepcopy(UPSTREAM_PATCH), stage_key="world_seed", fallback={},
    )
    content = messages[-1]["content"]
    assert "appellation 是 source 对 target 的称谓，即 target 相对于 source 的角色" in content
    assert "关系边从 source 指向 target" in content
    assert "source 的 appellation → target" in content


def test_container_gives_world_seed_the_full_configured_token_budget():
    config = InfrastructureConfig(database_url="sqlite:///:memory:", max_tokens=20000)
    service = RuntimeContainer._build_book_genesis_service(
        config=config,
        llm_client=_StubLLM(),
        skill_runtime=SimpleNamespace(router=None, prompt_layer_builder=None),
        artifact_store=None,
    )
    assert (service.max_tokens, service.world_seed_max_tokens) == (1600, 20000)
    assert BookGenesisService(llm_client=_StubLLM()).world_seed_max_tokens == 16384


def _refine_messages(stage_key: str) -> list[dict[str, str]]:
    return _build_stage_refine_messages(
        None, pack=deepcopy(UPSTREAM_PATCH), stage_key=stage_key, instruction="改", target_path="",
        current_payload={}, support_context={}, fallback_stage_payload={},
    )


def test_world_seed_refine_messages_carry_the_output_contract():
    content = _refine_messages("world_seed")[-1]["content"]
    assert "【输出要求】" in content
    assert _world_seed_output_requirement() in content
    for key in WORLD_SEED_KEYS:
        assert key in content
    assert "【输出要求】" not in _refine_messages("story_engine")[-1]["content"]


@pytest.mark.parametrize("target_path,wrap_scalar", [("actors[0]", False), ("calendar.epoch_label", True), ("", True)])
def test_world_seed_targeted_refine_omits_the_whole_seed_contract(target_path, wrap_scalar):
    messages = _build_stage_refine_messages(
        None, pack=deepcopy(UPSTREAM_PATCH), stage_key="world_seed", instruction="改", target_path=target_path,
        current_payload={}, support_context={}, fallback_stage_payload={}, wrap_scalar_value=wrap_scalar,
    )
    assert _world_seed_output_requirement() not in messages[-1]["content"]


class _LockRefusingClient:
    """Answers the CLI's Genesis calls and refuses the world_seed lock like the API's 409."""

    issues = [
        {"severity": "error", "code": "unknown_location_ref", "path": "actors[0].location_ref", "message": "node-9 is not on the map"},
        {"severity": "warning", "code": "missing_reveal", "path": "facts[0]", "message": "fact-1 has no reveal candidate"},
    ]

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def project_create(self, **_kwargs):
        return SimpleNamespace(project=SimpleNamespace(id="proj-cli"))

    async def genesis_stage_generate(self, *, project_id: str, stage_key: str):
        self.calls.append(("generate", stage_key))

    async def genesis_stage_lock(self, *, project_id: str, stage_key: str):
        self.calls.append(("lock", stage_key))
        if stage_key == "world_seed":
            raise ValueError(
                f"ForWin API POST /api/projects/{project_id}/genesis/stages/world_seed/lock failed with 409: "
                "world_seed 有 1 个校验错误，不能锁定"
            )

    async def genesis_get(self, project_id: str):
        return GenesisView(
            project_id=project_id,
            stage_states=[StageStateView(stage_key="world_seed", validation_issues=self.issues)],
        )

    async def task_active_generation_check(self, **_kwargs):
        raise AssertionError("a refused lock must stop before start-writing")

    async def project_start_writing(self, **_kwargs):
        raise AssertionError("a refused lock must stop before start-writing")


def test_cli_reports_world_seed_lock_refusal(capsys):
    client = _LockRefusingClient()
    args = build_parser().parse_args(["generate", "--title", "T", "--premise", "P", "--genre", "G"])

    with patch("forwin.cli._api_client", return_value=client), pytest.raises(SystemExit) as exited:
        cmd_generate(args)

    assert exited.value.code == 2
    assert client.calls[-1] == ("lock", "world_seed")
    report = capsys.readouterr().err
    for issue in client.issues:
        assert f"{issue['severity']} {issue['code']} ({issue['path']}): {issue['message']}" in report
    assert "world_seed 有 1 个校验错误" in report
    for next_step in ("genesis_stage_refine", "genesis_stage_lock", "project_start_writing", "proj-cli"):
        assert next_step in report


class WorldSeedStageOrderTests(unittest.TestCase):
    def test_stage_order_ends_with_world_seed_and_copies_match(self) -> None:
        self.assertEqual(GENESIS_STAGE_ORDER[-1], "world_seed")
        self.assertEqual(STAGE_KEY_ORDER, GENESIS_STAGE_ORDER)
        self.assertEqual(get_args(StageKey), GENESIS_STAGE_ORDER)
        app_state = (REPO_ROOT / "forwin/ui_assets/home/app_state.js").read_text(encoding="utf-8")
        match = re.search(r"const GENESIS_STAGE_ORDER = \[(.*?)\];", app_state)
        assert match is not None
        self.assertEqual(tuple(re.findall(r"'([^']+)'", match.group(1))), GENESIS_STAGE_ORDER)
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in (REPO_ROOT / "forwin").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
            and b"execution_bootstrap" in path.read_bytes()
        ]
        self.assertEqual(offenders, [])


class WorldSeedServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = get_engine(postgres_test_url("genesis-world-seed-stage"))
        init_db(self.engine)
        self.session_factory = get_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _ready_upstream(self, session, service: BookGenesisService) -> tuple[Project, BookGenesisRevision]:
        updater = StateUpdater(session)
        project = Project(
            id="proj-world-seed",
            title="初始世界测试书",
            premise="测试 world_seed 阶段。",
            genre="测试题材",
            target_total_chapters=6,
        )
        session.add(project)
        session.flush()
        revision = service.create_initial_revision(session=session, updater=updater, project=project)
        revision = service.patch_pack(
            session=session, updater=updater, project=project, revision=revision, patch=deepcopy(UPSTREAM_PATCH),
        )
        for stage_key in UPSTREAM_STAGES:
            revision = service.lock_stage(
                session=session, updater=updater, project=project, revision=revision, stage_key=stage_key,
            )
        return project, revision

    def _generate_world_seed(self, service: BookGenesisService) -> dict:
        with self.session_factory() as session:
            project, revision = self._ready_upstream(session, service)
            revision, _trace = service.generate_stage(
                session=session, updater=StateUpdater(session), project=project, revision=revision,
                stage_key="world_seed",
            )
            session.commit()
            return service.load_pack(revision)

    def test_world_seed_generation_normalizes_and_records_issues(self) -> None:
        reply = valid_seed()
        reply["actors"][0]["intents"][0]["scheduled_in"]["unit"] = "unknown-unit"
        del reply["actors"][1]["location_ref"]
        del reply["rules"]
        service = BookGenesisService(llm_client=_StubLLM(json.dumps(reply, ensure_ascii=False)))

        pack = self._generate_world_seed(service)

        seed = pack["world_seed"]
        self.assertEqual(tuple(seed), WORLD_SEED_KEYS)
        self.assertEqual(seed["rules"], [])
        self.assertEqual(seed["actors"][1]["location_ref"], "")
        self.assertEqual(seed["actors"][1]["travel_mode_ids"], [])
        state = pack["stage_states"]["world_seed"]
        self.assertIn("unknown_time_unit", _codes(state["validation_issues"]))
        self.assertIn("minor_actor_without_location", _codes(state["validation_issues"]))
        self.assertFalse(state["degraded"])
        self.assertEqual(state["status"], "generated")

    def test_world_seed_generation_prompt_follows_section_contract(self) -> None:
        client = _StubLLM(json.dumps(valid_seed(), ensure_ascii=False))
        service = BookGenesisService(llm_client=client)

        self._generate_world_seed(service)

        user_content = client.calls[0]["messages"][-1]["content"]
        titles = re.findall(r"【([^】]+)】", user_content)
        self.assertEqual(titles, [
            "阶段", "输出要求", "阶段硬约束", "已锁定阶段上下文（视为当前真值）", "项目 genre",
            "WorldBible", "地图节点表", "StoryEngine", "卷列表", "当前 WorldSeed", "参考骨架",
        ])
        self.assertIn("测试题材", user_content)
        self.assertIn('{"id": "node-2", "name": "渡口", "kind": "port", "submap": "subworld-1"}', user_content)
        self.assertIn('{"arc_number": 2, "synopsis": "卷二梗概"}', user_content)
        for key in WORLD_SEED_KEYS:
            self.assertIn(key, user_content)

    def test_world_seed_generation_uses_world_seed_token_budget(self) -> None:
        client = _StubLLM(json.dumps(valid_seed(), ensure_ascii=False))
        service = BookGenesisService(llm_client=client, max_tokens=1600)

        self._generate_world_seed(service)
        self.assertEqual(client.calls[0]["max_tokens"], 16384)

        client.calls.clear()
        service._call_json_with_trace(
            messages=[{"role": "user", "content": "brief"}], fallback={}, stage_key="brief",
        )
        self.assertEqual(client.calls[0]["max_tokens"], 1600)

    def test_world_seed_fallback_marks_degraded(self) -> None:
        client = _StubLLM("this is not json")
        service = BookGenesisService(llm_client=client, max_tokens=1600, world_seed_max_tokens=12000)

        pack = self._generate_world_seed(service)

        story_engine = pack["world"]["story_engine"]
        self.assertEqual(pack["world_seed"], fallback_world_seed(story_engine))
        self.assertEqual([actor["ref"] for actor in pack["world_seed"]["actors"]], ["cast-1", "cast-2", "opp-1", "faction-1"])
        self.assertTrue(pack["stage_states"]["world_seed"]["degraded"])
        self.assertEqual([call["max_tokens"] for call in client.calls], [12000, 12000])

    def test_retry_keeps_world_seed_cap_while_other_stages_shrink(self) -> None:
        client = _StubLLM("this is not json")
        service = BookGenesisService(llm_client=client, max_tokens=1600, world_seed_max_tokens=12000)
        with self.session_factory() as session:
            project, revision = self._ready_upstream(session, service)
            revision, _trace = service.refine_stage(
                session=session, updater=StateUpdater(session), project=project, revision=revision,
                stage_key="world_seed", instruction="补全初始世界",
            )
            session.commit()
            pack = service.load_pack(revision)

        self.assertEqual([call["max_tokens"] for call in client.calls], [12000, 12000])
        self.assertTrue(pack["stage_states"]["world_seed"]["degraded"])

        client.calls.clear()
        service._call_json_with_trace(
            messages=[{"role": "user", "content": "brief"}], fallback={}, stage_key="brief",
        )
        self.assertEqual([call["max_tokens"] for call in client.calls], [1600, 900])

    def test_world_seed_whole_stage_refine_normalizes_with_world_seed_budget(self) -> None:
        client = _StubLLM(json.dumps(valid_seed(), ensure_ascii=False))
        service = BookGenesisService(llm_client=client, max_tokens=1600, world_seed_max_tokens=12000)
        with self.session_factory() as session:
            project, revision = self._ready_upstream(session, service)
            revision, _trace = service.refine_stage(
                session=session, updater=StateUpdater(session), project=project, revision=revision,
                stage_key="world_seed", instruction="补全初始世界",
            )
            session.commit()
            pack = service.load_pack(revision)

        self.assertEqual(client.calls[0]["max_tokens"], 12000)
        self.assertEqual(tuple(pack["world_seed"]), WORLD_SEED_KEYS)
        state = pack["stage_states"]["world_seed"]
        self.assertEqual(state["status"], "edited")
        self.assertEqual(state["validation_issues"], [])

    def _locked_world_seed(self, session, service: BookGenesisService) -> tuple[Project, BookGenesisRevision]:
        project, revision = self._ready_upstream(session, service)
        updater = StateUpdater(session)
        revision = service.patch_pack(
            session=session, updater=updater, project=project, revision=revision, patch={"world_seed": valid_seed()},
        )
        revision = service.lock_stage(
            session=session, updater=updater, project=project, revision=revision, stage_key="world_seed",
        )
        self.assertEqual(project.creation_status, "genesis_ready")
        return project, revision

    def _assert_world_seed_stale(self, service: BookGenesisService, revision: BookGenesisRevision) -> dict:
        state = service.load_pack(revision)["stage_states"]["world_seed"]
        self.assertFalse(state["locked"])
        self.assertEqual(state["status"], "stale")
        return state

    def test_story_engine_patch_unlocks_locked_world_seed_until_relocked(self) -> None:
        service = BookGenesisService(llm_client=_StubLLM())
        story_engine = deepcopy(STORY_ENGINE)
        story_engine["core_cast"] = [item for item in story_engine["core_cast"] if item["id"] != "cast-2"]
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project, revision = self._locked_world_seed(session, service)
            revision = service.patch_pack(
                session=session, updater=updater, project=project, revision=revision,
                patch={"world": {"story_engine": story_engine}},
            )
            state = self._assert_world_seed_stale(service, revision)
            self.assertIn("unknown_actor_ref", _codes(state["validation_issues"]))

            revision = service.lock_stage(
                session=session, updater=updater, project=project, revision=revision, stage_key="story_engine",
            )
            self.assertEqual(project.creation_status, "creating")
            with self.assertRaises(GenesisStageInvalidError):
                service.lock_stage(
                    session=session, updater=updater, project=project, revision=revision, stage_key="world_seed",
                )

    def test_relocking_world_seed_after_compatible_upstream_change_makes_project_ready(self) -> None:
        service = BookGenesisService(llm_client=_StubLLM())
        story_engine = deepcopy(STORY_ENGINE)
        story_engine["core_cast"][0]["name"] = "甲改"
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project, revision = self._locked_world_seed(session, service)
            revision = service.patch_pack(
                session=session, updater=updater, project=project, revision=revision,
                patch={"world": {"story_engine": story_engine}},
            )
            self.assertEqual(self._assert_world_seed_stale(service, revision)["validation_issues"], [])
            revision = service.lock_stage(
                session=session, updater=updater, project=project, revision=revision, stage_key="story_engine",
            )
            self.assertEqual(project.creation_status, "creating")
            revision = service.lock_stage(
                session=session, updater=updater, project=project, revision=revision, stage_key="world_seed",
            )
            self.assertEqual(project.creation_status, "genesis_ready")

    def test_generating_or_refining_an_upstream_stage_marks_world_seed_stale(self) -> None:
        story_engine = deepcopy(STORY_ENGINE)
        story_engine["core_cast"][1]["name"] = "乙改"
        blueprint = deepcopy(BLUEPRINT)
        blueprint["summary"] = "两卷蓝图（改）"
        map_atlas = deepcopy(MAP_ATLAS)
        map_atlas["overview"] = "一城一渡口，外加新码头。"
        for stage_key, operation, reply in (
            ("story_engine", "generate", story_engine),
            ("map", "generate", map_atlas),
            ("book_blueprint", "refine", blueprint),
        ):
            with self.subTest(stage_key=stage_key), self.session_factory() as session:
                service = BookGenesisService(llm_client=_StubLLM(json.dumps(reply, ensure_ascii=False)))
                project, revision = self._locked_world_seed(session, service)
                if operation == "generate":
                    revision, _trace = service.generate_stage(
                        session=session, updater=StateUpdater(session), project=project, revision=revision,
                        stage_key=stage_key,
                    )
                else:
                    revision, _trace = service.refine_stage(
                        session=session, updater=StateUpdater(session), project=project, revision=revision,
                        stage_key=stage_key, instruction="改一处",
                    )
                self._assert_world_seed_stale(service, revision)
                session.rollback()

    def test_world_seed_lock_survives_unrelated_or_identical_patches(self) -> None:
        service = BookGenesisService(llm_client=_StubLLM())
        with self.session_factory() as session:
            updater = StateUpdater(session)
            project, revision = self._locked_world_seed(session, service)
            for patch in ({"book_brief": {"title": "新书名"}}, {"world": {"story_engine": deepcopy(STORY_ENGINE)}}):
                revision = service.patch_pack(
                    session=session, updater=updater, project=project, revision=revision, patch=patch,
                )
                state = service.load_pack(revision)["stage_states"]["world_seed"]
                self.assertTrue(state["locked"], patch)
                self.assertEqual(state["status"], "locked")

    def test_empty_world_seed_is_not_marked_stale(self) -> None:
        service = BookGenesisService(llm_client=_StubLLM())
        story_engine = deepcopy(STORY_ENGINE)
        story_engine["core_cast"][0]["name"] = "甲改"
        with self.session_factory() as session:
            project, revision = self._ready_upstream(session, service)
            revision = service.patch_pack(
                session=session, updater=StateUpdater(session), project=project, revision=revision,
                patch={"world": {"story_engine": story_engine}},
            )
            self.assertEqual(service.load_pack(revision)["stage_states"]["world_seed"]["status"], "todo")

    def test_world_seed_refine_support_context_is_scoped(self) -> None:
        service = BookGenesisService(llm_client=_StubLLM())
        with self.session_factory() as session:
            _project, revision = self._ready_upstream(session, service)
            pack = service.load_pack(revision)

        context = service._refine_support_context(pack=pack, stage_key="world_seed")

        self.assertEqual(set(context), {"story_engine", "map_nodes", "arcs"})
        self.assertEqual(context["story_engine"], pack["world"]["story_engine"])
        self.assertEqual([node["id"] for node in context["map_nodes"]], ["node-1", "node-2"])
        self.assertEqual(context["arcs"], [
            {"arc_number": 1, "synopsis": "卷一梗概"},
            {"arc_number": 2, "synopsis": "卷二梗概"},
        ])


class WorldSeedHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        database_url = postgres_test_url("genesis-world-seed-http")
        self.engine = get_engine(database_url)
        init_db(self.engine)
        self.harness = HttpRuntimeHarness(
            session_factory=get_session_factory(self.engine),
            config=InfrastructureConfig(
                database_url=database_url,
                minimax_api_key="test-key",
                minimax_base_url="http://example.invalid",
                minimax_model="fake-model",
            ),
            engine=self.engine,
        )
        self.client = TestClient(self.harness.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _project_with_locked_upstream(self, world_seed: dict) -> str:
        created = self.harness.create_project(
            ProjectCreateRequest.model_validate(
                {"title": "初始世界锁定书", "premise": "测试锁定。", "genre": "测试题材", "target_total_chapters": 6}
            )
        )
        self.harness.patch_project_genesis(
            created.project_id,
            BookGenesisPatchRequest.model_validate({**deepcopy(UPSTREAM_PATCH), "world_seed": world_seed}),
        )
        for stage_key in UPSTREAM_STAGES:
            response = self.client.post(f"/api/projects/{created.project_id}/genesis/stages/{stage_key}/lock")
            self.assertEqual(response.status_code, 200, response.text)
        return created.project_id

    def _lock_world_seed(self, project_id: str):
        return self.client.post(f"/api/projects/{project_id}/genesis/stages/world_seed/lock")

    def test_world_seed_lock_refused_while_validation_errors(self) -> None:
        seed = valid_seed()
        seed["actors"] = [actor for actor in seed["actors"] if actor["ref"] != "cast-2"]
        project_id = self._project_with_locked_upstream(seed)

        refused = self._lock_world_seed(project_id)
        self.assertEqual(refused.status_code, 409)
        self.assertIn("missing_actor", refused.json()["detail"])
        detail = self.harness.get_project_genesis(project_id)
        self.assertFalse(detail.pack.stage_states["world_seed"].locked)
        self.assertEqual(detail.creation_status, "creating")

        self.harness.patch_project_genesis(
            project_id, BookGenesisPatchRequest.model_validate({"world_seed": valid_seed()}),
        )
        locked = self._lock_world_seed(project_id)
        self.assertEqual(locked.status_code, 200, locked.text)
        body = locked.json()
        self.assertTrue(body["pack"]["stage_states"]["world_seed"]["locked"])
        self.assertEqual(body["creation_status"], "genesis_ready")
        self.assertTrue(body["can_start_writing"])

    def test_world_seed_lock_revalidates_against_current_story_engine(self) -> None:
        project_id = self._project_with_locked_upstream(valid_seed())
        story_engine = deepcopy(STORY_ENGINE)
        story_engine["core_cast"] = [item for item in story_engine["core_cast"] if item["id"] != "cast-2"]
        self.harness.patch_project_genesis(
            project_id, BookGenesisPatchRequest.model_validate({"world": {"story_engine": story_engine}}),
        )

        refused = self._lock_world_seed(project_id)

        self.assertEqual(refused.status_code, 409)
        self.assertRegex(refused.json()["detail"], "missing_actor|unknown_actor_ref")

    def test_patch_pack_world_seed_recomputes_issues(self) -> None:
        project_id = self._project_with_locked_upstream(valid_seed())
        self.assertEqual(self._lock_world_seed(project_id).status_code, 200)
        seed = valid_seed()
        seed["actors"][0]["location_ref"] = "node-missing"

        detail = self.harness.patch_project_genesis(
            project_id, BookGenesisPatchRequest.model_validate({"world_seed": seed}),
        )

        state = detail.pack.stage_states["world_seed"]
        self.assertEqual(state.status, "edited")
        self.assertFalse(state.locked)
        self.assertIn("unknown_location_ref", _codes(state.validation_issues))
        self.assertEqual(detail.pack.world_seed["actors"][0]["location_ref"], "node-missing")
        self.assertEqual(detail.pack.world_seed["actors"][0]["travel_mode_ids"], [])


if __name__ == "__main__":
    unittest.main()
