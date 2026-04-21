export function detectBrowserTarget(userAgent = '') {
  return /Firefox\//i.test(String(userAgent || '')) ? 'firefox' : 'chromium';
}

export function getBrowserCapabilities({ browserTarget, extensionApi }) {
  const target = String(browserTarget || 'chromium').trim() || 'chromium';
  return {
    browserTarget: target,
    supportsDebugger: target === 'chromium' && Boolean(extensionApi?.debugger?.attach),
    supportsBackgroundServiceWorker: target === 'chromium',
  };
}

export function createUnsupportedCapabilityError(browserTarget, capability, guidance = '') {
  const target = String(browserTarget || 'unknown').trim() || 'unknown';
  const capabilityName = String(capability || 'unknown').trim() || 'unknown';
  const suffix = guidance ? ` ${guidance}` : '';
  const error = new Error(
    `Current browser target "${target}" does not support capability "${capabilityName}".${suffix}`,
  );
  error.code = 'unsupported-browser-capability';
  error.browserTarget = target;
  error.capability = capabilityName;
  return error;
}

export function assertDebuggerCapability(capabilities, guidance = '') {
  if (capabilities?.supportsDebugger) {
    return;
  }
  throw createUnsupportedCapabilityError(
    capabilities?.browserTarget || 'unknown',
    'debugger',
    guidance || 'Please use the Chromium extension target for debugger-backed actions.',
  );
}
