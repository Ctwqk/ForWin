from __future__ import annotations

import re
from hashlib import sha1
from typing import TYPE_CHECKING

from forwin.book_state.extraction.contract import (
    BookStateExtractionIssue,
    BookStateExtractionRequest,
    BookStateExtractionResult,
)
from forwin.book_state.writer_contract import WriterContractDeltaBuilder
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    FactPatch,
    GraphDelta,
    GraphDeltaType,
    NodePatch,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


DEFAULT_BOOK_STATE_LAYERS = {"world", "map", "cognition", "narrative"}


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


class BookStateGraphDeltaExtractor:
    """Extract BookState GraphDelta candidates from writer output.

    The writer contract (time advance, state changes, events, payoffs and thread
    beats reported by the writer) is the primary source. Only when it yields no
    delta does the light structured-extraction fallback run. Canon admission
    remains a separate atomic operation.
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
        review_verdict_id = (
            request.review_verdict_id
            or f"book_state_direct_extract_{request.project_id}_{request.chapter_number}"
        )
        graph_deltas: list[GraphDelta] = []
        if self.session is not None:
            contract_result = WriterContractDeltaBuilder(self.session).build(
                project_id=request.project_id,
                chapter_number=request.chapter_number,
                writer_output=writer_output,
                review_verdict_id=review_verdict_id,
            )
            if contract_result.issues:
                return BookStateExtractionResult(
                    project_id=request.project_id,
                    chapter_number=request.chapter_number,
                    accepted=False,
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
                review_verdict_id=review_verdict_id,
            )
            if light_delta is None:
                return BookStateExtractionResult(
                    project_id=request.project_id,
                    chapter_number=request.chapter_number,
                    accepted=False,
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
        changes = ApprovedGraphDeltaSet(
            project_id=request.project_id,
            chapter_number=request.chapter_number,
            graph_deltas=graph_deltas,
            approved_by=["book_state_direct_extractor"],
            review_verdict_id=review_verdict_id,
            forced_accept_reason=request.forced_accept_reason,
        )
        return BookStateExtractionResult(
            project_id=request.project_id,
            chapter_number=request.chapter_number,
            accepted=True,
            changes=changes,
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
            "extraction_source": "empty_book_state_extraction_delta",
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
