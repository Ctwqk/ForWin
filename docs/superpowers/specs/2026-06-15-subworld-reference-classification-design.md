# Subworld Reference Classification Repair Design

## Approval And Scope

The user approved this direction on 2026-06-15 and explicitly authorized spec, plan, implementation, test, deploy, and production continuation without another review gate.

This spec replaces the narrow repair in commits `e129461` and `216af5c`. The first implementation action is to revert those commits, then rebuild the fix around generalized reference classification.

The scope is limited to strict subworld admission false positives exposed by the current 60-chapter and 240-chapter production runs. It does not relax admission for real unplanned named characters.

## Evidence From Four Checkpoints

### 60-chapter old blocker

Chapter 52 previously blocked on `老环线调度员`. The generalized class is:

- Role/title reference with descriptive prefix.
- It should not be treated as a new named character.

### 240-chapter old blocker

Chapter 27 previously blocked on `003号分割体` and `馆员陈潮白`. The generalized classes are:

- Numbered plot entity such as split-body or fragment references.
- Role prefix plus personal name, which should normalize to the personal name.

### 60-chapter new blocker

Chapter 54 now blocks on `L-7`. The generalized class is:

- Location, layer, coordinate, or technical identifier.
- It should not become a named character only because it is short.

### 240-chapter new blocker

Chapter 68 now blocks on `L7-09`, `QT-7741`, and `许晏/馆员`. The generalized classes are:

- Code-style identifier with Latin letters and digits.
- Compound identity or alias reference joined by a slash.
- The compound form should not be evaluated as one new character string. Known parts can remain valid separately.

## Root Cause

`ContinuityChecker._looks_like_named_character` currently defaults to `len(text) <= 12` after a small set of exclusions. That makes any short technical token, coordinate, or compound identity look like a named character unless it is explicitly filtered.

The previous fix added narrow exclusions for the observed surface forms. It worked for those chapters, but it did not change the underlying classification boundary between:

- Natural person-like names.
- Generic role references.
- Non-cast technical identifiers.
- Compound identity references.

## Design

### Reference Classifier

Create a focused classifier for chapter entity references. The classifier returns a normalized candidate name or an empty string.

Rules:

- Natural Chinese names such as `灰鸦`, `陈潮白`, `许晏`, and `沈岚` remain candidates.
- Generic role/title references such as `老环线调度员`, `系统巡检员`, and `第七区溺水者残影` are not candidates.
- Role-prefixed person names such as `馆员陈潮白` normalize to `陈潮白`.
- Code and coordinate identifiers such as `L-7`, `L7-09`, `QT-7741`, `VT-7-19-γ`, `E-7749`, and `XU-CH-1997-0847` are not candidates.
- Numbered plot entities such as `003号分割体`, `第004号分割体`, and `第40份密钥` are not ordinary named-character candidates.
- Compound identity references such as `许晏/馆员`, `许晏与馆员`, and `许晏（馆员人格）` are not treated as a single new character candidate.

The classifier must be broader than the four exact production strings, but still conservative enough to keep a true unknown cast member such as `灰鸦` blocked when not admitted.

### Checker Integration

Replace the narrow helper additions from `216af5c` with the classifier-backed candidate extraction.

`_candidate_character_name` should become the single path for subworld admission candidates. It should:

1. Reject malformed parenthetical annotations.
2. Normalize harmless parenthetical labels already supported by the checker.
3. Normalize role-prefixed personal names.
4. Reject generic roles, technical IDs, numbered plot artifacts, and compound identity forms.
5. Return natural person-like candidate names.

### Planner Integration

Planner entry-target inference should use the same normalization helper for explicit new-person targets. It should not admit technical IDs or coordinate tokens as character entry targets.

The planner can continue to infer deliberate natural-person entry targets from chapter plans, including `馆员陈潮白存在...` as `陈潮白`. It should not add `L-7`, `QT-7741`, or `第40份密钥` as character targets.

### Revert Requirement

The implementation must revert commits:

- `216af5c fix: narrow subworld admission false positives`
- `e129461 docs: design subworld admission review gate repair`

Then it must add a new spec, plan, tests, and generalized implementation. This keeps the history honest: the narrow repair is explicitly replaced rather than silently accumulated.

## Tests

Tests must cover both production examples and same-class variants:

- `老环线调度员`, `系统巡检员`, and `第七区溺水者残影` are ignored as generic references.
- `003号分割体`, `第004号分割体`, and `第40份密钥` are not named-character candidates.
- `L-7`, `L7-09`, `QT-7741`, `VT-7-19-γ`, `E-7749`, and `XU-CH-1997-0847` are not named-character candidates.
- `许晏/馆员`, `许晏与馆员`, and `许晏（馆员人格）` are not treated as one new character candidate.
- `馆员陈潮白` normalizes to `陈潮白`.
- `灰鸦` remains a named-character candidate and is still blocked if not allowed.
- Planner inference admits `陈潮白` from role-prefixed natural-person plan text, but does not admit technical IDs as character targets.

## Production Recovery

After tests pass and production deploys:

1. Confirm both affected projects have no active generation task.
2. Retry the 60-chapter project chapter 54 with `continue_generation=True`.
3. For the 240-chapter project, first inspect the current `band_checkpoint_warn`. If the checkpoint tool exposes warnings only and no active task exists, resolve or continue through the appropriate MCP workflow. Then retry chapter 68 with `continue_generation=True` if it remains the active chapter blocker.
4. Confirm through MCP that the original blocker class is gone and generation is queued or running.

Do not approve the blocked drafts directly unless the only remaining gate is a non-error checkpoint that the MCP workflow explicitly allows to pass.

## Verification

Local verification:

```bash
.venv/bin/pytest tests/test_subworld_control.py tests/test_band_plan_service.py
.venv/bin/python -m compileall forwin
```

Production verification:

```bash
git push origin master
ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'
```

Then use production MCP at `http://10.0.0.126:8896/mcp` for project, task, checkpoint, and chapter state.
