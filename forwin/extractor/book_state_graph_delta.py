from __future__ import annotations

import re
from hashlib import sha1
from typing import TYPE_CHECKING

from forwin.book_state.adapter import BookStateDeltaAdapter
from forwin.book_state.extraction_contract import (
    BookStateExtractionIssue,
    BookStateExtractionRequest,
    BookStateExtractionResult,
)
from forwin.book_state.writer_contract import WriterContractDeltaBuilder
from forwin.protocol.book_state import FactPatch, GraphDelta, GraphDeltaType, NodePatch
from forwin.extractor.world_v4 import WorldDeltaExtractor
from forwin.world_v4_review_gate import V4ReviewGate

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


DEFAULT_BOOK_STATE_LAYERS = {"world", "map", "cognition", "narrative"}
_FILTER_METADATA_KEYS = {"requested_book_state_layers", "filtered_patch_counts"}
_WORLD_CONTEXT_FIELDS = (
    "story_time",
    "operation",
    "target_type",
    "target_id",
    "source_type",
    "source_id",
    "world_line_id",
    "summary",
)


def _normalize_book_state_layers(layers: set[str] | None) -> set[str]:
    if layers is None:
        return set(DEFAULT_BOOK_STATE_LAYERS)
    normalized = {str(layer).strip().lower() for layer in layers if str(layer).strip()}
    unknown = normalized - DEFAULT_BOOK_STATE_LAYERS
    if unknown:
        raise ValueError(
            "unknown BookState extraction layers: " + ", ".join(sorted(unknown))
        )
    return normalized or set(DEFAULT_BOOK_STATE_LAYERS)


def _has_meaningful_graph_delta_content(delta: GraphDelta, layers: set[str]) -> bool:
    if (
        delta.node_patches
        or delta.edge_patches
        or delta.fact_patches
        or delta.map_patches
        or delta.cognition_patches
        or delta.narrative_patches
    ):
        return True
    if "world" not in layers or str(delta.delta_type) != "world_state":
        return False
    if any(str(getattr(delta, field, "") or "").strip() for field in _WORLD_CONTEXT_FIELDS):
        return True
    if delta.evidence_refs:
        return True
    metadata = {
        key: value
        for key, value in dict(delta.metadata).items()
        if key not in _FILTER_METADATA_KEYS
    }
    return bool(metadata)


def _merge_filtered_patch_counts(
    existing: object,
    removed_counts: dict[str, int],
) -> dict[str, int]:
    merged: dict[str, int] = {}
    if isinstance(existing, dict):
        for key, value in existing.items():
            layer = str(key)
            if layer not in DEFAULT_BOOK_STATE_LAYERS:
                continue
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                merged[layer] = count
    for key, value in removed_counts.items():
        if value > 0:
            merged[key] = merged.get(key, 0) + value
    return merged


def _filter_graph_delta_layers(
    graph_deltas: list[GraphDelta],
    layers: set[str],
) -> list[GraphDelta]:
    layers = _normalize_book_state_layers(layers)
    requested = sorted(str(layer) for layer in layers)
    filtered: list[GraphDelta] = []
    for delta in graph_deltas:
        update: dict[str, object] = {}
        counts = {
            "world": (
                len(delta.node_patches)
                + len(delta.edge_patches)
                + len(delta.fact_patches)
            ),
            "map": len(delta.map_patches),
            "cognition": len(delta.cognition_patches),
            "narrative": len(delta.narrative_patches),
        }
        if "world" not in layers:
            update["node_patches"] = []
            update["edge_patches"] = []
            update["fact_patches"] = []
        if "map" not in layers:
            update["map_patches"] = []
        if "cognition" not in layers:
            update["cognition_patches"] = []
        if "narrative" not in layers:
            update["narrative_patches"] = []
        metadata = dict(delta.metadata)
        removed_counts = {
            key: value
            for key, value in counts.items()
            if key not in layers and value > 0
        }
        update["metadata"] = {
            **metadata,
            "requested_book_state_layers": requested,
            "filtered_patch_counts": _merge_filtered_patch_counts(
                metadata.get("filtered_patch_counts"),
                removed_counts,
            ),
        }
        filtered_delta = delta.model_copy(update=update)
        if _has_meaningful_graph_delta_content(filtered_delta, layers):
            filtered.append(filtered_delta)
    return filtered


