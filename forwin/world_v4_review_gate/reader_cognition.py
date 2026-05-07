from __future__ import annotations

from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.world_v4 import ExtractedWorldChangeSet
from forwin.world_v4_review_gate.types import V4ReviewIssue


class ReaderCognitionReviewer:
    name = "ReaderCognitionReviewer"

    def review(
        self,
        extracted: ExtractedWorldChangeSet,
        *,
        chapter_intent: ChapterWorldDeltaIntent | None,
        promise_debt_count: int = 0,
    ) -> list[V4ReviewIssue]:
        issues: list[V4ReviewIssue] = []
        planned_reader_payoff = bool(
            chapter_intent and chapter_intent.reader_experience_intents
        )
        delivered_reader_payoff = bool(extracted.reader_experience_deltas)
        has_increment = any(
            (
                extracted.world_deltas,
                extracted.belief_updates,
                extracted.knowledge_gap_updates,
                extracted.reveal_events,
                extracted.knowledge_update_events,
                extracted.reader_experience_deltas,
            )
        )
        if int(promise_debt_count or 0) >= 5 and not has_increment:
            issues.append(
                V4ReviewIssue(
                    reviewer=self.name,
                    severity="fail",
                    failure_type="missing_chapter_increment",
                    message="本章没有可感知事件、认知变化、reveal 或 reader experience 增量。",
                    evidence_refs=["extracted:empty", "promise_debt_count"],
                    repair_patch={
                        "required_delta_patch": "至少加入 visible/offscreen/hint/knowledge/reveal 之一。",
                        "required_payoff_patch": "关闭一个旧问题或提供明确认知推进。",
                    },
                )
            )
        if int(promise_debt_count or 0) >= 3 and not planned_reader_payoff and not delivered_reader_payoff:
            issues.append(
                V4ReviewIssue(
                    reviewer=self.name,
                    severity="warn",
                    failure_type="unpaid_promise_debt",
                    message="promise debt 已累积，但本章没有 reader cognition payoff 计划或实际兑现。",
                    evidence_refs=["promise_debt_count"],
                    repair_patch={
                        "required_payoff_patch": "加入一个局部问题关闭、线索确认或阶段性微兑现。",
                    },
                )
            )
        return issues
