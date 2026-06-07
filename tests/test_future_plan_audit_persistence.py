from __future__ import annotations

from sqlalchemy import inspect

from forwin.context.assembler import _build_canon_quality_context
from forwin.models import (
    CandidateDraftRecord,
    ChapterDraft,
    ChapterPlan,
    ChapterReview,
    CharacterStateTransitionRow,
    CountdownLedgerRow,
    ArcPlanVersion,
    Project,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.planning.future_plan_auditor import (
    FuturePlanAuditIssue,
    FuturePlanAuditRepository,
    FuturePlanAuditRun,
)
from tests.postgres import postgres_test_url


def test_future_plan_audit_run_persists_pass_and_fail_results() -> None:
    engine = get_engine(postgres_test_url("future-plan-audit-persistence"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="未来计划审计", premise="测试", genre="悬疑")
            session.add(project)
            session.flush()
            repo = FuturePlanAuditRepository(session)
            fail_run = repo.save_run(
                FuturePlanAuditRun(
                    project_id=project.id,
                    current_chapter=22,
                    trigger_stage="post_acceptance",
                    inspected_chapters=[23],
                    status="fail",
                    issues=[
                        FuturePlanAuditIssue(
                            issue_type="countdown_future_plan_conflict",
                            severity="error",
                            target_chapter=23,
                            target_plan_id="plan-23",
                            description="future plan stale",
                            evidence_refs=["chapter_plan:23:one_line"],
                        )
                    ],
                    applied_plan_patch_ids=["patch-23"],
                    blocking_reasons=["countdown_future_plan_conflict:23"],
                )
            )
            pass_run = repo.save_run(
                FuturePlanAuditRun(
                    project_id=project.id,
                    current_chapter=23,
                    trigger_stage="post_acceptance",
                    inspected_chapters=[24],
                    status="pass",
                )
            )
            session.commit()

        with session_factory() as session:
            repo = FuturePlanAuditRepository(session)
            recent = repo.list_recent(project.id, limit=5)
            assert [row.id for row in recent] == [pass_run.id, fail_run.id]
            assert recent[1].issues[0].issue_type == "countdown_future_plan_conflict"
            assert recent[1].applied_plan_patch_ids == ["patch-23"]
    finally:
        engine.dispose()


def test_init_db_creates_future_plan_audit_runs_table() -> None:
    engine = get_engine(postgres_test_url("future-plan-audit-table"))
    init_db(engine)
    try:
        names = set(inspect(engine).get_table_names())
        assert "future_plan_audit_runs" in names
    finally:
        engine.dispose()


def test_canon_quality_context_keeps_string_false_countdown_observations_without_stale_main() -> None:
    engine = get_engine(postgres_test_url("future-plan-audit-countdown-context"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="倒计时上下文", premise="测试", genre="悬疑", target_total_chapters=60)
            session.add(project)
            session.flush()
            session.add(
                CountdownLedgerRow(
                    project_id=project.id,
                    countdown_key="memory_reset",
                    label="memory_reset",
                    chapter_number=22,
                    normalized_remaining_minutes=90,
                    raw_mention="九十分钟",
                    is_resolution_event="false",
                    status="resolved",
                )
            )
            session.add(
                CountdownLedgerRow(
                    project_id=project.id,
                    countdown_key="main",
                    label="main",
                    chapter_number=16,
                    normalized_remaining_minutes=9,
                    raw_mention="九分钟",
                    is_resolution_event="false",
                    status="consistent",
                )
            )
            session.commit()

            context = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=23,
                target_total_chapters=60,
            )

        constraints = {
            item["countdown_key"]: item["latest_remaining_minutes"]
            for item in context["countdown_constraints"]
        }
        assert constraints == {"memory_reset": 90}
    finally:
        engine.dispose()


def test_canon_quality_context_injects_latest_custody_state_constraints() -> None:
    engine = get_engine(postgres_test_url("future-plan-audit-character-context"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="角色状态上下文", premise="测试", genre="悬疑", target_total_chapters=36)
            session.add(project)
            session.flush()
            session.add(
                CharacterStateTransitionRow(
                    project_id=project.id,
                    character_name="韩青",
                    chapter_number=31,
                    transition_type="custody_state",
                    to_state="free",
                    terminality="none",
                    can_participate="true",
                    evidence_refs_json='["chapter:31"]',
                    payload_json="{}",
                )
            )
            session.commit()

            context = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=32,
                target_total_chapters=36,
            )

        constraints = context["character_state_constraints"]
        assert constraints == [
            {
                "character_name": "韩青",
                "transition_type": "custody_state",
                "latest_state": "free",
                "latest_chapter": 31,
                "can_participate": True,
                "evidence_refs": ["chapter:31"],
            }
        ]
    finally:
        engine.dispose()


def test_canon_quality_context_infers_recent_canon_release_without_transition_row() -> None:
    engine = get_engine(postgres_test_url("future-plan-audit-recent-release-context"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="最近正文释放", premise="主角：陆明。", genre="悬疑", target_total_chapters=36)
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_number=1,
                chapter_start=25,
                chapter_end=36,
                arc_synopsis="finale",
            )
            session.add(arc)
            session.flush()
            plan = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=31,
                title="第31章",
                status="accepted",
            )
            session.add(plan)
            session.flush()
            draft = ChapterDraft(
                chapter_plan_id=plan.id,
                version=1,
                summary="陆明利用父亲留下的硬件后门救出被关押的韩青。",
                body_text="陆明救出被关押的韩青，两人在地下检修线汇合。",
            )
            session.add(draft)
            session.flush()
            review = ChapterReview(draft_id=draft.id, verdict="pass")
            session.add(review)
            session.flush()
            session.add(
                CandidateDraftRecord(
                    project_id=project.id,
                    chapter_plan_id=plan.id,
                    chapter_number=31,
                    candidate_draft_id=draft.id,
                    review_id=review.id,
                    version=1,
                    status="canon_committed",
                    canon_status="canon",
                )
            )
            session.commit()

            context = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=32,
                target_total_chapters=36,
            )

        constraints = context["character_state_constraints"]
        assert constraints == [
            {
                "character_name": "韩青",
                "transition_type": "custody_state",
                "latest_state": "free",
                "latest_chapter": 31,
                "can_participate": True,
                "evidence_refs": ["recent_canon:31"],
            }
        ]
    finally:
        engine.dispose()


