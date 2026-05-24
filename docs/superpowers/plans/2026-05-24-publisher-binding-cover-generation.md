# Publisher Binding And Cover Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ForWin publishing durable and platform-aware by persisting Qidian/Fanqie work and chapter bindings, generating multiple MiniMax cover candidates, uploading the selected cover after the first chapter succeeds, syncing audit state, and routing platform text-compliance failures through the existing reviewer/repair path.

**Architecture:** Keep the browser extension as the authenticated platform automation layer. Extend the backend publisher runtime with durable bindings, cover assets, platform catalogs, MiniMax image generation, backend-owned publisher jobs, final preflight, and reviewer-integrated platform compliance. `PublisherUploadJob` remains the common job envelope and gains `task_kind`, with `chapter_upload`, `cover_generate`, `cover_upload`, and `audit_sync` as the first supported kinds.

**Tech Stack:** Python 3.12, SQLAlchemy, Alembic, pytest, FastAPI/Pydantic, httpx, JavaScript browser extension tests with `node --test`, MiniMax image API, existing ForWin reviewer and publisher runtime modules.

---

## Invariants

- Existing chapter upload behavior must remain compatible with old callers.
- Existing upload jobs without `task_kind` must behave as `chapter_upload`.
- The extension must never claim backend-owned `cover_generate` jobs.
- Cover generation and cover upload failures must not roll back chapter upload.
- Default cover behavior is automatic: generate multiple candidates, select the best valid candidate, and upload without waiting for confirmation.
- Manual confirmation remains available through UI/API as an override.
- Sensitive/platform text compliance belongs in reviewer output so the existing repair chain can fix it.
- Publisher preflight checks final submission readiness only; it does not create a separate content-repair path.
- Do not move platform DOM automation into the backend.
- Do not add Qidian "creative_type" or a fake Fanqie "no category needed" flow.

## File Map

Backend models and migrations:
- Modify: `forwin/models/publisher.py`
- Modify: `forwin/models/__init__.py`
- Modify: `forwin/models/base.py`
- Create: `forwin/migrations/versions/0018_publisher_bindings_covers.py`

Publisher runtime:
- Create: `forwin/publisher_runtime/bindings.py`
- Create: `forwin/publisher_runtime/covers.py`
- Create: `forwin/publisher_runtime/platform_catalogs.py`
- Create: `forwin/publisher_runtime/preflight.py`
- Create: `forwin/publisher_runtime/backend_jobs.py`
- Modify: `forwin/publisher_runtime/service.py`
- Modify: `forwin/publisher_runtime/upload_jobs.py`
- Modify: `forwin/publishers/manager.py`

Reviewer integration:
- Create: `forwin/reviewer/publisher_compliance.py`
- Modify: `forwin/reviewer/hub.py`
- Modify as needed: `forwin/api_schema/project.py`
- Modify as needed: `forwin/project_payloads/runtime_maps.py`

API and routes:
- Modify: `forwin/api_schema/publisher.py`
- Modify: `forwin/api_publisher_ops.py`
- Modify: `forwin/api_publisher_routes.py`
- Modify: `forwin/api_route_registry.py`

Automation and worker hook:
- Modify: `forwin/api_core/automation.py`
- Modify as needed: `forwin/api_core/state.py`
- Modify as needed: `forwin/cli.py`

Extension:
- Modify: `browser_extension/forwin-publisher/lib/controller.js`
- Modify: `browser_extension/forwin-publisher/platform-agent.js`
- Modify: `browser_extension/forwin-publisher/tests/controller.test.js`
- Modify as needed: `browser_extension/forwin-publisher/tests/platforms.test.js`
- Rebuild: `browser_extension/dist/forwin-publisher-chromium/*`
- Rebuild: `browser_extension/dist/forwin-publisher-firefox/*`

UI:
- Modify: `forwin/ui_assets/home/body.html`
- Modify: `forwin/ui_assets/home/app_library.js`
- Modify: `forwin/ui_assets/home/app_task_governance.js`
- Modify: `forwin/ui_assets/publishers/app_uploads.js`

Tests:
- Create: `tests/test_publisher_runtime_bindings.py`
- Create: `tests/test_publisher_runtime_covers.py`
- Create: `tests/test_publisher_runtime_platform_catalogs.py`
- Create: `tests/test_publisher_runtime_preflight.py`
- Create: `tests/test_publisher_compliance_reviewer.py`
- Modify: `tests/test_publisher_runtime_upload_jobs.py`

## Task 1: Add Durable Publisher Models And Migration

**Files:**
- Modify: `forwin/models/publisher.py`
- Modify: `forwin/models/__init__.py`
- Modify: `forwin/models/base.py`
- Create: `forwin/migrations/versions/0018_publisher_bindings_covers.py`
- Create: `tests/test_publisher_runtime_bindings.py`

- [ ] **Step 1: Write model smoke tests**

Create `tests/test_publisher_runtime_bindings.py` with DB-backed tests that use the same pattern as `tests/test_publisher_runtime_upload_jobs.py`.

