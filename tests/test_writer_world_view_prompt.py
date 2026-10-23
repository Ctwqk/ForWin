from __future__ import annotations

import json

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
from forwin.writer.prompt_core.world_view import (
    SCENE_LOCATION_INSTRUCTION,
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
        "active_knowledge_gaps": list(truths),
        "visible_world_lines": list(truths),
        "hidden_world_lines": list(truths),
        "must_not_reveal": list(truths),
        "current_arc_envelope": {"source_policy_tier": "tier", "resolved_target_size": 30},
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
        "    · ally-name本章确知：truth-of-actor-revealed；本章不得向读者明示"
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
    table = text[text.index("地点表"):].splitlines()
    assert table[1] == "    · home：home-name（POV 所在）"
    assert table[2] == "    · chapter-place：chapter-place-name（POV 约 360 分钟可达）"
    full = _prompt("scene_generation", _context(_build(source)))
    assert "    · n0：n0-name（POV 约 30 分钟可达）" in full
    assert "沿用当前地点\n<<FORWIN_ENTITIES>>" not in text


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

    section = text[text.index("【当前允许命名角色】"):].split("\n\n", 1)[0].splitlines()
    assert section[1:4] == ["  · hero-name（在场）", "  · ally-name（在场）", "  · known-0（别名：alias-0）"]
    # The single prompt lists six of the POV's known entities after the onstage ones.
    assert len(section) == 1 + 2 + 6
    admission = text[text.index("【人物准入规则】"):].split("\n\n", 1)[0]
    assert "本章出场名单：hero-name、ally-name" in admission
    assert "villain" not in text
    assert _prompt("single", _context(None, active_entities=[hidden])).count("villain") == 0


def test_personality_section_only_onstage():
    from forwin.context.providers.personality_provider import PersonalityContextProvider
    from forwin.context.request import ContextDraft, ContextRequest
    from forwin.retrieval.broker_core import RetrievalBroker
    from forwin.protocol.context import WorldModelRetrievalPack

    loadout = {"dominant": {"skill": "trait-loyal-protector", "weight": 0.72}, "secondary": [], "social_mask": [],
               "stress_modes": [], "relationship_patterns": [], "overrides": {}}
    characters = [
        {"character_id": ref, "character_name": f"{ref}-name", "personality_loadout": loadout}
        for ref in ("hero", "ally", "villain")
    ]
    draft = ContextDraft(data={
        "allowed_entities": ["hero-name", "ally-name", "villain-name"],
        "book_state_overlay": {"personality_characters": characters},
        "world_view": ChapterWorldView(onstage=[OnstageEntity(ref="ally", name="ally-name"),
                                                OnstageEntity(ref="hero", name="hero-name")]),
    })
    request = ContextRequest(project_id="p", chapter_plan=None, repo=None)

    PersonalityContextProvider().contribute(request, draft)

    contexts = draft.data["active_personality_contexts"]
    assert [item["character_id"] for item in contexts] == ["hero", "ally"]
    # Without a view no character is onstage.
    draft.data["world_view"] = None
    PersonalityContextProvider().contribute(request, draft)
    assert draft.data["active_personality_contexts"] == []
    # The broker's world-model pack no longer replaces them with every character.
    pack = _context(None, active_personality_contexts=contexts)
    world_pack = WorldModelRetrievalPack(pack_kind="writing", project_id="p", active_personality_contexts=[
        {"character_id": "villain", "character_name": "villain-name"},
    ])
    merged = RetrievalBroker()._merge_writer_world_model_pack(pack, world_pack)
    assert merged.active_personality_contexts == contexts


def test_world_view_is_outside_the_broker_context_budget(source):
    from forwin.retrieval.broker_core import RetrievalBroker

    bare = _context(None)
    with_view = _context(_build(source))

    assert len(render_world_view(with_view.world_view)) > 500
    assert (RetrievalBroker._estimate_pack_with_components(with_view)
            == RetrievalBroker._estimate_pack_with_components(bare))
