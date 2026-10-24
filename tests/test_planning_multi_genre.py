"""Three Genesis books plan, write and tick their first band through the same real services.

Only model answers are fixture-backed. The short writer samples use the harness's hard-floor
stub; extraction, translation, the world gate, planning and feasibility remain real.
``test_fixture_planning_outputs_are_valid`` needs no database. All other tests use the shared
PostgreSQL ``pipeline`` fixture, never a live model or production database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import wraps
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from forwin.canon import CanonAdmissionService  # noqa: F401 - initialize canon before maintenance
from forwin.audit.events import DecisionEventType
from forwin.book_state import BookStateProjection, BookStateRepository
from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.cognition import CognitionView
from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction
from forwin.book_state.extraction.evidence import validate_evidence
from forwin.book_state.extraction.translator import SOURCE_TYPE as CHAPTER_SOURCE_TYPE
from forwin.book_state.knowledge import HIDDEN_SENSITIVITY, is_concealed
from forwin.book_state.lexicon import lexicon_from_payload
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.book_state.story_clock import calendar_from_payload, duration_to_minutes
from forwin.checker.hard_floor import HardFloorResult
from forwin.generation.pipeline_core import project_chapters
from forwin.genesis import BookGenesisService
from forwin.genesis.handoff.commands import StartWritingCommand
from forwin.genesis.handoff.world_seed_committer import build_world_seed_deltas, seed_ref_id
from forwin.maintenance.post_canon import build_world_tick_service, save_world_prompt_trace
from forwin.map.genesis_adapter import build_subworld_map_specs_from_genesis
from forwin.map.generator import generate_subworld_map
from forwin.models.genesis import PromptTrace
from forwin.models.audit import DecisionEvent
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.phase import BandExperiencePlan
from forwin.models.planning_v6 import BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.planning.arc_progression import ArcProgressionService
from forwin.planning.band_planner.inputs import OfferedRefs
from forwin.planning.band_planner.service import BandPlanner
from forwin.planning.band_planner.types import BandPlanOutput
from forwin.planning.band_planner.validation import band_plan_issues
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.service import ArcBeatSheetService, beat_sheet_issues
from forwin.planning.beat_sheet.types import ArcBeatSheetData
from forwin.planning.plan_step import PlanStepService
from forwin.planning.feasibility import FeasibilityAuditor
from forwin.protocol.book_state import CognitionOverlay
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.world_engine.travel import estimate_travel
from forwin.world_engine.effect_validation import effect_reference_issues
from forwin.world_engine.types import ActorTurnBatch, AdjudicationResult
from forwin.world_view import builder as world_view_builder
from tests.chapter_delta_helpers import body_sha256
from tests.pipeline_harness import write_one_chapter
from tests.test_chapter_delta_multi_genre import (  # noqa: F401 - shared PostgreSQL fixture
    _Book, _ExtractionClient, _committed_refs, _resolved, pipeline,
)
from tests.test_world_seed_multi_genre import GENRES, _NoApiKeyClient, create_ready_project, load_fixture
from tests.test_world_tick_multi_genre import _WorldClient, _story_minutes, _world_step
from tests.test_writer_world_view_prompt import BUILDERS, _old_surfaces, _prompt


def _resolve(value: Any, refs: dict, project_id: str) -> Any:
    """Resolve fixture names as the P3 fixtures do, plus project-scoped seed fact references."""
    if isinstance(value, dict):
        return {key: _resolve(item, refs, project_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, refs, project_id) for item in value]
    if isinstance(value, str) and value.startswith('@fact:'):
        return f"fact:{seed_ref_id(project_id, 'fact', value.removeprefix('@fact:'))}"
    return _resolved(value, refs)


def _fixture_runtime(fixture: dict, project_id: str) -> BookStateRuntime:
    """Generate the same seeded map and apply real seed deltas in memory, without a database."""
    pack = fixture['pack']
    seed = pack['world_seed']
    calendar, lexicon = calendar_from_payload(seed['calendar']), lexicon_from_payload(seed['lexicon'])
    atlas = pack['world']['map_atlas']
    generated = [generate_subworld_map(spec.model_copy(update={'generation_seed': 15}))
                 for spec in build_subworld_map_specs_from_genesis(project_id=project_id, map_atlas=atlas)]
    assert all(result.validation_report.valid for result in generated)
    nodes = [node for result in generated for node in result.map_nodes]
    sites = {node.metadata['source_node_id']: node.id for node in nodes if node.metadata.get('source_node_id')}
    runtime = BookStateRuntime(
        project_id=project_id, as_of_chapter=0, world=ObjectiveWorldGraph(),
        map_graph=MapGraph(nodes=nodes, edges=[edge for result in generated for edge in result.map_edges]),
    )
    for delta in build_world_seed_deltas(
        project_id=project_id, revision_id='fixture', seed=seed,
        story_engine=pack['world']['story_engine'], map_node_ids=sites, calendar=calendar, lexicon=lexicon,
    ):
        runtime.world.apply_delta(delta)
        for item in delta.cognition_patches:
            key = (str(item.observer_type), item.observer_id)
            if key not in runtime.cognition_by_observer:
                runtime.cognition_by_observer[key] = CognitionView(CognitionOverlay(
                    id=f'fixture:{key}', project_id=project_id, observer_type=key[0], observer_id=key[1],
                ))
            runtime.cognition_by_observer[key].apply_cognition_patch(item.field_path, str(item.op), item.new_value)
        for item in delta.narrative_patches:
            runtime.narrative.apply_narrative_patch(item, project_id=project_id)
    runtime.map.cognition_by_observer.update(runtime.cognition_by_observer)
    return runtime


def _use_relocation_plan(fixture: dict) -> None:
    """Only the relocation scenario switches the unwritten chapters to an offstage POV."""
    overrides = fixture['world_tick']['planning_relocation']['plan_overrides']
    for chapter in fixture['band_plan']['chapters'][1:]:
        chapter.update(overrides)


@pytest.mark.parametrize('genre', GENRES)
def test_fixture_planning_outputs_are_valid(genre: str) -> None:
    fixture = load_fixture(genre)
    runtime = _fixture_runtime(fixture, f'fixture-{genre}')
    refs = _committed_refs(runtime)
    seed = fixture['pack']['world_seed']
    calendar, lexicon = calendar_from_payload(seed['calendar']), lexicon_from_payload(seed['lexicon'])
    tolerance = RuntimePolicy.for_profile('standard').review.travel_tolerance
    actors = {node.id for node in runtime.world.nodes_by_id.values() if node.node_type in ACTOR_NODE_TYPES}
    for section, bounds in ((fixture, (1, 3)), (fixture['planning_continuation'], (4, 6))):
        sheet = ArcBeatSheetData.model_validate(_resolve(section['beat_sheet'], refs, runtime.project_id))
        assert beat_sheet_issues(sheet, chapter_start=bounds[0], chapter_end=bounds[1],
                                 runtime=runtime, actor_ids=actors) == []
        offered = OfferedRefs(
            actor_ids=frozenset(actors),
            character_ids=frozenset(ref for ref in actors if runtime.world.get_node(ref).node_type == 'character'),
            location_ids=frozenset(runtime.map.nodes_by_id),
            facts={f'fact:{fact.id}': fact.proposition for fact in runtime.world.facts_by_id.values()},
            intent_ids=frozenset(node.id for node in runtime.world.nodes_by_id.values() if node.node_type == 'intent'),
            event_ids=frozenset(),
            clock_ids=frozenset(node.id for node in runtime.world.nodes_by_id.values() if node.node_type == 'clock'),
            relation_ids=frozenset(runtime.world.edges_by_id), news_fact_refs=frozenset(),
            milestone_ids=frozenset(item.id for item in sheet.milestones),
            time_units=frozenset([*(u.name for u in calendar.units), *(a.name for a in lexicon.time_unit_aliases)]),
            concealed_names={node.id: node.name for node in runtime.world.nodes_by_id.values()
                             if is_concealed(runtime.world, node)}, rewards_by_chapter={},
        )
        answers = [(section['band_plan'], list(range(bounds[0], bounds[1] + 1)))]
        if bounds == (1, 3):
            answers.append((fixture['planning_continuation']['replan'], [2, 3]))
            relocation = load_fixture(genre)
            _use_relocation_plan(relocation)
            answers.append((relocation['band_plan'], [1, 2, 3]))
        for answer, numbers in answers:
            output = BandPlanOutput.model_validate(_resolve(answer, refs, runtime.project_id))
            assert band_plan_issues(output, chapter_numbers=numbers, offered=offered, runtime=runtime,
                                    calendar=calendar, lexicon=lexicon) == []
            for chapter in output.chapters:
                writer_text = '\n'.join([chapter.title, chapter.one_line, *chapter.goals, chapter.hook,
                                         *(hint.hint_text for hint in chapter.hint_directives)])
                for fact in seed['facts']:
                    if fact['sensitivity'] in HIDDEN_SENSITIVITY:
                        assert fact['proposition'] not in writer_text
    for key in ('planning', 'planning_relocation'):
        answers = _resolved(fixture['world_tick'][key], refs)
        prompt = {'calendar': seed['calendar'], 'lexicon': seed['lexicon'],
                  'context': {'now': calendar.opening_minute + 2 * 1440}}
        adjudication = AdjudicationResult.model_validate(_story_minutes(answers['adjudicate'], prompt))
        ActorTurnBatch.model_validate(answers['actor_turn'])
        for outcome in adjudication.outcomes:
            assert effect_reference_issues(outcome.effects, runtime, lexicon) == []
            for move in outcome.effects.movements:
                relocation = load_fixture(genre)
                _use_relocation_plan(relocation)
                initial = BandPlanOutput.model_validate(_resolve(relocation['band_plan'], refs, runtime.project_id))
                departure = resolve_world_node_location_id(runtime.world, move.entity_ref)
                assert move.entity_ref == initial.chapters[1].pov_ref
                assert move.entity_ref in initial.chapters[1].onstage_refs
                assert move.entity_ref not in initial.chapters[0].onstage_refs
                assert move.entity_ref != initial.chapters[0].pov_ref
                chapter = _resolved(fixture['planning_continuation']['chapters']['1']['chapter_delta_extraction'], refs)
                assert move.entity_ref not in {item['entity_ref'] for item in chapter['movements']}
                assert move.entity_ref not in {ref for event in chapter['events'] for ref in event['participant_refs']}
                assert departure == initial.chapters[1].location_refs[0]
                travel = estimate_travel(runtime, lexicon, mover_id=move.entity_ref,
                                         from_location_id=departure, to_location_id=move.to_location_id)
                assert travel.reachable and travel.route_known and travel.minutes is not None
                assert travel.minutes <= move.arrive_at - move.depart_at
                returning = estimate_travel(runtime, lexicon, mover_id=move.entity_ref,
                                            from_location_id=move.to_location_id, to_location_id=departure)
                assert returning.reachable and returning.route_known and returning.minutes is not None
                span = initial.chapters[1].expected_span
                assert returning.minutes > duration_to_minutes(calendar, lexicon, amount=span.amount,
                                                               unit_name=span.unit) * (1 + tolerance)
    for number, chapter in fixture['planning_continuation']['chapters'].items():
        assert len(chapter['body'].strip()) >= 100
        extraction = ChapterDeltaExtraction.model_validate({
            'body_sha256': body_sha256(chapter['body']),
            **_resolved(chapter['chapter_delta_extraction'], refs),
        })
        _, issues = validate_evidence(extraction, chapter['body'])
        assert issues == [], (number, issues)
        assert {item.target_id for item in extraction.plan_fulfillment if item.status == 'done'} == {
            'goal:1', 'bind:0',
        }


class _PlanningClient:
    model = 'stub-planning-model'
    last_call_result = None

    def __init__(self, fixture: dict, project_id: str) -> None:
        self.fixture, self.project_id = fixture, project_id
        self.refs = _committed_refs(_fixture_runtime(fixture, project_id))
        self.calls: list[tuple[str, dict]] = []
        self.replanning = False
        # Like the real router client, record one attempt event per call for the trace.
        self.llm_attempt_events: list[dict] = []

    def chat(self, messages, **kwargs):
        stage = kwargs['stage_key']
        payload = json.JSONDecoder().raw_decode(messages[-1]['content'])[0]
        self.calls.append((stage, payload))

        section = self.fixture if payload['arc']['arc_number'] == 1 else self.fixture['planning_continuation']
        if stage == 'plan.arc_beats':
            answer = section['beat_sheet']
        elif stage == 'plan.band':
            answer = self.fixture['planning_continuation']['replan'] if self.replanning else section['band_plan']
            answer = {'chapters': [item for item in answer['chapters']
                                   if item['chapter_number'] in payload['chapters_to_plan']]}
        else:
            raise AssertionError(f'unexpected model stage {stage!r}')
        text = json.dumps(_resolve(answer, self.refs, self.project_id), ensure_ascii=False)
        self.llm_attempt_events.append(
            {'attempt_no': 1, 'stage_key': stage, 'model': self.model, 'output_chars': len(text)}
        )
        return text

    def drain_llm_attempt_events(self) -> list[dict]:
        events, self.llm_attempt_events = self.llm_attempt_events, []
        return events


@dataclass
class _PlannedBook:
    book: _Book
    fixture: dict
    client: _PlanningClient
    sheet_id: str
    band_id: str


def _start_planned_book(pipeline, genre: str, *, relocation: bool = False) -> _PlannedBook:
    fixture = load_fixture(genre)
    if relocation:
        _use_relocation_plan(fixture)
    project_id = f'proj-planning-{genre}'
    client = _PlanningClient(fixture, project_id)
    beats = ArcBeatSheetService(client, save_trace=save_world_prompt_trace)
    feasibility = FeasibilityAuditor()
    planner = BandPlanner(client, feasibility=feasibility, save_trace=save_world_prompt_trace)
    genesis = BookGenesisService(llm_client=_NoApiKeyClient(), beat_sheets=beats)
    policy = pipeline.policy.with_user_settings(manual_checkpoints=False, band_checkpoint_action='continue')
    pipeline.policy = policy
    pipeline.repair_execution = replace(pipeline.repair_execution, policy=policy)
    pipeline.canon_preparation_context = replace(pipeline.canon_preparation_context, policy=policy)
    with pipeline._SessionFactory() as session:
        project = create_ready_project(session, genesis, fixture, project_id)
        ProjectPolicyStore(session).initialize(project, policy)
        session.commit()
        with patch('forwin.map.genesis_adapter._stable_seed', return_value=15):
            handoff = genesis.handoff.start_writing(
                session=session, updater=StateUpdater(session), command=StartWritingCommand(project_id=project_id),
            )
        session.commit()
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
        client.refs = _committed_refs(runtime)
        sheets = ArcBeatSheetRepository(session)
        row = sheets.active_for_arc(handoff.active_arc_id)
        assert row is not None and row.source == 'llm', 'fixture beat sheet must not fall back'
        assert list(session.scalars(select(ChapterPlan).where(ChapterPlan.project_id == project_id))) == []
        outcome = planner.plan_band(
            session, project_id=project_id, arc_plan=session.get(ArcPlanVersion, handoff.active_arc_id),
            beat_sheet_row=row, band=sheets.load(row).band_breakdown[0], policy=policy,
        )
        assert outcome.band_plan_id is not None, (outcome.failure_reason, outcome.issues)
        assert not [issue for issue in outcome.issues if issue.severity == 'error']
        session.commit()
        sheet_id, band_id = row.id, outcome.band_plan_id
    pipeline.band_planner = planner
    pipeline.arc_progression = ArcProgressionService(beats)
    pipeline.post_canon_maintenance.plan_step = PlanStepService(planner, feasibility, pipeline.arc_progression)
    book = _Book(project_id, fixture['chapter_1'], client.refs)
    return _PlannedBook(book, fixture, client, sheet_id, band_id)


@pytest.mark.parametrize('genre', GENRES)
def test_start_writing_to_first_band_plan_in_three_genres(pipeline, genre: str) -> None:
    planned = _start_planned_book(pipeline, genre)
    project_id = planned.book.project_id
    expected = BandPlanOutput.model_validate(_resolve(planned.fixture['band_plan'], planned.book.refs, project_id))
    with pipeline._SessionFactory() as session:
        assert session.get(Project, project_id).creation_status == 'writing'
        assert BookStateRepository(session).latest_canon_chapter(project_id) == 0
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
        band = session.get(BandPlanRow, planned.band_id)
        assert (band.beat_sheet_id, band.version, band.replan_count, band.chapter_start, band.chapter_end) == (
            planned.sheet_id, 1, 0, 1, 3,
        )
        sheets = ArcBeatSheetRepository(session)
        sheet = sheets.load(sheets.active_for_arc(band.arc_plan_id))
        assert sheet == ArcBeatSheetData.model_validate(_resolve(planned.fixture['beat_sheet'], planned.book.refs, project_id))
        protections = [node for node in runtime.narrative.nodes_by_id.values()
                       if node.metadata.get('beat_sheet_id') == planned.sheet_id]
        assert len(protections) == len(sheet.protections) == 2
        for spec in sheet.protections:
            node, = [node for node in protections if node.payload['kind'] == spec.kind]
            assert node.node_type == 'review_constraint'
            assert (node.payload['ref'], node.payload['until_chapter'], node.payload['source']) == (
                spec.ref, spec.until_chapter, 'beat_sheet',
            )
            if spec.kind == 'must_not_know':
                assert (node.payload['observer_ref'], node.payload['fact_ref']) == (spec.observer_ref, spec.fact_ref)
        rows = list(session.scalars(select(ChapterPlan).where(ChapterPlan.project_id == project_id)
                                    .order_by(ChapterPlan.chapter_number)))
        assert [row.chapter_number for row in rows] == [1, 2, 3]
        calendar, lexicon = StoryCalendarRepository(session).get(project_id), BookLexiconRepository(session).get(project_id)
        for row, draft in zip(rows, expected.chapters, strict=True):
            assert (row.band_plan_id, row.status, row.pov_ref) == (band.id, 'planned', draft.pov_ref)
            assert (row.title, row.one_line, json.loads(row.goals_json), row.hook) == (
                draft.title, draft.one_line, draft.goals, draft.hook,
            )
            for name in ('onstage_refs', 'location_refs', 'world_bindings', 'reveal_directives',
                         'hint_directives', 'intended_changes', 'milestone_ids'):
                assert json.loads(getattr(row, f'{name}_json')) == draft.model_dump(mode='json')[name]
            assert row.expected_span_minutes == duration_to_minutes(
                calendar, lexicon, amount=draft.expected_span.amount, unit_name=draft.expected_span.unit,
            )
        assert list(session.scalars(select(BandExperiencePlan).where(BandExperiencePlan.project_id == project_id)))
        traces = list(session.scalars(select(PromptTrace).where(
            PromptTrace.project_id == project_id, PromptTrace.stage_key.in_(('plan.arc_beats', 'plan.band')),
        )))
        assert {trace.stage_key for trace in traces} == {'plan.arc_beats', 'plan.band'}
        assert len(traces) == 2
        for trace in traces:
            events = list(session.scalars(select(DecisionEvent.event_type).where(
                DecisionEvent.related_object_type == 'prompt_trace', DecisionEvent.related_object_id == trace.id,
            )))
            assert events == [DecisionEventType.LLM_REQUEST_SUCCEEDED]
    assert [stage for stage, _ in planned.client.calls] == ['plan.arc_beats', 'plan.band']


@pytest.mark.parametrize('genre', GENRES)
@pytest.mark.parametrize('builder', BUILDERS)
def test_writer_prompt_uses_world_view_and_hides_unpermitted_secrets(pipeline, genre: str, builder: str) -> None:
    planned = _start_planned_book(pipeline, genre)
    with pipeline._SessionFactory() as session:
        repo = StateRepository(session)
        plan = repo.get_chapter_plan(planned.book.project_id, 1)
        context = pipeline.retrieval_broker.build_chapter_context(repo, planned.book.project_id, plan)
        runtime = BookStateProjection(session).load_runtime_as_of(planned.book.project_id, as_of_chapter=0)
    assert context.world_view is not None
    assert context.world_view.pov_ref == plan.pov_ref
    pov = runtime.cognition_by_observer.get(('character', plan.pov_ref))
    known = pov.knowledge_by_ref if pov is not None else {}
    secrets = [fact.proposition for fact in runtime.world.facts_by_id.values()
               if fact.sensitivity_level in HIDDEN_SENSITIVITY
               and known.get(f'fact:{fact.id}') not in {'knows', 'believes', 'suspects', 'heard'}]
    # These books seed secret clocks (concealed by scope), rather than concealed actor identities.
    concealed = [node.name for node in runtime.world.nodes_by_id.values()
                 if is_concealed(runtime.world, node)
                 or (node.node_type == 'clock' and node.profile.get('scope') == 'secret'
                     and node.profile.get('owner_ref') != plan.pov_ref)]
    assert secrets and concealed
    # Poison the removed author-view surfaces as in test_writer_world_view_prompt; the real
    # retrieved POV view is retained. All five builders must ignore these old leak channels.
    context = context.model_copy(update=_old_surfaces(*secrets, *concealed))
    text = _prompt(builder, context)
    assert '【POV 世界视图】' in text
    assert context.world_view.pov_name in text
    for forbidden in [*secrets, *concealed]:
        assert forbidden not in text, (genre, builder, forbidden)


def _maintenance_results(pipeline, project_id: str, number: int) -> dict[str, dict]:
    with pipeline._SessionFactory() as session:
        rows = list(session.scalars(select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.project_id == project_id,
            PostCanonMaintenanceRun.chapter_number == number,
        )))
        assert {row.step_name for row in rows} == {'world', 'plan', 'feedback'}
        assert all(row.status == 'succeeded' for row in rows), [(row.step_name, row.last_error) for row in rows]
        return {row.step_name: json.loads(row.result_json) for row in rows}


def _write_and_tick(pipeline, monkeypatch, planned: _PlannedBook, number: int, *, relocation=False) -> dict:
    chapter = planned.fixture['planning_continuation']['chapters'][str(number)]
    body = chapter['body']
    extraction = _ExtractionClient({
        'body_sha256': body_sha256(body), **_resolved(chapter['chapter_delta_extraction'], planned.book.refs),
    })
    pipeline.llm_client = extraction
    pipeline.canon_preparation_context = replace(pipeline.canon_preparation_context, llm_client=extraction)
    written = []

    def writer(context, **_kwargs):
        assert context.chapter_number == number
        assert context.world_view is not None
        written.append(number)
        return WriterOutput(project_id=planned.book.project_id, chapter_number=number,
                            title=context.chapter_plan_title, body=body, char_count=len(body),
                            end_of_chapter_summary=chapter['summary'])

    monkeypatch.setattr(pipeline.writer, 'write_chapter', writer)
    # The established harness bypass for deliberately short drafts; no review or world gate is stubbed.
    monkeypatch.setattr(project_chapters, 'run_hard_floor', lambda **_: HardFloorResult(passed=True))
    key = 'planning_relocation' if relocation else 'planning'
    world_client = _WorldClient(planned.fixture['world_tick'][key], planned.book.refs)
    pipeline.post_canon_maintenance.world_tick = build_world_tick_service(
        llm_client=world_client, session_factory=pipeline._SessionFactory,
        artifact_store=pipeline.artifact_store, llm_available=True, admission=pipeline.canon_admission,
    )
    result = write_one_chapter(pipeline, project_id=planned.book.project_id)
    assert result.status == 'completed', result
    assert result.completed_chapters == [number]
    assert result.failed_chapters == result.paused_chapters == []
    assert written == [number]
    assert 'chapter_delta_extraction' in extraction.stage_keys
    # Reuse P3's real maintenance/order-control entry point. It is idempotent if the harness
    # already ran maintenance during acceptance; the persisted results are authoritative.
    world = _world_step(pipeline, planned.book, number, world_client)
    assert world['tick_id']
    if number == 1:
        assert world['graph_delta_ids']
    assert world['skipped_reason'] == ''
    assert world['conflict_ids'] == []
    assert world['dropped_candidate_ids'] == []
    steps = _maintenance_results(pipeline, planned.book.project_id, number)
    assert steps['plan']['conflict_ids'] == []
    assert steps['plan']['band_planning_deferred'] == []
    with pipeline._SessionFactory() as session:
        assert StateRepository(session).get_chapter_plan(planned.book.project_id, number).status == 'accepted'
        assert BookStateRepository(session).latest_canon_chapter(planned.book.project_id) == number
        deltas = BookStateRepository(session).list_graph_deltas(
            planned.book.project_id, after_chapter=number - 1, through_chapter=number,
        )
        assert [delta.id for delta in deltas if delta.source_type == 'world_engine'] == world['graph_delta_ids']
        assert any(delta.source_type == CHAPTER_SOURCE_TYPE for delta in deltas)
    return steps


def _record_call(monkeypatch, owner, name: str, calls: list[str], label: str) -> None:
    real = getattr(owner, name)

    @wraps(real)
    def wrapped(*args, **kwargs):
        calls.append(label)
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapped)


def test_three_genres_share_planning_pipeline(pipeline, monkeypatch) -> None:
    calls: list[str] = []
    for owner, name, label in (
        (ArcBeatSheetService, 'ensure_for_arc', 'ArcBeatSheetService.ensure_for_arc'),
        (BandPlanner, 'plan_band', 'BandPlanner.plan_band'),
        (FeasibilityAuditor, 'audit_plans', 'FeasibilityAuditor.audit_plans'),
        (world_view_builder, 'build_chapter_world_view', 'build_chapter_world_view'),
    ):
        _record_call(monkeypatch, owner, name, calls, label)
    sequences = {}
    for genre in GENRES:
        calls.clear()
        planned = _start_planned_book(pipeline, genre)
        for number in (1, 2, 3):
            steps = _write_and_tick(pipeline, monkeypatch, planned, number)
            assert steps['plan']['replanned_band_ids'] == []
            if number < 3:
                assert steps['plan']['planned_band_id'] is None
            else:
                assert steps['plan']['planned_band_id'] is not None
                assert steps['plan']['activated_arc_id'] is not None
        with pipeline._SessionFactory() as session:
            next_band = session.get(BandPlanRow, steps['plan']['planned_band_id'])
            assert (next_band.chapter_start, next_band.chapter_end, next_band.version) == (4, 6, 1)
            assert next_band.arc_plan_id == steps['plan']['activated_arc_id']
            plans = list(session.scalars(select(ChapterPlan).where(ChapterPlan.project_id == planned.book.project_id)
                                        .order_by(ChapterPlan.chapter_number)))
            assert [(plan.chapter_number, plan.status) for plan in plans] == [
                (1, 'accepted'), (2, 'accepted'), (3, 'accepted'), (4, 'planned'), (5, 'planned'), (6, 'planned'),
            ]
        assert [stage for stage, _ in planned.client.calls] == [
            'plan.arc_beats', 'plan.band', 'plan.arc_beats', 'plan.band',
        ]
        sequences[genre] = tuple(calls)
    first = sequences[GENRES[0]]
    assert first[:3] == ('ArcBeatSheetService.ensure_for_arc', 'BandPlanner.plan_band', 'FeasibilityAuditor.audit_plans')
    assert first.count('BandPlanner.plan_band') == 2
    assert first.count('build_chapter_world_view') >= 3
    assert first.count('FeasibilityAuditor.audit_plans') >= 7
    assert all(sequence == first for sequence in sequences.values()), sequences


@pytest.mark.parametrize('genre', GENRES)
def test_world_change_triggers_feasibility_replan(pipeline, monkeypatch, genre: str) -> None:
    planned = _start_planned_book(pipeline, genre, relocation=True)
    project_id = planned.book.project_id
    with pipeline._SessionFactory() as session:
        plans = list(session.scalars(select(ChapterPlan).where(ChapterPlan.project_id == project_id)))
        original_ids = {plan.chapter_number: plan.id for plan in plans}
        first_title = next(plan.title for plan in plans if plan.chapter_number == 1)
        old_band = session.get(BandPlanRow, planned.band_id)
        old_version, old_count = old_band.version, old_band.replan_count
        first_plan = next(plan for plan in plans if plan.chapter_number == 1)
        second_plan = next(plan for plan in plans if plan.chapter_number == 2)
        moved_ref = second_plan.pov_ref
        assert moved_ref in json.loads(second_plan.onstage_refs_json)
        assert moved_ref not in {first_plan.pov_ref, *json.loads(first_plan.onstage_refs_json)}
        extraction = _resolved(planned.fixture['planning_continuation']['chapters']['1']['chapter_delta_extraction'],
                               planned.book.refs)
        assert moved_ref not in {move['entity_ref'] for move in extraction['movements']}
        assert moved_ref not in {ref for event in extraction['events'] for ref in event['participant_refs']}
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
        original_location = resolve_world_node_location_id(runtime.world, moved_ref)
        assert original_location == json.loads(second_plan.location_refs_json)[0]
    audits = []
    real_audit = FeasibilityAuditor.audit_plans

    @wraps(real_audit)
    def audit(self, session, **kwargs):
        issues = real_audit(self, session, **kwargs)
        audits.append(([plan.chapter_number for plan in kwargs['plans']], issues))
        return issues

    monkeypatch.setattr(FeasibilityAuditor, 'audit_plans', audit)
    planned.client.replanning = True
    steps = _write_and_tick(pipeline, monkeypatch, planned, 1, relocation=True)
    assert any(numbers == [2, 3] and any(issue.code in {'location_unreachable', 'onstage_unavailable'}
                                       and issue.severity == 'error' for issue in issues)
               for numbers, issues in audits), audits
    assert steps['plan']['feasibility_issue_count'] > 0
    new_id, = steps['plan']['replanned_band_ids']
    expected = BandPlanOutput.model_validate(_resolve(planned.fixture['planning_continuation']['replan'],
                                                     planned.book.refs, project_id))
    with pipeline._SessionFactory() as session:
        old = session.get(BandPlanRow, planned.band_id)
        new = session.get(BandPlanRow, new_id)
        assert old.status == 'superseded'
        assert (new.version, new.replan_count, new.status) == (old_version + 1, old_count + 1, 'active')
        assert new.replan_count <= pipeline.policy.planning.max_replans_per_band
        first = session.get(ChapterPlan, original_ids[1])
        assert (first.status, first.band_plan_id, first.title) == ('accepted', planned.band_id, first_title)
        remaining = [session.get(ChapterPlan, original_ids[number]) for number in (2, 3)]
        for row, answer in zip(remaining, expected.chapters, strict=True):
            assert row.band_plan_id == new.id and row.status == 'planned'
            assert json.loads(row.location_refs_json) == answer.location_refs
            assert row.one_line == answer.one_line
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=1)
        pov_ref = expected.chapters[0].pov_ref
        assert pov_ref == moved_ref
        moved_to = resolve_world_node_location_id(runtime.world, pov_ref)
        assert moved_to == expected.chapters[0].location_refs[0]
        assert moved_to != original_location
        original_destination = _resolve(planned.fixture['band_plan']['chapters'][1]['location_refs'][0],
                                        planned.book.refs, project_id)
        travel = estimate_travel(runtime, BookLexiconRepository(session).get(project_id), mover_id=pov_ref,
                                 from_location_id=moved_to, to_location_id=original_destination)
        assert travel.reachable and travel.route_known and travel.minutes is not None
        assert travel.minutes > remaining[0].expected_span_minutes * (1 + pipeline.policy.review.travel_tolerance)
        sheet = ArcBeatSheetRepository(session).load(ArcBeatSheetRepository(session).active_for_arc(new.arc_plan_id))
        issues = real_audit(FeasibilityAuditor(), session, project_id=project_id, plans=remaining,
                            beat_sheet=sheet, policy=pipeline.policy)
        assert not [issue for issue in issues if issue.severity == 'error'], issues
    assert [stage for stage, _ in planned.client.calls] == ['plan.arc_beats', 'plan.band', 'plan.band']
    assert planned.client.calls[-1][1]['chapters_to_plan'] == [2, 3]
    assert planned.client.calls[-1][1]['notes']