Test cases:
- `test_init_db_creates_publisher_binding_tables`
- `test_work_binding_unique_per_project_platform`
- `test_chapter_binding_unique_per_work_and_chapter_number`
- `test_cover_asset_can_be_selected_for_work_binding`
- `test_upload_job_defaults_to_chapter_upload_task_kind`

Minimum assertions:
```python
from sqlalchemy import select

from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.publisher import (
    PublisherChapterBinding,
    PublisherCoverAsset,
    PublisherUploadJob,
    PublisherWorkBinding,
)
from tests.postgres import postgres_test_url


def test_upload_job_defaults_to_chapter_upload_task_kind() -> None:
    engine = get_engine(postgres_test_url("publisher-task-kind-default"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            job = PublisherUploadJob(
                platform_id="qidian",
                book_name="Book",
                chapter_title="Chapter",
                body_text="Body",
            )
            session.add(job)
        with Session() as session:
            stored = session.execute(select(PublisherUploadJob)).scalar_one()
            assert stored.task_kind == "chapter_upload"
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run the tests and verify RED**

Run:
```bash
python3 -m pytest tests/test_publisher_runtime_bindings.py -q
```

Expected: fail because the binding and cover models do not exist and `PublisherUploadJob.task_kind` is missing.

- [ ] **Step 3: Add models**

In `forwin/models/publisher.py`:
- Add `task_kind` to `PublisherUploadJob`:
```python
task_kind: Mapped[str] = mapped_column(String, default="chapter_upload", nullable=False)
```
- Add `Index("ix_publisher_upload_jobs_task_status", "task_kind", "status", "platform_id")` to `PublisherUploadJob.__table_args__`.
- Add `PublisherWorkBinding`, `PublisherChapterBinding`, `PublisherCoverAsset`, and `PublisherMilestone`.

Use string JSON columns consistent with existing publisher models:
- `raw_payload_json: Text default "{}"`
- `score_reasons_json: Text default "[]"`
- `platform_validation_json: Text default "{}"`
- `evidence_json: Text default "{}"`

Use default states:
- work `audit_state="unknown"`
- work `cover_state="none"`
- chapter `publish_state="unknown"`
- chapter `audit_state="unknown"`
- cover `status="generated"`
- cover `selection_state="candidate"`
- milestone `state="open"`

- [ ] **Step 4: Register models**

In `forwin/models/__init__.py`, import and export:
- `PublisherWorkBinding`
- `PublisherChapterBinding`
- `PublisherCoverAsset`
- `PublisherMilestone`

- [ ] **Step 5: Add Alembic migration**

Create `forwin/migrations/versions/0018_publisher_bindings_covers.py`.

Use:
```python
revision = "0018_publisher_bindings_covers"
down_revision = "0017_generation_task_payload"
```

Migration must:
- add `publisher_upload_jobs.task_kind` with server default `"chapter_upload"`;
- create the four new tables;
- create indexes for project/platform lookup and job-kind lookup;
- create a partial unique index for `(project_id, platform_id)` where `project_id <> ''`;
- create a partial unique index for `(work_binding_id, chapter_number)` where `chapter_number > 0`;
- create a partial unique index for selected covers where `selection_state in ('selected', 'approved')`.

- [ ] **Step 6: Add baseline upgrader**

In `forwin/models/base.py`:
- Add `"publisher_bindings_covers_v1"` to `POSTGRES_BASELINE_MIGRATIONS`.
- Add `_upgrade_publisher_bindings_covers(conn)` and call it from `_upgrade_postgresql_database()`.
- The upgrader must be idempotent and include `ALTER TABLE ... ADD COLUMN IF NOT EXISTS task_kind VARCHAR NOT NULL DEFAULT 'chapter_upload'`.
- Create the same tables and indexes as the Alembic migration.

- [ ] **Step 7: Run model tests**

Run:
```bash
python3 -m pytest tests/test_publisher_runtime_bindings.py -q
```

Expected: pass.

## Task 2: Add Binding Service And Upload Result Upserts

**Files:**
- Create: `forwin/publisher_runtime/bindings.py`
- Modify: `forwin/publisher_runtime/service.py`
- Modify: `forwin/publisher_runtime/upload_jobs.py`
- Modify: `forwin/publishers/manager.py`
- Modify: `tests/test_publisher_runtime_upload_jobs.py`
- Modify: `tests/test_publisher_runtime_bindings.py`

- [ ] **Step 1: Add failing binding service tests**

Add tests:
- `test_upload_success_upserts_work_binding_from_remote_payload`
- `test_upload_success_upserts_chapter_binding`
- `test_verified_draft_upserts_chapter_binding_as_drafted`
- `test_upload_result_reuses_existing_work_binding_for_project_platform`

Use result payload keys the extension already returns or can return:
```python
result_payload={
    "remote_book_id": "book-123",
    "remote_work_id": "book-123",
    "remote_book_url": "https://write.qq.com/portal/book/123",
    "remote_chapter_id": "chapter-1",
    "chapter_number": 1,
    "official_status": "published",
    "audit_state": "under_review",
}
```

- [ ] **Step 2: Implement `PublisherBindingService`**

Create `forwin/publisher_runtime/bindings.py` with methods:
- `get_work_binding(project_id, platform_id, book_name="")`
- `upsert_work_binding_from_upload_job(job, result_payload, current_url)`
- `upsert_chapter_binding_from_upload_job(job, work_binding, result_payload)`
- `update_from_cover_upload_result(job, result_payload, current_url)`
- `update_from_audit_sync_result(job, result_payload, current_url)`
- `serialize_work_binding(binding)`
- `serialize_chapter_binding(binding)`
- `list_work_bindings(project_id="", platform_id="")`
- `list_chapter_bindings(work_binding_id="", project_id="", platform_id="")`

Normalization rules:
- Prefer `remote_book_id`, then `remote_work_id`, then `work_id`, then `book_id`.
- Prefer `remote_book_url`, then `work_url`, then `current_url` when it is a management or dashboard URL.
- Map `official_status in {"published", "submitted"}` to chapter `publish_state="published"` for publish jobs and `"drafted"` for draft jobs.
- Map platform messages containing review terms to `audit_state="under_review"`.
- Store complete extension evidence in `raw_payload_json`, but never add `body_text`.

- [ ] **Step 3: Wire service into runtime**

In `forwin/publisher_runtime/service.py`:
- instantiate `self.bindings = PublisherBindingService(session_factory=session_factory)`;
- pass `bindings=self.bindings` to `UploadJobService`.

In `UploadJobService.__init__`, accept `bindings`.

- [ ] **Step 4: Upsert on terminal upload result**

In `update_upload_job_result()`:
- For `task_kind == "chapter_upload"` and terminal success, call binding upserts.
- Also handle recovered Qidian draft success when status is terminal and `official_status == "drafted"`.
- Do not upsert bindings on failed non-recovered jobs.
- Include serialized `work_binding` and `chapter_binding` in the returned job `result_payload`.

- [ ] **Step 5: Preserve audit redaction**

Extend existing audit tests to assert:
- body text does not appear in binding raw payloads;
- `remote_book_id` and `remote_chapter_id` do appear in binding raw payloads.

- [ ] **Step 6: Run tests**

Run:
```bash
python3 -m pytest \
  tests/test_publisher_runtime_upload_jobs.py \
  tests/test_publisher_runtime_bindings.py \
  -q
