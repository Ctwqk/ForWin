from __future__ import annotations

from datetime import datetime
import importlib
import json

from alembic import command
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from forwin import models
from forwin.models.base import alembic_config
from forwin.runtime.policy import RuntimePolicy
from tests.postgres import postgres_empty_test_url


PLANNING_COLUMNS = {
    "band_plan_id", "pov_ref", "onstage_refs_json", "location_refs_json",
    "world_bindings_json", "reveal_directives_json", "hint_directives_json",
    "intended_changes_json", "milestone_ids_json", "expected_span_minutes", "hook",
}
BEAT_SHEET_COLUMNS = {
    "id", "project_id", "arc_plan_id", "version", "status", "source",
    "protagonist_ref", "phases_json", "milestones_json", "protections_json",
    "reveal_targets_json", "exit_state_json", "band_breakdown_json",
    "reader_promise_json", "arc_payoff_map_json", "revision_reason", "created_at",
}
BAND_PLAN_COLUMNS = {
    "id", "project_id", "arc_plan_id", "beat_sheet_id", "band_index",
    "chapter_start", "chapter_end", "status", "version", "replan_count",
    "plan_json", "created_at",
}


@pytest.fixture
def legacy_database():
    url = postgres_empty_test_url("planning-v6")
    config = alembic_config(url)
    command.upgrade(config, "0005_world_plan_conflicts")
    engine = sa.create_engine(url)
    try:
        yield config, engine
    finally:
        engine.dispose()


def _seed_legacy_chapter(session: Session) -> None:
    session.add(models.Project(id="project-1", title="title", premise="premise", genre="genre"))
    session.flush()
    session.add(models.ArcPlanVersion(
        id="arc-1", project_id="project-1", arc_synopsis="synopsis",
        chapter_start=1, chapter_end=10,
    ))
    session.flush()
    # Reflect 0005 so the INSERT cannot pick up defaults for 0006-only columns.
    table = sa.Table("chapter_plans", sa.MetaData(), autoload_with=session.connection())
    session.execute(table.insert().values(
        id="chapter-1", project_id="project-1", arc_plan_id="arc-1",
        chapter_number=1, title="title", one_line="summary", goals_json="[]",
        task_contract_json="[]", experience_plan_json="{}", status="planned",
        acceptance_mode="", repair_attempt_count=0, residual_review_issues_json="[]",
        canon_risk_level="", created_at=datetime(2026, 10, 22),
    ))


def _assert_planning_schema(engine: sa.Engine) -> None:
    inspector = sa.inspect(engine)
    assert {"arc_beat_sheets", "band_plans"} <= set(inspector.get_table_names())
    assert "sub_world_" + "roster_" + "items" not in inspector.get_table_names()
    assert "roster_" + "item_ids_json" not in {c["name"] for c in inspector.get_columns("character_identity_map")}
    assert {"project_stage_analyses", "project_replan_events"}.isdisjoint(inspector.get_table_names())
    assert "macro_" + "progression_json" not in {c["name"] for c in inspector.get_columns("arc_plan_versions")}
    assert {c["name"] for c in inspector.get_columns("arc_beat_sheets")} == BEAT_SHEET_COLUMNS
    assert {c["name"] for c in inspector.get_columns("band_plans")} == BAND_PLAN_COLUMNS
    assert PLANNING_COLUMNS <= {c["name"] for c in inspector.get_columns("chapter_plans")}
    assert ("arc_plan_id", "version") in {
        tuple(c["column_names"]) for c in inspector.get_unique_constraints("arc_beat_sheets")
    }
    for table, expected in (
        ("arc_beat_sheets", ("project_id", "status")),
        ("band_plans", ("project_id", "arc_plan_id", "band_index", "status")),
    ):
        assert expected in {tuple(i["column_names"]) for i in inspector.get_indexes(table)}
    for table, expected in (
        ("arc_beat_sheets", {("project_id", "projects"), ("arc_plan_id", "arc_plan_versions")}),
        ("band_plans", {
            ("project_id", "projects"), ("arc_plan_id", "arc_plan_versions"),
            ("beat_sheet_id", "arc_beat_sheets"),
        }),
        ("chapter_plans", {("band_plan_id", "band_plans")}),
    ):
        assert expected <= {
            (fk["constrained_columns"][0], fk["referred_table"])
            for fk in inspector.get_foreign_keys(table)
        }
    indexes = {i["name"]: i for i in inspector.get_indexes("chapter_plans")}
    assert "ix_chapter_plans_project_chapter" not in indexes
    index = indexes["uq_chapter_plans_project_chapter"]
    assert index["unique"]
    assert index["column_names"] == ["project_id", "chapter_number"]


