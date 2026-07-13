from __future__ import annotations

import pytest
from minio.error import S3Error

from forwin.storage.artifacts import ArtifactStore, MinioObjectStore


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


def test_artifact_store_keeps_minio_when_another_role_wins_bucket_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class RacingMinio:
        def __init__(self, *_args, **_kwargs) -> None:
            self.bucket_checks = 0

        def bucket_exists(self, _bucket: str) -> bool:
            self.bucket_checks += 1
            return self.bucket_checks > 1

        def make_bucket(self, _bucket: str) -> None:
            raise _s3_error("BucketAlreadyOwnedByYou")

    monkeypatch.setattr("minio.Minio", RacingMinio)

    store = ArtifactStore(
        root_dir=str(tmp_path),
        backend="minio",
        minio_endpoint="minio:9000",
        minio_access_key="access-key",
        minio_secret_key="secret-key",
    )

    assert isinstance(store.object_store, MinioObjectStore)


def test_minio_bucket_creation_error_is_not_hidden_when_bucket_is_still_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingMinio:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def bucket_exists(self, _bucket: str) -> bool:
            return False

        def make_bucket(self, _bucket: str) -> None:
            raise _s3_error("AccessDenied")

    monkeypatch.setattr("minio.Minio", FailingMinio)

    with pytest.raises(S3Error, match="AccessDenied"):
        MinioObjectStore(
            endpoint="minio:9000",
            access_key="access-key",
            secret_key="secret-key",
            bucket="forwin-artifacts",
        )