```

Expected: pass.

## Task 3: Add Task Kind Support End To End

**Files:**
- Modify: `forwin/api_schema/publisher.py`
- Modify: `forwin/publisher_runtime/upload_jobs.py`
- Modify: `browser_extension/forwin-publisher/lib/controller.js`
- Modify: `browser_extension/forwin-publisher/tests/controller.test.js`
- Modify: `tests/test_publisher_runtime_upload_jobs.py`

- [ ] **Step 1: Add backend task-kind tests**

Tests:
- `test_claim_next_upload_job_does_not_return_cover_generate`
- `test_claim_next_upload_job_returns_cover_upload_and_audit_sync`
- `test_legacy_create_upload_job_returns_chapter_upload_task_kind`

Expected claim behavior:
- extension claims `chapter_upload`, `cover_upload`, and `audit_sync`;
- extension does not claim `cover_generate`;
- old create APIs do not need to pass `task_kind`.

- [ ] **Step 2: Update schemas and serializer**

In `PublisherUploadJobResponse`, change the default from `"upload"` to `"chapter_upload"`.

In `serialize_upload_job()`, include:
```python
"task_kind": str(job.task_kind or "chapter_upload"),
```

In `new_upload_job()`, accept `task_kind: str = "chapter_upload"` and write it to the model.

- [ ] **Step 3: Update claim filtering**

In `claim_next_upload_job()`, filter pending/running claimable kinds to:
```python
claimable_task_kinds = ("chapter_upload", "cover_upload", "audit_sync")
```

Keep platform filtering for all extension-claimed kinds.

- [ ] **Step 4: Dispatch task kinds in the extension controller**

In `browser_extension/forwin-publisher/lib/controller.js`:
- route `job.task_kind || 'chapter_upload'`;
- call existing chapter path only for `chapter_upload`;
- add `executeCoverUploadJobPayload(job, originTabId)`;
- add `executeAuditSyncJobPayload(job, originTabId)`;
- keep the existing status update format through `backend.updateUploadJobResult`.

Minimum dispatch:
```javascript
const taskKind = job.task_kind || 'chapter_upload';
if (taskKind === 'cover_upload') {
  return this.executeCoverUploadJobPayload(job, originTabId);
}
if (taskKind === 'audit_sync') {
  return this.executeAuditSyncJobPayload(job, originTabId);
}
return this.executeChapterUploadJobPayload(job, originTabId);
```

If the current method name is still `executeUploadJobPayload`, split it so the old public name delegates to the new dispatcher.

- [ ] **Step 5: Add controller tests**

In `browser_extension/forwin-publisher/tests/controller.test.js`:
- assert `cover_generate` is ignored by backend claim tests, if claim fixture exists;
- assert `cover_upload` calls `run-cover-upload`;
- assert `audit_sync` calls `run-audit-sync`;
- assert missing `task_kind` still calls `run-upload`.

- [ ] **Step 6: Run tests**

Run:
```bash
python3 -m pytest tests/test_publisher_runtime_upload_jobs.py -q
cd browser_extension/forwin-publisher && npm test
```

Expected: pass.

## Task 4: Add Platform Catalogs And Publisher Preflight

**Files:**
- Create: `forwin/publisher_runtime/platform_catalogs.py`
- Create: `forwin/publisher_runtime/preflight.py`
- Modify: `forwin/publisher_runtime/service.py`
- Modify: `forwin/publisher_runtime/upload_jobs.py`
- Create: `tests/test_publisher_runtime_platform_catalogs.py`
- Create: `tests/test_publisher_runtime_preflight.py`

- [ ] **Step 1: Write catalog tests**

Test known mappings:
- Qidian `audience="male"` resolves to the male publishing site payload.
- Qidian `audience="female"` resolves to the female publishing site payload.
- Qidian `primary_category="玄幻"` or existing ForWin equivalent resolves to Qidian primary category.
- Fanqie `audience="male"` resolves to `pindao=1`.
- Fanqie `audience="female"` resolves to `pindao=0`.
- Fanqie known genre metadata resolves to required category/tag names.
- Missing mapping returns a deterministic fallback and a warning, not random selection.

- [ ] **Step 2: Implement `PlatformMetadataCatalog`**

In `forwin/publisher_runtime/platform_catalogs.py`:
- define dataclasses or Pydantic-free plain dict helpers;
- expose `resolve_for_platform(platform_id, book_meta) -> dict`;
- include:
  - `resolved_audience`;
  - `resolved_primary_category`;
  - `resolved_subcategory`;
  - `resolved_theme_tags`;
  - `resolved_role_tags`;
  - `resolved_plot_tags`;
  - `warnings`;
  - `required_fields`.

Keep fallback mappings compatible with existing `platform-agent.js`.

- [ ] **Step 3: Attach resolved metadata to upload jobs**

In `new_upload_job()`:
- normalize `book_meta` as today;
- call the catalog resolver;
- store it under:
```python
payload["platform_meta"] = resolved_platform_meta
```

- [ ] **Step 4: Write preflight tests**

Test cases:
- Qidian missing `book_name` or `intro` hard-fails.
- Fanqie missing protagonist name hard-fails.
- Fanqie intro shorter than 50 or longer than 500 hard-fails.
- Missing category mapping returns a blocking error only when no deterministic fallback exists.
- A publisher compliance failure blocks if `publisher_compliance_required=True`.
- A publisher compliance warning does not block when warnings are allowed.
- Preflight does not emit sensitive-word replacement suggestions itself.

- [ ] **Step 5: Implement `PublisherPreflightService`**

Create `forwin/publisher_runtime/preflight.py`.

Return shape:
```python
{
    "ok": bool,
    "blocking": [ ... ],
    "warnings": [ ... ],
    "platform_meta": { ... },
    "requires_reviewer": bool,
}
```

Inputs:
- platform id;
- book name;
- chapter title/body metadata but not repair logic;
- `book_meta`;
- optional selected cover;
- optional latest publisher compliance verdict.

Validation:
- metadata presence;
- Fanqie intro and protagonist constraints;
- platform catalog resolution;
- selected cover readability when cover upload is required;
- reviewer verdict status when publisher compliance is required.

- [ ] **Step 6: Wire preflight into upload job creation**

In `UploadJobService.create_upload_job()`:
- run preflight before persisting the chapter upload job;
- for hard blockers, raise `ValueError` with a concise Chinese message and include details in API error;
- for warnings, store `preflight` in `result_payload_json`.

- [ ] **Step 7: Run tests**

Run:
```bash
python3 -m pytest \
  tests/test_publisher_runtime_platform_catalogs.py \
  tests/test_publisher_runtime_preflight.py \
  tests/test_publisher_runtime_upload_jobs.py \
  -q
