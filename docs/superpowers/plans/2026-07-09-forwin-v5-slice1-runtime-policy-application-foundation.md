# ForWin v5 Slice 1 Runtime Policy and Application Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ForWin's mode/flag/configuration overlap with one typed per-project runtime policy and immutable task snapshot, then establish the generation application boundary used by task producers and workers.

**Architecture:** Environment-backed `InfrastructureConfig` contains only process infrastructure and the model catalog. `RuntimePolicy` contains all project generation behavior, is persisted on `Project`, and is copied unchanged into each durable task. The current orchestrator remains temporarily, but receives policy explicitly and may no longer read generation behavior from infrastructure config, runtime settings, governance JSON, or request overrides.

**Tech Stack:** Python 3.12, Pydantic 2.9+, SQLAlchemy 2, Alembic, FastAPI, FastMCP, existing vanilla JavaScript console, pytest, Playwright.

## Global Constraints

- Existing project data, runtime settings files, request payloads, environment policy variables, and deprecated imports are not migrated.
- The only persisted quality profiles are exactly `standard` and `pulp`.
- Generation has no `operation_mode`; it always uses the strict blackbox pipeline.
- Gate delegation values are exactly `human` and `spark`.
- A fail verdict or hard residual never reaches gate delegation.
- `InfrastructureConfig` may contain secrets; `RuntimePolicy` and task payloads may not.
- `GenerationTask.policy_snapshot` is immutable for the lifetime of the task.
- Model credentials come only from environment-backed model catalog entries.
- Blocked and failed candidates are always retained; there is no `freeze_failed_candidates` switch.
- No compatibility alias named `Config`, `ProjectGovernanceSettings`, or `review_delegation_mode` is introduced.
- Swarm role topology and publisher security checks remain unchanged.
- Tasks 2-7 form one intentionally non-deployable refactor window: each task must pass its focused tests, and the first required full-suite green point is the end of Task 7.
- Every task follows RED -> GREEN -> focused regression -> commit.

---

## File Structure

### New files

- `forwin/runtime/policy.py`: immutable policy models and standard/pulp profile factory.
- `forwin/runtime/policy_store.py`: project policy JSON persistence with optimistic version checks.
- `forwin/application/__init__.py`: application-service exports.
- `forwin/application/errors.py`: stable application/configuration errors used by task payload and enqueue boundaries.
- `forwin/application/generation.py`: generation enqueue and claimed-task execution boundary.
- `forwin/generation/task_repository.py`: durable generation-task persistence used by the application service.
- `forwin/api_schema/policy.py`: project policy and read-only runtime catalog DTOs.
- `forwin/api_project_policy.py`: explicit project policy HTTP operations.
- `forwin/migrations/versions/0022_runtime_policy.py`: temporary forward migration, later squashed into the v5 baseline.
- `forwin/migrations/versions/0023_drop_governance_settings.py`: removes the old settings column after callers move to runtime policy.
- `tests/test_runtime_policy.py`: profile and validation tests.
- `tests/test_project_policy_store.py`: persistence/version tests.
- `tests/test_project_policy_api.py`: API DTO and policy update tests.
- `tests/test_generation_application_service.py`: enqueue and execute-boundary tests.

### Files removed in this slice

- `forwin/runtime_settings.py`
- `forwin/review_engine/rules/auto_approve.py`
- `tests/review_engine/test_auto_approve.py`
- `tests/review_engine/test_repair_v2_shadow.py`
- `tests/test_config_deprecations.py`

### Major modified ownership points

- `forwin/config.py`: infrastructure-only config, renamed `InfrastructureConfig`.
- `forwin/models/project.py`: `runtime_policy_json` and `runtime_policy_version` replace `governance_json`.
- `forwin/generation/task_payload.py`: exact policy snapshot replaces arbitrary runtime overrides.
- `forwin/runtime/{container,services,ports,factories}.py`: explicit infrastructure and policy inputs.
- `forwin/orchestrator_loop_core/*`: transitional consumers use `self.policy`, never behavior fields on `self.config`.
- `forwin/governance.py`: retains audit/checkpoint/constraint types, loses project runtime settings.
- `forwin/api_schema/{llm,governance,project}.py`: old settings and per-request mode overrides removed.
- `forwin/api_core/{app,state,runtime,tasks,generation}.py`: runtime-settings store removed and application service wired.
- `forwin/api_{runtime,system_routes,governance_support,governance_routes}.py`: policy/catalog operations replace global mutable preferences.
- `forwin/project_ops/{lifecycle,generation,reviews}.py`: project policy loaded once and passed to task creation.
- `forwin/production/{scheduler,executor}.py`: generation task creation goes through the application service.
- `forwin/mcp/client.py`, `forwin/mcp/http.py`, `forwin/mcp/models.py`: `project_set_gate_delegate` replaces the old reckless-mode mutation.
- `forwin/ui_assets/home/*`: only v5 policy controls remain.

---

### Task 1: Define the Immutable Runtime Policy

**Files:**
- Create: `forwin/runtime/policy.py`
- Create: `tests/test_runtime_policy.py`
- Modify: `forwin/runtime/__init__.py`

**Interfaces:**
- Produces: `QualityProfile`, `GateDelegate`, `BandCheckpointAction`.
- Produces: `ChapterLengthPolicy`, `PausePolicy`, `ReviewPolicy`, `PlanningPolicy`, `CanonPolicy`, `RuntimePolicy`.
- Produces: `RuntimePolicy.for_profile(profile, *, model_profile_id="")`.
- Produces: `RuntimePolicy.with_user_settings(quality_profile, model_profile_id, min_chars, target_chars, max_chars, review_interval_chapters, manual_checkpoints, band_checkpoint_action, generation_audit_interval, generation_audit_pauses, gate_delegate)`.

- [ ] **Step 1: Write failing policy tests**

```python
import pytest
from pydantic import ValidationError

from forwin.runtime.policy import RuntimePolicy


def test_standard_policy_is_strict_and_complete() -> None:
    policy = RuntimePolicy.for_profile("standard", model_profile_id="env-kimi")
    assert policy.quality_profile == "standard"
    assert policy.model_profile_id == "env-kimi"
    assert policy.chapter_length.model_dump() == {
        "min_chars": 2500,
        "target_chars": 2800,
        "max_chars": 3200,
    }
    assert policy.pause.gate_delegate == "human"
    assert policy.canon.hard_floor is True
    assert policy.canon.book_state_layers == ("world", "map", "cognition", "narrative")
    assert policy.review.allows_signal("canon_quality")
    assert policy.review.allows_repair_scope("book")


def test_pulp_policy_is_deliberately_small_but_keeps_hard_floor() -> None:
    policy = RuntimePolicy.for_profile("pulp")
    assert policy.chapter_length.target_chars == 2400
    assert policy.pause.manual_checkpoints is False
    assert policy.pause.band_checkpoint_action == "continue"
    assert policy.review.max_rewrites == 0
    assert policy.review.repair_scopes == ()
    assert policy.planning.use_llm_simulation is False
    assert policy.canon.hard_floor is True
    assert policy.canon.book_state_layers == ("world",)


def test_runtime_policy_rejects_removed_values_and_invalid_lengths() -> None:
    payload = RuntimePolicy.for_profile("standard").model_dump(mode="python")
    for removed in ("premium", "checkpoint", "copilot"):
        invalid = dict(payload)
        invalid["quality_profile"] = removed
        with pytest.raises(ValidationError):
            RuntimePolicy.model_validate(invalid)
    with pytest.raises(ValidationError):
        RuntimePolicy.for_profile("standard").with_user_settings(
            min_chars=3200,
            target_chars=2800,
            max_chars=3000,
        )
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_runtime_policy.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'forwin.runtime.policy'`.

- [ ] **Step 3: Implement the policy models**

