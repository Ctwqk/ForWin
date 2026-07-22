import test from 'node:test';
import assert from 'node:assert/strict';

import { guardRiskInspection } from '../lib/risk-inspection.js';

test('a failed risk inspection blocks the trusted mutation boundary', () => {
  let debuggerMutations = 0;
  const blocker = guardRiskInspection({
    ok: false,
    errorCode: 'platform-agent-timeout',
    error: 'Publisher page inspection timed out.',
    currentUrl: '',
    resultPayload: { phase: 'message-timeout' },
  }, 'before-file-input');

  if (!blocker) {
    debuggerMutations += 1;
  }

  assert.equal(debuggerMutations, 0);
  assert.equal(blocker.ok, false);
  assert.equal(blocker.errorCode, 'platform-agent-timeout');
  assert.equal(blocker.resultPayload.phase, 'risk-inspection-failed');
  assert.equal(blocker.resultPayload.inspection_phase, 'message-timeout');
  assert.equal(blocker.resultPayload.boundary, 'before-file-input');
});

test('a successful no-risk inspection permits the trusted mutation boundary', () => {
  assert.equal(
    guardRiskInspection({ detected: false, riskPause: false }, 'before-confirm'),
    null,
  );
});

test('a detected publisher risk remains a typed pause', () => {
  const pause = {
    ok: false,
    riskPause: true,
    riskReason: 'mfa',
    errorCode: 'publisher-risk-pause',
  };

  assert.equal(guardRiskInspection(pause, 'before-confirm'), pause);
});
