# Remove Story-Specific Hardcoding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove current-book-specific names, places, and scenario beats from ForWin production code and migrate regression tests so they verify generic behavior rather than one test novel.

**Architecture:** Treat story-specific text as a code smell with an automated audit gate. Production code may contain generic Chinese domain terms like `系统`, `核心区`, `记忆重置`, and `档案`, but must not contain a concrete test-book identity such as `林澈`, `沈宴秋`, `洛庭若`, `顾临川`, `旧城遗档`, `白塔重置`, `白塔`, `岫苑`, `地下旧轨`, `潮汐钟楼`, `失忆广场`, or `档案公会`. Tests should use neutral reusable fixtures unless a test is explicitly for Genesis seed extraction.

**Tech Stack:** Python, pytest, grep-based source audit, existing ForWin canon-quality and generation tests.

---

## File Map

- Create: `tests/test_no_story_specific_hardcoding.py`
  - Owns the regression gate that prevents current-book tokens from re-entering production code and selected generic test suites.
- Create: `tests/fixtures/generic_story_terms.py`
  - Provides neutral Chinese names and locations for test samples.
- Modify: `forwin/book_genesis.py`
  - Replace old fallback world examples and phase labels with generic placeholders.
- Modify: `forwin/writer/prompts.py`
  - Replace prompt examples such as `白塔巡检员` and `白塔内` with generic system-neutral wording.
- Modify: `forwin/api_project_ops.py`
  - Keep extension beats generic; remove residual `档案公会`, `林氏`, and any current-book continuation-specific text.
- Modify: `forwin/canon_names.py`
  - Ensure mother-name observation patterns use generic protagonist-name matching instead of `林澈`.
- Modify: `forwin/canon_quality/countdown_ledger.py`
  - Keep countdown resolution and policy logic generic.
- Modify: `forwin/canon_quality/final_completion.py`
  - Keep final gate crisis and resolution keywords generic.
- Modify: `forwin/canon_quality/character_state.py`
  - Ensure candidate character extraction is generic and not tied to fixed names.
- Modify: `forwin/context/assembler.py`
  - Ensure recent-canon custody detection uses generic character extraction.
- Modify: `forwin/director/arc_director.py`
  - Ensure fallback protagonist is extracted from premise or set to `主角`, never a concrete name.
- Modify: `forwin/orchestrator/loop.py`
  - Ensure placeholder autofix emits generic stable aliases.
- Modify tests using current-book terms:
  - `tests/test_countdown_ledger.py`
  - `tests/test_canon_quality_service.py`
  - `tests/test_character_state_transition_ledger.py`
  - `tests/test_identity_role_ledger.py`
  - `tests/test_context_provider_chain.py`
  - `tests/test_project_operation_guards.py`
  - `tests/test_placeholder_leakage_gate.py`
  - `tests/test_governance_review_and_checkpoint.py`
  - Other files reported by the audit gate.

---

## Task 1: Add the Audit Gate

**Files:**
- Create: `tests/test_no_story_specific_hardcoding.py`
- Create: `tests/fixtures/generic_story_terms.py`

- [ ] **Step 1: Add neutral fixture constants**

Create `tests/fixtures/generic_story_terms.py`:

```python
from __future__ import annotations

PROTAGONIST = "陆明"
ALLY = "韩青"
ANTAGONIST = "周砚"
PARENT = "陆远"
CITY = "灰城"
SYSTEM = "核心系统"
CORE_AREA = "核心区"
ARCHIVE_ORG = "档案署"
UNDERGROUND_ROUTE = "地下检修线"
CLOCK_TOWER = "钟塔"
PUBLIC_SQUARE = "中央广场"
SYSTEM_PATROL = "系统巡检员"
FAMILY_LINE = "陆氏"
```

- [ ] **Step 2: Add production hardcoding audit test**

Create `tests/test_no_story_specific_hardcoding.py`:

