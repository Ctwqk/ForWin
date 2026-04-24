from __future__ import annotations

import json

from sqlalchemy import func, select

from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.world_v4 import KnowledgeGapRow, WorldCompileRunV4Row, WorldDeltaRow
from forwin.planning.world_contracts import (
    ArcWorldContract,
    ChapterWorldDeltaIntent,
    RevealLadderStep,
    WorldContractRepository,
)
from forwin.protocol.world_v4 import (
    ApprovedWorldChangeSet,
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    ExtractedWorldChangeSet,
    GapObserverState,
    GapStatus,
    KnowledgeGap,
    KnowledgeUpdateEvent,
    KnowledgeUpdateType,
    ObserverType,
    ReaderExperienceDelta,
    RevealEvent,
    VisibilityState,
    WorldCompileRequest,
    WorldDelta,
    WorldLine,
)
from forwin.reviewer_v4.gate import V4ReviewGate
from forwin.world_model_v4.compiler import WorldModelCompiler
from forwin.world_model_v4.export import WorldModelExporter
from forwin.world_model_v4.repository import WorldModelRepository


def _observer(
    observer_type: ObserverType,
    observer_id: str,
    visibility: VisibilityState,
) -> GapObserverState:
    return GapObserverState(
        observer_type=observer_type,
        observer_id=observer_id,
        visibility=visibility,
    )


def _gap(
    project_id: str,
    *,
    status: GapStatus,
    reader: VisibilityState,
    protagonist: VisibilityState,
) -> KnowledgeGap:
    return KnowledgeGap(
        gap_id="gap_homeworld_siege",
        project_id=project_id,
        objective_truth="Day 30 父亲在母星被围",
        happened_at_story_time="Day 30",
        related_world_line_id="line_homeworld_siege",
        status=status,
        observer_states={
            "reader": _observer(ObserverType.READER, "reader", reader),
            "protagonist": _observer(
                ObserverType.CHARACTER,
                "protagonist",
                protagonist,
            ),
        },
        fairness_requirements=["第22章通讯延迟", "第23章乱码与旧部呼号"],
    )


def _delta(
    project_id: str,
    delta_id: str,
    chapter: int,
    kind: DeltaKind,
    summary: str,
    source_type: DeltaSourceType,
) -> WorldDelta:
    return WorldDelta(
        delta_id=delta_id,
        project_id=project_id,
        world_line_id="line_homeworld_siege",
        delta_kind=kind,
        summary=summary,
        objective_story_time=f"Day {chapter + 9}",
        narrative_chapter=chapter,
        source=DeltaSource(source_type=source_type),
    )


def _knowledge_update(
    project_id: str,
    chapter: int,
    observer_type: ObserverType,
    observer_id: str,
    from_state: VisibilityState,
    to_state: VisibilityState,
) -> KnowledgeUpdateEvent:
    return KnowledgeUpdateEvent(
        update_event_id=f"knowledge_{chapter}_{observer_id}_{to_state.value}",
        project_id=project_id,
        update_type=KnowledgeUpdateType.HINT
        if to_state in {VisibilityState.HINTED, VisibilityState.SUSPECTED}
        else KnowledgeUpdateType.REVEAL,
        observer_type=observer_type,
        observer_id=observer_id,
        related_gap_id="gap_homeworld_siege",
        from_state=from_state,
        to_state=to_state,
        chapter_number=chapter,
    )


def _compile(
    session,
    project_id: str,
    chapter: int,
    *,
    deltas: list[WorldDelta],
    gap: KnowledgeGap,
    reveal: RevealEvent | None = None,
    updates: list[KnowledgeUpdateEvent] | None = None,
    reader_exp: ReaderExperienceDelta | None = None,
) -> None:
    extracted = ExtractedWorldChangeSet(
        project_id=project_id,
        chapter_number=chapter,
        world_deltas=deltas,
        knowledge_gap_updates=[gap],
        reveal_events=[reveal] if reveal else [],
        knowledge_update_events=updates or [],
        reader_experience_deltas=[reader_exp] if reader_exp else [],
    )
    approved = ApprovedWorldChangeSet.from_extracted(
        extracted,
        approved_by=["e2e-fixture"],
        review_verdict_id=f"review-{chapter}",
    )
    WorldModelCompiler(session).compile(
        WorldCompileRequest(
            project_id=project_id,
            chapter_number=chapter,
            approved_changes=approved,
            review_verdict_id=f"review-{chapter}",
            compiler_run_id=f"compile-{chapter}",
        )
    )


def _gap_state(session, project_id: str) -> dict:
    row = session.execute(
        select(KnowledgeGapRow).where(
            KnowledgeGapRow.project_id == project_id,
            KnowledgeGapRow.gap_id == "gap_homeworld_siege",
        )
    ).scalar_one()
    return {
        "status": row.status,
        "observer_states": json.loads(row.observer_states_json),
    }


