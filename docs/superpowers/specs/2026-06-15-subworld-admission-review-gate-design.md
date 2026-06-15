# Subworld Admission Review Gate Repair Design

## Approval And Scope

The user approved this design on 2026-06-15 and explicitly authorized the follow-up spec and plan steps to proceed without another review gate. This spec covers the production review blockers found in:

- 60-chapter production test project `634f037db38443a7b9a4b8c6534f549f`, chapter 52.
- 240-chapter production test project `ed259b9ad0a44f65b7f84250168f91cc`, chapter 27.

The goal is to fix the repeatable subworld admission failure patterns and then retry both blocked chapters. This is not a broad relaxation of strict named-character admission.

## Problem

Both production runs reached `needs_review` after repeated repair attempts. The blocking errors come from strict subworld admission interpreting some narrative references as unapproved named characters:

- `老环线调度员`: a role/title style reference. It should not create a new named-character requirement by itself.
- `003号分割体`: a numbered split-body plot entity introduced by the active plan. It should be admitted through the chapter entry-target contract when the chapter plan explicitly calls for contact with it.
- `馆员陈潮白`: a role-prefix plus personal name reference. Admission should evaluate the canonical personal name `陈潮白`, not the prefixed surface form.

The current checker already filters some generic roles and malformed parenthetical metadata, and the band planner already infers simple entry targets from chapter goals such as `引入灰鸦作为...`. The production failures show the same boundary needs three narrow extensions, not a new bypass.

## Design

### Checker Candidate Normalization

Extend `ContinuityChecker._candidate_character_name` and its helpers so the checker applies production-safe normalization before deciding that a mention is an unknown named character.

Rules:

- Role-like old-title references ending in scheduler/operator style roles, such as `老环线调度员`, are treated as generic role references.
- Numbered split-body references like `003号分割体` are not treated as ordinary named characters by the checker unless they are admitted by planning as explicit entry targets.
- Role-prefix plus personal-name references are normalized before admission. `馆员陈潮白` should evaluate as `陈潮白` when the personal name shape is clear.

The checker must continue to flag a real unknown named character such as `灰鸦` when it is not allowed by active subworlds, known canon names, protagonist names, or chapter entry targets.

### Planning Entry-Target Inference

Extend the existing band-plan entry-target inference in `forwin/planning/band_plan_service.py`. It should admit explicit names from chapter plans that use production phrasing beyond the earlier `引入/登场` patterns.

Rules:

- Extract explicit target names from plan text such as `与003号分割体接触`, `接触003号分割体`, and `馆员陈潮白存在双重死亡记录`.
- Preserve the current filters for generic non-names, relationship phrases, long descriptive phrases, and common planning nouns.
- Do not infer broad unknown nouns or every short token as a target.

This keeps the admission contract in the planner when the plan intentionally introduces or foregrounds a new entity, while the checker still rejects unplanned named cast expansion.

### Tests

Add focused regression tests for the three production patterns:

- Subworld admission ignores `老环线调度员` while still reporting an unapproved `灰鸦`.
- Candidate extraction normalizes `馆员陈潮白` to `陈潮白`, allowing it when `陈潮白` is already allowed.
- Band planning infers `003号分割体` and `陈潮白` as chapter entry targets from explicit plan text.

Run the existing subworld and band-plan test files to catch regressions around strict admission.

### Production Recovery

After implementation, verification, push, and production deploy:

1. Confirm the two projects have no active generation task.
2. Retry chapter 52 for project `634f037db38443a7b9a4b8c6534f549f` with `continue_generation=True`.
3. Retry chapter 27 for project `ed259b9ad0a44f65b7f84250168f91cc` with `continue_generation=True`, unless the first retry already occupies the single production worker. If only one task can run at once, queue or start the second retry after confirming the system accepts it without duplicating active work.
4. Re-read MCP state and report whether each project has moved past the original blocker or is actively running.

Do not approve the current drafts directly. Retrying forces regeneration or review with the corrected admission rules.

## Error Handling

If a retried chapter still lands in `needs_review`, inspect the new residual issues through MCP and treat the new blocker as separate evidence. Do not stack another speculative checker change without a fresh root-cause pass.

If the residual issues are warnings only and the generation system can continue, allow the normal workflow to proceed. Do not pause or terminate a task unless MCP reports an active-task conflict or unsafe restart condition.

## Verification

Focused local verification:

```bash
pytest tests/test_subworld_control.py tests/test_band_plan_service.py
```

Production verification:

```bash
ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'
```

Then confirm service health and retry state through the production MCP endpoint at `http://10.0.0.126:8896/mcp`.
