from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forwin.models.publisher import (
    PublisherUploadAttempt,
    PublisherUploadJob,
    PublisherUploadReceipt,
)

from .attempts import PublisherProtocolError
from .browser_sessions import as_utc
from .protection import record_publication_receipt


class PublisherReceiptConflictError(PublisherProtocolError):
    code = "receipt_conflict"


class PublisherReceiptValidationError(PublisherProtocolError):
    code = "invalid_receipt"
    status_code = 422


class PublisherReceiptService:
    def record(
        self,
        session,
        *,
        job: PublisherUploadJob,
        attempt: PublisherUploadAttempt,
        receipt: dict[str, Any],
        source: str,
        observed_at: datetime,
    ) -> tuple[PublisherUploadReceipt, bool]:
        remote_book_id = str(receipt.get("remote_book_id") or "").strip()
        remote_chapter_id = str(receipt.get("remote_chapter_id") or "").strip()
        remote_url, parsed_book_id, parsed_chapter_id = self._normalize_remote_identity(
            platform_id=job.platform_id,
            remote_url=str(receipt.get("remote_url") or "").strip(),
        )
        if remote_book_id and parsed_book_id and remote_book_id != parsed_book_id:
            raise PublisherReceiptValidationError(
                "publisher receipt remote book ID conflicts with its URL"
            )
        if (
            remote_chapter_id
            and parsed_chapter_id
            and remote_chapter_id != parsed_chapter_id
        ):
            raise PublisherReceiptValidationError(
                "publisher receipt remote chapter ID conflicts with its URL"
            )
        remote_book_id = remote_book_id or parsed_book_id
        remote_chapter_id = remote_chapter_id or parsed_chapter_id
        official_state = str(receipt.get("official_state") or "").strip()
        content_sha256 = str(receipt.get("content_sha256") or "").strip().lower()
        expected_content = str(job.body_sha256 or "").strip().lower()
        if expected_content and content_sha256 != expected_content:
            raise PublisherReceiptValidationError(
                "publisher receipt content hash mismatch"
            )
        attempt_content = str(attempt.content_sha256 or "").strip().lower()
        if attempt_content and content_sha256 != attempt_content:
            raise PublisherReceiptValidationError(
                "publisher receipt does not match the claimed attempt content"
            )
        if job.task_kind == "cover_upload":
            try:
                job_payload = json.loads(job.result_payload_json or "{}")
            except json.JSONDecodeError:
                job_payload = {}
            expected_remote_book_id = str(
                job_payload.get("remote_book_id")
                if isinstance(job_payload, dict)
                else ""
            ).strip()
            if not expected_remote_book_id or remote_book_id != expected_remote_book_id:
                raise PublisherReceiptValidationError(
                    "cover receipt remote book ID does not match the claimed job"
                )
        if not any((remote_book_id, remote_chapter_id, remote_url)):
            raise PublisherReceiptValidationError(
                "publisher receipt requires stable remote identity"
            )
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        evidence_content_sha256 = (
            str(evidence.get("content_sha256") or "").strip().lower()
        )
        if evidence_content_sha256 != content_sha256:
            raise PublisherReceiptValidationError(
                "publisher receipt evidence content hash mismatch"
            )
        if job.task_kind == "cover_upload" and not any(
            (
                str(evidence.get("confirmation_text") or "").strip(),
                str(evidence.get("platform_message") or "").strip(),
                str(evidence.get("screenshot_sha256") or "").strip(),
            )
        ):
            raise PublisherReceiptValidationError(
                "cover receipt requires platform acceptance evidence"
            )
        client_observed_at = str(receipt.get("observed_at") or "").strip()
        if client_observed_at:
            evidence = {**evidence, "client_observed_at": client_observed_at}
        self._validate_task_receipt(
            job=job,
            remote_book_id=remote_book_id,
            remote_chapter_id=remote_chapter_id,
            remote_url=remote_url,
            official_state=official_state,
        )
        record_publication_receipt(session, job, state=official_state,
                                   remote_book_id=remote_book_id,
                                   remote_chapter_id=remote_chapter_id, evidence=evidence)
        receipt_key = self.receipt_key(
            job_identity=str(job.idempotency_key or job.id),
            platform_id=job.platform_id,
            remote_book_id=remote_book_id,
            remote_chapter_id=remote_chapter_id,
            remote_url=remote_url,
            content_sha256=content_sha256,
        )
        expected = {
            "upload_job_id": job.id,
            "idempotency_key": str(job.idempotency_key or ""),
            "platform_id": job.platform_id,
            "remote_book_id": remote_book_id,
            "remote_chapter_id": remote_chapter_id,
            "remote_url": remote_url,
            "official_state": official_state,
            "content_sha256": content_sha256,
        }
        existing = session.execute(
            select(PublisherUploadReceipt)
            .where(PublisherUploadReceipt.upload_job_id == job.id)
            .order_by(PublisherUploadReceipt.created_at.asc())
            .with_for_update()
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            self._assert_same_receipt(existing, expected)
            self._enrich_receipt(
                existing,
                official_state,
                evidence,
                observed_at,
                remote_url=remote_url,
            )
            return existing, False

        existing = session.execute(
            select(PublisherUploadReceipt)
            .where(PublisherUploadReceipt.receipt_key == receipt_key)
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            self._assert_same_receipt(existing, expected)
            self._enrich_receipt(
                existing,
                official_state,
                evidence,
                observed_at,
                remote_url=remote_url,
            )
            return existing, False

        row = PublisherUploadReceipt(
            upload_job_id=job.id,
            upload_attempt_id=attempt.id,
            receipt_key=receipt_key,
            idempotency_key=expected["idempotency_key"],
            platform_id=job.platform_id,
            remote_book_id=remote_book_id,
            remote_chapter_id=remote_chapter_id,
            remote_url=remote_url,
            official_state=official_state,
            content_sha256=content_sha256,
            evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            source=str(source or "extension").strip() or "extension",
            observed_at=observed_at,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            existing = session.execute(
                select(PublisherUploadReceipt)
                .where(PublisherUploadReceipt.receipt_key == receipt_key)
                .with_for_update()
            ).scalar_one_or_none()
            if existing is None:
                raise
            self._assert_same_receipt(existing, expected)
            self._enrich_receipt(
                existing,
                official_state,
                evidence,
                observed_at,
                remote_url=remote_url,
            )
            return existing, False
        return row, True

    @staticmethod
    def receipt_key(
        *,
        job_identity: str,
        platform_id: str,
        remote_book_id: str,
        remote_chapter_id: str,
        remote_url: str,
        content_sha256: str,
    ) -> str:
        stable_remote_url = (
            "" if remote_book_id or remote_chapter_id else str(remote_url or "").strip()
        )
        payload = "\0".join(
            (
                "publisher-receipt:v1",
                str(job_identity or "").strip(),
                str(platform_id or "").strip(),
                str(remote_book_id or "").strip(),
                str(remote_chapter_id or "").strip(),
                stable_remote_url,
                str(content_sha256 or "").strip().lower(),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def serialize(receipt: PublisherUploadReceipt) -> dict[str, Any]:
        try:
            evidence = json.loads(receipt.evidence_json or "{}")
        except json.JSONDecodeError:
            evidence = {}
        return {
            "receipt_id": receipt.id,
            "receipt_key": receipt.receipt_key,
            "remote_book_id": receipt.remote_book_id,
            "remote_chapter_id": receipt.remote_chapter_id,
            "remote_url": receipt.remote_url,
            "official_state": receipt.official_state,
            "content_sha256": receipt.content_sha256,
            "evidence": evidence if isinstance(evidence, dict) else {},
            "source": receipt.source,
            "observed_at": receipt.observed_at.isoformat(),
        }

    @staticmethod
    def _assert_same_receipt(
        receipt: PublisherUploadReceipt,
        expected: dict[str, Any],
    ) -> None:
        stored = {
            "upload_job_id": receipt.upload_job_id,
            "idempotency_key": receipt.idempotency_key,
            "platform_id": receipt.platform_id,
            "remote_book_id": receipt.remote_book_id,
            "remote_chapter_id": receipt.remote_chapter_id,
            "content_sha256": receipt.content_sha256,
        }
        mismatches = [
            field for field, value in stored.items() if value != expected.get(field)
        ]
        if mismatches:
            raise PublisherReceiptConflictError(
                "immutable publisher receipt mismatch: " + ", ".join(mismatches)
            )

    @staticmethod
    def _normalize_remote_identity(
        *,
        platform_id: str,
        remote_url: str,
    ) -> tuple[str, str, str]:
        raw = str(remote_url or "").strip()
        if not raw:
            return "", "", ""
        try:
            parsed = urlsplit(raw)
        except ValueError as exc:
            raise PublisherReceiptValidationError(
                "publisher receipt remote URL is invalid"
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PublisherReceiptValidationError(
                "publisher receipt remote URL is invalid"
            )
        remote_chapter_id = ""
        remote_book_id = ""
        canonical_query = ""
        normalized_platform = str(platform_id or "").strip()
        hostname = str(parsed.hostname or "").lower()
        if normalized_platform == "qidian":
            if hostname != "write.qq.com" and not hostname.endswith(".write.qq.com"):
                raise PublisherReceiptValidationError(
                    "publisher receipt URL does not belong to qidian"
                )
            values = parse_qs(parsed.query).get("ccid", [])
            if not values:
                values = parse_qs(parsed.fragment).get("ccid", [])
            if values and str(values[0]).isdigit():
                remote_chapter_id = str(values[0])
                canonical_query = urlencode({"ccid": remote_chapter_id})
            match = re.search(r"/CBID/(\d+)(?:/|$)", parsed.path, flags=re.IGNORECASE)
            if match:
                remote_book_id = match.group(1)
        elif normalized_platform == "fanqie":
            if hostname != "fanqienovel.com" and not hostname.endswith(
                ".fanqienovel.com"
            ):
                raise PublisherReceiptValidationError(
                    "publisher receipt URL does not belong to fanqie"
                )
            match = re.search(
                r"/main/writer/(?:chapter-manage|book-info)/(\d+)",
                parsed.path,
            )
            if match is None:
                match = re.search(r"/main/writer/(\d+)/publish(?:/|$)", parsed.path)
            if match:
                remote_book_id = match.group(1)
            values = parse_qs(parsed.query)
            for key in ("chapter_id", "item_id"):
                candidates = values.get(key, [])
                if candidates and str(candidates[0]).strip():
                    remote_chapter_id = str(candidates[0]).strip()
                    canonical_query = urlencode({"chapter_id": remote_chapter_id})
                    break
            if not remote_chapter_id:
                match = re.search(
                    r"/main/writer/(?:chapter-edit|chapter-detail)/\d+/(\d+)",
                    parsed.path,
                )
                if match:
                    remote_chapter_id = match.group(1)
        canonical_url = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/") or "/",
                canonical_query,
                "",
            )
        )
        return canonical_url, remote_book_id, remote_chapter_id

    @staticmethod
    def _validate_task_receipt(
        *,
        job: PublisherUploadJob,
        remote_book_id: str,
        remote_chapter_id: str,
        remote_url: str,
        official_state: str,
    ) -> None:
        if not remote_book_id:
            raise PublisherReceiptValidationError(
                "publisher receipt requires a stable remote book ID"
            )
        if job.task_kind == "chapter_upload":
            if not remote_chapter_id:
                raise PublisherReceiptValidationError(
                    "chapter receipt requires stable remote chapter identity"
                )
            if official_state not in {"drafted", "review_pending", "published"}:
                raise PublisherReceiptValidationError(
                    "chapter receipt has an unsupported official state"
                )
        elif job.task_kind == "cover_upload":
            if official_state != "cover_uploaded":
                raise PublisherReceiptValidationError(
                    "cover receipt must confirm cover_uploaded"
                )

    @staticmethod
    def _enrich_receipt(
        receipt: PublisherUploadReceipt,
        official_state: str,
        evidence: dict[str, Any],
        observed_at: datetime,
        *,
        remote_url: str,
    ) -> None:
        ranks = {
            "": 0,
            "drafted": 10,
            "review_pending": 20,
            "cover_uploaded": 20,
            "published": 30,
        }
        current_state = str(receipt.official_state or "")
        next_state = str(official_state or "")
        if ranks.get(next_state, 1) >= ranks.get(current_state, 1):
            receipt.official_state = next_state
        if not receipt.remote_url and remote_url:
            receipt.remote_url = remote_url
        try:
            current_evidence = json.loads(receipt.evidence_json or "{}")
        except json.JSONDecodeError:
            current_evidence = {}
        merged = current_evidence if isinstance(current_evidence, dict) else {}
        for key, value in evidence.items():
            merged.setdefault(key, value)
        receipt.evidence_json = json.dumps(merged, ensure_ascii=False, sort_keys=True)
        current_observed_at = as_utc(receipt.observed_at)
        next_observed_at = as_utc(observed_at)
        if next_observed_at is not None and (
            current_observed_at is None or next_observed_at > current_observed_at
        ):
            receipt.observed_at = observed_at


__all__ = [
    "PublisherReceiptConflictError",
    "PublisherReceiptService",
    "PublisherReceiptValidationError",
]
