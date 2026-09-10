from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import select

from forwin.protocol.context import AudienceHintView, ChapterContextPack
from forwin.writer.chapter_writer import ChapterWriter
from tests import test_feedback_action_lifecycle as lifecycle

action_session = lifecycle.action_session


def hint_context():
    return ChapterContextPack(
        project_id="book",
        project_title="Book",
        premise="Premise",
        genre="悬疑",
        setting_summary="旧站",
        chapter_number=5,
        chapter_plan_title="旧站",
        chapter_plan_one_line="调查线索",
        chapter_goals=["追查"],
        audience_hints=AudienceHintView(
            items=[
                {
                    "action_id": f"action-{number}",
                    "category": "prediction",
                    "text": "读者预测内鬼身份，保留既定因果。",
                }
                for number in range(4)
            ]
        ),
    )


class CapturingModel:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    def chat(self, messages, temperature, max_tokens):
        self.messages.append(messages)
        if self.error:
            raise self.error
        return (
            "<<FORWIN_TITLE>>\n旧站\n<<FORWIN_BODY>>\n"
            + "他踏进车站，认出了来人。" * 40
            + "\n<<FORWIN_SUMMARY>>\n找到了线索。"
        )


def test_prediction_sent_ids_are_captured_after_clipping_and_do_not_claim_body_success():
    model = CapturingModel()
    writer = ChapterWriter(model, writer_mode="single", min_chapter_chars=200)
    output = writer.write_chapter(hint_context())
    actual = model.messages[0]
    assert "读者预测内鬼身份" in actual[-1]["content"]
    evidence = output.generation_meta["prompt_trace"]["input_snapshot"][
        "feedback_inputs"
    ]
    assert len(evidence) == 1
    assert [item["action_id"] for item in evidence[0]["hints"]] == [
        "action-0",
        "action-1",
        "action-2",
    ]
    assert evidence[0]["input_status"] == "attempted_input"
    assert evidence[0]["adapter_outcome"] == "returned"
    assert (
        evidence[0]["messages_sha256"]
        == hashlib.sha256(
            json.dumps(
                actual, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    assert "feedback_action" not in output.body
    assert "body_implemented" not in evidence[0]


def test_exception_retains_exact_attempted_input_and_original_exception():
    error = KeyboardInterrupt("stop")
    model = CapturingModel(error)
    writer = ChapterWriter(model, writer_mode="single", min_chapter_chars=200)
    with pytest.raises(KeyboardInterrupt) as caught:
        writer.write_chapter(hint_context())
    assert caught.value is error
    evidence = writer.drain_feedback_inputs()
    assert len(evidence) == 1
    assert evidence[0]["adapter_outcome"] == "raised"
    assert evidence[0]["exception_type"] == "KeyboardInterrupt"
    assert [item["action_id"] for item in evidence[0]["hints"]] == [
        "action-0",
        "action-1",
        "action-2",
    ]
    assert writer.drain_feedback_inputs() == []


def test_final_message_clipping_must_keep_marker_and_exact_complete_text():
    from forwin.writer.prompt_core.sections import _audience_hints_section

    writer = ChapterWriter(
        CapturingModel(), writer_mode="single", min_chapter_chars=200
    )
    section = _audience_hints_section(hint_context())
    assert "feedback_action:action-0" in section
    # The final message no longer contains the text associated with action-0.
    final = section.replace("读者预测内鬼身份，保留既定因果。", "被裁掉", 1)
    writer._call_chat(
        [{"role": "user", "content": final}], temperature=0.1, max_tokens=100
    )
    evidence = writer.drain_feedback_inputs()
    assert [item["action_id"] for item in evidence[0]["hints"]] == [
        "action-1",
        "action-2",
    ]


def test_pre_call_failure_does_not_invent_input_evidence():
    writer = ChapterWriter(CapturingModel(), writer_mode="single")
    assert hasattr(writer, "drain_feedback_inputs")
    assert writer.drain_feedback_inputs() == []


@pytest.mark.parametrize("failure", [False, True])
def test_real_prompt_trace_persists_only_sent_actions_in_caller_transaction(
    action_session, failure
):
    from types import SimpleNamespace

    from forwin.audience.actions import ActionMapper
    from forwin.audience.feedback import FeedbackCooldown
    from forwin.models import FeedbackActionRecord
    from forwin.models.genesis import PromptTrace
    from forwin.observability.pipeline_trace import (
        PipelineAuditContext,
        PipelineTraceRecorder,
    )
    from forwin.state.updater import StateUpdater
    from forwin.writer.execution_telemetry import WriterExecutionTelemetry

    session = action_session
    mapper = ActionMapper()
    actions = mapper.map_actions(
        [
            lifecycle.aggregate_view(
                key=f"prediction-{n}", signal_type="prediction", direction="predicts"
            )
            for n in range(4)
        ]
    )
    records = mapper.record_actions(
        session,
        project_id="book",
        chapter_number=4,
        actions=actions,
        cooldown=FeedbackCooldown(),
    )
    hints = mapper.select_actions(
        session,
        project_id="book",
        chapter_number=4,
        action_ids=[row.id for row in records],
        cooldown=FeedbackCooldown(),
    )
    assert records[3].status == "proposed"
    session.commit()
    context = hint_context().model_copy(update={"audience_hints": hints})
    store = SimpleNamespace(save_observability_diagnostic=lambda **kwargs: {})
    recorder = PipelineTraceRecorder(
        audit=PipelineAuditContext(task_id="task"),
        artifact_store=store,
        observability=SimpleNamespace(),
    )
    updater = StateUpdater(session)
    writer = ChapterWriter(
        CapturingModel(KeyboardInterrupt("stop") if failure else None),
        writer_mode="single",
        min_chapter_chars=200,
    )
    if failure:
        with pytest.raises(KeyboardInterrupt) as caught:
            writer.write_chapter(context)
        telemetry = WriterExecutionTelemetry(
            recorder=recorder, writer=writer, model_client=writer.llm_client
        )
        trace_id = telemetry.record_failure_trace(
            updater=updater,
            project_id="book",
            chapter_number=5,
            context=context,
            stage_key="chapter_draft",
            template_id="writer:single",
            source_event_id="",
            exc=caught.value,
            duration_ms=10,
            attempts=[],
            skill_layers=None,
        )
    else:
        output = writer.write_chapter(context)
        trace_id = recorder.save_prompt_trace(
            session=session,
            updater=updater,
            project_id="book",
            prompt_trace=output.generation_meta["prompt_trace"],
        )
    trace = session.get(PromptTrace, trace_id)
    trace_inputs = json.loads(trace.input_snapshot_json)["feedback_inputs"]
    assert trace_inputs[0]["adapter_outcome"] == ("raised" if failure else "returned")
    for row in records[:3]:
        evidence = json.loads(row.prompt_inclusions_json)
        assert len(evidence) == 1
        assert evidence[0]["prompt_trace_id"] == trace_id
        assert evidence[0]["chapter_number"] == 5
        assert evidence[0]["messages_sha256"] == trace_inputs[0]["messages_sha256"]
        assert json.loads(row.body_observation_json) == {}
    assert json.loads(records[3].prompt_inclusions_json) == []
    session.rollback()
    assert list(session.scalars(select(PromptTrace))) == []
    assert all(
        json.loads(row.prompt_inclusions_json) == []
        for row in session.scalars(select(FeedbackActionRecord))
    )


def test_revision_writer_never_shares_pending_input_evidence():
    from forwin.canon.revision_model import isolated_writer
    from forwin.writer.prompt_core.sections import _audience_hints_section

    writer = ChapterWriter(CapturingModel(), writer_mode="single")
    isolated = isolated_writer(writer, {})
    with pytest.raises(ValueError, match="actual model identity unavailable"):
        isolated._call_chat(
            [{"role": "user", "content": _audience_hints_section(hint_context())}],
            temperature=0.1,
            max_tokens=100,
        )
    assert len(isolated.drain_feedback_inputs()) == 1
    assert writer.drain_feedback_inputs() == []


def test_hint_markers_cannot_leak_into_body_even_when_adapter_returns():
    from forwin.writer.prompt_core.sections import _audience_hints_section

    class EchoHintModel(CapturingModel):
        def chat(self, messages, temperature, max_tokens):
            return (
                "<<FORWIN_TITLE>>\n旧站\n<<FORWIN_BODY>>\n"
                + _audience_hints_section(hint_context())
                + "\n"
                + "他认出了来人。" * 40
            )

    writer = ChapterWriter(EchoHintModel(), writer_mode="single", min_chapter_chars=200)
    with pytest.raises(ValueError, match="feedback.*marker"):
        writer.write_chapter(hint_context())
    evidence = writer.drain_feedback_inputs()
    assert evidence and all(item["adapter_outcome"] == "returned" for item in evidence)


def test_scene_pipeline_records_only_actual_marked_writer_calls():
    class SceneModel(CapturingModel):
        def chat(self, messages, temperature, max_tokens, stage_key=""):
            self.messages.append((stage_key, messages))
            if stage_key == "scene_breakdown":
                return (
                    '{"scenes":[{"scene_no":1,"objective":"追查","target_chars":600}]}'
                )
            if stage_key.endswith("extraction"):
                return '{"state_changes":[],"new_events":[],"delivered_payoffs":[],"thread_beats":[],"time_advance":null,"lore_candidates":[],"timeline_hints":[],"writer_notes":[],"entity_mentions":[]}'
            return (
                "<<FORWIN_TITLE>>\n旧站\n<<FORWIN_BODY>>\n"
                + "他踏进车站，认出了来人。" * 60
                + "\n<<FORWIN_SUMMARY>>\n找到了线索。"
            )

    model = SceneModel()
    writer = ChapterWriter(model, writer_mode="scene", min_chapter_chars=200)
    output = writer.write_chapter(hint_context())
    assert output.generation_meta["mode"] == "scene"
    actual_stages = [
        stage
        for stage, messages in model.messages
        if "feedback_action:" in str(messages)
    ]
    evidence = output.generation_meta["prompt_trace"]["input_snapshot"][
        "feedback_inputs"
    ]
    assert [item["stage_key"] for item in evidence] == actual_stages
    assert {"scene_breakdown", "scene_generation", "scene_stitch"} <= set(actual_stages)
    assert all(len(item["hints"]) == 3 for item in evidence)
