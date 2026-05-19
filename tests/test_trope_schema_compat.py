from __future__ import annotations

from forwin.protocol.trope_library import TropeTemplate, load_trope_template_library


def test_seed_trope_templates_keep_new_schema_defaults() -> None:
    load_trope_template_library.cache_clear()

    first_template = load_trope_template_library()[0]

    assert isinstance(first_template, TropeTemplate)
    assert first_template.market_tier == "mainstream"
    assert first_template.cost_weight == 2
    assert first_template.desire_setup == ""
    assert first_template.anti_patterns == []
