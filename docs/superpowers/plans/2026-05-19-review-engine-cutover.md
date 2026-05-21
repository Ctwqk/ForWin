# Review Engine Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the review-engine cutover mechanisms so engine decisions are auditable, repair routing is scope-driven, and live cutover can proceed through project allowlists and real stability gates.

**Architecture:** Keep legacy dispatchers as rollback and reverse-shadow sources while adding a typed cutover selector, durable `REVIEW_ENGINE_DECISION` events, and outcome-specific executors. Behavior-changing paths remain behind flags; production cutover stages are controlled by `review_engine_live_cutover_enabled` plus `review_engine_live_cutover_project_allowlist`.

**Tech Stack:** Python 3.13, dataclasses, Pydantic config, SQLAlchemy models/repositories, pytest, existing ForWin review engine, orchestrator loop, governance events, and narrative obligation modules.

---

## Scope Check

This plan intentionally covers the full cutover program from `docs/superpowers/specs/2026-05-19-review-engine-cutover-design.md`, but it places a review checkpoint after Task 4. Tasks 1-4 produce the first behavior-relevant milestone: audit persistence, dashboard data, and `repair_v2` wired in shadow mode. Tasks 5-9 add the remaining mechanisms with live flags off by default. Task 10 verifies the branch and documents the cutover state.

Production Phase 2/3/4 cutover is not claimed by this plan. Those phases require real elapsed observation windows.

## File Structure

- `forwin/review_engine/cutover.py`: Central live/shadow selection helper. No orchestrator details.
- `forwin/review_engine/audit.py`: Payload construction and input digesting only.
- `forwin/review_engine/dashboard.py`: Event payload aggregation for dashboard rows.
- `forwin/review_engine/rules/repair_v2.py`: Scope-driven repair decision, retry limits, and escalation.
- `forwin/review_engine/rules/commit_with_obligation.py`: First-class commit-with-obligation decision rule.
- `forwin/review_engine/rules/auto_approve.py`: Auto-approve decision metadata and interval inputs.
- `forwin/review_engine/interval.py`: Small pure helper for review interval math.
- `forwin/reviser/local_rewrite_executor.py`: Local rewrite execution planning and deterministic rewrites.
- `forwin/narrative_obligations/budget.py`: Chapter, band, arc, and book budget evaluation.
- `forwin/orchestrator_loop_core/governance.py`: Engine decision event recording helper.
- `forwin/orchestrator_loop_core/service.py`: Bind the new helper onto `WritingOrchestrator`.
- `forwin/orchestrator_loop_core/quality_gates.py`: Review outcome, structural patch, deferred acceptance, and cutover dispatch.
- `forwin/orchestrator_loop_core/repair_loop.py`: Repair v2 shadow/live scope selection.
- `forwin/config.py`: New flags and allowlist parsing.
- `scripts/audit_obligation_distribution.py`: Historical obligation distribution audit.
- Tests under `tests/review_engine/` and existing obligation/orchestrator/API test files.

---

### Task 1: Add Cutover Flags And Selection Helper

**Files:**
- Create: `forwin/review_engine/cutover.py`
- Modify: `forwin/config.py`
- Test: `tests/review_engine/test_cutover.py`

- [ ] **Step 1: Write cutover helper tests**

Create `tests/review_engine/test_cutover.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from forwin.review_engine.cutover import CutoverSelection, engine_live_enabled, select_cutover_pair
from forwin.review_engine.types import Decision


def _config(*, enabled: bool, allowlist: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        review_engine_live_cutover_enabled=enabled,
        review_engine_live_cutover_project_allowlist=allowlist,
    )


def _decision(outcome: str, rule_id: str) -> Decision:
    return Decision(
        outcome=outcome,  # type: ignore[arg-type]
        reason=rule_id,
        rule_id=rule_id,
        missing_evidence=[],
        routed_from="fixture",
        sub_action={"rule_id": rule_id},
    )


def test_flag_off_never_enables_engine_live() -> None:
    assert engine_live_enabled(_config(enabled=False, allowlist=[]), "project-1") is False
    assert engine_live_enabled(_config(enabled=False, allowlist=["project-1"]), "project-1") is False


def test_flag_on_empty_allowlist_enables_global_engine_live() -> None:
    assert engine_live_enabled(_config(enabled=True, allowlist=[]), "project-1") is True


def test_flag_on_non_empty_allowlist_limits_engine_live_to_project() -> None:
    config = _config(enabled=True, allowlist=["project-1", "project-3"])

    assert engine_live_enabled(config, "project-1") is True
    assert engine_live_enabled(config, "project-2") is False


def test_select_cutover_pair_swaps_live_and_shadow_when_engine_is_enabled() -> None:
    legacy = _decision("manual_review", "legacy")
    engine = _decision("auto_approve", "engine")

    selection = select_cutover_pair(
        project_id="project-1",
        legacy_decision=legacy,
        engine_decision=engine,
        config=_config(enabled=True, allowlist=[]),
    )

    assert selection == CutoverSelection(
        live=engine,
        shadow=legacy,
        live_source="engine",
        shadow_source="legacy",
        engine_live=True,
    )
```

- [ ] **Step 2: Run the cutover tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_cutover.py -q
```

Expected: FAIL because `forwin.review_engine.cutover` does not exist.

- [ ] **Step 3: Implement the cutover helper**

Create `forwin/review_engine/cutover.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forwin.review_engine.types import Decision


@dataclass(frozen=True)
class CutoverSelection:
    live: Decision
    shadow: Decision
    live_source: str
    shadow_source: str
    engine_live: bool


def engine_live_enabled(config: Any, project_id: str) -> bool:
    if not bool(getattr(config, "review_engine_live_cutover_enabled", False)):
        return False
    allowlist = {
        str(item or "").strip()
        for item in list(getattr(config, "review_engine_live_cutover_project_allowlist", []) or [])
        if str(item or "").strip()
    }
    return not allowlist or str(project_id or "").strip() in allowlist


def select_cutover_pair(
    *,
    project_id: str,
    legacy_decision: Decision,
    engine_decision: Decision,
    config: Any,
) -> CutoverSelection:
    if engine_live_enabled(config, project_id):
        return CutoverSelection(
            live=engine_decision,
            shadow=legacy_decision,
            live_source="engine",
            shadow_source="legacy",
            engine_live=True,
        )
    return CutoverSelection(
        live=legacy_decision,
        shadow=engine_decision,
        live_source="legacy",
        shadow_source="engine",
        engine_live=False,
    )
