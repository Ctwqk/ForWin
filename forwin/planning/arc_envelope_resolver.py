from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.arc_sizing import ArcPolicyTier, policy_for_total_chapters
from forwin.models import ArcEnvelope, ArcEnvelopeAnalysis, ArcPlanVersion, ChapterPlan, Project, new_id
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.planning.band_window import BandWindow, BandWindowResolver
from forwin.planning.provisional_preview_service import ProvisionalBandPreview
from forwin.protocol.scenario_rehearsal import ScenarioRehearsalRecommendation, ScenarioRehearsalReport


def _clamp_int(value: float | int, lower: int, upper: int) -> int:
    return max(lower, min(int(round(value)), upper))


def _coerce_unit_float(value: object, *, default: float) -> float:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"high", "strong", "certain", "confident"}:
            return 0.85
        if normalized in {"medium", "moderate", "managed"}:
            return 0.65
        if normalized in {"low", "weak", "uncertain"}:
            return 0.35
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _load_goals_json(raw: str) -> list[str]:
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in payload if str(item).strip()] if isinstance(payload, list) else []


@dataclass(slots=True)
class BaseEnvelopeContext:
    total_chapters: int
    policy: ArcPolicyTier
    base_target_size: int
    base_soft_min: int
    base_soft_max: int
    provisional_band_size: int
    provisional_window: BandWindow


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


