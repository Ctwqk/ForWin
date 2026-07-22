import test from 'node:test';
import assert from 'node:assert/strict';

import { createUploadJournal } from '../lib/upload-journal.js';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function uploadClaim(overrides = {}) {
  const claim = {
    execution_mode: 'execute',
    job: {
      job_id: 'job-1',
      idempotency_key: 'publisher-job:v1:job-1',
      task_kind: 'chapter_upload',
      platform: 'qidian',
      content_sha256: 'a'.repeat(64),
      input: {
        book_name: 'Book',
        chapter_title: 'Chapter 1',
        body: 'body',
        publish: false,
      },
    },
    attempt: {
      attempt_id: 'attempt-1',
      attempt_number: 1,
      lease_epoch: 7,
      phase: 'claimed',
      lease_expires_at: '2026-07-21T12:01:30Z',
      heartbeat_interval_seconds: 30,
    },
  };
  return {
    ...claim,
    ...overrides,
    job: { ...claim.job, ...(overrides.job || {}) },
    attempt: { ...claim.attempt, ...(overrides.attempt || {}) },
  };
}

function uploadReceipt(overrides = {}) {
  return {
    content_sha256: 'a'.repeat(64),
    remote_book_id: 'remote-book-1',
    remote_chapter_id: 'remote-chapter-1',
    remote_url: 'https://write.qq.com/chapter/1',
    official_state: 'drafted',
    observed_at: '2026-07-21T12:02:00Z',
    evidence: {
      heading: 'Chapter 1',
      content_sha256: 'a'.repeat(64),
    },
    ...overrides,
  };
}

function uploadResult(overrides = {}) {
  return {
    outcome: 'succeeded',
    message: 'saved',
    current_url: 'https://write.qq.com/chapter/1',
    error_code: '',
    error_message: '',
    details: {},
    ...overrides,
  };
}

function memoryStore(initialValue) {
  let value = initialValue == null ? initialValue : structuredClone(initialValue);
  let writes = 0;
  return {
    read: async () => (value == null ? value : structuredClone(value)),
    write: async (next) => {
      writes += 1;
      value = structuredClone(next);
    },
    get value() {
      return value == null ? value : structuredClone(value);
    },
    get writes() {
      return writes;
    },
  };
}

test('recordClaim persists the new record before its promise resolves', async () => {
  const writeStarted = deferred();
  const allowWrite = deferred();
  let stored;
  let attemptedSnapshot;
  const journal = createUploadJournal({
    read: async () => stored,
    write: async (snapshot) => {
      attemptedSnapshot = structuredClone(snapshot);
      writeStarted.resolve();
      await allowWrite.promise;
      stored = structuredClone(snapshot);
    },
    now: () => '2026-07-21T12:00:00Z',
  });

  let settled = false;
  const recording = journal.recordClaim({
    clientId: 'extension-1',
    claim: uploadClaim(),
  });
  recording.then(() => {
    settled = true;
  });

  await writeStarted.promise;
  await Promise.resolve();
  assert.equal(settled, false);
  assert.equal(attemptedSnapshot.records[0].local_phase, 'claimed');
  assert.equal(stored, undefined);

  allowWrite.resolve();
  const record = await recording;
  assert.equal(record.client_id, 'extension-1');
  assert.equal(record.execution_mode, 'execute');
  assert.deepEqual(record.job, uploadClaim().job);
  assert.deepEqual(record.attempt, uploadClaim().attempt);
  assert.equal(record.local_phase, 'claimed');
  assert.equal(record.receipt, null);
  assert.equal(record.result, null);
  assert.equal(record.created_at, '2026-07-21T12:00:00Z');
  assert.equal(record.updated_at, '2026-07-21T12:00:00Z');
  assert.equal(record.acked_at, null);
  assert.equal(stored.version, 1);
  assert.deepEqual(stored.records[0], record);
});

test('recordClaim is idempotent and rejects fence or content conflicts', async () => {
  const store = memoryStore();
  const journal = createUploadJournal({
    read: store.read,
    write: store.write,
    now: () => '2026-07-21T12:00:00Z',
  });
  const claim = uploadClaim();

  const first = await journal.recordClaim({ clientId: 'extension-1', claim });
  const repeated = await journal.recordClaim({
    clientId: 'extension-1',
    claim: structuredClone(claim),
  });

  assert.deepEqual(repeated, first);
  assert.equal(store.writes, 1);
  assert.equal((await journal.load()).length, 1);
  assert.deepEqual(await journal.get('attempt-1'), first);

  await assert.rejects(
    journal.recordClaim({
      clientId: 'extension-1',
      claim: uploadClaim({ attempt: { lease_epoch: 8 } }),
    }),
    /conflicting upload claim.*attempt-1/,
  );
  await assert.rejects(
    journal.recordClaim({
      clientId: 'extension-1',
      claim: uploadClaim({ job: { content_sha256: 'b'.repeat(64) } }),
    }),
    /conflicting upload claim.*attempt-1/,
  );
  assert.equal(store.writes, 1);
});

