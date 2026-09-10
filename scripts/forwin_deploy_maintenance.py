"""One-shot maintenance from the verified candidate image; never starts a role."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def schema_heads():
    from alembic.migration import MigrationContext

    from forwin.config import InfrastructureConfig
    from forwin.models.base import get_engine

    engine = get_engine(InfrastructureConfig.from_env().database_url)
    try:
        with engine.connect() as connection:
            return list(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=("backup", "migrate", "verify"), required=True
    )
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[0-9a-f]{40}", args.source_revision)
        or os.environ.get("FORWIN_SOURCE_REVISION") != args.source_revision
    ):
        print("maintenance source revision mismatch", file=sys.stderr)
        return 1
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.release_id):
        print("invalid release identifier", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parents[1]
    try:
        before = schema_heads()
        backup_name = None
        if args.phase == "backup":
            destination = (
                Path(os.environ.get("FORWIN_DATA_DIR", "/app/data"))
                / "deploy-backups"
                / args.release_id
            )
            destination.mkdir(parents=True, exist_ok=False, mode=0o700)
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/backup_forwin_data.py"),
                    "--output-dir",
                    str(destination),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            backup = Path(result.stdout.strip())
            if backup.parent != destination:
                raise RuntimeError("backup returned an unexpected path")
            subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/verify_forwin_backup.py"),
                    str(backup),
                ],
                check=True,
            )
            (destination / "release.json").write_text(
                json.dumps(
                    {"source_revision": args.source_revision, "backup": backup.name}
                )
                + "\n"
            )
            backup_name = str(backup.relative_to(destination.parent.parent))
        elif args.phase == "migrate":
            # This public entry selects the legacy chain and owns the single PG
            # transaction. Do not replace with alembic stamp/upgrade subprocesses.
            subprocess.run(
                [sys.executable, "-m", "forwin.migrations"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            from forwin.config import InfrastructureConfig
            from forwin.models.base import get_engine, require_v5_schema

            engine = get_engine(InfrastructureConfig.from_env().database_url)
            try:
                require_v5_schema(engine)
            finally:
                engine.dispose()
        print(
            "FORWIN_MAINT_RESULT "
            + json.dumps(
                {
                    "phase": args.phase,
                    "source_revision": args.source_revision,
                    "schema_before": before,
                    "schema_after": schema_heads(),
                    "backup": backup_name,
                }
            )
        )
    except Exception:  # noqa: BLE001 - report failure without leaking runtime credentials.
        # The parent keeps the migration_started phase on unknown outcomes.
        print(
            f"maintenance {args.phase} failed; preserve the release and inspect the task",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