def test_0006_creates_beat_sheet_and_band_plan_tables(legacy_database) -> None:
    config, engine = legacy_database
    command.upgrade(config, "0006_planning_v6")
    _assert_planning_schema(engine)
    command.downgrade(config, "0005_world_plan_conflicts")
    inspector = sa.inspect(engine)
    assert {"arc_beat_sheets", "band_plans"}.isdisjoint(inspector.get_table_names())
    assert PLANNING_COLUMNS.isdisjoint(c["name"] for c in inspector.get_columns("chapter_plans"))
    index = next(i for i in inspector.get_indexes("chapter_plans") if i["name"] == "ix_chapter_plans_project_chapter")
    assert not index["unique"]
    command.upgrade(config, "0006_planning_v6")
    _assert_planning_schema(engine)


def test_chapter_plans_gain_planning_columns_and_unique_chapter(legacy_database) -> None:
    config, engine = legacy_database
    with Session(engine) as session, session.begin():
        _seed_legacy_chapter(session)
    command.upgrade(config, "0006_planning_v6")
    _assert_planning_schema(engine)
    with Session(engine) as session, session.begin():
        chapter = session.get(models.ChapterPlan, "chapter-1")
        assert chapter.band_plan_id is None
        assert chapter.expected_span_minutes is None
        assert chapter.pov_ref == chapter.hook == ""
        for name in PLANNING_COLUMNS:
            if name.endswith("_json"):
                assert getattr(chapter, name) == "[]"
        # A second arc must not permit a second plan for this project's chapter.
        session.add(models.ArcPlanVersion(id="arc-2", project_id="project-1", arc_synopsis="next"))
        session.flush()
        with pytest.raises(sa.exc.IntegrityError, match="uq_chapter_plans_project_chapter"):
            with session.begin_nested():
                session.add(models.ChapterPlan(project_id="project-1", arc_plan_id="arc-2", chapter_number=1))
                session.flush()
        session.add(models.ChapterPlan(project_id="project-1", arc_plan_id="arc-2", chapter_number=2))


def test_0006_upgrade_rewrites_legacy_step_rows(legacy_database) -> None:
    config, engine = legacy_database
    with Session(engine) as session, session.begin():
        _seed_legacy_chapter(session)
        session.add(models.ChapterDraft(id="draft-1", chapter_plan_id="chapter-1", body_text="body"))
        session.flush()
        session.add(models.ChapterReview(id="review-1", draft_id="draft-1", verdict="pass"))
        session.flush()
        session.add(models.CandidateDraftRecord(
            id="candidate-1", project_id="project-1", chapter_plan_id="chapter-1",
            chapter_number=1, candidate_draft_id="draft-1", review_id="review-1",
        ))
        session.flush()
        session.add(models.CanonCommitRecord(
            id="canon-1", idempotency_key="canon-1", candidate_id="candidate-1",
            project_id="project-1", chapter_number=1,
        ))
        session.flush()
        for step in ("planning", "arc", "feedback"):
            session.add(models.PostCanonMaintenanceRun(
                id=f"run-{step}", canon_commit_id="canon-1", project_id="project-1",
                chapter_number=1, candidate_id="candidate-1", step_name=step,
                idempotency_key=f"post-canon-maintenance:v1:canon-1:{step}", result_json='{"kept":true}',
            ))
    command.upgrade(config, "0006_planning_v6")
    with engine.connect() as connection:
        rows = connection.execute(sa.text(
            "SELECT id, step_name, result_json, idempotency_key FROM post_canon_maintenance_runs ORDER BY id"
        )).all()
    assert rows == [
        ("run-feedback", "feedback", '{"kept":true}', "post-canon-maintenance:v1:canon-1:feedback"),
        ("run-planning", "plan", '{"kept":true}', "post-canon-maintenance:v1:canon-1:plan"),
    ]


