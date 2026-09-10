"""Real API/personality owners must preserve concurrent current projections."""

import pytest
from sqlalchemy import select

from forwin.api_schema import (
    CharacterPersonalityReassignRequest,
    PersonalityLoadoutUpdateRequest,
)
from forwin.book_state.repository import BookStateRepository
from forwin.http.adapters import api_book_state_routes as routes
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import Project
from forwin.personality.assignment import PersonalityLoadoutAssigner
from forwin.personality.enrichment import RelationshipPersonalityEnricher
from forwin.protocol.book_state import WorldEdge, WorldNode
from tests.postgres import postgres_test_url


@pytest.fixture
def personality_store():
    engine = get_engine(postgres_test_url("personality-projection-race"))
    init_db(engine)
    factory = get_session_factory(engine)
    with factory.begin() as session:
        project = Project(title="People", premise="A quiet archive", genre="thriller")
        session.add(project)
        session.flush()
        for node_id in ("first", "second"):
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id=node_id,
                    project_id=project.id,
                    node_type="character",
                    name=node_id,
                    summary="Original summary",
                    profile={"public_identity": "a quiet observer"},
                )
            )
        BookStateRepository(session).create_world_edge(
            WorldEdge(
                id="rival",
                project_id=project.id,
                source_id="first",
                target_id="second",
                edge_type="enemy_of",
                edge_family="social",
                metadata={"description": "rival"},
            )
        )
        project_id = project.id
    yield factory, project_id
    engine.dispose()


def _concurrent_change(factory, project_id, *, manual=False, summary=False):
    with factory.begin() as session:
        session.scalar(
            select(Project.id).where(Project.id == project_id).with_for_update()
        )
        repo = BookStateRepository(session)
        node = repo.get_world_node("first")
        profile = {**node.profile, "concurrent_key": "preserve me"}
        metadata = {**node.metadata, "concurrent_key": "preserve me too"}
        if manual:
            profile["personality_loadout"] = {
                "dominant": {"skill": "trait-quiet-observer", "weight": 0.91}
            }
            metadata["personality_assignment"] = {
                "manual_override": True,
                "assignment_id": "concurrent-manual",
            }
        repo.create_world_node(
            node.model_copy(
                update={
                    "profile": profile,
                    "metadata": metadata,
                    **({"summary": "Concurrent summary"} if summary else {}),
                }
            )
        )


def _inject_before_mutation(
    monkeypatch, entry, factory, project_id, *, manual=False, summary=False
):
    called = []

    def inject():
        if not called:
            called.append(True)
            _concurrent_change(factory, project_id, manual=manual, summary=summary)

    if entry == "setter":
        original = routes._get_character_node

        def read(*args, **kwargs):
            node = original(*args, **kwargs)
            inject()
            return node

        monkeypatch.setattr(routes, "_get_character_node", read)
    elif entry in {"backfill", "reassign"}:
        original = PersonalityLoadoutAssigner.assign

        def assign(*args, **kwargs):
            result = original(*args, **kwargs)
            inject()
            return result

        monkeypatch.setattr(PersonalityLoadoutAssigner, "assign", assign)
    else:
        original = RelationshipPersonalityEnricher._add_pattern

        def enrich(*args, **kwargs):
            inject()
            return original(*args, **kwargs)

        monkeypatch.setattr(RelationshipPersonalityEnricher, "_add_pattern", enrich)
    return called


def _run(factory, project_id, entry):
    handlers = routes.build_handlers(get_session=factory)
    if entry == "setter":
        return handlers["set_character_personality_loadout"](
            project_id,
            "first",
            PersonalityLoadoutUpdateRequest(
                personality_loadout={}, reason="Manual update"
            ),
        )
    if entry == "backfill":
        return handlers["backfill_character_personalities"](project_id)
    if entry == "reassign":
        return handlers["reassign_character_personality"](
            project_id, "first", CharacterPersonalityReassignRequest()
        )
    with factory.begin() as session:
        return RelationshipPersonalityEnricher(session).enrich_relation(
            WorldEdge(
                id="rival",
                project_id=project_id,
                source_id="first",
                target_id="second",
                edge_type="enemy_of",
                edge_family="social",
                metadata={"description": "rival"},
            )
        )


def _seed_loadout(factory):
    with factory.begin() as session:
        repo = BookStateRepository(session)
        for node_id in ("first", "second"):
            node = repo.get_world_node(node_id)
            repo.create_world_node(
                node.model_copy(
                    update={
                        "profile": {
                            **node.profile,
                            "personality_loadout": {
                                "dominant": {
                                    "skill": "trait-quiet-observer",
                                    "weight": 0.5,
                                }
                            },
                        }
                    }
                )
            )


