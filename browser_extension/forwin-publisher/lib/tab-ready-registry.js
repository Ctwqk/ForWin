export const READY_CHANNELS = {
  CONTENT_BRIDGE: 'content-bridge',
  PLATFORM_AGENT: 'platform-agent',
};

function waitKey(tabId, channel) {
  return `${tabId}:${channel}`;
}

export class TabReadyRegistry {
  constructor() {
    this.ready = new Map();
    this.waiters = new Map();
  }

  isReady(tabId, channel) {
    return this.ready.get(waitKey(tabId, channel)) === true;
  }

  markReady(tabId, channel) {
    const key = waitKey(tabId, channel);
    this.ready.set(key, true);
    const waiters = this.waiters.get(key) || [];
    this.waiters.delete(key);
    for (const waiter of waiters) {
      globalThis.clearTimeout(waiter.timerId);
      waiter.resolve(true);
    }
  }

  reset(tabId, channel = null) {
    if (channel) {
      const key = waitKey(tabId, channel);
      this.ready.delete(key);
      this.#rejectWaiters(key);
      return;
    }
    const prefix = `${tabId}:`;
    for (const key of Array.from(this.ready.keys())) {
      if (key.startsWith(prefix)) {
        this.ready.delete(key);
      }
    }
    for (const key of Array.from(this.waiters.keys())) {
      if (key.startsWith(prefix)) {
        this.#rejectWaiters(key);
      }
    }
  }

  waitFor(tabId, channel, timeoutMs = 4000) {
    const key = waitKey(tabId, channel);
    if (this.isReady(tabId, channel)) {
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      const timerId = globalThis.setTimeout(() => {
        const pending = this.waiters.get(key) || [];
        this.waiters.set(
          key,
          pending.filter((entry) => entry.timerId !== timerId),
        );
        resolve(false);
      }, timeoutMs);
      const pending = this.waiters.get(key) || [];
      pending.push({ resolve, timerId });
      this.waiters.set(key, pending);
    });
  }

  #rejectWaiters(key) {
    const waiters = this.waiters.get(key) || [];
    this.waiters.delete(key);
    for (const waiter of waiters) {
      globalThis.clearTimeout(waiter.timerId);
      waiter.resolve(false);
    }
  }
}
