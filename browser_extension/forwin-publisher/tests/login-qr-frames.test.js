import test from 'node:test';
import assert from 'node:assert/strict';

import {
  findLoginQrFrameTargets,
  isLoginQrFrameUrl,
  sanitizeFrameUrlForStatus,
} from '../lib/login-qr-frames.js';

test('identifies Qidian and WeChat login QR frames without broad host matching', () => {
  assert.equal(isLoginQrFrameUrl('https://passport.yuewen.com/yuewen.html?ticket=secret'), true);
  assert.equal(isLoginQrFrameUrl('https://open.weixin.qq.com/connect/qrconnect?appid=abc'), true);
  assert.equal(isLoginQrFrameUrl('https://example.com/connect/qrcode/abc'), false);
});

test('orders nested QR frames before parent login frames and strips query data', () => {
  const frames = [
    { frameId: 0, parentFrameId: -1, url: 'https://write.qq.com/portal/login?secret=1' },
    { frameId: 8, parentFrameId: 4, url: 'https://open.weixin.qq.com/connect/qrconnect?appid=abc&state=secret' },
    { frameId: 4, parentFrameId: 0, url: 'https://passport.yuewen.com/yuewen.html?ticket=secret' },
  ];

  assert.deepEqual(findLoginQrFrameTargets(frames), [
    {
      frameId: 8,
      url: 'https://open.weixin.qq.com/connect/qrconnect',
      priority: 40,
    },
    {
      frameId: 4,
      url: 'https://passport.yuewen.com/yuewen.html',
      priority: 30,
    },
    {
      frameId: 0,
      url: 'https://write.qq.com/portal/login',
      priority: 20,
    },
  ]);
  assert.equal(
    sanitizeFrameUrlForStatus('https://open.weixin.qq.com/connect/qrcode/abc?secret=1'),
    'https://open.weixin.qq.com/connect/qrcode/abc',
  );
});
