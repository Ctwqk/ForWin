from __future__ import annotations

import pytest

from forwin.runtime.policy import RuntimePolicy
from forwin.world_engine.conflict_routing import auto_option, available_options, route_conflict
from forwin.world_engine.conflict_types import ConflictDraft


def _draft(*, source: str = "world_tick", impact_level: str = "major", violated: str = "") -> ConflictDraft:
    return ConflictDraft(
        project_id="project-1",
        source=source,
        detected_after_chapter=3,
        story_time=3_000,
        involved_refs=["actor-1"],
        intent_ref="intent-1",
        violated=violated or ("replay_cas" if source == "historical_rewrite" else "impact_review"),
        impact_level=impact_level,
        summary="A protected state would change.",
        proposed_change={},
        blocks_from_chapter=4,
    )


def _policy(delegate: str) -> RuntimePolicy:
    return RuntimePolicy.for_profile("standard").with_user_settings(world_conflict_delegate=delegate)


@pytest.mark.parametrize("delegate", ["llm", "human"])
@pytest.mark.parametrize(
    ("source", "impact_level", "expected"),
    [
        ("world_tick", "minor", {"llm": "auto", "human": "auto"}),
        ("world_tick", "moderate", {"llm": "llm", "human": "human"}),
        ("world_tick", "major", {"llm": "llm", "human": "human"}),
        ("world_tick", "critical", {"llm": "human", "human": "human"}),
        ("historical_rewrite", "minor", {"llm": "human", "human": "human"}),
        ("historical_rewrite", "moderate", {"llm": "human", "human": "human"}),
        ("historical_rewrite", "major", {"llm": "human", "human": "human"}),
        ("historical_rewrite", "critical", {"llm": "human", "human": "human"}),
    ],
)
def test_route_table(source: str, impact_level: str, expected: dict[str, str], delegate: str) -> None:
    draft = _draft(source=source, impact_level=impact_level)

    assert route_conflict(draft, policy=_policy(delegate)) == expected[delegate]


def test_auto_route_defaults_to_veto() -> None:
    draft = _draft(impact_level="minor")
    assert auto_option(draft) == "veto_world"
    assert auto_option(draft) in available_options(draft)


def test_moderate_conflict_routes_to_llm_when_policy_delegates() -> None:
    assert route_conflict(_draft(impact_level="moderate"), policy=_policy("llm")) == "llm"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("world_tick", "veto_world"),
        ("chapter_extraction", "rewrite_chapter"),
        ("historical_rewrite", None),
        ("plan_feasibility", None),
        ("band_planning", None),
    ],
)
def test_auto_option_by_source(source: str, expected: str | None) -> None:
    assert auto_option(_draft(source=source, violated="protection")) == expected


@pytest.mark.parametrize("delegate", ["llm", "human"])
@pytest.mark.parametrize(
    ("impact_level", "expected"),
    [
        ("minor", {"llm": "llm", "human": "human"}),
        ("moderate", {"llm": "llm", "human": "human"}),
        ("major", {"llm": "llm", "human": "human"}),
        ("critical", {"llm": "human", "human": "human"}),
    ],
)
@pytest.mark.parametrize("violated", ["protection", "milestone"])
def test_routing_matrix_for_chapter_conflicts(
    violated: str, impact_level: str, expected: dict[str, str], delegate: str,
) -> None:
    """A residual chapter conflict is never auto-routed; its text is rewritten or the plan amended around it."""
    draft = _draft(source="chapter_extraction", impact_level=impact_level, violated=violated)

    assert route_conflict(draft, policy=_policy(delegate)) == expected[delegate]
    assert available_options(draft, live_intent=True) == ("rewrite_chapter", "amend_plan")


