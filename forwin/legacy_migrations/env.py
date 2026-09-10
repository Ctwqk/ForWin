"""Online-only legacy upgrade with caller-owned transaction support."""

from alembic import context
from sqlalchemy import engine_from_config, pool


def _run(connection):
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Legacy upgrade requires online data and ownership validation.")
elif context.config.attributes.get("connection") is not None:
    _run(context.config.attributes["connection"])
else:
    engine = engine_from_config(
        context.config.get_section(context.config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()
