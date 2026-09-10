import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, text

from tests.postgres import postgres_empty_test_url
from tests.test_chapter_revision_migration import _seed
from tests.test_v5_live_migration import _alembic_config


def test_0004_preserves_existing_acceptance_and_marks_old_quality_provenance_unknown():
    url=postgres_empty_test_url("revision-validation-migration")
    engine=create_engine(url)
    config=_alembic_config(url)
    command.upgrade(config,"0001_v5_recovery")
    with engine.begin() as connection: _seed(connection)
    command.upgrade(config,"0003_serial_capacity")
    with engine.begin() as connection:
        before=dict(connection.execute(text("SELECT * FROM canon_commit_records WHERE id='commit'")).mappings().one())
        from forwin.models.canon_quality import CanonAdmissionRunRow
        table=Table("canon_admission_runs",MetaData(),autoload_with=connection)
        values={c.name:c.default.arg for c in CanonAdmissionRunRow.__table__.columns if c.name in table.c and c.default is not None and (c.default.is_scalar or c.default.is_clause_element)}
        values.update(id="old-run",project_id="p",draft_id="draft",chapter_number=1)
        connection.execute(table.insert().values(**values))
    command.upgrade(config,"0004_revision_validation")
    with engine.connect() as connection:
        assert dict(connection.execute(text("SELECT * FROM canon_commit_records WHERE id='commit'")).mappings().one())==before
        assert connection.execute(text("SELECT projection_json,projection_fingerprint FROM canon_admission_runs WHERE id='old-run'")).one()==("","")
        assert connection.scalar(text("SELECT active_commit_id FROM chapter_plans WHERE id='chapter'"))=="commit"
        tables=inspect(connection).get_table_names()
        assert "canon_quality_acceptance_evidence" in tables
        assert "canon_revision_validations" in tables
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO canon_revision_validations (id,project_id,candidate_id,base_book_revision,status,result_json) VALUES ('evidence','p','candidate',1,'unknown','{}')"))
    with pytest.raises(ValueError,match="durable revision"):
        command.downgrade(config,"0003_serial_capacity")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT status FROM canon_revision_validations WHERE id='evidence'"))=="unknown"
    engine.dispose()
