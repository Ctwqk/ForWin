Audited read-only at `80af1c83323b90f8d69321c0cabe4c98f9185f48`. I did not run pytest, start services, or mutate anything.

**V1 Matrix**

| Requirement | Existing pytest evidence | Production boundary | Release-gate status |
|---|---|---|---|
| Fresh PG upgrade/check/downgrade/upgrade | `tests/test_v5_live_migration.py::test_fresh_postgres_upgrade_downgrade_upgrade_cycle` | [0001_v5_baseline.py](../../forwin/migrations/versions/0001_v5_baseline.py#L22), [base.py](../../forwin/models/base.py#L77) | Strong disposable-Postgres test; still needs recorded release-environment run. |
| API, generation, MCP, publisher worker/browser all boot and healthy | `test_runtime_container_roles.py::test_runtime_roles_are_exactly_the_four_owned_processes`; `test_runtime_worker_roles.py::test_worker_bootstraps_resolve_only_their_owned_runtime`; `test_api_system_routes.py::test_health_reports_current_embedding_backend_status`; `test_mcp_server.py::ForWinMCPIntegrationTests::test_health_endpoint_reports_upstream_ok` | [container.py](../../forwin/runtime/container.py#L49), [workers.py](../../forwin/runtime/workers.py#L41), [API health](../../forwin/http/adapters/api_system_routes.py#L32), [MCP health](../../forwin/mcp/http.py#L430) | Gap: all are in-process/mocked boundaries. Requires deployed multi-process API/worker/MCP/publisher-browser health evidence. |
| RuntimePolicy, Genesis, task, candidate, Canon, BookState, outbox CRUD | `test_project_policy_api.py::test_project_create_initializes_standard_runtime_policy`; `test_mcp_server.py::ForWinMCPIntegrationTests::test_project_create_and_genesis_get_via_mcp`; `...::test_start_writing_continue_conflict_and_pause_via_mcp`; `test_candidate_draft_records.py::test_candidate_draft_record_tracks_review_and_canon_lifecycle`; `test_book_state_final.py::test_compiler_persists_delta_snapshots_and_review_gate`; `test_canon_atomic_transaction.py::test_atomic_commit_writes_all_authoritative_state_once` | [project lifecycle](../../forwin/application/projects/lifecycle.py#L67), [handoff](../../forwin/genesis/handoff/service.py#L30), [Canon admission](../../forwin/canon/admission.py#L59), [outbox store](../../forwin/outbox/store.py#L27) | Partial: components are covered, but no one API/MCP end-to-end CRUD smoke proves the entire stated set. Live smoke remains required. |
| Non-v5 schema fail-fast; no old tables/compat imports | `test_v5_recovery_schema.py::test_require_v5_schema_rejects_old_baseline_stamp`; `...::test_baseline_revision_is_rotated_and_is_the_only_revision` | [require_v5_schema](../../forwin/models/base.py#L82), baseline migration | Partial/gap: stale revision rejection is unit-tested. No test proves actual role startup rejects it, nor scans/proves absence of every compatibility import or old table. |
| Fresh project completes Genesis handoff | `test_genesis_handoff_service.py::GenesisHandoffServiceTests::test_start_writing_handoff_requires_manual_ui_and_genesis_ready`; `...::test_start_writing_materializes_current_arc_without_creating_generation_task`; `test_mcp_server.py::ForWinMCPIntegrationTests::test_start_writing_continue_conflict_and_pause_via_mcp` | [Genesis handoff](../../forwin/genesis/handoff/service.py#L30), [project Genesis action](../../forwin/application/projects/genesis.py#L425) | Strong test coverage; still requires fresh deployed-project evidence for V1. |

**V2 Matrix**

| Requirement | Exact pytest node IDs | Production boundary | Status |
|---|---|---|---|
| Outbox construction/serialization/flush exception fully rolls back Canon | `tests/test_canon_atomic_transaction.py::test_outbox_internal_failure_rolls_back_every_authoritative_write[row]`; `[serialization]`; `[flush]` | [CanonAdmissionService.commit_plan](../../forwin/canon/admission.py#L59) -> [enqueue_outbox_event](../../forwin/outbox/store.py#L27) | Strong PostgreSQL fault injection. It proves rollback of authoritative rows; deployed evidence is still needed for actual handler/process failure, which is outside this write-path bullet. |
| Commit-before crash, lease expiry/reclaim, exactly one accepted chapter/entity/delta/outbox | `tests/test_generation_worker_canon_recovery.py::test_reclaim_after_precommit_crash_commits_candidate_once`; `...::test_reclaim_after_canon_commit_replays_once_then_finishes_task`; `...::test_stale_worker_epoch_cannot_enter_canon_transaction` | [worker](../../forwin/generation/worker.py#L41), [lease claim](../../forwin/generation/task_lease.py#L27), [lease guard](../../forwin/application/generation.py#L528), Canon admission | Partial: `_assert_single_committed_state` asserts exactly one commit, GraphDelta, Entity, alias, obligation, and deterministic outbox IDs. But both “crashes” are preconstructed database states; neither kills a worker at the actual pre-commit/post-commit instruction boundary. A real process-crash/restart fault injection remains a release gap. |
| `EntityAdmissionPlan` stale revalidation | `tests/test_canon_atomic_transaction.py::test_entity_admission_fingerprint_change_after_preparation_is_stale`; `...::test_alias_conflict_created_after_entity_plan_preparation_rolls_back`; plus `...::test_stale_plan_rolls_back_and_returns_candidate_to_ready` | [locked-plan revalidation](../../forwin/canon/admission.py#L308), [entity admission](../../forwin/canon/entity_admission.py#L18) | Strong for fingerprint and post-prepare alias conflict; the normal stale-plan test covers adjacent revision drift. No live-only requirement beyond the worker crash gate above. |

**V3 Matrix**

| Requirement | Exact pytest node IDs | Production boundary | Status |
|---|---|---|---|
| Fail/error/ineligible candidate never calls Spark | `tests/test_gate_delegation_chapter.py::test_fail_verdict_never_reaches_gate_delegation`; `...::test_canon_ineligible_pass_or_warn_never_reaches_gate_delegation[verdict0]` through `[verdict3]`; `tests/test_gate_delegation.py::test_noneligible_checkpoint_never_reaches_spark[pending]`, `[fail]`, `[error]`, `[overridden]` | [candidate gate](../../forwin/generation/pipeline_core/chapter_review_gate.py#L20), [checkpoint gate](../../forwin/generation/pipeline_core/gate_delegation.py#L74) | Strong unit boundary. Real deployment should additionally verify policy routing does not enable a bypass. |
| Spark only substitutes for an otherwise manual pass/warn opportunity | `tests/test_gate_delegation_chapter.py::test_spark_only_approves_canon_eligible_review_opportunities[pass]`; `[warn]`; `tests/test_gate_delegation.py::test_human_gate_policy_never_calls_spark`; `...::test_spark_gate_policy_calls_exact_delegate` | Same candidate/checkpoint gates and [GateDelegationService](../../forwin/generation/gate_delegation.py#L507) | Strong policy/path coverage. |
| Model mismatch, schema/JSON error, timeout reject fail-closed | `tests/test_gate_delegation.py::test_unproven_actual_model_fails_closed`; parameterized selector `...::test_spark_failure_modes_reject_with_trace_and_failure_event` (five generated leaves: invalid JSON, incomplete schema, invalid schema, mismatch, timeout); `...::test_failed_route_persists_complete_bridge_trace` | [SparkGateDelegate.resolve](../../forwin/generation/gate_delegation.py#L188) | Strong fake-client coverage. Gap for real Spark routing/model attestation and configured network timeout behavior; those require deployed evidence. |
| Transaction error rejects; every attempt has PromptTrace + DecisionEvent; approval still follows preparation/admission | `tests/test_gate_delegation.py::test_delegation_audit_failure_rolls_back_dedicated_transaction`; `...::test_successful_spark_audit_survives_outer_transaction_rollback`; `...::test_spark_delegate_proves_model_and_persists_complete_sanitized_trace`; `tests/test_gate_delegation_chapter.py::test_spark_approval_runs_real_chapter_pipeline_through_canon` | [delegation transaction](../../forwin/generation/pipeline_core/gate_delegation.py#L22), Spark delegate, [Canon preparation](../../forwin/canon/preparation.py#L316), Canon admission | Partial: durable success trace/events and rollback are DB-backed, but transaction failure injects a stand-in delegate rather than `SparkGateDelegate`; it does not prove the real delegate’s failure path emits the required explicit result. |
| Spark cannot directly write candidate/Canon/accepted chapter/Entity/GraphDelta/CanonCommitRecord | `tests/test_gate_delegation.py::test_spark_delegate_module_has_no_authoritative_canon_write_surface`; `tests/test_gate_delegation_chapter.py::test_spark_approval_runs_real_chapter_pipeline_through_canon` | [Spark module](../../forwin/generation/gate_delegation.py#L178), Canon admission | Gap/weak: the first test is a string scan of one module; the second proves the intended path, not a capability boundary. It does not prevent indirect writes through a future collaborator. |

Focused suite proposal:

```bash
python -m pytest -q \
  tests/test_v5_live_migration.py \
  tests/test_v5_recovery_schema.py::test_require_v5_schema_rejects_old_baseline_stamp \
  tests/test_runtime_container_roles.py \
  tests/test_runtime_worker_roles.py \
  tests/test_api_system_routes.py::test_health_reports_current_embedding_backend_status \
  tests/test_mcp_server.py::ForWinMCPIntegrationTests::test_health_endpoint_reports_upstream_ok \
  tests/test_mcp_server.py::ForWinMCPIntegrationTests::test_project_create_and_genesis_get_via_mcp \
  tests/test_mcp_server.py::ForWinMCPIntegrationTests::test_start_writing_continue_conflict_and_pause_via_mcp \
  tests/test_genesis_handoff_service.py \
  tests/test_project_policy_api.py \
  tests/test_candidate_draft_records.py \
  tests/test_book_state_final.py \
  tests/test_projection_outbox.py \
  tests/test_canon_atomic_transaction.py \
  tests/test_generation_worker_canon_recovery.py \
  tests/test_gate_delegation.py \
  tests/test_gate_delegation_chapter.py
```

The release blockers are V1’s genuine deployed role smoke, V2’s actual kill/restart timing injection, V3’s real-provider timeout/model evidence, and a stronger enforceable no-direct-Canon boundary.
