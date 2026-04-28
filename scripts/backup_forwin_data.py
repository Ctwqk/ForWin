#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url


DEFAULT_DATABASE_URL = "postgresql+psycopg://forwin:forwin@localhost:5432/forwin"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Back up ForWin personal-server data.")
    parser.add_argument("--database-url", default=os.environ.get("FORWIN_DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--data-dir", default=os.environ.get("FORWIN_DATA_DIR", "data"))
    parser.add_argument("--output-dir", default="backups")
    parser.add_argument("--runtime-settings", default=os.environ.get("FORWIN_RUNTIME_SETTINGS_PATH", ""))
    parser.add_argument("--artifact-root", default=os.environ.get("FORWIN_ARTIFACT_ROOT", ""))
    parser.add_argument("--artifact-backend", default=os.environ.get("FORWIN_ARTIFACT_BACKEND", "local"))
    parser.add_argument("--env-file", default=os.environ.get("FORWIN_ENV_FILE", ".env"))
    parser.add_argument("--zip", action="store_true", help="Also create a .zip archive of the backup directory.")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip()


def masked_database_url(database_url: str) -> str:
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return "<invalid database url>"


def libpq_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("backup requires a PostgreSQL database URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def copy_file_if_present(source: Path, destination: Path) -> bool:
    if not source.exists() or not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def write_env_keys(env_file: Path, destination: Path) -> list[str]:
    keys: list[str] = []
    if env_file.exists() and env_file.is_file():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key:
                keys.append(key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(sorted(set(keys))) + ("\n" if keys else ""), encoding="utf-8")
    return sorted(set(keys))


def add_manifest_file(files: list[dict[str, object]], backup_root: Path, path: Path, *, source: str, role: str) -> None:
    files.append(
        {
            "path": path.relative_to(backup_root).as_posix(),
            "source": source,
            "role": role,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    )


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir)
    backup_root = output_dir / f"forwin-backup-{timestamp}"
    data_dir = Path(args.data_dir)
    backup_data_dir = backup_root / "data"
    backup_data_dir.mkdir(parents=True, exist_ok=False)

    files: list[dict[str, object]] = []
    notes: list[str] = []

    dump_path = backup_data_dir / "forwin.pg_dump"
    subprocess.run(
        ["pg_dump", "--format=custom", "--file", str(dump_path), libpq_database_url(str(args.database_url))],
        check=True,
    )
    add_manifest_file(files, backup_root, dump_path, source=masked_database_url(args.database_url), role="postgres_dump")

    runtime_settings = Path(args.runtime_settings) if args.runtime_settings else data_dir / "runtime_settings.json"
    runtime_dest = backup_data_dir / "runtime_settings.json"
    if copy_file_if_present(runtime_settings, runtime_dest):
        add_manifest_file(files, backup_root, runtime_dest, source=str(runtime_settings), role="runtime_settings")

    artifact_backend = str(args.artifact_backend or "local").strip().lower()
    artifact_root = Path(args.artifact_root) if args.artifact_root else data_dir / "artifacts"
    if artifact_backend == "local":
        artifact_dest = backup_data_dir / "artifacts"
        if artifact_root.exists() and artifact_root.is_dir():
            shutil.copytree(artifact_root, artifact_dest)
            for path in sorted(item for item in artifact_dest.rglob("*") if item.is_file()):
                add_manifest_file(files, backup_root, path, source=str(artifact_root / path.relative_to(artifact_dest)), role="artifact")
    elif artifact_backend == "minio":
        notes.append("FORWIN_ARTIFACT_BACKEND=minio: back up the MinIO volume/bucket separately.")
    else:
        notes.append(f"Unknown artifact backend {artifact_backend!r}: artifact backup was not attempted.")

    env_keys_dest = backup_root / ".env.keys.txt"
    env_keys = write_env_keys(Path(args.env_file), env_keys_dest)
    add_manifest_file(files, backup_root, env_keys_dest, source=str(args.env_file), role="env_keys")

    if os.environ.get("FORWIN_RETRIEVAL_BACKEND", "").strip().lower() == "qdrant":
        notes.append("FORWIN_RETRIEVAL_BACKEND=qdrant: back up the Qdrant volume separately.")

    manifest = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "backend": "postgresql",
            "url": masked_database_url(args.database_url),
        },
        "artifact_backend": artifact_backend,
        "git_commit": git_commit(),
        "python_version": sys.version,
        "env_keys": env_keys,
        "notes": notes,
        "files": files,
    }
    manifest_path = backup_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.zip:
        shutil.make_archive(str(backup_root), "zip", root_dir=backup_root)

    print(backup_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
