"""Candidate checks use disposable copies; accepted rows are never rewound live."""

import json

import pytest
from sqlalchemy import select

from forwin.canon.admission import CanonAdmissionService
from forwin.models.canon import CanonCommitRecord
from forwin.models.project import ChapterPlan, Project
from tests import test_canon_atomic_transaction as atomic_tests

prepared_canon = atomic_tests.prepared_canon


@pytest.mark.parametrize("field_path", ["state", "metadata.note"])
def test_replica_state_provenance_recognizes_whole_state_patches(prepared_canon, field_path):
    from dataclasses import replace
    from forwin.canon.revision_replica import (
        PrefixProvenanceUnknown, _verify_manifest_evidence, capture_revision,
    )

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(fixture.plan)
    with fixture.Session() as source:
        snapshot = capture_revision(
            source, project_id=fixture.project_id, candidate_id=fixture.plan.candidate_id,
            model_identity={"provider": "fixture", "model": "frozen"},
            policy_fingerprint="policy",
        )
    # Feed the provenance validator a whole-state source for the existing state
    # row; an unrelated metadata patch must not establish that provenance.
    tables = dict(snapshot.tables)
    state = tables["world_node_states"][0]
    patches = []
    for row in tables["graph_delta_patches"]:
        metadata = json.loads(row["metadata_json"] or "{}")
        if row["patch_type"] == "node" and metadata.get("node_id") == state["node_id"]:
            row = {**row, "op": "set", "field_path": field_path}
        patches.append(row)
    tables["graph_delta_patches"] = tuple(patches)
    snapshot = replace(snapshot, tables=tables)
    if field_path == "state":
        _verify_manifest_evidence(snapshot, active_ids={outcome.commit_id})
    else:
        with pytest.raises(PrefixProvenanceUnknown, match="world state lacks proven delta identity"):
            _verify_manifest_evidence(snapshot, active_ids={outcome.commit_id})


def test_replica_capture_rewinds_only_private_database(prepared_canon):
    from forwin.canon.revision_replica import CandidateReplica, capture_revision
    from forwin.models.entity import Entity

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    with fixture.Session() as source:
        snapshot = capture_revision(
            source,
            project_id=fixture.project_id,
            candidate_id=fixture.plan.candidate_id,
            model_identity={"provider": "fixture", "model": "frozen"},
            policy_fingerprint="policy",
        )
    with CandidateReplica(snapshot) as replica:
        assert replica.session.get(Project, fixture.project_id) is not None
        chapter = replica.session.get(ChapterPlan, fixture.chapter_plan_id)
        assert chapter.active_commit_id is None
        assert not list(replica.session.scalars(select(Entity))), (
            "suffix entities must not leak into prefix registration"
        )
        from forwin.models.subworld import SubWorldRosterItem

        roster = replica.session.scalar(select(SubWorldRosterItem))
        assert roster.entity_id is None
        assert json.loads(roster.metadata_json).get("pending_entity_admission") is True
        chapter.title = "Only in the candidate replica"
        replica.session.commit()
    with fixture.Session() as source:
        chapter = source.get(ChapterPlan, fixture.chapter_plan_id)
        assert chapter.active_commit_id == outcome.commit_id
        assert chapter.status == "accepted"
        assert chapter.title != "Only in the candidate replica"
        assert source.get(CanonCommitRecord, outcome.commit_id) is not None


def test_replica_missing_delta_provenance_is_unknown_not_empty_prefix(prepared_canon):
    from forwin.canon.revision_replica import (
        CandidateReplica,
        PrefixProvenanceUnknown,
        capture_revision,
    )

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    with fixture.Session.begin() as source:
        source.get(
            CanonCommitRecord, outcome.commit_id
        ).graph_delta_ids_json = json.dumps(["missing"])
    with (
        fixture.Session() as source,
        pytest.raises(PrefixProvenanceUnknown, match="delta manifest"),
    ):
        snapshot = capture_revision(
            source,
            project_id=fixture.project_id,
            candidate_id=fixture.plan.candidate_id,
            model_identity={"provider": "fixture", "model": "frozen"},
            policy_fingerprint="policy",
        )
        with CandidateReplica(snapshot):
            pass


