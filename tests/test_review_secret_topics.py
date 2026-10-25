"""Gate facts stay topic-only through the real repair overlay and every writer builder."""

import json
from types import SimpleNamespace

import pytest

from forwin.api_schema.review import ChapterReviewIssueInfo
from forwin.book_state.extraction.translator import _DeltaBuilder
from forwin.book_state.extraction.world_review import ChapterWorldReview
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.reviewer import BookStateReviewIssue, BookStateReviewVerdict
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.generation.pipeline_core.repair_patches import _current_chapter_repair_experience_plan
from forwin.naming import EntityAdmissionPlan
from forwin.planning.beat_sheet.service import ChapterMilestoneRefs
from forwin.protocol.book_state import FactNode, MapNode, WorldNode
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.review import world_consistency
from forwin.review.draft_service import DraftReviewService
from forwin.world_view.types import ChapterWorldView
from tests.chapter_delta_helpers import chapter_delta, world_review_output
from tests.test_chapter_world_view import _build, _plan, source  # noqa: F401 (source is a fixture)
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
    # Only the database-backed milestone lookup and translation/gate boundary are substituted; topic
    # construction, reviewer rendering, repair instructions, overlays and writer builders all run normally.
    monkeypatch.setattr(world_consistency, "chapter_milestone_refs", lambda *args: ChapterMilestoneRefs())
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


@pytest.mark.parametrize("builder", BUILDERS)
def test_review_api_preserves_audit_without_exposing_it_to_writer(monkeypatch, builder):
    issue = _review(monkeypatch, "premature_reveal")
    stored_issue = json.loads(json.dumps(issue.model_dump()))
    response = ChapterReviewIssueInfo.model_validate(stored_issue).model_dump(mode="json")

    assert response["original_result"] == stored_issue["original_result"]
    assert response["original_result"]["message"] == MESSAGES["premature_reveal"]
    assert response["original_result"]["ref"] == "fact:secret"
    assert response["original_result"]["fact_propositions"] == {"fact:secret": TRUTH}
    context = _context(ChapterWorldView(pov_ref="pov", pov_name="POV"))
    instruction = DraftReviewService._continuity_repair_instruction(continuity_issues=[issue], context=context)
    overlay = _current_chapter_repair_experience_plan(ChapterExperiencePlan(), instruction)
    text = _prompt(builder, context.model_copy(update={"chapter_experience_plan": overlay}))
    assert "涉及Visible subject、Visible place的未揭示真相" in text
    for writer_text in (response["description"], response["suggested_fix"], *instruction.must_fix, text):
        assert TRUTH not in writer_text
        assert "fact:secret" not in writer_text
        assert MESSAGES["premature_reveal"] not in writer_text


def test_review_api_accepts_legacy_issue_without_audit():
    response = ChapterReviewIssueInfo.model_validate({
        "rule_name": "legacy", "severity": "warning", "description": "Legacy issue",
    }).model_dump(mode="json")
    assert response["original_result"] == {}


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("repaired", [False, True], ids=["ordinary", "repaired"])
@pytest.mark.parametrize("related_refs", [[], ["hidden-state", "hidden-profile"]], ids=["empty", "concealed-only"])
def test_hinted_unnameable_fact_stays_neutral_in_writer_prompts(source, builder, repaired, related_refs):
    fact_id = "AUTHOR_ONLY_NAMELESS_FACT_ID_31c8"
    fact_ref = f"fact:{fact_id}"
    hidden_names = ["AUTHOR_ONLY_STATE_SUBJECT_31c8", "AUTHOR_ONLY_PROFILE_SUBJECT_31c8"]
    source.runtime.world.add_node(WorldNode(
        id="hidden-state", project_id="p", node_type="character", name=hidden_names[0],
        state={"visibility_level": "secret"},
    ))
    source.runtime.world.add_node(WorldNode(
        id="hidden-profile", project_id="p", node_type="character", name=hidden_names[1],
        profile={"visibility_level": "hidden"},
    ))
    source.runtime.world.add_fact(FactNode(
        id=fact_id, project_id="p", proposition=TRUTH, sensitivity_level="secret",
        related_node_refs=related_refs,
    ))
    hint = {"id": "d-neutral", "fact_ref": fact_ref, "hint_text": "A harmless clue"}
    view = _build(source, plan=_plan(
        onstage_refs_json=json.dumps(["hero", "ally"]), reveal_directives_json="[]",
        hint_directives_json=json.dumps([hint]),
    ))
    assert view.allowed_reveals == []
    assert [item.directive_id for item in view.hints] == ["d-neutral"]
    context = _context(view)
    if repaired:
        issue = world_consistency.WorldConsistencyReviewer()._issue(
            world_consistency._Finding(
                "error", "premature_reveal", f"the reader advances on {fact_ref}", fact_ref, "BookStateReviewGate",
            ),
            {fact_ref: TRUTH}, {fact_ref: []},
        )
        assert issue.original_result["fact_propositions"] == {fact_ref: TRUTH}
        instruction = DraftReviewService._continuity_repair_instruction(continuity_issues=[issue], context=context)
        assert fact_ref in instruction.evidence_refs
        overlay = _current_chapter_repair_experience_plan(ChapterExperiencePlan(), instruction)
        context = context.model_copy(update={"chapter_experience_plan": overlay})
    text = _prompt(builder, context)

    for hidden in (fact_id, TRUTH, *hidden_names):
        assert hidden not in text
    assert "A harmless clue" in text
    assert "未揭示真相" in text
    if repaired:
        assert "repair must fix:" in text
    assert next(topic.topic for topic in view.forbidden_topics if topic.fact_ref == fact_ref) == "未揭示真相"


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
