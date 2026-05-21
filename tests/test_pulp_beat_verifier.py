from __future__ import annotations

from forwin.checker.pulp_beat import verify_pulp_beats


def test_verify_pulp_beats_detects_core_payoff() -> None:
    body = "众人嘲笑他没资格。林远当场拿出合同，老板脸色大变，当众道歉，还赔偿三十万。门外忽然传来新的威胁。"

    result = verify_pulp_beats(body)

    assert result.pressure_present is True
    assert result.protagonist_action_present is True
    assert result.visible_payoff_present is True
    assert result.audience_reaction_present is True
    assert result.enemy_or_obstacle_damage_present is True
    assert result.new_gain_or_status_shift_present is True
    assert result.next_hook_present is True


def test_verify_pulp_beats_flags_missing_payoff() -> None:
    result = verify_pulp_beats("他走在路上，想了很多前情，夜色越来越深。")

    assert result.visible_payoff_present is False
    assert "visible_payoff_present" in result.missing_fields
