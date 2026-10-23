from __future__ import annotations

import logging

from alembic import command

from forwin.models.base import alembic_config
from tests.postgres import postgres_empty_test_url


def test_running_migrations_keeps_existing_loggers_enabled() -> None:
    """Alembic's logging setup must not silence loggers the process created before the upgrade."""
    logger = logging.getLogger("forwin.migration_logging_probe")
    logger.disabled = False

    command.upgrade(alembic_config(postgres_empty_test_url("migration-logging")), "head")

    assert not logger.disabled
