from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_plan_revision,
)
from forwin.book_state import BookStateRepository, BookStateReviewGate
from forwin.canon import CanonAdmissionOutcome, CanonPreparationOutcome
from forwin.canon.preparation import BookStateCanonPreparer, CanonPreparationContext
from forwin.config import InfrastructureConfig
from forwin.models import DecisionEvent, Project
from forwin.models.base import Base, get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    BookStateCompileResult,
    GraphDelta,
    NodePatch,
    WorldNode,
)
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.naming import EntityAdmissionPlan, EntityRegistrar
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.chapter_delta_helpers import chapter_delta
from tests.postgres import postgres_test_url


def _build_pipeline(database_url: str, artifact_root: str):
    policy = RuntimePolicy.for_profile("standard")
    policy = policy.model_copy(
        update={"canon": policy.canon.model_copy(update={"quality_gate": "pulp_fatal"})}
    )
    return RuntimeContainer.from_config(
        InfrastructureConfig(
            database_url=database_url,
            artifact_root=artifact_root,
            qdrant_url=":memory:",
            embedding_backend="hash",
            minimax_api_key="",
            minimax_model="fake-model",
        ),
        policy=policy,
        role="generation_worker",
    ).build_chapter_pipeline()


def _chapter_output(project_id: str, **overrides) -> WriterOutput:
    """WriterOutput whose chapter delta reports the repaired comms console."""
    body = overrides.pop("body", "防线修复后，通讯台传出乱码和父亲旧部呼号。")
    delta = overrides.pop("delta", {
        "entity_changes": [{
            "new_entity": {"name": "通讯台", "kind": "item"},
            "field": "status",
            "new_value": "修复完成",
            "evidence": "防线修复后，通讯台传出乱码",
        }],
    })
    fields = {
        "project_id": project_id,
        "chapter_number": 1,
        "title": "乱码呼号",
        "body": body,
        "end_of_chapter_summary": "收到异常通讯。",
        "chapter_delta": chapter_delta(body, **delta),
    }
    fields.update(overrides)
    return WriterOutput(**fields)


def _setup_project(session):
    updater = StateUpdater(session)
    project = updater.create_project(
        title="BookState gate",
        premise="殖民地防线与异常通讯",
        genre="科幻",
        runtime_policy=RuntimePolicy.for_profile("standard"),
    )
    arc = updater.create_arc_plan(
        project.id, "母星通讯危机", chapter_start=1, chapter_end=8
    )
    chapter = updater.create_chapter_plan(
        project_id=project.id,
        arc_plan_id=arc.id,
        chapter_number=1,
        title="乱码呼号",
        one_line="修复防线并收到异常通讯",
        goals=["修复防线", "处理乱码通讯"],
    )
    return project, chapter


def _persist_candidate(session, project, chapter, output, verdict):
    planned = (
        EntityRegistrar(session=session)
        .plan_writer_output(
            project_id=project.id,
            chapter_number=chapter.chapter_number,
            writer_output=output,
        )
        .writer_output
    )
    draft = ChapterDraft(
        chapter_plan_id=chapter.id,
        version=1,
        body_text=planned.body,
        summary=planned.end_of_chapter_summary,
        char_count=planned.char_count,
    )
    session.add(draft)
    session.flush()
    review = ChapterReview(
        draft_id=draft.id,
        verdict=verdict.verdict,
        issues_json="[]",
        review_meta_json=verdict.model_dump_json(),
    )
    session.add(review)
    session.flush()
    candidate = CandidateDraftRepository(session).create_reviewed_version(
        project_id=project.id,
        chapter_plan=chapter,
        draft=draft,
        review=review,
        writer_output=planned,
        plan_revision=candidate_plan_revision(chapter),
        policy_version=1,
    )
    return planned, candidate


