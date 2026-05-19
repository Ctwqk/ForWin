from __future__ import annotations

from forwin.planning.band_plan.band_role import BandRole, classify_band_role


def test_band_role_classifies_opening_mid_arc_and_final_for_30_chapter_project() -> None:
    assert classify_band_role(
        band_index=0,
        total_bands=5,
        target_total_chapters=30,
        last_chapter_of_band=6,
    ).role == BandRole.opening
    assert classify_band_role(
        band_index=2,
        total_bands=5,
        target_total_chapters=30,
        last_chapter_of_band=18,
    ).role == BandRole.mid_arc
    assert classify_band_role(
        band_index=4,
        total_bands=5,
        target_total_chapters=30,
        last_chapter_of_band=30,
    ).role == BandRole.final


def test_open_ended_project_uses_mid_arc_for_non_opening_band() -> None:
    result = classify_band_role(
        band_index=1,
        total_bands=0,
        target_total_chapters=0,
        last_chapter_of_band=12,
    )

    assert result.role == BandRole.mid_arc
    assert "no target_total_chapters" in result.reason