def test_arc2_homeworld_crisis_runs_through_v4_ledgers_review_and_export() -> None:
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)

    with Session.begin() as session:
        project = Project(
            title="Arc 2：新星殖民与母星危机",
            premise="台前殖民地站稳，幕后父亲母星被围。",
            genre="科幻",
            setting_summary="殖民地、母星、远程通讯阵列",
        )
        session.add(project)
        session.flush()
        project_id = project.id
        repo = WorldModelRepository(session)
        repo.create_world_line(
            WorldLine(
                world_line_id="line_colony_defense",
                project_id=project_id,
                line_type="primary_visible_line",
                title="殖民地防线",
                is_visible_onstage=True,
            )
        )
        repo.create_world_line(
            WorldLine(
                world_line_id="line_homeworld_siege",
                project_id=project_id,
                line_type="hidden_parallel_line",
                title="母星围困线",
                objective_state_summary="Day 30 父亲在母星被围",
            )
        )
        WorldContractRepository(session).save_arc_contract(
            ArcWorldContract(
                contract_id="arc2",
                project_id=project_id,
                arc_id="arc2",
                arc_number=2,
                primary_world_line_ids=["line_colony_defense"],
                hidden_world_line_ids=["line_homeworld_siege"],
                major_gap_ids=["gap_homeworld_siege"],
                reveal_ladder=[
                    RevealLadderStep(
                        gap_id="gap_homeworld_siege",
                        chapter_hint=22,
                        from_state="hidden",
                        to_state="hinted",
                        method="通讯延迟",
                    ),
                    RevealLadderStep(
                        gap_id="gap_homeworld_siege",
                        chapter_hint=25,
                        from_state="hinted",
                        to_state="partially_revealed",
                        method="残缺求援",
                    ),
                    RevealLadderStep(
                        gap_id="gap_homeworld_siege",
                        chapter_hint=28,
                        from_state="partially_revealed",
                        to_state="confirmed",
                        method="返回母星确认",
                    ),
                ],
            )
        )

        _compile(
            session,
            project_id,
            21,
            deltas=[
                _delta(
                    project_id,
                    "delta_ch21_siege_begins",
                    21,
                    DeltaKind.OFFSCREEN,
                    "幕后：敌军开始围困母星",
                    DeltaSourceType.FACTION_ACTION,
                )
            ],
            gap=_gap(
                project_id,
                status=GapStatus.OPEN,
                reader=VisibilityState.HIDDEN,
                protagonist=VisibilityState.UNKNOWN,
            ),
            reader_exp=ReaderExperienceDelta(
                reader_experience_delta_id="reader_exp_ch21",
                project_id=project_id,
                chapter_number=21,
                cognition_transition="hidden stays hidden",
                payoff_type="setup",
            ),
        )
        ch21 = _gap_state(session, project_id)
        assert ch21["status"] == "open"
        assert ch21["observer_states"]["reader"]["visibility"] == "hidden"
        assert ch21["observer_states"]["protagonist"]["visibility"] == "unknown"

        _compile(
            session,
            project_id,
            22,
            deltas=[
                _delta(
                    project_id,
                    "delta_ch22_comm_delay",
                    22,
                    DeltaKind.HINT,
                    "通讯延迟与干扰增强",
                    DeltaSourceType.INFORMATION_SPREAD,
                )
            ],
            gap=_gap(
                project_id,
                status=GapStatus.HINTED,
                reader=VisibilityState.HINTED,
                protagonist=VisibilityState.SUSPECTED,
            ),
            updates=[
                _knowledge_update(
                    project_id,
                    22,
                    ObserverType.READER,
                    "reader",
                    VisibilityState.HIDDEN,
                    VisibilityState.HINTED,
                ),
                _knowledge_update(
                    project_id,
                    22,
                    ObserverType.CHARACTER,
                    "protagonist",
                    VisibilityState.UNKNOWN,
                    VisibilityState.SUSPECTED,
                ),
            ],
        )
        ch22 = _gap_state(session, project_id)
        assert ch22["observer_states"]["reader"]["visibility"] == "hinted"
        assert ch22["observer_states"]["protagonist"]["visibility"] == "suspected"

        ch23_intent = ChapterWorldDeltaIntent(
            intent_id="ch23",
            project_id=project_id,
            chapter_number=23,
            hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
            must_not_reveal=["father_sieged"],
        )
        bad_reveal = V4ReviewGate().review(
            ExtractedWorldChangeSet(project_id=project_id, chapter_number=23),
            chapter_intent=ch23_intent,
            chapter_body="父亲在母星被围，主角立刻返航救父。",
        )
        assert bad_reveal.passed is False
        blocked = WorldModelCompiler(session).compile_gate_verdict(
            project_id=project_id,
            chapter_number=23,
            verdict=bad_reveal,
            compiler_run_id="compile-ch23-blocked",
        )
        assert blocked.committed is False

        _compile(
            session,
            project_id,
            23,
            deltas=[
                _delta(
                    project_id,
                    "delta_ch23_callsign_hint",
                    23,
                    DeltaKind.HINT,
                    "乱码通讯与父亲旧部呼号",
                    DeltaSourceType.INFORMATION_SPREAD,
                )
            ],
            gap=_gap(
                project_id,
                status=GapStatus.HINTED,
                reader=VisibilityState.HINTED,
                protagonist=VisibilityState.SUSPECTED,
            ),
            reader_exp=ReaderExperienceDelta(
                reader_experience_delta_id="reader_exp_ch23",
                project_id=project_id,
                chapter_number=23,
                cognition_transition="hidden -> hinted",
                payoff_type="short_term_hint",
                reward_tags=["mystery"],
            ),
        )
        ch23 = _gap_state(session, project_id)
        assert ch23["status"] == "hinted"

        _compile(
            session,
            project_id,
            25,
            deltas=[
                _delta(
                    project_id,
                    "delta_ch25_distress_call",
                    25,
                    DeltaKind.REVEAL,
                    "残缺求援显示母星遭遇围困",
                    DeltaSourceType.INFORMATION_SPREAD,
                )
            ],
            gap=_gap(
                project_id,
                status=GapStatus.PARTIALLY_CLOSED,
                reader=VisibilityState.PARTIALLY_REVEALED,
                protagonist=VisibilityState.PARTIALLY_KNOWN,
            ),
            reveal=RevealEvent(
                reveal_event_id="reveal_ch25_distress",
                project_id=project_id,
                related_gap_id="gap_homeworld_siege",
                reveal_to_reader=True,
                reveal_to_characters=["protagonist"],
                reveal_method="残缺求援",
                from_state=VisibilityState.HINTED,
                to_state=VisibilityState.PARTIALLY_REVEALED,
            ),
            updates=[
                _knowledge_update(
                    project_id,
                    25,
                    ObserverType.READER,
                    "reader",
                    VisibilityState.HINTED,
                    VisibilityState.PARTIALLY_REVEALED,
                ),
                _knowledge_update(
                    project_id,
                    25,
                    ObserverType.CHARACTER,
                    "protagonist",
                    VisibilityState.SUSPECTED,
                    VisibilityState.PARTIALLY_KNOWN,
                ),
            ],
        )
        ch25 = _gap_state(session, project_id)
        assert ch25["status"] == "partially_closed"
        assert ch25["observer_states"]["reader"]["visibility"] == "partially_revealed"
        assert ch25["observer_states"]["protagonist"]["visibility"] == "partially_known"

        _compile(
            session,
            project_id,
            28,
            deltas=[
                _delta(
                    project_id,
                    "delta_ch28_homeworld_confirmed",
                    28,
                    DeltaKind.REVEAL,
                    "返回母星确认父亲被围",
                    DeltaSourceType.CHARACTER_ACTION,
                )
            ],
            gap=_gap(
                project_id,
                status=GapStatus.CLOSED,
                reader=VisibilityState.CONFIRMED,
                protagonist=VisibilityState.CONFIRMED,
            ),
            reveal=RevealEvent(
                reveal_event_id="reveal_ch28_confirmed",
                project_id=project_id,
                related_gap_id="gap_homeworld_siege",
                reveal_to_reader=True,
                reveal_to_characters=["protagonist"],
                reveal_method="返回母星确认",
                from_state=VisibilityState.PARTIALLY_REVEALED,
                to_state=VisibilityState.CONFIRMED,
            ),
            reader_exp=ReaderExperienceDelta(
                reader_experience_delta_id="reader_exp_ch28",
                project_id=project_id,
                chapter_number=28,
                cognition_transition="partially_revealed -> confirmed",
                payoff_type="long_term_payoff",
                reward_tags=["mystery", "payoff"],
                next_desire="旧时代坐标真正价值是什么？",
            ),
        )
        ch28 = _gap_state(session, project_id)
        assert ch28["status"] == "closed"
        assert ch28["observer_states"]["reader"]["visibility"] == "confirmed"
        assert ch28["observer_states"]["protagonist"]["visibility"] == "confirmed"

        delta_count = session.scalar(
            select(func.count()).select_from(WorldDeltaRow).where(
                WorldDeltaRow.project_id == project_id
            )
        )
        compile_run_count = session.scalar(
            select(func.count()).select_from(WorldCompileRunV4Row).where(
                WorldCompileRunV4Row.project_id == project_id
            )
        )
        pages = WorldModelExporter(session).export_project(project_id, as_of_chapter=28)

    assert delta_count == 5
    assert compile_run_count == 6
    assert any(page.title == "Reveal Ladder" and "残缺求援" in page.body for page in pages)
    assert any(
        page.title == "Short / Medium / Long Term Delight"
        and "旧时代坐标真正价值是什么？" in page.body
        for page in pages
    )
