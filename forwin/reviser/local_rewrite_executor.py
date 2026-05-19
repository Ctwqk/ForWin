from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from forwin.protocol.writer import WriterOutput

RewriteStatus = Literal["rewritten", "needs_writer", "unsupported", "failed"]


@dataclass(frozen=True)
class RewriteResult:
    status: RewriteStatus
    issue_kind: str
    mode: str
    writer_output: WriterOutput | None = None
    instruction: str = ""
    reason: str = ""


class LocalRewriteExecutor:
    def execute(
        self,
        *,
        draft: WriterOutput,
        issue_kind: str,
        signals: list[object],
        context_pack: dict[str, object],
    ) -> RewriteResult:
        del signals, context_pack
        kind = str(issue_kind or "").strip()
        if kind in {"placeholder_leakage", "bare_role_placeholder_leakage"}:
            return self._rewrite_placeholder(draft=draft, issue_kind=kind)
        if kind == "body_truncated":
            return RewriteResult(
                status="needs_writer",
                issue_kind=kind,
                mode="continue_from_last_complete_scene",
                instruction=(
                    "continue_from_last_complete_scene: preserve existing "
                    "completed scenes and write only the missing continuation"
                ),
            )
        if kind == "body_duplicate_span":
            return self._drop_duplicate_paragraphs(draft=draft, issue_kind=kind)
        if kind == "internal_state_key_leakage":
            return self._strip_internal_state_keys(draft=draft, issue_kind=kind)
        if kind == "subworld_admission_unauthorized_new_entity":
            return RewriteResult(
                status="unsupported",
                issue_kind=kind,
                mode="metadata_required",
                reason="subworld admission repair requires metadata-aware executor",
            )
        return RewriteResult(status="unsupported", issue_kind=kind, mode="unsupported_issue")

    def _rewrite_placeholder(self, *, draft: WriterOutput, issue_kind: str) -> RewriteResult:
        body = (
            str(draft.body or "")
            .replace("{{地点}}", "旧城通道")
            .replace("{{角色}}", "韩青")
        )
        output = draft.model_copy(update={"body": body, "char_count": len(body)})
        return RewriteResult(
            status="rewritten",
            issue_kind=issue_kind,
            mode="deterministic_placeholder",
            writer_output=output,
        )

    def _drop_duplicate_paragraphs(
        self,
        *,
        draft: WriterOutput,
        issue_kind: str,
    ) -> RewriteResult:
        paragraphs = [item for item in str(draft.body or "").split("\n") if item.strip()]
        deduped = list(dict.fromkeys(paragraphs))
        body = "\n".join(deduped)
        output = draft.model_copy(update={"body": body, "char_count": len(body)})
        return RewriteResult(
            status="rewritten",
            issue_kind=issue_kind,
            mode="drop_duplicate_paragraphs",
            writer_output=output,
        )

    def _strip_internal_state_keys(
        self,
        *,
        draft: WriterOutput,
        issue_kind: str,
    ) -> RewriteResult:
        blocked = ("state_changes=", "world_deltas=", "generation_meta=", "prompt_revision_hash=")
        lines = [
            line
            for line in str(draft.body or "").splitlines()
            if not any(token in line for token in blocked)
        ]
        body = "\n".join(lines)
        output = draft.model_copy(update={"body": body, "char_count": len(body)})
        return RewriteResult(
            status="rewritten",
            issue_kind=issue_kind,
            mode="strip_internal_state_keys",
            writer_output=output,
        )
