from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from forwin.models.publisher import (
    PublisherChapterBinding,
    PublisherCoverAsset,
    PublisherMilestone,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from .browser_sessions import isoformat, utc_now


def _load_json_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _dump_json(payload: dict[str, Any] | list[Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _looks_like_remote_url(value: str) -> bool:
    url = str(value or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    return any(
        marker in url
        for marker in (
            "write.qq.com",
            "fanqienovel.com",
            "chapter-manage",
            "book-info",
            "dashboard",
            "portal",
        )
    )


def _url_query_values(url: str) -> dict[str, str]:
    parsed = urlparse(str(url or "").strip())
    values: dict[str, str] = {}
    for source in (parsed.query, parsed.fragment):
        for key, items in parse_qs(source, keep_blank_values=False).items():
            if items and str(items[0]).strip():
                values[str(key)] = str(items[0]).strip()
    return values


def _infer_fanqie_book_id_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if "fanqienovel.com" not in parsed.netloc:
        return ""
    match = re.search(
        r"/main/writer/(?:chapter-manage|book-info)/(?P<book_id>\d+)",
        parsed.path,
    )
    return match.group("book_id") if match else ""


def _infer_fanqie_chapter_id_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if "fanqienovel.com" not in parsed.netloc:
        return ""
    values = _url_query_values(url)
    for key in ("chapter_id", "item_id", "volume_id"):
        value = values.get(key, "")
        if value:
            return value
    match = re.search(
        r"/main/writer/(?:chapter-edit|chapter-detail)/\d+/(?P<chapter_id>\d+)",
        parsed.path,
    )
    return match.group("chapter_id") if match else ""


def _infer_qidian_book_id_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if "write.qq.com" not in parsed.netloc:
        return ""
    match = re.search(
        r"/portal/booknovels/chaptertmp/CBID/(?P<book_id>\d+)",
        parsed.path,
    )
    return match.group("book_id") if match else ""


def _infer_qidian_chapter_id_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if "write.qq.com" not in parsed.netloc:
        return ""
    values = _url_query_values(url)
    return values.get("ccid", "")


def _infer_remote_book_id(
    *,
    platform_id: str,
    result_payload: dict[str, Any],
    current_url: str = "",
) -> str:
    explicit = _first_text(
        result_payload,
        "remote_book_id",
        "remote_work_id",
        "work_id",
        "book_id",
    )
    if explicit:
        return explicit
    if platform_id == "qidian":
        return _infer_qidian_book_id_from_url(current_url)
    if platform_id == "fanqie":
        return _infer_fanqie_book_id_from_url(current_url)
    return ""


def _infer_remote_chapter_id(
    *,
    platform_id: str,
    result_payload: dict[str, Any],
    current_url: str = "",
) -> str:
    explicit = _first_text(
        result_payload,
        "remote_chapter_id",
        "chapter_id",
        "ccid",
    )
    if explicit:
        return explicit
    if platform_id == "qidian":
        return _infer_qidian_chapter_id_from_url(current_url)
    if platform_id == "fanqie":
        return _infer_fanqie_chapter_id_from_url(current_url)
    return ""


def _normalize_audit_state(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("audit_state") or payload.get("review_state") or "").strip()
    if explicit:
        return explicit
    haystack = "\n".join(
        str(payload.get(key) or "")
        for key in ("official_status", "platform_status", "platform_message", "message")
    )
    if any(marker in haystack for marker in ("待审核", "审核中", "under_review")):
        return "under_review"
    if any(marker in haystack for marker in ("审核通过", "approved")):
        return "approved"
    if any(marker in haystack for marker in ("审核失败", "被拒", "rejected")):
        return "rejected"
    return "unknown"


def _normalize_publish_state(job: PublisherUploadJob, payload: dict[str, Any]) -> str:
    official = str(payload.get("official_status") or payload.get("publish_state") or "").strip().lower()
    if official in {"draft", "drafted", "saved_draft"}:
        return "drafted"
    if official in {"published", "publish_success"}:
        return "published"
    if official in {"submitted", "under_review", "reviewing"}:
        return "submitted" if bool(job.publish) else "drafted"
    return "submitted" if bool(job.publish) else "drafted"


def _safe_raw_payload(
    *,
    job: PublisherUploadJob,
    result_payload: dict[str, Any],
    current_url: str = "",
) -> dict[str, Any]:
    payload = dict(result_payload)
    payload.pop("body", None)
    payload.pop("body_text", None)
    payload.update(
        {
            "job_id": job.id,
            "task_kind": str(job.task_kind or "chapter_upload"),
            "current_url": current_url,
        }
    )
    return payload


class PublisherBindingService:
    def __init__(self, *, session_factory) -> None:
        self.session_factory = session_factory

    def get_work_binding(
        self,
        session,
        *,
        project_id: str = "",
        platform_id: str,
        book_name: str = "",
        remote_book_id: str = "",
    ) -> PublisherWorkBinding | None:
        normalized_project_id = str(project_id or "").strip()
        normalized_platform_id = str(platform_id or "").strip()
        normalized_remote_book_id = str(remote_book_id or "").strip()
        normalized_book_name = str(book_name or "").strip()
        if normalized_project_id:
            existing = session.execute(
                select(PublisherWorkBinding)
                .where(
                    PublisherWorkBinding.project_id == normalized_project_id,
                    PublisherWorkBinding.platform_id == normalized_platform_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        if normalized_remote_book_id:
            existing = session.execute(
                select(PublisherWorkBinding)
                .where(
                    PublisherWorkBinding.platform_id == normalized_platform_id,
                    PublisherWorkBinding.remote_book_id == normalized_remote_book_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        if normalized_book_name:
            return session.execute(
                select(PublisherWorkBinding)
                .where(
                    PublisherWorkBinding.platform_id == normalized_platform_id,
                    PublisherWorkBinding.book_name == normalized_book_name,
                )
                .limit(1)
            ).scalar_one_or_none()
        return None

    def upsert_work_binding_from_upload_job(
        self,
        session,
        *,
        job: PublisherUploadJob,
        result_payload: dict[str, Any],
        current_url: str = "",
    ) -> PublisherWorkBinding:
        remote_book_id = _infer_remote_book_id(
            platform_id=job.platform_id,
            result_payload=result_payload,
            current_url=current_url,
        )
        remote_url = _first_text(
            result_payload,
            "remote_book_url",
            "work_url",
            "remote_url",
        )
        if not remote_url and _looks_like_remote_url(current_url):
            remote_url = str(current_url or "").strip()
        binding = self.get_work_binding(
            session,
            project_id=job.project_id,
            platform_id=job.platform_id,
            book_name=job.book_name,
            remote_book_id=remote_book_id,
        )
        if binding is None:
            binding = PublisherWorkBinding(
                project_id=job.project_id,
                platform_id=job.platform_id,
                book_name=job.book_name,
            )
            session.add(binding)
            session.flush()

        binding.project_id = job.project_id or binding.project_id
        binding.platform_id = job.platform_id
        binding.book_name = job.book_name or binding.book_name
        if remote_book_id:
            binding.remote_book_id = remote_book_id
        if remote_url:
            binding.remote_url = remote_url
        audit_state = _normalize_audit_state(result_payload)
        if audit_state != "unknown":
            binding.audit_state = audit_state
        binding.audit_reason = _first_text(
            result_payload, "audit_reason", "review_reason", "platform_message"
        )
        binding.platform_status = _first_text(
            result_payload, "platform_status", "official_status", "status"
        )
        binding.last_synced_at = utc_now()
        binding.raw_payload_json = _dump_json(
            _safe_raw_payload(
                job=job,
                result_payload=result_payload,
                current_url=current_url,
            )
        )
        session.flush()
        return binding

    def upsert_chapter_binding_from_upload_job(
        self,
        session,
        *,
        job: PublisherUploadJob,
        work_binding: PublisherWorkBinding,
        result_payload: dict[str, Any],
        current_url: str = "",
    ) -> PublisherChapterBinding:
        chapter_number = _as_int(result_payload.get("chapter_number"), 0)
        chapter_title = str(
            result_payload.get("chapter_title") or job.chapter_title or ""
        ).strip()
        stmt = select(PublisherChapterBinding).where(
            PublisherChapterBinding.work_binding_id == work_binding.id
        )
        if chapter_number > 0:
            stmt = stmt.where(PublisherChapterBinding.chapter_number == chapter_number)
        else:
            stmt = stmt.where(PublisherChapterBinding.chapter_title == chapter_title)
        chapter = session.execute(stmt.limit(1)).scalar_one_or_none()
        if chapter is None:
            chapter = PublisherChapterBinding(
                work_binding_id=work_binding.id,
                project_id=job.project_id,
                platform_id=job.platform_id,
                chapter_number=chapter_number,
                chapter_title=chapter_title,
            )
            session.add(chapter)
            session.flush()

        chapter.project_id = job.project_id or chapter.project_id
        chapter.platform_id = job.platform_id
        chapter.chapter_number = chapter_number or chapter.chapter_number
        chapter.chapter_title = chapter_title or chapter.chapter_title
        remote_chapter_id = _infer_remote_chapter_id(
            platform_id=job.platform_id,
            result_payload=result_payload,
            current_url=current_url,
        )
        if remote_chapter_id:
            chapter.remote_chapter_id = remote_chapter_id
        remote_url = _first_text(result_payload, "remote_chapter_url", "chapter_url")
        if not remote_url and _looks_like_remote_url(current_url):
            remote_url = str(current_url or "").strip()
        if remote_url:
            chapter.remote_url = remote_url
        chapter.publish_state = _normalize_publish_state(job, result_payload)
        audit_state = _normalize_audit_state(result_payload)
        if audit_state != "unknown":
            chapter.audit_state = audit_state
        chapter.audit_reason = _first_text(
            result_payload, "audit_reason", "review_reason", "platform_message"
        )
        chapter.word_count = _as_int(
            result_payload.get("word_count"),
            len(str(job.body_text or "")),
        )
        chapter.last_synced_at = utc_now()
        chapter.raw_payload_json = _dump_json(
            _safe_raw_payload(
                job=job,
                result_payload=result_payload,
                current_url=current_url,
            )
        )
        session.flush()
        return chapter

    def update_from_cover_upload_result(
        self,
        session,
        *,
        job: PublisherUploadJob,
        result_payload: dict[str, Any],
        current_url: str = "",
    ) -> PublisherWorkBinding | None:
        work_binding_id = _first_text(result_payload, "work_binding_id")
        binding = session.get(PublisherWorkBinding, work_binding_id) if work_binding_id else None
        if binding is None:
            binding = self.get_work_binding(
                session,
                project_id=job.project_id,
                platform_id=job.platform_id,
                book_name=job.book_name,
                remote_book_id=_first_text(result_payload, "remote_book_id", "work_id"),
            )
        if binding is None:
            return None
        cover_state = _first_text(result_payload, "cover_state", "status")
        if cover_state:
            binding.cover_state = cover_state
        if _looks_like_remote_url(current_url):
            binding.remote_url = current_url
        binding.last_synced_at = utc_now()
        binding.raw_payload_json = _dump_json(
            _safe_raw_payload(
                job=job,
                result_payload=result_payload,
                current_url=current_url,
            )
        )
        cover_asset_id = _first_text(result_payload, "cover_asset_id")
        if cover_asset_id:
            cover = session.get(PublisherCoverAsset, cover_asset_id)
            if cover is not None:
                cover.status = cover_state or cover.status
                cover.platform_validation_json = _dump_json(result_payload)
        session.flush()
        return binding

    def update_from_audit_sync_result(
        self,
        session,
        *,
        job: PublisherUploadJob,
        result_payload: dict[str, Any],
        current_url: str = "",
    ) -> PublisherWorkBinding | None:
        work_payload = result_payload.get("work")
        if not isinstance(work_payload, dict):
            work_payload = result_payload
        binding = self.upsert_work_binding_from_upload_job(
            session,
            job=job,
            result_payload=work_payload,
            current_url=current_url,
        )
        chapters = result_payload.get("chapters")
        if isinstance(chapters, list):
            for item in chapters:
                if isinstance(item, dict):
                    self.upsert_chapter_binding_from_upload_job(
                        session,
                        job=job,
                        work_binding=binding,
                        result_payload=item,
                        current_url=current_url,
                    )
        milestones = result_payload.get("milestones")
        if isinstance(milestones, list):
            for item in milestones:
                if not isinstance(item, dict):
                    continue
                milestone_type = str(item.get("milestone_type") or "").strip()
                if not milestone_type:
                    continue
                existing = session.execute(
                    select(PublisherMilestone)
                    .where(
                        PublisherMilestone.work_binding_id == binding.id,
                        PublisherMilestone.milestone_type == milestone_type,
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if existing is None:
                    existing = PublisherMilestone(
                        work_binding_id=binding.id,
                        milestone_type=milestone_type,
                    )
                    session.add(existing)
                existing.state = str(item.get("state") or existing.state or "open")
                existing.message = str(item.get("message") or existing.message or "")
                existing.evidence_json = _dump_json(item)
        session.flush()
        return binding

    def serialize_work_binding(self, binding: PublisherWorkBinding) -> dict[str, Any]:
        return {
            "id": binding.id,
            "project_id": binding.project_id,
            "platform": binding.platform_id,
            "book_name": binding.book_name,
            "remote_book_id": binding.remote_book_id,
            "remote_url": binding.remote_url,
            "audit_state": binding.audit_state,
            "audit_reason": binding.audit_reason,
            "platform_status": binding.platform_status,
            "cover_asset_id": binding.cover_asset_id,
            "cover_state": binding.cover_state,
            "last_synced_at": isoformat(binding.last_synced_at),
            "raw_payload": _load_json_object(binding.raw_payload_json),
            "created_at": isoformat(binding.created_at),
            "updated_at": isoformat(binding.updated_at),
        }

    def serialize_chapter_binding(self, binding: PublisherChapterBinding) -> dict[str, Any]:
        return {
            "id": binding.id,
            "work_binding_id": binding.work_binding_id,
            "project_id": binding.project_id,
            "platform": binding.platform_id,
            "chapter_number": binding.chapter_number,
            "chapter_title": binding.chapter_title,
            "remote_chapter_id": binding.remote_chapter_id,
            "remote_url": binding.remote_url,
            "publish_state": binding.publish_state,
            "audit_state": binding.audit_state,
            "audit_reason": binding.audit_reason,
            "word_count": binding.word_count,
            "last_synced_at": isoformat(binding.last_synced_at),
            "raw_payload": _load_json_object(binding.raw_payload_json),
            "created_at": isoformat(binding.created_at),
            "updated_at": isoformat(binding.updated_at),
        }

    def list_work_bindings(
        self,
        *,
        project_id: str = "",
        platform_id: str = "",
    ) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            stmt = select(PublisherWorkBinding).order_by(PublisherWorkBinding.updated_at.desc())
            if project_id:
                stmt = stmt.where(PublisherWorkBinding.project_id == project_id)
            if platform_id:
                stmt = stmt.where(PublisherWorkBinding.platform_id == platform_id)
            return [self.serialize_work_binding(row) for row in session.execute(stmt).scalars().all()]

    def list_chapter_bindings(
        self,
        *,
        work_binding_id: str = "",
        project_id: str = "",
        platform_id: str = "",
    ) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            stmt = select(PublisherChapterBinding).order_by(
                PublisherChapterBinding.updated_at.desc()
            )
            if work_binding_id:
                stmt = stmt.where(PublisherChapterBinding.work_binding_id == work_binding_id)
            if project_id:
                stmt = stmt.where(PublisherChapterBinding.project_id == project_id)
            if platform_id:
                stmt = stmt.where(PublisherChapterBinding.platform_id == platform_id)
            return [self.serialize_chapter_binding(row) for row in session.execute(stmt).scalars().all()]