```python
from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

QualityProfile = Literal["standard", "pulp"]
GateDelegate = Literal["human", "spark"]
BandCheckpointAction = Literal["continue", "pause_on_warn", "pause_always"]


class FrozenPolicyModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChapterLengthPolicy(FrozenPolicyModel):
    min_chars: int = Field(ge=500, le=20000)
    target_chars: int = Field(ge=500, le=20000)
    max_chars: int = Field(ge=500, le=20000)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if not self.min_chars <= self.target_chars <= self.max_chars:
            raise ValueError("chapter lengths must satisfy min <= target <= max")
        return self


class PausePolicy(FrozenPolicyModel):
    review_interval_chapters: int = Field(default=0, ge=0, le=500)
    manual_checkpoints: bool = True
    band_checkpoint_action: BandCheckpointAction = "pause_on_warn"
    generation_audit_interval: int = Field(default=6, ge=0, le=500)
    generation_audit_pauses: bool = False
    gate_delegate: GateDelegate = "human"


class ReviewPolicy(FrozenPolicyModel):
    signals: tuple[str, ...]
    repair_scopes: tuple[str, ...]
    max_rewrites: int = Field(ge=0, le=12)
    repair_models: tuple[str, ...]

    def allows_signal(self, name: str) -> bool:
        return name in self.signals

    def allows_repair_scope(self, scope: str) -> bool:
        return scope in self.repair_scopes


class PlanningPolicy(FrozenPolicyModel):
    future_constraints: bool
    plan_health: bool
    provisional_preview: bool
    use_llm_simulation: bool
    context_recency_window: int = Field(ge=0, le=1000)


class CanonPolicy(FrozenPolicyModel):
    hard_floor: bool = True
    quality_gate: Literal["strict", "pulp_fatal"] = "strict"
    book_state_layers: tuple[Literal["world", "map", "cognition", "narrative"], ...]


class RuntimePolicy(FrozenPolicyModel):
    schema_version: Literal[1] = 1
    quality_profile: QualityProfile = "standard"
    model_profile_id: str = ""
    chapter_length: ChapterLengthPolicy
    pause: PausePolicy
    review: ReviewPolicy
    planning: PlanningPolicy
    canon: CanonPolicy
    writer_attention_retries: int = Field(ge=0, le=12)

    @classmethod
    def for_profile(cls, profile: QualityProfile, *, model_profile_id: str = "") -> Self:
        if profile == "pulp":
            return cls(
                quality_profile="pulp",
                model_profile_id=model_profile_id,
                chapter_length=ChapterLengthPolicy(min_chars=1800, target_chars=2400, max_chars=3000),
                pause=PausePolicy(manual_checkpoints=False, band_checkpoint_action="continue", generation_audit_interval=0),
                review=ReviewPolicy(signals=("lint", "publisher"), repair_scopes=(), max_rewrites=0, repair_models=()),
                planning=PlanningPolicy(future_constraints=False, plan_health=False, provisional_preview=False, use_llm_simulation=False, context_recency_window=50),
                canon=CanonPolicy(quality_gate="pulp_fatal", book_state_layers=("world",)),
                writer_attention_retries=1,
            )
        return cls(
            quality_profile="standard",
            model_profile_id=model_profile_id,
            chapter_length=ChapterLengthPolicy(min_chars=2500, target_chars=2800, max_chars=3200),
            pause=PausePolicy(),
            review=ReviewPolicy(
                signals=("experience", "lint", "map_movement", "personality", "canon_quality", "publisher"),
                repair_scopes=("local", "chapter", "band", "arc", "book", "obligation"),
                max_rewrites=3,
                repair_models=("deepseek-reasoner", "deepseek-reasoner", "gpt-5.3-codex-spark"),
            ),
            planning=PlanningPolicy(future_constraints=True, plan_health=True, provisional_preview=False, use_llm_simulation=True, context_recency_window=0),
            canon=CanonPolicy(book_state_layers=("world", "map", "cognition", "narrative")),
            writer_attention_retries=3,
        )
```

Implement the user-editable surface exactly as follows. Selecting another quality profile starts from that profile's complete defaults and then applies explicit overrides:

```python
    def with_user_settings(
        self,
        *,
        quality_profile: QualityProfile | None = None,
        model_profile_id: str | None = None,
        min_chars: int | None = None,
        target_chars: int | None = None,
        max_chars: int | None = None,
        review_interval_chapters: int | None = None,
        manual_checkpoints: bool | None = None,
        band_checkpoint_action: BandCheckpointAction | None = None,
        generation_audit_interval: int | None = None,
        generation_audit_pauses: bool | None = None,
        gate_delegate: GateDelegate | None = None,
    ) -> Self:
        selected_profile = quality_profile or self.quality_profile
        selected_model = self.model_profile_id if model_profile_id is None else model_profile_id.strip()
        base = (
            type(self).for_profile(selected_profile, model_profile_id=selected_model)
            if selected_profile != self.quality_profile
            else self.model_copy(update={"model_profile_id": selected_model})
        )
        length_updates = {
            key: value
            for key, value in {
                "min_chars": min_chars,
                "target_chars": target_chars,
                "max_chars": max_chars,
            }.items()
            if value is not None
        }
        pause_updates = {
            key: value
            for key, value in {
                "review_interval_chapters": review_interval_chapters,
                "manual_checkpoints": manual_checkpoints,
                "band_checkpoint_action": band_checkpoint_action,
                "generation_audit_interval": generation_audit_interval,
                "generation_audit_pauses": generation_audit_pauses,
                "gate_delegate": gate_delegate,
            }.items()
            if value is not None
        }
        next_length = ChapterLengthPolicy.model_validate(
            {**base.chapter_length.model_dump(mode="python"), **length_updates}
        )
        next_pause = PausePolicy.model_validate(
            {**base.pause.model_dump(mode="python"), **pause_updates}
        )
        return type(self).model_validate(
            {
                **base.model_dump(mode="python"),
                "chapter_length": next_length,
                "pause": next_pause,
            }
        )
```

- [ ] **Step 4: Verify GREEN**

Run: `pytest -q tests/test_runtime_policy.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/runtime/policy.py forwin/runtime/__init__.py tests/test_runtime_policy.py
git commit -m "Add immutable v5 runtime policy"
```

---

### Task 2: Split Infrastructure Configuration from Generation Policy

**Files:**
- Modify: `forwin/config.py`
- Modify: `forwin/runtime/container.py`
- Modify: `forwin/runtime/services.py`
- Modify: `forwin/runtime/ports.py`
- Modify: `forwin/runtime/factories.py`
- Modify imports in: `forwin/api_artifacts.py`, `forwin/api_core/app.py`, `forwin/api_core/automation.py`, `forwin/api_core/generation.py`, `forwin/api_core/project_helpers.py`, `forwin/api_core/runtime.py`, `forwin/api_core/state.py`, `forwin/api_core/tasks.py`, `forwin/api_governance_support.py`, `forwin/api_observability_routes.py`, `forwin/api_runtime.py`, `forwin/canon_quality/service.py`, `forwin/checker/hard_floor.py`, `forwin/cli.py`, `forwin/generation/worker.py`, `forwin/generation/worker_cli.py`, `forwin/generation/worker_observability.py`, `forwin/llm/factory.py`, `forwin/llm_eval/cli.py`, `forwin/llm_eval/profiles.py`, `forwin/llm_eval/runner.py`, `forwin/migrations/env.py`, `forwin/orchestrator_loop_core/common.py`, `forwin/orchestrator_loop_core/service.py`
- Modify: `tests/test_config_defaults.py`
- Modify: `tests/test_config_env_resolution.py`
- Delete policy-profile assertions from: `tests/test_quality_profile.py`

**Interfaces:**
- Produces: `InfrastructureConfig.from_env()`.
- Produces: `ModelProfileConfig` and `InfrastructureConfig.resolve_model_profile(profile_id)`.
- Consumes: `RuntimePolicy` when constructing policy-sensitive runtime services.

- [ ] **Step 1: Write failing infrastructure-only tests**

```python
from forwin.config import InfrastructureConfig


def test_infrastructure_config_has_no_generation_policy_fields() -> None:
    config = InfrastructureConfig()
    for removed in (
        "quality_profile", "operation_mode", "writer_mode", "progression_mode",
        "review_delegation_mode", "freeze_failed_candidates", "review_interval_chapters",
        "review_engine_repair_v2_enabled", "review_engine_arc_patcher_enabled",
    ):
        assert not hasattr(config, removed)


def test_model_profile_resolution_is_environment_only(monkeypatch) -> None:
    monkeypatch.setenv("KIMI_API_KEY", "secret")
    config = InfrastructureConfig.from_env()
    profile = config.resolve_model_profile("env-kimi")
    assert profile.model
    assert profile.api_key == "secret"
    with pytest.raises(ValueError, match="Unknown model profile"):
        config.resolve_model_profile("saved-browser-profile")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_config_defaults.py tests/test_config_env_resolution.py -k 'infrastructure or model_profile'`

Expected: FAIL because `InfrastructureConfig` does not exist and policy fields remain on `Config`.

- [ ] **Step 3: Rename and reduce config**

In `forwin/config.py`:

```python
class ModelProfileConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    id: str
    name: str
    api_key: str
    base_url: str
    model: str


class InfrastructureConfig(_InfrastructureFields, _ConfigBaseModel):  # type: ignore[misc]
    model_config = {"extra": "forbid"}

    @classmethod
    def from_env(cls) -> "InfrastructureConfig":
        values = _infrastructure_env_values()
        return cls(**values)

    def resolve_model_profile(self, profile_id: str) -> ModelProfileConfig:
        requested = str(profile_id or "").strip()
        profiles = [ModelProfileConfig.model_validate(item) for item in self.llm_env_profiles]
        if not requested:
            direct = ModelProfileConfig(
                id="env-minimax",
                name="MiniMax (.env)",
                api_key=self.minimax_api_key,
                base_url=self.minimax_base_url,
                model=self.minimax_model,
            )
            return direct
        for profile in profiles:
            if profile.id == requested:
                return profile
        raise ValueError(f"Unknown model profile: {requested}")
```

