from __future__ import annotations

from forwin.experience.chapter_planner import ChapterExperiencePlanner

from forwin.protocol.experience import (
    ArcPayoffMap,
    BandDelightSchedule,
    ChapterExperiencePlan,
)
from forwin.models.phase import (
    BandExperiencePlan,
)
import json
from forwin.models import new_id
from forwin.protocol.review import RepairInstruction
from sqlalchemy.orm import Session
from forwin.state.repo import StateRepository


def _current_chapter_repair_experience_plan(
    current_plan: ChapterExperiencePlan,
    repair_instruction: RepairInstruction,
) -> ChapterExperiencePlan:
    return current_plan.model_copy(
        update=_chapter_experience_patch_payload(
            current_plan,
            repair_instruction,
        )
    )


def _chapter_experience_patch_payload(
    current_plan: ChapterExperiencePlan,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    repair_rule_anchors = [
        f"repair must fix: {item}"
        for item in repair_instruction.must_fix[:3]
        if str(item or "").strip()
    ]
    update: dict[str, object] = {
        "planned_reward_tags": list(
            repair_instruction.design_patch.get("planned_reward_tags")
            or current_plan.planned_reward_tags
            or ["mystery"]
        ),
        "selected_template_ids": list(
            repair_instruction.design_patch.get("selected_template_ids")
            or current_plan.selected_template_ids
        ),
        "hook_type": str(
            repair_instruction.design_patch.get("hook_type")
            or current_plan.hook_type
            or "cliffhanger_question"
        ),
        "question_hook": str(
            repair_instruction.design_patch.get("question_hook")
            or current_plan.question_hook
        ),
        "question_resolution": str(
            repair_instruction.design_patch.get("question_resolution")
            or current_plan.question_resolution
        ),
        "immersion_anchors": list(
            repair_instruction.design_patch.get("immersion_anchors")
            or current_plan.immersion_anchors
        ),
        "progress_markers": list(
            repair_instruction.design_patch.get("progress_markers")
            or current_plan.progress_markers
        ),
        "rule_anchors": list(
            repair_instruction.design_patch.get("rule_anchors")
            or current_plan.rule_anchors
        ),
        "relationship_or_status_shift": str(
            repair_instruction.design_patch.get("relationship_or_status_shift")
            or current_plan.relationship_or_status_shift
        ),
        "minimum_progress_channels": list(
            repair_instruction.design_patch.get("minimum_progress_channels")
            or current_plan.minimum_progress_channels
        ),
    }
    if (
        repair_instruction.failure_type == "hook_failure"
        and "hook_type" not in repair_instruction.design_patch
    ):
        update["hook_type"] = "hard_cliffhanger"
    if (
        repair_instruction.failure_type == "immersion"
        and not update["immersion_anchors"]
    ):
        update["immersion_anchors"] = ["补入感官锚点", "让角色即时反应落地"]
    if repair_instruction.failure_type == "immersion" and not update["rule_anchors"]:
        update["rule_anchors"] = ["补清规则边界或代价，防止作者强行感"]
    if repair_rule_anchors:
        existing_rule_anchors = [
            str(item) for item in update.get("rule_anchors", []) or []
        ]
        update["rule_anchors"] = [*repair_rule_anchors, *existing_rule_anchors]
    if repair_instruction.failure_type == "stall" and not update["progress_markers"]:
        update["progress_markers"] = ["让主目标出现不可逆推进"]
    if repair_instruction.failure_type == "stall" and not update["question_hook"]:
        update["question_hook"] = "补出一个比当前更强的新问题"
    return update


def _band_schedule_patch_payload(
    schedule: BandDelightSchedule,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    payload = schedule.model_dump(mode="json")
    payload.update(repair_instruction.design_patch)
    if repair_instruction.failure_type == "stall":
        payload["stall_guard_max_gap"] = 1
    if repair_instruction.failure_type == "immersion" and not payload.get(
        "immersion_anchor_scene_goal"
    ):
        payload["immersion_anchor_scene_goal"] = "每章都落一个可感知现场锚点"
    if repair_instruction.failure_type == "stall" and not payload.get(
        "curiosity_beats"
    ):
        payload["curiosity_beats"] = [
            {
                "chapter_hint": schedule.chapter_start,
                "question_open": "当前局面真正危险在哪里",
                "question_resolve": "先确认一个局部真相",
                "escalated_question": "更大的幕后压力是什么",
            }
        ]
    return payload


def _arc_payoff_patch_payload(
    payoff_map: ArcPayoffMap,
    repair_instruction: RepairInstruction,
) -> dict[str, object]:
    payload = payoff_map.model_dump(mode="json")
    patch = dict(repair_instruction.design_patch)
    if "macro_payoffs" in patch:
        payload["macro_payoffs"] = patch["macro_payoffs"]
    if "awe_kit" in patch:
        payload["awe_kit"] = patch["awe_kit"]
    if "revelation_layers" in patch:
        payload["revelation_layers"] = patch["revelation_layers"]
    if "ambiguity_constraints" in patch:
        payload["ambiguity_constraints"] = patch["ambiguity_constraints"]
    if repair_instruction.failure_type == "payoff_miss" and not payload.get(
        "macro_payoffs"
    ):
        payload["macro_payoffs"] = [
            {
                "payoff_id": "repair-payoff-1",
                "category": "mystery",
                "template_id": "mystery-locked-clue",
                "target_chapter_hint": "near-term",
                "setup_requirement": "缩短 setup 到本 band 内",
                "success_signal": "读者感到明确回报已经到账",
            }
        ]
    if repair_instruction.failure_type == "immersion" and not payload.get(
        "ambiguity_constraints"
    ):
        payload["ambiguity_constraints"] = ["关键翻盘必须回指既有规则或线索。"]
    return payload


class RepairPlanningStage:
    """Owns the repair patches stage behavior."""

    def _replace_band_schedule(
        self,
        *,
        session: Session,
        repo: StateRepository,
        project_id: str,
        chapter_number: int,
        schedule: BandDelightSchedule,
        repair_instruction: RepairInstruction | None = None,
    ) -> None:
        active_arc = repo.get_active_arc_plan(project_id)
        if active_arc is None:
            return
        session.query(BandExperiencePlan).filter(
            BandExperiencePlan.project_id == project_id,
            BandExperiencePlan.arc_id == active_arc.id,
            BandExperiencePlan.band_id == schedule.band_id,
        ).delete(synchronize_session=False)
        session.add(
            BandExperiencePlan(
                id=new_id(),
                project_id=project_id,
                arc_id=active_arc.id,
                band_id=schedule.band_id,
                chapter_start=schedule.chapter_start,
                chapter_end=schedule.chapter_end,
                stall_guard_max_gap=schedule.stall_guard_max_gap,
                schedule_json=json.dumps(
                    schedule.model_dump(mode="json"), ensure_ascii=False
                ),
            )
        )
        experience = repo.get_beat_sheet_experience(project_id)
        for number in range(
            max(chapter_number, schedule.chapter_start), schedule.chapter_end + 1
        ):
            plan = repo.get_chapter_plan(project_id, number)
            if plan is None:
                continue
            experience_plan = ChapterExperiencePlanner().derive_chapter_experience_plan(
                chapter_number=number,
                experience=experience,
                schedule=schedule,
                chapter_plan=plan,
            )
            if number == chapter_number and repair_instruction is not None:
                experience_plan = self._current_chapter_repair_experience_plan(
                    experience_plan,
                    repair_instruction,
                )
            plan.experience_plan_json = json.dumps(
                experience_plan.model_dump(mode="json"), ensure_ascii=False
            )
            session.add(plan)


    @staticmethod
    def _current_chapter_repair_experience_plan(
        current_plan: ChapterExperiencePlan, repair_instruction: RepairInstruction
    ) -> ChapterExperiencePlan:
        return _current_chapter_repair_experience_plan(current_plan, repair_instruction)

    @staticmethod
    def _chapter_experience_patch_payload(
        current_plan: ChapterExperiencePlan, repair_instruction: RepairInstruction
    ) -> dict[str, object]:
        return _chapter_experience_patch_payload(current_plan, repair_instruction)

    @staticmethod
    def _band_schedule_patch_payload(
        schedule: BandDelightSchedule, repair_instruction: RepairInstruction
    ) -> dict[str, object]:
        return _band_schedule_patch_payload(schedule, repair_instruction)

    @staticmethod
    def _arc_payoff_patch_payload(
        payoff_map: ArcPayoffMap, repair_instruction: RepairInstruction
    ) -> dict[str, object]:
        return _arc_payoff_patch_payload(payoff_map, repair_instruction)


__all__ = ["RepairPlanningStage"]
