from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.query import BookStateQuery
from forwin.retrieval.source_identity import CanonReadBaseline
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.visibility import node_page_visibility
from forwin.models.canon_quality import CanonQualitySignalRow
from forwin.models.knowledge import KnowledgeProjectionPageRow
from forwin.obsidian.frontmatter import frontmatter_hidden
from forwin.protocol.world_model import (
    EvidenceRef,
    WorldContextPack,
    WorldModelConflict,
    WorldModelPage,
)

from .page_repository import KnowledgePageRepository
from .store import load_json


class KnowledgeContextQuery:
    """Build prompt context directly from BookState plus disposable projections."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.book_state = BookStateRepository(session)

    def build(
        self,
        *,
        project_id: str,
        chapter_number: int,
        query_terms: Iterable[str] | None = None,
        max_pages: int = 6,
        baseline=None,
    ) -> WorldContextPack:
        as_of = max(0, int(chapter_number or 0) - 1)
        baseline = baseline or CanonReadBaseline.capture(self.session, project_id, as_of_chapter=as_of)
        runtime = BookStateQuery(self.session, baseline=baseline).runtime(
            project_id,
            as_of_chapter=as_of,
        )
        snapshot = self.book_state.latest_world_snapshot(project_id, as_of)
        if snapshot is None and not (
            runtime.world.nodes_by_id
            or runtime.world.edges_by_id
            or runtime.world.facts_by_id
        ):
            return WorldContextPack()

        terms = [
            str(term or "").strip().lower()
            for term in query_terms or []
            if str(term or "").strip()
        ]
        pages = self._pick_pages(
            project_id=project_id,
            terms=terms,
            max_pages=max_pages,
            as_of_chapter=as_of,
            runtime=runtime,
        )
        baseline.assert_current(self.session)
        # Old projections may predate a visibility change or omit status/tags.
        # Apply current as-of Canon visibility to copies, retaining full content
        # for the reviewer without modifying the stored projection or its hash.
        for index, page in enumerate(pages):
            source_id = (
                page.canonical_source_id
                if page.canonical_source_type == "book_state_node"
                else str(page.frontmatter.get("node_id") or "")
            )
            if not source_id or page.page_type in {"book", "overview"}:
                continue
            graph = runtime.map if page.page_type == "map_node" else runtime.world
            node = graph.nodes_by_id.get(source_id)
            labels = {
                "visibility": node_page_visibility(node),
                "truth_relation": node.metadata.get("truth_relation", "true"),
            } if node is not None else {"visibility": "hidden"}
            if frontmatter_hidden(labels):
                pages[index] = page.model_copy(update={"frontmatter": {
                    **page.frontmatter, "visibility": "hidden",
                }})
        conflicts = self._active_quality_conflicts(project_id)
        snapshot_id = (
            snapshot.id if snapshot is not None else f"book_state:{project_id}:{as_of}"
        )
        digest = (
            snapshot.objective_graph_digest
            if snapshot is not None
            else _runtime_digest(runtime)
        )
        return WorldContextPack(
            snapshot_id=snapshot_id,
            as_of_chapter=as_of,
            world_model_digest=digest,
            world_model_refs={
                "book_state_snapshot_id": snapshot_id,
                "as_of_chapter": str(as_of),
            },
            relevant_world_pages=pages,
            active_world_conflicts=conflicts,
            active_secrets=[page for page in pages if page.page_type == "secret"],
            active_promises=[page for page in pages if page.page_type == "promise"],
            active_resource_constraints=[
                page for page in pages if page.page_type in {"resource", "currency"}
            ],
            active_institution_rules=[
                page for page in pages if page.page_type == "institution"
            ],
        )

    def _pick_pages(
        self,
        *,
        project_id: str,
        terms: list[str],
        max_pages: int,
        as_of_chapter: int,
        runtime,
    ) -> list[WorldModelPage]:
        rows = [
            row for row in KnowledgePageRepository(self.session).list_valid_rows(project_id, runtime=runtime, as_of_chapter=as_of_chapter)
            if int(row.as_of_chapter or 0) <= as_of_chapter
        ]

        def score(row: KnowledgeProjectionPageRow) -> tuple[int, int, str]:
            text = (
                f"{row.title}\n{row.page_key}\n{row.page_type}\n{row.markdown[:1000]}"
            ).lower()
            match_score = sum(3 for term in terms if term in text)
            priority = {
                "contradiction": 8,
                "secret": 7,
                "promise": 6,
                "character": 5,
                "faction": 4,
                "region": 3,
                "node": 3,
                "overview": 2,
            }.get(row.page_type, 1)
            return match_score, priority, row.title

        ranked = sorted(rows, key=score, reverse=True)
        selected = ranked[: max(1, int(max_pages or 1))]
        if not any(row.page_type == "overview" for row in selected):
            overview = next((row for row in rows if row.page_type == "overview"), None)
            if overview is not None:
                selected = [overview, *selected[: max(0, len(selected) - 1)]]
        return [_page(row) for row in selected]

    def _active_quality_conflicts(self, project_id: str) -> list[WorldModelConflict]:
        rows = (
            self.session.execute(
                select(CanonQualitySignalRow)
                .where(
                    CanonQualitySignalRow.project_id == project_id,
                    CanonQualitySignalRow.status == "open",
                )
                .order_by(
                    CanonQualitySignalRow.severity.desc(),
                    CanonQualitySignalRow.created_at.desc(),
                )
                .limit(8)
            )
            .scalars()
            .all()
        )
        return [
            WorldModelConflict(
                id=row.id,
                conflict_type=row.signal_type,
                severity=row.severity,
                subject_key=row.subject_key,
                description=row.description,
                evidence_refs=[
                    EvidenceRef(
                        source_type="canon_quality",
                        source_id=row.signal_id,
                        chapter_number=int(row.chapter_number or 0),
                        summary=row.description,
                    )
                ],
                status=row.status,
            )
            for row in rows
        ]


def _page(row: KnowledgeProjectionPageRow) -> WorldModelPage:
    return WorldModelPage(
        id=row.id,
        page_key=row.page_key,
        page_type=row.page_type,
        title=row.title,
        vault_path=row.vault_path,
        markdown=row.markdown,
        frontmatter=load_json(row.frontmatter_json, {}),
        content_hash=row.content_hash,
        revision=row.revision,
        status=row.status,
        as_of_chapter=row.as_of_chapter,
        logical_identity_key=row.logical_identity_key,
        canonical_source_type=row.canonical_source_type,
        canonical_source_id=row.canonical_source_id,
        supersedes_page_id=row.supersedes_page_id,
        canonical_rank=row.canonical_rank,
    )


def _runtime_digest(runtime) -> str:  # noqa: ANN001
    payload = {
        "world": runtime.world.snapshot(),
        "narrative": runtime.narrative.snapshot(),
        "as_of_chapter": runtime.as_of_chapter,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["KnowledgeContextQuery"]
