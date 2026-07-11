from __future__ import annotations

from pathlib import Path

import pytest

from forwin import api_project_control_ops
from forwin.protocol import trope_library
from forwin.protocol.trope_md_loader import load_trope_templates_from_md


PULP_LIBRARY_PATH = Path("Design-docs/trope_library_pulp_v1.md")


def test_loads_pulp_markdown_library_templates() -> None:
    templates = load_trope_templates_from_md(PULP_LIBRARY_PATH)
    template_by_id = {item.template_id: item for item in templates}

    assert len(templates) >= 50
    power_level_up = template_by_id["power-level-up"]
    assert power_level_up.category == "power"
    assert power_level_up.subcategory == "升级"
    assert power_level_up.market_tier == "sinking"
    assert power_level_up.cost_weight == 1
    assert "写出主角当前的具体限制" in power_level_up.desire_setup
    assert "反派或环境" in power_level_up.resistance
    assert "具体变化" in power_level_up.payoff
    assert "三层反应" in power_level_up.aftermath
    assert power_level_up.anti_patterns
    assert power_level_up.review_signals
    assert "fanqie" in power_level_up.platform_fit
    assert power_level_up.audience_fit
    assert all(template.genre_fit for template in templates)
    assert all(
        template.payoff_shape or template.visible_payoff for template in templates
    )
    assert PULP_LIBRARY_PATH.read_text(encoding="utf-8").count("\n## ") >= 50


def test_trope_library_no_longer_generates_padding_templates() -> None:
    source = Path("forwin/protocol/trope_library.py").read_text(encoding="utf-8")

    assert "_BUILTIN_VARIANTS" not in source
    assert "_generated_template_payload" not in source


def test_configured_bad_override_path_fails_visibly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trope_library.load_trope_template_library.cache_clear()
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", "Design-docs/does-not-exist.md")

    with pytest.raises(FileNotFoundError):
        trope_library.load_trope_template_library()

    trope_library.load_trope_template_library.cache_clear()


def test_markdown_loader_rejects_file_with_no_templates(tmp_path: Path) -> None:
    bad_library = tmp_path / "empty_trope_library.md"
    bad_library.write_text(
        "# Notes\n\n## Not A Template\n\nNo parseable template sections.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no trope templates"):
        load_trope_templates_from_md(bad_library)


def test_markdown_override_summary_has_no_json_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trope_library.load_trope_template_library.cache_clear()
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", str(PULP_LIBRARY_PATH))
    templates = trope_library.load_trope_template_library()

    summary = trope_library.trope_registry_summary()

    assert summary.source == str(PULP_LIBRARY_PATH)
    assert summary.validation_errors == []
    assert summary.total_count == len(templates)
    assert summary.total_count >= 50

    trope_library.load_trope_template_library.cache_clear()


def test_markdown_override_drives_helpers_and_api_without_global_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trope_library.load_trope_template_library.cache_clear()
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", str(PULP_LIBRARY_PATH))

    templates = trope_library.load_trope_template_library()
    summary = trope_library.trope_registry_summary()
    power_templates = trope_library.trope_templates_by_category("power")
    api_power_templates = api_project_control_ops.get_trope_templates(category="power")

    assert len(templates) == summary.total_count
    assert any(template.template_id == "power-level-up" for template in power_templates)
    assert any(
        template.template_id == "power-level-up" for template in api_power_templates
    )

    trope_library.load_trope_template_library.cache_clear()


def test_markdown_override_summary_reports_effective_cached_library(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "override_trope_library.md"
    library_path.write_text(
        """
## cached-power · Cached Power

- **category**: power

## cached-social · Cached Social

- **category**: social

## cached-justice · Cached Justice

- **category**: justice

## cached-mystery · Cached Mystery

- **category**: mystery

## cached-emotion · Cached Emotion

- **category**: emotion
""".strip(),
        encoding="utf-8",
    )
    trope_library.load_trope_template_library.cache_clear()
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", str(library_path))
    templates = trope_library.load_trope_template_library()
    library_path.write_text(
        "# Corrupted after effective library load\n", encoding="utf-8"
    )

    summary = trope_library.trope_registry_summary()

    assert summary.source == str(library_path)
    assert summary.validation_errors == []
    assert summary.total_count == len(templates)
    assert summary.total_count == 5
    for category in ("power", "social", "justice", "mystery", "emotion"):
        assert summary.category_counts[category] >= 1

    trope_library.load_trope_template_library.cache_clear()


def test_markdown_loader_rejects_duplicate_template_ids(tmp_path: Path) -> None:
    duplicate_library = tmp_path / "duplicate_trope_library.md"
    duplicate_library.write_text(
        """
## dup-template · First

- **category**: power

## unique-social · Social

- **category**: social

## unique-justice · Justice

- **category**: justice

## unique-mystery · Mystery

- **category**: mystery

## unique-emotion · Emotion

- **category**: emotion

## dup-template · Duplicate

- **category**: power
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate template_id: dup-template"):
        load_trope_templates_from_md(duplicate_library)


def test_markdown_loader_parses_comma_list_schema_fields(tmp_path: Path) -> None:
    library_path = tmp_path / "list_fields_trope_library.md"
    library_path.write_text(
        """
## list-power · List Power

- **category**: power
- **risk_flags**: power_creep, repetition
- **recommended_hook_types**: advantage_reveal, status_flip
- **genre_fit**: 玄幻, 都市

## list-social · List Social

- **category**: social

## list-justice · List Justice

- **category**: justice

## list-mystery · List Mystery

- **category**: mystery

## list-emotion · List Emotion

- **category**: emotion
""".strip(),
        encoding="utf-8",
    )

    templates = load_trope_templates_from_md(library_path)
    list_power = {item.template_id: item for item in templates}["list-power"]

    assert list_power.risk_flags == ["power_creep", "repetition"]
    assert list_power.recommended_hook_types == ["advantage_reveal", "status_flip"]
    assert list_power.genre_fit == ["玄幻", "都市"]
