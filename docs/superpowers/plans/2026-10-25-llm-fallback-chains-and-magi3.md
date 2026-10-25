# LLM Fallback Chains, Codex Removal and the magi3 Move: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every ForWin LLM call goes through a per-route fallback chain of ordinary API profiles, DeepSeek first where Codex used to sit. Codex is removed from ForWin entirely, and the whole stack, data stores included, moves from infra-150 and the Swarm to magi3.

**Architecture:** `OpenAICompatibleAdapter` already walks a profile list. That list stops being "primary plus a priority table" and becomes an explicit chain per `_llm_task_route` name, taken from `RuntimePolicy.models.chains`. The Codex router, client, bridge, model registry and Codex-only delegate paths are deleted. The pause gate and the world-conflict reviewer become ordinary structured calls on the same chain. magi3 runs one docker compose stack under colima. It is deployed from magi1 by a repo script and reached from magi1 through an SSH tunnel.

**Tech Stack:** Python 3.13, SQLAlchemy and Alembic (new migration `0007_drop_codex`), pytest, docker compose on colima (macOS arm64), PostgreSQL 16, Qdrant v1.17.1, MinIO.

**Spec:** the user's requirements in this conversation (2026-10-25), recorded verbatim in "Decisions" below. The inventory behind every file:line reference is the LLM call-site inventory made at HEAD cbc88a7.

## Decisions

The user's words, then the rulings that implement them.

- "不再使用codex，原来codex的位置用deepseek替代" ("stop using Codex; DeepSeek takes Codex's places"). Every route where the router preferred Codex (review, planning, Genesis, extraction, entity admission, feedback, scene breakdown, the pause gate, the world-conflict reviewer) starts with DeepSeek. Routes where Codex was only the last resort, the prose routes, keep today's effective order.
- "都写成fallback-chain模式吧" ("make it all fallback chains"). No call path names a provider or model in code. Chains are data in `RuntimePolicy.models.chains`, and per-profile quirks stay in `profile_options.py`.
- "150上的codex-bridge帮我archive掉吧" ("archive the codex-bridge on 150"). Done on 2026-10-25: infra-150:/home/taiwei/archive/forwin-codex-bridge-20261025/. The bridge code leaves the repo in Task 2. A future chatgpt-bridge would be one more OpenAI-compatible profile in a chain, with no special path.
- "数据库也全部迁移上去" ("move all the databases too"). The Postgres, Qdrant and MinIO data and the `/app/data` volume all move to magi3. magi3 is reached through a Japan VPS relay; nothing is published on the VPS.
- Out of scope: Codex as the operator agent (`plugins/forwin-operator/.codex-plugin`, `.agents/skills/forwin-operator`, `docs/codex-forwin-mcp.md`, `scripts/check_codex_operator_ready.py`). Those drive ForWin through MCP and are not an LLM backend.

## Global Constraints

- Default chains, as profile ids, keyed by route name. Every route is listed; any other route uses `default`:

  | Route | Chain |
  |---|---|
  | `review_json`, `feedback_analysis`, `planning_json_low_risk`, `planning_prose` | `env-deepseek`, `env-kimi`, `env-minimax` |
  | `canon_extraction`, `planning_json_general`, `general`, `default` | `env-deepseek`, `env-kimi` |
  | `prose_generation`, `repair_generation` | `env-kimi`, `env-deepseek` |
  | `writer_preview` | `env-kimi`, `env-deepseek`, `env-minimax` |

  MiniMax stays off the routes where today's `_profile_suitable_for_route` rejects it. The world tick (`planning_json_general`) moves from Kimi-first to DeepSeek-first.
