from __future__ import annotations

from .map_context import (
    _build_genesis_map_overview,
    _visible_map_edge,
    _resolve_map_node_id,
    _visible_neighbors,
    _genesis_active_location_refs,
    _append_review_node_id,
    _review_graph_node_ids,
    _review_graph_edges,
    _build_map_context,
    _map_node_payloads,
    _map_edge_payload,
)

from .book_state_overlay import (
    _book_state_context_overlay,
    _resolve_book_state_location_id,
    _book_state_neighbors,
    _merge_book_state_map_overlay,
)

from .personality_integrity import (
    _project_personality_integrity_strict,
    _personality_integrity_issues,
    _save_personality_integrity_failure,
)

from .canon_quality_context import (
    _build_canon_quality_context,
    _truthy,
    _recent_canon_custody_constraints,
    _candidate_recent_canon_character_names,
    _custody_state_from_recent_text,
    _last_clause_fragment,
    _first_clause_fragment,
    _is_final_chapter_for_context,
    _looks_like_final_chapter_label,
)

from .assembler import (
    ChapterContextAssembler,
    assemble_context,
)

__all__ = [
    '_build_genesis_map_overview',
    '_visible_map_edge',
    '_resolve_map_node_id',
    '_visible_neighbors',
    '_genesis_active_location_refs',
    '_append_review_node_id',
    '_review_graph_node_ids',
    '_review_graph_edges',
    '_build_map_context',
    '_map_node_payloads',
    '_map_edge_payload',
    '_book_state_context_overlay',
    '_resolve_book_state_location_id',
    '_book_state_neighbors',
    '_merge_book_state_map_overlay',
    '_project_personality_integrity_strict',
    '_personality_integrity_issues',
    '_save_personality_integrity_failure',
    '_build_canon_quality_context',
    '_truthy',
    '_recent_canon_custody_constraints',
    '_candidate_recent_canon_character_names',
    '_custody_state_from_recent_text',
    '_last_clause_fragment',
    '_first_clause_fragment',
    '_is_final_chapter_for_context',
    '_looks_like_final_chapter_label',
    'ChapterContextAssembler',
    'assemble_context',
]
