from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("source", ["task_row", "project_item"])
def test_world_conflict_guidance_has_no_review_actions(tmp_path, source):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    script = Path("forwin/ui_assets/home/app_task_progress.js").read_text()
    fixture = r"""
const ACTIVE_TASK_STATUSES = new Set(['queued', 'running']);
function stageLabel(stage) { return stage; }
function pickLatestPublishableChapter() { return null; }
function createNode(tag, text = '', cls = '') {
  return { textContent: text, childNodes: [], appendChild(child) { this.childNodes.push(child); } };
}
function createButton(text) { return createNode('button', text); }
function allText(node) { return [node.textContent, ...node.childNodes.map(allText)].join(' '); }
const project = { blocking_reason: { code: 'world_conflict_pending' } };
const item = {
  project_id: 'p', status: 'needs_review', paused_chapters: [3],
  completed_chapters: [2], requested_chapters: 4,
  generation_control: {
    pending_review_chapters: [3], drafted_chapters: [3],
    generated_chapters: [2, 3], can_resume: true,
  },
};
if (SOURCE === 'project_item') {
  item.status = 'created';
  item.paused_chapters = [];
  item.generation_control = {
    blocking_reason: project.blocking_reason, planned_chapters: [3, 4], can_resume: false,
  };
  delete project.blocking_reason;
}
const chapters = [{ chapter_number: 3, status: 'planned', has_review: false }];
console.log(JSON.stringify({
  guidance: generationGuidance(item, project, chapters),
  panel: allText(renderGenerationControlPanel(item, project, chapters)),
}));
"""
    path = tmp_path / "world-conflict-ui.js"
    path.write_text(script + f"\nconst SOURCE = {json.dumps(source)};\n" + fixture)
    result = subprocess.run([node, str(path)], capture_output=True, text=True, check=True)
    output = json.loads(result.stdout)
    assert output["guidance"]["eyebrow"] == "世界冲突阻塞"
    assert output["guidance"]["title"] == "世界与计划冲突需要处理"
    assert output["guidance"]["next"] == "查看世界冲突并作出决定"
    assert output["guidance"]["reviewChapters"] == []
    assert "处理第一个 Review" not in output["panel"]
    assert "待人工检查" not in output["panel"]
    if source == "task_row":
        assert "继续生成剩余章节" in output["panel"]