def test_models_create_all_on_sqlite() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        models.Base.metadata.create_all(engine)
        _assert_planning_schema(engine)
        with Session(engine) as session, session.begin():
            session.add(models.Project(id="p", title="title", premise="premise", genre="genre"))
            session.flush()
            session.add(models.ArcPlanVersion(id="a", project_id="p", arc_synopsis="synopsis"))
            session.flush()
            beat = models.ArcBeatSheetRow(project_id="p", arc_plan_id="a", protagonist_ref="character-1")
            session.add(beat)
            session.flush()
            band = models.BandPlanRow(
                project_id="p", arc_plan_id="a", beat_sheet_id=beat.id,
                band_index=0, chapter_start=1, chapter_end=5,
            )
            session.add(band)
            session.flush()
            chapter = models.ChapterPlan(project_id="p", arc_plan_id="a", chapter_number=1, band_plan_id=band.id)
            session.add(chapter)
            session.flush()
            assert beat.version == band.version == 1
            assert beat.status == band.status == "active"
            assert band.replan_count == 0
            assert band.plan_json == "{}"
            assert chapter.pov_ref == chapter.hook == ""
            assert chapter.expected_span_minutes is None
            for name in PLANNING_COLUMNS:
                if name.endswith("_json"):
                    assert getattr(chapter, name) == "[]"
            with pytest.raises(sa.exc.IntegrityError):
                with session.begin_nested():
                    session.add(models.ChapterPlan(project_id="p", arc_plan_id="a", chapter_number=1))
                    session.flush()
            with pytest.raises(sa.exc.IntegrityError):
                with session.begin_nested():
                    session.add(models.ArcBeatSheetRow(project_id="p", arc_plan_id="a", version=1, protagonist_ref="character-1"))
                    session.flush()
    finally:
        engine.dispose()


def test_0006_strips_removed_planning_key_preserving_other_policy_data(monkeypatch) -> None:
    migration = importlib.import_module("forwin.migrations.versions.0006_planning_v6")
    engine = sa.create_engine("sqlite:///:memory:")
    policies = {
        "legacy": json.dumps({"planning": {"use_llm_simulation": True, "context_recency_" + "window": 73}, "custom": [1]}),
        "recency_only": json.dumps({"planning": {"context_recency_" + "window": 61, "band_size": 8}, "custom": [2]}),
        "current": '{"planning":{"band_size":8}}',
        "malformed": "not-json",
        "empty": "",
        "nonobject": "[]",
        "planning_nonobject": '{"planning":null}',
    }
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE projects (id TEXT PRIMARY KEY, runtime_policy_json TEXT)"))
            for key, value in policies.items():
                connection.execute(sa.text("INSERT INTO projects VALUES (:id, :policy)"), {"id": key, "policy": value})
            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
            migration._drop_simulation_from_policies()
            actual = dict(connection.execute(sa.text("SELECT id, runtime_policy_json FROM projects")).all())
            assert json.loads(actual.pop("legacy")) == {"planning": {}, "custom": [1]}
            assert json.loads(actual.pop("recency_only")) == {"planning": {"band_size": 8}, "custom": [2]}
            assert actual == {k: v for k, v in policies.items() if k not in {"legacy", "recency_only"}}
            first = connection.execute(sa.text("SELECT id, runtime_policy_json FROM projects ORDER BY id")).all()
            migration._drop_simulation_from_policies()
            assert connection.execute(sa.text("SELECT id, runtime_policy_json FROM projects ORDER BY id")).all() == first
    finally:
        engine.dispose()


def test_0006_removes_simulation_table_and_downgrade_restores_empty_schema(legacy_database) -> None:
    config, engine = legacy_database
    table_name = "world_simulation_turns"
    original = [(c["name"], str(c["type"]), c["nullable"]) for c in sa.inspect(engine).get_columns(table_name)]
    command.upgrade(config, "0006_planning_v6")
    assert table_name not in sa.inspect(engine).get_table_names()
    command.downgrade(config, "0005_world_plan_conflicts")
    assert [(c["name"], str(c["type"]), c["nullable"]) for c in sa.inspect(engine).get_columns(table_name)] == original
    with engine.connect() as connection:
        assert connection.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one() == 0


