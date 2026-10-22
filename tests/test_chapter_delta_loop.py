"""The real chapter loop where extraction, draft review, repair, canon and after-acceptance meet.

Each test takes the xianxia fixture book through the real start-writing handoff and runs
``continue_project`` for chapter one: the real draft review with the world_consistency
signal, the repair loop and its residual policy, the hard floor, canon preparation and the
after-acceptance obligation controls. Only the LLM is stubbed: the writer returns scripted
drafts, and ``chapter_delta_extraction`` answers each draft by its body hash.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.book_state.book_frame_repository import StoryCalendarRepository
from forwin.config import InfrastructureConfig
from forwin.models import DecisionEvent
from forwin.models.book_state import GraphDeltaRow
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.protocol.writer import WriterOutput
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import ChapterLengthPolicy, RuntimePolicy
from tests.chapter_delta_helpers import body_sha256
from tests.postgres import postgres_test_url
from tests.test_chapter_delta_multi_genre import _Book, _shortest_minutes, _start_book


GENRE = "xianxia"
# The fixture chapter is short; the loop's hard floor reads the policy's minimum length.
CHAPTER_LENGTH = ChapterLengthPolicy(min_chars=500, target_chars=600, max_chars=900)
TAIL = (
    "\n\n雨停时，崖下的松林里起了雾。祁长烬靠着残碑坐了很久，听见山风从断剑之间穿过，像是许多人压着嗓子说话。"
    "他把剑穗按在胸口，慢慢平复呼吸，等指尖不再发抖，才把碑文又读了一遍，一个字一个字记在心里。"
    "\n\n天边泛白的时候，他起身拍掉衣上的泥水，回头望向来时的石阶。山门的方向被云遮住了，什么也看不清。"
    "他忽然觉得，自己这些年守着的规矩，也许从一开始就是别人替他立下的；而那个替他立规矩的人，此刻大概还在山门里等他回去。"
    "\n\n他没有立刻下山。崖顶的风越来越冷，他却在碑前多站了一炷香，像是要把母亲留下的每一道刻痕都看进骨头里。"
)
# Each draft ends differently, so every rewrite has its own body hash.
ENDINGS = (
    "直到远处传来第一声晨钟，他才转身，沿着另一条更陡的小径往下走。今夜之后，他要自己去找答案。",
    "直到晨钟响过三遍，他才把红绳系紧，从崖背的小路下山。他决定先不回山门，去母亲守过的剑冢看一看。",
    "直到雾气散尽，他才收拾行囊，绕开来时的石阶下山。他要先查清母亲为何离开断剑崖。",
    "直到天色大亮，他才动身，一路踩着碎石下到崖底。剑穗贴在胸口，像一块不肯冷下去的炭。",
)
_BODY_HASH = re.compile(r"body_sha256: ([0-9a-f]{64})")


@pytest.fixture
def make_pipeline(tmp_path):
    pipelines = []

    def make(profile: str = "standard"):
        policy = RuntimePolicy.for_profile(profile)
        # The chapter review form needs a live LLM; only extraction is stubbed here.
        signals = tuple(signal for signal in policy.review.signals if signal != "canon_quality")
        policy = policy.model_copy(update={
            "review": policy.review.model_copy(update={"signals": signals}),
            "canon": policy.canon.model_copy(update={"quality_gate": "pulp_fatal"}),
            "chapter_length": CHAPTER_LENGTH,
        })
        pipeline = RuntimeContainer.from_config(
            InfrastructureConfig(
                database_url=postgres_test_url("chapter_delta_loop"),
                artifact_root=str(tmp_path / f"artifacts-{len(pipelines)}"),
                qdrant_url=":memory:",
                embedding_backend="hash",
                minimax_api_key="",
            ),
            policy=policy,
            role="generation_worker",
        ).build_chapter_pipeline()
        pipelines.append(pipeline)
        return pipeline

    try:
        yield make
    finally:
        for pipeline in pipelines:
            pipeline.close()


class _Extraction:
    """Stub LLM: answers chapter_delta_extraction per body hash; a body with no answer fails."""

    model = "stub-model"

    def __init__(self, answers: dict[str, dict[str, Any]] | None = None) -> None:
        self.answers = dict(answers or {})
        self.extracted: list[str] = []

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        stage_key = str(kwargs.get("stage_key") or "")
        if stage_key != "chapter_delta_extraction":
            raise AssertionError(f"unexpected LLM stage {stage_key!r}")
        body_hash = _BODY_HASH.search(messages[-1]["content"]).group(1)
        self.extracted.append(body_hash)
        if body_hash not in self.answers:
            raise ConnectionError("extraction provider unavailable")
        return json.dumps(self.answers[body_hash], ensure_ascii=False)

    def close(self) -> None:
        pass


class _Writer:
    """Stub writer: the first draft, then one rewrite per remaining draft (the last repeats)."""

    def __init__(self, book: _Book, bodies: list[str]) -> None:
        self.book = book
        self.bodies = bodies
        self.stage_keys: list[str] = []

    def __call__(self, context, *, trace_stage_key: str = "", **_kwargs: Any) -> WriterOutput:  # noqa: ANN001
        body = self.bodies[min(len(self.stage_keys), len(self.bodies) - 1)]
        self.stage_keys.append(trace_stage_key)
        return WriterOutput(
            project_id=self.book.project_id, chapter_number=context.chapter_number,
            title=self.book.chapter["plan"]["title"], body=body, char_count=len(body),
            end_of_chapter_summary=self.book.chapter["summary"],
        )


def _draft(book: _Book, index: int) -> str:
    return book.chapter["body"] + TAIL + ENDINGS[index]


def _answer(book: _Book, body: str, *, plan_fulfillment: list[dict[str, Any]] | None = None,
            **fields: Any) -> tuple[str, dict[str, Any]]:
    """The fixture extraction for ``body``, keyed by its hash."""
    extraction = {**book.extraction(), **fields}
    if plan_fulfillment is not None:
        extraction["plan_fulfillment"] = plan_fulfillment
    return body_sha256(body), extraction


def _goal(status: str, book: _Book) -> dict[str, Any]:
    (done,) = book.extraction()["plan_fulfillment"]
    return {**done, "status": status} if status == "done" else {"target_id": "goal:1", "status": status}


def _run(pipeline, book: _Book, writer: _Writer, client: _Extraction):
    pipeline.writer.write_chapter = writer
    pipeline.llm_client = client
    return pipeline.continue_project(book.project_id, max_chapters=1)


def _chapter_status(pipeline, book: _Book) -> str:
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        return session.scalar(select(ChapterPlan.status).where(
            ChapterPlan.project_id == book.project_id, ChapterPlan.chapter_number == 1,
        ))


def _events(pipeline, book: _Book, event_type: str) -> list[DecisionEvent]:
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        return list(session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == book.project_id, DecisionEvent.event_type == event_type,
        ).order_by(DecisionEvent.created_at)))


def _review_codes(pipeline, book: _Book) -> list[list[str]]:
    """Error codes of every chapter-one review, oldest draft first."""
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        reviews = session.scalars(
            select(ChapterReview).join(ChapterDraft, ChapterReview.draft_id == ChapterDraft.id)
            .join(ChapterPlan, ChapterDraft.chapter_plan_id == ChapterPlan.id)
            .where(ChapterPlan.project_id == book.project_id, ChapterPlan.chapter_number == 1)
            .order_by(ChapterDraft.version, ChapterReview.created_at)
        ).all()
        return [
            [issue["rule_name"] for issue in json.loads(review.issues_json or "[]") if issue.get("severity") == "error"]
            for review in reviews
        ]


def _committed_source(pipeline, book: _Book) -> list[str]:
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        return list(session.scalars(select(GraphDeltaRow.source_id).where(
            GraphDeltaRow.project_id == book.project_id, GraphDeltaRow.chapter_number == 1,
        )))


def _unquoted(extraction: dict[str, Any]) -> dict[str, Any]:
    """``extraction`` with every evidence quote replaced by text the body does not contain."""
    return {
        key: [{**item, "evidence": "invented quote"} for item in value] if isinstance(value, list) else value
        for key, value in extraction.items()
    }


@pytest.mark.parametrize("outage,blocking_code", [
    ("provider_down", "chapter_delta_missing"),
    ("every_quote_invented", "chapter_delta_unverified"),
])
def test_extraction_outage_ends_in_manual_review_not_failure(make_pipeline, outage: str, blocking_code: str) -> None:
    pipeline = make_pipeline()
    book = _start_book(pipeline, GENRE)
    drafts = [_draft(book, index) for index in range(len(ENDINGS))]
    writer = _Writer(book, drafts)
    client = _Extraction()
    if outage == "every_quote_invented":
        client.answers.update(
            (body_hash, _unquoted(extraction))
            for body_hash, extraction in (_answer(book, body) for body in drafts)
        )

    result = _run(pipeline, book, writer, client)

    assert (result.failed_chapters, result.paused_chapters) == ([], [1])
    assert _chapter_status(pipeline, book) == "needs_review"
    # Every draft was extracted, and each review blocked on the unusable extraction.
    assert len(writer.stage_keys) >= 2 and writer.stage_keys[1:] == ["chapter_rewrite"] * (len(writer.stage_keys) - 1)
    assert len(set(client.extracted)) == len(writer.stage_keys)
    assert all(blocking_code in codes for codes in _review_codes(pipeline, book))
    # The hard floor reports the extraction as unavailable instead of failing the chapter.
    assert _events(pipeline, book, DecisionEventType.HARD_GATE_HIT) == []
    (evaluation,) = _events(pipeline, book, DecisionEventType.PULP_BEAT_EVALUATED)
    payload = json.loads(evaluation.payload_json)
    assert "chapter_delta_unavailable" in payload["warning_reasons"]
    assert payload["gate_outcome"]["blocked"] is False
    assert _committed_source(pipeline, book) == []


@pytest.mark.parametrize("profile", ["standard", "pulp"])
def test_missed_goal_is_rewritten_and_the_delivering_rewrite_commits(make_pipeline, profile: str) -> None:
    pipeline = make_pipeline(profile)
    book = _start_book(pipeline, GENRE)
    missed, delivered = _draft(book, 0), _draft(book, 1)
    writer = _Writer(book, [missed, delivered])
    client = _Extraction(dict([
        _answer(book, missed, plan_fulfillment=[_goal("missing", book)]),
        _answer(book, delivered),
    ]))

    result = _run(pipeline, book, writer, client)

    assert result.completed_chapters == [1], result
    assert writer.stage_keys == ["chapter_draft", "chapter_rewrite"]
    first, rewrite = _review_codes(pipeline, book)
    assert "plan_goal_missing" in first and "plan_goal_missing" not in rewrite
    assert _chapter_status(pipeline, book) == "accepted"
    assert _committed_source(pipeline, book) == [f"chapter:1:{body_sha256(delivered)}"]


@pytest.mark.parametrize("profile,rewrites", [("standard", 3), ("pulp", 1)])
def test_persistent_goal_miss_ends_in_manual_review_after_its_rewrites(
    make_pipeline, profile: str, rewrites: int,
) -> None:
    pipeline = make_pipeline(profile)
    book = _start_book(pipeline, GENRE)
    drafts = [_draft(book, index) for index in range(len(ENDINGS))]
    writer = _Writer(book, drafts)
    client = _Extraction(dict(_answer(book, body, plan_fulfillment=[_goal("missing", book)]) for body in drafts))

    result = _run(pipeline, book, writer, client)

    assert (result.failed_chapters, result.paused_chapters) == ([], [1])
    assert writer.stage_keys == ["chapter_draft", *["chapter_rewrite"] * rewrites]
    assert all("plan_goal_missing" in codes for codes in _review_codes(pipeline, book))
    assert _chapter_status(pipeline, book) == "needs_review"
    assert _committed_source(pipeline, book) == []


def _too_fast(pipeline, session, book: _Book) -> dict[str, Any]:
    """The fixture movement, claimed in under half the time the world gate allows."""
    (movement,) = book.extraction()["movements"]
    shortest = _shortest_minutes(session, book.project_id, movement)
    unit = min(StoryCalendarRepository(session).get(book.project_id).units, key=lambda item: item.minutes)
    claimed_minutes = math.floor(shortest * (1 - pipeline.policy.review.travel_tolerance) / 2)
    assert claimed_minutes > 0
    return {
        **movement,
        "depart_offset": {"amount": 0, "unit": unit.name},
        "arrive_offset": {"amount": claimed_minutes / unit.minutes, "unit": unit.name},
    }


def test_world_error_is_rewritten_reextracted_committed_and_resolves_its_obligation(make_pipeline) -> None:
    pipeline = make_pipeline()
    book = _start_book(pipeline, GENRE)
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        obligations = NarrativeObligationRepository(session)
        # An open obligation carries the applied plan patch that deferred it.
        patch = obligations.create_plan_patch(NarrativePlanPatch(
            project_id=book.project_id, affected_chapters=[2], source_obligation_ids=["obl-sword-tomb"],
            validation_status="passed", applied=True,
        ))
        # Due next chapter, so the pre-write plan audit does not demand it now; chapter one
        # can still resolve it early.
        obligation = obligations.create_obligation(NarrativeObligation(
            id="obl-sword-tomb", project_id=book.project_id, origin_chapter_number=0,
            obligation_type="reader_promise", priority="P1", status="active",
            summary="祁长烬决定去母亲守过的剑冢。", hardness="design_debt", deadline_chapter=2,
            payoff_test="第2章前祁长烬决定去剑冢。", linked_plan_patch_ids=[patch.id],
        ))
        session.commit()
        too_fast = _too_fast(pipeline, session, book)
    first, corrected = _draft(book, 0), _draft(book, 1)
    target = f"obligation:{obligation.id}"
    goal = _goal("done", book)
    client = _Extraction(dict([
        _answer(book, first, movements=[too_fast], plan_fulfillment=[goal, {"target_id": target, "status": "missing"}]),
        _answer(book, corrected, plan_fulfillment=[
            goal, {"target_id": target, "status": "done", "evidence": "他决定先不回山门，去母亲守过的剑冢看一看"},
        ]),
    ]))
    writer = _Writer(book, [first, corrected])

    result = _run(pipeline, book, writer, client)

    assert result.completed_chapters == [1], result
    # The world error went to a draft rewrite, whose new body got its own extraction.
    assert writer.stage_keys == ["chapter_draft", "chapter_rewrite"]
    assert client.extracted == [body_sha256(first), body_sha256(corrected)]
    first_errors, rewrite_errors = _review_codes(pipeline, book)
    assert "movement_too_fast" in first_errors
    assert rewrite_errors == []
    assert _committed_source(pipeline, book) == [f"chapter:1:{body_sha256(corrected)}"]
    # After acceptance the committed delta's fulfillment resolved the obligation ahead of its deadline.
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        stored = session.get(NarrativeObligationRow, obligation.id)
        assert (stored.status, stored.resolution_chapter) == ("resolved", 1)
