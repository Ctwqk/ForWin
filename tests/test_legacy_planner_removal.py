from __future__ import annotations

import ast
import importlib
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from forwin.models import Base
from forwin.models.project import ArcPlanVersion, Project
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import ArcBeatSheetData, BeatPhase, Milestone
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise
from forwin.state.repo import StateRepository


TABLES = {
    "arc_envelopes", "arc_envelope_analyses", "arc_structure_drafts",
    "provisional_promotion_records",
}


def test_retired_planner_downgrade_recreates_the_baseline_schema(monkeypatch) -> None:
    migration = importlib.import_module("forwin.migrations.versions.0006_planning_v6")
    baseline_path = Path(__file__).resolve().parents[1] / "forwin/migrations/versions/0001_v5_baseline.py"
    baseline = ast.parse(baseline_path.read_text())
    upgrade = next(node for node in baseline.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade")
    original_ddl = []
    for node in upgrade.body:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in {"create_table", "create_index"}:
            continue
        if any(isinstance(arg, ast.Constant) and arg.value in TABLES for arg in call.args):
            original_ddl.append(ast.dump(node, include_attributes=False))
    restored = ast.parse(inspect_source(migration._restore_retired_arc_planner_tables)).body[0]
    assert [ast.dump(node, include_attributes=False) for node in restored.body] == original_ddl

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration._restore_retired_arc_planner_tables()
        inspector = inspect(connection)
        before = {
            name: (inspector.get_columns(name), inspector.get_foreign_keys(name), inspector.get_indexes(name))
            for name in TABLES
        }
        migration._drop_retired_arc_planner_tables()
        assert not TABLES.intersection(inspect(connection).get_table_names())
        migration._restore_retired_arc_planner_tables()
        inspector = inspect(connection)
        after = {
            name: (inspector.get_columns(name), inspector.get_foreign_keys(name), inspector.get_indexes(name))
            for name in TABLES
        }
        # SQLAlchemy type instances compare by identity, so compare their structural representations.
        assert repr(after) == repr(before)
    engine.dispose()


def inspect_source(function) -> str:
    import inspect as python_inspect

    return python_inspect.getsource(function)


def test_experience_reads_only_the_active_arcs_current_beat_sheet() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id="project", title="title", premise="premise", genre="genre"))
        session.add_all([
            ArcPlanVersion(id="old-arc", project_id="project", arc_synopsis="synopsis", arc_number=1, chapter_start=1, chapter_end=4, status="completed"),
            ArcPlanVersion(id="active-arc", project_id="project", arc_synopsis="synopsis", arc_number=2, chapter_start=5, chapter_end=8, status="active"),
        ])
        session.flush()
        sheets = ArcBeatSheetRepository(session)
        def data(label):
            return ArcBeatSheetData(
                protagonist_ref="hero", band_breakdown=[],
                phases=[BeatPhase(name="phase", chapter_range=(5, 8), purpose=f"purpose-{label}")],
                milestones=[Milestone(id="milestone", description=f"milestone-{label}", target_range=(6, 8))],
                reader_promise=ReaderPromise(genre_promise=label),
                arc_payoff_map=ArcPayoffMap(ambiguity_constraints=[label]),
            )
        sheets.create("project", "old-arc", data("completed"), source="fallback", reason="")
        sheets.create("project", "active-arc", data("superseded"), source="fallback", reason="")
        sheets.create("project", "active-arc", data("current"), source="fallback", reason="")
        repo = StateRepository(session)
        assert repo.get_reader_promise("project").genre_promise == "current"
        assert repo.get_arc_payoff_map("project").ambiguity_constraints == ["current"]
        assert repo.get_beat_sheet_experience("project").key_beats == ["purpose-current", "milestone-current"]
        assert repo.get_reader_promise("missing") is None
        assert repo.get_arc_payoff_map("missing") is None
        assert repo.get_beat_sheet_experience("missing").key_beats == []
        from forwin.application.read_models.runtime_maps import load_project_runtime_maps
        from forwin.application.read_models.arc_snapshot import project_arc_snapshot_payload

        maps = load_project_runtime_maps(session, ["project"])
        snapshot = project_arc_snapshot_payload(maps["latest_beat_sheet_map"]["project"])
        assert snapshot["active_arc_id"] == "active-arc"
        assert snapshot["active_reader_promise"]["genre_promise"] == "current"
        assert session.get(ArcPlanVersion, "old-arc").status == "completed"
    engine.dispose()


def test_repair_verification_attempts_reach_a_persisted_prompt_trace() -> None:
    from types import SimpleNamespace
    from forwin.generation.pipeline_core.review_autofix import ReviewWorkflowStage
    from forwin.generation.pipeline_core.runtime_helpers import RuntimeSupportStage
    from forwin.models.genesis import PromptTrace
    from forwin.protocol.review import RepairVerification, ReviewVerdict
    from forwin.protocol.writer import WriterOutput
    from forwin.state.updater import StateUpdater

    attempts = [{"stage_key": "repair_verification", "attempt_no": 1, "model": "stub", "output_chars": 10, "final_failure": False}]
    verifier = SimpleNamespace(
        verify=lambda **_: RepairVerification(fixed_all_must_fix=True, preserved_all_must_preserve=True),
        llm_client=SimpleNamespace(drain_llm_attempt_events=lambda: attempts),
    )
    pipeline = SimpleNamespace(repair_verifier=verifier)
    output = WriterOutput(chapter_number=1, title="title", body="body", end_of_chapter_summary="summary")
    review = ReviewWorkflowStage._review_with_repair_verification(
        pipeline, original_output=output, repaired_output=output,
        before_review=ReviewVerdict(verdict="pass"), review=ReviewVerdict(verdict="pass"), repair_instruction=None,
    )
    assert review.prompt_trace["attempts"] == attempts
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    events = []
    with Session(engine) as session:
        session.add(Project(id="project", title="title", premise="premise", genre="genre"))
        session.flush()
        pipeline.artifact_store = None
        pipeline._record_decision_event = lambda **kwargs: events.append(kwargs)
        pipeline._record_prompt_trace_performance_spans = lambda **_: None
        trace_id = RuntimeSupportStage._save_prompt_trace_payload(
            pipeline, session=session, updater=StateUpdater(session), project_id="project", prompt_trace=review.prompt_trace,
        )
        session.flush()
        assert session.get(PromptTrace, trace_id).stage_key == "repair_verification"
        assert events
    engine.dispose()