- Profiles: `env-deepseek`, `env-kimi` and `env-minimax` all come from env (`DEEPSEEK_*`, `KIMI_*`/`MOONSHOT_*`, `MINIMAX_*`). A chain entry whose profile has no API key is skipped and recorded with reason `profile_unconfigured`.
- `RuntimePolicy.model_profile_id`, when non-empty and configured, is pinned first in every chain. Empty means no pin; there is no `env-minimax` default primary any more.
- Fallback to the next profile happens on: the existing transient set (timeouts, network errors, HTTP 408/409/425/429/5xx/529); HTTP 401, 402, 403 and 404; and an empty completion (missing or blank `choices[0].message.content`). It does not happen on any other 4xx or on a parse failure; callers keep their own business retries.
- Policy literals: `GateDelegate = Literal["human", "llm"]` (default `"human"`), `WorldConflictDelegate = Literal["llm", "human"]` (default `"llm"`), `ConflictDecider = Literal["auto", "llm", "human"]`. Stage key `spark_pause_gate` becomes `pause_gate`.
- No `FORWIN_CODEX_*` setting, `codex_bridge` backend value or Codex model name remains under `forwin/`.
- On magi3 every published port binds `127.0.0.1`. Access from magi1 is `ssh -L 28899:127.0.0.1:8899 -L 28896:127.0.0.1:8896 magi3`.
- Secrets (API keys, the DB password, the publisher session secret) are never echoed. Copy them host to host through pipes into mode-600 files.
- Commits: `git -c user.name=CASPER -c user.email=magi1@CASPERs-Mac-mini.local commit`, with messages ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Address machines by name (magi1, magi3, infra-150).

## Review Focus

