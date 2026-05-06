from __future__ import annotations

from typing import Any

from forwin.experience.types import ArcExperienceBundle
from forwin.models.project import ChapterPlan, Project
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise


class ArcExperiencePlanningService:
    def plan_arc_experience(
        self,
        *,
        project: Project,
        structure: ArcStructureDraftData,
        chapter_plans: list[ChapterPlan],
        audience_trends: list[str],
        drafted_payload: dict[str, Any] | None = None,
    ) -> ArcExperienceBundle:
        payload = drafted_payload or {}
        reader_payload = payload.get("reader_promise") if isinstance(payload.get("reader_promise"), dict) else None
        payoff_payload = payload.get("arc_payoff_map") if isinstance(payload.get("arc_payoff_map"), dict) else None
        fallback = self._fallback_payload(
            project=project,
            structure=structure,
            chapter_plans=chapter_plans,
            audience_trends=audience_trends,
        )
        return ArcExperienceBundle(
            reader_promise=ReaderPromise.model_validate(reader_payload or fallback["reader_promise"]),
            arc_payoff_map=ArcPayoffMap.model_validate(payoff_payload or fallback["arc_payoff_map"]),
        )

    @staticmethod
    def _fallback_payload(
        *,
        project: Project,
        structure: ArcStructureDraftData,
        chapter_plans: list[ChapterPlan],
        audience_trends: list[str],
    ) -> dict[str, Any]:
        trend_text = " ".join(str(item) for item in audience_trends)
        core_pleasures = ["稳定微回报", "阶段性翻盘", "真相逐层揭开"]
        macro_payoffs: list[dict[str, Any]] = []
        if structure.key_beats:
            macro_payoffs.append(
                {
                    "payoff_id": "payoff-1",
                    "category": "mystery",
                    "template_id": "mystery-locked-clue",
                    "target_chapter_hint": "arc-mid",
                    "setup_requirement": structure.key_beats[0],
                    "success_signal": "读者确认真相正在逼近",
                }
            )
        if "character_heat" in trend_text or "relationship_interest" in trend_text:
            core_pleasures.append("角色关系与地位波动")
            macro_payoffs.append(
                {
                    "payoff_id": "payoff-emotion",
                    "category": "emotion",
                    "template_id": "emotion-knife-turn",
                    "target_chapter_hint": "arc-late",
                    "setup_requirement": "建立关键角色连结",
                    "success_signal": "关系站位出现明确变化",
                }
            )
        world_legibility = "关键冲突的规则与代价必须能被读者读懂。"
        ambiguity_constraints = ["关键结果必须能回指既有线索与规则。"]
        if "confusion" in trend_text or "risk" in trend_text or "prediction" in trend_text:
            world_legibility = "每个关键反转都要让读者看得懂代价、边界与因果。"
            ambiguity_constraints.append("所有认知反转都必须回指前文线索。")
        return {
            "reader_promise": {
                "genre_promise": f"{project.genre}网文",
                "pleasure_promise": f"{project.genre}读者会稳定获得悬念和回报",
                "core_pleasures": core_pleasures,
                "acceptable_drag_level": "low",
                "acceptable_exposition_density": "medium",
                "cliffhanger_aggressiveness": "high",
                "ambiguity_mode": "managed",
                "world_legibility_target": world_legibility,
            },
            "arc_payoff_map": {
                "macro_payoffs": macro_payoffs,
                "awe_kit": ["反转", "线索揭面", "代价升级"],
                "revelation_layers": [],
                "ambiguity_constraints": ambiguity_constraints,
            },
        }