```

Expected: pass.

## Task 5: Move Platform Text Compliance Into Reviewer

**Files:**
- Create: `forwin/reviewer/publisher_compliance.py`
- Modify: `forwin/reviewer/hub.py`
- Modify: `forwin/api_schema/project.py`
- Modify: `forwin/project_payloads/runtime_maps.py`
- Create: `tests/test_publisher_compliance_reviewer.py`

- [ ] **Step 1: Write reviewer tests**

Test cases:
- hard sensitive terms produce `publisher_compliance` errors;
- external contact or promotional patterns produce platform-compliance issues;
- uncertain risky words can be warnings;
- issue metadata is repair-friendly: `rule_name`, `severity`, `description`, and `evidence_refs` are populated;
- `HistoricalReviewHub` merges publisher compliance issues into the final verdict when enabled;
- hub output is unchanged when publisher compliance is disabled.

- [ ] **Step 2: Implement `PublisherComplianceReviewer`**

Create `forwin/reviewer/publisher_compliance.py`.

Class responsibilities:
- accept `platform_ids: list[str]`;
- scan title, intro, chapter body, and optional metadata;
- emit normal `ReviewVerdict` and `ContinuityIssue`;
- use `rule_name` values prefixed with `publisher_compliance_`;
- use evidence refs such as `publisher:qidian:body:term` or `publisher:fanqie:intro:length`.

Initial deterministic rules:
- phone/contact patterns;
- external link patterns;
- obvious promotional CTA patterns;
- configurable hard term list for content that platform upload should not auto-submit;
- Fanqie intro length/protagonist metadata warnings can be emitted here only when the checked text is part of draft-facing content; final form readiness stays in preflight.

- [ ] **Step 3: Wire into `HistoricalReviewHub`**

In `forwin/reviewer/hub.py`:
- instantiate the reviewer behind a flag `publisher_compliance_review_enabled`;
- call it during `review()`;
- merge issues into the same `ReviewVerdict` list used by existing repair paths;
- record a performance span if the hub already wraps other reviewers in spans;
- keep disabled behavior byte-for-byte equivalent where possible.

- [ ] **Step 4: Add project automation settings**

In `forwin/api_schema/project.py`, add fields to `ProjectAutomationPublishSettings`:
```python
cover_generation_enabled: bool = True
cover_confirmation_required: bool = False
cover_candidate_count: int = 4
cover_style_hint: str = ""
auto_cover_upload_enabled: bool = True
publisher_compliance_required: bool = True
```

In `forwin/project_payloads/runtime_maps.py`, normalize them with:
- candidate count clamped to `1..8`;
- confirmation default `False`;
- generation and auto-upload default `True`;
- compliance required default `True`.

- [ ] **Step 5: Run reviewer tests**

Run:
```bash
python3 -m pytest tests/test_publisher_compliance_reviewer.py -q
```

Expected: pass.

## Task 6: Add MiniMax Cover Generation And Selection

**Files:**
- Create: `forwin/publisher_runtime/covers.py`
- Modify: `forwin/publisher_runtime/service.py`
- Modify: `forwin/publisher_runtime/upload_jobs.py`
- Create: `tests/test_publisher_runtime_covers.py`
- Modify: `tests/test_publisher_runtime_upload_jobs.py`

- [ ] **Step 1: Write cover service tests with a fake MiniMax client**

Test cases:
- `test_cover_generation_stores_multiple_candidates`
- `test_cover_generation_selects_first_valid_when_scoring_fails`
- `test_cover_generation_prefers_highest_valid_score`
- `test_invalid_cover_is_not_selected_when_valid_candidate_exists`
- `test_cover_generation_job_marks_failed_when_no_valid_candidates`
- `test_selected_cover_is_enqueued_for_upload_after_first_chapter_success`
- `test_cover_generation_completion_enqueues_cover_upload_when_first_chapter_already_succeeded`

Do not call the real MiniMax API in tests.

- [ ] **Step 2: Implement image client boundary**

In `forwin/publisher_runtime/covers.py`, define:
- `MiniMaxImageClient`
- `CoverCandidateInput`
- `CoverGenerationResult`
- `PublisherCoverService`

Use `httpx.Client` or injectable transport.

MiniMax request:
- base URL from `Config.minimax_base_url`;
- API key from `Config.minimax_api_key`;
- endpoint path `/image_generation`;
- model default `"image-01"` for image generation, not the text model default;
- request `n=cover_candidate_count`;
- prefer base64 response;
- if provider returns URLs, download bytes and persist locally.

Do not log the API key, raw prompt with secrets, or response bodies containing image data.

- [ ] **Step 3: Persist assets**

Use a runtime data directory under the configured ForWin data root or a deterministic fallback:
```text
var/publisher_covers/{project_id}/{platform_id}/{cover_asset_id}.png
```

Store:
- file path;
- MIME type;
- file size;
- width;
- height;
- prompt;
- source meta;
- MiniMax request id when present.

Use Pillow only if already available. If Pillow is not available, use PNG/JPEG header parsing for width/height and keep tests focused on PNG/JPEG fixtures. Do not add a new heavyweight dependency just for first-pass validation.

- [ ] **Step 4: Validate and score**

Validation:
- readable PNG or JPEG;
- non-empty file;
- known dimensions;
- platform-specific size/dimension rules only when confirmed by catalog config;
- unknown public spec values are warnings, not hard failures.

Scoring:
- category match signals;
- intro/title/protagonist prompt coverage signals;
- obvious unsafe image metadata is a hard penalty if detected locally;
- if scoring raises, choose the first valid candidate.

Selection:
- mark exactly one candidate `selection_state="selected"` and `status="selected"`;
- leave previous selected rows as history by changing old rows to `selection_state="candidate"` unless manually approved.

- [ ] **Step 5: Enqueue cover generation on upload creation**

In `UploadJobService.create_upload_job()`:
- when `task_kind="chapter_upload"`;
- `create_if_missing=True`;
- cover generation enabled;
- no selected usable cover exists;
- create a `PublisherUploadJob` with `task_kind="cover_generate"`.

Payload:
```python
{
    "project_id": project_id,
    "book_name": book_name,
    "platform": platform,
    "book_meta": normalized_book_meta,
    "cover_candidate_count": count,
    "cover_confirmation_required": bool,
    "auto_cover_upload_enabled": bool,
}
```

- [ ] **Step 6: Add cover upload enqueue rules**

Rules:
- If first chapter succeeds and selected cover exists and auto upload is enabled, enqueue `cover_upload`.
- If first chapter succeeds but cover generation is still pending, cover generation completion later enqueues `cover_upload`.
- If manual confirmation is required, do not enqueue until user approves/selects a cover.
- Cover upload job payload must include `work_binding_id`, `cover_asset_id`, `remote_book_id`, `remote_url`, and `file_path`.

- [ ] **Step 7: Run cover tests**

Run:
```bash
python3 -m pytest \
  tests/test_publisher_runtime_covers.py \
  tests/test_publisher_runtime_upload_jobs.py \
  tests/test_publisher_runtime_bindings.py \
  -q
