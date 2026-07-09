# ForWin Reckless Review Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a project-level reckless mode that delegates every human review gate to `gpt-5.3-codex-spark` and preserves a complete, causally linked audit log.

**Architecture:** Extend project governance with a review delegation mode, then route explicit human gates through one `RecklessReviewAgent`. The agent makes a strict structured Spark call, verifies the actual model, saves the full PromptTrace before any approval is applied, and returns an approve/reject outcome to the owning gate.

**Tech Stack:** Python 3.12, Pydantic, SQLAlchemy, FastAPI, existing ForWin routed LLM adapter/Codex bridge, vanilla JavaScript task drawer, pytest/unittest, ForWin MCP.

## Global Constraints

- The only valid delegated model is exactly `gpt-5.3-codex-spark`.
- Default mode is `human`; reckless mode must be explicitly enabled per project or per continue request.
- A response from any fallback model is logged but cannot approve a gate.
- PromptTrace persistence must succeed before an approval can change state.
- Deterministic hard gates and user pause/terminate are never overridden.
- Existing untracked `.forwin-run-logs/` content is preserved.

---

### Task 1: Governance Mode Plumbing

**Files:**
- Modify: `forwin/governance.py`
- Modify: `forwin/config.py`
- Modify: `forwin/api_schema/governance.py`
- Modify: `forwin/api_schema/project.py`
- Modify: `forwin/api_governance_support.py`
- Modify: `forwin/project_ops/generation.py`
- Modify: `forwin/project_ops/reviews.py`
- Modify: `forwin/api_system_routes.py`
- Test: `tests/test_config_defaults.py`
- Test: `tests/test_project_operation_guards.py`
- Test: `tests/test_generation_task_payload.py`

**Interfaces:**
- Produces: `ReviewDelegationMode = Literal["human", "reckless"]`.
- Produces: `ProjectGovernanceSettings.review_delegation_mode`.
- Produces: `Config.review_delegation_mode` for per-task runtime overrides.
- Consumes: existing governance normalization and `copy_config` paths.

- [ ] **Step 1: Write failing governance tests**

```python
def test_reckless_review_mode_defaults_to_human() -> None:
    assert Config().review_delegation_mode == "human"
    assert ProjectGovernanceSettings().review_delegation_mode == "human"

def test_governance_round_trip_accepts_reckless_mode(self) -> None:
    project = self._create_project(project_id="proj-reckless-governance")
    response = api_module.update_project_governance(
        project.id,
        ProjectGovernanceUpdateRequest(
            review_delegation_mode="reckless",
            reason="delegate human gates to Spark",
        ),
    )
    self.assertEqual(response.governance.review_delegation_mode, "reckless")
    self.assertEqual(api_module.get_project(project.id).governance.review_delegation_mode, "reckless")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_config_defaults.py tests/test_project_operation_guards.py -k 'reckless or defaults'`

Expected: FAIL because the mode fields do not exist.

- [ ] **Step 3: Add the typed mode and propagate it**

Add to `forwin/governance.py`:

```python
ReviewDelegationMode = Literal["human", "reckless"]

class ProjectGovernanceSettings(BaseModel):
    default_operation_mode: str = "blackbox"
    review_delegation_mode: ReviewDelegationMode = "human"
```

Normalize unknown values to `human`, add `review_delegation_mode` to both governance request schemas and `governance_request_payload`, and copy the resolved value into every runtime config construction beside `operation_mode`.

Add to `_ConfigFields`:

```python
review_delegation_mode: Literal["human", "reckless"] = "human"
```

- [ ] **Step 4: Verify GREEN and task payload persistence**

Run: `pytest -q tests/test_config_defaults.py tests/test_project_operation_guards.py tests/test_generation_task_payload.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/governance.py forwin/config.py forwin/api_schema/governance.py forwin/api_schema/project.py forwin/api_governance_support.py forwin/project_ops/generation.py forwin/project_ops/reviews.py forwin/api_system_routes.py tests/test_config_defaults.py tests/test_project_operation_guards.py tests/test_generation_task_payload.py
git commit -m "Add reckless review governance mode"
```

