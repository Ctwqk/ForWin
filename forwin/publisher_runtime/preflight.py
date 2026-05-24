from __future__ import annotations

from pathlib import Path
from typing import Any

from .platform_catalogs import PlatformMetadataCatalog


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _tags(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_clean(item) for item in values if _clean(item)]


def _issue(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


class PublisherPreflightService:
    def __init__(self, *, platform_metadata_catalog: PlatformMetadataCatalog) -> None:
        self.platform_metadata_catalog = platform_metadata_catalog

    def check_upload_readiness(
        self,
        *,
        platform_id: str,
        book_name: str,
        chapter_title: str,
        body: str,
        create_if_missing: bool = False,
        book_meta: dict[str, Any] | None = None,
        cover_upload_required: bool = False,
        selected_cover: dict[str, Any] | None = None,
        publisher_compliance_required: bool = False,
        allow_compliance_warnings: bool = False,
        latest_publisher_compliance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta = book_meta if isinstance(book_meta, dict) else {}
        platform = _clean(platform_id)
        blocking: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        if not _clean(book_name):
            blocking.append(_issue("required", "book_name", "书名不能为空。"))
        if not _clean(chapter_title) and not cover_upload_required:
            blocking.append(_issue("required", "chapter_title", "章节标题不能为空。"))
        if not _clean(body) and not cover_upload_required:
            blocking.append(_issue("required", "body", "章节正文不能为空。"))

        platform_meta = self.platform_metadata_catalog.resolve_for_platform(platform, meta)
        warnings.extend(
            _issue(item.get("code", "platform_mapping_warning"), "book_meta", item.get("message", "平台映射使用了 fallback。"))
            for item in platform_meta.get("warnings", [])
            if isinstance(item, dict)
        )

        if create_if_missing:
            intro = _clean(meta.get("intro"))
            if platform == "fanqie":
                protagonists = _tags(meta.get("protagonist_names"))
                if not protagonists:
                    blocking.append(
                        _issue(
                            "required",
                            "book_meta.protagonist_names",
                            "番茄创建作品需要主角名。",
                        )
                    )
                if len(intro) < 50 or len(intro) > 500:
                    blocking.append(
                        _issue(
                            "length",
                            "book_meta.intro",
                            "番茄创建作品简介需要 50-500 字。",
                        )
                    )
            else:
                if not intro:
                    blocking.append(
                        _issue(
                            "required",
                            "book_meta.intro",
                            "起点创建作品需要简介。",
                        )
                    )
            if not _clean(meta.get("primary_category")):
                blocking.append(
                    _issue(
                        "required",
                        "book_meta.primary_category",
                        "创建作品需要主分类。",
                    )
                )

        if cover_upload_required:
            cover = selected_cover if isinstance(selected_cover, dict) else {}
            cover_path = _clean(cover.get("file_path"))
            if not cover_path:
                blocking.append(
                    _issue(
                        "required",
                        "selected_cover.file_path",
                        "封面上传需要已选择的封面文件。",
                    )
                )
            elif not Path(cover_path).is_file():
                blocking.append(
                    _issue(
                        "not_found",
                        "selected_cover.file_path",
                        "已选择的封面文件不存在或不可读。",
                    )
                )

        requires_reviewer = False
        if publisher_compliance_required:
            compliance = latest_publisher_compliance if isinstance(latest_publisher_compliance, dict) else {}
            verdict = _clean(compliance.get("verdict")).lower()
            issues = compliance.get("issues")
            issue_rows = [item for item in issues if isinstance(item, dict)] if isinstance(issues, list) else []
            has_error = any(_clean(item.get("severity")).lower() in {"error", "critical", "blocker"} for item in issue_rows)
            has_warning = any(_clean(item.get("severity")).lower() == "warning" for item in issue_rows)
            if not compliance:
                requires_reviewer = True
                warnings.append(
                    _issue(
                        "missing_review",
                        "publisher_compliance",
                        "缺少平台合规 reviewer 结果；上传前应先完成 review。",
                    )
                )
            elif verdict in {"fail", "failed", "reject", "rejected"} or has_error:
                blocking.append(
                    _issue(
                        "review_failed",
                        "publisher_compliance",
                        "平台合规 reviewer 未通过，需要走修复链路。",
                    )
                )
            elif has_warning and not allow_compliance_warnings:
                blocking.append(
                    _issue(
                        "review_warning",
                        "publisher_compliance",
                        "平台合规 reviewer 存在 warning，当前配置不允许忽略。",
                    )
                )

        return {
            "ok": not blocking,
            "blocking": blocking,
            "warnings": warnings,
            "platform_meta": platform_meta,
            "requires_reviewer": requires_reviewer,
        }
