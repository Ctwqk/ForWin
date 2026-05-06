from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.arc_sizing import ArcPolicyTier
from forwin.models import ArcPlanVersion, ArcStructureDraft, ChapterPlan, Project, new_id
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise


def _json_list(raw: str, default: list[Any] | None = None) -> list[Any]:
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return list(default or [])
    return payload if isinstance(payload, list) else list(default or [])


def _json_object(raw: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return dict(default or {})
    return payload if isinstance(payload, dict) else dict(default or {})


def _load_goals_json(raw: str) -> list[str]:
    values = _json_list(raw)
    return [str(item) for item in values if str(item).strip()]


@dataclass(slots=True)
class ArcStructureDraftData:
    phase_layout: list[str]
    key_beats: list[str]
    thread_priorities: list[dict[str, object]]
    hotspot_candidates: list[str]
    compression_candidates: list[str]


@dataclass(slots=True)
class ArcStructurePlanningResult:
    structure: ArcStructureDraftData
    row: ArcStructureDraft
    experience_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ArcPlanningBundle:
    structure: ArcStructureDraftData
    reader_promise: ReaderPromise
    arc_payoff_map: ArcPayoffMap


class ArcStructurePlanningService:
    def __init__(self, *, director: Any | None = None) -> None:
        self.director = director

    def load_latest(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
    ) -> ArcStructurePlanningResult | None:
        row = session.execute(
            select(ArcStructureDraft)
            .where(
                ArcStructureDraft.project_id == project_id,
                ArcStructureDraft.arc_id == arc_id,
            )
            .order_by(ArcStructureDraft.created_at.desc(), ArcStructureDraft.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return ArcStructurePlanningResult(
            structure=self.from_row(row),
            row=row,
            experience_payload={
                "reader_promise": _json_object(row.reader_promise_json),
                "arc_payoff_map": _json_object(row.arc_payoff_map_json),
            },
        )

    def ensure_structure(
        self,
        *,
        session: Session,
        project: Project,
        active_arc: ArcPlanVersion,
        total_chapters: int,
        policy: ArcPolicyTier,
        base_target_size: int,
        chapter_plans: list[ChapterPlan],
        audience_trends: list[str],
    ) -> ArcStructurePlanningResult:
        existing = self.load_latest(
            session=session,
            project_id=project.id,
            arc_id=active_arc.id,
        )
        if existing is not None:
            return existing

        drafted = self._draft_payload(
            project=project,
            total_chapters=total_chapters,
            policy=policy,
            base_target_size=base_target_size,
            chapter_plans=chapter_plans,
            audience_trends=audience_trends,
        )
        structure = self._structure_from_payload(
            drafted,
            project=project,
            chapter_plans=chapter_plans,
        )
        row = ArcStructureDraft(
            id=new_id(),
            project_id=project.id,
            arc_id=active_arc.id,
            phase_layout_json=json.dumps(structure.phase_layout, ensure_ascii=False),
            key_beats_json=json.dumps(structure.key_beats, ensure_ascii=False),
            thread_priorities_json=json.dumps(structure.thread_priorities, ensure_ascii=False),
            hotspot_candidates_json=json.dumps(structure.hotspot_candidates, ensure_ascii=False),
            compression_candidates_json=json.dumps(structure.compression_candidates, ensure_ascii=False),
            reader_promise_json="{}",
            arc_payoff_map_json="{}",
        )
        session.add(row)
        session.flush()
        return ArcStructurePlanningResult(
            structure=structure,
            row=row,
            experience_payload={
                key: drafted.get(key)
                for key in ("reader_promise", "arc_payoff_map")
                if isinstance(drafted.get(key), dict)
            },
        )

    def build_structure_draft(
        self,
        *,
        project: Project,
        total_chapters: int,
        chapter_plans: list[ChapterPlan],
        policy: ArcPolicyTier,
        base_target_size: int,
        audience_trends: list[str] | None = None,
    ) -> tuple[ArcStructureDraftData, dict[str, Any]]:
        drafted = self._draft_payload(
            project=project,
            total_chapters=total_chapters,
            policy=policy,
            base_target_size=base_target_size,
            chapter_plans=chapter_plans,
            audience_trends=audience_trends or [],
        )
        return (
            self._structure_from_payload(
                drafted,
                project=project,
                chapter_plans=chapter_plans,
            ),
            drafted,
        )

    @staticmethod
    def from_row(row: ArcStructureDraft) -> ArcStructureDraftData:
        return ArcStructureDraftData(
            phase_layout=[str(item) for item in _json_list(row.phase_layout_json)],
            key_beats=[str(item) for item in _json_list(row.key_beats_json)],
            thread_priorities=[
                item for item in _json_list(row.thread_priorities_json) if isinstance(item, dict)
            ],
            hotspot_candidates=[str(item) for item in _json_list(row.hotspot_candidates_json)],
            compression_candidates=[str(item) for item in _json_list(row.compression_candidates_json)],
        )

    def _draft_payload(
        self,
        *,
        project: Project,
        total_chapters: int,
        policy: ArcPolicyTier,
        base_target_size: int,
        chapter_plans: list[ChapterPlan],
        audience_trends: list[str],
    ) -> dict[str, Any]:
        chapter_seed = [
            {
                "chapter_number": plan.chapter_number,
                "title": plan.title,
                "one_line": plan.one_line,
                "goals": _load_goals_json(plan.goals_json),
            }
            for plan in chapter_plans[: min(len(chapter_plans), 8)]
        ]
        if self.director is not None:
            try:
                drafted = self.director.draft_arc_structure(
                    premise=project.premise,
                    genre=project.genre,
                    total_chapters=total_chapters,
                    policy_tier=policy.name,
                    base_target_size=base_target_size,
                    chapter_seed=chapter_seed,
                    audience_trends=audience_trends,
                )
            except Exception:
                drafted = {}
            if isinstance(drafted, dict) and drafted:
                return drafted
        return self._fallback_payload(project=project, chapter_plans=chapter_plans)

    @staticmethod
    def _structure_from_payload(
        payload: dict[str, Any],
        *,
        project: Project,
        chapter_plans: list[ChapterPlan],
    ) -> ArcStructureDraftData:
        fallback = ArcStructurePlanningService._fallback_payload(
            project=project,
            chapter_plans=chapter_plans,
        )
        source = payload or fallback
        return ArcStructureDraftData(
            phase_layout=[str(item) for item in source.get("phase_layout") or fallback["phase_layout"]],
            key_beats=[str(item) for item in source.get("key_beats") or fallback["key_beats"]],
            thread_priorities=[
                item
                for item in (source.get("thread_priorities") or fallback["thread_priorities"])
                if isinstance(item, dict)
            ],
            hotspot_candidates=[
                str(item) for item in source.get("hotspot_candidates") or fallback["hotspot_candidates"]
            ],
            compression_candidates=[
                str(item)
                for item in source.get("compression_candidates") or fallback["compression_candidates"]
            ],
        )

    @staticmethod
    def _fallback_payload(
        *,
        project: Project,
        chapter_plans: list[ChapterPlan],
    ) -> dict[str, Any]:
        return {
            "phase_layout": ["setup", "pressure", "turn", "payoff"],
            "key_beats": [
                plan.one_line or plan.title
                for plan in chapter_plans[: min(len(chapter_plans), 4)]
            ],
            "thread_priorities": [
                {
                    "name": f"主线阶段-{index + 1}",
                    "priority": index + 1,
                    "reason": plan.one_line or plan.title,
                }
                for index, plan in enumerate(chapter_plans[:3])
            ],
            "hotspot_candidates": [
                plan.title or plan.one_line
                for plan in chapter_plans[: min(len(chapter_plans), 3)]
            ],
            "compression_candidates": [
                plan.title or plan.one_line
                for plan in chapter_plans[2:4]
                if plan.one_line
            ],
            "reader_promise": {
                "genre_promise": f"{project.genre}网文",
                "pleasure_promise": f"{project.genre}读者会稳定获得悬念和回报",
                "core_pleasures": ["稳定微回报", "阶段性翻盘", "真相逐层揭开"],
                "acceptable_drag_level": "low",
                "acceptable_exposition_density": "medium",
                "cliffhanger_aggressiveness": "high",
                "ambiguity_mode": "managed",
                "world_legibility_target": "关键冲突的规则与代价必须能被读者读懂。",
            },
            "arc_payoff_map": {
                "macro_payoffs": [],
                "awe_kit": ["反转", "线索揭面", "代价升级"],
                "revelation_layers": [],
                "ambiguity_constraints": ["关键结果必须能回指既有线索与规则。"],
            },
        }
