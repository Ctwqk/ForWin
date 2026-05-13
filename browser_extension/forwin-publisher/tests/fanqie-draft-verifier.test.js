import test from 'node:test';
import assert from 'node:assert/strict';

import {
  verifyFanqieDraftWithRetries,
} from '../lib/fanqie-draft-verifier.js';

test('fanqie draft verifier waits through delayed chapter-manage indexing', async () => {
  let attempts = 0;
  const slept = [];

  const result = await verifyFanqieDraftWithRetries({
    chapterTitle: '第60章 新秩序',
    maxAttempts: 18,
    verify: async () => {
      attempts += 1;
      if (attempts < 16) {
        return {
          ok: false,
          error: '番茄章节管理页未找到新草稿。',
          errorCode: 'publish-not-confirmed',
        };
      }
      return {
        ok: true,
        currentUrl: 'https://fanqienovel.com/main/writer/chapter-manage/123?type=2',
        message: '章节草稿已进入番茄章节管理。',
        resultPayload: { verified_via: 'chapter-manage' },
      };
    },
    reload: async () => {},
    sleep: async (ms) => {
      slept.push(ms);
    },
  });

  assert.equal(result.ok, true);
  assert.equal(attempts, 16);
  assert.ok(slept.length >= 15);
});

test('fanqie draft verifier returns explicit timeout after exhausting retries', async () => {
  const result = await verifyFanqieDraftWithRetries({
    chapterTitle: '第60章 新秩序',
    maxAttempts: 3,
    verify: async () => ({
      ok: false,
      error: '番茄章节管理页未找到新草稿。',
      errorCode: 'publish-not-confirmed',
    }),
    reload: async () => {},
    sleep: async () => {},
  });

  assert.equal(result.ok, false);
  assert.equal(result.errorCode, 'chapter-editor-navigation-failed');
  assert.match(result.error, /未响应草稿核验/);
}
);
