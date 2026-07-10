from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

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
        context_pack: dict[str, object] | object,
    ) -> RewriteResult:
        del signals
        kind = str(issue_kind or "").strip()
        if kind in {"placeholder_leakage", "bare_role_placeholder_leakage"}:
            return self._rewrite_placeholder(
                draft=draft,
                issue_kind=kind,
                context_pack=context_pack,
            )
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

    def _rewrite_placeholder(
        self,
        *,
        draft: WriterOutput,
        issue_kind: str,
        context_pack: dict[str, object] | object,
    ) -> RewriteResult:
        body = str(draft.body or "")
        replacements: dict[str, str] = {}
        missing: list[str] = []
        if "{{角色}}" in body:
            character_anchor = _character_anchor(context_pack)
            if character_anchor:
                replacements["{{角色}}"] = character_anchor
            else:
                missing.append("character")
        if "{{地点}}" in body:
            location_anchor = _location_anchor(context_pack)
            if location_anchor:
                replacements["{{地点}}"] = location_anchor
            else:
                missing.append("location")
        if missing:
            return RewriteResult(
                status="needs_writer",
                issue_kind=issue_kind,
                mode="missing_canon_placeholder_anchor",
                instruction=(
                    "rewrite placeholder leakage with explicit canon anchors from "
                    f"context_pack; missing anchors: {', '.join(missing)}"
                ),
            )
        for token, value in replacements.items():
            body = body.replace(token, value)
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


def _character_anchor(context_pack: dict[str, object] | object) -> str:
    for item in _list_value(_context_value(context_pack, "active_entities")):
        if str(_item_value(item, "kind") or "").strip() == "character":
            name = _clean_anchor(_item_value(item, "name"))
            if name:
                return name
    for item in _list_value(_context_value(context_pack, "allowed_entities")):
        name = _clean_anchor(item)
        if name:
            return name
    for item in _list_value(_context_value(context_pack, "chapter_entry_targets")):
        name = _clean_anchor(_item_value(item, "entity_name"))
        if name:
            return name
    for item in _list_value(_context_value(context_pack, "active_personality_contexts")):
        name = _clean_anchor(_item_value(item, "character_name"))
        if name:
            return name
    return ""


def _location_anchor(context_pack: dict[str, object] | object) -> str:
    map_context = _context_value(context_pack, "map_context")
    for item in _list_value(_item_value(map_context, "active_locations")):
        name = _clean_anchor(
            _item_value(item, "location_name")
            or _item_value(item, "name")
            or _item_value(item, "location_id")
        )
        if name:
            return name
    for item in _list_value(_context_value(context_pack, "active_entities")):
        if str(_item_value(item, "kind") or "").strip() == "location":
            name = _clean_anchor(_item_value(item, "name"))
            if name:
                return name
    for item in _list_value(_item_value(map_context, "visible_anchor_nodes")):
        name = _clean_anchor(_item_value(item, "name") or _item_value(item, "node_id"))
        if name:
            return name
    return ""


def _context_value(context_pack: dict[str, object] | object, key: str) -> Any:
    if isinstance(context_pack, dict):
        return context_pack.get(key)
    return getattr(context_pack, key, None)


def _item_value(item: object, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _list_value(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _clean_anchor(value: object) -> str:
    text = str(value or "").strip()
    if not text or "{{" in text or "}}" in text:
        return ""
    if text in {"角色", "地点"}:
        return ""
    return text
