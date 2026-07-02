from __future__ import annotations

from pathlib import Path

from forwin.publisher_runtime.platform_catalogs import PlatformMetadataCatalog
from forwin.publisher_runtime.preflight import PublisherPreflightService


def _preflight() -> PublisherPreflightService:
    return PublisherPreflightService(platform_metadata_catalog=PlatformMetadataCatalog())


def test_qidian_create_if_missing_blocks_missing_intro() -> None:
    result = _preflight().check_upload_readiness(
        platform_id="qidian",
        book_name="测试书",
        chapter_title="第一章",
        body="正文",
        create_if_missing=True,
        book_meta={"audience": "male", "primary_category": "玄幻"},
    )

    assert result["ok"] is False
    assert any(item["field"] == "book_meta.intro" for item in result["blocking"])


def test_fanqie_create_if_missing_blocks_missing_protagonist() -> None:
    result = _preflight().check_upload_readiness(
        platform_id="fanqie",
        book_name="测试书",
        chapter_title="第一章",
        body="正文",
        create_if_missing=True,
        book_meta={"audience": "male", "primary_category": "都市日常", "intro": "这是一段超过五十字的简介，用于模拟番茄建书时需要填写的作品简介。它应当足够长，避免长度校验失败。"},
    )

    assert result["ok"] is False
    assert any(item["field"] == "book_meta.protagonist_names" for item in result["blocking"])


def test_fanqie_intro_length_is_checked() -> None:
    result = _preflight().check_upload_readiness(
        platform_id="fanqie",
        book_name="测试书",
        chapter_title="第一章",
        body="正文",
        create_if_missing=True,
        book_meta={
            "audience": "male",
            "primary_category": "都市日常",
            "intro": "太短",
            "protagonist_names": ["韩砚"],
        },
    )

    assert result["ok"] is False
    assert any(item["field"] == "book_meta.intro" for item in result["blocking"])


def test_non_create_chapter_upload_does_not_require_book_intro() -> None:
    result = _preflight().check_upload_readiness(
        platform_id="qidian",
        book_name="测试书",
        chapter_title="第一章",
        body="正文",
        create_if_missing=False,
        book_meta={},
    )

    assert result["ok"] is True


def test_selected_cover_readability_is_checked_when_required(tmp_path: Path) -> None:
    missing = tmp_path / "missing.png"
    result = _preflight().check_upload_readiness(
        platform_id="qidian",
        book_name="测试书",
        chapter_title="第一章",
        body="正文",
        create_if_missing=False,
        book_meta={},
        cover_upload_required=True,
        selected_cover={"file_path": str(missing)},
    )

    assert result["ok"] is False
    assert any(item["field"] == "selected_cover.file_path" for item in result["blocking"])


def test_publisher_compliance_failure_blocks_when_required() -> None:
    result = _preflight().check_upload_readiness(
        platform_id="qidian",
        book_name="测试书",
        chapter_title="第一章",
        body="正文",
        create_if_missing=False,
        book_meta={},
        publisher_compliance_required=True,
        latest_publisher_compliance={
            "verdict": "fail",
            "issues": [{"rule_name": "publisher_compliance_contact"}],
        },
    )

    assert result["ok"] is False
    assert any(item["field"] == "publisher_compliance" for item in result["blocking"])


def test_publisher_compliance_missing_blocks_when_required() -> None:
    result = _preflight().check_upload_readiness(
        platform_id="qidian",
        book_name="测试书",
        chapter_title="第一章",
        body="正文",
        create_if_missing=False,
        book_meta={},
        publisher_compliance_required=True,
    )

    assert result["ok"] is False
    assert result["requires_reviewer"] is True
    assert any(item["code"] == "missing_review" for item in result["blocking"])


def test_publisher_compliance_warning_does_not_block_when_allowed() -> None:
    result = _preflight().check_upload_readiness(
        platform_id="qidian",
        book_name="测试书",
        chapter_title="第一章",
        body="正文",
        create_if_missing=False,
        book_meta={},
        publisher_compliance_required=True,
        allow_compliance_warnings=True,
        latest_publisher_compliance={
            "verdict": "pass",
            "issues": [{"severity": "warning", "rule_name": "publisher_compliance_soft"}],
        },
    )

    assert result["ok"] is True
