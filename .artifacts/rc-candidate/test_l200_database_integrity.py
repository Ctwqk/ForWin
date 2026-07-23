from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from forwin.canon.admission import CanonAdmissionService
from forwin.models.audit import DecisionEvent
from forwin.models.book_state import GraphDeltaPatchRow, GraphDeltaRow
from forwin.models.draft import CandidateDraftRecord
from forwin.models.outbox import OutboxEvent
from forwin.models.project import Project
from forwin.models.publisher import PublisherUploadJob
from tests.test_canon_atomic_transaction import PreparedCanon


pytest_plugins = ("tests.test_canon_atomic_transaction",)


MODULE_PATH = Path(__file__).with_name("l200_evidence.py")
SPEC = importlib.util.spec_from_file_location("l200_database_evidence", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
l200 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(l200)


def database_url(prepared: PreparedCanon) -> str:
    engine = prepared.Session.kw["bind"]
    return engine.url.render_as_string(hide_password=False)


def commit(prepared: PreparedCanon) -> None:
    outcome = CanonAdmissionService(session_factory=prepared.Session).commit_plan(
        prepared.plan
    )
    assert outcome.blocked is False


def test_real_canon_outbox_payload_corruption_is_detected(
    prepared_canon: PreparedCanon,
) -> None:
    commit(prepared_canon)
    url = database_url(prepared_canon)
    baseline = l200.collect_database_state(url, prepared_canon.project_id)
    assert baseline["canon"]["candidate_identity_mismatches"] == 0
    assert baseline["candidates"]["reverse_identity_mismatches"] == 0
    assert baseline["outbox"]["identity_mismatches"] == 0
    assert baseline["outbox"]["missing_expected_events"] == 0

    with prepared_canon.Session.begin() as session:
        event = session.execute(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == prepared_canon.project_id,
                OutboxEvent.event_type == "canon.phase3.requested",
            )
        ).scalar_one()
        payload = json.loads(event.payload_json)
        payload["chapter_number"] = 999
        event.payload_json = json.dumps(payload)

    corrupted = l200.collect_database_state(url, prepared_canon.project_id)
    assert corrupted["outbox"]["identity_mismatches"] == 1


def test_real_reverse_candidate_identity_corruption_is_detected(
    prepared_canon: PreparedCanon,
) -> None:
    commit(prepared_canon)
    with prepared_canon.Session.begin() as session:
        candidate = session.get(CandidateDraftRecord, prepared_canon.candidate_id)
        assert candidate is not None
        candidate.chapter_number = 2

    corrupted = l200.collect_database_state(
        database_url(prepared_canon), prepared_canon.project_id
    )
    assert corrupted["canon"]["candidate_identity_mismatches"] == 1
    assert corrupted["candidates"]["reverse_identity_mismatches"] == 1


def test_real_unreferenced_chapter_graph_delta_is_detected(
    prepared_canon: PreparedCanon,
) -> None:
    commit(prepared_canon)
    with prepared_canon.Session.begin() as session:
        session.add(
            GraphDeltaRow(
                id="unreferenced-chapter-delta",
                project_id=prepared_canon.project_id,
                chapter_number=1,
                summary="This delta is not owned by a Canon commit.",
            )
        )

    corrupted = l200.collect_database_state(
        database_url(prepared_canon), prepared_canon.project_id
    )
    assert corrupted["graph"]["unreferenced_chapter_graph_deltas"] == 1


def test_real_orphan_graph_delta_patch_is_detected(
    prepared_canon: PreparedCanon,
) -> None:
    commit(prepared_canon)
    with prepared_canon.Session.begin() as session:
        session.add(
            GraphDeltaPatchRow(
                project_id=prepared_canon.project_id,
                delta_id="missing-graph-delta",
                chapter_number=1,
                patch_type="node",
                target_ref="character:missing",
                op="update",
                field_path="state",
            )
        )

    corrupted = l200.collect_database_state(
        database_url(prepared_canon), prepared_canon.project_id
    )
    assert corrupted["graph"]["orphan_graph_delta_patches"] == 1


def test_real_semantically_duplicate_graph_delta_is_detected(
    prepared_canon: PreparedCanon,
) -> None:
    commit(prepared_canon)
    with prepared_canon.Session.begin() as session:
        original = session.execute(
            select(GraphDeltaRow).where(
                GraphDeltaRow.project_id == prepared_canon.project_id
            )
        ).scalar_one()
        duplicate_id = f"{original.id}-duplicate"
        session.add(
            GraphDeltaRow(
                id=duplicate_id,
                project_id=original.project_id,
                chapter_number=original.chapter_number,
                story_time=original.story_time,
                delta_type=original.delta_type,
                operation=original.operation,
                target_type=original.target_type,
                target_id=original.target_id,
                source_type=original.source_type,
                source_id=original.source_id,
                world_line_id=original.world_line_id,
                summary=original.summary,
                evidence_refs_json=original.evidence_refs_json,
                review_verdict_id=original.review_verdict_id,
                allowed_for_canon=original.allowed_for_canon,
                metadata_json=original.metadata_json,
            )
        )
        patches = session.execute(
            select(GraphDeltaPatchRow).where(
                GraphDeltaPatchRow.delta_id == original.id
            )
        ).scalars()
        for patch in patches:
            session.add(
                GraphDeltaPatchRow(
                    project_id=patch.project_id,
                    delta_id=duplicate_id,
                    chapter_number=patch.chapter_number,
                    patch_type=patch.patch_type,
                    target_ref=patch.target_ref,
                    op=patch.op,
                    field_path=patch.field_path,
                    old_value_json=patch.old_value_json,
                    new_value_json=patch.new_value_json,
                    reason=patch.reason,
                    visibility_default=patch.visibility_default,
                    metadata_json=patch.metadata_json,
                )
            )

    corrupted = l200.collect_database_state(
        database_url(prepared_canon), prepared_canon.project_id
    )
    assert corrupted["graph"]["duplicate_semantic_graph_deltas"] == 1