class BookStateGraphDeltaExtractor:
    """Deterministically extract BookState GraphDelta candidates from writer output.

    The first direct-path slice reuses the existing deterministic world_v4
    extraction rules, then converts the approved result into BookState
    GraphDelta candidates. The orchestrator no longer treats the world_v4
    compiler as the canon success condition.
    """

    def __init__(
        self,
        *,
        layers: set[str] | None = None,
        session: Session | None = None,
    ) -> None:
        self.layers = _normalize_book_state_layers(layers)
        self.session = session

    def extract(self, request: BookStateExtractionRequest) -> BookStateExtractionResult:
        writer_output = request.writer_output.model_copy(update={"project_id": request.project_id})
        extracted = WorldDeltaExtractor().extract(
            writer_output,
            chapter_intent=request.chapter_intent,
        )
        gate_verdict = V4ReviewGate().review(
            extracted,
            chapter_intent=request.chapter_intent,
            chapter_body=writer_output.body,
        )
        if not gate_verdict.passed or gate_verdict.approved_changes is None:
            return BookStateExtractionResult(
                project_id=request.project_id,
                chapter_number=request.chapter_number,
                accepted=False,
                compatibility_extracted=extracted,
                compatibility_gate_verdict=gate_verdict,
                issues=[
                    BookStateExtractionIssue(
                        severity="error" if issue.severity == "fail" else issue.severity,
                        code=issue.failure_type,
                        message=issue.message,
                        evidence_refs=list(issue.evidence_refs),
                        metadata={
                            "reviewer": issue.reviewer,
                            "repair_patch": dict(issue.repair_patch),
                        },
                    )
                    for issue in gate_verdict.issues
                ],
                metadata={"extraction_path": "book_state_direct"},
            )

        changes = BookStateDeltaAdapter().from_world_change_set(
            gate_verdict.approved_changes,
            approved_by=["book_state_direct_extractor"],
            review_verdict_id=(
                request.review_verdict_id
                or f"book_state_direct_extract_{request.project_id}_{request.chapter_number}"
            ),
            forced_accept_reason=request.forced_accept_reason,
        )
        compatibility_deltas = [
            delta.model_copy(
                update={
                    "metadata": {
                        **dict(delta.metadata),
                        "extraction_path": "book_state_direct",
                        "compatibility_source": "world_v4_extractor",
                    }
                }
            )
            for delta in changes.graph_deltas
        ]
        graph_deltas = _filter_graph_delta_layers(compatibility_deltas, self.layers)
        if self.session is not None:
            contract_result = WriterContractDeltaBuilder(self.session).build(
                project_id=request.project_id,
                chapter_number=request.chapter_number,
                writer_output=writer_output,
                review_verdict_id=(
                    request.review_verdict_id
                    or f"book_state_direct_extract_{request.project_id}_{request.chapter_number}"
                ),
            )
            if contract_result.issues:
                return BookStateExtractionResult(
                    project_id=request.project_id,
                    chapter_number=request.chapter_number,
                    accepted=False,
                    compatibility_extracted=extracted,
                    compatibility_gate_verdict=gate_verdict,
                    issues=[
                        BookStateExtractionIssue(
                            severity="error",
                            code=issue.code,
                            message=issue.message,
                            evidence_refs=list(issue.evidence_refs),
                        )
                        for issue in contract_result.issues
                    ],
                    metadata={"extraction_path": "writer_contract"},
                )
            graph_deltas.extend(contract_result.graph_deltas)
        if not graph_deltas and "world" in self.layers and _needs_light_structured_extraction(writer_output):
            light_delta = _light_state_extraction_delta(
                project_id=request.project_id,
                chapter_number=request.chapter_number,
                writer_output=writer_output,
                review_verdict_id=(
                    request.review_verdict_id
                    or f"book_state_direct_extract_{request.project_id}_{request.chapter_number}"
                ),
            )
            if light_delta is None:
                return BookStateExtractionResult(
                    project_id=request.project_id,
                    chapter_number=request.chapter_number,
                    accepted=False,
                    compatibility_extracted=extracted,
                    compatibility_gate_verdict=gate_verdict,
                    issues=[
                        BookStateExtractionIssue(
                            severity="error",
                            code="light_state_extraction_empty",
                            message="轻量 BookState 提取没有得到角色、物品或势力节点，不能降级为 summary-only fact。",
                        )
                    ],
                    metadata={"extraction_path": "pulp_light_state_extraction"},
                )
            graph_deltas = [light_delta]
        changes = changes.model_copy(update={"graph_deltas": graph_deltas})
        return BookStateExtractionResult(
            project_id=request.project_id,
            chapter_number=request.chapter_number,
            accepted=True,
            changes=changes,
            compatibility_extracted=extracted,
            compatibility_gate_verdict=gate_verdict,
            metadata={
                "extraction_path": "book_state_direct",
                "graph_delta_count": len(graph_deltas),
            },
        )


