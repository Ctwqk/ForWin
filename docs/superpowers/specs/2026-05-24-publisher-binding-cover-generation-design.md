# Publisher Binding And Cover Generation Design

## Context

ForWin already has a working browser-extension publisher path for Qidian and
Fanqie. The extension can create books, choose audience/channel fields, select
categories, write chapter drafts, publish chapters, and return page-level
status payloads. The weak point is durability: platform work ids, chapter ids,
audit states, and upload state mostly remain in upload-job payloads or are
rediscovered from book names.

The next publishing pass needs to make the platform link durable, add MiniMax
cover generation, upload selected covers after the first chapter succeeds, and
bring platform compliance into the existing review and repair path so upload
failures do not create a separate correction workflow.

## Goals

- Persist project-to-platform work bindings and chapter bindings as structured
  database rows.
- Preserve the existing extension-driven platform automation instead of
  replacing it.
- Generate multiple cover candidates through MiniMax image generation when a
  create-if-missing upload is created and no usable cover exists.
- Automatically select a cover candidate by platform validation first and
  content fit second, without waiting for confirmation by default.
- Keep manual cover confirmation, selection, regeneration, upload, and retry
  controls available.
- Upload the selected cover after the first chapter publishing flow succeeds.
- Track work, chapter, cover, and audit state in first-class publisher records.
- Move content-level platform compliance into the reviewer path so existing
  repair and rewrite flows can handle sensitive terms and platform-risk issues.
- Keep final upload preflight focused on platform submission readiness:
  metadata, category mapping, cover files, binding state, and reviewer outcome.

## Non-Goals

- Do not rewrite the Qidian or Fanqie browser adapter from scratch.
- Do not submit signing, real-name, contract, or revenue forms automatically.
- Do not make cover upload block or roll back a successful chapter publish.
- Do not require human confirmation before the default cover upload path.
- Do not treat platform public documentation as sufficient truth when the live
  authenticated DOM shows different required fields.
- Do not put platform-specific page automation in the backend service.

## Architecture

Use a "persistent binding plus task extension" approach.

The backend owns durable publisher records, MiniMax cover generation, platform
catalogs, compliance review rules, and final preflight. The browser extension
continues to own authenticated platform navigation, DOM form filling, chapter
publishing, cover upload, and audit-state scraping.

Existing `PublisherUploadJob` remains the execution queue and gains a required
`task_kind` column. Existing rows and callers default to `chapter_upload`.
Supported kinds:

- `chapter_upload`
- `cover_generate`
- `cover_upload`
- `audit_sync`

The durable records are the source of truth. Job payloads remain execution
envelopes and retry/debug evidence, not the only place where remote ids or
audit states live.

## Data Model

### PublisherWorkBinding

Represents one ForWin project bound to one platform work.

Fields:

- `id`
- `project_id`
- `platform_id`
- `book_name`
- `remote_book_id`
- `remote_url`
- `audit_state`: `unknown`, `draft`, `under_review`, `approved`, `rejected`
- `audit_reason`
- `platform_status`
- `cover_asset_id`
- `cover_state`: `none`, `generated`, `queued`, `uploaded`,
  `under_review`, `approved`, `rejected`, `failed`
- `last_synced_at`
- `raw_payload_json`
- timestamps

Unique key:

- preferred: `(project_id, platform_id)` when `project_id` exists
- fallback lookup only: `(platform_id, book_name)` for manual uploads without a
  project id

### PublisherChapterBinding

Represents one local chapter on one platform.

Fields:

- `id`
- `work_binding_id`
- `project_id`
- `platform_id`
- `chapter_number`
- `chapter_title`
- `remote_chapter_id`
- `remote_url`
- `publish_state`: `unknown`, `drafted`, `submitted`, `published`, `failed`
- `audit_state`: `unknown`, `under_review`, `approved`, `rejected`
- `audit_reason`
- `word_count`
- `last_synced_at`
- `raw_payload_json`
- timestamps

Unique key:

- `(work_binding_id, chapter_number)` when chapter number exists
- fallback update by `(work_binding_id, chapter_title)` when publishing a
  manual chapter without a number

### PublisherCoverAsset

Represents a generated or uploaded cover candidate.

Fields:

- `id`
- `project_id`
- `work_binding_id`
- `source`: `minimax`, `uploaded`, `manual`
- `prompt`
- `source_meta_json`
- `status`: `generating`, `generated`, `selected`, `uploaded`, `rejected`,
  `failed`
- `selection_state`: `candidate`, `selected`, `approved`, `rejected`
- `score`
- `score_reasons_json`
- `width`
- `height`
- `file_size_bytes`
- `file_path`
- `mime_type`
- `platform_validation_json`
- `minimax_request_id`
- `raw_payload_json`
- timestamps

At most one active selected cover per project/platform should be used for
automatic cover upload. Previous selected covers remain as history.

### PublisherMilestone

Stores non-invasive platform reminders.

Fields:

- `id`
- `work_binding_id`
- `milestone_type`: `work_approved`, `word_count_threshold`,
  `signing_entry_visible`, `revenue_entry_visible`
