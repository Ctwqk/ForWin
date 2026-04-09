from __future__ import annotations

import json


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
"""


def _render_home_page_v2(
    *,
    has_api_key: bool,
    base_url: str,
    model: str,
    operation_mode: str,
    freeze_failed_candidates: bool,
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
      width:min(720px, 100vw);
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
    }
    @media (max-width: 720px) {
      .wrap { padding:18px 14px 56px; }
      .masthead { flex-direction:column; }
      .masthead h1 { font-size:40px; }
      .tabs { padding:14px 14px 0; }
      .tab-panel { padding:14px; }
      .status-bar { padding:14px; }
      .progress-grid { grid-template-columns:minmax(0, 1fr); }
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
        <p>首页收敛成两个主 Tab。配置只保留模型和平台登录；任务页变成统一任务中心，生成与上传混排，详情通过右侧抽屉承接，不再展示项目卡。</p>
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
        <button id="tab_task" class="tab-chip active" type="button" onclick="switchTab('task')">任务</button>
        <button id="tab_config" class="tab-chip" type="button" onclick="switchTab('config')">配置</button>
      </div>

      <section id="panel_task" class="tab-panel active">
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
          <h3>新建任务</h3>
          <p>统一入口。先选任务类型，再填写最少必要字段。</p>
        </div>
        <button class="ghost" type="button" onclick="closeTaskModal()">关闭</button>
      </div>
      <div class="pill-switch">
        <button id="new_task_kind_generation" type="button" class="active" onclick="setTaskModalKind('generation')">生成任务</button>
        <button id="new_task_kind_upload" type="button" onclick="setTaskModalKind('upload')">上传任务</button>
      </div>
      <section id="task_form_generation" class="drawer-grid">
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
        </div>
        <label class="checkbox">
          <input id="task_generation_freeze_failed_candidates" type="checkbox">
          <span>freeze_failed_candidates。黑箱写作出错时，保留冻结产物，方便后续人工接管。</span>
        </label>
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
          <label for="model_form_model">Model</label>
          <input id="model_form_model" spellcheck="false">
        </div>
        <div style="grid-column:1 / -1;">
          <label for="model_form_base_url">Base URL</label>
          <input id="model_form_base_url" spellcheck="false">
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

  <script>
    const EXTENSION_BRIDGE_CHANNEL = 'forwin-publisher-extension';
    const BACKEND_EXTENSION_KEY_READY = @@EXTENSION_READY@@;
    const EXTENSION_INSTALL_PATH = @@EXTENSION_INSTALL_PATH@@;
    const STAGE_ORDER = [
      'queued',
      'planning_arc',
      'creating_project',
      'resolving_arc_envelope',
      'running_provisional_preview',
      'assembling_context',
      'writing_chapter',
      'continuity_review',
      'applying_canon',
      'running_post_acceptance',
      'paused_for_review',
      'completed',
      'failed',
      'terminating',
      'cancelled',
    ];
    const TERMINAL_TASK_STATUSES = new Set(['completed', 'partial_failed', 'failed', 'needs_review', 'cancelled', 'succeeded']);
    const ACTIVE_TASK_STATUSES = new Set(['starting', 'running', 'pending', 'terminating']);
    const pendingBridgeRequests = new Map();
    let settingsState = null;
    let platformsState = [];
    let taskCenterState = [];
    let currentProfileId = '';
    let currentTaskModalKind = 'generation';
    let currentTaskPrefill = {};
    let currentDrawerTask = null;
    let taskPollHasActive = false;

@@PAGE_DOM_HELPERS_JS@@

    function setGlobalStatus(text, title = '系统状态') {
      document.getElementById('global_status_title').textContent = title;
      document.getElementById('global_status').textContent = text;
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
      const taskActive = tab === 'task';
      document.getElementById('tab_task').classList.toggle('active', taskActive);
      document.getElementById('tab_config').classList.toggle('active', !taskActive);
      document.getElementById('panel_task').classList.toggle('active', taskActive);
      document.getElementById('panel_config').classList.toggle('active', !taskActive);
    }

    function badgeKindByStatus(status) {
      if (['completed', 'succeeded', 'cancelled'].includes(status)) return 'ok';
      if (['failed', 'partial_failed'].includes(status)) return 'danger';
      if (['needs_review', 'terminating', 'pending'].includes(status)) return 'warn';
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
        assembling_context: '组装上下文',
        writing_chapter: '写作章节',
        continuity_review: '连续性审查',
        applying_canon: '写入 Canon',
        running_post_acceptance: '后置处理',
        paused_for_review: '等待人工检查',
        completed: '完成',
        failed: '失败',
        terminating: '终止中',
        cancelled: '已取消',
      };
      return map[stage] || stage || '未知阶段';
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

    async function loadSettings() {
      try {
        settingsState = await requestJson('/api/settings/llm');
        document.getElementById('saved_key_badge').textContent = `API Key：${settingsState.has_api_key ? '已保存' : '未保存'}`;
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

    function renderTaskList() {
      const list = document.getElementById('task_list');
      clearNode(list);
      if (!taskCenterState.length) {
        list.appendChild(createNode('div', '还没有任务。点击右上角“新建任务”开始。', 'empty'));
        return;
      }
      taskCenterState.forEach((item) => {
        const node = createNode('article', '', 'task-item');
        node.appendChild(createNode('div', `${serializeTaskType(item.task_kind)} · ${item.task_id}`, 'task-id'));
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
          item.project_id ? `项目：${item.project_id}` : '',
          item.extension_client_id ? `执行端：${item.extension_client_id}` : '',
          item.current_stage ? `阶段：${stageLabel(item.current_stage)}` : '',
          item.message ? `消息：${item.message}` : '',
          item.error ? `错误：${item.error}` : '',
        ].filter(Boolean).join('\n');
        node.appendChild(createNode('div', meta, 'meta-line'));
        const actions = createNode('div', '', 'action-row');
        actions.appendChild(createButton('查看详情', () => openTaskDrawer(item.task_kind, item.task_id), 'secondary'));
        actions.appendChild(createButton('终止', () => terminateTask(item), 'ghost'));
        actions.appendChild(createButton('删除', () => deleteTask(item), 'danger'));
        actions.querySelectorAll('button')[1].disabled = !item.terminable;
        actions.querySelectorAll('button')[2].disabled = !item.deletable;
        node.appendChild(actions);
        list.appendChild(node);
      });
    }

    async function loadTaskCenter(showStatus = false) {
      try {
        taskCenterState = await requestJson('/api/task-center/items?limit=80');
        taskPollHasActive = taskCenterState.some((item) => ACTIVE_TASK_STATUSES.has(item.status));
        renderTaskList();
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

    function setTaskModalKind(kind) {
      currentTaskModalKind = kind;
      document.getElementById('new_task_kind_generation').classList.toggle('active', kind === 'generation');
      document.getElementById('new_task_kind_upload').classList.toggle('active', kind === 'upload');
      document.getElementById('task_form_generation').style.display = kind === 'generation' ? 'grid' : 'none';
      document.getElementById('task_form_upload').style.display = kind === 'upload' ? 'grid' : 'none';
    }

    function applyTaskPrefill() {
      document.getElementById('task_generation_genre').value = currentTaskPrefill.genre || @@DEFAULT_GENRE_JSON@@;
      document.getElementById('task_generation_num_chapters').value = currentTaskPrefill.num_chapters || @@DEFAULT_CHAPTERS_JSON@@;
      document.getElementById('task_generation_premise').value = currentTaskPrefill.premise || '';
      document.getElementById('task_generation_operation_mode').value = currentTaskPrefill.operation_mode || @@OPERATION_MODE_JSON@@;
      document.getElementById('task_generation_freeze_failed_candidates').checked = currentTaskPrefill.freeze_failed_candidates ?? @@FREEZE_FAILED_JSON@@;
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
      applyTaskPrefill();
      document.getElementById('task_modal_shell').classList.add('open');
    }

    function closeTaskModal() {
      document.getElementById('task_modal_shell').classList.remove('open');
      currentTaskPrefill = {};
    }

    async function submitTaskModal() {
      try {
        if (currentTaskModalKind === 'generation') {
          const payload = {
            premise: document.getElementById('task_generation_premise').value.trim(),
            genre: document.getElementById('task_generation_genre').value.trim() || @@DEFAULT_GENRE_JSON@@,
            num_chapters: Number(document.getElementById('task_generation_num_chapters').value || @@DEFAULT_CHAPTERS_JSON@@),
            model_profile_id: document.getElementById('task_generation_model_profile_id').value || null,
            operation_mode: document.getElementById('task_generation_operation_mode').value,
            freeze_failed_candidates: document.getElementById('task_generation_freeze_failed_candidates').checked,
          };
          if (!payload.premise) {
            setGlobalStatus('生成任务必须填写 premise / prompt。', '新建任务');
            return;
          }
          const created = await requestJson('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          closeTaskModal();
          switchTab('task');
          await loadTaskCenter();
          setGlobalStatus(`已创建生成任务 ${created.task_id}。`, '新建任务');
          await openTaskDrawer('generation', created.task_id);
          return;
        }

        const protagonistNames = document.getElementById('task_upload_protagonist_names').value
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean);
        const payload = {
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
        if (currentDrawerTask && currentDrawerTask.task_kind === item.task_kind && currentDrawerTask.task_id === item.task_id) {
          await openTaskDrawer(item.task_kind, item.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务终止失败');
      }
    }

    async function deleteTask(item) {
      if (!window.confirm('删除后任务会从任务中心消失，确定继续吗？')) return;
      const url = item.task_kind === 'upload'
        ? `/api/publishers/upload-jobs/${item.task_id}`
        : `/api/tasks/${item.task_id}`;
      try {
        await requestJson(url, { method: 'DELETE' });
        setGlobalStatus(`任务 ${item.task_id} 已删除。`, '任务操作');
        await loadTaskCenter();
        if (currentDrawerTask && currentDrawerTask.task_kind === item.task_kind && currentDrawerTask.task_id === item.task_id) {
          closeTaskDrawer();
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务删除失败');
      }
    }

    function stageNodeState(stage, item, completedSet, currentIndex) {
      if (item.current_stage === 'failed' && stage === 'failed') return 'failed';
      if (item.current_stage === 'paused_for_review' && stage === 'paused_for_review') return 'paused';
      if (item.current_stage === 'terminating' && stage === 'terminating') return 'current';
      if (item.current_stage === 'cancelled' && stage === 'cancelled') return 'failed';
      if (item.current_stage === 'completed' && stage === 'completed') return 'completed';
      const stageIndex = STAGE_ORDER.indexOf(stage);
      if (completedSet.has(stage) && stageIndex < currentIndex) return 'completed';
      if (stage === item.current_stage) return 'current';
      if (currentIndex !== -1 && stageIndex < currentIndex) return 'completed';
      return 'upcoming';
    }

    function renderStageFlow(item) {
      const wrap = createNode('div', '', 'stage-flow');
      const history = Array.isArray(item.stage_history) ? item.stage_history : [];
      const completedSet = new Set(history.map((entry) => entry.stage).filter(Boolean));
      const currentIndex = STAGE_ORDER.indexOf(item.current_stage);
      STAGE_ORDER.forEach((stage) => {
        const node = createNode('div', '', `stage-node ${stageNodeState(stage, item, completedSet, currentIndex)}`);
        node.appendChild(createNode('div', stageLabel(stage), 'stage-name'));
        const historyEntry = history.find((entry) => entry.stage === stage);
        const notes = [];
        if (historyEntry?.at) notes.push(historyEntry.at);
        if (historyEntry?.chapter) notes.push(`章 ${historyEntry.chapter}`);
        node.appendChild(createNode('div', notes.join(' | ') || '未到达', 'stage-note'));
        wrap.appendChild(node);
      });
      return wrap;
    }

    async function loadProjectChapters(projectId) {
      return requestJson(`/api/projects/${projectId}/chapters`);
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

    async function showReview(projectId, chapterNumber) {
      try {
        const data = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review`);
        const lines = [
          `章节：第${chapterNumber}章《${data.title}》`,
          `状态：${data.status}`,
          `Verdict：${data.verdict}`,
          Array.isArray(data.issues) && data.issues.length
            ? data.issues.map((issue, index) => `${index + 1}. [${issue.severity}] ${issue.description}`).join('\n')
            : '无问题',
        ];
        window.alert(lines.join('\n'));
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Review 读取失败');
      }
    }

    async function approveReview(projectId, chapterNumber, continueGeneration = false) {
      try {
        const data = await requestJson(`/api/projects/${projectId}/chapters/${chapterNumber}/review/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ continue_generation: Boolean(continueGeneration) }),
        });
        setGlobalStatus(data.message || `第${chapterNumber}章已接受。`, 'Review 处理');
        await loadTaskCenter();
        if (data.task_id) {
          await openTaskDrawer('generation', data.task_id);
        } else if (currentDrawerTask?.project_id === projectId) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), 'Review 处理失败');
      }
    }

    async function renderGenerationDrawer(item) {
      const body = document.getElementById('drawer_body');
      const top = createNode('section', '', 'detail-card');
      const badges = createNode('div', '', 'badge-row');
      badges.appendChild(createNode('span', item.status, `badge ${badgeKindByStatus(item.status)}`));
      if (item.project_id) badges.appendChild(createNode('span', `Project ${item.project_id}`, 'badge'));
      top.appendChild(badges);
      top.appendChild(renderStageFlow(item));
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
      if (Array.isArray(item.frozen_artifacts) && item.frozen_artifacts.length) misc.appendChild(createNode('div', `冻结产物：${item.frozen_artifacts.join('\n')}`, 'meta-line'));
      body.appendChild(misc);

      if (!item.project_id) return;
      try {
        const chapters = await loadProjectChapters(item.project_id);
        const section = createNode('section', '', 'detail-card');
        section.appendChild(createNode('div', '项目章节', 'task-id'));
        const chapterList = createNode('div', '', 'drawer-grid');
        const visibleChapters = (Array.isArray(chapters) ? chapters : []).filter((chapter) => chapter.status !== 'planned');
        if (!visibleChapters.length) {
          chapterList.appendChild(createNode('div', '项目已创建，但还没有可展示的已生成章节。', 'empty'));
        } else {
          visibleChapters.forEach((chapter) => {
            const row = createNode('div', '', 'chapter-row');
            row.appendChild(createNode('strong', `第${chapter.chapter_number}章《${chapter.title}》`));
            row.appendChild(createNode('div', `状态：${chapter.status} | 字数：${chapter.char_count || 0}`, 'meta-line'));
            if (chapter.summary) row.appendChild(createNode('div', chapter.summary, 'meta-line'));
            const actions = createNode('div', '', 'action-row');
            const bodyId = `chapter_body_${item.project_id}_${chapter.chapter_number}`;
            actions.appendChild(createButton('查看正文', () => toggleChapterBody(item.project_id, chapter.chapter_number, bodyId), 'ghost'));
            actions.appendChild(createButton('创建上传任务', async () => {
              try {
                const chapterDetail = await requestJson(`/api/projects/${item.project_id}/chapters/${chapter.chapter_number}`);
                openTaskModal('upload', {
                  book_name: item.title || '',
                  chapter_title: chapter.title,
                  body: chapterDetail.body || '',
                });
              } catch (error) {
                setGlobalStatus(error.message || String(error), '章节读取失败');
              }
            }, 'secondary'));
            if (chapter.status === 'needs_review') {
              actions.appendChild(createButton('查看 Review', () => showReview(item.project_id, chapter.chapter_number), 'ghost'));
              actions.appendChild(createButton('接受', () => approveReview(item.project_id, chapter.chapter_number, false), 'ghost'));
              actions.appendChild(createButton('接受并继续', () => approveReview(item.project_id, chapter.chapter_number, true), 'primary'));
            }
            row.appendChild(actions);
            const bodyBlock = createNode('div', '', 'chapter-body');
            bodyBlock.id = bodyId;
            row.appendChild(bodyBlock);
            chapterList.appendChild(row);
          });
        }
        section.appendChild(chapterList);
        body.appendChild(section);
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
      ].filter(Boolean).join('\n');
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

    async function openTaskDrawer(taskKind, taskId) {
      try {
        const item = await requestJson(`/api/task-center/items/${taskKind}/${taskId}`);
        currentDrawerTask = item;
        document.getElementById('drawer_task_id').textContent = `${serializeTaskType(item.task_kind)} · ${item.task_id}`;
        document.getElementById('drawer_title').textContent = item.title || '未命名任务';
        document.getElementById('drawer_meta').textContent = [
          item.subtitle || '',
          item.project_id ? `Project ${item.project_id}` : '',
          item.current_stage ? `阶段：${stageLabel(item.current_stage)}` : '',
        ].filter(Boolean).join(' | ');
        clearNode(document.getElementById('drawer_body'));
        document.getElementById('task_drawer_overlay').classList.add('open');
        if (item.task_kind === 'generation') {
          await renderGenerationDrawer(item);
        } else {
          renderUploadDrawer(item);
        }
      } catch (error) {
        setGlobalStatus(error.message || String(error), '任务详情读取失败');
      }
    }

    function closeTaskDrawer(event) {
      if (event && event.target && event.target !== event.currentTarget) return;
      currentDrawerTask = null;
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
      await loadSettings();
      await loadPlatforms();
      await loadTaskCenter();
      setGlobalStatus('首页已加载。任务详情默认在右侧抽屉里打开。');
      window.setInterval(async () => {
        await loadPlatforms();
      }, 5000);
      window.setInterval(async () => {
        if (!taskPollHasActive && !currentDrawerTask) return;
        await loadTaskCenter();
        if (currentDrawerTask) {
          await openTaskDrawer(currentDrawerTask.task_kind, currentDrawerTask.task_id);
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
        .replace("@@OPERATION_MODE_JSON@@", json.dumps(operation_mode, ensure_ascii=False))
        .replace("@@FREEZE_FAILED_JSON@@", json.dumps(bool(freeze_failed_candidates)))
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
