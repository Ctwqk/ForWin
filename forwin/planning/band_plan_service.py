from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.persistence import ExperiencePersistence
from forwin.experience.service import ExperiencePlanningService
from forwin.experience.types import ArcExperienceBundle
from forwin.checker.reference_classifier import (
    candidate_character_name,
    looks_like_technical_identifier,
    normalize_character_reference,
)
from forwin.models.project import ChapterPlan, Project
from forwin.planning.band_plan.band_role import classify_band_role
from forwin.planning.band_plan.contract_templates import contract_for_role
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.planning.band_window import BandWindowResolver
from forwin.planning.progression_rules import active_progression_rules_for_chapter
from forwin.planning.world_contract_service import WorldContractPlanningService
from forwin.protocol.subworld import ChapterEntryTarget
from forwin.state.updater import StateUpdater


@dataclass(slots=True)
class BandPlanningRequest:
    project_id: str
    arc_id: str
    activation_chapter: int
    detailed_band_size: int
    chapter_plans: list[ChapterPlan]
    structure: ArcStructureDraftData
    arc_experience: ArcExperienceBundle


@dataclass(slots=True)
class BandPlanningResult:
    band_id: str
    chapter_start: int
    chapter_end: int
    schedule: object
    updated_chapter_numbers: list[int]


@dataclass(slots=True)
class BandPlanningBundle:
    request: BandPlanningRequest
    result: BandPlanningResult


class BandPlanService:
    def __init__(
        self,
        *,
        subworld_manager: Any | None = None,
        world_contract_service: WorldContractPlanningService | None = None,
        experience_service: ExperiencePlanningService | None = None,
        scheduler: BandExperienceScheduler | None = None,
        chapter_planner: ChapterExperiencePlanner | None = None,
        persistence: ExperiencePersistence | None = None,
        window_resolver: BandWindowResolver | None = None,
        trope_cost_ceiling: int = 3,
    ) -> None:
        if subworld_manager is None:
            from forwin.subworld_manager import SubWorldManager

            subworld_manager = SubWorldManager()
        self.subworld_manager = subworld_manager or SubWorldManager()
        self.world_contract_service = world_contract_service or WorldContractPlanningService()
        self.experience_service = experience_service or ExperiencePlanningService()
        self.scheduler = scheduler or BandExperienceScheduler()
        self.chapter_planner = chapter_planner or ChapterExperiencePlanner()
        self.persistence = persistence or ExperiencePersistence()
        self.window_resolver = window_resolver or BandWindowResolver()
        self.trope_cost_ceiling = _normalize_trope_cost_ceiling(trope_cost_ceiling)

    def ensure_current_band_plan(
        self,
        *,
        session: Session,
        request: BandPlanningRequest,
    ) -> BandPlanningResult:
        window = self.window_resolver.resolve(
            chapter_plans=request.chapter_plans,
            activation_chapter=request.activation_chapter,
            detailed_band_size=request.detailed_band_size,
        )
        calibration = self.experience_service.build_audience_calibration_profile(
            session=session,
            project_id=request.project_id,
        )
        from forwin.experience.trope_cooldown import recent_trope_usage

        if hasattr(session, "execute"):
            recent_template_ids, recent_categories = recent_trope_usage(
                session,
                project_id=request.project_id,
            )
        else:
            recent_template_ids, recent_categories = [], []
        calibration.recent_template_ids = recent_template_ids
        calibration.recent_trope_categories = recent_categories
        if hasattr(session, "execute"):
            rules = active_progression_rules_for_chapter(
                session,
                project_id=request.project_id,
                chapter_number=window.chapter_start,
            )
            blocked_template_ids: list[str] = []
            blocked_categories: list[str] = []
            for rule in rules:
                if rule.rule_type not in {"trope_filter", "repetition_ban"}:
                    continue
                blocked_template_ids.extend(
                    str(item).strip()
                    for item in rule.payload.get("blocked_template_ids", [])
                    if str(item).strip()
                )
                blocked_categories.extend(
                    str(item).strip()
                    for item in rule.payload.get("blocked_categories", [])
                    if str(item).strip()
                )
            calibration.progression_blocked_template_ids = blocked_template_ids
            calibration.progression_blocked_categories = blocked_categories
        schedule = self.scheduler.derive_band_delight_schedule(
            band_id=window.band_id,
            chapter_start=window.chapter_start,
            chapter_end=window.chapter_end,
            structure=request.structure,
            arc_experience=request.arc_experience,
            active_band=window.active_band,
            calibration=calibration,
            cost_ceiling=self.trope_cost_ceiling,
        )
        role = classify_band_role(
            band_index=_band_index(window.chapter_start, request.detailed_band_size),
            total_bands=_total_bands(
                _target_total_from_project_or_plans(
                    session=session,
                    project_id=request.project_id,
                    chapter_plans=request.chapter_plans,
                ),
                request.detailed_band_size,
            ),
            target_total_chapters=_target_total_from_project_or_plans(
                session=session,
                project_id=request.project_id,
                chapter_plans=request.chapter_plans,
            ),
            last_chapter_of_band=window.chapter_end,
        )
        contract = contract_for_role(role.role)
        schedule = schedule.model_copy(
            update={
                "band_role": role.role.value,
                "band_role_reason": role.reason,
                "band_contract_template": contract.model_dump(mode="json"),
            }
        )
        activation_plan = self.subworld_manager.plan_band_activation(
            session=session,
            updater=StateUpdater(session),
            project_id=request.project_id,
            chapter_start=window.chapter_start,
            chapter_end=window.chapter_end,
            active_band=window.active_band,
        )
        schedule = schedule.model_copy(
            update={
                "active_subworld_ids": activation_plan.active_subworld_ids,
                "chapter_entry_targets": activation_plan.chapter_entry_targets,
            }
        )
        self.persistence.save_band_experience_plan(
            session=session,
            project_id=request.project_id,
            arc_id=request.arc_id,
            schedule=schedule,
        )
        if hasattr(self.persistence, "save_trope_usage_records"):
            self.persistence.save_trope_usage_records(
                session=session,
                project_id=request.project_id,
                arc_id=request.arc_id,
                schedule=schedule,
            )
        updated_numbers: list[int] = []
        for plan in window.active_band:
            experience_plan = self.chapter_planner.derive_chapter_experience_plan(
                chapter_number=plan.chapter_number,
                structure=request.structure,
                arc_experience=request.arc_experience,
                schedule=schedule,
                chapter_plan=plan,
                calibration=calibration,
            )
            chapter_targets = _chapter_entry_targets_for_plan(schedule=schedule, plan=plan)
            experience_plan = experience_plan.model_copy(
                update={
                    "active_subworld_ids": list(schedule.active_subworld_ids),
                    "chapter_entry_targets": chapter_targets,
                    "entity_admission_rule": "strict_named_character",
                }
            )
            self.persistence.save_chapter_experience_plan(
                chapter_plan=plan,
                experience_plan=experience_plan,
            )
            session.add(plan)
            updated_numbers.append(int(plan.chapter_number or 0))
        self.world_contract_service.ensure_for_arc_band(
            session=session,
            project_id=request.project_id,
            arc_id=request.arc_id,
            chapter_plans=request.chapter_plans,
            activation_chapter=request.activation_chapter,
            detailed_band_size=request.detailed_band_size,
        )
        session.flush()
        return BandPlanningResult(
            band_id=window.band_id,
            chapter_start=window.chapter_start,
            chapter_end=window.chapter_end,
            schedule=schedule,
            updated_chapter_numbers=updated_numbers,
        )


