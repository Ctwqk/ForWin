from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.book_state.knowledge import HIDDEN_SENSITIVITY
from forwin.protocol.book_state import FactNode
from forwin.protocol.context import ChapterContextPack, EntitySnapshot, TimelineSnapshot
from forwin.protocol.scene import SceneOutput, ScenePlan
from forwin.protocol.world_model import WorldContextPack, WorldModelPage
from forwin.world_view.types import ChapterWorldView, OnstageEntity, ViewEntity, ViewLocation
from forwin.writer.prompt_core import (
    build_preview_chapter_prompt,
    build_scene_breakdown_prompt,
    build_scene_generation_prompt,
    build_scene_stitch_prompt,
    build_single_chapter_draft_prompt,
)
from forwin.writer.prompt_core.planning import (
    ARC_BEATS_INSTRUCTIONS,
    BAND_PLAN_INSTRUCTIONS,
    build_arc_beats_messages,
    build_band_plan_messages,
)
from forwin.writer.prompt_core.world_view import (
    LOCATION_TABLE_HEADING,
    SCENE_LOCATION_INSTRUCTION,
    UNNAMED_ACTOR_LABEL,
    _world_view_section,
    forbidden_topic_text,
    render_world_view,
)
from tests.test_chapter_world_view import (  # noqa: F401  (source is a fixture)
    FACTS,
    POV_KNOWLEDGE,
    READER_KNOWLEDGE,
    REVEALS,
    SCHEDULE,
    _build,
    _plan,
    source,
)


BUILDERS = ("scene_breakdown", "scene_generation", "scene_stitch", "single", "preview")
# Headings of the sections the view replaces; none may come back in any builder.
REMOVED_HEADINGS = (
    "【世界状态意图】", "【当前 Arc Envelope】", "【WorldModel 当前世界状态】", "【地图运行时】", "【当前时间】",
    "【世界压力】",
)


def _context(view: ChapterWorldView | None, **fields) -> ChapterContextPack:
    payload = {
        "project_id": "p", "project_title": "title", "premise": "premise", "genre": "genre",
        "setting_summary": "setting", "chapter_number": 6, "chapter_plan_title": "plan-title",
        "chapter_plan_one_line": "plan-line", "chapter_goals": ["plan-goal"], "world_view": view,
    }
    return ChapterContextPack(**{**payload, **fields})


def _prompt(builder: str, context: ChapterContextPack) -> str:
    if builder == "scene_breakdown":
        messages = build_scene_breakdown_prompt(context)
    elif builder == "scene_generation":
        messages = build_scene_generation_prompt(context, ScenePlan(scene_no=1, objective="scene-objective"))
    elif builder == "scene_stitch":
        messages = build_scene_stitch_prompt(
            context, [SceneOutput(scene_no=1, scene_objective="scene-objective", text="scene-text")],
        )
    elif builder == "single":
        messages = build_single_chapter_draft_prompt(context)
    else:
        messages = build_preview_chapter_prompt(context)
    return "\n".join(str(message["content"]) for message in messages)


def _old_surfaces(*truths: str) -> dict:
    """Context fields the removed sections rendered, each carrying a secret's truth."""
    first, *rest = truths
    return {
        "active_entities": [EntitySnapshot(entity_id="villain", kind="character", name="villain-name",
                                           description=first, current_state={})],
        "timeline": TimelineSnapshot(current_time_label=truths[-1], ordinal=1),
        "map_context": {"map_node_count": 3, "map_edge_count": 2, "active_locations": [
            {"entity_name": "villain-name", "location_name": truths[-1], "nearby_nodes": []},
        ]},
        "world_context": WorldContextPack(
            snapshot_id="snap", as_of_chapter=5,
            relevant_world_pages=[WorldModelPage(page_key="k", title="page", page_type="secret",
                                                 markdown=" ".join(rest) or first)],
            active_secrets=[WorldModelPage(page_key="s", title="secret-page", markdown=first)],
        ),
    }


