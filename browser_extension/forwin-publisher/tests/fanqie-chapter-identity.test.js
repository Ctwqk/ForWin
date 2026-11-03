import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');
const match = source.match(/\n  function chapterRemoteIdentity\([^]*?\n  \}/);
if (!match) {
  throw new Error('platform-agent.js no longer defines chapterRemoteIdentity');
}

const MANAGE_URL = 'https://fanqienovel.com/main/writer/chapter-manage/7000&%E5%BD%92%E9%80%94?type=1';

// The chapter-manage row as Fanqie rendered it on 2026-11-03: the title links to the preview page
// and an unlabelled edit icon links to the editor.
function identity(anchors, { title = '空账与旧债', expected = '', href = MANAGE_URL } = {}) {
  const nodes = anchors.map(([text, link]) => ({
    innerText: text,
    href: new URL(link, href).href,
    getAttribute: () => link,
  }));
  return runInNewContext(`${match[0]}\nchapterRemoteIdentity('fanqie', ${JSON.stringify(title)}, ${JSON.stringify(expected)})`, {
    URL,
    window: { location: { href } },
    document: { querySelectorAll: () => nodes },
  });
}

test('a chapter-manage title linking to the preview page gives the book and chapter ids', () => {
  const result = identity([
    ['新建章节', '/main/writer/7000/publish/?enter_from=newchapter'],
    ['第1章 空账与旧债', '/main/writer/preview/7000&8001'],
    ['', '/main/writer/7000/publish/8001/?enter_from=modifychapter'],
  ]);
  assert.equal(result.remote_book_id, '7000');
  assert.equal(result.remote_chapter_id, '8001');
  assert.equal(result.remote_url, 'https://fanqienovel.com/main/writer/preview/7000&8001');
});

test('the preview link of another chapter is never taken', () => {
  const result = identity([['第2章 四条规矩', '/main/writer/preview/7000&8002']]);
  assert.equal(result.remote_chapter_id, undefined);
});

test('an expected chapter id matches only its own preview link', () => {
  const anchors = [
    ['第1章 空账与旧债', '/main/writer/preview/7000&8001'],
    ['第1章 空账与旧债', '/main/writer/preview/7000&8009'],
  ];
  assert.equal(identity(anchors, { expected: '8009' }).remote_chapter_id, '8009');
  assert.deepEqual({ ...identity(anchors, { expected: '8005' }) }, {});
});
