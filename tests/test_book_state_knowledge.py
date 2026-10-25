from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from forwin.book_state import BookStateCompiler, BookStateProjection, BookStateRepository
from forwin.book_state.cognition import CognitionView
from forwin.book_state.knowledge import (
    HIDDEN_SENSITIVITY,
    KNOWLEDGE_CHANNELS,
    KNOWLEDGE_STATES,
    KnowledgeEvent,
    knowledge_event_to_patch,
    knowledge_evidence_ref,
)
from forwin.book_state.schema import validate_graph_delta
from forwin.models.base import Base, get_engine, get_session_factory, init_db
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionOverlay,
    CognitionPatch,
    FactNode,
    GraphDelta,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


def _view(**overlay_fields) -> CognitionView:
    return CognitionView(
        CognitionOverlay(
            id="cog",
            project_id="p1",
            observer_type="character",
            observer_id="c1",
            **overlay_fields,
        )
    )


def _fact(fact_id: str, *, truth_value: str = "true", sensitivity_level: str = "") -> FactNode:
    return FactNode(
        id=fact_id,
        project_id="p1",
        proposition=f"proposition {fact_id}",
        truth_value=truth_value,
        sensitivity_level=sensitivity_level,
    )


def _create_project(session) -> str:
    return StateUpdater(session).create_project(
        title="Knowledge events",
        premise="cognition writes",
        genre="test",
        setting_summary="setting",
        runtime_policy=RuntimePolicy.for_profile("standard"),
    ).id


def _delta(project_id: str, chapter: int, delta_id: str, patches: list[CognitionPatch]) -> GraphDelta:
    return GraphDelta(
        id=delta_id,
        project_id=project_id,
        chapter_number=chapter,
        cognition_patches=patches,
    )


def test_knowledge_event_to_patch_shape() -> None:
    event = KnowledgeEvent("character", "c1", "fact:f1", "believes", "told", "c2", 600)

    patch = knowledge_event_to_patch(event, knowledge_by_ref={})

    assert patch.observer_type == "character"
    assert patch.observer_id == "c1"
    assert str(patch.op) == "merge"
    assert patch.field_path == "knowledge_by_ref"
    assert patch.old_value == {"fact:f1": None}
    assert patch.new_value == {"fact:f1": "believes"}
    assert patch.evidence_refs == ["knowledge:told:c2:600"]
    assert knowledge_evidence_ref(event) == "knowledge:told:c2:600"


def test_knowledge_event_reads_only_touched_ref_from_observer_snapshot() -> None:
    known = {"fact:f1": "heard", "fact:f2": "knows"}
    patch = knowledge_event_to_patch(
        KnowledgeEvent("character", "c1", "fact:f1", "believes", "told"),
        knowledge_by_ref=known,
    )
    assert patch.old_value == {"fact:f1": "heard"}
    assert patch.new_value == {"fact:f1": "believes"}
    assert known == {"fact:f1": "heard", "fact:f2": "knows"}
    known["fact:f1"] = "knows"
    assert patch.old_value == {"fact:f1": "heard"}


def test_knowledge_evidence_ref_marks_missing_source_and_time() -> None:
    event = KnowledgeEvent("reader", "reader", "fact:f1", "knows", "document")

    assert knowledge_evidence_ref(event) == "knowledge:document:-:-"
    assert knowledge_evidence_ref(
        KnowledgeEvent("reader", "reader", "fact:f1", "knows", "document", at=0)
    ) == "knowledge:document:-:0"


def test_knowledge_vocabularies() -> None:
    assert KNOWLEDGE_STATES == ("heard", "suspects", "believes", "knows", "disbelieves", "misled")
    assert KNOWLEDGE_CHANNELS == ("witnessed", "told", "rumor", "document", "inferred", "seeded")
    assert HIDDEN_SENSITIVITY == frozenset({"hidden", "secret", "must_not_reveal"})


@pytest.mark.parametrize(
    "event",
    [
        KnowledgeEvent("character", "c1", "fact:f1", "maybe", "told"),
        KnowledgeEvent("character", "c1", "fact:f1", "believes", "telepathy"),
        KnowledgeEvent("character", "c1", "f1", "believes", "told"),
        KnowledgeEvent("alien", "c1", "fact:f1", "believes", "told"),
    ],
    ids=["state", "channel", "fact_ref", "observer_type"],
)
def test_knowledge_event_rejects_unknown_state_channel_and_ref(event: KnowledgeEvent) -> None:
    with pytest.raises(ValueError):
        knowledge_event_to_patch(event, knowledge_by_ref={})