def test_real_projection_target_and_chapter_memory_digest_come_from_canon(
    prepared_canon: PreparedCanon,
) -> None:
    commit(prepared_canon)
    state = l200.collect_database_state(
        database_url(prepared_canon), prepared_canon.project_id
    )
    target = state["projection_target"]
    assert target["chapter_number"] == 1
    assert target["canon_commit_id"] == prepared_canon.plan.canon_commit_id
    assert (
        target["event_id"]
        == f"{prepared_canon.plan.idempotency_key}:canon.projection.requested"
    )
    expected_payload = [
        {
            "chapter_number": 1,
            "title": "Chapter one",
            "summary": "A new character enters canon.",
            "body": "Shen Linchuan enters the archive.",
        }
    ]
    expected_digest = hashlib.sha256(
        json.dumps(
            expected_payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    assert target["chapter_memory_source_digest"] == expected_digest
    assert len(target["llm_kb_source_digest"]) == 64
    assert target["llm_kb_source_digest"] == l200.collect_database_state(
        database_url(prepared_canon), prepared_canon.project_id
    )["projection_target"]["llm_kb_source_digest"]
    assert len(target["chapter_memory_payload_hashes"]) == 1
    assert all(
        len(point_id) == 36 and len(payload_hash) == 64
        for point_id, payload_hash in target[
            "chapter_memory_payload_hashes"
        ].items()
    )


def test_real_policy_change_then_revert_remains_visible_in_freeze_audit(
    prepared_canon: PreparedCanon,
) -> None:
    url = database_url(prepared_canon)
    baseline = l200.collect_database_freeze_audit(
        url,
        prepared_canon.project_id,
    )
    with prepared_canon.Session.begin() as session:
        project = session.get(Project, prepared_canon.project_id)
        assert project is not None
        original_version = project.runtime_policy_version
        project.runtime_policy_version = original_version + 1
        session.add(
            DecisionEvent(
                project_id=prepared_canon.project_id,
                event_family="runtime_policy",
                event_type="runtime_policy_updated",
                actor_type="operator",
                actor_id="test",
                summary="temporary policy change",
                reason="freeze audit regression",
                payload_json=json.dumps(
                    {
                        "previous_version": original_version,
                        "version": original_version + 1,
                    }
                ),
            )
        )

    changed = l200.collect_database_freeze_audit(
        url,
        prepared_canon.project_id,
    )
    assert changed["runtime_policy_version"] == original_version + 1
    assert (
        changed["runtime_policy_update_events"]
        == baseline["runtime_policy_update_events"] + 1
    )

    with prepared_canon.Session.begin() as session:
        project = session.get(Project, prepared_canon.project_id)
        assert project is not None
        project.runtime_policy_version = original_version

    reverted = l200.collect_database_freeze_audit(
        url,
        prepared_canon.project_id,
    )
    assert reverted["runtime_policy_version"] == baseline["runtime_policy_version"]
    assert reverted["runtime_policy_update_sha256"] != baseline[
        "runtime_policy_update_sha256"
    ]
    assert (
        reverted["runtime_policy_update_events"]
        == baseline["runtime_policy_update_events"] + 1
    )


@pytest.mark.parametrize(
    ("status", "expected_unsettled"),
    [
        ("scheduled", 0),
        ("succeeded", 0),
        ("cancelled", 0),
        ("paused", 1),
        ("reconciling", 1),
        ("pending", 1),
        ("running", 1),
        ("failed", 1),
        ("unknown_future_state", 1),
    ],
)
def test_real_publisher_status_uses_terminal_allowlist(
    prepared_canon: PreparedCanon,
    status: str,
    expected_unsettled: int,
) -> None:
    with prepared_canon.Session.begin() as session:
        session.add(
            PublisherUploadJob(
                project_id=prepared_canon.project_id,
                platform_id="qidian",
                task_kind="chapter_upload",
                status=status,
                book_name="L200 evidence",
                chapter_title="Chapter one",
                body_text="body",
                result_payload_json="{}",
            )
        )

    state = l200.collect_database_state(
        database_url(prepared_canon), prepared_canon.project_id
    )
    assert state["publisher"]["unsettled_jobs"] == expected_unsettled