test('attempt-specific operations reject an unknown attempt', async () => {
  const store = memoryStore();
  const journal = createUploadJournal({ read: store.read, write: store.write });

  const operations = [
    () => journal.get('missing-attempt'),
    () => journal.markMutationStarted('missing-attempt'),
    () => journal.saveReceipt('missing-attempt', uploadReceipt()),
    () => journal.saveResult('missing-attempt', uploadResult({ outcome: 'failed' })),
    () => journal.markAcknowledged('missing-attempt'),
  ];
  for (const operation of operations) {
    await assert.rejects(operation(), /unknown upload attempt.*missing-attempt/);
  }
});

test('receipt and result advance through the durable local phase sequence', async () => {
  const store = memoryStore();
  const timestamps = [
    '2026-07-21T12:00:00Z',
    '2026-07-21T12:01:00Z',
    '2026-07-21T12:02:00Z',
    '2026-07-21T12:03:00Z',
    '2026-07-21T12:04:00Z',
  ];
  const journal = createUploadJournal({
    read: store.read,
    write: store.write,
    now: () => timestamps.shift(),
  });

  await journal.recordClaim({ clientId: 'extension-1', claim: uploadClaim() });
  assert.equal((await journal.markMutationStarted('attempt-1')).local_phase, 'mutation_started');

  const receipt = uploadReceipt();
  const receiptPending = await journal.saveReceipt('attempt-1', receipt);
  assert.equal(receiptPending.local_phase, 'receipt_observed');
  assert.deepEqual(receiptPending.receipt, receipt);
  const writesAfterReceipt = store.writes;
  assert.deepEqual(await journal.saveReceipt('attempt-1', structuredClone(receipt)), receiptPending);
  assert.equal(store.writes, writesAfterReceipt);
  await assert.rejects(
    journal.saveReceipt('attempt-1', uploadReceipt({ remote_chapter_id: 'different-chapter' })),
    /conflicting receipt.*attempt-1/,
  );

  const result = uploadResult();
  const resultPending = await journal.saveResult('attempt-1', result);
  assert.equal(resultPending.local_phase, 'ack_pending');
  assert.deepEqual(resultPending.result, result);
  const writesAfterResult = store.writes;
  assert.deepEqual(await journal.saveResult('attempt-1', structuredClone(result)), resultPending);
  assert.equal(store.writes, writesAfterResult);
  await assert.rejects(
    journal.saveResult('attempt-1', uploadResult({ message: 'different result' })),
    /conflicting result.*attempt-1/,
  );

  const acknowledged = await journal.markAcknowledged('attempt-1');
  assert.equal(acknowledged.local_phase, 'acked');
  assert.equal(acknowledged.created_at, '2026-07-21T12:00:00Z');
  assert.equal(acknowledged.updated_at, '2026-07-21T12:04:00Z');
  assert.equal(acknowledged.acked_at, '2026-07-21T12:04:00Z');
  assert.deepEqual(store.value.records[0], acknowledged);
  assert.equal(store.writes, 5);
});

test('saveResult permits failed and read-only results without a receipt', async () => {
  const store = memoryStore();
  const journal = createUploadJournal({ read: store.read, write: store.write });

  await journal.recordClaim({ clientId: 'extension-1', claim: uploadClaim() });
  const failed = uploadResult({
    outcome: 'failed',
    message: '',
    error_code: 'platform_rejected',
    error_message: 'Rejected by platform',
  });
  const failedPending = await journal.saveResult('attempt-1', failed);
  assert.equal(failedPending.local_phase, 'ack_pending');
  assert.equal(failedPending.receipt, null);

  await journal.recordClaim({
    clientId: 'extension-1',
    claim: uploadClaim({
      execution_mode: 'reconcile',
      job: { job_id: 'job-2', idempotency_key: 'publisher-job:v1:job-2' },
      attempt: { attempt_id: 'attempt-2' },
    }),
  });
  const reconcileResult = {
    outcome: 'indeterminate',
    observed_at: '2026-07-21T12:05:00Z',
    current_url: '',
    evidence: { reason: 'No conclusive remote evidence' },
  };
  const reconcilePending = await journal.saveResult('attempt-2', reconcileResult);
  assert.equal(reconcilePending.local_phase, 'ack_pending');
  assert.deepEqual(reconcilePending.result, reconcileResult);

  await journal.recordClaim({
    clientId: 'extension-1',
    claim: uploadClaim({
      job: { job_id: 'job-3', idempotency_key: 'publisher-job:v1:job-3' },
      attempt: { attempt_id: 'attempt-3' },
    }),
  });
  const auditClaim = uploadClaim({
    job: {
      job_id: 'job-audit',
      idempotency_key: 'publisher-job:v1:job-audit',
      task_kind: 'audit_sync',
    },
    attempt: { attempt_id: 'attempt-audit' },
  });
  await journal.recordClaim({ clientId: 'extension-1', claim: auditClaim });
  const auditResult = uploadResult({ details: { chapters: [] } });
  const auditPending = await journal.saveResult('attempt-audit', auditResult);
  assert.equal(auditPending.local_phase, 'ack_pending');
  assert.deepEqual(auditPending.result, auditResult);

  await assert.rejects(
    journal.saveResult('attempt-3', uploadResult()),
    /receipt.*required.*attempt-3/,
  );
  assert.equal((await journal.get('attempt-3')).local_phase, 'claimed');
});