- `state`: `open`, `acknowledged`, `dismissed`
- `message`
- `evidence_json`
- timestamps

This supports signing and revenue reminders without automating contractual or
personal-information flows.

## Main Flow

### Chapter Upload Creation

When creating a `chapter_upload` job:

1. Normalize project id and book metadata as today.
2. Run reviewer-aware publisher compliance checks if the chapter has project
   context and does not already have a passing compliance result.
3. Run publisher preflight for non-content submission readiness.
4. If `create_if_missing=true`, cover generation is enabled, and there is no
   usable selected cover, enqueue `cover_generate`.
5. Enqueue the chapter upload job.

Cover generation starts early and can run in parallel with platform chapter
publishing.

### Chapter Upload Result

When the extension reports success or a verified draft:

1. Merge execution payload into the job as today.
2. Extract platform work identifiers where available:
   `remote_book_id`, `work_id`, `book_id`, `remote_url`, and current URL.
3. Upsert `PublisherWorkBinding`.
4. Upsert `PublisherChapterBinding` with `official_status`, word count, remote
   chapter id, and audit state.
5. If this is the first successful chapter path and a selected cover exists,
   enqueue `cover_upload`.
6. If the selected cover does not exist yet, let `cover_generate` completion
   enqueue `cover_upload` once a work binding and first successful chapter
   binding exist.

### Cover Generation

`PublisherCoverService` runs in the backend.

Inputs:

- project title
- `book_meta.intro`
- `primary_category`
- `theme_tags`, `role_tags`, `plot_tags`
- `protagonist_names`
- Genesis or project summary where available
- optional `cover_style_hint`

MiniMax integration:

- Call the configured MiniMax image endpoint with path `/image_generation`.
- Default model: `image-01`.
- Default candidate count: 4, configurable up to the provider maximum.
- Prefer `response_format=base64` and store local files immediately.
- If the provider returns URLs, download and store the files because temporary
  URLs can expire.

Prompt policy:

- Generate cover art, not final typography-heavy cover layout.
- Avoid depending on accurate Chinese text rendering by the image model.
- Use title, genre, tone, protagonist, and symbolic scene cues.
- Allow later local text overlay as a separate enhancement, but do not require
  it for the first pass.

Candidate selection:

1. Validate format, dimensions, file size, and image readability.
2. Apply platform-specific validation if platform requirements are known.
3. Treat uncertain external specs as warnings, not hard failures.
4. Score content fit by category, intro, protagonist, tone, and obvious unsafe
   visual content.
5. Select the highest-scoring valid candidate.
6. If scoring fails, select the first valid candidate.
7. If no candidate is valid, mark generation failed and keep reasons for retry.

Default behavior does not wait for human confirmation. Manual confirmation is a
UI override that can approve, reject, select another candidate, or regenerate.

### Cover Upload

`cover_upload` is an extension-executed task.

Payload:

- `work_binding_id`
- `platform_id`
- `book_name`
- `remote_book_id`
- `remote_url`
- `cover_asset_id`
- `file_path`

The extension navigates to the platform work management page, uploads the cover
file, waits for an upload or review signal, then returns:

- `cover_state`
- platform message
- current URL
- work id and page identifiers if found
- optional page text summary for audit evidence

The backend updates `PublisherWorkBinding.cover_state` and
`PublisherCoverAsset.platform_validation_json`. Cover upload failure never
rolls back chapter publishing.

### Audit Sync

`audit_sync` is an extension-executed task that can run manually or from a
scheduled publisher maintenance path.

It updates:

- work audit state from book/work info pages
- chapter publish and audit state from chapter management pages
- cover review state from work management pages
- signing/revenue milestones when the platform shows relevant entry points

The backend stores normalized states in bindings and keeps the raw evidence in
`raw_payload_json`.

## Reviewer And Preflight Boundary

### PublisherComplianceReviewer

Add a reviewer owned by the review surface, for example
`forwin/reviewer/publisher_compliance.py`, and wire it into
`HistoricalReviewHub`.

Responsibilities:

- Detect platform-content risks that can be fixed by rewriting:
  sensitive terms, obvious prohibited expressions, external links, contact
  information, suspicious promotional text, risky title or intro phrases, and
  platform-specific text constraints that affect the draft.
- Emit normal `ReviewVerdict` and `ContinuityIssue` values.
- Use `issue_type="publisher_compliance"` and reviewer
  `publisher_compliance`.
- Include platform ids in evidence refs when a rule is platform-specific.
- Default severity:
  - `error` for hard platform form or policy blockers that should stop
    auto-publish.
  - `warning` for uncertain or soft content risks.
- Keep platform rule dictionaries configurable and testable.

This reviewer should reuse existing review and repair mechanics. It should not
perform browser checks, upload checks, cover checks, or remote status sync.

### PublisherPreflightService

Preflight remains in the publisher runtime, but its scope is final submission
readiness:

- required metadata is present
- platform category mapping is available or has a deterministic fallback
- Fanqie intro length and protagonist requirements are satisfiable
- selected cover exists when cover upload is required
- selected cover file is readable and passes known local validation
- work binding and chapter binding can be resolved for follow-up jobs
- latest publisher compliance review is passing or warnings are explicitly
  allowed