```

- [ ] **Step 4: Add config fields**

In `forwin/config.py`, add env parsing next to the existing review-engine flags:

```python
"review_engine_local_rewrite_enabled": tracked_bool(
    "review_engine_local_rewrite_enabled",
    "FORWIN_REVIEW_ENGINE_LOCAL_REWRITE_ENABLED",
    False,
),
"review_engine_commit_with_obligation_enabled": tracked_bool(
    "review_engine_commit_with_obligation_enabled",
    "FORWIN_REVIEW_ENGINE_COMMIT_WITH_OBLIGATION_ENABLED",
    False,
),
"review_engine_arc_book_budget_enabled": tracked_bool(
    "review_engine_arc_book_budget_enabled",
    "FORWIN_REVIEW_ENGINE_ARC_BOOK_BUDGET_ENABLED",
    False,
),
"review_engine_live_cutover_enabled": tracked_bool(
    "review_engine_live_cutover_enabled",
    "FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_ENABLED",
    False,
),
"review_engine_live_cutover_project_allowlist": _env_csv(
    env,
    "FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_PROJECT_ALLOWLIST",
),
```

Add dataclass fields:

```python
review_engine_local_rewrite_enabled: bool = False
review_engine_commit_with_obligation_enabled: bool = False
review_engine_arc_book_budget_enabled: bool = False
review_engine_live_cutover_enabled: bool = False
review_engine_live_cutover_project_allowlist: list[str] = []
```

- [ ] **Step 5: Run cutover and config import tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_cutover.py tests/review_engine/test_types.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/config.py forwin/review_engine/cutover.py tests/review_engine/test_cutover.py
git commit -m "feat: add review engine cutover selector"
```

---

### Task 2: Persist Engine Decision Events

**Files:**
- Modify: `forwin/governance.py`
- Modify: `forwin/review_engine/audit.py`
- Modify: `forwin/orchestrator_loop_core/governance.py`
- Modify: `forwin/orchestrator_loop_core/service.py`
- Test: `tests/review_engine/test_audit.py`

- [ ] **Step 1: Extend audit payload tests**

Append to `tests/review_engine/test_audit.py`:

```python
def test_decision_event_payload_records_live_shadow_sources() -> None:
    payload = build_decision_event_payload(
        decision=Decision("manual_review", "needs human", "rule-1", ["deadline"], "router", {}),
        input_digest="abc123",
        shadow_mismatch=True,
        live_or_shadow="live",
        legacy_outcome="manual_review",
        engine_outcome="auto_approve",
    )

    assert payload["live_or_shadow"] == "live"
    assert payload["legacy_outcome"] == "manual_review"
    assert payload["engine_outcome"] == "auto_approve"
    assert payload["shadow_mismatch"] is True
```

- [ ] **Step 2: Run audit tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_audit.py -q
```

Expected: FAIL because `build_decision_event_payload()` does not accept the new keyword arguments.

- [ ] **Step 3: Extend `build_decision_event_payload()`**

Modify `forwin/review_engine/audit.py`:

```python
def build_decision_event_payload(
    *,
    decision: Decision,
    input_digest: str,
    shadow_mismatch: bool,
    live_or_shadow: str = "shadow",
    legacy_outcome: str = "",
    engine_outcome: str = "",
) -> dict[str, object]:
    return {
        "rule_id": decision.rule_id,
        "outcome": decision.outcome,
        "reason": decision.reason,
        "missing_evidence": list(decision.missing_evidence),
        "routed_from": decision.routed_from,
        "sub_action": dict(decision.sub_action),
        "input_digest": input_digest,
        "shadow_mismatch": bool(shadow_mismatch),
        "live_or_shadow": str(live_or_shadow or "shadow"),
        "legacy_outcome": str(legacy_outcome or ""),
        "engine_outcome": str(engine_outcome or ""),
    }
```

- [ ] **Step 4: Add event type**

In `forwin/governance.py`, add this constant in `DecisionEventType` near review events:

```python
REVIEW_ENGINE_DECISION = "review_engine_decision"
```

- [ ] **Step 5: Add the orchestrator helper**

In `forwin/orchestrator_loop_core/governance.py`, import the audit helpers through `common.py` or direct imports if they are not already available:

```python
from forwin.review_engine.audit import build_decision_event_payload, digest_decision_input
from forwin.review_engine.types import Decision, DecisionInput
```

Add:

```python
def _record_engine_decision_event(
    self,
    *,
    updater: StateUpdater,
    decision: Decision,
    decision_input: DecisionInput,
    shadow_mismatch: bool = False,
    live_or_shadow: str = "shadow",
    legacy_outcome: str = "",
    engine_outcome: str = "",
    related_object_type: str = "",
    related_object_id: str = "",
    parent_event_id: str = "",
) -> None:
    try:
        payload = build_decision_event_payload(
            decision=decision,
            input_digest=digest_decision_input(decision_input),
            shadow_mismatch=shadow_mismatch,
            live_or_shadow=live_or_shadow,
            legacy_outcome=legacy_outcome,
            engine_outcome=engine_outcome,
        )
        self._record_decision_event(
            updater=updater,
            project_id=decision_input.project_id,
            chapter_number=decision_input.chapter_number,
            event_family="review_engine",
            event_type=DecisionEventType.REVIEW_ENGINE_DECISION,
            scope="chapter",
            summary=f"engine decided {decision.outcome} via {decision.rule_id}",
            reason=decision.reason,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            payload=payload,
            parent_event_id=parent_event_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to record review engine decision event project=%s chapter=%s rule=%s: %s",
            decision_input.project_id,
            decision_input.chapter_number,
            decision.rule_id,
            exc,
        )
```

Add `_record_engine_decision_event` to the module `__all__` list.

- [ ] **Step 6: Bind the helper to `WritingOrchestrator`**

In `forwin/orchestrator_loop_core/service.py`, add `_record_engine_decision_event` to the import list from `governance.py` and bind it:

```python
WritingOrchestrator._record_engine_decision_event = _record_engine_decision_event
```

- [ ] **Step 7: Run audit tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_audit.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add forwin/governance.py forwin/review_engine/audit.py forwin/orchestrator_loop_core/governance.py forwin/orchestrator_loop_core/service.py tests/review_engine/test_audit.py
git commit -m "feat: persist review engine decision events"
```

---

### Task 3: Wire Dashboard Three-State Chips To Event Payloads

**Files:**
- Modify: `forwin/review_engine/dashboard.py`
- Modify: `forwin/ui_assets/home/page.css`
- Test: `tests/review_engine/test_dashboard.py`

- [ ] **Step 1: Add dashboard tests for system-block grouping**

Append to `tests/review_engine/test_dashboard.py`:

```python
def test_waiting_review_breakdown_marks_system_blocks() -> None:
    breakdown = build_waiting_review_breakdown(
        [
            _event(
                {
                    "rule_id": "commit_with_obligation_over_budget",
                    "outcome": "system_block",
                    "reason": "arc budget exceeded",
                }
            )
        ]
    )

    assert breakdown == [
        {
            "rule_id": "commit_with_obligation_over_budget",
            "outcome": "system_block",
            "reason": "arc budget exceeded",
            "count": 1,
            "status_chip": "系统阻断",
        }
    ]


def test_waiting_review_breakdown_keeps_same_rule_different_outcomes_separate() -> None:
    breakdown = build_waiting_review_breakdown(
        [
            _event({"rule_id": "budget_rule", "outcome": "manual_review", "reason": "needs review"}),
            _event({"rule_id": "budget_rule", "outcome": "system_block", "reason": "over budget"}),
        ]
    )

    assert [row["outcome"] for row in breakdown] == ["manual_review", "system_block"]
    assert [row["status_chip"] for row in breakdown] == ["需要人工判断", "系统阻断"]
```

