import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');
const names = [
  'fanqieModalTexts', 'hasFanqiePublishSettings', 'findFanqiePublishSettingsModal',
  'chooseFanqieNonAiIfPresent', 'chooseFanqieAiDisclosure',
  'submitFanqiePublishSettings', 'hasFanqieRiskDetectionModal',
  'handleFanqieRiskDetectionModal', 'waitForFanqiePublishSettings',
  'findFanqiePublishStep', 'dismissFanqieGuideModal', 'confirmFanqiePublishIfNeeded',
];
const implementation = names.map((name) => source.match(
  new RegExp(`  (?:async )?function ${name}\\([^]*?\\n  \\}`),
)?.[0] || '').join('\n');

function fixture({ steps = ['settings'], aiField = true, selectable = true, hidden = false } = {}) {
  const clicks = [];
  let step = 0;
  class Element {
    constructor(text = '') { this.innerText = text; this.visible = !hidden; }
    dispatchEvent() {}
  }
  class Input extends Element { constructor(value) { super(); this.value = value; this.checked = false; } }
  const yes = new Input('1');
  const no = new Input('2');
  const label = (text, input, other) => {
    const element = new Element(text);
    element.querySelector = () => input;
    element.click = () => {
      clicks.push(text);
      if (selectable) { input.checked = true; other.checked = false; }
    };
    input.click = element.click;
    return element;
  };
  const labels = [label('是', yes, no), label('否', no, yes)];
  const button = (text) => {
    const element = new Element(text);
    element.click = () => { clicks.push(text); step += 1; };
    return element;
  };
  const modals = steps.map((kind) => {
    const text = {
      settings: `发布设置\n章节\n第1章\n${aiField ? '是否使用AI\n是\n否' : ''}`,
      typo: '发布提示\n检测到你还有错别字未修改，是否确定提交？',
      detection: '请选择内容检测方式\n全面检测（本章节剩余次数：2/2次）\n基础检测（不限次数）',
      unrelated: 'AI灵感生成\n下一步',
      unknown: '新的未知发布警告 是否继续？ 继续',
    }[kind];
    const modal = new Element(text);
    const buttons = ({ settings: ['取消', '确认发布'], typo: ['取消', '提交'],
      detection: ['仅基础检测', '全面检测'], unrelated: ['下一步'], unknown: ['继续'] })[kind].map(button);
    modal.querySelectorAll = (selector) => selector.includes('label') ? (aiField ? labels : []) : buttons;
    modal.querySelector = (selector) => {
      if (selector.includes('value="1"')) return aiField ? yes : null;
      if (selector.includes('value="2"')) return aiField ? no : null;
      if (selector.includes('button')) return buttons.at(-1);
      return null;
    };
    return modal;
  });
  const current = () => modals[step] || null;
  const clickTexts = async (texts) => {
    const target = current()?.querySelectorAll('button').find((node) => texts.includes(node.innerText));
    if (target) target.click();
    return Boolean(target);
  };
  const context = {
    HTMLElement: Element, HTMLInputElement: Input, Event: class {},
    window: { location: { href: 'https://fanqienovel.com/main/writer/1/publish/2/' } },
    document: {
      querySelector: (selector) => selector === '.publish-confirm-container-new'
        && current()?.innerText.includes('发布设置') ? current() : null,
      querySelectorAll: () => current() ? [current()] : [],
    },
    pageText: () => `AI灵感生成\n下一步\n${current()?.innerText || ''}`,
    includesAny: (text, values) => values.some((value) => text.includes(value)),
    isVisibleElement: (node) => Boolean(node && node.visible),
    waitForCondition: async (check) => check() || null,
    sleep: async () => {}, dismissFanqieGuideSteps: async () => {}, setDebugStep: () => {},
    clickExactTexts: clickTexts, clickByKeywords: clickTexts,
    clickActionExactText: (label) => clickTexts([label]), clickExactText: (label) => clickTexts([label]),
  };
  return {
    clicks, yes, no,
    confirm: (publish) => runInNewContext(`${implementation}\nconfirmFanqiePublishIfNeeded(${publish})`, context),
    detected: () => runInNewContext(`${implementation}\nhasFanqiePublishSettings()`, context),
  };
}

test('draft-only uploads never advance or confirm a publication modal', async () => {
  const page = fixture();
  assert.equal(await page.confirm(false), false);
  assert.deepEqual(page.clicks, []);
});

test('ForWin publication truthfully declares AI use before confirmation', async () => {
  const page = fixture();
  assert.equal(await page.confirm(true), true);
  assert.equal(page.yes.checked, true);
  assert.equal(page.no.checked, false);
  assert.deepEqual(page.clicks, ['是', '确认发布']);
});

test('publication stops if the AI declaration cannot be selected', async () => {
  const page = fixture({ selectable: false });
  assert.equal(await page.confirm(true), false);
  assert.equal(page.clicks.includes('确认发布'), false);
});

test('an unrecognized disclosure form is never submitted', async () => {
  const page = fixture({ aiField: false });
  assert.equal(await page.confirm(true), false);
  assert.deepEqual(page.clicks, []);
});

test('AI tools and the editor next button are not publication settings', async () => {
  const page = fixture({ steps: ['unrelated'] });
  assert.equal(page.detected(), false);
  assert.equal(await page.confirm(true), false);
  assert.deepEqual(page.clicks, []);
});

test('hidden publication dialogs are ignored', () => {
  assert.equal(fixture({ hidden: true }).detected(), false);
});

test('unknown publication warnings are not dismissed as onboarding guides', async () => {
  const page = fixture({ steps: ['unknown'] });
  assert.equal(await page.confirm(true), false);
  assert.deepEqual(page.clicks, []);
});

test('known typo and content-check dialogs advance in order before disclosure', async () => {
  const page = fixture({ steps: ['typo', 'detection', 'settings'] });
  assert.equal(await page.confirm(true), true);
  assert.deepEqual(page.clicks, ['提交', '仅基础检测', '是', '确认发布']);
});