---

### Task 2: Strict Spark Reviewer and Complete Trace

**Files:**
- Create: `forwin/reckless_review.py`
- Modify: `forwin/governance.py`
- Modify: `forwin/llm/codex_client.py`
- Modify: `forwin/llm/router.py`
- Test: `tests/test_reckless_review.py`
- Test: `tests/test_llm_router.py`

**Interfaces:**
- Consumes: an object exposing `chat`, `last_call_result`, and `drain_llm_attempt_events`.
- Produces: `RECKLESS_REVIEW_MODEL`, `RecklessReviewRequest`, `RecklessReviewDecision`, `RecklessReviewOutcome`, and `RecklessReviewAgent.review_and_record`.
- Produces DecisionEvent types: `reckless_review_requested`, `reckless_review_decided`, `reckless_review_failed`, `reckless_gate_overridden`.

- [ ] **Step 1: Write failing agent tests**

Cover these exact cases in `tests/test_reckless_review.py`:

```python
def test_spark_approval_persists_full_prompt_response_and_attempts(session) -> None:
    outcome = agent.review_and_record(
        updater=StateUpdater(session),
        request=RecklessReviewRequest(
            project_id=project.id,
            task_id="task-1",
            gate_kind="chapter_review",
            chapter_number=4,
            input_snapshot={"draft": "full draft", "issues": [{"code": "x"}]},
        ),
    )
    assert outcome.approved is True
    trace = session.get(PromptTrace, outcome.trace_id)
    assert "full draft" in trace.input_snapshot_json
    assert "approve" in trace.attempts_json
    assert trace.backend in {"ordinary", "codex_bridge"}

def test_non_spark_success_cannot_approve(session) -> None:
    outcome = fallback_agent.review_and_record(
        updater=StateUpdater(session),
        request=RecklessReviewRequest(
            project_id=project.id,
            task_id="task-1",
            gate_kind="chapter_review",
            chapter_number=4,
            input_snapshot={"draft": "full draft"},
        ),
    )
    assert outcome.approved is False
    assert outcome.failure_reason == "model_mismatch"

def test_invalid_json_and_call_failure_are_logged_and_rejected(session) -> None:
    request = RecklessReviewRequest(
        project_id=project.id,
        task_id="task-1",
        gate_kind="chapter_review",
        chapter_number=4,
        input_snapshot={"draft": "full draft"},
    )
    assert invalid_agent.review_and_record(
        updater=StateUpdater(session), request=request
    ).approved is False
    assert failing_agent.review_and_record(
        updater=StateUpdater(session), request=request
    ).approved is False
```

Also assert that sensitive keys such as `api_key`, `authorization`, `cookie`, `token`, and `password` are removed while full story prompt/output text remains.

- [ ] **Step 2: Run agent tests and verify RED**

Run: `pytest -q tests/test_reckless_review.py`

Expected: import failure because `forwin.reckless_review` does not exist.

- [ ] **Step 3: Implement strict structured review**

Use these stable models:

```python
RECKLESS_REVIEW_MODEL = "gpt-5.3-codex-spark"

class RecklessReviewDecision(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1)
    risk_level: Literal["low", "medium", "high"] = "medium"
    findings: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

class RecklessReviewOutcome(BaseModel):
    completed: bool = False
    approved: bool = False
    decision: str = "reject"
    reason: str = ""
    failure_reason: str = ""
    requested_model: str = RECKLESS_REVIEW_MODEL
    actual_model: str = ""
    backend: str = ""
    trace_id: str = ""
    decision_event_id: str = ""
```

Call the adapter with `task_family="review"`, `stage_key="reckless_human_gate"`, `preferred_provider_kind="spark"`, `preferred_model=RECKLESS_REVIEW_MODEL`, `temperature=0.1`, JSON response format, a closed output schema, and `permission_profile="prompt_only_readonly"`.

