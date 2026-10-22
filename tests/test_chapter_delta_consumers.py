from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.candidate_drafts import CandidateDraftRepository
from forwin.checker.hard_floor import run_hard_floor
from forwin.checker.pulp_beat import delivered_payoffs_from_fulfillment, verify_pulp_beats
from forwin.checker.rules import ContinuityChecker
from forwin.models.base import Base
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.protocol.context import ChapterContextPack, EntitySnapshot, PlotThreadSnapshot, ReviewContextPack
from forwin.protocol.writer import WriterOutput
from forwin.review.webnovel import WebNovelExperienceReviewer
from forwin.runtime.policy import ChapterLengthPolicy, RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.writer.chapter_writer import ChapterWriter
from tests.chapter_delta_helpers import chapter_delta


_BODY = "甲乙二人在旧站台碰面，乙把账本交给甲，随后两人一起走向码头。" * 4
_LEGACY_FIELDS = (
    "state_changes",
    "new_events",
    "delivered_payoffs",
    "thread_beats",
    "time_advance",
    "lore_candidates",
    "timeline_hints",
    "writer_notes",
    "entity_mentions",
    "scene_continuation",
)


def _output(body: str = _BODY, **delta: Any) -> WriterOutput:
    return WriterOutput(
        project_id="p1",
        chapter_number=5,
        title="第五章",
        body=body,
        char_count=len(body),
        end_of_chapter_summary="两人走向码头。",
        chapter_delta=chapter_delta(body, **delta),
    )


def _event(*refs: str, flashback: bool = False) -> dict[str, Any]:
    return {
        "summary": "乙把账本交给甲",
        "participant_refs": list(refs),
        "significance": "minor",
        "visibility": "public",
        "evidence": "乙把账本交给甲",
        "flashback": flashback,
    }


class _FakeBookState:
    """BookState as of a chapter: a character is dead from the chapter of its death on."""

    def __init__(
        self,
        entities: list[EntitySnapshot],
        *,
        deaths: dict[str, int] | None = None,
        threads: list[PlotThreadSnapshot] | None = None,
    ) -> None:
        self.entities = entities
        self.deaths = deaths or {}
        self.threads = threads or []

    def active_entities(self, project_id, *, as_of_chapter, kinds=None):  # noqa: ANN001, ANN201
        return [
            entity.model_copy(update={"current_state": {
                **entity.current_state,
                "alive_status": "dead" if self.deaths.get(entity.entity_id, 10**9) <= as_of_chapter else "alive",
            }})
            for entity in self.entities
            if kinds is None or entity.kind in kinds
        ]

    def canon_name_anchors(self, project_id, *, as_of_chapter):  # noqa: ANN001, ANN201
        return []

    def active_threads(self, project_id, *, as_of_chapter):  # noqa: ANN001, ANN201
        return list(self.threads)


def _checker(**book_state: Any) -> ContinuityChecker:
    return ContinuityChecker(
        SimpleNamespace(),
        min_chars=10,
        max_chars=1000,
        book_state_query=_FakeBookState([
            EntitySnapshot(
                entity_id="char-a", kind="character", name="甲", description="",
                current_state={"status": "已死"},
            ),
            EntitySnapshot(
                entity_id="char-b", kind="character", name="乙", aliases=["老乙"], description="",
                current_state={"status": "在场"},
            ),
        ], **book_state),
    )


def _issues(output: WriterOutput, rule_name: str, **book_state: Any) -> list:
    verdict = _checker(**book_state).check("p1", output)
    return [issue for issue in verdict.issues if issue.rule_name == rule_name]


def _dead_issues(output: WriterOutput) -> list:
    # 乙 died in chapter 4; the chapter under review is chapter 5.
    return _issues(output, "dead_character_active", deaths={"char-b": 4})


