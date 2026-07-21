from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from forwin.observability.payloads import artifact_manifest_item
from forwin.observability.redaction import redact_payload
from forwin.protocol.scene import SceneOutput
from forwin.protocol.writer import WriterOutput

logger = logging.getLogger(__name__)


def _close_minio_client(client: Any | None) -> None:
    if client is None:
        return
    try:
        close = getattr(client, "close", None)
        if callable(close):
            close()
            return
        clear = getattr(getattr(client, "_http", None), "clear", None)
        if callable(clear):
            clear()
    except Exception:  # noqa: BLE001
        logger.debug("MinIO client close failed.", exc_info=True)


class ObjectStore:
    def write_text(self, relative_path: str, content: str, *, content_type: str) -> str:
        raise NotImplementedError

    def read_text(self, uri: str) -> str:
        raise NotImplementedError

    def close(self) -> None:
        return None


class LocalObjectStore(ObjectStore):
    def __init__(self, root_dir: str) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def write_text(self, relative_path: str, content: str, *, content_type: str) -> str:
        path = self.root_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(path)

    def read_text(self, uri: str) -> str:
        return Path(uri).read_text(encoding="utf-8")


class MinioObjectStore(ObjectStore):
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        prefix: str = "artifacts",
        secure: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.secure = secure
        self._client: Any | None = None
        self._client_lock = threading.Lock()
        self._closed = False

    def _ensure_client(self) -> Any:
        if self._closed:
            raise RuntimeError("MinIO object store is closed")
        client = self._client
        if client is not None:
            return client

        with self._client_lock:
            if self._closed:
                raise RuntimeError("MinIO object store is closed")
            client = self._client
            if client is not None:
                return client

            from minio import Minio
            from minio.error import S3Error

            client = Minio(
                self.endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
            )
            try:
                if not client.bucket_exists(self.bucket):
                    try:
                        client.make_bucket(self.bucket)
                    except S3Error as exc:
                        race_codes = {
                            "BucketAlreadyExists",
                            "BucketAlreadyOwnedByYou",
                        }
                        if (
                            exc.code not in race_codes
                            or not client.bucket_exists(self.bucket)
                        ):
                            raise
                        logger.info(
                            "MinIO bucket %s was created by another process.",
                            self.bucket,
                        )
            except Exception:
                _close_minio_client(client)
                raise
            self._client = client
            return client

    def close(self) -> None:
        with self._client_lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
        _close_minio_client(client)

    def _key(self, relative_path: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{relative_path}".strip("/")
        return relative_path.strip("/")

    def write_text(self, relative_path: str, content: str, *, content_type: str) -> str:
        payload = content.encode("utf-8")
        key = self._key(relative_path)
        client = self._ensure_client()
        client.put_object(
            self.bucket,
            key,
            io.BytesIO(payload),
            len(payload),
            content_type=content_type,
        )
        return f"minio://{self.bucket}/{key}"

    def read_text(self, uri: str) -> str:
        parsed = urlparse(uri)
        client = self._ensure_client()
        response = client.get_object(parsed.netloc, parsed.path.lstrip("/"))
        try:
            return response.read().decode("utf-8")
        finally:
            response.close()
            response.release_conn()


class ArtifactStore:
    """Stores writer artifacts under a project-scoped namespace."""

    def __init__(
        self,
        root_dir: str = "data/artifacts",
        *,
        backend: str = "local",
        minio_endpoint: str = "",
        minio_access_key: str = "",
        minio_secret_key: str = "",
        minio_bucket: str = "forwin-artifacts",
        minio_prefix: str = "artifacts",
        minio_secure: bool = False,
        object_store: ObjectStore | None = None,
        owns_object_store: bool = False,
    ) -> None:
        self.root_dir = root_dir
        self._closed = False
        self._close_lock = threading.Lock()
        self._owns_object_store = object_store is None or owns_object_store
        if object_store is not None:
            self.object_store = object_store
            return

        if backend == "local":
            self.object_store = LocalObjectStore(root_dir)
            return
        if backend != "minio":
            raise ValueError(
                f"unsupported artifact backend {backend!r}; expected 'local' or 'minio'"
            )

        required_config = {
            "endpoint": minio_endpoint,
            "access_key": minio_access_key,
            "secret_key": minio_secret_key,
            "bucket": minio_bucket,
        }
        missing = [
            name for name, value in required_config.items() if not str(value or "").strip()
        ]
        if missing:
            raise ValueError(
                "minio artifact backend requires non-empty " + ", ".join(missing)
            )
        self.object_store = MinioObjectStore(
            endpoint=minio_endpoint,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            bucket=minio_bucket,
            prefix=minio_prefix,
            secure=minio_secure,
        )

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            object_store = self.object_store if self._owns_object_store else None
        close = getattr(object_store, "close", None)
        if callable(close):
            close()

    def save_writer_output(
        self,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        *,
        namespace_root: str | None = None,
    ) -> dict[str, object]:
        version = datetime.now(timezone.utc).strftime("v%Y%m%dT%H%M%S%fZ")
        chapter_root = namespace_root or f"projects/{project_id}/chapters/{chapter_number}"
        draft_path = self.object_store.write_text(
            f"{chapter_root}/drafts/{version}.txt",
            writer_output.body,
            content_type="text/plain; charset=utf-8",
        )

        scene_outputs: list[SceneOutput] = []
        for scene in writer_output.scene_outputs:
            scene_path = self.object_store.write_text(
                f"{chapter_root}/scenes/scene_{scene.scene_no:02d}_{version}.txt",
                scene.text,
                content_type="text/plain; charset=utf-8",
            )
            scene_outputs.append(scene.model_copy(update={"text_blob_path": scene_path}))

        updated_output = writer_output.model_copy(
            update={
                "draft_blob_path": draft_path,
                "scene_outputs": scene_outputs,
            }
        )
        meta_path = self.object_store.write_text(
            f"{chapter_root}/raw/writer_output_{version}.json",
            json.dumps(updated_output.model_dump(mode="json"), ensure_ascii=False, indent=2),
            content_type="application/json",
        )
        return {
            "draft_blob_path": draft_path,
            "meta_path": meta_path,
            "writer_output": updated_output,
        }

    def save_frozen_candidate(
        self,
        *,
        project_id: str,
        chapter_number: int,
        payload: dict,
    ) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return self.object_store.write_text(
            f"projects/{project_id}/chapters/{chapter_number}/frozen/{timestamp}.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            content_type="application/json",
        )

    def save_llm_artifact(
        self,
        *,
        project_id: str,
        artifact_kind: str,
        content: str,
        trace_scope: str = "llm",
        stage_key: str = "",
        chapter_number: int = 0,
        attempt_group_id: str = "",
        attempt_no: int = 0,
        content_type: str = "application/json",
    ) -> dict[str, object]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safe_kind = _safe_path_part(artifact_kind) or "artifact"
        safe_scope = _safe_path_part(trace_scope) or "llm"
        safe_stage = _safe_path_part(stage_key) or "stage"
        safe_group = _safe_path_part(attempt_group_id) or "attempt"
        suffix = "json" if "json" in content_type else "txt"
        chapter_part = f"chapters/{int(chapter_number)}/" if int(chapter_number or 0) > 0 else ""
        relative_path = (
            f"projects/{project_id}/{chapter_part}llm_traces/{safe_scope}/{safe_stage}/"
            f"{timestamp}_{safe_group}_{int(attempt_no or 0):02d}_{safe_kind}.{suffix}"
        )
        uri = self.object_store.write_text(
            relative_path,
            str(content or ""),
            content_type=content_type,
        )
        encoded = str(content or "").encode("utf-8")
        preview = " ".join(str(content or "").split())[:500]
        return {
            "artifact_uri": uri,
            "artifact_kind": safe_kind,
            "content_type": content_type,
            "size": len(encoded),
            "hash": hashlib.sha256(encoded).hexdigest(),
            "preview": preview,
        }

    def save_keyed_artifact(
        self,
        *,
        project_id: str,
        artifact_key: str,
        content: str,
        content_type: str,
    ) -> dict[str, object]:
        key = _safe_artifact_key(artifact_key)
        uri = self.object_store.write_text(
            f"projects/{project_id}/keyed/{key}",
            str(content or ""),
            content_type=content_type,
        )
        encoded = str(content or "").encode("utf-8")
        return {
            "artifact_uri": uri,
            "artifact_key": key,
            "content_type": content_type,
            "size": len(encoded),
            "hash": hashlib.sha256(encoded).hexdigest(),
        }

    def save_observability_diagnostic(
        self,
        *,
        project_id: str,
        chapter_number: int,
        kind: str,
        payload: dict,
        source_event_id: str = "",
        trace_id: str = "",
    ) -> dict[str, object]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safe_kind = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_"
            for ch in str(kind or "diagnostic")
        ).strip("_") or "diagnostic"
        content = json.dumps(redact_payload(payload), ensure_ascii=False, indent=2, sort_keys=True)
        uri = self.object_store.write_text(
            f"projects/{project_id}/chapters/{chapter_number}/observability/{safe_kind}_{timestamp}.json",
            content,
            content_type="application/json",
        )
        return artifact_manifest_item(
            uri=uri,
            kind=safe_kind,
            redaction_state="redacted",
            source_event_id=source_event_id,
            trace_id=trace_id,
            content=content,
        )

    def read_text(self, uri: str) -> str:
        return self.object_store.read_text(uri)

    def read_json(self, uri: str) -> dict:
        return json.loads(self.read_text(uri))


def _safe_path_part(value: object) -> str:
    text = str(value or "").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)
    return cleaned.strip("_")[:96]


def _safe_artifact_key(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact_key must be a safe relative path")
    for part in path.parts:
        if len(part) > 128 or any(
            not (ch.isalnum() or ch in {"-", "_", "."}) for ch in part
        ):
            raise ValueError("artifact_key contains an unsafe path segment")
    return path.as_posix()
