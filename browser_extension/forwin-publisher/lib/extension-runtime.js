export const extensionApi = globalThis.browser ?? globalThis.chrome;
export const usePromiseApi = Boolean(globalThis.browser);

export function getRuntimeLastError() {
  return globalThis.chrome?.runtime?.lastError
    || globalThis.browser?.runtime?.lastError
    || null;
}

export function wrapCall(target, method, ...args) {
  if (!target || typeof target[method] !== 'function') {
    return Promise.resolve(undefined);
  }
  if (usePromiseApi) {
    return target[method](...args);
  }
  return new Promise((resolve, reject) => {
    target[method](...args, (value) => {
      const lastError = getRuntimeLastError();
      if (lastError) {
        reject(new Error(lastError.message));
        return;
      }
      resolve(value);
    });
  });
}

export function reportBackgroundError(context, error) {
  const message = error instanceof Error ? error.message : String(error || '');
  if (!message) {
    return;
  }
  console.warn(`[ForWin Publisher Bridge] ${context}: ${message}`);
}