def test_dead_character_check_reads_alive_status_not_status_text() -> None:
    # "已死" in the free-text status of a living character is not death.
    assert _dead_issues(_output(events=[_event("char-a")])) == []

    issues = _dead_issues(_output(events=[_event("char-b")]))
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].entity_names == ["乙"]

    # A recalled event does not make the dead act now; a movement always does.
    assert _dead_issues(_output(events=[_event("char-b", flashback=True)])) == []
    moved = _dead_issues(_output(movements=[{
        "entity_ref": "char-b", "from_ref": "site-1", "to_ref": "site-2", "evidence": "两人一起走向码头",
    }]))
    assert [issue.severity for issue in moved] == ["error"]
    # A ref naming the dead character by an alias matches too.
    assert len(_dead_issues(_output(events=[_event("new:老乙")]))) == 1
    # No extraction degrades to no extracted facts.
    assert _dead_issues(_output().model_copy(update={"chapter_delta": None})) == []


def test_dead_character_check_counts_deaths_as_of_the_previous_chapter() -> None:
    output = _output(events=[_event("char-b")])

    # A character who dies in this chapter (5) is not dead as of chapter 4.
    assert _issues(output, "dead_character_active", deaths={"char-b": 5}) == []
    assert len(_issues(output, "dead_character_active", deaths={"char-b": 4})) == 1


def test_thread_beat_on_a_closed_thread_warns() -> None:
    threads = [
        PlotThreadSnapshot(thread_id="thread-old", name="旧案", description="", status="resolved", priority=5),
        PlotThreadSnapshot(thread_id="thread-ledger", name="账本", description="", status="active", priority=5),
    ]
    output = _output(thread_beats=[
        {"thread_ref": "plot_thread:thread-old", "beat_type": "advance", "evidence": "乙把账本交给甲"},
        {"new_thread_title": "旧案", "beat_type": "advance", "evidence": "乙把账本交给甲"},
        {"thread_ref": "thread-ledger", "beat_type": "advance", "evidence": "乙把账本交给甲"},
    ])

    issues = _issues(output, "thread_already_closed", threads=threads)

    assert [(issue.severity, issue.description.count("旧案")) for issue in issues] == [
        ("warning", 1), ("warning", 1),
    ]


def test_entity_change_and_event_structure_warnings() -> None:
    output = _output(
        entity_changes=[
            {"ref": " ", "field": "mood", "new_value": "警觉", "evidence": "乙把账本交给甲"},
            {"ref": "char-a", "field": " ", "new_value": "警觉", "evidence": "乙把账本交给甲"},
        ],
        events=[{**_event("char-a"), "summary": " "}],
    )
    verdict = _checker().check("p1", output)

    assert {
        (issue.rule_name, issue.severity)
        for issue in verdict.issues
        if issue.rule_name in {"empty_entity_name", "empty_field_name", "event_summary_empty"}
    } == {
        ("empty_entity_name", "warning"), ("empty_field_name", "warning"), ("event_summary_empty", "warning"),
    }
    clean = _checker().check("p1", _output(
        entity_changes=[{"ref": "char-a", "field": "mood", "new_value": "警觉", "evidence": "乙把账本交给甲"}],
        events=[_event("char-a")],
    ))
    assert not {"empty_entity_name", "empty_field_name", "event_summary_empty"} & {
        issue.rule_name for issue in clean.issues
    }


def _policy() -> RuntimePolicy:
    return RuntimePolicy.for_profile("standard").model_copy(update={
        "chapter_length": ChapterLengthPolicy.model_construct(min_chars=20, target_chars=40, max_chars=400),
    })


def _context() -> ChapterContextPack:
    return ChapterContextPack(
        project_id="p1", project_title="测试", premise="前提", genre="悬疑", setting_summary="设定",
        chapter_number=5, chapter_plan_title="第五章", chapter_plan_one_line="碰面", chapter_goals=["交接账本"],
    )


def _hard_floor(output: WriterOutput):
    return run_hard_floor(
        writer_output=output, context_pack=_context(), repo=None,
        project_id="p1", chapter_number=5, policy=_policy(),
    )


