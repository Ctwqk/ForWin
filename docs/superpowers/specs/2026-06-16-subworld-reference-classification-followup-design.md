# Subworld Reference Classification Follow-up Design

## Context

The generalized subworld reference classifier fixed the prior production blockers for technical identifiers, numbered plot entities, role-prefixed names, and Chinese compound identities. The next production review pass exposed adjacent reference shapes:

- generic placeholder people such as `未知人物`
- organization names such as `若槐宗邦`
- mixed character-and-technical-code aliases such as `灰鸦/L-7`

The 240-chapter run advanced from chapter 68 through chapter 88 before hitting the placeholder case at chapter 89, so the previous fix is effective for its target shapes. The 60-chapter run regenerated chapter 54 and now exposes the adjacent shapes above.

## Design

Extend `forwin.checker.reference_classifier` rather than adding project- or chapter-specific exceptions.

Generic placeholder person references should be treated like role references and ignored by subworld admission. Organization references should be treated as non-character references using organization suffix/keyword classification. Mixed `character/technical-id` references should normalize to the character side, so an allowed character is not blocked by a technical alias, while an unauthorized character still remains visible as the real admission problem.

## Scope

In scope:

- Add classifier tests for `未知人物`, `若槐宗邦`, and variants.
- Add admission tests proving these shapes do not create spurious unknown named entities.
- Normalize `灰鸦/L-7` to `灰鸦`.
- Preserve existing behavior for `许晏/馆员`, `L-7`, `QT-7741`, and real names.

Out of scope:

- Automatically admitting `灰鸦` into chapter 54.
- Changing subworld allowed-list generation.
- Changing review severity or retry policy.

## Verification

Run targeted classifier/subworld tests, then the full focused suites:

- `tests/test_subworld_control.py`
- `tests/test_band_plan_service.py`

After deploy, retry the blocked review chapters through production MCP and inspect residual review issues before approving or continuing.
