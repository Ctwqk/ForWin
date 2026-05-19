from __future__ import annotations

from pathlib import Path

import pytest

from forwin.protocol import trope_library
from forwin.protocol.trope_md_loader import load_trope_templates_from_md


PULP_LIBRARY_PATH = Path("Design-docs/trope_library_pulp_v1.md")


def test_loads_pulp_markdown_library_templates() -> None:
    templates = load_trope_templates_from_md(PULP_LIBRARY_PATH)
    template_by_id = {item.template_id: item for item in templates}

    assert len(templates) >= 8
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


def test_configured_bad_override_path_fails_visibly(monkeypatch: pytest.MonkeyPatch) -> None:
    trope_library.load_trope_template_library.cache_clear()
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", "Design-docs/does-not-exist.md")

    with pytest.raises(FileNotFoundError):
        trope_library.load_trope_template_library()

    trope_library.load_trope_template_library.cache_clear()


def test_markdown_loader_rejects_file_with_no_templates(tmp_path: Path) -> None:
    bad_library = tmp_path / "empty_trope_library.md"
    bad_library.write_text("# Notes\n\n## Not A Template\n\nNo parseable template sections.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no trope templates"):
        load_trope_templates_from_md(bad_library)


def test_markdown_override_summary_has_no_json_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    trope_library.load_trope_template_library.cache_clear()
    monkeypatch.setenv("FORWIN_TROPE_TEMPLATE_PATH", str(PULP_LIBRARY_PATH))
    templates = trope_library.load_trope_template_library()
    monkeypatch.setattr(trope_library, "TROPE_TEMPLATE_LIBRARY", templates)

    summary = trope_library.trope_registry_summary()

    assert summary.source == str(PULP_LIBRARY_PATH)
    assert summary.validation_errors == []
    assert summary.total_count >= 8

    trope_library.load_trope_template_library.cache_clear()
