from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.context.providers.genesis_provider import GenesisContextProvider
from forwin.context.request import ContextDraft, ContextRequest
from forwin.genesis.normalize import _normalize_story_engine_payload
from forwin.protocol.book_state import WORLD_EDGE_TYPES_BY_FAMILY


def normalize(payload):
    return _normalize_story_engine_payload(None, payload=payload, fallback={})


def test_story_engine_normalizer_assigns_ids():
    result = normalize({
        "core_cast": [{"name": "A"}, {"name": "B"}],
        "opposition": [{"name": "C"}],
        "factions": [{"name": "D"}],
    })
    assert [item["id"] for item in result["core_cast"]] == ["cast-1", "cast-2"]
    assert result["opposition"][0]["id"] == "opp-1"
    assert result["factions"][0]["id"] == "faction-1"


def test_story_engine_ids_are_unique_across_collections_and_stable():
    result = normalize({
        "core_cast": [{"id": "shared"}, {"id": "shared"}, {"id": "shared-2"}],
        "factions": [{"id": "shared"}, {"id": "shared"}],
        "opposition": [{"id": "shared"}, {"id": "shared"}],
    })
    assert [item["id"] for item in result["core_cast"]] == ["shared", "shared-2", "shared-2-2"]
    assert [item["id"] for item in result["factions"]] == ["shared-3", "shared-4"]
    assert [item["id"] for item in result["opposition"]] == ["shared-5", "shared-6"]
    assert normalize(result) == result


def test_relationship_axes_are_structured_and_resolved_by_name():
    result = normalize({
        "core_cast": [{"name": "A"}, {"name": "faction-1"}],
        "factions": [{"name": "D"}],
        "opposition": [{"name": "C"}],
        "relationship_axes": [
            {"source": "A", "target": "faction-1", "edge_type": "rival_of"},
            {"source": "C", "target": "D", "edge_type": "frenemy", "visibility": "secret", "note": "Tension"},
            {"source": "missing", "target": "unknown", "visibility": "invalid"},
            "An uneasy alliance",
        ],
    })
    assert result["relationship_axes"] == [
        {"source": "cast-1", "target": "faction-1", "edge_type": "rival_of", "note": "", "visibility": "public"},
        {"source": "opp-1", "target": "faction-1", "edge_type": "", "note": "Tension", "visibility": "secret"},
        {"source": "", "target": "", "edge_type": "", "note": "", "visibility": "public"},
        {"source": "", "target": "", "edge_type": "", "note": "An uneasy alliance", "visibility": "public"},
    ]
    assert normalize(result) == result


@pytest.mark.parametrize("edge_type", sorted(set().union(*WORLD_EDGE_TYPES_BY_FAMILY.values())))
def test_relationship_axes_accept_world_edge_types(edge_type):
    result = normalize({"relationship_axes": [{"edge_type": edge_type}]})
    assert result["relationship_axes"][0]["edge_type"] == edge_type


def test_secret_string_upgrades_to_secrets_list():
    result = normalize({"core_cast": [
        {"secret": " A hidden debt "},
        {"secret": "  "},
        {"secrets": [" A hidden debt ", "", "A second identity"], "secret": "superseded"},
    ]})
    assert [item["secrets"] for item in result["core_cast"]] == [
        ["A hidden debt"], [], ["A hidden debt", "A second identity"],
    ]
    assert all("secret" not in item for item in result["core_cast"])


def test_secrets_string_reads_like_the_legacy_secret():
    result = normalize({"core_cast": [
        {"secrets": " A hidden debt "},
        {"secrets": "  ", "secret": "A legacy secret"},
    ]})
    assert [item["secrets"] for item in result["core_cast"]] == [["A hidden debt"], ["A legacy secret"]]


def test_long_arcs_are_structured():
    result = normalize({
        "core_cast": [{"name": "A"}],
        "factions": [{"name": "D"}],
        "opposition": [{"name": "C"}],
        "long_arcs": [
            "Recover the evidence",
            {"summary": " Break the pact ", "owner_refs": ["A", "faction-1", "C", "missing", ""]},
            {"id": "arc-kept", "summary": "A final choice"},
        ],
        "reader_promises": [" An earned resolution "],
    })
    assert result["long_arcs"] == [
        {"id": "long-arc-1", "summary": "Recover the evidence", "owner_refs": []},
        {"id": "long-arc-2", "summary": "Break the pact", "owner_refs": ["cast-1", "faction-1", "opp-1"]},
        {"id": "arc-kept", "summary": "A final choice", "owner_refs": []},
    ]
    assert result["reader_promises"] == ["An earned resolution"]
    assert normalize(result) == result


def test_long_arc_ids_are_unique_after_deletion_and_creation():
    result = normalize({"long_arcs": [
        {"id": "long-arc-2", "summary": "Second arc"},
        {"id": "long-arc-3", "summary": "Third arc"},
        {"id": "long-arc-3", "summary": "New arc"},
    ]})
    assert [item["id"] for item in result["long_arcs"]] == [
        "long-arc-2", "long-arc-3", "long-arc-3-2",
    ]
    assert normalize(result) == result


@pytest.mark.parametrize("arc", [{"summary": "New arc"}, "New arc"])
def test_long_arc_default_ids_do_not_collide_with_explicit_ids(arc):
    result = normalize({"long_arcs": [
        {"id": "long-arc-2", "summary": "Existing arc"},
        arc,
    ]})
    assert [item["id"] for item in result["long_arcs"]] == [
        "long-arc-2", "long-arc-2-2",
    ]
    assert normalize(result) == result


def test_long_arc_ids_use_a_separate_namespace_from_actor_ids():
    result = normalize({
        "core_cast": [{"id": "long-arc-1", "name": "A"}],
        "long_arcs": [
            {"summary": "First arc", "owner_refs": ["long-arc-1"]},
            {"summary": "Second arc", "owner_refs": ["long-arc-2"]},
        ],
    })
    assert result["core_cast"][0]["id"] == "long-arc-1"
    assert result["long_arcs"] == [
        {"id": "long-arc-1", "summary": "First arc", "owner_refs": ["long-arc-1"]},
        {"id": "long-arc-2", "summary": "Second arc", "owner_refs": []},
    ]
    assert normalize(result) == result


def test_genesis_story_summary_joins_long_arc_summaries():
    revision = SimpleNamespace(id="revision-1", revision=1, pack_json=json.dumps({
        "world": {"story_engine": {"long_arcs": [
            {"id": "long-arc-1", "summary": "Recover the evidence", "owner_refs": ["cast-1"]},
            {"id": "long-arc-2", "summary": " A final choice ", "owner_refs": []},
            {"id": "long-arc-3", "summary": "", "owner_refs": []},
        ]}},
    }))
    request = ContextRequest(
        project_id="project-1", chapter_plan=None,
        repo=SimpleNamespace(get_active_genesis_revision=lambda project_id: revision),
    )
    draft = ContextDraft()
    GenesisContextProvider().contribute(request, draft)
    assert draft.data["genesis_story_engine_summary"] == "Recover the evidence；A final choice"
