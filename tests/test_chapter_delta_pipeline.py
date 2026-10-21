from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select

from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.book_state import BookStateRepository
from forwin.book_state.book_frame_repository import StoryCalendarRepository
from forwin.book_state.extraction.chapter_delta_extractor import ensure_chapter_delta
from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.candidate_drafts import CandidateDraftRepository, candidate_plan_revision
from forwin.checker.rules import ContinuityChecker
from forwin.config import InfrastructureConfig
from forwin.models import DecisionEvent
from forwin.models.base import Base
from forwin.models.book_state import GraphDeltaPatchRow, GraphDeltaRow
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.genesis import PromptTrace
from forwin.models.project import ChapterPlan, Project
from forwin.naming import EntityRegistrar
from forwin.protocol.book_state import WorldNode
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


OPENING = 600
BODY = "林夜收下陈七递来的铜令，两日后才赶到渡口。"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload(**fields: Any) -> dict[str, Any]:
    return {"body_sha256": "0" * 64, "time": {"elapsed": None, "evidence": ""}, **fields}


class ExtractionClient:
    """Stub LLM: answers chapter_delta_extraction with ``payload`` and admission with register_character."""

    def __init__(self, payload: dict[str, Any] | str | None = None) -> None:
        self.payload = _payload() if payload is None else payload
        self.stage_keys: list[str] = []
        self.model = "stub-model"

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        stage_key = str(kwargs.get("stage_key") or "")
        self.stage_keys.append(stage_key)
        if stage_key == "chapter_delta_extraction":
            return self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        names = json.loads(messages[-1]["content"])["unknown_names"]
        return json.dumps({"decisions": [
            {"name": name, "decision": "register_character", "canonical_name": name, "aliases": [], "role_hint": "role"}
            for name in names
        ]}, ensure_ascii=False)

    def extraction_calls(self) -> int:
        return self.stage_keys.count("chapter_delta_extraction")

    def close(self) -> None:
        pass


class AdmitAll:
    def classify(self, *, names, **_kwargs):  # noqa: ANN001, ANN003
        return [{"name": name, "decision": "register_character", "canonical_name": name, "role_hint": "role"} for name in names]


@pytest.fixture
def runtime(tmp_path):
    policy = RuntimePolicy.for_profile("standard")
    policy = policy.model_copy(update={"canon": policy.canon.model_copy(update={"quality_gate": "pulp_fatal"})})
    pipeline = RuntimeContainer.from_config(
        InfrastructureConfig(
            database_url=postgres_test_url("chapter_delta_pipeline"),
            artifact_root=str(tmp_path / "artifacts"),
            qdrant_url=":memory:",
            embedding_backend="hash",
            minimax_api_key="",
        ),
        policy=policy,
        role="generation_worker",
    ).build_chapter_pipeline()
    try:
        with pipeline._SessionFactory() as session:  # noqa: SLF001
            updater = StateUpdater(session)
            project = updater.create_project(
                title="chapter delta pipeline", premise="p", genre="g", runtime_policy=pipeline.policy,
            )
            StoryCalendarRepository(session).save(
                project.id, StoryCalendar("", OPENING, (CalendarUnit("日", 1440), CalendarUnit("时", 60))),
            )
            arc = updater.create_arc_plan(project.id, "arc", chapter_start=1, chapter_end=4)
            chapter = updater.create_chapter_plan(
                project_id=project.id, arc_plan_id=arc.id, chapter_number=1, title="一", one_line="一", goals=["一"],
            )
            session.commit()
            yield pipeline, session, chapter
    finally:
        pipeline.close()


def _output(chapter: ChapterPlan, body: str = BODY, **fields: Any) -> WriterOutput:
    return WriterOutput(
        project_id=chapter.project_id, chapter_number=chapter.chapter_number, title="一",
        body=body, end_of_chapter_summary="林夜得到铜令。", **fields,
    )


