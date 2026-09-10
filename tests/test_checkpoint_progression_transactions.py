"""The actual chapter caller ends checkpoint transactions before model work."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from forwin.generation.pipeline_core.project_chapters import ChapterExecutionStage
from forwin.models import BandCheckpoint, BandExperiencePlan, Project
from forwin.review.plan_checks import BandCheckpointEvaluator
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from tests import test_checkpoint_evidence_actions as actions
from tests.test_accepted_history_controls import _Controls

checkpoint_book = actions.checkpoint_book


class _ReachedBoundary(BaseException):
    def __init__(self, lock_available):
        self.lock_available = lock_available


@pytest.mark.parametrize("boundary", ["manual_start", "plan_audit", "writer"])
def test_actual_chapter_caller_releases_checkpoint_project_lock_before_models(
    checkpoint_book, boundary
):
    book = checkpoint_book
    with book.Session.begin() as session:
        session.add(
            BandExperiencePlan(
                project_id=book.project_id,
                arc_id=book.arc_id,
                band_id="band-2",
                chapter_start=4,
                chapter_end=6,
            )
        )
        session.flush()
        before_id = BandCheckpointEvaluator(session).refresh(book.project_id, 3).id
    actions._change_contract(book, delivered=True)

    def reached_boundary():
        with book.Session.begin() as other:
            try:
                other.scalar(
                    select(Project.id)
                    .where(Project.id == book.project_id)
                    .with_for_update(nowait=True)
                )
            except OperationalError as exc:
                raise _ReachedBoundary(False) from exc
        raise _ReachedBoundary(True)

    class Stage(_Controls):
        def _abort_requested(self):
            return False

        def _pause_requested(self):
            return False

        def _recover_post_canon_before_chapter(self, *, session, **_):
            # Maintenance owns an independent transaction in the actual caller.
            session.commit()

        def _manual_boundary_checkpoint(self, session, **request):
            if boundary == "manual_start":
                reached_boundary()
            return super()._manual_boundary_checkpoint(session, **request)

        def _audit_current_plan_before_write(self, **request):
            if boundary == "plan_audit":
                reached_boundary()
            return request["context"]

        def _emit_progress(self, *_args, **_request):
            pass

    stage = Stage()
    stage.retrieval_broker = SimpleNamespace(
        build_chapter_context=lambda *_: SimpleNamespace(),
        last_observability_summary={},
    )
    stage.writer_execution = SimpleNamespace(execute=lambda _: reached_boundary())
    with book.Session() as session:
        with pytest.raises(_ReachedBoundary) as reached:
            ChapterExecutionStage._run_project_chapters(
                stage,
                session=session,
                repo=StateRepository(session),
                updater=StateUpdater(session),
                checker=SimpleNamespace(),
                project_id=book.project_id,
                chapter_numbers=[4],
                requested_chapters=1,
            )
        assert reached.value.lock_available, (
            f"checkpoint Project lock held at {boundary}"
        )
    # The caller must commit the fresh evidence, not roll it back to drop its lock.
    with book.Session() as session:
        current = StateRepository(session).get_latest_band_checkpoint(
            book.project_id, band_id="band-1"
        )
        assert current.id != before_id
        assert current.status == "pass"
        assert BandCheckpointEvaluator(session).inspect(current).current
        assert session.get(BandCheckpoint, before_id).status == "pass"
