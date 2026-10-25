from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("pin,clear_pin", [("", False), ("env-kimi", False), ("env-kimi", True)])
def test_unrelated_drawer_save_preserves_or_clears_pin(tmp_path, pin, clear_pin):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    state = Path("forwin/ui_assets/home/app_state.js").read_text()
    select_function = state[state.index("    function populateModelProfileSelect("):state.index("    function syncBookBulkActions(")]
    controls = Path("forwin/ui_assets/home/app_task_control.js").read_text()
    fixture = r'''
class Element {
  constructor(tag, text = '') {
    this.tag = tag; this.textContent = text; this.childNodes = []; this._value = '';
    this.classList = { toggle() {} };
  }
  appendChild(child) {
    this.childNodes.push(child);
    if (this.tag === 'select' && (this.childNodes.length === 1 || child.selected)) this._value = child.value;
  }
  setAttribute() {}
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
}
const document = { createElement: tag => new Element(tag), createTextNode: text => new Element('text', text) };
function createNode(tag, text = '') { return new Element(tag, text); }
function createButton(text, action) { const b = new Element('button', text); b.click = action; return b; }
function createLabeledField(label, input) { const el = createNode('label', label); el.appendChild(input); return el; }
function clearNode(el) { el.childNodes = []; el.value = ''; }
function flatten(el) { return [el, ...el.childNodes.flatMap(flatten)]; }
function normalizeReviewInterval(value) { return Number(value); }
let runtimeCatalogState = {
  default_model_profile_id: 'env-kimi',
  model_profiles: [{ id: 'env-kimi', name: 'Kimi' }, { id: 'env-deepseek', name: 'DeepSeek' }],
};
let currentDrawerTask = null;
let pending;
let saved;
function openProjectControlActionModal(config) { pending = config.onSubmit({reason: 'length change'}); }
async function requestJson(url, options) { saved = {url, ...JSON.parse(options.body)}; return {}; }
function setGlobalStatus() {}
async function loadBooks() {}
(async () => {
  const card = renderProjectControlCard({project_id: 'p'}, {
    runtime_policy_version: 4,
    runtime_policy: { model_profile_id: PIN, chapter_length: { min_chars: 2500, target_chars: 2800, max_chars: 3200 } },
  });
  const elements = flatten(card);
  const select = elements.find(el => el.id === 'runtime_policy_model_profile_id');
  const initial = select.value;
  if (CLEAR_PIN) select.value = '';
  elements.find(el => el.id === 'runtime_policy_target_chapter_chars').value = '2900';
  elements.find(el => el.textContent === '保存运行策略').click();
  await pending;
  console.log(JSON.stringify({initial, options: select.childNodes.map(el => ({value: el.value, label: el.textContent})), saved}));
})().catch(error => { console.error(error); process.exit(1); });
'''
    path = tmp_path / "policy-drawer.js"
    path.write_text(select_function + controls + f"\nconst PIN = {json.dumps(pin)}; const CLEAR_PIN = {json.dumps(clear_pin)};\n" + fixture)
    result = subprocess.run([node, str(path)], capture_output=True, text=True, check=True)
    output = json.loads(result.stdout)
    assert output["initial"] == pin
    assert output["options"][0] == {"value": "", "label": "按路由 chain（不固定）"}
    assert output["saved"]["model_profile_id"] == ("" if clear_pin else pin)
    assert output["saved"]["target_chapter_chars"] == 2900
    assert output["saved"]["expected_version"] == 4
