from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, text
from tests.postgres import postgres_empty_test_url
from tests.test_v5_live_migration import _alembic_config
from forwin.models import Project
from forwin.models.projection import ProjectionCheckpoint


def test_incremental_migration_preserves_old_checkpoint_and_marks_revision_unknown():
    url = postgres_empty_test_url("incremental-embeddings")
    engine = create_engine(url)
    config = _alembic_config(url)
    command.upgrade(config, "0009_knowledge_dependencies")
    try:
        with engine.begin() as connection:
            for model, overrides in [
                (Project, dict(id="p", title="旧书", premise="保留")),
                (ProjectionCheckpoint, dict(id="checkpoint", project_id="p", projection_kind="chapter_memory", status="healthy", projected_chapter_number=99, target_chapter_number=99, source_digest="old-digest", last_error="old-error")),
            ]:
                table = Table(model.__tablename__, MetaData(), autoload_with=connection)
                values = {c.name: c.default.arg for c in model.__table__.columns if c.name in table.c and c.default is not None and (c.default.is_scalar or c.default.is_clause_element)}
                connection.execute(table.insert().values(**{**values, **overrides}))
            before = dict(connection.execute(text("SELECT * FROM projection_checkpoints")).mappings().one())
        command.upgrade(config, "head")
        with engine.connect() as connection:
            after = dict(connection.execute(text("SELECT * FROM projection_checkpoints")).mappings().one())
            assert {key: after[key] for key in before} == before
            assert "projected_book_revision" in after
            assert after["projected_book_revision"] is None
            assert after["target_book_revision"] is None
            assert connection.scalar(text("SELECT count(*) FROM embedding_cache_entries")) == 0
            assert any(item["column_names"] == ["project_id", "base_book_revision"] for item in inspect(connection).get_indexes("canon_commit_records"))
    finally:
        engine.dispose()
