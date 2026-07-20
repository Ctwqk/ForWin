from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from forwin.http.adapters.api_projection_routes import build_handlers
from forwin.knowledge_system.page_repository import KnowledgePageRepository
from forwin.models.base import Base
from forwin.models.knowledge import KnowledgeProjectionPageRow
from forwin.models.project import Project
from forwin.obsidian import exporter as exporter_module
from forwin.obsidian.canvas import render_canvas
from forwin.obsidian.exporter import (
    OBSIDIAN_MANIFEST_FILENAME,
    ObsidianExporter,
    converge_managed_projection_files,
    load_managed_projection_manifest,
    write_managed_text_if_changed,
)


def _write(root: Path, rel_path: str, content: str) -> Path:
    path = root.joinpath(*rel_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _converge(
    root: Path,
    desired_paths: set[str],
    *,
    as_of_chapter: int = 1,
):
    return converge_managed_projection_files(
        root,
        project_id="project-1",
        as_of_chapter=as_of_chapter,
        desired_paths=desired_paths,
        prior_state=load_managed_projection_manifest(root, "project-1"),
    )


def test_unchanged_stale_file_deletes_unknown_survives_and_replay_is_idle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    old = _write(root, "03_Actors/old.md", "managed old")
    keep = _write(root, "03_Actors/keep.md", "managed keep")
    unknown = _write(root, "human-notes.md", "unknown human file")

    initial = _converge(root, {"03_Actors/old.md", "03_Actors/keep.md"})
    assert initial.deletion_enabled is False
    assert initial.deletion_disabled_reason == "missing_manifest"
    assert initial.manifest_written is True

    converged = _converge(root, {"03_Actors/keep.md"}, as_of_chapter=2)
    assert converged.deletion_enabled is True
    assert converged.deleted_files == ("03_Actors/old.md",)
    assert not old.exists()
    assert keep.exists()
    assert unknown.exists()

    manifest = root / OBSIDIAN_MANIFEST_FILENAME
    manifest_mtime = manifest.stat().st_mtime_ns
    keep_mtime = keep.stat().st_mtime_ns
    replay = _converge(root, {"03_Actors/keep.md"}, as_of_chapter=2)

    assert replay.source_digest == converged.source_digest
    assert replay.manifest_written is False
    assert replay.deleted_files == ()
    assert manifest.stat().st_mtime_ns == manifest_mtime
    assert keep.stat().st_mtime_ns == keep_mtime


def test_modified_stale_file_is_retained_and_ownership_is_released(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    stale = _write(root, "03_Actors/stale.md", "managed content")
    _write(root, "03_Actors/keep.md", "keep")
    _converge(root, {"03_Actors/stale.md", "03_Actors/keep.md"})
    stale.write_text("human modified content", encoding="utf-8")

    retained = _converge(root, {"03_Actors/keep.md"}, as_of_chapter=2)

    assert retained.retained_human_modified == ("03_Actors/stale.md",)
    assert stale.read_text(encoding="utf-8") == "human modified content"
    replay = _converge(root, {"03_Actors/keep.md"}, as_of_chapter=2)
    assert replay.retained_human_modified == ()
    assert stale.exists()


@pytest.mark.parametrize(
    ("manifest_content", "expected_reason"),
    [
        (None, "missing_manifest"),
        ("{not-json", "corrupt_manifest"),
    ],
)
def test_missing_or_corrupt_manifest_disables_deletion_for_the_run(
    tmp_path: Path,
    manifest_content: str | None,
    expected_reason: str,
) -> None:
    root = tmp_path / expected_reason
    root.mkdir()
    stale = _write(root, "stale.md", "unowned stale")
    _write(root, "keep.md", "keep")
    if manifest_content is not None:
        (root / OBSIDIAN_MANIFEST_FILENAME).write_text(
            manifest_content,
            encoding="utf-8",
        )

    result = _converge(root, {"keep.md"})

    assert result.deletion_enabled is False
    assert result.deletion_disabled_reason == expected_reason
    assert stale.exists()
    assert load_managed_projection_manifest(
        root, "project-1"
    ).deletion_enabled is True


def test_traversal_and_symlink_manifest_entries_are_retained_unsafe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    link = _write(root, "linked.md", "managed link target")
    _write(root, "keep.md", "keep")
    _converge(root, {"linked.md", "keep.md"})

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link.unlink()
    link.symlink_to(outside)
    manifest_path = root / OBSIDIAN_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["../outside.md"] = {
        "sha256": hashlib.sha256(b"outside").hexdigest(),
        "kind": "page",
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = _converge(root, {"keep.md"}, as_of_chapter=2)

    assert result.deletion_enabled is True
    assert set(result.retained_unsafe) == {"../outside.md", "linked.md"}
    assert link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_parent_symlink_swap_cannot_redirect_stale_delete_outside_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    managed_dir = root / "managed"
    managed_dir.mkdir(parents=True)
    stale = _write(root, "managed/stale.md", "managed stale")
    _write(root, "keep.md", "keep")
    _converge(root, {"managed/stale.md", "keep.md"})
    stale_inode = stale.stat().st_ino

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "stale.md"
    outside_file.write_text("outside must survive", encoding="utf-8")
    moved_dir = root / "managed-original"
    swapped = False
    original_read = exporter_module._read_all_fd

    def read_then_swap(file_fd: int) -> bytes:
        nonlocal swapped
        content = original_read(file_fd)
        if not swapped and exporter_module.os.fstat(file_fd).st_ino == stale_inode:
            swapped = True
            managed_dir.rename(moved_dir)
            managed_dir.symlink_to(outside_dir, target_is_directory=True)
        return content

    monkeypatch.setattr(exporter_module, "_read_all_fd", read_then_swap)
    result = _converge(root, {"keep.md"}, as_of_chapter=2)

    assert result.deleted_files == ("managed/stale.md",)
    assert not (moved_dir / "stale.md").exists()
    assert outside_file.read_text(encoding="utf-8") == "outside must survive"


def test_parent_symlink_swap_cannot_redirect_generated_write_outside_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    managed_dir = root / "managed"
    managed_dir.mkdir(parents=True)
    _write(root, "managed/page.md", "old managed page")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "page.md"
    outside_file.write_text("outside must survive", encoding="utf-8")
    moved_dir = root / "managed-original"
    original_write = exporter_module._write_all_fd
    swapped = False

    def write_then_swap(file_fd: int, content: bytes) -> None:
        nonlocal swapped
        original_write(file_fd, content)
        if not swapped:
            swapped = True
            managed_dir.rename(moved_dir)
            managed_dir.symlink_to(outside_dir, target_is_directory=True)

    monkeypatch.setattr(exporter_module, "_write_all_fd", write_then_swap)
    assert write_managed_text_if_changed(
        root,
        "managed/page.md",
        "new managed page",
        expected_current="old managed page",
    ) is True

    assert (moved_dir / "page.md").read_text(encoding="utf-8") == "new managed page"
    assert outside_file.read_text(encoding="utf-8") == "outside must survive"


def test_manifest_replace_failure_preserves_previous_manifest_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    _write(root, "keep.md", "keep")
    _converge(root, {"keep.md"}, as_of_chapter=1)
    manifest_path = root / OBSIDIAN_MANIFEST_FILENAME
    previous = manifest_path.read_text(encoding="utf-8")

    def fail_replace(_source, _target, **_kwargs) -> None:
        raise OSError("replace interrupted")

    monkeypatch.setattr(exporter_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace interrupted"):
        _converge(root, {"keep.md"}, as_of_chapter=2)

    assert manifest_path.read_text(encoding="utf-8") == previous
    assert list(root.glob(".forwin-write-*.tmp")) == []


def test_generated_text_and_canvas_skip_byte_identical_replay(
    tmp_path: Path,
) -> None:
    text_path = tmp_path / "AGENTS.md"
    assert write_managed_text_if_changed(tmp_path, "AGENTS.md", "rules") is True
    text_mtime = text_path.stat().st_mtime_ns
    assert write_managed_text_if_changed(tmp_path, "AGENTS.md", "rules") is False
    assert text_path.stat().st_mtime_ns == text_mtime

    canvas_path = tmp_path / "map.canvas"
    canvas_content = render_canvas(
        page_paths=["node.md"],
        edges=[],
    )
    assert write_managed_text_if_changed(
        tmp_path,
        "map.canvas",
        canvas_content,
    ) is True
    canvas_mtime = canvas_path.stat().st_mtime_ns
    assert write_managed_text_if_changed(
        tmp_path,
        "map.canvas",
        canvas_content,
    ) is False
    assert canvas_path.stat().st_mtime_ns == canvas_mtime


def test_world_and_map_nodes_have_distinct_managed_namespaces() -> None:
    exporter = object.__new__(ObsidianExporter)
    world_path = exporter._node_relpath(
        SimpleNamespace(node_type="location", id="loc_city", name="City")
    )
    map_path = exporter._map_node_relpath(
        SimpleNamespace(node_type="settlement", id="loc_city", name="City")
    )

    assert world_path == "02_Map/Nodes/World_City_loc_city.md"
    assert map_path == "02_Map/Nodes/Map_City_loc_city.md"
    assert world_path != map_path


def test_missing_projection_pages_retire_and_leave_other_owner_live() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[Project.__table__, KnowledgeProjectionPageRow.__table__],
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with sessions.begin() as session:
            session.add(
                Project(
                    id="project-1",
                    title="Projection retirement",
                    premise="Retire stale disposable pages.",
                    genre="thriller",
                )
            )
            session.add_all(
                [
                    KnowledgeProjectionPageRow(
                        id="keep",
                        project_id="project-1",
                        page_key="character:keep",
                        page_type="character",
                        title="Keep",
                        vault_path="same.md",
                        projection_kind="obsidian",
                        status="canon_live",
                        logical_identity_key="character:keep",
                    ),
                    KnowledgeProjectionPageRow(
                        id="stale",
                        project_id="project-1",
                        page_key="character:stale",
                        page_type="character",
                        title="Stale",
                        vault_path="same.md",
                        projection_kind="obsidian",
                        status="canon_live",
                        logical_identity_key="character:stale",
                    ),
                    KnowledgeProjectionPageRow(
                        id="other-owner",
                        project_id="project-1",
                        page_key="world:other",
                        page_type="overview",
                        title="Other",
                        vault_path="other.md",
                        projection_kind="world_studio",
                        status="canon_live",
                        logical_identity_key="overview:other",
                    ),
                ]
            )

        with sessions.begin() as session:
            repo = KnowledgePageRepository(session)
            assert repo.retire_missing_projection_pages(
                "project-1",
                projection_kind="obsidian",
                active_page_ids={"keep"},
            ) == 1
            assert repo.resolve_page_key("project-1", "character:stale") is None
            assert [
                row.id
                for row in repo.list_canonical_rows(
                    "project-1",
                    include_superseded=True,
                )
            ] == ["keep", "other-owner"]

        with sessions() as session:
            assert session.get(KnowledgeProjectionPageRow, "keep").status == "canon_live"
            stale = session.get(KnowledgeProjectionPageRow, "stale")
            assert stale.status == "retired"
            assert stale.canon_status == "retired_projection"
            assert (
                session.get(KnowledgeProjectionPageRow, "other-owner").status
                == "canon_live"
            )

        handlers = build_handlers(get_session=sessions)
        assert {
            page.id for page in handlers["list_projection_pages"]("project-1")
        } == {"keep", "other-owner"}
        with pytest.raises(HTTPException) as exc_info:
            handlers["get_projection_page"]("project-1", "character:stale")
        assert exc_info.value.status_code == 404
    finally:
        engine.dispose()
