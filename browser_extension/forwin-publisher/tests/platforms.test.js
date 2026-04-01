import test from 'node:test';
import assert from 'node:assert/strict';

import { buildHeartbeatState, getProbeUrl, isLoginComplete, shouldProbeLogin } from '../lib/platforms.js';

test('qidian login does not complete from dashboard url alone without strong auth signal', () => {
  assert.equal(
    isLoginComplete('qidian', {
      url: 'https://write.qq.com/portal/dashboard',
      cookies: [],
    }),
    false,
  );
});

test('qidian login only probes after strong cookies appear on the login page', () => {
  const cookies = [
    { name: 'AppAuthToken' },
    { name: 'pubtoken' },
  ];

  assert.equal(
    shouldProbeLogin('qidian', {
      url: 'https://write.qq.com/portal/login',
      cookies,
      probeIndex: 0,
    }),
    true,
  );
  assert.equal(getProbeUrl('qidian', 0), 'https://write.qq.com/portal/dashboard');
});

test('fanqie heartbeat auto-connects when strong writer cookies are present', () => {
  const state = buildHeartbeatState('fanqie', [
    { name: 'sessionid' },
    { name: 'has_biz_token' },
  ], {
    connected: false,
    loginMethod: 'scan',
    lastError: '',
  });

  assert.equal(state.connected, true);
  assert.equal(state.login_method, 'scan');
  assert.equal(state.raw_state.cookie_signal, true);
});

test('fanqie probe url points to the modern writer console', () => {
  assert.equal(getProbeUrl('fanqie', 0), 'https://fanqienovel.com/main/writer/');
  assert.equal(
    isLoginComplete('fanqie', {
      url: 'https://fanqienovel.com/main/writer/',
      cookies: [{ name: 'sessionid' }, { name: 'sid_tt' }],
    }),
    true,
  );
});

test('qidian heartbeat auto-connects when auth cookies are present', () => {
  const state = buildHeartbeatState('qidian', [
    { name: 'AppAuthToken' },
    { name: 'pubtoken' },
  ], {
    connected: false,
    loginMethod: 'scan',
    lastError: '',
  });

  assert.equal(state.connected, true);
  assert.equal(state.raw_state.cookie_signal, true);
});
