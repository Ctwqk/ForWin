from __future__ import annotations

from forwin.context.request import ContextDraft, ContextRequest


class StateContextProvider:
    name = "state"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        repo = request.repo
        chapter_plan = request.chapter_plan
        project_id = request.project_id
        project = repo.get_project(project_id)

        allowed_entities_getter = getattr(repo, "get_allowed_entity_snapshots", None)
        if callable(allowed_entities_getter):
            entities = allowed_entities_getter(project_id, chapter_plan.chapter_number)
        else:
            entities = repo.get_active_entities(project_id)

        allowed_entities = [entity.name for entity in entities if entity.kind == "character"]
        relations_getter = getattr(repo, "get_active_relations")
        try:
            relations = relations_getter(project_id, entity_names=allowed_entities)
        except TypeError:
            relations = [
                relation
                for relation in relations_getter(project_id)
                if relation.source_name in allowed_entities or relation.target_name in allowed_entities
            ]

        npc_intents_getter = getattr(repo, "get_recent_npc_intents", None)
        world_pressure_getter = getattr(repo, "get_latest_world_pressure", None)
        active_subworld_summary_getter = getattr(repo, "get_active_subworld_summary", None)
        active_subworld_region_drafts_getter = getattr(repo, "get_active_subworld_region_drafts", None)
        draft.data.update(
            {
                "project": project,
                "entities": entities,
                "allowed_entities": allowed_entities,
                "relations": relations,
                "threads": repo.get_active_threads(project_id),
                "summaries": repo.get_chapter_summaries(project_id, chapter_plan.chapter_number),
                "timeline": repo.get_current_timeline(project_id),
                "npc_intents": (
                    npc_intents_getter(project_id, before_chapter=chapter_plan.chapter_number)
                    if callable(npc_intents_getter)
                    else []
                ),
                "world_pressure": (
                    world_pressure_getter(project_id, before_chapter=chapter_plan.chapter_number)
                    if callable(world_pressure_getter)
                    else None
                ),
                "active_subworlds": (
                    active_subworld_summary_getter(project_id, chapter_plan.chapter_number)
                    if callable(active_subworld_summary_getter)
                    else []
                ),
                "runtime_region_drafts": (
                    active_subworld_region_drafts_getter(project_id, chapter_plan.chapter_number)
                    if callable(active_subworld_region_drafts_getter)
                    else []
                ),
            }
        )
