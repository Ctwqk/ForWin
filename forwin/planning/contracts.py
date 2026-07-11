from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from forwin.protocol.experience import BandDelightSchedule


PlanTaskType = Literal[
    "plot_advance",
    "relationship_shift",
    "setup",
    "withhold",
    "experience_delivery",
]


PLAN_TASK_TYPES = {
    "plot_advance",
    "relationship_shift",
    "setup",
    "withhold",
    "experience_delivery",
}


class PlanTaskItem(BaseModel):
    task_type: PlanTaskType
    description: str = ""
    target_name: str = ""
    required_keywords: list[str] = Field(default_factory=list)
    forbidden_keywords: list[str] = Field(default_factory=list)
    source: str = "derived"


def load_plan_task_contract(
    raw: str | list[dict[str, Any]] | None,
) -> list[PlanTaskItem]:
    if isinstance(raw, list):
        payload = raw
    else:
        try:
            payload = json.loads(raw or "[]") or []
        except (json.JSONDecodeError, TypeError):
            payload = []
    tasks: list[PlanTaskItem] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            tasks.append(PlanTaskItem.model_validate(item))
        except Exception:
            continue
    return tasks


def derive_chapter_task_contract(goals: list[str]) -> list[PlanTaskItem]:
    tasks: list[PlanTaskItem] = []
    for goal in goals[:4]:
        text = str(goal or "").strip()
        if len(text) < 2:
            continue
        if is_derived_goal_control_instruction(text):
            continue
        tasks.append(
            PlanTaskItem(
                task_type="plot_advance",
                description=text,
                source="derived_from_goals",
            )
        )
    return tasks


def is_derived_goal_control_instruction(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return any(
        marker in value
        for marker in (
            "accepted canon",
            "canon 优先",
            "不改写已发生事实",
            "必须紧接最新 accepted canon",
            "必须紧接此状态",
            "承接上一章 accepted",
            "连续性护栏",
            "最新 canon ledger",
            "旧计划/旧摘要",
            "不得回退成几天",
            "不要回退成几天",
            "不要写成几天",
            "分钟级倒计时不得回退",
        )
    )


def derive_band_task_contract(schedule: "BandDelightSchedule") -> list[PlanTaskItem]:
    tasks: list[PlanTaskItem] = []
    seen_reward_targets: set[str] = set()
    for reward in schedule.scheduled_rewards:
        target = str(reward.category or "").strip()
        if not target or target in seen_reward_targets:
            continue
        seen_reward_targets.add(target)
        tasks.append(
            PlanTaskItem(
                task_type="experience_delivery",
                description=f"本 band 至少交付一次 {target} 回报。",
                target_name=target,
                source="derived_from_schedule",
            )
        )
    for beat in schedule.curiosity_beats[:2]:
        if not str(beat.question_open or "").strip():
            continue
        tasks.append(
            PlanTaskItem(
                task_type="setup",
                description=str(beat.question_open or "").strip(),
                source="derived_from_schedule",
            )
        )
        if str(beat.question_resolve or "").strip():
            tasks.append(
                PlanTaskItem(
                    task_type="plot_advance",
                    description=str(beat.question_resolve or "").strip(),
                    source="derived_from_schedule",
                )
            )
    return tasks


def plan_task_contract_to_json(tasks: list[PlanTaskItem]) -> str:
    return json.dumps(
        [item.model_dump(mode="json") for item in tasks], ensure_ascii=False
    )


__all__ = [
    "PLAN_TASK_TYPES",
    "PlanTaskItem",
    "PlanTaskType",
    "derive_band_task_contract",
    "derive_chapter_task_contract",
    "is_derived_goal_control_instruction",
    "load_plan_task_contract",
    "plan_task_contract_to_json",
]