def test_canon_quality_context_keeps_closed_countdown_constraints() -> None:
    engine = get_engine(postgres_test_url("future-plan-audit-closed-countdown-context"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="关闭倒计时上下文", premise="测试", genre="悬疑", target_total_chapters=36)
            session.add(project)
            session.flush()
            session.add(
                CountdownLedgerRow(
                    project_id=project.id,
                    countdown_key="terminal_audit_window",
                    label="终端审计窗口",
                    chapter_number=31,
                    normalized_remaining_minutes=0,
                    raw_mention="窗口归零",
                    is_resolution_event="true",
                    status="resolved",
                )
            )
            session.commit()

            context = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=32,
                target_total_chapters=36,
            )

        assert context["countdown_constraints"] == [
            {
                "countdown_key": "terminal_audit_window",
                "label": "终端审计窗口",
                "latest_remaining_minutes": 0,
                "latest_chapter": 31,
                "raw_mention": "窗口归零",
                "status": "resolved",
            }
        ]
    finally:
        engine.dispose()


def test_canon_quality_context_projects_countdowns_to_invariant_constraints() -> None:
    engine = get_engine(postgres_test_url("future-plan-audit-invariant-context"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="强状态上下文", premise="测试", genre="悬疑", target_total_chapters=36)
            session.add(project)
            session.flush()
            session.add(
                CountdownLedgerRow(
                    project_id=project.id,
                    countdown_key="audit_window",
                    label="审计窗口",
                    chapter_number=12,
                    normalized_remaining_minutes=18,
                    raw_mention="审计窗口还剩十八分钟",
                    status="consistent",
                )
            )
            session.commit()

            context = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=13,
                target_total_chapters=36,
            )

        assert context["invariant_constraints"] == [
            {
                "invariant_key": "countdown:audit_window",
                "kind": "monotonic_numeric",
                "subject_key": "audit_window",
                "label": "审计窗口",
                "current_value": 18,
                "value_unit": "minutes",
                "status": "active",
                "latest_chapter": 12,
                "constraints": {
                    "monotonic": True,
                    "cannot_increase_without_bridge": True,
                    "raw_mention": "审计窗口还剩十八分钟",
                },
                "allowed_bridges": ["reset", "reopened", "branch_clock"],
            }
        ]
    finally:
        engine.dispose()


def test_canon_quality_context_drops_stale_generic_main_when_memory_reset_is_active() -> None:
    engine = get_engine(postgres_test_url("future-plan-audit-drop-stale-main-context"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="主线倒计时去重", premise="测试", genre="悬疑", target_total_chapters=36)
            session.add(project)
            session.flush()
            session.add_all(
                [
                    CountdownLedgerRow(
                        project_id=project.id,
                        countdown_key="main",
                        label="main",
                        chapter_number=15,
                        normalized_remaining_minutes=9,
                        raw_mention="九分钟",
                        status="consistent",
                    ),
                    CountdownLedgerRow(
                        project_id=project.id,
                        countdown_key="memory_reset",
                        label="memory_reset",
                        chapter_number=31,
                        normalized_remaining_minutes=44,
                        raw_mention="44分钟",
                        status="consistent",
                    ),
                ]
            )
            session.commit()

            context = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=32,
                target_total_chapters=36,
            )

        assert context["countdown_constraints"] == [
            {
                "countdown_key": "memory_reset",
                "label": "memory_reset",
                "latest_remaining_minutes": 44,
                "latest_chapter": 31,
                "raw_mention": "44分钟",
                "status": "consistent",
            }
        ]
    finally:
        engine.dispose()
