from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from forwin.models import (
    GenerationTask,
    Project,
    PublisherBrowserSession,
    PublisherBrowserSessionEntry,
    PublisherConnectionState,
    PublisherExtensionClient,
    PublisherExtensionPlatformState,
    PublisherUploadJob,
)
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from scripts import purge_all_projects as purge_module
from scripts.purge_all_projects import (
    MinioTarget,
    PurgeBlocked,
    PurgeTargets,
    purge_all_projects,
)
from tests.postgres import postgres_test_url


QDRANT_NAMES = ("chapter_memories", "llm_kb_vectors")
GLOBAL_PUBLISHER_TABLES = (
    PublisherExtensionClient,
    PublisherConnectionState,
    PublisherExtensionPlatformState,
    PublisherBrowserSession,
    PublisherBrowserSessionEntry,
)


class FakeQdrant:
    def __init__(self, names: list[str], *, fail_delete: bool = False) -> None:
        self.names = list(names)
        self.fail_delete = fail_delete
        self.deleted: list[str] = []

    def get_collections(self) -> SimpleNamespace:
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in self.names]
        )

    def delete_collection(self, collection_name: str) -> bool:
        if self.fail_delete:
            raise RuntimeError("qdrant unavailable")
        self.deleted.append(collection_name)
        self.names.remove(collection_name)
        return True

    def close(self) -> None:
        return None


class FakeMinio:
    """Lists every configured object regardless of prefix to prove the script
    re-checks the prefix itself before deleting."""

    def __init__(self, names: list[str]) -> None:
        self.names = list(names)
        self.list_calls: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []

    def list_objects(self, bucket_name, prefix=None, recursive=False):  # noqa: ANN001
        self.list_calls.append((bucket_name, str(prefix)))
        return [SimpleNamespace(object_name=name) for name in self.names]

    def remove_object(self, bucket_name, object_name) -> None:  # noqa: ANN001
        self.removed.append((bucket_name, object_name))


class FakeCoverService:
    def __init__(self, removed: list[str] | None = None) -> None:
        self.removed = list(removed or [])
        self.calls = 0

    def cleanup_orphaned_files(self) -> list[str]:
        self.calls += 1
        return list(self.removed)