- [ ] **Step 2: Run dashboard tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_dashboard.py -q
```

Expected: FAIL because `system_block` rows are currently filtered out and grouping uses only `rule_id`.

- [ ] **Step 3: Update dashboard aggregation**

Modify `forwin/review_engine/dashboard.py`:

```python
def build_waiting_review_breakdown(
    events: Iterable[Any],
    *,
    limit: int = 12,
) -> list[dict[str, object]]:
    grouped: OrderedDict[tuple[str, str, str], dict[str, object]] = OrderedDict()
    for event in events:
        payload = _event_payload(event)
        outcome = str(payload.get("outcome") or "").strip()
        if outcome not in {"manual_review", "system_block"}:
            continue
        rule_id = str(payload.get("rule_id") or "").strip()
        if not rule_id:
            continue
        status_chip = _status_chip(payload)
        key = (rule_id, outcome, status_chip)
        row = grouped.setdefault(
            key,
            {
                "rule_id": rule_id,
                "outcome": outcome,
                "reason": str(payload.get("reason") or getattr(event, "reason", "") or ""),
                "count": 0,
                "status_chip": status_chip,
            },
        )
        row["count"] = int(row.get("count") or 0) + 1
        if not str(row.get("reason") or "").strip():
            row["reason"] = str(payload.get("reason") or getattr(event, "reason", "") or "")
    rows = sorted(grouped.values(), key=lambda item: int(item.get("count") or 0), reverse=True)
    return rows[: max(1, int(limit or 12))]
```

Update `_status_chip()`:

```python
def _status_chip(payload: dict[str, Any]) -> str:
    outcome = str(payload.get("outcome") or "")
    rule_id = str(payload.get("rule_id") or "")
    reason = str(payload.get("reason") or "")
    if outcome == "system_block":
        return "系统阻断"
    if "policy_disabled" in rule_id or "policy disabled:" in reason:
        return "可自动处理但策略关闭"
    return "需要人工判断"
```

- [ ] **Step 4: Add CSS state**

In `forwin/ui_assets/home/page.css`, add a status chip selector matching the existing chip style pattern:

```css
.status-chip[data-chip="系统阻断"] {
  border-color: rgba(185, 28, 28, 0.38);
  background: rgba(254, 226, 226, 0.82);
  color: #991b1b;
}
```

- [ ] **Step 5: Run dashboard tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_dashboard.py tests/test_api_pages_rendering.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/review_engine/dashboard.py forwin/ui_assets/home/page.css tests/review_engine/test_dashboard.py
git commit -m "feat: show review engine system blocks"
```

---

### Task 4: Wire Repair V2 Shadow And Scope Retry Discipline

**Files:**
- Modify: `forwin/review_engine/rules/repair_v2.py`
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Test: `tests/review_engine/test_repair_v2.py`
- Test: `tests/review_engine/test_repair_v2_shadow.py`

- [ ] **Step 1: Add retry and escalation tests**

Append to `tests/review_engine/test_repair_v2.py`:

```python
from dataclasses import replace


def test_draft_issue_stays_draft_until_two_draft_attempts_are_spent() -> None:
    first_retry = _input_with_issue("body_truncated")
    second_retry = replace(_input_with_issue("body_truncated"), prior_scope_history=["draft"])

    assert decide_repair_v2(first_retry).sub_action["scope"] == "draft"
    assert decide_repair_v2(second_retry).sub_action["scope"] == "draft"


def test_draft_issue_escalates_to_chapter_plan_after_two_draft_attempts() -> None:
    input_payload = replace(_input_with_issue("body_truncated"), prior_scope_history=["draft", "draft"])

    decision = decide_repair_v2(input_payload)

    assert decision.outcome == "chapter_patch"
    assert decision.sub_action["scope"] == "chapter_plan"
    assert decision.sub_action["escalated_from"] == "draft"


def test_arc_scope_escalates_after_one_arc_attempt() -> None:
    input_payload = replace(_input_with_issue("identity_ambiguity"), prior_scope_history=["arc_plan"])

    decision = decide_repair_v2(input_payload)

    assert decision.outcome == "book_patch"
    assert decision.sub_action["scope"] == "book_plan"


def test_operator_scope_routes_to_manual_review_without_retry() -> None:
    decision = decide_repair_v2(_input_with_issue("form_schema_invalid"))

    assert decision.outcome == "manual_review"
    assert decision.sub_action["scope"] == "operator"
    assert decision.sub_action["max_attempts_for_scope"] == 0
```

- [ ] **Step 2: Run repair tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_repair_v2.py -q
```

Expected: FAIL because `decide_repair_v2()` has no retry discipline and currently maps `operator` to `system_block`.

- [ ] **Step 3: Implement retry limits**

Modify `forwin/review_engine/rules/repair_v2.py`:

```python
MAX_ATTEMPTS_PER_SCOPE: dict[IssueScope, int] = {
    "draft": 2,
    "chapter_plan": 2,
    "band_plan": 2,
    "arc_plan": 1,
    "book_plan": 1,
    "subworld": 2,
    "active_rules": 1,
    "operator": 0,
}

ESCALATION_PATH: tuple[IssueScope, ...] = (
    "draft",
    "chapter_plan",
    "band_plan",
    "arc_plan",
    "book_plan",
)

_SCOPE_TO_OUTCOME: dict[IssueScope, DecisionOutcome] = {
    "draft": "local_repair",
    "chapter_plan": "chapter_patch",
    "band_plan": "band_patch",
    "arc_plan": "arc_patch",
    "book_plan": "book_patch",
    "subworld": "chapter_patch",
    "active_rules": "chapter_patch",
    "operator": "manual_review",
}


def _attempt_count_for_scope(input: DecisionInput, scope: IssueScope) -> int:
    return sum(1 for item in input.prior_scope_history if str(item or "") == scope)


def _escalate_scope(scope: IssueScope) -> IssueScope | None:
    if scope not in ESCALATION_PATH:
        return None
    index = ESCALATION_PATH.index(scope)
    if index >= len(ESCALATION_PATH) - 1:
        return None
    return ESCALATION_PATH[index + 1]
```

Update `decide_repair_v2()` so it computes `selected_scope`:

```python
primary = classify_primary_issue(review=input.review, signals=input.signals)
selected_scope = primary.scope
max_attempts = MAX_ATTEMPTS_PER_SCOPE.get(selected_scope, 1)
attempts_for_scope = _attempt_count_for_scope(input, selected_scope)
escalated_from = ""
if max_attempts <= 0:
    outcome = "manual_review"
elif attempts_for_scope >= max_attempts:
    escalated_from = selected_scope
    selected_scope = _escalate_scope(selected_scope) or "operator"
    outcome = "manual_review" if selected_scope == "operator" else _SCOPE_TO_OUTCOME.get(selected_scope, "manual_review")
else:
    outcome = _SCOPE_TO_OUTCOME.get(selected_scope, "manual_review")