@pytest.mark.parametrize("builder", BUILDERS)
def test_no_builder_leaks_unpermitted_secret_truth(source, builder):
    source.runtime.world.add_fact(FactNode(
        id="pov-learns", project_id="p", proposition="pov-learns-truth", sensitivity_level="secret",
        related_node_refs=["ally"],
    ))
    reveals = [*REVEALS, {"id": "d-pov-learns", "fact_ref": "fact:pov-learns", "observer": "hero",
                          "target_state": "knows"}]
    view = _build(source, plan=_plan(reveal_directives_json=json.dumps(reveals)))
    secrets = {
        ref for ref, (_, sensitivity, truth, _, _) in FACTS.items()
        if truth == "true" and (sensitivity in HIDDEN_SENSITIVITY or ref in SCHEDULE)
    }
    held = {ref for ref, state in POV_KNOWLEDGE.items() if state != "disbelieves"}
    reader_known = {ref for ref, state in READER_KNOWLEDGE.items() if state in {"knows", "believes"}}
    permitted = {"revealed", "pov-revealed", "actor-revealed"}
    # Truths the POV does not hold, the reader may not learn and no directive permits; the POV
    # learning a secret is narrated as perception only, without its truth.
    unpermitted = [FACTS[ref][0] for ref in sorted(secrets - held - reader_known - permitted)]
    assert unpermitted == ["due-truth", "forbidden-truth", "nameless-truth", "hinted-truth", "pov-disbelieved-truth"]
    unpermitted.append("pov-learns-truth")

    text = _prompt(builder, _context(view, **_old_surfaces(*unpermitted)))

    for truth in unpermitted:
        assert truth not in text, truth
    # The topic stands in for the secret it names; the permitted truths are there.
    assert forbidden_topic_text(["ally-name", "villain-name"]) in text
    assert forbidden_topic_text(["ally-name"]) in text
    for truth in ("truth-of-revealed", "truth-of-pov-revealed", "truth-of-actor-revealed", "pov-secret-truth"):
        assert truth in text, truth


@pytest.mark.parametrize("builder", BUILDERS)
def test_removed_world_sections_absent_from_all_builders(source, builder):
    text = _prompt(builder, _context(_build(source), **_old_surfaces("surface-text")))

    assert "【POV 世界视图】" in text
    for heading in REMOVED_HEADINGS:
        assert heading not in text, heading
    assert "surface-text" not in text


def test_world_view_section_renders_reveals_by_observer(source):
    source.runtime.world.add_fact(FactNode(
        id="pov-learns", project_id="p", proposition="pov-learns-truth", sensitivity_level="secret",
        related_node_refs=["ally"],
    ))
    reveals = [*REVEALS, {"id": "d-pov-learns", "fact_ref": "fact:pov-learns", "observer": "hero",
                          "target_state": "suspects"}]
    view = _build(source, plan=_plan(reveal_directives_json=json.dumps(reveals)))
    lines = render_world_view(view).splitlines()

    def line(*texts: str) -> str:
        return next(item for item in lines if all(text in item for text in texts))

    # The reader may come to know a stated truth; a suspicion is only hinted at.
    assert line("truth-of-revealed").endswith("可以让读者确知：truth-of-revealed")
    assert line("可以让读者", "truth-of-pov-revealed") == (
        "    · 可以让读者怀疑，只暗示不要明说：truth-of-pov-revealed"
    )
    # An actor learns it; the reader is not told.
    assert line("truth-of-actor-revealed") == (
        "    · ally-name本章确知：truth-of-actor-revealed（本章不得向读者明示）"
    )
    # The POV learning a secret is perception only: its topic, never its truth.
    pov_line = line("hero-name本章怀疑")
    assert forbidden_topic_text(["ally-name"]) in pov_line and "不要向读者说出真相内容" in pov_line
    # POV-only secrets, false beliefs and insider clocks carry their markers.
    assert line("pov-secret-truth").endswith("（POV 知道；本章不得向读者明示）")
    assert line("pov-lie-text") == "    · POV 以为（实为误信）：pov-lie-text"
    assert line("insider-clock-label").endswith("（POV 知道；本章不得向读者明示）")
    assert "（POV 知道" not in line("public-clock-label")
    # Reader knowledge without its fact refs; hints and topics under their own headings.
    assert "    · reader-place-text" in lines
    assert "    · hint-value" in lines
    assert any("禁止定论的话题" in item for item in lines)
    assert _world_view_section(_context(view)) == render_world_view(view)
    assert _world_view_section(_context(None)) is None


def test_scene_location_marker_lists_view_location_ids(source):
    view = _build(source, budget=0)
    # Trimming kept only the chapter's location; the POV's own location still heads the table.
    assert [location.id for location in view.locations] == ["chapter-place"]
    text = _prompt("scene_generation", _context(view))

    assert f"<<FORWIN_LOCATION>>\n{SCENE_LOCATION_INSTRUCTION}\n" in text
    assert SCENE_LOCATION_INSTRUCTION == "填地点表中的 id，新地点写 new:<名称>"
    table = text[text.index(LOCATION_TABLE_HEADING):].splitlines()
    assert table[0] == f"{LOCATION_TABLE_HEADING}："
    assert table[1] == "    · home：home-name（POV 所在）"
    assert table[2] == "    · chapter-place：chapter-place-name（POV 约 360 分钟可达）"
    full = _prompt("scene_generation", _context(_build(source)))
    assert "    · n0：n0-name（POV 约 30 分钟可达）" in full
    assert "沿用当前地点\n<<FORWIN_ENTITIES>>" not in text
    # Only the scene prompt has a <<FORWIN_LOCATION>> output; the others show a neutral table.
    for builder in BUILDERS:
        prompt = _prompt(builder, _context(view))
        assert LOCATION_TABLE_HEADING in prompt
        assert prompt.count(SCENE_LOCATION_INSTRUCTION) == (builder == "scene_generation")