```

Expected: pass.

## Task 7: Add Backend Publisher Job Runner

**Files:**
- Create: `forwin/publisher_runtime/backend_jobs.py`
- Modify: `forwin/publisher_runtime/service.py`
- Modify: `forwin/api_core/automation.py`
- Modify as needed: `forwin/cli.py`
- Create or extend: `tests/test_publisher_runtime_covers.py`

- [ ] **Step 1: Add runner tests**

Test cases:
- `test_backend_runner_claims_only_cover_generate_jobs`
- `test_backend_runner_marks_cover_generate_running_then_succeeded`
- `test_backend_runner_records_failure_without_affecting_chapter_job`
- `test_automation_scheduler_can_process_one_pending_cover_generate_job`

- [ ] **Step 2: Implement `PublisherBackendJobRunner`**

In `forwin/publisher_runtime/backend_jobs.py`:
- claim pending `PublisherUploadJob` rows with `task_kind="cover_generate"`;
- set `status="running"`, `started_at`, and `extension_client_id="backend"`;
- call `PublisherCoverService.generate_for_job(job_id)`;
- write terminal result through a backend-safe method, not extension auth;
- leave chapter jobs untouched.

Concurrency:
- use row-level locking where practical:
```python
select(PublisherUploadJob)
    .where(PublisherUploadJob.task_kind == "cover_generate")
    .where(PublisherUploadJob.status == "pending")
    .with_for_update(skip_locked=True)