def _review_candidate(pipeline, session, chapter: ChapterPlan, writer_output: WriterOutput, review_output):
    execution = replace(
        pipeline.repair_execution,
        _review_current_output=review_output,
        _review_with_repair_verification=lambda *, review, **_kwargs: review,
    )
    repo = StateRepository(session)
    return pipeline.repair.review_candidate(
        execution=execution,
        session=session,
        repo=repo,
        updater=StateUpdater(session),
        checker=ContinuityChecker(repo),
        project_id=chapter.project_id,
        chapter_plan=chapter,
        context=pipeline.retrieval_broker.build_chapter_context(repo, chapter.project_id, chapter),
        writer_output=writer_output,
    )


def _persist_candidate(session, chapter: ChapterPlan, output: WriterOutput) -> tuple[WriterOutput, str]:
    planned = EntityRegistrar(session=session, classifier=AdmitAll()).plan_writer_output(
        project_id=chapter.project_id, chapter_number=chapter.chapter_number, writer_output=output,
    ).writer_output
    draft = ChapterDraft(
        chapter_plan_id=chapter.id, version=1, body_text=planned.body,
        summary=planned.end_of_chapter_summary, char_count=len(planned.body),
    )
    session.add(draft)
    session.flush()
    verdict = ReviewVerdict(verdict="pass", issues=[])
    review = ChapterReview(draft_id=draft.id, verdict="pass", issues_json="[]", review_meta_json=verdict.model_dump_json())
    session.add(review)
    session.flush()
    candidate = CandidateDraftRepository(session).create_reviewed_version(
        project_id=chapter.project_id, chapter_plan=chapter, draft=draft, review=review,
        writer_output=planned, plan_revision=candidate_plan_revision(chapter), policy_version=1,
    )
    return planned, candidate.id


def _prepare(pipeline, session, chapter: ChapterPlan, planned: WriterOutput, candidate_id: str, *, llm_client):
    repo, updater, _checker = pipeline._make_state_helpers(session)  # noqa: SLF001
    return pipeline.canon_preparation.prepare(
        context=replace(pipeline.canon_preparation_context, llm_client=llm_client),
        session=session, repo=repo, updater=updater, candidate_id=candidate_id,
        project_id=chapter.project_id, chapter_number=chapter.chapter_number, writer_output=planned,
        verdict=ReviewVerdict(verdict="pass", issues=[]), acceptance_mode="normal",
        repair_attempt_count=0, residual_review_issues=[], canon_risk_level="low",
    )


def _blocked_payloads(session, project_id: str) -> list[dict[str, Any]]:
    events = session.scalars(
        select(DecisionEvent).where(
            DecisionEvent.project_id == project_id, DecisionEvent.event_type == "canon_commit_blocked",
        )
    ).all()
    return [json.loads(event.payload_json or "{}") for event in events]


def _chapter_payload(body: str = BODY) -> dict[str, Any]:
    return _payload(
        time={"elapsed": {"amount": 2, "unit": "日"}, "evidence": "两日后才赶到渡口"},
        events=[{
            "summary": "陈七交出铜令", "participant_refs": ["new:陈七"], "significance": "minor",
            "visibility": "public", "evidence": "林夜收下陈七递来的铜令",
        }],
        entity_mentions=[{"name": "陈七", "kind": "character", "evidence": "陈七递来的铜令"}],
    )


def test_every_candidate_gets_an_extraction_before_review(runtime) -> None:
    pipeline, session, chapter = runtime
    client = ExtractionClient()
    pipeline.llm_client = client
    reviewed: list[WriterOutput] = []

    def review_output(*, writer_output, **_kwargs):  # noqa: ANN001, ANN003
        reviewed.append(writer_output)
        return ReviewVerdict(verdict="pass", issues=[])

    output, review, _forced = _review_candidate(pipeline, session, chapter, _output(chapter), review_output)

    assert review.verdict == "pass"
    assert client.extraction_calls() == 1
    assert len(reviewed) == 1
    assert reviewed[0].chapter_delta is not None
    assert reviewed[0].chapter_delta.body_sha256 == _sha(BODY)
    assert output.chapter_delta == reviewed[0].chapter_delta
    assert output.generation_meta["chapter_delta_issues"] == []
    trace = session.scalar(select(PromptTrace).where(PromptTrace.stage_key == "chapter_delta_extraction"))
    assert trace is not None
    assert trace.trace_scope == "writer"


