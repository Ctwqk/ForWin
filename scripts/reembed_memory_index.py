#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass

from sqlalchemy import select

from forwin.config import InfrastructureConfig
from forwin.models import ChapterDraft, ChapterPlan
from forwin.models.base import get_engine, get_session_factory, require_v5_schema
from forwin.retrieval.memory_index import ChapterMemoryIndex, create_memory_index


@dataclass(frozen=True)
class ReembedResult:
    scanned: int = 0
    upserted: int = 0
    skipped_without_draft: int = 0
    dry_run: bool = False


class _DryRunMemoryIndex:
    def upsert_chapter(self, **_kwargs) -> None:  # noqa: ANN003
        return None


def reembed_project_memories(
    *,
    session_factory,
    memory_index: ChapterMemoryIndex,
    project_id: str = "",
    from_chapter: int = 1,
    to_chapter: int = 10_000,
    limit: int = 0,
    dry_run: bool = False,
) -> ReembedResult:
    scanned = 0
    upserted = 0
    skipped_without_draft = 0
    with session_factory() as session:
        statement = (
            select(ChapterPlan)
            .where(
                ChapterPlan.status == "accepted",
                ChapterPlan.chapter_number >= int(from_chapter),
                ChapterPlan.chapter_number <= int(to_chapter),
            )
            .order_by(ChapterPlan.project_id.asc(), ChapterPlan.chapter_number.asc())
        )
        if project_id:
            statement = statement.where(ChapterPlan.project_id == project_id)
        if int(limit or 0) > 0:
            statement = statement.limit(int(limit))
        plans = session.execute(statement).scalars().all()
        for plan in plans:
            scanned += 1
            draft = session.execute(
                select(ChapterDraft)
                .where(ChapterDraft.chapter_plan_id == plan.id)
                .order_by(ChapterDraft.version.desc(), ChapterDraft.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if draft is None:
                skipped_without_draft += 1
                continue
            if not dry_run:
                memory_index.upsert_chapter(
                    project_id=str(plan.project_id),
                    chapter_number=int(plan.chapter_number or 0),
                    title=str(plan.title or f"第{plan.chapter_number}章"),
                    summary=str(draft.summary or ""),
                    body=str(draft.body_text or ""),
                )
            upserted += 1
    return ReembedResult(
        scanned=scanned,
        upserted=upserted,
        skipped_without_draft=skipped_without_draft,
        dry_run=dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the chapter memory Qdrant index from accepted drafts."
    )
    parser.add_argument("--project-id", default="")
    parser.add_argument("--from-chapter", type=int, default=1)
    parser.add_argument("--to-chapter", type=int, default=10_000)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--collection", default="")
    parser.add_argument("--embedding-backend", default="")
    parser.add_argument("--embedding-base-url", default="")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--embedding-dims", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    config = InfrastructureConfig.from_env()
    engine = get_engine(config.database_url)
    require_v5_schema(engine)
    session_factory = get_session_factory(engine)
    collection = args.collection or config.qdrant_collection
    embedding_backend = args.embedding_backend or config.embedding_backend
    embedding_base_url = args.embedding_base_url or config.embedding_base_url
    embedding_model = args.embedding_model or config.embedding_model
    embedding_dims = args.embedding_dims or config.embedding_dims
    memory_index: ChapterMemoryIndex = (
        _DryRunMemoryIndex()
        if args.dry_run
        else create_memory_index(
            backend=config.retrieval_backend,
            root_dir=config.retrieval_root,
            qdrant_url=config.qdrant_url,
            qdrant_collection=collection,
            embedding_backend=embedding_backend,
            embedding_base_url=embedding_base_url,
            embedding_api_key=config.embedding_api_key,
            embedding_model=embedding_model,
            embedding_dims=embedding_dims,
            embedding_required=config.embedding_required,
        )
    )
    try:
        result = reembed_project_memories(
            session_factory=session_factory,
            memory_index=memory_index,
            project_id=args.project_id,
            from_chapter=args.from_chapter,
            to_chapter=args.to_chapter,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        print(
            "reembed complete: "
            f"scanned={result.scanned} "
            f"upserted={result.upserted} "
            f"skipped_without_draft={result.skipped_without_draft} "
            f"collection={collection} "
            f"embedding_backend={embedding_backend} "
            f"dry_run={result.dry_run}"
        )
        if not args.dry_run:
            actual_dims = _memory_index_vector_dims(memory_index)
            expected_dims = int(embedding_dims or 0)
            if expected_dims > 0 and actual_dims > 0 and actual_dims != expected_dims:
                print(
                    "reembed failed: "
                    f"collection vector dims={actual_dims} expected={expected_dims}"
                )
                return 2
        return 0
    finally:
        close = getattr(memory_index, "close", None)
        try:
            if callable(close):
                close()
        finally:
            engine.dispose()


def _memory_index_vector_dims(memory_index: ChapterMemoryIndex) -> int:
    collection_size = getattr(memory_index, "collection_vector_size", None)
    if callable(collection_size):
        value = collection_size()
        if value:
            return int(value)
    status = getattr(memory_index, "embedding_status", None)
    if callable(status):
        payload = status()
        if isinstance(payload, dict):
            return int(payload.get("dims") or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