```

- [ ] **Step 3: Wire runner into runtime**

In `PublisherRuntimeService`, instantiate:
```python
self.cover_service = PublisherCoverService(...)
self.backend_jobs = PublisherBackendJobRunner(...)
```

Pass config values needed by MiniMax and file storage from the same runtime construction path used by publisher services.

- [ ] **Step 4: Add automation scheduler hook**

In `forwin/api_core/automation.py`, after the existing production scheduler pass, call a small publisher maintenance pass when runtime exists:
```python
runtime.publisher_manager.runtime.backend_jobs.run_pending_once(limit=1)
```

Use the real container access pattern in the file; do not introduce a parallel global singleton.

Failures must be logged and swallowed like existing scheduler pass failures, because cover generation failure is not generation failure.

- [ ] **Step 5: Add optional CLI command**

In `forwin/cli.py`, add:
```bash
forwin publisher-worker --once
```

It should call the same backend runner. This is for manual recovery and tests, not a required new daemon.

- [ ] **Step 6: Run runner tests**

Run:
```bash
python3 -m pytest tests/test_publisher_runtime_covers.py -q
```

Expected: pass.

## Task 8: Add Publisher APIs

**Files:**
- Modify: `forwin/api_schema/publisher.py`
- Modify: `forwin/api_publisher_ops.py`
- Modify: `forwin/api_publisher_routes.py`
- Modify: `forwin/api_route_registry.py`
- Modify: `forwin/publishers/manager.py`
- Create or extend backend API tests if this repo already has publisher route tests.

- [ ] **Step 1: Add response/request schemas**

Add:
- `PublisherWorkBindingResponse`
- `PublisherChapterBindingResponse`
- `PublisherCoverAssetResponse`
- `PublisherCoverGenerateRequest`
- `PublisherCoverSelectRequest`
- `PublisherCoverUploadRequest`
- `PublisherAuditSyncRequest`
- `PublisherPreflightResponse`

Use existing Pydantic style and default empty strings/lists/dicts.

- [ ] **Step 2: Add manager methods**

In `PublisherManager`:
- `list_work_bindings(project_id="", platform="")`
- `list_chapter_bindings(project_id="", platform="", work_binding_id="")`
- `list_cover_assets(project_id="", platform="", work_binding_id="")`
- `generate_cover_candidates(...)`
- `select_cover_asset(...)`
- `approve_cover_asset(...)`
- `reject_cover_asset(...)`
- `enqueue_cover_upload(...)`
- `enqueue_audit_sync(...)`
- `get_preflight(...)`

- [ ] **Step 3: Add ops handlers**

In `forwin/api_publisher_ops.py`, add handler functions that:
- validate extension auth only for extension result endpoints, not for normal user APIs;
- convert manager exceptions to `HTTPException`;
- return typed schema responses.

- [ ] **Step 4: Register routes**

Add routes near existing publisher routes:
```text
GET  /api/publishers/work-bindings
GET  /api/publishers/chapter-bindings
GET  /api/publishers/covers
POST /api/publishers/covers/generate
POST /api/publishers/covers/{cover_asset_id}/select
POST /api/publishers/covers/{cover_asset_id}/approve
POST /api/publishers/covers/{cover_asset_id}/reject
POST /api/publishers/covers/{cover_asset_id}/upload
POST /api/publishers/audit-sync
POST /api/publishers/preflight
```

- [ ] **Step 5: Run API/schema checks**

Run:
```bash
python3 -m pytest \
  tests/test_publisher_runtime_bindings.py \
  tests/test_publisher_runtime_covers.py \
  tests/test_publisher_runtime_preflight.py \
  -q
