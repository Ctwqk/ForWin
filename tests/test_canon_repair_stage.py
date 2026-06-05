from __future__ import annotations

import importlib
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.models import base as base_module
from forwin.models.base import Base
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.phase import ChapterRewriteAttempt
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.project_ops.reviews import get_chapter_review
from forwin.orchestrator_loop_core.repair_loop import _attempts_for_repair_phase


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _noop_decision_refs(*args, **kwargs):
    return []


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, object] | None] = []

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params.append(params)


class _FakeEngine:
    def __init__(self, conn: _RecordingConnection) -> None:
        self.conn = conn

    def begin(self):
        return self

    def __enter__(self) -> _RecordingConnection:
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _RecordingAlembicOp:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.add_column_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.create_index_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def add_column(self, *args, **kwargs) -> None:
        self.add_column_calls.append((args, kwargs))

    def create_index(self, *args, **kwargs) -> None:
        self.create_index_calls.append((args, kwargs))

    def execute(self, statement) -> None:
        self.statements.append(str(statement))


def _rewrite_attempt_phase_migration():
    return importlib.import_module(
        "forwin.migrations.versions.0019_chapter_rewrite_attempt_phase"
    )


def test_rewrite_attempt_phase_fields_are_serialized_in_review_detail():
    Session = _session_factory()
    session = Session()
    try:
        project = Project(id="p", title="测试项目", genre="玄幻", premise="premise")
        arc = ArcPlanVersion(id="arc1", project_id="p", arc_synopsis="arc")
        plan = ChapterPlan(
            id="cp1",
            project_id="p",
            arc_plan_id="arc1",
            chapter_number=1,
            title="第一章",
            one_line="开场",
            goals_json="[]",
            status="needs_review",
            repair_attempt_count=2,
        )
        draft = ChapterDraft(
            id="d1",
            chapter_plan_id="cp1",
            version=1,
            body_text="正文" * 100,
            summary="summary",
            char_count=200,
        )
        review = ChapterReview(
            id="r1",
            draft_id="d1",
            verdict="warn",
            issues_json="[]",
            review_meta_json=json.dumps({"review_summary": "warn"}, ensure_ascii=False),
        )
        attempt = ChapterRewriteAttempt(
            id="a1",
            project_id="p",
            chapter_number=1,
            attempt_no=2,
            repair_phase="canon_repair",
            phase_attempt_no=1,
            trigger_review_id="r1",
            repair_scope="draft",
            design_patch_json="{}",
            source_draft_id="d1",
            result_draft_id="d1",
            result_verdict="warn",
            result_review_id="r1",
        )
        session.add_all([project, arc, plan, draft, review, attempt])
        session.commit()
    finally:
        session.close()

    detail = get_chapter_review(
        "p",
        1,
        get_session=Session,
        decision_refs_for_chapter_review=_noop_decision_refs,
    )

    assert detail.rewrite_attempts[0].attempt_no == 2
    assert detail.rewrite_attempts[0].repair_phase == "canon_repair"
    assert detail.rewrite_attempts[0].phase_attempt_no == 1


def test_runtime_upgrade_helper_backfills_rewrite_attempt_phase_columns():
    conn = _RecordingConnection()

    base_module._upgrade_chapter_rewrite_attempt_phase(conn)

    statements = "\n".join(conn.statements)
    assert (
        "ALTER TABLE chapter_rewrite_attempts "
        "ADD COLUMN IF NOT EXISTS repair_phase VARCHAR NOT NULL DEFAULT 'review_repair'"
    ) in statements
    assert (
        "ALTER TABLE chapter_rewrite_attempts "
        "ADD COLUMN IF NOT EXISTS phase_attempt_no INTEGER NOT NULL DEFAULT 0"
    ) in statements
    assert "UPDATE chapter_rewrite_attempts" in statements
    assert "SET phase_attempt_no = attempt_no" in statements
    assert "WHERE phase_attempt_no = 0" in statements
    assert (
        "CREATE INDEX IF NOT EXISTS ix_chapter_rewrite_attempts_project_chapter_phase"
    ) in statements
    assert "chapter_rewrite_attempt_phase_v1" in base_module.POSTGRES_BASELINE_MIGRATIONS


def test_runtime_postgresql_upgrade_invokes_rewrite_attempt_phase_helper(monkeypatch):
    called = False

    def _record_call(conn):
        nonlocal called
        called = True

    monkeypatch.setattr(base_module, "_upgrade_chapter_rewrite_attempt_phase", _record_call)

    base_module._upgrade_postgresql_database(_FakeEngine(_RecordingConnection()))

    assert called


def test_alembic_revision_fits_version_table():
    migration = _rewrite_attempt_phase_migration()

    assert migration.revision == "0019_rewrite_attempt_phase"
    assert len(migration.revision) <= 32


def test_alembic_upgrade_uses_idempotent_sql_and_backfills_phase_attempt(monkeypatch):
    migration = _rewrite_attempt_phase_migration()
    fake_op = _RecordingAlembicOp()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    statements = "\n".join(fake_op.statements)
    assert "ADD COLUMN IF NOT EXISTS repair_phase" in statements
    assert "ADD COLUMN IF NOT EXISTS phase_attempt_no" in statements
    assert "CREATE INDEX IF NOT EXISTS ix_chapter_rewrite_attempts_project_chapter_phase" in statements
    assert (
        "UPDATE chapter_rewrite_attempts SET phase_attempt_no = attempt_no "
        "WHERE phase_attempt_no = 0"
    ) in statements
    assert fake_op.add_column_calls == []
    assert fake_op.create_index_calls == []


class _Attempt:
    def __init__(self, repair_scope: str, repair_phase: str):
        self.repair_scope = repair_scope
        self.repair_phase = repair_phase


def test_attempts_for_repair_phase_filters_history_without_deleting_total_history():
    attempts = [
        _Attempt("draft", "review_repair"),
        _Attempt("draft", "review_repair"),
        _Attempt("chapter_plan", "review_repair"),
        _Attempt("draft", "canon_repair"),
    ]

    phase_attempts = _attempts_for_repair_phase(attempts, "canon_repair")

    assert len(attempts) == 4
    assert [item.repair_scope for item in phase_attempts] == ["draft"]
