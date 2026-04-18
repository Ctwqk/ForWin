from __future__ import annotations

import json

from forwin.config import (
    DEFAULT_MINIMAX_BASE_URL,
    DEFAULT_MINIMAX_MODEL,
    DEFAULT_MOONSHOT_BASE_URL,
    DEFAULT_MOONSHOT_MODEL,
)


LLM_PROVIDER_PRESETS = [
    {
        "id": "minimax",
        "label": "MiniMax 中文站",
        "default_name": "MiniMax 主账号",
        "base_url": DEFAULT_MINIMAX_BASE_URL,
        "sites": [
            {
                "label": "MiniMax 中文站 / 开放平台",
                "base_url": DEFAULT_MINIMAX_BASE_URL,
            },
        ],
        "default_model": DEFAULT_MINIMAX_MODEL,
        "recommended_models": [
            DEFAULT_MINIMAX_MODEL,
            "MiniMax-M2.5",
            "MiniMax-M2.5-highspeed",
            "MiniMax-M2.1",
            "MiniMax-M2.1-highspeed",
            "MiniMax-M2",
            "MiniMax-M2-Her",
        ],
        "hint": "使用 MiniMax 中文站 OpenAI 兼容接口。",
    },
    {
        "id": "moonshot",
        "label": "Kimi 中文站 / Moonshot.cn",
        "default_name": "Kimi 主账号",
        "base_url": DEFAULT_MOONSHOT_BASE_URL,
        "sites": [
            {
                "label": "Kimi 中文站 / Moonshot.cn",
                "base_url": DEFAULT_MOONSHOT_BASE_URL,
            },
        ],
        "default_model": DEFAULT_MOONSHOT_MODEL,
        "recommended_models": [
            DEFAULT_MOONSHOT_MODEL,
            "kimi-k2-thinking",
            "kimi-k2-thinking-turbo",
            "kimi-k2",
            "kimi-k2-0905-preview",
            "kimi-k2-turbo-preview",
            "moonshot-v1-128k",
            "moonshot-v1-32k",
            "moonshot-v1-8k",
        ],
        "hint": "Moonshot.cn 中文站 OpenAI 兼容接口，默认推荐 kimi-k2.5。",
    },
]


PAGE_DOM_HELPERS_JS = """
    function clearNode(node) {
      if (!node) return;
      node.replaceChildren();
    }

    function createNode(tag, text = '', className = '') {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text) node.textContent = text;
      return node;
    }

    function createButton(label, onClick, className = '') {
      const button = document.createElement('button');
      if (className) button.className = className;
      button.textContent = label;
      button.addEventListener('click', onClick);
      return button;
    }

    function createLabeledField(labelText, control) {
      const wrap = document.createElement('div');
      const label = document.createElement('label');
      label.textContent = labelText;
      wrap.appendChild(label);
      wrap.appendChild(control);
      return wrap;
    }

    function isProjectBackedTaskId(taskId) {
      return typeof taskId === 'string' && taskId.startsWith('project-');
    }
"""


