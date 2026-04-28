from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_backup_script_generates_manifest_without_env_values(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "pg_dump",
        """#!/usr/bin/env python3
import pathlib
import sys
out = pathlib.Path(sys.argv[sys.argv.index('--file') + 1])
out.write_bytes(b'fake postgres dump')
""",
    )

    data_dir = tmp_path / "data"
    artifacts = data_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (data_dir / "runtime_settings.json").write_text('{"model":"test"}\n', encoding="utf-8")
    (artifacts / "draft.txt").write_text("chapter text\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("MINIMAX_API_KEY=secret-value\nFORWIN_DATABASE_URL=postgres://secret\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/backup_forwin_data.py",
            "--database-url",
            "postgresql+psycopg://forwin:secret@localhost:5432/forwin",
            "--data-dir",
            str(data_dir),
            "--output-dir",
            str(tmp_path / "backups"),
            "--env-file",
            str(env_file),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    backup_root = Path(result.stdout.strip())
    manifest = json.loads((backup_root / "manifest.json").read_text(encoding="utf-8"))
    roles = {item["role"] for item in manifest["files"]}
    assert {"postgres_dump", "runtime_settings", "artifact", "env_keys"} <= roles
    assert "secret" not in manifest["database"]["url"]
    assert "MINIMAX_API_KEY" in (backup_root / ".env.keys.txt").read_text(encoding="utf-8")
    assert "secret-value" not in (backup_root / ".env.keys.txt").read_text(encoding="utf-8")


def test_verify_backup_script_detects_checksum_mismatch(tmp_path: Path) -> None:
    backup_root = tmp_path / "forwin-backup-test"
    data_dir = backup_root / "data"
    data_dir.mkdir(parents=True)
    dump = data_dir / "forwin.pg_dump"
    dump.write_bytes(b"fake dump")
    manifest = {
        "format_version": 1,
        "files": [
            {
                "path": "data/forwin.pg_dump",
                "role": "postgres_dump",
                "size": dump.stat().st_size,
                "sha256": "0" * 64,
            }
        ],
    }
    (backup_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "scripts/verify_forwin_backup.py", str(backup_root)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "checksum mismatch" in result.stderr


def test_verify_backup_script_accepts_valid_manifest_with_pg_restore_list(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "pg_restore",
        """#!/usr/bin/env python3
import sys
raise SystemExit(0 if '--list' in sys.argv else 1)
""",
    )

    backup_root = tmp_path / "forwin-backup-test"
    data_dir = backup_root / "data"
    data_dir.mkdir(parents=True)
    dump = data_dir / "forwin.pg_dump"
    dump.write_bytes(b"fake dump")
    manifest = {
        "format_version": 1,
        "files": [
            {
                "path": "data/forwin.pg_dump",
                "role": "postgres_dump",
                "size": dump.stat().st_size,
                "sha256": _sha256(dump),
            }
        ],
    }
    (backup_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        [sys.executable, "scripts/verify_forwin_backup.py", str(backup_root)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "backup verification ok" in result.stdout
