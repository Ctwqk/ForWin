from __future__ import annotations

import importlib.util

import pytest
from sqlalchemy import create_mock_engine

from forwin import models  # noqa: F401
from forwin.config import Config
from forwin.models.base import Base
from forwin.models.base import get_engine


def test_get_engine_rejects_sqlite_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORWIN_TEST_DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="PostgreSQL"):
        get_engine("data/novel.db")


def test_legacy_db_path_config_is_rejected_by_engine() -> None:
    config = Config(db_path="data/novel.db")

    with pytest.raises(ValueError, match="PostgreSQL"):
        get_engine(config.database_url)


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