def test_knowledge_state_defaults_follow_sensitivity() -> None:
    view = _view(knowledge_by_ref={"fact:misled": "misled"})

    assert view.knowledge_state(_fact("open")) == "knows"
    assert view.knowledge_state(_fact("vault", sensitivity_level="secret")) == "unknown"
    assert view.knowledge_state(_fact("withheld", sensitivity_level="must_not_reveal")) == "unknown"
    assert view.knowledge_state(_fact("misled", sensitivity_level="secret")) == "misled"


def test_false_beliefs_detects_believed_false_fact_and_overrides() -> None:
    facts = {
        fact.id: fact
        for fact in (
            _fact("lie", truth_value="false"),
            _fact("rumor", truth_value="false"),
            _fact("denied", truth_value="false"),
            _fact("unrecorded", truth_value="false"),
            _fact("truth"),
        )
    }
    view = _view(
        knowledge_by_ref={
            "fact:lie": "believes",
            "fact:rumor": "knows",
            "fact:denied": "disbelieves",
            "fact:truth": "believes",
            "fact:missing": "believes",
        },
        field_overrides={"field:node-1:state.location_id": "map-2"},
    )

    assert view.false_beliefs(facts) == [
        "fact:lie",
        "fact:rumor",
        "field:node-1:state.location_id",
    ]
    assert _view().false_beliefs(facts) == []


def test_apply_cognition_patch_merges_and_replaces_knowledge() -> None:
    view = _view(knowledge_by_ref={"fact:a": "heard"})

    view.apply_cognition_patch("knowledge_by_ref", "merge", {"fact:b": "knows"})
    assert view.knowledge_by_ref == {"fact:a": "heard", "fact:b": "knows"}

    view.apply_cognition_patch("knowledge_by_ref", "merge", {"fact:a": "believes"})
    assert view.knowledge_by_ref == {"fact:a": "believes", "fact:b": "knows"}

    view.apply_cognition_patch("knowledge_by_ref", "replace", {"fact:c": "suspects"})
    assert view.knowledge_by_ref == {"fact:c": "suspects"}

    view.apply_cognition_patch("knowledge_by_ref", "set", {"fact:d": "misled"})
    assert view.knowledge_by_ref == {"fact:d": "misled"}
    assert view.diff_against_objective()["knowledge_by_ref"] == {"fact:d": "misled"}


