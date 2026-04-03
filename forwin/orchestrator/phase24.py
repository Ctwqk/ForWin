from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.director.arc_director import ArcDirector
from forwin.models import (
    ArcEnvelope,
    ArcEnvelopeAnalysis,
    ArcPlanVersion,
    ArcStructureDraft,
    ChapterPlan,
    Project,
    ProvisionalBandExecution,
    ProvisionalPromotionRecord,
    new_id,
)
from forwin.orchestrator.goals import load_goals_json

logger = logging.getLogger(__name__)


def _clamp_int(value: float | int, lower: int, upper: int) -> int:
    return max(lower, min(int(round(value)), upper))


@dataclass(slots=True)
class ArcPolicyTier:
    name: str
    ratio: float
    min_size: int
    max_size: int
    soft_min_ratio: float
    soft_max_ratio: float


@dataclass(slots=True)
class ArcStructureDraftData:
    phase_layout: list[str]
    key_beats: list[str]
    thread_priorities: list[dict[str, object]]
    hotspot_candidates: list[str]
    compression_candidates: list[str]


@dataclass(slots=True)
class ArcEnvelopeResolution:
    base_target_size: int
    base_soft_min: int
    base_soft_max: int
    resolved_target_size: int
    resolved_soft_min: int
    resolved_soft_max: int
    detailed_band_size: int
    frozen_zone_size: int
    recommendation: str
    evidence: list[str]
    expansion_signals: list[str]
    compression_signals: list[str]
    confidence: float
    based_on_band_id: str
    source_policy_tier: str


@dataclass(slots=True)
class ProvisionalBandPreview:
    band_id: str
    artifact_path: str
    aggregate_verdict: str
    preview_chapter_count: int
    total_char_count: int
    issue_count: int
    failure_count: int
    chapter_numbers: list[int]
    summary_lines: list[str]


_ARC_POLICY_TIERS = [
    ((1, 150), ArcPolicyTier("short", 0.18, 12, 24, 0.75, 1.25)),
    ((151, 400), ArcPolicyTier("medium", 0.15, 16, 30, 0.65, 1.50)),
    ((401, 800), ArcPolicyTier("long", 0.10, 20, 40, 0.55, 1.70)),
    ((801, 10**9), ArcPolicyTier("ultra-long", 0.08, 24, 48, 0.50, 2.00)),
]


def policy_for_total_chapters(total_chapters: int) -> ArcPolicyTier:
    total = max(1, int(total_chapters))
    for (lower, upper), policy in _ARC_POLICY_TIERS:
        if lower <= total <= upper:
            return policy
    return _ARC_POLICY_TIERS[0][1]


