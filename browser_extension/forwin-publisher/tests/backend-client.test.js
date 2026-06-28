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
