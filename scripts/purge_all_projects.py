#!/usr/bin/env python3
"""Delete every ForWin project together with its derived external stores.

Defaults to a dry run. Nothing is deleted unless ``--yes-delete-all-projects``
is passed. Global publisher extension/browser-session tables are never touched.

Exit codes: 0 success, 2 external store errors, 3 blocked by active work.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from forwin.config import InfrastructureConfig
from forwin.generation.task_repository import TERMINAL_GENERATION_STATUSES
from forwin.http.project_support import _delete_project
from forwin.http.runtime import UPLOAD_TERMINAL_STATUSES
from forwin.llm_kb.store import DEFAULT_LLM_KB_ROOT
from forwin.models import GenerationTask, Project, PublisherUploadJob
from forwin.models.base import get_engine, get_session_factory, require_v5_schema
from forwin.obsidian.exporter import DEFAULT_VAULT_ROOT
from forwin.publisher_runtime.covers import PublisherCoverService
from forwin.retrieval.memory_index import _create_qdrant_client

DEFAULT_PUBLISHER_COVER_DIR = "data/publisher_covers"
_MAX_BLOCKERS_LISTED = 20


@dataclass(frozen=True)
class MinioTarget:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    prefix: str
    secure: bool


@dataclass(frozen=True)
class PurgeTargets:
    artifact_root: Path
    minio: MinioTarget | None
    qdrant_url: str
    qdrant_collection_names: tuple[str, ...]
    local_project_roots: tuple[Path, ...]
    publisher_cover_dir: Path


@dataclass(frozen=True)
class PurgeReport:
    dry_run: bool
    project_ids: tuple[str, ...]
    deleted_projects: int
    removed_paths: tuple[str, ...]
    removed_minio_objects: int
    dropped_qdrant_collections: tuple[str, ...]
    errors: tuple[str, ...]


class PurgeBlocked(RuntimeError):
    """Raised when active generation or upload work makes a purge unsafe."""


def targets_from_config(config: InfrastructureConfig) -> PurgeTargets:
    if not str(config.artifact_root or "").strip():
        raise ValueError("FORWIN_ARTIFACT_ROOT must not be empty.")
    minio: MinioTarget | None = None
    if str(config.artifact_backend or "").strip().lower() == "minio":
        minio = MinioTarget(
            endpoint=config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            bucket=config.minio_bucket,
            prefix=str(config.minio_prefix or "").strip("/"),
            secure=bool(config.minio_secure),
        )
    collection_names = tuple(
        dict.fromkeys(
            name.strip()
            for name in (config.qdrant_collection, config.llm_kb_qdrant_collection)
            if str(name or "").strip()
        )
    )
    return PurgeTargets(
        artifact_root=Path(config.artifact_root),
        minio=minio,
        qdrant_url=config.qdrant_url,
        qdrant_collection_names=collection_names,
        local_project_roots=(DEFAULT_LLM_KB_ROOT, DEFAULT_VAULT_ROOT),
        publisher_cover_dir=Path(
            os.environ.get("FORWIN_PUBLISHER_COVER_DIR") or DEFAULT_PUBLISHER_COVER_DIR
        ),
    )


def purge_all_projects(
    *,
    session_factory,
    targets: PurgeTargets,
    dry_run: bool,
    qdrant_client=None,
    minio_client=None,
    cover_service=None,
) -> PurgeReport:
    with session_factory() as session:
        _raise_if_active_work(session)
        project_ids = tuple(
            session.execute(select(Project.id).order_by(Project.id)).scalars().all()
        )

    deleted_projects = 0
    if not dry_run:
        for project_id in project_ids:
            with session_factory() as session:
                _delete_project(session, project_id)
                session.commit()
            deleted_projects += 1

    errors: list[str] = []
    removed_paths = _purge_local_paths(targets, dry_run=dry_run, errors=errors)
    if not dry_run:
        removed_paths.extend(
            _cleanup_covers(
                session_factory=session_factory,
                targets=targets,
                cover_service=cover_service,
                errors=errors,
            )
        )
    dropped_collections = _purge_qdrant(
        targets, qdrant_client, dry_run=dry_run, errors=errors
    )
    removed_minio_objects = _purge_minio(
        targets.minio, minio_client, dry_run=dry_run, errors=errors
    )
    return PurgeReport(
        dry_run=dry_run,
        project_ids=project_ids,
        deleted_projects=deleted_projects,
        removed_paths=tuple(removed_paths),
        removed_minio_objects=removed_minio_objects,
        dropped_qdrant_collections=tuple(dropped_collections),
        errors=tuple(errors),
    )


def _raise_if_active_work(session) -> None:
    task_ids = (
        session.execute(
            select(GenerationTask.id)
            .where(GenerationTask.status.notin_(tuple(TERMINAL_GENERATION_STATUSES)))
            .order_by(GenerationTask.id)
        )
        .scalars()
        .all()
    )
    job_ids = (
        session.execute(
            select(PublisherUploadJob.id)
            .where(PublisherUploadJob.status.notin_(tuple(UPLOAD_TERMINAL_STATUSES)))
            .order_by(PublisherUploadJob.id)
        )
        .scalars()
        .all()
    )
    if not task_ids and not job_ids:
        return
    parts: list[str] = []
    if task_ids:
        parts.append(f"generation tasks {_format_ids(task_ids)}")
    if job_ids:
        parts.append(f"publisher upload jobs {_format_ids(job_ids)}")
    raise PurgeBlocked(
        "active work must finish or be terminated before purging: " + "; ".join(parts)
    )


def _format_ids(ids: list[str]) -> str:
    listed = ", ".join(ids[:_MAX_BLOCKERS_LISTED])
    extra = len(ids) - _MAX_BLOCKERS_LISTED
    return f"[{listed}{f', +{extra} more' if extra > 0 else ''}]"


def _purge_local_paths(
    targets: PurgeTargets, *, dry_run: bool, errors: list[str]
) -> list[str]:
    # artifact_root/projects holds only per-project data; everything inside goes.
    # The other roots (llm_kb, vaults) hold one directory per project; only
    # directories are removed so unrelated files next to them survive.
    planned = _children(targets.artifact_root / "projects", directories_only=False)
    for root in targets.local_project_roots:
        planned.extend(_children(root, directories_only=True))
    removed: list[str] = []
    for path in planned:
        if dry_run:
            removed.append(str(path))
            continue
        try:
            _remove_path(path)
        except OSError as exc:
            errors.append(f"local path {path}: {exc}")
        else:
            removed.append(str(path))
    return removed


def _children(root: Path, *, directories_only: bool) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        child
        for child in sorted(root.iterdir())
        if not directories_only or (child.is_dir() and not child.is_symlink())
    ]


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _cleanup_covers(
    *,
    session_factory,
    targets: PurgeTargets,
    cover_service,
    errors: list[str],
) -> list[str]:
    try:
        service = cover_service or PublisherCoverService(
            session_factory=session_factory,
            cover_dir=targets.publisher_cover_dir,
        )
        return [str(path) for path in service.cleanup_orphaned_files()]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"publisher covers: {exc}")
        return []


def _purge_qdrant(
    targets: PurgeTargets, client: Any, *, dry_run: bool, errors: list[str]
) -> list[str]:
    if not targets.qdrant_collection_names:
        return []
    patterns = [
        re.compile(rf"^{re.escape(name)}(?:_\d+d)?$")
        for name in targets.qdrant_collection_names
    ]
    owns_client = client is None
    dropped: list[str] = []
    try:
        if client is None:
            client = _create_qdrant_client(targets.qdrant_url)
        existing = [item.name for item in client.get_collections().collections]
        for name in sorted(existing):
            if not any(pattern.match(name) for pattern in patterns):
                continue
            if dry_run:
                dropped.append(name)
                continue
            try:
                client.delete_collection(collection_name=name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"qdrant collection {name}: {exc}")
            else:
                dropped.append(name)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"qdrant {targets.qdrant_url}: {exc}")
    finally:
        if owns_client:
            _close_quietly(client)
    return dropped


def _create_minio_client(target: MinioTarget) -> Any:
    from minio import Minio

    return Minio(
        target.endpoint,
        access_key=target.access_key,
        secret_key=target.secret_key,
        secure=target.secure,
    )


def _purge_minio(
    target: MinioTarget | None, client: Any, *, dry_run: bool, errors: list[str]
) -> int:
    if target is None:
        return 0
    key_prefix = f"{target.prefix.strip('/')}/projects/".lstrip("/")
    removed = 0
    try:
        client = client or _create_minio_client(target)
        # Re-check the prefix locally: the delete below is irreversible.
        names = [
            item.object_name
            for item in client.list_objects(
                target.bucket, prefix=key_prefix, recursive=True
            )
            if str(item.object_name).startswith(key_prefix)
        ]
        for name in names:
            if dry_run:
                removed += 1
                continue
            try:
                client.remove_object(target.bucket, name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"minio object {name}: {exc}")
            else:
                removed += 1
    except Exception as exc:  # noqa: BLE001
        errors.append(f"minio {target.endpoint}/{target.bucket}: {exc}")
    return removed


def _close_quietly(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _print_report(report: PurgeReport) -> None:
    mode = "DRY-RUN (nothing was deleted)" if report.dry_run else "PURGE COMPLETE"
    print(f"{mode}")
    print(f"projects: {len(report.project_ids)} found, {report.deleted_projects} deleted")
    for project_id in report.project_ids:
        print(f"  project {project_id}")
    verb = "would remove" if report.dry_run else "removed"
    print(f"local paths {verb}: {len(report.removed_paths)}")
    for path in report.removed_paths:
        print(f"  {path}")
    print(f"minio objects {verb}: {report.removed_minio_objects}")
    print(f"qdrant collections {verb}: {len(report.dropped_qdrant_collections)}")
    for name in report.dropped_qdrant_collections:
        print(f"  {name}")
    if report.errors:
        print(f"errors: {len(report.errors)}", file=sys.stderr)
        for error in report.errors:
            print(f"  {error}", file=sys.stderr)
    if report.dry_run:
        print("Re-run with --yes-delete-all-projects to delete.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delete every project and its derived stores (database rows, local "
            "artifacts, LLM KB, vaults, covers, Qdrant collections, MinIO "
            "objects). Dry-run unless --yes-delete-all-projects is given."
        )
    )
    parser.add_argument(
        "--yes-delete-all-projects",
        action="store_true",
        help="Actually delete. Without this flag the script only reports.",
    )
    args = parser.parse_args(argv)

    config = InfrastructureConfig.from_env()
    targets = targets_from_config(config)
    engine = get_engine(config.database_url)
    try:
        require_v5_schema(engine)
        session_factory = get_session_factory(engine)
        try:
            report = purge_all_projects(
                session_factory=session_factory,
                targets=targets,
                dry_run=not args.yes_delete_all_projects,
            )
        except PurgeBlocked as exc:
            print(f"purge blocked: {exc}", file=sys.stderr)
            return 3
        _print_report(report)
        return 2 if report.errors else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
