from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from typing import get_args

import pytest
from pydantic import ValidationError

from forwin.book_state.extraction.chapter_delta_schema import (
    ChapterDeltaExtraction,
    ChapterTime,
    Duration,
    EntityChange,
    EntityMention,
    EventItem,
    KnowledgeEventItem,
    RelationChange,
    ThreadBeat,
)
from forwin.book_state.extraction.chapter_input import build_chapter_extraction_input
from forwin.book_state.extraction.evidence import (
    dropped_ratio,
    evidence_in_body,
    normalize_for_evidence,
    validate_evidence,
)
from forwin.naming.entity_registrar import writer_output_admission_fingerprint
from forwin.protocol.writer import WriterOutput


BODY = "甲走进\n  城门。"


def _extraction(**kwargs):
    return ChapterDeltaExtraction(
        body_sha256=hashlib.sha256(BODY.encode()).hexdigest(),
        time=ChapterTime(elapsed=None, evidence="甲走进城门"),
        **kwargs,
    )


@pytest.mark.parametrize("model,payload", [
    (KnowledgeEventItem, {"observer_ref": "reader", "state": "unknown", "channel": "told"}),
    (KnowledgeEventItem, {"observer_ref": "reader", "state": "knows", "channel": "unknown"}),
    (RelationChange, {"source_ref": "a", "target_ref": "b", "edge_type": "unknown", "op": "create"}),
    (ThreadBeat, {"beat_type": "unknown"}),
    (EventItem, {"summary": "event", "significance": "unknown", "visibility": "public"}),
])
def test_schema_rejects_unknown_enum_tokens(model, payload) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "evidence": BODY})


def test_schema_forbids_extra_fields_and_defaults_lists() -> None:
    with pytest.raises(ValidationError):
        _extraction(unexpected=True)
    with pytest.raises(ValidationError):
        Duration(amount=1, unit="day", unexpected=True)
    first, second = _extraction(), _extraction()
    first.entity_changes.append(EntityChange(ref="a", field="state.status", new_value="active", evidence=BODY))
    assert second.entity_changes == []
    assert second.events == []
    assert second.schema_version == "v1"


def test_evidence_normalization_ignores_whitespace_only() -> None:
    assert evidence_in_body("甲走进城门", BODY)
    assert not evidence_in_body("甲进了城门", BODY)
    assert not evidence_in_body(" \n\t", BODY)
    assert normalize_for_evidence(" Ａ\nＢ\tＣ ") == "ABC"
    assert not evidence_in_body("甲走进城门!", BODY)


def test_validate_evidence_drops_and_reports() -> None:
    before = _extraction(
        movements=[
            {"entity_ref": "a", "from_ref": "b", "to_ref": "c", "evidence": "甲走进城门"},
            {"entity_ref": "a", "from_ref": "c", "to_ref": "d", "evidence": "甲进了城门"},
        ],
        plan_fulfillment=[{"target_id": "goal:1", "status": "missing"}],
    )
    after, issues = validate_evidence(before, BODY)
    assert len(after.movements) == 1
    assert len(before.movements) == 2
    assert after.plan_fulfillment == before.plan_fulfillment
    assert [(issue.severity, issue.code, issue.path) for issue in issues] == [
        ("warning", "evidence_not_in_body", "movements[1]"),
    ]
    assert dropped_ratio(issues, before) == pytest.approx(1 / 3)
    assert dropped_ratio([], _extraction()) == 0.0


@pytest.mark.parametrize("field,payload", [
    ("entity_changes", {"ref": "a", "field": "state.status", "new_value": "active"}),
    ("relation_changes", {"source_ref": "a", "target_ref": "b", "edge_type": "ally_of", "op": "create"}),
    ("knowledge_events", {"observer_ref": "reader", "state": "knows", "channel": "told"}),
    ("movements", {"entity_ref": "a", "from_ref": "b", "to_ref": "c"}),
    ("new_sites", {"name": "site"}),
    ("item_transfers", {"from_ref": "a", "to_ref": "b"}),
    ("events", {"summary": "event", "significance": "minor", "visibility": "public"}),
    ("thread_beats", {"beat_type": "setup"}),
    ("intent_interactions", {"intent_ref": "intent", "effect": "advanced"}),
    ("plan_fulfillment", {"target_id": "goal:1", "status": "done"}),
    ("plan_fulfillment", {"target_id": "goal:1", "status": "missing"}),
    ("entity_mentions", {"name": "name", "kind": "character"}),
])
def test_validate_evidence_checks_every_item_list(field, payload) -> None:
    before = _extraction(**{field: [{**payload, "evidence": "unsupported"}]})
    after, issues = validate_evidence(before, BODY)
    assert getattr(after, field) == []
    assert [(issue.code, issue.path) for issue in issues] == [("evidence_not_in_body", f"{field}[0]")]
    assert dropped_ratio(issues, before) == 1.0