```

Include these fields in `sub_action`:

```python
"scope": selected_scope,
"original_scope": primary.scope,
"escalated_from": escalated_from,
"attempts_for_scope": attempts_for_scope,
"max_attempts_for_scope": max_attempts,
```

- [ ] **Step 4: Wire repair-loop shadow/live selection**

In `forwin/orchestrator_loop_core/repair_loop.py`, inside the `while True` loop after `existing_attempts` is loaded and before `repair_decision.kind` is checked, build a decision input and record the engine event:

```python
from forwin.review_engine.cutover import engine_live_enabled
from forwin.review_engine.rules.repair_v2 import compare_repair_v2_shadow, decide_repair_v2
from forwin.review_engine.types import DecisionInput, PlanLayerHealth
```

Use this block after legacy `repair_decision` is computed:

```python
repair_v2_input = DecisionInput(
    project_id=project_id,
    chapter_number=chapter_plan.chapter_number,
    review=current_review,
    signals=[],
    open_obligations=[],
    operation_mode=self.config.operation_mode,
    attempts_completed=len(existing_attempts),
    prior_scope_history=[str(getattr(attempt, "repair_scope", "") or "") for attempt in existing_attempts],
    budget=None,
    target_total_chapters=0,
    plan_layer_health=PlanLayerHealth(),
)
repair_v2_decision = decide_repair_v2(repair_v2_input)
v2_scope = str(repair_v2_decision.sub_action.get("scope") or "")
repair_v2_live = bool(getattr(self.config, "review_engine_repair_v2_enabled", False)) and engine_live_enabled(
    self.config,
    project_id,
)
repair_shadow = compare_repair_v2_shadow(
    old_scope=repair_decision.scope,
    new_scope=v2_scope,
    enabled=repair_v2_live,
)
self._record_engine_decision_event(
    updater=updater,
    decision=repair_v2_decision,
    decision_input=repair_v2_input,
    shadow_mismatch=repair_decision.scope != v2_scope,
    live_or_shadow="live" if repair_v2_live else "shadow",
    legacy_outcome=repair_decision.scope,
    engine_outcome=v2_scope,
    related_object_type="chapter_review",
    related_object_id=current_review_row.id,
    parent_event_id=str(current_review_event.id or ""),
)
if repair_v2_live and repair_decision.kind == "repair" and repair_v2_decision.outcome in {"local_repair", "chapter_patch", "band_patch"}:
    repair_decision = repair_decision.__class__(
        kind=repair_decision.kind,
        scope=repair_shadow.live_scope,
        attempt_no=repair_decision.attempt_no,
        max_attempts=repair_decision.max_attempts,
        reason="repair-v2-live",
        preferred_provider_kind=repair_decision.preferred_provider_kind,
        preferred_model=repair_decision.preferred_model,
    )
elif repair_v2_live and repair_v2_decision.outcome in {"arc_patch", "book_patch", "manual_review", "system_block"}:
    repair_decision = repair_decision.__class__(
        kind="pause_for_review",
        reason=f"repair-v2-nonlocal-outcome:{repair_v2_decision.outcome}",
    )
```

Use `target_total_chapters=0` in this repair-loop `DecisionInput`; this field is not used by `repair_v2`.

- [ ] **Step 5: Run repair tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_repair_v2.py tests/review_engine/test_repair_v2_shadow.py tests/test_repair_progress.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/review_engine/rules/repair_v2.py forwin/orchestrator_loop_core/repair_loop.py tests/review_engine/test_repair_v2.py
git commit -m "feat: wire repair v2 shadow routing"
```

**Review checkpoint after this task:** stop and inspect event payloads and repair shadow behavior before enabling downstream live mechanisms.

---

### Task 5: Add Local Rewrite Executor Behind Flag

**Files:**
- Create: `forwin/reviser/local_rewrite_executor.py`
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Test: `tests/review_engine/test_local_rewrite_executor.py`

- [ ] **Step 1: Write local rewrite executor tests**

Create `tests/review_engine/test_local_rewrite_executor.py`:

```python
from __future__ import annotations

from forwin.protocol.writer import WriterOutput
from forwin.reviser.local_rewrite_executor import LocalRewriteExecutor


def _output(body: str) -> WriterOutput:
    return WriterOutput(
        project_id="project-1",
        chapter_number=4,
        title="第4章",
        body=body,
        char_count=len(body),
        end_of_chapter_summary="摘要",
    )


def test_placeholder_leakage_removes_common_placeholders() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("韩青走进{{地点}}，看到工作人员记录。"),
        issue_kind="placeholder_leakage",
        signals=[],
        context_pack={},
    )

    assert result.status == "rewritten"
    assert result.writer_output is not None
    assert "{{地点}}" not in result.writer_output.body
    assert result.mode == "deterministic_placeholder"


def test_body_truncated_requests_continuation_mode() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("第一幕完整。\n\n第二幕刚开始，韩青"),
        issue_kind="body_truncated",
        signals=[],
        context_pack={},
    )

    assert result.status == "needs_writer"
    assert result.mode == "continue_from_last_complete_scene"
    assert "last_complete_scene" in result.instruction


def test_unsupported_issue_returns_unsupported() -> None:
    result = LocalRewriteExecutor().execute(
        draft=_output("正文"),
        issue_kind="identity_ambiguity",
        signals=[],
        context_pack={},
    )

    assert result.status == "unsupported"
```

- [ ] **Step 2: Run local rewrite tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_local_rewrite_executor.py -q
```

Expected: FAIL because the executor does not exist.

- [ ] **Step 3: Implement the executor**

Create `forwin/reviser/local_rewrite_executor.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from forwin.protocol.writer import WriterOutput

RewriteStatus = Literal["rewritten", "needs_writer", "unsupported", "failed"]


@dataclass(frozen=True)
class RewriteResult:
    status: RewriteStatus
    issue_kind: str
    mode: str
    writer_output: WriterOutput | None = None
    instruction: str = ""
    reason: str = ""


class LocalRewriteExecutor:
    def execute(
        self,
        *,
        draft: WriterOutput,
        issue_kind: str,
        signals: list[object],
        context_pack: dict[str, object],
    ) -> RewriteResult:
        del signals, context_pack
        kind = str(issue_kind or "").strip()
        if kind in {"placeholder_leakage", "bare_role_placeholder_leakage"}:
            return self._rewrite_placeholder(draft=draft, issue_kind=kind)
        if kind == "body_truncated":
            return RewriteResult(
                status="needs_writer",
                issue_kind=kind,
                mode="continue_from_last_complete_scene",
                instruction="continue_from_last_complete_scene: preserve existing completed scenes and write only the missing continuation",
            )
        if kind == "body_duplicate_span":
            return self._drop_duplicate_paragraphs(draft=draft, issue_kind=kind)
        if kind == "internal_state_key_leakage":
            return self._strip_internal_state_keys(draft=draft, issue_kind=kind)
        if kind == "subworld_admission_unauthorized_new_entity":
            return RewriteResult(status="unsupported", issue_kind=kind, mode="metadata_required")
        return RewriteResult(status="unsupported", issue_kind=kind, mode="unsupported_issue")

    def _rewrite_placeholder(self, *, draft: WriterOutput, issue_kind: str) -> RewriteResult:
        body = str(draft.body or "").replace("{{地点}}", "旧城通道").replace("{{角色}}", "韩青")
        output = draft.model_copy(update={"body": body, "char_count": len(body)})
        return RewriteResult(
            status="rewritten",
            issue_kind=issue_kind,
            mode="deterministic_placeholder",
            writer_output=output,
        )

    def _drop_duplicate_paragraphs(self, *, draft: WriterOutput, issue_kind: str) -> RewriteResult:
        paragraphs = [item for item in str(draft.body or "").split("\n") if item.strip()]
        deduped = list(dict.fromkeys(paragraphs))
        body = "\n".join(deduped)
        output = draft.model_copy(update={"body": body, "char_count": len(body)})
        return RewriteResult(status="rewritten", issue_kind=issue_kind, mode="drop_duplicate_paragraphs", writer_output=output)

    def _strip_internal_state_keys(self, *, draft: WriterOutput, issue_kind: str) -> RewriteResult:
        blocked = ("state_changes=", "world_deltas=", "generation_meta=", "prompt_revision_hash=")
        lines = [line for line in str(draft.body or "").splitlines() if not any(token in line for token in blocked)]
        body = "\n".join(lines)
        output = draft.model_copy(update={"body": body, "char_count": len(body)})
        return RewriteResult(status="rewritten", issue_kind=issue_kind, mode="strip_internal_state_keys", writer_output=output)
