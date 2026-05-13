from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.world_model import WorldModelConflictRow, WorldModelPageRow
from forwin.protocol.world_model import WorldContextPack

from .page_repository import WorldModelPageRepository
from .store import conflict_to_schema, page_to_schema, snapshot_to_schema
from .store import WorldModelStore


class WorldModelRetriever:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.store = WorldModelStore(session)

    def build_context(
        self,
        *,
        project_id: str,
        chapter_number: int,
        query_terms: Iterable[str] | None = None,
        max_pages: int = 6,
    ) -> WorldContextPack:
        as_of = max(0, int(chapter_number or 0) - 1)
        snapshot_row = self.store.latest_snapshot(project_id, as_of_chapter=as_of)
        if snapshot_row is None and as_of == 0:
            snapshot_row = self.store.latest_snapshot(project_id, as_of_chapter=0)
        if snapshot_row is None:
            return WorldContextPack()
        snapshot = snapshot_to_schema(snapshot_row)
        terms = [str(term).strip().lower() for term in (query_terms or []) if str(term).strip()]
        pages = self._pick_pages(project_id=project_id, terms=terms, max_pages=max_pages)
        conflicts = [
            conflict_to_schema(row)
            for row in self.session.execute(
                select(WorldModelConflictRow)
                .where(WorldModelConflictRow.project_id == project_id, WorldModelConflictRow.status == "open")
                .order_by(WorldModelConflictRow.severity.desc(), WorldModelConflictRow.created_at.desc())
                .limit(8)
            ).scalars().all()
        ]
        secrets = [page for page in pages if page.page_type == "secret"]
        promises = [page for page in pages if page.page_type == "promise"]
        resources = [page for page in pages if page.page_type in {"resource", "currency"}]
        institutions = [page for page in pages if page.page_type == "institution"]
        return WorldContextPack(
            snapshot_id=snapshot.id,
            as_of_chapter=snapshot.as_of_chapter,
            world_model_digest=snapshot.source_digest,
            world_model_refs={
                "snapshot_id": snapshot.id,
                "as_of_chapter": str(snapshot.as_of_chapter),
            },
            relevant_world_pages=pages,
            active_world_conflicts=conflicts,
            active_secrets=secrets,
            active_promises=promises,
            active_resource_constraints=resources,
            active_institution_rules=institutions,
        )

    def _pick_pages(self, *, project_id: str, terms: list[str], max_pages: int) -> list:
        rows = WorldModelPageRepository(self.session).list_canonical_rows(project_id)
        if not rows:
            return []

        def score(row: WorldModelPageRow) -> tuple[int, int, str]:
            text = f"{row.title}\n{row.page_key}\n{row.page_type}\n{row.markdown[:1000]}".lower()
            match_score = sum(3 for term in terms if term and term in text)
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
            return (match_score, priority, row.title)

        ranked = sorted(rows, key=score, reverse=True)
        selected = ranked[: max(1, int(max_pages or 1))]
        if not any(row.page_type == "overview" for row in selected):
            overview = next((row for row in rows if row.page_type == "overview"), None)
            if overview is not None:
                selected = [overview, *selected[: max(0, len(selected) - 1)]]
        return [page_to_schema(row) for row in selected]
