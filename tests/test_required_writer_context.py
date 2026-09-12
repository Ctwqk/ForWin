"""Required accepted facts survive selection and actual Writer requests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.book_state import BookStateRepository
from forwin.book_state.query import BookStateQuery
from forwin.models import Project
from forwin.models.project import ArcPlanVersion
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.protocol.book_state import FactNode, WorldEdge, WorldNode
from forwin.protocol.context import ChapterContextPack, RepairContract, WritingPack
from forwin.protocol.scene import ScenePlan
from forwin.retrieval.broker_core.broker import RetrievalBroker
from forwin.retrieval.source_identity import CanonReadBaseline
from tests.postgres import postgres_test_url
from tests.test_writer_context_budget import prompts


@pytest.fixture
def required_session():
    engine = get_engine(postgres_test_url())
    init_db(engine)
    try:
        with get_session_factory(engine).begin() as session:
            session.add(
                Project(
                    id="required",
                    title="人证",
                    premise="核实",
                    genre="悬疑",
                    setting_summary="港",
                )
            )
            session.flush()
            session.add(
                ArcPlanVersion(id="arc", project_id="required", arc_synopsis="核实")
            )
            session.flush()
            repo = BookStateRepository(session)
            for i in range(24):
                repo.create_world_node(
                    WorldNode(
                        id=f"person-{i}",
                        project_id="required",
                        node_type="character",
                        name=f"证人{i:02}",
                        aliases=[f"别名{i:02}", *(["同名"] if i in (20, 21) else [])],
                        importance=10 if i < 10 else 1,
                        state={"custody": f"已释放{i:02}"},
                        description=f"身份{i:02}",
                    )
                )
                repo.append_world_node_state(
                    project_id="required",
                    node_id=f"person-{i}",
                    node_type="character",
                    as_of_chapter=0,
                    state={"custody": f"已释放{i:02}"},
                )
            repo.create_world_node(
                WorldNode(
                    id="port",
                    project_id="required",
                    node_type="location",
                    name="码头",
                    state={"access": "封闭"},
                )
            )
            repo.append_world_node_state(
                project_id="required",
                node_id="port",
                node_type="location",
                as_of_chapter=0,
                state={"access": "封闭"},
            )
            repo.create_world_edge(
                WorldEdge(
                    id="bond",
                    project_id="required",
                    source_id="person-20",
                    target_id="person-21",
                    edge_family="social",
                    edge_type="ally_of",
                    state={"trust": "已破裂"},
                )
            )
            repo.create_fact_node(
                FactNode(
                    id="proof",
                    project_id="required",
                    proposition="保管权已移交",
                    related_node_refs=["node:person-22"],
                    related_edge_refs=["edge:bond"],
                )
            )
            yield session
    finally:
        engine.dispose()


def base_context(session, **updates):
    baseline = CanonReadBaseline.capture(session, "required", as_of_chapter=1)
    query = BookStateQuery(session, baseline=baseline)
    return ChapterContextPack(
        project_id="required",
        project_title="人证",
        premise="核实",
        genre="悬疑",
        setting_summary="港",
        chapter_number=2,
        chapter_plan_title="核实",
        chapter_plan_one_line="核对登记",
        chapter_goals=[],
        canon_read_baseline=baseline,
        active_entities=query.active_entities("required", as_of_chapter=1),
        active_relations=query.active_relations("required", as_of_chapter=1),
        allowed_entities=[f"证人{i:02}" for i in range(24)],
    ).model_copy(update=updates)


def build_pack(monkeypatch, session, base, budget=10):
    import forwin.retrieval.broker_core.broker as module

    monkeypatch.setattr(
        module,
        "_assemble_context",
        lambda *a, **kw: base.model_copy(
            update={"canon_read_baseline": kw["baseline"]}
        ),
    )
    broker = RetrievalBroker(
        context_budget_chars=budget,
        memory_index=SimpleNamespace(search=lambda **kw: []),
    )
    monkeypatch.setattr(
        broker,
        "build_world_model_pack",
        lambda *a, **kw: WritingPack(
            project_id="required", accepted_cognition=base.accepted_cognition
        ),
    )
    pack = broker.build_chapter_context(
        SimpleNamespace(session=session), "required", SimpleNamespace(chapter_number=2)
    )
    return broker, pack


def assert_visible(pack, names):
    for messages in prompts(pack):
        text = "\n".join(m["content"] for m in messages)
        for i in names:
            assert f"证人{i:02}" in text
            assert f"person-{i}" in text
            assert f"已释放{i:02}" in text


def test_more_than_ten_required_low_rank_people_survive_every_layout(
    monkeypatch, required_session
):
    from forwin.planning.contracts import PlanTaskItem

    base = base_context(
        required_session,
        chapter_task_contract=[
            PlanTaskItem(task_type="plot_advance", target_name=f"别名{i:02}")
            for i in range(10, 23)
        ],
    )
    broker, pack = build_pack(monkeypatch, required_session, base)
    assert {f"person-{i}" for i in range(10, 23)} <= {
        e.entity_id for e in pack.active_entities
    }
    assert_visible(pack, range(10, 23))
    assert pack.required_entity_ids == sorted(pack.required_entity_ids)
    assert broker.last_observability_summary["soft_budget_exceeded"] is True
    assert (
        broker.last_observability_summary["soft_budget_overflow_chars"]
        == broker._estimate_chars(pack) - 10
    )
    for messages in prompts(pack):
        assert "证人23" in messages[1]["content"]  # actual full permitted roster


def test_obligation_refs_pull_fact_relation_endpoints_and_current_state(
    monkeypatch, required_session
):
    base = base_context(
        required_session,
        active_narrative_obligations=[
            {"id": "o", "subject_refs": ["fact:proof", "field:person-23:custody"]}
        ],
    )
    _, pack = build_pack(monkeypatch, required_session, base)
    assert_visible(pack, (20, 21, 22, 23))
    for messages in prompts(pack):
        text = messages[1]["content"]
        assert "bond" in text and "已破裂" in text and "保管权已移交" in text
    assert "bond" in pack.required_relation_ids
    assert any(
        "obligation" in s for s in pack.required_selection_sources["node:person-22"]
    )


@pytest.mark.parametrize(
    "ref",
    ["node:missing", "field:person-20:missing", "edge:missing", "fact:missing", "同名"],
)
def test_genuine_missing_or_ambiguous_required_refs_fail(
    monkeypatch, required_session, ref
):
    base = base_context(
        required_session, active_narrative_obligations=[{"subject_refs": [ref]}]
    )
    with pytest.raises(ValueError, match="required_context"):
        build_pack(monkeypatch, required_session, base)


def test_scene_rehydrates_unselected_people_and_location_at_same_baseline(
    monkeypatch, required_session
):
    broker, pack = build_pack(
        monkeypatch, required_session, base_context(required_session)
    )
    assert "person-23" not in {e.entity_id for e in pack.active_entities}
    scenes = [
        ScenePlan(
            scene_no=1,
            objective="核实",
            involved_entities=["别名23"],
            location_hint="码头",
        )
    ]
    hydrated = broker.hydrate_required_context(
        SimpleNamespace(session=required_session), pack, scene_plans=scenes
    )
    assert_visible(hydrated, (23,))
    for messages in prompts(hydrated):
        assert "封闭" in messages[1]["content"]
    assert hydrated.canon_read_baseline == pack.canon_read_baseline
    assert "canon_read_baseline" not in hydrated.model_dump()


def test_reward_category_new_entry_and_descriptive_location_are_not_missing_people(
    monkeypatch, required_session
):
    from forwin.planning.contracts import PlanTaskItem
    from forwin.protocol.subworld import ChapterEntryTarget

    base = base_context(
        required_session,
        chapter_task_contract=[
            PlanTaskItem(task_type="experience_delivery", target_name="mystery")
        ],
        chapter_entry_targets=[ChapterEntryTarget(entity_name="新信使")],
    )
    broker, pack = build_pack(monkeypatch, required_session, base)
    pack = broker.hydrate_required_context(
        SimpleNamespace(session=required_session),
        pack,
        scene_plans=[
            ScenePlan(
                scene_no=1,
                objective="见面",
                involved_entities=["新信使"],
                location_hint="暮色下的一处无名街角",
            )
        ],
    )
    assert "mystery" not in pack.required_entity_ids
    assert not any(e.name == "新信使" for e in pack.active_entities)


@pytest.mark.parametrize(
    "layout", ["single", "preview", "breakdown", "scene", "stitch", "repair"]
)
def test_actual_sent_writer_requests_retain_required_identity_and_state(
    monkeypatch, required_session, layout
):
    import json
    from forwin.planning.contracts import PlanTaskItem
    from forwin.writer.chapter_writer import ChapterWriter
    from forwin.protocol.scene import SceneOutput

    base = base_context(
        required_session,
        chapter_task_contract=[
            PlanTaskItem(task_type="plot_advance", target_name=f"别名{i:02}")
            for i in range(10, 23)
        ],
    )
    broker, pack = build_pack(monkeypatch, required_session, base)
    sent = []

    def chat(messages, **kwargs):
        sent.append(messages)
        if layout == "breakdown":
            return json.dumps({"scenes": [{"scene_no": 1, "objective": "核对"}]})
        return (
            "<<FORWIN_TITLE>>\n核对\n<<FORWIN_BODY>>\n"
            + "他们逐个核实登记簿。" * 400
            + "\n<<FORWIN_SUMMARY>>\n已核对。"
        )

    writer = ChapterWriter(SimpleNamespace(chat=chat))
    if layout == "single":
        writer._write_single_chapter(pack)
    elif layout == "preview":
        writer.write_preview_chapter(pack)
    elif layout == "breakdown":
        writer._plan_scenes(pack)
    elif layout == "scene":
        writer._generate_scene(pack, ScenePlan(scene_no=1, objective="核对"))
    elif layout == "stitch":
        writer._stitch_scenes(
            pack, [SceneOutput(scene_no=1, scene_objective="核对", text="已核对。")]
        )
    else:
        repaired = broker.prepare_repair_context(
            pack,
            RepairContract(
                must_fix=["证人23也需核对。"],
                must_preserve=["保留登记。"],
                must_not_reveal=["保密标记"],
            ),
        )
        writer._write_single_chapter(repaired)
        assert "已释放23" in sent[0][1]["content"]
        assert "保密标记" in sent[0][1]["content"]
    text = "\n".join(m["content"] for m in sent[0])
    for i in range(10, 23):
        assert f"person-{i}" in text and f"已释放{i:02}" in text
    assert "已破裂" in text and "bond" in text and "证人23" in text


@pytest.mark.parametrize("reverse", [False, True])
def test_live_scene_pipeline_hydrates_after_breakdown_before_any_scene(
    monkeypatch, required_session, reverse
):
    import json
    from forwin.writer.chapter_writer import ChapterWriter
    from forwin.protocol.subworld import ChapterEntryTarget

    base = base_context(
        required_session,
        chapter_entry_targets=[ChapterEntryTarget(entity_name="新信使")],
    )
    _, pack = build_pack(monkeypatch, required_session, base)
    sent = []
    scenes = [
        {
            "scene_no": 1,
            "objective": "核对",
            "involved_entities": ["别名23", "新信使"],
            "location_hint": "码头",
        },
        {"scene_no": 2, "objective": "谈话", "involved_entities": ["证人20", "证人21"]},
    ]
    if reverse:
        scenes.reverse()

    def chat(messages, *, stage_key="", **kwargs):
        stage = stage_key
        sent.append((stage, messages))
        if stage == "scene_breakdown":
            return json.dumps({"scenes": scenes}, ensure_ascii=False)
        if "extraction" in str(stage):
            return '{"state_changes":[],"new_events":[],"delivered_payoffs":[]}'
        return (
            "<<FORWIN_TITLE>>\n核对\n<<FORWIN_BODY>>\n"
            + "他们逐个核实登记簿。" * 400
            + "\n<<FORWIN_SUMMARY>>\n已核对。"
        )

    output = ChapterWriter(SimpleNamespace(chat=chat)).write_chapter(
        pack.model_copy(deep=True)
    )
    assert output.generation_meta["mode"] == "scene"
    assert not output.generation_meta.get("fallback_from_scene")
    assert len(sent) == 7
    assert output.generation_meta["context_budget"]["soft_budget_exceeded"] is True
    assert "已释放23" not in sent[0][1][1]["content"]
    for stage, messages in sent[1:4]:
        text = "\n".join(m["content"] for m in messages)
        assert (
            "已释放23" in text
            and "已释放20" in text
            and "已破裂" in text
            and "封闭" in text
        )


@pytest.mark.parametrize("fault", ["missing", "ambiguous", "canon_change"])
def test_scene_input_failure_stops_without_single_fallback(
    monkeypatch, required_session, fault
):
    import json
    from forwin.writer.chapter_writer import ChapterWriter
    from forwin.retrieval.source_identity import CanonBaselineChanged

    _, pack = build_pack(monkeypatch, required_session, base_context(required_session))
    sent = []

    def chat(messages, **kwargs):
        sent.append(messages)
        if fault == "canon_change":
            project = required_session.get(Project, "required")
            project.book_revision += 1
            required_session.flush()
        return json.dumps(
            {
                "scenes": [
                    {
                        "scene_no": 1,
                        "objective": "核对",
                        "involved_entities": [
                            "node:missing"
                            if fault == "missing"
                            else "同名"
                            if fault == "ambiguous"
                            else "证人23"
                        ],
                    }
                ]
            }
        )

    with pytest.raises((ValueError, CanonBaselineChanged)):
        ChapterWriter(SimpleNamespace(chat=chat)).write_chapter(pack)
    assert len(sent) == 1


def test_transported_context_needs_reassembly_only_for_missing_accepted_input(
    monkeypatch, required_session
):
    from forwin.retrieval.requirements import hydrate_requirements, RequiredContextError

    broker, pack = build_pack(
        monkeypatch, required_session, base_context(required_session)
    )
    scenes = [ScenePlan(scene_no=1, objective="核对", involved_entities=["证人23"])]
    hydrated = pack.required_context_hydrator(pack, scenes)
    restored = ChapterContextPack.model_validate(hydrated.model_dump())
    assert (
        restored.canon_read_baseline is None
        and restored.required_context_hydrator is None
    )
    assert_visible(hydrate_requirements(restored, scene_plans=scenes), (23,))
    with pytest.raises(RequiredContextError):
        hydrate_requirements(
            restored,
            scene_plans=[
                ScenePlan(
                    scene_no=2, objective="核对", involved_entities=["node:person-22"]
                )
            ],
        )


@pytest.mark.parametrize("scope", ["draft", "chapter_plan", "band_plan"])
def test_repair_owner_hydrates_new_authored_targets_against_original_baseline(
    monkeypatch, required_session, scope
):
    import json
    import forwin.retrieval.broker_core.broker as module
    from forwin.models.project import ChapterPlan
    from forwin.protocol.review import RepairInstruction
    from forwin.review.repair.plan_patch import (
        RepairPlanPatchRequest,
        RepairPlanPatchService,
    )
    from forwin.state.repo import StateRepository
    from forwin.planning.contracts import load_plan_task_contract

    chapter = ChapterPlan(
        id="chapter",
        project_id="required",
        arc_plan_id="arc",
        chapter_number=2,
        title="核对",
    )
    required_session.add(chapter)
    required_session.flush()
    base = base_context(required_session)
    broker, pack = build_pack(monkeypatch, required_session, base)

    def assemble(repo, project_id, plan, *, baseline):
        return base.model_copy(
            update={
                "canon_read_baseline": baseline,
                "chapter_task_contract": load_plan_task_contract(
                    plan.task_contract_json
                ),
                "chapter_experience_plan": repo.get_chapter_experience_plan(
                    project_id, plan.chapter_number
                ),
            }
        )

    monkeypatch.setattr(module, "_assemble_context", assemble)
    from forwin.models.phase import BandExperiencePlan
    from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan

    if scope == "band_plan":
        schedule = BandDelightSchedule(band_id="band", chapter_start=1, chapter_end=3)
        required_session.add(
            BandExperiencePlan(
                id="band-row",
                project_id="required",
                arc_id="arc",
                band_id="band",
                chapter_start=1,
                chapter_end=3,
                schedule_json=schedule.model_dump_json(),
            )
        )
        required_session.flush()
    instruction = RepairInstruction(
        repair_scope=scope,
        failure_type="continuity",
        must_fix=["证人23的获释必须保留"],
        must_not_reveal=["保密标记"],
        design_patch={
            "chapter_task_contract": [
                {"task_type": "plot_advance", "target_name": "别名23"}
            ],
            "immersion_anchors": ["证人23在码头核对"],
        },
    )
    result = RepairPlanPatchService(
        retrieval_broker=broker,
        arc_envelope_manager=SimpleNamespace(
            _derive_chapter_experience_plan=lambda **kw: ChapterExperiencePlan()
        ),
    ).apply(
        RepairPlanPatchRequest(
            session=required_session,
            repo=StateRepository(required_session),
            project_id="required",
            chapter_plan=chapter,
            context=pack,
            repair_scope=scope,
            instruction=instruction,
        )
    )
    assert_visible(result.context, (23,))
    assert result.context.canon_read_baseline == pack.canon_read_baseline
    assert result.context.required_context_hydrator is not None
    assert result.context.repair_contract.must_not_reveal == ["保密标记"]
    assert not result.context.context_budget_summary == {}
    if scope == "chapter_plan":
        assert json.loads(chapter.task_contract_json)[0]["target_name"] == "别名23"


@pytest.mark.parametrize("scope", ["draft", "chapter_plan"])
def test_repair_refuses_actual_canon_change(monkeypatch, required_session, scope):
    from forwin.models.project import ChapterPlan
    from forwin.protocol.review import RepairInstruction
    from forwin.review.repair.plan_patch import (
        RepairPlanPatchRequest,
        RepairPlanPatchService,
    )
    from forwin.retrieval.source_identity import CanonBaselineChanged
    from forwin.state.repo import StateRepository

    chapter = ChapterPlan(
        id="chapter",
        project_id="required",
        arc_plan_id="arc",
        chapter_number=2,
        title="核对",
    )
    required_session.add(chapter)
    required_session.flush()
    broker, pack = build_pack(
        monkeypatch, required_session, base_context(required_session)
    )
    required_session.get(Project, "required").book_revision += 1
    required_session.flush()
    with pytest.raises(CanonBaselineChanged):
        RepairPlanPatchService(
            retrieval_broker=broker, arc_envelope_manager=SimpleNamespace()
        ).apply(
            RepairPlanPatchRequest(
                session=required_session,
                repo=StateRepository(required_session),
                project_id="required",
                chapter_plan=chapter,
                context=pack,
                repair_scope=scope,
                instruction=RepairInstruction(
                    repair_scope=scope,
                    failure_type="continuity",
                    must_fix=["证人23出场"],
                ),
            )
        )


@pytest.mark.parametrize(
    "ref", ["proof", "character:别名23", "field:person:colon:state.custody"]
)
def test_required_refs_accept_real_fact_ids_typed_names_and_colon_node_ids(
    monkeypatch, required_session, ref
):
    repo = BookStateRepository(required_session)
    repo.create_world_node(
        WorldNode(
            id="person:colon",
            project_id="required",
            node_type="character",
            name="冒号人物",
        )
    )
    repo.append_world_node_state(
        project_id="required",
        node_id="person:colon",
        node_type="character",
        as_of_chapter=0,
        state={"custody": "释放冒号"},
    )
    base = base_context(
        required_session, active_narrative_obligations=[{"subject_refs": [ref]}]
    )
    _, pack = build_pack(monkeypatch, required_session, base)
    text = prompts(pack)[0][1]["content"]
    assert {
        "proof": "保管权已移交",
        "character:别名23": "已释放23",
        "field:person:colon:state.custody": "释放冒号",
    }[ref] in text


def test_map_location_hydrates_native_site_state(monkeypatch, required_session):
    from forwin.protocol.book_state import MapNode
    from forwin.planning.constraints import NarrativeConstraintInfo

    repo = BookStateRepository(required_session)
    repo.create_map_node(
        MapNode(
            id="native-port", project_id="required", node_type="site", name="原生码头"
        )
    )
    repo.create_world_node(
        WorldNode(
            id="site-state",
            project_id="required",
            node_type="site_state",
            name="码头现况",
            profile={"map_node_id": "native-port"},
        )
    )
    repo.append_world_node_state(
        project_id="required",
        node_id="site-state",
        node_type="site_state",
        as_of_chapter=0,
        state={"access": "守卫已撤走"},
    )
    base = base_context(
        required_session,
        active_future_constraints=[
            NarrativeConstraintInfo(
                constraint_type="location_availability",
                subject_name="原生码头",
                payload={"entity_refs": ["node:person-23"], "edge_id": "bond"},
            )
        ],
    )
    _, pack = build_pack(monkeypatch, required_session, base)
    for messages in prompts(pack):
        assert "守卫已撤走" in messages[1]["content"]
        assert "已破裂" in messages[1]["content"]
        assert "已释放23" in messages[1]["content"]


def test_canon_quality_context_preserves_real_obligation_subjects(
    monkeypatch, required_session
):
    from forwin.context.assembler_core.canon_quality_context import (
        _build_canon_quality_context,
    )
    from forwin.narrative_obligations.repository import NarrativeObligationRepository
    from forwin.narrative_obligations.types import NarrativeObligation

    NarrativeObligationRepository(required_session).create_obligation(
        NarrativeObligation(
            project_id="required",
            origin_chapter_number=0,
            obligation_type="motivation_gap",
            status="active",
            summary="核对证人",
            deadline_chapter=2,
            payoff_test="给出证明",
            subject_refs=["node:person-23"],
        )
    )
    quality = _build_canon_quality_context(
        session=required_session,
        project_id="required",
        chapter_number=2,
        target_total_chapters=20,
    )
    assert quality["active_narrative_obligations"][0]["subject_refs"] == [
        "node:person-23"
    ]
    _, pack = build_pack(
        monkeypatch,
        required_session,
        base_context(required_session, canon_quality_context=quality),
    )
    assert_visible(pack, (23,))


def test_required_retired_identity_and_aliases_are_explicit(
    monkeypatch, required_session
):
    repo = BookStateRepository(required_session)
    repo.create_world_node(
        WorldNode(
            id="retired",
            project_id="required",
            node_type="character",
            name="离场人",
            aliases=["旧名"],
            status="retired",
            is_active=False,
        )
    )
    _, pack = build_pack(
        monkeypatch,
        required_session,
        base_context(
            required_session,
            active_narrative_obligations=[{"subject_refs": ["node:retired"]}],
        ),
    )
    for messages in prompts(pack):
        assert "retired" in messages[1]["content"]
        assert "旧名" in messages[1]["content"]
        assert "is_active=false" in messages[1]["content"]


def test_repeated_hydration_does_not_expand_relation_neighborhood(
    monkeypatch, required_session
):
    repo = BookStateRepository(required_session)
    for source, target in ((21, 22), (22, 23)):
        repo.create_world_edge(
            WorldEdge(
                id=f"bond-{source}-{target}",
                project_id="required",
                source_id=f"person-{source}",
                target_id=f"person-{target}",
                edge_family="social",
                edge_type="ally_of",
            )
        )
    _, pack = build_pack(
        monkeypatch,
        required_session,
        base_context(
            required_session,
            active_narrative_obligations=[{"subject_refs": ["person-20"]}],
        ),
    )
    first = set(pack.required_entity_ids)
    for _ in range(3):
        pack = pack.required_context_hydrator(pack, [])
    assert set(pack.required_entity_ids) == first == {"person-20"}


def test_scene_hydration_uses_n_minus_one_state_not_later_node_state(
    monkeypatch, required_session
):
    BookStateRepository(required_session).append_world_node_state(
        project_id="required",
        node_id="person-23",
        node_type="character",
        as_of_chapter=2,
        state={"custody": "本章未来状态"},
    )
    _, pack = build_pack(monkeypatch, required_session, base_context(required_session))
    hydrated = pack.required_context_hydrator(
        pack, [ScenePlan(scene_no=1, objective="核对", involved_entities=["别名23"])]
    )
    assert_visible(hydrated, (23,))
    assert "本章未来状态" not in str(prompts(hydrated))


def test_hydration_capability_is_not_in_transport_schema():
    for mode in ("validation", "serialization"):
        assert (
            "required_context_hydrator"
            not in ChapterContextPack.model_json_schema(mode=mode)["properties"]
        )


def test_writer_hydration_does_not_introduce_config_import_cycle():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import forwin.config; import forwin.writer; import forwin.retrieval",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_hydration_rejects_changed_chapter_on_original_read_fence(
    monkeypatch, required_session
):
    from forwin.retrieval.requirements import RequiredContextError

    _, pack = build_pack(monkeypatch, required_session, base_context(required_session))
    wrong_chapter = pack.model_copy(update={"chapter_number": 3})
    with pytest.raises(RequiredContextError, match="baseline"):
        pack.required_context_hydrator(wrong_chapter, [])


def test_required_hub_does_not_promote_unrelated_peripheral_cast(
    monkeypatch, required_session
):
    repo = BookStateRepository(required_session)
    for i in range(20):
        repo.create_world_edge(
            WorldEdge(
                id=f"hub-{i}",
                project_id="required",
                source_id="person-20",
                target_id=f"person-{i}",
                edge_family="social",
                edge_type="ally_of",
            )
        )
    _, pack = build_pack(
        monkeypatch,
        required_session,
        base_context(
            required_session,
            active_narrative_obligations=[{"subject_refs": ["person-20", "person-21"]}],
        ),
    )
    assert set(pack.required_entity_ids) == {"person-20", "person-21"}
    assert pack.required_relation_ids == ["bond"]
    assert set(e.entity_id for e in pack.active_entities) == {"person-20", "person-21"}


def test_canonical_id_wins_over_alias_collision_and_prefix(
    monkeypatch, required_session
):
    from forwin.planning.contracts import PlanTaskItem

    repo = BookStateRepository(required_session)
    repo.create_world_node(
        WorldNode(
            id="character:identity",
            project_id="required",
            node_type="character",
            name="真名",
        )
    )
    repo.create_world_node(
        WorldNode(
            id="imposter",
            project_id="required",
            node_type="character",
            name="另一个人",
            aliases=["character:identity"],
        )
    )
    base = base_context(
        required_session,
        chapter_task_contract=[
            PlanTaskItem(task_type="plot_advance", target_name="character:identity")
        ],
    )
    _, pack = build_pack(monkeypatch, required_session, base)
    assert pack.required_entity_ids == ["character:identity"]
    assert (
        BookStateQuery(required_session)
        .entities_by_names("required", ["character:identity"], as_of_chapter=1)[
            "character:identity"
        ]
        .entity_id
        == "character:identity"
    )


def test_query_rejects_ambiguous_alias_instead_of_last_row_wins(required_session):
    with pytest.raises(ValueError, match="ambiguous"):
        BookStateQuery(required_session).entities_by_names(
            "required", ["同名"], as_of_chapter=1
        )


def test_hydration_preserves_accepted_cognition_author_intent_and_repair(
    monkeypatch, required_session
):
    from forwin.protocol.book_state import CognitionOverlay
    from forwin.planning.world_contracts import ChapterWorldDeltaIntent

    BookStateRepository(required_session).upsert_cognition_overlay(
        CognitionOverlay(
            id="cognition",
            project_id="required",
            observer_type="character",
            observer_id="person-23",
            as_of_chapter=0,
            visible_refs=["node:person-20"],
            hidden_refs=["field:person-20:custody"],
        )
    )
    snapshots = BookStateQuery(required_session).accepted_cognition(
        "required", as_of_chapter=1
    )
    base = base_context(
        required_session,
        accepted_cognition=snapshots,
        chapter_world_delta_intent=ChapterWorldDeltaIntent(
            intent_id="planned",
            project_id="required",
            chapter_number=2,
            expected_observer_state_changes={"person-23": "稍后才知道获释"},
        ),
    )
    broker, pack = build_pack(monkeypatch, required_session, base)
    contract = RepairContract(
        must_fix=["证人23只在获知后行动"], must_not_reveal=["保密标记"]
    )
    repaired = broker.prepare_repair_context(pack, contract)
    hydrated = repaired.required_context_hydrator(
        repaired.model_copy(deep=True),
        [ScenePlan(scene_no=1, objective="核对", involved_entities=["证人23"])],
    )
    assert hydrated.accepted_cognition == snapshots
    assert (
        hydrated.chapter_world_delta_intent.expected_observer_state_changes
        == base.chapter_world_delta_intent.expected_observer_state_changes
    )
    assert hydrated.repair_contract == contract
    for messages in prompts(hydrated):
        text = messages[1]["content"]
        assert "稍后才知道获释" in text and "保密标记" in text
        accepted = text.split("【已接纳认知（N−1）】")[1].split(
            "【作者计划（尚未发生）】"
        )[0]
        assert "稍后才知道获释" not in accepted
        assert "hidden" in accepted
