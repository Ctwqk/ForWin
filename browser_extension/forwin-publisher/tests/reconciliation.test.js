import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildAttemptResult,
  buildExecutionReceipt,
  buildReconciliationRequest,
  extractRemoteIdentity,
} from '../lib/reconciliation.js';

const HASH = 'a'.repeat(64);
const OBSERVED_AT = '2026-07-21T21:00:00.000Z';

function chapterJob(overrides = {}) {
  return {
    job_id: 'job-1',
    idempotency_key: 'publisher-job:v1:job-1',
    task_kind: 'chapter_upload',
    platform: 'qidian',
    content_sha256: HASH,
    input: {
      book_name: 'Book',
      chapter_title: 'Chapter 1',
      body: 'Body',
      publish: false,
      create_if_missing: false,
    },
    ...overrides,
  };
}

test('extractRemoteIdentity keeps stable qidian ccid and book identity', () => {
  assert.deepEqual(
    extractRemoteIdentity(
      'qidian',
      'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222?entry=publish#ccid=333',
    ),
    {
      remote_book_id: '222',
      remote_chapter_id: '333',
      remote_url: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222?ccid=333',
    },
  );
});

test('extractRemoteIdentity reads stable fanqie book and chapter identity', () => {
  assert.deepEqual(
    extractRemoteIdentity(
      'fanqie',
      'https://fanqienovel.com/main/writer/chapter-edit/222/333?token=volatile',
    ),
    {
      remote_book_id: '222',
      remote_chapter_id: '333',
      remote_url: 'https://fanqienovel.com/main/writer/chapter-edit/222/333?chapter_id=333',
    },
  );
});

test('extractRemoteIdentity reads a qidian remote book page without inventing a chapter', () => {
  assert.deepEqual(
    extractRemoteIdentity('qidian', 'https://write.qq.com/portal/book/222?token=volatile'),
    {
      remote_book_id: '222',
      remote_chapter_id: '',
      remote_url: 'https://write.qq.com/portal/book/222',
    },
  );
});

test('buildExecutionReceipt binds a successful mutation to exact content and remote identity', () => {
  const receipt = buildExecutionReceipt({
    job: chapterJob(),
    result: {
      ok: true,
      currentUrl: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222#ccid=333',
      message: 'Saved',
      resultPayload: {
        official_status: 'drafted',
        verified_via: 'chapter-page',
        observed_content_sha256: HASH,
        expected_normalized_sha256: 'd'.repeat(64),
        observed_normalized_sha256: 'd'.repeat(64),
        content_match_basis: 'normalized-editor-text-sha256',
      },
    },
    observedAt: OBSERVED_AT,
  });

  assert.equal(receipt.content_sha256, HASH);
  assert.equal(receipt.remote_book_id, '222');
  assert.equal(receipt.remote_chapter_id, '333');
  assert.equal(receipt.official_state, 'drafted');
  assert.equal(receipt.evidence.content_sha256, HASH);
  assert.equal(receipt.evidence.matched, true);
});

test('buildExecutionReceipt rejects chapter success without stable remote identity', () => {
  assert.throws(
    () => buildExecutionReceipt({
      job: chapterJob(),
      result: { ok: true, currentUrl: 'https://write.qq.com/portal/dashboard' },
      observedAt: OBSERVED_AT,
    }),
    /stable remote book and chapter identity/,
  );
});

test('buildExecutionReceipt rejects chapter success without observed content proof', () => {
  assert.throws(
    () => buildExecutionReceipt({
      job: chapterJob(),
      result: {
        ok: true,
        currentUrl: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222#ccid=333',
        message: 'Saved',
      },
      observedAt: OBSERVED_AT,
    }),
    /observed content evidence matching the claim/,
  );
});