def test_local_rewrite_triggers_reextraction(runtime) -> None:
    pipeline, session, chapter = runtime
    client = ExtractionClient()
    pipeline.llm_client = client
    reviewed: list[WriterOutput] = []
    duplicated = "林夜收下铜令。\n林夜收下铜令。\n两日后才赶到渡口。"

    def review_output(*, writer_output, **_kwargs):  # noqa: ANN001, ANN003
        reviewed.append(writer_output)
        if len(reviewed) > 1:
            return ReviewVerdict(verdict="pass", issues=[])
        return ReviewVerdict(verdict="fail", issues=[ContinuityIssue(
            rule_name="body_duplicate_span", issue_type="body_duplicate_span", severity="error",
            description="正文重复段落。", evidence_refs=["draft:body"], blocking=True,
        )])

    output, review, _forced = _review_candidate(
        pipeline, session, chapter, _output(chapter, body=duplicated), review_output,
    )

    assert review.verdict == "pass"
    assert len(reviewed) == 2
    assert reviewed[1].body != reviewed[0].body
    assert client.extraction_calls() == 2
    assert reviewed[0].chapter_delta.body_sha256 == _sha(duplicated)
    assert reviewed[1].chapter_delta.body_sha256 == _sha(reviewed[1].body)
    assert output.chapter_delta.body_sha256 == _sha(output.body)


def test_unchanged_body_reuses_extraction(runtime) -> None:
    pipeline, session, chapter = runtime
    client = ExtractionClient()
    pipeline.llm_client = client

    first = pipeline._plan_writer_output_entities(  # noqa: SLF001
        session=session, project_id=chapter.project_id, chapter_number=1, writer_output=_output(chapter),
    )
    second = pipeline._plan_writer_output_entities(  # noqa: SLF001
        session=session, project_id=chapter.project_id, chapter_number=1, writer_output=first,
    )
    reused, result = ensure_chapter_delta(
        session, llm_client=client, project_id=chapter.project_id, chapter_number=1, writer_output=second,
    )

    assert client.extraction_calls() == 1
    assert second.chapter_delta == first.chapter_delta
    assert result is None
    assert reused is second


def test_admission_reads_new_refs_from_chapter_delta() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        project = Project(title="admission", premise="p", genre="g")
        session.add(project)
        session.flush()
        output = WriterOutput(
            project_id=project.id, chapter_number=1, title="一", body=BODY, end_of_chapter_summary="s",
            chapter_delta=ChapterDeltaExtraction.model_validate(_payload(
                body_sha256=_sha(BODY),
                events=[{
                    "summary": "陈七交出铜令", "participant_refs": ["new:陈七"], "significance": "minor",
                    "visibility": "public", "evidence": "林夜收下陈七递来的铜令",
                }],
            )),
        )
        classifier = AdmitAll()

        result = EntityRegistrar(session=session, classifier=classifier).plan_writer_output(
            project_id=project.id, chapter_number=1, writer_output=output,
        )

        assert output.chapter_delta.entity_mentions == []
        assert [(item.mention_name, item.action) for item in result.plan.decisions] == [("陈七", "register_character")]
        assert result.registered_names == ["陈七"]
    finally:
        session.close()
        engine.dispose()


def test_canon_commits_translated_delta(runtime) -> None:
    pipeline, session, chapter = runtime
    output = _output(chapter, chapter_delta=ChapterDeltaExtraction.model_validate(
        {**_chapter_payload(), "body_sha256": _sha(BODY)}
    ))
    planned, candidate_id = _persist_candidate(session, chapter, output)
    client = ExtractionClient()

    preparation = _prepare(pipeline, session, chapter, planned, candidate_id, llm_client=client)
    assert preparation.plan is not None, preparation.blocked_path
    session.commit()
    outcome = pipeline.canon_admission.commit_plan(preparation.plan)

    assert not outcome.blocked, outcome.blocked_path
    assert client.extraction_calls() == 0
    session.expire_all()
    rows = session.scalars(select(GraphDeltaRow).where(GraphDeltaRow.project_id == chapter.project_id)).all()
    assert [(row.source_type, row.story_time_start, row.story_time_end) for row in rows] == [
        ("chapter_extraction", OPENING, OPENING + 2 * 1440),
    ]
    assert rows[0].source_id == f"chapter:1:{_sha(BODY)}"
    names = {node.name for node in BookStateRepository(session).list_world_nodes(chapter.project_id, as_of_chapter=1)}
    assert "陈七" in names


