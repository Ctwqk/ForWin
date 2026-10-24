"""Gate facts stay topic-only through the real repair overlay and every writer builder."""

from types import SimpleNamespace

import pytest

from forwin.book_state.extraction.translator import _DeltaBuilder
from forwin.book_state.extraction.world_review import ChapterWorldReview
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.reviewer import BookStateReviewIssue, BookStateReviewVerdict
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.generation.pipeline_core.repair_patches import _current_chapter_repair_experience_plan
from forwin.naming import EntityAdmissionPlan
from forwin.protocol.book_state import FactNode, MapNode, WorldNode
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.review import world_consistency
from forwin.review.draft_service import DraftReviewService
from forwin.world_view.types import ChapterWorldView
from tests.chapter_delta_helpers import chapter_delta, world_review_output
from tests.test_writer_world_view_prompt import BUILDERS, _context, _prompt


TRUTH = "AUTHOR_ONLY_GATE_TRUTH_91a6"
MESSAGES = {
    "contradicts_hidden_fact": "fact:public is presented as true but contradicts the hidden fact:secret",
    "premature_reveal": "the reader advances on fact:secret in chapter 1; its earliest reveal chapter is 6",
    "told_by_unaware_source": "speaker tells fact:secret without knowing it (state: )",
    "unexplained_secret_knowledge": "speaker knows the secret fact:secret from a rumor",
    "future_gate_code": "fact:secret: blocked",
}


def _review(monkeypatch, code, related_refs=None):
    runtime = BookStateRuntime(
        project_id="p", as_of_chapter=0,
        world=ObjectiveWorldGraph(nodes=[
            WorldNode(id="speaker", project_id="p", node_type="character", name="Visible subject"),
            WorldNode(id="hidden-state", project_id="p", node_type="character", name="Hidden state name",
                      state={"visibility_level": "secret"}),
            WorldNode(id="hidden-profile", project_id="p", node_type="character", name="Hidden profile name",
                      profile={"visibility_level": "hidden"}),
            WorldNode(id="abstract", project_id="p", node_type="secret", name=TRUTH),
        ], facts=[
            FactNode(id="secret", project_id="p", proposition=TRUTH, sensitivity_level="secret",
                     related_node_refs=related_refs if related_refs is not None else [
                         "speaker", "hidden-state", "hidden-profile", "abstract", "place", "speaker",
                     ]),
            FactNode(id="public", project_id="p", proposition="PUBLIC_PROPOSITION"),
        ]),
        map_graph=MapGraph(nodes=[MapNode(id="place", project_id="p", node_type="site", name="Visible place")]),
    )
    translation = _DeltaBuilder(
        project_id="p", chapter_number=1, runtime=runtime, calendar=None, lexicon=BookLexicon(), start=0,
        admission_plan=EntityAdmissionPlan(project_id="p", chapter_number=1, candidate_fingerprint=""),
    ).build(chapter_delta("body"), chapter_summary="", review_verdict_id="test")
    gate = BookStateReviewVerdict(
        project_id="p", chapter_number=1, verdict_id="test", accepted=False,
        issues=[BookStateReviewIssue(
            severity="warning" if code == "unexplained_secret_knowledge" else "error",
            code=code, target_ref="fact:secret", message=MESSAGES[code],
        )],
    )
    # Only the database-backed translation/gate boundary is substituted; topic construction,
    # reviewer rendering, repair instructions, overlays and writer builders all run normally.
    monkeypatch.setattr(world_consistency, "review_chapter_extraction", lambda *args, **kwargs: ChapterWorldReview(
        translation=translation, gate=gate,
    ))
    return world_consistency.WorldConsistencyReviewer().review(
        session=None, project_id="p", chapter_number=1,
        writer_output=world_review_output(SimpleNamespace(project_id="p")), travel_tolerance=0.2,
    ).issues[0]


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("code", ["contradicts_hidden_fact", "premature_reveal", "told_by_unaware_source"])
def test_gate_topic_survives_repair_into_writer_prompt(monkeypatch, code, builder):
    issue = _review(monkeypatch, code)
    context = _context(ChapterWorldView(pov_ref="pov", pov_name="POV"))
    instruction = DraftReviewService._continuity_repair_instruction(continuity_issues=[issue], context=context)
    overlay = _current_chapter_repair_experience_plan(ChapterExperiencePlan(), instruction)
    text = _prompt(builder, context.model_copy(update={"chapter_experience_plan": overlay}))

    assert TRUTH not in text
    assert "涉及Visible subject、Visible place的未揭示真相" in text
    assert "repair must fix:" in text
    for hidden in (TRUTH, "PUBLIC_PROPOSITION", "Hidden state name", "Hidden profile name"):
        assert hidden not in issue.description + issue.suggested_fix + text
    assert issue.original_result["message"] == MESSAGES[code]
    assert issue.original_result["fact_propositions"]["fact:secret"] == TRUTH
    if code == "told_by_unaware_source":
        assert issue.entity_names == ["Visible subject"]


@pytest.mark.parametrize("code", ["unexplained_secret_knowledge", "future_gate_code"])
def test_topic_rendering_is_not_limited_to_three_gate_codes(monkeypatch, code):
    issue = _review(monkeypatch, code)
    assert TRUTH not in issue.description
    assert "涉及Visible subject、Visible place的未揭示真相" in issue.description


@pytest.mark.parametrize("related_refs", [[], ["hidden-state", "hidden-profile", "abstract"], ["missing"]])
def test_fact_without_nameable_subject_uses_neutral_topic(monkeypatch, related_refs):
    issue = _review(monkeypatch, "premature_reveal", related_refs)
    assert "未揭示真相" in issue.description
    assert "涉及的" not in issue.description
    for hidden in (TRUTH, "fact:secret", "Hidden state name", "Hidden profile name", "missing"):
        assert hidden not in issue.description


def test_suggested_fix_fact_refs_are_topics_too(monkeypatch):
    monkeypatch.setattr(world_consistency, "world_repair_fix", lambda code: "Reconsider fact:secret.")
    issue = _review(monkeypatch, "premature_reveal")
    assert "涉及Visible subject、Visible place的未揭示真相" in issue.suggested_fix
    assert TRUTH not in issue.suggested_fix
    assert "fact:secret" not in issue.suggested_fix
