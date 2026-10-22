from __future__ import annotations

import json
from typing import Any

import pytest

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.extraction.chapter_delta_extractor import ChapterDeltaExtractor
from forwin.book_state.extraction.chapter_delta_schema import ChapterDeltaExtraction
from forwin.book_state.extraction.chapter_input import (
    ChapterExtractionInput, EntityRow, FulfillmentTarget, IntentRow, LocationRow, SecretRow,
    build_chapter_extraction_input,
)
from forwin.book_state.extraction.ref_check import unknown_refs
from forwin.book_state.extraction.translator import ChapterDeltaTranslator
from forwin.naming.types import EntityAdmissionPlan
from tests.test_chapter_delta_translator import _seed_world, _session_factory


INPUT = ChapterExtractionInput(
    project_id="project", chapter_number=2,
    entities=(
        EntityRow("person-1", "character", "Ari", ("A",)),
        EntityRow("blade-1", "item", "Blade", ()),
    ),
    locations=(
        LocationRow("site-1", "Gate", "region-1", "world-1"),
        LocationRow("site-2", "Tower", "region-1", "world-1"),
    ),
    calendar_units=("日",), time_aliases=(),
    fulfillment_targets=(FulfillmentTarget("goal:1", "Open the gate"),),
    intents=(IntentRow("intent-1", "person-1", "Enter", "planned"),),
    secrets=(SecretRow("fact:secret-1", "The gate hides a passage", "secret"),),
    travel_modes=("walk",),
)


def _extraction(**fields: Any) -> ChapterDeltaExtraction:
    return ChapterDeltaExtraction.model_validate({
        "body_sha256": "0" * 64, "time": {"elapsed": None, "evidence": ""}, **fields,
    })


def _paths(**fields: Any) -> list[tuple[str, str]]:
    return [(item.path, item.ref) for item in unknown_refs(_extraction(**fields), INPUT)]


def test_refs_from_the_input_tables_resolve() -> None:
    assert _paths(
        entity_changes=[
            {"ref": "person-1", "field": "location_id", "new_value": "site-2", "evidence": "e"},
            {"ref": "blade-1", "field": "holder_id", "new_value": "person-1", "evidence": "e"},
            {"new_entity": {"name": "Guild", "kind": "faction"}, "field": "status", "new_value": "x", "evidence": "e"},
        ],
        relation_changes=[
            {"source_ref": "person-1", "target_ref": "new:Bo", "edge_type": "ally_of", "op": "create", "evidence": "e"},
            {"source_ref": "site-1", "target_ref": "person-1", "edge_type": "ally_of", "op": "create", "evidence": "e"},
        ],
        knowledge_events=[
            {"observer_ref": "reader", "fact_ref": "fact:secret-1", "state": "knows", "channel": "told", "evidence": "e"},
            {
                "observer_ref": "person-1", "new_fact": {"proposition": "p", "truth_value": True, "sensitivity": "public"},
                "state": "knows", "channel": "told", "source_ref": "new:Bo",
                "touches_secret_ref": "fact:secret-1", "contradicts_ref": "fact:secret-1", "evidence": "e",
            },
        ],
        new_sites=[
            {"name": "Ford", "near_ref": "site-1", "evidence": "e"},
            {"name": "Mill", "near_ref": "new:Ford", "evidence": "e"},
            {"name": "Hut", "region_ref": "region-1", "evidence": "e"},
        ],
        movements=[{"entity_ref": "person-1", "from_ref": "site-1", "to_ref": "new:Mill", "evidence": "e"}],
        item_transfers=[
            {"item_ref": "blade-1", "from_ref": "person-1", "to_ref": "site-2", "evidence": "e"},
            {"new_item": {"name": "Key", "kind": "item"}, "from_ref": "new:Hut", "to_ref": "new:Bo", "evidence": "e"},
        ],
        events=[{
            "summary": "s", "participant_refs": ["person-1", "new:Bo"], "location_ref": "site-1",
            "significance": "minor", "visibility": "public", "evidence": "e",
        }],
        thread_beats=[{"new_thread_title": "Gate", "beat_type": "setup", "evidence": "e"}],
        intent_interactions=[{"intent_ref": "intent-1", "effect": "advanced", "evidence": "e"}],
    ) == []