def test_knowledge_events_compile_and_replay() -> None:
    engine = get_engine(postgres_test_url())
    init_db(engine)
    Session = get_session_factory(engine)
    events = [
        KnowledgeEvent("reader", "reader", "fact:f1", "knows", "document", "chapter:0", 0),
        KnowledgeEvent("character", "c1", "fact:f1", "believes", "told", "c2", 600),
        KnowledgeEvent("character", "c1", "fact:f2", "heard", "rumor", "", 600),
    ]

    with Session.begin() as session:
        project_id = _create_project(session)
        result = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=0,
                graph_deltas=[
                    _delta(
                        project_id,
                        0,
                        "delta-knowledge-0",
                        [knowledge_event_to_patch(event, knowledge_by_ref={}) for event in events],
                    )
                ],
            )
        )
        assert result.committed, result.blocked_reasons
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
        overlays = {
            (str(overlay.observer_type), overlay.observer_id): overlay
            for overlay in BookStateRepository(session).list_cognition_overlays(
                project_id, as_of_chapter=0
            )
        }

    reader = runtime.cognition_by_observer[("reader", "reader")]
    character = runtime.cognition_by_observer[("character", "c1")]
    assert reader.knowledge_by_ref == {"fact:f1": "knows"}
    assert reader.evidence_by_ref == {"fact:f1": ["knowledge:document:chapter:0:0"]}
    assert character.knowledge_by_ref == {"fact:f1": "believes", "fact:f2": "heard"}
    assert character.evidence_by_ref == {
        "fact:f1": ["knowledge:told:c2:600"],
        "fact:f2": ["knowledge:rumor:-:600"],
    }
    assert overlays[("character", "c1")].knowledge_by_ref == character.knowledge_by_ref
    assert overlays[("reader", "reader")].knowledge_by_ref == {"fact:f1": "knows"}


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_knowledge_updates_accumulate_across_chapters_without_double_evidence(backend: str) -> None:
    engine = create_engine("sqlite://") if backend == "sqlite" else get_engine(postgres_test_url())
    if backend == "sqlite":
        Base.metadata.create_all(engine)
    else:
        init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project_id = _create_project(session)
        for chapter, event in (
            (0, KnowledgeEvent("character", "c1", "fact:f1", "heard", "rumor", "", 10)),
            (1, KnowledgeEvent("character", "c1", "fact:f1", "believes", "told", "c2", 700)),
        ):
            runtime = BookStateProjection(session).load_runtime_as_of(
                project_id, as_of_chapter=max(chapter - 1, 0),
            )
            view = runtime.cognition_by_observer.get((event.observer_type, event.observer_id))
            result = BookStateCompiler(session).compile(
                ApprovedGraphDeltaSet(
                    project_id=project_id,
                    chapter_number=chapter,
                    graph_deltas=[
                        _delta(
                            project_id,
                            chapter,
                            f"delta-knowledge-{chapter}",
                            [knowledge_event_to_patch(
                                event, knowledge_by_ref=view.knowledge_by_ref if view is not None else {},
                            )],
                        )
                    ],
                )
            )
            assert result.committed, result.blocked_reasons
        latest = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=1)
        earlier = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)

    view = latest.cognition_by_observer[("character", "c1")]
    assert view.knowledge_by_ref == {"fact:f1": "believes"}
    assert view.evidence_by_ref == {
        "fact:f1": ["knowledge:rumor:-:10", "knowledge:told:c2:700"]
    }
    old_view = earlier.cognition_by_observer[("character", "c1")]
    assert old_view.knowledge_by_ref == {"fact:f1": "heard"}
    assert old_view.evidence_by_ref == {"fact:f1": ["knowledge:rumor:-:10"]}
    engine.dispose()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_knowledge_patch_old_value_is_compared_per_ref(backend: str) -> None:
    engine = create_engine("sqlite://") if backend == "sqlite" else get_engine(postgres_test_url())
    if backend == "sqlite":
        Base.metadata.create_all(engine)
    else:
        init_db(engine)
    Session = get_session_factory(engine)

    def compile_patch(session, project_id: str, chapter: int, fact_ref: str, old_value):
        patch = knowledge_event_to_patch(
            KnowledgeEvent("character", "c1", fact_ref, "knows", "witnessed", "scene", 900),
            knowledge_by_ref={},
        ).model_copy(update={"old_value": old_value})
        return BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=chapter,
                graph_deltas=[_delta(project_id, chapter, f"delta-cas-{chapter}", [patch])],
            )
        )

    with Session.begin() as session:
        project_id = _create_project(session)
        seed = BookStateCompiler(session).compile(
            ApprovedGraphDeltaSet(
                project_id=project_id,
                chapter_number=0,
                graph_deltas=[
                    _delta(
                        project_id,
                        0,
                        "delta-cas-seed",
                        [
                            knowledge_event_to_patch(
                                KnowledgeEvent("character", "c1", "fact:f1", "suspects", "inferred"),
                                knowledge_by_ref={},
                            ),
                            knowledge_event_to_patch(
                                KnowledgeEvent("character", "c1", "fact:f2", "heard", "rumor"),
                                knowledge_by_ref={},
                            ),
                        ],
                    )
                ],
            )
        )
        assert seed.committed, seed.blocked_reasons

        stale = compile_patch(session, project_id, 1, "fact:f1", {"fact:f1": "believes"})
        assert stale.committed is False
        assert any("old_value mismatch" in reason for reason in stale.blocked_reasons)

        fresh = compile_patch(session, project_id, 1, "fact:f1", {"fact:f1": "suspects"})
        assert fresh.committed, fresh.blocked_reasons

        # The ref is compared on its own: untouched refs (f2) do not matter, and a
        # ref that holds no state yet matches None.
        first_time = compile_patch(session, project_id, 2, "fact:f9", {"fact:f9": None})
        assert first_time.committed, first_time.blocked_reasons

        already_known = compile_patch(session, project_id, 3, "fact:f9", {"fact:f9": None})
        assert already_known.committed is False
        assert any("old_value mismatch" in reason for reason in already_known.blocked_reasons)
    engine.dispose()


def _knowledge_delta(new_value: object, *, op: str = "merge") -> GraphDelta:
    return GraphDelta(
        id="delta-1",
        project_id="project-1",
        cognition_patches=[
            CognitionPatch(
                observer_type="character",
                observer_id="c1",
                op=op,
                field_path="knowledge_by_ref",
                new_value=new_value,
            )
        ],
    )


def test_invalid_knowledge_patch_is_schema_error() -> None:
    assert validate_graph_delta(_knowledge_delta({"fact:f1": "believes"})).ok

    bad_state = validate_graph_delta(_knowledge_delta({"fact:f1": "maybe"}))
    assert not bad_state.ok
    assert [issue.code for issue in bad_state.errors] == ["invalid_knowledge_state"]

    bad_ref = validate_graph_delta(_knowledge_delta({"node:n1": "knows"}))
    assert not bad_ref.ok
    assert [issue.code for issue in bad_ref.errors] == ["invalid_knowledge_ref"]

    not_a_mapping = validate_graph_delta(_knowledge_delta("fact:f1", op="append"))
    assert not not_a_mapping.ok
    assert [issue.code for issue in not_a_mapping.errors] == ["invalid_knowledge_patch"]

    assert not validate_graph_delta(_knowledge_delta({"fact:f1": "maybe"}, op="replace")).ok
