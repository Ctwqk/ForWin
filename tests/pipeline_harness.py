"""Genesis-seeded books for pipeline tests; callers supply their writer/review stubs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from forwin.genesis import BookGenesisService
from forwin.genesis.handoff.commands import StartWritingCommand
from forwin.generation.pipeline_core.result import RunResult
from forwin.models.genesis import BookGenesisRevision
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from tests.helpers.band_plans import stub_plan, write_band_plan
from tests.helpers.genesis_seed import seeded_map_atlas, seeded_story_engine, seeded_world_seed


def seeded_book(
    Session,
    *,
    chapters: int = 1,
    policy: RuntimePolicy | None = None,
    arc_ranges: Sequence[tuple[int, int]] | None = None,
    plans: Mapping[int, Mapping[str, Any]] | None = None,
) -> str:
    """Commit chapter-0 canon through Genesis handoff and preplan the active arc.

    Prewritten band plans keep writer/review tests independent of planning LLMs. Future
    arcs remain unplanned, as in production. Every call creates an independent project.
    """
    if chapters < 1:
        raise ValueError("chapters must be positive")
    ranges = list(arc_ranges or [(1, chapters)])
    if [number for start, end in ranges for number in range(start, end + 1)] != list(range(1, chapters + 1)):
        raise ValueError("arc_ranges must cover chapters in order without gaps")
    policy = policy or RuntimePolicy.for_profile("standard")
    pack = {
        "world": {"map_atlas": seeded_map_atlas(), "story_engine": seeded_story_engine()},
        "world_seed": seeded_world_seed(),
        "book_arc_blueprint": {"arcs": [
            {"arc_number": index, "title": f"arc-{index}", "arc_synopsis": f"arc-{index}",
             "chapter_start": start, "chapter_end": end, "target_size": end - start + 1}
            for index, (start, end) in enumerate(ranges, start=1)
        ]},
    }
    # No model is needed: handoff uses the deterministic fallback beat sheet.
    genesis = BookGenesisService(llm_client=None)
    with Session.begin() as session:
        updater = StateUpdater(session)
        project = updater.create_project(
            title="pipeline harness", premise="pipeline harness", genre="test",
            runtime_policy=policy, creation_status="genesis_ready", target_total_chapters=chapters,
        )
        revision = BookGenesisRevision(project_id=project.id, status="locked", pack_json=json.dumps(pack))
        session.add(revision)
        session.flush()
        project.active_genesis_revision_id = revision.id
        handoff = genesis.handoff.start_writing(
            session=session, updater=updater, command=StartWritingCommand(project_id=project.id),
        )
        sheets = ArcBeatSheetRepository(session)
        sheet = sheets.load(sheets.active_for_arc(handoff.active_arc_id))
        for band in sheet.band_breakdown:
            write_band_plan(session, project.id, {
                number: dict(plans[number]) if plans is not None and number in plans else stub_plan(number)
                for number in range(band.chapter_start, band.chapter_end + 1)
            })
        return project.id


def write_one_chapter(pipeline, *, project_id: str | None = None) -> RunResult:
    """Continue one chapter, seeding a one-chapter book when none was supplied."""
    if project_id is None:
        project_id = seeded_book(pipeline._SessionFactory, chapters=1, policy=pipeline.policy)
    return pipeline.continue_project(project_id, max_chapters=1)