python3 -m compileall forwin
```

Expected: pass.

## Task 9: Add Extension Cover Upload And Audit Sync

**Files:**
- Modify: `browser_extension/forwin-publisher/platform-agent.js`
- Modify: `browser_extension/forwin-publisher/lib/controller.js`
- Modify: `browser_extension/forwin-publisher/tests/controller.test.js`
- Modify: `browser_extension/forwin-publisher/tests/platforms.test.js`

- [ ] **Step 1: Add extension tests**

Tests:
- `run-cover-upload` receives file path, work id/url, and returns normalized `cover_state`.
- `run-audit-sync` returns normalized work, chapter, and cover audit payloads.
- Existing `run-upload` behavior is unchanged.
- Controller posts result payloads with `task_kind` preserved.

- [ ] **Step 2: Add platform-agent actions**

In the message listener, add:
- `run-cover-upload`
- `run-audit-sync`

Implement:
```javascript
async function runCoverUpload(payload) { ... }
async function runAuditSync(payload) { ... }
```

The first implementation should:
- navigate to the known dashboard/book info URL when `remote_url` exists;
- otherwise reuse existing dashboard search helpers by `book_name`;
- upload from `file_path`;
- wait for platform success/review text;
- return normalized payload:
```javascript
{
  ok: true,
  currentUrl,
  message,
  resultPayload: {
    task_kind: 'cover_upload',
    cover_state: 'under_review',
    remote_book_id,
    remote_url,
    platform_message,
  },
}
```

- [ ] **Step 3: Fanqie cover upload path**

Use existing Fanqie dashboard/book helpers first. Required behavior:
- find book info/manage page;
- locate cover upload/change control;
- set file input with the local file;
- wait for upload/review indicator;
- return `cover_state` as `uploaded`, `under_review`, `approved`, `rejected`, or `failed`.

If the DOM selector is uncertain, implement a selector ladder with structured error payload:
```javascript
{
  errorCode: 'cover-upload-control-not-found',
  resultPayload: { phase: 'find-cover-upload-control', currentUrl }
}
```

- [ ] **Step 4: Qidian cover upload path**

Use existing Qidian dashboard helpers first. Required behavior:
- find work entry by binding URL, remote id, or book name;
- open work management/book info page;
- upload local file through cover control;
- return normalized state and page evidence.

- [ ] **Step 5: Audit sync path**

Implement platform-specific audit scraping using existing helpers:
- work audit state;
- chapter audit/publish state;
- cover audit state;
- signing/revenue entry visibility as milestone evidence.

Return:
```javascript
{
  work: {...},
  chapters: [...],
  cover: {...},
  milestones: [...],
}
```

- [ ] **Step 6: Run extension tests and build**

Run:
```bash
cd browser_extension/forwin-publisher && npm test
cd browser_extension/forwin-publisher && npm run build
```

Expected: pass and dist directories updated.

## Task 10: Add UI Controls And Status Surfaces

**Files:**
- Modify: `forwin/ui_assets/home/body.html`
- Modify: `forwin/ui_assets/home/app_library.js`
- Modify: `forwin/ui_assets/home/app_task_governance.js`
- Modify: `forwin/ui_assets/publishers/app_uploads.js`

- [ ] **Step 1: Add automation settings fields**

In the project automation publishing settings UI:
- cover generation enabled toggle;
- cover confirmation required toggle default off;
- cover candidate count number input default `4`;
- cover style hint text input;
- auto cover upload enabled toggle default on;
- publisher compliance required toggle default on.

The payload must match `ProjectAutomationPublishSettings`.

- [ ] **Step 2: Add upload modal controls**

In the manual upload modal:
- optional cover generation toggle;
- candidate count;
- confirmation required;
- style hint;
- show preflight warnings before enqueue;
- do not block on warnings unless preflight marks blocking.

- [ ] **Step 3: Add task detail/status display**

In publisher upload UI:
- show `task_kind`;
- show work binding remote id/url;
- show chapter binding remote id/url;
- show audit state;
- show selected cover state;
- show preflight warnings/blockers;
- link compliance failures to review/repair when the payload contains reviewer issue metadata.

- [ ] **Step 4: Add cover candidate actions**

For a project/platform:
- list cover candidates;
- show selected/approved state;
- select;
- approve;
- reject;
- regenerate;
- enqueue upload;
- enqueue audit sync.

Keep the UI compact and operational. Do not redesign the whole page.

- [ ] **Step 5: Manual browser check**

Start the app as usual for this repo, open the project UI, and verify:
- upload modal still submits old chapter jobs;
- new settings persist through refresh;
- cover candidate actions call the intended endpoints;
- long Chinese title/intro text does not overflow controls.

## Task 11: Wire Auto Publish Creation Path

**Files:**
- Modify: `forwin/production/executor.py`
- Modify: `forwin/api_core/generation.py`
- Modify as needed: `forwin/api_schema/project.py`
- Modify tests around automated publish enqueue if present.

- [ ] **Step 1: Extend auto publish payload**

When generation enqueues publish jobs, pass:
- `cover_generation_enabled`;
- `cover_confirmation_required`;
- `cover_candidate_count`;
- `cover_style_hint`;
- `auto_cover_upload_enabled`;
- `publisher_compliance_required`.

Keep current `book_meta` derivation intact.

- [ ] **Step 2: Preserve existing generation behavior**

If `publish.enabled` is false, do nothing.

If publishing is enabled but cover generation fails, chapter production should still complete and the chapter upload job should remain queued.

- [ ] **Step 3: Add/extend tests**

Add tests that assert generated chapter publish enqueue includes cover settings and that missing settings use approved defaults.

Run:
```bash
python3 -m pytest \
  tests/test_publisher_runtime_upload_jobs.py \
  tests/test_publisher_runtime_covers.py \
  -q
