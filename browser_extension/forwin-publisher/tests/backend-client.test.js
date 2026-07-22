import test from 'node:test';
import assert from 'node:assert/strict';

import { createBackendClient } from '../lib/backend-client.js';

test('backend client posts login QR notifications to extension endpoint', async () => {
  const calls = [];
  const client = createBackendClient(
    async (url, options = {}) => {
      calls.push({ url, options });
      return {
        ok: true,
        json: async () => ({ ok: true, dispatched: true }),
      };
    },
    { backendBaseUrl: 'http://127.0.0.1:8899/', apiKey: ' secret ' },
  );

  const payload = {
    client_id: 'client-1',
    platform: 'fanqie',
    current_url: 'https://fanqienovel.com/main/writer/',
    image_data_url: 'data:image/png;base64,cXI=',
    source: 'canvas',
  };

  const response = await client.notifyLoginQr(payload);

  assert.deepEqual(response, { ok: true, dispatched: true });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'http://127.0.0.1:8899/api/publishers/extension/login-qr');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.headers['X-Forwin-Extension-Key'], 'secret');
  assert.deepEqual(JSON.parse(calls[0].options.body), payload);
});

test('backend client posts upload job and attempt APIs with encoded path parameters', async () => {
  const calls = [];
  const client = createBackendClient(
    async (url, options = {}) => {
      calls.push({ url, options });
      return {
        ok: true,
        json: async () => ({ ok: true }),
      };
    },
    { backendBaseUrl: 'http://127.0.0.1:8899/', apiKey: ' secret ' },
  );

  const jobId = 'job/with space?';
  const attemptId = 'attempt#1/2';
  const requests = [
    {
      method: 'claimNextUploadJob',
      args: [{ client_id: 'client-1' }],
      path: '/api/publishers/extension/upload-jobs/claim',
      payload: { client_id: 'client-1' },
    },
    {
      method: 'heartbeatUploadAttempt',
      args: [jobId, attemptId, { lease_token: 'lease-1' }],
      path: '/api/publishers/extension/upload-jobs/job%2Fwith%20space%3F/attempts/attempt%231%2F2/heartbeat',
      payload: { lease_token: 'lease-1' },
    },
    {
      method: 'updateUploadAttemptPhase',
      args: [jobId, attemptId, { phase: 'uploading' }],
      path: '/api/publishers/extension/upload-jobs/job%2Fwith%20space%3F/attempts/attempt%231%2F2/phase',
      payload: { phase: 'uploading' },
    },
    {
      method: 'pauseUploadAttempt',
      args: [jobId, attemptId, { risk_reason: 'captcha' }],
      path: '/api/publishers/extension/upload-jobs/job%2Fwith%20space%3F/attempts/attempt%231%2F2/pause',
      payload: { risk_reason: 'captcha' },
    },
    {
      method: 'submitUploadAttemptResult',
      args: [jobId, attemptId, { status: 'succeeded' }],
      path: '/api/publishers/extension/upload-jobs/job%2Fwith%20space%3F/attempts/attempt%231%2F2/result',
      payload: { status: 'succeeded' },
    },
    {
      method: 'submitUploadReceipt',
      args: [jobId, attemptId, { receipt_id: 'receipt-1' }],
      path: '/api/publishers/extension/upload-jobs/job%2Fwith%20space%3F/attempts/attempt%231%2F2/receipt',
      payload: { receipt_id: 'receipt-1' },
    },
    {
      method: 'reconcileUploadAttempt',
      args: [jobId, attemptId, { observed_status: 'published' }],
      path: '/api/publishers/extension/upload-jobs/job%2Fwith%20space%3F/attempts/attempt%231%2F2/reconcile',
      payload: { observed_status: 'published' },
    },
  ];

  for (const request of requests) {
    await client[request.method](...request.args);
  }

  assert.equal(calls.length, requests.length);
  calls.forEach(({ url, options }, index) => {
    assert.equal(url, `http://127.0.0.1:8899${requests[index].path}`);
    assert.equal(options.method, 'POST');
    assert.deepEqual(options.headers, {
      'Content-Type': 'application/json',
      'X-Forwin-Extension-Key': 'secret',
    });
    assert.equal(options.body, JSON.stringify(requests[index].payload));
  });
});

test('backend client no longer exposes legacy upload job methods', () => {
  const client = createBackendClient(async () => {}, {
    backendBaseUrl: 'http://127.0.0.1:8899',
    apiKey: 'secret',
  });

  assert.equal(Object.hasOwn(client, 'getUploadJob'), false);
  assert.equal(Object.hasOwn(client, 'updateUploadJobResult'), false);
});

test('backend client preserves structured API error metadata', async (t) => {
  const cases = [
    {
      name: 'prefers the nested error code',
      payload: {
        detail: 'upload attempt fence conflict',
        code: 'TOP_LEVEL_CODE',
        error: { code: 'ATTEMPT_FENCE_CONFLICT' },
      },
      expectedCode: 'ATTEMPT_FENCE_CONFLICT',
      expectedMessage: 'upload attempt fence conflict',
    },
    {
      name: 'falls back to the top-level code',
      payload: {
        message: 'upload attempt is no longer active',
        code: 'ATTEMPT_NOT_ACTIVE',
      },
      expectedCode: 'ATTEMPT_NOT_ACTIVE',
      expectedMessage: 'upload attempt is no longer active',
    },
    {
      name: 'reads code and message from a FastAPI detail object',
      payload: {
        detail: {
          code: 'ATTEMPT_FENCE_CONFLICT',
          message: 'heartbeat fence is stale',
          expected_fence: 7,
        },
      },
      expectedCode: 'ATTEMPT_FENCE_CONFLICT',
      expectedMessage: 'heartbeat fence is stale',
    },
  ];

  for (const testCase of cases) {
    await t.test(testCase.name, async () => {
      const client = createBackendClient(
        async () => ({
          ok: false,
          status: 409,
          json: async () => testCase.payload,
        }),
        { backendBaseUrl: 'http://127.0.0.1:8899', apiKey: 'secret' },
      );

      await assert.rejects(
        () => client.claimNextUploadJob({ client_id: 'client-1' }),
        (error) => {
          assert.equal(error.message, testCase.expectedMessage);
          assert.equal(error.status, 409);
          assert.equal(error.code, testCase.expectedCode);
          assert.strictEqual(error.payload, testCase.payload);
          return true;
        },
      );
    });
  }
});