@pytest.mark.parametrize("entry", ["setter", "backfill", "reassign", "enricher"])
def test_actual_personality_entry_preserves_concurrent_unrelated_fields(
    personality_store, monkeypatch, entry
):
    factory, project_id = personality_store
    if entry == "enricher":
        _seed_loadout(factory)
    injected = _inject_before_mutation(
        monkeypatch, entry, factory, project_id, summary=entry == "setter"
    )
    _run(factory, project_id, entry)
    assert injected == [True]
    with factory() as session:
        node = BookStateRepository(session).get_world_node("first")
        assert node.profile.get("concurrent_key") == "preserve me"
        assert node.metadata.get("concurrent_key") == "preserve me too"
        if entry == "setter":
            assert node.summary == "Concurrent summary"


@pytest.mark.parametrize("entry", ["backfill", "reassign", "enricher"])
def test_automatic_personality_entry_preserves_concurrent_manual_override(
    personality_store, monkeypatch, entry
):
    factory, project_id = personality_store
    if entry == "enricher":
        _seed_loadout(factory)
    injected = _inject_before_mutation(
        monkeypatch, entry, factory, project_id, manual=True
    )
    _run(factory, project_id, entry)
    assert injected == [True]
    with factory() as session:
        node = BookStateRepository(session).get_world_node("first")
        assert (
            node.metadata["personality_assignment"]["assignment_id"]
            == "concurrent-manual"
        )
        assert node.profile["personality_loadout"] == {
            "dominant": {"skill": "trait-quiet-observer", "weight": 0.91}
        }


@pytest.mark.parametrize("entry", ["backfill", "reassign"])
def test_automatic_assignment_rejects_changed_character_inputs(
    personality_store, monkeypatch, entry
):
    from fastapi import HTTPException

    factory, project_id = personality_store
    _inject_before_mutation(monkeypatch, entry, factory, project_id, summary=True)
    with pytest.raises(HTTPException) as error:
        _run(factory, project_id, entry)
    assert error.value.status_code == 409
    with factory() as session:
        node = BookStateRepository(session).get_world_node("first")
        assert node.summary == "Concurrent summary"
        assert not node.profile.get("personality_loadout")


def test_mutation_refreshes_cached_row_and_preserves_caller_rollback(personality_store):
    from forwin.models.book_state import WorldNodeRow
    from forwin.personality.mutations import apply_assignment

    factory, project_id = personality_store
    with factory() as session:
        cached = session.get(WorldNodeRow, "first")
        original = BookStateRepository(session).get_world_node("first")
        _concurrent_change(factory, project_id, summary=True)
        mutation = apply_assignment(
            session,
            original=original,
            loadout={},
            assignment={"manual_override": True},
            mode="manual",
        )
        assert cached.summary == "Concurrent summary"
        assert mutation.current.profile["concurrent_key"] == "preserve me"
        session.rollback()
    with factory() as session:
        node = BookStateRepository(session).get_world_node("first")
        assert node.summary == "Concurrent summary"
        assert "personality_loadout" not in node.profile


def test_actual_setter_waits_for_project_owner_before_patch(personality_store):
    from contextlib import contextmanager

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    factory, project_id = personality_store

    @contextmanager
    def bounded_session():
        with factory() as session:
            session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            yield session

    with factory.begin() as owner:
        owner.scalar(
            select(Project.id).where(Project.id == project_id).with_for_update()
        )
        with pytest.raises(OperationalError, match="lock timeout"):
            routes.build_handlers(get_session=bounded_session)[
                "set_character_personality_loadout"
            ](
                project_id,
                "first",
                PersonalityLoadoutUpdateRequest(personality_loadout={}),
            )


def test_enricher_refuses_relation_changed_after_read(personality_store, monkeypatch):
    factory, project_id = personality_store
    _seed_loadout(factory)
    original = RelationshipPersonalityEnricher._add_pattern
    changed = []

    def interleave(*args, **kwargs):
        if not changed:
            changed.append(True)
            with factory.begin() as session:
                repo = BookStateRepository(session)
                relation = repo.list_world_edges(project_id)[0]
                repo.create_world_edge(relation.model_copy(update={"is_active": False}))
        return original(*args, **kwargs)

    monkeypatch.setattr(RelationshipPersonalityEnricher, "_add_pattern", interleave)
    with pytest.raises(ValueError, match="relationship changed"):
        _run(factory, project_id, "enricher")
    with factory() as session:
        assert (
            BookStateRepository(session)
            .get_world_node("first")
            .profile["personality_loadout"]
            .get("relationship_patterns", [])
            == []
        )