def _commit(pipeline, session, chapter: ChapterPlan, output: WriterOutput):
    planned, candidate_id = _persist_candidate(session, chapter, output)
    preparation = _prepare(pipeline, session, chapter, planned, candidate_id, llm_client=ExtractionClient())
    assert preparation.plan is not None, preparation.blocked_path
    session.commit()
    outcome = pipeline.canon_admission.commit_plan(preparation.plan)
    assert not outcome.blocked, outcome.blocked_path
    session.expire_all()
    return planned, session.scalars(select(GraphDeltaRow).where(GraphDeltaRow.project_id == chapter.project_id)).all()


def test_off_stage_new_characters_in_relations_and_knowledge_are_admitted_and_committed(runtime) -> None:
    pipeline, session, chapter = runtime
    hero = f"char-lin-{chapter.project_id}"
    BookStateRepository(session).create_world_node(
        WorldNode(id=hero, project_id=chapter.project_id, node_type="character", name="林夜", state={"status": "active"}),
        version_chapter=0,
    )
    session.commit()
    body = "林夜读完王五的信，才知道赵六是他失散多年的兄长。"
    output = _output(chapter, body=body, chapter_delta=ChapterDeltaExtraction.model_validate(_payload(
        body_sha256=_sha(body),
        relation_changes=[{
            "source_ref": hero, "target_ref": "new:赵六", "edge_type": "family_of", "op": "create",
            "evidence": "赵六是他失散多年的兄长",
        }],
        knowledge_events=[{
            "observer_ref": hero, "new_fact": {"proposition": "赵六是林夜的兄长", "truth_value": True, "sensitivity": "public"},
            "state": "knows", "channel": "document", "source_ref": "new:王五", "evidence": "林夜读完王五的信",
        }],
    )))

    planned, rows = _commit(pipeline, session, chapter, output)

    plan = planned.generation_meta["entity_admission_plan"]
    assert sorted((item["mention_name"], item["action"]) for item in plan["decisions"]) == [
        ("王五", "register_character"), ("赵六", "register_character"),
    ]
    assert [row.source_type for row in rows] == ["chapter_extraction"]
    names = {node.name for node in BookStateRepository(session).list_world_nodes(chapter.project_id, as_of_chapter=1)}
    assert {"王五", "赵六"} <= names


def test_valid_empty_extraction_commits_patchless_delta(runtime) -> None:
    pipeline, session, chapter = runtime
    output = _output(chapter, chapter_delta=ChapterDeltaExtraction.model_validate(_payload(body_sha256=_sha(BODY))))

    _planned, rows = _commit(pipeline, session, chapter, output)

    assert [(row.source_type, row.story_time_start, row.story_time_end) for row in rows] == [
        ("chapter_extraction", OPENING, OPENING),
    ]
    patches = session.scalars(select(GraphDeltaPatchRow).where(GraphDeltaPatchRow.delta_id == rows[0].id)).all()
    assert patches == []


def test_canon_records_translation_warnings_on_the_delta(runtime) -> None:
    pipeline, session, chapter = runtime
    output = _output(chapter, chapter_delta=ChapterDeltaExtraction.model_validate(_payload(
        body_sha256=_sha(BODY), time={"elapsed": {"amount": 1, "unit": "年"}, "evidence": "两日后才赶到渡口"},
    )))

    _planned, rows = _commit(pipeline, session, chapter, output)

    warnings = json.loads(rows[0].metadata_json)["translation_warnings"]
    assert [item["code"] for item in warnings] == ["elapsed_unknown"]
    assert set(warnings[0]) == {"code", "message"}