def test_hard_floor_counts_events_from_chapter_delta() -> None:
    assert _hard_floor(_output(events=[_event("char-a")])).checks["at_least_one_event"] is True
    # Every fact-bearing list counts, e.g. a chapter whose only fact is a movement.
    movement_only = _output(movements=[{
        "entity_ref": "char-a", "from_ref": "site-1", "to_ref": "site-2", "evidence": "两人一起走向码头",
    }])
    assert _hard_floor(movement_only).checks["at_least_one_event"] is True
    assert _hard_floor(_output()).checks["at_least_one_event"] is False


def _dropped(path: str) -> dict[str, Any]:
    return {"severity": "warning", "code": "evidence_not_in_body", "path": path, "message": "m"}


def test_hard_floor_does_not_fail_a_chapter_it_cannot_assess() -> None:
    missing = _output().model_copy(update={"chapter_delta": None})
    every_fact_dropped = _output(entity_mentions=[{"name": "甲", "kind": "character", "evidence": "甲乙二人"}])
    every_fact_dropped = every_fact_dropped.model_copy(update={"generation_meta": {
        "chapter_delta_issues": [_dropped("events[0]"), _dropped("movements[1]")],
    }})

    for output in (missing, every_fact_dropped):
        result = _hard_floor(output)

        assert result.passed, result.fail_reasons
        assert result.checks["chapter_delta_available"] is False
        assert "at_least_one_event" not in result.checks
        assert "chapter_delta_unavailable" in result.warning_reasons


def test_hard_floor_still_fails_an_extraction_without_events() -> None:
    # Dropped mentions or fulfillment entries are not facts; the extraction found no events.
    output = _output().model_copy(update={"generation_meta": {
        "chapter_delta_issues": [_dropped("entity_mentions[0]"), _dropped("plan_fulfillment[0]")],
    }})
    kept_event = _output(events=[_event("char-a")]).model_copy(update={"generation_meta": {
        "chapter_delta_issues": [_dropped("events[1]")],
    }})

    result = _hard_floor(output)

    assert result.checks["chapter_delta_available"] is True
    assert result.checks["at_least_one_event"] is False
    assert "at_least_one_event" in result.fail_reasons
    assert _hard_floor(kept_event).checks["at_least_one_event"] is True


def test_pulp_payoff_from_plan_fulfillment_reward_targets() -> None:
    body = "系统提示：甲的临时视野已解锁，他立刻看见了账本上的暗记。"
    delta = chapter_delta(body, plan_fulfillment=[
        {"target_id": "reward:power", "status": "done", "evidence": "甲的临时视野已解锁"},
        {"target_id": "reward:social", "status": "partial", "evidence": "他立刻看见了账本上的暗记"},
        {"target_id": "goal:1", "status": "done", "evidence": "他立刻看见了账本上的暗记"},
        {"target_id": "reward:mystery", "status": "done", "evidence": ""},
    ])

    payoffs = delivered_payoffs_from_fulfillment(delta)

    assert [(payoff.target_id, payoff.category) for payoff in payoffs] == [("reward:power", "power")]
    assert delivered_payoffs_from_fulfillment(None) == ()
    result = verify_pulp_beats(body, track=None, delivered_payoffs=payoffs)
    assert result.visible_payoff_present is True
    assert result.visible_payoff_evidence == ["delivered_payoff:reward:power"]
    # A quote the body does not contain is no evidence.
    elsewhere = verify_pulp_beats("甲什么也没看见。", track=None, delivered_payoffs=payoffs)
    assert elsewhere.visible_payoff_present is False


def test_payoff_evidence_quote_is_four_to_240_characters() -> None:
    body = "甲" + "乙看见了账本上的暗记。" * 30

    def payoffs(evidence: str) -> list[str]:
        delta = chapter_delta(body, plan_fulfillment=[
            {"target_id": "reward:mystery", "status": "done", "evidence": evidence},
        ])
        return [payoff.target_id for payoff in delivered_payoffs_from_fulfillment(delta)]

    assert payoffs("甲") == []
    assert payoffs("乙看 见了") == ["reward:mystery"]
    assert payoffs(body[1:241]) == ["reward:mystery"]
    assert payoffs(body[1:242]) == []