def _render_home_page_v2(
    *,
    has_api_key: bool,
    base_url: str,
    model: str,
    operation_mode: str,
    freeze_failed_candidates: bool,
    min_chapter_chars: int = 2500,
    review_interval_chapters: int = 0,
    default_genre: str = "玄幻",
    default_chapters: int = 3,
    extension_api_key_configured: bool = False,
    extension_install_path: str = "browser_extension/forwin-publisher",
) -> str:
    html = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ForWin 创作台</title>
  <style>
    :root {
      --paper:#efe4d4;
      --panel:#fffaf2;
      --panel-2:#f8efe2;
      --ink:#18211e;
      --muted:#6d6255;
      --accent:#b24b31;
      --accent-2:#1f5b54;
      --line:#ddcbb4;
      --line-strong:#ccb193;
      --ok:#216841;
      --warn:#9b5a1d;
      --danger:#8b2e2e;
      --shadow:0 20px 60px rgba(56,36,17,.12);
      --ui:"Avenir Next","Segoe UI","PingFang SC","Noto Sans SC",sans-serif;
      --serif:"Iowan Old Style","Palatino Linotype","Source Han Serif SC","Noto Serif SC",serif;
    }
    * { box-sizing:border-box; }
    body {
      margin:0;
      color:var(--ink);
      font-family:var(--serif);
      background:
        radial-gradient(circle at top left, rgba(255,244,225,.96), transparent 28%),
        radial-gradient(circle at 88% 4%, rgba(178,75,49,.12), transparent 18%),
        linear-gradient(135deg, #f5ece0 0%, #eaddcc 54%, #f4ecdf 100%);
      min-height:100vh;
    }
    body::before {
      content:"";
      position:fixed;
      inset:0;
      background:
        linear-gradient(rgba(255,255,255,.14), rgba(255,255,255,0)),
        repeating-linear-gradient(
          90deg,
          rgba(121,94,60,.04) 0,
          rgba(121,94,60,.04) 1px,
          transparent 1px,
          transparent 104px
        );
      pointer-events:none;
    }
    .wrap {
      position:relative;
      max-width:1380px;
      margin:0 auto;
      padding:24px 22px 72px;
    }
    .masthead {
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:18px;
      margin-bottom:18px;
    }
    .brand-mark {
      display:inline-flex;
      align-items:center;
      gap:10px;
      padding:8px 14px;
      border-radius:999px;
      border:1px solid rgba(178,75,49,.18);
      background:rgba(178,75,49,.06);
      color:var(--accent);
      font:600 12px/1 var(--ui);
      letter-spacing:.14em;
      text-transform:uppercase;
    }
    .masthead h1 {
      margin:14px 0 8px;
      font-size:52px;
      line-height:1;
      letter-spacing:-.04em;
    }
    .masthead p {
      margin:0;
      max-width:68ch;
      color:var(--muted);
      line-height:1.8;
      font-size:15px;
      font-family:var(--ui);
    }
    .head-actions {
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      justify-content:flex-end;
    }
    .button, button {
      appearance:none;
      border:1px solid transparent;
      border-radius:999px;
      padding:11px 17px;
      cursor:pointer;
      font:600 14px/1.1 var(--ui);
      transition:transform .16s ease, box-shadow .16s ease, background .16s ease, border-color .16s ease, opacity .16s ease;
      text-decoration:none;
    }
    .button:hover, button:hover { transform:translateY(-1px); }
    .button:disabled, button:disabled { cursor:not-allowed; opacity:.48; transform:none; }
    .primary {
      background:linear-gradient(135deg, var(--accent), #cb6a46);
      color:#fff;
      box-shadow:0 14px 28px rgba(178,75,49,.2);
    }
    .secondary {
      background:rgba(31,91,84,.08);
      border-color:rgba(31,91,84,.16);
      color:var(--accent-2);
    }
    .ghost {
      background:rgba(255,255,255,.65);
      border-color:rgba(109,98,85,.12);
      color:var(--ink);
    }
    .danger {
      background:rgba(139,46,46,.08);
      border-color:rgba(139,46,46,.18);
      color:var(--danger);
    }
    .shell {
      display:grid;
      gap:18px;
    }
    .surface {
      border:1px solid rgba(204,177,147,.48);
      background:linear-gradient(180deg, rgba(255,253,248,.95), rgba(247,240,229,.92));
      border-radius:28px;
      box-shadow:var(--shadow);
      overflow:hidden;
    }
    .status-bar {
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:12px;
      padding:16px 20px;
      border-bottom:1px solid rgba(109,98,85,.1);
      background:linear-gradient(180deg, rgba(255,255,255,.38), rgba(255,255,255,0));
    }
    .status-bar strong {
      font:600 14px/1.2 var(--ui);
    }
    .status-bar .meta {
      font:500 13px/1.5 var(--ui);
      color:var(--muted);
      white-space:pre-wrap;
    }
    .tabs {
      display:flex;
      gap:10px;
      padding:18px 20px 0;
    }
    .tab-chip {
      padding:11px 16px;
      border-radius:999px;
      border:1px solid rgba(109,98,85,.14);
      background:rgba(255,255,255,.72);
      color:var(--muted);
    }
    .tab-chip.active {
      background:linear-gradient(135deg, rgba(178,75,49,.12), rgba(31,91,84,.12));
      border-color:rgba(178,75,49,.24);
      color:var(--ink);
    }
    .tab-panel {
      display:none;
      padding:20px;
    }
    .tab-panel.active {
      display:block;
    }
    .section-head {
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:14px;
      margin-bottom:14px;
    }
    .section-head h2, .section-head h3 {
      margin:0 0 6px;
      font-size:26px;
      line-height:1.08;
    }
    .section-head p {
      margin:0;
      color:var(--muted);
      font:500 14px/1.7 var(--ui);
    }
    .config-grid {
      display:grid;
      grid-template-columns:minmax(0, 1.05fr) minmax(360px, .95fr);
      gap:18px;
    }
    .stack {
      display:grid;
      gap:18px;
    }
    .card {
      border:1px solid rgba(204,177,147,.42);
      border-radius:24px;
      background:linear-gradient(180deg, rgba(255,255,255,.7), rgba(250,244,235,.78));
      padding:18px;
      box-shadow:0 10px 28px rgba(56,36,17,.06);
    }
    .list {
      display:grid;
      gap:12px;
    }
    .list-item {
      border:1px solid rgba(204,177,147,.44);
      border-radius:20px;
      padding:16px;
      background:rgba(255,255,255,.74);
      display:grid;
      gap:10px;
    }
    .list-top {
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:12px;
    }
    .list-top strong {
      display:block;
      font:700 18px/1.2 var(--serif);
    }
    .meta-line, .muted {
      color:var(--muted);
      font:500 13px/1.7 var(--ui);
      white-space:pre-wrap;
    }
    .badge-row, .action-row, .inline-fields {
      display:flex;
      gap:8px;
      flex-wrap:wrap;
      align-items:center;
    }
    .badge {
      display:inline-flex;
      align-items:center;
      gap:6px;
      border-radius:999px;
      padding:6px 10px;
      font:700 12px/1 var(--ui);
      letter-spacing:.04em;
      background:rgba(31,91,84,.08);
      color:var(--accent-2);
      border:1px solid rgba(31,91,84,.14);
    }
    .badge.warn { background:rgba(155,90,29,.08); color:var(--warn); border-color:rgba(155,90,29,.18); }
    .badge.ok { background:rgba(33,104,65,.08); color:var(--ok); border-color:rgba(33,104,65,.18); }
    .badge.danger { background:rgba(139,46,46,.08); color:var(--danger); border-color:rgba(139,46,46,.18); }
    .empty {
      padding:28px 18px;
      text-align:center;
      color:var(--muted);
      border:1px dashed rgba(204,177,147,.7);
      border-radius:20px;
      background:rgba(255,255,255,.48);
      font:500 14px/1.7 var(--ui);
    }
    .task-toolbar {
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
      margin-bottom:14px;
    }
    .selection-line {
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
    }
    .select-toggle {
      display:inline-flex;
      align-items:center;
      gap:8px;
      color:var(--muted);
      font:600 12px/1.2 var(--ui);
      letter-spacing:.06em;
      text-transform:uppercase;
    }
    .select-toggle input {
      width:auto;
      margin:0;
    }
    .task-list {
      display:grid;
      gap:12px;
    }
    .task-item {
      border:1px solid rgba(204,177,147,.44);
      border-radius:22px;
      background:linear-gradient(180deg, rgba(255,255,255,.76), rgba(247,241,233,.86));
      padding:18px;
      display:grid;
      gap:12px;
    }
    .task-id {
      color:var(--muted);
      font:600 12px/1.2 var(--ui);
      letter-spacing:.08em;
      text-transform:uppercase;
    }
    .pill-switch {
      display:inline-flex;
      gap:8px;
      padding:6px;
      border:1px solid rgba(204,177,147,.58);
      border-radius:999px;
      background:rgba(255,255,255,.7);
    }
    .pill-switch button {
      background:transparent;
      color:var(--muted);
      box-shadow:none;
      border:none;
      padding:10px 14px;
    }
    .pill-switch button.active {
      background:linear-gradient(135deg, rgba(178,75,49,.12), rgba(31,91,84,.12));
      color:var(--ink);
    }
    .overlay {
      position:fixed;
      inset:0;
      background:rgba(24,24,18,.24);
      backdrop-filter:blur(4px);
      display:none;
      align-items:stretch;
      justify-content:flex-end;
      z-index:50;
    }
    .overlay.open {
      display:flex;
    }
    .drawer {
      width:min(960px, 100vw);
      background:linear-gradient(180deg, rgba(255,252,247,.98), rgba(247,240,229,.98));
      border-left:1px solid rgba(204,177,147,.58);
      box-shadow:-12px 0 40px rgba(56,36,17,.12);
      padding:22px;
      overflow:auto;
      display:grid;
      gap:18px;
    }
    .drawer-head {
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:14px;
      position:sticky;
      top:-22px;
      padding:22px 0 12px;
      background:linear-gradient(180deg, rgba(255,252,247,.98), rgba(255,252,247,.98), rgba(255,252,247,0));
    }
    .drawer-head h3 {
      margin:0 0 8px;
      font-size:28px;
      line-height:1.05;
    }
    .drawer-grid {
      display:grid;
      gap:14px;
    }
    .detail-card {
      border:1px solid rgba(204,177,147,.44);
      border-radius:22px;
      padding:16px;
      background:rgba(255,255,255,.72);
      display:grid;
      gap:10px;
    }
    .control-cockpit {
      position:relative;
      overflow:hidden;
      border:1px solid rgba(31,91,84,.18);
      border-radius:28px;
      padding:20px;
      background:
        radial-gradient(circle at 12% 0%, rgba(178,75,49,.11), transparent 34%),
        linear-gradient(135deg, rgba(255,253,248,.96), rgba(239,247,241,.9));
      box-shadow:inset 0 1px 0 rgba(255,255,255,.72);
      display:grid;
      gap:18px;
    }
    .control-cockpit.blocked {
      border-color:rgba(155,90,29,.28);
      background:
        radial-gradient(circle at 12% 0%, rgba(155,90,29,.13), transparent 36%),
        linear-gradient(135deg, rgba(255,253,248,.96), rgba(251,243,231,.92));
    }
    .control-cockpit.failed {
      border-color:rgba(139,46,46,.28);
      background:
        radial-gradient(circle at 12% 0%, rgba(139,46,46,.12), transparent 36%),
        linear-gradient(135deg, rgba(255,253,248,.96), rgba(251,235,231,.9));
    }
    .control-main {
      display:grid;
      grid-template-columns:minmax(0, 1.6fr) minmax(260px, .9fr);
      gap:18px;
      align-items:start;
    }
    .control-copy {
      display:grid;
      gap:10px;
    }
    .control-eyebrow {
      color:var(--accent-2);
      font:800 12px/1.2 var(--ui);
      letter-spacing:.12em;
      text-transform:uppercase;
    }
    .control-title {
      margin:0;
      font:800 26px/1.08 var(--display);
      letter-spacing:-.02em;
    }
    .control-description {
      color:var(--muted);
      font:500 14px/1.65 var(--ui);
      max-width:58ch;
      white-space:pre-line;
    }
    .control-actions {
      display:flex;
      flex-wrap:wrap;
      gap:10px;
      align-items:center;
    }
    .control-side {
      border-left:1px solid rgba(204,177,147,.46);
      padding-left:16px;
      display:grid;
      gap:10px;
    }
    .control-side-row {
      display:flex;
      justify-content:space-between;
      gap:12px;
      color:var(--muted);
      font:600 12px/1.45 var(--ui);
      border-bottom:1px solid rgba(204,177,147,.24);
      padding-bottom:8px;
    }
    .control-side-row strong {
      color:var(--ink);
      text-align:right;
      font:800 12px/1.45 var(--ui);
    }
    .operator-queue {
      display:grid;
      gap:10px;
    }
    .queue-item {
      display:grid;
      grid-template-columns:minmax(0, 1fr) auto;
      gap:12px;
      align-items:center;
      padding:12px 14px;
      border-radius:18px;
      border:1px solid rgba(204,177,147,.4);
      background:rgba(255,255,255,.62);
    }
    .queue-item.warn {
      border-color:rgba(155,90,29,.24);
      background:rgba(155,90,29,.07);
    }
    .queue-item.failed {
      border-color:rgba(139,46,46,.24);
      background:rgba(139,46,46,.07);
    }
    .queue-main {
      display:grid;
      gap:4px;
    }
    .queue-main strong {
      font:800 13px/1.3 var(--ui);
    }
    .queue-main span {
      color:var(--muted);
      font:500 12px/1.5 var(--ui);
    }
    .queue-actions {
      display:flex;
      flex-wrap:wrap;
      justify-content:flex-end;
      gap:8px;
    }
    .checkpoint-note {
      border-radius:18px;
      padding:12px 14px;
      color:var(--muted);
      background:rgba(31,91,84,.07);
      font:500 12px/1.6 var(--ui);
    }
    .stage-flow {
      display:grid;
      grid-template-columns:repeat(auto-fit, minmax(132px, 1fr));
      gap:10px;
    }
    .stage-node {
      position:relative;
      min-height:84px;
      padding:12px;
      border-radius:18px;
      border:1px solid rgba(204,177,147,.48);
      background:rgba(255,255,255,.72);
      display:grid;
      gap:8px;
      align-content:start;
    }
    .stage-node.completed {
      border-color:rgba(33,104,65,.28);
      background:rgba(33,104,65,.08);
    }
    .stage-node.current {
      border-color:rgba(178,75,49,.36);
      box-shadow:inset 0 0 0 1px rgba(178,75,49,.16);
      background:linear-gradient(135deg, rgba(178,75,49,.12), rgba(255,255,255,.84));
    }
    .stage-node.failed, .stage-node.paused {
      border-color:rgba(139,46,46,.26);
      background:rgba(139,46,46,.08);
    }
    .stage-node.upcoming {
      opacity:.64;
    }
    .stage-name {
      font:700 13px/1.35 var(--ui);
      letter-spacing:.02em;
    }
    .stage-note {
      color:var(--muted);
      font:500 12px/1.55 var(--ui);
    }
    .task-map {
      display:grid;
      gap:16px;
    }
    .task-map-head {
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:12px;
    }
    .task-map-title {
      display:grid;
      gap:4px;
    }
    .task-map-title strong {
      font:800 15px/1.25 var(--ui);
      letter-spacing:.02em;
    }
    .task-map-title span {
      color:var(--muted);
      font:500 12px/1.6 var(--ui);
    }
    .macro-flow {
      display:grid;
      grid-template-columns:repeat(6, minmax(0, 1fr));
      gap:8px;
    }
    .macro-node {
      min-height:72px;
      padding:12px;
      border-radius:18px;
      border:1px solid rgba(204,177,147,.42);
      background:rgba(255,255,255,.64);
      display:grid;
      gap:7px;
      align-content:start;
    }
    .macro-node.completed {
      border-color:rgba(33,104,65,.28);
      background:rgba(33,104,65,.08);
    }
    .macro-node.current {
      border-color:rgba(178,75,49,.36);
      background:linear-gradient(135deg, rgba(178,75,49,.13), rgba(255,255,255,.86));
      box-shadow:inset 0 0 0 1px rgba(178,75,49,.14);
    }
    .macro-node.failed, .macro-node.paused {
      border-color:rgba(139,46,46,.26);
      background:rgba(139,46,46,.08);
    }
    .macro-node.upcoming {
      opacity:.58;
    }
    .chapter-timeline {
      display:grid;
      gap:10px;
    }
    .chapter-line {
      display:grid;
      grid-template-columns:148px minmax(0, 1fr);
      gap:12px;
      padding:12px;
      border-radius:20px;
      border:1px solid rgba(204,177,147,.42);
      background:rgba(255,255,255,.58);
    }
    .chapter-line.current {
      border-color:rgba(178,75,49,.34);
      background:linear-gradient(135deg, rgba(178,75,49,.1), rgba(255,255,255,.7));
    }
    .chapter-line.failed {
      border-color:rgba(139,46,46,.25);
      background:rgba(139,46,46,.07);
    }
    .chapter-line.accepted {
      border-color:rgba(33,104,65,.24);
      background:rgba(33,104,65,.06);
    }
    .chapter-line-head {
      display:grid;
      gap:6px;
      align-content:start;
    }
    .chapter-line-head strong {
      font:800 14px/1.25 var(--ui);
    }
    .chapter-steps {
      display:grid;
      grid-template-columns:repeat(7, minmax(0, 1fr));
      gap:7px;
      align-items:stretch;
    }
    .chapter-step {
      min-height:58px;
      padding:9px 10px;
      border-radius:14px;
      border:1px solid rgba(204,177,147,.38);
      background:rgba(255,253,248,.7);
      display:grid;
      gap:5px;
      align-content:start;
    }
    .chapter-step.completed {
      border-color:rgba(33,104,65,.24);
      background:rgba(33,104,65,.08);
    }
    .chapter-step.current {
      border-color:rgba(178,75,49,.36);
      background:rgba(178,75,49,.1);
    }
    .chapter-step.failed {
      border-color:rgba(139,46,46,.28);
      background:rgba(139,46,46,.1);
    }
    .chapter-step.paused {
      border-color:rgba(155,90,29,.32);
      background:rgba(155,90,29,.1);
    }
    .chapter-step.upcoming {
      opacity:.42;
    }
    .chapter-step.skipped {
      opacity:.68;
      background:rgba(255,253,248,.48);
    }
    .chapter-step.inspectable {
      cursor:pointer;
      position:relative;
      box-shadow:inset 0 0 0 1px rgba(139,46,46,.1);
    }
    .chapter-step.inspectable:hover {
      transform:translateY(-1px);
      border-color:rgba(139,46,46,.48);
      background:rgba(139,46,46,.13);
    }
    .chapter-step strong {
      font:800 12px/1.25 var(--ui);
    }
    .chapter-step span {
      color:var(--muted);
      font:500 11px/1.4 var(--ui);
    }
    .progress-grid {
      display:grid;
      grid-template-columns:repeat(4, minmax(0, 1fr));
      gap:10px;
    }
    .metric {
      border-radius:18px;
      padding:14px;
      background:rgba(255,255,255,.72);
      border:1px solid rgba(204,177,147,.44);
    }
    .metric strong {
      display:block;
      font-size:26px;
      line-height:1;
      margin-bottom:6px;
    }
    .metric span {
      color:var(--muted);
      font:600 12px/1.4 var(--ui);
      letter-spacing:.06em;
      text-transform:uppercase;
    }
    .field-grid {
      display:grid;
      grid-template-columns:repeat(2, minmax(0, 1fr));
      gap:14px 16px;
    }
    .binding-grid {
      display:grid;
      grid-template-columns:repeat(2, minmax(0, 1fr));
      gap:14px;
    }
    .binding-card {
      border:1px solid rgba(204,177,147,.44);
      border-radius:22px;
      padding:16px;
      background:rgba(255,255,255,.62);
      display:grid;
      gap:12px;
      align-content:start;
    }
    .binding-card strong {
      font:700 14px/1.3 var(--ui);
      letter-spacing:.02em;
    }
    .binding-card p {
      margin:0;
      color:var(--muted);
      font:500 13px/1.6 var(--ui);
    }
    label {
      display:block;
      margin-bottom:6px;
      color:var(--muted);
      font:700 12px/1.2 var(--ui);
      letter-spacing:.08em;
      text-transform:uppercase;
    }
    input, textarea, select {
      width:100%;
      border:1px solid rgba(204,177,147,.76);
      border-radius:18px;
      padding:12px 14px;
      background:rgba(255,253,248,.98);
      color:var(--ink);
      font:500 14px/1.5 var(--ui);
      box-shadow:inset 0 1px 0 rgba(255,255,255,.7);
    }
    input:focus, textarea:focus, select:focus {
      outline:none;
      border-color:rgba(178,75,49,.42);
      box-shadow:0 0 0 4px rgba(178,75,49,.08);
    }
    textarea { min-height:118px; resize:vertical; }
    .checkbox {
      display:flex;
      gap:10px;
      align-items:flex-start;
      padding:12px 14px;
      border-radius:18px;
      border:1px solid rgba(204,177,147,.44);
      background:rgba(255,255,255,.62);
      font:500 14px/1.6 var(--ui);
      color:var(--ink);
    }
    .checkbox input {
      width:auto;
      margin-top:2px;
    }
    .modal-shell {
      position:fixed;
      inset:0;
      z-index:60;
      display:none;
      align-items:center;
      justify-content:center;
      padding:24px;
      background:rgba(21,21,18,.28);
      backdrop-filter:blur(5px);
    }
    .modal-shell.open {
      display:flex;
    }
    .modal {
      width:min(840px, 100%);
      max-height:min(92vh, 100%);
      overflow:auto;
      border-radius:28px;
      border:1px solid rgba(204,177,147,.62);
      background:linear-gradient(180deg, rgba(255,252,247,.99), rgba(247,240,229,.98));
      box-shadow:0 30px 80px rgba(34,24,14,.2);
      padding:22px;
      display:grid;
      gap:16px;
    }
    .code {
      padding:14px;
      border-radius:18px;
      background:#fffdf7;
      border:1px solid rgba(204,177,147,.44);
      font:500 13px/1.75 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
      white-space:pre-wrap;
      word-break:break-word;
    }
    .chapter-row {
      display:grid;
      gap:10px;
      padding:14px;
      border-radius:18px;
      background:rgba(255,255,255,.64);
      border:1px solid rgba(204,177,147,.42);
    }
    .chapter-row.focused {
      border-color:rgba(178,75,49,.42);
      box-shadow:inset 0 0 0 1px rgba(178,75,49,.16);
      background:linear-gradient(135deg, rgba(178,75,49,.1), rgba(255,255,255,.88));
    }
    .chapter-body {
      display:none;
      padding:12px 14px;
      border-radius:16px;
      background:#fffdf8;
      border:1px solid rgba(204,177,147,.38);
      font:500 13px/1.75 var(--ui);
      white-space:pre-wrap;
    }
    .chapter-body.open {
      display:block;
    }
    @media (max-width: 1080px) {
      .config-grid { grid-template-columns:minmax(0, 1fr); }
      .progress-grid { grid-template-columns:repeat(2, minmax(0, 1fr)); }
      .field-grid { grid-template-columns:minmax(0, 1fr); }
      .binding-grid { grid-template-columns:minmax(0, 1fr); }
      .control-main { grid-template-columns:minmax(0, 1fr); }
      .control-side { border-left:0; border-top:1px solid rgba(204,177,147,.46); padding-left:0; padding-top:14px; }
      .queue-item { grid-template-columns:minmax(0, 1fr); }
      .queue-actions { justify-content:flex-start; }
      .macro-flow { grid-template-columns:repeat(3, minmax(0, 1fr)); }
      .chapter-line { grid-template-columns:minmax(0, 1fr); }
      .chapter-steps { grid-template-columns:repeat(4, minmax(0, 1fr)); }
    }
    @media (max-width: 720px) {
      .wrap { padding:18px 14px 56px; }
      .masthead { flex-direction:column; }
      .masthead h1 { font-size:40px; }
      .tabs { padding:14px 14px 0; }
      .tab-panel { padding:14px; }
      .status-bar { padding:14px; }
      .progress-grid { grid-template-columns:minmax(0, 1fr); }
      .macro-flow, .chapter-steps { grid-template-columns:minmax(0, 1fr); }
      .drawer { padding:16px; }
      .drawer-head { top:-16px; padding:16px 0 8px; }
      .modal-shell { padding:12px; }
      .modal { padding:16px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="masthead">
      <div>
        <div class="brand-mark">ForWin Workspace</div>
        <h1>配置归配置，任务归任务。</h1>
        <p>首页现在拆成书本、任务、配置三个视角。书本负责作品管理与章节进度，任务中心继续承接生成与上传执行流，配置页只保留模型与平台登录。</p>
      </div>
      <div class="head-actions">
        <a class="button secondary" href="/publishers">高级发布页</a>
        <button class="ghost" type="button" onclick="openExtensionOptions()">扩展设置</button>
      </div>
    </section>

    <section class="surface shell">
      <div class="status-bar">
        <div>
          <strong id="global_status_title">系统状态</strong>
          <div id="global_status" class="meta">等待加载首页数据。</div>
        </div>
        <div class="badge-row">
          <span id="saved_key_badge" class="badge">API Key：@@HAS_API_KEY_TEXT@@</span>
          <span id="extension_ready_badge" class="badge @@EXTENSION_BADGE_CLASS@@">扩展回写：@@EXTENSION_READY_TEXT@@</span>
        </div>
      </div>

      <div class="tabs">
        <button id="tab_book" class="tab-chip active" type="button" onclick="switchTab('book')">书本</button>
        <button id="tab_task" class="tab-chip" type="button" onclick="switchTab('task')">任务</button>
        <button id="tab_config" class="tab-chip" type="button" onclick="switchTab('config')">配置</button>
      </div>

      <section id="panel_book" class="tab-panel active">
        <div class="task-toolbar">
          <div>
            <div class="section-head" style="margin:0;">
              <div>
                <h2>书本管理</h2>
                <p>这里看每本书的章节规划、已生成章节和已上传章节。建书和后续生成从这里发起。</p>
              </div>
            </div>
          </div>
          <div class="action-row">
            <button class="secondary" type="button" onclick="loadBooks(true)">刷新</button>
            <button id="book_select_all_btn" class="ghost" type="button" onclick="toggleSelectAllBooks()">全选</button>
            <button id="book_bulk_delete_btn" class="danger" type="button" onclick="bulkDeleteBooks()" disabled>批量删除</button>
            <button class="primary" type="button" onclick="openBookModal()">新建书本</button>
          </div>
        </div>
        <div id="book_list" class="task-list"></div>
      </section>

      <section id="panel_task" class="tab-panel">
        <div class="task-toolbar">
          <div>
            <div class="section-head" style="margin:0;">
              <div>
                <h2>统一任务中心</h2>
                <p>生成任务与上传任务按更新时间混排；右侧抽屉承接任务详情、章节查看和从章节直接派生上传任务。</p>
              </div>
            </div>
          </div>
          <div class="action-row">
            <button class="secondary" type="button" onclick="loadTaskCenter(true)">刷新</button>
            <button id="task_select_all_btn" class="ghost" type="button" onclick="toggleSelectAllTasks()">全选可删</button>
            <button id="task_bulk_delete_btn" class="danger" type="button" onclick="bulkDeleteTasks()" disabled>批量删除</button>
            <button class="primary" type="button" onclick="openTaskModal('generation')">新建任务</button>
          </div>
        </div>
        <div id="task_list" class="task-list"></div>
      </section>

      <section id="panel_config" class="tab-panel">
        <div class="config-grid">
          <div class="stack">
            <section class="card">
              <div class="section-head">
                <div>
                  <h2>模型列表</h2>
                  <p>每条模型只展示必要元信息；新增和编辑都走同一个设置弹窗。</p>
                </div>
                <div class="action-row">
                  <button class="secondary" type="button" onclick="loadSettings()">刷新</button>
                  <button class="primary" type="button" onclick="openModelModal('')">添加模型</button>
                </div>
              </div>
              <div id="profile_list" class="list"></div>
            </section>
            <section class="card">
              <div class="section-head">
                <div>
                  <h2>生成设置</h2>
                  <p>这些是新建生成任务的默认值；任务弹窗里仍可按单次任务覆盖。</p>
                </div>
                <button class="primary" type="button" onclick="saveGenerationPreferences()">保存</button>
              </div>
              <div class="field-grid">
                <div>
                  <label for="config_generation_min_chapter_chars">每章最少字数</label>
                  <input id="config_generation_min_chapter_chars" type="number" min="500" max="50000" step="100" value="@@MIN_CHAPTER_CHARS@@">
                </div>
                <div>
                  <label for="config_generation_review_interval_chapters">每 N 章人工检查</label>
                  <input id="config_generation_review_interval_chapters" type="number" min="0" max="200" step="1" value="@@REVIEW_INTERVAL_CHAPTERS@@">
                </div>
                <div>
                  <label for="config_generation_operation_mode">Operation Mode</label>
                  <select id="config_generation_operation_mode">
                    <option value="blackbox">blackbox</option>
                    <option value="copilot">copilot</option>
                    <option value="checkpoint">checkpoint</option>
                  </select>
                </div>
              </div>
              <label class="checkbox" style="margin-top:12px;">
                <input id="config_generation_freeze_failed_candidates" type="checkbox">
                <span>freeze_failed_candidates。黑箱写作出错时，保留冻结产物，方便后续人工接管。</span>
              </label>
            </section>
          </div>
          <div class="stack">
            <section class="card">
              <div class="section-head">
                <div>
                  <h2>平台列表</h2>
                  <p>这里直接看平台登录状态、扩展在线状态和当前执行端 Client ID。</p>
                </div>
                <button class="secondary" type="button" onclick="loadPlatforms()">刷新</button>
              </div>
              <div id="platform_list" class="list"></div>
            </section>
          </div>
        </div>
      </section>
    </section>
  </div>

  <div id="task_modal_shell" class="modal-shell" onclick="dismissModal(event, 'task_modal_shell')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="section-head" style="margin-bottom:0;">
        <div>
          <h3 id="task_modal_title">新建任务</h3>
          <p id="task_modal_description">统一入口。先选任务类型，再填写最少必要字段。</p>
        </div>
        <button class="ghost" type="button" onclick="closeTaskModal()">关闭</button>
      </div>
      <div class="pill-switch">
        <button id="new_task_kind_generation" type="button" class="active" onclick="setTaskModalKind('generation')">生成任务</button>
        <button id="new_task_kind_upload" type="button" onclick="setTaskModalKind('upload')">上传任务</button>
      </div>
      <section id="task_form_generation" class="drawer-grid">
        <div id="task_generation_project_hint" class="meta-line" style="display:none;"></div>
        <div class="field-grid">
          <div style="grid-column:1 / -1;">
            <label for="task_generation_premise">Premise / Prompt</label>
            <textarea id="task_generation_premise" placeholder="写清世界观、主角处境、关键冲突和你想要的开篇质感。"></textarea>
          </div>
          <div>
            <label for="task_generation_genre">Genre</label>
            <input id="task_generation_genre" value="@@DEFAULT_GENRE@@" spellcheck="false">
          </div>
          <div>
            <label for="task_generation_num_chapters">Num Chapters</label>
            <input id="task_generation_num_chapters" type="number" min="1" max="200" value="@@DEFAULT_CHAPTERS@@">
          </div>
          <div>
            <label for="task_generation_min_chapter_chars">每章最少字数</label>
            <input id="task_generation_min_chapter_chars" type="number" min="500" max="50000" step="100" value="@@MIN_CHAPTER_CHARS@@">
          </div>
          <div>
            <label for="task_generation_model_profile_id">Model Profile</label>
            <select id="task_generation_model_profile_id"></select>
          </div>
          <div>
            <label for="task_generation_operation_mode">Operation Mode</label>
            <select id="task_generation_operation_mode">
              <option value="blackbox">blackbox</option>
              <option value="copilot">copilot</option>
              <option value="checkpoint">checkpoint</option>
            </select>
          </div>
          <div>
            <label for="task_generation_progression_mode">推进策略覆盖</label>
            <select id="task_generation_progression_mode">
              <option value="">沿用项目默认</option>
              <option value="legacy_relaxed">legacy_relaxed</option>
              <option value="serial_canon">serial_canon</option>
              <option value="serial_canon_band_guard">serial_canon_band_guard</option>
            </select>
          </div>
        </div>
        <label class="checkbox">
          <input id="task_generation_freeze_failed_candidates" type="checkbox">
          <span>freeze_failed_candidates。黑箱写作出错时，保留冻结产物，方便后续人工接管。</span>
        </label>
        <div class="inline-fields">
          <label class="checkbox" style="flex:1 1 220px;">
            <input id="task_generation_auto_band_checkpoint" type="checkbox">
            <span>本次运行启用 auto band checkpoint。</span>
          </label>
          <label class="checkbox" style="flex:1 1 220px;">
            <input id="task_generation_manual_checkpoints_enabled" type="checkbox">
            <span>本次运行允许 manual checkpoint。</span>
          </label>
          <label class="checkbox" style="flex:1 1 220px;">
            <input id="task_generation_future_constraints_enabled" type="checkbox">
            <span>本次运行启用 future constraints。</span>
          </label>
        </div>
      </section>
      <section id="task_form_upload" class="drawer-grid" style="display:none;">
        <div class="field-grid">
          <div>
            <label for="task_upload_platform">Platform</label>
            <select id="task_upload_platform"></select>
          </div>
          <div>
            <label for="task_upload_book_name">Book Name</label>
            <input id="task_upload_book_name" spellcheck="false">
          </div>
          <div>
            <label for="task_upload_chapter_title">Chapter Title</label>
            <input id="task_upload_chapter_title">
          </div>
          <div>
            <label for="task_upload_upload_url">Upload URL</label>
            <input id="task_upload_upload_url" placeholder="可选，留空则走平台默认入口">
          </div>
          <div style="grid-column:1 / -1;">
            <label for="task_upload_body">Body</label>
            <textarea id="task_upload_body" placeholder="粘贴章节正文。若从生成任务章节发起，这里会自动预填。"></textarea>
          </div>
          <div>
            <label for="task_upload_audience">Audience</label>
            <select id="task_upload_audience">
              <option value="">未指定</option>
              <option value="male">male</option>
              <option value="female">female</option>
            </select>
          </div>
          <div>
            <label for="task_upload_primary_category">Primary Category</label>
            <input id="task_upload_primary_category" placeholder="例如：都市日常 / 玄幻脑洞">
          </div>
          <div>
            <label for="task_upload_protagonist_names">Protagonists</label>
            <input id="task_upload_protagonist_names" placeholder="逗号分隔">
          </div>
          <div>
            <label for="task_upload_intro">Intro</label>
            <input id="task_upload_intro" placeholder="作品简介摘要">
          </div>
        </div>
        <div class="inline-fields">
          <label class="checkbox" style="flex:1 1 220px;">
            <input id="task_upload_publish" type="checkbox" checked>
            <span>publish。勾选后直接走发布动作。</span>
          </label>
          <label class="checkbox" style="flex:1 1 220px;">
            <input id="task_upload_create_if_missing" type="checkbox">
            <span>create_if_missing。平台作品不存在时尝试先建书。</span>
          </label>
        </div>
      </section>
      <div class="action-row" style="justify-content:flex-end;">
        <button class="ghost" type="button" onclick="closeTaskModal()">取消</button>
        <button class="primary" type="button" onclick="submitTaskModal()">创建任务</button>
      </div>
    </div>
  </div>

  <div id="model_modal_shell" class="modal-shell" onclick="dismissModal(event, 'model_modal_shell')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="section-head" style="margin-bottom:0;">
        <div>
          <h3 id="model_modal_title">模型设置</h3>
          <p>新增和编辑共用这个弹窗。API Key 留空时会保留已保存值。</p>
        </div>
        <button class="ghost" type="button" onclick="closeModelModal()">关闭</button>
      </div>
      <div class="field-grid">
        <div>
          <label for="model_form_name">名称</label>
          <input id="model_form_name" placeholder="例如：MiniMax 主账号">
        </div>
        <div>
          <label for="model_form_provider_preset">供应商预设</label>
          <select id="model_form_provider_preset"></select>
        </div>
        <div>
          <label for="model_form_model">当前 Model / 自定义</label>
          <input id="model_form_model" spellcheck="false">
        </div>
        <div>
          <label for="model_form_recommended_model">模型下拉</label>
          <select id="model_form_recommended_model"></select>
        </div>
        <div style="grid-column:1 / -1;">
          <div class="action-row">
            <button class="secondary" type="button" onclick="applySelectedModelPreset()">套用供应商预设</button>
            <span id="model_form_provider_hint" class="meta-line"></span>
          </div>
        </div>
        <div style="grid-column:1 / -1;">
          <label for="model_form_base_url_select">API 站点 / Base URL 下拉</label>
          <select id="model_form_base_url_select"></select>
        </div>
        <div style="grid-column:1 / -1;">
          <label for="model_form_base_url">当前 Base URL / 自定义</label>
          <input id="model_form_base_url" spellcheck="false" placeholder="选择自定义时在这里手填">
        </div>
        <div style="grid-column:1 / -1;">
          <label for="model_form_api_key">API Key</label>
          <input id="model_form_api_key" type="password" placeholder="留空表示不修改已保存的 Key" autocomplete="off">
        </div>
      </div>
      <label class="checkbox">
        <input id="model_form_set_default" type="checkbox">
        <span>保存后同时设为默认模型。</span>
      </label>
      <div class="action-row" style="justify-content:flex-end;">
        <button class="ghost" type="button" onclick="closeModelModal()">取消</button>
        <button class="primary" type="button" onclick="saveModelProfile()">保存</button>
      </div>
    </div>
  </div>

  <div id="book_modal_shell" class="modal-shell" onclick="dismissModal(event, 'book_modal_shell')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="section-head" style="margin-bottom:0;">
        <div>
          <h3>新建书本</h3>
          <p>先建立作品，再决定何时对这本书发起生成任务。</p>
        </div>
        <button class="ghost" type="button" onclick="closeBookModal()">关闭</button>
      </div>
      <div class="field-grid">
        <div>
          <label for="book_form_title">书名</label>
          <input id="book_form_title" placeholder="例如：灰烬城的最后见习医师">
        </div>
        <div>
          <label for="book_form_genre">Genre</label>
          <input id="book_form_genre" value="@@DEFAULT_GENRE@@" spellcheck="false">
        </div>
        <div>
          <label for="book_form_target_total_chapters">总章节数</label>
          <input id="book_form_target_total_chapters" type="number" min="1" max="200" value="@@DEFAULT_CHAPTERS@@">
        </div>
        <div style="grid-column:1 / -1;">
          <label for="book_form_premise">Premise / Prompt</label>
          <textarea id="book_form_premise" placeholder="写清作品 premise、主角处境、冲突方向和你要的整体质感。"></textarea>
        </div>
        <div style="grid-column:1 / -1;">
          <label for="book_form_setting_summary">Setting Summary</label>
          <textarea id="book_form_setting_summary" placeholder="可选。先填基础世界观和规则约束。"></textarea>
        </div>
        <div style="grid-column:1 / -1;">
          <label>绑定平台</label>
          <div class="binding-grid">
            <section class="binding-card">
              <div>
                <strong>平台 1 · 默认平台</strong>
                <p>用于自动发布和章节上传的默认预填值。</p>
              </div>
              <div>
                <label for="book_form_publish_platform_1">平台</label>
                <select id="book_form_publish_platform_1"></select>
              </div>
              <div>
                <label for="book_form_publish_mode_1">平台书目状态</label>
                <select id="book_form_publish_mode_1">
                  <option value="chapter_only">平台已有书目，只创建章节</option>
                  <option value="create_book">平台还没有书目，需要先建书</option>
                </select>
              </div>
              <div>
                <label for="book_form_publish_book_name_1">平台作品名</label>
                <input id="book_form_publish_book_name_1" placeholder="默认与书名一致，可按平台作品名单独填写">
              </div>
              <div>
                <label for="book_form_publish_upload_url_1">默认上传页 URL</label>
                <input id="book_form_publish_upload_url_1" placeholder="可选。后续创建章节上传任务时会自动带上">
              </div>
            </section>
            <section class="binding-card">
              <div>
                <strong>平台 2 · 可选</strong>
                <p>可额外绑定第二个平台，后续手动上传时可直接切换使用。</p>
              </div>
              <div>
                <label for="book_form_publish_platform_2">平台</label>
                <select id="book_form_publish_platform_2"></select>
              </div>
              <div>
                <label for="book_form_publish_mode_2">平台书目状态</label>
                <select id="book_form_publish_mode_2">
                  <option value="chapter_only">平台已有书目，只创建章节</option>
                  <option value="create_book">平台还没有书目，需要先建书</option>
                </select>
              </div>
              <div>
                <label for="book_form_publish_book_name_2">平台作品名</label>
                <input id="book_form_publish_book_name_2" placeholder="默认与书名一致，可按平台作品名单独填写">
              </div>
              <div>
                <label for="book_form_publish_upload_url_2">默认上传页 URL</label>
                <input id="book_form_publish_upload_url_2" placeholder="可选。后续创建章节上传任务时会自动带上">
              </div>
            </section>
          </div>
        </div>
      </div>
      <div class="action-row" style="justify-content:flex-end;">
        <button class="ghost" type="button" onclick="closeBookModal()">取消</button>
        <button class="primary" type="button" onclick="submitBookModal()">创建书本</button>
      </div>
    </div>
  </div>

  <div id="task_drawer_overlay" class="overlay" onclick="closeTaskDrawer(event)">
    <aside class="drawer" onclick="event.stopPropagation()">
      <div class="drawer-head">
        <div>
          <div id="drawer_task_id" class="task-id"></div>
          <h3 id="drawer_title">任务详情</h3>
          <div id="drawer_meta" class="meta-line"></div>
        </div>
        <button class="ghost" type="button" onclick="closeTaskDrawer()">关闭</button>
      </div>
      <div id="drawer_body" class="drawer-grid"></div>
    </aside>
  </div>

  <div id="governance_action_modal_shell" class="modal-shell" onclick="dismissModal(event, 'governance_action_modal_shell')">
    <div class="modal" onclick="event.stopPropagation()">
      <div class="section-head" style="margin-bottom:0;">
        <div>
          <h3 id="governance_action_modal_title">治理动作</h3>
          <p id="governance_action_modal_description">所有治理动作都要求填写原因，便于进入决策链与审计时间线。</p>
        </div>
        <button class="ghost" type="button" onclick="closeGovernanceActionModal()">关闭</button>
      </div>
      <div id="governance_action_modal_fields" class="field-grid"></div>
      <div>
        <label for="governance_action_modal_reason">Reason</label>
        <textarea id="governance_action_modal_reason" placeholder="写清为什么要执行这次治理动作。"></textarea>
      </div>
      <div class="action-row" style="justify-content:flex-end;">
        <button class="ghost" type="button" onclick="closeGovernanceActionModal()">取消</button>
        <button id="governance_action_modal_submit" class="primary" type="button" onclick="submitGovernanceActionModal()">提交</button>
      </div>
    </div>
  </div>

  <script>
    const EXTENSION_BRIDGE_CHANNEL = 'forwin-publisher-extension';
    const BACKEND_EXTENSION_KEY_READY = @@EXTENSION_READY@@;
    const EXTENSION_INSTALL_PATH = @@EXTENSION_INSTALL_PATH@@;
    const MODEL_PROVIDER_PRESETS = @@MODEL_PROVIDER_PRESETS_JSON@@;
    const STAGE_ORDER = [
      'queued',
      'planning_arc',
      'creating_project',
      'resolving_arc_envelope',
      'running_provisional_preview',
      'provisional_failed',
      'assembling_context',
      'writing_chapter',
      'chapter_failed',
      'continuity_review',
      'applying_canon',
      'running_post_acceptance',
      'paused_for_review',
      'completed',
      'failed',
      'terminating',
      'cancelled',
    ];
    const TERMINAL_TASK_STATUSES = new Set(['completed', 'partial_failed', 'failed', 'needs_review', 'cancelled', 'paused', 'succeeded']);
    const ACTIVE_TASK_STATUSES = new Set(['starting', 'running', 'pending', 'terminating']);
    const pendingBridgeRequests = new Map();
    let settingsState = null;
    let platformsState = [];
    let booksState = [];
    let taskCenterState = [];
    let selectedBookIds = new Set();
    let selectedTaskKeys = new Set();
    let currentProfileId = '';
    let currentTaskModalKind = 'generation';
    let currentTaskPrefill = {};
    let currentGovernanceAction = null;
    let currentDrawerTask = null;
    let currentDrawerSignature = '';
    let drawerRequestToken = 0;
    let taskPollHasActive = false;
    let booksStateSignature = '';
    let taskCenterStateSignature = '';

@@PAGE_DOM_HELPERS_JS@@

    function setGlobalStatus(text, title = '系统状态') {
      document.getElementById('global_status_title').textContent = title;
      document.getElementById('global_status').textContent = text;
    }

    function taskSelectionKey(item) {
      return `${item.task_kind}:${item.task_id}`;
    }

    function normalizeForSignature(value) {
      if (Array.isArray(value)) return value.map((item) => normalizeForSignature(item));
      if (value && typeof value === 'object') {
        const normalized = {};
        Object.keys(value).sort().forEach((key) => {
          normalized[key] = normalizeForSignature(value[key]);
        });
        return normalized;
      }
      return value;
    }

    function dataSignature(value) {
      return JSON.stringify(normalizeForSignature(value));
    }

    function syncBookBulkActions() {
      const selectableCount = booksState.length;
      const selectedCount = selectedBookIds.size;
      const selectAllBtn = document.getElementById('book_select_all_btn');
      const bulkDeleteBtn = document.getElementById('book_bulk_delete_btn');
      if (selectAllBtn) {
        selectAllBtn.disabled = selectableCount === 0;
        selectAllBtn.textContent = selectableCount > 0 && selectedCount === selectableCount ? '取消全选' : '全选';
      }
      if (bulkDeleteBtn) {
        bulkDeleteBtn.disabled = selectedCount === 0;
        bulkDeleteBtn.textContent = selectedCount > 0 ? `批量删除（${selectedCount}）` : '批量删除';
      }
    }

    function syncTaskBulkActions() {
      const selectableCount = taskCenterState.filter((item) => item.deletable).length;
      const selectedCount = selectedTaskKeys.size;
      const selectAllBtn = document.getElementById('task_select_all_btn');
      const bulkDeleteBtn = document.getElementById('task_bulk_delete_btn');
      if (selectAllBtn) {
        selectAllBtn.disabled = selectableCount === 0;
        selectAllBtn.textContent = selectableCount > 0 && selectedCount === selectableCount ? '取消全选' : '全选可删';
      }
      if (bulkDeleteBtn) {
        bulkDeleteBtn.disabled = selectedCount === 0;
        bulkDeleteBtn.textContent = selectedCount > 0 ? `批量删除（${selectedCount}）` : '批量删除';
      }
    }

    function bridgeId() {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
      }
      return `forwin-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function bridgeRequest(action, payload = {}, timeoutMs = 1800) {
      return new Promise((resolve, reject) => {
        const correlationId = bridgeId();
        const timer = window.setTimeout(() => {
          pendingBridgeRequests.delete(correlationId);
          reject(new Error('浏览器扩展未响应。'));
        }, timeoutMs);
        pendingBridgeRequests.set(correlationId, { resolve, reject, timer });
        window.postMessage(
          {
            channel: EXTENSION_BRIDGE_CHANNEL,
            direction: 'page-to-extension',
            kind: 'request',
            correlationId,
            action,
            payload,
          },
          window.location.origin,
        );
      });
    }

    function switchTab(tab) {
      const bookActive = tab === 'book';
      const taskActive = tab === 'task';
      document.getElementById('tab_book').classList.toggle('active', bookActive);
      document.getElementById('tab_task').classList.toggle('active', taskActive);
      document.getElementById('tab_config').classList.toggle('active', tab === 'config');
      document.getElementById('panel_book').classList.toggle('active', bookActive);
      document.getElementById('panel_task').classList.toggle('active', taskActive);
      document.getElementById('panel_config').classList.toggle('active', tab === 'config');
    }

    function badgeKindByStatus(status) {
      if (['completed', 'succeeded', 'cancelled', 'accepted'].includes(status)) return 'ok';
      if (['failed', 'partial_failed'].includes(status)) return 'danger';
      if (['needs_review', 'terminating', 'pending', 'running', 'drafted', 'paused'].includes(status)) return 'warn';
      return '';
    }

    async function requestJson(url, options = {}) {
      const response = await fetch(url, options);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || payload.message || `HTTP ${response.status}`);
      }
      return payload;
    }

    function serializeTaskType(kind) {
      return kind === 'upload' ? '上传' : '生成';
    }

    function stageLabel(stage) {
      const map = {
        queued: '排队',
        planning_arc: '规划大纲',
        creating_project: '创建项目',
        resolving_arc_envelope: '解析 Arc Envelope',
        running_provisional_preview: 'Provisional 预演',
        provisional_failed: 'Provisional 失败',
        assembling_context: '组装上下文',
        writing_chapter: '写作章节',
        chapter_failed: '章节失败',
        continuity_review: '连续性审查',
        applying_canon: '写入 Canon',
        running_post_acceptance: '后置处理',
        paused_for_review: '等待人工检查',
        completed: '完成',
        failed: '失败',
        terminating: '终止中',
        cancelled: '已取消',
        paused: '已安全暂停',
      };
      return map[stage] || stage || '未知阶段';
    }

    function chapterStatusLabel(status) {
      const map = {
        planned: '待生成正文',
        running: '生成中',
        drafted: '已出正文',
        accepted: '已写入 Canon',
        needs_review: '待人工检查',
        failed: '生成失败',
        completed: '已完成',
      };
      return map[status] || status || '未知状态';
    }

    function updateTaskModalSelects() {
      const modelSelect = document.getElementById('task_generation_model_profile_id');
      const platformSelect = document.getElementById('task_upload_platform');
      clearNode(modelSelect);
      clearNode(platformSelect);
      const profiles = Array.isArray(settingsState?.profiles) ? settingsState.profiles : [];
      if (!profiles.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = '暂无模型配置';
        modelSelect.appendChild(option);
      } else {
        profiles.forEach((profile) => {
          const option = document.createElement('option');
          option.value = profile.id;
          option.textContent = `${profile.name}${profile.id === settingsState.default_profile_id ? ' · 默认' : ''}`;
          if (profile.id === settingsState.default_profile_id) option.selected = true;
          modelSelect.appendChild(option);
        });
      }
      platformsState.forEach((platform) => {
        const option = document.createElement('option');
        option.value = platform.platform_id;
        option.textContent = platform.display_name;
        platformSelect.appendChild(option);
      });
    }

    function renderProfiles() {
      const list = document.getElementById('profile_list');
      clearNode(list);
      const profiles = Array.isArray(settingsState?.profiles) ? settingsState.profiles : [];
      if (!profiles.length) {
        list.appendChild(createNode('div', '还没有模型配置。先添加一条，让生成任务只需要下拉选择。', 'empty'));
        return;
      }
      profiles.forEach((profile) => {
        const item = createNode('article', '', 'list-item');
        const top = createNode('div', '', 'list-top');
        const titleWrap = document.createElement('div');
        titleWrap.appendChild(createNode('strong', profile.name || '未命名模型'));
        titleWrap.appendChild(createNode('div', `${profile.model} | ${profile.base_url}`, 'meta-line'));
        top.appendChild(titleWrap);
        const badges = createNode('div', '', 'badge-row');
        if (profile.id === settingsState.default_profile_id) badges.appendChild(createNode('span', '默认', 'badge ok'));
        badges.appendChild(createNode('span', profile.has_api_key ? 'Key 已保存' : 'Key 未保存', `badge ${profile.has_api_key ? 'ok' : 'warn'}`));
        top.appendChild(badges);
        item.appendChild(top);
        const actions = createNode('div', '', 'action-row');
        actions.appendChild(createButton('设置', () => openModelModal(profile.id), 'secondary'));
        actions.appendChild(createButton('设为默认', () => setDefaultProfile(profile.id), 'ghost'));
        actions.appendChild(createButton('删除', () => deleteProfile(profile.id), 'danger'));
        item.appendChild(actions);
        list.appendChild(item);
      });
    }

    function modelPresetById(presetId) {
      return MODEL_PROVIDER_PRESETS.find((preset) => preset.id === presetId) || null;
    }

    function modelPresetSites(preset) {
      const sites = Array.isArray(preset?.sites) ? preset.sites : [];
      if (sites.length) return sites;
      if (preset?.base_url) {
        return [{ label: '默认站点', base_url: preset.base_url }];
      }
      return [];
    }

    function normalizedBaseUrl(value) {
      return String(value || '').trim().replace(/\\/+$/, '').toLowerCase();
    }

    function detectModelPresetId(baseUrl, model) {
      const normalizedCurrentBaseUrl = normalizedBaseUrl(baseUrl);
      const normalizedModel = (model || '').trim();
      const matched = MODEL_PROVIDER_PRESETS.find((preset) => {
        const siteMatched = modelPresetSites(preset).some((site) => (
          normalizedCurrentBaseUrl && normalizedCurrentBaseUrl === normalizedBaseUrl(site.base_url)
        ));
        const recommended = Array.isArray(preset.recommended_models) ? preset.recommended_models.map((item) => String(item || '').trim()) : [];
        return (
          siteMatched
          || (normalizedModel && recommended.includes(normalizedModel))
        );
      });
      return matched ? matched.id : '';
    }

    function syncModelPresetControls(preferredPresetId = null) {
      const providerSelect = document.getElementById('model_form_provider_preset');
      const baseUrlSelect = document.getElementById('model_form_base_url_select');
      const modelSelect = document.getElementById('model_form_recommended_model');
      const hint = document.getElementById('model_form_provider_hint');
      const currentModel = document.getElementById('model_form_model').value.trim();
      const currentBaseUrl = document.getElementById('model_form_base_url').value.trim();
      const selectedPresetId = preferredPresetId !== null
        ? preferredPresetId
        : detectModelPresetId(currentBaseUrl, currentModel);

      clearNode(providerSelect);
      const customOption = document.createElement('option');
      customOption.value = '';
      customOption.textContent = '自定义';
      providerSelect.appendChild(customOption);
      MODEL_PROVIDER_PRESETS.forEach((preset) => {
        const option = document.createElement('option');
        option.value = preset.id;
        option.textContent = `${preset.label} · ${preset.default_model}`;
        providerSelect.appendChild(option);
      });
      providerSelect.value = selectedPresetId;

      const selectedPreset = modelPresetById(providerSelect.value);
      const sites = modelPresetSites(selectedPreset);
      clearNode(baseUrlSelect);
      if (!sites.length) {
        const option = document.createElement('option');
        option.value = currentBaseUrl;
        option.textContent = currentBaseUrl || '自定义当前值';
        baseUrlSelect.appendChild(option);
      } else {
        sites.forEach((site) => {
          const option = document.createElement('option');
          option.value = site.base_url;
          option.textContent = `${site.label} · ${site.base_url}`;
          baseUrlSelect.appendChild(option);
        });
        if (currentBaseUrl && !sites.some((site) => normalizedBaseUrl(site.base_url) === normalizedBaseUrl(currentBaseUrl))) {
          const option = document.createElement('option');
          option.value = currentBaseUrl;
          option.textContent = `${currentBaseUrl} · 当前自定义`;
          baseUrlSelect.appendChild(option);
        }
      }
      baseUrlSelect.value = currentBaseUrl || (sites[0]?.base_url || '');

      clearNode(modelSelect);
      const recommendedModels = selectedPreset && Array.isArray(selectedPreset.recommended_models)
        ? selectedPreset.recommended_models.map((item) => String(item || '').trim()).filter(Boolean)
        : [];
      if (!recommendedModels.length) {
        const option = document.createElement('option');
        option.value = currentModel;
        option.textContent = currentModel || '自定义当前值';
        modelSelect.appendChild(option);
      } else {
        recommendedModels.forEach((modelName) => {
          const option = document.createElement('option');
          option.value = modelName;
          option.textContent = modelName === selectedPreset.default_model ? `${modelName} · 推荐` : modelName;
          modelSelect.appendChild(option);
        });
        if (currentModel && !recommendedModels.includes(currentModel)) {
          const option = document.createElement('option');
          option.value = currentModel;
          option.textContent = `${currentModel} · 当前值`;
          modelSelect.appendChild(option);
        }
      }
      modelSelect.value = currentModel || (selectedPreset?.default_model || '');
      hint.textContent = selectedPreset
        ? `${selectedPreset.hint} 默认站点：${sites[0]?.base_url || selectedPreset.base_url || ''}`
        : '保留手填 base URL / model，用于任意 OpenAI 兼容服务。';
    }

    function applyModelPresetById(presetId) {
      const preset = modelPresetById(presetId);
      if (!preset) {
        syncModelPresetControls();
        return;
      }
      const nameInput = document.getElementById('model_form_name');
      const sites = modelPresetSites(preset);
      document.getElementById('model_form_base_url').value = sites[0]?.base_url || preset.base_url || '';
      document.getElementById('model_form_model').value = preset.default_model || '';
      if (!nameInput.value.trim()) {
        nameInput.value = preset.default_name || preset.label || '';
      }
      syncModelPresetControls(preset.id);
    }

    function applySelectedModelPreset() {
      applyModelPresetById(document.getElementById('model_form_provider_preset').value);
    }

    function normalizeMinChapterChars(value) {
      const normalized = Number(value || @@MIN_CHAPTER_CHARS_JSON@@);
      if (!Number.isFinite(normalized)) return @@MIN_CHAPTER_CHARS_JSON@@;
      return Math.max(500, Math.min(50000, Math.round(normalized)));
    }

    function normalizeReviewInterval(value) {
      const normalized = Number(value || 0);
      if (!Number.isFinite(normalized)) return 0;
      return Math.max(0, Math.min(200, Math.round(normalized)));
    }

    function normalizeProgressionMode(value) {
      const normalized = String(value || '').trim();
      if (['legacy_relaxed', 'serial_canon', 'serial_canon_band_guard'].includes(normalized)) {
        return normalized;
      }
      return '';
    }

    function strictGovernanceDefaults() {
      return {
        progression_mode: 'serial_canon_band_guard',
        auto_band_checkpoint: true,
        band_warn_action: 'pause',
        manual_checkpoints_enabled: true,
        future_constraints_enabled: true,
      };
    }

    function applyGenerationPreferenceFields() {
      const minChars = normalizeMinChapterChars(settingsState?.min_chapter_chars || @@MIN_CHAPTER_CHARS_JSON@@);
      document.getElementById('config_generation_min_chapter_chars').value = minChars;
      document.getElementById('config_generation_review_interval_chapters').value = normalizeReviewInterval(settingsState?.review_interval_chapters ?? @@REVIEW_INTERVAL_CHAPTERS_JSON@@);
      document.getElementById('config_generation_operation_mode').value = settingsState?.operation_mode || @@OPERATION_MODE_JSON@@;
      document.getElementById('config_generation_freeze_failed_candidates').checked = settingsState?.freeze_failed_candidates ?? @@FREEZE_FAILED_JSON@@;
    }

    async function loadSettings() {
      try {
        settingsState = await requestJson('/api/settings/llm');
        document.getElementById('saved_key_badge').textContent = `API Key：${settingsState.has_api_key ? '已保存' : '未保存'}`;
        applyGenerationPreferenceFields();
        updateTaskModalSelects();
        renderProfiles();
      } catch (error) {
        setGlobalStatus(error.message || String(error), '模型配置读取失败');
      }
    }

    function openModelModal(profileId) {
      currentProfileId = profileId || '';
      const profile = (settingsState?.profiles || []).find((item) => item.id === currentProfileId);
      document.getElementById('model_modal_title').textContent = profile ? '模型设置' : '添加模型';
      document.getElementById('model_form_name').value = profile?.name || '';
      document.getElementById('model_form_model').value = profile?.model || @@MODEL_JSON@@;
      document.getElementById('model_form_base_url').value = profile?.base_url || @@BASE_URL_JSON@@;
      document.getElementById('model_form_api_key').value = '';
      document.getElementById('model_form_set_default').checked = Boolean(profile && settingsState?.default_profile_id === profile.id);
      syncModelPresetControls();
      document.getElementById('model_modal_shell').classList.add('open');
    }

    function closeModelModal() {
      document.getElementById('model_modal_shell').classList.remove('open');
    }

    async function saveModelProfile() {
      const payload = {
        profile_id: currentProfileId || null,
        name: document.getElementById('model_form_name').value.trim(),
        api_key: document.getElementById('model_form_api_key').value.trim(),
        base_url: document.getElementById('model_form_base_url').value.trim(),
        model: document.getElementById('model_form_model').value.trim(),
        set_as_default: document.getElementById('model_form_set_default').checked,
      };
      if (!payload.name) {
        setGlobalStatus('请先填写模型配置名称。', '模型配置');
        return;
      }
      try {
        settingsState = await requestJson('/api/settings/llm/profiles', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        closeModelModal();
        renderProfiles();
        updateTaskModalSelects();
        setGlobalStatus(settingsState.message || '模型配置已保存。', '模型配置');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '模型配置保存失败');
      }
    }

    async function saveGenerationPreferences() {
      const payload = {
        operation_mode: document.getElementById('config_generation_operation_mode').value,
        freeze_failed_candidates: document.getElementById('config_generation_freeze_failed_candidates').checked,
        min_chapter_chars: normalizeMinChapterChars(document.getElementById('config_generation_min_chapter_chars').value),
        review_interval_chapters: normalizeReviewInterval(document.getElementById('config_generation_review_interval_chapters').value),
      };
      try {
        settingsState = await requestJson('/api/settings/llm/preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        applyGenerationPreferenceFields();
        setGlobalStatus(settingsState.message || '生成设置已保存。', '生成设置');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '生成设置保存失败');
      }
    }

    async function setDefaultProfile(profileId) {
      try {
        settingsState = await requestJson('/api/settings/llm/default-profile', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile_id: profileId }),
        });
        renderProfiles();
        updateTaskModalSelects();
        setGlobalStatus(settingsState.message || '默认模型已切换。', '模型配置');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '默认模型切换失败');
      }
    }

    async function deleteProfile(profileId) {
      if (!window.confirm('确定删除这条模型配置吗？')) return;
      try {
        settingsState = await requestJson(`/api/settings/llm/profiles/${profileId}`, { method: 'DELETE' });
        renderProfiles();
        updateTaskModalSelects();
        setGlobalStatus(settingsState.message || '模型配置已删除。', '模型配置');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '模型配置删除失败');
      }
    }

    document.getElementById('model_form_provider_preset').addEventListener('change', () => {
      const presetId = document.getElementById('model_form_provider_preset').value;
      if (presetId) {
        applyModelPresetById(presetId);
      } else {
        syncModelPresetControls('');
      }
    });

    document.getElementById('model_form_base_url_select').addEventListener('change', (event) => {
      const value = event.target?.value || '';
      if (value) {
        document.getElementById('model_form_base_url').value = value;
      }
      syncModelPresetControls(document.getElementById('model_form_provider_preset').value);
    });

    document.getElementById('model_form_recommended_model').addEventListener('change', (event) => {
      const value = event.target?.value || '';
      if (value) {
        document.getElementById('model_form_model').value = value;
      }
    });

    function renderPlatforms() {
      const list = document.getElementById('platform_list');
      clearNode(list);
      if (!platformsState.length) {
        list.appendChild(createNode('div', '还没有收到平台状态。请先让浏览器扩展连上后端。', 'empty'));
        return;
      }
      platformsState.forEach((item) => {
        const card = createNode('article', '', 'list-item');
        const top = createNode('div', '', 'list-top');
        const titleWrap = document.createElement('div');
        titleWrap.appendChild(createNode('strong', item.display_name));
        titleWrap.appendChild(createNode('div', `登录状态：${item.connected ? '已登录' : '未登录'} | 扩展：${item.extension_online ? '在线' : '离线'}`, 'meta-line'));
        top.appendChild(titleWrap);
        const badges = createNode('div', '', 'badge-row');
        badges.appendChild(createNode('span', item.connected ? '已登录' : '待登录', `badge ${item.connected ? 'ok' : 'warn'}`));
        badges.appendChild(createNode('span', item.extension_online ? '扩展在线' : '扩展离线', `badge ${item.extension_online ? 'ok' : 'warn'}`));
        top.appendChild(badges);
        card.appendChild(top);
        card.appendChild(createNode('div', `Client ID：${item.extension_client_id || '未绑定'}`, 'meta-line'));
        card.appendChild(createNode('div', `最近心跳：${item.last_heartbeat_at || '无'}`, 'meta-line'));
        if (item.last_error) card.appendChild(createNode('div', `最近错误：${item.last_error}`, 'meta-line'));
        const actions = createNode('div', '', 'action-row');
        actions.appendChild(createButton(item.connected ? '重新登录' : '登录', () => connectPlatform(item.platform_id), 'secondary'));
        actions.appendChild(createButton('打开官网', () => window.open(item.login_url, '_blank', 'noopener,noreferrer'), 'ghost'));
        card.appendChild(actions);
        list.appendChild(card);
      });
      updateTaskModalSelects();
    }

    async function loadPlatforms() {
      try {
        platformsState = await requestJson('/api/publishers/platforms');
        renderPlatforms();
      } catch (error) {
        setGlobalStatus(error.message || String(error), '平台状态读取失败');
      }
    }

    async function connectPlatform(platform) {
      if (!BACKEND_EXTENSION_KEY_READY) {
        setGlobalStatus('后端尚未配置 FORWIN_PUBLISHER_EXTENSION_API_KEY。扩展可以打开登录页，但状态无法稳定回写。', '平台登录');
      }
      try {
        const payload = await bridgeRequest('open-login', { platform }, 2500);
        setGlobalStatus(payload.message || '登录弹窗已打开，请在浏览器扩展弹窗中完成登录。', '平台登录');
      } catch (error) {
        setGlobalStatus(`${error.message || String(error)}\n若扩展尚未加载，请用开发者模式加载：${EXTENSION_INSTALL_PATH}`, '平台登录');
      }
    }

    function fillPlatformSelect(selectId, selectedValue = '', includeEmpty = true) {
      const select = document.getElementById(selectId);
      if (!select) return;
      clearNode(select);
      if (includeEmpty) {
        const emptyOption = document.createElement('option');
        emptyOption.value = '';
        emptyOption.textContent = '暂不绑定平台';
        select.appendChild(emptyOption);
      }
      (platformsState || []).forEach((item) => {
        const option = document.createElement('option');
        option.value = item.platform_id;
        option.textContent = item.display_name || item.platform_id;
        select.appendChild(option);
      });
      select.value = selectedValue || '';
    }

    function normalizedPublishBindings(automation = {}) {
      const rawBindings = Array.isArray(automation?.publish_bindings) ? automation.publish_bindings : [];
      const candidates = [
        ...(automation?.publish?.platform ? [automation.publish] : []),
        ...rawBindings,
      ];
      const bindings = [];
      const seen = new Set();
      candidates.forEach((item) => {
        const platform = String(item?.platform || '').trim();
        if (!platform || seen.has(platform) || bindings.length >= 2) return;
        bindings.push({
          platform,
          book_name: String(item?.book_name || '').trim(),
          upload_url: String(item?.upload_url || '').trim(),
          create_if_missing: Boolean(item?.create_if_missing),
        });
        seen.add(platform);
      });
      return bindings;
    }

    function formatPublishBindingsSummary(automation = {}) {
      return normalizedPublishBindings(automation)
        .map((item, index) => `${index === 0 ? '默认 ' : ''}${item.platform}${item.create_if_missing ? '（先建书）' : '（只传章节）'}`)
        .join(' / ');
    }

    function resolveBookPrefillBindings(prefill = {}) {
      if (Array.isArray(prefill.publish_bindings) && prefill.publish_bindings.length) {
        return prefill.publish_bindings.slice(0, 2);
      }
      if (prefill.publish_platform) {
        return [
          {
            platform: prefill.publish_platform,
            book_name: prefill.publish_book_name || prefill.title || '',
            upload_url: prefill.publish_upload_url || '',
            create_if_missing: prefill.platform_has_existing_book === false,
          },
        ];
      }
      return [];
    }

    function readBookBinding(index) {
      return {
        platform: document.getElementById(`book_form_publish_platform_${index}`).value,
        book_name: document.getElementById(`book_form_publish_book_name_${index}`).value.trim(),
        upload_url: document.getElementById(`book_form_publish_upload_url_${index}`).value.trim(),
        create_if_missing: document.getElementById(`book_form_publish_mode_${index}`).value === 'create_book',
      };
    }

    function openBookModal(prefill = {}) {
      const bindings = resolveBookPrefillBindings(prefill);
      const primaryBinding = bindings[0] || {};
      const secondaryBinding = bindings[1] || {};
      fillPlatformSelect('book_form_publish_platform_1', primaryBinding.platform || '', true);
      fillPlatformSelect('book_form_publish_platform_2', secondaryBinding.platform || '', true);
      document.getElementById('book_form_title').value = prefill.title || '';
      document.getElementById('book_form_genre').value = prefill.genre || @@DEFAULT_GENRE_JSON@@;
      document.getElementById('book_form_target_total_chapters').value = prefill.target_total_chapters || @@DEFAULT_CHAPTERS_JSON@@;
      document.getElementById('book_form_premise').value = prefill.premise || '';
      document.getElementById('book_form_setting_summary').value = prefill.setting_summary || '';
      document.getElementById('book_form_publish_mode_1').value = primaryBinding.create_if_missing ? 'create_book' : 'chapter_only';
      document.getElementById('book_form_publish_mode_2').value = secondaryBinding.create_if_missing ? 'create_book' : 'chapter_only';
      document.getElementById('book_form_publish_book_name_1').value = primaryBinding.book_name || prefill.title || '';
      document.getElementById('book_form_publish_book_name_2').value = secondaryBinding.book_name || '';
      document.getElementById('book_form_publish_upload_url_1').value = primaryBinding.upload_url || '';
      document.getElementById('book_form_publish_upload_url_2').value = secondaryBinding.upload_url || '';
      document.getElementById('book_modal_shell').classList.add('open');
    }

    function closeBookModal() {
      document.getElementById('book_modal_shell').classList.remove('open');
    }

    async function submitBookModal() {
      try {
        const rawBindings = [readBookBinding(1), readBookBinding(2)];
        const publishBindings = [];
        const seenPlatforms = new Set();
        for (const item of rawBindings) {
          if (!item.platform) continue;
          if (seenPlatforms.has(item.platform)) {
            setGlobalStatus('两个绑定平台不能重复，请调整后再创建。', '书本管理');
            return;
          }
          publishBindings.push(item);
          seenPlatforms.add(item.platform);
        }
        const primaryBinding = publishBindings[0] || null;
        const payload = {
          title: document.getElementById('book_form_title').value.trim(),
          premise: document.getElementById('book_form_premise').value.trim(),
          genre: document.getElementById('book_form_genre').value.trim() || @@DEFAULT_GENRE_JSON@@,
          target_total_chapters: Number(document.getElementById('book_form_target_total_chapters').value || @@DEFAULT_CHAPTERS_JSON@@),
          setting_summary: document.getElementById('book_form_setting_summary').value.trim(),
          publish_bindings: publishBindings,
          publish_platform: primaryBinding?.platform || '',
          publish_book_name: primaryBinding?.book_name || '',
          publish_upload_url: primaryBinding?.upload_url || '',
          platform_has_existing_book: primaryBinding ? !primaryBinding.create_if_missing : true,
        };
        if (!payload.title) {
          setGlobalStatus('新建书本必须填写书名。', '书本管理');
          return;
        }
        if (!payload.premise) {
          setGlobalStatus('新建书本必须填写 premise / prompt。', '书本管理');
          return;
        }
        if (!Number.isFinite(payload.target_total_chapters) || payload.target_total_chapters < 1 || payload.target_total_chapters > 200) {
          setGlobalStatus('总章节数必须是 1 到 200 之间的整数。', '书本管理');
          return;
        }
        const created = await requestJson('/api/projects', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        closeBookModal();
        switchTab('book');
        await loadBooks();
        setGlobalStatus(created.message || `书本《${payload.title}》已创建。`, '书本管理');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '新建书本失败');
      }
    }

    function toggleSelectAllBooks() {
      if (selectedBookIds.size === booksState.length) {
        selectedBookIds = new Set();
      } else {
        selectedBookIds = new Set(booksState.map((book) => book.id).filter(Boolean));
      }
      renderBooks();
    }

    async function bulkDeleteBooks() {
      const projectIds = Array.from(selectedBookIds);
      if (!projectIds.length) return;
      if (!window.confirm(`确定批量删除这 ${projectIds.length} 本书吗？相关章节、review 和关联数据都会一起删除。`)) return;
      try {
        const result = await requestJson('/api/projects/bulk-delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_ids: projectIds }),
        });
        const deletedProjectIds = new Set(Array.isArray(result.deleted_ids) ? result.deleted_ids : []);
        selectedBookIds = new Set(
          Array.from(selectedBookIds).filter((projectId) => !deletedProjectIds.has(projectId))
        );
        if (currentDrawerTask?.project_id && deletedProjectIds.has(currentDrawerTask.project_id)) {
          closeTaskDrawer();
        }
        setGlobalStatus(result.message || '批量删除完成。', '书本管理');
        await loadBooks();
        await loadTaskCenter();
      } catch (error) {
        setGlobalStatus(error.message || String(error), '批量删除书本失败');
      }
    }

    async function deleteBook(book) {
      if (!window.confirm(`删除书本《${book.title || '未命名'}》后，章节、review 和关联数据都会删除，确定继续吗？`)) return;
      try {
        const result = await requestJson(`/api/projects/${book.id}`, { method: 'DELETE' });
        selectedBookIds.delete(book.id);
        setGlobalStatus(result.message || '书本已删除。', '书本管理');
        await loadBooks();
        await loadTaskCenter();
        if (currentDrawerTask?.project_id === book.id) {
          closeTaskDrawer();
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '删除书本失败');
      }
    }

    function renderBooks() {
      const list = document.getElementById('book_list');
      clearNode(list);
      if (!booksState.length) {
        list.appendChild(createNode('div', '还没有书本。先新建一本，再决定什么时候开始生成。', 'empty'));
        syncBookBulkActions();
        return;
      }
      booksState.forEach((book) => {
        const node = createNode('article', '', 'task-item');
        const selectionLine = createNode('div', '', 'selection-line');
        selectionLine.appendChild(createNode('div', `书本 · ${book.id}`, 'task-id'));
        const selectWrap = document.createElement('label');
        selectWrap.className = 'select-toggle';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = selectedBookIds.has(book.id);
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) selectedBookIds.add(book.id);
          else selectedBookIds.delete(book.id);
          syncBookBulkActions();
        });
        selectWrap.appendChild(checkbox);
        selectWrap.appendChild(document.createTextNode('选择'));
        selectionLine.appendChild(selectWrap);
        node.appendChild(selectionLine);
        const top = createNode('div', '', 'list-top');
        const titleWrap = document.createElement('div');
        titleWrap.appendChild(createNode('strong', book.title || '未命名书本'));
        titleWrap.appendChild(createNode('div', `${book.genre || ''}${book.created_at ? ` | 创建于 ${book.created_at}` : ''}`, 'meta-line'));
        top.appendChild(titleWrap);
        const badges = createNode('div', '', 'badge-row');
        badges.appendChild(createNode('span', `目标 ${book.target_total_chapters || @@DEFAULT_CHAPTERS_JSON@@} 章`, 'badge'));
        badges.appendChild(createNode('span', `已规划 ${book.chapter_count || 0}`, 'badge'));
        badges.appendChild(createNode('span', `已生成 ${book.generated_chapter_count || 0}`, 'badge ok'));
        badges.appendChild(createNode('span', `已上传 ${book.uploaded_chapter_count || 0}`, 'badge'));
        if (book.needs_review_chapter_count) {
          badges.appendChild(createNode('span', `待处理 ${book.needs_review_chapter_count}`, 'badge warn'));
        }
        top.appendChild(badges);
        node.appendChild(top);
        const publishBindingsSummary = formatPublishBindingsSummary(book.automation || {});
        const meta = [
          book.premise ? `Premise：${book.premise}` : '',
          book.target_total_chapters ? `计划总章数：${book.target_total_chapters}` : '',
          book.latest_stage ? `最近阶段：${book.latest_stage}` : '',
          book.pacing_summary ? `节奏：${book.pacing_summary}` : '',
          book.upload_task_count ? `上传任务：${book.upload_task_count}` : '',
          book.automation?.publish?.platform
            ? `发布默认：${book.automation.publish.platform} · ${book.automation.publish.create_if_missing ? '平台未建书，首次上传先建书' : '平台已有书目，只传章节'}`
            : '',
          publishBindingsSummary
            ? `已绑定平台：${publishBindingsSummary}`
            : '',
          book.automation?.enabled
            ? `自动化：${book.automation.daily_start_time || '09:00'} 开始，每日 ${book.automation.daily_chapter_quota || 1} 章${book.automation.auto_publish ? '，完成后自动发布' : ''}`
            : '自动化：关闭',
        ].filter(Boolean).join('\\n');
        if (meta) {
          node.appendChild(createNode('div', meta, 'meta-line'));
        }
        if (Array.isArray(book.chapters) && book.chapters.length) {
          const preview = book.chapters
            .slice(-3)
            .map((chapter) => `第${chapter.chapter_number}章 ${chapterStatusLabel(chapter.status)} · ${chapter.title}`)
            .join('\\n');
          node.appendChild(createNode('div', preview, 'meta-line'));
        }
        const actions = createNode('div', '', 'action-row');
        actions.appendChild(createButton('查看书本', () => openTaskDrawer('generation', `project-${book.id}`), 'secondary'));
        const publishButton = createButton('发布到平台', () => openBookPublishModal(book), 'secondary');
        if (!pickLatestPublishableChapter(book)) {
          publishButton.disabled = true;
        }
        actions.appendChild(publishButton);
        const control = book.generation_control || {};
        const hasReviewBlocker = Boolean(book.needs_review_chapter_count || (Array.isArray(control.pending_review_chapters) && control.pending_review_chapters.length));
        let generateLabel = '生成首批章节';
        let generateClass = 'primary';
        let generateAction = () => openTaskModal('generation', {
          project_id: book.id,
          book_title: book.title,
          premise: book.premise || '',
          genre: book.genre || @@DEFAULT_GENRE_JSON@@,
          num_chapters: book.target_total_chapters || @@DEFAULT_CHAPTERS_JSON@@,
          operation_mode: book.governance?.default_operation_mode || '',
          progression_mode: book.governance?.progression_mode || '',
          auto_band_checkpoint: Boolean(book.governance?.auto_band_checkpoint),
          manual_checkpoints_enabled: Boolean(book.governance?.manual_checkpoints_enabled),
          future_constraints_enabled: Boolean(book.governance?.future_constraints_enabled),
        });
        if (hasReviewBlocker) {
          generateLabel = '处理 Review';
          generateClass = 'primary';
          generateAction = () => openTaskDrawer('generation', `project-${book.id}`);
        } else if (control.can_resume) {
          generateLabel = '继续生成剩余章节';
          generateClass = 'primary';
          generateAction = () => continueProjectGeneration(book.id);
        } else if (book.chapter_count) {
          generateLabel = control.plan_state === 'completed' ? '写作完成' : '查看进度';
          generateClass = 'ghost';
          generateAction = () => openTaskDrawer('generation', `project-${book.id}`);
        }
        const generateButton = createButton(
          generateLabel,
          generateAction,
          generateClass,
        );
        actions.appendChild(generateButton);
        actions.appendChild(createButton('删除书本', () => deleteBook(book), 'danger'));
        node.appendChild(actions);
        list.appendChild(node);
      });
      syncBookBulkActions();
    }

    async function loadBooks(showStatus = false) {
      try {
        const nextBooksState = await requestJson('/api/projects');
        const nextSignature = dataSignature(nextBooksState);
        booksState = nextBooksState;
        selectedBookIds = new Set(
          Array.from(selectedBookIds).filter((projectId) => booksState.some((book) => book.id === projectId))
        );
        if (nextSignature !== booksStateSignature) {
          booksStateSignature = nextSignature;
          renderBooks();
        } else if (!document.getElementById('book_list')?.childNodes.length) {
          booksStateSignature = nextSignature;
          renderBooks();
        } else {
          syncBookBulkActions();
        }
        if (showStatus) setGlobalStatus(`已刷新 ${booksState.length} 本书。`, '书本管理');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '书本列表读取失败');
      }
    }

    function toggleSelectAllTasks() {
      const deletableItems = taskCenterState.filter((item) => item.deletable);
      if (selectedTaskKeys.size === deletableItems.length) {
        selectedTaskKeys = new Set();
      } else {
        selectedTaskKeys = new Set(deletableItems.map((item) => taskSelectionKey(item)));
      }
      renderTaskList();
    }

    async function bulkDeleteTasks() {
      const items = taskCenterState
        .filter((item) => selectedTaskKeys.has(taskSelectionKey(item)) && item.deletable)
        .map((item) => ({ task_kind: item.task_kind, task_id: item.task_id }));
      if (!items.length) return;
      if (!window.confirm(`确定批量删除这 ${items.length} 条任务吗？`)) return;
      try {
        const result = await requestJson('/api/tasks/bulk-delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items }),
        });
        const deletedIds = new Set(Array.isArray(result.deleted_ids) ? result.deleted_ids : []);
        selectedTaskKeys = new Set(
          Array.from(selectedTaskKeys).filter((key) => !deletedIds.has(key))
        );
        if (currentDrawerTask && deletedIds.has(`${currentDrawerTask.task_kind}:${currentDrawerTask.task_id}`)) {
          closeTaskDrawer();
        }
        setGlobalStatus(result.message || '批量删除完成。', '任务操作');
        await loadTaskCenter();
        await loadBooks();
      } catch (error) {
        setGlobalStatus(error.message || String(error), '批量删除任务失败');
      }
    }

    function renderTaskList() {
      const list = document.getElementById('task_list');
      clearNode(list);
      if (!taskCenterState.length) {
        list.appendChild(createNode('div', '还没有任务。点击右上角“新建任务”开始。', 'empty'));
        syncTaskBulkActions();
        return;
      }
      taskCenterState.forEach((item) => {
        const node = createNode('article', '', 'task-item');
        const selectionLine = createNode('div', '', 'selection-line');
        selectionLine.appendChild(createNode('div', `${serializeTaskType(item.task_kind)} · ${item.task_id}`, 'task-id'));
        const selectWrap = document.createElement('label');
        selectWrap.className = 'select-toggle';
        const checkbox = document.createElement('input');
        const selectionKey = taskSelectionKey(item);
        checkbox.type = 'checkbox';
        checkbox.checked = selectedTaskKeys.has(selectionKey);
        checkbox.disabled = !item.deletable;
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) selectedTaskKeys.add(selectionKey);
          else selectedTaskKeys.delete(selectionKey);
          syncTaskBulkActions();
        });
        selectWrap.appendChild(checkbox);
        selectWrap.appendChild(document.createTextNode(item.deletable ? '选择' : '不可删'));
        selectionLine.appendChild(selectWrap);
        node.appendChild(selectionLine);
        const top = createNode('div', '', 'list-top');
        const titleWrap = document.createElement('div');
        titleWrap.appendChild(createNode('strong', item.title || '未命名任务'));
        titleWrap.appendChild(createNode('div', `${item.status}${item.subtitle ? ` | ${item.subtitle}` : ''}`, 'meta-line'));
        top.appendChild(titleWrap);
        const badges = createNode('div', '', 'badge-row');
        badges.appendChild(createNode('span', serializeTaskType(item.task_kind), 'badge'));
        badges.appendChild(createNode('span', item.status, `badge ${badgeKindByStatus(item.status)}`));
        top.appendChild(badges);
        node.appendChild(top);
        const meta = [
          item.updated_at ? `更新时间：${item.updated_at}` : '',
          item.project_id ? `书本：${item.project_id}` : '',
          item.extension_client_id ? `执行端：${item.extension_client_id}` : '',
          item.current_stage ? `阶段：${stageLabel(item.current_stage)}` : '',
          item.message ? `消息：${item.message}` : '',
          item.error ? `错误：${item.error}` : '',
        ].filter(Boolean).join('\\n');
        node.appendChild(createNode('div', meta, 'meta-line'));
        const actions = createNode('div', '', 'action-row');
        actions.appendChild(createButton('查看详情', () => openTaskDrawer(item.task_kind, item.task_id), 'secondary'));
        actions.appendChild(createButton('暂停', () => pauseTask(item), 'ghost'));
        actions.appendChild(createButton('强制终止', () => terminateTask(item), 'ghost'));
        if (item.task_kind === 'generation' && item.generation_control?.can_resume && item.project_id) {
          actions.appendChild(createButton('继续', () => continueProjectGeneration(item.project_id), 'primary'));
        }
        actions.appendChild(createButton('删除', () => deleteTask(item), 'danger'));
        actions.querySelectorAll('button')[1].disabled = !item.pausable;
        actions.querySelectorAll('button')[2].disabled = !item.terminable;
        actions.querySelectorAll('button')[actions.querySelectorAll('button').length - 1].disabled = !item.deletable;
        node.appendChild(actions);
        list.appendChild(node);
      });
      syncTaskBulkActions();
    }

    async function loadTaskCenter(showStatus = false) {
      try {
        const nextTaskCenterState = await requestJson('/api/task-center/items?limit=80');
        const nextSignature = dataSignature(nextTaskCenterState);
        taskCenterState = nextTaskCenterState;
        const validSelectionKeys = new Set(
          taskCenterState
            .filter((item) => item.deletable)
            .map((item) => taskSelectionKey(item))
        );
        selectedTaskKeys = new Set(
          Array.from(selectedTaskKeys).filter((key) => validSelectionKeys.has(key))
        );
        taskPollHasActive = taskCenterState.some((item) => ACTIVE_TASK_STATUSES.has(item.status));
        if (nextSignature !== taskCenterStateSignature) {
          taskCenterStateSignature = nextSignature;
          renderTaskList();
        } else if (!document.getElementById('task_list')?.childNodes.length) {
          taskCenterStateSignature = nextSignature;
          renderTaskList();
        } else {
          syncTaskBulkActions();
        }
        if (showStatus) setGlobalStatus(`已刷新 ${taskCenterState.length} 条任务。`, '任务中心');
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务中心读取失败');
      }
    }

    function dismissModal(event, id) {
      if (event.target === event.currentTarget) {
        document.getElementById(id).classList.remove('open');
      }
    }

    function closeGovernanceActionModal() {
      document.getElementById('governance_action_modal_shell').classList.remove('open');
      currentGovernanceAction = null;
      clearNode(document.getElementById('governance_action_modal_fields'));
      document.getElementById('governance_action_modal_reason').value = '';
    }

    function openGovernanceActionModal(config = {}) {
      currentGovernanceAction = config || {};
      document.getElementById('governance_action_modal_title').textContent = config.title || '治理动作';
      document.getElementById('governance_action_modal_description').textContent = config.description || '所有治理动作都要求填写原因，便于进入决策链与审计时间线。';
      document.getElementById('governance_action_modal_submit').textContent = config.confirmLabel || '提交';
      document.getElementById('governance_action_modal_reason').value = config.reason || '';
      const fields = document.getElementById('governance_action_modal_fields');
      clearNode(fields);
      (Array.isArray(config.fields) ? config.fields : []).forEach((field) => {
        let control = null;
        if (field.type === 'select') {
          control = document.createElement('select');
          (Array.isArray(field.options) ? field.options : []).forEach((optionValue) => {
            const option = document.createElement('option');
            option.value = String(optionValue || '');
            option.textContent = String(optionValue || '');
            control.appendChild(option);
          });
          control.value = String(field.value ?? '');
        } else {
          control = field.type === 'textarea' ? document.createElement('textarea') : document.createElement('input');
          if (field.type !== 'textarea') {
            control.type = field.type === 'number' ? 'number' : 'text';
          } else {
            control.rows = Number(field.rows || 8);
            control.spellcheck = false;
          }
          if (field.type === 'number') {
            control.min = String(field.min ?? 0);
            control.step = String(field.step ?? 1);
          }
          control.value = String(field.value ?? '');
        }
        control.id = `governance_action_field_${field.name}`;
        fields.appendChild(createLabeledField(field.label || field.name, control));
      });
      document.getElementById('governance_action_modal_shell').classList.add('open');
    }

    async function submitGovernanceActionModal() {
      if (!currentGovernanceAction?.onSubmit) return;
      const reason = String(document.getElementById('governance_action_modal_reason').value || '').trim();
      if (!reason) {
        setGlobalStatus('治理动作必须填写 reason。', '治理动作');
        return;
      }
      const values = {};
      (Array.isArray(currentGovernanceAction.fields) ? currentGovernanceAction.fields : []).forEach((field) => {
        const control = document.getElementById(`governance_action_field_${field.name}`);
        if (!control) return;
        if (field.type === 'number') {
          values[field.name] = Number(control.value || 0);
        } else {
          values[field.name] = String(control.value || '').trim();
        }
      });
      try {
        await currentGovernanceAction.onSubmit({ reason, ...values });
        closeGovernanceActionModal();
      } catch (error) {
        setGlobalStatus(error.message || String(error), currentGovernanceAction.errorTitle || '治理动作失败');
      }
    }

    function setTaskModalKind(kind) {
      currentTaskModalKind = kind;
      document.getElementById('new_task_kind_generation').classList.toggle('active', kind === 'generation');
      document.getElementById('new_task_kind_upload').classList.toggle('active', kind === 'upload');
      document.getElementById('task_form_generation').style.display = kind === 'generation' ? 'grid' : 'none';
      document.getElementById('task_form_upload').style.display = kind === 'upload' ? 'grid' : 'none';
    }

    function updateTaskModalHeader() {
      const title = document.getElementById('task_modal_title');
      const description = document.getElementById('task_modal_description');
      if (!title || !description) return;
      if (currentTaskModalKind === 'upload') {
        title.textContent = '新建上传任务';
        description.textContent = '统一入口。先选任务类型，再填写最少必要字段。';
        return;
      }
      if (currentTaskPrefill?.continue_generation) {
        title.textContent = '继续生成';
        description.textContent = '沿用现有生成 modal，但本次提交会走 continue-generation，并允许覆盖本次治理策略。';
        return;
      }
      title.textContent = '新建任务';
      description.textContent = '统一入口。先选任务类型，再填写最少必要字段。';
    }

    function applyTaskPrefill() {
      const projectHint = document.getElementById('task_generation_project_hint');
      if (currentTaskPrefill.project_id) {
        projectHint.style.display = 'block';
        projectHint.textContent = currentTaskPrefill.continue_generation
          ? `继续生成目标：${currentTaskPrefill.book_title || '未命名书本'} · ${currentTaskPrefill.project_id}`
          : `当前生成目标：${currentTaskPrefill.book_title || '未命名书本'} · ${currentTaskPrefill.project_id}`;
      } else {
        projectHint.style.display = 'none';
        projectHint.textContent = '';
      }
      document.getElementById('task_generation_genre').value = currentTaskPrefill.genre || @@DEFAULT_GENRE_JSON@@;
      document.getElementById('task_generation_num_chapters').value = currentTaskPrefill.num_chapters || @@DEFAULT_CHAPTERS_JSON@@;
      document.getElementById('task_generation_min_chapter_chars').value = normalizeMinChapterChars(
        currentTaskPrefill.min_chapter_chars || settingsState?.min_chapter_chars || @@MIN_CHAPTER_CHARS_JSON@@
      );
      document.getElementById('task_generation_premise').value = currentTaskPrefill.premise || '';
      document.getElementById('task_generation_operation_mode').value = currentTaskPrefill.operation_mode || settingsState?.operation_mode || @@OPERATION_MODE_JSON@@;
      document.getElementById('task_generation_freeze_failed_candidates').checked = currentTaskPrefill.freeze_failed_candidates ?? settingsState?.freeze_failed_candidates ?? @@FREEZE_FAILED_JSON@@;
      const strictDefaults = strictGovernanceDefaults();
      const defaultProgressionMode = currentTaskPrefill.progression_mode ?? settingsState?.progression_mode ?? strictDefaults.progression_mode;
      document.getElementById('task_generation_progression_mode').value = normalizeProgressionMode(defaultProgressionMode) || strictDefaults.progression_mode;
      document.getElementById('task_generation_auto_band_checkpoint').checked = currentTaskPrefill.auto_band_checkpoint ?? settingsState?.auto_band_checkpoint ?? strictDefaults.auto_band_checkpoint;
      document.getElementById('task_generation_manual_checkpoints_enabled').checked = currentTaskPrefill.manual_checkpoints_enabled ?? settingsState?.manual_checkpoints_enabled ?? strictDefaults.manual_checkpoints_enabled;
      document.getElementById('task_generation_future_constraints_enabled').checked = currentTaskPrefill.future_constraints_enabled ?? settingsState?.future_constraints_enabled ?? strictDefaults.future_constraints_enabled;
      if (currentTaskPrefill.model_profile_id) {
        document.getElementById('task_generation_model_profile_id').value = currentTaskPrefill.model_profile_id;
      }

      document.getElementById('task_upload_platform').value = currentTaskPrefill.platform || (platformsState[0]?.platform_id || '');
      document.getElementById('task_upload_book_name').value = currentTaskPrefill.book_name || '';
      document.getElementById('task_upload_chapter_title').value = currentTaskPrefill.chapter_title || '';
      document.getElementById('task_upload_upload_url').value = currentTaskPrefill.upload_url || '';
      document.getElementById('task_upload_body').value = currentTaskPrefill.body || '';
      document.getElementById('task_upload_publish').checked = currentTaskPrefill.publish ?? true;
      document.getElementById('task_upload_create_if_missing').checked = currentTaskPrefill.create_if_missing ?? false;
      document.getElementById('task_upload_audience').value = currentTaskPrefill.audience || '';
      document.getElementById('task_upload_primary_category').value = currentTaskPrefill.primary_category || '';
      document.getElementById('task_upload_protagonist_names').value = currentTaskPrefill.protagonist_names || '';
      document.getElementById('task_upload_intro').value = currentTaskPrefill.intro || '';
    }

    function openTaskModal(kind = 'generation', prefill = {}) {
      currentTaskPrefill = prefill || {};
      setTaskModalKind(kind);
      updateTaskModalHeader();
      applyTaskPrefill();
      document.getElementById('task_modal_shell').classList.add('open');
    }

    function closeTaskModal() {
      document.getElementById('task_modal_shell').classList.remove('open');
      currentTaskPrefill = {};
    }

    function preferredPublishBinding(book) {
      const automation = book?.automation || {};
      const bindings = Array.isArray(automation.publish_bindings)
        ? automation.publish_bindings.filter((item) => item?.platform)
        : [];
      if (bindings.length) return bindings[0];
      if (automation.publish?.platform) return automation.publish;
      return {};
    }

    function pickLatestPublishableChapter(book) {
      const chapters = Array.isArray(book?.chapters) ? book.chapters : [];
      return chapters
        .filter((chapter) => Number(chapter?.char_count || 0) > 0 || ['drafted', 'accepted'].includes(String(chapter?.status || '')))
        .sort((left, right) => Number(right?.chapter_number || 0) - Number(left?.chapter_number || 0))[0] || null;
    }

    async function openBookPublishModal(book) {
      const chapter = pickLatestPublishableChapter(book);
      if (!chapter) {
        setGlobalStatus('当前书本还没有可发布的已生成章节。', '书本发布');
        return;
      }
      try {
        const chapterDetail = await requestJson(`/api/projects/${book.id}/chapters/${chapter.chapter_number}`);
        const binding = preferredPublishBinding(book);
        const bookMeta = binding?.book_meta || {};
        openTaskModal('upload', {
          project_id: book.id,
          platform: binding?.platform || '',
          book_name: binding?.book_name || book.title || '',
          chapter_title: chapterDetail.title || chapter.title || `第${chapter.chapter_number}章`,
          body: chapterDetail.body || '',
          upload_url: binding?.upload_url || '',
          create_if_missing: Boolean(binding?.create_if_missing),
          audience: bookMeta?.audience || '',
          primary_category: bookMeta?.primary_category || '',
          protagonist_names: Array.isArray(bookMeta?.protagonist_names) ? bookMeta.protagonist_names.join(', ') : '',
          intro: bookMeta?.intro || '',
          publish: true,
        });
      } catch (error) {
        setGlobalStatus(error.message || String(error), '章节读取失败');
      }
    }

    async function submitTaskModal() {
      try {
        if (currentTaskModalKind === 'generation') {
          const payload = {
            project_id: currentTaskPrefill.project_id || null,
            premise: document.getElementById('task_generation_premise').value.trim(),
            genre: document.getElementById('task_generation_genre').value.trim() || @@DEFAULT_GENRE_JSON@@,
            num_chapters: Number(document.getElementById('task_generation_num_chapters').value || @@DEFAULT_CHAPTERS_JSON@@),
            min_chapter_chars: normalizeMinChapterChars(document.getElementById('task_generation_min_chapter_chars').value),
            review_interval_chapters: normalizeReviewInterval(settingsState?.review_interval_chapters ?? @@REVIEW_INTERVAL_CHAPTERS_JSON@@),
            model_profile_id: document.getElementById('task_generation_model_profile_id').value || null,
            operation_mode: document.getElementById('task_generation_operation_mode').value,
            freeze_failed_candidates: document.getElementById('task_generation_freeze_failed_candidates').checked,
            progression_mode: normalizeProgressionMode(document.getElementById('task_generation_progression_mode').value),
            auto_band_checkpoint: document.getElementById('task_generation_auto_band_checkpoint').checked,
            manual_checkpoints_enabled: document.getElementById('task_generation_manual_checkpoints_enabled').checked,
            future_constraints_enabled: document.getElementById('task_generation_future_constraints_enabled').checked,
          };
          if (!currentTaskPrefill.continue_generation && !payload.premise) {
            setGlobalStatus('生成任务必须填写 premise / prompt。', '新建任务');
            return;
          }
          const requestUrl = currentTaskPrefill.continue_generation && currentTaskPrefill.project_id
            ? `/api/projects/${currentTaskPrefill.project_id}/continue-generation`
            : '/api/generate';
          const requestPayload = currentTaskPrefill.continue_generation
            ? {
                max_chapters: Number(document.getElementById('task_generation_num_chapters').value || 0) || null,
                operation_mode: payload.operation_mode,
                review_interval_chapters: payload.review_interval_chapters,
                progression_mode: payload.progression_mode || null,
                auto_band_checkpoint: payload.auto_band_checkpoint,
                manual_checkpoints_enabled: payload.manual_checkpoints_enabled,
                future_constraints_enabled: payload.future_constraints_enabled,
              }
            : payload;
          const created = await requestJson(requestUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestPayload),
          });
          closeTaskModal();
          switchTab('task');
          await loadTaskCenter();
          await loadBooks();
          setGlobalStatus(
            currentTaskPrefill.continue_generation
              ? `已创建继续生成任务 ${created.task_id}。`
              : `已创建生成任务 ${created.task_id}。`,
            currentTaskPrefill.continue_generation ? '继续生成' : '新建任务'
          );
          await openTaskDrawer('generation', created.task_id);
          return;
        }

        const protagonistNames = document.getElementById('task_upload_protagonist_names').value
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean);
        const payload = {
          project_id: currentTaskPrefill.project_id || null,
          platform: document.getElementById('task_upload_platform').value,
          book_name: document.getElementById('task_upload_book_name').value.trim(),
          chapter_title: document.getElementById('task_upload_chapter_title').value.trim(),
          body: document.getElementById('task_upload_body').value,
          upload_url: document.getElementById('task_upload_upload_url').value.trim() || null,
          publish: document.getElementById('task_upload_publish').checked,
          create_if_missing: document.getElementById('task_upload_create_if_missing').checked,
          book_meta: {
            audience: document.getElementById('task_upload_audience').value,
            primary_category: document.getElementById('task_upload_primary_category').value.trim(),
            protagonist_names: protagonistNames,
            intro: document.getElementById('task_upload_intro').value.trim(),
          },
        };
        if (!payload.platform || !payload.book_name || !payload.chapter_title || !payload.body.trim()) {
          setGlobalStatus('上传任务至少需要平台、作品名、章节名和正文。', '新建任务');
          return;
        }
        const created = await requestJson('/api/publishers/upload-jobs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        closeTaskModal();
        switchTab('task');
        await loadTaskCenter();
        await loadBooks();
        setGlobalStatus(`已创建上传任务 ${created.job_id}。`, '新建任务');
        await openTaskDrawer('upload', created.job_id);
      } catch (error) {
        setGlobalStatus(error.message || String(error), '新建任务失败');
      }
    }

    async function terminateTask(item) {
      const url = item.task_kind === 'upload'
        ? `/api/publishers/upload-jobs/${item.task_id}/terminate`
        : `/api/tasks/${item.task_id}/terminate`;
      try {
        const result = await requestJson(url, { method: 'POST' });
        setGlobalStatus(result.message || '已发送终止请求。', '任务操作');
        await loadTaskCenter();
        await loadBooks();
        if (currentDrawerTask && currentDrawerTask.task_kind === item.task_kind && currentDrawerTask.task_id === item.task_id) {
          await openTaskDrawer(item.task_kind, item.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务终止失败');
      }
    }

    async function pauseTask(item) {
      if (item.task_kind !== 'generation') {
        setGlobalStatus('发布任务暂不支持安全暂停，请使用终止。', '任务操作');
        return;
      }
      try {
        const result = await requestJson(`/api/tasks/${item.task_id}/pause`, { method: 'POST' });
        setGlobalStatus(result.message || '已发送安全暂停请求。', '任务操作');
        await loadTaskCenter();
        await loadBooks();
        if (currentDrawerTask && currentDrawerTask.task_kind === item.task_kind && currentDrawerTask.task_id === item.task_id) {
          await openTaskDrawer(item.task_kind, item.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务暂停失败');
      }
    }

    async function continueProjectGeneration(projectId) {
      const project = booksState.find((book) => book.id === projectId);
      openTaskModal('generation', {
        continue_generation: true,
        project_id: projectId,
        book_title: project?.title || '',
        premise: project?.premise || '',
        genre: project?.genre || @@DEFAULT_GENRE_JSON@@,
        num_chapters: project?.target_total_chapters || @@DEFAULT_CHAPTERS_JSON@@,
        operation_mode: project?.governance?.default_operation_mode || '',
        progression_mode: project?.governance?.progression_mode || '',
        auto_band_checkpoint: Boolean(project?.governance?.auto_band_checkpoint),
        manual_checkpoints_enabled: Boolean(project?.governance?.manual_checkpoints_enabled),
        future_constraints_enabled: Boolean(project?.governance?.future_constraints_enabled),
      });
    }

    async function deleteTask(item) {
      if (!window.confirm('删除后任务会从任务中心消失，确定继续吗？')) return;
      const url = item.task_kind === 'upload'
        ? `/api/publishers/upload-jobs/${item.task_id}`
        : `/api/tasks/${item.task_id}`;
      try {
        await requestJson(url, { method: 'DELETE' });
        selectedTaskKeys.delete(taskSelectionKey(item));
        setGlobalStatus(`任务 ${item.task_id} 已删除。`, '任务操作');
        await loadTaskCenter();
        await loadBooks();
        if (currentDrawerTask && currentDrawerTask.task_kind === item.task_kind && currentDrawerTask.task_id === item.task_id) {
          closeTaskDrawer();
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务删除失败');
      }
    }

    const CHAPTER_PIPELINE_STAGES = [
      ['assembling_context', '组装'],
      ['writing_chapter', '写作'],
      ['continuity_review', '审查'],
      ['applying_canon', 'Canon'],
      ['running_post_acceptance', '后置'],
      ['paused_for_review', '人工检查'],
      ['chapter_failed', '失败'],
    ];
    const CHAPTER_RUNTIME_STAGES = new Set(CHAPTER_PIPELINE_STAGES.map(([stage]) => stage));

    function taskHistory(item) {
      return Array.isArray(item.stage_history) ? item.stage_history.filter((entry) => entry && entry.stage) : [];
    }

    function latestHistoryEntry(history, stage) {
      for (let index = history.length - 1; index >= 0; index -= 1) {
        if (history[index]?.stage === stage) return history[index];
      }
      return null;
    }

    function latestChapterStageEntry(history, chapterNumber, stage) {
      for (let index = history.length - 1; index >= 0; index -= 1) {
        const entry = history[index];
        if (Number(entry?.chapter || 0) === Number(chapterNumber) && entry?.stage === stage) return entry;
      }
      return null;
    }

    function taskIntSet(values) {
      return new Set((Array.isArray(values) ? values : []).map((value) => Number(value || 0)).filter(Boolean));
    }

    function chapterStatusFromTask(item, chapter) {
      const number = Number(chapter?.chapter_number || 0);
      if (chapter?.status) return chapter.status;
      if (taskIntSet(item.paused_chapters).has(number)) return 'needs_review';
      if (taskIntSet(item.failed_chapters).has(number)) return 'failed';
      if (taskIntSet(item.completed_chapters).has(number)) return 'accepted';
      if (Number(item.current_chapter || 0) === number && CHAPTER_RUNTIME_STAGES.has(item.current_stage)) return 'running';
      return number ? 'planned' : '';
    }

    function formatStageNote(entry, fallback = '') {
      if (!entry) return fallback;
      const notes = [];
      if (entry.at) notes.push(entry.at);
      if (entry.chapter) notes.push(`章 ${entry.chapter}`);
      return notes.join(' | ') || fallback;
    }

    function stageFailureInspectable(item, chapter, stage, state, entry, status) {
      if (state === 'failed' || state === 'paused') return true;
      if (entry?.message && ['chapter_failed', 'paused_for_review', 'provisional_failed', 'failed'].includes(stage)) return true;
      if (stage === 'chapter_failed' && Array.isArray(item.failed_chapters) && item.failed_chapters.includes(Number(chapter?.chapter_number || 0))) return true;
      if (status === 'failed' && stage === 'chapter_failed') return true;
      if (status === 'needs_review' && stage === 'paused_for_review') return true;
      return false;
    }

    async function showStageFailureDetail(item, chapter, stage, entry, state, status) {
      const chapterNumber = Number(chapter?.chapter_number || entry?.chapter || 0);
      const lines = [
        `步骤：${stageLabel(stage)}`,
        chapterNumber ? `章节：第${chapterNumber}章${chapter?.title ? `《${chapter.title}》` : ''}` : '',
        `步骤状态：${state || '-'}`,
        status ? `章节状态：${chapterStatusLabel(status)}` : '',
        entry?.at ? `到达时间：${entry.at}` : '',
        entry?.message ? `阶段消息：${entry.message}` : '',
        item.message ? `任务消息：${item.message}` : '',
        item.error ? `任务错误：${item.error}` : '',
        Array.isArray(item.failed_chapters) && item.failed_chapters.length ? `失败章节：${item.failed_chapters.join(', ')}` : '',
        Array.isArray(item.paused_chapters) && item.paused_chapters.length ? `待 review 章节：${item.paused_chapters.join(', ')}` : '',
        Array.isArray(item.frozen_artifacts) && item.frozen_artifacts.length ? `冻结产物：${item.frozen_artifacts.join('\\n')}` : '',
      ].filter(Boolean);

      if (item.project_id && chapterNumber && (status === 'needs_review' || chapter?.has_review)) {
        try {
          const review = await requestJson(`/api/projects/${item.project_id}/chapters/${chapterNumber}/review`);
          lines.push(`Review verdict：${review.verdict || '-'}`);
          if (Array.isArray(review.issues) && review.issues.length) {
            lines.push('Review 问题：');
            review.issues.forEach((issue, index) => {
              const tags = [issue.severity || '-', issue.issue_group || '', issue.issue_type || ''].filter(Boolean).join(' / ');
              lines.push(`${index + 1}. [${tags}] ${issue.description || issue.rule_name || '未命名问题'}`);
            });
          }
          if (review.recommended_action) lines.push(`建议动作：${review.recommended_action}`);
          if (review.review_summary) lines.push(`Review 摘要：${review.review_summary}`);
        } catch (error) {
          lines.push(`Review 读取失败：${error.message || String(error)}`);
        }
      }

      window.alert(lines.join('\\n') || '这个步骤没有记录到失败原因。');
    }

    function renderMacroProgress(item) {
      const history = taskHistory(item);
      const completed = Array.isArray(item.completed_chapters) ? item.completed_chapters.length : 0;
      const failed = Array.isArray(item.failed_chapters) ? item.failed_chapters.length : 0;
      const paused = Array.isArray(item.paused_chapters) ? item.paused_chapters.length : 0;
      const requested = Number(item.requested_chapters || 0);
      const hasChapterWork = history.some((entry) => CHAPTER_RUNTIME_STAGES.has(entry.stage));
      const hasTerminal = ['completed', 'failed', 'partial_failed', 'needs_review', 'cancelled', 'paused'].includes(item.status);
      const provisionalEntry = latestHistoryEntry(history, 'running_provisional_preview');
      const provisionalFailed = latestHistoryEntry(history, 'provisional_failed');
      const currentIsChapterWork = CHAPTER_RUNTIME_STAGES.has(item.current_stage);
      const nodes = [
        {
          key: 'queued',
          label: '排队',
          state: latestHistoryEntry(history, 'queued') ? 'completed' : 'upcoming',
          note: formatStageNote(latestHistoryEntry(history, 'queued'), '等待开始'),
        },
        {
          key: 'planning_arc',
          label: '大纲',
          state: latestHistoryEntry(history, 'planning_arc') ? 'completed' : 'upcoming',
          note: formatStageNote(latestHistoryEntry(history, 'planning_arc'), '未记录'),
        },
        {
          key: 'resolving_arc_envelope',
          label: 'Arc',
          state: latestHistoryEntry(history, 'resolving_arc_envelope') ? 'completed' : 'upcoming',
          note: formatStageNote(latestHistoryEntry(history, 'resolving_arc_envelope'), '未解析'),
        },
        {
          key: 'provisional',
          label: 'Provisional',
          state: provisionalFailed ? 'failed' : (provisionalEntry ? 'completed' : 'upcoming'),
          note: provisionalFailed
            ? formatStageNote(provisionalFailed, '预演失败')
            : formatStageNote(provisionalEntry, '未执行或已跳过'),
        },
        {
          key: 'chapter_loop',
          label: '逐章生成',
          state: currentIsChapterWork ? 'current' : (hasChapterWork ? 'completed' : 'upcoming'),
          note: `${completed}/${requested || '-'} 完成 · ${failed} 失败 · ${paused} 暂停`,
        },
        {
          key: 'terminal',
          label: '结果',
          state: item.status === 'failed' || item.status === 'partial_failed' ? 'failed'
            : (['needs_review', 'paused'].includes(item.status) ? 'paused'
              : (hasTerminal ? 'completed' : 'upcoming')),
          note: stageLabel(item.current_stage || item.status),
        },
      ];

      const wrap = createNode('div', '', 'task-map');
      const head = createNode('div', '', 'task-map-head');
      const title = createNode('div', '', 'task-map-title');
      title.appendChild(createNode('strong', '任务主线'));
      title.appendChild(createNode('span', '这里展示一次性 gate；逐章循环见下方章节流水线。'));
      head.appendChild(title);
      head.appendChild(createNode('span', item.status || '', `badge ${badgeKindByStatus(item.status)}`));
      wrap.appendChild(head);

      const flow = createNode('div', '', 'macro-flow');
      nodes.forEach((nodeInfo) => {
        const node = createNode('div', '', `macro-node ${nodeInfo.state}`);
        node.appendChild(createNode('div', nodeInfo.label, 'stage-name'));
        node.appendChild(createNode('div', nodeInfo.note, 'stage-note'));
        flow.appendChild(node);
      });
      wrap.appendChild(flow);
      return wrap;
    }

    function chapterNumbersForTimeline(item, chapters) {
      const numbers = new Set();
      const requested = Number(item.requested_chapters || 0);
      for (let number = 1; number <= requested; number += 1) numbers.add(number);
      taskHistory(item).forEach((entry) => {
        const chapter = Number(entry.chapter || 0);
        if (chapter) numbers.add(chapter);
      });
      (Array.isArray(chapters) ? chapters : []).forEach((chapter) => {
        const number = Number(chapter.chapter_number || 0);
        if (number) numbers.add(number);
      });
      return Array.from(numbers).sort((a, b) => a - b);
    }

    function chapterLineState(item, chapter, status) {
      const number = Number(chapter?.chapter_number || 0);
      if (status === 'failed') return 'failed';
      if (status === 'accepted' || status === 'completed') return 'accepted';
      if (status === 'needs_review') return 'current';
      if (Number(item.current_chapter || 0) === number && CHAPTER_RUNTIME_STAGES.has(item.current_stage)) return 'current';
      return '';
    }

    function chapterStepState(item, history, chapter, stage, status) {
      const number = Number(chapter.chapter_number || 0);
      const entry = latestChapterStageEntry(history, number, stage);
      if (Number(item.current_chapter || 0) === number && item.current_stage === stage) {
        return stage === 'paused_for_review' ? 'paused' : 'current';
      }
      if (stage === 'chapter_failed' && status === 'failed') return 'failed';
      if (stage === 'paused_for_review' && status === 'needs_review') return 'paused';
      if (stage === 'paused_for_review' && ['accepted', 'completed'].includes(status)) return 'skipped';
      if (!entry) {
        if (['accepted', 'completed'].includes(status) && !['chapter_failed', 'paused_for_review'].includes(stage)) {
          return 'completed';
        }
        if (status === 'drafted' && ['assembling_context', 'writing_chapter', 'continuity_review'].includes(stage)) {
          return 'completed';
        }
        return 'upcoming';
      }
      if (stage === 'chapter_failed') return 'failed';
      if (stage === 'paused_for_review') return 'paused';
      return 'completed';
    }

    function renderChapterTimeline(item, chapters = []) {
      const history = taskHistory(item);
      const chapterByNumber = new Map((Array.isArray(chapters) ? chapters : []).map((chapter) => [Number(chapter.chapter_number || 0), chapter]));
      const numbers = chapterNumbersForTimeline(item, chapters);
      const section = createNode('section', '', 'detail-card');
      const head = createNode('div', '', 'task-map-head');
      const title = createNode('div', '', 'task-map-title');
      title.appendChild(createNode('strong', '章节流水线'));
      title.appendChild(createNode('span', '每一行是一章；横向从组装、写作、审查到写入 Canon，不再混用不同章节的 stage。'));
      head.appendChild(title);
      head.appendChild(createNode('span', `${numbers.length || 0} 章`, 'badge'));
      section.appendChild(head);

      const list = createNode('div', '', 'chapter-timeline');
      if (!numbers.length) {
        list.appendChild(createNode('div', '暂无章节进度。', 'empty'));
      }
      numbers.forEach((number) => {
        const chapter = chapterByNumber.get(number) || { chapter_number: number };
        const status = chapterStatusFromTask(item, chapter);
        const badgeStatus = status || 'planned';
        const row = createNode('div', '', `chapter-line ${chapterLineState(item, chapter, status)}`);
        const rowHead = createNode('div', '', 'chapter-line-head');
        rowHead.appendChild(createNode('strong', `第${number}章`));
        rowHead.appendChild(createNode('span', chapterStatusLabel(badgeStatus), `badge ${badgeKindByStatus(status)}`));
        const details = [
          chapter.title ? `《${chapter.title}》` : '',
          chapter.char_count ? `${chapter.char_count} 字` : '',
          chapter.has_draft ? '有正文' : '',
          chapter.has_review ? '有审查' : '',
        ].filter(Boolean).join(' · ');
        rowHead.appendChild(createNode('div', details || '尚无产物', 'stage-note'));
        if (item.project_id) {
          const chapterActions = createNode('div', '', 'action-row');
          chapterActions.appendChild(createButton('开始前 checkpoint', () => createManualCheckpointFromDrawer(item.project_id, {
            boundary_kind: 'chapter_start',
            boundary_chapter: number,
          }), 'ghost'));
          chapterActions.appendChild(createButton('accepted 后 checkpoint', () => createManualCheckpointFromDrawer(item.project_id, {
            boundary_kind: 'chapter_accepted',
            boundary_chapter: number,
          }), 'ghost'));
          rowHead.appendChild(chapterActions);
        }
        row.appendChild(rowHead);

        const steps = createNode('div', '', 'chapter-steps');
        CHAPTER_PIPELINE_STAGES.forEach(([stage, label]) => {
          const state = chapterStepState(item, history, chapter, stage, status);
          const step = createNode('div', '', `chapter-step ${state}`);
          step.appendChild(createNode('strong', label));
          const entry = latestChapterStageEntry(history, number, stage);
          let note = entry?.at || '';
          if (!note) {
            if (state === 'upcoming') note = '未到达';
            else if (state === 'failed') note = '失败';
            else if (state === 'paused') note = '待处理';
            else if (state === 'skipped') note = '未触发';
            else if (state === 'current') note = '进行中';
            else note = '已完成';
          }
          step.appendChild(createNode('span', note));
          if (stageFailureInspectable(item, chapter, stage, state, entry, status)) {
            step.classList.add('inspectable');
            step.title = '点击查看失败 / 暂停原因';
            step.setAttribute('role', 'button');
            step.tabIndex = 0;
            const inspect = () => showStageFailureDetail(item, chapter, stage, entry, state, status);
            step.addEventListener('click', inspect);
            step.addEventListener('keydown', (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                inspect();
              }
            });
          }
          steps.appendChild(step);
        });
        row.appendChild(steps);
        list.appendChild(row);
      });
      section.appendChild(list);
      return section;
    }

    async function loadProjectChapters(projectId) {
      return requestJson(`/api/projects/${projectId}/chapters`);
    }

    async function loadProjectDetail(projectId) {
      return requestJson(`/api/projects/${projectId}`);
    }

    async function toggleChapterBody(projectId, chapterNumber, bodyId) {
      const body = document.getElementById(bodyId);
      if (!body) return;
      if (body.classList.contains('open')) {
        body.classList.remove('open');
        return;
      }
      if (!body.dataset.loaded) {
        try {
          const chapter = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}`);
          body.textContent = chapter.body || '';
          body.dataset.loaded = '1';
        } catch (error) {
          body.textContent = error.message || String(error);
          body.dataset.loaded = '1';
        }
      }
      body.classList.add('open');
    }

    function describeDecisionScope(item) {
      return [
        item.created_at || '',
        item.scope || '',
        item.band_id ? `band ${item.band_id}` : '',
        item.chapter_number ? `chapter ${item.chapter_number}` : '',
        item.related_object_type || '',
        item.related_object_id ? `#${item.related_object_id}` : '',
      ].filter(Boolean).join(' | ');
    }

    function latestDecisionRefId(refs) {
      const items = Array.isArray(refs) ? refs : [];
      for (let index = items.length - 1; index >= 0; index -= 1) {
        const value = String(items[index]?.id || items[index]?.decision_event_id || '').trim();
        if (value) return value;
      }
      return '';
    }

    function focusDecisionEvent(decisionEventId, preferredFilter = 'all') {
      const eventId = String(decisionEventId || '').trim();
      if (!eventId) return false;
      if (window.currentDecisionTimelineController?.setFilter) {
        window.currentDecisionTimelineController.setFilter(preferredFilter || 'all');
      }
      const row = document.getElementById(`decision_event_${eventId}`);
      if (!row) return false;
      const parent = row.parentElement;
      if (parent) {
        parent.querySelectorAll('.chapter-row.focused').forEach((node) => node.classList.remove('focused'));
      }
      row.classList.add('focused');
      row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      window.setTimeout(() => row.classList.remove('focused'), 2200);
      return true;
    }

    async function jumpToReviewDecisionChain(projectId, chapterNumber) {
      try {
        const data = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review`);
        const targetId = latestDecisionRefId(data.decision_refs);
        if (!targetId || !focusDecisionEvent(targetId, 'chapter')) {
          setGlobalStatus('当前 Review 还没有可跳转的决策链，或时间线里暂未返回对应事件。', '治理时间线');
          return;
        }
        setGlobalStatus(`已跳到第 ${chapterNumber} 章 Review 的决策链。`, '治理时间线');
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Review 决策链读取失败');
      }
    }

    function jumpToCheckpointDecisionChain(checkpoint = null) {
      const targetId = latestDecisionRefId(checkpoint?.decision_refs);
      if (!targetId || !focusDecisionEvent(targetId, 'band')) {
        setGlobalStatus('当前 checkpoint 还没有可跳转的决策链。', '治理时间线');
        return;
      }
      setGlobalStatus(`已跳到 band ${checkpoint?.band_id || '-'} checkpoint 的决策链。`, '治理时间线');
    }

    async function showReview(projectId, chapterNumber) {
      try {
        const data = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review`);
        const lines = [
          `章节：第${chapterNumber}章《${data.title}》`,
          `状态：${data.status}`,
          `Verdict：${data.verdict}`,
          data.recommended_action ? `建议动作：${data.recommended_action}` : '',
          data.review_summary ? `摘要：${data.review_summary}` : '',
          Array.isArray(data.issues) && data.issues.length
            ? data.issues.map((issue, index) => `${index + 1}. [${[issue.severity, issue.issue_group, issue.issue_type].filter(Boolean).join(' / ')}] ${issue.description}`).join('\\n')
            : '无问题',
          Array.isArray(data.decision_refs) && data.decision_refs.length
            ? `决策链：${data.decision_refs.map((ref) => `${ref.event_type || 'event'}#${ref.id || ref.decision_event_id || '?'}`).join(', ')}`
            : '',
        ];
        window.alert(lines.join('\\n'));
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Review 读取失败');
      }
    }

    async function executeApproveReview(projectId, chapterNumber, continueGeneration = false, reason = '') {
      try {
        const data = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            continue_generation: Boolean(continueGeneration),
            reason: String(reason || '').trim(),
          }),
        });
        setGlobalStatus(data.message || `第${chapterNumber}章已接受。`, 'Review 处理');
        await loadTaskCenter();
        await loadBooks();
        if (data.task_id) {
          await openTaskDrawer('generation', data.task_id);
        } else if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Review 处理失败');
      }
    }

    function approveReview(projectId, chapterNumber, continueGeneration = false) {
      openGovernanceActionModal({
        title: continueGeneration ? `接受并继续 · 第${chapterNumber}章` : `接受 Review · 第${chapterNumber}章`,
        description: continueGeneration
          ? '本次会先接受当前 review，再尝试继续生成；如果仍命中治理 gate，会保留阻断。'
          : '接受当前 chapter review，并把理由写入决策时间线。',
        confirmLabel: continueGeneration ? '接受并继续' : '接受 Review',
        errorTitle: 'Review 处理失败',
        onSubmit: ({ reason }) => executeApproveReview(projectId, chapterNumber, continueGeneration, reason),
      });
    }

    function uniqueChapterNumbers(...groups) {
      const numbers = new Set();
      groups.forEach((values) => {
        (Array.isArray(values) ? values : []).forEach((value) => {
          const number = Number(value?.chapter_number || value || 0);
          if (number) numbers.add(number);
        });
      });
      return Array.from(numbers).sort((left, right) => left - right);
    }

    function chapterLookup(chapters = []) {
      return new Map((Array.isArray(chapters) ? chapters : []).map((chapter) => [
        Number(chapter.chapter_number || 0),
        chapter,
      ]));
    }

    function chaptersForNumbers(chapters, numbers) {
      const lookup = chapterLookup(chapters);
      return uniqueChapterNumbers(numbers).map((number) => lookup.get(number) || { chapter_number: number });
    }

    function actionableReviewChapters(item, control, chapters = []) {
      const pendingFromControl = Array.isArray(control.pending_review_chapters) ? control.pending_review_chapters : [];
      const pausedFromTask = Array.isArray(item.paused_chapters) ? item.paused_chapters : [];
      const pendingFromChapters = (Array.isArray(chapters) ? chapters : [])
        .filter((chapter) => chapter.status === 'needs_review')
        .map((chapter) => chapter.chapter_number);
      return chaptersForNumbers(chapters, uniqueChapterNumbers(pendingFromControl, pausedFromTask, pendingFromChapters));
    }

    function actionableFailedChapters(item, control, chapters = []) {
      const failedFromControl = Array.isArray(control.failed_chapters) ? control.failed_chapters : [];
      const failedFromTask = Array.isArray(item.failed_chapters) ? item.failed_chapters : [];
      const failedFromChapters = (Array.isArray(chapters) ? chapters : [])
        .filter((chapter) => chapter.status === 'failed')
        .map((chapter) => chapter.chapter_number);
      return chaptersForNumbers(chapters, uniqueChapterNumbers(failedFromControl, failedFromTask, failedFromChapters));
    }

    function firstPlannedOrFailedChapter(control, chapters = []) {
      const next = Number(control.next_chapter || 0);
      if (next) return next;
      const candidates = (Array.isArray(chapters) ? chapters : [])
        .filter((chapter) => ['planned', 'failed'].includes(chapter.status))
        .map((chapter) => Number(chapter.chapter_number || 0))
        .filter(Boolean)
        .sort((left, right) => left - right);
      return candidates[0] || 0;
    }

    function projectAsBook(item, project, chapters) {
      return {
        id: item.project_id,
        title: project?.title || item.title || '',
        chapters: Array.isArray(chapters) ? chapters : [],
        automation: project?.automation || {},
      };
    }

    function generationGuidance(item, project, chapters = []) {
      const control = item.generation_control || {};
      const reviewChapters = actionableReviewChapters(item, control, chapters);
      const failedChapters = actionableFailedChapters(item, control, chapters);
      const completed = Array.isArray(item.completed_chapters) ? item.completed_chapters.length : 0;
      const requested = Number(item.requested_chapters || project?.target_total_chapters || 0);
      const nextChapter = firstPlannedOrFailedChapter(control, chapters);
      const currentStage = stageLabel(item.current_stage || item.status);
      const currentChapter = Number(item.current_chapter || 0);
      const isActive = ACTIVE_TASK_STATUSES.has(item.status);

      if (item.pause_requested) {
        return {
          tone: 'blocked',
          eyebrow: '暂停请求已发出',
          title: '等待安全 checkpoint',
          description: '系统不会中断正在进行的 LLM 请求。当前请求或当前小阶段结束后，任务会保存进度并进入 paused。',
          next: '等待暂停落点',
          safety: '不要重启容器；等待任务自己进入 paused。',
          reviewChapters,
          failedChapters,
          completed,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (reviewChapters.length || item.status === 'needs_review') {
        const first = reviewChapters[0];
        const chapterLabel = first?.chapter_number ? `第 ${first.chapter_number} 章` : '待 review 章节';
        return {
          tone: 'blocked',
          eyebrow: '人工检查阻塞',
          title: `${chapterLabel} 需要处理`,
          description: '状态机要求先处理 needs_review。继续生成会被拒绝，直到人工查看 review、接受或修复该章。',
          next: '查看 Review 并决定是否接受',
          safety: '接受并继续会新建 continue task，不会重写已 accepted 章节。',
          reviewChapters,
          failedChapters,
          completed,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (item.status === 'paused') {
        return {
          tone: 'blocked',
          eyebrow: '已安全暂停',
          title: '进度已保存，可以继续',
          description: '任务停在安全边界。继续生成会从 planned / failed 章节恢复，不会重写已写入 Canon 的章节。',
          next: control.can_resume ? '继续生成剩余章节' : '等待可继续章节',
          safety: '如果还有 needs_review，系统会先要求处理 review。',
          reviewChapters,
          failedChapters,
          completed,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (['failed', 'partial_failed'].includes(item.status) || failedChapters.length) {
        return {
          tone: 'failed',
          eyebrow: item.status === 'partial_failed' ? '部分失败' : '生成失败',
          title: failedChapters.length ? `第 ${failedChapters[0].chapter_number} 章开始需要处理` : '生成任务失败',
          description: control.can_resume
            ? '失败章节可通过继续生成重试。先点失败节点查看原因，再决定是否直接继续。'
            : '当前没有可继续章节。先查看失败原因，确认是模型/API 问题还是流程问题。',
          next: control.can_resume ? '查看失败原因或重试剩余章节' : '查看失败原因',
          safety: '继续生成只会选择 failed / planned 章节。',
          reviewChapters,
          failedChapters,
          completed,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (isActive) {
        return {
          tone: '',
          eyebrow: '正在执行',
          title: currentChapter ? `第 ${currentChapter} 章 · ${currentStage}` : currentStage,
          description: '任务正在按状态机推进。安全暂停只会请求在 checkpoint 停住；强制终止用于必须中断的场景。',
          next: currentChapter ? `完成第 ${currentChapter} 章当前阶段` : '等待当前阶段完成',
          safety: '运行中不要重启容器，除非接受当前任务中断。',
          reviewChapters,
          failedChapters,
          completed,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      if (item.status === 'completed') {
        return {
          tone: '',
          eyebrow: '本轮完成',
          title: '章节已写入 Canon',
          description: '本轮选择的章节已经完成写作、review、canon 和后置处理。可以查看正文，或把最近章节发布到平台。',
          next: '查看正文或发布章节',
          safety: '后续继续生成会从下一批 planned 章节开始。',
          reviewChapters,
          failedChapters,
          completed,
          requested,
          nextChapter,
          currentStage,
          currentChapter,
          isActive,
        };
      }

      return {
        tone: control.can_resume ? 'blocked' : '',
        eyebrow: control.can_resume ? '可继续' : '书本状态',
        title: control.can_resume ? '还有章节未完成' : '没有活跃生成任务',
        description: control.can_resume
          ? '当前书本还有 planned / failed 章节，可以继续生成。'
          : '当前没有运行中的生成任务。可以从书本页新建生成，或查看已有章节与自动化配置。',
        next: control.can_resume ? '继续生成剩余章节' : '按需要创建新任务',
        safety: '书本入口不会自动重写已 accepted 章节。',
        reviewChapters,
        failedChapters,
        completed,
        requested,
        nextChapter,
        currentStage,
        currentChapter,
        isActive,
      };
    }

    function appendControlSideRow(parent, label, value) {
      const row = createNode('div', '', 'control-side-row');
      row.appendChild(createNode('span', label));
      row.appendChild(createNode('strong', String(value || '-')));
      parent.appendChild(row);
    }

    function renderGenerationQueue(item, project, chapters, guidance) {
      const queue = createNode('div', '', 'operator-queue');
      const control = item.generation_control || {};
      const reviewItems = guidance.reviewChapters.slice(0, 4);
      const failedItems = guidance.failedChapters.slice(0, 4);

      reviewItems.forEach((chapter) => {
        const row = createNode('div', '', 'queue-item warn');
        const main = createNode('div', '', 'queue-main');
        main.appendChild(createNode('strong', `第${chapter.chapter_number}章 · 待人工检查`));
        main.appendChild(createNode('span', chapter.title ? `《${chapter.title}》` : 'Review checkpoint 已阻塞继续生成。'));
        row.appendChild(main);
        const actions = createNode('div', '', 'queue-actions');
        const canReview = Boolean(item.project_id && chapter.chapter_number && chapter.has_review);
        const reviewButton = createButton('查看 Review', () => showReview(item.project_id, chapter.chapter_number), 'ghost');
        reviewButton.disabled = !canReview;
        actions.appendChild(reviewButton);
        const decisionButton = createButton('Review 决策链', () => jumpToReviewDecisionChain(item.project_id, chapter.chapter_number), 'ghost');
        decisionButton.disabled = !canReview;
        actions.appendChild(decisionButton);
        const acceptButton = createButton('接受', () => approveReview(item.project_id, chapter.chapter_number, false), 'secondary');
        acceptButton.disabled = !canReview;
        actions.appendChild(acceptButton);
        const continueButton = createButton('接受并继续', () => approveReview(item.project_id, chapter.chapter_number, true), 'primary');
        continueButton.disabled = !canReview;
        actions.appendChild(continueButton);
        row.appendChild(actions);
        queue.appendChild(row);
      });

      failedItems.forEach((chapter) => {
        const row = createNode('div', '', 'queue-item failed');
        const main = createNode('div', '', 'queue-main');
        main.appendChild(createNode('strong', `第${chapter.chapter_number}章 · 生成失败`));
        main.appendChild(createNode('span', chapter.title ? `《${chapter.title}》` : '点击失败节点可查看记录到的失败信息。'));
        row.appendChild(main);
        const actions = createNode('div', '', 'queue-actions');
        actions.appendChild(createButton('查看原因', () => showStageFailureDetail(
          item,
          chapter,
          'chapter_failed',
          latestChapterStageEntry(taskHistory(item), chapter.chapter_number, 'chapter_failed'),
          'failed',
          'failed',
        ), 'ghost'));
        if (control.can_resume && item.project_id) {
          actions.appendChild(createButton('重试剩余章节', () => continueProjectGeneration(item.project_id), 'primary'));
        }
        row.appendChild(actions);
        queue.appendChild(row);
      });

      if (!reviewItems.length && !failedItems.length) {
        queue.appendChild(createNode('div', guidance.safety, 'checkpoint-note'));
      }
      return queue;
    }

    function renderGenerationControlPanel(item, project, chapters = []) {
      const guidance = generationGuidance(item, project, chapters);
      const control = item.generation_control || {};
      const panel = createNode('section', '', `control-cockpit ${guidance.tone || ''}`);
      const main = createNode('div', '', 'control-main');
      const copy = createNode('div', '', 'control-copy');
      copy.appendChild(createNode('div', guidance.eyebrow, 'control-eyebrow'));
      copy.appendChild(createNode('h3', guidance.title, 'control-title'));
      copy.appendChild(createNode('div', guidance.description, 'control-description'));
      const actions = createNode('div', '', 'control-actions');
      if (guidance.reviewChapters.length) {
        const firstReview = guidance.reviewChapters[0];
        const reviewButton = createButton('处理第一个 Review', () => showReview(item.project_id, firstReview.chapter_number), 'primary');
        reviewButton.disabled = !(item.project_id && firstReview?.chapter_number && firstReview?.has_review);
        actions.appendChild(reviewButton);
      } else if (control.can_resume && item.project_id) {
        actions.appendChild(createButton('继续生成剩余章节', () => continueProjectGeneration(item.project_id), 'primary'));
      }
      if (item.pausable) actions.appendChild(createButton(item.pause_requested ? '已请求暂停' : '安全暂停', () => pauseTask(item), 'secondary'));
      if (project && item.project_id && pickLatestPublishableChapter(projectAsBook(item, project, chapters))) {
        actions.appendChild(createButton('发布最近章节', () => openBookPublishModal(projectAsBook(item, project, chapters)), 'secondary'));
      }
      if (item.terminable) actions.appendChild(createButton('强制终止', () => terminateTask(item), 'danger'));
      if (actions.childNodes.length) copy.appendChild(actions);
      main.appendChild(copy);

      const side = createNode('div', '', 'control-side');
      appendControlSideRow(side, '当前阶段', guidance.currentStage);
      appendControlSideRow(side, '当前章', guidance.currentChapter || '未进入章节');
      appendControlSideRow(side, '下一步', guidance.next);
      appendControlSideRow(side, '计划状态', control.plan_state || 'none');
      appendControlSideRow(side, '写作状态', control.writing_state || 'not_started');
      appendControlSideRow(side, 'Review', control.review_state || 'none');
      appendControlSideRow(side, '阻断原因', control.blocking_reason?.code ? (control.blocking_reason.message || control.blocking_reason.code) : '无');
      appendControlSideRow(side, 'Next Gate', control.next_gate || '未计算');
      appendControlSideRow(side, '下次人工检查', control.review_interval_chapters ? `${control.chapters_until_review} 章后` : '未设置');
      appendControlSideRow(side, 'Replan 可触发', `${control.chapters_until_replan_eligible || 0} 章后`);
      main.appendChild(side);
      panel.appendChild(main);
      panel.appendChild(renderGenerationQueue(item, project, chapters, guidance));
      return panel;
    }

    function governanceLabel(governance = {}) {
      const parts = [
        governance.progression_mode || 'legacy_relaxed',
        governance.auto_band_checkpoint ? 'auto-band-checkpoint' : 'manual-band-checkpoint',
        governance.future_constraints_enabled ? 'future-constraints:on' : 'future-constraints:off',
      ];
      return parts.join(' | ');
    }

    function renderDecisionTimeline(project = {}) {
      const card = createNode('section', '', 'detail-card');
      card.appendChild(createNode('div', '决策时间线', 'task-id'));
      const items = Array.isArray(project.decision_timeline) ? project.decision_timeline : [];
      if (!items.length) {
        card.appendChild(createNode('div', '当前还没有可展示的治理决策记录。', 'meta-line'));
        return card;
      }
      const toolbar = createNode('div', '', 'badge-row');
      const filterSelect = document.createElement('select');
      [
        ['all', '全部'],
        ['arc', 'arc'],
        ['project', 'project'],
        ['band', 'band'],
        ['chapter', 'chapter'],
        ['task', 'task'],
      ].forEach(([value, label]) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        filterSelect.appendChild(option);
      });
      toolbar.appendChild(createLabeledField('范围筛选', filterSelect));
      card.appendChild(toolbar);
      const list = createNode('div', '', 'drawer-grid');
      const rows = [];
      items.slice(0, 40).forEach((item) => {
        const row = createNode('div', '', 'chapter-row');
        const eventId = String(item.id || item.decision_event_id || '').trim();
        if (eventId) row.id = `decision_event_${eventId}`;
        row.dataset.scope = String(item.scope || 'project');
        row.appendChild(createNode('strong', `${item.event_family || 'event'} · ${item.event_type || 'unknown'}`));
        row.appendChild(createNode('div', describeDecisionScope(item), 'meta-line'));
        if (item.summary) row.appendChild(createNode('div', item.summary, 'meta-line'));
        if (item.reason) row.appendChild(createNode('div', `reason：${item.reason}`, 'meta-line'));
        if (item.parent_event_id || item.causal_root_id) {
          row.appendChild(createNode('div', `parent：${item.parent_event_id || '-'} | root：${item.causal_root_id || '-'}`, 'meta-line'));
        }
        if (eventId) {
          const actions = createNode('div', '', 'action-row');
          actions.appendChild(createButton('定位', () => focusDecisionEvent(eventId, filterSelect.value), 'ghost'));
          row.appendChild(actions);
        }
        rows.push(row);
        list.appendChild(row);
      });
      const applyFilter = () => {
        const scope = filterSelect.value || 'all';
        rows.forEach((row) => {
          const visible = scope === 'all' || scope === 'arc' || row.dataset.scope === scope;
          row.style.display = visible ? '' : 'none';
        });
      };
      filterSelect.addEventListener('change', applyFilter);
      applyFilter();
      window.currentDecisionTimelineController = {
        setFilter(scope) {
          filterSelect.value = ['all', 'arc', 'project', 'band', 'chapter', 'task'].includes(scope) ? scope : 'all';
          applyFilter();
        },
      };
      card.appendChild(list);
      return card;
    }

    function buildQueryString(params = {}) {
      const search = new URLSearchParams();
      Object.entries(params || {}).forEach(([key, value]) => {
        if (value === null || value === undefined) return;
        const normalized = String(value || '').trim();
        if (!normalized) return;
        search.set(key, normalized);
      });
      const query = search.toString();
      return query ? `?${query}` : '';
    }

    async function renderCausalReplayCard(item, project = {}) {
      const projectId = item.project_id;
      const latestCheckpoint = project.latest_band_checkpoint || item.generation_control?.latest_band_checkpoint || {};
      const defaultChapter = Number(item.current_chapter || item.generation_control?.current_chapter || 0);
      const defaultTaskId = String(item.task_id || '');
      const defaultBandId = String(latestCheckpoint.band_id || '');
      const card = createNode('section', '', 'detail-card');
      card.appendChild(createNode('div', '因果回放', 'task-id'));
      const toolbar = createNode('div', '', 'action-row');
      const scopeSelect = document.createElement('select');
      ['arc', 'project', 'band', 'chapter', 'task'].forEach((scope) => {
        const option = document.createElement('option');
        option.value = scope;
        option.textContent = scope;
        scopeSelect.appendChild(option);
      });
      scopeSelect.value = 'arc';
      toolbar.appendChild(scopeSelect);
      toolbar.appendChild(createButton('刷新回放', () => loadReplay(), 'ghost'));
      card.appendChild(toolbar);
      const content = createNode('div', '正在加载因果回放...', 'meta-line');
      card.appendChild(content);

      const loadReplay = async () => {
        clearNode(content);
        content.appendChild(createNode('div', '正在加载因果回放...', 'meta-line'));
        const scope = scopeSelect.value || 'project';
        const query = buildQueryString({
          scope,
          arc_id: scope === 'arc' ? (project.active_arc_id || '') : '',
          band_id: scope === 'band' ? defaultBandId : '',
          chapter_number: scope === 'chapter' ? defaultChapter : '',
          task_id: scope === 'task' ? defaultTaskId : '',
        });
        try {
          const replay = await requestJson(`/api/projects/${projectId}/causal-replay${query}`);
          clearNode(content);
          if (!replay?.timeline?.length) {
            content.appendChild(createNode('div', '当前范围没有可回放的治理事件。', 'meta-line'));
            return;
          }
          const summaryLines = [
            replay.root_event?.summary ? `Root：${replay.root_event.summary}` : '',
            replay.current_outcome ? `当前结果：${replay.current_outcome}` : '',
            Array.isArray(replay.linked_review_refs) && replay.linked_review_refs.length ? `关联 Review：${replay.linked_review_refs.length}` : '',
            Array.isArray(replay.linked_checkpoint_refs) && replay.linked_checkpoint_refs.length ? `关联 Checkpoint：${replay.linked_checkpoint_refs.length}` : '',
          ].filter(Boolean);
          if (summaryLines.length) {
            content.appendChild(createNode('div', summaryLines.join('\\n'), 'meta-line'));
          }
          const list = createNode('div', '', 'list');
          replay.timeline.slice(0, 18).forEach((event) => {
            const row = createNode('div', '', 'list-item');
            row.appendChild(createNode('strong', `${event.event_type || 'event'} · ${event.summary || '-'}`));
            row.appendChild(createNode('div', [
              event.scope ? `scope=${event.scope}` : '',
              event.chapter_number ? `chapter=${event.chapter_number}` : '',
              event.band_id ? `band=${event.band_id}` : '',
              event.parent_event_id ? `parent=${event.parent_event_id}` : '',
            ].filter(Boolean).join(' | '), 'meta-line'));
            if (event.id) {
              const actions = createNode('div', '', 'action-row');
              actions.appendChild(createButton('跳到时间线', () => focusDecisionEvent(event.id, scope), 'ghost'));
              row.appendChild(actions);
            }
            list.appendChild(row);
          });
          content.appendChild(list);
        } catch (error) {
          clearNode(content);
          content.appendChild(createNode('div', error.message || String(error), 'meta-line'));
        }
      };

      scopeSelect.addEventListener('change', loadReplay);
      await loadReplay();
      return card;
    }

    async function renderGovernanceInsightsCard(item) {
      const card = createNode('section', '', 'detail-card');
      card.appendChild(createNode('div', '治理洞察', 'task-id'));
      const content = createNode('div', '正在加载治理洞察...', 'meta-line');
      card.appendChild(content);
      try {
        const insights = await requestJson(`/api/projects/${item.project_id}/governance-insights`);
        clearNode(content);
        const headline = [
          Array.isArray(insights.most_common_blocking_reasons) && insights.most_common_blocking_reasons.length
            ? `高频阻断：${insights.most_common_blocking_reasons.map((row) => `${row.name}(${row.count})`).join(' / ')}`
            : '高频阻断：暂无',
          Array.isArray(insights.top_override_rule_types) && insights.top_override_rule_types.length
            ? `高频 override：${insights.top_override_rule_types.map((row) => `${row.name}(${row.count})`).join(' / ')}`
            : '高频 override：暂无',
          `forced accept：${insights.forced_accept_frequency || 0}`,
        ];
        content.appendChild(createNode('div', headline.join('\\n'), 'meta-line'));
        if (Array.isArray(insights.recent_band_checkpoint_distribution) && insights.recent_band_checkpoint_distribution.length) {
          content.appendChild(createNode('div', `最近 checkpoint：${insights.recent_band_checkpoint_distribution.map((row) => `${row.name}(${row.count})`).join(' / ')}`, 'meta-line'));
        }
        if (Array.isArray(insights.issue_group_distribution) && insights.issue_group_distribution.length) {
          content.appendChild(createNode('div', `Issue group：${insights.issue_group_distribution.map((row) => `${row.name}(${row.count})`).join(' / ')}`, 'meta-line'));
        }
        if (Array.isArray(insights.recommended_adjustments) && insights.recommended_adjustments.length) {
          const reco = createNode('div', '', 'list');
          insights.recommended_adjustments.slice(0, 5).forEach((entry) => {
            const row = createNode('div', '', 'list-item');
            row.appendChild(createNode('strong', `${entry.type || 'adjustment'} · ${entry.target || '-'}`));
            row.appendChild(createNode('div', `${entry.reason || ''}${entry.count ? `\\ncount=${entry.count}` : ''}`, 'meta-line'));
            reco.appendChild(row);
          });
          content.appendChild(reco);
        }
        if (Array.isArray(insights.recent_examples) && insights.recent_examples.length) {
          const examples = createNode('div', '', 'list');
          insights.recent_examples.slice(0, 6).forEach((entry) => {
            const row = createNode('div', '', 'list-item');
            row.appendChild(createNode('strong', `${entry.event_type || 'event'} · ${entry.summary || '-'}`));
            row.appendChild(createNode('div', [
              entry.chapter_number ? `chapter=${entry.chapter_number}` : '',
              entry.band_id ? `band=${entry.band_id}` : '',
              entry.blocking_reason ? `block=${entry.blocking_reason}` : '',
            ].filter(Boolean).join(' | '), 'meta-line'));
            if (entry.event_id) {
              const actions = createNode('div', '', 'action-row');
              actions.appendChild(createButton('跳到时间线', () => focusDecisionEvent(entry.event_id, 'all'), 'ghost'));
              row.appendChild(actions);
            }
            examples.appendChild(row);
          });
          content.appendChild(examples);
        }
      } catch (error) {
        clearNode(content);
        content.appendChild(createNode('div', error.message || String(error), 'meta-line'));
      }
      return card;
    }

    async function executeSaveProjectGovernance(projectId, fields, reason) {
      try {
        const payload = { ...fields, reason };
        const result = await requestJson(`/api/projects/${projectId}/governance`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        setGlobalStatus(result.message || '项目治理设置已保存。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '治理设置保存失败');
      }
    }

    function saveProjectGovernanceFromDrawer(projectId, fields) {
      openGovernanceActionModal({
        title: '保存治理设置',
        description: '项目级治理会影响后续默认行为；本次修改原因会进入决策时间线。',
        confirmLabel: '保存治理设置',
        errorTitle: '治理设置保存失败',
        onSubmit: ({ reason }) => executeSaveProjectGovernance(projectId, fields, reason),
      });
    }

    async function executeCreateManualCheckpoint(projectId, payload) {
      try {
        await requestJson(`/api/projects/${projectId}/manual-checkpoints`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        setGlobalStatus('manual checkpoint 已创建。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'manual checkpoint 创建失败');
      }
    }

    function createManualCheckpointFromDrawer(projectId, defaults = {}) {
      openGovernanceActionModal({
        title: '插入 Manual Checkpoint',
        description: 'v1 只支持章开始前、章 accepted 后、band 结束处三个边界。',
        confirmLabel: '创建 Checkpoint',
        errorTitle: 'manual checkpoint 创建失败',
        fields: [
          {
            name: 'boundary_kind',
            label: '边界',
            type: 'select',
            value: defaults.boundary_kind || 'band_end',
            options: ['chapter_start', 'chapter_accepted', 'band_end'],
          },
          {
            name: 'boundary_chapter',
            label: '章节号',
            type: 'number',
            value: defaults.boundary_chapter || 0,
            min: 0,
            step: 1,
          },
        ],
        onSubmit: ({ reason, boundary_kind, boundary_chapter }) => executeCreateManualCheckpoint(projectId, {
          boundary_kind: String(boundary_kind || '').trim(),
          boundary_chapter: Number.isFinite(Number(boundary_chapter)) ? Number(boundary_chapter) : 0,
          reason,
        }),
      });
    }

    async function executeCreateNarrativeConstraint(projectId, payload) {
      try {
        await requestJson(`/api/projects/${projectId}/constraints`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        setGlobalStatus('narrative constraint 已创建。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'narrative constraint 创建失败');
      }
    }

    function createNarrativeConstraintFromDrawer(projectId) {
      openGovernanceActionModal({
        title: '新增 Narrative Constraint',
        description: 'constraint 可保存展示；只有启用 future constraints 后才参与 review/checkpoint 判定。',
        confirmLabel: '创建 Constraint',
        errorTitle: 'narrative constraint 创建失败',
        fields: [
          { name: 'constraint_type', label: '约束类型', type: 'select', value: 'character_availability', options: ['character_availability', 'secret_withhold', 'relationship_preserve', 'thread_keep_open', 'location_availability', 'rule_preserve'] },
          { name: 'level', label: '级别', type: 'select', value: 'hard', options: ['hard', 'soft', 'hint'] },
          { name: 'subject_name', label: '主体', type: 'text', value: '' },
          { name: 'description', label: '说明', type: 'text', value: '' },
          { name: 'effective_from_chapter', label: '起始章节', type: 'number', value: 1, min: 1, step: 1 },
          { name: 'protect_until_chapter', label: '保护到章节', type: 'number', value: 0, min: 0, step: 1 },
        ],
        onSubmit: ({ reason, constraint_type, level, subject_name, description, effective_from_chapter, protect_until_chapter }) => executeCreateNarrativeConstraint(projectId, {
          constraint_type,
          level,
          subject_name,
          description,
          effective_from_chapter: Number.isFinite(Number(effective_from_chapter)) ? Number(effective_from_chapter) : 1,
          protect_until_chapter: Number.isFinite(Number(protect_until_chapter)) ? Number(protect_until_chapter) : 0,
          reason,
        }),
      });
    }

    async function executeUpdateNarrativeConstraint(projectId, constraintId, payload) {
      try {
        await requestJson(`/api/projects/${projectId}/constraints/${constraintId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        setGlobalStatus('narrative constraint 已更新。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'narrative constraint 更新失败');
      }
    }

    function editNarrativeConstraintFromDrawer(projectId, constraint) {
      if (!constraint?.id) return;
      openGovernanceActionModal({
        title: '编辑 Narrative Constraint',
        description: '修改会写入 constraint_updated 决策事件。',
        confirmLabel: '保存 Constraint',
        errorTitle: 'narrative constraint 更新失败',
        fields: [
          { name: 'constraint_type', label: '约束类型', type: 'select', value: constraint.constraint_type || 'character_availability', options: ['character_availability', 'secret_withhold', 'relationship_preserve', 'thread_keep_open', 'location_availability', 'rule_preserve'] },
          { name: 'level', label: '级别', type: 'select', value: constraint.level || 'hard', options: ['hard', 'soft', 'hint'] },
          { name: 'subject_name', label: '主体', type: 'text', value: constraint.subject_name || '' },
          { name: 'description', label: '说明', type: 'text', value: constraint.description || '' },
          { name: 'effective_from_chapter', label: '起始章节', type: 'number', value: constraint.effective_from_chapter || 1, min: 1, step: 1 },
          { name: 'protect_until_chapter', label: '保护到章节', type: 'number', value: constraint.protect_until_chapter || 0, min: 0, step: 1 },
        ],
        onSubmit: ({ reason, constraint_type, level, subject_name, description, effective_from_chapter, protect_until_chapter }) => executeUpdateNarrativeConstraint(projectId, constraint.id, {
          constraint_type,
          level,
          subject_name,
          description,
          effective_from_chapter: Number.isFinite(Number(effective_from_chapter)) ? Number(effective_from_chapter) : 1,
          protect_until_chapter: Number.isFinite(Number(protect_until_chapter)) ? Number(protect_until_chapter) : 0,
          reason,
        }),
      });
    }

    function archiveNarrativeConstraintFromDrawer(projectId, constraint) {
      if (!constraint?.id) return;
      openGovernanceActionModal({
        title: '停用 Narrative Constraint',
        description: '停用后 constraint 仍会保留展示，但不再作为 active constraint。',
        confirmLabel: '停用 Constraint',
        errorTitle: 'narrative constraint 停用失败',
        onSubmit: ({ reason }) => executeUpdateNarrativeConstraint(projectId, constraint.id, {
          status: 'inactive',
          reason,
        }),
      });
    }

    function normalizeTaskContractItems(rawText) {
      let parsed = [];
      try {
        parsed = JSON.parse(String(rawText || '[]'));
      } catch (error) {
        throw new Error('Task contract 必须是 JSON 数组。');
      }
      if (!Array.isArray(parsed)) {
        throw new Error('Task contract 必须是 JSON 数组。');
      }
      return parsed.map((item) => ({
        task_type: String(item?.task_type || 'plot_advance').trim(),
        description: String(item?.description || '').trim(),
        target_name: String(item?.target_name || '').trim(),
        required_keywords: Array.isArray(item?.required_keywords) ? item.required_keywords.map((entry) => String(entry || '').trim()).filter(Boolean) : [],
        forbidden_keywords: Array.isArray(item?.forbidden_keywords) ? item.forbidden_keywords.map((entry) => String(entry || '').trim()).filter(Boolean) : [],
        source: String(item?.source || 'manual').trim() || 'manual',
      }));
    }

    async function executeUpdateTaskContract(projectId, scope, identifier, items, reason) {
      const url = scope === 'band'
        ? `/api/projects/${projectId}/bands/${encodeURIComponent(identifier)}/task-contract`
        : `/api/projects/${projectId}/chapters/${Number(identifier || 0)}/task-contract`;
      await requestJson(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items, reason }),
      });
      setGlobalStatus('task contract 已更新。', '治理设置');
      await loadBooks();
      if (currentDrawerTask?.project_id === projectId) {
        await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
      }
    }

    async function editTaskContractFromDrawer(projectId, scope, identifier) {
      if (!projectId || !identifier) {
        setGlobalStatus('缺少 task contract 目标。', '治理设置');
        return;
      }
      const url = scope === 'band'
        ? `/api/projects/${projectId}/bands/${encodeURIComponent(identifier)}/task-contract`
        : `/api/projects/${projectId}/chapters/${Number(identifier || 0)}/task-contract`;
      try {
        const current = await requestJson(url);
        openGovernanceActionModal({
          title: scope === 'band' ? `编辑 Band Task Contract · ${identifier}` : `编辑 Chapter Task Contract · 第${identifier}章`,
          description: '填写 PlanTaskItem JSON 数组。修改会进入决策时间线，review/checkpoint 会使用这份合同判断规划履约。',
          confirmLabel: '保存 Task Contract',
          errorTitle: 'task contract 更新失败',
          fields: [
            {
              name: 'items_json',
              label: 'PlanTaskItem JSON',
              type: 'textarea',
              rows: 10,
              value: JSON.stringify(current.items || [], null, 2),
            },
          ],
          onSubmit: ({ reason, items_json }) => executeUpdateTaskContract(
            projectId,
            scope,
            identifier,
            normalizeTaskContractItems(items_json),
            reason
          ),
        });
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'task contract 读取失败');
      }
    }

    async function executeApproveBandCheckpoint(projectId, bandId, status, reason) {
      try {
        await requestJson(`/api/projects/${projectId}/bands/${bandId}/checkpoint/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status, reason }),
        });
        setGlobalStatus('band checkpoint 已更新。', '治理设置');
        await loadBooks();
        if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'band checkpoint 更新失败');
      }
    }

    function approveBandCheckpointFromDrawer(projectId, bandId, status = 'overridden') {
      openGovernanceActionModal({
        title: status === 'pass' ? `放行 Checkpoint · ${bandId}` : `Override Checkpoint · ${bandId}`,
        description: status === 'pass'
          ? '人工确认当前 band checkpoint 可以 pass。'
          : '当前 override 会进入治理时间线，后续 insight 会统计这类人工放行。',
        confirmLabel: status === 'pass' ? 'Pass Checkpoint' : 'Override Checkpoint',
        errorTitle: 'band checkpoint 更新失败',
        onSubmit: ({ reason }) => executeApproveBandCheckpoint(projectId, bandId, status, reason),
      });
    }

    function renderGovernanceCard(item, project = {}) {
      const governance = project.governance || {};
      const blockingReason = project.blocking_reason || item.generation_control?.blocking_reason || {};
      const latestCheckpoint = project.latest_band_checkpoint || item.generation_control?.latest_band_checkpoint || null;
      const card = createNode('section', '', 'detail-card');
      card.appendChild(createNode('div', '治理设置', 'task-id'));
      const badges = createNode('div', '', 'badge-row');
      badges.appendChild(createNode('span', governance.default_operation_mode || 'blackbox', 'badge'));
      badges.appendChild(createNode('span', governance.progression_mode || 'legacy_relaxed', 'badge'));
      if (governance.auto_band_checkpoint) badges.appendChild(createNode('span', 'auto band checkpoint', 'badge ok'));
      badges.appendChild(createNode('span', governance.future_constraints_enabled ? 'future constraints 参与判定' : 'future constraints 仅保存/展示', governance.future_constraints_enabled ? 'badge ok' : 'badge warn'));
      card.appendChild(badges);
      card.appendChild(createNode('div', `当前策略：${governanceLabel(governance)}\n下一 gate：${project.next_gate || item.generation_control?.next_gate || '-'}\n人工检查间隔：${governance.review_interval_chapters || 0}`, 'meta-line'));
      if (blockingReason?.code) {
        card.appendChild(createNode('div', `阻断原因：${blockingReason.message || blockingReason.code}${blockingReason.detail ? `\n${blockingReason.detail}` : ''}`, 'meta-line'));
        if (blockingReason.decision_event_id) {
          const blockingActions = createNode('div', '', 'action-row');
          blockingActions.appendChild(createButton('跳到阻断决策', () => {
            if (!focusDecisionEvent(blockingReason.decision_event_id, 'all')) {
              setGlobalStatus('当前阻断原因没有映射到可见的决策事件。', '治理时间线');
            }
          }, 'ghost'));
          card.appendChild(blockingActions);
        }
      }
      if (latestCheckpoint) {
        card.appendChild(createNode('div', `最新 band checkpoint：${latestCheckpoint.band_id || '-'} | ${latestCheckpoint.status || 'pending'}${latestCheckpoint.summary ? `\n${latestCheckpoint.summary}` : ''}`, 'meta-line'));
        if (Array.isArray(latestCheckpoint.issues) && latestCheckpoint.issues.length) {
          card.appendChild(createNode('div', latestCheckpoint.issues.slice(0, 5).map((issue) => [
            issue.severity || 'info',
            issue.issue_group || '',
            issue.category || '',
            issue.description || issue.code || '',
          ].filter(Boolean).join(' · ')).join('\\n'), 'meta-line'));
        }
        if (Array.isArray(latestCheckpoint.decision_refs) && latestCheckpoint.decision_refs.length) {
          const checkpointActions = createNode('div', '', 'action-row');
          checkpointActions.appendChild(createButton('查看 Checkpoint 决策链', () => jumpToCheckpointDecisionChain(latestCheckpoint), 'ghost'));
          card.appendChild(checkpointActions);
        }
      }

      const form = createNode('div', '', 'drawer-grid');
      const operationMode = document.createElement('select');
      ['blackbox', 'copilot', 'checkpoint'].forEach((value) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        operationMode.appendChild(option);
      });
      operationMode.value = governance.default_operation_mode || 'blackbox';
      form.appendChild(createLabeledField('默认运行模式', operationMode));

      const progressionMode = document.createElement('select');
      ['legacy_relaxed', 'serial_canon', 'serial_canon_band_guard'].forEach((value) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        progressionMode.appendChild(option);
      });
      progressionMode.value = governance.progression_mode || 'legacy_relaxed';
      form.appendChild(createLabeledField('推进策略', progressionMode));

      const reviewInterval = document.createElement('input');
      reviewInterval.type = 'number';
      reviewInterval.min = '0';
      reviewInterval.max = '200';
      reviewInterval.step = '1';
      reviewInterval.value = String(governance.review_interval_chapters || 0);
      form.appendChild(createLabeledField('每 N 章人工检查', reviewInterval));

      const autoBandCheckpoint = document.createElement('input');
      autoBandCheckpoint.type = 'checkbox';
      autoBandCheckpoint.checked = Boolean(governance.auto_band_checkpoint);
      const autoBandCheckpointWrap = document.createElement('label');
      autoBandCheckpointWrap.className = 'checkbox';
      autoBandCheckpointWrap.appendChild(autoBandCheckpoint);
      autoBandCheckpointWrap.appendChild(document.createTextNode('自动 band checkpoint'));
      form.appendChild(autoBandCheckpointWrap);

      const manualCheckpoint = document.createElement('input');
      manualCheckpoint.type = 'checkbox';
      manualCheckpoint.checked = Boolean(governance.manual_checkpoints_enabled);
      const manualCheckpointWrap = document.createElement('label');
      manualCheckpointWrap.className = 'checkbox';
      manualCheckpointWrap.appendChild(manualCheckpoint);
      manualCheckpointWrap.appendChild(document.createTextNode('允许 manual checkpoint'));
      form.appendChild(manualCheckpointWrap);

      const futureConstraints = document.createElement('input');
      futureConstraints.type = 'checkbox';
      futureConstraints.checked = Boolean(governance.future_constraints_enabled);
      const futureConstraintsWrap = document.createElement('label');
      futureConstraintsWrap.className = 'checkbox';
      futureConstraintsWrap.appendChild(futureConstraints);
      futureConstraintsWrap.appendChild(document.createTextNode('启用 future constraints'));
      form.appendChild(futureConstraintsWrap);

      card.appendChild(form);

      const actions = createNode('div', '', 'action-row');
      actions.appendChild(createButton('保存治理设置', () => saveProjectGovernanceFromDrawer(item.project_id, {
        default_operation_mode: operationMode.value,
        progression_mode: progressionMode.value,
        review_interval_chapters: normalizeReviewInterval(reviewInterval.value),
        auto_band_checkpoint: autoBandCheckpoint.checked,
        manual_checkpoints_enabled: manualCheckpoint.checked,
        future_constraints_enabled: futureConstraints.checked,
      }), 'primary'));
      actions.appendChild(createButton('插入 Manual Checkpoint', () => createManualCheckpointFromDrawer(item.project_id, {
        boundary_kind: latestCheckpoint?.boundary_kind || 'band_end',
        boundary_chapter: latestCheckpoint?.boundary_chapter || item.current_chapter || 0,
      }), 'secondary'));
      const contractChapter = Number(item.current_chapter || project.generation_control?.next_chapter || 0);
      if (contractChapter > 0) {
        actions.appendChild(createButton('编辑本章 Task Contract', () => editTaskContractFromDrawer(item.project_id, 'chapter', contractChapter), 'ghost'));
      }
      if (latestCheckpoint?.band_id) {
        actions.appendChild(createButton('编辑 Band Task Contract', () => editTaskContractFromDrawer(item.project_id, 'band', latestCheckpoint.band_id), 'ghost'));
      }
      actions.appendChild(createButton('新增 Constraint', () => createNarrativeConstraintFromDrawer(item.project_id), 'ghost'));
      if (latestCheckpoint?.band_id && ['warn', 'fail', 'pending', 'error'].includes(String(latestCheckpoint.status || ''))) {
        actions.appendChild(createButton('Override Checkpoint', () => approveBandCheckpointFromDrawer(item.project_id, latestCheckpoint.band_id, 'overridden'), 'ghost'));
      }
      card.appendChild(actions);

      const constraints = Array.isArray(project.narrative_constraints) ? project.narrative_constraints : [];
      if (constraints.length) {
        const constraintsList = createNode('div', '', 'list');
        constraints.slice(0, 8).forEach((constraint) => {
          const row = createNode('div', '', 'list-item');
          row.appendChild(createNode('strong', `${constraint.status || 'active'} · ${constraint.level || 'hard'} · ${constraint.constraint_type || ''}`));
          row.appendChild(createNode('div', `${constraint.subject_name || constraint.description || '-'}${constraint.protect_until_chapter ? `\nprotect_until=${constraint.protect_until_chapter}` : ''}`, 'meta-line'));
          const constraintActions = createNode('div', '', 'action-row');
          constraintActions.appendChild(createButton('编辑', () => editNarrativeConstraintFromDrawer(item.project_id, constraint), 'ghost'));
          if (String(constraint.status || 'active') === 'active') {
            constraintActions.appendChild(createButton('停用', () => archiveNarrativeConstraintFromDrawer(item.project_id, constraint), 'ghost'));
          }
          row.appendChild(constraintActions);
          constraintsList.appendChild(row);
        });
        card.appendChild(constraintsList);
      }
      return card;
    }

    async function renderGenerationDrawer(item, drawerProject = null, drawerChapters = null) {
      const body = document.getElementById('drawer_body');
      let projectDetail = drawerProject || null;
      let projectChapters = Array.isArray(drawerChapters) ? drawerChapters : null;
      let projectLoadError = null;
      if (item.project_id) {
        try {
          projectDetail = projectDetail || await loadProjectDetail(item.project_id);
          projectChapters = projectChapters || (Array.isArray(projectDetail.chapters)
            ? projectDetail.chapters
            : await loadProjectChapters(item.project_id));
        } catch (error) {
          projectLoadError = error;
          projectChapters = projectChapters || [];
        }
      }

      body.appendChild(renderGenerationControlPanel(item, projectDetail, projectChapters || []));
      if (item.project_id && projectDetail) {
        body.appendChild(renderGovernanceCard(item, projectDetail));
        body.appendChild(await renderCausalReplayCard(item, projectDetail));
        body.appendChild(await renderGovernanceInsightsCard(item));
      }

      const top = createNode('section', '', 'detail-card');
      const badges = createNode('div', '', 'badge-row');
      badges.appendChild(createNode('span', item.status, `badge ${badgeKindByStatus(item.status)}`));
      if (item.project_id) badges.appendChild(createNode('span', `Project ${item.project_id}`, 'badge'));
      top.appendChild(badges);
      top.appendChild(renderMacroProgress(item));
      body.appendChild(top);

      const metrics = createNode('section', '', 'progress-grid');
      [
        ['已完成', Array.isArray(item.completed_chapters) ? item.completed_chapters.length : 0],
        ['失败', Array.isArray(item.failed_chapters) ? item.failed_chapters.length : 0],
        ['暂停', Array.isArray(item.paused_chapters) ? item.paused_chapters.length : 0],
        ['当前章', item.current_chapter || 0],
      ].forEach(([label, value]) => {
        const metric = createNode('div', '', 'metric');
        metric.appendChild(createNode('strong', String(value)));
        metric.appendChild(createNode('span', label));
        metrics.appendChild(metric);
      });
      body.appendChild(metrics);

      const misc = createNode('section', '', 'detail-card');
      misc.appendChild(createNode('div', `请求章节数：${item.requested_chapters || 0}`, 'meta-line'));
      if (item.message) misc.appendChild(createNode('div', `消息：${item.message}`, 'meta-line'));
      if (item.error) misc.appendChild(createNode('div', `错误：${item.error}`, 'meta-line'));
      if (Array.isArray(item.failed_chapters) && item.failed_chapters.length) misc.appendChild(createNode('div', `失败章节：${item.failed_chapters.join(', ')}`, 'meta-line'));
      if (Array.isArray(item.paused_chapters) && item.paused_chapters.length) misc.appendChild(createNode('div', `暂停章节：${item.paused_chapters.join(', ')}`, 'meta-line'));
      if (Array.isArray(item.frozen_artifacts) && item.frozen_artifacts.length) misc.appendChild(createNode('div', `冻结产物：${item.frozen_artifacts.join('\\n')}`, 'meta-line'));
      const control = item.generation_control || {};
      const controlLines = [
        `计划状态：${control.plan_state || '-'}`,
        `写作状态：${control.writing_state || '-'}`,
        `Review：${control.review_state || '-'}`,
        `下一章：${control.next_chapter || 0}`,
        `距人工检查：${control.review_interval_chapters ? control.chapters_until_review : '未设置'}`,
        `距 replan 可触发：${control.chapters_until_replan_eligible || 0}`,
      ];
      misc.appendChild(createNode('div', controlLines.join('\\n'), 'meta-line'));
      body.appendChild(misc);

      if (!item.project_id) {
        body.appendChild(renderChapterTimeline(item, []));
        return;
      }
      if (projectLoadError) {
        body.appendChild(createNode('div', projectLoadError.message || String(projectLoadError), 'detail-card'));
        return;
      }

      const project = projectDetail || {};
      const chapters = projectChapters || [];
      try {
        body.appendChild(renderChapterTimeline(item, chapters));
        const automation = project.automation || {};
        const automationCard = createNode('section', '', 'detail-card');
        automationCard.appendChild(createNode('div', '每日自动化', 'task-id'));
        const automationBadges = createNode('div', '', 'badge-row');
        automationBadges.appendChild(createNode('span', automation.enabled ? '已开启' : '已关闭', `badge ${automation.enabled ? 'ok' : 'warn'}`));
        automationBadges.appendChild(createNode('span', `每日 ${automation.daily_chapter_quota || 1} 章`, 'badge'));
        automationBadges.appendChild(createNode('span', `${automation.daily_start_time || '09:00'} 开始`, 'badge'));
        if (automation.auto_publish) automationBadges.appendChild(createNode('span', '完成后自动发布', 'badge ok'));
        automationCard.appendChild(automationBadges);

        const automationStatus = [
          automation.last_scheduler_at ? `上次调度：${automation.last_scheduler_at}` : '',
          automation.last_scheduler_action ? `动作：${automation.last_scheduler_action}` : '',
          automation.last_scheduler_message ? `说明：${automation.last_scheduler_message}` : '',
          automation.last_scheduler_task_id ? `任务：${automation.last_scheduler_task_id}` : '',
        ].filter(Boolean).join('\\n');
        if (automationStatus) {
          automationCard.appendChild(createNode('div', automationStatus, 'meta-line'));
        }

        const automationForm = createNode('div', '', 'drawer-grid');
        const enabledInput = document.createElement('input');
        enabledInput.type = 'checkbox';
        enabledInput.checked = Boolean(automation.enabled);
        const enabledWrap = document.createElement('label');
        enabledWrap.className = 'checkbox';
        enabledWrap.appendChild(enabledInput);
        enabledWrap.appendChild(document.createTextNode('到点后自动处理这个书本的生成任务'));
        automationForm.appendChild(enabledWrap);

        const autoPublishInput = document.createElement('input');
        autoPublishInput.type = 'checkbox';
        autoPublishInput.checked = Boolean(automation.auto_publish);
        const autoPublishWrap = document.createElement('label');
        autoPublishWrap.className = 'checkbox';
        autoPublishWrap.appendChild(autoPublishInput);
        autoPublishWrap.appendChild(document.createTextNode('生成完成后自动创建发布任务'));
        automationForm.appendChild(autoPublishWrap);

        const timeInput = document.createElement('input');
        timeInput.type = 'time';
        timeInput.value = automation.daily_start_time || '09:00';
        automationForm.appendChild(createLabeledField('每日开始时间', timeInput));

        const quotaInput = document.createElement('input');
        quotaInput.type = 'number';
        quotaInput.min = '1';
        quotaInput.max = '20';
        quotaInput.step = '1';
        quotaInput.value = String(automation.daily_chapter_quota || 1);
        automationForm.appendChild(createLabeledField('每天最多生成几章', quotaInput));

        const platformSelect = document.createElement('select');
        const selectedPlatform = automation.publish?.platform || '';
        const platformOptions = Array.isArray(platformsState) ? platformsState : [];
        if (!selectedPlatform) {
          const emptyOption = document.createElement('option');
          emptyOption.value = '';
          emptyOption.textContent = '选择发布平台';
          platformSelect.appendChild(emptyOption);
        }
        if (selectedPlatform && !platformOptions.some((entry) => entry.platform_id === selectedPlatform)) {
          const currentOption = document.createElement('option');
          currentOption.value = selectedPlatform;
          currentOption.textContent = selectedPlatform;
          platformSelect.appendChild(currentOption);
        }
        platformOptions.forEach((entry) => {
          const option = document.createElement('option');
          option.value = entry.platform_id;
          option.textContent = entry.display_name || entry.platform_id;
          platformSelect.appendChild(option);
        });
        platformSelect.value = selectedPlatform;
        automationForm.appendChild(createLabeledField('自动发布平台', platformSelect));

        const bookNameInput = document.createElement('input');
        bookNameInput.type = 'text';
        bookNameInput.value = automation.publish?.book_name || project.title || '';
        automationForm.appendChild(createLabeledField('发布时作品名', bookNameInput));

        const uploadUrlInput = document.createElement('input');
        uploadUrlInput.type = 'text';
        uploadUrlInput.value = automation.publish?.upload_url || '';
        automationForm.appendChild(createLabeledField('固定上传页 URL', uploadUrlInput));

        const createIfMissingInput = document.createElement('input');
        createIfMissingInput.type = 'checkbox';
        createIfMissingInput.checked = Boolean(automation.publish?.create_if_missing);
        const createIfMissingWrap = document.createElement('label');
        createIfMissingWrap.className = 'checkbox';
        createIfMissingWrap.appendChild(createIfMissingInput);
        createIfMissingWrap.appendChild(document.createTextNode('若作品不存在则自动新建'));
        automationForm.appendChild(createIfMissingWrap);

        const audienceInput = document.createElement('input');
        audienceInput.type = 'text';
        audienceInput.value = automation.publish?.book_meta?.audience || '';
        automationForm.appendChild(createLabeledField('目标读者', audienceInput));

        const primaryCategoryInput = document.createElement('input');
        primaryCategoryInput.type = 'text';
        primaryCategoryInput.value = automation.publish?.book_meta?.primary_category || '';
        automationForm.appendChild(createLabeledField('主分类', primaryCategoryInput));

        const protagonistNamesInput = document.createElement('input');
        protagonistNamesInput.type = 'text';
        protagonistNamesInput.value = Array.isArray(automation.publish?.book_meta?.protagonist_names)
          ? automation.publish.book_meta.protagonist_names.join(', ')
          : '';
        automationForm.appendChild(createLabeledField('主角名（逗号分隔）', protagonistNamesInput));

        const introInput = document.createElement('textarea');
        introInput.rows = 4;
        introInput.value = automation.publish?.book_meta?.intro || '';
        automationForm.appendChild(createLabeledField('作品简介', introInput));

        const publishBindingsSummary = formatPublishBindingsSummary(automation);
        if (publishBindingsSummary) {
          automationCard.appendChild(createNode('div', `已绑定平台：${publishBindingsSummary}`, 'meta-line'));
        }
        automationCard.appendChild(automationForm);
        automationCard.appendChild(createNode(
          'div',
          '说明：系统每天只会在设定时间之后检查一次；如果仍有章节待 review，当天会跳过自动生成。',
          'meta-line',
        ));
        automationCard.appendChild(createButton('保存自动化设置', async () => {
          try {
            const payload = {
              enabled: enabledInput.checked,
              daily_start_time: timeInput.value || '09:00',
              daily_chapter_quota: Number(quotaInput.value || 1),
              auto_publish: autoPublishInput.checked,
              publish: {
                platform: platformSelect.value || '',
                book_name: bookNameInput.value.trim(),
                upload_url: uploadUrlInput.value.trim(),
                create_if_missing: createIfMissingInput.checked,
                book_meta: {
                  audience: audienceInput.value.trim(),
                  primary_category: primaryCategoryInput.value.trim(),
                  protagonist_names: protagonistNamesInput.value
                    .split(',')
                    .map((name) => name.trim())
                    .filter(Boolean),
                  intro: introInput.value.trim(),
                },
              },
            };
            const result = await requestJson(`/api/projects/${item.project_id}/automation`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload),
            });
            setGlobalStatus(result.message || '自动化设置已保存。', '书本自动化');
            await loadBooks();
            if (currentDrawerTask?.project_id === item.project_id) {
              await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
            }
          } catch (error) {
            setGlobalStatus(error.message || String(error), '自动化设置保存失败');
          }
        }, 'primary'));
        body.appendChild(automationCard);

        const section = createNode('section', '', 'detail-card');
        section.appendChild(createNode('div', '项目章节', 'task-id'));
        const chapterList = createNode('div', '', 'drawer-grid');
        const visibleChapters = chapters.filter((chapter) => chapter.status !== 'planned');
        if (!visibleChapters.length) {
          chapterList.appendChild(createNode('div', '项目已创建，但还没有可展示的已生成章节。', 'empty'));
        } else {
          visibleChapters.forEach((chapter) => {
            const row = createNode('div', '', 'chapter-row');
            const hasDraft = Boolean(chapter.has_draft);
            const hasReview = Boolean(chapter.has_review);
            row.appendChild(createNode('strong', `第${chapter.chapter_number}章《${chapter.title}》`));
            row.appendChild(createNode('div', `状态：${chapterStatusLabel(chapter.status)} | 字数：${chapter.char_count || 0}`, 'meta-line'));
            if (chapter.summary) row.appendChild(createNode('div', chapter.summary, 'meta-line'));
            const actions = createNode('div', '', 'action-row');
            const bodyId = `chapter_body_${item.project_id}_${chapter.chapter_number}`;
            if (hasDraft) {
              actions.appendChild(createButton('查看正文', () => toggleChapterBody(item.project_id, chapter.chapter_number, bodyId), 'ghost'));
              actions.appendChild(createButton('发布到平台', async () => {
                try {
                  const chapterDetail = await requestJson(`/api/projects/${item.project_id}/chapters/${chapter.chapter_number}`);
                  openTaskModal('upload', {
                    project_id: item.project_id,
                    platform: project.automation?.publish?.platform || '',
                    book_name: project.automation?.publish?.book_name || project.title || item.title || '',
                    chapter_title: chapter.title,
                    body: chapterDetail.body || '',
                    upload_url: project.automation?.publish?.upload_url || '',
                    create_if_missing: Boolean(project.automation?.publish?.create_if_missing),
                  });
                } catch (error) {
                  setGlobalStatus(error.message || String(error), '章节读取失败');
                }
              }, 'secondary'));
            }
            if (chapter.status === 'needs_review' && hasReview) {
              actions.appendChild(createButton('查看 Review', () => showReview(item.project_id, chapter.chapter_number), 'ghost'));
              actions.appendChild(createButton('Review 决策链', () => jumpToReviewDecisionChain(item.project_id, chapter.chapter_number), 'ghost'));
              actions.appendChild(createButton('接受', () => approveReview(item.project_id, chapter.chapter_number, false), 'ghost'));
              actions.appendChild(createButton('接受并继续', () => approveReview(item.project_id, chapter.chapter_number, true), 'primary'));
            }
            if (!actions.childNodes.length) {
              const reason = chapter.status === 'needs_review'
                ? '该章当前停在待处理状态，但还没有可查看的 draft / review。'
                : '该章目前只有计划信息，还没有可查看正文。';
              row.appendChild(createNode('div', reason, 'meta-line'));
            } else {
              row.appendChild(actions);
            }
            const bodyBlock = createNode('div', '', 'chapter-body');
            bodyBlock.id = bodyId;
            row.appendChild(bodyBlock);
            chapterList.appendChild(row);
          });
        }
        section.appendChild(chapterList);
        body.appendChild(section);
        body.appendChild(renderDecisionTimeline(project));
      } catch (error) {
        body.appendChild(createNode('div', error.message || String(error), 'detail-card'));
      }
    }

    function renderUploadDrawer(item) {
      const body = document.getElementById('drawer_body');
      const top = createNode('section', '', 'detail-card');
      const badges = createNode('div', '', 'badge-row');
      badges.appendChild(createNode('span', item.status, `badge ${badgeKindByStatus(item.status)}`));
      if (item.display_name) badges.appendChild(createNode('span', item.display_name, 'badge'));
      if (item.project_id) badges.appendChild(createNode('span', `Book ${item.project_id}`, 'badge'));
      top.appendChild(badges);
      const lines = [
        item.subtitle ? `章节：${item.subtitle}` : '',
        item.extension_client_id ? `执行端：${item.extension_client_id}` : '执行端：待领取',
        item.current_url ? `当前页面：${item.current_url}` : '',
        item.created_at ? `创建：${item.created_at}` : '',
        item.updated_at ? `更新：${item.updated_at}` : '',
        item.started_at ? `开始：${item.started_at}` : '',
        item.finished_at ? `结束：${item.finished_at}` : '',
        item.message ? `消息：${item.message}` : '',
        item.error ? `错误：${item.error}` : '',
      ].filter(Boolean).join('\\n');
      top.appendChild(createNode('div', lines, 'meta-line'));
      body.appendChild(top);

      const timelineCard = createNode('section', '', 'detail-card');
      timelineCard.appendChild(createNode('div', '时间线', 'task-id'));
      const timeline = createNode('div', '', 'drawer-grid');
      const phase = item.result_payload?.phase ? `phase=${item.result_payload.phase}` : '';
      [
        item.created_at ? `创建任务 | ${item.created_at}` : '',
        item.claimed_at ? `扩展领取 | ${item.claimed_at}` : '',
        item.started_at ? `开始执行 | ${item.started_at}` : '',
        phase ? `当前阶段 | ${phase}` : '',
        item.abort_requested ? '终止请求 | 已发出' : '',
        item.finished_at ? `结束执行 | ${item.finished_at}` : '',
        item.status ? `最终状态 | ${item.status}` : '',
      ].filter(Boolean).forEach((line) => {
        timeline.appendChild(createNode('div', line, 'meta-line'));
      });
      timelineCard.appendChild(timeline);
      body.appendChild(timelineCard);

      const payloadCard = createNode('section', '', 'detail-card');
      payloadCard.appendChild(createNode('div', '任务参数摘要', 'task-id'));
      payloadCard.appendChild(createNode('div', `平台：${item.platform || ''}\n作品：${item.title || ''}\n发布：${item.publish ? '是' : '否'}\n终止请求：${item.abort_requested ? '已发出' : '否'}`, 'meta-line'));
      const code = createNode('div', JSON.stringify(item.result_payload || {}, null, 2), 'code');
      payloadCard.appendChild(code);
      body.appendChild(payloadCard);
    }

    async function loadDrawerSnapshot(taskKind, taskId) {
      const item = await requestJson(`/api/task-center/items/${taskKind}/${taskId}`);
      if (item.task_kind !== 'generation' || !item.project_id) {
        return { item };
      }
      const project = await loadProjectDetail(item.project_id);
      const chapters = Array.isArray(project.chapters) ? project.chapters : await loadProjectChapters(item.project_id);
      return { item, project, chapters };
    }

    function captureDrawerBodyState() {
      const body = document.getElementById('drawer_body');
      const openChapterBodies = [];
      body.querySelectorAll('.chapter-body.open').forEach((node) => {
        openChapterBodies.push({
          id: node.id,
          text: node.textContent || '',
          loaded: node.dataset.loaded || '',
        });
      });
      return {
        scrollTop: body.scrollTop,
        openChapterBodies,
      };
    }

    function restoreDrawerBodyState(snapshot) {
      if (!snapshot) return;
      const body = document.getElementById('drawer_body');
      body.scrollTop = snapshot.scrollTop || 0;
      (snapshot.openChapterBodies || []).forEach((chapterBody) => {
        const node = document.getElementById(chapterBody.id);
        if (!node) return;
        node.textContent = chapterBody.text || '';
        if (chapterBody.loaded) node.dataset.loaded = chapterBody.loaded;
        node.classList.add('open');
      });
    }

    async function renderDrawerSnapshot(snapshot, { preserveBodyState = false, requestToken = drawerRequestToken } = {}) {
      if (requestToken !== drawerRequestToken) return;
      const overlay = document.getElementById('task_drawer_overlay');
      const body = document.getElementById('drawer_body');
      const bodyState = preserveBodyState ? captureDrawerBodyState() : null;
      const item = snapshot.item;
      currentDrawerTask = item;
      currentDrawerSignature = dataSignature(snapshot);
      document.getElementById('drawer_task_id').textContent = `${serializeTaskType(item.task_kind)} · ${item.task_id}`;
      document.getElementById('drawer_title').textContent = item.title || '未命名任务';
      document.getElementById('drawer_meta').textContent = [
        item.subtitle || '',
        item.project_id ? `书本 ${item.project_id}` : '',
        item.current_stage ? `阶段：${stageLabel(item.current_stage)}` : '',
      ].filter(Boolean).join(' | ');
      clearNode(body);
      overlay.classList.add('open');
      if (item.task_kind === 'generation') {
        await renderGenerationDrawer(item, snapshot.project || null, snapshot.chapters || null);
      } else {
        renderUploadDrawer(item);
      }
      if (requestToken !== drawerRequestToken) return;
      restoreDrawerBodyState(bodyState);
    }

    async function openTaskDrawer(taskKind, taskId) {
      const requestToken = ++drawerRequestToken;
      try {
        const snapshot = await loadDrawerSnapshot(taskKind, taskId);
        if (requestToken !== drawerRequestToken) return;
        await renderDrawerSnapshot(snapshot, {
          preserveBodyState: currentDrawerTask?.task_kind === taskKind && currentDrawerTask?.task_id === taskId,
          requestToken,
        });
      } catch (error) {
        if (requestToken !== drawerRequestToken) return;
        if (String(error?.message || '').includes('404')) {
          closeTaskDrawer();
          setGlobalStatus('任务详情不存在，已关闭右侧详情。', '任务详情');
          return;
        }
        setGlobalStatus(error.message || String(error), '任务详情读取失败');
      }
    }

    async function refreshCurrentDrawerIfChanged() {
      if (!currentDrawerTask) return;
      const requestToken = drawerRequestToken;
      try {
        const snapshot = await loadDrawerSnapshot(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        if (requestToken !== drawerRequestToken || !currentDrawerTask) return;
        const nextSignature = dataSignature(snapshot);
        if (nextSignature === currentDrawerSignature) return;
        await renderDrawerSnapshot(snapshot, { preserveBodyState: true, requestToken });
      } catch (error) {
        if (requestToken !== drawerRequestToken) return;
        if (String(error?.message || '').includes('404')) {
          closeTaskDrawer();
          setGlobalStatus('当前任务已不存在，已关闭右侧详情。', '任务详情');
          return;
        }
        setGlobalStatus(error.message || String(error), '任务详情刷新失败');
      }
    }

    function closeTaskDrawer(event) {
      if (event && event.target && event.target !== event.currentTarget) return;
      drawerRequestToken += 1;
      currentDrawerTask = null;
      currentDrawerSignature = '';
      document.getElementById('task_drawer_overlay').classList.remove('open');
    }

    async function openExtensionOptions() {
      try {
        await bridgeRequest('open-options', {}, 1500);
      } catch (error) {
        setGlobalStatus(`${error.message || String(error)}\n若尚未安装扩展，请解压并加载：${EXTENSION_INSTALL_PATH}`, '浏览器扩展');
      }
    }

    window.addEventListener('message', async (event) => {
      if (event.source !== window) return;
      if (event.origin !== window.location.origin) return;
      const data = event.data;
      if (!data || typeof data !== 'object') return;
      if (data.channel !== EXTENSION_BRIDGE_CHANNEL || data.direction !== 'extension-to-page') return;
      if (data.kind === 'response' && data.correlationId) {
        const pending = pendingBridgeRequests.get(data.correlationId);
        if (!pending) return;
        window.clearTimeout(pending.timer);
        pendingBridgeRequests.delete(data.correlationId);
        if (data.ok) pending.resolve(data.payload);
        else pending.reject(new Error(data.error || '浏览器扩展返回失败。'));
        return;
      }
      if (data.kind !== 'event') return;
      if (data.event === 'login-status' && data.payload?.platform) {
        setGlobalStatus(data.payload.message || '登录状态有更新。', '平台登录');
        await loadPlatforms();
      }
      if (data.event === 'upload-status' && data.payload?.jobId) {
        setGlobalStatus(data.payload.message || '上传状态有更新。', '上传任务');
        await loadTaskCenter();
        if (currentDrawerTask?.task_kind === 'upload' && currentDrawerTask?.task_id === data.payload.jobId) {
          await openTaskDrawer('upload', data.payload.jobId);
        }
      }
    });

    async function bootstrap() {
      document.getElementById('task_generation_operation_mode').value = @@OPERATION_MODE_JSON@@;
      document.getElementById('task_generation_freeze_failed_candidates').checked = @@FREEZE_FAILED_JSON@@;
      document.getElementById('task_generation_min_chapter_chars').value = @@MIN_CHAPTER_CHARS_JSON@@;
      document.getElementById('config_generation_operation_mode').value = @@OPERATION_MODE_JSON@@;
      document.getElementById('config_generation_freeze_failed_candidates').checked = @@FREEZE_FAILED_JSON@@;
      document.getElementById('config_generation_min_chapter_chars').value = @@MIN_CHAPTER_CHARS_JSON@@;
      document.getElementById('config_generation_review_interval_chapters').value = @@REVIEW_INTERVAL_CHAPTERS_JSON@@;
      await loadSettings();
      await loadPlatforms();
      await loadBooks();
      await loadTaskCenter();
      setGlobalStatus('首页已加载。先看书本，再按需要进入任务中心。');
      window.setInterval(async () => {
        await loadPlatforms();
      }, 5000);
      window.setInterval(async () => {
        if (!taskPollHasActive && !currentDrawerTask) return;
        await loadTaskCenter();
        await loadBooks();
        if (currentDrawerTask) {
          await refreshCurrentDrawerIfChanged();
        }
      }, 2500);
    }

    bootstrap();
  </script>
</body>
</html>
    """
    return (
        html
        .replace("@@HAS_API_KEY_TEXT@@", "已保存" if has_api_key else "未保存")
        .replace("@@EXTENSION_BADGE_CLASS@@", "ok" if extension_api_key_configured else "warn")
        .replace("@@EXTENSION_READY_TEXT@@", "已配置" if extension_api_key_configured else "未配置")
        .replace("@@BASE_URL_JSON@@", json.dumps(base_url, ensure_ascii=False))
        .replace("@@MODEL_JSON@@", json.dumps(model, ensure_ascii=False))
        .replace("@@MODEL_PROVIDER_PRESETS_JSON@@", json.dumps(LLM_PROVIDER_PRESETS, ensure_ascii=False))
        .replace("@@OPERATION_MODE_JSON@@", json.dumps(operation_mode, ensure_ascii=False))
        .replace("@@FREEZE_FAILED_JSON@@", json.dumps(bool(freeze_failed_candidates)))
        .replace("@@MIN_CHAPTER_CHARS@@", str(max(500, int(min_chapter_chars))))
        .replace("@@MIN_CHAPTER_CHARS_JSON@@", json.dumps(max(500, int(min_chapter_chars))))
        .replace("@@REVIEW_INTERVAL_CHAPTERS@@", str(max(0, int(review_interval_chapters))))
        .replace("@@REVIEW_INTERVAL_CHAPTERS_JSON@@", json.dumps(max(0, int(review_interval_chapters))))
        .replace("@@DEFAULT_GENRE@@", default_genre)
        .replace("@@DEFAULT_GENRE_JSON@@", json.dumps(default_genre, ensure_ascii=False))
        .replace("@@DEFAULT_CHAPTERS@@", str(int(default_chapters)))
        .replace("@@DEFAULT_CHAPTERS_JSON@@", json.dumps(int(default_chapters)))
        .replace("@@EXTENSION_READY@@", json.dumps(bool(extension_api_key_configured)))
        .replace("@@EXTENSION_INSTALL_PATH@@", json.dumps(extension_install_path, ensure_ascii=False))
        .replace("@@PAGE_DOM_HELPERS_JS@@", PAGE_DOM_HELPERS_JS)
    )


def render_home_page(
    *,
    has_api_key: bool,
    base_url: str,
    model: str,
    operation_mode: str,
    freeze_failed_candidates: bool,
    min_chapter_chars: int = 2500,
    review_interval_chapters: int = 0,
    default_genre: str = "玄幻",
    default_chapters: int = 3,
    extension_api_key_configured: bool = False,
    extension_install_path: str = "browser_extension/forwin-publisher",
) -> str:
    return _render_home_page_v2(
        has_api_key=has_api_key,
        base_url=base_url,
        model=model,
        operation_mode=operation_mode,
        freeze_failed_candidates=freeze_failed_candidates,
        min_chapter_chars=min_chapter_chars,
        review_interval_chapters=review_interval_chapters,
        default_genre=default_genre,
        default_chapters=default_chapters,
        extension_api_key_configured=extension_api_key_configured,
        extension_install_path=extension_install_path,
    )

def render_publishers_page(
    *,
    backend_ready: dict[str, object],
    extension_install_path: str,
) -> str:
    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>平台发布管理</title>
  <style>
    :root {{
      --bg:#f2eadf;
      --card:#fffaf3;
      --card-strong:#fffdf9;
      --ink:#17211d;
      --accent:#b64b2a;
      --accent-2:#284d46;
      --line:#ddceb7;
      --muted:#725f49;
      --ok:#226b37;
      --warn:#a25920;
      --ui:"Inter","SF Pro Display","Segoe UI","PingFang SC","Noto Sans SC",sans-serif;
      --serif:"Iowan Old Style","Palatino Linotype","Noto Serif SC","Source Han Serif SC",serif;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0;
      font-family:var(--serif);
      background:
        radial-gradient(circle at top left,#fff6df 0,transparent 32%),
        radial-gradient(circle at 92% 8%, rgba(182,75,42,.12), transparent 18%),
        linear-gradient(135deg,#f4efe6,#efe2cf);
      color:var(--ink);
      min-height:100vh;
    }}
    body::before {{
      content:"";
      position:fixed;
      inset:0;
      background:repeating-linear-gradient(90deg, rgba(120,96,64,.04) 0, rgba(120,96,64,.04) 1px, transparent 1px, transparent 108px);
      pointer-events:none;
    }}
    .wrap {{ position:relative; max-width:1220px; margin:0 auto; padding:32px 24px 80px; }}
    .masthead {{
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:16px;
      padding-bottom:18px;
      margin-bottom:22px;
      border-bottom:1px solid rgba(114,95,73,.16);
    }}
    .masthead-note {{
      font:500 13px/1.6 var(--ui);
      color:var(--muted);
      max-width:44ch;
    }}
    .hero {{
      display:grid;
      grid-template-columns:minmax(0, 1.08fr) minmax(320px, .92fr);
      gap:22px;
      align-items:stretch;
    }}
    .hero-card, .card {{
      background:linear-gradient(180deg, rgba(255,253,249,.95), rgba(248,241,231,.93));
      border:1px solid rgba(205,184,155,.62);
      border-radius:28px;
      padding:26px;
      box-shadow:0 20px 56px rgba(73,52,28,.10);
      backdrop-filter:blur(8px);
    }}
    .hero-card {{
      min-height:280px;
      display:flex;
      flex-direction:column;
      justify-content:space-between;
      background:
        linear-gradient(180deg, rgba(255,255,255,.34), rgba(255,255,255,0)),
        radial-gradient(circle at bottom right, rgba(182,75,42,.12), transparent 28%),
        linear-gradient(135deg, rgba(40,77,70,.05), transparent 55%);
    }}
    .eyebrow {{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:8px 14px;
      border-radius:999px;
      border:1px solid rgba(182,75,42,.18);
      color:var(--accent);
      background:rgba(182,75,42,.06);
      font:600 12px/1 var(--ui);
      text-transform:uppercase;
      letter-spacing:.14em;
    }}
    h1 {{ margin:18px 0 10px; font-size:52px; line-height:.98; letter-spacing:-.03em; max-width:10ch; }}
    h2 {{ margin:0 0 8px; font-size:28px; line-height:1.08; }}
    p {{ line-height:1.75; color:var(--muted); }}
    .hero-copy {{ max-width:56ch; }}
    .hero-actions, .actions {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }}
    .hero-actions {{ margin-top:20px; }}
    .layout-grid {{ display:grid; grid-template-columns:minmax(0, 1fr) minmax(340px, .96fr); gap:22px; margin-top:24px; }}
    .stack {{ display:grid; gap:22px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:18px; margin-top:20px; }}
    button, a.button {{
      border:1px solid transparent;
      background:linear-gradient(135deg, var(--accent), #ce6a41);
      color:#fff;
      border-radius:999px;
      padding:11px 16px;
      cursor:pointer;
      font:600 14px/1.1 var(--ui);
      text-decoration:none;
      transition:transform .18s ease, box-shadow .18s ease;
      box-shadow:0 12px 24px rgba(182,75,42,.18);
    }}
    button:hover, a.button:hover {{ transform:translateY(-1px); }}
    button.secondary, a.button.secondary {{
      background:rgba(40,77,70,.06);
      color:var(--accent-2);
      border-color:rgba(40,77,70,.14);
      box-shadow:none;
    }}
    .muted {{ color:var(--muted); font-family:var(--ui); }}
    label {{
      display:block;
      margin-bottom:6px;
      font:600 12px/1.2 var(--ui);
      color:var(--muted);
      text-transform:uppercase;
      letter-spacing:.08em;
    }}
    input, textarea, select {{
      width:100%;
      margin-top:8px;
      margin-bottom:12px;
      border:1px solid rgba(205,184,155,.78);
      border-radius:16px;
      padding:12px 14px;
      background:rgba(255,253,248,.98);
      font:500 14px/1.45 var(--ui);
      box-sizing:border-box;
      color:var(--ink);
    }}
    input:focus, textarea:focus, select:focus {{
      outline:none;
      border-color:rgba(182,75,42,.42);
      box-shadow:0 0 0 4px rgba(182,75,42,.08);
    }}
    textarea {{ min-height:180px; resize:vertical; }}
    .status {{
      margin-top:10px;
      padding:14px 16px;
      border-radius:18px;
      border:1px solid rgba(205,184,155,.72);
      background:rgba(255,255,255,.58);
      font:500 14px/1.6 var(--ui);
      color:var(--muted);
      white-space:pre-wrap;
    }}
    .ok {{ color:var(--ok); }}
    .warn {{ color:var(--warn); }}
    .summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; margin-top:18px; }}
    .task-list {{ display:grid; gap:12px; margin-top:16px; }}
    .task-item {{
      border:1px solid rgba(205,184,155,.72);
      border-radius:20px;
      padding:18px;
      background:linear-gradient(180deg, rgba(255,255,255,.62), rgba(252,247,240,.56));
    }}
    .task-item strong {{ display:block; margin-bottom:8px; font-size:18px; line-height:1.2; }}
    .task-item a {{ color:var(--accent); }}
    code {{ font-family:"SFMono-Regular","Consolas",monospace; font-size:13px; }}
    .card-title {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; }}
    .section-note {{ font:500 13px/1.6 var(--ui); color:var(--muted); }}
    @media (max-width: 980px) {{
      .masthead, .hero, .layout-grid {{ grid-template-columns:1fr; display:grid; }}
      .masthead {{ gap:12px; }}
      h1 {{ font-size:42px; max-width:11ch; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="masthead">
      <div>
        <div class="eyebrow">Publisher Control</div>
        <h1>平台发布管理</h1>
      </div>
      <div class="masthead-note">这一页只负责扩展安装、平台登录和上传排障。日常的项目生成与一键发布已经回到首页控制台。</div>
    </header>

    <section class="hero">
      <article class="hero-card">
        <div class="hero-copy">
          <h2>让浏览器扩展接管真实平台操作</h2>
          <p>ForWin 把登录、上传和后续评论采集都交给浏览器扩展执行。后端只保存任务、接收心跳并持久化结果，不再在服务器里偷偷跑浏览器。</p>
        </div>
        <div class="hero-actions">
          <a class="button secondary" href="/">返回首页控制台</a>
          <button class="secondary" onclick="openExtensionOptions()">打开扩展设置</button>
          <a class="button secondary" href="/api/publishers/extension-package">下载扩展包</a>
        </div>
      </article>
      <aside class="card">
        <div class="card-title">
          <div>
            <h2 style="margin-top:0;">安装提示</h2>
            <div class="section-note">如果你通过 Mac 或笔记本访问这台 Linux 后端，请在本机浏览器安装扩展。</div>
          </div>
        </div>
        <p class="muted">不要直接把 Linux 服务器路径当成本机路径使用。先下载 zip，再用浏览器开发者模式加载解压目录。</p>
        <p><code>{extension_install_path}</code></p>
        <p class="muted">扩展里要填当前 ForWin 后端 URL 和共享 API Key。首次安装时，请先在浏览器扩展管理页手动打开扩展选项；页面里的“打开扩展设置”按钮要等扩展已经完成首次配置后才会生效。</p>
      </aside>
    </section>

    <section class="layout-grid">
      <div class="stack">
        <div class="card">
          <div class="card-title">
            <div>
              <h2 style="margin-top:0;">扩展状态</h2>
              <div class="section-note">确认桥接是否在线、后端地址是否一致。</div>
            </div>
          </div>
          <div id="extension_summary" class="status">正在检测浏览器扩展...</div>
        </div>
        <div id="platforms" class="grid"></div>
      </div>

      <div class="stack">
        <div class="card">
          <div class="card-title">
            <div>
              <h2 style="margin-top:0;">上传任务</h2>
              <div class="section-note">这里仍然保留手动上传入口，适合直接验证平台链路。</div>
            </div>
          </div>
      <label>平台</label>
      <select id="platform"></select>
      <label>作品名</label>
      <input id="book_name" placeholder="输入平台里的作品名">
      <div class="muted" style="margin-top:-4px;">如果平台里还没有这本书，可以勾选下面的“找不到作品时自动创建新书”。</div>
      <label style="display:flex;align-items:center;gap:10px;margin:8px 0 0;">
        <input id="create_if_missing" type="checkbox" style="width:auto;">
        找不到作品时自动创建新书
      </label>
      <div class="grid" style="margin-top:8px;">
        <div>
          <label>目标读者</label>
          <select id="book_audience">
            <option value="male">男频</option>
            <option value="female">女频</option>
          </select>
        </div>
        <div>
          <label>主分类</label>
          <input id="book_primary_category" placeholder="例如：都市日常 / 东方仙侠">
        </div>
      </div>
      <div class="grid" style="margin-top:8px;">
        <div>
          <label>主角名 1</label>
          <input id="book_protagonist_1" placeholder="主角名">
        </div>
        <div>
          <label>主角名 2</label>
          <input id="book_protagonist_2" placeholder="可选">
        </div>
      </div>
      <label style="margin-top:8px;">作品简介</label>
      <textarea id="book_intro" placeholder="50-500 字。创建新书时会用到。"></textarea>
      <label>章节标题</label>
      <input id="chapter_title" placeholder="例如：第一百二十三章 风雪夜归人">
      <label>正文</label>
      <textarea id="body" placeholder="粘贴章节正文"></textarea>
      <label>可选上传页 URL</label>
      <input id="upload_url" placeholder="如果你知道某个平台的具体章节编辑页，可以填这里">
      <div class="actions">
        <button onclick="upload(true)">直接发布</button>
        <button class="secondary" onclick="upload(false)">保存草稿</button>
      </div>
      <div id="upload_status" class="status"></div>
        </div>
        <div class="card">
          <div class="card-title">
            <div>
              <h2 style="margin-top:0;">最近上传任务</h2>
              <div class="section-note">这里会自动刷新排队和执行中的任务。</div>
            </div>
          </div>
          <div id="upload_jobs_status" class="status">正在加载任务列表...</div>
          <div id="upload_jobs_list" class="task-list"></div>
        </div>
      </div>
    </section>
  </div>
  <script>
    const EXTENSION_BRIDGE_CHANNEL = 'forwin-publisher-extension';
    const BACKEND_EXTENSION_KEY_READY = {json.dumps(bool(backend_ready.get("extension_api_key_configured")))};
    const EXTENSION_INSTALL_PATH = {json.dumps(extension_install_path, ensure_ascii=False)};
    const pendingBridgeRequests = new Map();
    let uploadPollTimer = null;
    let uploadJobsPollTimer = null;
    let selectedPlatformId = '';

    function bridgeId() {{
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {{
        return window.crypto.randomUUID();
      }}
      return `forwin-${{Date.now()}}-${{Math.random().toString(16).slice(2)}}`;
    }}

{PAGE_DOM_HELPERS_JS}

    function normalizeOrigin(value) {{
      try {{
        return new URL(value).origin;
      }} catch (_error) {{
        return '';
      }}
    }}

    function showManualExtensionSetup(extraMessage = '') {{
      const currentOrigin = window.location.origin;
      const lines = [
        '首次安装请手动完成这几步：',
        '1. 点击页面里的“下载扩展包”，把 zip 下载到你当前这台浏览器所在的电脑。',
        '2. 解压 zip，得到 forwin-publisher 文件夹。',
        '3. 打开 chrome://extensions 或 edge://extensions。',
        '4. 开启“开发者模式”，点击“加载已解压的扩展程序”，选择解压后的 forwin-publisher 文件夹。',
        '5. 打开扩展卡片的“详情”，进入“扩展程序选项”。',
        `6. 在扩展里把 ForWin Backend URL 填成：${{currentOrigin}}`,
        '7. Extension API Key 填你服务器 .env 里的 FORWIN_PUBLISHER_EXTENSION_API_KEY。',
        '8. 保存后刷新当前 /publishers 页面。',
      ];
      if (extraMessage) {{
        lines.push('', `补充信息：${{extraMessage}}`);
      }}
      window.alert(lines.join('\\n'));
    }}

    function setPlatformStatus(platform, text, kind = 'warn') {{
      const el = document.getElementById(`status_${{platform}}`);
      if (!el) return;
      el.textContent = text;
      el.className = `status ${{kind}}`;
    }}

    function renderExtensionSummary(details = null, error = '') {{
      const el = document.getElementById('extension_summary');
      if (!BACKEND_EXTENSION_KEY_READY) {{
        el.textContent = '后端还没有配置扩展 API Key。请先设置 FORWIN_PUBLISHER_EXTENSION_API_KEY，再让扩展连接这个实例。';
        el.className = 'status warn';
        return;
      }}
      if (!details) {{
        el.textContent = `未检测到浏览器扩展。请确认你已经用开发者模式加载：\\n${{EXTENSION_INSTALL_PATH}}\\n并且在扩展设置里把当前 ForWin 地址加入桥接。${{error ? `\\n${{error}}` : ''}}`;
        el.className = 'status warn';
        return;
      }}
      const configuredOrigin = normalizeOrigin(details.backendBaseUrl);
      const currentOrigin = window.location.origin;
      const sameBackend = configuredOrigin === currentOrigin;
      el.textContent = [
        '浏览器扩展已连接',
        `客户端：${{details.browserName || 'unknown'}} / ${{details.extensionVersion || 'dev'}}`,
        `扩展配置的后端：${{details.backendBaseUrl || '未配置'}}`,
        `当前页面后端：${{currentOrigin}}`,
        sameBackend ? '后端地址匹配，可以直接登录和上传。' : '扩展里的后端地址和当前页面不一致，请先去扩展设置里改成当前地址。',
      ].join('\\n');
      el.className = `status ${{sameBackend ? 'ok' : 'warn'}}`;
    }}

    function bridgeRequest(action, payload = {{}}, timeoutMs = 1800) {{
      return new Promise((resolve, reject) => {{
        const correlationId = bridgeId();
        const timer = window.setTimeout(() => {{
          pendingBridgeRequests.delete(correlationId);
          reject(new Error('浏览器扩展未响应。'));
        }}, timeoutMs);
        pendingBridgeRequests.set(correlationId, {{ resolve, reject, timer }});
        window.postMessage(
          {{
            channel: EXTENSION_BRIDGE_CHANNEL,
            direction: 'page-to-extension',
            kind: 'request',
            correlationId,
            action,
            payload,
          }},
          window.location.origin,
        );
      }});
    }}

    async function pingExtension() {{
      try {{
        const payload = await bridgeRequest('ping');
        renderExtensionSummary(payload);
        return payload;
      }} catch (error) {{
        renderExtensionSummary(null, error.message || String(error));
        return null;
      }}
    }}

    async function loadPlatforms() {{
      const res = await fetch('/api/publishers/platforms');
      const data = await res.json();
      const grid = document.getElementById('platforms');
      const select = document.getElementById('platform');
      const nextSelectedPlatformId = selectedPlatformId || select.value || '';
      clearNode(grid);
      clearNode(select);
      data.forEach((item) => {{
        const option = document.createElement('option');
        option.value = item.platform_id;
        option.textContent = item.display_name;
        if (item.platform_id === nextSelectedPlatformId) {{
          option.selected = true;
        }}
        select.appendChild(option);

        const heartbeat = item.last_heartbeat_at ? `最近心跳：${{item.last_heartbeat_at}}` : '还没有收到扩展心跳';
        const online = item.extension_online ? '扩展在线' : '扩展离线';
        const card = document.createElement('div');
        card.className = 'card';
        card.appendChild(createNode('h2', item.display_name));
        if (item.extension_client_id) {{
          card.appendChild(createNode('p', `当前执行端 Client ID：${{item.extension_client_id}}`, 'muted'));
        }}
        const loginText = document.createElement('p');
        loginText.appendChild(document.createTextNode('登录入口：'));
        const loginLink = document.createElement('a');
        loginLink.href = item.login_url;
        loginLink.target = '_blank';
        loginLink.rel = 'noreferrer';
        loginLink.textContent = item.login_url;
        loginText.appendChild(loginLink);
        card.appendChild(loginText);
        card.appendChild(createNode('p', `支持登录：${{item.supported_login_methods.join(' / ') || 'scan'}}`, 'muted'));
        card.appendChild(createNode('p', `支持动作：${{item.supported_actions.join(' / ')}}`, 'muted'));
        const actions = createNode('div', '', 'actions');
        actions.appendChild(createButton(item.connected ? '重新连接' : '连接平台', () => connectPlatform(item.platform_id)));
        actions.appendChild(createButton('仅打开官网', () => openOfficialSite(item.login_url, item.platform_id), 'secondary'));
        card.appendChild(actions);
        const status = createNode(
          'div',
          `${{item.connected ? '已连接' : '未连接'}} | ${{online}}\\n${{heartbeat}}`,
          `status ${{item.connected ? 'ok' : 'warn'}}`,
        );
        status.id = `status_${{item.platform_id}}`;
        card.appendChild(status);
        if (item.last_error) {{
          card.appendChild(createNode('p', `最近错误：${{item.last_error}}`, 'status warn'));
        }}
        grid.appendChild(card);
      }});
      if (!select.value && data.length) {{
        select.value = data[0].platform_id;
      }}
      selectedPlatformId = select.value || '';
    }}

    function openOfficialSite(url, platform) {{
      window.open(url, '_blank', 'noopener,noreferrer');
      setPlatformStatus(platform, '已在浏览器里打开平台官网，但这一步不会自动通知后端登录成功。请优先用“连接平台”让扩展接管整个登录流程。', 'warn');
    }}

    async function connectPlatform(platform) {{
      if (!BACKEND_EXTENSION_KEY_READY) {{
        setPlatformStatus(platform, '后端未配置扩展 API Key，暂时无法接收扩展心跳和任务回写。', 'warn');
        return;
      }}
      setPlatformStatus(platform, '正在请求扩展打开登录弹窗...', 'warn');
      try {{
        const response = await bridgeRequest('open-login', {{ platform }}, 2500);
        setPlatformStatus(platform, response.message || '登录弹窗已打开，请在弹窗里完成扫码。', 'warn');
      }} catch (error) {{
        setPlatformStatus(platform, error.message || String(error), 'warn');
      }}
    }}

    function stopUploadPolling() {{
      if (uploadPollTimer) {{
        window.clearTimeout(uploadPollTimer);
        uploadPollTimer = null;
      }}
    }}

    function renderUploadJob(data) {{
      const lines = [
        `任务状态：${{data.status}}`,
        `平台：${{data.display_name}}`,
        `作品：${{data.book_name}}`,
        `章节：${{data.chapter_title}}`,
        data.message ? `说明：${{data.message}}` : '',
        data.error ? `错误：${{data.error}}` : '',
        data.current_url ? `当前页面：${{data.current_url}}` : '',
        data.started_at ? `开始时间：${{data.started_at}}` : '',
        data.finished_at ? `结束时间：${{data.finished_at}}` : '',
      ].filter(Boolean);
      const el = document.getElementById('upload_status');
      el.textContent = lines.join('\\n');
      el.className = `status ${{data.status === 'succeeded' ? 'ok' : (data.status === 'failed' ? 'warn' : '')}}`;
    }}

    function stopUploadJobsPolling() {{
      if (uploadJobsPollTimer) {{
        window.clearTimeout(uploadJobsPollTimer);
        uploadJobsPollTimer = null;
      }}
    }}

    function renderUploadJobs(items) {{
      const statusEl = document.getElementById('upload_jobs_status');
      const listEl = document.getElementById('upload_jobs_list');
      clearNode(listEl);
      if (!items.length) {{
        statusEl.textContent = '最近没有上传任务。';
        statusEl.className = 'status';
        return false;
      }}
      const activeCount = items.filter((item) => item.status === 'pending' || item.status === 'running').length;
      statusEl.textContent = activeCount
        ? `最近任务中有 ${{activeCount}} 条仍在执行或排队，列表会自动刷新。`
        : `最近展示 ${{items.length}} 条上传任务。`;
      statusEl.className = `status ${{activeCount ? 'warn' : 'ok'}}`;
      items.forEach((item) => {{
        const node = document.createElement('div');
        node.className = 'task-item';
        node.appendChild(createNode('strong', `${{item.display_name}} | ${{item.status}} | ${{item.book_name}} / ${{item.chapter_title}}`));
        const lines = [
          item.extension_client_id ? `执行端：${{item.extension_client_id}}` : '执行端：等待分配',
          item.created_at ? `创建时间：${{item.created_at}}` : '',
          item.started_at ? `开始时间：${{item.started_at}}` : '',
          item.finished_at ? `结束时间：${{item.finished_at}}` : '',
          item.message ? `说明：${{item.message}}` : '',
          item.error ? `错误：${{item.error}}` : '',
        ].filter(Boolean);
        node.appendChild(createNode('div', lines.join('\\n'), 'status'));
        if (item.current_url) {{
          const linkWrap = document.createElement('p');
          linkWrap.className = 'muted';
          linkWrap.appendChild(document.createTextNode('当前页面：'));
          const link = document.createElement('a');
          link.href = item.current_url;
          link.target = '_blank';
          link.rel = 'noreferrer';
          link.textContent = item.current_url;
          linkWrap.appendChild(link);
          node.appendChild(linkWrap);
        }}
        listEl.appendChild(node);
      }});
      return activeCount > 0;
    }}

    async function loadUploadJobs(immediate = false) {{
      stopUploadJobsPolling();
      const run = async () => {{
        const res = await fetch('/api/publishers/upload-jobs?limit=30');
        const data = await res.json();
        const hasActive = renderUploadJobs(Array.isArray(data) ? data : []);
        uploadJobsPollTimer = window.setTimeout(run, hasActive ? 2000 : 12000);
      }};
      if (immediate) {{
        await run();
      }} else {{
        uploadJobsPollTimer = window.setTimeout(run, 0);
      }}
    }}

    async function pollUploadJob(jobId, immediate = false) {{
      stopUploadPolling();
      const run = async () => {{
        const res = await fetch(`/api/publishers/upload-jobs/${{jobId}}`);
        const data = await res.json();
        renderUploadJob(data);
        await loadUploadJobs(true);
        if (data.status === 'succeeded' || data.status === 'failed') {{
          await loadPlatforms();
          return;
        }}
        uploadPollTimer = window.setTimeout(run, 1500);
      }};
      if (immediate) {{
        await run();
      }} else {{
        uploadPollTimer = window.setTimeout(run, 0);
      }}
    }}

    async function upload(publish) {{
      selectedPlatformId = document.getElementById('platform').value;
      const protagonistNames = [
        document.getElementById('book_protagonist_1').value,
        document.getElementById('book_protagonist_2').value,
      ].map((item) => item.trim()).filter(Boolean);
      const payload = {{
        platform: selectedPlatformId,
        book_name: document.getElementById('book_name').value,
        chapter_title: document.getElementById('chapter_title').value,
        body: document.getElementById('body').value,
        upload_url: document.getElementById('upload_url').value || null,
        publish,
        create_if_missing: document.getElementById('create_if_missing').checked,
        book_meta: {{
          audience: document.getElementById('book_audience').value,
          primary_category: document.getElementById('book_primary_category').value.trim(),
          protagonist_names: protagonistNames,
          intro: document.getElementById('book_intro').value.trim(),
        }},
      }};
      const res = await fetch('/api/publishers/upload-jobs', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('upload_status').textContent = data.detail || '创建上传任务失败';
        document.getElementById('upload_status').className = 'status warn';
        return;
      }}
      renderUploadJob(data);
      document.getElementById('upload_status').textContent += '\\n任务已入队，等待首选 Linux 扩展优先领取。';
      await loadUploadJobs(true);
      await pollUploadJob(data.job_id, true);
    }}

    window.addEventListener('message', (event) => {{
      if (event.source !== window) return;
      if (event.origin !== window.location.origin) return;
      const data = event.data;
      if (!data || typeof data !== 'object') return;
      if (data.channel !== EXTENSION_BRIDGE_CHANNEL) return;
      if (data.direction !== 'extension-to-page') return;
      if (data.kind === 'response' && data.correlationId) {{
        const pending = pendingBridgeRequests.get(data.correlationId);
        if (!pending) return;
        window.clearTimeout(pending.timer);
        pendingBridgeRequests.delete(data.correlationId);
        if (data.ok) {{
          pending.resolve(data.payload);
        }} else {{
          pending.reject(new Error(data.error || '浏览器扩展返回了失败响应。'));
        }}
        return;
      }}
      if (data.kind !== 'event') return;
      if (data.event === 'login-status' && data.payload && data.payload.platform) {{
        setPlatformStatus(
          data.payload.platform,
          data.payload.message || (data.payload.connected ? '登录成功，后端状态正在刷新。' : '登录状态有更新。'),
          data.payload.connected ? 'ok' : 'warn',
        );
        loadPlatforms();
        return;
      }}
      if (data.event === 'upload-status' && data.payload && data.payload.jobId) {{
        pollUploadJob(data.payload.jobId, true);
      }}
    }});

    async function openExtensionOptions() {{
      try {{
        await bridgeRequest('open-options', {{}}, 1500);
      }} catch (error) {{
        renderExtensionSummary(null, error.message || String(error));
        showManualExtensionSetup(error.message || String(error));
      }}
    }}

    async function boot() {{
      await loadPlatforms();
      await loadUploadJobs(true);
      document.getElementById('platform').addEventListener('change', (event) => {{
        selectedPlatformId = event.target.value || '';
      }});
      await pingExtension();
      window.setInterval(loadPlatforms, 5000);
    }}

    boot();
  </script>
</body>
</html>
        """