@pytest.mark.parametrize("evidence,code", [("", "elapsed_without_evidence"), (" \n", "elapsed_without_evidence"), ("unsupported", "evidence_not_in_body")])
def test_validate_evidence_clears_unsupported_elapsed(evidence, code) -> None:
    before = _extraction()
    before.time = ChapterTime(elapsed=Duration(amount=1, unit="day"), evidence=evidence)
    after, issues = validate_evidence(before, BODY)
    assert after.time.elapsed is None
    assert before.time.elapsed.amount == 1
    assert [(issue.code, issue.path) for issue in issues] == [(code, "time")]
    assert dropped_ratio(issues, before) == 0.0


@pytest.mark.parametrize("evidence", ["", " \n", "unsupported"])
def test_validate_evidence_skips_time_warning_without_elapsed(evidence) -> None:
    before = _extraction()
    before.time = ChapterTime(elapsed=None, evidence=evidence)

    after, issues = validate_evidence(before, BODY)

    assert after.time == before.time
    assert issues == []


def test_writer_output_without_chapter_delta_preserves_legacy_fingerprint() -> None:
    output = WriterOutput(chapter_number=1, title="title", body=BODY, end_of_chapter_summary="summary")
    legacy_payload = output.model_dump(
        mode="json",
        exclude={
            "generation_meta": True,
            "draft_blob_path": True,
            "scene_outputs": {"__all__": {"text_blob_path"}},
        },
    )
    legacy_payload.pop("chapter_delta")
    legacy_json = json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    legacy_fingerprint = hashlib.sha256(legacy_json.encode("utf-8")).hexdigest()
    restored = WriterOutput.model_validate_json(legacy_json)

    assert restored.chapter_delta is None
    assert writer_output_admission_fingerprint(restored) == legacy_fingerprint
    assert writer_output_admission_fingerprint(output) == legacy_fingerprint


def test_writer_output_chapter_delta_round_trips_and_changes_fingerprint() -> None:
    output = WriterOutput(chapter_number=1, title="title", body=BODY, end_of_chapter_summary="summary")
    assert output.chapter_delta is None
    original = writer_output_admission_fingerprint(output)
    output.chapter_delta = _extraction()
    assert writer_output_admission_fingerprint(output) != original
    restored = WriterOutput.model_validate_json(output.model_dump_json())
    assert restored.chapter_delta == output.chapter_delta
    assert writer_output_admission_fingerprint(restored) == writer_output_admission_fingerprint(output)
    restored.chapter_delta.entity_mentions.append(
        EntityMention(name="name", kind="character", evidence=BODY),
    )
    assert writer_output_admission_fingerprint(restored) != writer_output_admission_fingerprint(output)


