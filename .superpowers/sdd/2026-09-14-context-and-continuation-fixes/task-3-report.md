# Task 3 report: business timestamps and shared dependency preparation

## Status

DONE

Implementation was reviewed against `task-3-brief.md`, `global-constraints.md`, and the Task 3 preparation notes in `source-notes.md`. Production behavior was based on source commit `204748a0ccc326e5a1521dd2cce59e820c302d65`; the branch also contains the controller's later docs-only commit `101644d7756d68df2243c72bbe346dd52fe4a8be`.

## What changed

- Raised the knowledge dependency manifest rule version from 1 to 2. Version 1 and malformed manifests now fail closed through the existing `dependencies_valid` owner.
- Removed the generic recursive exclusion for `created_at`, `updated_at`, and `refreshed_at`. Those names now remain semantic when they occur in WorldNode state, character profile, metadata, nested evidence, cognition metadata, facts, or other business payloads.
- Moved the only system-audit exclusion to the exact ORM extraction boundary in `llm_kb_inputs`: top-level `created_at` and `updated_at` columns on `ArcWorldContractRow` and `ChapterWorldDeltaIntentRow`. Business values with the same names inside contract/intent JSON remain fingerprinted.
- Added a per-runtime `DependencyPreparation`. It hashes cognition once, indexes world/map edge adjacency once, and lazily materializes each world node with current runtime state once. There is no module/process-global cache.
- `KnowledgePageRepository.list_valid_rows` creates one fresh preparation per call, parses each stored dependency manifest once, and uses the shared preparation for every row.
- `ObsidianExporter.export_project` creates one fresh preparation for every export, reuses its materialized world nodes for rendering and dependency proofs, and replaces it when the same exporter instance starts a later export.
- `LLMKnowledgeBaseCompiler` uses the same preparation owner for rendered nodes and its whole-book dependency manifest. Whole-book and LLM-KB scopes retain world nodes, world/map edges, facts, narrative state, cognition, and their extra source inputs.
- Existing projection rebuild remains the recovery path: old proofs are ignored without changing their Markdown/content audit, and the next export writes a current v2 proof.

## TDD evidence

### RED

Command:

```text
FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test .venv/bin/python -m pytest -q tests/test_knowledge_dependencies.py
```

Observed before production changes:

```text
8 failed, 2 passed in 7.46s
```

Expected failures established that:

- changing `state.created_at`, `profile.updated_at`, or nested evidence `refreshed_at` left a node manifest valid;
- current manifests still had version 1 and legacy version 1 proofs were accepted;
- real `list_valid_rows` validation performed 10,000 world-state reads for 100 node pages instead of 100;
- real map-only validation performed 20,000 unrelated world-state reads for 200 map pages instead of zero;
- the actual Writer read accepted a page carrying a legacy proof;
- the real export/render path performed 10,400 world-state reads at 100 nodes, exceeding the linear bound.

The LLM-KB audit-noise/business-payload integration test already passed in RED because the former global recursive exclusion happened to ignore the ORM audit column. It stayed in the suite to prevent the narrower extraction fix from regressing that behavior while business timestamps were restored.

### GREEN

Focused command after implementation:

```text
FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test .venv/bin/python -m pytest -q tests/test_knowledge_dependencies.py
```

Observed:

```text
10 passed in 7.63s
```

After self-review added explicit whole-book fact invalidation coverage, the focused dependency plus existing adjacency/cognition regression command was:

```text
FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test .venv/bin/python -m pytest -q tests/test_knowledge_dependencies.py tests/test_canon_read_baseline.py::test_semantic_dependencies_cover_membership_and_actual_render_inputs
```

Observed:

```text
18 passed in 7.65s
```

The performance assertions exercise production paths. They establish exactly 100 state reads and one cognition serialization for 100 node rows, one manifest parse per row, zero world-state reads for 200 map-only rows, and linear export bounds across 100 then 200 nodes using the same exporter instance.

## Final verification

Command:

```text
FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test .venv/bin/python -m pytest -q tests/test_knowledge_dependencies.py tests/test_knowledge_dependency_migration.py tests/test_history_input_contract.py tests/test_canon_read_baseline.py tests/test_knowledge_system_v46.py tests/test_obsidian_managed_manifest.py && /Users/magi1/.cache/uv/archive-v0/bS7R8jmAL67VQuBIQf_CG/ruff-0.16.7.data/scripts/ruff check forwin/knowledge_system/dependencies.py tests/test_knowledge_dependencies.py && .venv/bin/python -m py_compile forwin/knowledge_system/dependencies.py forwin/knowledge_system/page_repository.py forwin/obsidian/exporter.py forwin/llm_kb/compiler.py && git diff --check
```

Observed:

```text
129 passed in 22.22s
All checks passed!
```

This covers the new dependency tests plus existing knowledge migration, Writer/history input, Canon baseline and LLM-KB validation, V4.6 knowledge, and Obsidian managed-manifest regressions. Per task constraints, no model, live MCP, production, full-repository QA, push, or deployment call was made. The controller owns final full lint/QA and push.

## Files changed

- `forwin/knowledge_system/dependencies.py`
- `forwin/knowledge_system/page_repository.py`
- `forwin/obsidian/exporter.py`
- `forwin/llm_kb/compiler.py`
- `tests/test_knowledge_dependencies.py`
- `.superpowers/sdd/2026-09-14-context-and-continuation-fixes/task-3-report.md`

## Self-review

- Re-read the Task 3 checklist after implementation and verified timestamp ownership, old-manifest rejection/rebuild recovery, local versus whole-book scopes, adjacency/cognition/fact invalidation, real Writer and LLM-KB reads, actual list/export operation counts, and exporter-instance reuse.
- Confirmed the preparation object is scoped to its exact runtime identity and every production batch owner constructs it afresh. No schema, migration, global cache, alternate validation path, or source copy was added.
- Confirmed `list_valid_rows` still performs no superseding or other projection mutation while rejecting unknown/legacy proofs.
- Confirmed parent-owned docs and all pre-existing untracked files remain unstaged and unchanged by this task.

## Concerns

None. Full repository QA and deployment remain controller-owned by instruction.