test('buildExecutionReceipt binds cover acceptance to the claimed remote book', () => {
  const receipt = buildExecutionReceipt({
    job: chapterJob({
      task_kind: 'cover_upload',
      content_sha256: 'c'.repeat(64),
      input: {
        book_name: 'Book',
        work_binding_id: 'binding-1',
        remote_book_id: '222',
        cover_asset_id: 'cover-1',
        file_path: '/tmp/cover.png',
      },
    }),
    result: {
      ok: true,
      currentUrl: 'https://write.qq.com/portal/book/222',
      message: 'Cover accepted',
      resultPayload: {
        cover_state: 'under_review',
        platform_message: 'Waiting for review',
        acceptance_signal: '提交成功',
      },
    },
    observedAt: OBSERVED_AT,
  });

  assert.equal(receipt.remote_book_id, '222');
  assert.equal(receipt.official_state, 'cover_uploaded');
  assert.equal(receipt.evidence.content_sha256, 'c'.repeat(64));
  assert.equal(receipt.evidence.confirmation_text, 'Cover accepted');
  assert.equal(receipt.evidence.platform_message, 'Waiting for review');
});

test('buildExecutionReceipt rejects cover success without exact platform acceptance proof', () => {
  const job = chapterJob({
    task_kind: 'cover_upload',
    content_sha256: 'c'.repeat(64),
    input: {
      book_name: 'Book',
      work_binding_id: 'binding-1',
      remote_book_id: '222',
      cover_asset_id: 'cover-1',
      file_path: '/tmp/cover.png',
    },
  });

  assert.throws(
    () => buildExecutionReceipt({
      job,
      result: {
        ok: true,
        currentUrl: 'https://write.qq.com/portal/book/222',
        message: 'Cover action submitted',
      },
      observedAt: OBSERVED_AT,
    }),
    /explicit platform acceptance evidence/,
  );
  assert.throws(
    () => buildExecutionReceipt({
      job,
      result: {
        ok: true,
        currentUrl: 'https://write.qq.com/portal/book/999',
        acceptanceSignal: '提交成功',
      },
      observedAt: OBSERVED_AT,
    }),
    /observed remote book identity to match/,
  );
});

test('buildAttemptResult emits task-typed result details', () => {
  assert.deepEqual(
    buildAttemptResult({
      job: chapterJob(),
      result: { ok: true, message: 'Saved', currentUrl: 'https://example.test' },
    }).details,
    {},
  );

  const cover = buildAttemptResult({
    job: chapterJob({ task_kind: 'cover_upload' }),
    result: {
      ok: true,
      resultPayload: {
        cover_state: 'uploaded',
        audit_state: 'under_review',
        platform_message: 'Accepted',
        ignored: 'not part of the DTO',
      },
    },
  });
  assert.deepEqual(cover.details, {
    cover_state: 'uploaded',
    audit_state: 'under_review',
    platform_message: 'Accepted',
  });

  const failed = buildAttemptResult({
    job: chapterJob(),
    result: { ok: false, errorCode: 'publish-failed', error: 'No confirmation' },
  });
  assert.equal(failed.outcome, 'failed');
  assert.equal(failed.error_code, 'publish-failed');
  assert.equal(failed.error_message, 'No confirmation');
});

test('matched reconciliation carries a conditional durable receipt', () => {
  const request = buildReconciliationRequest({
    job: chapterJob(),
    observedAt: OBSERVED_AT,
    observation: {
      outcome: 'matched',
      currentUrl: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222#ccid=333',
      matchedContentSha256: HASH,
      officialState: 'drafted',
      confirmationText: 'Saved',
      matchBasis: ['chapter_title', 'content_sha256'],
    },
  });

  assert.equal(request.outcome, 'matched');
  assert.equal(request.evidence.matched_content_sha256, HASH);
  assert.equal(request.receipt.remote_book_id, '222');
  assert.equal(request.receipt.remote_chapter_id, '333');
  assert.equal(request.receipt.content_sha256, HASH);
});

