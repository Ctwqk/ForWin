from __future__ import annotations

from dataclasses import dataclass
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.director.arc_director import ArcDirector
from forwin.models import (
    ArcPlanVersion,
    ChapterDraft,
    ChapterPlan,
    ChapterTimeline,
    PlotThread,
    Project,
    ProjectReplanEvent,
    ProjectStageAnalysis,
    SignalWindowAggregate,
    StoryTimePoint,
    new_id,
)
from forwin.orchestrator.goals import load_goals_json
from forwin.orchestrator.thread_sampling import sample_active_threads
from forwin.protocol import SubWorldPlanDelta
from forwin.state.updater import StateUpdater
from forwin.subworld_manager import SubWorldManager


@dataclass(slots=True)
class PacingAssessment:
    risk_level: str
    verdict: str
    summary: str
    stale_threads: list[str]
    active_thread_count: int
    unresolved_thread_count: int
    recent_char_counts: list[int]
    recent_beat_count: int


@dataclass(slots=True)
class StageAssessment:
    stage_label: str
    progress_ratio: float
    timeline_label: str
    timeline_ordinal: int


class StageAnalyzer:
    def analyze(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
    ) -> StageAssessment:
        total = session.execute(
            select(func.count(ChapterPlan.id)).where(ChapterPlan.project_id == project_id)
        ).scalar_one()
        ratio = 0.0 if total <= 0 else min(1.0, chapter_number / max(total, 1))
        if ratio < 0.2:
            stage_label = "opening"
        elif ratio < 0.45:
            stage_label = "rising"
        elif ratio < 0.65:
            stage_label = "midpoint"
        elif ratio < 0.85:
            stage_label = "late"
        else:
            stage_label = "finale"

        timeline_row = session.execute(
            select(StoryTimePoint)
            .join(ChapterTimeline, ChapterTimeline.end_time_id == StoryTimePoint.id)
            .where(
                ChapterTimeline.project_id == project_id,
                ChapterTimeline.chapter_number == chapter_number,
            )
            .limit(1)
        ).scalar_one_or_none()
        if timeline_row is None:
            timeline_row = session.execute(
                select(StoryTimePoint)
                .where(StoryTimePoint.project_id == project_id)
                .order_by(StoryTimePoint.ordinal.desc())
                .limit(1)
            ).scalar_one_or_none()

        return StageAssessment(
            stage_label=stage_label,
            progress_ratio=ratio,
            timeline_label=timeline_row.label if timeline_row else "",
            timeline_ordinal=timeline_row.ordinal if timeline_row else 0,
        )


