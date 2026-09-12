from __future__ import annotations

from forwin.book_state.query import BookStateQuery
from forwin.context.request import ContextDraft, ContextRequest
from forwin.review.query import ReviewQuery


class StateContextProvider:
    name = "state"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        repo = request.repo
        chapter_plan = request.chapter_plan
        project_id = request.project_id
        project = repo.get_project(project_id)
        if request.session is None:
            raise RuntimeError("StateContextProvider requires a database session")
        as_of_chapter = max(int(chapter_plan.chapter_number) - 1, 0)
        book_state = BookStateQuery(request.session, baseline=request.baseline)
        review_query = ReviewQuery(request.session)
        entities = book_state.active_entities(
            project_id,
            as_of_chapter=as_of_chapter,
        )
        allowed_entities = [
            entity.name for entity in entities if entity.kind == "character"
        ]
        relations = book_state.active_relations(
            project_id,
            as_of_chapter=as_of_chapter,
            entity_names=allowed_entities,
        )

        draft.data.update(
            {
                "project": project,
                "entities": entities,
                "allowed_entities": allowed_entities,
                "relations": relations,
                "threads": book_state.active_threads(
                    project_id,
                    as_of_chapter=as_of_chapter,
                ),
                "summaries": review_query.chapter_summaries(
                    project_id,
                    before_chapter=chapter_plan.chapter_number,
                ),
                "timeline": book_state.current_timeline(
                    project_id,
                    as_of_chapter=as_of_chapter,
                ),
                "world_pressure": repo.get_latest_world_pressure(
                    project_id, before_chapter=chapter_plan.chapter_number
                ),
                "active_subworlds": repo.get_active_subworld_summary(
                    project_id, chapter_plan.chapter_number
                ),
                "runtime_region_drafts": repo.get_active_subworld_region_drafts(
                    project_id, chapter_plan.chapter_number
                ),
            }
        )