def test_replica_contains_no_other_project_or_publisher_jobs(prepared_canon):
    from forwin.canon.revision_replica import CandidateReplica, capture_revision
    from forwin.models.publisher import PublisherUploadJob

    fixture = prepared_canon
    CanonAdmissionService(session_factory=fixture.Session).commit_plan(fixture.plan)
    with fixture.Session.begin() as source:
        source.add(
            Project(id="unrelated-project", title="Do not clone", premise="private")
        )
    with fixture.Session() as source:
        snapshot = capture_revision(
            source,
            project_id=fixture.project_id,
            candidate_id=fixture.plan.candidate_id,
            model_identity={"provider": "fixture", "model": "frozen"},
            policy_fingerprint="policy",
        )
    with CandidateReplica(snapshot) as replica:
        assert replica.session.get(Project, "unrelated-project") is None
        assert not list(replica.session.scalars(select(PublisherUploadJob)))


def test_replica_restores_existing_entity_aliases_and_matching_roster_before_image(
    prepared_canon,
):
    from forwin.canon.revision_replica import CandidateReplica, capture_revision
    from forwin.models.entity import Entity, EntityAlias

    fixture = prepared_canon
    decision = fixture.plan.entity_admission_plan.decisions[0]
    with fixture.Session.begin() as session:
        session.add(
            Entity(
                id=decision.entity_id,
                project_id=fixture.project_id,
                kind="character",
                name=decision.canonical_name,
                aliases_json='["Original alias"]',
                created_at_chapter=0,
                is_active=True,
            )
        )
        session.flush()
        session.add(
            EntityAlias(
                project_id=fixture.project_id,
                entity_id=decision.entity_id,
                alias="Original alias",
            )
        )
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert not outcome.blocked, outcome
    with fixture.Session() as session:
        snapshot = capture_revision(
            session,
            project_id=fixture.project_id,
            candidate_id=fixture.plan.candidate_id,
            model_identity={"provider": "fixture", "model": "frozen"},
            policy_fingerprint="policy",
        )
    with CandidateReplica(snapshot) as replica:
        entity = replica.session.get(Entity, decision.entity_id)
        assert json.loads(entity.aliases_json) == ["Original alias"]
        aliases = list(replica.session.scalars(select(EntityAlias.alias)))
        assert aliases == ["Original alias"]


def test_missing_acceptance_registry_before_image_is_unknown(prepared_canon):
    from sqlalchemy import delete

    from forwin.canon.revision_replica import (
        CandidateReplica,
        PrefixProvenanceUnknown,
        capture_revision,
    )
    from forwin.models.audit import DecisionEvent

    fixture = prepared_canon
    outcome = CanonAdmissionService(session_factory=fixture.Session).commit_plan(
        fixture.plan
    )
    assert not outcome.blocked
    with fixture.Session.begin() as session:
        session.execute(
            delete(DecisionEvent).where(
                DecisionEvent.event_type == "canon_entity_before_image"
            )
        )
        snapshot = capture_revision(
            session,
            project_id=fixture.project_id,
            candidate_id=fixture.plan.candidate_id,
            model_identity={"provider": "fixture", "model": "frozen"},
            policy_fingerprint="policy",
        )
    with (
        pytest.raises(PrefixProvenanceUnknown, match="before-image"),
        CandidateReplica(snapshot),
    ):
        pass


def test_character_history_preserves_life_and_custody_dimensions():
    from forwin.canon.revision_evaluator import aggregate_historical_characters
    from forwin.canon_quality.chapter_review_form.form_builder import _character_ask

    merged = aggregate_historical_characters(
        [
            {
                "character_name": "Lin",
                "chapter_number": 1,
                "to_state": "alive",
                "payload": {"must_track": True},
            },
            {
                "character_name": "Lin",
                "chapter_number": 2,
                "to_state": "captured",
                "payload": {},
            },
            {
                "character_name": "Lin",
                "chapter_number": 3,
                "to_state": "dead",
                "payload": {},
            },
        ]
    )
    assert len(merged) == 1
    ask = _character_ask(merged[0])
    assert ask.prior_life_state == "dead"
    assert ask.prior_custody_state == "captured"
    assert ask.must_track
