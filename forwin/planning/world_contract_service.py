from __future__ import annotations

from sqlalchemy.orm import Session

from forwin.models import ArcPlanVersion, ChapterPlan, Project
from forwin.planning.band_window import BandWindowResolver
from forwin.planning.world_contracts import (
    ArcWorldContract,
    BandWorldContract,
    ChapterWorldDeltaIntent,
    ReaderCognitionTransition,
    RevealLadderStep,
    WorldContractRepository,
)


def _load_goals_json(raw: str) -> list[str]:
    try:
        payload = __import__("json").loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in payload if str(item).strip()] if isinstance(payload, list) else []


class WorldContractPlanningService:
    def ensure_for_arc_band(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        chapter_plans: list[ChapterPlan],
        activation_chapter: int,
        detailed_band_size: int,
    ) -> None:
        if not chapter_plans:
            return
        project = session.get(Project, project_id)
        arc = session.get(ArcPlanVersion, arc_id)
        if project is None or arc is None:
            return

        ordered_plans = sorted(chapter_plans, key=lambda plan: plan.chapter_number)
        all_text_parts = [project.title, project.premise, project.setting_summary, arc.arc_synopsis]
        for plan in ordered_plans:
            all_text_parts.extend([plan.title, plan.one_line])
            all_text_parts.extend(_load_goals_json(plan.goals_json))
        all_text = "\n".join(str(part or "") for part in all_text_parts)

        has_homeworld_crisis = "母星" in all_text or "homeworld" in all_text.lower()
        has_colony_line = "殖民" in all_text or "colony" in all_text.lower()
        primary_line_ids = ["line_colony_defense"] if has_colony_line else ["primary_visible_line"]
        hidden_line_ids = ["line_homeworld_siege"] if has_homeworld_crisis else []
        major_gap_ids = ["gap_homeworld_siege"] if has_homeworld_crisis else []
        reveal_ladder = (
            [
                RevealLadderStep(
                    gap_id="gap_homeworld_siege",
                    chapter_hint=22,
                    from_state="hidden",
                    to_state="hinted",
                    method="通讯延迟",
                    fairness_evidence=["第22章必须出现通讯异常"],
                    must_not_reveal_before=25,
                ),
                RevealLadderStep(
                    gap_id="gap_homeworld_siege",
                    chapter_hint=25,
                    from_state="hinted",
                    to_state="partially_revealed",
                    method="残缺求援",
                    fairness_evidence=["第25章残缺求援只能部分揭示"],
                ),
                RevealLadderStep(
                    gap_id="gap_homeworld_siege",
                    chapter_hint=28,
                    from_state="partially_revealed",
                    to_state="closed",
                    method="返回母星确认",
                    fairness_evidence=["第28章确认此前线索成立"],
                ),
            ]
            if has_homeworld_crisis
            else []
        )
        reader_trajectory = (
            [
                ReaderCognitionTransition(
                    chapter_hint=22,
                    observer_id="reader",
                    from_state="hidden",
                    to_state="hinted",
                    intended_effect="不安与追问",
                    payoff_type="short_term_hint",
                ),
                ReaderCognitionTransition(
                    chapter_hint=25,
                    observer_id="reader",
                    from_state="hinted",
                    to_state="partially_revealed",
                    intended_effect="危机确认但真相未全开",
                    payoff_type="medium_reveal",
                ),
                ReaderCognitionTransition(
                    chapter_hint=28,
                    observer_id="reader",
                    from_state="partially_revealed",
                    to_state="closed",
                    intended_effect="长期悬念阶段兑现",
                    payoff_type="long_term_payoff",
                ),
            ]
            if has_homeworld_crisis
            else []
        )

        repo = WorldContractRepository(session)
        repo.save_arc_contract(
            ArcWorldContract(
                contract_id=f"arc_world_contract_{arc.id}",
                project_id=project_id,
                arc_id=arc_id,
                arc_number=arc.arc_number,
                title=arc.arc_synopsis,
                primary_world_line_ids=primary_line_ids,
                hidden_world_line_ids=hidden_line_ids,
                major_gap_ids=major_gap_ids,
                reveal_ladder=reveal_ladder,
                reader_cognition_trajectory=reader_trajectory,
                medium_term_payoff_promises=["一个隐藏线从 hinted 走到 partially_revealed"] if has_homeworld_crisis else [],
                long_term_payoff_promises=["殖民地成为反攻母星基础"] if has_homeworld_crisis else [],
                arc_exit_objective_state="殖民地成为反攻母星基础" if has_homeworld_crisis else arc.arc_synopsis,
                arc_exit_reader_state="partially_revealed" if has_homeworld_crisis else "aware",
            )
        )

        window = BandWindowResolver().resolve(
            chapter_plans=ordered_plans,
            activation_chapter=activation_chapter,
            detailed_band_size=detailed_band_size,
        )
        active_band = window.active_band
        repo.save_band_contract(
            BandWorldContract(
                contract_id=f"band_world_contract_{arc.id}_{window.chapter_start}_{window.chapter_end}",
                project_id=project_id,
                arc_id=arc_id,
                band_id=window.band_id,
                chapter_start=window.chapter_start,
                chapter_end=window.chapter_end,
                foreground_world_line_ids=primary_line_ids,
                hidden_world_line_ids=hidden_line_ids,
                required_hints=["乱码通讯", "父亲旧部呼号"] if has_homeworld_crisis else [],
                gap_transitions={"gap_homeworld_siege": "hidden -> hinted"} if has_homeworld_crisis else {},
                payoff_commitments=["本 band 只给 mystery hint，不做 full reveal"] if has_homeworld_crisis else [],
                band_exit_reader_state="hinted" if has_homeworld_crisis else "aware",
                band_exit_hidden_line_state="母星通讯被进一步切断" if has_homeworld_crisis else "",
            )
        )

        for plan in active_band:
            is_homeworld_hint_chapter = has_homeworld_crisis and plan.chapter_number == 23
            repo.save_chapter_intent(
                ChapterWorldDeltaIntent(
                    intent_id=f"chapter_{plan.chapter_number}_world_intent",
                    project_id=project_id,
                    chapter_plan_id=plan.id,
                    chapter_number=plan.chapter_number,
                    visible_delta_intents=["殖民地防线修复"] if is_homeworld_hint_chapter else _load_goals_json(plan.goals_json)[:1],
                    offscreen_delta_intents=["敌方切断第三通讯阵列"] if is_homeworld_hint_chapter else [],
                    hint_delta_intents=["乱码通讯", "父亲旧部呼号"] if is_homeworld_hint_chapter else [],
                    knowledge_delta_intents=["主角进入 suspected 状态"] if is_homeworld_hint_chapter else [],
                    reader_experience_intents=["mystery hint"] if is_homeworld_hint_chapter else [],
                    must_not_reveal=["father_sieged"] if has_homeworld_crisis and plan.chapter_number < 25 else [],
                    delta_sources=["faction_action", "information_spread"] if is_homeworld_hint_chapter else [],
                    expected_observer_state_changes={
                        "reader": "hidden -> hinted",
                        "protagonist": "unknown -> suspected",
                    }
                    if is_homeworld_hint_chapter
                    else {},
                )
            )