def _prepare_candidate(pipeline, session, project, chapter, output, verdict):
    repo, updater, _checker = pipeline._make_state_helpers(session)  # noqa: SLF001
    planned, candidate = _persist_candidate(
        session,
        project,
        chapter,
        output,
        verdict,
    )
    return pipeline.canon_preparation.prepare(
        context=pipeline.canon_preparation_context,
        session=session,
        repo=repo,
        updater=updater,
        candidate_id=candidate.id,
        project_id=project.id,
        chapter_number=chapter.chapter_number,
        writer_output=planned,
        verdict=verdict,
        acceptance_mode="normal",
        repair_attempt_count=0,
        residual_review_issues=[],
        canon_risk_level="low",
    )


def test_canon_admission_commits_book_state_without_projection_compatibility_event() -> (
    None
):
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("pipeline-bookstate-no-projection-compat")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        pipeline = _build_pipeline(db_path, str(Path(tmp) / "artifacts"))
        with Session() as session:
            project, chapter = _setup_project(session)
            preparation = _prepare_candidate(
                pipeline,
                session,
                project,
                chapter,
                _chapter_output(project.id),
                ReviewVerdict(verdict="pass", issues=[]),
            )
            assert preparation.plan is not None
            session.commit()
            result = pipeline.canon_admission.commit_plan(preparation.plan)

        with Session() as session:
            graph_deltas = session.scalar(
                select(func.count()).select_from(GraphDeltaRow)
            )
            events = session.execute(select(DecisionEvent)).scalars().all()

        event_types = {event.event_type for event in events}
        payloads = "\n".join(event.payload_json or "" for event in events)
        assert isinstance(result, CanonAdmissionOutcome)
        assert not result.blocked
        assert graph_deltas > 0
        assert "legacy_projection_failed" not in event_types
        assert "projection.legacy_world_model_projection" not in payloads


def test_canon_admission_blocks_rule_rewrite_before_book_state_commit() -> None:
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("pipeline-bookstate-review-block")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        pipeline = _build_pipeline(db_path, str(Path(tmp) / "artifacts"))
        with Session() as session:
            project, chapter = _setup_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="rule-transit-protocol",
                    project_id=project.id,
                    node_type="rule",
                    name="通行协议",
                    profile={"public_version": "三印同亮，门右移一格。"},
                    created_at_chapter=0,
                ),
                version_chapter=0,
            )
            outcome = _prepare_candidate(
                pipeline,
                session,
                project,
                chapter,
                _chapter_output(
                    project.id,
                    title="改写规则",
                    body="众人误称通行协议会让门右移两格。",
                    end_of_chapter_summary="正文改写了既有规则。",
                    delta={"entity_changes": [{
                        "ref": "rule-transit-protocol",
                        "field": "public_version",
                        "new_value": "三印同亮，门右移两格。",
                        "evidence": "通行协议会让门右移两格",
                    }]},
                ),
                ReviewVerdict(verdict="pass", issues=[]),
            )
            session.commit()

        with Session() as session:
            graph_deltas = session.scalar(
                select(func.count()).select_from(GraphDeltaRow)
            )

        assert isinstance(outcome, CanonPreparationOutcome)
        assert outcome.blocked
        assert outcome.block_kind == "book_state"
        assert graph_deltas == 0


def test_book_state_compile_failure_rolls_back_graph_deltas(monkeypatch) -> None:
    def fail_compile(self, approved_changes, *, compiler_run_id: str = ""):
        return BookStateCompileResult(
            project_id=approved_changes.project_id,
            chapter_number=approved_changes.chapter_number,
            compiler_run_id=compiler_run_id,
            committed=False,
            blocked_reasons=["forced test failure"],
        )

    monkeypatch.setattr(
        "forwin.canon.admission.BookStateCompiler.compile", fail_compile
    )
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("pipeline-bookstate-rollback")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        pipeline = _build_pipeline(db_path, str(Path(tmp) / "artifacts"))
        with Session() as session:
            project, chapter = _setup_project(session)
            preparation = _prepare_candidate(
                pipeline,
                session,
                project,
                chapter,
                _chapter_output(project.id),
                ReviewVerdict(verdict="pass", issues=[]),
            )
            assert preparation.plan is not None
            session.commit()
            result = pipeline.canon_admission.commit_plan(preparation.plan)

        with Session() as session:
            graph_deltas = session.scalar(
                select(func.count()).select_from(GraphDeltaRow)
            )

        assert isinstance(result, CanonAdmissionOutcome)
        assert result.blocked
        assert result.block_kind == "canon_write_failed"
        assert graph_deltas == 0