Remove `PULP_OVERRIDES`, `PREMIUM_OVERRIDES`, `apply_quality_profile`, `_DEPRECATED_REVIEW_CUTOVER_FIELDS`, `_warn_deprecated_review_cutover_config`, `GovernanceConfig`, `runtime_settings_path`, and every generation-policy field from `_ConfigFields` and `_env_values`. Do not export `Config = InfrastructureConfig`.

Replace every `from forwin.config import Config` with `InfrastructureConfig` and update type annotations and `from_env` calls. Runtime policy values must not be re-added to satisfy a caller; the caller is migrated in Tasks 5-7.

- [ ] **Step 4: Keep writer construction explicit**

Replace the old `.writer` property with:

```python
def writer_profile(self, policy: RuntimePolicy) -> WriterProfile:
    lengths = policy.chapter_length
    return WriterProfile.from_values(
        temperature=self.temperature,
        max_tokens=self.max_tokens,
        default_scene_count=self.default_scene_count,
        max_scene_count=self.max_scene_count,
        min_chapter_chars=lengths.min_chars,
        target_chapter_chars=lengths.target_chars,
        max_chapter_chars=lengths.max_chars,
        prompt_budget_chars=self.prompt_budget_chars,
    )
```

Update `RuntimeContainer` and writer factories to pass a `RuntimePolicy` explicitly.

- [ ] **Step 5: Verify focused configuration tests**

Run: `pytest -q tests/test_config_defaults.py tests/test_config_env_resolution.py tests/test_lan_deployment_config.py tests/test_publisher_runtime_auth.py`

Expected: PASS, with no test referring to a generation mode or review-engine flag.

- [ ] **Step 6: Commit**

```bash
git add forwin/config.py forwin/runtime forwin/api_artifacts.py forwin/api_core forwin/api_governance_support.py forwin/api_observability_routes.py forwin/api_runtime.py forwin/canon_quality/service.py forwin/checker/hard_floor.py forwin/cli.py forwin/generation forwin/llm forwin/llm_eval forwin/migrations/env.py forwin/orchestrator_loop_core tests/test_config_defaults.py tests/test_config_env_resolution.py tests/test_lan_deployment_config.py tests/test_publisher_runtime_auth.py tests/test_quality_profile.py
git commit -m "Separate infrastructure config from runtime policy"
```

---

### Task 3: Persist Project Runtime Policy with Optimistic Versioning

**Files:**
- Modify: `forwin/models/project.py`
- Create: `forwin/runtime/policy_store.py`
- Create: `forwin/migrations/versions/0022_runtime_policy.py`
- Create: `tests/test_project_policy_store.py`
- Modify: `forwin/state/updater.py`

**Interfaces:**
- Produces: `Project.runtime_policy_json`, `Project.runtime_policy_version`.
- Produces: `ProjectPolicyRecord(policy, version)`.
- Produces: `ProjectPolicyStore.load(project)` and `.save(project, policy, expected_version)`.

- [ ] **Step 1: Write failing store tests**

```python
def test_project_policy_store_round_trips_and_increments_version(session) -> None:
    project = Project(id="p1", title="T", premise="P", runtime_policy_json="", runtime_policy_version=0)
    session.add(project)
    store = ProjectPolicyStore(session)
    created = store.initialize(project, RuntimePolicy.for_profile("standard"))
    assert created.version == 1
    updated_policy = created.policy.with_user_settings(gate_delegate="spark")
    updated = store.save(project, updated_policy, expected_version=1)
    assert updated.version == 2
    assert store.load(project).policy.pause.gate_delegate == "spark"


def test_project_policy_store_rejects_stale_and_missing_policy(session) -> None:
    project = Project(id="p2", title="T", premise="P", runtime_policy_json="", runtime_policy_version=0)
    session.add(project)
    store = ProjectPolicyStore(session)
    with pytest.raises(ProjectPolicyMissing):
        store.load(project)
    store.initialize(project, RuntimePolicy.for_profile("standard"))
    with pytest.raises(ProjectPolicyVersionConflict):
        store.save(project, RuntimePolicy.for_profile("pulp"), expected_version=0)
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_project_policy_store.py`

Expected: FAIL because policy columns and store do not exist.

- [ ] **Step 3: Add model fields and migration**

In `Project`:

```python
runtime_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
runtime_policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

Keep `governance_json` for this task only so the existing API remains runnable while the new store is tested. Task 4 removes the field and column immediately after all settings callers move.

Migration `0022_runtime_policy.py`:

```python
revision = "0022_runtime_policy"
down_revision = "0021_outbox_events"

def upgrade() -> None:
    op.add_column("projects", sa.Column("runtime_policy_json", sa.Text(), nullable=False, server_default=""))
    op.add_column("projects", sa.Column("runtime_policy_version", sa.Integer(), nullable=False, server_default="0"))

def downgrade() -> None:
    op.drop_column("projects", "runtime_policy_version")
    op.drop_column("projects", "runtime_policy_json")
```

- [ ] **Step 4: Implement strict store semantics**

```python
class ProjectPolicyMissing(RuntimeError):
    pass


class ProjectPolicyVersionConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectPolicyRecord:
    policy: RuntimePolicy
    version: int


class ProjectPolicyStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load(self, project: Project) -> ProjectPolicyRecord:
        if not project.runtime_policy_json or project.runtime_policy_version < 1:
            raise ProjectPolicyMissing(project.id)
        return ProjectPolicyRecord(
            RuntimePolicy.model_validate_json(project.runtime_policy_json),
            int(project.runtime_policy_version),
        )

    def initialize(self, project: Project, policy: RuntimePolicy) -> ProjectPolicyRecord:
        if project.runtime_policy_version:
            raise ProjectPolicyVersionConflict(project.id)
        return self._write(project, policy, 1)

    def save(self, project: Project, policy: RuntimePolicy, *, expected_version: int) -> ProjectPolicyRecord:
        if int(project.runtime_policy_version) != int(expected_version):
            raise ProjectPolicyVersionConflict(project.id)
        return self._write(project, policy, expected_version + 1)

    def _write(self, project: Project, policy: RuntimePolicy, version: int) -> ProjectPolicyRecord:
        project.runtime_policy_json = policy.model_dump_json()
        project.runtime_policy_version = int(version)
        self.session.add(project)
        self.session.flush()
        return ProjectPolicyRecord(policy=policy, version=int(version))
```

Update `StateUpdater.create_project` to require a `RuntimePolicy` and persist version 1. It may no longer accept `governance`.

- [ ] **Step 5: Verify GREEN and migration head**

Run: `pytest -q tests/test_project_policy_store.py tests/test_config_env_resolution.py::test_alembic_env_uses_forwin_database_url_when_ini_has_no_url`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/models/project.py forwin/runtime/policy_store.py forwin/migrations/versions/0022_runtime_policy.py forwin/state/updater.py tests/test_project_policy_store.py
git commit -m "Persist versioned project runtime policy"
```

---

### Task 4: Replace Governance Settings with Project Policy API

**Files:**
- Create: `forwin/api_schema/policy.py`
- Create: `forwin/api_project_policy.py`
- Create: `forwin/migrations/versions/0023_drop_governance_settings.py`
- Create: `tests/test_project_policy_api.py`
- Modify: `forwin/api_schema/__init__.py`
- Modify: `forwin/api_schema/governance.py`
- Modify: `forwin/api_schema/llm.py`
- Modify: `forwin/api_schema/project.py`
- Modify: `forwin/api_governance_support.py`
- Modify: `forwin/api_governance_routes.py`
- Modify: `forwin/api_governance_ops.py`
- Modify: `forwin/governance.py`
- Modify: `forwin/project_payloads/project_detail.py`
- Modify: `forwin/project_payloads/project_summary.py`
- Modify: `forwin/project_ops/lifecycle.py`
- Modify: `forwin/project_ops/generation.py`
- Modify: `forwin/project_ops/reviews.py`

**Interfaces:**
- Produces: `RuntimePolicyUpdateRequest`, `RuntimePolicyResponse`.
- Produces: `get_project_policy` and `update_project_policy` operations.
- Produces: `DecisionEventType.RUNTIME_POLICY_UPDATED`.
- Removes: `ProjectGovernanceSettings`, `new_project_governance`, `normalize_project_governance`, `governance_to_json`.

- [ ] **Step 1: Write failing API tests**