Drain stale attempt events before the call. After the call, derive the actual model from the Codex trace or the final successful ordinary attempt. Save the full sanitized request, response, attempts, and raw Codex events in PromptTrace before returning `approved=True`.

- [ ] **Step 4: Preserve Codex bridge raw events**

In `CodexBridgeClient.chat`, retain a token-free `last_call_trace` containing the exact request body and response body. In `LLMCallRouter`, merge that trace and the exact requested model into `LLMCallResult.trace` for Codex success.

- [ ] **Step 5: Verify GREEN**

Run: `pytest -q tests/test_reckless_review.py tests/test_llm_router.py`

Expected: PASS, including raw-event and model-mismatch assertions.

- [ ] **Step 6: Commit**

```bash
git add forwin/reckless_review.py forwin/governance.py forwin/llm/codex_client.py forwin/llm/router.py tests/test_reckless_review.py tests/test_llm_router.py
git commit -m "Add strict Spark gate reviewer with full traces"
```

---

### Task 3: Delegate Chapter Review Gates

**Files:**
- Create: `forwin/orchestrator_loop_core/reckless_review.py`
- Modify: `forwin/orchestrator_loop_core/service.py`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Test: `tests/test_governance_review_and_checkpoint.py`
- Test: `tests/test_phase05_regressions.py`

**Interfaces:**
- Consumes: `RecklessReviewAgent.review_and_record`.
- Produces: `WritingOrchestrator._delegate_reckless_review` and `_pause_for_human_or_reckless_review`.
- Produces: chapter `acceptance_mode="reckless_approved"` after delegated approval.

- [ ] **Step 1: Write failing chapter-gate tests**

Add focused regressions that use a fake Spark reviewer:

```python
def test_checkpoint_mode_spark_approval_continues_to_acceptance() -> None:
    result, plan = run_reckless_chapter_fixture(
        gate_kind="checkpoint_mode", spark_decision="approve"
    )
    assert result.status == "completed"
    assert plan.status == "accepted"
    assert plan.acceptance_mode == "reckless_approved"

def test_spark_rejection_keeps_needs_review_and_pauses() -> None:
    result, plan = run_reckless_chapter_fixture(
        gate_kind="copilot_non_pass", spark_decision="reject"
    )
    assert result.status == "needs_review"
    assert plan.status == "needs_review"

def test_reckless_mode_does_not_bypass_canon_system_block() -> None:
    result, _plan = run_reckless_chapter_fixture(
        gate_kind="canon_system_block", spark_decision="approve"
    )
    assert result.status == "needs_review"
    assert result.system_block_chapters == [1]
```

Exercise checkpoint mode, copilot non-pass, blackbox fail, `should_apply_canon=False`, and interval review using parameterized fixtures where practical.

- [ ] **Step 2: Run targeted tests and verify RED**

Run: `pytest -q tests/test_governance_review_and_checkpoint.py tests/test_phase05_regressions.py -k 'reckless'`

Expected: FAIL because chapter gates still pause without invoking Spark.

- [ ] **Step 3: Add one chapter review decision point**

Refactor the duplicated pre-canon human branches into one `review_gate_reason`. In human mode preserve current behavior exactly. In reckless mode call the reviewer with the complete writer output, review verdict, residual issues, operation mode, interval metadata, chapter plan identity, and source review/draft ids.

On approve, continue to `_apply_canon_candidate` and record `reckless_approved`. On reject/failure, preserve `needs_review` and `paused_for_review`. Do not intercept the later deterministic canon block branch.

- [ ] **Step 4: Verify GREEN and neighboring regressions**

