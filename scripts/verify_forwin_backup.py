#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a ForWin backup directory or zip file.")
    parser.add_argument("backup")
    parser.add_argument(
        "--restore-database-url",
        default="",
        help="Optional empty PostgreSQL database URL for full restore verification.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_backup_root(path: Path, tempdir: tempfile.TemporaryDirectory[str] | None) -> Path:
    if path.is_dir():
        return path
    if path.suffix.lower() != ".zip":
        raise RuntimeError(f"backup path is neither a directory nor a zip file: {path}")
    if tempdir is None:
        raise RuntimeError("internal error: tempdir missing for zip extraction")
    root = Path(tempdir.name)
    with zipfile.ZipFile(path) as archive:
        archive.extractall(root)
    if (root / "manifest.json").exists():
        return root
    candidates = [item for item in root.iterdir() if item.is_dir()]
    if len(candidates) == 1 and (candidates[0] / "manifest.json").exists():
        return candidates[0]
    return root


def verify_manifest_files(backup_root: Path, manifest: dict) -> list[Path]:
    dump_files: list[Path] = []
    for item in manifest.get("files", []):
        if not isinstance(item, dict):
            raise RuntimeError("manifest contains a non-object file entry")
        rel_path = str(item.get("path") or "").strip()
        expected_hash = str(item.get("sha256") or "").strip()
        expected_size = int(item.get("size") or 0)
        if not rel_path:
            raise RuntimeError("manifest file entry is missing path")
        path = backup_root / rel_path
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"manifest file missing: {rel_path}")
        if path.stat().st_size != expected_size:
            raise RuntimeError(f"manifest file size mismatch: {rel_path}")
        if sha256_file(path) != expected_hash:
            raise RuntimeError(f"manifest checksum mismatch: {rel_path}")
        if str(item.get("role") or "") == "postgres_dump":
            dump_files.append(path)
    if not dump_files:
        raise RuntimeError("manifest does not contain a PostgreSQL dump")
    return dump_files


def verify_pg_restore_list(dump_path: Path) -> None:
    subprocess.run(["pg_restore", "--list", str(dump_path)], check=True, capture_output=True, text=True)


def libpq_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("restore verification requires a PostgreSQL database URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def sqlalchemy_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("restore verification requires a PostgreSQL database URL")
    if url.drivername == "postgresql":
        return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    return database_url


def verify_full_restore(dump_path: Path, database_url: str) -> None:
    subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", libpq_database_url(database_url), str(dump_path)],
        check=True,
    )
    engine = create_engine(sqlalchemy_database_url(database_url), pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            table_count = conn.execute(
                text(
                    """
                    SELECT count(*)
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    """
                )
            ).scalar_one()
        if int(table_count or 0) <= 0:
            raise RuntimeError("restore verification found no public tables")
    finally:
        engine.dispose()


def main() -> int:
    args = parse_args()
    tempdir: tempfile.TemporaryDirectory[str] | None = None
    try:
        backup_path = Path(args.backup)
        if backup_path.suffix.lower() == ".zip":
            tempdir = tempfile.TemporaryDirectory()
        backup_root = load_backup_root(backup_path, tempdir)
        manifest_path = backup_root / "manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("manifest.json is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dump_files = verify_manifest_files(backup_root, manifest)
        dump_path = dump_files[0]
        verify_pg_restore_list(dump_path)
        if args.restore_database_url:
            verify_full_restore(dump_path, args.restore_database_url)
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"backup verification failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if tempdir is not None:
            tempdir.cleanup()

    print("backup verification ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