@pytest.mark.parametrize("fields,expected", [
    ({"events": [{
        "summary": "s", "participant_refs": ["Ari"], "location_ref": "Gate",
        "significance": "minor", "visibility": "public", "evidence": "e",
    }]}, [("events[0].participant_refs[0]", "Ari"), ("events[0].location_ref", "Gate")]),
    ({"movements": [{"entity_ref": "person-1", "from_ref": "site-9", "to_ref": "new:Ford", "evidence": "e"}]},
     [("movements[0].to_ref", "new:Ford"), ("movements[0].from_ref", "site-9")]),
    ({"entity_changes": [{"ref": "", "field": "status", "new_value": "x", "evidence": "e"}]},
     [("entity_changes[0].ref", "")]),
    ({"entity_changes": [{"ref": "person-1", "field": "location_id", "new_value": "Tower", "evidence": "e"}]},
     [("entity_changes[0].new_value", "Tower")]),
    ({"entity_changes": [{"ref": "blade-1", "field": "holder_id", "new_value": "Ari", "evidence": "e"}]},
     [("entity_changes[0].new_value", "Ari")]),
    ({"relation_changes": [
        {"source_ref": "person-2", "target_ref": "person-1", "edge_type": "ally_of", "op": "create", "evidence": "e"},
    ]}, [("relation_changes[0].source_ref", "person-2")]),
    ({"knowledge_events": [
        {"observer_ref": "blade-1", "fact_ref": "fact:other", "state": "knows", "channel": "told",
         "source_ref": "Ari", "touches_secret_ref": "secret-1", "evidence": "e"},
        {"observer_ref": "reader", "state": "knows", "channel": "told", "evidence": "e"},
    ]}, [
        ("knowledge_events[0].observer_ref", "blade-1"), ("knowledge_events[0].fact_ref", "fact:other"),
        ("knowledge_events[0].source_ref", "Ari"), ("knowledge_events[0].touches_secret_ref", "secret-1"),
        ("knowledge_events[1].fact_ref", ""),
    ]),
    ({"new_sites": [
        {"name": "Ford", "near_ref": "new:Mill", "evidence": "e"},
        {"name": "Mill", "region_ref": "region-9", "evidence": "e"},
    ]}, [("new_sites[0].near_ref", "new:Mill"), ("new_sites[1].region_ref", "region-9")]),
    ({"item_transfers": [{"from_ref": "site-1", "to_ref": "Ari", "evidence": "e"}]},
     [("item_transfers[0].item_ref", ""), ("item_transfers[0].to_ref", "Ari")]),
    ({"thread_beats": [{"thread_ref": "plot_thread:gate", "beat_type": "advance", "evidence": "e"}]},
     [("thread_beats[0].thread_ref", "plot_thread:gate")]),
    ({"intent_interactions": [{"intent_ref": "person-1", "effect": "advanced", "evidence": "e"}]},
     [("intent_interactions[0].intent_ref", "person-1")]),
])
def test_refs_outside_the_input_tables_are_reported(fields, expected) -> None:
    assert _paths(**fields) == expected


def test_location_change_of_a_mover_is_left_to_its_movement() -> None:
    assert _paths(
        entity_changes=[{"ref": "person-1", "field": "location_id", "new_value": "Tower", "evidence": "e"}],
        movements=[{"entity_ref": "person-1", "from_ref": "", "to_ref": "site-2", "evidence": "e"}],
    ) == []


def test_an_intent_is_an_entity_ref_but_not_an_observer() -> None:
    assert _paths(
        entity_changes=[{"ref": "intent-1", "field": "status", "new_value": "due", "evidence": "e"}],
        knowledge_events=[
            {"observer_ref": "intent-1", "fact_ref": "fact:secret-1", "state": "knows", "channel": "told", "evidence": "e"},
        ],
    ) == [("knowledge_events[0].observer_ref", "intent-1")]


def test_a_new_location_named_like_a_listed_site_points_at_its_id() -> None:
    (unknown,) = unknown_refs(_extraction(movements=[
        {"entity_ref": "person-1", "from_ref": "", "to_ref": "new:Tower", "evidence": "e"},
    ]), INPUT)

    assert (unknown.ref, unknown.suggestion) == ("new:Tower", "site-2")


def test_a_name_used_as_a_ref_points_at_the_id_of_its_kind() -> None:
    refs = unknown_refs(_extraction(events=[{
        "summary": "s", "participant_refs": ["Ari", "A", "Gate", "Nobody"], "location_ref": "Gate",
        "significance": "minor", "visibility": "public", "evidence": "e",
    }]), INPUT)

    assert [(item.ref, item.suggestion) for item in refs] == [
        ("Ari", "person-1"), ("A", "person-1"), ("Gate", ""), ("Nobody", ""), ("Gate", "site-1"),
    ]


def test_extractor_corrects_an_unknown_location_before_translation() -> None:
    body = "甲沿大路走到site-2。"
    engine, Session = _session_factory()
    world = _seed_world(Session)

    def moving_to(to_ref: str) -> str:
        return json.dumps({
            "body_sha256": "untrusted", "time": {"elapsed": None, "evidence": ""},
            "movements": [{
                "entity_ref": world.hero, "from_ref": world.loc(1), "to_ref": to_ref, "evidence": "甲沿大路走到site-2",
            }],
        }, ensure_ascii=False)

    client = _Replies(moving_to("site-2"), moving_to(world.loc(2)))
    try:
        with Session() as session:
            result = ChapterDeltaExtractor(client).extract(
                extraction_input=build_chapter_extraction_input(session, project_id=world.project_id, chapter_number=1),
                chapter_title="一", chapter_body=body, chapter_summary="甲到了。",
                calendar=StoryCalendarRepository(session).get(world.project_id),
                lexicon=BookLexiconRepository(session).get(world.project_id),
            )
            translation = ChapterDeltaTranslator(session).translate(
                project_id=world.project_id, chapter_number=1, extraction=result.extraction,
                admission_plan=EntityAdmissionPlan(
                    project_id=world.project_id, chapter_number=1, candidate_fingerprint="fingerprint",
                ),
                chapter_summary="甲到了。",
            )
    finally:
        engine.dispose()

    assert len(client.calls) == 2
    feedback = client.calls[1][-1]["content"]
    assert "movements[0].to_ref" in feedback and repr(world.loc(2)) in feedback
    assert translation.issues == ()
    assert translation.delta.metadata["movements"][0]["to_id"] == world.loc(2)


class _Replies:
    def __init__(self, *replies: str) -> None:
        self.replies = iter(replies)
        self.calls: list[list[dict]] = []

    def chat(self, messages, **_kwargs):  # noqa: ANN001, ANN003
        self.calls.append(messages)
        return next(self.replies)
