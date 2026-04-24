from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select

from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.world_v4 import KnowledgeGapRow, WorldCompileRunV4Row
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
    ObserverType,
    ReaderExperienceDelta,
    RevealEvent,
    VisibilityState,
    WorldCompileRequest,
    WorldDelta,
    WorldLine,
)
from forwin.protocol.writer import WriterOutput
from forwin.retrieval.broker import RetrievalBroker
from forwin.reviewer_v4.gate import V4ReviewGate
from forwin.state.repo import StateRepository
from forwin.world_model_v4.compiler import WorldModelCompiler
from forwin.world_model_v4.repository import WorldModelRepository


@dataclass
class DeterministicV4RuntimeResult:
    completed_chapters: list[int] = field(default_factory=list)
    v4_compile_runs: list[str] = field(default_factory=list)
    blocked_compile_runs: list[str] = field(default_factory=list)
    final_gap_status: str = ""
    writer_pack_hidden_truth_count: int = 0
    writer_pack_must_not_reveal: list[str] = field(default_factory=list)
    review_pack_hidden_truth_count: int = 0
    compiler_pack_accepted_delta_ids: list[str] = field(default_factory=list)
    first_attempt_gate_status: str = ""
    repair_instruction_scope: str = ""
    repair_instruction_must_not_reveal: list[str] = field(default_factory=list)
    final_attempt_gate_status: str = ""


class DeterministicChapterWriter:
    def write_chapter(self, chapter_number: int) -> WriterOutput:
        bodies = {
            21: "主角团建立殖民地防线，远方母星线仍未进入台前。",
            22: "能源危机中，通讯延迟和干扰开始出现。",
            23: "防线修复后，通讯台传出乱码和父亲旧部呼号。",
            25: "残缺求援传来，主角终于知道母星遭遇围困的一角。",
            28: "返回母星后，主角确认父亲确实被围，并看见旧时代坐标。",
        }
        return WriterOutput(
            project_id="",
            chapter_number=chapter_number,
            title=f"第{chapter_number}章",
            body=bodies[chapter_number],
            char_count=len(bodies[chapter_number]),
            end_of_chapter_summary=bodies[chapter_number],
        )


def _observer(observer_type: ObserverType, observer_id: str, visibility: VisibilityState) -> GapObserverState:
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
            "protagonist": _observer(ObserverType.CHARACTER, "protagonist", protagonist),
        },
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
        narrative_chapter=chapter,
        source=DeltaSource(source_type=source_type),
        source_refs=[f"fixture:chapter:{chapter}"],
    )