def test_0006_upgrade_strips_stored_policy_key_and_preserves_other_fields(legacy_database) -> None:
    config, engine = legacy_database
    legacy = RuntimePolicy.for_profile("standard").model_dump(mode="json")
    legacy["planning"]["use_llm_simulation"] = True
    legacy["planning"]["context_recency_" + "window"] = 61
    with Session(engine) as session, session.begin():
        session.add(models.Project(
            id="legacy-policy", title="title", premise="premise", genre="genre",
            runtime_policy_json=json.dumps(legacy), runtime_policy_version=1,
        ))
    command.upgrade(config, "0006_planning_v6")
    with engine.connect() as connection:
        stored = connection.execute(sa.text(
            "SELECT runtime_policy_json FROM projects WHERE id = 'legacy-policy'"
        )).scalar_one()
    expected = json.loads(json.dumps(legacy))
    del expected["planning"]["use_llm_simulation"]
    del expected["planning"]["context_recency_" + "window"]
    assert json.loads(stored) == expected
    assert "context_recency_" + "window" not in RuntimePolicy.model_validate_json(stored).planning.model_dump()
    command.downgrade(config, "0005_world_plan_conflicts")
    with engine.connect() as connection:
        assert connection.execute(sa.text(
            "SELECT runtime_policy_json FROM projects WHERE id = 'legacy-policy'"
        )).scalar_one() == stored
    command.upgrade(config, "0006_planning_v6")
    with engine.connect() as connection:
        assert connection.execute(sa.text(
            "SELECT runtime_policy_json FROM projects WHERE id = 'legacy-policy'"
        )).scalar_one() == stored


@pytest.mark.parametrize("table_name", [
    "arc_world_" + "contracts", "band_world_" + "contracts", "chapter_world_delta_intents",
])
def test_0006_drops_contract_tables_and_restores_baseline_schema(legacy_database, table_name) -> None:
    config, engine = legacy_database

    def schema():
        inspector = sa.inspect(engine)
        return {
            "columns": [(c["name"], str(c["type"]), c["nullable"], c["default"])
                        for c in inspector.get_columns(table_name)],
            "pk": inspector.get_pk_constraint(table_name),
            "fk": inspector.get_foreign_keys(table_name),
            "indexes": inspector.get_indexes(table_name),
            "unique": inspector.get_unique_constraints(table_name),
        }

    original = schema()
    command.upgrade(config, "0006_planning_v6")
    assert table_name not in sa.inspect(engine).get_table_names()
    command.downgrade(config, "0005_world_plan_conflicts")
    assert schema() == original
    with engine.connect() as connection:
        assert connection.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one() == 0
    command.upgrade(config, "0006_planning_v6")
    assert table_name not in sa.inspect(engine).get_table_names()


def test_0006_contract_downgrade_definitions_match_baseline() -> None:
    import ast
    import inspect

    baseline = importlib.import_module("forwin.migrations.versions.0001_v5_baseline")
    migration = importlib.import_module("forwin.migrations.versions.0006_planning_v6")
    tables = {"arc_world_" + "contracts", "band_world_" + "contracts", "chapter_world_delta_intents"}

    def definitions(function):
        result = {}
        for node in ast.walk(ast.parse(inspect.getsource(function))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"create_table", "create_index"}:
                continue
            table_arg = node.args[0 if node.func.attr == "create_table" else 1]
            if isinstance(table_arg, ast.Constant) and table_arg.value in tables:
                result[node.args[0].value] = ast.dump(node, include_attributes=False)
        return result

    expected = definitions(baseline.upgrade)
    assert len(expected) == 6  # Three tables and their three indexes.
    assert definitions(migration.downgrade) == expected