```python
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PRODUCTION_PATHS = [
    REPO_ROOT / "forwin",
]

BANNED_CURRENT_BOOK_TERMS = (
    "d2338a0e8bfe4e00a068b03ce9e9b0bf",
    "旧城遗档",
    "白塔重置",
    "林澈",
    "沈宴秋",
    "洛庭若",
    "顾临川",
    "林远舟",
    "沈砚",
    "白塔",
    "岫苑",
    "地下旧轨",
    "潮汐钟楼",
    "失忆广场",
    "档案公会",
    "白塔巡检员",
    "林氏",
)

ALLOWED_PRODUCTION_FILES = {
    # Keep empty. If a future exception is genuinely needed, add a code comment
    # in the target file explaining why production code must name this exact story.
}


def _python_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in paths:
        files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


def test_production_code_has_no_current_book_hardcoding() -> None:
    violations: list[str] = []
    for path in _python_files(PRODUCTION_PATHS):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWED_PRODUCTION_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for term in BANNED_CURRENT_BOOK_TERMS:
            if term in text:
                violations.append(f"{relative}: contains {term!r}")
    assert violations == []
```

- [ ] **Step 3: Run audit test and confirm it fails before cleanup**

Run:

```bash
FORWIN_HTTP_BIND=127.0.0.1 .venv/bin/python -m pytest tests/test_no_story_specific_hardcoding.py -q
```

Expected before cleanup: FAIL listing production files such as `forwin/book_genesis.py`, `forwin/writer/prompts.py`, or any remaining file with current-book tokens.

---

## Task 2: Remove Current-Book Tokens From Production Code

**Files:**
- Modify: `forwin/book_genesis.py`
- Modify: `forwin/writer/prompts.py`
- Modify: `forwin/api_project_ops.py`
- Modify: `forwin/canon_names.py`
- Modify: `forwin/canon_quality/countdown_ledger.py`
- Modify: `forwin/canon_quality/final_completion.py`
- Modify: `forwin/canon_quality/character_state.py`
- Modify: `forwin/context/assembler.py`
- Modify: `forwin/director/arc_director.py`
- Modify: `forwin/orchestrator/loop.py`

- [ ] **Step 1: Replace Genesis fallback examples**

In `forwin/book_genesis.py`, replace concrete fallback examples:

```python
("白塔", "地下旧轨", "潮汐钟楼", "岫苑", "档案公会", "失忆广场")
```

with generic examples:

```python
("核心系统", "地下检修线", "钟塔", "档案署", "中央广场", "边缘城区")
```

Replace regex alternation that names current-book locations:

```python
(?:白塔|旧轨|钟楼|塔|楼|苑|公会|广场|旧城区|新区|港区|港口|旧港|记忆馆|档案馆|数据市场|市场|街区|码头|城区)
```

with:

```python
(?:核心系统|地下线|检修线|钟塔|塔|楼|档案署|署|广场|旧城区|新区|港区|港口|记忆馆|档案馆|数据市场|市场|街区|码头|城区|边缘区)
```

Replace phase label:

```python
"白塔逼近"
```

with:

```python
"系统逼近"
```

- [ ] **Step 2: Replace prompt examples**

In `forwin/writer/prompts.py`, replace:

```python
"白塔巡检员"
"被困在机房/地下/白塔内"
"白塔权限"
```

with:

```python
"系统巡检员"
"被困在机房/地下设施/系统核心内"
"系统权限"
```

- [ ] **Step 3: Keep extension beats generic**

In `forwin/api_project_ops.py`, replace residual concrete beats:

```python
"档案公会的清算"
"让档案公会旧账浮出水面，补足谁授权、谁执行、谁受益的因果证据链。"
"林氏守门人的真相"
"揭示林氏维护者身份的完整因果，关闭家族档案为何被拆散和抹除的主线缺口。"
```

with:

```python
"组织旧账的清算"
"让关键组织旧账浮出水面，补足谁授权、谁执行、谁受益的因果证据链。"
"家族守门人的真相"
"揭示家族维护者身份的完整因果，关闭家族档案为何被拆散和抹除的主线缺口。"
```

- [ ] **Step 4: Confirm generic character and state logic**

Ensure these files contain no banned term from `BANNED_CURRENT_BOOK_TERMS`:

