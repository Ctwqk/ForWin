from __future__ import annotations

import atexit
import os
import re
import threading
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from forwin.models.base import get_engine, init_db


DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test"

_LOCK = threading.Lock()
_CREATED: set[str] = set()
_TEMPLATE_NAME = ""


def _base_url():
    return make_url(os.environ.get("FORWIN_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL))


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _admin_engine():
    base = _base_url()
    admin = base.set(database=os.environ.get("FORWIN_TEST_ADMIN_DATABASE", "postgres"))
    return create_engine(admin, isolation_level="AUTOCOMMIT", pool_pre_ping=True, future=True)


def _sanitize_name(name: str | None) -> str:
    raw = str(name or "db").rsplit("/", 1)[-1].replace(".db", "")
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_").lower()
    return normalized[:36] or "db"


def _drop_database(name: str) -> None:
    try:
        engine = _admin_engine()
        with engine.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {_quote_identifier(name)} WITH (FORCE)"))
    finally:
        try:
            engine.dispose()
        except UnboundLocalError:
            pass


def _ensure_template() -> str:
    global _TEMPLATE_NAME
    with _LOCK:
        if _TEMPLATE_NAME:
            return _TEMPLATE_NAME
        base = _base_url()
        base_name = _sanitize_name(base.database or "forwin_test")
        template_name = f"{base_name}_template_{os.getpid()}_{uuid4().hex[:8]}"
        engine = _admin_engine()
        try:
            with engine.connect() as conn:
                conn.execute(text(f"CREATE DATABASE {_quote_identifier(template_name)} TEMPLATE template0"))
        finally:
            engine.dispose()
        _CREATED.add(template_name)
        template_url = base.set(database=template_name).render_as_string(hide_password=False)
        template_engine = get_engine(template_url)
        try:
            init_db(template_engine)
        finally:
            template_engine.dispose()
        _TEMPLATE_NAME = template_name
        return template_name


def postgres_test_url(name: str | None = None) -> str:
    base = _base_url()
    template_name = _ensure_template()
    db_name = (
        f"{_sanitize_name(base.database or 'forwin_test')}_"
        f"{_sanitize_name(name)}_{uuid4().hex[:8]}"
    )
    engine = _admin_engine()
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE {_quote_identifier(db_name)} "
                    f"TEMPLATE {_quote_identifier(template_name)}"
                )
            )
    finally:
        engine.dispose()
    _CREATED.add(db_name)
    return base.set(database=db_name).render_as_string(hide_password=False)


@atexit.register
def _cleanup_databases() -> None:
    for name in sorted(_CREATED, reverse=True):
        _drop_database(name)
