import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');

function extract(names) {
  return names.map((name) => {
    const match = source.match(new RegExp(`\\n  (?:async )?function ${name}\\([^]*?\\n  \\}`));
    if (!match) {
      throw new Error(`platform-agent.js no longer defines ${name}`);
    }
    return match[0];
  }).join('\n');
}

// Objects created inside a vm context have another Object.prototype, so
// results are copied into this realm before strict deep comparison.
function plain(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

// URL and Error come from this realm so errors thrown by the tests are
// recognized by `instanceof Error` inside the extracted code.
function load(names, context) {
  return runInNewContext(`${extract(names)}\n({ ${names.join(', ')} })`, { URL, Error, ...context });
}

class FakeElement {
  constructor(options = {}) {
    Object.assign(this, { text: '', attrs: {}, parentElement: null, links: [], ...options });
    this.mutations = options.mutations || [];
  }

  get innerText() { return this.text; }

  get textContent() { return this.text; }

  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }

  closest() { return null; }

  querySelectorAll(selector) { return selector === 'a[href]' ? this.links : []; }

  click() { this.mutations.push('click'); }

  dispatchEvent() { this.mutations.push('dispatchEvent'); }
}

const noRisk = () => ({ detected: false, riskPause: false });

// ---------------------------------------------------------------------------
// run-create-book

const RUN_CREATE_NAMES = [
  'includesAny', 'canonicalPublisherUrl', 'remoteBookIdFromPublisherUrl', 'isCreateBookPage',
  'isPublisherHost', 'createBookFailureResult', 'createBookOnCurrentPage', 'runCreateBook',
];

function createBookHarness({
  href,
  fanqie = async () => true,
  qidian = async () => false,
  risk = noRisk,
  text = '',
} = {}) {
  const calls = [];
  const location = { href };
  const context = {
    window: { location },
    document: {},
    setDebugStep() {},
    pageText: () => text,
    buildRiskPauseResult: risk,
    createFanqieBook: async (...args) => {
      calls.push({ platform: 'fanqie', args });
      return fanqie({ location }, ...args);
    },
    createQidianBook: async (...args) => {
      calls.push({ platform: 'qidian', args });
      return qidian({ location }, ...args);
    },
  };
  const { runCreateBook } = load(RUN_CREATE_NAMES, context);
  return { run: async (payload) => plain(await runCreateBook(payload)), calls, location };
}

const FANQIE_CREATE_URL = 'https://fanqienovel.com/main/writer/create';
const QIDIAN_CREATE_URL = 'https://write.qq.com/portal/dashboard/create-novel?from=S5';
const META = { primary_category: '悬疑', theme_tags: ['都市'], intro: '简介' };

test('run-create-book on Fanqie returns the book id read from the book-info URL', async () => {
  const harness = createBookHarness({
    href: FANQIE_CREATE_URL,
    fanqie: async ({ location }, _name, _meta, options) => {
      options.onSubmitted();
      location.href = 'https://fanqienovel.com/main/writer/book-info/7650001?from=create';
      return true;
    },
  });

  const result = await harness.run({
    platform: 'fanqie', book_name: '雾港潮生录', book_meta: META,
  });

  assert.equal(result.ok, true);
  assert.equal(result.remote_book_id, '7650001');
  assert.equal(result.remote_url, 'https://fanqienovel.com/main/writer/book-info/7650001');
  assert.equal(result.resultPayload.task_kind, 'create_book');
  assert.equal(result.resultPayload.remote_book_id, '7650001');
  assert.equal(harness.calls.length, 1);
  assert.equal(harness.calls[0].args[0], '雾港潮生录');
  assert.deepEqual(plain(harness.calls[0].args[1]), META);
  assert.equal(harness.calls[0].args[2].stayOnBookPage, true);
});