test('a restarted journal recovers receipt_observed and ack_pending records', async () => {
  const store = memoryStore();
  const firstWorker = createUploadJournal({ read: store.read, write: store.write });
  await firstWorker.recordClaim({ clientId: 'extension-1', claim: uploadClaim() });
  await firstWorker.markMutationStarted('attempt-1');
  await firstWorker.saveReceipt('attempt-1', uploadReceipt());

  const secondWorker = createUploadJournal({ read: store.read, write: store.write });
  const loadedAfterReceipt = await secondWorker.load();
  assert.equal(loadedAfterReceipt.length, 1);
  assert.equal(loadedAfterReceipt[0].local_phase, 'receipt_observed');
  assert.deepEqual(await secondWorker.pending(), loadedAfterReceipt);

  await secondWorker.saveResult('attempt-1', uploadResult());

  const thirdWorker = createUploadJournal({ read: store.read, write: store.write });
  const loadedAfterResult = await thirdWorker.load();
  assert.equal(loadedAfterResult[0].local_phase, 'ack_pending');
  assert.deepEqual(await thirdWorker.pending(), loadedAfterResult);
  assert.deepEqual(loadedAfterResult[0].receipt, uploadReceipt());
  assert.deepEqual(loadedAfterResult[0].result, uploadResult());
});

test('every phase mutation waits for its storage write before resolving', async () => {
  let stored;
  let nextWriteGate;
  const journal = createUploadJournal({
    read: async () => stored,
    write: async (snapshot) => {
      const gate = nextWriteGate;
      if (gate) {
        gate.started.resolve(structuredClone(snapshot));
        await gate.release.promise;
        nextWriteGate = undefined;
      }
      stored = structuredClone(snapshot);
    },
  });
  await journal.recordClaim({ clientId: 'extension-1', claim: uploadClaim() });

  async function expectBlockedWrite(invoke, expectedPhase) {
    const started = deferred();
    const release = deferred();
    nextWriteGate = { release, started };
    let settled = false;
    const operation = invoke();
    operation.then(() => {
      settled = true;
    });

    const attempted = await started.promise;
    await Promise.resolve();
    assert.equal(settled, false);
    assert.equal(attempted.records[0].local_phase, expectedPhase);
    assert.notEqual(stored.records[0].local_phase, expectedPhase);

    release.resolve();
    await operation;
    assert.equal(stored.records[0].local_phase, expectedPhase);
  }

  await expectBlockedWrite(
    () => journal.markMutationStarted('attempt-1'),
    'mutation_started',
  );
  await expectBlockedWrite(
    () => journal.saveReceipt('attempt-1', uploadReceipt()),
    'receipt_observed',
  );
  await expectBlockedWrite(
    () => journal.saveResult('attempt-1', uploadResult()),
    'ack_pending',
  );
  await expectBlockedWrite(
    () => journal.markAcknowledged('attempt-1'),
    'acked',
  );
});

test('concurrent writes are serialized without losing earlier records', async () => {
  const firstWriteStarted = deferred();
  const releaseFirstWrite = deferred();
  let stored;
  let writeCalls = 0;
  let activeWrites = 0;
  let maximumActiveWrites = 0;
  const journal = createUploadJournal({
    read: async () => stored,
    write: async (snapshot) => {
      const call = writeCalls;
      writeCalls += 1;
      activeWrites += 1;
      maximumActiveWrites = Math.max(maximumActiveWrites, activeWrites);
      if (call === 0) {
        firstWriteStarted.resolve();
        await releaseFirstWrite.promise;
      }
      stored = structuredClone(snapshot);
      activeWrites -= 1;
    },
  });

  const first = journal.recordClaim({ clientId: 'extension-1', claim: uploadClaim() });
  const second = journal.recordClaim({
    clientId: 'extension-1',
    claim: uploadClaim({
      job: { job_id: 'job-2', idempotency_key: 'publisher-job:v1:job-2' },
      attempt: { attempt_id: 'attempt-2' },
    }),
  });

  await firstWriteStarted.promise;
  await Promise.resolve();
  assert.equal(writeCalls, 1);
  releaseFirstWrite.resolve();
  await Promise.all([first, second]);

  assert.equal(maximumActiveWrites, 1);
  assert.equal(writeCalls, 2);
  assert.deepEqual(
    stored.records.map((record) => record.attempt.attempt_id),
    ['attempt-1', 'attempt-2'],
  );
});