def test_single_prompt_renders_canon_quality_once():
    context = _context(None, canon_quality_context={"is_final_chapter": True})
    text = _prompt("single", context)

    assert text.count("【Canon 质量连续性约束】\n") == 1
    assert text.index("【Canon 质量连续性约束】\n") < text.index("【本章计划】")


def test_named_entities_come_from_the_view(source):
    view = ChapterWorldView(
        pov_ref="hero", pov_name="hero-name",
        onstage=[OnstageEntity(ref="hero", name="hero-name"), OnstageEntity(ref="ally", name="ally-name")],
        known_entities=[ViewEntity(ref=f"k{index}", name=f"known-{index}", aliases=[f"alias-{index}"])
                        for index in range(8)],
        locations=[ViewLocation(id="home", name="home-name", minutes_from_pov=0)],
    )
    hidden = EntitySnapshot(entity_id="villain", kind="character", name="villain-name",
                            description="villain-description", current_state={})
    text = _prompt("single", _context(view, active_entities=[hidden], allowed_entities=["villain-name"]))

    section = text[text.index("【当前允许使用的专名】"):].split("\n\n", 1)[0].splitlines()
    assert section[1:4] == ["  · hero-name（在场）", "  · ally-name（在场）", "  · known-0（别名：alias-0）"]
    # The single prompt lists six of the POV's known entities after the onstage ones.
    assert len(section) == 1 + 2 + 6
    assert "命名人物只能使用本章出场名单与【当前允许使用的专名】里的名字" in text
    admission = text[text.index("【人物准入规则】"):].split("\n\n", 1)[0]
    assert "本章出场名单：hero-name、ally-name" in admission
    assert "villain" not in text
    assert _prompt("single", _context(None, active_entities=[hidden])).count("villain") == 0


def test_concealed_and_unnamed_actors_are_never_named(source, caplog):
    from forwin.protocol.book_state import WorldNode

    world = source.runtime.world
    for ref, identity in (("hooded", "hooded-identity"), ("shade", "")):
        world.add_node(WorldNode(id=ref, project_id="p", node_type="character", name=f"{ref}-name",
                                 profile={"public_identity": identity}, state={"visibility_level": "secret"}))
    world.add_node(WorldNode(id="guild", project_id="p", node_type="institution", name="guild-name"))
    reveals = [
        {"id": "d-hooded", "fact_ref": "fact:actor-revealed", "observer": "hooded", "target_state": "knows"},
        {"id": "d-guild", "fact_ref": "fact:revealed", "observer": "guild", "target_state": "knows"},
        {"id": "d-pov-bare", "fact_ref": "fact:pov-revealed", "observer": "hero", "target_state": "knows"},
    ]
    view = _build(source, plan=_plan(onstage_refs_json=json.dumps(["hero", "ally", "hooded", "shade"]),
                                     reveal_directives_json=json.dumps(reveals)))
    assert [(entity.ref, entity.name, entity.concealed) for entity in view.onstage] == [
        ("hero", "hero-name", False), ("ally", "ally-name", False), ("hooded", "", True), ("shade", "", True),
    ]
    # A POV reveal whose fact has no nameable subject is logged: its row cannot say what it concerns.
    assert any("d-pov-bare" in record.message and "nameable" in record.message for record in caplog.records)

    for builder in BUILDERS:
        text = _prompt(builder, _context(view))
        for name in ("hooded-name", "shade-name", "guild"):
            assert name not in text, (builder, name)
    lines = render_world_view(view).splitlines()
    # Onstage by public identity, or the neutral label; reveal observers by the label, never an id.
    assert "    · hooded-identity" in lines and f"    · {UNNAMED_ACTOR_LABEL}" in lines
    assert f"    · {UNNAMED_ACTOR_LABEL}本章确知：truth-of-actor-revealed（本章不得向读者明示）" in lines
    assert f"    · {UNNAMED_ACTOR_LABEL}本章确知：truth-of-revealed（本章不得向读者明示）" in lines
    text = _prompt("single", _context(view))
    assert "本章出场名单：hero-name、ally-name\n" in text
    named = text[text.index("【当前允许使用的专名】"):].split("\n\n", 1)[0]
    assert "hooded-identity" not in named and UNNAMED_ACTOR_LABEL not in named
    # A POV without a name is the label too.
    assert f"  · 视角人物：{UNNAMED_ACTOR_LABEL}（按此人的视角叙述）" in render_world_view(
        ChapterWorldView(pov_ref="hero")).splitlines()


