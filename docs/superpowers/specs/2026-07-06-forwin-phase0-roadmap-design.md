# ForWin Phase 0 Roadmap Design

## Goal

Implement the Phase 0 unblocker from `forwin_integrated_roadmap_phase0.md`: repair-exhausted chapters must receive a structured final decision, and subworld admission failures must route through an explicit admission path instead of blind writer repair or default force-accept.

## Requirements

- Final acceptance rules always evaluate `FinalAcceptanceGate`, including reviews with no `repair_verification`.
- Repair-exhausted decisions never fall through to `no_rule_matched`; they report one of the structured final gate reasons already supported by `FinalAcceptanceGate`.
- `subworld_admission_missing_canon_entity`, `subworld_admission_unauthorized_new_entity`, and `sub_world_unknown_named_entity` route to a dedicated `subworld_admission_patch` decision outcome.
- Subworld admission handling classifies unknown references as `register_entity`, `genericize_background_reference`, or `manual_review_required`.
- Register actions update the chapter experience plan entry targets so the admission roster can admit the entity on the retry path. When a real session is available, the action also creates a durable entity and roster row.
- Genericize actions reuse the existing safe subworld autofix path and must not mask protected canon names.
- Uncertain admission cases return a structured manual action with one-click-style choices rather than rewriting or accepting blindly.
- Repair prompts carry explicit length budget metadata so repeated repairs are instructed to replace/compress instead of append when the chapter is already over budget.
- Tests cover final gate, repair routing, admission policy, admission patch executor, and repair budget instruction metadata.

## Architecture

The review engine owns decision routing. `DecisionOutcome` gains `subworld_admission_patch`, and `repair_v2` maps subworld admission issue kinds to that outcome while preserving `scope="subworld"`. The repair loop treats that outcome as locally executable and delegates to a subworld admission patch helper before rebuilding the chapter context.

The policy layer is separate from the orchestrator. `forwin/subworld/admission_policy.py` classifies a review issue plus writer output and plan context into one of the three required actions. `forwin/subworld/admission_patch.py` applies that action: register by adding chapter entry targets and durable rows where possible, genericize by returning replacement metadata, or return a manual action payload.

Final acceptance remains conservative. Missing repair verification and hard/unknown residual issues stay manual and non-forceable. Only blackbox mode with successful verification and soft residual issues can force-accept.

## Testing

Unit tests provide the regression proof:

- `tests/review_engine/test_final_acceptance.py` proves no-verification final reviews match the final gate and do not return `no_rule_matched`.
- `tests/review_engine/test_repair_v2.py` proves subworld admission issue kinds route to `subworld_admission_patch`.
- `tests/test_subworld_admission_policy.py` proves register, genericize, and manual classifications.
- `tests/test_subworld_admission_patch.py` proves register patches chapter entry targets and genericize returns replacements without durable state mutation.
- `tests/test_repair_word_budget.py` proves default repair instructions include bounded replacement/compression metadata.

## Runtime Verification

After tests pass, deploy from `/Users/magi1/ForWin-source-github` through the 150 sync path, then use the ForWin MCP tools for a 100-chapter real-machine run. Project/task/chapter truth must come from MCP, not raw database reads.