def test_extraction_failure_blocks_canon_without_regex_fallback(runtime) -> None:
    pipeline, session, chapter = runtime
    body = "林夜收下玄铁令，决定进入问心阁。"
    planned, candidate_id = _persist_candidate(session, chapter, _output(chapter, body=body))
    client = ExtractionClient(payload="not json")

    preparation = _prepare(pipeline, session, chapter, planned, candidate_id, llm_client=client)

    assert preparation.blocked
    assert preparation.block_kind == "book_state"
    assert preparation.blocked_path == "chapter-delta-extraction-blocked"
    assert client.extraction_calls() == 3
    payload = _blocked_payloads(session, chapter.project_id)[-1]
    assert payload["blocked_path"] == "chapter-delta-extraction-blocked"
    assert "extraction_failed" in [issue["code"] for issue in payload["issues"]]
    gate = parse_gate_outcome(payload)
    assert gate is not None and gate.gate_id == "chapter_delta_extraction" and gate.blocked
    assert session.scalars(select(GraphDeltaRow)).all() == []
    assert BookStateRepository(session).list_world_nodes(chapter.project_id, as_of_chapter=1) == []


def test_translation_error_blocks_canon(runtime) -> None:
    pipeline, session, chapter = runtime
    output = _output(chapter, chapter_delta=ChapterDeltaExtraction.model_validate(_payload(
        body_sha256=_sha(BODY),
        movements=[{"entity_ref": "unknown-node", "from_ref": "", "to_ref": "nowhere", "evidence": "赶到渡口"}],
    )))
    planned, candidate_id = _persist_candidate(session, chapter, output)

    preparation = _prepare(pipeline, session, chapter, planned, candidate_id, llm_client=ExtractionClient())

    assert preparation.blocked
    assert preparation.blocked_path == "chapter-delta-translation-blocked"
    payload = _blocked_payloads(session, chapter.project_id)[-1]
    assert {issue["code"] for issue in payload["issues"]} == {"unresolved_ref"}
    gate = parse_gate_outcome(payload)
    assert gate is not None and gate.gate_id == "chapter_delta_translation"


@pytest.mark.parametrize("with_llm", [True, False])
def test_human_accept_with_stale_extraction_reextracts_or_blocks(runtime, monkeypatch, with_llm: bool) -> None:
    pipeline, session, chapter = runtime
    stale = ChapterDeltaExtraction.model_validate({**_chapter_payload(), "body_sha256": _sha("an earlier body")})
    planned, candidate_id = _persist_candidate(session, chapter, _output(chapter, chapter_delta=stale))
    chapter.status = "needs_review"
    session.add(chapter)
    session.commit()
    client = ExtractionClient(_chapter_payload())
    pipeline.canon_preparation_context = replace(
        pipeline.canon_preparation_context, llm_client=client if with_llm else None,
    )
    monkeypatch.setattr(pipeline, "_load_writer_output_from_meta", lambda _meta: planned)
    monkeypatch.setattr(pipeline, "_run_phase3_pass", lambda **_kwargs: {})
    monkeypatch.setattr(pipeline, "_run_post_canon_order_controls", lambda **_kwargs: None)

    result = pipeline.accept_review(chapter.project_id, 1)

    session.expire_all()
    rows = session.scalars(select(GraphDeltaRow).where(GraphDeltaRow.project_id == chapter.project_id)).all()
    status = session.scalar(select(ChapterPlan.status).where(ChapterPlan.id == chapter.id))
    if with_llm:
        assert result["status"] in {"accepted", "maintenance_pending"}, result
        assert client.extraction_calls() == 1
        assert [row.source_id for row in rows] == [f"chapter:1:{_sha(BODY)}"]
        assert status == "accepted"
    else:
        assert result["status"] == "needs_review"
        assert result["frozen_artifact"] == "chapter-delta-extraction-blocked"
        assert rows == []
        assert status == "needs_review"
        payload = _blocked_payloads(session, chapter.project_id)[-1]
        assert payload["blocked_path"] == "chapter-delta-extraction-blocked"
        assert [issue["code"] for issue in payload["issues"]] == ["extraction_unavailable"]
