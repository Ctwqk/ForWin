from __future__ import annotations

from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.world_v4 import ExtractedWorldChangeSet
from forwin.world_v4_review_gate.types import V4ReviewIssue


class RevealReviewer:
    name = "RevealReviewer"

    def review(
        self,
        extracted: ExtractedWorldChangeSet,
        *,
        chapter_intent: ChapterWorldDeltaIntent | None,
        chapter_body: str,
    ) -> list[V4ReviewIssue]:
        if chapter_intent is None or "father_sieged" not in chapter_intent.must_not_reveal:
            return []
        issues: list[V4ReviewIssue] = []
        text = chapter_body or ""
        summaries = "；".join(delta.summary for delta in extracted.world_deltas)
        combined = text + summaries
        planned_reveal_chapter = int(chapter_intent.metadata.get("planned_reveal_chapter") or 0)
        has_reveal_delta = any(
            str(getattr(delta.delta_kind, "value", delta.delta_kind)) == "reveal"
            for delta in extracted.world_deltas
        )
        if planned_reveal_chapter and extracted.chapter_number < planned_reveal_chapter and has_reveal_delta:
            issues.append(
                V4ReviewIssue(
                    reviewer=self.name,
                    severity="fail",
                    failure_type="reveal_before_planned_chapter",
                    message="本章 reveal 早于 RevealLadder 计划章节。",
                    evidence_refs=[
                        f"chapter_intent:planned_reveal_chapter:{planned_reveal_chapter}",
                        f"chapter:{extracted.chapter_number}",
                    ],
                    repair_patch={
                        "must_not_reveal": list(chapter_intent.must_not_reveal),
                        "required_hint_patch": "改为 hint / suspicion，不进入 direct reveal。",
                    },
                )
            )
        early_reveal = (
            ("父亲" in combined and "被围" in combined)
            or "父亲已被围" in combined
            or "父亲在母星被围" in combined
        )
        if early_reveal:
            issues.append(
                V4ReviewIssue(
                    reviewer=self.name,
                    severity="fail",
                    failure_type="early_reveal",
                    message="本章违反 must_not_reveal，提前揭示父亲被围。",
                    evidence_refs=["chapter_body:父亲/被围", "chapter_intent:must_not_reveal"],
                    repair_patch={
                        "must_not_reveal": list(chapter_intent.must_not_reveal),
                        "required_hint_patch": "只保留乱码通讯、旧部呼号、通讯延迟等 hint。",
                    },
                )
            )
        return issues
