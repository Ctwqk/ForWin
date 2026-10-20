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

const CONFIRM_SOURCE = extract([
  'isVisibleElement', 'pageText', 'includesAny',
  'findFanqiePublishSettingsModal', 'hasFanqiePublishSettings', 'chooseFanqieAiDisclosure',
  'submitFanqiePublishSettings', 'findFanqiePublishStep', 'confirmFanqiePublishIfNeeded',
]);
const UPLOAD_SOURCE = `${CONFIRM_SOURCE}\n${extract([
  'runUpload', 'clickAction', 'waitForPageSignal', 'verifyPublishOutcome', 'resolveOfficialStatus',
  'chapterRemoteIdentity', 'buildFanqieDraftVerifyRequest', 'extractFanqieWorkId',
])}`;
const HASH = 'a'.repeat(64);

function matchesCompound(node, selector) {
  const parsed = selector.match(/^([a-z]+)?((?:\.[\w-]+)*)((?:\[[\w-]+(?:="[^"]*")?\])*)$/);
  if (!parsed) {
    throw new Error(`fake DOM does not support selector: ${selector}`);
  }
  const [, tag, classes, attrs] = parsed;
  return (!tag || node.tagName === tag)
    && classes.split('.').filter(Boolean).every((name) => node.classList.has(name))
    && Array.from(attrs.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g))
      .every(([, name, value]) => (value === undefined ? name in node.attrs : node.attrs[name] === value));
}

class FakeElement {
  constructor(tagName, { className = '', attrs = {}, text = '', onClick = null } = {}, children = []) {
    Object.assign(this, { tagName, attrs, text, onClick, hidden: false, parentElement: null, children: [] });
    this.classList = new Set(className.split(/\s+/).filter(Boolean));
    children.forEach((child) => this.append(child));
  }

  append(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }

  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
      this.parentElement = null;
    }
  }

  get shown() { return !this.hidden && (!this.parentElement || this.parentElement.shown); }
  get offsetWidth() { return this.shown ? 100 : 0; }
  get offsetHeight() { return this.shown ? 24 : 0; }
  getClientRects() { return this.shown ? [{ width: 100, height: 24 }] : []; }

  get innerText() {
    return [this.text, ...this.children.filter((child) => !child.hidden).map((child) => child.innerText)]
      .filter(Boolean).join('\n');
  }

  get textContent() { return [this.text, ...this.children.map((child) => child.textContent)].join(''); }

  matches(selector) { return selector.split(',').some((part) => matchesCompound(this, part.trim())); }

  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => node.children.forEach((child) => {
      if (child.matches(selector)) found.push(child);
      walk(child);
    });
    walk(this);
    return found;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  click() { this.onClick?.(); }
  dispatchEvent() { return true; }
}

class FakeInput extends FakeElement {
  constructor(attrs) {
    super('input', { attrs });
    this.checked = false;
  }
}

function fanqiePage({
  steps = ['settings'], opened = true, aiField = true, otherQuestion = false, scheduleRadios = false,
  layout = 'nested', selectable = true, hidden = false, closesOnConfirm = true, pageNote = '',
} = {}) {
  const clicks = [];
  const genericClicks = [];
  const radios = {};
  const status = new FakeElement('div', { text: pageNote });
  const body = new FakeElement('body', {}, [
    new FakeElement('div', { className: 'ProseMirror', text: '第18章 归途\n正文第一段。' }),
    status,
    new FakeElement('button', {
      className: 'publish-button auto-editor-next',
      text: '下一步',
      onClick: () => { clicks.push('下一步'); advance(); },
    }),
  ]);
  const button = (text, action = () => {}) => new FakeElement('button', {
    text, onClick: () => { clicks.push(text); action(); },
  });
  const question = (name, prompt, [first, second] = ['是', '否']) => {
    const inputs = [new FakeInput({ type: 'radio', name }), new FakeInput({ type: 'radio', name })];
    radios[name] = { yes: inputs[0], no: inputs[1] };
    const label = (text, input) => new FakeElement('label', {
      className: 'arco-radio',
      text,
      onClick: () => {
        clicks.push(text);
        if (selectable) inputs.forEach((candidate) => { candidate.checked = candidate === input; });
      },
    }, [input]);
    return { prompt: new FakeElement('div', { text: prompt }), labels: [label(first, inputs[0]), label(second, inputs[1])] };
  };
  const modal = (children) => new FakeElement('div', { className: 'arco-modal', attrs: { role: 'dialog' } }, children);
  const settingsModal = () => {
    const questions = [
      ...(otherQuestion ? [question('contest', '是否参与征文活动')] : []),
      ...(aiField ? [question('ai', '是否使用AI')] : []),
      ...(scheduleRadios ? [question('schedule', '发布时间', ['立即发布', '定时发布'])] : []),
    ];
    const form = layout === 'flat'
      ? questions.flatMap((item) => [item.prompt, ...item.labels])
      : questions.map((item) => new FakeElement('div', { className: 'arco-form-item' }, [
        item.prompt,
        new FakeElement('div', { className: 'arco-radio-group' }, item.labels),
      ]));
    const settings = modal([
      new FakeElement('div', { text: '发布设置' }),
      new FakeElement('div', { text: '章节\n第18章' }),
      new FakeElement('div', { className: 'arco-form' }, form),
      new FakeElement('div', { text: '定时发布' }),
      new FakeElement('div', { className: 'arco-modal-footer' }, [
        button('取消'),
        button('确认发布', () => {
          if (closesOnConfirm) {
            settings.remove();
            status.text = '审核中';
          }
        }),
      ]),
    ]);
    return settings;
  };
  const prompt = (text, labels, next) => modal([
    new FakeElement('div', { text }),
    ...labels.map((label) => button(label, label === next ? advance : () => {})),
  ]);
  const builders = {
    settings: settingsModal,
    typo: () => prompt('发布提示\n检测到你还有错别字未修改，是否确定提交？', ['取消', '提交'], '提交'),
    detection: () => prompt('请选择内容检测方式\n全面检测（本章节剩余次数：2/2次）\n基础检测（不限次数）', ['仅基础检测', '全面检测'], '仅基础检测'),
    unrelated: () => prompt('AI灵感生成', ['下一步'], '下一步'),
    unknown: () => prompt('新的未知发布警告 是否继续？', ['继续'], '继续'),
    review: () => prompt('发布提示\n本章将提交审核，是否继续？', ['继续'], '继续'),
  };
  let current = null;
  let index = -1;
  function advance() {
    current?.remove();
    index += 1;
    current = steps[index] ? body.append(builders[steps[index]]()) : null;
    if (current) current.hidden = hidden;
  }
  if (opened) advance();

  const generic = (labels) => { genericClicks.push(labels); return false; };
  const context = {
    HTMLElement: FakeElement,
    HTMLInputElement: FakeInput,
    Event: class { constructor(type) { this.type = type; } },
    URL,
    URLSearchParams,
    window: { location: { href: 'https://fanqienovel.com/main/writer/7000/publish/8000' } },
    document: {
      body,
      querySelector: (selector) => body.querySelector(selector),
      querySelectorAll: (selector) => body.querySelectorAll(selector),
    },
    waitForCondition: async (check) => check() || null,
    sleep: async () => {},
    setDebugStep: () => {},
    clickExactTexts: async (labels) => generic(labels),
    clickByKeywords: async (labels) => generic(labels),
    clickActionExactText: async (label) => generic([label]),
    clickExactText: async (label) => generic([label]),
    buildRiskPauseResult: () => ({ riskPause: false }),
    preparePublishPage: async () => true,
    waitForEditorReady: async () => true,
    fillField: async () => true,
    selectorsForTitle: () => [],
    fillFanqieSequence: async () => true,
    fillFanqieTitle: async () => true,
    dismissFanqieGuideModal: async () => {},
    readFanqieEditorStatus: () => ({ bodyCharCount: 120 }),
    locateFanqieTrustedBodyTarget: () => null,
    waitForFanqieSaved: async () => true,
    chapterContentEvidence: async () => ({
      matched: true,
      resultPayload: { observed_content_sha256: HASH, content_match_basis: 'normalized-editor-text-sha256' },
    }),
  };
  const payload = (publish) => JSON.stringify({
    platform: 'fanqie',
    book_name: '归途',
    chapter_title: '第18章 归途',
    body: '正文第一段。',
    publish,
    trustedBodyDone: true,
    content_sha256: HASH,
  });
  return {
    clicks,
    genericClicks,
    radios,
    detected: () => runInNewContext(`${CONFIRM_SOURCE}\nhasFanqiePublishSettings()`, context),
    confirm: (publish) => runInNewContext(`${CONFIRM_SOURCE}\nconfirmFanqiePublishIfNeeded(${publish})`, context),
    upload: (publish) => runInNewContext(`${UPLOAD_SOURCE}\nrunUpload(${payload(publish)})`, context),
  };
}

test('draft-only uploads never advance or confirm a publication modal', async () => {
  const page = fanqiePage();
  const result = await page.confirm(false);
  assert.equal(result.confirmed, false);
  assert.equal(result.phase, 'not-requested');
  assert.deepEqual(page.clicks, []);
});

test('ForWin publication truthfully declares AI use before confirmation', async () => {
  const page = fanqiePage();
  const result = await page.confirm(true);
  assert.equal(result.confirmed, true);
  assert.equal(result.phase, 'confirmed');
  assert.equal(page.radios.ai.yes.checked, true);
  assert.equal(page.radios.ai.no.checked, false);
  assert.deepEqual(page.clicks, ['是', '确认发布']);
});

test('publication stops if the AI declaration cannot be selected', async () => {
  const page = fanqiePage({ selectable: false });
  const result = await page.confirm(true);
  assert.equal(result.confirmed, false);
  assert.equal(result.phase, 'ai-disclosure-unverified');
  assert.equal(page.clicks.includes('确认发布'), false);
});

test('an unrecognized disclosure form is never submitted', async () => {
  const page = fanqiePage({ aiField: false });
  const result = await page.confirm(true);
  assert.equal(result.confirmed, false);
  assert.equal(result.phase, 'ai-disclosure-unverified');
  assert.deepEqual(page.clicks, []);
});

test('the AI answer is chosen inside the AI question, not another 是/否 question', async () => {
  const page = fanqiePage({ otherQuestion: true });
  const result = await page.confirm(true);
  assert.equal(result.confirmed, true);
  assert.equal(page.radios.ai.yes.checked, true);
  assert.equal(page.radios.ai.no.checked, false);
  assert.equal(page.radios.contest.yes.checked, false);
  assert.equal(page.radios.contest.no.checked, false);
  assert.deepEqual(page.clicks, ['是', '确认发布']);
});

test('an AI question that cannot be separated from other 是/否 answers stops confirmation', async () => {
  const page = fanqiePage({ otherQuestion: true, layout: 'flat' });
  const result = await page.confirm(true);
  assert.equal(result.confirmed, false);
  assert.equal(result.phase, 'ai-disclosure-unverified');
  assert.deepEqual(page.clicks, []);
  assert.equal(page.radios.contest.yes.checked, false);
  assert.equal(page.radios.ai.yes.checked, false);
});

test('a single flat AI question is still answered beside unrelated radios', async () => {
  const page = fanqiePage({ layout: 'flat', scheduleRadios: true });
  const result = await page.confirm(true);
  assert.equal(result.confirmed, true);
  assert.equal(page.radios.ai.yes.checked, true);
  assert.equal(page.radios.schedule.yes.checked, false);
  assert.equal(page.radios.schedule.no.checked, false);
});

test('AI tools and the editor next button are not publication settings', async () => {
  const page = fanqiePage({ steps: ['unrelated'] });
  assert.equal(page.detected(), false);
  const result = await page.confirm(true);
  assert.equal(result.confirmed, false);
  assert.equal(result.phase, 'unrecognized-dialog');
  assert.deepEqual(page.clicks, []);
});

test('hidden publication dialogs are ignored', () => {
  assert.equal(fanqiePage({ hidden: true }).detected(), false);
});

test('unknown publication warnings are not dismissed as onboarding guides', async () => {
  const page = fanqiePage({ steps: ['unknown'] });
  const result = await page.confirm(true);
  assert.equal(result.confirmed, false);
  assert.deepEqual(page.clicks, []);
  assert.deepEqual(page.genericClicks, []);
});

test('known typo and content-check dialogs advance in order before disclosure', async () => {
  const page = fanqiePage({ steps: ['typo', 'detection', 'settings'] });
  const result = await page.confirm(true);
  assert.equal(result.confirmed, true);
  assert.deepEqual(page.clicks, ['提交', '仅基础检测', '是', '确认发布']);
});

test('upload stops with publish-not-confirmed when an unknown dialog halts confirmation', async () => {
  const page = fanqiePage({ steps: ['review'], opened: false });
  const result = await page.upload(true);
  assert.equal(result.ok, false);
  assert.equal(result.errorCode, 'publish-not-confirmed');
  assert.equal(result.resultPayload.confirm_phase, 'unrecognized-dialog');
  assert.equal(result.resultPayload.publish_clicked, false);
  assert.match(result.error, /unrecognized-dialog/);
  assert.deepEqual(page.clicks, ['下一步']);
  assert.deepEqual(page.genericClicks, []);
});

test('upload never turns page text into a receipt after the AI disclosure fails', async () => {
  const page = fanqiePage({ aiField: false, opened: false, pageNote: '已发布章节 17' });
  const result = await page.upload(true);
  assert.equal(result.ok, false);
  assert.equal(result.errorCode, 'publish-not-confirmed');
  assert.equal(result.resultPayload.confirm_phase, 'ai-disclosure-unverified');
  assert.equal(result.resultPayload.official_status, undefined);
  assert.deepEqual(page.clicks, ['下一步']);
});

test('upload reports an unknown remote state when the dialog stays open after 确认发布', async () => {
  const page = fanqiePage({ opened: false, closesOnConfirm: false });
  const result = await page.upload(true);
  assert.equal(result.ok, false);
  assert.equal(result.errorCode, 'publish-not-confirmed');
  assert.equal(result.resultPayload.confirm_phase, 'settings-still-open');
  assert.equal(result.resultPayload.publish_clicked, true);
  assert.match(result.error, /远端状态未知/);
  assert.deepEqual(page.clicks, ['下一步', '是', '确认发布']);
});

test('upload verifies the platform outcome after a confirmed publication', async () => {
  const page = fanqiePage({ steps: ['typo', 'detection', 'settings'], opened: false });
  const result = await page.upload(true);
  assert.equal(result.ok, true);
  assert.equal(result.resultPayload.mode, 'publish');
  assert.equal(result.resultPayload.official_status, 'review-pending');
  assert.equal(result.resultPayload.remote_book_id, '7000');
  assert.equal(result.resultPayload.remote_chapter_id, '8000');
  assert.deepEqual(page.clicks, ['下一步', '提交', '仅基础检测', '是', '确认发布']);
});

test('draft uploads skip publication confirmation and go to draft verification', async () => {
  const page = fanqiePage({ steps: ['typo', 'settings'], opened: false });
  const result = await page.upload(false);
  assert.equal(result.ok, false);
  assert.equal(result.errorCode, 'fanqie-draft-verify-required');
  assert.equal(result.resultPayload.mode, 'draft');
  assert.deepEqual(page.clicks, ['下一步']);
});