def test_candidate_columns_filled_from_chapter_delta() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="候选", premise="前提", genre="悬疑", runtime_policy=RuntimePolicy.for_profile("standard"),
        )
        arc = updater.create_arc_plan(project.id, "主线")
        plan = updater.create_chapter_plan(
            project_id=project.id, arc_plan_id=arc.id, chapter_number=5,
            title="第五章", one_line="碰面", goals=["交接账本"],
        )
        output = _output(
            entity_changes=[{"ref": "char-a", "field": "mood", "new_value": "警觉", "evidence": "乙把账本交给甲"}],
            events=[_event("char-a", "char-b")],
            thread_beats=[{"new_thread_title": "账本", "beat_type": "advance", "evidence": "乙把账本交给甲"}],
        ).model_copy(update={"project_id": project.id})
        draft = ChapterDraft(chapter_plan_id=plan.id, version=1, body_text=output.body, char_count=output.char_count)
        session.add(draft)
        session.flush()
        review = ChapterReview(draft_id=draft.id, verdict="pass", issues_json="[]", review_meta_json="{}")
        session.add(review)
        session.flush()

        record = CandidateDraftRepository(session).create_reviewed_version(
            project_id=project.id, chapter_plan=plan, draft=draft, review=review,
            writer_output=output, plan_revision="r1", policy_version=1,
        )

        delta = output.chapter_delta
        assert json.loads(record.state_change_candidates_json) == [
            item.model_dump(mode="json") for item in delta.entity_changes
        ]
        assert json.loads(record.event_candidates_json) == [item.model_dump(mode="json") for item in delta.events]
        assert json.loads(record.thread_beat_candidates_json) == [
            item.model_dump(mode="json") for item in delta.thread_beats
        ]
        assert json.loads(record.event_candidates_json)[0]["participant_refs"] == ["char-a", "char-b"]
    finally:
        session.close()
        engine.dispose()


def test_writer_no_longer_runs_structured_extraction() -> None:
    class StageRecordingClient:
        def __init__(self) -> None:
            self.stage_keys: list[str] = []

        def chat(self, _messages, *, stage_key: str = "", **_kwargs) -> str:  # noqa: ANN001
            self.stage_keys.append(stage_key)
            if stage_key == "scene_breakdown":
                return '{"scenes":[{"scene_no":1,"objective":"碰面","must_progress_points":["碰面"],"target_chars":300}]}'
            return (
                "<<FORWIN_TITLE>>\n旧站碰面\n<<FORWIN_BODY>>\n"
                + "甲乙二人在旧站台碰面，乙把账本交给甲。" * 20
                + "\n<<FORWIN_SUMMARY>>\n账本交接完成。"
            )

    client = StageRecordingClient()
    writer = ChapterWriter(client, writer_mode="scene", min_chapter_chars=200)

    output = writer.write_chapter(_context())

    assert client.stage_keys == ["scene_breakdown", "scene_generation", "scene_stitch"]
    assert output.chapter_delta is None
    assert output.generation_meta["mode"] == "scene"
    assert output.generation_meta["call_count"] == 3
    assert not any("structured_extraction" in key for key in output.generation_meta)


def test_webnovel_reviewer_counts_every_fact_list_as_progress() -> None:
    reviewer = WebNovelExperienceReviewer(llm_enabled=False)
    movement_only = _output(movements=[{
        "entity_ref": "char-a", "from_ref": "site-1", "to_ref": "site-2", "evidence": "两人一起走向码头",
    }])
    context = ReviewContextPack(
        project_id="p1", project_title="测试", chapter_number=5,
        chapter_plan_title="第五章", chapter_plan_one_line="碰面",
    )

    score, refs = reviewer._progress_score(context, movement_only)

    assert reviewer._is_assessable(movement_only) is True
    assert score > 0
    assert "movement:char-a:site-1->site-2" in refs
    assert reviewer._is_assessable(_output()) is False


def test_writer_output_has_no_legacy_structured_fields() -> None:
    assert not set(_LEGACY_FIELDS) & set(WriterOutput.model_fields)
    assert "chapter_delta" in WriterOutput.model_fields
