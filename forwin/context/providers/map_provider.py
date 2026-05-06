from __future__ import annotations

from forwin.context.request import ContextDraft, ContextRequest


class MapContextProvider:
    name = "map"

    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None:
        from forwin.context.assembler import _build_genesis_map_overview, _build_map_context

        map_context = _build_map_context(
            request.session,
            request.project_id,
            draft.data.get("entities", []),
            genesis_story_engine=draft.data.get("genesis_story_engine", {}),
        )
        draft.data.update(
            {
                "genesis_map_overview": _build_genesis_map_overview(
                    draft.data.get("genesis_map_atlas", {}),
                    draft.data.get("runtime_region_drafts", []),
                ),
                "map_context": map_context,
            }
        )