test('run-create-book on Fanqie reports an unknown outcome when no book-info URL is reached', async () => {
  const harness = createBookHarness({
    href: FANQIE_CREATE_URL,
    fanqie: async (_page, _name, _meta, options) => {
      options.onSubmitted();
      return true;
    },
  });

  const result = await harness.run({ platform: 'fanqie', book_name: 'B', book_meta: META });

  assert.equal(result.ok, false);
  assert.equal(result.errorCode, 'create-book-outcome-unknown');
  assert.equal(result.remote_book_id, undefined);
  assert.equal(result.resultPayload.submitted, true);
});

test('run-create-book on Qidian returns the id from the start-writing link without clicking it', async () => {
  const startWriting = new FakeElement({
    text: '开始写作',
    attrs: { href: '/portal/booknovels/chaptertmp/CBID/222#ccid=new' },
  });
  const harness = createBookHarness({
    href: QIDIAN_CREATE_URL,
    qidian: async (_page, _name, _meta, options) => {
      options.onSubmitted();
      options.onStartWritingReady(startWriting);
      return false;
    },
  });

  const result = await harness.run({ platform: 'qidian', book_name: 'B', book_meta: META });

  assert.equal(result.ok, true);
  assert.equal(result.remote_book_id, '222');
  assert.equal(result.remote_url, 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222');
  assert.equal(harness.calls[0].args[2].stayOnBookPage, true);
  assert.deepEqual(startWriting.mutations, []);
});

test('run-create-book on Qidian reports an unknown outcome when the id is not readable', async () => {
  for (const href of [
    null,
    '',
    '/portal/booknovels/addType/abc',
    'javascript:void(0)',
    'https://example.com/portal/booknovels/chaptertmp/CBID/222',
  ]) {
    const attrs = href === null ? {} : { href };
    const startWriting = new FakeElement({ text: '开始写作', attrs });
    const harness = createBookHarness({
      href: QIDIAN_CREATE_URL,
      qidian: async (_page, _name, _meta, options) => {
        options.onSubmitted();
        options.onStartWritingReady(startWriting);
        return false;
      },
    });

    const result = await harness.run({ platform: 'qidian', book_name: 'B', book_meta: META });

    assert.equal(result.ok, false, `href ${href}`);
    assert.equal(result.errorCode, 'create-book-outcome-unknown', `href ${href}`);
    assert.equal(result.resultPayload.submitted, true);
    assert.equal(result.remote_book_id, undefined);
  }
});

test('run-create-book on Qidian reports an unknown outcome when no start-writing entry appeared', async () => {
  const harness = createBookHarness({
    href: QIDIAN_CREATE_URL,
    text: '创建成功',
    qidian: async (_page, _name, _meta, options) => {
      options.onSubmitted();
      return false;
    },
  });

  const result = await harness.run({ platform: 'qidian', book_name: 'B', book_meta: META });

  assert.equal(result.errorCode, 'create-book-outcome-unknown');
  assert.match(result.resultPayload.platform_message, /创建成功/);
});

test('run-create-book treats an unexpected error after the submit click as an unknown outcome', async () => {
  const broken = { getAttribute() { throw new Error('页面对象已失效'); } };
  const harness = createBookHarness({
    href: QIDIAN_CREATE_URL,
    qidian: async (_page, _name, _meta, options) => {
      options.onSubmitted();
      options.onStartWritingReady(broken);
      return false;
    },
  });

  const result = await harness.run({ platform: 'qidian', book_name: 'B', book_meta: META });

  assert.equal(result.ok, false);
  assert.equal(result.errorCode, 'create-book-outcome-unknown');
  assert.equal(result.resultPayload.cause_message, '页面对象已失效');
});

test('run-create-book keeps failures before the submit click definitive', async () => {
  const error = Object.assign(new Error('标签选择失败'), { code: 'create-book-validation-failed' });
  const harness = createBookHarness({
    href: FANQIE_CREATE_URL,
    fanqie: async () => { throw error; },
  });

  const result = await harness.run({ platform: 'fanqie', book_name: 'B', book_meta: META });

  assert.equal(result.ok, false);
  assert.equal(result.errorCode, 'create-book-validation-failed');
  assert.equal(result.resultPayload.submitted, false);
});

test('run-create-book treats an uncoded failure before submit as definitive too', async () => {
  const harness = createBookHarness({
    href: QIDIAN_CREATE_URL,
    qidian: async () => { throw new Error('未找到可填写的输入框'); },
  });

  const result = await harness.run({ platform: 'qidian', book_name: 'B', book_meta: META });

  assert.equal(result.errorCode, 'create-book-failed');
  assert.equal(result.resultPayload.submitted, false);
});

test('run-create-book keeps explicit platform refusals after submit definitive', async () => {
  const limited = Object.assign(new Error('当日创建作品上限'), {
    code: 'create-book-rate-limited',
    resultPayload: { platform_reason: 'daily-create-limit' },
  });
  const rejected = Object.assign(new Error('作品名称已存在'), { code: 'create-book-validation-failed' });
  for (const [error, code] of [[limited, 'create-book-rate-limited'], [rejected, 'create-book-validation-failed']]) {
    const harness = createBookHarness({
      href: FANQIE_CREATE_URL,
      fanqie: async (_page, _name, _meta, options) => {
        options.onSubmitted();
        throw error;
      },
    });

    const result = await harness.run({ platform: 'fanqie', book_name: 'B', book_meta: META });

    assert.equal(result.ok, false);
    assert.equal(result.errorCode, code);
    assert.equal(result.resultPayload.submitted, true);
  }
});

test('run-create-book reports an unknown outcome for an unclear failure after submit', async () => {
  const unclear = [
    Object.assign(new Error('未进入书本详情'), {
      code: 'create-book-validation-failed',
      resultPayload: { platform_reason: 'book-page-not-reached' },
    }),
    Object.assign(new Error('没有开始写作入口'), { code: 'chapter-editor-navigation-failed' }),
    new Error('页面脚本异常'),
  ];
  for (const error of unclear) {
    const harness = createBookHarness({
      href: FANQIE_CREATE_URL,
      text: '请完成验证',
      fanqie: async (_page, _name, _meta, options) => {
        options.onSubmitted();
        throw error;
      },
    });

    const result = await harness.run({ platform: 'fanqie', book_name: 'B', book_meta: META });

    assert.equal(result.ok, false, error.message);
    assert.equal(result.errorCode, 'create-book-outcome-unknown', error.message);
    assert.equal(result.resultPayload.cause_message, error.message);
  }
});

test('run-create-book refuses to touch a page that is not the create page', async () => {
  for (const [platform, href] of [
    ['fanqie', 'https://fanqienovel.com/main/writer/'],
    ['qidian', 'https://write.qq.com/portal/dashboard'],
    ['fanqie', 'https://example.com/main/writer/create'],
  ]) {
    const harness = createBookHarness({ href });

    const result = await harness.run({ platform, book_name: 'B', book_meta: META });

    assert.equal(result.ok, false);
    assert.equal(result.errorCode, 'create-book-page-not-ready');
    assert.deepEqual(harness.calls, []);
  }
});

test('run-create-book validates its input before any mutation', async () => {
  const harness = createBookHarness({ href: FANQIE_CREATE_URL });

  const unsupported = await harness.run({ platform: 'weibo', book_name: 'B', book_meta: META });
  const nameless = await harness.run({ platform: 'fanqie', book_name: '  ', book_meta: META });

  assert.equal(unsupported.errorCode, 'create-book-platform-unsupported');
  assert.equal(nameless.errorCode, 'create-book-validation-failed');
  assert.deepEqual(harness.calls, []);
});

test('run-create-book stops on a risk signal before creating anything', async () => {
  const harness = createBookHarness({
    href: FANQIE_CREATE_URL,
    risk: () => ({ ok: false, riskPause: true, riskReason: 'captcha', errorCode: 'publisher-risk-pause' }),
  });

  const result = await harness.run({ platform: 'fanqie', book_name: 'B', book_meta: META });

  assert.equal(result.riskPause, true);
  assert.equal(result.riskReason, 'captcha');
  assert.deepEqual(harness.calls, []);
});

test('run-create-book leaves the cover to cover_upload and says so', async () => {
  const harness = createBookHarness({
    href: FANQIE_CREATE_URL,
    fanqie: async ({ location }, _name, _meta, options) => {
      options.onSubmitted();
      location.href = 'https://fanqienovel.com/main/writer/book-info/9';
      return true;
    },
  });

  const result = await harness.run({
    platform: 'fanqie', book_name: 'B', book_meta: META, cover_path: 'file:///covers/a.png',
  });

  assert.equal(result.ok, true);
  assert.equal(result.resultPayload.cover_deferred, true);
  assert.equal(harness.calls[0].args.length, 3);
  assert.doesNotMatch(JSON.stringify(plain(harness.calls)), /covers\/a\.png/);
});

// ---------------------------------------------------------------------------
// createFanqieBook / createQidianBook keep their legacy behavior by default

const FANQIE_BOOK_NAMES = [
  'includesAny', 'makeUploadError', 'normalizeFanqieBookMeta', 'createFanqieBook',
];

function fanqieCreateHarness({ pageTextValue, finalHref }) {
  const navigations = [];
  const events = [];
  const location = {
    get href() { return finalHref; },
    set href(value) { navigations.push(value); },
  };
  const context = {
    window: { location },
    ensureFanqieCreateBookPage: async () => true,
    fillField: async () => {},
    setFanqieAudience: () => {},
    sleep: async () => {},
    selectFanqieTags: async () => true,
    selectFanqieContestIfPresent: async () => {},
    clickActionExactText: async () => { events.push('submit-click'); return true; },
    clickExactText: async () => true,
    clickByKeywords: async () => true,
    pageText: () => pageTextValue,
    waitForUrlContains: async () => true,
    waitForPageSignal: async () => true,
  };
  const { createFanqieBook } = load(FANQIE_BOOK_NAMES, context);
  return { createFanqieBook, navigations, events };
}

test('createFanqieBook still returns to the works page by default', async () => {
  const harness = fanqieCreateHarness({
    pageTextValue: '书籍信息待审核 雾港潮生录',
    finalHref: 'https://fanqienovel.com/main/writer/book-info/7650001',
  });

  const created = await harness.createFanqieBook('雾港潮生录', {});

  assert.equal(created, true);
  assert.deepEqual(harness.navigations, ['https://fanqienovel.com/main/writer/']);
});

test('createFanqieBook stayOnBookPage keeps the book page and reports the submit click', async () => {
  const harness = fanqieCreateHarness({
    pageTextValue: '书籍信息待审核 雾港潮生录',
    finalHref: 'https://fanqienovel.com/main/writer/book-info/7650001',
  });
  const events = [];

  const created = await harness.createFanqieBook('雾港潮生录', {}, {
    stayOnBookPage: true,
    onSubmitted: () => events.push(`submitted-after-${harness.events.join(',')}`),
  });

  assert.equal(created, true);
  assert.deepEqual(harness.navigations, []);
  assert.deepEqual(events, ['submitted-after-submit-click']);
});

test('createFanqieBook marks "no book page after submit" as unclear, not as a refusal', async () => {
  const harness = fanqieCreateHarness({
    pageTextValue: '某个别的页面',
    finalHref: 'https://fanqienovel.com/main/writer/create',
  });

  await assert.rejects(
    harness.createFanqieBook('雾港潮生录', {}, { stayOnBookPage: true }),
    (error) => error.code === 'create-book-validation-failed'
      && error.resultPayload?.platform_reason === 'book-page-not-reached',
  );
});

const QIDIAN_BOOK_NAMES = [
  'includesAny', 'makeUploadError', 'normalizeQidianBookMeta', 'resolveQidianCategory',
  'createQidianBook',
];

function qidianCreateHarness({ withStartWriting = false, pageTextValue = '' } = {}) {
  const events = [];
  const navigations = [];
  class HTMLElement {}
  class HTMLInputElement extends HTMLElement {}
  class HTMLSelectElement extends HTMLElement {}
  const submit = Object.assign(new HTMLElement(), { click: () => events.push('submit-click') });
  const trigger = Object.assign(new HTMLElement(), { click: () => events.push('category-click') });
  const startWriting = Object.assign(new HTMLElement(), {
    innerText: '开始写作',
    click: () => events.push('start-writing-click'),
  });
  const location = {
    get href() { return QIDIAN_CREATE_URL; },
    set href(value) { navigations.push(value); },
  };
  const context = {
    HTMLElement,
    HTMLInputElement,
    HTMLSelectElement,
    window: { location },
    document: {
      querySelector: (selector) => ({
        'select[name="category"]': null,
        '.write-form-li .ui-select-button': trigger,
        'button.jSubmit, .jSubmit': submit,
      })[selector] ?? null,
      querySelectorAll: () => (withStartWriting ? [startWriting] : []),
    },
    waitForSelector: async () => new HTMLInputElement(),
    fillNode: () => {},
    sleep: async () => {},
    selectQidianPublishSite: async () => true,
    waitForQidianCategoryReady: async () => true,
    clickExactText: async () => true,
    clickByKeywords: async () => true,
    maybeSelectQidianSubcategory: async () => true,
    maybeFillQidianIntro: async () => true,
    maybeChooseQidianPrecollect: async () => true,
    maybeChooseQidianContestDefault: async () => true,
    pageText: () => pageTextValue,
    isVisibleElement: () => true,
    waitForCondition: async (check) => check(),
  };
  const { createQidianBook } = load(QIDIAN_BOOK_NAMES, context);
  return { createQidianBook, events, navigations, startWriting };
}

test('createQidianBook still clicks start-writing by default', async () => {
  const harness = qidianCreateHarness({ withStartWriting: true });

  const result = await harness.createQidianBook('书名', {});

  assert.equal(result, false);
  assert.deepEqual(harness.events.filter((event) => event.endsWith('click') && event !== 'category-click'), [
    'submit-click', 'start-writing-click',
  ]);
});

test('createQidianBook stayOnBookPage hands over start-writing without clicking or navigating', async () => {
  const harness = qidianCreateHarness({ withStartWriting: true });
  const seen = [];

  const result = await harness.createQidianBook('书名', {}, {
    stayOnBookPage: true,
    onSubmitted: () => seen.push(`submitted-after-${harness.events.at(-1)}`),
    onStartWritingReady: (node) => seen.push(node === harness.startWriting ? 'node' : 'other'),
  });

  assert.equal(result, false);
  assert.deepEqual(seen, ['submitted-after-submit-click', 'node']);
  assert.ok(!harness.events.includes('start-writing-click'));
  assert.deepEqual(harness.navigations, []);
});

test('createQidianBook stayOnBookPage does not navigate when only the success text is visible', async () => {
  const harness = qidianCreateHarness({ pageTextValue: '作品创建成功' });
  const seen = [];

  const result = await harness.createQidianBook('书名', {}, {
    stayOnBookPage: true,
    onSubmitted: () => seen.push('submitted'),
    onStartWritingReady: () => seen.push('node'),
  });

  assert.equal(result, false);
  assert.deepEqual(seen, ['submitted']);
  assert.deepEqual(harness.navigations, []);
});

test('createQidianBook without the option still goes to the dashboard on the success text', async () => {
  const harness = qidianCreateHarness({ pageTextValue: '作品创建成功' });

  const result = await harness.createQidianBook('书名', {});

  assert.equal(result, false);
  assert.deepEqual(harness.navigations, ['https://write.qq.com/portal/dashboard']);
});

// ---------------------------------------------------------------------------
// reconcile-create

const RECONCILE_CREATE_NAMES = [
  'includesAny', 'canonicalPublisherUrl', 'remoteBookIdFromPublisherUrl', 'isWorksListPage',
  'exactTitleOfWorkCard', 'listFanqieBookCandidates', 'listQidianBookCandidates',
  'reconcileCreateReadOnly',
];

const FANQIE_WORKS_URL = 'https://fanqienovel.com/main/writer/';
const QIDIAN_WORKS_URL = 'https://write.qq.com/portal/dashboard';

function fanqieCard(title, id, mutations, { extraLink = false } = {}) {
  const card = new FakeElement({ text: `${title} 章节管理 创建章节`, mutations });
  const anchor = new FakeElement({
    text: '章节管理',
    attrs: { href: `/main/writer/chapter-manage/${id}&${encodeURIComponent(title)}?type=1` },
    parentElement: card,
    mutations,
  });
  const anchors = [anchor];
  if (extraLink) {
    anchors.push(new FakeElement({
      text: '管理',
      attrs: { href: `/main/writer/chapter-manage/${id}` },
      parentElement: card,
      mutations,
    }));
  }
  return anchors;
}

function qidianCard(title, href, mutations) {
  const link = new FakeElement({ text: '去写作', attrs: { href: href || '' }, mutations });
  return new FakeElement({
    text: `${title} 去写作 草稿`, links: href ? [link] : [], mutations,
  });
}

function reconcileCreateHarness({ href, anchors = [], cards = [], risk = noRisk }) {
  const context = {
    window: { location: { href } },
    document: {
      querySelectorAll: (selector) => {
        if (selector === 'a[href*="/chapter-manage/"]') return anchors;
        if (selector === '.g-prodution-item') return cards;
        return [];
      },
    },
    pageText: () => '',
    buildRiskPauseResult: risk,
    setDebugStep() {},
  };
  const { reconcileCreateReadOnly } = load(RECONCILE_CREATE_NAMES, context);
  return async (payload) => plain(await reconcileCreateReadOnly(payload));
}

test('reconcile-create on Fanqie lists name candidates and never claims found or absent', async () => {
  const mutations = [];
  const anchors = [
    ...fanqieCard('雾港潮生录', '7650001', mutations, { extraLink: true }),
    ...fanqieCard('雾港潮生录（二）', '7650002', mutations),
    ...fanqieCard('别的书', '7650003', mutations),
  ];
  const reconcile = reconcileCreateHarness({ href: FANQIE_WORKS_URL, anchors });

  const result = await reconcile({ platform: 'fanqie', book_name: '雾港潮生录' });

  assert.equal(result.finding, 'unknown');
  assert.equal(result.works_list_complete, false);
  assert.deepEqual(result.candidates.map((item) => item.remote_book_id), ['7650001', '7650002']);
  assert.equal(result.candidates[0].remote_url, 'https://fanqienovel.com/main/writer/chapter-manage/7650001');
  assert.match(result.candidates[0].text, /雾港潮生录/);
  assert.equal(result.candidates[0].exact_title, null);
  assert.deepEqual(mutations, []);
});

test('reconcile-create on Fanqie stays unknown even when exactly one exact-looking card exists', async () => {
  const anchors = fanqieCard('雾港潮生录', '7650001', []);
  const reconcile = reconcileCreateHarness({ href: FANQIE_WORKS_URL, anchors });

  const result = await reconcile({ platform: 'fanqie', book_name: '雾港潮生录' });

  assert.equal(result.finding, 'unknown');
  assert.equal(result.candidates.length, 1);
});

test('reconcile-create stays unknown, not absent, when nothing matches the name', async () => {
  const fanqie = reconcileCreateHarness({
    href: FANQIE_WORKS_URL, anchors: fanqieCard('别的书', '1', []),
  });
  const qidian = reconcileCreateHarness({
    href: QIDIAN_WORKS_URL,
    cards: [qidianCard('别的书', '/portal/booknovels/chaptertmp/CBID/5', [])],
  });

  for (const [reconcile, platform] of [[fanqie, 'fanqie'], [qidian, 'qidian']]) {
    const result = await reconcile({ platform, book_name: '雾港潮生录' });
    assert.equal(result.finding, 'unknown', platform);
    assert.deepEqual(result.candidates, [], platform);
    assert.notEqual(result.finding, 'absent');
  }
});

test('reconcile-create on Qidian lists matching dashboard cards with the id from the card link', async () => {
  const mutations = [];
  const cards = [
    qidianCard('雾港潮生录', '/portal/booknovels/chaptertmp/CBID/222?x=1', mutations),
    qidianCard('雾港潮生录 外传', '', mutations),
    qidianCard('别的书', '/portal/booknovels/chaptertmp/CBID/333', mutations),
  ];
  const reconcile = reconcileCreateHarness({ href: QIDIAN_WORKS_URL, cards });

  const result = await reconcile({ platform: 'qidian', book_name: '雾港潮生录' });

  assert.equal(result.finding, 'unknown');
  assert.deepEqual(plain(result.candidates.map((item) => [item.remote_book_id, item.remote_url])), [
    ['222', 'https://write.qq.com/portal/booknovels/chaptertmp/CBID/222'],
    ['', ''],
  ]);
  assert.equal(result.candidates[0].exact_title, null);
  assert.deepEqual(mutations, []);
});

test('reconcile-create only reads the works list page of the requested platform', async () => {
  const anchors = fanqieCard('雾港潮生录', '7650001', []);
  const cases = [
    ['fanqie', 'https://fanqienovel.com/main/writer/create'],
    ['fanqie', 'https://write.qq.com/portal/dashboard'],
    ['qidian', 'https://write.qq.com/portal/dashboard/create-novel'],
    ['qidian', 'https://fanqienovel.com/main/writer/'],
    ['weibo', FANQIE_WORKS_URL],
  ];
  for (const [platform, href] of cases) {
    const reconcile = reconcileCreateHarness({ href, anchors });

    const result = await reconcile({ platform, book_name: '雾港潮生录' });

    assert.equal(result.finding, 'unknown', `${platform} ${href}`);
    assert.deepEqual(result.candidates, [], `${platform} ${href}`);
    assert.equal(result.reason, 'works-list-page-not-open', `${platform} ${href}`);
  }
});

test('reconcile-create without a book name lists nothing', async () => {
  const reconcile = reconcileCreateHarness({
    href: FANQIE_WORKS_URL, anchors: fanqieCard('雾港潮生录', '7650001', []),
  });

  const result = await reconcile({ platform: 'fanqie', book_name: ' ' });

  assert.equal(result.finding, 'unknown');
  assert.deepEqual(result.candidates, []);
});

test('reconcile-create reports a risk signal as a pause while keeping finding unknown', async () => {
  const reconcile = reconcileCreateHarness({
    href: FANQIE_WORKS_URL,
    risk: () => ({
      riskPause: true,
      riskReason: 'captcha',
      error: 'Publisher captcha risk signal detected.',
      riskEvidence: { selector: 'body', matchedText: '滑块验证' },
    }),
  });

  const result = await reconcile({ platform: 'fanqie', book_name: 'B' });

  assert.equal(result.outcome, 'risk_pause');
  assert.equal(result.riskReason, 'captcha');
  assert.equal(result.finding, 'unknown');
  assert.equal(result.platformMessage, '滑块验证');
});

test('reconcile-create keeps a named seam for exact-title matching that is off today', () => {
  const { exactTitleOfWorkCard } = load(['exactTitleOfWorkCard'], {});

  assert.equal(exactTitleOfWorkCard('fanqie', {}), null);
  assert.equal(exactTitleOfWorkCard('qidian', {}), null);
});

test('reconcile-create never mutates the page', () => {
  const block = extract([
    'listFanqieBookCandidates', 'listQidianBookCandidates', 'exactTitleOfWorkCard',
    'reconcileCreateReadOnly',
  ]);

  assert.doesNotMatch(block, /\.click\s*\(/);
  assert.doesNotMatch(block, /dispatchEvent\s*\(/);
  assert.doesNotMatch(block, /fill[A-Z]\w*\s*\(/);
  assert.doesNotMatch(block, /clickByKeywords|clickExactText|clickActionExactText/);
  assert.doesNotMatch(block, /window\.location\.(?:href\s*=|assign|replace)/);
  assert.doesNotMatch(block, /create(?:Fanqie|Qidian)Book\s*\(|runUpload\s*\(|runCreateBook\s*\(/);
  assert.doesNotMatch(block, /finding:\s*['"](?:found|absent)['"]/);
});

test('platform agent registers run-create-book and reconcile-create with safe failure replies', () => {
  assert.match(source, /message\.action\s*===\s*'run-create-book'/);
  assert.match(source, /runCreateBook\(message\.payload\s*\|\|\s*\{\}\)/);
  assert.match(source, /message\.action\s*===\s*'reconcile-create'/);
  const reconcileDispatch = source.match(
    /message\.action === 'reconcile-create'[^]*?\n    \}\n/,
  )?.[0] || '';
  assert.match(reconcileDispatch, /reconcileCreateReadOnly\(message\.payload \|\| \{\}\)/);
  assert.match(reconcileDispatch, /finding:\s*'unknown'/);
  assert.match(reconcileDispatch, /candidates:\s*\[\]/);
  assert.match(reconcileDispatch, /return true;/);
});

// ---------------------------------------------------------------------------
// reconcile-upload keeps its strict vocabulary

const RECONCILE_UPLOAD_NAMES = [
  'coverAcceptanceSignal', 'reconcileUploadReadOnly',
];
const HASH = 'a'.repeat(64);

function reconcileUploadHarness({ title = '', body = '', text = '' } = {}) {
  const context = {
    window: { location: { href: 'https://fanqienovel.com/main/writer/publish/1' } },
    pageText: () => text,
    buildRiskPauseResult: noRisk,
    observedCoverRemoteBookId: () => '',
    chapterTitleForReadOnlyReconciliation: () => title,
    chapterBodyForReadOnlyReconciliation: () => body,
    sha256Hex: async (value) => (value === '正文' ? HASH : 'b'.repeat(64)),
    resolveOfficialStatus: () => 'unknown',
  };
  const { reconcileUploadReadOnly } = load(RECONCILE_UPLOAD_NAMES, context);
  return async (payload) => plain(await reconcileUploadReadOnly(payload));
}

test('reconcile-upload answers indeterminate, never absent, for a chapter it cannot find', async () => {
  const reconcile = reconcileUploadHarness({ title: '', body: '', text: '章节管理 共0篇草稿' });
  const payload = {
    task_kind: 'chapter_upload', chapter_title: '第一章', content_sha256: HASH, publish: false,
  };

  const result = await reconcile(payload);

  assert.equal(result.outcome, 'indeterminate');
  assert.equal(result.matched, false);
  assert.notEqual(result.outcome, 'absent');
});

test('reconcile-upload answers indeterminate when title or content hash differ', async () => {
  const payload = {
    task_kind: 'chapter_upload', chapter_title: '第一章', content_sha256: HASH, publish: false,
  };
  const titleOnly = reconcileUploadHarness({ title: '第一章', body: '别的正文' });
  const bodyOnly = reconcileUploadHarness({ title: '第二章', body: '正文' });

  assert.equal((await titleOnly(payload)).outcome, 'indeterminate');
  assert.equal((await bodyOnly(payload)).outcome, 'indeterminate');
});

test('reconcile-upload still reports a match only for title and content hash together', async () => {
  const reconcile = reconcileUploadHarness({ title: '第一章', body: '正文' });

  const result = await reconcile({
    task_kind: 'chapter_upload', chapter_title: '第一章', content_sha256: HASH, publish: false,
  });

  assert.equal(result.outcome, 'matched');
  assert.equal(result.matchedContentSha256, HASH);
});

test('reconcile-upload leaves cover and other task kinds indeterminate', async () => {
  const reconcile = reconcileUploadHarness({ text: '' });

  for (const taskKind of ['cover_upload', 'create_book', 'audit_sync']) {
    const result = await reconcile({ task_kind: taskKind, remote_book_id: '1' });
    assert.equal(result.outcome, 'indeterminate', taskKind);
  }
});

test('reconcile-upload source has no absence verdict at all', () => {
  const block = extract(['reconcileUploadReadOnly']);

  assert.doesNotMatch(block, /absent/);
  assert.doesNotMatch(block, /finding/);
});