@pytest.fixture()
def engine():
    engine = get_engine(postgres_test_url("purge_all_projects"))
    init_db(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def session_factory(engine):
    return get_session_factory(engine)


@pytest.fixture()
def targets(tmp_path: Path) -> PurgeTargets:
    return PurgeTargets(
        artifact_root=tmp_path / "artifacts",
        minio=None,
        qdrant_url="http://qdrant.invalid:6333",
        qdrant_collection_names=QDRANT_NAMES,
        local_project_roots=(tmp_path / "llm_kb", tmp_path / "vaults"),
        publisher_cover_dir=tmp_path / "covers",
    )


def _add_project(session_factory, title: str) -> str:
    project_id = new_id()
    with session_factory.begin() as session:
        session.add(
            Project(
                id=project_id,
                title=title,
                premise="测试 premise",
                genre="玄幻",
                setting_summary="",
            )
        )
    return project_id


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _seed_local_files(targets: PurgeTargets, project_id: str) -> dict[str, Path]:
    llm_kb_root, vault_root = targets.local_project_roots
    return {
        "artifact": _write(targets.artifact_root / "projects" / project_id / "x.txt"),
        "audit": _write(
            targets.artifact_root / "audit_bundles" / "projects" / project_id / "b.json"
        ),
        "llm_kb": _write(llm_kb_root / project_id / "a.md"),
        "vault": _write(vault_root / project_id / "n.md"),
    }


def _count(session_factory, model) -> int:
    with session_factory() as session:
        return session.query(model).count()


def test_purge_refuses_when_generation_task_active(
    session_factory, targets: PurgeTargets
) -> None:
    project_id = _add_project(session_factory, "进行中")
    with session_factory.begin() as session:
        session.add(
            GenerationTask(
                id="task-active-1",
                project_id=project_id,
                task_kind="generation",
                status="running",
                current_stage="writing_chapter",
                message="",
            )
        )

    with pytest.raises(PurgeBlocked) as excinfo:
        purge_all_projects(
            session_factory=session_factory,
            targets=targets,
            dry_run=False,
            qdrant_client=FakeQdrant(list(QDRANT_NAMES)),
            cover_service=FakeCoverService(),
        )

    assert "task-active-1" in str(excinfo.value)
    assert _count(session_factory, Project) == 1
    assert _count(session_factory, GenerationTask) == 1


def test_purge_refuses_when_upload_job_active(
    session_factory, targets: PurgeTargets
) -> None:
    project_id = _add_project(session_factory, "上传中")
    with session_factory.begin() as session:
        session.add(
            PublisherUploadJob(
                id="upload-active-1",
                project_id=project_id,
                platform_id="fanqie",
                status="running",
                book_name="测试书",
                chapter_title="第一章",
                body_text="正文",
                publish=False,
            )
        )

    with pytest.raises(PurgeBlocked) as excinfo:
        purge_all_projects(
            session_factory=session_factory,
            targets=targets,
            dry_run=False,
            qdrant_client=FakeQdrant(list(QDRANT_NAMES)),
            cover_service=FakeCoverService(),
        )

    assert "upload-active-1" in str(excinfo.value)
    assert _count(session_factory, Project) == 1
    assert _count(session_factory, PublisherUploadJob) == 1


def test_purge_does_not_block_on_terminal_tasks_and_jobs(
    session_factory, targets: PurgeTargets
) -> None:
    project_id = _add_project(session_factory, "已结束")
    with session_factory.begin() as session:
        session.add(
            GenerationTask(
                id="task-done-1",
                project_id=project_id,
                task_kind="generation",
                status="completed",
                current_stage="completed",
                message="",
            )
        )
        session.add(
            PublisherUploadJob(
                id="upload-done-1",
                project_id=project_id,
                platform_id="fanqie",
                status="succeeded",
                book_name="测试书",
                chapter_title="第一章",
                body_text="正文",
                publish=False,
            )
        )

    report = purge_all_projects(
        session_factory=session_factory,
        targets=targets,
        dry_run=False,
        qdrant_client=FakeQdrant(list(QDRANT_NAMES)),
        cover_service=FakeCoverService(),
    )

    assert report.deleted_projects == 1
    assert _count(session_factory, Project) == 0
    assert _count(session_factory, GenerationTask) == 0
    assert _count(session_factory, PublisherUploadJob) == 0


def test_purge_dry_run_changes_nothing(
    session_factory, targets: PurgeTargets
) -> None:
    project_ids = sorted(
        [_add_project(session_factory, "书一"), _add_project(session_factory, "书二")]
    )
    seeded = {pid: _seed_local_files(targets, pid) for pid in project_ids}
    qdrant = FakeQdrant(["chapter_memories", "chapter_memories_384d", "skills"])
    cover_service = FakeCoverService()

    report = purge_all_projects(
        session_factory=session_factory,
        targets=targets,
        dry_run=True,
        qdrant_client=qdrant,
        cover_service=cover_service,
    )

    assert report.dry_run is True
    assert report.project_ids == tuple(project_ids)
    assert report.deleted_projects == 0
    for pid in project_ids:
        assert str(targets.artifact_root / "projects" / pid) in report.removed_paths
        assert str(targets.local_project_roots[0] / pid) in report.removed_paths
        assert str(targets.local_project_roots[1] / pid) in report.removed_paths
    assert not any("audit_bundles" in path for path in report.removed_paths)
    assert report.dropped_qdrant_collections == (
        "chapter_memories",
        "chapter_memories_384d",
    )
    assert report.errors == ()
    assert _count(session_factory, Project) == 2
    for files in seeded.values():
        assert all(path.exists() for path in files.values())
    assert qdrant.deleted == []
    assert cover_service.calls == 0


def test_purge_deletes_projects_and_local_stores_but_keeps_global_publisher_tables(
    session_factory, targets: PurgeTargets
) -> None:
    project_ids = [_add_project(session_factory, "书一"), _add_project(session_factory, "书二")]
    seeded = {pid: _seed_local_files(targets, pid) for pid in project_ids}
    with session_factory.begin() as session:
        session.add(PublisherExtensionClient(client_id="client-1"))
        session.flush()
        session.add(
            PublisherConnectionState(
                platform_id="fanqie", extension_client_id="client-1"
            )
        )
        session.add(
            PublisherExtensionPlatformState(client_id="client-1", platform_id="fanqie")
        )
        session.add(
            PublisherBrowserSession(
                platform_id="fanqie", extension_client_id="client-1"
            )
        )
        session.add(
            PublisherBrowserSessionEntry(client_id="client-1", platform_id="fanqie")
        )
    cover_service = FakeCoverService(removed=[str(targets.publisher_cover_dir / "c.png")])

    report = purge_all_projects(
        session_factory=session_factory,
        targets=targets,
        dry_run=False,
        qdrant_client=FakeQdrant(list(QDRANT_NAMES)),
        cover_service=cover_service,
    )

    assert report.dry_run is False
    assert report.deleted_projects == 2
    assert report.project_ids == tuple(sorted(project_ids))
    assert report.errors == ()
    assert _count(session_factory, Project) == 0
    projects_dir = targets.artifact_root / "projects"
    assert projects_dir.is_dir()
    assert list(projects_dir.iterdir()) == []
    for files in seeded.values():
        assert files["audit"].exists()
        assert not files["artifact"].exists()
        assert not files["llm_kb"].exists()
        assert not files["vault"].exists()
    for pid in project_ids:
        assert not (targets.local_project_roots[0] / pid).exists()
        assert not (targets.local_project_roots[1] / pid).exists()
    assert targets.local_project_roots[0].is_dir()
    assert targets.local_project_roots[1].is_dir()
    for model in GLOBAL_PUBLISHER_TABLES:
        assert _count(session_factory, model) == 1, model.__tablename__
    assert cover_service.calls == 1
    assert str(targets.publisher_cover_dir / "c.png") in report.removed_paths
    assert str(targets.artifact_root / "projects" / project_ids[0]) in report.removed_paths


def test_purge_drops_dimension_suffixed_qdrant_collections(
    session_factory, targets: PurgeTargets
) -> None:
    qdrant = FakeQdrant(
        ["chapter_memories", "chapter_memories_384d", "llm_kb_vectors", "skills"]
    )

    report = purge_all_projects(
        session_factory=session_factory,
        targets=targets,
        dry_run=False,
        qdrant_client=qdrant,
        cover_service=FakeCoverService(),
    )

    assert sorted(qdrant.deleted) == [
        "chapter_memories",
        "chapter_memories_384d",
        "llm_kb_vectors",
    ]
    assert qdrant.names == ["skills"]
    assert report.dropped_qdrant_collections == tuple(sorted(qdrant.deleted))
    assert report.errors == ()


def test_purge_does_not_drop_similarly_named_qdrant_collections(
    session_factory, targets: PurgeTargets
) -> None:
    qdrant = FakeQdrant(
        ["chapter_memories_backup", "chapter_memories_d", "xchapter_memories_384d"]
    )

    report = purge_all_projects(
        session_factory=session_factory,
        targets=targets,
        dry_run=False,
        qdrant_client=qdrant,
        cover_service=FakeCoverService(),
    )

    assert qdrant.deleted == []
    assert report.dropped_qdrant_collections == ()


def test_purge_reports_external_store_errors(
    session_factory, targets: PurgeTargets
) -> None:
    project_id = _add_project(session_factory, "外部存储失败")
    files = _seed_local_files(targets, project_id)
    cover_service = FakeCoverService()

    report = purge_all_projects(
        session_factory=session_factory,
        targets=targets,
        dry_run=False,
        qdrant_client=FakeQdrant(list(QDRANT_NAMES), fail_delete=True),
        cover_service=cover_service,
    )

    assert report.deleted_projects == 1
    assert _count(session_factory, Project) == 0
    assert not files["artifact"].exists()
    assert cover_service.calls == 1
    assert report.errors
    assert any("qdrant" in error.lower() for error in report.errors)
    assert report.dropped_qdrant_collections == ()


def test_purge_continues_to_minio_after_cover_cleanup_error(
    session_factory, targets: PurgeTargets
) -> None:
    class BrokenCoverService:
        def cleanup_orphaned_files(self) -> list[str]:
            raise OSError("cover dir unreadable")

    minio = FakeMinio(["artifacts/projects/a/1.txt"])
    targets = PurgeTargets(
        artifact_root=targets.artifact_root,
        minio=MinioTarget(
            endpoint="minio.invalid:9000",
            access_key="ak",
            secret_key="sk",
            bucket="forwin-artifacts",
            prefix="artifacts",
            secure=False,
        ),
        qdrant_url=targets.qdrant_url,
        qdrant_collection_names=targets.qdrant_collection_names,
        local_project_roots=targets.local_project_roots,
        publisher_cover_dir=targets.publisher_cover_dir,
    )

    report = purge_all_projects(
        session_factory=session_factory,
        targets=targets,
        dry_run=False,
        qdrant_client=FakeQdrant(list(QDRANT_NAMES)),
        minio_client=minio,
        cover_service=BrokenCoverService(),
    )

    assert any("cover" in error.lower() for error in report.errors)
    assert report.removed_minio_objects == 1
    assert minio.removed == [("forwin-artifacts", "artifacts/projects/a/1.txt")]


def test_purge_removes_minio_project_objects(
    session_factory, targets: PurgeTargets
) -> None:
    minio = FakeMinio(
        [
            "artifacts/projects/a/1.txt",
            "artifacts/projects/b/2.txt",
            "artifacts/other/3.txt",
        ]
    )
    targets = PurgeTargets(
        artifact_root=targets.artifact_root,
        minio=MinioTarget(
            endpoint="minio.invalid:9000",
            access_key="ak",
            secret_key="sk",
            bucket="forwin-artifacts",
            prefix="artifacts",
            secure=False,
        ),
        qdrant_url=targets.qdrant_url,
        qdrant_collection_names=targets.qdrant_collection_names,
        local_project_roots=targets.local_project_roots,
        publisher_cover_dir=targets.publisher_cover_dir,
    )

    report = purge_all_projects(
        session_factory=session_factory,
        targets=targets,
        dry_run=False,
        qdrant_client=FakeQdrant(list(QDRANT_NAMES)),
        minio_client=minio,
        cover_service=FakeCoverService(),
    )

    assert minio.list_calls == [("forwin-artifacts", "artifacts/projects/")]
    assert minio.removed == [
        ("forwin-artifacts", "artifacts/projects/a/1.txt"),
        ("forwin-artifacts", "artifacts/projects/b/2.txt"),
    ]
    assert report.removed_minio_objects == 2
    assert report.errors == ()


def test_purge_dry_run_does_not_remove_minio_objects(
    session_factory, targets: PurgeTargets
) -> None:
    minio = FakeMinio(["artifacts/projects/a/1.txt", "artifacts/other/3.txt"])
    targets = PurgeTargets(
        artifact_root=targets.artifact_root,
        minio=MinioTarget(
            endpoint="minio.invalid:9000",
            access_key="ak",
            secret_key="sk",
            bucket="forwin-artifacts",
            prefix="artifacts",
            secure=False,
        ),
        qdrant_url=targets.qdrant_url,
        qdrant_collection_names=targets.qdrant_collection_names,
        local_project_roots=targets.local_project_roots,
        publisher_cover_dir=targets.publisher_cover_dir,
    )

    report = purge_all_projects(
        session_factory=session_factory,
        targets=targets,
        dry_run=True,
        qdrant_client=FakeQdrant(list(QDRANT_NAMES)),
        minio_client=minio,
        cover_service=FakeCoverService(),
    )

    assert minio.removed == []
    assert report.removed_minio_objects == 1


def test_targets_from_config_maps_infrastructure_fields(tmp_path: Path, monkeypatch) -> None:
    from forwin.config import InfrastructureConfig

    monkeypatch.setenv("FORWIN_PUBLISHER_COVER_DIR", str(tmp_path / "covers"))
    local = purge_module.targets_from_config(
        InfrastructureConfig(
            artifact_root=str(tmp_path / "artifacts"),
            artifact_backend="local",
            qdrant_url="http://qdrant.invalid:6333",
            qdrant_collection="chapter_memories",
            llm_kb_qdrant_collection="llm_kb_vectors",
        )
    )
    assert local.artifact_root == tmp_path / "artifacts"
    assert local.minio is None
    assert local.qdrant_url == "http://qdrant.invalid:6333"
    assert local.qdrant_collection_names == ("chapter_memories", "llm_kb_vectors")
    assert local.local_project_roots == (
        purge_module.DEFAULT_LLM_KB_ROOT,
        purge_module.DEFAULT_VAULT_ROOT,
    )
    assert local.publisher_cover_dir == tmp_path / "covers"

    remote = purge_module.targets_from_config(
        InfrastructureConfig(
            artifact_root=str(tmp_path / "artifacts"),
            artifact_backend="minio",
            minio_endpoint="minio.invalid:9000",
            minio_access_key="ak",
            minio_secret_key="sk",
            minio_bucket="bucket",
            minio_prefix="/pfx/",
            minio_secure=True,
        )
    )
    assert remote.minio == MinioTarget(
        endpoint="minio.invalid:9000",
        access_key="ak",
        secret_key="sk",
        bucket="bucket",
        prefix="pfx",
        secure=True,
    )


@pytest.fixture()
def main_env(monkeypatch, tmp_path: Path, session_factory, engine):
    qdrant = FakeQdrant(list(QDRANT_NAMES))
    monkeypatch.setenv("FORWIN_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("FORWIN_ARTIFACT_BACKEND", "local")
    monkeypatch.setenv("FORWIN_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("FORWIN_PUBLISHER_COVER_DIR", str(tmp_path / "covers"))
    monkeypatch.setattr(purge_module, "DEFAULT_LLM_KB_ROOT", tmp_path / "llm_kb")
    monkeypatch.setattr(purge_module, "DEFAULT_VAULT_ROOT", tmp_path / "vaults")
    monkeypatch.setattr(purge_module, "require_v5_schema", lambda _engine: None)
    monkeypatch.setattr(purge_module, "_create_qdrant_client", lambda _url: qdrant)
    return SimpleNamespace(qdrant=qdrant, tmp_path=tmp_path)


def test_main_defaults_to_dry_run(main_env, session_factory, capsys) -> None:
    project_id = _add_project(session_factory, "默认干跑")
    kept = _write(main_env.tmp_path / "artifacts" / "projects" / project_id / "x.txt")

    assert purge_module.main([]) == 0

    assert _count(session_factory, Project) == 1
    assert kept.exists()
    assert main_env.qdrant.deleted == []
    output = capsys.readouterr().out
    assert "dry-run" in output.lower()
    assert project_id in output


def test_main_deletes_with_explicit_flag(main_env, session_factory) -> None:
    project_id = _add_project(session_factory, "显式删除")
    removed = _write(main_env.tmp_path / "artifacts" / "projects" / project_id / "x.txt")

    assert purge_module.main(["--yes-delete-all-projects"]) == 0

    assert _count(session_factory, Project) == 0
    assert not removed.exists()
    assert sorted(main_env.qdrant.deleted) == sorted(QDRANT_NAMES)


def test_main_returns_2_when_external_store_errors(main_env, session_factory) -> None:
    main_env.qdrant.fail_delete = True
    _add_project(session_factory, "外部存储失败")

    assert purge_module.main(["--yes-delete-all-projects"]) == 2

    assert _count(session_factory, Project) == 0


def test_main_returns_3_when_blocked(main_env, session_factory, capsys) -> None:
    project_id = _add_project(session_factory, "被阻塞")
    with session_factory.begin() as session:
        session.add(
            GenerationTask(
                id="task-blocking-1",
                project_id=project_id,
                task_kind="generation",
                status="queued",
                current_stage="queued",
                message="",
            )
        )

    assert purge_module.main(["--yes-delete-all-projects"]) == 3

    assert _count(session_factory, Project) == 1
    assert "task-blocking-1" in capsys.readouterr().err