Run: `pytest -q tests/test_governance_review_and_checkpoint.py tests/test_phase05_regressions.py -k 'reckless or checkpoint or needs_review or canon_gate'`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/orchestrator_loop_core/reckless_review.py forwin/orchestrator_loop_core/service.py forwin/orchestrator_loop_core/project_chapters.py tests/test_governance_review_and_checkpoint.py tests/test_phase05_regressions.py
git commit -m "Delegate chapter review gates in reckless mode"
```

---

### Task 4: Delegate Manual, Band, and Audit Pauses

**Files:**
- Modify: `forwin/orchestrator_loop_core/reckless_review.py`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Modify: `forwin/orchestrator_loop_core/governance.py`
- Test: `tests/test_governance_review_and_checkpoint.py`
- Test: `tests/test_generation_audit_checkpoints.py`

**Interfaces:**
- Consumes: project governance `review_delegation_mode` and the agent outcome.
- Produces: `_delegate_checkpoint_if_reckless` and `_resolve_reckless_checkpoint`.

- [ ] **Step 1: Write failing checkpoint tests**

Cover:

```python
def test_reckless_approval_overrides_manual_chapter_start_checkpoint() -> None:
    assert result.status == "completed"
    assert checkpoint.status == "overridden"
    assert checkpoint.resolved_at is not None

@pytest.mark.parametrize("status", ["warn", "fail", "error"])
def test_reckless_approval_overrides_pausing_band_checkpoint(status: str) -> None:
    assert result.status == "completed"

def test_reckless_reject_preserves_checkpoint_pause() -> None:
    assert result.paused is True
    assert checkpoint.status != "overridden"

def test_reckless_generation_audit_approval_skips_only_current_pause() -> None:
    assert result.status == "completed"
    assert project_governance.generation_audit_pause_enabled is True
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest -q tests/test_governance_review_and_checkpoint.py tests/test_generation_audit_checkpoints.py -k 'reckless'`

Expected: FAIL because these gates still pause directly.

- [ ] **Step 3: Apply model-approved checkpoint semantics**

Before each manual checkpoint pause, call Spark with the full serialized checkpoint. Before a pausing band checkpoint, include evaluator status and every issue. On approval set `status="overridden"`, set reason to the model reason, set `resolved_at=datetime.now(timezone.utc)`, save the row, and emit `reckless_gate_overridden` linked to the review trace.

For generation audit pause, pass the complete audit payload to Spark and clear only the local `generation_audit_pause` boolean when approved.

- [ ] **Step 4: Verify GREEN**

Run: `pytest -q tests/test_governance_review_and_checkpoint.py tests/test_generation_audit_checkpoints.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/orchestrator_loop_core/reckless_review.py forwin/orchestrator_loop_core/project_chapters.py forwin/orchestrator_loop_core/governance.py tests/test_governance_review_and_checkpoint.py tests/test_generation_audit_checkpoints.py
git commit -m "Delegate checkpoint pauses in reckless mode"
```

---

### Task 5: Expose Mode and Full Audit Surface

**Files:**
- Modify: `forwin/ui_assets/home/app_task_governance.js`
- Modify: `forwin/mcp/client.py`
- Modify: `forwin/mcp/http.py`
- Modify: `forwin/api_schema/genesis.py`
- Modify: `forwin/api_observability_routes.py`
- Modify: `forwin/project_payloads/genesis.py`
- Test: `tests/test_api_pages_rendering.py`
- Test: `tests/test_mcp_server.py`
- Test: `tests/test_observability_v451.py`

**Interfaces:**
- Produces MCP tool: `project_set_reckless_mode(project_id, enabled, reason)`.
- Extends PromptTrace API fields: `backend`, `codex_job_id`, `permission_profile`, `fallback_used`.
- Adds task-drawer checkbox posting `review_delegation_mode`.

- [ ] **Step 1: Write failing API/MCP/UI tests**

```python
def test_prompt_trace_detail_exposes_execution_identity() -> None:
    detail = api_module.get_prompt_trace_detail(trace.id)
    assert detail.backend == "codex_bridge"
    assert detail.permission_profile == "prompt_only_readonly"

