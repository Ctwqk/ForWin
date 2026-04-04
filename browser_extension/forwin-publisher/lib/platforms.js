const fanqieCookieNames = new Set([
  'sessionid',
  'sessionid_ss',
  'passport_auth_status',
  'passport_auth_status_ss',
  'has_biz_token',
  'sid_tt',
]);

export const PLATFORM_ADAPTERS = {
  qidian: {
    platformId: 'qidian',
    displayName: '起点小说',
    loginUrl: 'https://write.qq.com/portal/login',
    dashboardUrl: 'https://write.qq.com/portal/dashboard',
    publishUrl: 'https://write.qq.com/portal/dashboard',
    commentUrl: 'https://pcwrite.yuewen.com/authorh5/message-notify',
    cookieUrls: [
      'https://write.qq.com/',
      'https://www.qq.com/',
      'https://www.yuewen.com/',
    ],
    probeUrls: [
      'https://write.qq.com/portal/dashboard',
      'https://write.qq.com/portal/home',
      'https://write.qq.com/portal/dashboard',
    ],
  },
  fanqie: {
    platformId: 'fanqie',
    displayName: '番茄小说',
    loginUrl: 'https://fanqienovel.com/main/writer/',
    dashboardUrl: 'https://fanqienovel.com/main/writer/',
    publishUrl: 'https://fanqienovel.com/main/writer/',
    commentUrl: 'https://fanqienovel.com/main/writer/',
    cookieUrls: [
      'https://fanqienovel.com/',
      'https://www.fanqienovel.com/',
    ],
    probeUrls: [
      'https://fanqienovel.com/main/writer/',
    ],
  },
};

export function getPlatformAdapter(platformId) {
  const adapter = PLATFORM_ADAPTERS[platformId];
  if (!adapter) {
    throw new Error(`Unsupported publisher platform: ${platformId}`);
  }
  return adapter;
}

export function getCookieNameSet(cookies = []) {
  return new Set(
    cookies
      .map((cookie) => String(cookie?.name || '').trim())
      .filter(Boolean),
  );
}

function qidianCookieConnected(cookieNames) {
  if (!cookieNames.has('AppAuthToken')) {
    return false;
  }
  return ['pubtoken', 'ywopenid', 'ywkey', 'ywKey', 'ywtab'].some((name) => cookieNames.has(name));
}

function fanqieCookieConnected(cookieNames) {
  const hasSession = cookieNames.has('sessionid') || cookieNames.has('sessionid_ss');
  const hasWriterSignal = Array.from(fanqieCookieNames).some((name) => cookieNames.has(name));
  return hasSession && hasWriterSignal;
}

export function isLoginComplete(platformId, { url = '', cookies = [] } = {}) {
  const cookieNames = getCookieNameSet(cookies);
  if (platformId === 'qidian') {
    return qidianCookieConnected(cookieNames) && !url.includes('/portal/login');
  }
  if (platformId === 'fanqie') {
    return fanqieCookieConnected(cookieNames) && !url.includes('/main/writer/login');
  }
  return false;
}

export function shouldProbeLogin(platformId, { url = '', cookies = [], probeIndex = 0 } = {}) {
  const adapter = getPlatformAdapter(platformId);
  if (probeIndex >= adapter.probeUrls.length) {
    return false;
  }
  const cookieNames = getCookieNameSet(cookies);
  if (platformId === 'qidian') {
    return qidianCookieConnected(cookieNames) && url.includes('/portal/login');
  }
  if (platformId === 'fanqie') {
    return fanqieCookieConnected(cookieNames) && !url.includes('/main/writer/');
  }
  return false;
}

export function getProbeUrl(platformId, probeIndex) {
  const adapter = getPlatformAdapter(platformId);
  return adapter.probeUrls[probeIndex] || '';
}

export function buildHeartbeatState(platformId, cookies = [], savedState = {}) {
  const cookieNames = getCookieNameSet(cookies);
  const loginMethod = savedState.loginMethod || 'scan';
  const lastError = savedState.lastError || '';
  if (platformId === 'qidian') {
    const cookieSignal = qidianCookieConnected(cookieNames);
    return {
      platform: platformId,
      connected: Boolean(savedState.connected || cookieSignal),
      login_method: loginMethod,
      last_error: lastError,
      raw_state: {
        cookie_names: Array.from(cookieNames).sort(),
        cookie_signal: cookieSignal,
      },
    };
  }
  if (platformId === 'fanqie') {
    const cookieSignal = fanqieCookieConnected(cookieNames);
    return {
      platform: platformId,
      connected: Boolean(savedState.connected || cookieSignal),
      login_method: loginMethod,
      last_error: lastError,
      raw_state: {
        cookie_names: Array.from(cookieNames).sort(),
        cookie_signal: cookieSignal,
      },
    };
  }
  throw new Error(`Unsupported publisher platform: ${platformId}`);
}