def test_accept_review_respects_canon_gate_block(monkeypatch) -> None:
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("accept-review-block")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        pipeline = _build_pipeline(db_path, str(Path(tmp) / "artifacts"))
        with Session.begin() as session:
            updater = StateUpdater(session)
            project = updater.create_project(
                title="Accept",
                premise="p",
                genre="g",
                runtime_policy=RuntimePolicy.for_profile("standard"),
            )
            arc = updater.create_arc_plan(
                project.id, "arc", chapter_start=1, chapter_end=1
            )
            chapter = updater.create_chapter_plan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=1,
                title="一",
                one_line="一",
                goals=["一"],
            )
            _persist_candidate(
                session,
                project,
                chapter,
                WriterOutput(
                    project_id=project.id,
                    chapter_number=1,
                    title="一",
                    body="正文",
                    end_of_chapter_summary="总结",
                ),
                ReviewVerdict(verdict="pass", issues=[]),
            )
            chapter.status = "needs_review"
            session.add(chapter)

        monkeypatch.setattr(
            pipeline,
            "_load_writer_output_from_meta",
            lambda _meta: WriterOutput(
                project_id=project.id,
                chapter_number=1,
                title="一",
                body="正文",
                end_of_chapter_summary="总结",
            ),
        )
        monkeypatch.setattr(
            pipeline,
            "_load_review_verdict",
            lambda _review: ReviewVerdict(verdict="pass", issues=[]),
        )
        monkeypatch.setattr(
            pipeline.canon_preparation,
            "prepare",
            lambda **_kwargs: CanonPreparationOutcome(
                blocked_path="book-state-review-gate-blocked",
                block_kind="book_state",
            ),
        )
        monkeypatch.setattr(
            pipeline,
            "_run_phase3_pass",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("phase3 should not run")
            ),
        )
        result = pipeline.accept_review(project.id, 1)

        with Session() as session:
            status = session.scalar(
                select(ChapterPlan.status).where(ChapterPlan.id == chapter.id)
            )

        assert "needs_review" in result["message"]
        assert status == "needs_review"


def test_accept_review_rejects_planned_retry_candidate() -> None:
    with TemporaryDirectory() as tmp:
        db_path = postgres_test_url("accept-review-planned-retry")
        engine = get_engine(db_path)
        init_db(engine)
        Session = get_session_factory(engine)
        pipeline = _build_pipeline(db_path, str(Path(tmp) / "artifacts"))
        with Session.begin() as session:
            project, chapter = _setup_project(session)
            _persist_candidate(
                session,
                project,
                chapter,
                WriterOutput(
                    project_id=project.id,
                    chapter_number=1,
                    title="一",
                    body="等待整章重写的旧正文",
                    end_of_chapter_summary="旧候选仍在审计链中。",
                ),
                ReviewVerdict(verdict="fail", issues=[]),
            )

        with pytest.raises(ValueError, match="planned"):
            pipeline.accept_review(project.id, 1)


def _sqlite_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _transit_rule(project_id: str) -> WorldNode:
    return WorldNode(
        id="rule-transit-protocol",
        project_id=project_id,
        node_type="rule",
        name="通行协议",
        profile={"public_version": "三印同亮，门右移一格。"},
        created_at_chapter=5,
    )