```python
def test_project_create_initializes_standard_runtime_policy(api_module) -> None:
    created = create_genesis_project(api_module, project_id="policy-create")
    response = api_module.get_project_policy(created.project_id)
    assert response.version == 1
    assert response.policy.quality_profile == "standard"
    assert not hasattr(response.policy, "operation_mode")


def test_project_policy_update_requires_expected_version_and_reason(api_module) -> None:
    project = create_genesis_project(api_module, project_id="policy-update")
    request = RuntimePolicyUpdateRequest(
        expected_version=1,
        quality_profile="standard",
        model_profile_id="env-kimi",
        min_chapter_chars=2500,
        target_chapter_chars=2800,
        max_chapter_chars=3200,
        review_interval_chapters=0,
        manual_checkpoints=True,
        band_checkpoint_action="pause_on_warn",
        generation_audit_interval=6,
        generation_audit_pauses=False,
        gate_delegate="spark",
        reason="delegate optional pauses",
    )
    response = api_module.update_project_policy(
        project.project_id,
        request,
    )
    assert response.version == 2
    assert response.policy.pause.gate_delegate == "spark"
    with pytest.raises(HTTPException) as exc:
        api_module.update_project_policy(project.project_id, request)
    assert exc.value.status_code == 409
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_project_policy_api.py`

Expected: FAIL because the policy API DTOs and operations do not exist.

- [ ] **Step 3: Add exact transport DTOs**

```python
class RuntimePolicyUpdateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    quality_profile: QualityProfile
    model_profile_id: str = ""
    min_chapter_chars: int = Field(ge=500, le=20000)
    target_chapter_chars: int = Field(ge=500, le=20000)
    max_chapter_chars: int = Field(ge=500, le=20000)
    review_interval_chapters: int = Field(ge=0, le=500)
    manual_checkpoints: bool
    band_checkpoint_action: BandCheckpointAction
    generation_audit_interval: int = Field(ge=0, le=500)
    generation_audit_pauses: bool
    gate_delegate: GateDelegate
    reason: str = Field(min_length=1)


class RuntimePolicyResponse(BaseModel):
    ok: bool = True
    project_id: str
    version: int
    policy: RuntimePolicy
    message: str = ""
```

- [ ] **Step 4: Implement operations and remove settings from governance**

`forwin/api_project_policy.py` uses this operation body:

```python
def update_project_policy(
    project_id: str,
    request: RuntimePolicyUpdateRequest,
    *,
    session_factory,
) -> RuntimePolicyResponse:
    with session_factory.begin() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在")
        store = ProjectPolicyStore(session)
        current = store.load(project)
        next_policy = current.policy.with_user_settings(
            quality_profile=request.quality_profile,
            model_profile_id=request.model_profile_id,
            min_chars=request.min_chapter_chars,
            target_chars=request.target_chapter_chars,
            max_chars=request.max_chapter_chars,
            review_interval_chapters=request.review_interval_chapters,
            manual_checkpoints=request.manual_checkpoints,
            band_checkpoint_action=request.band_checkpoint_action,
            generation_audit_interval=request.generation_audit_interval,
            generation_audit_pauses=request.generation_audit_pauses,
            gate_delegate=request.gate_delegate,
        )
        try:
            saved = store.save(project, next_policy, expected_version=request.expected_version)
        except ProjectPolicyVersionConflict as exc:
            raise HTTPException(409, "runtime policy version conflict") from exc
        StateUpdater(session).save_decision_event(
            DecisionEventInfo(
                project_id=project_id,
                event_family="audit_action",
                event_type=DecisionEventType.RUNTIME_POLICY_UPDATED,
                actor_type="api",
                summary="项目运行策略已更新。",
                reason=request.reason,
                payload={"previous_version": current.version, "new_version": saved.version},
            )
        )
        return RuntimePolicyResponse(project_id=project_id, version=saved.version, policy=saved.policy)
```

Map `ProjectPolicyMissing` to HTTP 500 in both get and update operations.

Add `RUNTIME_POLICY_UPDATED = "runtime_policy_updated"` to `DecisionEventType` and replace the old `GOVERNANCE_UPDATED` settings event at every policy update call site.

Remove project settings get/update handlers from governance routes while retaining manual checkpoints, constraints, decision events, and insights. Rename project payload field `governance` to `runtime_policy` and include the policy version.

Add migration `0023_drop_governance_settings.py` after all callers move:

```python
revision = "0023_drop_governance"
down_revision = "0022_runtime_policy"

def upgrade() -> None:
    op.drop_column("projects", "governance_json")

def downgrade() -> None:
    op.add_column("projects", sa.Column("governance_json", sa.Text(), nullable=False, server_default="{}"))
```

Remove `Project.governance_json` from the model in this task.

Project creation must call:

```python
policy = RuntimePolicy.for_profile("standard")
project = updater.create_project(
    title=title,
    premise=premise,
    genre=str(req.genre or "").strip() or "玄幻",
    setting_summary=str(req.setting_summary or "").strip(),
    target_total_chapters=max(1, int(req.target_total_chapters or 1)),
    runtime_policy=policy,
    creation_status="creating",
    automation_json=json.dumps(automation.model_dump(mode="json"), ensure_ascii=False),
)
```

Continue/review operations load policy from `ProjectPolicyStore(session)`; they do not merge request overrides or infrastructure defaults.

- [ ] **Step 5: Verify GREEN**

Run: `pytest -q tests/test_project_policy_api.py tests/test_project_operation_guards.py tests/test_governance_decision_api.py tests/test_governance_review_and_checkpoint.py`

Expected: PASS. Governance tests cover checkpoints/constraints only and no longer assert generation settings.

- [ ] **Step 6: Commit**

```bash
git add forwin/api_schema forwin/api_project_policy.py forwin/migrations/versions/0023_drop_governance_settings.py forwin/models/project.py forwin/api_governance_support.py forwin/api_governance_routes.py forwin/api_governance_ops.py forwin/governance.py forwin/project_payloads forwin/project_ops tests/test_project_policy_api.py tests/test_project_operation_guards.py tests/test_governance_decision_api.py tests/test_governance_review_and_checkpoint.py
git commit -m "Replace project governance settings with runtime policy"
```

---

### Task 5: Store an Exact Policy Snapshot in Every Generation Task

**Files:**
- Create: `forwin/application/errors.py`
- Modify: `forwin/generation/task_payload.py`
- Modify: `forwin/generation/worker.py`
- Modify: `forwin/models/task.py`
- Modify: `tests/test_generation_task_payload.py`
- Modify: `tests/test_generation_task_persistence.py`

**Interfaces:**
- Produces: `GenerationTaskExecutionPayload.policy_snapshot: RuntimePolicy`.
- Produces: `GenerationExecutionContext(infrastructure, policy, task_id, root_event_id)`.
- Removes: `runtime_overrides`, `runtime_overrides_from_config`, `build_worker_config_from_payload`.

- [ ] **Step 1: Replace payload tests with exact snapshot tests**

```python
def test_execution_payload_serializes_exact_non_secret_policy() -> None:
    policy = RuntimePolicy.for_profile("standard", model_profile_id="env-kimi").with_user_settings(
        gate_delegate="spark"
    )
    payload = execution_payload(
        mode="continue",
        policy=policy,
        policy_version=3,
        root_event_id="root-1",
        max_chapters=5,
    )
    raw = payload.model_dump(mode="json")
    assert raw["policy_version"] == 3
    assert raw["policy_snapshot"]["pause"]["gate_delegate"] == "spark"
    assert "runtime_overrides" not in raw
    assert "api_key" not in json.dumps(raw)


def test_worker_context_resolves_credentials_without_mutating_policy() -> None:
    infrastructure = InfrastructureConfig(llm_env_profiles=[ENV_PROFILE])
    payload = GenerationTaskExecutionPayload(
        mode="continue",
        policy_version=2,
        policy_snapshot=RuntimePolicy.for_profile("pulp", model_profile_id="env-kimi"),
        root_event_id="root-1",
    )
    context = build_execution_context(infrastructure, payload, task_id="task-1")
    assert context.model_profile.api_key == "secret"
    assert context.policy == payload.policy_snapshot
    assert context.task_id == "task-1"
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_generation_task_payload.py`

Expected: FAIL because payload still contains arbitrary runtime overrides.

- [ ] **Step 3: Implement immutable snapshot payload**

Create stable application errors first:

```python
class PermanentConfigurationError(RuntimeError):
    pass


class ProjectNotFound(LookupError):
    pass


class ActiveGenerationTaskError(RuntimeError):
    pass
```

