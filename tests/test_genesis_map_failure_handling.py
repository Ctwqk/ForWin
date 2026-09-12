"""Map model failures must preserve the authored revision, including refinements."""

import json

import pytest

from forwin.codex_bridge.runner import CodexExecRunner
from forwin.llm.codex_client import CodexBridgeClient
from tests.test_genesis_map_completeness import complete_atlas, model_service


def test_complete_map_refinement_request_preserves_legacy_routes_and_is_sendable():
    source, calls = complete_atlas(), []
    source["edges"][0] = {
        "from": "station",
        "to": "depot",
        "travel_time": "10分钟",
        "control": "双人取样",
    }
    payload, _ = model_service(source, calls)._call_json_with_trace(
        messages=[{"role": "user", "content": "更新地图，保留路线原文"}],
        fallback=complete_atlas(),
        stage_key="map:refine",
    )
    assert payload == source
    request_schema = CodexBridgeClient._structured_output_schema(
        calls[0]["output_schema"]
    )
    if request_schema is not None:
        normalized = CodexExecRunner._codex_output_schema(request_schema)

        def check_objects(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    assert value.get("additionalProperties") is False
                for child in value.values():
                    check_objects(child)
            elif isinstance(value, list):
                for child in value:
                    check_objects(child)

        check_objects(normalized)
        # A sendable schema must also allow the existing legacy route fields.
        from jsonschema import Draft202012Validator

        Draft202012Validator(normalized).validate(source)
    assert calls[0]["response_format"] == {"type": "json_object"}


def unavailable_service(failure):
    service = model_service({})
    if failure == "missing_credentials":
        service.llm_client.api_key = ""

    def chat(*args, **kwargs):
        if failure == "invalid_json":
            return "not a JSON object"
        if failure == "empty_response":
            return ""
        raise RuntimeError("model unavailable")

    service.llm_client.chat = chat
    return service


@pytest.mark.parametrize("stage", ["map", "map:refine", "map:refine_item"])
@pytest.mark.parametrize(
    "failure", ["transport", "invalid_json", "empty_response", "missing_credentials"]
)
def test_map_model_failure_cannot_return_a_successful_scaffold(stage, failure):
    with pytest.raises((RuntimeError, ValueError)):
        unavailable_service(failure)._call_json_with_trace(
            messages=[{"role": "user", "content": "修改地图"}],
            fallback=complete_atlas(),
            stage_key=stage,
        )


@pytest.mark.parametrize("failure", ["transport", "missing_credentials"])
def test_non_map_model_failure_keeps_existing_fallback_behavior(failure):
    payload, trace = unavailable_service(failure)._call_json_with_trace(
        messages=[{"role": "user", "content": "建立简报"}],
        fallback={"one_line": "保留的简报"},
        stage_key="brief",
    )
    assert payload == {"one_line": "保留的简报"}
    assert trace["output_summary"]["mode"] == "fallback"


def test_map_generation_with_codex_only_configuration_still_accepts_valid_output():
    source = complete_atlas()
    service = model_service(source)
    service.llm_client.api_key = ""
    service.llm_client.codex_enabled = True
    payload, _ = service._call_json_with_trace(
        messages=[{"role": "user", "content": "生成地图"}],
        fallback={},
        stage_key="map",
    )
    assert payload == source


@pytest.mark.parametrize("target_path", ["overview", "topology_rules", "edges"])
@pytest.mark.parametrize("response", [{}, {"unrelated": "not a replacement"}])
def test_map_item_missing_value_cannot_create_a_persisted_revision(
    target_path, response
):
    from sqlalchemy import select

    from forwin.models import Project
    from forwin.models.base import get_engine, get_session_factory
    from forwin.models.genesis import BookGenesisRevision
    from forwin.state.updater import StateUpdater
    from tests.postgres import postgres_test_url

    service = model_service(response)
    engine = get_engine(postgres_test_url("map-item-missing-value"))
    try:
        with get_session_factory(engine)() as session:
            project = Project(id="map-item", title="河谷", premise="调查", genre="悬疑")
            session.add(project)
            session.flush()
            updater = StateUpdater(session)
            revision = service.create_initial_revision(
                session=session, updater=updater, project=project
            )
            pack = service.load_pack(revision)
            pack["world"]["map_atlas"] = complete_atlas()
            revision.pack_json = json.dumps(pack, ensure_ascii=False)
            session.commit()
            original_id, original_json = revision.id, revision.pack_json
            with pytest.raises(ValueError, match="value"):
                service.refine_stage(
                    session=session,
                    updater=updater,
                    project=project,
                    revision=revision,
                    stage_key="map",
                    instruction="更新目标内容",
                    target_path=target_path,
                )
            session.commit()
            assert project.active_genesis_revision_id == original_id
            assert (
                session.get(BookGenesisRevision, original_id).pack_json == original_json
            )
            assert len(session.scalars(select(BookGenesisRevision)).all()) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("target_path", ["overview", "topology_rules", "edges"])
def test_map_item_explicit_same_value_is_a_valid_response(target_path):
    from forwin.models import Project

    atlas = complete_atlas()
    payload, trace = model_service({"value": atlas[target_path]})._refine_stage_payload(
        project=Project(id="map-item", title="河谷", premise="调查", genre="悬疑"),
        pack={"world": {"map_atlas": atlas}},
        stage_key="map",
        instruction="核实目标内容",
        target_path=target_path,
    )
    assert payload[target_path] == atlas[target_path]
    assert trace["output_summary"]["mode"] == "success"


@pytest.mark.parametrize(
    "operation,failure",
    [
        ("generate", "transport"),
        ("refine", "transport"),
        ("refine_item", "transport"),
        ("generate", "invalid_json"),
        ("refine", "empty_response"),
        ("refine", "missing_credentials"),
    ],
)
def test_failed_map_model_call_cannot_advance_or_edit_persisted_revision(
    operation, failure
):
    from sqlalchemy import select

    from forwin.models import Project
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.models.genesis import BookGenesisRevision
    from forwin.state.updater import StateUpdater
    from tests.postgres import postgres_test_url

    service = unavailable_service(failure)
    engine = get_engine(postgres_test_url("genesis-map-model-failure"))
    init_db(engine)
    try:
        with get_session_factory(engine)() as session:
            project = Project(
                id="map-model-failure", title="河谷", premise="调查", genre="悬疑"
            )
            session.add(project)
            session.flush()
            updater = StateUpdater(session)
            revision = service.create_initial_revision(
                session=session, updater=updater, project=project
            )
            pack = service.load_pack(revision)
            pack["world"]["map_atlas"] = complete_atlas()
            revision.pack_json = json.dumps(pack, ensure_ascii=False)
            session.commit()
            original_id, original_json = revision.id, revision.pack_json
            common = {
                "session": session,
                "updater": updater,
                "project": project,
                "revision": revision,
                "stage_key": "map",
            }
            with pytest.raises((RuntimeError, ValueError)):
                if operation == "generate":
                    service.generate_stage(**common)
                else:
                    service.refine_stage(
                        **common,
                        instruction="修改路线",
                        target_path="edges[0]" if operation == "refine_item" else "",
                    )
            # Commit rather than relying on rollback to hide an accidental write.
            session.commit()
            assert project.active_genesis_revision_id == original_id
            assert (
                session.get(BookGenesisRevision, original_id).pack_json == original_json
            )
            assert len(session.scalars(select(BookGenesisRevision)).all()) == 1
    finally:
        engine.dispose()
