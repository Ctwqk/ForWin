from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.models.phase import BandExperiencePlan
from forwin.protocol.context import ChapterContextPack, MemorySnippet
from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan
from forwin.protocol.review import RepairInstruction
from forwin.protocol.scene import ScenePlan
from forwin.retrieval import RetrievalBroker
from forwin.review.repair.plan_patch import (
    RepairPlanPatchRequest,
    RepairPlanPatchService,
)
from forwin.state.repo import StateRepository
from forwin.writer import prompt_core
from tests.test_candidate_review_owner import database as _shared_database

database = _shared_database


def _context(**updates):
    return ChapterContextPack(
        project_id="book",
        project_title="Book",
        premise="Fixture",
        genre="mystery",
        setting_summary="",
        chapter_number=1,
        chapter_plan_title="誓约",
        chapter_plan_one_line="核对补充证据",
        chapter_goals=[],
        **updates,
    )


def _instruction(scope="chapter_plan"):
    return RepairInstruction(
        repair_scope=scope,
        failure_type="continuity",
        must_fix=["纠错标记甲", "纠错标记乙", "纠错标记丙", "纠错标记丁"],
        must_preserve=["保留既有承诺标记"],
        must_not_reveal=["尚不可公开的秘密标记"],
    )


def _prompt(context, mode):
    builders = {
        "single": lambda: prompt_core.build_single_chapter_draft_prompt(context),
        "preview": lambda: prompt_core.build_preview_chapter_prompt(context),
        "breakdown": lambda: prompt_core.build_scene_breakdown_prompt(context),
        "scene": lambda: prompt_core.build_scene_generation_prompt(
            context, ScenePlan(scene_no=1, objective="核对")
        ),
        "stitch": lambda: prompt_core.build_scene_stitch_prompt(context, []),
    }
    return "\n".join(item["content"] for item in builders[mode]())


class ContextBroker(RetrievalBroker):
    def build_chapter_context(self, repo, project_id, chapter_plan):
        return _context(
            chapter_experience_plan=repo.get_chapter_experience_plan(
                project_id, chapter_plan.chapter_number
            )
        )


@pytest.mark.parametrize(
    "scope,has_band",
    [
        ("draft", False),
        ("chapter_plan", False),
        ("band_plan", True),
        ("band_plan", False),
    ],
)
@pytest.mark.parametrize("mode", ["single", "preview", "breakdown", "scene", "stitch"])
def test_repair_owner_delivers_entire_current_contract(database, scope, has_band, mode):
    session, chapter = database
    if has_band:
        schedule = BandDelightSchedule(band_id="band", chapter_start=1, chapter_end=2)
        session.add(
            BandExperiencePlan(
                id="band-row",
                project_id="book",
                arc_id="arc",
                band_id="band",
                chapter_start=1,
                chapter_end=2,
                schedule_json=schedule.model_dump_json(),
            )
        )
        session.flush()
    owner = RepairPlanPatchService(
        retrieval_broker=ContextBroker(context_budget_chars=50_000),
        arc_envelope_manager=SimpleNamespace(
            _derive_chapter_experience_plan=lambda **_: ChapterExperiencePlan()
        ),
    )
    instruction = _instruction(scope)
    original = _context()
    result = owner.apply(
        RepairPlanPatchRequest(
            session=session,
            repo=StateRepository(session),
            project_id="book",
            chapter_plan=chapter,
            context=original,
            repair_scope=scope,
            instruction=instruction,
        )
    )
    content = _prompt(result.context, mode)
    for kind in ("must_fix", "must_preserve", "must_not_reveal"):
        for constraint in getattr(instruction, kind):
            assert constraint in content, (scope, mode, kind, constraint)
    # Current rewrite requirements are not permanent chapter-plan facts.
    assert "纠错标记" not in chapter.experience_plan_json
    assert "保留既有承诺标记" not in _prompt(original, mode)
    instruction.must_preserve.append("之后修改的合同不得污染已生成上下文")
    assert "之后修改的合同" not in _prompt(result.context, mode)


def test_repair_contract_is_mandatory_and_counted_in_existing_soft_budget():
    contract = _instruction().model_dump(
        include={"must_fix", "must_preserve", "must_not_reveal"}
    )
    context = _context(repair_contract=contract)
    broker = RetrievalBroker(context_budget_chars=100_000)
    baseline = _context()
    assert broker._estimate_chars(context) > broker._estimate_chars(baseline)
    required_cost = broker._estimate_chars(context)
    broker.context_budget_chars = required_cost + 10
    with_memory = context.model_copy(
        update={
            "retrieved_memories": [
                MemorySnippet(
                    chapter_number=1, title="额外材料", excerpt="次要内容" * 100
                )
            ]
        }
    )
    trimmed = broker._trim_pack(with_memory)
    assert not trimmed.retrieved_memories
    for items in contract.values():
        for constraint in items:
            assert constraint in _prompt(trimmed, "single")


def test_next_repair_replaces_contract_without_mutating_prior_view():
    broker = RetrievalBroker(context_budget_chars=50_000)
    original = _context(must_not_reveal=["现有世界秘密"])
    first = broker.prepare_repair_context(original, _instruction())
    second = broker.prepare_repair_context(
        first,
        RepairInstruction(
            repair_scope="band_plan",
            failure_type="continuity",
            must_fix=["本轮新的纠错"],
        ),
    )
    for mode in ("single", "preview", "breakdown", "scene", "stitch"):
        content = _prompt(second, mode)
        assert "本轮新的纠错" in content
        assert "纠错标记" not in content
        assert "保留既有承诺标记" not in content
        assert "尚不可公开的秘密标记" not in content
        assert "现有世界秘密" in content
        assert "保留既有承诺标记" in _prompt(first, mode)
        assert "本次重写合同" not in _prompt(original, mode)
