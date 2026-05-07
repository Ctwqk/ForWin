from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC_ROOT = ROOT / "Design-docs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_current_architecture_and_design_status_are_documented() -> None:
    current = _read(DOC_ROOT / "CURRENT_ARCHITECTURE.md")
    status = _read(DOC_ROOT / "DESIGN_STATUS.md")
    readme = _read(ROOT / "README.md")

    assert "唯一 canon source 是 `BookState DB Canon`" in current
    assert "地图 canon：`BookMap / Scheme C`" in current
    assert "`world_model_v4`：world_v4 compatibility projection" in current
    assert "`reviewer_v4`：world_v4 extraction compatibility gate" in current
    assert "CURRENT_ARCHITECTURE.md" in readme
    assert "DESIGN_STATUS.md" in readme

    for expected in [
        "`CURRENT_ARCHITECTURE.md` | active-current",
        "`V4.5_markstone.md` | active-current",
        "`V4_final_book_state_runtime.md` | active-current",
        "`map_scheme_c.md` | active-current",
        "`V2_9_2.md` | baseline-with-overrides",
        "`provisional_mechanism_check.md` | legacy-compatibility",
        "`docs/superpowers/plans/2026-04-24-forwin-v4-world-model.md` | historical-plan",
    ]:
        assert expected in status


def test_historical_superpowers_plans_are_not_current_architecture_sources() -> None:
    for rel_path in [
        "docs/superpowers/plans/2026-04-24-forwin-v4-world-model.md",
        "docs/superpowers/plans/2026-04-24-forwin-v4-1-runtime-hardening.md",
    ]:
        text = _read(ROOT / rel_path)
        assert "Status: historical implementation plan" in text
        assert "CURRENT_ARCHITECTURE.md" in text
        assert "DESIGN_STATUS.md" in text


def test_design_guard_does_not_reintroduce_local_subworld_semantics() -> None:
    active_docs = [
        "CURRENT_ARCHITECTURE.md",
        "V4.5_markstone.md",
        "V4.5.1_markstone.md",
        "V4_final_book_state_runtime.md",
        "map_scheme_c.md",
        "writing_flow_state_machine.md",
    ]
    forbidden_phrases = [
        "SubWorld = 城市",
        "SubWorld=城市",
        "把城市建成 `SubWorld`",
        "城市/客栈/遗迹入口作为 SubWorld",
    ]
    offenders = [
        (name, phrase)
        for name in active_docs
        for phrase in forbidden_phrases
        if phrase in _read(DOC_ROOT / name)
    ]

    assert offenders == []