@pytest.mark.parametrize(
    ("source", "impact_level", "expected"),
    [
        ("world_tick", "minor", ("veto_world", "plot_responds", "amend_plan", "accept_with_obligation")),
        ("world_tick", "moderate", ("veto_world", "plot_responds", "amend_plan", "accept_with_obligation")),
        ("world_tick", "major", ("veto_world", "plot_responds", "amend_plan")),
        ("world_tick", "critical", ("veto_world", "plot_responds", "amend_plan")),
        ("historical_rewrite", "minor", ("veto_world",)),
        ("historical_rewrite", "moderate", ("veto_world",)),
        ("historical_rewrite", "major", ("veto_world",)),
        ("historical_rewrite", "critical", ("veto_world",)),
    ],
)
def test_available_options_by_source_and_impact(
    source: str, impact_level: str, expected: tuple[str, ...]
) -> None:
    options = available_options(_draft(source=source, impact_level=impact_level), live_intent=True)

    assert options == expected
    assert "rewrite_chapter" not in options


@pytest.mark.parametrize("violated", ["protection", "milestone", "reveal_schedule"])
@pytest.mark.parametrize("impact_level", ["minor", "moderate", "major", "critical"])
def test_broken_boundary_offers_only_veto_or_amend(violated: str, impact_level: str) -> None:
    assert available_options(_draft(impact_level=impact_level, violated=violated)) == ("veto_world", "amend_plan")


@pytest.mark.parametrize("violated", ["onstage_slip", "feasibility", "impact_review"])
def test_other_violations_keep_the_impact_options(violated: str) -> None:
    assert available_options(_draft(impact_level="minor", violated=violated), live_intent=True) == (
        "veto_world", "plot_responds", "amend_plan", "accept_with_obligation",
    )


@pytest.mark.parametrize("impact_level", ["minor", "moderate", "major", "critical"])
@pytest.mark.parametrize("delegate", ["llm", "human"])
def test_historical_rewrite_routing_matches_replay_cas(impact_level: str, delegate: str) -> None:
    from forwin.world_engine.conflict_events import REPLAY_CAS_DECIDER, REPLAY_CAS_OPTIONS

    draft = _draft(source="historical_rewrite", impact_level=impact_level)
    assert available_options(draft) == REPLAY_CAS_OPTIONS == ("veto_world",)
    assert route_conflict(draft, policy=_policy(delegate)) == REPLAY_CAS_DECIDER == "human"


@pytest.mark.parametrize("source", ["plan_feasibility", "band_planning"])
@pytest.mark.parametrize("violated", ["feasibility", "milestone", "reveal_schedule", "protection"])
@pytest.mark.parametrize("impact_level", ["minor", "moderate", "major", "critical"])
@pytest.mark.parametrize("delegate", ["llm", "human"])
def test_plan_conflict_offers_only_amend_and_routes_human(
    source: str, violated: str, impact_level: str, delegate: str,
) -> None:
    draft = _draft(source=source, impact_level=impact_level, violated=violated)

    assert available_options(draft) == ("amend_plan",)
    assert route_conflict(draft, policy=_policy(delegate)) == "human"


def test_band_planner_conflicts_agree_with_routing(monkeypatch) -> None:
    """Ruling P4a-R3: the conflict BandPlanner opens offers and routes what the routing table says."""
    from types import SimpleNamespace

    from forwin.planning.band_planner import service as module
    from forwin.planning.band_planner.types import PlanIssue
    from forwin.planning.beat_sheet.types import BandSpan

    created: list[tuple[ConflictDraft, str, tuple[str, ...]]] = []

    class Recorder:
        def __init__(self, session) -> None:
            pass

        def create(self, draft, *, required_decider, options):
            created.append((draft, required_decider, tuple(options)))
            return draft

    monkeypatch.setattr(module, "WorldConflictRepository", Recorder)
    monkeypatch.setattr(module, "BookStateQuery", lambda session: SimpleNamespace(
        current_story_minute=lambda project_id, as_of_chapter: 0,
    ))
    module._band_planning_conflict(
        None, project_id="project-1", band=BandSpan(band_index=0, chapter_start=1, chapter_end=4),
        issues=[PlanIssue(code="onstage_unavailable", severity="error", chapter_number=2, ref="actor-1", message="m")],
        as_of=0,
    )

    ((draft, decider, options),) = created
    assert draft.source == "band_planning"
    assert available_options(draft) == options
    for delegate in ("llm", "human"):
        assert route_conflict(draft, policy=_policy(delegate)) == decider