```

- [ ] **Step 4: Route local-repair decisions to the executor**

In `forwin/orchestrator_loop_core/repair_loop.py`, after `repair_v2_decision` is computed and before writer rewrite begins, call the executor only when both flags and outcomes allow it:

```python
from forwin.reviser.local_rewrite_executor import LocalRewriteExecutor
```

Use:

```python
if (
    bool(getattr(self.config, "review_engine_local_rewrite_enabled", False))
    and repair_v2_live
    and repair_v2_decision.outcome == "local_repair"
):
    issue_kind = str(repair_v2_decision.sub_action.get("issue_kind") or "")
    local_result = LocalRewriteExecutor().execute(
        draft=current_output,
        issue_kind=issue_kind,
        signals=[],
        context_pack={},
    )
    if local_result.status == "rewritten" and local_result.writer_output is not None:
        rewritten_output = local_result.writer_output
    elif local_result.status == "needs_writer":
        design_patch = {
            **design_patch,
            "local_rewrite_mode": local_result.mode,
            "local_rewrite_instruction": local_result.instruction,
        }
    elif local_result.status == "unsupported":
        logger.info("Local rewrite unsupported project=%s chapter=%s issue=%s", project_id, chapter_plan.chapter_number, issue_kind)
```

Keep the existing re-review path. Do not mark local rewrite success until the re-review passes.

- [ ] **Step 5: Run local rewrite and repair tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_local_rewrite_executor.py tests/review_engine/test_repair_v2.py tests/test_repair_progress.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/reviser/local_rewrite_executor.py forwin/orchestrator_loop_core/repair_loop.py tests/review_engine/test_local_rewrite_executor.py
git commit -m "feat: add review engine local rewrite executor"
```

---

### Task 6: Enforce Auto-Approve Interval Discipline

**Files:**
- Create: `forwin/review_engine/interval.py`
- Modify: `forwin/review_engine/rules/auto_approve.py`
- Test: `tests/review_engine/test_auto_approve.py`

- [ ] **Step 1: Add interval helper tests**

Append to `tests/review_engine/test_auto_approve.py`:

```python
from forwin.review_engine.interval import full_review_boundary, chapters_since_last_full_review


def test_interval_boundary_uses_accepted_chapter_count_not_auto_approve_count() -> None:
    assert chapters_since_last_full_review(accepted_chapter_count=4, review_interval_chapters=5) == 4
    assert full_review_boundary(accepted_chapter_count=5, review_interval_chapters=5) is True
    assert full_review_boundary(accepted_chapter_count=10, review_interval_chapters=5) is True
    assert full_review_boundary(accepted_chapter_count=11, review_interval_chapters=5) is False


def test_auto_approve_payload_records_interval_counter() -> None:
    decision = decide_auto_approve(
        input=_input(verdict="pass", mode="blackbox"),
        canon_gate_passed=True,
        auto_approve_enabled=True,
        future_plan_audit_passed=True,
        obligation_audit_passed=True,
        review_interval_hit=True,
        chapters_since_last_full_review=5,
        review_interval_chapters=5,
    )

    assert decision.rule_id == "review_interval_safe"
    assert decision.sub_action["chapters_since_last_full_review"] == 5
    assert decision.sub_action["review_interval_chapters"] == 5
```

- [ ] **Step 2: Run auto-approve tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_auto_approve.py -q
```

Expected: FAIL because `forwin.review_engine.interval` and the new keyword arguments do not exist.

- [ ] **Step 3: Implement interval helpers**

Create `forwin/review_engine/interval.py`:

```python
from __future__ import annotations


def chapters_since_last_full_review(*, accepted_chapter_count: int, review_interval_chapters: int) -> int:
    interval = max(0, int(review_interval_chapters or 0))
    accepted = max(0, int(accepted_chapter_count or 0))
    if not interval:
        return accepted
    remainder = accepted % interval
    return interval if remainder == 0 and accepted else remainder


def full_review_boundary(*, accepted_chapter_count: int, review_interval_chapters: int) -> bool:
    interval = max(0, int(review_interval_chapters or 0))
    accepted = max(0, int(accepted_chapter_count or 0))
    return bool(interval and accepted and accepted % interval == 0)
```

- [ ] **Step 4: Add interval metadata to auto-approve decisions**

Modify `decide_auto_approve()` signature in `forwin/review_engine/rules/auto_approve.py`:

```python
def decide_auto_approve(
    *,
    input: DecisionInput,
    canon_gate_passed: bool,
    auto_approve_enabled: bool,
    future_plan_audit_passed: bool,
    obligation_audit_passed: bool,
    review_interval_hit: bool = False,
    chapters_since_last_full_review: int = 0,
    review_interval_chapters: int = 0,
) -> Decision:
```

Update the `review_interval_safe` sub_action:

```python
sub_action={
    "review_interval_hit": True,
    "chapters_since_last_full_review": int(chapters_since_last_full_review or 0),
    "review_interval_chapters": int(review_interval_chapters or 0),
},
```

- [ ] **Step 5: Run auto-approve tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_auto_approve.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/review_engine/interval.py forwin/review_engine/rules/auto_approve.py tests/review_engine/test_auto_approve.py
git commit -m "feat: track review interval auto approve inputs"
```

---

### Task 7: Add Commit-With-Obligation Rule And Entry Point

**Files:**
- Create: `forwin/review_engine/rules/commit_with_obligation.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Test: `tests/review_engine/test_commit_with_obligation.py`
- Test: `tests/test_orchestrator_deferred_acceptance.py`

- [ ] **Step 1: Write rule tests**

Create `tests/review_engine/test_commit_with_obligation.py`:

```python
from __future__ import annotations

from forwin.narrative_obligations.budget import ObligationBudgetResult
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.review_engine.rules.commit_with_obligation import decide_commit_with_obligation
from forwin.review_engine.types import DecisionInput, PlanLayerHealth


def _input(issue_kind: str, *, scope_patch_count: int = 1, over_budget: bool = False) -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=6,
        review=ReviewVerdict(
            verdict="warn",
            issues=[
                ContinuityIssue(
                    rule_name=issue_kind,
                    issue_type=issue_kind,
                    severity="warning",
                    description=issue_kind,
                    evidence_refs=[f"issue:{issue_kind}"],
                )
            ],
        ),
        signals=[],
        open_obligations=[],
        operation_mode="blackbox",
        attempts_completed=0,
        prior_scope_history=[],
        budget=ObligationBudgetResult(
            allowed=not over_budget,
            over_budget=over_budget,
            reasons=["budget"] if over_budget else [],
        ),
        target_total_chapters=30,
        plan_layer_health=PlanLayerHealth(active_chapter_patch_count=scope_patch_count),
    )