If preflight finds a content issue and there is no publisher compliance review,
it should request or surface the missing review instead of inventing a second
repair path.

## Platform Catalogs

Move primary mapping data out of `platform-agent.js` into backend catalogs.

Recommended module:

- `forwin/publisher_runtime/platform_catalogs.py`

Catalog contents:

- Qidian audience/site mapping
- Qidian primary category mapping
- Qidian subcategory fallback rules
- Fanqie gender/channel mapping
- Fanqie main category mapping
- Fanqie theme, role, and plot tag mapping
- synonyms from ForWin metadata to platform names
- validation rules for required fields

The backend attaches resolved platform metadata to job payloads. The extension
keeps a local fallback mapping for compatibility with old backend payloads and
for last-resort operation if a catalog value is missing.

## API And UI

Add or extend APIs for:

- list and get work bindings
- list and get chapter bindings
- list cover assets for a project
- generate or regenerate cover candidates
- select, approve, or reject a cover
- enqueue cover upload
- enqueue audit sync
- read preflight results

Project automation settings gain:

- `cover_generation_enabled`
- `cover_confirmation_required` default `false`
- `cover_candidate_count`
- `cover_style_hint`
- `auto_cover_upload_enabled`
- `publisher_compliance_required`

UI surfaces:

- publisher task detail shows work binding, chapter binding, remote URL, audit
  state, selected cover, cover upload state, and preflight result
- project publisher settings show cover generation options
- project page can display generated cover candidates and allow manual select,
  approve, reject, regenerate, upload, and sync audit
- task drawer links content compliance failures back to the review or repair
  flow instead of presenting them as opaque upload failures

## Error Handling

- Chapter upload failure prevents cover upload for that chapter path.
- Cover generation failure does not fail chapter upload.
- Cover upload failure does not roll back chapter upload.
- Audit sync failure records an error and can be retried.
- Missing platform login remains an extension/session error.
- Missing metadata or unmappable required platform fields can block job
  creation or mark the job failed before browser execution.
- Sensitive content errors come from reviewer results and should route through
  repair/rewrite.
- Unknown platform DOM changes should return structured extension errors with
  current URL and phase.

## Tests

Backend model and runtime tests:

- create a chapter upload job with `create_if_missing=true` and no cover;
  verify `cover_generate` is enqueued.
- cover generation stores multiple `PublisherCoverAsset` rows and selects one.
- invalid cover candidates are not selected when at least one valid candidate
  exists.
- upload job success upserts `PublisherWorkBinding`.
- upload job success upserts `PublisherChapterBinding`.
- selected cover plus first chapter success enqueues `cover_upload`.
- cover generation completion enqueues `cover_upload` if first chapter already
  succeeded.
- audit sync result updates work and chapter audit states.
- publisher preflight blocks missing hard metadata but does not duplicate
  reviewer repair logic.

Reviewer tests:

- publisher compliance reviewer emits `publisher_compliance` issues for hard
  sensitive terms.
- warnings and errors merge into `HistoricalReviewHub` verdicts.
- a publisher compliance failure produces repair-friendly issue metadata.
- existing reviewer behavior is unchanged when publisher compliance is disabled.

Catalog tests:

- Qidian and Fanqie category mappings resolve known project metadata.
- missing mappings produce deterministic fallback warnings.
- extension payload contains resolved platform metadata.

Extension tests:

- cover upload job receives local cover path and returns a normalized cover
  state.
- audit sync job returns normalized work/chapter/cover state payloads.
- existing chapter upload tests still pass with task kind payloads.

Browser/manual verification:

- validate authenticated Qidian work info page navigation and cover upload.
- validate authenticated Fanqie book info page navigation and cover upload.
- validate audit-state scraping on real pages after platform login.

## Rollout Plan

Implement as one program with staged verification:

1. Add data models, migrations, schemas, serializers, and binding upsert logic.
2. Add task-kind support while keeping existing chapter upload behavior working.
3. Add publisher compliance reviewer and wire it into the review hub behind a
   setting.
4. Add preflight service scoped to final submission readiness.
5. Add platform catalogs and payload resolution.
6. Add MiniMax cover generation, local asset storage, validation, scoring, and
   selection.
7. Add cover upload and audit sync task execution in the extension.
8. Add UI/API controls for bindings, cover candidates, preflight, retries, and
   audit sync.
9. Run focused backend tests, extension tests, and authenticated browser
   verification for Qidian and Fanqie.

## Completion Definition

The work is complete when a new project can enqueue create-if-missing publishing
and ForWin can:

- generate multiple MiniMax cover candidates,
- automatically select a valid candidate without waiting for confirmation,
- create or find the platform work,
- publish or draft the first chapter,
- persist work and chapter bindings with remote ids and audit state,
- enqueue and execute cover upload after the first chapter succeeds,
- sync work/chapter/cover audit state,
- surface platform-content compliance through reviewer and repair flows,
- block only final submission readiness failures in publisher preflight,
- show all relevant state in the publisher UI,
- retry failed cover upload and audit sync tasks without corrupting bindings.