@pytest.mark.parametrize("module", ["forwin.protocol.writer", "forwin.book_state.extraction.chapter_delta_schema", "forwin.book_state.knowledge"])
def test_contract_imports_in_fresh_interpreter(module) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}; from forwin.protocol.writer import WriterOutput; WriterOutput.model_json_schema()"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_build_input_collects_entities_locations_targets_secrets() -> None:
    from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter, seed_ref_id
    from forwin.map.models import MapNodeRow
    from forwin.models.base import get_engine, get_session_factory
    from forwin.models.genesis import BookGenesisRevision
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.models.project import ArcPlanVersion, ChapterPlan
    from forwin.runtime.policy import RuntimePolicy
    from forwin.state.updater import StateUpdater
    from tests.helpers.genesis_seed import seeded_map_atlas, seeded_story_engine, seeded_world_seed
    from tests.postgres import postgres_test_url

    engine = get_engine(postgres_test_url("chapter_delta_input"))
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            seed = seeded_world_seed()
            seed["lexicon"] = {
                "time_unit_aliases": [{"name": "tick", "minutes": 1}],
                "travel_modes": [{"id": "walk", "name": "walking", "speed_multiplier": 1, "allowed_edge_types": ["road"]}],
            }
            pack = {"world": {"story_engine": seeded_story_engine(), "map_atlas": seeded_map_atlas()}, "world_seed": seed}
            project = StateUpdater(session).create_project(
                title="extraction", premise="extraction", genre="test",
                runtime_policy=RuntimePolicy.for_profile("standard"), creation_status="genesis_ready",
            )
            revision = BookGenesisRevision(project_id=project.id, pack_json=json.dumps(pack))
            session.add(revision)
            for node in pack["world"]["map_atlas"]["nodes"]:
                session.add(MapNodeRow(id=node["id"], project_id=project.id, node_type="site", name=node["name"], metadata_json=json.dumps({"source_node_id": node["id"]})))
            session.flush()
            WorldSeedCommitter().commit(session=session, project=project, revision=revision, pack=pack)
            arc = ArcPlanVersion(project_id=project.id, arc_synopsis="arc")
            session.add(arc)
            session.flush()
            session.add_all([
                NarrativeObligationRow(id="owed", project_id=project.id, summary="Resolve debt", payoff_test="Debt is paid"),
                NarrativeObligationRow(id="legacy", project_id=project.id, summary="Legacy obligation"),
                ChapterPlan(project_id=project.id, arc_plan_id=arc.id, chapter_number=1,
                            goals_json=json.dumps(["Reach the site"]),
                            task_contract_json=json.dumps({"obligations_to_resolve": ["owed"]}),
                            experience_plan_json=json.dumps({"planned_reward_tags": ["mystery"], "obligations_to_resolve": ["legacy", "owed"]})),
            ])
            session.flush()
            result = build_chapter_extraction_input(session, project_id=project.id, chapter_number=1)
            hero_id = seed_ref_id(project.id, "character", "cast-hero")
            assert any(row.id == hero_id and row.name == "陶然" and row.kind == "character" for row in result.entities)
            assert {row.id for row in result.locations} == {"node-old-city", "node-ruins"}
            assert {row.target_id: row.description for row in result.fulfillment_targets} == {
                "goal:1": "Reach the site", "obligation:owed": "Debt is paid",
                "obligation:legacy": "Legacy obligation", "reward:mystery": "mystery (planned)",
                "reward:power": "power (optional)", "reward:social": "social (optional)",
                "reward:justice": "justice (optional)", "reward:emotion": "emotion (optional)",
            }
            # Planned rewards come first, then every other reward tag.
            assert [row.target_id for row in result.fulfillment_targets if row.target_id.startswith("reward:")] == [
                "reward:mystery", "reward:power", "reward:social", "reward:justice", "reward:emotion",
            ]
            assert [(row.fact_ref, row.proposition, row.sensitivity) for row in result.secrets] == [
                (f"fact:{seed_ref_id(project.id, 'fact', 'fact-seal')}", "遗迹封印正在松动", "secret"),
            ]
            assert result.calendar_units == ("日",)
            assert result.time_aliases == ("tick",)
            assert result.travel_modes == ("walk",)
            assert [(row.owner_id, row.summary, row.status) for row in result.intents] == [(hero_id, "潜入地下遗迹", "planned")]
    finally:
        engine.dispose()


def test_input_without_planned_rewards_still_offers_every_reward_target() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from forwin.models.base import Base
    from forwin.models.project import ArcPlanVersion, ChapterPlan
    from forwin.protocol.experience import RewardTag
    from forwin.runtime.policy import RuntimePolicy
    from forwin.state.updater import StateUpdater

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        project = StateUpdater(session).create_project(
            title="rewards", premise="p", genre="g", runtime_policy=RuntimePolicy.for_profile("standard"),
        )
        arc = ArcPlanVersion(project_id=project.id, arc_synopsis="arc")
        session.add(arc)
        session.flush()
        session.add(ChapterPlan(
            project_id=project.id, arc_plan_id=arc.id, chapter_number=1,
            goals_json=json.dumps(["Reach the site"]), experience_plan_json="{}",
        ))
        session.flush()

        result = build_chapter_extraction_input(session, project_id=project.id, chapter_number=1)

        assert [(row.target_id, row.description) for row in result.fulfillment_targets] == [
            ("goal:1", "Reach the site"),
            *((f"reward:{tag}", f"{tag} (optional)") for tag in get_args(RewardTag)),
        ]
    finally:
        session.close()
        engine.dispose()