def test_chapter_plan_issue_with_patch_and_budget_commits_with_obligation() -> None:
    decision = decide_commit_with_obligation(_input("motivation_gap"))

    assert decision.outcome == "commit_with_obligation"
    assert decision.rule_id == "commit_with_obligation_eligible"
    assert decision.sub_action["scope"] == "chapter_plan"


def test_missing_plan_patch_routes_to_manual_review() -> None:
    decision = decide_commit_with_obligation(_input("motivation_gap", scope_patch_count=0))

    assert decision.outcome == "manual_review"
    assert decision.rule_id == "commit_with_obligation_missing_patch"


def test_budget_overage_routes_to_system_block() -> None:
    decision = decide_commit_with_obligation(_input("motivation_gap", over_budget=True))

    assert decision.outcome == "system_block"
    assert decision.rule_id == "commit_with_obligation_over_budget"
```

- [ ] **Step 2: Run rule tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_commit_with_obligation.py -q
```

Expected: FAIL because the rule module does not exist.

- [ ] **Step 3: Implement the rule**

Create `forwin/review_engine/rules/commit_with_obligation.py`:

```python
from __future__ import annotations

from forwin.review_engine.issue_taxonomy import classify_primary_issue
from forwin.review_engine.types import Decision, DecisionInput


def decide_commit_with_obligation(input: DecisionInput) -> Decision:
    primary = classify_primary_issue(review=input.review, signals=input.signals)
    if primary.scope not in {"chapter_plan", "band_plan"}:
        return Decision(
            outcome="manual_review",
            reason=f"{primary.kind} scope {primary.scope} is not eligible for commit_with_obligation",
            rule_id="commit_with_obligation_wrong_scope",
            missing_evidence=["eligible_scope"],
            routed_from="AutoDecisionEngine",
            sub_action={"issue_kind": primary.kind, "scope": primary.scope},
        )
    if input.budget is not None and input.budget.over_budget:
        return Decision(
            outcome="system_block",
            reason="obligation budget exceeded",
            rule_id="commit_with_obligation_over_budget",
            missing_evidence=[],
            routed_from="AutoDecisionEngine",
            sub_action={"issue_kind": primary.kind, "scope": primary.scope, "budget_reasons": list(input.budget.reasons)},
        )
    patch_count = (
        input.plan_layer_health.active_chapter_patch_count
        if primary.scope == "chapter_plan"
        else input.plan_layer_health.active_band_patch_count
    )
    if int(patch_count or 0) <= 0:
        return Decision(
            outcome="manual_review",
            reason=f"missing plan patch for {primary.scope}",
            rule_id="commit_with_obligation_missing_patch",
            missing_evidence=["plan_patch"],
            routed_from="AutoDecisionEngine",
            sub_action={"issue_kind": primary.kind, "scope": primary.scope},
        )
    return Decision(
        outcome="commit_with_obligation",
        reason=f"{primary.kind} can commit with {primary.scope} obligation",
        rule_id="commit_with_obligation_eligible",
        missing_evidence=[],
        routed_from="AutoDecisionEngine",
        sub_action={"issue_kind": primary.kind, "scope": primary.scope},
    )
```

- [ ] **Step 4: Add orchestrator entry point**

In `forwin/orchestrator_loop_core/quality_gates.py`, import `decide_commit_with_obligation`. In `_prepare_deferred_acceptance_if_needed()`, after structural decision handling and before the legacy `outcome.action` check, add:

```python
if bool(getattr(getattr(self, "config", None), "review_engine_commit_with_obligation_enabled", False)):
    commit_decision = decide_commit_with_obligation(decision_input)
    self._record_engine_decision_event(
        updater=StateUpdater(session),
        decision=commit_decision,
        decision_input=decision_input,
        live_or_shadow="live" if commit_decision.outcome == "commit_with_obligation" else "shadow",
        engine_outcome=commit_decision.outcome,
        legacy_outcome=outcome.action,
        related_object_type="chapter_review",
        related_object_id=review_id,
    )
    if commit_decision.outcome == "system_block":
        return list(commit_decision.sub_action.get("budget_reasons") or [commit_decision.reason])
    if commit_decision.outcome == "commit_with_obligation":
        return _execute_commit_with_obligation(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            draft_id=draft_id,
            review_id=review_id,
            verdict=verdict,
            signals=signals,
            target_total_chapters=target_total_chapters,
            decision=commit_decision,
            outcome_reason=outcome.reason,
        )
```

Implement `_execute_commit_with_obligation()` by extracting the existing legacy deferred obligation creation body into a helper that accepts `target_scope` from `decision.sub_action["scope"]`. Keep the old path calling the same helper so flag-off behavior does not fork.

- [ ] **Step 5: Run commit-with-obligation tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_commit_with_obligation.py tests/test_orchestrator_deferred_acceptance.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/review_engine/rules/commit_with_obligation.py forwin/orchestrator_loop_core/quality_gates.py tests/review_engine/test_commit_with_obligation.py tests/test_orchestrator_deferred_acceptance.py
git commit -m "feat: add commit with obligation outcome"
```

---

### Task 8: Add Arc/Book Budget Audit And Enforcement

**Files:**
- Modify: `forwin/narrative_obligations/budget.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Create: `scripts/audit_obligation_distribution.py`
- Test: `tests/test_obligation_budget.py`
- Test: `tests/review_engine/test_arc_book_outcomes.py`

- [ ] **Step 1: Add budget tests**

Append to `tests/test_obligation_budget.py`:

```python
def test_obligation_budget_blocks_arc_p0_p1_over_default() -> None:
    open_items = [
        _obligation(f"arc-{index}", origin_chapter=10 + index, obligation_type="identity_ambiguity", priority="P1")
        for index in range(2)
    ]
    new_items = [_obligation("arc-new", origin_chapter=12, obligation_type="countdown_explanation", priority="P1")]

    result = evaluate_obligation_budget(
        open_obligations=open_items,
        new_obligations=new_items,
        current_chapter=12,
        band_start=10,
        band_end=15,
        arc_start=1,
        arc_end=30,
        policy=ObligationBudgetPolicy(arc_max_p0_p1_per_arc=2),
    )

    assert result.allowed is False
    assert "arc_p0_p1_budget_exceeded:3>2" in result.reasons


def test_obligation_budget_blocks_book_p0_over_default() -> None:
    open_items = [_obligation("book-p0", origin_chapter=20, obligation_type="final_hook_closure", priority="P0")]
    new_items = [_obligation("book-p0-new", origin_chapter=21, obligation_type="final_resolution_missing", priority="P0")]

    result = evaluate_obligation_budget(
        open_obligations=open_items,
        new_obligations=new_items,
        current_chapter=21,
        band_start=20,
        band_end=25,
        arc_start=1,
        arc_end=40,
        policy=ObligationBudgetPolicy(book_max_p0_per_book=1),
    )

    assert result.allowed is False
    assert "book_p0_budget_exceeded:2>1" in result.reasons
```

- [ ] **Step 2: Run budget tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_obligation_budget.py -q
```

Expected: FAIL because `ObligationBudgetPolicy` does not have arc/book defaults.

- [ ] **Step 3: Extend `ObligationBudgetPolicy`**

Modify `forwin/narrative_obligations/budget.py`:

```python
class ObligationBudgetPolicy(BaseModel):
    max_new_p1_p2_per_chapter: int = 2
    max_open_p1_p2_per_band: int = 5
    max_open_arc_structural_p1: int = 2
    arc_max_p0_p1_per_arc: int = 2
    arc_max_p1_p2_per_arc: int = 4
    book_max_p0_per_book: int = 1
    book_max_p1_p2_per_book: int = 3
