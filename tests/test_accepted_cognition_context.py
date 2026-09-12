"""Accepted cognition must survive real context and sent-message boundaries."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.book_state import BookStateRepository
from forwin.book_state.query import BookStateQuery
from forwin.context.assembler_core.assembler import ChapterContextAssembler
from forwin.context.providers.state_provider import StateContextProvider
from forwin.context.request import ContextDraft, ContextRequest
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.book_state import (
    CognitionOverlay,
    CognitionPatch,
    GraphDelta,
    WorldNode,
)
from forwin.protocol.context import RepairContract, WritingPack
from forwin.protocol.scene import ScenePlan, SceneOutput
from forwin.protocol.writer import WriterOutput
from forwin.retrieval.broker_core.broker import RetrievalBroker
from forwin.retrieval.source_identity import CanonReadBaseline
from forwin.review.context_builder import build_review_context_pack
from forwin.review.llm_webnovel import LLMWebNovelReviewer
from forwin.review.webnovel import WebNovelExperienceReviewer
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.writer.chapter_writer import ChapterWriter
from tests.postgres import postgres_test_url
from tests.test_context_provider_chain import _FakeRepo

SECRET = "field:A:secret"
LATER = "N章后半A当面告诉B秘密"


@pytest.fixture
def cognition_session():
    engine = get_engine(postgres_test_url())
    init_db(engine)
    factory = get_session_factory(engine)
    try:
        with factory.begin() as session:
            ProjectPolicyStore(session).initialize(
                Project(
                    id="project-1",
                    title="认知",
                    premise="查案",
                    genre="悬疑",
                    setting_summary="城",
                ),
                RuntimePolicy.for_profile("standard"),
            )
            repo = BookStateRepository(session)
            for actor in ("A", "B"):
                repo.create_world_node(
                    WorldNode(
                        id=actor,
                        project_id="project-1",
                        node_type="character",
                        name=actor,
                        state={"secret": "作者秘密，不能自动成为角色知识"},
                    )
                )
            for kind, actor, refs in [
                ("character", "A", [SECRET]),
                ("character", "B", ["node:A"]),
                ("reader", "reader", ["fact:clue"]),
            ]:
                repo.upsert_cognition_overlay(
                    CognitionOverlay(
                        id=f"cog-{actor}",
                        project_id="project-1",
                        observer_type=kind,
                        observer_id=actor,
                        as_of_chapter=0,
                        visible_refs=refs,
                    )
                )
            repo.append_graph_delta(
                GraphDelta(
                    id="learn-at-1",
                    project_id="project-1",
                    chapter_number=1,
                    cognition_patches=[
                        CognitionPatch(
                            observer_type="character",
                            observer_id="A",
                            op="append",
                            field_path="confirmed_refs",
                            new_value=SECRET,
                            evidence_refs=["event:confession"],
                        ),
                        CognitionPatch(
                            observer_type="character",
                            observer_id="B",
                            op="merge",
                            field_path="field_overrides",
                            new_value={"field:A:occupation": "误信A是医生"},
                            evidence_refs=["event:lie"],
                        ),
                        CognitionPatch(
                            observer_type="reader",
                            observer_id="reader",
                            op="merge",
                            field_path="false_facts",
                            new_value={
                                "rumor": {
                                    "id": "rumor",
                                    "project_id": "project-1",
                                    "fact_type": "rumor",
                                    "summary": "误信钟楼无人",
                                }
                            },
                            evidence_refs=["event:rumor"],
                        ),
                    ],
                )
            )
            repo.append_graph_delta(
                GraphDelta(
                    id="future-reveal",
                    project_id="project-1",
                    chapter_number=2,
                    cognition_patches=[
                        CognitionPatch(
                            observer_type="character",
                            observer_id="B",
                            op="append",
                            field_path="confirmed_refs",
                            new_value=SECRET,
                            evidence_refs=["event:later"],
                        ),
                    ],
                )
            )
            yield session
    finally:
        engine.dispose()


def accepted(session):
    baseline = CanonReadBaseline.capture(session, "project-1", as_of_chapter=1)
    return BookStateQuery(session, baseline=baseline).accepted_cognition(
        "project-1", as_of_chapter=1
    )


def test_query_reads_live_refs_overrides_false_beliefs_and_evidence(cognition_session):
    snapshots = accepted(cognition_session)
    by_id = {item.observer_id: item for item in snapshots}
    assert by_id["A"].ref_states[SECRET] == "confirmed"
    assert by_id["B"].ref_states[SECRET] == "unknown"
    assert by_id["B"].ref_states["node:A"] == "known"
    assert by_id["reader"].ref_states["fact:clue"] == "known"
    assert by_id["reader"].ref_states[SECRET] == "unknown"
    assert by_id["B"].field_overrides == {"field:A:occupation": "误信A是医生"}
    assert by_id["reader"].false_facts["rumor"]["summary"] == "误信钟楼无人"
    assert by_id["A"].evidence_by_ref[SECRET] == ["event:confession"]
    assert by_id["A"].as_of_chapter == 1
    assert by_id["A"].sources_by_ref[SECRET][0].chapter_number == 1
    assert by_id["A"].sources_by_ref[SECRET][0].delta_id == "learn-at-1"
    assert not by_id["B"].sources_by_ref.get(SECRET)
    assert not by_id["reader"].sources_by_ref.get(
        "fact:clue"
    )  # snapshot height is not acquisition time
    assert "作者秘密" not in json.dumps(
        [item.model_dump() for item in snapshots], ensure_ascii=False
    )


def make_context(session):
    plan = SimpleNamespace(
        chapter_number=2, title="查案", one_line="先查线索再告知B", goals_json="[]"
    )
    baseline = CanonReadBaseline.capture(session, "project-1", as_of_chapter=1)
    draft = ContextDraft(
        data={
            "goals": [],
            "band_world_contract": SimpleNamespace(
                band_exit_reader_state="阶段末读者知道全部", required_hints=[]
            ),
            "chapter_world_delta_intent": ChapterWorldDeltaIntent(
                intent_id="intent-2",
                project_id="project-1",
                chapter_number=2,
                expected_observer_state_changes={"B": LATER},
            ),
        }
    )
    StateContextProvider().contribute(
        ContextRequest(
            project_id="project-1",
            chapter_plan=plan,
            repo=_FakeRepo(session),
            session=session,
            baseline=baseline,
        ),
        draft,
    )
    pack = ChapterContextAssembler()._build_pack(
        project_id="project-1", chapter_plan=plan, draft=draft
    )
    return pack.model_copy(update={"canon_read_baseline": baseline})


def test_assembler_and_broker_merge_do_not_promote_plans(cognition_session):
    pack = make_context(cognition_session)
    assert pack.reader_cognition_state != "阶段末读者知道全部"
    assert pack.observer_visibility_states == {}
    assert pack.planned_reader_cognition_state == "阶段末读者知道全部"
    assert {x.observer_id for x in pack.accepted_cognition} == {"A", "B", "reader"}
    broker = RetrievalBroker()
    world = broker.build_world_model_pack(
        _FakeRepo(cognition_session),
        "project-1",
        2,
        "writing",
        baseline=pack.canon_read_baseline,
        include_secondary=False,
    )
    assert world.accepted_cognition == pack.accepted_cognition
    merged = broker._merge_writer_world_model_pack(pack, world)
    assert merged.accepted_cognition == world.accepted_cognition
    stale = pack.model_copy(
        update={
            "character_cognition_states": {"B": LATER},
            "observer_visibility_states": {"B": LATER},
            "reader_cognition_state": LATER,
        }
    )
    empty = broker._merge_writer_world_model_pack(
        stale, WritingPack(project_id="project-1")
    )
    assert (
        empty.character_cognition_states == {}
        and empty.observer_visibility_states == {}
    )
    assert not empty.reader_cognition_state and not empty.accepted_cognition


@pytest.mark.parametrize(
    "layout", ["single", "preview", "breakdown", "scene", "stitch", "repair"]
)
def test_actual_writer_requests_separate_accepted_planned_and_provisional(
    cognition_session, layout
):
    pack = make_context(cognition_session)
    sent = []

    def chat(messages, **kwargs):
        sent.append(messages)
        return (
            '{"scenes":[{"scene_no":1,"objective":"查线索"},{"scene_no":2,"objective":"告知B"}]}'
            if layout == "breakdown"
            else "<<FORWIN_TITLE>>\n查案\n<<FORWIN_BODY>>\n"
            + "A交出纸条，B读完后才得知秘密。" * 200
            + "\n<<FORWIN_SUMMARY>>\nB读到秘密。"
        )

    writer = ChapterWriter(SimpleNamespace(chat=chat))
    prior = SceneOutput(
        scene_no=1,
        scene_objective="告知B",
        text="A当面告诉B秘密，B现在听到了。",
        micro_summary="B得知秘密",
    )
    if layout == "single":
        writer._write_single_chapter(pack)
    elif layout == "preview":
        writer.write_preview_chapter(pack)
    elif layout == "repair":
        pack.repair_contract = RepairContract(must_fix=["B在告知前保持未知"])
        writer._write_single_chapter(pack, trace_stage_key="chapter_repair")
    elif layout == "breakdown":
        writer._plan_scenes(pack)
    elif layout == "scene":
        writer._generate_scene(
            pack, ScenePlan(scene_no=2, objective="B回应告知"), previous_scenes=[prior]
        )
    else:
        writer._stitch_scenes(pack, [prior])
    assert sent
    text = "\n".join(m["content"] for m in sent[0])
    section = text.split("【已接纳认知（N−1）】", 1)[1].split(
        "【作者计划（尚未发生）】", 1
    )[0]
    assert LATER not in section and "阶段末读者知道全部" not in section
    snapshots = json.loads(section.split("\n", 2)[2].strip())
    assert {x["observer_id"]: x for x in snapshots}["B"]["ref_states"][
        SECRET
    ] == "unknown"
    assert LATER in text.split("【作者计划（尚未发生）】", 1)[1]
    assert "仅在本章已写正文明确发生获知事件之后" in text
    if layout in ("scene", "stitch"):
        assert prior.text in text


@pytest.mark.parametrize("normalize", [False, True])
def test_reviewer_messages_separate_accepted_and_planned(cognition_session, normalize):
    pack = make_context(cognition_session)
    review = (
        WebNovelExperienceReviewer()._normalize_context(pack)
        if normalize
        else build_review_context_pack(repo=_FakeRepo(cognition_session), context=pack)
    )
    sent = []

    def chat(messages, **kwargs):
        sent.append(messages)
        return '{"verdict":"pass","issues":[]}'

    reviewer = LLMWebNovelReviewer(llm_client=SimpleNamespace(chat=chat))
    output = WriterOutput(
        chapter_number=2,
        title="查案",
        body="先查线索，后A当面告诉B秘密。",
        end_of_chapter_summary="告知",
    )
    reviewer.review(review, output)
    messages = sent[0]
    text = "\n".join(m["content"] for m in messages)
    sent_payload = json.loads(
        next(m["content"] for m in messages if m["role"] == "user").split(
            "审查数据：", 1
        )[1]
    )
    accepted_payload = sent_payload["world"]["reveal_context"]["accepted_cognition"]
    assert {x["observer_id"]: x for x in accepted_payload}["B"]["ref_states"][
        SECRET
    ] == "unknown"
    assert LATER not in json.dumps(accepted_payload, ensure_ascii=False)
    assert LATER in text and "仅在本章已写正文明确发生获知事件之后" in text


def test_differing_observer_snapshot_heights_do_not_skip_older_observer_patch(
    cognition_session,
):
    # A's chapter-zero snapshot still needs chapter-one replay, even if B has a
    # newer independent snapshot. These are real repository inputs to D1.
    BookStateRepository(cognition_session).upsert_cognition_overlay(
        CognitionOverlay(
            id="newer-B",
            project_id="project-1",
            observer_type="character",
            observer_id="B",
            as_of_chapter=1,
        )
    )
    views = (
        BookStateQuery(cognition_session)
        .runtime("project-1", as_of_chapter=1)
        .cognition_by_observer
    )
    assert views[("character", "A")].get_belief(SECRET) == "confirmed"


def test_replay_uses_initial_observer_cutoffs_and_keeps_snapshot_state(
    cognition_session,
):
    repo = BookStateRepository(cognition_session)
    repo.upsert_cognition_overlay(
        CognitionOverlay(
            id="newer-B",
            project_id="project-1",
            observer_type="character",
            observer_id="B",
            as_of_chapter=1,
            field_overrides={"field:A:occupation": "已纠正为教师"},
        )
    )
    # The observer has no snapshot. Both deltas must replay; creation during the
    # first patch must not accidentally establish a new replay cutoff at N−1.
    for chapter, ref in [(0, "fact:first"), (1, "fact:second")]:
        repo.append_graph_delta(
            GraphDelta(
                id=f"new-observer-{chapter}",
                project_id="project-1",
                chapter_number=chapter,
                cognition_patches=[
                    CognitionPatch(
                        observer_type="character",
                        observer_id="C",
                        op="append",
                        field_path="confirmed_refs",
                        new_value=ref,
                    ),
                ],
            )
        )
    runtime = BookStateQuery(cognition_session).runtime("project-1", as_of_chapter=1)
    assert (
        runtime.cognition_by_observer[("character", "C")].get_belief("fact:first")
        == "confirmed"
    )
    assert (
        runtime.cognition_by_observer[("character", "C")].get_belief("fact:second")
        == "confirmed"
    )
    assert runtime.cognition_by_observer[("character", "B")].field_overrides == {
        "field:A:occupation": "已纠正为教师"
    }


def test_dict_patch_evidence_uses_concrete_refs_without_fabricating_timing(
    cognition_session,
):
    by_id = {item.observer_id: item for item in accepted(cognition_session)}
    assert by_id["B"].evidence_by_ref["field:A:occupation"] == ["event:lie"]
    assert by_id["reader"].evidence_by_ref["fact:rumor"] == ["event:rumor"]
    assert by_id["B"].sources_by_ref["field:A:occupation"][0].chapter_number == 1
    assert not any(
        ref.startswith("{") for item in by_id.values() for ref in item.ref_states
    )


def test_reviewer_reloads_serialized_cognition_at_its_read_baseline(cognition_session):
    from forwin.protocol.context import ChapterContextPack

    pack = make_context(cognition_session)
    serialized = ChapterContextPack.model_validate_json(pack.model_dump_json())
    assert serialized.canon_read_baseline is None
    # A transported snapshot cannot certify this claim; the repository still
    # records B unknown at N−1 and only confirms B in chapter N.
    next(
        item for item in serialized.accepted_cognition if item.observer_id == "B"
    ).ref_states[SECRET] = "confirmed"
    review = build_review_context_pack(
        repo=_FakeRepo(cognition_session), context=serialized
    )
    assert (
        next(
            item for item in review.accepted_cognition if item.observer_id == "B"
        ).ref_states[SECRET]
        == "unknown"
    )


def test_reviewer_rejects_changed_retained_canon_baseline(cognition_session):
    from forwin.retrieval.source_identity import CanonBaselineChanged

    pack = make_context(cognition_session)
    cognition_session.get(Project, "project-1").book_revision += 1
    cognition_session.flush()
    with pytest.raises(CanonBaselineChanged):
        build_review_context_pack(repo=_FakeRepo(cognition_session), context=pack)


@pytest.mark.parametrize(
    "current_evidence",
    [
        {"field:A:occupation": ["event:correction"]},
        {},
        {"field:A:occupation": []},
    ],
    ids=["corrected-support", "cleared-support", "explicit-empty-support"],
)
def test_corrected_snapshot_does_not_reacquire_historical_dict_evidence(
    cognition_session,
    current_evidence,
):
    BookStateRepository(cognition_session).upsert_cognition_overlay(
        CognitionOverlay(
            id="corrected-B",
            project_id="project-1",
            observer_type="character",
            observer_id="B",
            as_of_chapter=1,
            field_overrides={"field:A:occupation": "已纠正为教师"},
            evidence_by_ref=current_evidence,
        )
    )
    snapshot = next(
        item for item in accepted(cognition_session) if item.observer_id == "B"
    )
    assert snapshot.field_overrides == {"field:A:occupation": "已纠正为教师"}
    assert snapshot.evidence_by_ref == current_evidence
    # Superseded support still belongs to its historical source event, never to
    # the current corrected belief or a snapshot that explicitly cleared it.
    history = snapshot.sources_by_ref["field:A:occupation"]
    assert len(history) == 1
    assert history[0].delta_id == "learn-at-1"
    assert history[0].chapter_number == 1
    assert history[0].evidence_refs == ["event:lie"]
