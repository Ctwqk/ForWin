"""Regression for locked reference facts lost before Writer and BODY review."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.context.assembler_core.assembler import ChapterContextAssembler
from forwin.context.providers.genesis_provider import GenesisContextProvider
from forwin.context.request import ContextDraft, ContextRequest
from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.context import AcceptedCognitionSnapshot, EntitySnapshot, WritingPack
from forwin.protocol.scene import ScenePlan
from forwin.protocol.writer import WriterOutput
from forwin.retrieval.broker_core.broker import RetrievalBroker
from forwin.retrieval.broker_core.helpers import _budget_genesis_references
from forwin.review.context_builder import build_review_context_pack
from forwin.review.llm_webnovel import LLMWebNovelReviewer
from forwin.review.webnovel import WebNovelExperienceReviewer
from forwin.writer.prompt_core import (
    build_preview_chapter_prompt,
    build_scene_breakdown_prompt,
    build_scene_generation_prompt,
    build_scene_stitch_prompt,
    build_single_chapter_draft_prompt,
)


def _context(world=None, *, raw_pack=None):
    if world is None:
        world = {
            "world_bible": {
                "overview": "钟楼失火后的山城。",
                "history_slice": "七年前钟楼失火；去年钟楼重建。",
                "axioms": ["信件跨城必须经过驿站。"],
                "culture_profiles": [{"unrelated_library": "无需注入的整套方言词库"}],
            },
            "story_engine": {
                "long_arcs": ["最终查清钟楼案。"],
                "core_cast": [
                    {"name": "温禾", "secret": "七年前她烧毁了原账；去年她补写的账是另一份文件。", "current_base": "写前驻地"},
                    {"name": "宋安", "secret": "他宣称亲眼见过火灾，其实当时人在外城。"},
                ],
                "template_library": {"future": "不应当成为已发生历史的将来场景"},
            },
        }
    revision = SimpleNamespace(
        id="locked-revision-9", revision=9,
        pack_json=raw_pack if raw_pack is not None else json.dumps({"world": world}, ensure_ascii=False),
    )
    repo = SimpleNamespace(get_active_genesis_revision=lambda project_id: revision)
    plan = SimpleNamespace(chapter_number=1, title="温禾调档", one_line="核对账册", goals_json='[]')
    draft = ContextDraft(data={"project": SimpleNamespace(
        title="钟楼", premise="查明失火真相", genre="悬疑", setting_summary="山城",
    ), "goals": []})
    GenesisContextProvider().contribute(ContextRequest(project_id="p", chapter_plan=plan, repo=repo), draft)
    return ChapterContextAssembler()._build_pack(project_id="p", chapter_plan=plan, draft=draft)


@pytest.mark.parametrize("stage", ["single", "preview", "breakdown", "scene", "stitch"])
def test_locked_history_and_secrets_reach_every_writing_message(stage):
    context = RetrievalBroker()._trim_pack(_context())
    builders = {
        "single": lambda: build_single_chapter_draft_prompt(context),
        "preview": lambda: build_preview_chapter_prompt(context),
        "breakdown": lambda: build_scene_breakdown_prompt(context),
        "scene": lambda: build_scene_generation_prompt(context, ScenePlan(scene_no=1, objective="核对账册")),
        "stitch": lambda: build_scene_stitch_prompt(context, []),
    }
    text = "\n".join(message["content"] for message in builders[stage]())
    assert "七年前钟楼失火；去年钟楼重建。" in text
    assert "七年前她烧毁了原账；去年她补写的账是另一份文件。" in text
    assert "他宣称亲眼见过火灾，其实当时人在外城。" in text
    assert "信件跨城必须经过驿站。" in text
    assert "locked-revision-9" in text
    assert "world.story_engine.core_cast.0.secret" in text
    assert "无需注入的整套方言词库" not in text
    assert "不应当成为已发生历史的将来场景" not in text
    # Genesis secret notes must not become current entity state or reveal grants.
    assert not context.active_entities and not context.character_cognition_states
    assert not context.planned_reveal_ladder


@pytest.mark.parametrize("normalize", [False, True])
def test_main_review_messages_keep_reference_provenance_and_full_body(normalize):
    context = RetrievalBroker()._trim_pack(_context())
    review = (WebNovelExperienceReviewer()._normalize_context(context) if normalize
              else build_review_context_pack(context=context))
    body = '宋安说：“七年前我在场。”温禾知道这是谎话。她去年补写的是另一份账册。'
    reviewer = LLMWebNovelReviewer()
    payload = reviewer._llm_payload(review, WriterOutput(chapter_number=1, title="调档", body=body, end_of_chapter_summary="核对"))
    messages = reviewer._llm_review_messages(
        payload=payload, evidence_ids=[item["evidence_id"] for item in payload["evidence_index"]],
    )
    text = "\n".join(item["content"] for item in messages)
    assert "七年前钟楼失火；去年钟楼重建。" in text
    assert "七年前她烧毁了原账；去年她补写的账是另一份文件。" in text
    assert "他宣称亲眼见过火灾，其实当时人在外城。" in text
    assert payload["draft"]["body"] == body
    assert payload["world"]["genesis_world_overview"] == "钟楼失火后的山城。"
    assert payload["world"]["genesis_story_engine_summary"] == "最终查清钟楼案。"
    assert payload["world"]["genesis_context_refs"]["genesis_revision_id"] == "locked-revision-9"
    assert any(item["evidence_id"] == "genesis:locked-revision-9:world.story_engine.core_cast.0.secret"
               for item in payload["evidence_index"])


def test_reference_budget_omits_whole_fact_and_reports_incompleteness_at_both_boundaries():
    context = _context(world={
        "world_bible": {"history_slice": "过长史书" * 20000},
        "story_engine": {"core_cast": [{"name": "温禾", "secret": "她在第九年改过账。"}]},
    })
    prompt = "\n".join(m["content"] for m in build_single_chapter_draft_prompt(context))
    payload = LLMWebNovelReviewer()._llm_payload(
        build_review_context_pack(context=context), WriterOutput(chapter_number=1, title="调档", body="核对原件", end_of_chapter_summary="核对"),
    )
    assert "过长史书" not in prompt  # A cut sentence could reverse the original fact.
    assert "她在第九年改过账。" in prompt
    assert context.genesis_reference_omitted_count == 1
    assert payload["world"]["genesis_reference_omitted_count"] == 1
    assert "未完整提供" in prompt


@pytest.mark.parametrize("raw_pack", ['{}', 'not-json', '{"world": {"world_bible": null, "story_engine": []}}'])
def test_absent_reference_fields_do_not_fabricate_history(raw_pack):
    context = _context(raw_pack=raw_pack)
    payload = LLMWebNovelReviewer()._llm_payload(
        build_review_context_pack(context=context), WriterOutput(chapter_number=1, title="开篇", body="开篇", end_of_chapter_summary="开篇"),
    )
    assert payload["world"]["genesis_reference_facts"] == []
    assert not [item for item in payload["evidence_index"] if item["kind"] == "genesis_reference"]


def test_actual_broker_budget_does_not_displace_canon_with_oversized_background():
    context = _context(world={
        "world_bible": {"history_slice": "遥远的旧史" * 1500},
        "story_engine": {"core_cast": [{"name": "温禾", "secret": "她在第九年改过账。"}]},
    })
    context.active_entities = [EntitySnapshot(entity_id=f"e{i}", kind="character", name=f"人物{i}", description="", current_state={}) for i in range(8)]
    context.previous_chapter_summaries = ["第1章接受的事实", "第2章接受的事实", "第3章接受的事实"]
    broker = RetrievalBroker()
    trimmed = broker._trim_pack(context)
    prompt = "\n".join(m["content"] for m in build_single_chapter_draft_prompt(trimmed))
    assert len(trimmed.active_entities) == 8
    assert trimmed.previous_chapter_summaries == ["第1章接受的事实", "第2章接受的事实", "第3章接受的事实"]
    assert broker._estimate_chars(trimmed) <= broker.context_budget_chars
    assert "遥远的旧史" not in prompt and "她在第九年改过账。" in prompt
    assert trimmed.genesis_reference_omitted_count == 1


def test_budget_discards_unrelated_source_before_current_canon():
    context = _context(world={"story_engine": {"core_cast": [
        {"name": "远方人", "secret": "从未登场的旧背景。" * 70},
    ]}})
    context.active_entities = [EntitySnapshot(
        entity_id=f"e{i}", kind="character", name=f"人物{i}",
        description="人" * 500, current_state={},
    ) for i in range(8)]
    context.previous_chapter_summaries = ["第1章接受的事实", "第2章接受的事实", "第3章接受的事实"]
    without_background = context.model_copy(update={
        "genesis_reference_facts": [], "genesis_reference_omitted_count": 1,
    })
    budget = RetrievalBroker._estimate_chars(without_background)
    # The source fits its separate quota; total pressure must remove it first.
    assert _budget_genesis_references(context, budget // 4).genesis_reference_facts
    trimmed = RetrievalBroker(context_budget_chars=budget)._trim_pack(context)
    assert len(trimmed.active_entities) == 8
    assert len(trimmed.previous_chapter_summaries) == 3
    assert trimmed.genesis_reference_facts == []
    assert trimmed.genesis_reference_omitted_count == 1


def test_reference_quota_includes_the_existing_component_separator_cost():
    context = _context(world={"world_bible": {"history_slice": "第一年开始"}})
    encoded = json.dumps(context.genesis_reference_facts[0].model_dump(mode="json"), ensure_ascii=False)
    trimmed = _budget_genesis_references(context, len(encoded))
    assert trimmed.genesis_reference_facts == []
    assert trimmed.genesis_reference_omitted_count == 1


def test_reference_relevance_does_not_change_when_its_entity_is_trimmed():
    context = _context(world={"story_engine": {"core_cast": [
        {"name": "人物7", "secret": "曾经改账。" * 200},
    ]}})
    context.active_entities = [EntitySnapshot(
        entity_id=f"e{i}", kind="character", name=f"人物{i}",
        description="人" * 1000, current_state={},
    ) for i in range(8)]
    context.previous_chapter_summaries = ["第1章接受的事实", "第2章接受的事实", "第3章接受的事实"]
    trimmed = RetrievalBroker()._trim_pack(context)
    assert len(trimmed.active_entities) < 8
    assert [fact.subject for fact in trimmed.genesis_reference_facts] == ["人物7"]
    assert trimmed.genesis_reference_omitted_count == 0


def test_retrieval_merge_preserves_available_nested_cognition_for_both_models():
    broker = RetrievalBroker()
    source = WritingPack(project_id="p", accepted_cognition=[AcceptedCognitionSnapshot(
        observer_type="character", observer_id="宋安", as_of_chapter=0,
        false_facts={"belief-a": {"summary": "false:believed"}},
    )], must_not_reveal=["此章不得公开烧账者身份"])
    context = broker._trim_pack(broker._merge_writer_world_model_pack(_context(), source))
    prompt = "\n".join(m["content"] for m in build_single_chapter_draft_prompt(context))
    payload = LLMWebNovelReviewer()._llm_payload(build_review_context_pack(context=context), WriterOutput(
        chapter_number=1, title="调档", body="核对原件", end_of_chapter_summary="核对",
    ))
    assert "false:believed" in prompt
    assert payload["world"]["reveal_context"]["accepted_cognition"][0]["false_facts"] == {"belief-a": {"summary": "false:believed"}}
    assert "此章不得公开烧账者身份" in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("normalize", [False, True])
def test_review_receives_concrete_reveal_and_knowledge_constraints(normalize):
    context = RetrievalBroker()._trim_pack(_context())
    context.must_not_reveal = ["此章不得公开烧账者身份"]
    context.accepted_cognition = [AcceptedCognitionSnapshot(
        observer_type="character", observer_id="宋安", as_of_chapter=0,
        false_facts={"lost-ledger": {"summary": "仍以为原账只是遗失"}},
    ), AcceptedCognitionSnapshot(
        observer_type="reader", observer_id="reader", as_of_chapter=0,
        ref_states={"fact:只能看到焦痕": "known"},
    )]
    context.fair_misdirection_requirements = ["只允许给出焦痕线索"]
    context.chapter_world_delta_intent = ChapterWorldDeltaIntent(
        intent_id="intent-1", project_id="p", chapter_number=1,
        hint_delta_intents=["可展示账本封皮上的微量灰烬"],
    )
    review = (WebNovelExperienceReviewer()._normalize_context(context) if normalize
              else build_review_context_pack(context=context))
    payload = LLMWebNovelReviewer()._llm_payload(
        review, WriterOutput(chapter_number=1, title="调档", body="检查焦痕", end_of_chapter_summary="检查"),
    )
    text = json.dumps(payload, ensure_ascii=False)
    assert "此章不得公开烧账者身份" in text
    assert "仍以为原账只是遗失" in text
    assert "只允许给出焦痕线索" in text
    assert "只能看到焦痕" in text
    assert "可展示账本封皮上的微量灰烬" in text
    assert any(item["evidence_id"] == "world:reveal_context" for item in payload["evidence_index"])
