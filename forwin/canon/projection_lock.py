"""Project-first serialization shared by current projection mutation owners."""

from sqlalchemy import select

from forwin.models.project import Project


def lock_projection_project(session, project_id):
    # Cache is scoped to the actual transaction object, not the Session lifetime.
    # A rollback/commit gives the next operation a different transaction object.
    transaction = session.get_nested_transaction() or session.get_transaction()
    cache = session.info.setdefault("projection_project_locks", {})
    if transaction is not None and cache.get(project_id) is transaction:
        return
    with session.no_autoflush:
        session.scalar(
            select(Project.id).where(Project.id == project_id).with_for_update()
        )
    cache[project_id] = session.get_nested_transaction() or session.get_transaction()


from sqlalchemy import event
from sqlalchemy.orm import Session


@event.listens_for(Session, "after_transaction_end")
def _forget_finished_projection_locks(session, transaction):
    cache = session.info.get("projection_project_locks")
    if cache:
        for project_id in [key for key, owner in cache.items() if owner is transaction]:
            del cache[project_id]
        if not cache:
            session.info.pop("projection_project_locks", None)