1. DeepSeek's balance runs out mid-chapter (HTTP 402). The call moves to Kimi and the run continues; the attempt events show `auth_or_balance` on the DeepSeek attempt.
2. A chain names a profile whose key is not set on this host. The profile is skipped and recorded, with no crash. If no profile on a route is configured, the error names the route: `no configured LLM profile for route review_json`.
3. LLM availability checks (`_llm_available`, Genesis' scaffold fallback, the Genesis 400) answer "available" when any chain profile is configured, not only a primary. Today they read the primary key only.
4. The cover generator keeps using `MINIMAX_API_KEY`/`MINIMAX_BASE_URL` when a project pins DeepSeek. Today it takes the primary profile's key.
5. A database still holding `"codex"`/`"spark"` values in `runtime_policy_json` or `world_plan_conflicts` loads cleanly after `0007_drop_codex`. The migration rewrites them; there is no runtime shim.

---

### Task 1: Chain-based routing in the adapter

**Files:**
- Modify: `forwin/config.py` (lines 144-179 and 491-509: `env-minimax` joins the profile registry; `resolve_model_profile("")` returns no pin), `forwin/api_schema/policy.py:46-73` (`default_model_profile_id=""`)
- Modify: `forwin/runtime/policy.py` (`ModelsPolicy.chains` replaces `role_preferences`; delete `ReviewPolicy.repair_models`, which nothing reads)
- Modify: `forwin/writer/llm/routing.py` (delete the `_profile_route_priority` table, the kind drop rules at 123-126, `_profile_suitable_for_route`, and the `spark` kind and `codex_models` threading), `forwin/writer/llm/adapter.py`, `forwin/writer/llm/errors.py`, `forwin/writer/llm/telemetry.py`
- Modify: `forwin/runtime/container.py:403, 472-488, 639-661, 810-813`, `forwin/genesis/llm.py:316-320`, `forwin/application/projects/genesis.py:433`
- Test: `tests/test_llm_chain_routing.py` (new). Update `tests/test_llm_client_retry.py`, `tests/test_runtime_container.py`, `tests/test_profile_max_tokens.py` and `tests/test_runtime_policy.py`.

**Interfaces:**
- Produces:
  - `forwin.runtime.policy.DEFAULT_LLM_CHAINS: dict[str, tuple[str, ...]]`, the table in Global Constraints.
  - `ModelsPolicy.chains: dict[str, tuple[str, ...]] = DEFAULT_LLM_CHAINS`.
  - `InfrastructureConfig.llm_profiles: dict[str, ModelProfileConfig]`, insertion-ordered and holding every env profile that has a key.
  - `LLMClient(profiles: Mapping[str, ModelProfileConfig], chains: Mapping[str, tuple[str, ...]], pinned_profile_id: str = "", *, timeout..., retry...)`, which replaces the primary-plus-`fallback_profiles` constructor.
  - `LLMClient.chat_with_meta(...) -> LLMChatResult(content: str, profile_id: str, model: str)`. It takes the same kwargs as `chat`, and `chat` returns `chat_with_meta(...).content`.
  - `forwin.runtime.container.llm_configured(infrastructure, policy) -> bool`, true when any profile in any of the policy's chains is configured.
- Consumes: the route names from `_llm_task_route` (routing.py:233-289), unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/test_llm_chain_routing.py`, using a fake transport that returns scripted HTTP responses per base URL:
  - `test_review_route_tries_deepseek_then_kimi_then_minimax`: DeepSeek → 503, Kimi → 200 "ok". Expect content "ok", attempt events `[env-deepseek, env-kimi]` and `meta.profile_id == "env-kimi"`.
  - `test_prose_route_starts_with_kimi_and_never_uses_minimax`: the candidate chain for `prose_generation` is `["env-kimi", "env-deepseek"]`.
  - `test_world_tick_route_is_deepseek_first`: `task_family="world_engine"`, `stage_key="world.adjudicate"` → the first attempt is `env-deepseek`.
  - `test_http_402_falls_back_with_auth_or_balance_reason`: DeepSeek → 402. The next profile answers, and the DeepSeek attempt has `error_category == "auth_or_balance"`.
  - `test_http_400_does_not_fall_back`: DeepSeek → 400 raises; Kimi is never called.
  - `test_empty_completion_falls_back`: DeepSeek → 200 with `content: ""`; Kimi answers.
  - `test_unconfigured_profile_is_skipped_and_recorded`: with no `KIMI_API_KEY`, `skipped_profiles` contains `{"profile_id": "env-kimi", "reason": "profile_unconfigured"}`.
  - `test_no_configured_profile_names_the_route`: with no keys set, the error message contains `no configured LLM profile for route review_json`.
  - `test_pinned_profile_goes_first_on_every_route`: `pinned_profile_id="env-minimax"` puts it first on `prose_generation` too, de-duplicated.
  - `test_llm_configured_true_with_only_deepseek_key`
  - `test_cover_client_uses_minimax_env_not_pinned_profile`
  - `test_no_caller_pins_a_provider_or_model` (user requirement: every chat call has a chain, none is fixed to a model): scan `forwin/**/*.py` for `preferred_model=`, `preferred_provider_kind=` and a literal `model=` argument passed to a chat call outside `forwin/config.py`/`forwin/writer/llm/`; expect none.
  - `test_every_chat_call_goes_through_llm_client_chain`: every `.chat(`/`.chat_with_meta(` call site under `forwin/` reaches `OpenAICompatibleAdapter` (no direct `httpx.post` to `/chat/completions` outside `forwin/writer/llm/transport.py`/`adapter.py`).
  - `test_embed_does_not_depend_on_a_pinned_profile`: `embed()` works with `pinned_profile_id=""` using the retrieval `embedding_*` config.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_llm_chain_routing.py -q -p no:cacheprovider`. Expected: FAIL.
- [ ] **Step 3: Implement.** `_request_profiles` becomes `_chain_profiles(route) -> (list[profile], skipped)`, which builds `[pinned] + chains.get(route, chains["default"])`, de-duplicates, and skips unconfigured profiles. `_route_profiles_with_metadata` keeps its telemetry (`candidate_chain`, `skipped_profiles`); set `route_policy_version` to `"v5.0-policy-chains"` in one shared constant, replacing the 5 copies. Add 401/402/403/404 and the empty completion to `_is_fallback_retryable`, with categories `auth_or_balance`, `model_not_found` and `empty_response`. Drop the `preferred_provider_kind`/`preferred_model` kwargs from every caller and from the adapter. The container builds one client per policy from `infrastructure.llm_profiles` and `policy.models.chains`.
- [ ] **Step 4: Run** the new file plus the updated ones. Expected: PASS.
- [ ] **Step 5: Commit** `feat(llm): route every call through a per-route fallback chain`.

### Task 2: Delete the Codex backend

**Files:**
- Delete: `forwin/llm/router.py`, `forwin/llm/codex_client.py`, `forwin/llm/codex_models.py`, `forwin/llm/factory.py`, `forwin/codex_bridge/`, the `forwin-codex-bridge` console script (`pyproject.toml:36`), `tests/test_llm_router.py`, `tests/test_codex_bridge.py`, `tests/test_codex_model_registry.py`
- Modify:
  - `forwin/config.py`: drop every `codex_*` field, `DEFAULT_CODEX_*` and `FORWIN_CODEX_*`.
  - `forwin/runtime/container.py:14, 657, 661`.
  - `forwin/http/adapters/api_system_routes.py:80-153` and `forwin/http/routes.py:298-301`: delete `GET /api/settings/codex/health`.
  - `forwin/api_schema/tasks.py:13-20` and `forwin/api_schema/__init__.py`: delete `CodexBridgeStatusResponse`.
  - `ui_assets/home/body.html:21,71-77` and `ui_assets/home/app_library.js:64-96`: delete the Codex badge and panel.
  - `scripts/supervise_forwin_interventions.py:603-615`.
  - `forwin/llm_eval/profiles.py` and `forwin/llm_eval/runner.py`: delete the Codex CLI adapter and the `codex-spark|spark|codex` aliases. Also `forwin/cli.py:328,341`.
  - `forwin/observability/cost_ledger.py`: drop `codex_usage_attempts`; also `forwin/mcp/models.py:112`.
  - `forwin/writer/llm/telemetry.py:47-49`: drop the `codex_bridge` `usage_source`.
  - `.env.example:129-141` and `.circleci/config.yml:37`.
  - `docs/codex-pro-runtime.md`: delete it; `README.md` and `Design-docs/DESIGN_STATUS.md` say Codex was removed 2026-10-25.
- Test: `tests/test_architecture_boundaries.py` gains `test_no_codex_llm_backend_remains`. Update the remaining tests in the inventory's section 5 that reference deleted names.

**Interfaces:**
- Consumes: Task 1's `LLMClient`, which no longer takes `codex_models`.
- Produces: nothing new. Callers use `LLMClient` directly.

- [ ] **Step 1: Write the failing test** `test_no_codex_llm_backend_remains`. It scans `forwin/**/*.py`, `ui_assets/**` and `pyproject.toml` and asserts no match for `codex_bridge|CodexBridge|codex_models|FORWIN_CODEX|codex_usage_attempts|gpt-5\.3-codex-spark|gpt-6-luna`. The scan skips nothing under `forwin/`; the in-world `secrets_codex` does not match the pattern.
- [ ] **Step 2: Run it.** Expected: FAIL, listing the files.
- [ ] **Step 3: Delete and edit** the files above until the scan is clean. (Tasks 3 and 4 own the delegate files and their literals; this task leaves those files' delegate logic alone and only removes imports of deleted modules.)
- [ ] **Step 4: Run** the boundary test and `.venv/bin/python -m pytest -q -p no:cacheprovider -x tests/test_runtime_container.py tests/test_api_system_routes.py tests/test_llm_eval_runner.py tests/test_llm_eval_core.py tests/test_cost_ledger.py tests/test_supervise_forwin_interventions.py`. Expected: PASS. (The boundary test may stay red until Task 3 removes the delegate literals. If so, mark it `xfail(strict=True)` with reason "delegates move in Task 3" and let Task 3 remove the marker.)
- [ ] **Step 5: Commit** `refactor(llm): delete the Codex router, client and bridge`.

### Task 3: The pause gate and the world-conflict reviewer on the chain

**Files:**
- Create: `forwin/llm/structured_call.py` (moved from `generation/gate_delegation.py:180-330`)
- Modify:
  - `forwin/generation/gate_delegation.py`: `SparkGateDelegate` becomes `LLMGateDelegate`; delete `SPARK_GATE_MODEL`.
  - `forwin/generation/pipeline_core/gate_delegation.py:69` (`delegate="llm"`).
  - `forwin/audit/gate_outcome.py:35`.
  - `forwin/world_engine/conflict_delegate.py`: delete `WORLD_CONFLICT_DEFAULT_MODEL` and the `role_models` use.
  - `forwin/world_engine/conflict_routing.py:26, 62-78`, `forwin/world_engine/conflict_types.py:21`, `forwin/world_engine/conflict_resolution.py:73`.
  - `forwin/world_engine/chapter_conflicts.py:253-363` (`_codex_decided` becomes `_llm_decided`, `_codex_rewrites` becomes `_llm_rewrites`, `decided_by="llm"`), `forwin/world_engine/tick_conflicts.py:191-208`, `forwin/generation/pipeline_core/chapter_conflicts.py`.
  - `forwin/runtime/policy.py:8-9, 33, 119, 284-285`, `forwin/api_schema/policy.py:29-30`.
  - `forwin/mcp/http.py:260-292`, `forwin/mcp/client.py:352-460, 803-804`, `forwin/mcp/models.py:286-287`.
  - `ui_assets/home/app_task_control.js:611-648` (pills read "LLM").
  - `forwin/observability/cost_ledger.py:1151-1167` (role `delegate`).
  - `forwin/runtime/container.py:443-448`, `forwin/maintenance/post_canon.py:1038-1039`.
- Test: `tests/test_gate_delegation.py`, `tests/test_world_conflict_delegate.py`, `tests/test_world_conflict_routing.py`, `tests/test_chapter_extraction_conflicts.py`, `tests/test_gate_delegation_chapter.py`, `tests/test_mcp_server.py`, `tests/test_project_policy_api.py`, `tests/test_cost_ledger.py`

**Interfaces:**
- Consumes: `LLMClient.chat_with_meta` (Task 1).
- Produces:
  - `run_structured_llm_call(*, llm_client, messages, output_schema, stage_key, task_family="review", trace_scope, label, recorder, ...) -> StructuredCallOutcome`, where `StructuredCallOutcome` has `ok: bool`, `payload: dict | None`, `failure_reason: Literal["llm_call_failed", "parse_or_schema"] | None`, `profile_id: str` and `model: str`. There is no `requested_model`, and `model_mismatch` is gone.
  - `LLMGateDelegate`.
  - The decider value `"llm"`.

- [ ] **Step 1: Write the failing tests:**
  - `test_gate_resolves_on_whichever_chain_profile_answers`: DeepSeek → 503, Kimi → valid JSON approve. The resolution is approved, and the decision event payload has `profile_id == "env-kimi"` and `model == "kimi-k2.5"`.
  - `test_gate_parse_failure_pauses_for_human`
  - `test_moderate_conflict_routes_to_llm_when_policy_delegates`: `route_conflict(...) == "llm"`.
  - `test_world_conflict_delegate_records_llm_decider_and_served_model`
  - `test_world_conflict_amend_plan_still_escalates_to_human`
  - `test_policy_rejects_legacy_codex_and_spark_literals`: `RuntimePolicy.model_validate({... "world": {"conflict_delegate": "codex"}})` raises `ValidationError`.
  - `test_mcp_set_world_conflict_delegate_accepts_llm_and_human_only`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Keep the trace and decision-event writing of the old `run_codex_structured_call`, and take `profile_id`/`model` from `chat_with_meta`. Pause-gate stage key: `pause_gate`; trace template id: `pause_gate`. Remove Task 2's `xfail` marker if one was added.
- [ ] **Step 4: Run** the listed test files and `tests/test_architecture_boundaries.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(world_engine): decide gates and world conflicts on the LLM chain`.

### Task 4: Migration `0007_drop_codex`

**Files:**
- Create: `forwin/migrations/versions/0007_drop_codex.py` (beside `0006_planning_v6.py`)
- Modify: `forwin/models/genesis.py:55-58` (drop `codex_job_id` and `permission_profile`); also `forwin/observability/llm_trace.py:544,639`, `forwin/observability/sinks.py:204`, `forwin/state/updater.py:260-282`, `forwin/api_schema/genesis.py:41-44`, `forwin/http/adapters/api_observability_routes.py:58-61`, `forwin/application/read_models/genesis.py:177-180` and `forwin/genesis/workspace/service.py:358,479`
- Test: `tests/test_migration_0007_drop_codex.py`

**Interfaces:**
- Consumes: the literals from Task 3.
- Produces: schema head `0007_drop_codex`.

- [ ] **Step 1: Write the failing tests**, on the PostgreSQL test DB at 127.0.0.1:55432, following `tests/test_planning_v6_schema.py`:
  - `test_0007_rewrites_policy_literals`: a project row at 0006 whose `runtime_policy_json` has `pause.gate_delegate="spark"`, `world.conflict_delegate="codex"`, `models.role_preferences` and `review.repair_models`. After upgrade, the values are `"llm"` and `"llm"`, both keys are gone, and `RuntimePolicy.model_validate_json` loads it.
  - `test_0007_rewrites_conflict_deciders`: `required_decider="codex"` becomes `"llm"`, and `decision_json.decided_by="codex"` becomes `"llm"`.
  - `test_0007_drops_codex_trace_columns`: after upgrade, `prompt_traces` has no `codex_job_id` and no `permission_profile` column.
  - `test_0007_downgrade_restores_columns_and_literals`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the migration. Rewrite the JSON in Python row by row, because `runtime_policy_json` is text. The downgrade re-adds the columns as nullable with server default `''` and maps `"llm"` back to `"codex"`/`"spark"` per field.
- [ ] **Step 4: Run** the migration tests, then the full suite: `.venv/bin/python -m pytest -q -p no:cacheprovider; echo RC=$?`. Expected: PASS, RC=0.
- [ ] **Step 5: Commit** `feat(db): 0007 rewrites Codex policy values and drops Codex trace columns`.

### Task 5: The magi3 compose stack and its scripts

**Files:**
- Create:
  - `deploy/magi3/docker-compose.yml` and `deploy/magi3/env.example` (key names only).
  - `deploy/magi3/README.md` (the operations page: start, stop, logs, deploy, backup, restore, tunnel).
  - `deploy/magi3/com.forwin.magi3-tunnel.plist` and `deploy/magi3/com.forwin.magi3-backup.plist` (launchd agents for magi1).
  - `scripts/deploy_magi3.sh` and `scripts/backup_magi3.sh`.
- Modify: `Dockerfile` (an optional `ARG PIP_INDEX_URL`, passed to `pip install` only when non-empty)
- Test: `tests/test_magi3_deploy_files.py`

**Interfaces:**
- Consumes: the image targets `forwin-runtime` and `publisher-browser-runtime` (Dockerfile:32, 44), and the worker commands from the current Swarm specs:
  - `python -m forwin.cli -v generation-worker --worker-id forwin-generation-worker --lease-seconds 300 --poll-interval 2`
  - `python -m forwin.cli -v outbox-worker --worker-id forwin-outbox-worker --poll-interval 2`
  - `python -m forwin.cli -v publisher-worker --limit 1 --poll-interval 2`
  - MCP: `python -m uvicorn forwin.mcp.http:app --host 0.0.0.0 --port 8896`, with the httpx `/health` healthcheck.
  - Browser: tmpfs `/dev/shm` of 1 GiB, with the Swarm healthcheck.
- Produces: the compose project `forwin`, with services `postgres` (`postgres:16-alpine`), `qdrant` (`qdrant/qdrant:v1.17.1`), `minio` (`minio/minio:RELEASE.2025-09-07T16-13-09Z`), `app`, `mcp`, `generation-worker`, `outbox-worker`, `publisher-worker` and `publisher-browser`, plus `migrate` (profile `tools`, `alembic upgrade head`). The volumes are `forwin-postgres`, `forwin-qdrant`, `forwin-minio` and `forwin-data` (mounted at `/app/data`). In-network URLs: `postgresql+psycopg://forwin:${FORWIN_POSTGRES_PASSWORD}@postgres:5432/forwin`, `http://qdrant:6333` and `minio:9000`.
  - `scripts/deploy_magi3.sh [--ref <sha>]`, run on magi1. It refuses a dirty tree or a ref that is not `origin/master`, and pushes the ref to `magi3:~/forwin/repo.git`. On magi3 it checks the ref out into `~/forwin/src`, then runs `docker compose build`, `docker compose run --rm migrate` and `docker compose up -d --remove-orphans`. It waits up to 180 s for app `/health` and MCP `/health` on magi3's localhost, writes `~/forwin/src/.deploy-commit`, and exits non-zero on any failure.
  - `scripts/backup_magi3.sh`, run on magi1. It writes `pg_dump -Fc` to `~/backups/forwin-magi3/forwin-<UTC ts>.dump`, plus a weekly tar of `forwin-data`, keeps 14 days, and exits non-zero if the dump is under 1 KiB.

- [ ] **Step 1: Write the failing tests** in `tests/test_magi3_deploy_files.py`:
  - `test_every_published_port_binds_localhost`: every `ports` entry in the compose file starts with `"127.0.0.1:"`.
  - `test_compose_has_the_expected_services_and_volumes`
  - `test_compose_sets_no_codex_variables`
  - `test_worker_subcommands_exist_in_cli`: `generation-worker`, `outbox-worker` and `publisher-worker` are registered in `forwin/cli.py`.
  - `test_scripts_parse`: `bash -n` passes for both scripts.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Write the files.** The tunnel agent runs `/usr/bin/ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 28899:127.0.0.1:8899 -L 28896:127.0.0.1:8896 magi3` with `KeepAlive`. The backup agent runs daily at 03:17.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(deploy): single-host compose stack and scripts for magi3`.

### Task 6: Move production to magi3 (operations; the controller runs it with user checkpoints)

Run only after Tasks 1–5 are on `origin/master` and the full suite is green. Steps marked **[confirm]** wait for the user.

- [ ] **Step 1: Prepare magi3.**
  - `brew install colima docker docker-compose docker-buildx`.
  - `colima start --vm-type vz --cpu 8 --memory 12 --disk 100`.
  - Create `~/forwin/repo.git` (bare) and `~/forwin/src`.
  - Build `deploy/magi3/.env` on magi3, mode 600. Copy `DEEPSEEK_API_KEY`, `KIMI_API_KEY`, the `MINIMAX_*` settings, `FORWIN_PUBLISHER_*` and the MinIO keys from the infra-150 service env through a pipe. Generate a new `FORWIN_POSTGRES_PASSWORD`.
  - Check: `docker info` works and `.env` has every key in `env.example`, verified by names only.
- [ ] **Step 2: Build and start the data stores.** Run `scripts/deploy_magi3.sh` with app services held back (`docker compose up -d postgres qdrant minio`). Check: all three are healthy.
- [ ] **Step 3 [confirm]: Cut over.**
  - Scale the six `forwin-*-swarm` services on infra-150 to 0.
  - `pg_dump -Fc` the `forwin-postgres` container on infra-150, relay it through magi1, and `pg_restore --no-owner` it into magi3's `postgres`.
  - Tar the `forwin-swarm-data` volume on `colima-swarmbridged` (magi1) into magi3's `forwin-data`.
  - No MinIO on magi3 (2026-10-25 ruling: MinIO stopped publishing images, and infra-150's bucket held 0 objects); artifacts use the `local` backend on `forwin-data`, so check infra-150's bucket is still empty before cutover.
  - Qdrant collections were deleted by the purge; check none exist on infra-150 before skipping them.
  - Check: row counts of `projects`, `alembic_version` and the publisher account tables match on both sides.
- [ ] **Step 4: Deploy the app on magi3.** Run `scripts/deploy_magi3.sh` to migrate to `0007_drop_codex` and start everything. Checks:
  - `alembic current` = `0007_drop_codex`;
  - app `/health` 200;
  - MCP `tools/list` and `project_list` work;
  - one `general`-route chat through the container's client answers, with `profile_id == "env-deepseek"`.
- [ ] **Step 5: Set up access from magi1.**
  - Load the tunnel launchd agent.
  - Point the Claude Code MCP local override at `http://127.0.0.1:28896/mcp`, and commit `.mcp.json` with that URL.
  - Load the backup agent and run it once. Check: the dump exists and is at least 1 KiB.
- [ ] **Step 6: Update the docs.**
  - `AGENTS.md`: the deploy command becomes `scripts/deploy_magi3.sh`; magi3 hosts ForWin; infra-150 no longer does.
  - `deploy/magi3/README.md` and the memory files.
  - Commit and push.
- [ ] **Step 7 [confirm, one week later]: Retire infra-150's ForWin.**
  - `docker service rm` the six services.
  - Stop `forwin-postgres`, `forwin-qdrant`, `forwin-minio` and their socat proxies, keeping the volumes for 30 days.
  - Remove the `forwin` entry from infra-150's `deploy-github-sync.sh` (back it up beside the script first; it is not in git).