```bash
git diff -- forwin/canon_names.py forwin/canon_quality/countdown_ledger.py forwin/canon_quality/final_completion.py forwin/canon_quality/character_state.py forwin/context/assembler.py forwin/director/arc_director.py forwin/orchestrator/loop.py | grep '^+' | grep -E '旧城遗档|白塔重置|林澈|沈宴秋|洛庭若|顾临川|林远舟|沈砚|白塔|岫苑|地下旧轨|潮汐钟楼|失忆广场|档案公会|白塔巡检员|林氏'
```

Expected: no output.

- [ ] **Step 5: Run production audit**

Run:

```bash
FORWIN_HTTP_BIND=127.0.0.1 .venv/bin/python -m pytest tests/test_no_story_specific_hardcoding.py -q
```

Expected: PASS.

---

## Task 3: Migrate New and High-Risk Tests to Neutral Fixtures

**Files:**
- Modify: `tests/test_countdown_ledger.py`
- Modify: `tests/test_canon_quality_service.py`
- Modify: `tests/test_character_state_transition_ledger.py`
- Modify: `tests/test_identity_role_ledger.py`
- Modify: `tests/test_context_provider_chain.py`
- Modify: `tests/test_project_operation_guards.py`
- Modify: `tests/test_placeholder_leakage_gate.py`
- Modify: `tests/test_governance_review_and_checkpoint.py`

- [ ] **Step 1: Convert the new countdown regression to neutral names**

In `tests/test_countdown_ledger.py`, keep the behavior:

```python
body="记忆重置倒计时闪烁着猩红的数字：08:12。主角继续奔跑。倒计时：07:03。"
```

Do not use `林澈`, `沈宴秋`, `白塔`, or `岫苑` in this test.

- [ ] **Step 2: Convert the service fallback regression to neutral names**

In `tests/test_canon_quality_service.py`, the fallback test should use:

```python
project = Project(
    title="终章倒计时门禁",
    premise="主角：陆明。主线倒计时必须单调减少。",
    genre="悬疑",
    target_total_chapters=36,
)
```

Use `陆明`, `韩青`, and `核心系统` in the body and summary.

- [ ] **Step 3: Convert high-risk character-state tests**

For tests that assert generic custody or terminal-state behavior, replace:

```python
林澈 -> 陆明
沈宴秋 -> 韩青
洛庭若 -> 周砚
白塔巡检员 -> 系统巡检员
档案公会 -> 档案署
地下旧轨 -> 地下检修线
```

Keep assertions tied to variables:

```python
PROTAGONIST = "陆明"
ALLY = "韩青"
ANTAGONIST = "周砚"
assert regression.subject_key == f"character:{ALLY}"
```

- [ ] **Step 4: Convert high-risk identity tests**

For identity tests, replace fixed names with local variables:

```python
name = "韩青"
body = f"{name}抬起头，她说自己愿意承担代价。"
central_characters={name}
```

Keep tests about object pronouns and gender drift exactly the same behaviorally.

- [ ] **Step 5: Convert prompt/context tests touched by this work**

In prompt/context tests, replace concrete examples:

```python
"林澈必须关闭白塔系统。"
```

with:

```python
"陆明必须关闭核心系统。"
```

- [ ] **Step 6: Run migrated test group**

Run:

```bash
FORWIN_HTTP_BIND=127.0.0.1 .venv/bin/python -m pytest tests/test_countdown_ledger.py tests/test_canon_quality_service.py tests/test_character_state_transition_ledger.py tests/test_identity_role_ledger.py tests/test_context_provider_chain.py tests/test_project_operation_guards.py tests/test_placeholder_leakage_gate.py tests/test_governance_review_and_checkpoint.py -q
```

Expected: all selected tests pass.

---

## Task 4: Add Test-Suite Guard for New Generic Regression Tests

**Files:**
- Modify: `tests/test_no_story_specific_hardcoding.py`

- [ ] **Step 1: Add selected-test audit**

Append to `tests/test_no_story_specific_hardcoding.py`:

