import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');
const implementation = (name) => source.match(new RegExp(`  (?:async )?function ${name}\\([^]*?\\n  \\}`))?.[0];

function bodyEditor(initialParagraphs) {
  class Element {
    focus() {}
    dispatchEvent() {}
  }
  const editor = new Element();
  const paragraph = new Element();
  let paragraphs = [...initialParagraphs];
  let selected;
  const selection = { removeAllRanges() {}, addRange() {} };
  const context = {
    HTMLElement: Element,
    InputEvent: class {},
    Event: class {},
    window: { getSelection: () => selection, location: { href: 'https://fanqienovel.com/editor' } },
    document: {
      querySelector: (selector) => selector.endsWith(' p') ? paragraph : editor,
      createRange: () => ({ selectNodeContents: (node) => { selected = node; } }),
      execCommand: (command, _unused, value) => {
        if (command === 'delete') {
          if (selected === editor) paragraphs = [];
          else paragraphs.shift();
        }
        if (command === 'insertText') paragraphs.unshift(...value.split('\n'));
        return true;
      },
    },
    readFanqieEditorStatus: () => ({ bodyCharCount: paragraphs.join('').length }),
  };
  return {
    apply: (text) => runInNewContext(`${implementation('applyFanqieTrustedBody')}\napplyFanqieTrustedBody(${JSON.stringify(text)})`, context),
    body: () => paragraphs.join('\n'),
  };
}

test('Fanqie replaces every paragraph when retrying an existing draft', () => {
  const editor = bodyEditor(['旧开头', '旧中段', '旧结尾']);
  assert.equal(editor.apply('新开头\n新结尾').ok, true);
  assert.equal(editor.body(), '新开头\n新结尾');
});

test('Fanqie applying the same multi-paragraph body twice is idempotent', () => {
  const editor = bodyEditor(['']);
  editor.apply('开头\n中段\n结尾');
  editor.apply('开头\n中段\n结尾');
  assert.equal(editor.body(), '开头\n中段\n结尾');
});

async function sequence(initialValue, title) {
  class Input { constructor() { this.value = initialValue; } }
  const node = new Input();
  await runInNewContext(`${implementation('fillFanqieSequence')}\nfillFanqieSequence(${JSON.stringify(title)})`, {
    HTMLInputElement: Input,
    HTMLTextAreaElement: class {},
    document: { querySelector: () => node },
    fillInputExact: async (input, value) => { input.value = value; return true; },
  });
  return node.value;
}

test('Fanqie uses chapter 2 even when a drafts-only book defaults the sequence to 1', async () => {
  assert.equal(await sequence('1', '第2章'), '2');
});

test('Fanqie fills an empty sequence from the canonical chapter title', async () => {
  assert.equal(await sequence('', '第 123 章 热井'), '123');
});

test('Fanqie preserves an existing sequence when no chapter number is supplied', async () => {
  assert.equal(await sequence('4', '热井'), '4');
});
