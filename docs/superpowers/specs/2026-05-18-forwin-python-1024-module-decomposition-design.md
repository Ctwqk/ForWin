# ForWin Python 1024-Line Module Decomposition Design

Date: 2026-05-18

Status: user-approved direction, pending written-spec review

## Scope

Split production Python modules that are over 1024 lines when they have clear internal structure and a low-risk compatibility path. Test files are explicitly out of scope and must not be split in this pass.

The goal is maintainability without behavior change. The first move for each target is a mechanical extraction behind the existing public module path. Only extract shared helpers when the split reveals real duplication or a stable cross-module boundary.

## Selection Rules

Include a module when all of these are true:

- it is production Python, not a test file
- it is over 1024 lines
- it has obvious domain sections, repeated helper clusters, or public facade behavior that can survive as a re-export shell
- targeted regression tests already exist or can be added without inventing a large fixture framework

Defer a module when it is over 1024 lines but is DB-heavy, transaction-heavy, or tightly coupled to runtime mutation, unless the implementation pass exposes a small helper extraction with clear tests.

## In-Scope Modules

Current production modules selected for the next decomposition batch:

- `forwin/canon_quality/countdown_ledger.py`
- `forwin/api_project_payloads.py`
- `forwin/api_schemas.py`
- `forwin/writer/prompts.py`
- `forwin/writer/llm_client.py`
- `forwin/context/assembler.py`
- `forwin/retrieval/broker.py`

These are selected because each has clear internal grouping and broad public imports that can be preserved through compatibility shells.

## Deferred Modules

These modules remain over 1024 lines for now:

- `forwin/state/repo.py`
- `forwin/book_state/repository.py`
- `forwin/state/updater.py`
- `forwin/world_model/compiler.py`
- `forwin/writer/chapter_writer.py`
- `forwin/orchestrator/phase4.py`

They are more stateful, DB-facing, or orchestration-heavy. They should be revisited after the lower-risk modules are split and after any common helpers from this batch settle.

## Global Compatibility Rules

- Keep every old public import path working.
- Keep old modules as thin compatibility shells and re-export facades.
- Do not rename public classes, Pydantic models, API response keys, or prompt builder functions.
- Prefer moving code first, then cleaning local duplication in a separate commit.
- Avoid new generic helper packages until at least two moved modules need the same helper.
- Keep new modules below 1024 lines, preferably below 900.
- Preserve BookState as the only canon source. Obsidian, LLM KB, World Studio, Qdrant, and legacy world modules remain projections or compatibility surfaces.
- Do not split tests.

## Countdown Ledger Split

Create `forwin/canon_quality/countdown/` and keep `forwin/canon_quality/countdown_ledger.py` as the public shell.

Proposed modules:

- `parsing.py`: countdown record parsing, ledger row normalization, numeric and status extraction
- `mentions.py`: text mention scanning, context windows, alias matching
- `filters.py`: stale phrase filters, allowed bridge filters, resolution filters
- `retrospective.py`: retrospective and contradiction analysis helpers
- `keys.py`: key resolution, alias-to-key matching, canonical key utilities
- `analysis.py`: analyzer coordination and public result construction
- `__init__.py`: public exports used by the shell

Possible shared helper extraction:

- local text window helpers
- clause-boundary extraction
- duration and reference predicates

Risk controls:

- keep public functions importable from `countdown_ledger.py`
- preserve existing dataclass and result names
- run all countdown ledger tests after each extraction step

## API Schema And Payload Split

### Schemas

Create `forwin/api_schema/` instead of `forwin/api_schemas/` to avoid conflicting with the existing `api_schemas.py` module file. Keep `forwin/api_schemas.py` as a compatibility shell.

Proposed schema modules:

- `common.py`: shared base types and utility validators
- `tasks.py`: task and generation task schemas
- `project.py`: project summary/detail request and response models
- `genesis.py`: Genesis stage and handoff schemas
- `governance.py`: governance and quality schemas
- `observability.py`: runtime, health, and metrics schemas
- `publisher.py`: publisher-related schemas
- `world.py`: world and map-facing schemas
- `llm.py`: model/provider configuration schemas when separable
- `__init__.py`: aggregate public exports

### Project Payloads

Create `forwin/project_payloads/` and keep `forwin/api_project_payloads.py` as a compatibility shell.

Proposed payload modules:

- `common.py`: row conversion, JSON decode helpers, project id normalization
- `generation.py`: generation control and active-task payloads
- `arc_snapshot.py`: arc snapshot and chapter arc response builders
- `runtime_maps.py`: runtime map payload builders
- `genesis.py`: Genesis state and handoff payload builders
- `project_summary.py`: project list and summary payloads
- `project_detail.py`: project detail response construction
- `provisional.py`: provisional project payload construction
- `scenario.py`: scenario payload construction
- `__init__.py`: aggregate public exports

Possible shared helper extraction:

- JSON load/dump normalization
- latest-row grouping
- optional timestamp serialization
- project id normalization

Risk controls:

- shell must re-export all prior names
- response shapes must be snapshot-compatible
- route tests must keep importing through old module paths

## Writer Split

### Prompt Builders

