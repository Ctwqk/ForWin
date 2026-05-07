from __future__ import annotations

from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.world_v4 import ExtractedWorldChangeSet
from forwin.world_v4_review_gate.types import V4ReviewIssue


def _source_type_value(source_type) -> str:
    return str(getattr(source_type, "value", source_type) or "").strip()


class WorldDeltaReviewer:
    name = "WorldDeltaReviewer"

    def review(
        self,
        extracted: ExtractedWorldChangeSet,
        *,
        chapter_intent: ChapterWorldDeltaIntent | None = None,
    ) -> list[V4ReviewIssue]:
        issues: list[V4ReviewIssue] = []
        for delta in extracted.world_deltas:
            if not str(getattr(delta, "world_line_id", "") or "").strip():
                issues.append(
                    V4ReviewIssue(
                        reviewer=self.name,
                        severity="fail",
                        failure_type="missing_world_line",
                        message=f"WorldDelta {delta.delta_id} 缺少 world_line_id。",
                        evidence_refs=[f"world_delta:{delta.delta_id}"],
                    )
                )
            source = getattr(delta, "source", None)
            if source is None or not _source_type_value(getattr(source, "source_type", "")):
                issues.append(
                    V4ReviewIssue(
                        reviewer=self.name,
                        severity="fail",
                        failure_type="missing_delta_source",
                        message=f"WorldDelta {delta.delta_id} 缺少 source_type。",
                        evidence_refs=[f"world_delta:{delta.delta_id}:source"],
                        repair_patch={
                            "required_delta_patch": "补充 faction_action / environmental_event 等明确来源。",
                        },
                    )
                )
            delta_kind = str(getattr(delta.delta_kind, "value", delta.delta_kind))
            if (
                delta_kind == "offscreen"
                and chapter_intent is not None
                and not chapter_intent.hint_delta_intents
                and not chapter_intent.reveal_delta_intents
            ):
                issues.append(
                    V4ReviewIssue(
                        reviewer=self.name,
                        severity="warn",
                        failure_type="offscreen_without_reveal_plan",
                        message=f"Hidden offscreen delta {delta.delta_id} 缺少后续 hint/reveal 计划。",
                        evidence_refs=[f"world_delta:{delta.delta_id}", "chapter_intent:hint/reveal"],
                        repair_patch={
                            "required_hint_patch": "为 hidden line 增加可感知 hint 或后续 reveal ladder。",
                        },
                    )
                )
        return issues
