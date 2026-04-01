from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import Engine, event, text
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    """Generate a new UUID4 hex string."""
    return uuid4().hex


def get_engine(db_path: str) -> Engine:
    """Create a SQLite engine with WAL journal mode enabled."""
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def set_wal_mode(dbapi_connection, connection_record):  # type: ignore[misc]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create and return a sessionmaker bound to the given engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create all tables defined in the metadata."""
    Base.metadata.create_all(engine)
    upgrade_db(engine)


def upgrade_db(engine: Engine) -> None:
    """Apply lightweight forward-only schema upgrades."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT OR IGNORE INTO schema_migrations(version)
                VALUES ('publisher_extension_v1')
                """
            )
        )
        applied = conn.execute(
            text(
                """
                SELECT 1
                FROM schema_migrations
                WHERE version = 'entity_alias_index_v1'
                """
            )
        ).scalar_one_or_none()
        if applied is None:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS entity_aliases (
                        id TEXT PRIMARY KEY,
                        entity_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        alias TEXT NOT NULL,
                        FOREIGN KEY(entity_id) REFERENCES entities(id)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_alias_project_alias
                    ON entity_aliases(project_id, alias)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_entity_aliases_project_id
                    ON entity_aliases(project_id)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_entity_aliases_alias
                    ON entity_aliases(alias)
                    """
                )
            )
            rows = conn.execute(
                text("SELECT id, project_id, aliases_json FROM entities")
            ).mappings()
            for row in rows:
                try:
                    aliases = json.loads(row["aliases_json"] or "[]") or []
                except (json.JSONDecodeError, TypeError):
                    aliases = []
                for alias in aliases:
                    alias_text = str(alias).strip()
                    if not alias_text:
                        continue
                    conn.execute(
                        text(
                            """
                            INSERT OR IGNORE INTO entity_aliases(id, entity_id, project_id, alias)
                            VALUES (:id, :entity_id, :project_id, :alias)
                            """
                        ),
                        {
                            "id": new_id(),
                            "entity_id": row["id"],
                            "project_id": row["project_id"],
                            "alias": alias_text,
                        },
                    )
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version)
                    VALUES ('entity_alias_index_v1')
                    """
                )
            )
