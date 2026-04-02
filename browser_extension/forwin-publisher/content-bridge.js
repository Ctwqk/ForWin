(function () {
  const CHANNEL = globalThis.__FORWIN_CHANNELS__?.BRIDGE_CHANNEL || 'forwin-publisher-extension';
  const runtime = (globalThis.browser && globalThis.browser.runtime) || (globalThis.chrome && globalThis.chrome.runtime);
  if (!runtime) {
    return;
  }

  function announceReady() {
    try {
      runtime.sendMessage({ action: 'content-bridge-ready' });
    } catch (_error) {
      // Ignore when the background worker is still waking up.
    }
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window || event.origin !== window.location.origin) {
      return;
    }
    const data = event.data;
    if (!data || typeof data !== 'object') {
      return;
    }
    if (data.channel !== CHANNEL || data.direction !== 'page-to-extension' || data.kind !== 'request') {
      return;
    }

    runtime.sendMessage(
      {
        action: data.action,
        payload: data.payload || {},
      },
      (response) => {
        const lastError = globalThis.chrome?.runtime?.lastError || globalThis.browser?.runtime?.lastError;
        window.postMessage(
          {
            channel: CHANNEL,
            direction: 'extension-to-page',
            kind: 'response',
            correlationId: data.correlationId,
            ok: !lastError && Boolean(response?.ok),
            payload: response?.payload,
            error: lastError?.message || response?.error || '',
          },
          window.location.origin,
        );
      },
    );
  });

  runtime.onMessage.addListener((message) => {
    if (!message || message.channel !== CHANNEL || message.kind !== 'event') {
      return;
    }
    window.postMessage(
      {
        channel: CHANNEL,
        direction: 'extension-to-page',
        kind: 'event',
        event: message.event,
        payload: message.payload || {},
      },
      window.location.origin,
    );
  });

  window.addEventListener('pageshow', announceReady);
  announceReady();
})();
