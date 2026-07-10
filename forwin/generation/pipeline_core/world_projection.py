from __future__ import annotations

from sqlalchemy.orm import Session

from forwin.audience.feedback import run_feedback_aggregation_pass
from forwin.planning.stage_analysis import save_stage_analysis
from forwin.protocol.writer import WriterOutput
from forwin.simulation.world import save_npc_intents, save_world_turn


@staticmethod
def _prompt_trace_success_summary(
    writer_output: WriterOutput,
) -> dict[str, object]:
    generation_meta = getattr(writer_output, "generation_meta", {}) or {}
    prompt_trace = (
        generation_meta.get("prompt_trace")
        if isinstance(generation_meta, dict)
        else {}
    )
    attempts = (
        prompt_trace.get("attempts", [])
        if isinstance(prompt_trace, dict)
        else []
    )
    if not isinstance(attempts, list):
        attempts = []
    successful = None
    for item in attempts:
        if not isinstance(item, dict):
            continue
        if int(item.get("output_chars") or 0) > 0 and not str(
            item.get("error_class") or ""
        ):
            successful = item
    if successful is None and attempts:
        successful = next(
            (
                item
                for item in reversed(attempts)
                if isinstance(item, dict)
            ),
            None,
        )
    if not isinstance(successful, dict):
        return {
            "prompt_trace_id": str(
                generation_meta.get("prompt_trace_id", "") or ""
            ),
            "effective_model": "",
            "effective_profile_id": "",
            "successful_attempt_no": 0,
            "attempt_group_id": "",
            "output_chars": int(
                getattr(writer_output, "char_count", 0) or 0
            ),
            "fallback_chain": generation_meta.get("model_fallbacks", []),
        }
    return {
        "prompt_trace_id": str(
            generation_meta.get("prompt_trace_id", "") or ""
        ),
        "effective_model": str(successful.get("model") or ""),
        "effective_profile_id": str(successful.get("profile_id") or ""),
        "effective_profile_name": str(successful.get("profile_name") or ""),
        "successful_attempt_no": int(successful.get("attempt_no") or 0),
        "attempt_group_id": str(successful.get("attempt_group_id") or ""),
        "output_chars": int(
            successful.get("output_chars")
            or getattr(writer_output, "char_count", 0)
            or 0
        ),
        "fallback_chain": generation_meta.get("model_fallbacks", []),
    }


def _run_phase3_pass(
    self,
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
) -> None:
    stage = self.stage_analyzer.analyze(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    pacing = self.pacing_strategist.analyze(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    save_stage_analysis(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage=stage,
        pacing=pacing,
    )
    self.replan_governor.apply_if_needed(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage=stage,
        pacing=pacing,
    )
    self.arc_envelope_manager.ensure_active_arc_resolution(
        session=session,
        project_id=project_id,
        activation_chapter=chapter_number + 1,
    )
    self.arc_envelope_manager.record_provisional_promotion(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        reason="accepted-into-canon",
    )
    intents = self.npc_intent_generator.generate(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    self._flush_background_llm_trace(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage_key="npc_intents",
        trace_scope="phase4",
    )
    save_npc_intents(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        intents=intents,
    )
    world_turn = self.world_simulator.simulate(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
    )
    self._flush_background_llm_trace(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        stage_key="world_pressure",
        trace_scope="phase4",
    )
    save_world_turn(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        turn=world_turn,
    )
    run_feedback_aggregation_pass(
        session,
        project_id,
        chapter_number,
        cooldown_chapters=3,
        comment_to_reader_ratio=80,
    )


__all__ = ["_prompt_trace_success_summary", "_run_phase3_pass"]
