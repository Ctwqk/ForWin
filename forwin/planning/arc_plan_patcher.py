from __future__ import annotations

from forwin.models.base import new_id
from forwin.narrative_obligations.types import NarrativePlanPatch


class ArcPlanPatcher:
    def build_patch(
        self,
        *,
        project_id: str,
        origin_chapter_number: int,
        target_arc_id: str,
        issue_kind: str,
        summary: str,
        source_signal_ids: list[str],
        source_obligation_ids: list[str],
        payoff_test: str,
        affected_chapters: list[int] | None = None,
    ) -> NarrativePlanPatch:
        return NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="arc_defer_acceptance",
            target_scope="arc",
            target_arc_id=str(target_arc_id or ""),
            affected_chapters=[int(chapter) for chapter in affected_chapters or []],
            source_signal_ids=list(source_signal_ids),
            source_obligation_ids=list(source_obligation_ids),
            new_contract={
                "issue_kind": issue_kind,
                "summary": summary,
                "payoff_test": payoff_test,
                "origin_chapter_number": int(origin_chapter_number or 0),
            },
            diff_summary=f"Bind structural issue {issue_kind} to arc plan {target_arc_id}.",
            writer_context_injections=[
                {
                    "type": "structural_plan_patch",
                    "scope": "arc",
                    "issue_kind": issue_kind,
                    "instruction": summary,
                    "payoff_test": payoff_test,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "structural_plan_patch",
                    "scope": "arc",
                    "issue_kind": issue_kind,
                    "payoff_test": payoff_test,
                }
            ],
            expected_resolution_tests=[payoff_test] if payoff_test else [],
            metadata={"origin_chapter_number": int(origin_chapter_number or 0)},
        )