@pytest.mark.parametrize("source_type", ["writer_output", "chapter_extraction"])
def test_review_gate_blocks_writer_rewrite_of_canonical_rule_definition(source_type: str) -> None:
    engine, session = _sqlite_session()
    try:
        project = Project(title="规则门禁", premise="测试", genre="科幻")
        session.add(project)
        session.flush()
        BookStateRepository(session).create_world_node(_transit_rule(project.id), version_chapter=5)

        verdict = BookStateReviewGate(session).review(
            ApprovedGraphDeltaSet(
                project_id=project.id,
                chapter_number=6,
                graph_deltas=[
                    GraphDelta(
                        id="delta-rule-rewrite",
                        project_id=project.id,
                        chapter_number=6,
                        source_type=source_type,
                        node_patches=[
                            NodePatch(
                                node_id="rule-transit-protocol",
                                node_type="rule",
                                op="set",
                                field_path="profile.public_version",
                                old_value="三印同亮，门右移一格。",
                                new_value="三印同亮，门右移两格。",
                            )
                        ],
                    )
                ],
            )
        )

        assert verdict.accepted is False
        issue = next(
            item
            for item in verdict.issues
            if item.code == "immutable_rule_definition_conflict"
        )
        assert issue.target_ref == "node:rule-transit-protocol:profile.public_version"
    finally:
        session.close()
        engine.dispose()


def test_book_state_preparer_records_review_block_reason() -> None:
    engine, session = _sqlite_session()
    recorded_events: list[dict[str, object]] = []

    def record_decision_event(**payload):
        recorded_events.append(payload)
        return SimpleNamespace(id=f"event-{len(recorded_events)}")

    try:
        project = Project(title="规则审计", premise="测试", genre="科幻")
        session.add(project)
        session.flush()
        BookStateRepository(session).create_world_node(_transit_rule(project.id), version_chapter=5)
        body = "众人误称门会右移两格。"
        output = WriterOutput(
            project_id=project.id,
            chapter_number=6,
            title="第六章",
            body=body,
            end_of_chapter_summary="正文改写了既有规则。",
            chapter_delta=chapter_delta(body, entity_changes=[{
                "ref": "rule-transit-protocol",
                "field": "public_version",
                "new_value": "三印同亮，门右移两格。",
                "evidence": "门会右移两格",
            }]),
        )

        outcome = BookStateCanonPreparer().prepare(
            context=CanonPreparationContext(
                policy=RuntimePolicy.for_profile("standard"),
                llm_client=object(),  # type: ignore[arg-type]
                artifact_store=object(),  # type: ignore[arg-type]
                _record_decision_event=record_decision_event,  # type: ignore[arg-type]
                _record_rule_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
            ),
            session=session,
            candidate_id="candidate-review-6",
            project_id=project.id,
            chapter_number=6,
            writer_output=output,
            verdict=ReviewVerdict(verdict="pass"),
            entity_admission_plan=EntityAdmissionPlan(
                project_id=project.id, chapter_number=6, candidate_fingerprint="",
            ),
        )

        assert outcome.blocked is True
        assert outcome.blocked_path == "book-state-review-gate-blocked"
        assert recorded_events[-1]["event_type"] == DecisionEventType.CANON_COMMIT_BLOCKED
        payload = recorded_events[-1]["payload"]
        assert isinstance(payload, dict)
        assert payload["issues"][0]["code"] == "immutable_rule_definition_conflict"
        gate_outcome = parse_gate_outcome(payload)
        assert gate_outcome is not None
        assert gate_outcome.gate_id == "book_state_review"
        assert gate_outcome.candidate_id == "candidate-review-6"
        assert gate_outcome.decision == "block"
        assert gate_outcome.blocked is True
        assert gate_outcome.issue_keys == ["immutable_rule_definition_conflict"]
    finally:
        session.close()
        engine.dispose()
