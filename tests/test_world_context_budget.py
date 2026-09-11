"""World pages consume budget only for the representation the Writer sees."""

from __future__ import annotations

import pytest

from forwin.obsidian.frontmatter import render_page
from forwin.protocol.context import ChapterContextPack, MemorySnippet
from forwin.protocol.scene import ScenePlan
from forwin.protocol.world_model import (
    WorldContextPack,
    WorldModelConflict,
    WorldModelPage,
)
from forwin.retrieval.broker_core.broker import RetrievalBroker
from forwin.writer.prompt_core.builders import (
    build_preview_chapter_prompt,
    build_scene_breakdown_prompt,
    build_scene_generation_prompt,
    build_scene_stitch_prompt,
    build_single_chapter_draft_prompt,
)


def page(extra: str = "", *, hidden: bool = False) -> WorldModelPage:
    metadata = {
        "node_id": "captain",
        "visibility": "hidden" if hidden else "reader_known",
        "truth_relation": "true",
    }
    return WorldModelPage(
        page_key="captain",
        page_type="character",
        title="船长",
        frontmatter=metadata,
        markdown=render_page(
            metadata,
            "船长",
            {
                "Canon Summary": "船長保管钥匙。",
                "Current State": "钥匙封在布袋里。",
                "Manual Notes": extra,
            },
        ),
    )


def context(world: WorldContextPack) -> ChapterContextPack:
    return ChapterContextPack(
        project_title="归航",
        premise="核对航行记录",
        genre="冒险",
        setting_summary="港口",
        chapter_number=8,
        chapter_plan_title="核查",
        chapter_plan_one_line="核实保管过程",
        chapter_goals=["找到凭据"],
        previous_chapter_summaries=["船长接收钥匙。", "钥匙封入布袋。"],
        retrieved_memories=[
            MemorySnippet(
                chapter_number=2,
                title="交接",
                summary="保管权已转移。",
                excerpt="原文证据",
            )
        ],
        world_context=world,
    )


def prompts(pack: ChapterContextPack) -> list[list[dict]]:
    return [
        build_single_chapter_draft_prompt(pack),
        build_preview_chapter_prompt(pack),
        build_scene_breakdown_prompt(pack),
        build_scene_generation_prompt(pack, ScenePlan(scene_no=1, objective="核查")),
        build_scene_stitch_prompt(pack, []),
    ]


@pytest.mark.parametrize(
    "inflation",
    [
        "manual_notes",
        "metadata",
        "specialized_copies",
        "hidden_page",
        "secret_content",
        "unused_without_snapshot",
    ],
)
def test_same_writer_world_input_has_same_budget_and_history(inflation):
    original_page = page()
    world = WorldContextPack(
        snapshot_id="snapshot7", as_of_chapter=7, relevant_world_pages=[original_page]
    )
    changed = world.model_copy(deep=True)
    if inflation == "manual_notes":
        changed.relevant_world_pages = [page("ignored" * 10000)]
    elif inflation == "metadata":
        changed.world_model_digest = "digest" * 10000
        changed.world_model_refs = {"unused": "ref" * 10000}
        changed.relevant_world_pages[0].frontmatter["audit"] = "metadata" * 10000
    elif inflation == "specialized_copies":
        changed.active_resource_constraints = [page("not rendered" * 10000)]
        changed.active_institution_rules = [page("not rendered" * 10000)]
    elif inflation == "hidden_page":
        changed.relevant_world_pages.append(page("hidden" * 10000, hidden=True))
    elif inflation == "secret_content":
        world.active_secrets = [page(hidden=True)]
        changed.active_secrets = [page("hidden" * 10000, hidden=True)]
    else:
        world.snapshot_id = changed.snapshot_id = ""
        changed.relevant_world_pages = [page("invisible without snapshot" * 10000)]
    plain, inflated = context(world), context(changed)
    original = inflated.model_dump(mode="json")
    assert prompts(plain) == prompts(inflated)
    broker = RetrievalBroker(
        context_budget_chars=RetrievalBroker._estimate_pack_with_components(plain)
    )
    assert broker._estimate_chars(plain) == broker._estimate_chars(inflated)
    assert broker._estimate_pack_with_components(
        plain
    ) == broker._estimate_pack_with_components(inflated)
    a, b = broker._trim_pack(plain), broker._trim_pack(inflated)
    assert (
        a.previous_chapter_summaries
        == b.previous_chapter_summaries
        == plain.previous_chapter_summaries
    )
    assert a.retrieved_memories == b.retrieved_memories == plain.retrieved_memories
    assert prompts(a) == prompts(b)
    assert inflated.model_dump(mode="json") == original


