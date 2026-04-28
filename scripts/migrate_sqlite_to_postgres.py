#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, DateTime, delete, func, select, text
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import forwin.models as _models  # noqa: F401
from forwin.models.base import Base, alembic_config, get_engine, init_db


EXCLUDED_SOURCE_TABLES = {"schema_migrations"}
EXCLUDED_TARGET_TABLES = {"chapter_memories", "llm_kb_vectors"}
HIGH_VALUE_TABLES = {
    "projects",
    "book_genesis_revisions",
    "chapter_plans",
    "chapter_drafts",
    "chapter_reviews",
    "generation_tasks",
    "decision_events",
    "prompt_traces",
    "publisher_upload_jobs",
    "publisher_browser_sessions",
    "world_lines",
    "world_nodes",
    "fact_nodes",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline one-shot migration from legacy ForWin SQLite state to Postgres."
    )
    parser.add_argument("--sqlite-path", required=True, help="Path to the legacy SQLite database.")
    parser.add_argument("--database-url", required=True, help="Target postgresql+psycopg:// URL.")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--truncate-target", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def source_connect(sqlite_path: Path) -> sqlite3.Connection:
    if not sqlite_path.exists() or not sqlite_path.is_file():
        raise SystemExit(f"SQLite source does not exist: {sqlite_path}")
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise SystemExit(f"SQLite source is not readable: {exc}") from exc
    return conn


def source_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return {str(row["name"]) for row in rows if str(row["name"]) not in EXCLUDED_SOURCE_TABLES}


def source_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return [str(row["name"]) for row in rows]


def source_count(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) AS count FROM "{table_name}"').fetchone()["count"])


def ordered_tables():
    return [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in EXCLUDED_TARGET_TABLES
    ]


def check_source_tables(conn: sqlite3.Connection) -> None:
    existing = source_tables(conn)
    missing = [table.name for table in ordered_tables() if table.name not in existing]
    if missing:
        raise SystemExit(
            "SQLite source is missing expected business tables: " + ", ".join(missing)
        )


def convert_value(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, Boolean):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "t"}
        return bool(value)
    if isinstance(column.type, DateTime):
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value)
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    return value


def iter_source_batches(
    conn: sqlite3.Connection,
    table,
    columns: list[str],
    batch_size: int,
):
    offset = 0
    quoted = ", ".join(f'"{name}"' for name in columns)
    column_map = {column.name: column for column in table.columns}
    while True:
        rows = conn.execute(
            f'SELECT {quoted} FROM "{table.name}" LIMIT ? OFFSET ?',
            (batch_size, offset),
        ).fetchall()
        if not rows:
            break
        batch: list[dict[str, Any]] = []
        for row in rows:
            batch.append(
                {
                    name: convert_value(column_map[name], row[name])
                    for name in columns
                }
            )
        yield batch
        offset += len(rows)


def table_columns_for_import(conn: sqlite3.Connection, table) -> list[str]:
    source_names = source_columns(conn, table.name)
    target_names = {column.name for column in table.columns}
    unknown = [name for name in source_names if name not in target_names]
    if unknown:
        raise SystemExit(
            f"SQLite table {table.name} has columns not present in target metadata: "
            + ", ".join(unknown)
        )
    return source_names


def truncate_target(engine) -> None:
    with engine.begin() as target:
        for table in reversed(ordered_tables()):
            target.execute(delete(table))


def import_tables(
    source: sqlite3.Connection,
    engine,
    *,
    batch_size: int,
) -> dict[str, int]:
    imported: dict[str, int] = {}
    with engine.begin() as target:
        for table in ordered_tables():
            columns = table_columns_for_import(source, table)
            count = 0
            for batch in iter_source_batches(source, table, columns, batch_size):
                target.execute(table.insert(), batch)
                count += len(batch)
            imported[table.name] = count
    return imported


def target_count(engine, table_name: str) -> int:
    table = Base.metadata.tables[table_name]
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def order_columns(table, columns: list[str]) -> list[str]:
    pk = [column.name for column in table.primary_key.columns if column.name in columns]
    if pk:
        return pk
    return columns


def digest_rows(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_digest(source: sqlite3.Connection, table, columns: list[str]) -> str:
    ordered = order_columns(table, columns)
    quoted = ", ".join(f'"{name}"' for name in columns)
    order_by = ", ".join(f'"{name}"' for name in ordered)
    column_map = {column.name: column for column in table.columns}
    rows = []
    for row in source.execute(f'SELECT {quoted} FROM "{table.name}" ORDER BY {order_by}').fetchall():
        rows.append({name: convert_value(column_map[name], row[name]) for name in columns})
    return digest_rows(rows)


def target_digest(engine, table, columns: list[str]) -> str:
    ordered = order_columns(table, columns)
    selected = [table.c[name] for name in columns]
    stmt = select(*selected).order_by(*(table.c[name] for name in ordered))
    with engine.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(stmt).fetchall()]
    return digest_rows(rows)


def verify(source: sqlite3.Connection, engine) -> dict[str, Any]:
    report: dict[str, Any] = {"tables": {}, "digests": {}}
    failures: list[str] = []
    for table in ordered_tables():
        columns = table_columns_for_import(source, table)
        src = source_count(source, table.name)
        dst = target_count(engine, table.name)
        report["tables"][table.name] = {"source": src, "target": dst, "ok": src == dst}
        if src != dst:
            failures.append(f"{table.name}: source={src} target={dst}")
        if table.name in HIGH_VALUE_TABLES:
            src_digest = source_digest(source, table, columns)
            dst_digest = target_digest(engine, table, columns)
            report["digests"][table.name] = {
                "source": src_digest,
                "target": dst_digest,
                "ok": src_digest == dst_digest,
            }
            if src_digest != dst_digest:
                failures.append(f"{table.name}: digest mismatch")

    rendered_url = str(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    head = ScriptDirectory.from_config(alembic_config(rendered_url)).get_current_head()
    report["alembic"] = {
        "current": current,
        "expected": head,
        "ok": current == head,
    }
    if current != head:
        failures.append(f"alembic head mismatch: {current}")
    report["ok"] = not failures
    report["failures"] = failures
    return report


def main() -> int:
    args = parse_args()
    batch_size = max(int(args.batch_size or 500), 1)
    source = source_connect(Path(args.sqlite_path).expanduser())
    try:
        check_source_tables(source)
        engine = get_engine(args.database_url)
        init_db(engine)
        if args.verify_only:
            report = verify(source, engine)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 1
        if args.dry_run:
            report = {
                table.name: source_count(source, table.name)
                for table in ordered_tables()
            }
            print(json.dumps({"dry_run": True, "source_counts": report}, ensure_ascii=False, indent=2))
            return 0
        if args.truncate_target:
            truncate_target(engine)
        imported = import_tables(source, engine, batch_size=batch_size)
        report = verify(source, engine)
        report["imported"] = imported
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1
    finally:
        source.close()


if __name__ == "__main__":
    raise SystemExit(main())
