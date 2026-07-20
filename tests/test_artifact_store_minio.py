from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from minio.error import S3Error

from forwin.storage.artifacts import (
    ArtifactStore,
    LocalObjectStore,
    MinioObjectStore,
    ObjectStore,
)


MINIO_CONFIG: dict[str, Any] = {
    "backend": "minio",
    "minio_endpoint": "minio:9000",
    "minio_access_key": "access-key",
    "minio_secret_key": "secret-key",
    "minio_bucket": "forwin-artifacts",
    "minio_prefix": "artifacts",
    "minio_secure": False,
}


def _artifact_store(root_dir: str, **overrides: Any) -> ArtifactStore:
    return ArtifactStore(root_dir=root_dir, **(MINIO_CONFIG | overrides))


def _minio_store() -> MinioObjectStore:
    return MinioObjectStore(
        endpoint="minio:9000",
        access_key="access-key",
        secret_key="secret-key",
        bucket="forwin-artifacts",
    )


def _s3_error(code: str) -> S3Error:
    return S3Error(
        response=None,
        code=code,
        message=code,
        resource="/forwin-artifacts",
        request_id="request-id",
        host_id="host-id",
        bucket_name="forwin-artifacts",
    )


class _InjectedStore(ObjectStore):
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def write_text(self, relative_path: str, content: str, *, content_type: str) -> str:
        self.values[relative_path] = content
        return f"injected://{relative_path}"

    def read_text(self, uri: str) -> str:
        return self.values[uri.removeprefix("injected://")]