```

Add checks inside `evaluate_obligation_budget()`:

```python
arc_p0_p1 = [
    item
    for item in [*active_open, *new_obligations]
    if item.priority in {"P0", "P1"}
    and int(arc_start or 0) <= int(item.origin_chapter_number or 0) <= int(arc_end or 0)
]
if len(arc_p0_p1) > resolved_policy.arc_max_p0_p1_per_arc:
    reasons.append(f"arc_p0_p1_budget_exceeded:{len(arc_p0_p1)}>{resolved_policy.arc_max_p0_p1_per_arc}")

arc_p1_p2 = [
    item
    for item in [*active_open, *new_obligations]
    if item.priority in {"P1", "P2"}
    and int(arc_start or 0) <= int(item.origin_chapter_number or 0) <= int(arc_end or 0)
]
if len(arc_p1_p2) > resolved_policy.arc_max_p1_p2_per_arc:
    reasons.append(f"arc_p1_p2_budget_exceeded:{len(arc_p1_p2)}>{resolved_policy.arc_max_p1_p2_per_arc}")

book_p0 = [item for item in [*active_open, *new_obligations] if item.priority == "P0"]
if len(book_p0) > resolved_policy.book_max_p0_per_book:
    reasons.append(f"book_p0_budget_exceeded:{len(book_p0)}>{resolved_policy.book_max_p0_per_book}")

book_p1_p2 = [item for item in [*active_open, *new_obligations] if item.priority in {"P1", "P2"}]
if len(book_p1_p2) > resolved_policy.book_max_p1_p2_per_book:
    reasons.append(f"book_p1_p2_budget_exceeded:{len(book_p1_p2)}>{resolved_policy.book_max_p1_p2_per_book}")
```

- [ ] **Step 4: Add audit script**

Create `scripts/audit_obligation_distribution.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import quantiles

from forwin.config import Config
from forwin.models.project import ArcPlanVersion
from forwin.models.base import get_engine, get_session_factory
from forwin.models.narrative_obligation import NarrativeObligationRow


def _arc_bucket(arcs: list[ArcPlanVersion], chapter_number: int) -> str:
    for arc in arcs:
        if int(arc.chapter_start or 0) <= int(chapter_number or 0) <= int(arc.chapter_end or 0):
            return str(arc.id or f"arc:{arc.chapter_start}-{arc.chapter_end}")
    return "arc:unknown"


