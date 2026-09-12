from alembic import command
from sqlalchemy import MetaData, Table, create_engine, text
from tests.postgres import postgres_empty_test_url
from tests.test_v5_live_migration import _alembic_config
from forwin.models import Project
from forwin.models.knowledge import KnowledgeProjectionPageRow


def test_0009_preserves_all_old_page_bytes_and_marks_dependencies_unknown():
    url = postgres_empty_test_url("knowledge-dependencies")
    engine = create_engine(url)
    config = _alembic_config(url)
    command.upgrade(config, "0008_generation_continuation")
    try:
        with engine.begin() as connection:
            for model, overrides in [
                (Project, dict(id="p", title="旧书", premise="旧设定")),
                (
                    KnowledgeProjectionPageRow,
                    dict(
                        id="page",
                        project_id="p",
                        page_key="old",
                        markdown="# Canon\n旧事实\n# Manual Notes\n保留人工笔记",
                        content_hash="old-hash",
                        source_digest="old-digest",
                    ),
                ),
            ]:
                table = Table(model.__tablename__, MetaData(), autoload_with=connection)
                values = {
                    c.name: c.default.arg
                    for c in model.__table__.columns
                    if c.name in table.c
                    and c.default is not None
                    and (c.default.is_scalar or c.default.is_clause_element)
                }
                values.update(overrides)
                connection.execute(table.insert().values(**values))
            before = dict(
                connection.execute(text("SELECT * FROM knowledge_projection_pages"))
                .mappings()
                .one()
            )
        command.upgrade(config, "0009_knowledge_dependencies")
        with engine.connect() as connection:
            after = dict(
                connection.execute(text("SELECT * FROM knowledge_projection_pages"))
                .mappings()
                .one()
            )
            assert {key: after[key] for key in before} == before
            assert after["dependency_manifest_json"] == "{}"
    finally:
        engine.dispose()