```python
class GenerationTaskExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: ExecutionMode
    premise: str = ""
    genre: str = ""
    num_chapters: int = 0
    auto_continue: bool = True
    root_event_id: str = ""
    run_until_chapter: int = 0
    max_chapters: int = 0
    policy_version: int = Field(ge=1)
    policy_snapshot: RuntimePolicy


@dataclass(frozen=True, slots=True)
class GenerationExecutionContext:
    infrastructure: InfrastructureConfig
    policy: RuntimePolicy
    model_profile: ModelProfileConfig
    task_id: str
    root_event_id: str


def execution_payload(
    *,
    mode: ExecutionMode,
    policy: RuntimePolicy,
    policy_version: int,
    root_event_id: str,
    premise: str = "",
    genre: str = "",
    num_chapters: int = 0,
    auto_continue: bool = True,
    run_until_chapter: int = 0,
    max_chapters: int = 0,
) -> GenerationTaskExecutionPayload:
    return GenerationTaskExecutionPayload(
        mode=mode,
        premise=premise,
        genre=genre,
        num_chapters=num_chapters,
        auto_continue=auto_continue,
        root_event_id=root_event_id,
        run_until_chapter=run_until_chapter,
        max_chapters=max_chapters,
        policy_version=policy_version,
        policy_snapshot=policy,
    )


def payload_from_json(raw: str | None) -> GenerationTaskExecutionPayload:
    try:
        return GenerationTaskExecutionPayload.model_validate_json(str(raw or ""))
    except (ValueError, TypeError) as exc:
        raise PermanentConfigurationError("generation task has no valid v5 policy snapshot") from exc


def build_execution_context(
    infrastructure: InfrastructureConfig,
    payload: GenerationTaskExecutionPayload,
    *,
    task_id: str,
) -> GenerationExecutionContext:
    return GenerationExecutionContext(
        infrastructure=infrastructure,
        policy=payload.policy_snapshot,
        model_profile=infrastructure.resolve_model_profile(payload.policy_snapshot.model_profile_id),
        task_id=str(task_id or ""),
        root_event_id=payload.root_event_id,
    )
```

`payload_from_json` therefore rejects malformed or missing policy snapshots; it never creates a default continue payload.

- [ ] **Step 4: Update worker execution**

Resolve the model profile from `policy_snapshot.model_profile_id`, create `GenerationExecutionContext`, and pass both infrastructure and policy to the runtime container/application service. Do not call `copy_config` or insert task IDs into config.

- [ ] **Step 5: Verify GREEN**

Run: `pytest -q tests/test_generation_task_payload.py tests/test_generation_task_persistence.py tests/test_generation_worker_observability.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/application/errors.py forwin/generation/task_payload.py forwin/generation/worker.py forwin/models/task.py tests/test_generation_task_payload.py tests/test_generation_task_persistence.py tests/test_generation_worker_observability.py
git commit -m "Snapshot runtime policy in generation tasks"
```

---

### Task 6: Inject Policy Explicitly into Runtime Services and Orchestrator

**Files:**
- Modify: `forwin/runtime/container.py`
- Modify: `forwin/runtime/services.py`
- Modify: `forwin/runtime/ports.py`
- Modify: `forwin/runtime/factories.py`
- Modify: `forwin/orchestrator_loop_core/service.py`
- Modify: `forwin/orchestrator_loop_core/common.py`
- Modify: `forwin/orchestrator_loop_core/run_control.py`
- Modify: `forwin/orchestrator_loop_core/governance.py`
- Modify: `forwin/api_runtime.py`
- Modify: `tests/test_runtime_container.py`
- Modify: `tests/test_runtime_container_roles.py`
- Modify: `tests/test_api_runtime_progress.py`

**Interfaces:**
- `RuntimeContainer.from_config(infrastructure, *, policy, role)` requires policy.
- `RuntimeServices.infrastructure` and `.policy` replace `.config` as the behavior source.
- `WritingOrchestrator.policy` is immutable for one task execution.

- [ ] **Step 1: Write failing injection tests**

```python
def test_runtime_container_requires_policy_and_exposes_it_to_orchestrator(monkeypatch) -> None:
    infrastructure = InfrastructureConfig(database_url=FAKE_URL)
    policy = RuntimePolicy.for_profile("pulp")
    container = RuntimeContainer.from_config(infrastructure, policy=policy, role="generation_worker")
    orchestrator = container.build_writing_orchestrator()
    assert orchestrator.infrastructure is infrastructure
    assert orchestrator.policy is policy
    assert not hasattr(orchestrator, "config")


def test_runtime_container_does_not_accept_implicit_default_policy() -> None:
    with pytest.raises(TypeError):
        RuntimeContainer.from_config(InfrastructureConfig())
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_runtime_container.py tests/test_runtime_container_roles.py -k 'policy or orchestrator'`

Expected: FAIL because policy is not a container input and orchestrator exposes `config`.

- [ ] **Step 3: Change runtime construction**

```python
@dataclass(slots=True)
class RuntimeContainer:
    infrastructure: InfrastructureConfig
    policy: RuntimePolicy
    role: RuntimeRole = "full"

    @classmethod
    def from_config(
        cls,
        infrastructure: InfrastructureConfig,
        *,
        policy: RuntimePolicy,
        role: RuntimeRole = "full",
    ) -> "RuntimeContainer":
        return cls(infrastructure=infrastructure, policy=policy, role=_validate_runtime_role(role))
```

`RuntimeServices` receives `infrastructure` and `policy`. Writer profile, phase-4 LLM use, context recency, review signal selection, band planner cost, provisional preview, and BookState layers are built from policy. Infrastructure-only clients continue to read endpoints and credentials from infrastructure.

Change `WritingOrchestrator.__init__` to require `services` and set:

```python
self.infrastructure = services.infrastructure
self.policy = services.policy
self._task_id = task_id
self._causal_root_id = causal_root_id
```

Delete the legacy constructor that accepts config and discovers `RuntimeContainer` through `globals()`.

- [ ] **Step 4: Replace API runtime builders**

Remove `copy_config`, `build_runtime_config`, and `build_saved_runtime_config`. `run_generation_with_context` and `run_continue_project_with_context` receive `GenerationExecutionContext` and construct `RuntimeContainer.from_config(context.infrastructure, policy=context.policy, role="generation_worker")`.

- [ ] **Step 5: Verify GREEN**

Run: `pytest -q tests/test_runtime_container.py tests/test_runtime_container_roles.py tests/test_api_runtime_progress.py`

Expected: PASS, and `test_writing_orchestrator_keeps_legacy_config_constructor` is deleted rather than updated.

- [ ] **Step 6: Commit**

```bash
git add forwin/runtime forwin/orchestrator_loop_core forwin/api_runtime.py tests/test_runtime_container.py tests/test_runtime_container_roles.py tests/test_api_runtime_progress.py
git commit -m "Inject runtime policy into generation services"
```

---

### Task 7: Remove Operation Modes and Review-Engine Feature Flags from Runtime Decisions

**Files:**
- Create: `forwin/generation/gate_delegation.py`
- Delete: `forwin/reckless_review.py`
- Delete: `forwin/orchestrator_loop_core/reckless_review.py`
- Delete: `forwin/review_engine/rules/auto_approve.py`
- Delete: `tests/review_engine/test_auto_approve.py`
- Delete: `tests/review_engine/test_repair_v2_shadow.py`
- Delete: `tests/test_reckless_review.py`
- Delete: `tests/test_reckless_review_chapter_gate.py`
- Create: `tests/test_gate_delegation.py`
- Create: `tests/test_gate_delegation_chapter.py`
- Modify: `forwin/review_engine/types.py`
- Modify: `forwin/review_engine/audit.py`
- Modify: `forwin/review_engine/engine.py`
- Modify: `forwin/review_engine/rules/final_acceptance.py`
- Modify: `forwin/review_engine/rules/repair_v2.py`
- Modify: `forwin/review_engine/rules/review_outcome.py`
- Modify: `forwin/reviser/final_acceptance.py`
- Modify: `forwin/orchestrator_loop_core/chapter_review_gate.py`
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Modify: `forwin/orchestrator_loop_core/obligation_resolution.py`
- Modify: `forwin/orchestrator_loop_core/writer_attention.py`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Modify: `forwin/runtime/container.py`
- Modify: `forwin/runtime/services.py`
- Modify: `forwin/governance.py`
- Modify: `tests/review_engine/test_arc_book_outcomes.py`
- Modify: `tests/review_engine/test_audit.py`
- Modify: `tests/review_engine/test_commit_with_obligation.py`
- Modify: `tests/review_engine/test_engine.py`
- Modify: `tests/review_engine/test_final_acceptance.py`
- Modify: `tests/review_engine/test_repair_v2.py`
- Modify: `tests/review_engine/test_review_outcome_engine_only.py`
- Modify: `tests/review_engine/test_rule_parity.py`
- Modify: `tests/review_engine/test_types.py`
- Modify: `tests/test_hard_floor.py`
- Modify: `tests/test_canon_repair_stage.py`
- Modify: `tests/test_writer_attention_fallback.py`