@pytest.mark.parametrize(
    "visible_change", ["summary", "conflict", "promise", "secret_notice"]
)
def test_visible_world_input_is_still_charged(visible_change):
    a = context(
        WorldContextPack(
            snapshot_id="snapshot7", as_of_chapter=7, relevant_world_pages=[page()]
        )
    )
    b = a.model_copy(deep=True)
    if visible_change == "summary":
        b.world_context.relevant_world_pages[0].markdown = render_page(
            b.world_context.relevant_world_pages[0].frontmatter,
            "船长",
            {
                "Canon Summary": "船长昨日在公证人面前接收了一把铜钥匙，并把它封进存证布袋。"
                * 3,
                "Current State": "布袋存于库房。",
            },
        )
    elif visible_change == "conflict":
        b.world_context.active_world_conflicts = [
            WorldModelConflict(
                conflict_type="custody", description="持有人存在矛盾，不能静默忽略。"
            )
        ]
    elif visible_change == "promise":
        b.world_context.active_promises = [
            page().model_copy(update={"title": "必须解释保管权如何转移"})
        ]
    else:
        b.world_context.active_secrets = [page(hidden=True)]
    assert prompts(a) != prompts(b)
    assert RetrievalBroker._estimate_chars(b) > RetrievalBroker._estimate_chars(a)
    assert RetrievalBroker._estimate_pack_with_components(
        b
    ) > RetrievalBroker._estimate_pack_with_components(a)


def test_page_pruning_releases_only_rendered_page_cost():
    first = page()
    second = page().model_copy(
        update={
            "page_key": "guard",
            "title": "守卫",
            "markdown": page().markdown.replace(
                "船長保管钥匙。", "守卫持有另一枚证物，必须核对真实去向。" * 10
            ),
        }
    )
    pack = context(
        WorldContextPack(
            snapshot_id="snapshot7",
            relevant_world_pages=[first, second],
            active_resource_constraints=[second],
        )
    )
    pack.previous_chapter_summaries = [pack.previous_chapter_summaries[-1]]
    pack.retrieved_memories = []
    smaller = pack.model_copy(
        update={
            "world_context": pack.world_context.model_copy(
                update={"relevant_world_pages": [first]}
            )
        }
    )
    broker = RetrievalBroker(
        context_budget_chars=RetrievalBroker._estimate_pack_with_components(smaller)
    )
    result = broker._trim_pack(pack)
    assert result.world_context.relevant_world_pages == [first]
    assert result.world_context.active_resource_constraints == [second]
    assert broker._estimate_pack_with_components(result) <= broker.context_budget_chars
    assert pack.world_context.relevant_world_pages == [first, second]


def test_empty_world_keeps_existing_component_overhead():
    pack = context(WorldContextPack()).model_copy(
        update={
            "previous_chapter_summaries": [],
            "retrieved_memories": [],
        }
    )
    # The original component estimator has a fixed empty-world allowance in
    # addition to the base pack. This change must not alter no-world retention.
    expected = RetrievalBroker._estimate_chars(
        pack
    ) + RetrievalBroker._estimate_component_chars(WorldContextPack())
    assert RetrievalBroker._estimate_pack_with_components(pack) == expected