class ArcEnvelopeResolver:
    def __init__(
        self,
        *,
        director: Any | None = None,
        window_resolver: BandWindowResolver | None = None,
    ) -> None:
        self.director = director
        self.window_resolver = window_resolver or BandWindowResolver()

    def build_base_context(
        self,
        *,
        session: Session,
        project: Project,
        active_arc: ArcPlanVersion,
        chapter_plans: list[ChapterPlan],
        activation_chapter: int,
    ) -> BaseEnvelopeContext:
        total_chapter_count = int(
            getattr(project, "target_total_chapters", 0)
            or session.execute(
                select(func.count(ChapterPlan.id)).where(ChapterPlan.project_id == project.id)
            ).scalar_one()
            or len(chapter_plans)
            or 0
        )
        policy = policy_for_total_chapters(total_chapter_count)
        persisted_target = max(0, int(getattr(active_arc, "planned_target_size", 0) or 0))
        persisted_soft_min = max(0, int(getattr(active_arc, "planned_soft_min", 0) or 0))
        persisted_soft_max = max(0, int(getattr(active_arc, "planned_soft_max", 0) or 0))
        if persisted_target > 0:
            base_target_size = persisted_target
            base_soft_min = persisted_soft_min or max(1, int(round(base_target_size * policy.soft_min_ratio)))
            base_soft_max = persisted_soft_max or max(base_target_size, int(round(base_target_size * policy.soft_max_ratio)))
        else:
            base_target_size = _clamp_int(total_chapter_count * policy.ratio, policy.min_size, policy.max_size)
            base_soft_min = max(1, int(round(base_target_size * policy.soft_min_ratio)))
            base_soft_max = max(base_target_size, int(round(base_target_size * policy.soft_max_ratio)))
        provisional_band_size = _clamp_int(base_target_size * 0.40, 4, 12)
        provisional_window = self.window_resolver.resolve(
            chapter_plans=chapter_plans,
            activation_chapter=activation_chapter,
            detailed_band_size=provisional_band_size,
        )
        return BaseEnvelopeContext(
            total_chapters=total_chapter_count,
            policy=policy,
            base_target_size=base_target_size,
            base_soft_min=base_soft_min,
            base_soft_max=base_soft_max,
            provisional_band_size=provisional_band_size,
            provisional_window=provisional_window,
        )

    def get_existing_envelope(
        self,
        *,
        session: Session,
        active_arc: ArcPlanVersion,
        activation_chapter: int,
    ) -> ArcEnvelope | None:
        existing = session.execute(
            select(ArcEnvelope)
            .where(ArcEnvelope.arc_id == active_arc.id)
            .order_by(ArcEnvelope.updated_at.desc(), ArcEnvelope.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            return None
        existing.current_projected_size = max(
            existing.current_projected_size,
            min(existing.resolved_soft_max, max(activation_chapter, existing.resolved_target_size)),
        )
        session.add(existing)
        session.flush()
        return existing

    def ensure_resolution(
        self,
        *,
        session: Session,
        project: Project,
        active_arc: ArcPlanVersion,
        chapter_plans: list[ChapterPlan],
        activation_chapter: int,
        structure: ArcStructureDraftData,
        rehearsal_report: ScenarioRehearsalReport | None,
        preview: ProvisionalBandPreview | None,
        base_context: BaseEnvelopeContext | None = None,
    ) -> ArcEnvelope:
        existing = self.get_existing_envelope(
            session=session,
            active_arc=active_arc,
            activation_chapter=activation_chapter,
        )
        if existing is not None:
            return existing
        context = base_context or self.build_base_context(
            session=session,
            project=project,
            active_arc=active_arc,
            chapter_plans=chapter_plans,
            activation_chapter=activation_chapter,
        )
        resolution = self._resolve_envelope(
            chapter_plans=chapter_plans,
            total_chapters=context.total_chapters,
            policy=context.policy,
            base_target_size=context.base_target_size,
            base_soft_min=context.base_soft_min,
            base_soft_max=context.base_soft_max,
            structure=structure,
            provisional_band=context.provisional_window.active_band,
            band_id=context.provisional_window.band_id,
            preview=preview,
            rehearsal=rehearsal_report,
        )
        envelope = ArcEnvelope(
            id=new_id(),
            project_id=project.id,
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
        session.add(
            ArcEnvelopeAnalysis(
                id=new_id(),
                project_id=project.id,
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
        )
        session.flush()
        return envelope

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
        rehearsal: ScenarioRehearsalReport | None = None,
    ) -> ArcEnvelopeResolution:
        evidence = [f"policy={policy.name}", f"base_target={base_target_size}", f"scenario_band={len(provisional_band)}"]
        expansion_signals: list[str] = []
        compression_signals: list[str] = []
        if len(structure.hotspot_candidates) >= 3:
            expansion_signals.append("热点候选密度高")
        if len(structure.key_beats) >= 4 and total_chapters >= 150:
            expansion_signals.append("中层结构显示多段推进")
        if len(structure.compression_candidates) >= 2:
            compression_signals.append("中段存在可压缩片段")
        if len(provisional_band) <= max(4, base_target_size // 3):
            compression_signals.append("近端 rehearsal band 较短")
        if rehearsal is not None:
            evidence.extend(
                [
                    f"scenario_rehearsal={rehearsal.recommendation.value}",
                    f"scenario_risk_count={len(rehearsal.risk_findings)}",
                    f"scenario_patch_count={len(rehearsal.required_plan_patches)}",
                ]
            )
            if rehearsal.recommendation == ScenarioRehearsalRecommendation.PASS:
                expansion_signals.append("scenario rehearsal 通过")
            elif rehearsal.recommendation == ScenarioRehearsalRecommendation.PATCH:
                evidence.append("scenario rehearsal 要求 plan patch")
            elif rehearsal.recommendation in {
                ScenarioRehearsalRecommendation.REPLAN,
                ScenarioRehearsalRecommendation.BLOCK,
            }:
                compression_signals.append("scenario rehearsal 暴露高风险结构问题")
            for finding in rehearsal.risk_findings:
                if finding.severity == "fail":
                    compression_signals.append(f"scenario blocker: {finding.risk_type}")
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
            resolved_target_size = min(base_soft_max, max(base_target_size + 2, int(round(base_target_size * 1.2))))
        elif len(compression_signals) > len(expansion_signals):
            recommendation = "compress"
            resolved_target_size = max(base_soft_min, min(base_target_size - 2, int(round(base_target_size * 0.85))))

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
                            "goals": _load_goals_json(plan.goals_json),
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
            expansion_signals = [str(item) for item in (analysis_payload.get("expansion_signals") or expansion_signals)]
            compression_signals = [str(item) for item in (analysis_payload.get("compression_signals") or compression_signals)]
            evidence = [str(item) for item in (analysis_payload.get("evidence") or evidence)]
            resolved_target_size = _clamp_int(
                int(analysis_payload.get("suggested_target") or resolved_target_size),
                base_soft_min,
                base_soft_max,
            )
            confidence = max(0.2, min(0.95, _coerce_unit_float(analysis_payload.get("confidence"), default=0.65)))
        else:
            confidence = 0.65 if recommendation == "keep" else 0.72

        detailed_band_size = _clamp_int(resolved_target_size * 0.40, 4, 12)
        frozen_zone_size = _clamp_int(detailed_band_size * 0.35, 2, 4)
        resolved_soft_min = max(1, _clamp_int(resolved_target_size * policy.soft_min_ratio, 1, base_soft_max))
        resolved_soft_max = max(
            resolved_target_size,
            _clamp_int(
                resolved_target_size * policy.soft_max_ratio,
                resolved_target_size,
                max(base_soft_max, resolved_target_size),
            ),
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