@pytest.mark.parametrize("backend", ["", "LOCAL", " local ", "s3", "minio "])
def test_artifact_store_rejects_every_non_exact_backend(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class InertMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def bucket_exists(self, _bucket: str) -> bool:
            return True

    monkeypatch.setattr("minio.Minio", InertMinio)

    with pytest.raises(ValueError, match="backend"):
        ArtifactStore(root_dir=str(tmp_path), backend=backend)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("minio_endpoint", "", "endpoint"),
        ("minio_endpoint", "   ", "endpoint"),
        ("minio_access_key", "", "access_key"),
        ("minio_access_key", "   ", "access_key"),
        ("minio_secret_key", "", "secret_key"),
        ("minio_secret_key", "   ", "secret_key"),
        ("minio_bucket", "", "bucket"),
        ("minio_bucket", "   ", "bucket"),
    ],
)
def test_artifact_store_rejects_incomplete_minio_configuration(
    field: str,
    value: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class InertMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def bucket_exists(self, _bucket: str) -> bool:
            return True

    monkeypatch.setattr("minio.Minio", InertMinio)

    with pytest.raises(ValueError, match=message):
        _artifact_store(str(tmp_path), **{field: value})


def test_artifact_store_accepts_an_explicit_injected_store(tmp_path: Path) -> None:
    injected = _InjectedStore()

    store = ArtifactStore(
        root_dir=str(tmp_path),
        backend="not-a-configured-backend",
        object_store=injected,
    )

    assert store.object_store is injected


def test_artifact_store_preserves_explicit_local_behavior(tmp_path: Path) -> None:
    store = ArtifactStore(root_dir=str(tmp_path), backend="local")

    uri = store.object_store.write_text(
        "nested/artifact.txt",
        "local artifact",
        content_type="text/plain",
    )

    assert isinstance(store.object_store, LocalObjectStore)
    assert Path(uri) == tmp_path / "nested/artifact.txt"
    assert store.read_text(uri) == "local artifact"


def test_complete_minio_configuration_performs_zero_network_at_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    constructor_calls: list[bool] = []

    class ConstructorTrap:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            constructor_calls.append(True)
            raise AssertionError("Minio client construction is first-I/O work")

    monkeypatch.setattr("minio.Minio", ConstructorTrap)

    store = _artifact_store(str(tmp_path))

    assert isinstance(store.object_store, MinioObjectStore)
    assert constructor_calls == []


def test_close_before_first_io_is_inert_and_final(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    constructor_calls: list[bool] = []

    class ConstructorTrap:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            constructor_calls.append(True)

    monkeypatch.setattr("minio.Minio", ConstructorTrap)
    store = _artifact_store(str(tmp_path))

    store.close()
    store.close()

    assert constructor_calls == []
    with pytest.raises(RuntimeError, match="closed"):
        store.object_store.write_text(
            "after-close.txt",
            "value",
            content_type="text/plain",
        )


def test_materialized_minio_pool_is_closed_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clear_calls: list[str] = []

    class Pool:
        def clear(self) -> None:
            clear_calls.append("clear")

    class ClosableMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._http = Pool()

        def bucket_exists(self, _bucket: str) -> bool:
            return True

        def put_object(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr("minio.Minio", ClosableMinio)
    store = _artifact_store(str(tmp_path))
    store.save_frozen_candidate(
        project_id="project-1",
        chapter_number=1,
        payload={"value": 1},
    )

    store.close()
    store.close()

    assert clear_calls == ["clear"]


def test_artifact_store_does_not_close_injected_store(tmp_path: Path) -> None:
    close_calls: list[str] = []

    class InjectedStore(_InjectedStore):
        def close(self) -> None:
            close_calls.append("close")

    store = ArtifactStore(
        root_dir=str(tmp_path),
        object_store=InjectedStore(),
    )

    store.close()

    assert close_calls == []


def test_minio_first_io_initialization_is_thread_safe_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_lock = threading.Lock()
    barrier = threading.Barrier(8)
    clients: list[object] = []
    bucket_checks: list[str] = []
    writes: list[str] = []

    class SlowMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            with state_lock:
                clients.append(self)
            time.sleep(0.02)

        def bucket_exists(self, bucket: str) -> bool:
            with state_lock:
                bucket_checks.append(bucket)
            time.sleep(0.02)
            return True

        def put_object(
            self,
            _bucket: str,
            key: str,
            data: Any,
            _length: int,
            *,
            content_type: str,
        ) -> None:
            assert content_type == "text/plain"
            assert data.read().decode("utf-8").startswith("value-")
            with state_lock:
                writes.append(key)

    monkeypatch.setattr("minio.Minio", SlowMinio)
    store = _minio_store()

    assert clients == []
    assert bucket_checks == []

    def write(index: int) -> str:
        barrier.wait(timeout=2)
        return store.write_text(
            f"thread-{index}.txt",
            f"value-{index}",
            content_type="text/plain",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        uris = list(executor.map(write, range(8)))

    assert len(clients) == 1
    assert bucket_checks == ["forwin-artifacts"]
    assert len(writes) == 8
    assert len(set(uris)) == 8


def test_minio_bucket_creation_race_is_resolved_on_first_io_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[RacingMinio] = []

    class RacingMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.bucket_checks = 0
            self.bucket_creations = 0
            self.writes = 0
            clients.append(self)

        def bucket_exists(self, _bucket: str) -> bool:
            self.bucket_checks += 1
            return self.bucket_checks > 1

        def make_bucket(self, _bucket: str) -> None:
            self.bucket_creations += 1
            raise _s3_error("BucketAlreadyOwnedByYou")

        def put_object(self, *_args: Any, **_kwargs: Any) -> None:
            self.writes += 1

    monkeypatch.setattr("minio.Minio", RacingMinio)
    store = _minio_store()

    assert clients == []
    first_uri = store.write_text("first.txt", "one", content_type="text/plain")
    second_uri = store.write_text("second.txt", "two", content_type="text/plain")

    assert first_uri == "minio://forwin-artifacts/artifacts/first.txt"
    assert second_uri == "minio://forwin-artifacts/artifacts/second.txt"
    assert len(clients) == 1
    assert clients[0].bucket_checks == 2
    assert clients[0].bucket_creations == 1
    assert clients[0].writes == 2


def test_failed_client_initialization_propagates_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failure = ConnectionError("minio unavailable")
    constructor_calls = 0

    class ReadyMinio:
        def bucket_exists(self, _bucket: str) -> bool:
            return True

        def put_object(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    def build_client(*_args: Any, **_kwargs: Any) -> ReadyMinio:
        nonlocal constructor_calls
        constructor_calls += 1
        if constructor_calls == 1:
            raise failure
        return ReadyMinio()

    monkeypatch.setattr("minio.Minio", build_client)
    store = _artifact_store(str(tmp_path))

    with pytest.raises(ConnectionError) as exc_info:
        store.save_frozen_candidate(
            project_id="project-1",
            chapter_number=1,
            payload={"value": 1},
        )
    assert exc_info.value is failure

    uri = store.save_frozen_candidate(
        project_id="project-1",
        chapter_number=1,
        payload={"value": 2},
    )
    store.save_frozen_candidate(
        project_id="project-1",
        chapter_number=1,
        payload={"value": 3},
    )

    assert uri.startswith("minio://forwin-artifacts/artifacts/")
    assert constructor_calls == 2


def test_minio_authentication_error_propagates_unchanged_on_first_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = _s3_error("AccessDenied")
    clear_calls: list[str] = []

    class Pool:
        def clear(self) -> None:
            clear_calls.append("clear")

    class AuthenticationFailureMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._http = Pool()

        def bucket_exists(self, _bucket: str) -> bool:
            raise failure

    monkeypatch.setattr("minio.Minio", AuthenticationFailureMinio)
    store = _minio_store()

    with pytest.raises(S3Error) as exc_info:
        store.read_text("minio://forwin-artifacts/artifacts/value.txt")

    assert exc_info.value is failure
    assert clear_calls == ["clear"]


def test_minio_bucket_creation_error_propagates_unchanged_on_first_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = _s3_error("InvalidBucketName")

    class BucketFailureMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def bucket_exists(self, _bucket: str) -> bool:
            return False

        def make_bucket(self, _bucket: str) -> None:
            raise failure

    monkeypatch.setattr("minio.Minio", BucketFailureMinio)
    store = _minio_store()

    with pytest.raises(S3Error) as exc_info:
        store.write_text("value.txt", "value", content_type="text/plain")

    assert exc_info.value is failure


@pytest.mark.parametrize("operation", ["read", "write"])
def test_minio_io_error_propagates_unchanged_without_fallback(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failure = OSError(f"{operation} failed")

    class IOFailureMinio:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def bucket_exists(self, _bucket: str) -> bool:
            return True

        def get_object(self, *_args: Any, **_kwargs: Any) -> Any:
            raise failure

        def put_object(self, *_args: Any, **_kwargs: Any) -> None:
            raise failure

    monkeypatch.setattr("minio.Minio", IOFailureMinio)
    store = _artifact_store(str(tmp_path))

    with pytest.raises(OSError) as exc_info:
        if operation == "read":
            store.read_text("minio://forwin-artifacts/artifacts/value.txt")
        else:
            store.save_frozen_candidate(
                project_id="project-1",
                chapter_number=1,
                payload={"value": 1},
            )

    assert exc_info.value is failure
    assert isinstance(store.object_store, MinioObjectStore)