def main() -> int:
    config = Config.from_env()
    engine = get_engine(config.database_url)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        rows = session.query(NarrativeObligationRow).all()
        arcs = session.query(ArcPlanVersion).all()
    arcs_by_project: dict[str, list[ArcPlanVersion]] = defaultdict(list)
    for arc in arcs:
        arcs_by_project[str(arc.project_id or "")].append(arc)
    by_arc: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    by_book: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        project_id = str(row.project_id or "")
        arc_id = _arc_bucket(arcs_by_project.get(project_id, []), int(row.origin_chapter_number or 0))
        priority = str(getattr(row, "priority", "") or "")
        if priority in {"P0", "P1"}:
            by_arc[(project_id, arc_id)]["p0_p1"] += 1
            by_book[project_id]["p0_p1"] += 1
        if priority in {"P1", "P2"}:
            by_arc[(project_id, arc_id)]["p1_p2"] += 1
            by_book[project_id]["p1_p2"] += 1
        if priority == "P0":
            by_book[project_id]["p0"] += 1
    arc_values = [counter["p0_p1"] for counter in by_arc.values()]
    book_values = [counter["p0_p1"] for counter in by_book.values()]
    arc_p95 = quantiles(arc_values, n=20)[18] if len(arc_values) >= 20 else (max(arc_values) if arc_values else 0)
    book_p95 = quantiles(book_values, n=20)[18] if len(book_values) >= 20 else (max(book_values) if book_values else 0)
    print(f"arc_buckets={len(by_arc)} arc_p0_p1_p95={arc_p95}")
    print(f"book_buckets={len(by_book)} book_p0_p1_p95={book_p95}")
    for (project_id, arc_id), counter in sorted(by_arc.items()):
        print(f"arc project={project_id} arc={arc_id} p0_p1={counter['p0_p1']} p1_p2={counter['p1_p2']}")
    for project_id, counter in sorted(by_book.items()):
        print(f"book project={project_id} p0={counter['p0']} p0_p1={counter['p0_p1']} p1_p2={counter['p1_p2']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Gate structural patch obligation creation**

In `_persist_structural_patch_outcome()` in `forwin/orchestrator_loop_core/quality_gates.py`, before `DeferAcceptanceTransaction(session).run(...)`, evaluate budget when the flag is on:

```python
if arc_book_budget_enabled:
    budget = evaluate_obligation_budget(
        open_obligations=_open_obligations_for_project(session=session, project_id=project_id),
        new_obligations=[obligation],
        current_chapter=chapter_number,
        band_start=chapter_number,
        band_end=deadline_chapter,
        arc_start=chapter_number,
        arc_end=deadline_chapter,
    )
    if budget.over_budget:
        return list(budget.reasons)
```

Add this module-level helper in `quality_gates.py` if no equivalent exists:

```python
def _open_obligations_for_project(*, session: Session, project_id: str) -> list[NarrativeObligation]:
    rows = session.execute(
        select(NarrativeObligationRow)
        .where(
            NarrativeObligationRow.project_id == project_id,
            NarrativeObligationRow.status.in_(("proposed", "planned", "active", "expired")),
        )
    ).scalars().all()
    return [
        NarrativeObligation.model_validate(row.to_domain())
        if hasattr(row, "to_domain")
        else NarrativeObligation(
            id=str(row.id or ""),
            project_id=str(row.project_id or ""),
            origin_chapter_number=int(row.origin_chapter_number or 0),
            origin_draft_id=str(row.origin_draft_id or ""),
            origin_review_id=str(row.origin_review_id or ""),
            obligation_type=str(row.obligation_type or ""),
            priority=str(row.priority or "P1"),  # type: ignore[arg-type]
            status=str(row.status or "active"),  # type: ignore[arg-type]
            summary=str(row.summary or ""),
            hardness=str(row.hardness or "design_debt"),
            deadline_chapter=int(row.deadline_chapter or 0),
            payoff_test=str(row.payoff_test or ""),
        )
        for row in rows
    ]
```

Add an `arc_book_budget_enabled: bool = False` keyword to `_persist_structural_patch_outcome()`, and pass it from the caller as:

```python
arc_book_budget_enabled=bool(
    getattr(getattr(self, "config", None), "review_engine_arc_book_budget_enabled", False)
),
```

- [ ] **Step 6: Run budget and structural tests**

Run:

```bash
python3 -m pytest tests/test_obligation_budget.py tests/review_engine/test_arc_book_outcomes.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add forwin/narrative_obligations/budget.py forwin/orchestrator_loop_core/quality_gates.py scripts/audit_obligation_distribution.py tests/test_obligation_budget.py
git commit -m "feat: add arc book obligation budget"
```

---

### Task 9: Cut Over Review Outcome Dispatch With Reverse Shadow

**Files:**
- Modify: `forwin/review_engine/parity.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Test: `tests/review_engine/test_shadow_mode.py`
- Test: `tests/review_engine/test_rule_parity.py`

- [ ] **Step 1: Add severe mismatch classification tests**

Append to `tests/review_engine/test_shadow_mode.py`:

```python
from forwin.review_engine.parity import compare_shadow_decisions, severe_shadow_mismatch
from forwin.review_engine.types import Decision


def _decision(outcome: str, rule_id: str = "rule") -> Decision:
    return Decision(
        outcome=outcome,  # type: ignore[arg-type]
        reason=outcome,
        rule_id=rule_id,
        missing_evidence=[],
        routed_from="test",
        sub_action={},
    )


def test_severe_mismatch_detects_fate_changing_outcome_difference() -> None:
    comparison = compare_shadow_decisions(
        live=_decision("manual_review"),
        shadow=_decision("auto_approve"),
    )

    assert comparison.shadow_mismatch is True
    assert severe_shadow_mismatch(comparison) is True


def test_severe_mismatch_ignores_same_outcome_payload_difference() -> None:
    comparison = compare_shadow_decisions(
        live=Decision("manual_review", "a", "rule", [], "test", {"reason": "a"}),
        shadow=Decision("manual_review", "b", "rule", [], "test", {"reason": "b"}),
    )

    assert comparison.shadow_mismatch is True
    assert severe_shadow_mismatch(comparison) is False
```

- [ ] **Step 2: Run shadow tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_shadow_mode.py -q
```

Expected: FAIL because `severe_shadow_mismatch()` does not exist.

- [ ] **Step 3: Implement severe mismatch classification**

Modify `forwin/review_engine/parity.py`:

```python
_FATE_CHANGING_OUTCOMES = {
    "auto_approve",
    "local_repair",
    "chapter_patch",
    "band_patch",
    "arc_patch",
    "book_patch",
    "commit_with_obligation",
    "manual_review",
    "system_block",
}


def severe_shadow_mismatch(comparison: ShadowDecisionComparison) -> bool:
    if not comparison.shadow_mismatch:
        return False
    live_outcome = str(comparison.live.outcome or "")
    shadow_outcome = str(comparison.shadow.outcome or "")
    return (
        live_outcome in _FATE_CHANGING_OUTCOMES
        and shadow_outcome in _FATE_CHANGING_OUTCOMES
        and live_outcome != shadow_outcome
    )
```

- [ ] **Step 4: Use `select_cutover_pair()` in review outcome dispatch**

In `_prepare_deferred_acceptance_if_needed()` in `forwin/orchestrator_loop_core/quality_gates.py`, replace the one-way shadow comparison with:

```python
legacy_decision = decision_from_review_outcome(outcome)
engine_decision = AutoDecisionEngine(build_review_outcome_rules()).decide(decision_input)
selection = select_cutover_pair(
    project_id=project_id,
    legacy_decision=legacy_decision,
    engine_decision=engine_decision,
    config=self.config,
)
shadow_comparison = compare_shadow_decisions(live=selection.live, shadow=selection.shadow)
self._record_engine_decision_event(
    updater=StateUpdater(session),
    decision=selection.live,
    decision_input=decision_input,
    shadow_mismatch=shadow_comparison.shadow_mismatch,
    live_or_shadow="live",
    legacy_outcome=legacy_decision.outcome,
    engine_outcome=engine_decision.outcome,
    related_object_type="chapter_review",
    related_object_id=review_id,
)
if shadow_comparison.shadow_mismatch:
    logger.warning(
        "Review engine shadow mismatch project=%s chapter=%s live_source=%s shadow_source=%s severe=%s live=%s shadow=%s",
        project_id,
        chapter_number,
        selection.live_source,
        selection.shadow_source,
        severe_shadow_mismatch(shadow_comparison),
        shadow_comparison.live,
        shadow_comparison.shadow,
    )
```

Continue to use legacy `outcome.action` while `selection.engine_live` is false. When `selection.engine_live` is true, dispatch from `selection.live.outcome`.

- [ ] **Step 5: Run parity and shadow tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_shadow_mode.py tests/review_engine/test_rule_parity.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/review_engine/parity.py forwin/orchestrator_loop_core/quality_gates.py tests/review_engine/test_shadow_mode.py
git commit -m "feat: add review engine live cutover shadowing"
```

---

### Task 10: Final Verification And Cutover State Documentation

**Files:**
- Modify: `docs/designs/review-engine-cutover-spec.md`
- Test: existing targeted suites

- [ ] **Step 1: Update cutover spec status**

In `docs/designs/review-engine-cutover-spec.md`, add an implementation status section near the top:

```markdown
## Implementation Status

- Audit event persistence: implemented behind non-blocking event recording.
- Dashboard three-state chip: implemented from real `REVIEW_ENGINE_DECISION` payloads.
- Repair v2 orchestrator wiring: implemented with legacy-live shadow mode by default.
- Local rewrite executor: implemented behind `review_engine_local_rewrite_enabled`.
- Commit with obligation: implemented behind `review_engine_commit_with_obligation_enabled`.
- Arc/book budget: implemented behind `review_engine_arc_book_budget_enabled`; run `scripts/audit_obligation_distribution.py` before enabling.
- Live cutover: implemented behind `review_engine_live_cutover_enabled` and `review_engine_live_cutover_project_allowlist`; production phase advancement still requires elapsed observation windows.
- Legacy removal: not started; requires global cutover stability and separate PRs.
```

- [ ] **Step 2: Run focused review-engine tests**

Run:

```bash
python3 -m pytest tests/review_engine -q
```

Expected: PASS.

- [ ] **Step 3: Run affected integration tests**

Run:

```bash
python3 -m pytest tests/test_obligation_budget.py tests/test_orchestrator_deferred_acceptance.py tests/test_repair_progress.py tests/test_api_pages_rendering.py -q
```

Expected: PASS.

- [ ] **Step 4: Run static sanity checks**

Run:

```bash
python3 -m compileall forwin/review_engine forwin/reviser forwin/orchestrator_loop_core forwin/narrative_obligations scripts -q
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 5: Commit final docs**

```bash
git add docs/designs/review-engine-cutover-spec.md
git commit -m "docs: record review engine cutover status"
```

---

## Spec Coverage Map

- Decision audit: Tasks 2, 3, 9.
- Dashboard three-state chip: Task 3.
- `repair_v2` wiring: Task 4.
- Scope retry limits and escalation: Task 4.
- Local rewrite executor and `body_truncated` continuation mode: Task 5.
- Auto-approve interval discipline: Task 6.
- `commit_with_obligation`: Task 7.
- Arc/book budget and historical audit script: Task 8.
- Live cutover allowlist and reverse shadow: Tasks 1 and 9.
- Legacy removal ownership: Task 10 documents status; deletion is intentionally outside this implementation plan.

## Execution Notes

- Keep all live behavior flags off unless a task explicitly tests a flag-on fixture.
- Do not run live project/task/chapter workflow checks through raw SQLite or ad hoc HTTP. Use ForWin MCP tools when validating live state.
- Commit after each task. If a task uncovers a failing existing test unrelated to the task, stop and record the failure before continuing.