Create `forwin/writer/prompt_core/` and keep `forwin/writer/prompts.py` as the compatibility shell. Avoid a `forwin/writer/prompts/` package because it would conflict with the existing module file.

Proposed modules:

- `sections.py`: reusable section rendering helpers
- `constraints.py`: canon, governance, and quality constraint section builders
- `builders.py`: high-level prompt assembly entry points
- `scene.py`: scene plan and chapter-shape prompt fragments
- `extraction.py`: revision/extraction prompt helpers
- `utils.py`: text trimming, heading, and list formatting helpers
- `__init__.py`: aggregate public exports

Possible shared helper extraction:

- deterministic section joining
- budget-aware section rendering
- bullet/list normalization

### LLM Client

Create `forwin/writer/llm/` and keep `forwin/writer/llm_client.py` as the compatibility shell.

Proposed modules:

- `adapter.py`: `LLMClient` coordinator and public class surface
- `profiles.py`: provider/model profile metadata
- `routing.py`: request routing and provider selection
- `http_transport.py`: HTTP request construction, retries, streaming-compatible request helpers
- `errors.py`: provider error normalization
- `telemetry.py`: usage, timing, and retry diagnostics
- `embeddings.py`: embedding client behavior
- `__init__.py`: aggregate public exports

Risk controls:

- `from forwin.writer.llm_client import LLMClient` must keep working
- tests that monkeypatch old module internals may need shell-level proxies instead of bare aliases
- retry and error semantics must stay covered before cleanup commits

## Context And Retrieval Split

### Context Assembler

Create `forwin/context/assembler_core/` and keep `forwin/context/assembler.py` as the compatibility shell.

Proposed modules:

- `map_context.py`: map and world context collection
- `book_state_overlay.py`: BookState overlay and projection helpers
- `canon_quality_context.py`: canon-quality context assembly
- `personality_integrity.py`: personality and continuity checks
- `assembler.py`: public assembler coordinator
- `helpers.py`: local formatting and truncation helpers
- `__init__.py`: aggregate public exports

### Retrieval Broker

Create `forwin/retrieval/broker_core/` and keep `forwin/retrieval/broker.py` as the compatibility shell.

Proposed modules:

- `broker.py`: public broker coordinator
- `world_pack.py`: world/context pack construction
- `visibility.py`: frontmatter and hidden-content filtering
- `budgeting.py`: selection and token/character budget helpers
- `memory.py`: memory payload helpers
- `helpers.py`: local normalization helpers
- `__init__.py`: aggregate public exports

Possible shared helper extraction:

- hidden/frontmatter visibility predicates
- context truncation and budget estimation
- component character estimation

Risk controls:

- old broker and assembler imports stay valid
- context payload keys and ordering stay stable unless tests prove ordering is irrelevant
- world-v4 and map integration tests remain the regression gate

## Guardrail Tests

Update the existing large-module guardrails rather than splitting test files.

Expected test coverage additions:

- compatibility imports for each old shell
- line-count checks for new production modules
- allowlist entries for intentionally retained shells
- public export checks for schemas, payload builders, prompt builders, LLM client, assembler, broker, and countdown ledger

The guardrail should measure production Python and ignore `tests/`.

## Targeted Verification Matrix

Countdown:

```bash
python3 -m pytest tests/test_countdown_ledger.py -q
```

API schemas and payloads:

```bash
python3 -m pytest tests/test_api_split_modules.py tests/test_project_operation_guards.py tests/test_generation_control_payload.py tests/test_book_genesis_flow.py tests/test_mcp_server.py -q
```

Writer prompts and LLM client:

```bash
python3 -m pytest tests/test_writer_prompt_contract.py tests/test_prompt_revision.py tests/test_llm_client_retry.py tests/test_phase05_regressions.py -q
```

Context and retrieval:

```bash
python3 -m pytest tests/test_context_provider_chain.py tests/test_world_model.py tests/test_world_v4_context_pack.py tests/test_world_v4_retrieval_packs.py tests/test_map_world_integration.py -q
```

Final broad gate:

```bash
python3 -m compileall -q forwin
python3 -m pytest tests/test_large_module_boundaries.py -q
python3 -m pytest -q
git diff --check
```

If the full repo test run is blocked by environment-only dependencies, record the exact blocker and keep the focused matrix as the minimum acceptance gate.

## Implementation Sequencing

1. Strengthen line-count and compatibility import guardrails.
2. Split countdown ledger.
3. Split API schemas and project payloads.
4. Split writer prompt builders.
5. Split LLM client.
6. Split context assembler and retrieval broker.
7. Re-run the full verification matrix.

Each numbered group should be a separate commit after its focused tests pass. Cleanup commits are allowed after mechanical moves, but behavior changes are out of scope unless required to preserve existing behavior.

## Done Criteria

- No selected production module remains over 1024 lines except thin compatibility shells.
- No test file is split as part of this work.
- Old import paths continue to work.
- New packages have explicit public exports.
- Targeted tests pass for every touched surface.
- `python3 -m compileall -q forwin` passes.
- `git diff --check` passes.
- Any remaining production Python module over 1024 lines is either deferred in this spec or newly justified in the implementation notes.