test('acknowledgement keeps only the most recently acked records without regression', async () => {
  const store = memoryStore();
  let tick = 0;
  const journal = createUploadJournal({
    read: store.read,
    write: store.write,
    now: () => new Date(Date.UTC(2026, 6, 21, 12, 0, tick++)).toISOString(),
    maxAcknowledged: 2,
  });
  const claims = [1, 2, 3].map((number) => uploadClaim({
    job: {
      job_id: `job-${number}`,
      idempotency_key: `publisher-job:v1:job-${number}`,
    },
    attempt: { attempt_id: `attempt-${number}` },
  }));

  for (const claim of claims) {
    await journal.recordClaim({ clientId: 'extension-1', claim });
    await journal.saveResult(
      claim.attempt.attempt_id,
      uploadResult({
        outcome: 'failed',
        error_code: 'platform_rejected',
        error_message: 'Rejected by platform',
      }),
    );
  }
  await journal.markAcknowledged('attempt-2');
  await journal.markAcknowledged('attempt-3');
  const latestAcknowledgement = await journal.markAcknowledged('attempt-1');

  assert.deepEqual(
    (await journal.load()).map((record) => record.attempt.attempt_id),
    ['attempt-3', 'attempt-1'],
  );
  assert.deepEqual(await journal.pending(), []);
  await assert.rejects(journal.get('attempt-2'), /unknown upload attempt.*attempt-2/);

  const writesBeforeRepeat = store.writes;
  const repeatedClaim = await journal.recordClaim({
    clientId: 'extension-1',
    claim: claims[0],
  });
  assert.equal(repeatedClaim.local_phase, 'acked');
  assert.equal(repeatedClaim.acked_at, latestAcknowledgement.acked_at);
  assert.equal(store.writes, writesBeforeRepeat);
  await assert.rejects(
    journal.markMutationStarted('attempt-1'),
    /already acknowledged/,
  );
  await assert.rejects(
    journal.saveReceipt('attempt-1', uploadReceipt()),
    /already acknowledged/,
  );
  await assert.rejects(
    journal.saveResult('attempt-1', uploadResult({ outcome: 'failed' })),
    /already acknowledged/,
  );
  const writesBeforeRepeatedAck = store.writes;
  const repeatedAck = await journal.markAcknowledged('attempt-1');
  assert.equal(repeatedAck.acked_at, latestAcknowledgement.acked_at);
  assert.equal(store.writes, writesBeforeRepeatedAck);
  assert.equal((await journal.get('attempt-1')).local_phase, 'acked');
});

test('compact persists a stricter acknowledged-record limit after restart', async () => {
  const store = memoryStore();
  const initialJournal = createUploadJournal({
    read: store.read,
    write: store.write,
    maxAcknowledged: 3,
  });

  for (const number of [1, 2, 3]) {
    const claim = uploadClaim({
      job: {
        job_id: `job-${number}`,
        idempotency_key: `publisher-job:v1:job-${number}`,
      },
      attempt: { attempt_id: `attempt-${number}` },
    });
    await initialJournal.recordClaim({ clientId: 'extension-1', claim });
    await initialJournal.saveResult(
      `attempt-${number}`,
      uploadResult({
        outcome: 'failed',
        error_code: 'platform_rejected',
        error_message: 'Rejected by platform',
      }),
    );
    await initialJournal.markAcknowledged(`attempt-${number}`);
  }

  const restarted = createUploadJournal({
    read: store.read,
    write: store.write,
    maxAcknowledged: 1,
  });
  assert.equal((await restarted.load()).length, 3);
  const writesBeforeCompact = store.writes;
  const compacted = await restarted.compact();

  assert.deepEqual(
    compacted.map((record) => record.attempt.attempt_id),
    ['attempt-3'],
  );
  assert.equal(store.value.records.length, 1);
  assert.equal(store.writes, writesBeforeCompact + 1);

  const afterAnotherRestart = createUploadJournal({
    read: store.read,
    write: store.write,
    maxAcknowledged: 1,
  });
  assert.deepEqual(await afterAnotherRestart.load(), compacted);
});
