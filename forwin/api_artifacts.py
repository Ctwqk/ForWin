from __future__ import annotations

from forwin.config import Config
from forwin.storage import ArtifactStore


def build_artifact_store(config: Config) -> ArtifactStore:
    return ArtifactStore(
        config.artifact_root,
        backend=config.artifact_backend,
        minio_endpoint=config.minio_endpoint,
        minio_access_key=config.minio_access_key,
        minio_secret_key=config.minio_secret_key,
        minio_bucket=config.minio_bucket,
        minio_prefix=config.minio_prefix,
        minio_secure=config.minio_secure,
    )
