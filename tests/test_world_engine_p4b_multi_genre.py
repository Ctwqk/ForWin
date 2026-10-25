"""P4b paths share real services across three Genesis books; all model answers are stubs.

Movement and quiet-turn checks run against seeded in-memory runtimes. The other
scenarios use the shared PostgreSQL pipeline, including canon admission and persisted maintenance.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from forwin.book_state import BookStateProjection, BookStateRepository
from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.lexicon import lexicon_from_payload
from forwin.book_state.query import BookStateQuery
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.book_state.story_clock import calendar_from_payload
from forwin.candidate_drafts import CandidateDraftRepository
from forwin.canon import CanonAdmissionService
from forwin.canon.entity_admission import EntityAdmissionCommitter
from forwin.checker.rules import ContinuityChecker
from forwin.generation.pipeline_core.chapter_conflicts import ChapterConflictBlocked, ChapterConflicts
from forwin.models.draft import ChapterDraft
from forwin.models.entity import Entity
from forwin.models.genesis import PromptTrace
from forwin.models.project import Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch
from forwin.review.repair import service as repair_service
from forwin.review.repair.service import RepairService
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.world_engine.actor_turns import ActorTurnRunner, build_actor_view, turn_to_candidate
from forwin.world_engine.chapter_conflicts import ChapterConflictRecorder
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_resolution import ConflictResolutionService
from forwin.world_engine.conflict_types import ConflictDecision
from forwin.world_engine.constraints import classify_impact
from forwin.world_engine.running_state import RunningStateCheck
from forwin.world_engine.drift import WorldDriftRunner
from forwin.world_engine.adjudicator import WorldAdjudicator
from forwin.world_engine.types import TickCandidate, TickContext, WorldEffects
from tests.test_chapter_delta_multi_genre import (  # noqa: F401 - shared PostgreSQL fixture
    _Book, _ExtractionClient, _commit, _committed_refs, _resolved, _review, pipeline,
)
from tests.test_planning_multi_genre import _fixture_runtime, _start_planned_book
from tests.test_world_seed_multi_genre import GENRES, load_fixture, spy
from tests.test_world_tick_multi_genre import _chapter_one, _tick_delta, _world_step
from tests.test_world_tick_service import _WorldLLM


def _quiet_turns(payload: dict, _content: str) -> dict:
    # Keep the one-day wait, expressed in a unit provided by this book's prompt.
    unit = min(payload['calendar']['units'], key=lambda item: item['minutes'])
    return {'turns': [
        {'actor_id': actor['actor_id'], 'new_intents': [], 'agenda_changes': [],
         'next_turn_in': {'amount': 1440 / unit['minutes'], 'unit': unit['name']}}
        for actor in payload['actors']
    ]}


@pytest.mark.parametrize('genre', GENRES)
def test_quiet_turns_validate_for_all_fixture_actors(genre: str) -> None:
    fixture = load_fixture(genre)
    runtime = _fixture_runtime(fixture, f'p4b-quiet-turns-{genre}')
    seed = fixture['pack']['world_seed']
    calendar = calendar_from_payload(seed['calendar'])
    lexicon = lexicon_from_payload(seed['lexicon'])
    policy = RuntimePolicy.for_profile('standard').world
    context = TickContext(project_id=runtime.project_id, chapter_number=0,
                          now=calendar.opening_minute + 2880, window_start=calendar.opening_minute, seed=15)
    views = [build_actor_view(runtime, lexicon, actor_id=node.id, now=context.now,
                              vetoes=(), boundary_refs=())
             for node in runtime.world.nodes_by_id.values() if node.node_type in ACTOR_NODE_TYPES]
    assert {view.node_type for view in views} == {'character', 'faction'}
    client = _WorldLLM(actor_turn=_quiet_turns)
    calls = ActorTurnRunner(client).run(context=context, views=views, policy=policy,
                                       calendar=calendar, lexicon=lexicon)
    assert [call.actor_id for call in calls] == [view.actor_id for view in views]
    failures = {call.actor_id: call.issues for call in calls
                if call.result is None or call.issues or call.failure_reason}
    assert not failures, failures
    assert len(client.calls) == (len(views) + policy.actor_turn_batch_size - 1) // policy.actor_turn_batch_size
    with patch.object(BookLexiconRepository, 'get', return_value=lexicon), \
            patch.object(StoryCalendarRepository, 'get', return_value=calendar):
        check = RunningStateCheck(None, runtime=runtime, lexicon=lexicon, calendar=calendar,
                                  context=context, tick_id='quiet-tick', travel_tolerance=0.2)
        for call in calls:
            candidate = turn_to_candidate(call, now=context.now, calendar=calendar, lexicon=lexicon)
            assert candidate.new_intents == [] and candidate.agenda_changes == []
            assert candidate.effects == WorldEffects()
            assert candidate.next_turn_at == {call.actor_id: context.now + 1440}
            assert check.admit(candidate) == []
            assert check.runtime.world.get_state(call.actor_id) == {
                **runtime.world.get_state(call.actor_id), 'next_turn_at': context.now + 1440,
            }
    assert len(check.accepted) == len(views)


def _blocked_move(genre: str, kind: str) -> None:
    fixture = load_fixture(genre)
    project_id = f'p4b-rule-{genre}'
    initial = _fixture_runtime(fixture, project_id)
    move = _resolved(fixture['chapter_1']['chapter_delta_extraction']['movements'][0],
                     _committed_refs(initial))
    seed = fixture['pack']['world_seed']
    target = initial.map.nodes_by_id[move['to_ref']].metadata['source_node_id']
    mode = seed['lexicon']['travel_modes'][0]['id']
    if kind == 'actor_cannot_enter':
        predicate = {'kind': kind, 'location_ref': target}
    else:
        predicate = {'kind': kind, 'mode_ids': [mode],
                     'edge_types': sorted({str(edge.edge_type) for edge in initial.map.edges_by_id.values()})}
        # The scenario removes the required equipment before Genesis commits it.
        for actor in seed['actors']:
            actor['travel_mode_ids'] = []
    seed['rules'].append({'id': 'p4b-move-rule', 'statement': 'Restricted passage.',
                          'hardness': 'hard', 'checkable': predicate})
    runtime = _fixture_runtime(fixture, project_id)
    rule = next(node for node in runtime.world.nodes_by_id.values()
                if node.node_type == 'rule' and node.profile.get('statement') == 'Restricted passage.')
    lexicon = lexicon_from_payload(seed['lexicon'])
    calendar = calendar_from_payload(seed['calendar'])
    start = calendar.opening_minute
    delta = GraphDelta(
        id='blocked-move', project_id=project_id, chapter_number=1, source_type='world_engine',
        story_time_start=start, story_time_end=start + 2880,
        node_patches=[NodePatch(node_id=move['entity_ref'], node_type='character', op='set',
            field_path='state.location_id', old_value=move['from_ref'], new_value=move['to_ref'])],
        metadata={'movements': [{'entity_id': move['entity_ref'], 'from_id': move['from_ref'],
                                  'to_id': move['to_ref'], 'depart_minute': start, 'arrive_minute': start + 2880}]},
    )
    if kind == 'actor_cannot_enter':
        assert rule.profile['checkable']['location_ref'] == move['to_ref']
    with patch.object(BookLexiconRepository, 'get', return_value=lexicon):
        verdict = BookStateReviewGate(None).review(
            ApprovedGraphDeltaSet(project_id=project_id, chapter_number=1, graph_deltas=[delta]),
            base_chapter=0, runtime=runtime,
        )
    assert not verdict.accepted, verdict
    assert ('error', 'rule_violated', f'rule:{rule.id}') in {
        (issue.severity, issue.code, issue.target_ref) for issue in verdict.issues
    }
    candidate = TickCandidate(candidate_id='blocked-move', origin='adjudication', summary='Blocked move.', effects=WorldEffects(
        movements=[{'entity_ref': move['entity_ref'], 'to_location_id': move['to_ref'],
                    'depart_at': start, 'arrive_at': start + 2880}],
    ))
    with patch.object(BookLexiconRepository, 'get', return_value=lexicon), \
            patch.object(StoryCalendarRepository, 'get', return_value=calendar):
        check = RunningStateCheck(None, runtime=runtime, lexicon=lexicon, calendar=calendar,
            context=TickContext(project_id=project_id, chapter_number=0, now=start + 2880,
                                window_start=start, seed=15), tick_id='rule-tick', travel_tolerance=0.2)
        issues = check.admit(candidate)
        assert check.accepted == []
    assert 'rule_violated' in {issue.code for issue in issues}, issues
    assert runtime.world.get_state(move['entity_ref'])['location_id'] == move['from_ref']


@pytest.mark.parametrize('genre', GENRES)
@pytest.mark.parametrize('kind', ['actor_cannot_enter', 'requires_travel_mode'])
def test_checkable_rule_blocks_move_in_three_genres(genre: str, kind: str) -> None:
    _blocked_move(genre, kind)


def _conflict_rewrite(pipeline, monkeypatch, genre: str) -> _Book:
    planned = _start_planned_book(pipeline, genre)
    book = replace(planned.book, chapter=planned.fixture['planning_continuation']['chapters']['1'])
    broken = deepcopy(book.chapter)
    scenario = planned.fixture['p4b']
    broken['body'] += '\n\n' + scenario['death_evidence']
    broken['chapter_delta_extraction'].setdefault('entity_changes', []).append({
        'ref': planned.fixture['beat_sheet']['protagonist_ref'], 'field': 'alive_status',
        'new_value': 'dead', 'evidence': scenario['death_evidence'],
    })
    bad_book = replace(book, chapter=broken)
    pipeline.llm_client = _ExtractionClient(bad_book.extraction())
    policy = pipeline.policy.model_copy(update={
        'world': pipeline.policy.world.model_copy(update={'conflict_delegate': 'human'}),
    })
    with pipeline._SessionFactory() as session:
        output, verdict = _review(pipeline, session, bad_book)
        assert verdict.verdict == 'fail'
        repo = StateRepository(session)
        plan = repo.get_chapter_plan(book.project_id, 1)
        stage = ChapterConflicts(pipeline, session, repo, StateUpdater(session), ContinuityChecker(repo),
                                policy, plan, pipeline.retrieval_broker.build_chapter_context(repo, book.project_id, plan))
        with pytest.raises(ChapterConflictBlocked) as paused:
            stage.record(output, verdict, False)
        conflict_id = paused.value.conflict_id
        assert str(paused.value) == f'world_conflict:{conflict_id}'
        assert plan.status == 'needs_review'
        candidate = CandidateDraftRepository(session).latest_for_chapter(project_id=book.project_id, chapter_number=1)
        saved_id = candidate.candidate_draft_id
        draft = session.get(ChapterDraft, saved_id)
        assert pipeline._load_writer_output_from_meta(draft.llm_raw_response).body == broken['body']
        row = session.get(WorldPlanConflictRow, conflict_id)
        assert (row.source, row.status, row.required_decider, row.impact_level) == (
            'chapter_extraction', 'open', 'human', 'major',
        )
        assert json.loads(row.proposed_change_json)['boundary']['kind'] == 'must_be_alive'
        assert BookStateRepository(session).latest_canon_chapter(book.project_id) == 0
        resolved = ConflictResolutionService(session, admission=pipeline.canon_admission, policy=policy).resolve(
            project_id=book.project_id, conflict_id=conflict_id,
            decision=ConflictDecision(option='rewrite_chapter', decided_by='human',
                                      rationale='Repair the saved draft.', decided_at=datetime.now(UTC)),
        )
        session.commit()
        assert resolved.graph_delta_ids == ()
        assert repo.get_chapter_plan(book.project_id, 1).status == 'planned'

    writes, loops = [], []
    client = _ExtractionClient(book.extraction())
    pipeline.llm_client = client

    def writer(context, **kwargs):
        writes.append(context)
        return book.draft()

    real_loop = repair_service._run_repair_loop_for_phase

    def repair_loop(execution, **kwargs):
        loops.append((kwargs['repair_phase'], kwargs['current_draft'].id, kwargs['current_output'].body))
        assert kwargs['current_review'].repair_instruction.repair_scope == 'draft'
        assert kwargs['current_review'].repair_instruction.must_fix
        return real_loop(execution, **kwargs)

    with monkeypatch.context() as local:
        local.setattr(pipeline.writer, 'write_chapter', writer)
        local.setattr(repair_service, '_run_repair_loop_for_phase', repair_loop)
        with pipeline._SessionFactory() as session:
            repo = StateRepository(session)
            plan = repo.get_chapter_plan(book.project_id, 1)
            stage = ChapterConflicts(pipeline, session, repo, StateUpdater(session), ContinuityChecker(repo),
                                    policy, plan, pipeline.retrieval_broker.build_chapter_context(repo, book.project_id, plan))
            resumed = stage.resume()
            assert resumed is not None
            output, verdict, forced = resumed
            assert output.body == book.chapter['body'] and verdict.verdict in {'pass', 'warn'}
            assert not forced
            stage.record(output, verdict, forced)
            _commit(pipeline, session, book, output, verdict, client)
            attempts = repo.list_chapter_rewrite_attempts(book.project_id, 1)
            assert len(attempts) == 1
            assert (attempts[0].repair_phase, attempts[0].phase_attempt_no, attempts[0].source_draft_id) == (
                'world_conflict_repair', 1, saved_id,
            )
            assert WorldConflictRepository(session).pending_chapter_resumes(book.project_id, chapter_number=1) == []
            latest = CandidateDraftRepository(session).latest_for_chapter(
                project_id=book.project_id, chapter_number=1)
            row = session.get(WorldPlanConflictRow, conflict_id)
            assert row.status == 'resolved'
            assert json.loads(row.result_refs_json) == [f'candidate:{latest.id}']
            runtime = BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=1)
            hero = _resolved(planned.fixture['beat_sheet']['protagonist_ref'], book.refs)
            assert runtime.world.get_state(hero)['alive_status'] == 'alive'
            assert repo.get_chapter_plan(book.project_id, 1).status == 'accepted'
            assert BookStateRepository(session).latest_canon_chapter(book.project_id) == 1
            assert stage.resume() is None
    assert loops == [('world_conflict_repair', saved_id, broken['body'])]
    assert len(writes) == 1
    return book


@pytest.mark.parametrize('genre', GENRES)
def test_chapter_conflict_rewrite_resume_in_three_genres(pipeline, monkeypatch, genre: str) -> None:
    _conflict_rewrite(pipeline, monkeypatch, genre)


def _tick_p4b(pipeline, book: _Book, genre: str, *, drift: bool, new_actor: bool) -> None:
    fixture = load_fixture(genre)
    with pipeline._SessionFactory() as session:
        before = BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=1)
        now = BookStateQuery(session).current_story_minute(book.project_id, as_of_chapter=1)
        variable, = [node for node in before.world.nodes_by_id.values() if node.node_type == 'world_variable']
        previous = before.world.get_state(variable.id)
        policy = pipeline.policy.model_copy(update={'world': pipeline.policy.world.model_copy(update={
            'drift_every_story_minutes': 1 if drift else 0,
        })})
        project = session.get(Project, book.project_id)
        store = ProjectPolicyStore(session)
        store.save(project, policy, expected_version=store.load(project).version)
        session.commit()
    actor = _resolved(fixture['p4b']['new_actor'], book.refs)
    # Place the new actor at the opening site, outside the chapter's protected cast.
    effects = {'new_actors': [actor], 'events': [{
        'summary': actor['summary'], 'participant_refs': [f"actor:new:{actor['key']}"],
        'location_id': actor['location_id'], 'visibility': 'public', 'at': now,
    }]}
    nominated = _resolved(fixture['world_tick']['adjudicate']['outcomes'][0]['intent_id'], book.refs)
    settled = {}

    def adjudicate(prompt, _content):
        outcomes = []
        for intent in prompt['intents']:
            introduces_actor = new_actor and intent['id'] == nominated
            # Resolving unrelated intents inherits their declared major/critical impact,
            # even with no effects, and holds them for review instead of a quiet tick.
            result = 'resolved' if introduces_actor else 'disrupted'
            outcomes.append({'intent_id': intent['id'], 'result': result,
                'summary': actor['summary'] if introduces_actor else 'The attempt did not proceed.',
                'effects': effects if introduces_actor else {}})
            settled[intent['id']] = result
        return {'outcomes': outcomes}

    def drift_answer(prompt, _content):
        assert drift
        assert [item['id'] for item in prompt['variables']] == [variable.id]
        return {'changes': [{'variable_id': variable.id, 'new_level': previous['level'] + 1,
            'trend': 'rising', 'summary': fixture['p4b']['drift_summary'], 'events': [{
                'summary': fixture['p4b']['drift_summary'], 'participant_refs': variable.profile['holder_refs'],
                'location_id': '', 'visibility': 'public', 'at': now,
            }]}]}

    client = _WorldLLM(adjudicate=adjudicate, actor_turn=_quiet_turns, drift=drift_answer)
    step = _world_step(pipeline, book, 1, client)
    assert step['skipped_reason'] == ''
    assert step['dropped_candidate_ids'] == [], step
    assert step['postponed_candidate_ids'] == [], step
    assert step['conflict_ids'] == [], step
    with pipeline._SessionFactory() as session:
        delta = _tick_delta(session, book.project_id, 1)
        after = BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=1)
        assert step['graph_delta_ids'] == [delta.id]
        assert settled
        assert {ref: after.world.get_state(ref)['status'] for ref in settled} == settled
        assert not delta.cognition_patches or all(str(p.observer_type) != 'reader' for p in delta.cognition_patches)
        for patches in (delta.node_patches, delta.edge_patches, delta.fact_patches, delta.cognition_patches):
            for item in patches:
                if str(item.op) != 'create':
                    assert 'old_value' in item.model_fields_set and item.field_path
        stages = [stage for stage, _content in client.calls]
        assert stages.count('world.adjudicate') == 1
        assert 'review.world_conflict' not in stages
        traces = list(session.scalars(select(PromptTrace).where(PromptTrace.project_id == book.project_id)))
        for stage in set(stages):
            assert any(trace.stage_key == stage for trace in traces), stage
        if drift:
            assert stages.count('world.drift') == 1
            assert delta.metadata['candidate_impacts'][f'drift:{variable.id}'] == 'moderate'
            state = after.world.get_state(variable.id)
            assert (state['level'], state['last_drift_at'], state['trend']) == (previous['level'] + 1, now, 'rising')
            assert any(node.node_type == 'event' and node.name == fixture['p4b']['drift_summary']
                       for node in after.world.nodes_by_id.values())
            assert {node.id for node in after.world.nodes_by_id.values() if node.node_type == 'intent'} == {
                node.id for node in before.world.nodes_by_id.values() if node.node_type == 'intent'
            }
        else:
            assert 'world.drift' not in stages
        if new_actor:
            assert settled[nominated] == 'resolved'
            assert delta.metadata['candidate_impacts'][f'adjudication:{nominated}'] == 'moderate'
            node, = [node for node in after.world.nodes_by_id.values() if node.name == actor['name']]
            state = after.world.get_state(node.id)
            assert (node.node_type, state['actor_tier'], state['alive_status'], state['location_id']) == (
                'character', 'minor', 'alive', actor['location_id'],
            )
            entity = session.get(Entity, node.id)
            assert entity is not None and entity.is_active and entity.name == actor['name']
            assert entity.created_at_chapter == 1 and node.profile['personality_loadout']
            assert 'actor:new:' not in delta.model_dump_json()
            assert classify_impact(before, TickCandidate(candidate_id='arrival', origin='adjudication', summary=actor['summary'],
                effects=WorldEffects.model_validate(effects))) == 'moderate'
            events = [node for node in after.world.nodes_by_id.values() if node.node_type == 'event'
                      and node.id not in before.world.nodes_by_id]
            assert any(node.id in after.world.get_state(event.id).get('participant_ids', []) for event in events)
    # Persisted maintenance is idempotent, including model calls and actor creation.
    calls = list(client.calls)
    assert _world_step(pipeline, book, 1, client) == step
    assert client.calls == calls


@pytest.mark.parametrize('genre', GENRES)
def test_drift_step_in_three_genres(pipeline, genre: str) -> None:
    _tick_p4b(pipeline, _chapter_one(pipeline, genre), genre, drift=True, new_actor=False)


@pytest.mark.parametrize('genre', GENRES)
def test_new_actor_in_three_genres(pipeline, genre: str) -> None:
    _tick_p4b(pipeline, _chapter_one(pipeline, genre), genre, drift=False, new_actor=True)


def test_three_genres_share_p4b_paths(pipeline, monkeypatch) -> None:
    sequences = {}
    for genre in GENRES:
        calls = []
        with ExitStack() as spies:
            for owner, name in (
                (ChapterConflictRecorder, 'record'), (ChapterConflicts, 'resume'),
                (RepairService, 'repair_world_conflict'), (WorldAdjudicator, 'adjudicate'),
                (WorldDriftRunner, 'drift'), (CanonAdmissionService, 'commit_world_tick'),
            ):
                label = f'{owner.__name__}.{name}'
                spies.enter_context(patch.object(owner, name, spy(label, getattr(owner, name), calls)))
            book = _conflict_rewrite(pipeline, monkeypatch, genre)
            with patch.object(BookStateReviewGate, 'review',
                              spy('BookStateReviewGate.review', BookStateReviewGate.review, calls)):
                for kind in ('actor_cannot_enter', 'requires_travel_mode'):
                    _blocked_move(genre, kind)
            with patch.object(EntityAdmissionCommitter, 'apply', spy(
                'EntityAdmissionCommitter.apply', EntityAdmissionCommitter.apply, calls,
            )):
                _tick_p4b(pipeline, book, genre, drift=True, new_actor=True)
        sequences[genre] = calls
    expected = [
        'ChapterConflictRecorder.record', 'ChapterConflicts.resume', 'RepairService.repair_world_conflict',
        'ChapterConflictRecorder.record', 'ChapterConflicts.resume',
        'BookStateReviewGate.review', 'BookStateReviewGate.review',
        'BookStateReviewGate.review', 'BookStateReviewGate.review',
        'WorldAdjudicator.adjudicate', 'WorldDriftRunner.drift', 'CanonAdmissionService.commit_world_tick',
        'EntityAdmissionCommitter.apply',
    ]
    assert sequences == {genre: expected for genre in GENRES}