**Interfaces:**
- `DecisionInput` no longer has `operation_mode`.
- `FinalAcceptanceGate.evaluate(review, verification)` has no mode argument.
- Repair and review behavior consumes `RuntimePolicy` methods and typed nested fields.
- `chapter_blackbox_failure`, checkpoint, and copilot review gate kinds are absent.
- `GateDelegationService` and `SparkGateDelegate` replace every reckless-review class, method, and event name.
- `SparkGateDelegate.resolve(*, updater: StateUpdater, request: GateDelegationRequest) -> GateResolution` is the only Spark delegation entry point.

- [ ] **Step 1: Write failing invariant tests**

```python
def test_fail_verdict_never_reaches_gate_delegation() -> None:
    delegate = SpyGateDelegate()
    outcome = evaluate_candidate_gate(
        policy=RuntimePolicy.for_profile("standard").with_user_settings(gate_delegate="spark"),
        verdict=review_verdict("fail", hard_issue=True),
        eligible=False,
        delegate=delegate,
    )
    assert outcome.pause_required is True
    assert outcome.should_apply_canon is False
    assert delegate.calls == []


def test_removed_mode_and_flag_tokens_are_absent_from_production() -> None:
    forbidden = ("operation_mode", "review_delegation_mode", "review_engine_", "chapter_blackbox_failure")
    offenders = scan_python_sources(ROOT / "forwin", forbidden)
    assert offenders == []
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_architecture_boundaries.py tests/test_gate_delegation_chapter.py tests/review_engine/test_final_acceptance.py`

Expected: FAIL with old mode/flag tokens and fail-gate delegation.

- [ ] **Step 3: Make strict generation unconditional**

Use these replacements:

```text
self.config.review_interval_chapters       -> self.policy.pause.review_interval_chapters
governance.manual_checkpoints_enabled      -> self.policy.pause.manual_checkpoints
governance.auto_band_checkpoint            -> self.policy.pause.band_checkpoint_action != "continue"
governance.band_warn_action == "pause"     -> self.policy.pause.band_checkpoint_action in {"pause_on_warn", "pause_always"}
generation_audit_interval_chapters          -> self.policy.pause.generation_audit_interval
generation_audit_pause_enabled              -> self.policy.pause.generation_audit_pauses
self.config.review_fail_max_rewrites        -> self.policy.review.max_rewrites
self.config.book_state_layers               -> self.policy.canon.book_state_layers
self.config.blackbox_writer_attention_retries -> self.policy.writer_attention_retries
```

Review signal and repair-scope checks use `policy.review.allows_signal` and `allows_repair_scope`. `hard_floor_gate_enabled` becomes unconditional `policy.canon.hard_floor`, which is true in both profiles.

`ChapterReviewGateOutcome` receives `eligible: bool`. If false, it records `needs_review` and returns without calling `GateDelegationService`. Optional interval/checkpoint gates are evaluated only after eligibility.

Move the strict Spark call and trace persistence into `forwin/generation/gate_delegation.py` with these public types:

```python
class GateDelegationRequest(BaseModel):
    project_id: str
    task_id: str
    gate_kind: str
    scope: str
    chapter_number: int = 0
    band_id: str = ""
    input_snapshot: dict[str, Any] = Field(default_factory=dict)


class GateResolution(BaseModel):
    resolved: bool = False
    approved: bool = False
    decision: Literal["approve", "reject"] = "reject"
    delegate: GateDelegate
    reason: str = ""
    risk_level: Literal["", "low", "medium", "high"] = ""
    findings: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    failure_reason: str = ""
    actual_model: str = ""
    backend: str = ""
    trace_id: str = ""
    decision_event_id: str = ""


class GateDelegationService:
    def __init__(self, *, spark_delegate: SparkGateDelegate) -> None:
        self.spark_delegate = spark_delegate

    def resolve(
        self,
        request: GateDelegationRequest,
        *,
        policy: RuntimePolicy,
        updater: StateUpdater,
    ) -> GateResolution:
        if policy.pause.gate_delegate == "human":
            return GateResolution(delegate="human", reason="human_pause_required")
        return self.spark_delegate.resolve(updater=updater, request=request)
```

Move the complete body of `forwin/reckless_review.py:RecklessReviewAgent.review_and_record`, `_drain_attempts`, `_actual_model`, `_sanitize_complete_log`, and `_json_dump` into `SparkGateDelegate.resolve` without changing call ordering. Apply this exact identifier mapping: `RECKLESS_REVIEW_MODEL -> SPARK_GATE_MODEL`, `RecklessReviewRequest -> GateDelegationRequest`, `RecklessReviewDecision -> SparkGateDecision`, `RecklessReviewOutcome -> GateResolution`, `trace_scope="reckless_review" -> "gate_delegation"`, and `stage_key="reckless_human_gate" -> "spark_pause_gate"`. Rename events to `GATE_DELEGATION_REQUESTED`, `GATE_DELEGATION_DECIDED`, `GATE_DELEGATION_FAILED`, and `GATE_DELEGATION_APPROVED`. Remove the four `RECKLESS_*` constants and all `reckless_approved` acceptance modes; gate approval records `acceptance_mode="gate_approved"` only for an already eligible candidate.

- [ ] **Step 4: Remove mode-dependent review rules**

Remove `OperationMode`, the `operation_mode` member of `DecisionInput`, auto-approve rules, copilot rule IDs, shadow/cutover audit fields, and the mode argument from final acceptance. Keep only verified soft-residual eligibility.

- [ ] **Step 5: Verify GREEN**

Run: `pytest -q tests/review_engine tests/test_hard_floor.py tests/test_canon_repair_stage.py tests/test_gate_delegation.py tests/test_gate_delegation_chapter.py tests/test_writer_attention_fallback.py tests/test_architecture_boundaries.py`

Expected: PASS and no production source contains removed tokens.

- [ ] **Step 6: Commit**

```bash
git add -A forwin/reckless_review.py forwin/generation/gate_delegation.py forwin/review_engine forwin/reviser/final_acceptance.py forwin/orchestrator_loop_core forwin/runtime forwin/governance.py tests/review_engine tests/test_reckless_review.py tests/test_reckless_review_chapter_gate.py tests/test_gate_delegation.py tests/test_gate_delegation_chapter.py tests/test_hard_floor.py tests/test_canon_repair_stage.py tests/test_writer_attention_fallback.py tests/test_architecture_boundaries.py
git commit -m "Remove generation modes and review feature flags"
```

---

### Task 8: Establish GenerationApplicationService as the Task Boundary

**Files:**
- Create: `forwin/application/__init__.py`
- Create: `forwin/application/generation.py`
- Create: `forwin/generation/task_repository.py`
- Create: `tests/test_generation_application_service.py`
- Modify: `forwin/api_core/generation.py`
- Modify: `forwin/generation/worker.py`
- Modify: `forwin/production/executor.py`
- Modify: `forwin/production/scheduler.py`
- Modify: `forwin/runtime/container.py`
- Modify: `forwin/runtime/services.py`

**Interfaces:**
- Produces: `EnqueueGenerationCommand`, `GenerationTaskHandle`, `GenerationApplicationService.enqueue`.
- Produces: `GenerationApplicationService.execute_claimed(task, resume_from_chapter, worker_id)`.
- Produces: `GenerationTaskRepository.has_active` and `.create`.
- Consumes: `ProjectPolicyStore`, durable task lease helpers, and existing generation runner functions.

- [ ] **Step 1: Write failing service tests**

```python
def test_enqueue_snapshots_project_policy_and_creates_one_task(session_factory) -> None:
    project = create_project_with_policy(session_factory, RuntimePolicy.for_profile("standard"))
    service = build_generation_application_service(session_factory)
    handle = service.enqueue(
        EnqueueGenerationCommand(
            project_id=project.id,
            requested_chapters=3,
            max_chapters=3,
            run_until_chapter=3,
            auto_continue=True,
            title="continue",
            subtitle="3 chapters",
            root_event_type="continue_requested",
        )
    )
    task = load_task(session_factory, handle.task_id)
    payload = payload_from_json(task.execution_payload_json)
    assert payload.policy_version == 1
    assert payload.policy_snapshot == RuntimePolicy.for_profile("standard")


def test_enqueue_rejects_second_active_task(session_factory) -> None:
    service = build_generation_application_service(session_factory)
    service.enqueue(command_for("project-1"))
    with pytest.raises(ActiveGenerationTaskError):
        service.enqueue(command_for("project-1"))
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_generation_application_service.py`

Expected: FAIL because `forwin.application.generation` does not exist.

- [ ] **Step 3: Implement enqueue transaction**

