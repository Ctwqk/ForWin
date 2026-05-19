from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


ACTION_ACTIVE_TASK = "active_task"
ACTION_WAITING_REVIEW = "waiting_review"
ACTION_STARTED_INITIAL_GENERATION = "started_initial_generation"
ACTION_STARTED_CONTINUE_GENERATION = "started_continue_generation"
ACTION_RAN_REVIEW_JOBS = "ran_review_jobs"
ACTION_STARTED_PUBLISH_JOBS = "started_publish_jobs"
ACTION_IDLE = "idle"
ACTION_BLOCKED = "blocked"


class ProductionDecisionEvent(BaseModel):
    project_id: str
    action: str
    blocked_reason: str = ""
    task_id: str = ""
    publish_job_count: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


def action_for_blocked_reason(reason: str) -> str:
    normalized = str(reason or "").strip()
    if normalized == "waiting_review":
        return ACTION_WAITING_REVIEW
    if normalized == "active_generation_task":
        return ACTION_ACTIVE_TASK
    return ACTION_BLOCKED


def message_for_action(
    action: str,
    *,
    chapter_count: int = 0,
    publish_job_count: int = 0,
    blocked_reason: str = "",
) -> str:
    if action == ACTION_ACTIVE_TASK:
        return "已有运行中的生成任务，今日不重复调度。"
    if action == ACTION_WAITING_REVIEW:
        return "仍有章节等待人工 review，今日暂停自动生成。"
    if action == ACTION_STARTED_INITIAL_GENERATION:
        return f"已按计划启动首批 {max(0, int(chapter_count or 0))} 章生成。"
    if action == ACTION_STARTED_CONTINUE_GENERATION:
        return f"已按计划继续生成，今日最多处理 {max(0, int(chapter_count or 0))} 章。"
    if action == ACTION_RAN_REVIEW_JOBS:
        return f"已按计划处理 {max(0, int(chapter_count or 0))} 个 review 任务。"
    if action == ACTION_STARTED_PUBLISH_JOBS:
        return f"已按计划创建 {max(0, int(publish_job_count or 0))} 个发布上传任务。"
    if action == ACTION_IDLE:
        return "没有待生成章节，今日无需调度。"
    if blocked_reason:
        return f"今日调度被阻塞：{blocked_reason}。"
    return "今日调度被阻塞。"
