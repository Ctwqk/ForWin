from __future__ import annotations

from forwin.experience.service import AudienceCalibrationProfile
from forwin.experience.types import ArcExperienceBundle
from forwin.models.project import ChapterPlan
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.protocol.experience import (
    AmbiguityPayoff,
    BandDelightSchedule,
    BandRewardItem,
    CuriosityBeat,
)
from forwin.protocol.trope_library import load_trope_template_library, trope_templates_by_category


class BandExperienceScheduler:
    def derive_band_delight_schedule(
        self,
        *,
        band_id: str,
        chapter_start: int,
        chapter_end: int,
        structure: ArcStructureDraftData,
        arc_experience: ArcExperienceBundle,
        active_band: list[ChapterPlan],
        calibration: AudienceCalibrationProfile | None = None,
        cost_ceiling: int = 3,
    ) -> BandDelightSchedule:
        calibration = calibration or AudienceCalibrationProfile()
        normalized_cost_ceiling = _normalize_cost_ceiling(cost_ceiling)
        band_length = max(1, chapter_end - chapter_start + 1)
        stall_guard_max_gap = max(1, min(2, band_length - 1 if band_length > 1 else 1))
        scheduled_rewards: list[BandRewardItem] = []
        curiosity_beats: list[CuriosityBeat] = []
        ambiguity_payoffs: list[AmbiguityPayoff] = []
        payoff_map = arc_experience.arc_payoff_map
        reader_promise = arc_experience.reader_promise
        macro_by_category = {
            item.category: item
            for item in payoff_map.macro_payoffs
            if item.category not in {"emotion"}
        }
        used_template_ids: set[str] = set()

        def chapter_for(slot: str) -> int:
            if slot == "early":
                return chapter_start
            if slot == "late":
                return chapter_end
            if slot == "mid":
                return chapter_start + max(0, (band_length - 1) // 2)
            return chapter_start

        def template_for(category: str) -> str:
            macro = macro_by_category.get(category)
            if macro is not None and macro.template_id and macro.template_id not in used_template_ids:
                used_template_ids.add(macro.template_id)
                return macro.template_id

            category_templates = _sorted_templates(trope_templates_by_category(category))
            under_ceiling = [
                template
                for template in category_templates
                if template.template_id not in used_template_ids
                and template.cost_weight <= normalized_cost_ceiling
            ]
            fallback_same_category = [
                template
                for template in category_templates
                if template.template_id not in used_template_ids
            ]
            library_templates = _sorted_templates(load_trope_template_library())
            fallback_library = [
                template
                for template in library_templates
                if template.template_id not in used_template_ids
            ]

            for candidates in (under_ceiling, fallback_same_category, fallback_library, library_templates):
                if candidates:
                    selected = candidates[0].template_id
                    if selected:
                        used_template_ids.add(selected)
                    return selected
            return ""

        blueprint: list[tuple[str, str, str]] = [
            ("power", "early", "micro_progress_power"),
            ("social", "mid" if band_length >= 2 else "early", "social_dominance"),
            ("mystery", "late", "mystery_clue_or_reveal"),
        ]
        if calibration.boost_reward_density and band_length >= 3:
            blueprint.insert(1, ("power", "mid" if band_length >= 4 else "late", "micro_progress_power"))
        pleasures_text = " ".join(reader_promise.core_pleasures)
        if band_length >= 3:
            blueprint.insert(1, ("power", "mid", "micro_progress_power"))
        if any(item.category == "justice" for item in payoff_map.macro_payoffs):
            blueprint.append(("justice", "late", "justice_snap"))
        elif any(item.category == "emotion" for item in payoff_map.macro_payoffs) or any(
            token in pleasures_text for token in ("角色", "关系", "情感")
        ):
            blueprint.append(("emotion", "late", "emotion_knife"))
        elif calibration.protect_character_heat:
            blueprint.append(("emotion", "late", "emotion_knife"))

        for category, slot, intent in blueprint:
            scheduled_rewards.append(
                BandRewardItem(
                    chapter_hint=chapter_for(slot),
                    category=category,
                    template_id=template_for(category),
                    intent=intent,
                )
            )

        reward_chapters = sorted(set(item.chapter_hint for item in scheduled_rewards))
        cursor = chapter_start
        while cursor <= chapter_end:
            if reward_chapters and any(abs(cursor - chapter) <= stall_guard_max_gap for chapter in reward_chapters):
                cursor += 1
                continue
            category = "power" if cursor < chapter_end else "mystery"
            scheduled_rewards.append(
                BandRewardItem(
                    chapter_hint=cursor,
                    category=category,
                    template_id=template_for(category),
                    intent="stall_guard_cover",
                )
            )
            reward_chapters = sorted(set(item.chapter_hint for item in scheduled_rewards))
            cursor += 1

        first_question = (
            active_band[0].one_line
            if active_band and active_band[0].one_line
            else (structure.key_beats[0] if structure.key_beats else "当前局面真正的问题是什么")
        )
        opened_question = (
            structure.key_beats[1]
            if len(structure.key_beats) > 1
            else "当前危机背后还有谁在推动局势"
        )
        curiosity_beats.append(
            CuriosityBeat(
                chapter_hint=chapter_start,
                question_open=first_question,
                question_resolve=(
                    structure.key_beats[0]
                    if structure.key_beats
                    else "本 band 至少确认一条线索不是偶然"
                ),
                escalated_question=opened_question,
            )
        )
        if band_length >= 3:
            curiosity_beats.append(
                CuriosityBeat(
                    chapter_hint=chapter_for("late"),
                    question_open=opened_question,
                    question_resolve="确认一个阶段性真相或代价",
                    escalated_question=(
                        structure.key_beats[2]
                        if len(structure.key_beats) > 2
                        else "真正的规则限制究竟是什么"
                    ),
                )
            )
        if calibration.clarify_rule_legibility:
            curiosity_beats.append(
                CuriosityBeat(
                    chapter_hint=chapter_for("mid"),
                    question_open="这条规则到底限制了什么",
                    question_resolve="明确一条正在生效的代价、边界或因果限制",
                    escalated_question="如果继续逼近真相，会触发什么新的代价",
                )
            )

        ambiguity_constraints = [item for item in payoff_map.ambiguity_constraints if str(item).strip()]
        ambiguity_mode = (reader_promise.ambiguity_mode or "managed").strip()
        if calibration.hold_managed_ambiguity and ambiguity_mode == "stable":
            ambiguity_mode = "managed"
        ambiguity_payoffs.append(
            AmbiguityPayoff(
                chapter_hint=chapter_start,
                payoff_type="confirmation",
                summary="先确认一条小事实，证明叙事并非随机失真。",
                constraint_ref=ambiguity_constraints[0] if ambiguity_constraints else "",
            )
        )
        ambiguity_payoffs.append(
            AmbiguityPayoff(
                chapter_hint=chapter_for("mid"),
                payoff_type="constraint",
                summary="明确这一段不允许被打破的规则边界或代价。",
                constraint_ref=ambiguity_constraints[0] if ambiguity_constraints else "",
            )
        )
        ambiguity_payoffs.append(
            AmbiguityPayoff(
                chapter_hint=chapter_end,
                payoff_type="reversal",
                summary=(
                    "在不破坏规则边界的前提下给出一次认知反转。"
                    if ambiguity_mode == "high"
                    else "预置或兑现一次受控认知转向，但不能破坏已确认事实。"
                ),
                constraint_ref=(
                    ambiguity_constraints[1]
                    if len(ambiguity_constraints) > 1
                    else (ambiguity_constraints[0] if ambiguity_constraints else "")
                ),
            )
        )
        immersion_anchor_chapter = chapter_for("mid") if band_length > 1 else chapter_start
        return BandDelightSchedule(
            band_id=band_id,
            chapter_start=chapter_start,
            chapter_end=chapter_end,
            scheduled_rewards=sorted(
                scheduled_rewards,
                key=lambda item: (item.chapter_hint, item.category, item.template_id),
            ),
            immersion_anchor_scene_goal=(
                f"第{immersion_anchor_chapter}章必须给出一个可感知现场的沉浸 anchor scene："
                + (
                    structure.key_beats[0]
                    if structure.key_beats
                    else (active_band[0].one_line if active_band else "让读者进入现场")
                )
            ),
            stall_guard_max_gap=stall_guard_max_gap,
            curiosity_beats=curiosity_beats,
            ambiguity_payoffs=ambiguity_payoffs,
        )


def _normalize_cost_ceiling(value: object) -> int:
    if value is None:
        return 3
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 3


def _sorted_templates(templates: object) -> list:
    return sorted(
        list(templates or []),
        key=lambda template: (int(getattr(template, "cost_weight", 2) or 0), str(template.template_id)),
    )