def test_planner_prompts_mark_the_fields_that_reach_the_writer():
    """The beat-sheet and band-plan LLMs see secrets; the fields the writer reads verbatim say so."""

    def writer_facing(instructions, *fields):
        matches = [item for item in instructions if "原样交给写作者" in item and all(f in item for f in fields)]
        assert len(matches) == 1
        assert "秘密" in matches[0]
        return matches[0]

    arguments = {"payload": {}, "digest_text": "digest", "output_schema": {}, "allowed": {}}
    # Milestone descriptions become the experience plan's curiosity-beat questions.
    arc = writer_facing(ARC_BEATS_INSTRUCTIONS, "purpose", "milestones 的 description", "reader_promise",
                        "arc_payoff_map", "ambiguity_constraints")
    assert "revelation_layers.summary 仅是作者侧规划笔记，永不交给写作者" in arc
    assert "chapter_window 用章号或起止章号范围，仅用于规划揭示节奏" in arc
    assert "规划者保留完整 arc_payoff_map" in arc
    assert "本章揭示只通过世界视图中的本章 reveal_directives 交给写作者" in arc
    assert "purpose 与 milestones 的 description 按卷起始章的读者知情权限填写" in arc
    assert "band 的每一章" in arc
    assert "本阶段起始章或更早的 reveal_targets" in arc
    band = writer_facing(BAND_PLAN_INSTRUCTIONS, "title", "one_line", "goals", "hook", "hint_text")
    assert "本章 reveal_directives" in band
    for instruction in (arc, band):
        assert '"concealed": true' in instruction
        assert "所有面向写作者的文本" in instruction
    assert len(ARC_BEATS_INSTRUCTIONS) == 8
    assert len(BAND_PLAN_INSTRUCTIONS) == 9
    binding_instruction = next(item for item in BAND_PLAN_INSTRUCTIONS if "world_bindings 写" in item)
    assert "每项世界绑定必须由本章面向写作者的某条 goals 承接" in binding_instruction
    assert binding_instruction in build_band_plan_messages(**arguments)[0]["content"]
    for messages, instruction in ((build_arc_beats_messages(**arguments), arc),
                                  (build_band_plan_messages(**arguments), band)):
        system = next(message["content"] for message in messages if message["role"] == "system")
        assert instruction in system


def test_personality_section_only_onstage(monkeypatch):
    from forwin.context.providers.personality_provider import PersonalityContextProvider
    from forwin.context.request import ContextDraft, ContextRequest
    from forwin.retrieval.broker_core import RetrievalBroker

    loadout = {"dominant": {"skill": "trait-loyal-protector", "weight": 0.72}, "secondary": [], "social_mask": [],
               "stress_modes": [], "relationship_patterns": [], "overrides": {}}
    characters = [
        {"character_id": ref, "character_name": f"{ref}-name", "personality_loadout": loadout}
        for ref in ("hero", "ally", "villain")
    ]
    draft = ContextDraft(data={
        "allowed_entities": ["hero-name", "ally-name", "villain-name"],
        "book_state_overlay": {"personality_characters": characters},
        # The concealed villain is onstage but its context would name it.
        "world_view": ChapterWorldView(onstage=[OnstageEntity(ref="ally", name="ally-name"),
                                                OnstageEntity(ref="hero", name="hero-name"),
                                                OnstageEntity(ref="villain", concealed=True)]),
    })
    request = ContextRequest(project_id="p", chapter_plan=None, repo=None)

    PersonalityContextProvider().contribute(request, draft)

    contexts = draft.data["active_personality_contexts"]
    assert [item["character_id"] for item in contexts] == ["hero", "ally"]
    # Without a view no character is onstage.
    draft.data["world_view"] = None
    PersonalityContextProvider().contribute(request, draft)
    assert draft.data["active_personality_contexts"] == []
    # Retrieval preserves the provider's onstage-only personality context.
    pack = _context(None, active_personality_contexts=contexts)
    monkeypatch.setattr(
        "forwin.retrieval.broker_core.broker._assemble_context",
        lambda *_args: pack,
    )
    memory_index = SimpleNamespace(search=lambda **_kwargs: [])
    selected = RetrievalBroker(memory_index=memory_index).build_chapter_context(None, "p", None)
    assert selected.active_personality_contexts == contexts


def test_world_view_is_outside_the_broker_context_budget(source):
    from forwin.retrieval.broker_core import RetrievalBroker

    bare = _context(None)
    with_view = _context(_build(source))

    assert len(render_world_view(with_view.world_view)) > 500
    assert (RetrievalBroker._estimate_pack_with_components(with_view)
            == RetrievalBroker._estimate_pack_with_components(bare))