def _target_total_from_project_or_plans(
    *,
    session: Session,
    project_id: str,
    chapter_plans: list[ChapterPlan],
) -> int:
    project = session.get(Project, project_id)
    if project is not None and int(project.target_total_chapters or 0) > 0:
        return int(project.target_total_chapters or 0)
    return max([int(plan.chapter_number or 0) for plan in chapter_plans] or [0])


def _band_index(chapter_start: int, band_size: int) -> int:
    size = max(1, int(band_size or 1))
    return max(0, (int(chapter_start or 1) - 1) // size)


def _total_bands(target_total_chapters: int, band_size: int) -> int:
    target = int(target_total_chapters or 0)
    if target <= 0:
        return 0
    size = max(1, int(band_size or 1))
    return (target + size - 1) // size


def _normalize_trope_cost_ceiling(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 3


_ENTRY_TARGET_PATTERNS = [
    re.compile(r"(?:引入|介绍|接触|遇见|认识|结识)(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:作为|，|,|、|。|；|;|$)"),
    re.compile(r"与(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:接触|会面|交涉|交易|对话)"),
    re.compile(r"(?:让|使)(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:首次)?登场"),
    re.compile(r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,12})(?:首次)?登场"),
    re.compile(r"(?:查明|确认|发现|得知|获知)?(?P<name>(?:馆员|审计员|调度员|接线员|管理员|工程师)?[\u4e00-\u9fff·]{2,4})(?:存在|持有|掌握|暴露|揭示)"),
]

_ENTRY_TARGET_NON_NAMES = {
    "一个角色",
    "一名角色",
    "新角色",
    "新人物",
    "关键人物",
    "潜在信息源",
    "信息源",
    "线索",
    "新线索",
    "危机",
    "伏笔",
}


def _chapter_entry_targets_for_plan(*, schedule: object, plan: ChapterPlan) -> list[ChapterEntryTarget]:
    chapter_number = int(plan.chapter_number or 0)
    targets = [
        item for item in getattr(schedule, "chapter_entry_targets", []) if item.chapter_hint == chapter_number
    ]
    seen = {str(item.entity_name or "").strip() for item in targets if str(item.entity_name or "").strip()}
    subworld_ids = [str(item or "").strip() for item in getattr(schedule, "active_subworld_ids", []) if str(item or "").strip()]
    subworld_id = subworld_ids[0] if subworld_ids else ""
    for name in _infer_plan_entry_target_names(plan):
        if name in seen:
            continue
        targets.append(
            ChapterEntryTarget(
                chapter_hint=chapter_number,
                entity_name=name,
                subworld_id=subworld_id,
                role_hint="chapter_plan_named_entry",
            )
        )
        seen.add(name)
    return targets


def _infer_plan_entry_target_names(plan: ChapterPlan) -> list[str]:
    texts = [str(plan.title or ""), str(plan.one_line or "")]
    try:
        goals = json.loads(str(plan.goals_json or "[]"))
    except (TypeError, json.JSONDecodeError):
        goals = []
    texts.extend(str(item or "") for item in goals if str(item or "").strip())

    names: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for pattern in _ENTRY_TARGET_PATTERNS:
            for match in pattern.finditer(text):
                name = _normalize_entry_target_name(match.group("name"))
                if not name or name in seen:
                    continue
                names.append(name)
                seen.add(name)
    return names


def _normalize_entry_target_name(value: str) -> str:
    raw_name = str(value or "").strip(" \t\r\n：:，,。；;、")
    name = normalize_character_reference(raw_name)
    if not name or name in _ENTRY_TARGET_NON_NAMES:
        return ""
    if looks_like_technical_identifier(name):
        return ""
    if "的" in name or len(name) > 8:
        return ""
    if any(token in name for token in ("任务", "主线", "危机", "线索", "关系", "权限", "信息")):
        return ""
    return candidate_character_name(name)