def _needs_light_structured_extraction(writer_output) -> bool:  # noqa: ANN001
    meta = dict(getattr(writer_output, "generation_meta", {}) or {})
    status = str(meta.get("structured_extraction", "") or "").strip()
    mode = str(meta.get("mode", "") or "").strip()
    return status in {"deferred", "skipped", "degraded", "partial_degraded"} or mode == "single"


def _light_state_extraction_delta(
    *,
    project_id: str,
    chapter_number: int,
    writer_output,
    review_verdict_id: str,
) -> GraphDelta | None:
    body = str(getattr(writer_output, "body", "") or "")
    summary = (
        str(getattr(writer_output, "end_of_chapter_summary", "") or "").strip()
        or _first_sentence(body)
        or str(getattr(writer_output, "title", "") or f"第{chapter_number}章").strip()
    )
    title = str(getattr(writer_output, "title", "") or f"第{chapter_number}章").strip()
    characters = _extract_light_entities_from_mentions(
        writer_output,
        kinds={"", "character", "person"},
    )
    characters.extend(
        match.group(1)
        for match in re.finditer(
            r"(?:^|[，。！？；、\s])([\u4e00-\u9fff]{2,4})(?=收下|获得|进入|决定|发现|交给|看见|确认|拿起|带走|说)",
            body,
        )
    )
    possessions = _extract_light_entities_from_mentions(
        writer_output,
        kinds={"item", "artifact", "resource", "object"},
    )
    possessions.extend(
        match.group(1)
        for match in re.finditer(
            r"(?:收下|获得|拿起|带走|交给|打开|拾起|取出)([\u4e00-\u9fff]{1,8}(?:令|剑|刀|盒|钥匙|档案|芯片|账本|锚点|戒指|印章))",
            body,
        )
    )
    factions = _extract_light_entities_from_mentions(
        writer_output,
        kinds={"faction", "group", "organization", "organisation", "family", "sect"},
    )
    factions.extend(
        match.group(1)
        for match in re.finditer(
            r"(?:进入|来到|前往|离开|拜入|加入|对抗|寻找)([\u4e00-\u9fff]{2,10}(?:阁|会|派|盟|集团|公司|局|队|宗|门))",
            body,
        )
    )
    characters = _unique_light_names(characters, min_len=2, max_len=12)
    possessions = _unique_light_names(possessions, min_len=2, max_len=12)
    factions = _unique_light_names(factions, min_len=2, max_len=14)
    if not characters and not possessions and not factions:
        return None

    digest = sha1(
        f"{project_id}:{chapter_number}:{summary}:{characters}:{possessions}:{factions}".encode("utf-8")
    ).hexdigest()[:12]
    fact_id = f"fact_pulp_light_ch{chapter_number}_{digest}"
    source_ref = f"chapter:{chapter_number}"
    node_patches: list[NodePatch] = []
    related_refs: list[str] = []
    for name in characters:
        node_id = _light_node_id("character", project_id, name)
        related_refs.append(node_id)
        node_patches.append(
            _light_node_patch(
                node_id=node_id,
                node_type="character",
                name=name,
                project_id=project_id,
                chapter_number=chapter_number,
                source_ref=source_ref,
                summary=f"第{chapter_number}章出现的角色：{name}",
                tags=["pulp_light", "character"],
            )
        )
    for name in possessions:
        node_id = _light_node_id("item", project_id, name)
        related_refs.append(node_id)
        node_patches.append(
            _light_node_patch(
                node_id=node_id,
                node_type="item",
                name=name,
                project_id=project_id,
                chapter_number=chapter_number,
                source_ref=source_ref,
                summary=f"第{chapter_number}章出现的物品：{name}",
                tags=["pulp_light", "possession"],
            )
        )
    for name in factions:
        node_id = _light_node_id("faction", project_id, name)
        related_refs.append(node_id)
        node_patches.append(
            _light_node_patch(
                node_id=node_id,
                node_type="faction",
                name=name,
                project_id=project_id,
                chapter_number=chapter_number,
                source_ref=source_ref,
                summary=f"第{chapter_number}章出现的势力：{name}",
                tags=["pulp_light", "faction"],
            )
        )
    return GraphDelta(
        id=f"delta_pulp_light_state_ch{chapter_number}_{digest}",
        project_id=project_id,
        chapter_number=chapter_number,
        delta_type=GraphDeltaType.WORLD_STATE,
        operation="pulp_light_state_extraction",
        target_type="chapter_state",
        target_id=f"chapter_state:{chapter_number}",
        source_type="writer_output",
        source_id=source_ref,
        world_line_id="main",
        summary=summary,
        node_patches=node_patches,
        fact_patches=[
            FactPatch(
                fact_id=fact_id,
                op="create",
                proposition=summary,
                truth_value="true",
                related_refs=related_refs,
                new_value={
                    "project_id": project_id,
                    "fact_type": "key_event",
                    "created_at_chapter": chapter_number,
                    "source_refs": [source_ref],
                    "related_node_refs": related_refs,
                    "metadata": {
                        "title": title,
                        "extraction_path": "pulp_light_state_extraction",
                    },
                },
                reason="Light BookState fact linked to extracted chapter state nodes.",
            )
        ],
        evidence_refs=[source_ref],
        review_verdict_id=review_verdict_id,
        metadata={
            "extraction_path": "pulp_light_state_extraction",
            "compatibility_source": "empty_world_v4_extractor",
            "characters": characters,
            "possessions": possessions,
            "factions": factions,
            "structured_extraction": str(
                dict(getattr(writer_output, "generation_meta", {}) or {}).get("structured_extraction", "")
            ),
        },
    )


