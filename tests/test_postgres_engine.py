from __future__ import annotations

import importlib.util

import pytest
from sqlalchemy import create_mock_engine

from forwin import models  # noqa: F401
from forwin.config import InfrastructureConfig
from forwin.models.base import Base
from forwin.models.base import get_engine
from tests import postgres


def test_get_engine_rejects_sqlite_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORWIN_TEST_DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="PostgreSQL"):
        get_engine("data/novel.db")


def test_removed_db_path_config_alias_is_rejected() -> None:
    with pytest.raises(ValueError):
        InfrastructureConfig(db_path="data/novel.db")


def test_get_engine_accepts_postgresql_url() -> None:
    if importlib.util.find_spec("psycopg") is None:
        pytest.skip("psycopg is not installed in this Python environment")
    engine = get_engine("postgresql://forwin:forwin@localhost:5432/forwin")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


def test_postgres_metadata_ddl_compiles() -> None:
    statements: list[str] = []
    engine = create_mock_engine(
        "postgresql+psycopg://forwin:forwin@localhost:5432/forwin",
        lambda sql, *multiparams, **params: statements.append(str(sql.compile(dialect=engine.dialect))),
    )

    Base.metadata.create_all(engine)

    assert any("CREATE TABLE projects" in statement for statement in statements)
    assert any("CREATE TABLE generation_tasks" in statement for statement in statements)


def test_cleanup_test_databases_preserves_template_and_persistent_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dropped: list[str] = []
    monkeypatch.setattr(postgres, "_CREATED", {"template", "session", "transient"})
    monkeypatch.setattr(postgres, "_PERSISTENT", {"session"})
    monkeypatch.setattr(postgres, "_TEMPLATE_NAME", "template")
    monkeypatch.setattr(postgres, "_drop_database", dropped.append)

    postgres.cleanup_test_databases()

    assert dropped == ["transient"]
    assert postgres._CREATED == {"template", "session"}
