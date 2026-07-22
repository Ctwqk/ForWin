const JOURNAL_VERSION = 1;
const DEFAULT_MAX_ACKNOWLEDGED = 100;

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function emptySnapshot() {
  return {
    version: JOURNAL_VERSION,
    records: [],
  };
}

function compactRecords(records, maxAcknowledged) {
  let acknowledgedToKeep = maxAcknowledged;
  const keep = records.map(() => true);
  for (let index = records.length - 1; index >= 0; index -= 1) {
    if (records[index].local_phase !== 'acked') {
      continue;
    }
    if (acknowledgedToKeep > 0) {
      acknowledgedToKeep -= 1;
    } else {
      keep[index] = false;
    }
  }
  return records.filter((_record, index) => keep[index]);
}

function sameValue(left, right) {
  if (left === right) {
    return true;
  }
  if (left == null || right == null || typeof left !== 'object' || typeof right !== 'object') {
    return false;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => sameValue(value, right[index]));
  }
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => (
      key === rightKeys[index] && sameValue(left[key], right[key])
    ));
}

export function createUploadJournal({
  read,
  write,
  now = () => new Date().toISOString(),
  maxAcknowledged = DEFAULT_MAX_ACKNOWLEDGED,
} = {}) {
  if (typeof read !== 'function' || typeof write !== 'function') {
    throw new TypeError('upload journal requires read and write functions');
  }
  if (typeof now !== 'function') {
    throw new TypeError('upload journal now must be a function');
  }
  if (!Number.isInteger(maxAcknowledged) || maxAcknowledged < 0) {
    throw new RangeError('upload journal maxAcknowledged must be a non-negative integer');
  }

  let snapshot;
  let queue = Promise.resolve();

  function schedule(operation) {
    const result = queue.then(operation, operation);
    queue = result.catch(() => undefined);
    return result;
  }

  async function ensureLoaded() {
    if (snapshot) {
      return;
    }
    const stored = await read();
    snapshot = stored && Array.isArray(stored.records)
      ? clone(stored)
      : emptySnapshot();
  }

  function findRecord(attemptId) {
    return snapshot.records.find((record) => record.attempt?.attempt_id === attemptId);
  }

  function requireRecord(attemptId) {
    const record = findRecord(attemptId);
    if (!record) {
      throw new Error(`unknown upload attempt: ${attemptId}`);
    }
    return record;
  }

  async function updateRecord(attemptId, update, options = {}) {
    const index = snapshot.records.findIndex(
      (record) => record.attempt?.attempt_id === attemptId,
    );
    if (index < 0) {
      throw new Error(`unknown upload attempt: ${attemptId}`);
    }
    const current = snapshot.records[index];
    const nextRecord = clone(current);
    if (update(nextRecord, current) === false) {
      return clone(current);
    }
    const timestamp = now();
    nextRecord.updated_at = timestamp;
    if (nextRecord.local_phase === 'acked' && !nextRecord.acked_at) {
      nextRecord.acked_at = timestamp;
    }
    const next = clone(snapshot);
    if (options.moveToEnd) {
      next.records.splice(index, 1);
      next.records.push(nextRecord);
    } else {
      next.records[index] = nextRecord;
    }
    if (options.compactAcknowledged) {
      next.records = compactRecords(next.records, maxAcknowledged);
    }
    await write(clone(next));
    snapshot = next;
    return clone(nextRecord);
  }

  function assertNotAcknowledged(record) {
    if (record.local_phase === 'acked') {
      throw new Error(`upload attempt ${record.attempt.attempt_id} is already acknowledged`);
    }
  }

  return {
    async load() {
      return schedule(async () => {
        await ensureLoaded();
        return clone(snapshot.records);
      });
    },

    async recordClaim({ clientId, claim }) {
      return schedule(async () => {
        await ensureLoaded();
        const attemptId = String(claim?.attempt?.attempt_id || '').trim();
        if (!attemptId) {
          throw new TypeError('upload claim requires an attempt_id');
        }
        const existing = findRecord(attemptId);
        if (existing) {
          const matches = existing.client_id === clientId
            && existing.execution_mode === claim.execution_mode
            && sameValue(existing.job, claim.job)
            && sameValue(existing.attempt, claim.attempt);
          if (!matches) {
            throw new Error(`conflicting upload claim for attempt ${attemptId}`);
          }
          return clone(existing);
        }
        const timestamp = now();
        const record = {
          client_id: clientId,
          execution_mode: claim.execution_mode,
          job: clone(claim.job),
          attempt: clone(claim.attempt),
          local_phase: 'claimed',
          receipt: null,
          result: null,
          created_at: timestamp,
          updated_at: timestamp,
          acked_at: null,
        };
        const next = clone(snapshot);
        next.records.push(record);
        await write(clone(next));
        snapshot = next;
        return clone(record);
      });
    },

    async get(attemptId) {
      return schedule(async () => {
        await ensureLoaded();
        return clone(requireRecord(attemptId));
      });
    },

    async pending() {
      return schedule(async () => {
        await ensureLoaded();
        return clone(snapshot.records.filter((record) => record.local_phase !== 'acked'));
      });
    },

    async compact() {
      return schedule(async () => {
        await ensureLoaded();
        const records = compactRecords(snapshot.records, maxAcknowledged);
        if (records.length === snapshot.records.length) {
          return clone(records);
        }
        const next = {
          ...clone(snapshot),
          records,
        };
        await write(clone(next));
        snapshot = next;
        return clone(records);
      });
    },

    async markMutationStarted(attemptId) {
      return schedule(async () => {
        await ensureLoaded();
        return updateRecord(attemptId, (record) => {
          assertNotAcknowledged(record);
          if (record.local_phase === 'mutation_started') {
            return false;
          }
          if (record.local_phase !== 'claimed') {
            throw new Error(
              `cannot mark mutation started from ${record.local_phase} for attempt ${attemptId}`,
            );
          }
          record.local_phase = 'mutation_started';
          return true;
        });
      });
    },

    async saveReceipt(attemptId, receipt) {
      return schedule(async () => {
        await ensureLoaded();
        return updateRecord(attemptId, (record) => {
          assertNotAcknowledged(record);
          if (record.receipt) {
            if (!sameValue(record.receipt, receipt)) {
              throw new Error(`conflicting receipt for upload attempt ${attemptId}`);
            }
            return false;
          }
          if (record.local_phase !== 'mutation_started') {
            throw new Error(
              `cannot save receipt from ${record.local_phase} for attempt ${attemptId}`,
            );
          }
          record.receipt = clone(receipt);
          record.local_phase = 'receipt_observed';
          return true;
        });
      });
    },

    async saveResult(attemptId, result) {
      return schedule(async () => {
        await ensureLoaded();
        return updateRecord(attemptId, (record) => {
          assertNotAcknowledged(record);
          if (record.result) {
            if (!sameValue(record.result, result)) {
              throw new Error(`conflicting result for upload attempt ${attemptId}`);
            }
            return false;
          }
          const maySkipReceipt = record.execution_mode === 'reconcile'
            || record.job?.task_kind === 'audit_sync'
            || ['failed', 'cancelled'].includes(result?.outcome);
          if (!record.receipt && !maySkipReceipt) {
            throw new Error(`receipt is required before result for upload attempt ${attemptId}`);
          }
          record.result = clone(result);
          record.local_phase = 'ack_pending';
          return true;
        });
      });
    },

    async markAcknowledged(attemptId) {
      return schedule(async () => {
        await ensureLoaded();
        return updateRecord(attemptId, (record) => {
          if (record.local_phase === 'acked') {
            return false;
          }
          if (record.local_phase !== 'ack_pending') {
            throw new Error(
              `cannot acknowledge ${record.local_phase} attempt ${attemptId}`,
            );
          }
          record.local_phase = 'acked';
          return true;
        }, { compactAcknowledged: true, moveToEnd: true });
      });
    },
  };
}