def _extract_light_entities_from_mentions(writer_output, *, kinds: set[str]) -> list[str]:  # noqa: ANN001
    names: list[str] = []
    for mention in getattr(writer_output, "entity_mentions", []) or []:
        kind = str(getattr(mention, "entity_kind", "") or "").strip().lower()
        if kind not in kinds:
            continue
        if getattr(mention, "is_named", True) is False:
            continue
        names.append(str(getattr(mention, "entity_name", "") or ""))
    return names


def _unique_light_names(
    names: list[str],
    *,
    min_len: int,
    max_len: int,
) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in names:
        text = re.sub(r"[《》“”\"'，。！？、；：\s]+", "", str(name or "").strip())
        if not text or not (min_len <= len(text) <= max_len):
            continue
        if not re.search(r"[\u4e00-\u9fff]", text):
            continue
        if text in {"决定", "进入", "收下", "获得", "发现", "确认", "拿起", "带走", "交给"}:
            continue
        if text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _light_node_patch(
    *,
    node_id: str,
    node_type: str,
    name: str,
    project_id: str,
    chapter_number: int,
    source_ref: str,
    summary: str,
    tags: list[str],
) -> NodePatch:
    return NodePatch(
        node_id=node_id,
        node_type=node_type,
        op="create",
        new_value={
            "project_id": project_id,
            "name": name,
            "summary": summary,
            "description": summary,
            "status": "active" if node_type == "character" else "known",
            "importance": 4 if node_type == "character" else 3,
            "tags": tags,
            "created_at_chapter": chapter_number,
            "valid_from_chapter": chapter_number,
            "source_refs": [source_ref],
            "state": {
                "status": "active" if node_type == "character" else "known",
                "first_seen_chapter": chapter_number,
            },
            "metadata": {"extraction_path": "pulp_light_state_extraction"},
        },
        reason="Light BookState extraction from accepted deferred structured output.",
    )


def _light_node_id(kind: str, project_id: str, name: str) -> str:
    digest = sha1(f"{project_id}:{kind}:{name}".encode("utf-8")).hexdigest()[:12]
    safe_kind = re.sub(r"[^a-z0-9_]+", "_", kind.lower()).strip("_") or "node"
    return f"{safe_kind}_{digest}"


def _first_sentence(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return ""
    for delimiter in ("。", "！", "？", ".", "!", "?"):
        index = normalized.find(delimiter)
        if index >= 0:
            return normalized[: index + 1]
    return normalized[:120]


__all__ = ["BookStateGraphDeltaExtractor"]