class PacingStrategist:
    def __init__(
        self,
        *,
        window_size: int = 3,
        stale_thread_window: int = 3,
        min_avg_chars: int = 1600,
        max_avg_chars: int = 3800,
        active_thread_limit: int = 20,
    ) -> None:
        self.window_size = max(window_size, 2)
        self.stale_thread_window = max(stale_thread_window, 2)
        self.min_avg_chars = max(300, int(min_avg_chars))
        self.max_avg_chars = max(self.min_avg_chars + 100, int(max_avg_chars))
        self.active_thread_limit = max(1, int(active_thread_limit))

    def analyze(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
    ) -> PacingAssessment:
        recent_rows = session.execute(
            select(ChapterDraft.char_count)
            .join(ChapterPlan, ChapterDraft.chapter_plan_id == ChapterPlan.id)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number <= chapter_number,
            )
            .order_by(ChapterPlan.chapter_number.desc())
            .limit(self.window_size)
        ).all()
        recent_char_counts = [int(row[0] or 0) for row in reversed(recent_rows)]

        sampled = sample_active_threads(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            limit=self.active_thread_limit,
            stale_window=self.stale_thread_window,
            recent_window=self.window_size,
        )
        threads = sampled.threads
        latest_beats = sampled.latest_beats
        stale_threads: list[str] = []
        recent_beat_count = 0
        for thread in threads:
            last_beat = latest_beats.get(thread.id)
            if last_beat is not None and last_beat.chapter_number >= max(
                1, chapter_number - self.window_size + 1
            ):
                recent_beat_count += 1
            last_active_chapter = (
                last_beat.chapter_number if last_beat is not None else thread.opened_at_chapter
            )
            if chapter_number - last_active_chapter >= self.stale_thread_window:
                stale_threads.append(thread.name)

        risk_level = "low"
        verdict = "steady"
        reasons: list[str] = []
        if stale_threads:
            risk_level = "high" if len(stale_threads) >= 2 else "medium"
            verdict = "stale_threads"
            reasons.append("关键线索长时间未推进")
        if recent_char_counts:
            avg_chars = sum(recent_char_counts) / len(recent_char_counts)
            if avg_chars < self.min_avg_chars:
                risk_level = "high"
                verdict = "compressed"
                reasons.append("近期章节长度偏短")
            elif avg_chars > self.max_avg_chars and risk_level == "low":
                risk_level = "medium"
                verdict = "overextended"
                reasons.append("近期章节长度偏长")
        if chapter_number >= self.window_size and recent_beat_count == 0 and threads:
            risk_level = "high"
            verdict = "thread_drift"
            reasons.append("最近窗口内没有主线 beat")
        if not reasons:
            reasons.append("最近章节推进均衡")

        # ── Audience pacing signal boost ──
        audience_pacing = session.execute(
            select(SignalWindowAggregate)
            .where(
                SignalWindowAggregate.project_id == project_id,
                SignalWindowAggregate.signal_type == "pacing",
                SignalWindowAggregate.window_type == "medium",
                SignalWindowAggregate.signal_level == "confirmed",
            )
            .order_by(SignalWindowAggregate.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if audience_pacing is not None:
            if risk_level == "low":
                risk_level = "medium"
            if verdict == "steady":
                verdict = "audience_pacing_concern"
            reasons.append(
                f"读者节奏反馈（{audience_pacing.signal_level}，"
                f"{audience_pacing.unique_user_count}人）"
            )

        return PacingAssessment(
            risk_level=risk_level,
            verdict=verdict,
            summary="；".join(reasons),
            stale_threads=stale_threads,
            active_thread_count=len(threads),
            unresolved_thread_count=len(stale_threads),
            recent_char_counts=recent_char_counts,
            recent_beat_count=recent_beat_count,
        )


class ReplanGovernor:
    def __init__(
        self,
        *,
        cooldown_chapters: int = 3,
        director: ArcDirector | None = None,
        subworld_manager: SubWorldManager | None = None,
    ) -> None:
        self.cooldown_chapters = max(cooldown_chapters, 1)
        self.director = director
        self.subworld_manager = subworld_manager or SubWorldManager(director=director)

    def apply_if_needed(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        stage: StageAssessment,
        pacing: PacingAssessment,
    ) -> ProjectReplanEvent | None:
        latest = session.execute(
            select(ProjectReplanEvent)
            .where(ProjectReplanEvent.project_id == project_id)
            .order_by(ProjectReplanEvent.trigger_chapter.desc(), ProjectReplanEvent.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if pacing.risk_level != "high":
            return None

        strategy = self._choose_strategy(stage=stage, pacing=pacing)

        if latest is not None and chapter_number < latest.cooldown_until_chapter:
            if latest.status == "cooldown":
                return None
            return self._record_event(
                session=session,
                project_id=project_id,
                chapter_number=chapter_number,
                pacing=pacing,
                strategy=latest.strategy if latest.strategy else strategy,
                status="cooldown",
                reason=f"replan 冷却中，需等待到第 {latest.cooldown_until_chapter} 章。",
            )

        focus_threads = pacing.stale_threads[:2]
        self._apply_strategy(
            strategy=strategy,
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            stage=stage,
            pacing=pacing,
            focus_threads=focus_threads,
        )
        return self._record_event(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            pacing=pacing,
            strategy=strategy,
            status="applied",
            reason=(
                f"执行 {strategy} 级 replan，基于 {stage.stage_label} 阶段分析，"
                f"优先回收线程：{', '.join(focus_threads) or '主线'}。"
            ),
            focus_threads=focus_threads,
        )

    def _record_event(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        pacing: PacingAssessment,
        strategy: str,
        status: str,
        reason: str,
        focus_threads: list[str] | None = None,
    ) -> ProjectReplanEvent:
        event = ProjectReplanEvent(
            id=new_id(),
            project_id=project_id,
            trigger_chapter=chapter_number,
            risk_level=pacing.risk_level,
            reason=reason,
            focus_threads_json=json.dumps(focus_threads or [], ensure_ascii=False),
            strategy=strategy,
            status=status,
            cooldown_until_chapter=chapter_number + self.cooldown_chapters,
        )
        session.add(event)
        session.flush()
        return event

    @staticmethod
    def _choose_strategy(
        *,
        stage: StageAssessment,
        pacing: PacingAssessment,
    ) -> str:
        if pacing.verdict in {"compressed", "overextended"}:
            return "patch"
        if pacing.verdict in {"stale_threads", "thread_drift"}:
            if len(pacing.stale_threads) <= 1 and stage.progress_ratio < 0.8:
                return "reband"
            return "rearc"
        return "rearc"

    def _apply_strategy(
        self,
        *,
        strategy: str,
        session: Session,
        project_id: str,
        chapter_number: int,
        stage: StageAssessment,
        pacing: PacingAssessment,
        focus_threads: list[str],
    ) -> None:
        if strategy == "patch":
            self._apply_patch(
                session=session,
                project_id=project_id,
                chapter_number=chapter_number,
                stage=stage,
                pacing=pacing,
                focus_threads=focus_threads,
            )
            return
        if strategy == "reband":
            self._apply_reband(
                session=session,
                project_id=project_id,
                chapter_number=chapter_number,
                stage=stage,
                focus_threads=focus_threads,
            )
            return
        self._apply_rearc(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            stage=stage,
            focus_threads=focus_threads,
        )

    def _apply_patch(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        stage: StageAssessment,
        pacing: PacingAssessment,
        focus_threads: list[str],
    ) -> None:
        next_plan = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number > chapter_number,
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .order_by(ChapterPlan.chapter_number.asc())
            .limit(1)
        ).scalar_one_or_none()
        if next_plan is None:
            return
        focus_note = "、".join(focus_threads) if focus_threads else "当前主线"
        goals = load_goals_json(next_plan.goals_json)
        patch_goal = (
            f"优先修补节奏风险：{pacing.verdict}，围绕{focus_note}提升有效推进。"
        )
        if patch_goal not in goals:
            goals.insert(0, patch_goal)
        next_plan.goals_json = json.dumps(goals[:4], ensure_ascii=False)
        next_plan.one_line = (
            f"[patch/{stage.stage_label}] 迅速压缩偏移段落，重新聚焦 {focus_note}。"
        )

    def _apply_reband(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        stage: StageAssessment,
        focus_threads: list[str],
    ) -> None:
        future_plans = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number > chapter_number,
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .order_by(ChapterPlan.chapter_number.asc())
            .limit(3)
        ).scalars().all()
        if not future_plans:
            return
        focus_note = "、".join(focus_threads) if focus_threads else "当前主线"
        templates = [
            f"立即回收线索：{focus_note}",
            f"放大回收结果带来的代价，强化 {focus_note} 的连锁反应",
            f"把 {focus_note} 重新并回主线阶段目标",
        ]
        for index, plan in enumerate(future_plans):
            goals = load_goals_json(plan.goals_json)
            goal = templates[min(index, len(templates) - 1)]
            if goal not in goals:
                goals.insert(0, goal)
            plan.goals_json = json.dumps(goals[:4], ensure_ascii=False)
            plan.one_line = (
                f"[reband/{stage.stage_label}] {templates[min(index, len(templates) - 1)]}"
            )

    def _apply_rearc(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        stage: StageAssessment,
        focus_threads: list[str],
    ) -> None:
        project = session.get(Project, project_id)
        active_arc = session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == project_id,
                ArcPlanVersion.status == "active",
            )
            .order_by(ArcPlanVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        base_synopsis = active_arc.arc_synopsis if active_arc else ""
        next_version = (active_arc.version if active_arc else 0) + 1
        if active_arc is not None:
            active_arc.status = "superseded"

        focus_note = "、".join(focus_threads) if focus_threads else "主线冲突"
        new_arc = ArcPlanVersion(
            id=new_id(),
            project_id=project_id,
            version=next_version,
            arc_synopsis=(
                f"{base_synopsis}\n\n[Phase3 replan v{next_version}] "
                f"阶段={stage.stage_label}，后续章节优先推进：{focus_note}。"
            ).strip(),
            status="active",
        )
        session.add(new_arc)
        session.flush()

        future_plans = session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number > chapter_number,
                ChapterPlan.status.in_(("planned", "failed")),
            )
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        if self.director is not None and project is not None:
            delta_payload = self.director.plan_subworld_delta(
                premise=project.premise,
                genre=project.genre,
                arc_synopsis=new_arc.arc_synopsis,
                chapter_seed=[
                    {
                        "chapter_number": plan.chapter_number,
                        "title": plan.title,
                        "one_line": plan.one_line,
                        "goals": load_goals_json(plan.goals_json),
                    }
                    for plan in future_plans[:4]
                ],
                existing_subworlds=self.subworld_manager.summarize_registry(session, project_id),
                focus_threads=focus_threads,
            )
            self.subworld_manager.apply_arc_delta(
                session=session,
                updater=StateUpdater(session),
                project_id=project_id,
                arc_id=new_arc.id,
                delta=SubWorldPlanDelta.model_validate(delta_payload),
                chapter_number=chapter_number + 1,
                entity_map={},
            )
        for plan in future_plans:
            plan.arc_plan_id = new_arc.id
            goals = load_goals_json(plan.goals_json)
            if focus_threads:
                extra_goal = f"推进线索：{'、'.join(focus_threads)}"
                if extra_goal not in goals:
                    goals.append(extra_goal)
            plan.goals_json = json.dumps(goals, ensure_ascii=False)
            prefix = f"[{stage.stage_label}] "
            if plan.one_line and not plan.one_line.startswith(prefix):
                plan.one_line = f"{prefix}{plan.one_line}"


def save_stage_analysis(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    stage: StageAssessment,
    pacing: PacingAssessment,
) -> ProjectStageAnalysis:
    row = ProjectStageAnalysis(
        id=new_id(),
        project_id=project_id,
        chapter_number=chapter_number,
        stage_label=stage.stage_label,
        progress_ratio=stage.progress_ratio,
        timeline_label=stage.timeline_label,
        timeline_ordinal=stage.timeline_ordinal,
        pacing_verdict=pacing.verdict,
        pacing_summary=pacing.summary,
        stale_threads_json=json.dumps(pacing.stale_threads, ensure_ascii=False),
        active_thread_count=pacing.active_thread_count,
        unresolved_thread_count=pacing.unresolved_thread_count,
    )
    session.add(row)
    session.flush()
    return row