def _compile(
    session,
    project_id: str,
    chapter: int,
    *,
    deltas: list[WorldDelta],
    gap: KnowledgeGap,
    reveal: RevealEvent | None = None,
    reader_exp: ReaderExperienceDelta | None = None,
) -> None:
    extracted = ExtractedWorldChangeSet(
        project_id=project_id,
        chapter_number=chapter,
        world_deltas=deltas,
        knowledge_gap_updates=[gap],
        reveal_events=[reveal] if reveal else [],
        reader_experience_deltas=[reader_exp] if reader_exp else [],
    )
    approved = ApprovedWorldChangeSet.from_extracted(
        extracted,
        approved_by=["deterministic-runtime-fixture"],
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


def run_deterministic_v4_runtime_fixture(
    *,
    chapters: list[int],
    fixture_name: str,
) -> DeterministicV4RuntimeResult:
    assert fixture_name == "arc2_homeworld_crisis"
    engine = get_engine(":memory:")
    init_db(engine)
    Session = get_session_factory(engine)
    writer = DeterministicChapterWriter()
    result = DeterministicV4RuntimeResult()

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
        contracts = WorldContractRepository(session)
        contracts.save_arc_contract(
            ArcWorldContract(
                contract_id="arc2",
                project_id=project_id,
                arc_id="arc2",
                arc_number=2,
                primary_world_line_ids=["line_colony_defense"],
                hidden_world_line_ids=["line_homeworld_siege"],
                major_gap_ids=["gap_homeworld_siege"],
                reveal_ladder=[
                    RevealLadderStep(gap_id="gap_homeworld_siege", chapter_hint=22, from_state="hidden", to_state="hinted", method="通讯延迟"),
                    RevealLadderStep(gap_id="gap_homeworld_siege", chapter_hint=25, from_state="hinted", to_state="partially_revealed", method="残缺求援"),
                    RevealLadderStep(gap_id="gap_homeworld_siege", chapter_hint=28, from_state="partially_revealed", to_state="confirmed", method="返回母星确认"),
                ],
            )
        )
        contracts.save_chapter_intent(
            ChapterWorldDeltaIntent(
                intent_id="ch23",
                project_id=project_id,
                chapter_number=23,
                hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
                must_not_reveal=["father_sieged"],
                expected_observer_state_changes={"protagonist": "unknown -> suspected"},
            )
        )

        for chapter in chapters:
            writer.write_chapter(chapter)
            if chapter == 21:
                _compile(
                    session,
                    project_id,
                    chapter,
                    deltas=[_delta(project_id, "delta_ch21_siege_begins", 21, DeltaKind.OFFSCREEN, "幕后：敌军开始围困母星", DeltaSourceType.FACTION_ACTION)],
                    gap=_gap(project_id, status=GapStatus.OPEN, reader=VisibilityState.HIDDEN, protagonist=VisibilityState.UNKNOWN),
                )
            elif chapter == 22:
                _compile(
                    session,
                    project_id,
                    chapter,
                    deltas=[_delta(project_id, "delta_ch22_comm_delay", 22, DeltaKind.HINT, "通讯延迟与干扰增强", DeltaSourceType.INFORMATION_SPREAD)],
                    gap=_gap(project_id, status=GapStatus.HINTED, reader=VisibilityState.HINTED, protagonist=VisibilityState.SUSPECTED),
                )
            elif chapter == 23:
                state_repo = StateRepository(session)
                broker = RetrievalBroker()
                writer_pack = broker.build_world_model_pack(
                    state_repo,
                    project_id,
                    23,
                    "writing",
                )
                review_pack = broker.build_world_model_pack(
                    state_repo,
                    project_id,
                    23,
                    "review",
                )
                result.writer_pack_hidden_truth_count = len(
                    writer_pack.hidden_objective_truths
                )
                result.writer_pack_must_not_reveal = list(writer_pack.must_not_reveal)
                result.review_pack_hidden_truth_count = len(
                    review_pack.hidden_objective_truths
                )
                intent = ChapterWorldDeltaIntent(
                    intent_id="ch23",
                    project_id=project_id,
                    chapter_number=23,
                    hint_delta_intents=["乱码通讯", "父亲旧部呼号"],
                    must_not_reveal=["father_sieged"],
                    expected_observer_state_changes={"protagonist": "unknown -> suspected"},
                )
                blocked_verdict = V4ReviewGate().review(
                    ExtractedWorldChangeSet(project_id=project_id, chapter_number=23),
                    chapter_intent=intent,
                    chapter_body="父亲在母星被围，主角立刻返航救父。",
                )
                result.first_attempt_gate_status = (
                    "approved" if blocked_verdict.passed else "blocked"
                )
                if blocked_verdict.repair_instruction is not None:
                    result.repair_instruction_scope = (
                        blocked_verdict.repair_instruction.repair_scope
                    )
                    result.repair_instruction_must_not_reveal = list(
                        blocked_verdict.repair_instruction.must_not_reveal
                    )
                blocked = WorldModelCompiler(session).compile_gate_verdict(
                    project_id=project_id,
                    chapter_number=23,
                    verdict=blocked_verdict,
                    compiler_run_id="compile-23-early-reveal-blocked",
                )
                if not blocked.committed:
                    result.blocked_compile_runs.append(blocked.compiler_run_id)
                repaired_extracted = ExtractedWorldChangeSet(
                    project_id=project_id,
                    chapter_number=chapter,
                    world_deltas=[
                        _delta(
                            project_id,
                            "delta_ch23_callsign_hint",
                            23,
                            DeltaKind.HINT,
                            "乱码通讯与父亲旧部呼号",
                            DeltaSourceType.INFORMATION_SPREAD,
                        )
                    ],
                    knowledge_gap_updates=[
                        _gap(
                            project_id,
                            status=GapStatus.HINTED,
                            reader=VisibilityState.HINTED,
                            protagonist=VisibilityState.SUSPECTED,
                        )
                    ],
                    reader_experience_deltas=[
                        ReaderExperienceDelta(
                            reader_experience_delta_id="reader_exp_ch23",
                            project_id=project_id,
                            chapter_number=23,
                            cognition_transition="hidden -> hinted",
                            payoff_type="short_term_hint",
                            reward_tags=["mystery"],
                        )
                    ],
                )
                repaired_verdict = V4ReviewGate().review(
                    repaired_extracted,
                    chapter_intent=intent,
                    chapter_body="防线修复后，通讯台传出乱码和父亲旧部呼号。",
                )
                result.final_attempt_gate_status = (
                    "approved" if repaired_verdict.passed else "blocked"
                )
                WorldModelCompiler(session).compile_gate_verdict(
                    project_id=project_id,
                    chapter_number=23,
                    verdict=repaired_verdict,
                    compiler_run_id="compile-23",
                )
                compiler_pack = broker.build_world_model_pack(
                    state_repo,
                    project_id,
                    23,
                    "compiler",
                )
                result.compiler_pack_accepted_delta_ids = [
                    delta_id
                    for delta_id in compiler_pack.accepted_delta_ids
                    if delta_id.startswith("delta_ch23_")
                ]
            elif chapter == 25:
                _compile(
                    session,
                    project_id,
                    chapter,
                    deltas=[_delta(project_id, "delta_ch25_distress_call", 25, DeltaKind.REVEAL, "残缺求援显示母星遭遇围困", DeltaSourceType.INFORMATION_SPREAD)],
                    gap=_gap(project_id, status=GapStatus.PARTIALLY_CLOSED, reader=VisibilityState.PARTIALLY_REVEALED, protagonist=VisibilityState.PARTIALLY_KNOWN),
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
                )
            elif chapter == 28:
                _compile(
                    session,
                    project_id,
                    chapter,
                    deltas=[_delta(project_id, "delta_ch28_homeworld_confirmed", 28, DeltaKind.REVEAL, "返回母星确认父亲被围", DeltaSourceType.CHARACTER_ACTION)],
                    gap=_gap(project_id, status=GapStatus.CLOSED, reader=VisibilityState.CONFIRMED, protagonist=VisibilityState.CONFIRMED),
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
            result.completed_chapters.append(chapter)

        runs = list(
            session.execute(
                select(WorldCompileRunV4Row).where(WorldCompileRunV4Row.project_id == project_id)
            )
            .scalars()
            .all()
        )
        result.v4_compile_runs = [
            run.compiler_run_id
            for run in runs
            if run.committed and run.compiler_run_id.startswith("compile-")
        ]
        gap_row = session.execute(
            select(KnowledgeGapRow).where(
                KnowledgeGapRow.project_id == project_id,
                KnowledgeGapRow.gap_id == "gap_homeworld_siege",
            )
        ).scalar_one()
        result.final_gap_status = gap_row.status
    return result


def test_runtime_loop_uses_v4_contracts_review_gate_and_compiler_for_arc2() -> None:
    result = run_deterministic_v4_runtime_fixture(
        chapters=[21, 22, 23, 25, 28],
        fixture_name="arc2_homeworld_crisis",
    )

    assert result.completed_chapters == [21, 22, 23, 25, 28]
    assert result.v4_compile_runs == [
        "compile-21",
        "compile-22",
        "compile-23",
        "compile-25",
        "compile-28",
    ]
    assert result.blocked_compile_runs == ["compile-23-early-reveal-blocked"]
    assert result.final_gap_status == "closed"
    assert result.writer_pack_hidden_truth_count == 0
    assert result.writer_pack_must_not_reveal == ["father_sieged"]
    assert result.review_pack_hidden_truth_count == 1
    assert result.compiler_pack_accepted_delta_ids == ["delta_ch23_callsign_hint"]
    assert result.first_attempt_gate_status == "blocked"
    assert result.repair_instruction_scope == "world_model"
    assert result.repair_instruction_must_not_reveal == ["father_sieged"]
    assert result.final_attempt_gate_status == "approved"
