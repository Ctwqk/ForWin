from __future__ import annotations

from sqlalchemy import inspect

from forwin.book_state import BookStateRepository
from forwin.context.assembler_core import _build_canon_quality_context
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.planning.future_plan_audit import (
    FuturePlanAuditIssue,
    FuturePlanAuditRepository,
    FuturePlanAuditRun,
)
from forwin.protocol.book_state import WorldNode
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
                            issue_type="obligation_missing_from_future_plan",
                            severity="error",
                            target_chapter=23,
                            target_plan_id="plan-23",
                            description="future plan lacks obligation binding",
                            patch_type="obligation_plan_binding",
                            evidence_refs=["chapter_plan:23:one_line"],
                        )
                    ],
                    applied_plan_patch_ids=["patch-23"],
                    blocking_reasons=["obligation_missing_from_future_plan:23"],
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
            assert recent[1].issues[0].issue_type == "obligation_missing_from_future_plan"
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


def test_canon_quality_context_projects_book_state_rule_as_immutable_invariant() -> None:
    engine = get_engine(postgres_test_url("future-plan-audit-book-state-rule-context"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(
                title="规则上下文",
                premise="测试",
                genre="科幻",
                target_total_chapters=36,
            )
            session.add(project)
            session.flush()
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="rule-transit-protocol",
                    project_id=project.id,
                    node_type="rule",
                    name="通行协议",
                    description="门禁协议的公开版本。",
                    created_at_chapter=5,
                    source_refs=["chapter:5"],
                    profile={
                        "trigger_condition": "三枚印记同时点亮",
                        "effect_description": "门扉只向右移动一格",
                        "public_version": "三印同亮，门右移一格。",
                        "hidden_version_fact_id": "fact-secret-backdoor",
                    },
                    state={"status": "active"},
                ),
                version_chapter=5,
            )
            session.commit()

            context = _build_canon_quality_context(
                session=session,
                project_id=project.id,
                chapter_number=6,
                target_total_chapters=36,
            )

        invariant = next(
            item
            for item in context["invariant_constraints"]
            if item["invariant_key"] == "book_state_rule:rule-transit-protocol"
        )
        assert invariant["kind"] == "active_rule"
        assert invariant["label"] == "通行协议"
        assert invariant["current_value"]["public_version"] == "三印同亮，门右移一格。"
        assert "hidden_version_fact_id" not in invariant["current_value"]
        assert invariant["constraints"]["immutable_definition"] is True
        assert invariant["evidence_refs"] == ["chapter:5"]
    finally:
        engine.dispose()


