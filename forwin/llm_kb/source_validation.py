"""Validate a compiled file set against current Canon and its exact file bytes."""

from __future__ import annotations
import json
from forwin.book_state.query import BookStateQuery
from forwin.knowledge_system.dependencies import dependencies_valid, llm_kb_inputs
from forwin.retrieval.source_identity import text_hash


def compiled_manifest(root, project_id):
    """Read the current local generation identity, without asserting Canon validity."""
    try:
        manifest = json.loads(
            (root / project_id / "retrieval_index.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("file_hashes"), dict):
        return {}
    try:
        as_of = int(manifest.get("as_of_chapter", -1))
    except (ValueError, TypeError):
        return {}
    if manifest.get("project_id") != project_id or as_of < 0:
        return {}
    return manifest


def validated_manifest(root, project_id, session, baseline):
    if session is None or baseline is None:
        return {}
    baseline.assert_current(session, project_id=project_id)
    manifest = compiled_manifest(root, project_id)
    if not manifest or int(manifest["as_of_chapter"]) > baseline.as_of_chapter:
        return {}
    runtime = BookStateQuery(session, baseline=baseline).runtime(
        project_id, as_of_chapter=baseline.as_of_chapter
    )
    extra = llm_kb_inputs(session, project_id, baseline.as_of_chapter)
    if not dependencies_valid(
        manifest.get("dependency_manifest"), runtime, extra=extra
    ):
        return {}
    return manifest


def validated_file(root, project_id, key, manifest):
    # Keys must come from both the compiled manifest and a fixed namespace.
    from .store import ROOT_FILE_KEYS

    role_paths = {
        f"packs/{role}/context.json"
        for role in ("writer", "reviewer", "planner", "compiler")
    }
    if key not in ROOT_FILE_KEYS | role_paths:
        return None
    expected = manifest.get("file_hashes", {}).get(key)
    if not expected:
        return None
    try:
        content = (root / project_id / key).read_text(encoding="utf-8")
    except OSError:
        return None
    return content if text_hash(content) == expected else None
