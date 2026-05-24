from __future__ import annotations

import base64
import json
import os
import struct
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from forwin.config import DEFAULT_MINIMAX_BASE_URL
from forwin.models.publisher import (
    PublisherChapterBinding,
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from .browser_sessions import utc_now


def _load_json_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _dump_json(payload: dict[str, Any] | list[Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _safe_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or ""))
    return cleaned.strip("_") or "manual"


def _image_dimensions(data: bytes) -> tuple[str, int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", int(width), int(height)
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            segment_length = struct.unpack(">H", data[index:index + 2])[0]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3} and index + 7 < len(data):
                height, width = struct.unpack(">HH", data[index + 3:index + 7])
                return "image/jpeg", int(width), int(height)
            index += segment_length
    return "", 0, 0


class MiniMaxImageClient:
    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_MINIMAX_BASE_URL,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.api_key = str(api_key or os.environ.get("MINIMAX_API_KEY", "")).strip()
        self.base_url = str(base_url or os.environ.get("MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL)).rstrip("/")
        self.timeout_seconds = float(timeout_seconds or 120.0)

    def generate_images(
        self,
        *,
        prompt: str,
        model: str = "image-01",
        count: int = 4,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("MINIMAX_API_KEY 未设置，无法生成封面。")
        url = f"{self.base_url}/image_generation"
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "prompt": prompt,
                "n": count,
                "response_format": "base64",
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        request_id = str(payload.get("request_id") or payload.get("id") or "")
        rows = payload.get("data") or payload.get("images") or []
        if isinstance(rows, dict):
            rows = [rows]
        result: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            b64 = row.get("base64") or row.get("image_base64") or row.get("b64_json")
            if b64:
                result.append(
                    {
                        "base64": b64,
                        "mime_type": row.get("mime_type") or "image/png",
                        "request_id": request_id,
                        "raw": {k: v for k, v in row.items() if k not in {"base64", "image_base64", "b64_json"}},
                    }
                )
                continue
            image_url = row.get("url") or row.get("image_url")
            if image_url:
                image_response = httpx.get(str(image_url), timeout=self.timeout_seconds)
                image_response.raise_for_status()
                result.append(
                    {
                        "bytes": image_response.content,
                        "mime_type": image_response.headers.get("content-type", "image/png").split(";")[0],
                        "request_id": request_id,
                        "raw": {"url": image_url},
                    }
                )
        return result


class PublisherCoverService:
    def __init__(
        self,
        *,
        session_factory,
        image_client: MiniMaxImageClient | None = None,
        cover_dir: str | Path = "",
        minimax_model: str = "image-01",
    ) -> None:
        self.session_factory = session_factory
        self.image_client = image_client or MiniMaxImageClient()
        self.cover_dir = Path(cover_dir or os.environ.get("FORWIN_PUBLISHER_COVER_DIR", "var/publisher_covers"))
        self.minimax_model = str(minimax_model or "image-01")

    def generate_cover_candidates(
        self,
        *,
        project_id: str = "",
        platform_id: str,
        book_name: str,
        book_meta: dict[str, Any] | None = None,
        candidate_count: int = 4,
        cover_style_hint: str = "",
        work_binding_id: str = "",
        cover_confirmation_required: bool = False,
    ) -> dict[str, Any]:
        meta = book_meta if isinstance(book_meta, dict) else {}
        count = max(1, min(int(candidate_count or 4), 8))
        prompt = self.build_prompt(
            book_name=book_name,
            book_meta=meta,
            cover_style_hint=cover_style_hint,
        )
        rows = self.image_client.generate_images(
            prompt=prompt,
            model=self.minimax_model,
            count=count,
        )
        created: list[PublisherCoverAsset] = []
        with self.session_factory() as session:
            for index, row in enumerate(rows):
                asset = self._store_candidate(
                    session,
                    row=row,
                    index=index,
                    project_id=project_id,
                    platform_id=platform_id,
                    work_binding_id=work_binding_id,
                    prompt=prompt,
                    book_meta=meta,
                )
                created.append(asset)
            selected = self._select_best_candidate(
                session,
                created,
                cover_confirmation_required=cover_confirmation_required,
            )
            session.commit()
            return {
                "ok": selected is not None,
                "cover_asset_ids": [asset.id for asset in created],
                "selected_cover_asset_id": selected.id if selected is not None else "",
                "prompt": prompt,
                "failure_reason": "" if selected is not None else "没有生成可用封面。",
            }

    def generate_for_job(self, job_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None:
                raise ValueError("封面生成任务不存在。")
            payload = _load_json_object(job.result_payload_json)
        result = self.generate_cover_candidates(
            project_id=str(payload.get("project_id") or job.project_id or ""),
            platform_id=job.platform_id,
            book_name=job.book_name,
            book_meta=payload.get("book_meta") if isinstance(payload.get("book_meta"), dict) else {},
            candidate_count=int(payload.get("cover_candidate_count") or 4),
            cover_style_hint=str(payload.get("cover_style_hint") or ""),
            cover_confirmation_required=bool(payload.get("cover_confirmation_required", False)),
        )
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is not None:
                payload = _load_json_object(job.result_payload_json)
                payload.update(result)
                job.result_payload_json = _dump_json(payload)
                job.result_message = "封面候选已生成。" if result.get("ok") else "封面生成失败。"
                job.error_message = "" if result.get("ok") else str(result.get("failure_reason") or "")
                job.status = "succeeded" if result.get("ok") else "failed"
                job.finished_at = utc_now()
                if result.get("ok"):
                    self.enqueue_cover_upload_if_ready(
                        session,
                        job=job,
                        payload=payload,
                    )
            session.commit()
        return result

    def build_prompt(
        self,
        *,
        book_name: str,
        book_meta: dict[str, Any],
        cover_style_hint: str = "",
    ) -> str:
        parts = [
            f"为中文网络小说《{book_name}》生成封面主视觉插画。",
            "不要生成可读文字、标题字或作者名；只生成适合后续排版的封面图。",
        ]
        for key, label in (
            ("primary_category", "类型"),
            ("intro", "简介"),
        ):
            value = str(book_meta.get(key) or "").strip()
            if value:
                parts.append(f"{label}：{value}")
        protagonists = book_meta.get("protagonist_names")
        if isinstance(protagonists, list) and protagonists:
            parts.append("主角：" + "、".join(str(item) for item in protagonists[:3]))
        tags: list[str] = []
        for key in ("theme_tags", "role_tags", "plot_tags"):
            values = book_meta.get(key)
            if isinstance(values, list):
                tags.extend(str(item).strip() for item in values if str(item).strip())
        if tags:
            parts.append("关键词：" + "、".join(tags[:8]))
        if cover_style_hint:
            parts.append("风格要求：" + cover_style_hint)
        parts.append("画面应清晰、商业网文封面感、主体突出、竖版构图。")
        return "\n".join(parts)

    def _store_candidate(
        self,
        session,
        *,
        row: dict[str, Any],
        index: int,
        project_id: str,
        platform_id: str,
        work_binding_id: str,
        prompt: str,
        book_meta: dict[str, Any],
    ) -> PublisherCoverAsset:
        data = row.get("bytes")
        if data is None and row.get("base64"):
            data = base64.b64decode(str(row.get("base64") or ""))
        data = bytes(data or b"")
        detected_mime, width, height = _image_dimensions(data)
        mime_type = detected_mime or str(row.get("mime_type") or "")
        extension = ".jpg" if mime_type == "image/jpeg" else ".png"
        asset = PublisherCoverAsset(
            project_id=project_id,
            work_binding_id=work_binding_id,
            source="minimax",
            prompt=prompt,
            source_meta_json=_dump_json(
                {
                    "platform": platform_id,
                    "book_meta": book_meta,
                    "index": index,
                    "raw": row.get("raw") if isinstance(row.get("raw"), dict) else {},
                }
            ),
            status="generated",
            selection_state="candidate",
            score=float(row.get("score") or 0.0),
            width=width,
            height=height,
            file_size_bytes=len(data),
            mime_type=mime_type,
            platform_validation_json=_dump_json(
                self.validate_candidate_bytes(data=data, mime_type=mime_type, width=width, height=height)
            ),
            minimax_request_id=str(row.get("request_id") or ""),
            raw_payload_json=_dump_json({k: v for k, v in row.items() if k not in {"bytes", "base64"}}),
        )
        session.add(asset)
        session.flush()
        if data:
            folder = self.cover_dir / _safe_part(project_id or "manual") / _safe_part(platform_id)
            folder.mkdir(parents=True, exist_ok=True)
            file_path = folder / f"{asset.id}{extension}"
            file_path.write_bytes(data)
            asset.file_path = str(file_path)
        return asset

    def validate_candidate_bytes(
        self,
        *,
        data: bytes,
        mime_type: str,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not data:
            errors.append("empty_file")
        if mime_type not in {"image/png", "image/jpeg"}:
            errors.append("unsupported_format")
        if width <= 0 or height <= 0:
            errors.append("unknown_dimensions")
        if len(data) > 5 * 1024 * 1024:
            warnings.append("file_larger_than_5m")
        return {"valid": not errors, "errors": errors, "warnings": warnings}

    def _select_best_candidate(
        self,
        session,
        assets: list[PublisherCoverAsset],
        *,
        cover_confirmation_required: bool,
    ) -> PublisherCoverAsset | None:
        valid_assets = [
            asset
            for asset in assets
            if _load_json_object(asset.platform_validation_json).get("valid") is True
        ]
        if not valid_assets:
            for asset in assets:
                asset.status = "failed"
            return None
        selected = sorted(valid_assets, key=lambda asset: float(asset.score or 0.0), reverse=True)[0]
        for asset in assets:
            if asset.id == selected.id:
                asset.status = "generated" if cover_confirmation_required else "selected"
                asset.selection_state = "candidate" if cover_confirmation_required else "selected"
            else:
                asset.selection_state = "candidate"
        session.flush()
        return selected

    def selected_cover_for_project(
        self,
        session,
        *,
        project_id: str,
        work_binding_id: str = "",
    ) -> PublisherCoverAsset | None:
        stmt = select(PublisherCoverAsset).where(
            PublisherCoverAsset.selection_state.in_(["selected", "approved"])
        )
        if work_binding_id:
            stmt = stmt.where(
                PublisherCoverAsset.work_binding_id.in_([work_binding_id, ""])
            )
        if project_id:
            stmt = stmt.where(PublisherCoverAsset.project_id == project_id)
        return session.execute(
            stmt.order_by(PublisherCoverAsset.updated_at.desc()).limit(1)
        ).scalar_one_or_none()

    def enqueue_cover_upload_if_ready(
        self,
        session,
        *,
        job: PublisherUploadJob,
        payload: dict[str, Any],
        work_binding: PublisherWorkBinding | None = None,
    ) -> PublisherUploadJob | None:
        if bool(payload.get("cover_confirmation_required", False)):
            return None
        if payload.get("auto_cover_upload_enabled") is False:
            return None
        if work_binding is None:
            work_binding_id = str(payload.get("work_binding_id") or "").strip()
            work_binding = session.get(PublisherWorkBinding, work_binding_id) if work_binding_id else None
        if work_binding is None:
            work_binding = session.execute(
                select(PublisherWorkBinding)
                .where(
                    PublisherWorkBinding.project_id == job.project_id,
                    PublisherWorkBinding.platform_id == job.platform_id,
                )
                .limit(1)
            ).scalar_one_or_none()
        if work_binding is None:
            return None
        first_chapter = session.execute(
            select(PublisherChapterBinding)
            .where(
                PublisherChapterBinding.work_binding_id == work_binding.id,
                PublisherChapterBinding.publish_state.in_(["published", "submitted", "drafted"]),
            )
            .limit(1)
        ).scalar_one_or_none()
        if first_chapter is None:
            return None
        cover = self.selected_cover_for_project(
            session,
            project_id=work_binding.project_id,
            work_binding_id=work_binding.id,
        )
        if cover is None:
            return None
        existing = session.execute(
            select(PublisherUploadJob)
            .where(
                PublisherUploadJob.task_kind == "cover_upload",
                PublisherUploadJob.platform_id == work_binding.platform_id,
                PublisherUploadJob.status.in_(["pending", "running", "succeeded"]),
                PublisherUploadJob.result_payload_json.contains(cover.id),
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return None
        cover.work_binding_id = work_binding.id
        work_binding.cover_asset_id = cover.id
        work_binding.cover_state = "queued"
        upload_payload = {
            "project_id": work_binding.project_id,
            "work_binding_id": work_binding.id,
            "platform": work_binding.platform_id,
            "book_name": work_binding.book_name,
            "remote_book_id": work_binding.remote_book_id,
            "remote_url": work_binding.remote_url,
            "cover_asset_id": cover.id,
            "file_path": cover.file_path,
            "auto_cover_upload_enabled": True,
        }
        upload_job = PublisherUploadJob(
            project_id=work_binding.project_id,
            platform_id=work_binding.platform_id,
            task_kind="cover_upload",
            status="pending",
            book_name=work_binding.book_name,
            chapter_title="",
            body_text="",
            upload_url=work_binding.remote_url,
            publish=False,
            result_message="封面上传任务已创建，等待浏览器扩展执行。",
            result_payload_json=_dump_json(upload_payload),
        )
        session.add(upload_job)
        session.flush()
        return upload_job

    def list_cover_assets(
        self,
        *,
        project_id: str = "",
        work_binding_id: str = "",
    ) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            stmt = select(PublisherCoverAsset).order_by(PublisherCoverAsset.updated_at.desc())
            if project_id:
                stmt = stmt.where(PublisherCoverAsset.project_id == project_id)
            if work_binding_id:
                stmt = stmt.where(PublisherCoverAsset.work_binding_id == work_binding_id)
            return [self.serialize_cover_asset(row) for row in session.execute(stmt).scalars().all()]

    def set_cover_selection(
        self,
        cover_asset_id: str,
        *,
        selection_state: str,
        status: str = "",
    ) -> dict[str, Any]:
        normalized_state = str(selection_state or "").strip()
        if normalized_state not in {"candidate", "selected", "approved", "rejected"}:
            raise ValueError("不支持的封面选择状态。")
        with self.session_factory() as session:
            cover = session.get(PublisherCoverAsset, cover_asset_id)
            if cover is None:
                raise ValueError("封面不存在。")
            if normalized_state in {"selected", "approved"}:
                existing = session.execute(
                    select(PublisherCoverAsset).where(
                        PublisherCoverAsset.id != cover.id,
                        PublisherCoverAsset.project_id == cover.project_id,
                        PublisherCoverAsset.selection_state.in_(["selected", "approved"]),
                    )
                ).scalars().all()
                for row in existing:
                    row.selection_state = "candidate"
                    if row.status in {"selected", "approved"}:
                        row.status = "generated"
            cover.selection_state = normalized_state
            cover.status = status or (
                "selected"
                if normalized_state == "selected"
                else "approved" if normalized_state == "approved" else normalized_state
            )
            session.commit()
            session.refresh(cover)
            return self.serialize_cover_asset(cover)

    def serialize_cover_asset(self, asset: PublisherCoverAsset) -> dict[str, Any]:
        return {
            "id": asset.id,
            "project_id": asset.project_id,
            "work_binding_id": asset.work_binding_id,
            "source": asset.source,
            "prompt": asset.prompt,
            "source_meta": _load_json_object(asset.source_meta_json),
            "status": asset.status,
            "selection_state": asset.selection_state,
            "score": asset.score,
            "score_reasons": json.loads(asset.score_reasons_json or "[]"),
            "width": asset.width,
            "height": asset.height,
            "file_size_bytes": asset.file_size_bytes,
            "file_path": asset.file_path,
            "mime_type": asset.mime_type,
            "platform_validation": _load_json_object(asset.platform_validation_json),
            "minimax_request_id": asset.minimax_request_id,
            "raw_payload": _load_json_object(asset.raw_payload_json),
        }