class ArcEnvelopeManager:
    def __init__(
        self,
        *,
        director: ArcDirector | None = None,
        provisional_executor: Any | None = None,
    ) -> None:
        self.director = director
        self.provisional_executor = provisional_executor

    def ensure_active_arc_resolution(
        self,
        *,
        session: Session,
        project_id: str,
        activation_chapter: int = 1,
    ) -> ArcEnvelope | None:
        active_arc = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
            .order_by(ArcPlanVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if active_arc is None:
            return None

        existing = session.execute(
            select(ArcEnvelope)
            .where(ArcEnvelope.arc_id == active_arc.id)
            .order_by(ArcEnvelope.updated_at.desc(), ArcEnvelope.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing.current_projected_size = max(
                existing.current_projected_size,
                min(existing.resolved_soft_max, max(activation_chapter, existing.resolved_target_size)),
            )
            return existing

        chapter_plans = session.execute(
            select(ChapterPlan)
            .where(ChapterPlan.arc_plan_id == active_arc.id)
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        total_chapter_count = len(
            session.execute(
                select(ChapterPlan.id).where(ChapterPlan.project_id == project_id)
            ).scalars().all()
        )
        project = session.get(Project, project_id)
        if project is None:
            return None

        policy = policy_for_total_chapters(total_chapter_count)
        base_target_size = _clamp_int(
            total_chapter_count * policy.ratio,
            policy.min_size,
            policy.max_size,
        )
        base_soft_min = max(1, int(round(base_target_size * policy.soft_min_ratio)))
        base_soft_max = max(base_target_size, int(round(base_target_size * policy.soft_max_ratio)))
        provisional_target = _clamp_int(base_target_size * 0.40, 4, 12)
        provisional_band = chapter_plans[: provisional_target]
        band_id = f"band:1:{provisional_target}"

        structure = self._build_structure_draft(
            project=project,
            total_chapters=total_chapter_count,
            chapter_plans=chapter_plans,
            policy=policy,
            base_target_size=base_target_size,
        )
        structure_row = ArcStructureDraft(
            id=new_id(),
            project_id=project_id,
            arc_id=active_arc.id,
            phase_layout_json=json.dumps(structure.phase_layout, ensure_ascii=False),
            key_beats_json=json.dumps(structure.key_beats, ensure_ascii=False),
            thread_priorities_json=json.dumps(structure.thread_priorities, ensure_ascii=False),
            hotspot_candidates_json=json.dumps(structure.hotspot_candidates, ensure_ascii=False),
            compression_candidates_json=json.dumps(structure.compression_candidates, ensure_ascii=False),
        )
        session.add(structure_row)
        session.flush()

        preview = self._execute_provisional_band(
            session=session,
            project_id=project_id,
            arc_id=active_arc.id,
            band_id=band_id,
            chapter_plans=provisional_band,
        )
        resolution = self._resolve_envelope(
            chapter_plans=chapter_plans,
            total_chapters=total_chapter_count,
            policy=policy,
            base_target_size=base_target_size,
            base_soft_min=base_soft_min,
            base_soft_max=base_soft_max,
            structure=structure,
            provisional_band=provisional_band,
            band_id=band_id,
            preview=preview,
        )
        envelope = ArcEnvelope(
            id=new_id(),
            project_id=project_id,
            arc_id=active_arc.id,
            base_target_size=resolution.base_target_size,
            base_soft_min=resolution.base_soft_min,
            base_soft_max=resolution.base_soft_max,
            resolved_target_size=resolution.resolved_target_size,
            resolved_soft_min=resolution.resolved_soft_min,
            resolved_soft_max=resolution.resolved_soft_max,
            detailed_band_size=resolution.detailed_band_size,
            frozen_zone_size=resolution.frozen_zone_size,
            current_projected_size=max(
                resolution.resolved_target_size,
                min(resolution.resolved_soft_max, max(activation_chapter, resolution.detailed_band_size)),
            ),
            current_confidence=resolution.confidence,
            source_policy_tier=resolution.source_policy_tier,
        )
        session.add(envelope)
        analysis = ArcEnvelopeAnalysis(
            id=new_id(),
            project_id=project_id,
            arc_id=active_arc.id,
            based_on_band_id=resolution.based_on_band_id,
            recommendation=resolution.recommendation,
            evidence_json=json.dumps(resolution.evidence, ensure_ascii=False),
            expansion_signals_json=json.dumps(resolution.expansion_signals, ensure_ascii=False),
            compression_signals_json=json.dumps(resolution.compression_signals, ensure_ascii=False),
            suggested_target=resolution.resolved_target_size,
            suggested_soft_min=resolution.resolved_soft_min,
            suggested_soft_max=resolution.resolved_soft_max,
            confidence=resolution.confidence,
        )
        session.add(analysis)
        if preview is not None:
            session.add(
                ProvisionalBandExecution(
                    id=new_id(),
                    project_id=project_id,
                    arc_id=active_arc.id,
                    band_id=preview.band_id,
                    chapter_numbers_json=json.dumps(preview.chapter_numbers, ensure_ascii=False),
                    artifact_path=preview.artifact_path,
                    aggregate_verdict=preview.aggregate_verdict,
                    preview_char_count=preview.total_char_count,
                    issue_count=preview.issue_count,
                    failure_count=preview.failure_count,
                )
            )
        session.flush()
        return envelope

    def backfill_missing_resolutions(self, *, session: Session) -> int:
        project_ids = session.execute(
            select(ArcPlanVersion.project_id)
            .where(ArcPlanVersion.status == "active")
            .distinct()
        ).scalars().all()
        created = 0
        original_director = self.director
        original_executor = self.provisional_executor
        self.director = None
        self.provisional_executor = None
        try:
            for project_id in project_ids:
                existing = session.execute(
                    select(ArcEnvelope.id)
                    .join(ArcPlanVersion, ArcPlanVersion.id == ArcEnvelope.arc_id)
                    .where(
                        ArcPlanVersion.project_id == project_id,
                        ArcPlanVersion.status == "active",
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if existing:
                    continue
                row = self.ensure_active_arc_resolution(
                    session=session,
                    project_id=project_id,
                    activation_chapter=1,
                )
                if row is not None:
                    created += 1
        finally:
            self.director = original_director
            self.provisional_executor = original_executor
        return created

    def record_provisional_promotion(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        reason: str = "accepted",
    ) -> ProvisionalPromotionRecord | None:
        chapter_plan = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
            .limit(1)
        ).scalar_one_or_none()
        if chapter_plan is None:
            return None
        envelope = session.execute(
            select(ArcEnvelope)
            .where(ArcEnvelope.arc_id == chapter_plan.arc_plan_id)
            .order_by(ArcEnvelope.updated_at.desc(), ArcEnvelope.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if envelope is None:
            return None
        band_index = max(1, ((chapter_number - 1) // max(1, envelope.detailed_band_size)) + 1)
        band_id = f"{chapter_plan.arc_plan_id}:band:{band_index}"
        analysis = session.execute(
            select(ArcEnvelopeAnalysis)
            .where(
                ArcEnvelopeAnalysis.arc_id == chapter_plan.arc_plan_id,
                ArcEnvelopeAnalysis.based_on_band_id == band_id,
            )
            .order_by(ArcEnvelopeAnalysis.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        existing = session.execute(
            select(ProvisionalPromotionRecord)
            .where(
                ProvisionalPromotionRecord.project_id == project_id,
                ProvisionalPromotionRecord.arc_id == chapter_plan.arc_plan_id,
                ProvisionalPromotionRecord.band_id == band_id,
            )
            .order_by(ProvisionalPromotionRecord.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            existing = ProvisionalPromotionRecord(
                id=new_id(),
                project_id=project_id,
                arc_id=chapter_plan.arc_plan_id,
                band_id=band_id,
                promoted_chapter_ids_json="[]",
                promotion_reason=reason,
                based_on_analysis_id=analysis.id if analysis else "",
            )
            session.add(existing)
            session.flush()
        try:
            promoted_ids = json.loads(existing.promoted_chapter_ids_json or "[]") or []
        except (json.JSONDecodeError, TypeError):
            promoted_ids = []
        chapter_id = chapter_plan.id
        if chapter_id not in promoted_ids:
            promoted_ids.append(chapter_id)
        existing.promoted_chapter_ids_json = json.dumps(promoted_ids, ensure_ascii=False)
        existing.promotion_reason = reason
        if analysis is not None:
            existing.based_on_analysis_id = analysis.id
        return existing

    def _build_structure_draft(
        self,
        *,
        project: Project,
        total_chapters: int,
        chapter_plans: list[ChapterPlan],
        policy: ArcPolicyTier,
        base_target_size: int,
    ) -> ArcStructureDraftData:
        chapter_seed = [
            {
                "chapter_number": plan.chapter_number,
                "title": plan.title,
                "one_line": plan.one_line,
                "goals": load_goals_json(plan.goals_json),
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
                )
            except Exception:
                drafted = {}
            if drafted:
                return ArcStructureDraftData(
                    phase_layout=[str(item) for item in drafted.get("phase_layout") or []],
                    key_beats=[str(item) for item in drafted.get("key_beats") or []],
                    thread_priorities=[
                        item for item in (drafted.get("thread_priorities") or []) if isinstance(item, dict)
                    ],
                    hotspot_candidates=[str(item) for item in drafted.get("hotspot_candidates") or []],
                    compression_candidates=[str(item) for item in drafted.get("compression_candidates") or []],
                )
        return ArcStructureDraftData(
            phase_layout=["setup", "pressure", "turn", "payoff"],
            key_beats=[
                plan.one_line or plan.title
                for plan in chapter_plans[: min(len(chapter_plans), 4)]
            ],
            thread_priorities=[
                {
                    "name": f"主线阶段-{index + 1}",
                    "priority": index + 1,
                    "reason": plan.one_line or plan.title,
                }
                for index, plan in enumerate(chapter_plans[:3])
            ],
            hotspot_candidates=[
                plan.title or plan.one_line
                for plan in chapter_plans[: min(len(chapter_plans), 3)]
            ],
            compression_candidates=[
                plan.title or plan.one_line
                for plan in chapter_plans[2:4]
                if plan.one_line
            ],
        )

    def _resolve_envelope(
        self,
        *,
        chapter_plans: list[ChapterPlan],
        total_chapters: int,
        policy: ArcPolicyTier,
        base_target_size: int,
        base_soft_min: int,
        base_soft_max: int,
        structure: ArcStructureDraftData,
        provisional_band: list[ChapterPlan],
        band_id: str,
        preview: ProvisionalBandPreview | None,
    ) -> ArcEnvelopeResolution:
        evidence = [
            f"policy={policy.name}",
            f"base_target={base_target_size}",
            f"provisional_band={len(provisional_band)}",
        ]
        expansion_signals: list[str] = []
        compression_signals: list[str] = []
        if len(structure.hotspot_candidates) >= 3:
            expansion_signals.append("热点候选密度高")
        if len(structure.key_beats) >= 4 and total_chapters >= 150:
            expansion_signals.append("中层结构显示多段推进")
        if len(structure.compression_candidates) >= 2:
            compression_signals.append("中段存在可压缩片段")
        if len(provisional_band) <= max(4, base_target_size // 3):
            compression_signals.append("近端预演带较短")
        if preview is not None:
            evidence.extend(
                [
                    f"provisional_verdict={preview.aggregate_verdict}",
                    f"provisional_char_count={preview.total_char_count}",
                    f"provisional_issue_count={preview.issue_count}",
                ]
            )
            if preview.aggregate_verdict == "pass":
                expansion_signals.append("provisional band 运行顺滑")
            elif preview.aggregate_verdict == "warn":
                evidence.append("provisional band 有轻微审查警告")
            else:
                compression_signals.append("provisional band 暴露不稳定点")
            if preview.failure_count:
                compression_signals.append("provisional band 出现生成失败")

        recommendation = "keep"
        resolved_target_size = base_target_size
        if len(expansion_signals) > len(compression_signals):
            recommendation = "expand"
            resolved_target_size = min(
                base_soft_max,
                max(base_target_size + 2, int(round(base_target_size * 1.2))),
            )
        elif len(compression_signals) > len(expansion_signals):
            recommendation = "compress"
            resolved_target_size = max(
                base_soft_min,
                min(base_target_size - 2, int(round(base_target_size * 0.85))),
            )

        analysis_payload: dict[str, Any] | None = None
        if self.director is not None:
            try:
                analysis_payload = self.director.analyze_arc_envelope(
                    total_chapters=total_chapters,
                    policy_tier=policy.name,
                    base_target_size=base_target_size,
                    base_soft_min=base_soft_min,
                    base_soft_max=base_soft_max,
                    structure_draft={
                        "phase_layout": structure.phase_layout,
                        "key_beats": structure.key_beats,
                        "thread_priorities": structure.thread_priorities,
                        "hotspot_candidates": structure.hotspot_candidates,
                        "compression_candidates": structure.compression_candidates,
                    },
                    provisional_band=[
                        {
                            "chapter_number": plan.chapter_number,
                            "title": plan.title,
                            "one_line": plan.one_line,
                            "goals": load_goals_json(plan.goals_json),
                        }
                        for plan in provisional_band
                    ],
                )
            except Exception:
                analysis_payload = None
        if isinstance(analysis_payload, dict):
            suggestion = str(analysis_payload.get("recommendation") or recommendation).strip().lower()
            if suggestion in {"keep", "expand", "compress"}:
                recommendation = suggestion
            expansion_signals = [
                str(item) for item in (analysis_payload.get("expansion_signals") or expansion_signals)
            ]
            compression_signals = [
                str(item) for item in (analysis_payload.get("compression_signals") or compression_signals)
            ]
            evidence = [str(item) for item in (analysis_payload.get("evidence") or evidence)]
            resolved_target_size = _clamp_int(
                int(analysis_payload.get("suggested_target") or resolved_target_size),
                base_soft_min,
                base_soft_max,
            )
            confidence = max(0.2, min(0.95, float(analysis_payload.get("confidence") or 0.65)))
        else:
            confidence = 0.65 if recommendation == "keep" else 0.72

        detailed_band_size = _clamp_int(resolved_target_size * 0.40, 4, 12)
        frozen_zone_size = _clamp_int(detailed_band_size * 0.35, 2, 4)
        resolved_soft_min = max(1, _clamp_int(resolved_target_size * policy.soft_min_ratio, 1, base_soft_max))
        resolved_soft_max = max(
            resolved_target_size,
            _clamp_int(resolved_target_size * policy.soft_max_ratio, resolved_target_size, max(base_soft_max, resolved_target_size)),
        )
        return ArcEnvelopeResolution(
            base_target_size=base_target_size,
            base_soft_min=base_soft_min,
            base_soft_max=base_soft_max,
            resolved_target_size=resolved_target_size,
            resolved_soft_min=resolved_soft_min,
            resolved_soft_max=resolved_soft_max,
            detailed_band_size=detailed_band_size,
            frozen_zone_size=frozen_zone_size,
            recommendation=recommendation,
            evidence=evidence,
            expansion_signals=expansion_signals,
            compression_signals=compression_signals,
            confidence=confidence,
            based_on_band_id=band_id,
            source_policy_tier=policy.name,
        )

    def _execute_provisional_band(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_plans: list[ChapterPlan],
    ) -> ProvisionalBandPreview | None:
        if self.provisional_executor is None or not chapter_plans:
            return None
        try:
            preview = self.provisional_executor(
                session=session,
                project_id=project_id,
                arc_id=arc_id,
                band_id=band_id,
                chapter_plans=chapter_plans,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Provisional band execution failed for %s/%s.", project_id, band_id, exc_info=True)
            return None
        if preview is None:
            return None
        if isinstance(preview, ProvisionalBandPreview):
            return preview
        if isinstance(preview, dict):
            return ProvisionalBandPreview(
                band_id=str(preview.get("band_id") or band_id),
                artifact_path=str(preview.get("artifact_path") or ""),
                aggregate_verdict=str(preview.get("aggregate_verdict") or "warn"),
                preview_chapter_count=int(preview.get("preview_chapter_count") or 0),
                total_char_count=int(preview.get("total_char_count") or 0),
                issue_count=int(preview.get("issue_count") or 0),
                failure_count=int(preview.get("failure_count") or 0),
                chapter_numbers=[int(item) for item in (preview.get("chapter_numbers") or [])],
                summary_lines=[str(item) for item in (preview.get("summary_lines") or [])],
            )
        return None