def test_input_builder_filters_prior_runtime_without_database(monkeypatch) -> None:
    from types import SimpleNamespace

    from forwin.book_state.extraction import chapter_input
    from forwin.book_state.lexicon import BookLexicon
    from forwin.book_state.map_graph import MapGraph
    from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
    from forwin.protocol.book_state import FactNode, MapNode, WorldNode

    nodes = [
        WorldNode(id=kind, project_id="p", node_type=kind, name=kind, aliases=["alias"])
        for kind in ("character", "faction", "organization", "group", "item", "location", "rule")
    ]
    nodes.extend([
        WorldNode(id="inactive", project_id="p", node_type="character", is_active=False),
        WorldNode(id="intent-due", project_id="p", node_type="intent",
                  profile={"owner_ref": "character", "summary": "Act now"}, state={"status": "planned"}),
        WorldNode(id="intent-resolved", project_id="p", node_type="intent", state={"status": "resolved"}),
    ])
    world = ObjectiveWorldGraph(nodes=nodes, facts=[
        FactNode(id=level, project_id="p", proposition=level, sensitivity_level=level)
        for level in ("public", "hidden", "secret")
    ])
    # Runtime state can differ from the node's creation-time state after replay.
    world.states_by_node_id["intent-due"] = {"status": "due"}
    runtime = BookStateRuntime(project_id="p", as_of_chapter=2, world=world, map_graph=MapGraph(nodes=[
        MapNode(id="map", project_id="p", node_type="site", name="Site", region_id="region", subworld_id="subworld"),
    ]))
    seen = []

    def load_runtime(_self, project_id, *, as_of_chapter):
        seen.append((project_id, as_of_chapter))
        return runtime

    monkeypatch.setattr(chapter_input.BookStateProjection, "load_runtime_as_of", load_runtime)
    monkeypatch.setattr(chapter_input.StoryCalendarRepository, "get", lambda _self, _project: None)
    monkeypatch.setattr(chapter_input.BookLexiconRepository, "get", lambda _self, _project: BookLexicon())
    monkeypatch.setattr(chapter_input.NarrativeObligationRepository, "list_active_for_context", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chapter_input.NarrativeObligationRepository, "list_planned_for_chapter", lambda *_args, **_kwargs: [])
    result = build_chapter_extraction_input(SimpleNamespace(scalar=lambda _query: None), project_id="p", chapter_number=3)
    assert seen == [("p", 2)]
    assert {row.kind for row in result.entities} == {"character", "faction", "organization", "group", "item", "location"}
    assert all(row.aliases == ("alias",) for row in result.entities)
    assert "inactive" not in {row.id for row in result.entities}
    assert [(row.id, row.owner_id, row.summary, row.status) for row in result.intents] == [("intent-due", "character", "Act now", "due")]
    assert [(row.id, row.region_id, row.subworld_id) for row in result.locations] == [("map", "region", "subworld")]
    assert {row.fact_ref for row in result.secrets} == {"fact:hidden", "fact:secret"}
    assert result.fulfillment_targets == result.calendar_units == result.time_aliases == result.travel_modes == ()


@pytest.mark.parametrize("with_plan", [True, False])
def test_input_offers_unplanned_active_and_current_chapter_obligations(with_plan) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from forwin.models.base import Base
    from forwin.models.narrative_obligation import NarrativeObligationRow
    from forwin.models.project import ArcPlanVersion, ChapterPlan, Project

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            project = Project(title="Obligations", premise="test", genre="test")
            session.add(project)
            session.flush()
            session.add_all([
                NarrativeObligationRow(id=ref, project_id=project.id, status=status,
                                       origin_chapter_number=origin, deadline_chapter=deadline,
                                       summary=f"Resolve {ref}", payoff_test="" if ref == "active" else f"Pay off {ref}")
                for ref, status, origin, deadline in (
                    ("active", "active", 1, 3), ("listed", "active", 1, 4),
                    ("current", "active", 3, 3), ("planned", "planned", 3, 5),
                    ("future", "active", 4, 5), ("resolved", "resolved", 1, 3),
                    ("old-planned", "planned", 1, 3),
                )
            ])
            if with_plan:
                arc = ArcPlanVersion(project_id=project.id, arc_synopsis="arc")
                session.add(arc)
                session.flush()
                session.add(ChapterPlan(
                    project_id=project.id, arc_plan_id=arc.id, chapter_number=3,
                    task_contract_json=json.dumps({"obligations_to_resolve": ["listed", "listed"]}),
                    experience_plan_json=json.dumps({"obligations_to_resolve": ["listed"]}),
                ))
            session.flush()

            result = build_chapter_extraction_input(session, project_id=project.id, chapter_number=3)

            targets = [target for target in result.fulfillment_targets if target.target_id.startswith("obligation:")]
            assert {target.target_id for target in targets} == {
                "obligation:active", "obligation:listed", "obligation:current", "obligation:planned",
            }
            assert len(targets) == 4
            assert next(target.description for target in targets if target.target_id == "obligation:active") == "Resolve active"
            if with_plan:
                assert targets[0].target_id == "obligation:listed"
    finally:
        engine.dispose()
