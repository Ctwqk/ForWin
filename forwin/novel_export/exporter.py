"""Atomically advance a private local book projection after verifying its files."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path

from forwin.obsidian.exporter import (
    ensure_managed_directory,
    read_managed_text,
    write_managed_text_if_changed,
)

from .snapshot import BookExport, digest


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_export(root: Path, snapshot: BookExport, book: str) -> None:
    snapshot = BookExport.model_validate(snapshot.model_dump())
    if digest(book) != snapshot.book_sha256:
        raise ValueError("export book hash differs from frozen manifest")
    root = Path(root).absolute()
    if ".." in root.parts or any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("export root must not contain traversal or a symlink")
    root_fd = _open_export_root(root)
    try:
        name = f".{snapshot.project_id}.lock"
        try:
            lock_fd = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
        except FileExistsError:
            # Separate creation and opening an existing lock. Concurrent
            # O_CREAT|O_NOFOLLOW alone can fail with ENOENT on macOS.
            lock_fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ValueError("export lock must be a regular file")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _write_locked(root_fd, snapshot, book)
        finally:
            os.close(lock_fd)
    finally:
        os.close(root_fd)


def _open_export_root(root: Path) -> int:
    """Create/open each ancestor without following path replacements."""
    descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        os.fchmod(descriptor, 0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_locked(root: int, snapshot, book):
    project = snapshot.project_id
    relative = f"{project}/revisions/{snapshot.book_revision}"
    ensure_managed_directory(root, relative)
    manifest = _json(snapshot.model_dump(mode="json"))
    for name, content in (("book.md", book), ("manifest.json", manifest)):
        # An existing different revision file is evidence of corruption or a
        # conflicting snapshot. Never silently replace it with a new meaning.
        path = f"{relative}/{name}"
        write_managed_text_if_changed(root, path, content, expected_current=None)
        if read_managed_text(root, path) != content:
            raise RuntimeError("export file verification failed")
    _sync_directories(root, relative)
    current_path = f"{project}/current.json"
    current_text = read_managed_text(root, current_path)
    current = json.loads(current_text) if current_text is not None else None
    if current is not None:
        if (
            not isinstance(current, dict)
            or current.get("project_id") != project
            or type(current.get("book_revision")) is not int
        ):
            raise ValueError("export current pointer is invalid")
        if current["book_revision"] > snapshot.book_revision:
            return
    pointer = {
        "schema_version": 1,
        "project_id": project,
        "book_revision": snapshot.book_revision,
        "manifest": f"revisions/{snapshot.book_revision}/manifest.json",
        "book": f"revisions/{snapshot.book_revision}/book.md",
        "manifest_sha256": digest(manifest),
        "book_sha256": snapshot.book_sha256,
    }
    desired = _json(pointer)
    if current is not None and current["book_revision"] == snapshot.book_revision:
        if current_text != desired:
            raise ValueError("export current revision has conflicting identity")
        return
    write_managed_text_if_changed(
        root, current_path, desired, expected_current=current_text
    )
    _sync_directories(root, project)


def _sync_directories(root: int, relative: str) -> None:
    """Persist file renames and every new ancestor before advertising a tree."""
    descriptors = [os.dup(root)]
    try:
        for component in relative.split("/"):
            descriptors.append(
                os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptors[-1],
                )
            )
        for descriptor in reversed(descriptors):
            os.fsync(descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
