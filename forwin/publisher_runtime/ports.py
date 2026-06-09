from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class PublisherJobBatchRequest:
    platform: str
    book_name: str
    jobs: list[dict[str, Any]]
    upload_url: str | None
    publish: bool
    project_id: str = ""
    create_if_missing: bool = False
    book_meta: dict[str, Any] | None = None
    cover_generation_enabled: bool = True
    cover_confirmation_required: bool = False
    cover_candidate_count: int = 4
    cover_style_hint: str = ""
    auto_cover_upload_enabled: bool = True
    publisher_compliance_required: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class PublisherJobClient(Protocol):
    def create_upload_jobs_batch(self, request: PublisherJobBatchRequest) -> int:
        ...


class PublisherRuntimeJobClient:
    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def create_upload_jobs_batch(self, request: PublisherJobBatchRequest) -> int:
        return self.runtime.create_upload_jobs_batch(
            project_id=request.project_id,
            platform=request.platform,
            book_name=request.book_name,
            jobs=request.jobs,
            upload_url=request.upload_url,
            publish=request.publish,
            create_if_missing=request.create_if_missing,
            book_meta=request.book_meta,
            cover_generation_enabled=request.cover_generation_enabled,
            cover_confirmation_required=request.cover_confirmation_required,
            cover_candidate_count=request.cover_candidate_count,
            cover_style_hint=request.cover_style_hint,
            auto_cover_upload_enabled=request.auto_cover_upload_enabled,
            publisher_compliance_required=request.publisher_compliance_required,
        )


__all__ = [
    "PublisherJobBatchRequest",
    "PublisherJobClient",
    "PublisherRuntimeJobClient",
]