```

Expected: pass.

## Task 12: End-To-End Verification

- [ ] **Step 1: Run focused backend suite**

Run:
```bash
python3 -m pytest \
  tests/test_publisher_runtime_upload_jobs.py \
  tests/test_publisher_runtime_bindings.py \
  tests/test_publisher_runtime_covers.py \
  tests/test_publisher_runtime_platform_catalogs.py \
  tests/test_publisher_runtime_preflight.py \
  tests/test_publisher_compliance_reviewer.py \
  -q
```

- [ ] **Step 2: Run extension suite**

Run:
```bash
cd browser_extension/forwin-publisher && npm test
```

- [ ] **Step 3: Run legacy gate**

Run:
```bash
python3 scripts/audit_legacy_inventory.py --strict
```

- [ ] **Step 4: Run compile check**

Run:
```bash
python3 -m compileall forwin
```

- [ ] **Step 5: Authenticated browser verification**

Use the extension in the browser where platform login is available.

Qidian:
- create/find work;
- publish or save first chapter;
- verify work binding has remote id/url;
- verify chapter binding has remote chapter id or draft URL evidence;
- upload selected cover after first chapter success;
- run audit sync and confirm work/chapter/cover audit state updates.

Fanqie:
- create/find work with channel/category/tag/protagonist metadata;
- publish or save first chapter;
- verify work binding has remote id/url;
- verify chapter binding status;
- upload selected cover after first chapter success;
- run audit sync and confirm "book info under review" or equivalent state is normalized.

Browser assistance note:
- Firefox is fine for manual verification if the extension package is built for Firefox.
- Chrome/Chromium is only needed if debugging platform DOM with Chromium-specific tooling or if the extension target behaves differently.

## Completion Checklist

- [ ] Old chapter upload jobs still work without new fields.
- [ ] `PublisherWorkBinding` and `PublisherChapterBinding` are created from successful upload results.
- [ ] `remote_book_id`, `remote_url`, `remote_chapter_id`, audit state, and raw evidence are durable.
- [ ] `cover_generate` jobs are backend-owned and not claimed by the extension.
- [ ] MiniMax cover generation creates multiple stored candidates.
- [ ] One valid cover is selected automatically by default.
- [ ] Manual select/approve/reject/regenerate controls exist.
- [ ] `cover_upload` is enqueued after first chapter success when auto upload is enabled.
- [ ] Cover upload failure does not alter chapter success.
- [ ] `audit_sync` updates work/chapter/cover states and milestones.
- [ ] Publisher compliance issues are emitted by reviewer and can feed repair.
- [ ] Publisher preflight checks metadata, mapping, cover, binding, and reviewer state without duplicating repair logic.
- [ ] UI shows task kind, binding, cover, preflight, and audit state.
- [ ] Backend pytest suite passes.
- [ ] Extension npm suite passes.
- [ ] Legacy inventory strict gate passes.
