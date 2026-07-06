from __future__ import annotations

from forwin.protocol.trope_library import TropeTemplate, load_trope_template_library


def test_seed_trope_templates_keep_new_schema_defaults() -> None:
    template = TropeTemplate(template_id="schema-default", category="power")

    assert template.market_tier == "mainstream"
    assert template.cost_weight == 2
    assert template.platform_fit == []
    assert template.audience_fit == []
    assert template.desire_setup == ""
    assert template.anti_patterns == []


def test_default_trope_library_has_fanqie_ready_metadata() -> None:
    load_trope_template_library.cache_clear()

    templates = load_trope_template_library()

    assert len(templates) >= 50
    assert {"power", "social", "justice", "mystery", "emotion"}.issubset(
        {template.category for template in templates}
    )
    for template in templates:
        assert template.genre_fit
        assert template.audience_fit
        assert "fanqie" in template.platform_fit
        assert 1 <= template.cost_weight <= 3
        assert template.payoff_shape or template.visible_payoff
        assert template.desire_setup
        assert template.resistance
        assert template.payoff
        assert template.aftermath
