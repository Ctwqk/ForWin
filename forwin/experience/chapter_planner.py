from __future__ import annotations

from forwin.experience.service import AudienceCalibrationProfile
from forwin.experience.types import ArcExperienceBundle
from forwin.models.project import ChapterPlan
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.protocol.experience import BandDelightSchedule, ChapterExperiencePlan


def _load_goals_json(raw: str) -> list[str]:
    try:
        payload = __import__("json").loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in payload if str(item).strip()] if isinstance(payload, list) else []


class ChapterExperiencePlanner:
    def derive_chapter_experience_plan(
        self,
        *,
        chapter_number: int,
        structure: ArcStructureDraftData,
        arc_experience: ArcExperienceBundle,
        schedule: BandDelightSchedule,
        chapter_plan: ChapterPlan,
        calibration: AudienceCalibrationProfile | None = None,
    ) -> ChapterExperiencePlan:
        calibration = calibration or AudienceCalibrationProfile()
        chapter_rewards = [
            item for item in schedule.scheduled_rewards if item.chapter_hint == chapter_number
        ]
        goals = _load_goals_json(chapter_plan.goals_json)
        reward_tags = [item.category for item in chapter_rewards]
        hook_type = "cliffhanger_question"
        if "power" in reward_tags:
            hook_type = "advantage_reveal"
        elif "emotion" in reward_tags:
            hook_type = "emotional_knife"
        elif "justice" in reward_tags:
            hook_type = "retribution_snap"
        elif "social" in reward_tags:
            hook_type = "status_flip"
        immersion_anchors = [
            schedule.immersion_anchor_scene_goal
            if chapter_number == schedule.chapter_start + max(0, (schedule.chapter_end - schedule.chapter_start) // 2)
            else "",
            chapter_plan.one_line,
            *goals[:2],
        ]
        progress_markers = goals[:3] or [chapter_plan.one_line or chapter_plan.title]
        if any(item.intent == "micro_progress_power" for item in chapter_rewards):
            progress_markers = [*(progress_markers[:2]), "给主角一个可验证的微进展/实力兑现"]
        if any(item.intent == "social_dominance" for item in chapter_rewards):
            progress_markers = [*progress_markers[:2], "让社会地位或公开场面出现明确逆转"]
        if any(item.intent == "mystery_clue_or_reveal" for item in chapter_rewards):
            progress_markers = [*progress_markers[:2], "给出一条真实可追踪的新线索或半揭晓"]
        chapter_curiosity = next(
            (item for item in schedule.curiosity_beats if item.chapter_hint == chapter_number),
            None,
        )
        question_hook = (
            chapter_curiosity.question_open
            if chapter_curiosity is not None
            else (chapter_plan.one_line or chapter_plan.title)
        )
        question_resolution = (
            chapter_curiosity.question_resolve
            if chapter_curiosity is not None
            else (
                "至少解决一个小问题，并换来更大的问题"
                if "mystery" in reward_tags
                else "至少兑现一个可验证的局面变化"
            )
        )
        payoff_map = arc_experience.arc_payoff_map
        reader_promise = arc_experience.reader_promise
        rule_anchors = [
            item.summary
            for item in payoff_map.revelation_layers[:2]
            if str(item.summary).strip()
        ]
        rule_anchors.extend(
            item for item in payoff_map.ambiguity_constraints[:2] if str(item).strip()
        )
        if reader_promise.world_legibility_target:
            rule_anchors.append(reader_promise.world_legibility_target)
        if calibration.clarify_rule_legibility:
            rule_anchors.append("把当前冲突涉及的规则、代价与因果关系讲清楚。")
            progress_markers.append("明确一条正在生效的规则边界或代价")
        minimum_progress_channels = ["event", "thread"]
        if "power" in reward_tags or "justice" in reward_tags:
            minimum_progress_channels.append("state")
        if "social" in reward_tags:
            minimum_progress_channels.append("status")
        if "emotion" in reward_tags:
            minimum_progress_channels.append("relationship")
        if "mystery" in reward_tags:
            minimum_progress_channels.append("rule")
        if calibration.clarify_rule_legibility and "rule" not in minimum_progress_channels:
            minimum_progress_channels.append("rule")
        if calibration.protect_character_heat and "relationship" not in minimum_progress_channels:
            minimum_progress_channels.append("relationship")
        relationship_or_status_shift = ""
        if "social" in reward_tags:
            relationship_or_status_shift = "本章至少要让公开地位、评价或权力排序发生一次明确变化。"
        elif "emotion" in reward_tags:
            relationship_or_status_shift = "本章至少要让角色关系或情感站位发生一次明确变化。"
        elif calibration.protect_character_heat:
            relationship_or_status_shift = "给当前高热角色一处可记忆的互动、态度变化或存在感强化。"
        return ChapterExperiencePlan(
            planned_reward_tags=reward_tags,
            selected_template_ids=[item.template_id for item in chapter_rewards],
            hook_type=hook_type,
            question_hook=question_hook,
            question_resolution=question_resolution,
            immersion_anchors=[str(item).strip() for item in immersion_anchors if str(item).strip()],
            progress_markers=[str(item).strip() for item in progress_markers if str(item).strip()],
            rule_anchors=[str(item).strip() for item in rule_anchors if str(item).strip()],
            relationship_or_status_shift=relationship_or_status_shift,
            minimum_progress_channels=list(dict.fromkeys(minimum_progress_channels)),
        )
