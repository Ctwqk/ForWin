import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

const source = await readFile(new URL('../platform-agent.js', import.meta.url), 'utf8');
const implementation = source.match(/  function setFanqieAudience\(value\) \{[\s\S]*?\n  \}/)?.[0];

// Model the observed controlled-radio behavior: assigning checked updates the
// value tracker, while the form only accepts the choice through its label.
function chooseAudience(value, initiallyChecked = false) {
  let accepted = '';
  class Element {}
  class Radio extends Element {
    constructor(channel) {
      super();
      this.value = channel;
      this.current = initiallyChecked;
      this.tracked = initiallyChecked;
      this.label = Object.assign(new Element(), {
        click: () => { this.current = true; accepted = this.value; },
      });
    }
    get checked() { return this.current; }
    set checked(value) { this.current = value; this.tracked = value; }
    click() {
      this.current = true;
      if (this.tracked !== this.current) accepted = this.value;
    }
    closest(selector) { return selector === 'label' ? this.label : null; }
    dispatchEvent() {}
  }
  const radios = [new Radio('1'), new Radio('0')];
  const context = {
    value,
    HTMLInputElement: Radio,
    HTMLElement: Element,
    Event: class {},
    document: {
      querySelector: (selector) => radios.find((radio) => selector.includes(`value="${radio.value}"`)),
    },
  };
  assert.ok(implementation);
  const result = runInNewContext(`${implementation}\nsetFanqieAudience(value)`, context);
  return { result, accepted };
}

test('Fanqie male audience choice reaches the controlled form', () => {
  assert.deepEqual(chooseAudience('男频'), { result: true, accepted: '1' });
});

test('Fanqie female audience choice reaches the controlled form', () => {
  assert.deepEqual(chooseAudience('female'), { result: true, accepted: '0' });
});

test('Fanqie audience selection recovers a checked radio with uncommitted form state', () => {
  assert.deepEqual(chooseAudience('male', true), { result: true, accepted: '1' });
});
