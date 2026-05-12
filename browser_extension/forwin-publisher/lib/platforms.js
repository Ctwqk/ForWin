const fanqieSessionCookieNames = new Set([
  'sessionid',
  'sessionid_ss',
]);

const fanqieWriterSignalCookieNames = new Set([
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

function hasAnyCookie(cookieNames, requiredNames) {
  for (const name of requiredNames) {
    if (cookieNames.has(name)) {
      return true;
    }
  }
  return false;
}

function qidianCookieConnected(cookieNames) {
  if (!cookieNames.has('AppAuthToken')) {
    return false;
  }
  return hasAnyCookie(cookieNames, ['pubtoken', 'ywopenid', 'ywkey', 'ywKey', 'ywtab']);
}

function fanqieCookieConnected(cookieNames) {
  const hasSession = hasAnyCookie(cookieNames, fanqieSessionCookieNames);
  const hasWriterSignal = hasAnyCookie(cookieNames, fanqieWriterSignalCookieNames);
  return hasSession && hasWriterSignal;
}

function isKnownLoginUrl(platformId, url = '') {
  const value = String(url || '');
  if (platformId === 'qidian') {
    return value.includes('write.qq.com') && value.includes('/portal/login');
  }
  if (platformId === 'fanqie') {
    return value.includes('fanqienovel.com') && value.includes('/main/writer/login');
  }
  return false;
}

function pageInspectionState(platformId, inspection) {
  if (inspection === undefined) {
    return {
      provided: false,
      inspected: false,
      authenticated: false,
      loginVisible: false,
      currentUrl: '',
    };
  }
  if (!inspection || !inspection.ok) {
    return {
      provided: true,
      inspected: false,
      authenticated: false,
      loginVisible: false,
      currentUrl: '',
    };
  }
  const currentUrl = String(inspection.currentUrl || inspection.url || '');
  const platformMatches = !inspection.platform || inspection.platform === platformId;
  if (!platformMatches) {
    return {
      provided: true,
      inspected: false,
      authenticated: false,
      loginVisible: false,
      currentUrl: '',
    };
  }
  return {
    provided: true,
    inspected: true,
    authenticated: Boolean(inspection.authenticated),
    loginVisible: Boolean(inspection.loginVisible) || isKnownLoginUrl(platformId, currentUrl),
    currentUrl,
  };
}

function heartbeatPayload(platformId, cookieNames, savedState, cookieSignal, inspection) {
  const loginMethod = savedState.loginMethod || 'scan';
  const page = pageInspectionState(platformId, inspection);
  const savedError = String(savedState.lastError || '').trim();
  const savedLoginRequired = ['login-required', 'platform-login-required'].includes(
    savedError.toLowerCase(),
  );
  const loggedOutByPage = page.inspected && page.loginVisible && !page.authenticated;
  const loggedOutBySavedError = savedLoginRequired && !page.authenticated;
  const waitingForPageEvidence = page.provided && !page.inspected && cookieSignal;
  const connected = (loggedOutByPage || loggedOutBySavedError)
    ? false
    : (page.authenticated || (!waitingForPageEvidence && cookieSignal));
  const lastError = (loggedOutByPage || loggedOutBySavedError) ? 'login-required' : savedError;
  return {
    platform: platformId,
    connected,
    login_method: loginMethod,
    last_error: lastError,
    raw_state: {
      cookie_names: Array.from(cookieNames).sort(),
      cookie_signal: cookieSignal,
      page_inspected: page.inspected,
      page_authenticated: page.authenticated,
      page_login_visible: page.loginVisible,
      page_evidence_required: page.provided,
      current_url: page.currentUrl,
    },
  };
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

export function buildHeartbeatState(platformId, cookies = [], savedState = {}, inspection = undefined) {
  const cookieNames = getCookieNameSet(cookies);
  if (platformId === 'qidian') {
    const cookieSignal = qidianCookieConnected(cookieNames);
    return heartbeatPayload(platformId, cookieNames, savedState, cookieSignal, inspection);
  }
  if (platformId === 'fanqie') {
    const cookieSignal = fanqieCookieConnected(cookieNames);
    return heartbeatPayload(platformId, cookieNames, savedState, cookieSignal, inspection);
  }
  throw new Error(`Unsupported publisher platform: ${platformId}`);
}
