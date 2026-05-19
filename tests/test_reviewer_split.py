from __future__ import annotations

from forwin.protocol.book_state import MapEdge, MapNode
from forwin.protocol.context import ReviewContextPack
from forwin.protocol.writer import SceneOutput, TimeAdvance, WriterOutput


def _movement_context() -> ReviewContextPack:
    nodes = [
        MapNode(id="city", project_id="project-1", node_type="settlement", name="城"),
        MapNode(id="inner", project_id="project-1", node_type="site", name="内殿"),
    ]
    edge = MapEdge(
        id="long_road",
        project_id="project-1",
        from_node_id="city",
        to_node_id="inner",
        edge_type="road",
        bidirectional=True,
        travel_time=2.0,
    )
    return ReviewContextPack(
        project_id="project-1",
        project_title="测试书",
        chapter_number=1,
        chapter_plan_title="赶路",
        chapter_plan_one_line="主角抵达内殿。",
        map_context={
            "chapter_travel_time_budget": 0.25,
            "review_graph": {
                "available": True,
                "map_nodes": [node.model_dump(mode="json") for node in nodes],
                "map_edges": [edge.model_dump(mode="json")],
            },
        },
    )


def _movement_output() -> WriterOutput:
    return WriterOutput(
        project_id="project-1",
        chapter_number=1,
        title="赶路",
        body="主角从城中抵达内殿。",
        end_of_chapter_summary="主角抵达内殿。",
        scene_outputs=[
            SceneOutput(scene_no=1, scene_objective="出发", scene_location_id="city", text="出发。"),
            SceneOutput(scene_no=2, scene_objective="抵达", scene_location_id="inner", text="抵达。"),
        ],
        time_advance=TimeAdvance(new_time_label="片刻后", duration_description="片刻后"),
    )


def test_map_movement_reviewer_owns_deterministic_movement_issue() -> None:
    from forwin.reviewer.map_movement import MapMovementReviewer

    verdict = MapMovementReviewer().review(_movement_context(), _movement_output())

    assert verdict.verdict == "fail"
    assert [issue.rule_name for issue in verdict.issues] == ["map_travel_time_exceeds_chapter_time"]
    assert verdict.issues[0].reviewer == "map_movement"


def test_webnovel_reviewer_facade_keeps_legacy_movement_behavior_without_owning_method() -> None:
    from forwin.reviewer.webnovel import WebNovelExperienceReviewer

    reviewer = WebNovelExperienceReviewer(llm_enabled=False)
    verdict = reviewer.review(_movement_context(), _movement_output())

    assert not hasattr(WebNovelExperienceReviewer, "_map_movement_issue")
    assert any(issue.rule_name == "map_travel_time_exceeds_chapter_time" for issue in verdict.issues)


def test_llm_webnovel_reviewer_owns_llm_prompt_and_json_repair() -> None:
    from forwin.reviewer.llm_webnovel import LLMWebNovelReviewer
    from forwin.reviewer.webnovel import WebNovelExperienceReviewer

    assert hasattr(LLMWebNovelReviewer, "_llm_review_messages")
    assert hasattr(LLMWebNovelReviewer, "_repair_llm_json")
    assert hasattr(LLMWebNovelReviewer, "_verdict_from_payload")
    assert not hasattr(WebNovelExperienceReviewer, "_review_with_llm")
    assert not hasattr(WebNovelExperienceReviewer, "_llm_payload")
    assert not hasattr(WebNovelExperienceReviewer, "_repair_llm_json")


def test_webnovel_reviewer_no_longer_carries_legacy_map_movement_helpers() -> None:
    from forwin.reviewer.webnovel import WebNovelExperienceReviewer

    assert not hasattr(WebNovelExperienceReviewer, "_legacy_map_movement_issue")
    assert not hasattr(WebNovelExperienceReviewer, "_observer_cognition_views")
    assert not hasattr(WebNovelExperienceReviewer, "_map_path_issue")


def test_historical_review_hub_accepts_split_reviewer_ports() -> None:
    from forwin.protocol.review import ReviewVerdict
    from forwin.reviewer.hub import HistoricalReviewHub

    class Continuity:
        verdict = "pass"
        issues = []
        review_summary = ""

    class Checker:
        def check(self, project_id, writer_output):
            return Continuity()

    class StubReviewer:
        def __init__(self, verdict: str = "pass") -> None:
            self.verdict = verdict
            self.calls = 0

        def review(self, context, writer_output, **_kwargs):
            self.calls += 1
            return ReviewVerdict(verdict=self.verdict, issues=[])

    class StubLintCollector:
        def collect(self, writer_output):
            return []

    experience = StubReviewer()
    map_movement = StubReviewer("pass")
    governance = StubReviewer("pass")
    personality = StubReviewer("pass")
    hub = HistoricalReviewHub(
        experience_reviewer=experience,
        map_movement_reviewer=map_movement,
        governance_reviewer=governance,
        personality_reviewer=personality,
        lint_collector=StubLintCollector(),
    )

    verdict = hub.review(
        project_id="project-1",
        context=_movement_context(),
        writer_output=_movement_output(),
        continuity_checker=Checker(),
    )

    assert verdict.verdict == "pass"
    assert experience.calls == 1
    assert map_movement.calls == 1
    assert governance.calls == 1
    assert personality.calls == 1


def test_historical_review_hub_merge_preserves_arc_repair_scope() -> None:
    from forwin.protocol.review import RepairInstruction
    from forwin.reviewer.hub import HistoricalReviewHub

    base = RepairInstruction(
        repair_scope="chapter_plan",
        failure_type="mixed",
        must_fix=["chapter pacing"],
        scope_reason="chapter-level issue",
    )
    arc = RepairInstruction(
        repair_scope="arc",
        failure_type="mixed",
        must_fix=["identity ambiguity"],
        scope_reason="arc-level issue",
    )

    merged = HistoricalReviewHub._merge_repair_instructions(
        continuity_instruction=base,
        governance_instruction=None,
        webnovel_instruction=arc,
    )

    assert merged is not None
    assert merged.repair_scope == "arc"
