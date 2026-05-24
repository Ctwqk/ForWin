from __future__ import annotations

from forwin.publisher_runtime.platform_catalogs import PlatformMetadataCatalog


def test_qidian_audience_and_category_resolve() -> None:
    catalog = PlatformMetadataCatalog()

    resolved = catalog.resolve_for_platform(
        "qidian",
        {
            "audience": "male",
            "primary_category": "玄幻",
            "theme_tags": ["传统玄幻"],
        },
    )

    assert resolved["resolved_audience"]["value"] == "male"
    assert resolved["resolved_audience"]["label"] == "男生"
    assert resolved["resolved_primary_category"]["label"] == "玄幻"
    assert resolved["warnings"] == []


def test_qidian_female_audience_resolves_to_female_site() -> None:
    catalog = PlatformMetadataCatalog()

    resolved = catalog.resolve_for_platform(
        "qidian",
        {"audience": "female", "primary_category": "古代言情"},
    )

    assert resolved["resolved_audience"]["label"] == "女生"


def test_fanqie_audience_category_and_tags_resolve() -> None:
    catalog = PlatformMetadataCatalog()

    resolved = catalog.resolve_for_platform(
        "fanqie",
        {
            "audience": "male",
            "primary_category": "都市日常",
            "theme_tags": ["都市生活"],
            "role_tags": ["系统流"],
            "plot_tags": ["悬疑"],
        },
    )

    assert resolved["resolved_audience"]["value"] == 1
    assert resolved["resolved_primary_category"]["label"] == "都市"
    assert "都市生活" in resolved["resolved_theme_tags"]
    assert "系统流" in resolved["resolved_role_tags"]
    assert "悬疑灵异" in resolved["resolved_plot_tags"]


def test_missing_mapping_returns_deterministic_fallback_warning() -> None:
    catalog = PlatformMetadataCatalog()

    resolved = catalog.resolve_for_platform(
        "fanqie",
        {"audience": "unknown", "primary_category": "完全不存在的类型"},
    )

    assert resolved["resolved_audience"]["value"] == 1
    assert resolved["resolved_primary_category"]["label"] == "都市"
    assert any("fallback" in item["code"] for item in resolved["warnings"])
