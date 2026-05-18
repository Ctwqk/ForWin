# ForWin Review Follow-Up Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the review follow-up issues for governance negation and API dependency ownership.

**Architecture:** Keep the change narrow. `governance_keywords` moves from keyword-level negation to occurrence-level negation. `ApiRouteDeps` keeps its public shape and legacy flat-constructor compatibility, while removing the empty `WorldModelDeps` group and making `PublisherDeps` the sole owner of publisher dependencies.

**Tech Stack:** Python 3, dataclasses, pytest, existing ForWin route registry and governance tests.

---

### Task 1: Fix Governance Keyword Occurrence Semantics

**Files:**
- Modify: `forwin/governance_keywords.py`
- Modify: `tests/test_governance_keyword_registry.py`

- [ ] **Step 1: Add mixed occurrence regression test**

Add a test where the first `死亡` occurrence is positive and a later occurrence is negated:

```python
def test_mixed_negated_and_positive_death_occurrences_still_trigger() -> None:
    constraint = NarrativeConstraintInfo(
        id="c1",
        constraint_type="character_availability",
        subject_name="韩青",
        description="韩青不能死亡",
    )

    issues = evaluate_constraint_issues(
        [constraint],
        combined_text="本章韩青死亡。要避免死亡披露过早。",
        state_changes=[],
        events=[],
        thread_beats=[],
        reviewer="test",
        issue_type="test",
        target_scope="chapter",
    )

    assert [issue.rule_name for issue in issues] == ["future_constraint_violation"]
```

- [ ] **Step 2: Run the targeted test and confirm failure**

Run:

```bash
python3 -m pytest tests/test_governance_keyword_registry.py::test_mixed_negated_and_positive_death_occurrences_still_trigger -q
```

Expected before implementation: failure because no violation is produced.

- [ ] **Step 3: Implement occurrence-level negation**

In `forwin/governance_keywords.py`, add `_occurrence_is_prefix_negated()` and update the public helpers:

```python
def _occurrence_is_prefix_negated(text: str, keyword: str, index: int, *, window: int = 12) -> bool:
    if not keyword or index < 0:
        return False
    local = str(text or "")
    clause_start = max(
        local.rfind(marker, 0, index)
        for marker in ("。", "！", "？", "；", ";", "\n", "，", ",")
    )
    before = local[max(clause_start + 1, index - window) : index]
    return any(marker in before for marker in NEGATION_MARKERS)


def _keyword_occurrences(text: str, keyword: str) -> list[int]:
    if not keyword:
        return []
    local = str(text or "")
    indexes: list[int] = []
    index = local.find(keyword)
    while index >= 0:
        indexes.append(index)
        index = local.find(keyword, index + len(keyword))
    return indexes
```

`keyword_is_prefix_negated()` should return `True` only when the keyword appears and every occurrence is negated. `text_has_unnegated_keyword()` and `first_unnegated_keyword()` should return based on any unnegated occurrence.

- [ ] **Step 4: Run governance keyword tests**

Run:

```bash
python3 -m pytest tests/test_governance_keyword_registry.py -q
```

Expected: all tests pass.

### Task 2: Clean ApiRouteDeps Domain Ownership

**Files:**
- Modify: `forwin/api_route_registry.py`
- Modify: `tests/test_architecture_boundaries.py`

- [ ] **Step 1: Add/update API registry tests**

Update `test_api_route_deps_are_grouped_by_domain` to expect no `world_model` group. Add assertions that `CoreDeps` does not own publisher fields and `PublisherDeps` does. Add a legacy-constructor smoke test with dummy callables proving `deps.get_publisher_manager` resolves through `PublisherDeps`.

- [ ] **Step 2: Run architecture tests and confirm failure**

Run:

```bash
python3 -m pytest tests/test_architecture_boundaries.py::test_api_route_deps_are_grouped_by_domain -q
```

Expected before implementation: failure because `world_model` still exists and publisher fields still live in `CoreDeps`.

- [ ] **Step 3: Remove `WorldModelDeps` and publisher duplication**

In `forwin/api_route_registry.py`:

- delete `WorldModelDeps`
- remove `ApiRouteDeps.world_model`
- remove `world_model` from constructor parameters and `__getattr__`
- remove `get_publisher_manager` and `render_publishers_page` from `CoreDeps`
- construct `PublisherDeps` from explicit `publisher` or legacy kwargs
- read publisher dependencies in `register_api_routes()` from `deps.publisher`

- [ ] **Step 4: Run architecture/runtime tests**

Run:

```bash
python3 -m pytest tests/test_architecture_boundaries.py tests/test_world_v4_aliases.py tests/test_runtime_container.py -q
```

Expected: all pass.

### Task 3: Final Verification

**Files:**
- Verify all modified files

- [ ] **Step 1: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_governance_keyword_registry.py -q
python3 -m pytest tests/test_architecture_boundaries.py tests/test_world_v4_aliases.py tests/test_runtime_container.py -q
python3 -m pytest tests/test_config_defaults.py tests/test_writer_prompt_contract.py -q
```

Expected: all pass.

- [ ] **Step 2: Run syntax and diff hygiene checks**

Run:

```bash
python3 -m compileall -q forwin
git diff --check
```

Expected: both pass with no output.