test('cover reconciliation stays indeterminate without a remote asset hash', () => {
  const job = chapterJob({
    task_kind: 'cover_upload',
    content_sha256: 'c'.repeat(64),
    input: {
      book_name: 'Book',
      work_binding_id: 'binding-1',
      remote_book_id: '222',
      cover_asset_id: 'cover-1',
      file_path: '/tmp/cover.png',
    },
  });
  const matched = buildReconciliationRequest({
    job,
    observedAt: OBSERVED_AT,
    observation: {
      outcome: 'matched',
      currentUrl: 'https://write.qq.com/portal/book/222',
      remoteBookId: '222',
      matchedContentSha256: 'c'.repeat(64),
      officialState: 'cover_uploaded',
      acceptanceSignal: '审核中',
      matchBasis: ['remote_book_id', 'local_file_sha256', 'platform_acceptance'],
    },
  });
  assert.equal(matched.outcome, 'indeterminate');
  assert.equal(matched.receipt, undefined);
  assert.equal(matched.error_code, 'cover-asset-hash-unobservable');

  const wrongBook = buildReconciliationRequest({
    job,
    observedAt: OBSERVED_AT,
    observation: {
      outcome: 'matched',
      currentUrl: 'https://write.qq.com/portal/book/999',
      remoteBookId: '999',
      matchedContentSha256: 'c'.repeat(64),
      acceptanceSignal: '审核中',
      matchBasis: ['remote_book_id', 'local_file_sha256', 'platform_acceptance'],
    },
  });
  assert.equal(wrongBook.outcome, 'indeterminate');
  assert.equal(wrongBook.receipt, undefined);
  assert.equal(wrongBook.error_code, 'cover-asset-hash-unobservable');
});

test('content hash mismatch remains indeterminate and cannot mint a receipt', () => {
  const request = buildReconciliationRequest({
    job: chapterJob(),
    observedAt: OBSERVED_AT,
    observation: {
      outcome: 'matched',
      currentUrl: 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222#ccid=333',
      matchedContentSha256: 'b'.repeat(64),
      officialState: 'drafted',
    },
  });

  assert.equal(request.outcome, 'indeterminate');
  assert.equal(request.receipt, undefined);
  assert.equal(request.error_code, 'content-hash-mismatch');
});

test('absence remains indeterminate because claim does not expose backend authority', () => {
  const observation = {
    outcome: 'absent',
    currentUrl: 'https://fanqienovel.com/main/writer/chapter-manage/222?type=2',
    paginationComplete: true,
    reason: 'Scanned all pages',
  };
  const job = chapterJob({ platform: 'fanqie' });

  const disabled = buildReconciliationRequest({ job, observation, observedAt: OBSERVED_AT });
  assert.equal(disabled.outcome, 'indeterminate');
  assert.match(disabled.evidence.reason, /not enabled/);
  assert.equal(disabled.evidence.pagination_complete, true);
  assert.equal(disabled.receipt, undefined);
});

test('indeterminate and risk pause observations remain receipt-free', () => {
  const indeterminate = buildReconciliationRequest({
    job: chapterJob(),
    observedAt: OBSERVED_AT,
    observation: { outcome: 'indeterminate', reason: 'Page did not expose body text' },
  });
  assert.equal(indeterminate.outcome, 'indeterminate');
  assert.equal(indeterminate.receipt, undefined);

  const paused = buildReconciliationRequest({
    job: chapterJob(),
    observedAt: OBSERVED_AT,
    observation: {
      outcome: 'risk_pause',
      riskReason: 'account_risk',
      reason: 'Account verification required',
    },
  });
  assert.equal(paused.outcome, 'risk_pause');
  assert.equal(paused.evidence.risk_reason, 'account_risk');
  assert.equal(paused.evidence.reason, 'Account verification required');
  assert.equal(paused.receipt, undefined);

  const untypedPause = buildReconciliationRequest({
    job: chapterJob(),
    observedAt: OBSERVED_AT,
    observation: { outcome: 'risk_pause', reason: 'Unknown challenge' },
  });
  assert.equal(untypedPause.outcome, 'indeterminate');
  assert.equal(untypedPause.error_code, 'risk-reason-invalid');
});