```python
@dataclass(frozen=True, slots=True)
class EnqueueGenerationCommand:
    project_id: str
    requested_chapters: int
    max_chapters: int
    run_until_chapter: int
    auto_continue: bool
    title: str
    subtitle: str
    root_event_type: str


@dataclass(frozen=True, slots=True)
class GenerationTaskHandle:
    task_id: str
    project_id: str


GenerationRunner = Callable[
    [GenerationTask, GenerationExecutionContext, int, str],
    None,
]


class GenerationApplicationService:
    def __init__(
        self,
        *,
        session_factory,
        infrastructure: InfrastructureConfig,
        runner: GenerationRunner,
    ) -> None:
        self.session_factory = session_factory
        self.infrastructure = infrastructure
        self.runner = runner

    def enqueue(self, command: EnqueueGenerationCommand) -> GenerationTaskHandle:
        with self.session_factory.begin() as session:
            project = session.get(Project, command.project_id)
            if project is None:
                raise ProjectNotFound(command.project_id)
            repository = GenerationTaskRepository(session)
            if repository.has_active(command.project_id):
                raise ActiveGenerationTaskError(command.project_id)
            policy_record = ProjectPolicyStore(session).load(project)
            task_id = new_task_id()
            root_event = StateUpdater(session).save_decision_event(
                DecisionEventInfo(
                    project_id=command.project_id,
                    task_id=task_id,
                    event_family="business_event",
                    event_type=command.root_event_type,
                    actor_type="api",
                    summary="生成任务已创建。",
                    payload={"requested_chapters": command.requested_chapters},
                )
            )
            payload = execution_payload(
                mode="continue",
                policy=policy_record.policy,
                policy_version=policy_record.version,
                root_event_id=root_event.id,
                auto_continue=command.auto_continue,
                run_until_chapter=command.run_until_chapter,
                max_chapters=command.max_chapters,
            )
            task = repository.create(
                task_id=task_id,
                project_id=command.project_id,
                title=command.title,
                subtitle=command.subtitle,
                requested_chapters=command.requested_chapters,
                max_chapters=command.max_chapters,
                run_until_chapter=command.run_until_chapter,
                payload=payload,
            )
            return GenerationTaskHandle(task_id=task.id, project_id=project.id)

    def execute_claimed(
        self,
        task: GenerationTask,
        *,
        resume_from_chapter: int,
        worker_id: str,
    ) -> None:
        payload = payload_from_json(task.execution_payload_json)
        context = build_execution_context(self.infrastructure, payload, task_id=task.id)
        self.runner(task, context, int(resume_from_chapter), str(worker_id))
```

Implement `GenerationTaskRepository` without callbacks:

```python
TERMINAL_GENERATION_STATUSES = frozenset(
    {"completed", "partial_failed", "failed", "needs_review", "cancelled", "paused"}
)


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


class GenerationTaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def has_active(self, project_id: str) -> bool:
        return self.session.execute(
            select(GenerationTask.id).where(
                GenerationTask.project_id == project_id,
                GenerationTask.deleted_at.is_(None),
                GenerationTask.task_kind == "generation",
                GenerationTask.status.notin_(TERMINAL_GENERATION_STATUSES),
            ).limit(1)
        ).first() is not None

    def create(
        self,
        *,
        task_id: str,
        project_id: str,
        title: str,
        subtitle: str,
        requested_chapters: int,
        max_chapters: int,
        run_until_chapter: int,
        payload: GenerationTaskExecutionPayload,
    ) -> GenerationTask:
        task = GenerationTask(
            id=task_id,
            task_kind="generation",
            status="queued",
            title=title,
            subtitle=subtitle,
            project_id=project_id,
            message=f"开始生成 {requested_chapters} 章。",
            current_stage="queued",
            requested_chapters=requested_chapters,
            max_chapters=max_chapters,
            run_until_chapter=run_until_chapter,
            execution_payload_json=payload_to_json(payload),
        )
        self.session.add(task)
        self.session.flush()
        return task
```

The service owns task ID creation, root event, policy snapshot, active-task uniqueness, and task persistence. Existing project workset and blocker validation may call this service after they compute a valid command; those guards move fully into the service in Slice 5.

- [ ] **Step 4: Route producers and worker execution through the service**

Replace `_create_generation_task` and `_create_continue_generation_task` bodies with application-service calls, then remove their direct persistence logic. `ProductionExecutor` receives `GenerationApplicationService` instead of two callbacks. Worker obtains the service from `RuntimeContainer` and calls `execute_claimed`.

- [ ] **Step 5: Verify GREEN**

Run: `pytest -q tests/test_generation_application_service.py tests/test_generation_task_persistence.py tests/test_generation_worker_observability.py tests/test_production_scheduler.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/application forwin/generation/task_repository.py forwin/api_core/generation.py forwin/generation/worker.py forwin/production forwin/runtime tests/test_generation_application_service.py tests/test_generation_task_persistence.py tests/test_generation_worker_observability.py tests/test_production_scheduler.py
git commit -m "Add generation application task boundary"
```

---

### Task 9: Delete RuntimeSettingsStore and Mutable LLM Preference APIs

**Files:**
- Delete: `forwin/runtime_settings.py`
- Modify: `forwin/api_core/app.py`
- Modify: `forwin/api_core/state.py`
- Modify: `forwin/api_core/runtime.py`
- Modify: `forwin/api_core/tasks.py`
- Modify: `forwin/api_runtime.py`
- Modify: `forwin/api_system_routes.py`
- Modify: `forwin/api_route_registry.py`
- Modify: `forwin/api_schema/llm.py`
- Modify: `forwin/api_pages_home.py`
- Modify: `tests/test_env_llm_profiles.py`
- Modify: `tests/test_api_pages_rendering.py`
- Modify API fixtures in: `tests/test_book_genesis_flow.py`, `tests/test_llm_router.py`, `tests/test_mcp_server.py`, `tests/test_phase05_regressions.py`, `tests/test_world_model.py`

**Interfaces:**
- Produces: read-only `RuntimeCatalogResponse` with sanitized environment model profiles and bootstrap policy.
- Removes: `/api/settings/llm/preferences`, profile upsert/default/delete APIs, and runtime settings file writes.

- [ ] **Step 1: Write failing catalog tests**

```python
def test_runtime_catalog_is_read_only_and_secret_free(api_module) -> None:
    response = api_module.get_runtime_catalog()
    assert response.bootstrap_policy.quality_profile == "standard"
    assert response.model_profiles
    assert response.model_profiles[0].has_api_key is True
    assert not hasattr(response.model_profiles[0], "api_key")


def test_mutable_llm_settings_routes_are_not_registered(api_module) -> None:
    routes = {(route.path, method) for route in api_module.app.routes for method in route.methods}
    assert ("/api/settings/llm/preferences", "POST") not in routes
    assert all("/api/settings/llm/profiles" not in path for path, _method in routes)
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_env_llm_profiles.py tests/test_api_pages_rendering.py -k 'catalog or settings_routes'`

Expected: FAIL because runtime settings are mutable and store-backed.

- [ ] **Step 3: Expose a read-only catalog**

```python
class RuntimeCatalogResponse(BaseModel):
    model_profiles: list[ModelProfile]
    default_model_profile_id: str
    bootstrap_policy: RuntimePolicy


def runtime_catalog(config: InfrastructureConfig) -> RuntimeCatalogResponse:
    profiles = [
        ModelProfile(
            id=item["id"],
            name=item["name"],
            has_api_key=bool(item["api_key"]),
            base_url=item["base_url"],
            model=item["model"],
        )
        for item in config.llm_env_profiles
    ]
    return RuntimeCatalogResponse(
        model_profiles=profiles,
        default_model_profile_id=profiles[0].id if profiles else "env-minimax",
        bootstrap_policy=RuntimePolicy.for_profile("standard"),
    )
```

Delete all file persistence and mutable profile routes. Update API startup state to hold only `InfrastructureConfig`, `RuntimeContainer`, and application services.

- [ ] **Step 4: Rewrite affected tests around environment catalog**

Tests that previously created a `RuntimeSettingsStore` must inject this exact environment catalog fixture and remove tests for disk round trips, profile deletion, and old preference normalization:

```python
ENV_PROFILE = {
    "id": "env-kimi",
    "name": "Kimi (.env)",
    "api_key": "test-secret",
    "base_url": "https://api.moonshot.cn/v1",
    "model": "kimi-k2.5",
}
infrastructure = InfrastructureConfig(llm_env_profiles=[ENV_PROFILE])
```

- [ ] **Step 5: Verify GREEN**

Run: `pytest -q tests/test_env_llm_profiles.py tests/test_api_pages_rendering.py tests/test_book_genesis_flow.py tests/test_llm_router.py tests/test_mcp_server.py tests/test_world_model.py`

Expected: PASS and `rg -n "RuntimeSettingsStore|runtime_settings_path" forwin tests` has no matches.

- [ ] **Step 6: Commit**