def test_set_reckless_mode_mcp_posts_governance_update() -> None:
    result = await client.project_set_reckless_mode(
        project_id="project-1", enabled=True, reason="stress run"
    )
    assert request_json["review_delegation_mode"] == "reckless"
```

Static page rendering must include `review_delegation_mode`, `鲁莽模式`, and `Codex 5.3 Spark`.

- [ ] **Step 2: Run and verify RED**

Run: `pytest -q tests/test_api_pages_rendering.py tests/test_mcp_server.py tests/test_observability_v451.py -k 'reckless or prompt_trace_detail'`

Expected: FAIL because the UI, MCP tool, and trace fields are absent.

- [ ] **Step 3: Implement the surfaces**

Add a project governance checkbox with:

```javascript
recklessMode.checked = governance.review_delegation_mode === 'reckless';
// save payload
review_delegation_mode: recklessMode.checked ? 'reckless' : 'human',
```

When enabled, render a warning badge labeled `鲁莽模式 · Codex 5.3 Spark 审核`.

The MCP client posts to `/api/projects/{project_id}/governance` with the mode and non-empty reason; the MCP server registers the write tool and returns the refreshed project.

Extend PromptTrace serializers without removing existing fields.

- [ ] **Step 4: Verify GREEN**

Run: `pytest -q tests/test_api_pages_rendering.py tests/test_mcp_server.py tests/test_observability_v451.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/ui_assets/home/app_task_governance.js forwin/mcp/client.py forwin/mcp/http.py forwin/api_schema/genesis.py forwin/api_observability_routes.py forwin/project_payloads/genesis.py tests/test_api_pages_rendering.py tests/test_mcp_server.py tests/test_observability_v451.py
git commit -m "Expose reckless mode and Spark audit traces"
```

---

### Task 6: Verification, Push, Deploy, and Runtime Acceptance

**Files:**
- Modify only if a test exposes a feature regression.
- Preserve: `.forwin-run-logs/`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: deployed master and runtime evidence for both delegated gate classes.

- [ ] **Step 1: Run focused suites**

Run:

```bash
pytest -q tests/test_reckless_review.py tests/test_governance_review_and_checkpoint.py tests/test_generation_audit_checkpoints.py tests/test_project_operation_guards.py tests/test_generation_task_payload.py tests/test_llm_router.py tests/test_api_pages_rendering.py tests/test_mcp_server.py tests/test_observability_v451.py
```

Expected: PASS with no new warnings attributable to this feature.

- [ ] **Step 2: Run the full suite**

Run: `pytest -q`

Expected: all existing tests pass; pre-existing skips remain documented.

- [ ] **Step 3: Verify repository scope**

Run: `git status --short && git diff --check`

Expected: only intended tracked changes/commits and the pre-existing untracked `.forwin-run-logs/`; no whitespace errors.

- [ ] **Step 4: Push master**

Run: `git push origin master`

Expected: remote master advances to the feature commit.

- [ ] **Step 5: Deploy through the approved sync path**

Run: `ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'`

Expected: deploy reports the pushed commit and healthy ForWin services.

- [ ] **Step 6: Run operator preflight**

Run: `python3 scripts/check_codex_operator_ready.py`

Expected: required checks pass.

- [ ] **Step 7: Runtime acceptance through ForWin MCP**

Use `task_active_generation_check` before every generation mutation. Enable reckless mode with `project_set_reckless_mode`, run a bounded project configured to hit a chapter review and a pausing band checkpoint, then verify:

- both gates have `reckless_review_requested` and `reckless_review_decided` events;
- accepted gates have `reckless_gate_overridden`;
- actual model is exactly `gpt-5.3-codex-spark`;
- each event references a PromptTrace containing full input, raw output, attempts, backend, and permission profile;
- generation proceeds after approval;
- no hard gate is bypassed.

- [ ] **Step 8: Final status**

Report commit ids, focused/full test results, deploy image/commit, runtime project/task ids, delegated gate outcomes, and trace ids.
