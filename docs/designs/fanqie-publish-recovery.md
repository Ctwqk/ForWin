# Fanqie publication recovery

## Goal

Allow an authorized ForWin-generated chapter to pass Fanqie's current publication flow with an accurate AI disclosure, while preserving the distinction between saving a draft and publishing it.

## Scope

- Extension publication confirmation and version 0.1.63.
- Operator guidance for runtime contracts, remote verification, and expired schedules.
- No schema changes, automatic bulk release, historical Canon repair, or production source patches.

## Design

Draft requests return before any publication dialog action. Publication confirmation recognizes only visible settings dialogs and the observed typo/content-check prompts. Each action targets an exact button in its recognized modal; unknown prompts stop the flow. The confirmation path does not invoke the generic guide dismissor. A stopped confirmation ends the upload command with `publish-not-confirmed` and its stop phase, so page-text outcome verification runs only after the settings dialog closes. If “确认发布” was clicked but the dialog stayed open, the error reports an unknown remote state that must be reconciled before any retry.

ForWin-generated text is declared AI-assisted by selecting “是” for “是否使用AI”. The answer is taken from the smallest container that holds that question; if that container also holds another 是/否 pair, confirmation stops. The code verifies the radio selection before pressing “确认发布”; an absent, ambiguous, or unselectable disclosure form stops confirmation. The current observed form has one 是/否 radio pair and a separate scheduling switch. A maximum of four transitions bounds the flow.

Operator recovery must inspect the deployed endpoint rather than infer its behavior from a newer checkout. The legacy project upload endpoint can create a chapter job, while the newer source requires a materialized Canon job. Failure/cancellation never proves that the remote draft is absent: reconcile stable remote IDs and complete normalized text first.

On 2026-10-17, the existing first-chapter draft was checked against the accepted source and published through Fanqie's normal UI with AI=yes. The platform returned 审核中 and then 已发布; the public reader URL was independently accessible without login. Chapter 18 was uploaded through the deployed project endpoint and its reopened body matched the accepted source. These operator results are not evidence that this source change is deployed.

## Risk

The deployed API/browser are older than master and have an incompatible release history. This patch must join the approved complete-image and forward-migration process. Do not copy source into production or deploy current master directly onto the legacy database.

Unknown future platform dialogs stop confirmation. Existing outcome verification remains responsible for proving publication; a closed modal alone is not a public-release receipt. Legacy chapter bindings still lack reliable remote identity, and the deployed audit-sync implementation returns no chapter records; preserve independently verified receipts instead of fabricating extension results.

## Verification

- Sixteen behavior regressions run against a selector-aware DOM fixture. They cover drafts, truthful disclosure, missing, ambiguous, or unselectable disclosure, AI sidebar false positives, hidden dialogs, unknown warnings, and the observed publication sequence. Upload-level regressions check that a stopped confirmation returns `publish-not-confirmed` instead of a page-text receipt. Failures were observed before the corresponding fixes.
- All 198 extension tests pass; Chromium and Firefox packages build.
- Independent code review found and verified the fix for generic guide dismissal after the publish action.
- Run operator readiness against the actual production service, using an explicit tunnel endpoint when the direct LAN route is unavailable. Pre-PR evaluation's fixed LAN endpoint may fail independently of the application.

## Rollback

Revert the source change before deployment if necessary. After deployment, use the approved image release procedure. A code rollback must not unpublish, overwrite, or resend the already published chapter. Preserve remote receipts and the old schedule's actual expiration.