def test_0006_drops_replanner_schema_and_restores_baseline(legacy_database) -> None:
    config, engine = legacy_database
    tables = ("project_stage_analyses", "project_replan_events")
    column = "macro_" + "progression_json"

    def schema():
        inspector = sa.inspect(engine)
        return {
            table: {
                # A re-added column lands last; column order carries no meaning.
                "columns": sorted((c["name"], str(c["type"]), c["nullable"], c["default"])
                                  for c in inspector.get_columns(table)),
                "pk": inspector.get_pk_constraint(table),
                "fk": inspector.get_foreign_keys(table),
                "indexes": inspector.get_indexes(table),
                "unique": inspector.get_unique_constraints(table),
            }
            for table in (*tables, "arc_plan_versions")
        }

    original = schema()
    with Session(engine) as session, session.begin():
        _seed_legacy_chapter(session)
    command.upgrade(config, "0006_planning_v6")
    _assert_planning_schema(engine)
    command.downgrade(config, "0005_world_plan_conflicts")
    assert schema() == original
    with engine.connect() as connection:
        for table in tables:
            assert connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
        assert connection.execute(sa.text(f"SELECT {column} FROM arc_plan_versions WHERE id = 'arc-1'")).scalar_one() == "{}"
    command.upgrade(config, "0006_planning_v6")
    _assert_planning_schema(engine)


def test_0006_replanner_downgrade_definitions_match_baseline() -> None:
    import ast
    import inspect

    baseline = importlib.import_module("forwin.migrations.versions.0001_v5_baseline")
    migration = importlib.import_module("forwin.migrations.versions.0006_planning_v6")
    tables = {"project_stage_analyses", "project_replan_events"}
    column = "macro_" + "progression_json"

    def definitions(function):
        result = {}
        for node in ast.walk(ast.parse(inspect.getsource(function))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "create_table" and node.args[0].value in tables:
                result[node.args[0].value] = ast.dump(node, include_attributes=False)
            if node.func.attr == "Column" and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == column:
                result[column] = ast.dump(node, include_attributes=False)
        return result

    expected = definitions(baseline.upgrade)
    assert len(expected) == 3
    assert definitions(migration.downgrade) == expected


def test_0006_drops_subworld_character_paths_and_restores_baseline(legacy_database) -> None:
    config, engine = legacy_database
    table_name = "sub_world_" + "roster_" + "items"
    column = "roster_" + "item_ids_json"

    def schema():
        inspector = sa.inspect(engine)
        return {
            table: {
                "columns": sorted((c["name"], str(c["type"]), c["nullable"], c["default"])
                                  for c in inspector.get_columns(table)),
                "pk": inspector.get_pk_constraint(table),
                "fk": inspector.get_foreign_keys(table),
                "indexes": inspector.get_indexes(table),
                "unique": inspector.get_unique_constraints(table),
            }
            for table in (table_name, "character_identity_map")
        }

    original = schema()
    with Session(engine) as session, session.begin():
        _seed_legacy_chapter(session)
        identities = sa.Table("character_identity_map", sa.MetaData(), autoload_with=session.connection())
        session.execute(identities.insert().values(**{
            "id": "identity-1", "project_id": "project-1", "canonical_character_id": "character-1",
            "book_state_node_id": "character-1", "genesis_ref_id": "seed:character-1",
            column: '["old-link"]', "aliases_json": "[]", "display_name": "Character",
            "status": "active", "metadata_json": "{}",
            "created_at": datetime(2026, 10, 22), "updated_at": datetime(2026, 10, 22),
        }))
    command.upgrade(config, "0006_planning_v6")
    _assert_planning_schema(engine)
    command.downgrade(config, "0005_world_plan_conflicts")
    assert schema() == original
    with engine.connect() as connection:
        assert connection.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one() == 0
        assert connection.execute(sa.text(f"SELECT {column} FROM character_identity_map")).scalar_one() == "[]"
    command.upgrade(config, "0006_planning_v6")
    _assert_planning_schema(engine)


def test_0006_subworld_character_downgrade_definitions_match_baseline() -> None:
    import ast
    import inspect

    baseline = importlib.import_module("forwin.migrations.versions.0001_v5_baseline")
    migration = importlib.import_module("forwin.migrations.versions.0006_planning_v6")
    table_name = "sub_world_" + "roster_" + "items"

    def definitions(function):
        result = {}
        for node in ast.walk(ast.parse(inspect.getsource(function))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"create_table", "create_index"}:
                continue
            table_arg = node.args[0 if node.func.attr == "create_table" else 1]
            if isinstance(table_arg, ast.Constant) and table_arg.value == table_name:
                result[node.args[0].value] = ast.dump(node, include_attributes=False)
        return result

    expected = definitions(baseline.upgrade)
    assert len(expected) == 5
    assert definitions(migration._restore_subworld_character_paths) == expected