```bash
git add -A forwin/runtime_settings.py forwin/api_core forwin/api_runtime.py forwin/api_system_routes.py forwin/api_route_registry.py forwin/api_schema/llm.py forwin/api_pages_home.py tests/test_env_llm_profiles.py tests/test_api_pages_rendering.py tests/test_book_genesis_flow.py tests/test_llm_router.py tests/test_mcp_server.py tests/test_phase05_regressions.py tests/test_world_model.py
git commit -m "Remove mutable runtime settings store"
```

---

### Task 10: Update MCP and Console to the v5 Policy Surface

**Files:**
- Modify: `forwin/mcp/client.py`
- Modify: `forwin/mcp/http.py`
- Modify: `forwin/mcp/models.py`
- Modify: `forwin/ui_assets/home/body.html`
- Modify: `forwin/ui_assets/home/app_bootstrap.js`
- Modify: `forwin/ui_assets/home/app_library.js`
- Modify: `forwin/ui_assets/home/app_state.js`
- Modify: `forwin/ui_assets/home/app_task_governance.js`
- Modify: `forwin/ui_assets/home/app_task_progress.js`
- Modify: `forwin/api_pages_home.py`
- Modify: `tests/test_mcp_server.py`
- Modify: `tests/browser/fixtures.py`
- Modify: `tests/browser/test_governance_and_chapters.py`
- Modify: `tests/browser/test_home_console.py`
- Modify: `tests/browser/test_real_backend_e2e.py`

**Interfaces:**
- Produces MCP tool: `project_set_gate_delegate(project_id, delegate, reason)`.
- Console exposes only profile, model profile, chapter lengths, pause policy, and gate delegate.
- Removes all UI controls for operation/progression mode, freeze switch, API-key persistence, and review feature flags.

- [ ] **Step 1: Write failing MCP/browser tests**

```python
def test_project_set_gate_delegate_via_mcp_updates_runtime_policy(self) -> None:
    result = self._call_tool(
        "project_set_gate_delegate",
        {"project_id": self.project_id, "delegate": "spark", "reason": "autonomous optional gates"},
    )
    self.assertEqual(result.project.runtime_policy.pause.gate_delegate, "spark")


def test_console_exposes_only_v5_generation_policy(page) -> None:
    page.goto("/")
    expect(page.get_by_label("质量配置")).to_be_visible()
    expect(page.get_by_label("暂停门委托")).to_be_visible()
    expect(page.locator("#config_generation_operation_mode")).to_have_count(0)
    expect(page.locator("#task_generation_progression_mode")).to_have_count(0)
    expect(page.locator("#config_generation_freeze_failed_candidates")).to_have_count(0)
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_mcp_server.py tests/browser/test_governance_and_chapters.py tests/browser/test_home_console.py`

Expected: FAIL because old reckless/mode controls and tool names remain.

- [ ] **Step 3: Rename MCP operation and payload**

```python
async def project_set_gate_delegate(
    project_id: str,
    delegate: Literal["human", "spark"],
    reason: str,
) -> MutationResult:
    policy = await client.project_get_policy(project_id)
    return await client.project_update_policy(
        project_id,
        expected_version=policy.version,
        gate_delegate=delegate,
        reason=reason,
    )
```

Delete `project_set_reckless_mode` from client, server registration, models, and tests.

- [ ] **Step 4: Replace console controls**

Render one project policy form with:

- quality profile select: standard/pulp;
- environment model profile select;
- min/target/max chapter lengths;
- review interval number;
- manual checkpoint checkbox;
- band checkpoint action select;
- generation audit interval and pause checkbox;
- gate delegate segmented control: human/Spark.

The form submits `RuntimePolicyUpdateRequest` with current `expected_version` and mandatory reason. Remove global preference save and API-key profile forms.

- [ ] **Step 5: Verify browser behavior**

Use the `build-web-apps:frontend-testing-debugging` skill during execution because this task changes rendered controls. Start the test server and run:

Run: `pytest -q tests/test_mcp_server.py tests/browser/test_governance_and_chapters.py tests/browser/test_home_console.py`

Expected: PASS.

Run: `pytest -q tests/browser --browser chromium`

Expected: PASS with no console errors or missing-control selectors.

- [ ] **Step 6: Commit**

```bash
git add forwin/mcp forwin/ui_assets/home forwin/api_pages_home.py tests/test_mcp_server.py tests/browser
git commit -m "Expose only v5 runtime policy controls"
```

---

### Task 11: Lock Slice 1 Boundaries and Run the Completion Gate

**Files:**
- Modify: `tests/test_architecture_boundaries.py`
- Modify: `tests/test_config_defaults.py`
- Delete: `tests/test_config_deprecations.py`
- Modify: `Design-docs/DESIGN_STATUS.md`
- Modify: `forwin_architecture_consolidation_audit.md`

**Interfaces:**
- Produces architecture guards for removed configuration and application boundaries.
- Produces the Slice 1 completion evidence recorded in design status.

- [ ] **Step 1: Add architecture guards**

```python
def test_v5_runtime_policy_has_no_removed_config_surface() -> None:
    forbidden = (
        "operation_mode", "default_operation_mode", "progression_mode",
        "review_delegation_mode", "review_engine_", "RuntimeSettingsStore",
        "PREMIUM_OVERRIDES", "checkpoint operation mode", "copilot",
    )
    offenders = scan_paths(ROOT / "forwin", forbidden)
    assert offenders == []


def test_generation_task_payload_has_one_policy_snapshot() -> None:
    source = _read("forwin/generation/task_payload.py")
    assert "policy_snapshot: RuntimePolicy" in source
    assert "runtime_overrides" not in source


def test_task_producers_use_generation_application_service() -> None:
    for path in (
        "forwin/api_core/generation.py",
        "forwin/generation/worker.py",
        "forwin/production/executor.py",
    ):
        assert "GenerationApplicationService" in _read(path)
```

- [ ] **Step 2: Run architecture tests and fix only real offenders**

Run: `pytest -q tests/test_architecture_boundaries.py tests/test_config_defaults.py`

Expected: PASS. Do not add allowlist entries for production code.

- [ ] **Step 3: Run targeted Slice 1 suite**

Run:

```bash
pytest -q \
  tests/test_runtime_policy.py \
  tests/test_project_policy_store.py \
  tests/test_project_policy_api.py \
  tests/test_generation_task_payload.py \
  tests/test_generation_application_service.py \
  tests/test_runtime_container.py \
  tests/test_generation_task_persistence.py \
  tests/test_generation_task_lease.py \
  tests/test_generation_worker_observability.py \
  tests/test_production_scheduler.py \
  tests/test_mcp_server.py \
  tests/browser/test_governance_and_chapters.py \
  tests/browser/test_home_console.py
```

Expected: PASS.

- [ ] **Step 4: Run deletion searches**

Run:

```bash
rg -n "operation_mode|default_operation_mode|progression_mode|review_delegation_mode|review_engine_.*_enabled|RuntimeSettingsStore|PREMIUM_OVERRIDES|chapter_blackbox_failure" forwin tests
```

Expected: no matches except historical migration filenames or the v5 design/plan documents; production and active tests must be clean.

Run:

```bash
rg -n "runtime_overrides|governance_json|project_set_reckless_mode" forwin tests
```

Expected: no matches.

- [ ] **Step 5: Run full backend and browser verification**

Run: `pytest -q`

Expected: PASS.

Run: `pytest -q tests/browser --browser chromium`

Expected: PASS.

- [ ] **Step 6: Record Slice 1 status and commit**

Update `Design-docs/DESIGN_STATUS.md` and the audit implementation roadmap with the exact commit IDs and test commands. Do not mark later slices complete.

```bash
git add tests/test_architecture_boundaries.py tests/test_config_defaults.py tests/test_config_deprecations.py Design-docs/DESIGN_STATUS.md forwin_architecture_consolidation_audit.md
git commit -m "Complete v5 runtime policy foundation"
```

- [ ] **Step 7: Push, deploy, and run the 30-chapter gate**

Run: `git push origin master`

Expected: push succeeds.

Run: `ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'`

Expected: deploy sync completes successfully and all ForWin roles become healthy.

Using the `forwin-operator` skill and ForWin MCP tools, create a fresh Genesis project, complete and lock all six Genesis stages, call `project_start_writing` for 30 chapters, and monitor with `task_get`/`chapter_list`. During the run, do not edit source code.

Acceptance:

- 30 chapters accepted or explicitly paused by a declared v5 policy gate;
- every task contains one immutable policy snapshot;
- no operation/progression/reckless compatibility fields appear in API/MCP payloads;
- no duplicate task, chapter, or GraphDelta;
- no source-code hotfix during the run.

Record the run in `docs/operations/30-chapter-v5-slice1-test-log-2026-07-09.md`, using the same timestamped check format as `docs/operations/100-chapter-test-log-2026-07-06.md`. Include project id, task ids, policy version/hash, accepted chapter count, every pause/block, duplicate checks, deployment commit, and the explicit statement that no source file changed during the run. Commit and push that evidence file.