```python
GENERIC_TEST_FILES = (
    "tests/test_countdown_ledger.py",
    "tests/test_canon_quality_service.py",
    "tests/test_character_state_transition_ledger.py",
    "tests/test_identity_role_ledger.py",
    "tests/test_context_provider_chain.py",
    "tests/test_project_operation_guards.py",
    "tests/test_placeholder_leakage_gate.py",
    "tests/test_governance_review_and_checkpoint.py",
)

TEST_TERMS_ALLOWED_FOR_LEGACY_FIXTURE_MIGRATION = {
    # Keep empty after Task 3. If a file is too large to migrate in one pass,
    # add a temporary exact file entry and remove it before final completion.
}


def test_generic_regression_tests_do_not_reuse_current_book_fixture() -> None:
    violations: list[str] = []
    for relative in GENERIC_TEST_FILES:
        if relative in TEST_TERMS_ALLOWED_FOR_LEGACY_FIXTURE_MIGRATION:
            continue
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for term in BANNED_CURRENT_BOOK_TERMS:
            if term in text:
                violations.append(f"{relative}: contains {term!r}")
    assert violations == []
```

- [ ] **Step 2: Run guard**

Run:

```bash
FORWIN_HTTP_BIND=127.0.0.1 .venv/bin/python -m pytest tests/test_no_story_specific_hardcoding.py -q
```

Expected: PASS.

---

## Task 5: Verify Existing Behavior Still Works

**Files:**
- No production changes unless a regression appears.

- [ ] **Step 1: Run targeted canon-quality tests**

Run:

```bash
FORWIN_HTTP_BIND=127.0.0.1 .venv/bin/python -m pytest tests/test_countdown_ledger.py tests/test_canon_quality_service.py tests/test_character_state_transition_ledger.py tests/test_identity_role_ledger.py -q
```

Expected: PASS.

- [ ] **Step 2: Run plan/future-audit tests**

Run:

```bash
FORWIN_HTTP_BIND=127.0.0.1 .venv/bin/python -m pytest tests/test_future_plan_auditor.py tests/test_future_plan_audit_persistence.py tests/test_obligation_plan_binding_audit.py tests/test_plan_patch_validator.py tests/test_plan_backed_deferred_acceptance.py -q
```

Expected: PASS.

- [ ] **Step 3: Run prompt/context/governance tests**

Run:

```bash
FORWIN_HTTP_BIND=127.0.0.1 .venv/bin/python -m pytest tests/test_writer_prompt_contract.py tests/test_context_provider_chain.py tests/test_governance_review_and_checkpoint.py tests/test_project_operation_guards.py tests/test_placeholder_leakage_gate.py -q
```

Expected: PASS.

- [ ] **Step 4: Run source hygiene checks**

Run:

```bash
git diff --check
git diff -- forwin | grep '^+' | grep -E 'd2338|旧城遗档|白塔重置|林澈|沈宴秋|洛庭若|顾临川|林远舟|沈砚|白塔|岫苑|地下旧轨|潮汐钟楼|失忆广场|档案公会|白塔巡检员|林氏'
```

Expected: `git diff --check` exits 0. The grep command prints no lines.

---

## Task 6: Re-run the Current 12-Chapter Regression Decision

**Files:**
- No code changes.

- [ ] **Step 1: Inspect project state through ForWin MCP**

Use ForWin MCP:

```text
project_get(project_id="d2338a0e8bfe4e00a068b03ce9e9b0bf")
chapter_get(project_id="d2338a0e8bfe4e00a068b03ce9e9b0bf", chapter_number=36)
```

Expected: confirms whether the historical accepted chapter still contains the old `28:00` regression.

- [ ] **Step 2: Decide whether to retry chapter 36**

If the user wants the test novel repaired after generic hardcoding cleanup, run only after checking no active task:

```text
task_active_generation_check(project_id="d2338a0e8bfe4e00a068b03ce9e9b0bf")
chapter_review_retry(project_id="d2338a0e8bfe4e00a068b03ce9e9b0bf", chapter_number=36, allow_accepted=true, continue_generation=false, reason="Regenerate final chapter after generic countdown gate fix.")
```

Expected: chapter 36 is reset for review/regeneration without introducing another active generation task accidentally.

---

## Completion Criteria

- Production source audit has zero current-book token violations.
- Selected generic regression tests have zero current-book token violations.
- No new production code path is conditional on a concrete project id, book title, character name, or location from the test novel.
- Countdown, identity, character-state, future-plan, prompt, context, governance, and placeholder regression tests pass with `FORWIN_HTTP_BIND=127.0.0.1`.
- `git diff --check` passes.
