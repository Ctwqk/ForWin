import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from forwin.book_state.repository import BookStateRepository
from forwin.canon.admission import CanonAdmissionService
from forwin.models.project import Project
from tests import test_canon_atomic_transaction as atomic

prepared_canon = atomic.prepared_canon


def test_non_canon_world_writer_serializes_before_overwriting_revision_source(
    prepared_canon,
):
    fixture = prepared_canon
    CanonAdmissionService(session_factory=fixture.Session).commit_plan(fixture.plan)
    with fixture.Session.begin() as owner:
        owner.scalar(
            select(Project).where(Project.id == fixture.project_id).with_for_update()
        )
        with fixture.Session.begin() as other:
            other.execute(text("SET LOCAL lock_timeout = '100ms'"))
            node = BookStateRepository(other).get_world_node(
                fixture.plan.entity_admission_plan.decisions[0].entity_id
            )
            changed = node.model_copy(update={"summary": "A concurrent world edit"})
            with pytest.raises(OperationalError, match="lock timeout"):
                BookStateRepository(other).create_world_node(changed)
                other.flush()
            other.rollback()
    with fixture.Session.begin() as other:
        BookStateRepository(other).create_world_node(changed)
    with fixture.Session() as session:
        assert (
            BookStateRepository(session).get_world_node(node.id).summary
            == "A concurrent world edit"
        )


def test_savepoint_rollback_must_reacquire_project_before_projection_mutation(
    prepared_canon,
):
    from forwin.canon.projection_lock import lock_projection_project

    f = prepared_canon
    CanonAdmissionService(session_factory=f.Session).commit_plan(f.plan)
    with f.Session.begin() as writer:
        writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
        nested = writer.begin_nested()
        lock_projection_project(writer, f.project_id)
        nested.rollback()
        with f.Session.begin() as canon:
            canon.execute(text("SET LOCAL lock_timeout = '100ms'"))
            # PG releases the nested-acquired Project lock on savepoint rollback.
            canon.scalar(
                select(Project.id).where(Project.id == f.project_id).with_for_update()
            )
            repo = BookStateRepository(writer)
            node = repo.get_world_node(
                f.plan.entity_admission_plan.decisions[0].entity_id
            )
            with pytest.raises(OperationalError, match="lock timeout"):
                repo.create_world_node(
                    node.model_copy(update={"summary": "after savepoint rollback"})
                )
                writer.flush()
            writer.rollback()
