import test from 'node:test';
import assert from 'node:assert/strict';

import {
  uploadExecutionTimeoutMs,
  uploadMessageTimeoutMs,
} from '../lib/upload-timeouts.js';

test('qidian upload timeouts allow the full save and ccid verification workflow', () => {
  assert.ok(uploadMessageTimeoutMs('qidian') >= 240000);
  assert.ok(uploadExecutionTimeoutMs('qidian') >= 420000);
});

test('fanqie keeps a shorter message timeout but gets longer overall verification budget', () => {
  assert.ok(uploadMessageTimeoutMs('fanqie') >= 30000);
  assert.ok(uploadExecutionTimeoutMs('fanqie') >= 120000);
});
