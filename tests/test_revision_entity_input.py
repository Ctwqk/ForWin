import json

import pytest

from forwin.naming.entity_registrar import LLMEntityAdmissionClassifier
from forwin.protocol import EntitySnapshot, WriterOutput
from tests import test_canon_atomic_transaction as atomic

prepared_canon = atomic.prepared_canon


def test_historical_registrar_reads_complete_body_and_all_characters():
    seen = []

    class Client:
        def chat(self, messages, **kwargs):
            seen.append((messages, kwargs))
            return json.dumps(
                {"decisions": [{"name": "Unknown", "decision": "background_generic"}]}
            )

    body = "a" * 3000 + " The last character identifies Unknown."
    entities = [
        EntitySnapshot(
            entity_id=f"e{i}",
            name=f"Person{i}",
            kind="character",
            description="",
            current_state={},
        )
        for i in range(90)
    ]
    LLMEntityAdmissionClassifier(Client(), historical_full_input=True).classify(
        project_id="p",
        chapter_number=1,
        names=["Unknown"],
        writer_output=WriterOutput(
            project_id="p",
            chapter_number=1,
            title="Chapter",
            body=body,
            end_of_chapter_summary="",
        ),
        existing_entities=entities,
    )
    payload = json.loads(seen[0][0][1]["content"])
    assert payload["body_excerpt"] == body
    assert len(payload["existing_characters"]) == 90
    assert seen[0][1]["max_tokens"] > 600


def test_historical_registrar_input_budget_does_not_truncate_or_call_model():
    class Client:
        def chat(self, *args, **kwargs):
            pytest.fail("over-budget history must remain unknown")

    with pytest.raises(ValueError, match="budget"):
        LLMEntityAdmissionClassifier(Client(), historical_full_input=True).classify(
            project_id="p",
            chapter_number=1,
            names=["Unknown"],
            writer_output=WriterOutput(
                project_id="p",
                chapter_number=1,
                title="Chapter",
                body="a" * 130000,
                end_of_chapter_summary="",
            ),
            existing_entities=[],
        )


@pytest.mark.parametrize(
    "response,expected",
    [("timeout", "unknown"), ("missing", "unknown"), ("conflict", "fail")],
)
def test_historical_registrar_unavailable_coverage_is_unknown_and_real_conflict_is_fail(
    prepared_canon, response, expected
):
    from forwin.canon.revision_service import (
        RevisionValidationService,
        save_revision_proposal,
    )
    from forwin.models.canon import CanonRevisionValidationRecord
    from forwin.runtime.policy import RuntimePolicy
    from forwin.writer import ChapterWriter
    from tests.test_revision_full_suffix import BodyModel, _book

    fixture = prepared_canon
    ids, body, _old, _ = _book(fixture)
    with fixture.Session.begin() as session:
        proposal = save_revision_proposal(
            session,
            project_id=ids[0],
            chapter_number=1,
            body=body + "\n陆明走入档案室。",
        )
        candidate_id = proposal.id

    class EntityModel(BodyModel):
        def chat(self, messages, **kwargs):
            result = super().chat(messages, **kwargs)
            try:
                request = json.loads(messages[-1]["content"])
            except ValueError:
                request = {}
            if "unknown_names" in request:
                if response == "timeout":
                    raise TimeoutError("classifier unavailable")
                return json.dumps(
                    {
                        "decisions": []
                        if response == "missing"
                        else [
                            {
                                "name": "陆明",
                                "decision": "plan_conflict",
                                "reason": "A verified incompatible identity.",
                            }
                        ]
                    }
                )
            value = json.loads(result)
            if "entity_mentions" in value:
                value["entity_mentions"] = [
                    {
                        "entity_name": "陆明",
                        "entity_kind": "character",
                        "is_named": True,
                    }
                ]
            return json.dumps(value)

    prepared = RevisionValidationService(
        session_factory=fixture.Session,
        writer=ChapterWriter(EntityModel()),
        policy=RuntimePolicy.for_profile("standard"),
    ).prepare(project_id=ids[0], candidate_id=candidate_id)
    assert prepared.blocked
    with fixture.Session() as session:
        record = session.get(CanonRevisionValidationRecord, prepared.blocked_path)
        assert record.status == expected, record.result_json
