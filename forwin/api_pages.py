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


def render_home_page(
    *,
    has_api_key: bool,
    base_url: str,
    model: str,
    operation_mode: str,
    freeze_failed_candidates: bool,
    default_genre: str = "玄幻",
    default_chapters: int = 3,
) -> str:
    base_url_json = json.dumps(base_url, ensure_ascii=False)
    model_json = json.dumps(model, ensure_ascii=False)
    operation_mode_json = json.dumps(operation_mode, ensure_ascii=False)
    freeze_failed_json = json.dumps(bool(freeze_failed_candidates))
    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ForWin 创作台</title>
  <style>
    :root {{
      --paper:#efe6d7;
      --panel:#f9f3e9;
      --panel-strong:#fffdf8;
      --ink:#17211d;
      --muted:#6f6352;
      --accent:#ad4a2d;
      --accent-2:#284d46;
      --line:#decfba;
      --line-strong:#cdb89b;
      --shadow:0 24px 70px rgba(43,28,11,.12);
      --warn:#8a5418;
      --ok:#1d6d45;
      --danger:#8e2f2f;
      --ui:"Inter","SF Pro Display","Segoe UI","PingFang SC","Noto Sans SC",sans-serif;
      --serif:"Iowan Old Style","Palatino Linotype","Noto Serif SC","Source Han Serif SC",serif;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0;
      color:var(--ink);
      font-family:var(--serif);
      background:
        radial-gradient(circle at 0% 0%, rgba(255,247,224,.95), transparent 28%),
        radial-gradient(circle at 88% 8%, rgba(173,74,45,.12), transparent 20%),
        linear-gradient(135deg, #f3ebdf 0%, #e9dfcf 55%, #f2ebde 100%);
      min-height:100vh;
      position:relative;
    }}
    body::before {{
      content:"";
      position:fixed;
      inset:0;
      background:
        linear-gradient(rgba(255,255,255,.14), rgba(255,255,255,0)),
        repeating-linear-gradient(
          90deg,
          rgba(125,96,55,.04) 0,
          rgba(125,96,55,.04) 1px,
          transparent 1px,
          transparent 96px
        );
      pointer-events:none;
    }}
    .wrap {{
      position:relative;
      max-width:1320px;
      margin:0 auto;
      padding:28px 24px 80px;
    }}
    .masthead {{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:16px;
      margin-bottom:24px;
      padding-bottom:18px;
      border-bottom:1px solid rgba(111,99,82,.16);
    }}
    .brand-lockup {{
      display:flex;
      flex-direction:column;
      gap:6px;
    }}
    .brand-name {{
      font-size:14px;
      letter-spacing:.22em;
      text-transform:uppercase;
      font-family:var(--ui);
      color:var(--muted);
    }}
    .brand-note {{
      font-size:13px;
      color:var(--muted);
      font-family:var(--ui);
    }}
    .masthead-links {{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
    }}
    .hero {{
      display:grid;
      grid-template-columns:minmax(0, 1.25fr) minmax(360px, .9fr);
      gap:24px;
      align-items:stretch;
    }}
    .panel {{
      position:relative;
      overflow:hidden;
      background:linear-gradient(180deg, rgba(255,253,249,.94), rgba(247,240,228,.92));
      border:1px solid rgba(205,184,155,.58);
      border-radius:28px;
      box-shadow:var(--shadow);
      backdrop-filter:blur(12px);
      transition:transform .24s ease, box-shadow .24s ease, border-color .24s ease;
      animation:riseIn .45s ease both;
    }}
    .panel:hover {{
      transform:translateY(-2px);
      box-shadow:0 28px 76px rgba(43,28,11,.14);
      border-color:rgba(173,74,45,.26);
    }}
    .lead {{
      padding:40px 38px;
      min-height:360px;
      display:flex;
      flex-direction:column;
      justify-content:space-between;
      background:
        linear-gradient(180deg, rgba(255,255,255,.36), rgba(255,255,255,0)),
        radial-gradient(circle at bottom right, rgba(173,74,45,.14), transparent 32%),
        linear-gradient(120deg, rgba(40,77,70,.05), transparent 52%);
    }}
    .lead::after {{
      content:"";
      position:absolute;
      right:-36px;
      bottom:-44px;
      width:220px;
      height:220px;
      border-radius:50%;
      border:1px solid rgba(173,74,45,.12);
      box-shadow:0 0 0 26px rgba(173,74,45,.04), 0 0 0 64px rgba(173,74,45,.025);
    }}
    .eyebrow {{
      display:inline-flex;
      align-items:center;
      gap:8px;
      border:1px solid rgba(173,74,45,.18);
      border-radius:999px;
      padding:8px 14px;
      color:var(--accent);
      font-size:12px;
      letter-spacing:.14em;
      text-transform:uppercase;
      font-family:var(--ui);
      background:rgba(173,74,45,.06);
    }}
    h1 {{
      margin:18px 0 12px;
      font-size:56px;
      line-height:.98;
      letter-spacing:-.03em;
      max-width:10ch;
    }}
    .lead p {{
      margin:0;
      max-width:58ch;
      font-size:16px;
      line-height:1.9;
      color:var(--muted);
    }}
    .lead-footnote {{
      margin-top:18px;
      padding-top:18px;
      border-top:1px solid rgba(111,99,82,.14);
      font-size:13px;
      color:var(--muted);
      font-family:var(--ui);
    }}
    .hero-actions {{
      display:flex;
      gap:12px;
      flex-wrap:wrap;
      margin-top:26px;
    }}
    a.button, button {{
      appearance:none;
      border:1px solid transparent;
      border-radius:999px;
      padding:12px 18px;
      cursor:pointer;
      font:600 14px/1.1 var(--ui);
      text-decoration:none;
      letter-spacing:.01em;
      transition:transform .18s ease, box-shadow .18s ease, opacity .18s ease, border-color .18s ease, background .18s ease;
    }}
    a.button:hover, button:hover {{ transform:translateY(-1px); }}
    .primary {{
      color:#fff;
      background:linear-gradient(135deg, var(--accent), #c8633c);
      box-shadow:0 16px 30px rgba(173,74,45,.22);
    }}
    .secondary {{
      color:var(--accent-2);
      background:rgba(40,77,70,.06);
      border-color:rgba(40,77,70,.14);
    }}
    .form {{
      padding:34px 28px;
      display:grid;
      gap:16px;
      align-content:start;
      background:
        linear-gradient(180deg, rgba(255,255,255,.36), rgba(255,255,255,0)),
        radial-gradient(circle at top right, rgba(40,77,70,.08), transparent 28%);
    }}
    .form h2, .projects-head h2 {{
      margin:0 0 4px;
      font-size:28px;
      line-height:1.08;
    }}
    .hint {{
      color:var(--muted);
      font-size:14px;
      line-height:1.7;
      font-family:var(--ui);
    }}
    .grid {{
      display:grid;
      grid-template-columns:repeat(2, minmax(0, 1fr));
      gap:14px 16px;
    }}
    label {{
      display:block;
      font-size:12px;
      margin-bottom:6px;
      color:var(--muted);
      font-family:var(--ui);
      letter-spacing:.08em;
      text-transform:uppercase;
    }}
    input, textarea, select {{
      width:100%;
      border:1px solid rgba(205,184,155,.82);
      border-radius:18px;
      padding:13px 15px;
      background:rgba(255,253,248,.98);
      color:var(--ink);
      font:500 14px/1.4 var(--ui);
      box-shadow:inset 0 1px 0 rgba(255,255,255,.8);
      transition:border-color .18s ease, box-shadow .18s ease, transform .18s ease;
    }}
    input:focus, textarea:focus, select:focus {{
      outline:none;
      border-color:rgba(173,74,45,.46);
      box-shadow:0 0 0 4px rgba(173,74,45,.08);
    }}
    textarea {{ min-height:180px; resize:vertical; }}
    .stats {{
      display:grid;
      grid-template-columns:repeat(3, minmax(0,1fr));
      gap:12px;
      margin-top:28px;
    }}
    .stat {{
      padding:18px 18px 16px;
      border-radius:22px;
      background:rgba(255,255,255,.5);
      border:1px solid rgba(231,216,196,.8);
      min-height:112px;
    }}
    .stat b {{
      display:block;
      font-size:28px;
      margin-bottom:8px;
      line-height:1;
    }}
    .row {{
      display:flex;
      gap:12px;
      flex-wrap:wrap;
      align-items:center;
    }}
    .status {{
      margin-top:8px;
      padding:15px 16px;
      border-radius:18px;
      background:rgba(255,255,255,.58);
      border:1px solid rgba(205,184,155,.72);
      color:var(--muted);
      min-height:56px;
      line-height:1.6;
      white-space:pre-wrap;
      font-family:var(--ui);
    }}
    .status.ok {{ color:var(--ok); }}
    .status.warn {{ color:var(--warn); }}
    .status.error {{ color:var(--danger); }}
    .section-grid {{
      display:grid;
      grid-template-columns:repeat(2, minmax(0, 1fr));
      gap:22px;
      margin-top:26px;
    }}
    .mini-grid {{
      display:grid;
      grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));
      gap:14px;
    }}
    .projects {{
      margin-top:26px;
      padding:28px;
    }}
    .projects-head {{
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:12px;
      margin-bottom:18px;
    }}
    .project-list {{
      display:grid;
      grid-template-columns:repeat(auto-fit, minmax(320px, 1fr));
      gap:16px;
    }}
    .project-card {{
      padding:22px;
      border-radius:24px;
      border:1px solid rgba(205,184,155,.74);
      background:linear-gradient(180deg, rgba(255,255,255,.62), rgba(252,247,240,.56));
      position:relative;
      overflow:hidden;
    }}
    .project-card::before {{
      content:"";
      position:absolute;
      left:22px;
      top:0;
      width:72px;
      height:3px;
      background:linear-gradient(90deg, var(--accent), rgba(173,74,45,0));
    }}
    .project-card h3 {{
      margin:0 0 8px;
      font-size:24px;
      line-height:1.12;
    }}
    .project-card p {{
      margin:0 0 10px;
      color:var(--muted);
      line-height:1.7;
    }}
    .task-card, .platform-card, .profile-card {{
      padding:18px;
      border-radius:20px;
      border:1px solid rgba(205,184,155,.74);
      background:linear-gradient(180deg, rgba(255,255,255,.62), rgba(252,247,240,.56));
    }}
    .task-list, .platform-list, .profile-list {{
      display:grid;
      gap:12px;
      margin-top:12px;
    }}
    .task-shell {{
      margin-top:18px;
    }}
    .task-toolbar {{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
      flex-wrap:wrap;
      margin-bottom:14px;
    }}
    .task-filter-group {{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
    }}
    .filter-chip {{
      appearance:none;
      border:1px solid rgba(40,77,70,.14);
      background:rgba(40,77,70,.04);
      color:var(--accent-2);
      border-radius:999px;
      padding:10px 14px;
      font:600 13px/1 var(--ui);
      cursor:pointer;
      transition:all .18s ease;
      box-shadow:none;
    }}
    .filter-chip:hover {{
      transform:translateY(-1px);
    }}
    .filter-chip.active {{
      background:linear-gradient(135deg, var(--accent), #c8633c);
      color:#fff;
      border-color:transparent;
      box-shadow:0 12px 24px rgba(173,74,45,.18);
    }}
    .task-columns {{
      display:grid;
      grid-template-columns:repeat(2, minmax(0, 1fr));
      gap:16px;
    }}
    .task-pane {{
      min-height:220px;
      transition:opacity .2s ease, transform .2s ease;
    }}
    .task-pane.hidden {{
      display:none;
    }}
    .task-pane-head {{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
      margin-bottom:4px;
    }}
    .task-pane-head h3 {{
      margin:0;
      font-size:20px;
      line-height:1.1;
    }}
    .empty-state {{
      min-height:160px;
      display:flex;
      align-items:center;
      justify-content:center;
      text-align:center;
      padding:24px;
      border-style:dashed;
      background:linear-gradient(180deg, rgba(255,255,255,.52), rgba(251,246,238,.44));
    }}
    .task-card strong,
    .platform-card strong,
    .profile-card strong {{
      display:block;
      margin-bottom:6px;
      font-size:18px;
      line-height:1.2;
    }}
    .badge {{
      display:inline-flex;
      align-items:center;
      gap:6px;
      border-radius:999px;
      padding:6px 10px;
      font-size:12px;
      background:rgba(178,74,40,.10);
      color:var(--accent);
      font-family:var(--ui);
      border:1px solid rgba(173,74,45,.12);
    }}
    .danger {{
      background:rgba(142,47,47,.10);
      color:var(--danger);
      border:1px solid rgba(142,47,47,.18);
    }}
    .inline-note {{
      font-size:13px;
      color:var(--muted);
      line-height:1.5;
      font-family:var(--ui);
    }}
    .project-tools {{
      margin-top:16px;
      padding-top:16px;
      border-top:1px dashed rgba(111,99,82,.22);
    }}
    .task-meta {{
      font-size:13px;
      color:var(--muted);
      line-height:1.5;
      font-family:var(--ui);
    }}
    .muted {{
      color:var(--muted);
      font-family:var(--ui);
    }}
    a {{
      color:var(--accent);
      text-decoration:none;
    }}
    a:hover {{
      text-decoration:underline;
    }}
    @keyframes riseIn {{
      from {{
        opacity:0;
        transform:translateY(12px);
      }}
      to {{
        opacity:1;
        transform:translateY(0);
      }}
    }}
    @media (max-width: 900px) {{
      .masthead {{
        flex-direction:column;
        align-items:flex-start;
      }}
      .hero {{ grid-template-columns:1fr; }}
      .grid, .stats, .section-grid {{ grid-template-columns:1fr; }}
      h1 {{
        font-size:42px;
        max-width:11ch;
      }}
      .projects {{
        padding:22px;
      }}
      .task-columns {{
        grid-template-columns:1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="masthead">
      <div class="brand-lockup">
        <span class="brand-name">ForWin Control Desk</span>
        <span class="brand-note">长篇网文生成、项目检查与平台发布共用一套控制台。</span>
      </div>
      <div class="masthead-links">
        <a class="button secondary" href="#generator">生成入口</a>
        <a class="button secondary" href="#task_panels">任务队列</a>
        <a class="button secondary" href="/publishers">平台安装与排障</a>
      </div>
    </header>
    <section class="hero">
      <article class="panel lead">
        <div>
          <span class="eyebrow">FORWIN · Longform Web Novel Studio</span>
          <h1>从设定到章节，把长篇连载真正跑起来</h1>
          <p>首页现在同时承接生成与发布。模型配置、生成任务、发布任务、平台状态和项目卡内一键发布都放在这里；平台页只保留给扩展安装和排障。</p>
          <div class="stats">
            <div class="stat">
              <b>多模型</b>
              <span class="muted">支持保存多个 Base URL / API Key / Model 档案</span>
            </div>
            <div class="stat">
              <b>双任务栏</b>
              <span class="muted">生成任务和发布任务拆开看，不再混在项目卡里</span>
            </div>
            <div class="stat">
              <b>卡内发布</b>
              <span class="muted">从项目卡直接选章节入队上传，不再手动复制正文</span>
            </div>
          </div>
        </div>
        <div class="hero-actions">
          <a class="button primary" href="#generator">开始生成</a>
          <a class="button secondary" href="#task_panels">看任务队列</a>
          <a class="button secondary" href="/publishers">平台安装与排障页</a>
        </div>
        <div class="lead-footnote">视觉层重构后，这里更像创作工作台而不是原始调试页，但所有交互仍走现有后端接口。</div>
      </article>
      <section class="panel form" id="generator">
        <div>
          <h2>生成新项目</h2>
          <div class="hint">先保存模型档案，再用下拉选择本次生成使用哪一套模型。运行模式仍可按次覆盖。</div>
        </div>
        <div class="grid">
          <div>
            <label for="generate_profile_id">模型配置</label>
            <select id="generate_profile_id"></select>
            <div class="inline-note" id="generate_profile_meta">正在加载模型配置...</div>
          </div>
          <div>
            <label for="num_chapters">章节数</label>
            <input id="num_chapters" type="number" min="1" max="20" value="{default_chapters}">
          </div>
        </div>
        <div class="grid">
          <div>
            <label for="genre">题材</label>
            <select id="genre">
              <option selected>{default_genre}</option>
              <option>仙侠</option>
              <option>都市</option>
              <option>悬疑</option>
              <option>科幻</option>
              <option>历史</option>
            </select>
          </div>
          <div>
            <label for="operation_mode">本次运行模式</label>
            <select id="operation_mode">
              <option value="blackbox" {"selected" if operation_mode == "blackbox" else ""}>黑箱模式</option>
              <option value="checkpoint" {"selected" if operation_mode == "checkpoint" else ""}>检查点模式</option>
              <option value="copilot" {"selected" if operation_mode == "copilot" else ""}>共驾模式</option>
            </select>
            <div id="mode_hint" class="hint" style="margin-top:6px;"></div>
          </div>
          <div style="display:flex;align-items:end;">
            <label style="display:flex; gap:10px; align-items:flex-start; margin:0 0 10px;">
              <input id="freeze_failed_candidates" type="checkbox" style="width:auto; margin-top:4px;" {"checked" if freeze_failed_candidates else ""}>
              <span>状态写入失败时冻结 candidate artifact</span>
            </label>
          </div>
        </div>
        <div>
          <label for="premise">故事前提</label>
          <textarea id="premise" placeholder="例如：废土纪元三百年后，失忆少年在边境黑井苏醒，身上带着一枚会记录他死亡次数的青铜环。"></textarea>
        </div>
        <div class="row">
          <button class="primary" onclick="startGeneration()">提交生成任务</button>
          <button class="secondary" onclick="loadProjects()">刷新项目列表</button>
        </div>
        <div id="task_status" class="status">等待提交任务。</div>
      </section>
    </section>

    <section class="section-grid">
      <section class="panel projects">
        <div class="projects-head">
          <div>
            <h2 style="margin:0 0 6px; font-size:24px;">模型配置</h2>
            <div class="hint">这里维护可复用的模型档案。生成任务只需要下拉选择，不再每次手填 Base URL / API Key / Model。</div>
          </div>
          <span id="saved_badge" class="badge">已保存 API Key：{"是" if has_api_key else "否"}</span>
        </div>
        <div class="mini-grid">
          <div>
            <label for="profile_picker">已有模型档案</label>
            <select id="profile_picker"></select>
          </div>
          <div>
            <label for="profile_name">配置名称</label>
            <input id="profile_name" placeholder="例如：MiniMax 主账号 / OpenRouter 备用">
          </div>
        </div>
        <div class="grid">
          <div>
            <label for="profile_api_key">API Key</label>
            <input id="profile_api_key" type="password" placeholder="sk-..." autocomplete="off">
          </div>
          <div>
            <label for="profile_model">Model</label>
            <input id="profile_model" value="{model}" spellcheck="false">
          </div>
        </div>
        <div class="grid">
          <div>
            <label for="profile_base_url">Base URL</label>
            <input id="profile_base_url" value="{base_url}" spellcheck="false">
          </div>
          <div>
            <label for="saved_operation_mode">默认运行模式</label>
            <select id="saved_operation_mode">
              <option value="blackbox" {"selected" if operation_mode == "blackbox" else ""}>黑箱模式</option>
              <option value="checkpoint" {"selected" if operation_mode == "checkpoint" else ""}>检查点模式</option>
              <option value="copilot" {"selected" if operation_mode == "copilot" else ""}>共驾模式</option>
            </select>
            <div id="saved_mode_hint" class="hint" style="margin-top:6px;"></div>
          </div>
        </div>
        <div class="row">
          <label style="display:flex; gap:10px; align-items:flex-start; margin:0;">
            <input id="saved_freeze_failed_candidates" type="checkbox" style="width:auto; margin-top:4px;" {"checked" if freeze_failed_candidates else ""}>
            <span>状态写入失败时冻结 candidate artifact</span>
          </label>
          <label style="display:flex; gap:10px; align-items:flex-start; margin:0;">
            <input id="profile_set_default" type="checkbox" style="width:auto; margin-top:4px;">
            <span>保存后设为默认模型</span>
          </label>
        </div>
        <div class="row">
          <button class="primary" onclick="saveProfile()">保存模型配置</button>
          <button class="secondary" onclick="saveRuntimePreferences()">保存运行偏好</button>
          <button class="secondary" onclick="resetProfileForm()">新建配置</button>
          <button class="secondary" onclick="setDefaultProfile()">设为默认</button>
          <button class="secondary danger" onclick="deleteProfile()">删除当前配置</button>
        </div>
        <div id="settings_status" class="status">模型配置尚未修改。</div>
        <div id="profile_list" class="profile-list"></div>
      </section>

      <section class="panel projects" id="task_panels">
        <div class="projects-head">
          <div>
            <h2 style="margin:0 0 6px; font-size:24px;">平台与任务</h2>
            <div class="hint">首页直接看扩展状态和最近队列，必要时再跳转到平台安装与排障页。</div>
          </div>
          <a class="button secondary" href="/publishers">高级发布页</a>
        </div>
        <div id="platform_list" class="platform-list"></div>
        <div class="task-shell">
          <div class="task-toolbar">
            <div class="task-filter-group">
              <button id="task_filter_all" class="filter-chip active" onclick="setTaskView('all')">全部任务</button>
              <button id="task_filter_generation" class="filter-chip" onclick="setTaskView('generation')">只看生成</button>
              <button id="task_filter_publish" class="filter-chip" onclick="setTaskView('publish')">只看上传</button>
            </div>
            <div class="section-note">根据当前工作切换任务视图，空态也会跟着切换。</div>
          </div>
          <div id="task_columns" class="task-columns">
            <div id="generation_task_pane" class="task-pane">
              <div class="task-pane-head">
                <h3>生成任务</h3>
                <button class="secondary" onclick="loadTasks()">刷新</button>
              </div>
              <div id="generation_tasks" class="task-list"></div>
            </div>
            <div id="publish_task_pane" class="task-pane">
              <div class="task-pane-head">
                <h3>发布任务</h3>
                <button class="secondary" onclick="loadUploadJobs()">刷新</button>
              </div>
              <div id="publish_tasks" class="task-list"></div>
            </div>
          </div>
        </div>
      </section>
    </section>

    <section class="panel projects">
      <div class="projects-head">
        <div>
          <h2 style="margin:0 0 6px; font-size:24px;">已有项目</h2>
          <div class="hint">项目卡里可以直接删除项目、查看章节状态，并把指定章节直接丢进平台发布队列。</div>
        </div>
        <button class="secondary" onclick="loadProjects()">刷新项目列表</button>
      </div>
      <div id="project_list" class="project-list"></div>
    </section>
  </div>
  <script>
    let currentTaskId = null;
    let settingsState = null;
    let platformsState = [];
    let currentProfileId = '';
    let taskViewMode = 'all';
    let tasksPollTimer = null;
    let uploadsPollTimer = null;

    function setStatus(text) {{
      document.getElementById('task_status').textContent = text;
    }}

{PAGE_DOM_HELPERS_JS}

    function modeHintText(mode) {{
      if (mode === 'checkpoint') {{
        return '检查点模式：写完初稿和 review 后暂停，等你确认再继续写入 canon。';
      }}
      if (mode === 'copilot') {{
        return '共驾模式：默认自动跑，但 review 不是 pass 时会停下来给你接管。';
      }}
      return '黑箱模式：默认一路自动执行，只在任务结束或严重失败时停下。';
    }}

    function updateModeHints() {{
      document.getElementById('saved_mode_hint').textContent = modeHintText(document.getElementById('saved_operation_mode').value);
      document.getElementById('mode_hint').textContent = modeHintText(document.getElementById('operation_mode').value);
    }}

    function setTaskView(mode) {{
      taskViewMode = mode;
      const columns = document.getElementById('task_columns');
      const generationPane = document.getElementById('generation_task_pane');
      const publishPane = document.getElementById('publish_task_pane');
      const allButton = document.getElementById('task_filter_all');
      const generationButton = document.getElementById('task_filter_generation');
      const publishButton = document.getElementById('task_filter_publish');

      allButton.classList.toggle('active', mode === 'all');
      generationButton.classList.toggle('active', mode === 'generation');
      publishButton.classList.toggle('active', mode === 'publish');

      generationPane.classList.toggle('hidden', mode === 'publish');
      publishPane.classList.toggle('hidden', mode === 'generation');

      columns.style.gridTemplateColumns = mode === 'all' ? 'repeat(2, minmax(0, 1fr))' : 'minmax(0, 1fr)';
    }}

    function selectedProfile() {{
      if (!settingsState || !Array.isArray(settingsState.profiles)) return null;
      const targetId = document.getElementById('generate_profile_id').value || settingsState.default_profile_id;
      return settingsState.profiles.find((item) => item.id === targetId) || settingsState.profiles[0] || null;
    }}

    function updateGenerateProfileMeta() {{
      const profile = selectedProfile();
      const el = document.getElementById('generate_profile_meta');
      if (!profile) {{
        el.textContent = '还没有模型配置，请先在下面保存至少一条。';
        return;
      }}
      el.textContent = `当前使用：${{profile.name}} | ${{profile.model}} | ${{profile.base_url}} | API Key：${{profile.has_api_key ? '已保存' : '未保存'}}`;
    }}

    function resetProfileForm() {{
      currentProfileId = '';
      document.getElementById('profile_picker').value = '';
      document.getElementById('profile_name').value = '';
      document.getElementById('profile_api_key').value = '';
      document.getElementById('profile_base_url').value = {base_url_json};
      document.getElementById('profile_model').value = {model_json};
      document.getElementById('profile_set_default').checked = false;
    }}

    function fillProfileForm(profileId) {{
      currentProfileId = profileId || '';
      const profile = (settingsState?.profiles || []).find((item) => item.id === currentProfileId);
      if (!profile) {{
        resetProfileForm();
        return;
      }}
      document.getElementById('profile_picker').value = profile.id;
      document.getElementById('profile_name').value = profile.name || '';
      document.getElementById('profile_api_key').value = '';
      document.getElementById('profile_base_url').value = profile.base_url || {base_url_json};
      document.getElementById('profile_model').value = profile.model || {model_json};
      document.getElementById('profile_set_default').checked = profile.id === settingsState.default_profile_id;
    }}

    function renderProfiles() {{
      const list = document.getElementById('profile_list');
      const picker = document.getElementById('profile_picker');
      const generate = document.getElementById('generate_profile_id');
      clearNode(list);
      clearNode(picker);
      clearNode(generate);

      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = '新建模型配置';
      picker.appendChild(blank);

      const profiles = Array.isArray(settingsState?.profiles) ? settingsState.profiles : [];
      if (!profiles.length) {{
        list.appendChild(createNode('div', '还没有模型配置，请先保存一条。', 'profile-card'));
        updateGenerateProfileMeta();
        return;
      }}

      profiles.forEach((profile) => {{
        const pickerOption = document.createElement('option');
        pickerOption.value = profile.id;
        pickerOption.textContent = profile.name;
        picker.appendChild(pickerOption);

        const genOption = document.createElement('option');
        genOption.value = profile.id;
        genOption.textContent = `${{profile.name}}${{profile.id === settingsState.default_profile_id ? ' · 默认' : ''}}`;
        if (profile.id === settingsState.default_profile_id) {{
          genOption.selected = true;
        }}
        generate.appendChild(genOption);

        const card = createNode('article', '', 'profile-card');
        card.appendChild(createNode('strong', profile.name));
        if (profile.id === settingsState.default_profile_id) {{
          card.appendChild(createNode('span', '默认模型', 'badge'));
        }}
        card.appendChild(createNode('div', `${{profile.model}} | ${{profile.base_url}}`, 'task-meta'));
        card.appendChild(createNode('div', `API Key：${{profile.has_api_key ? '已保存' : '未保存'}}`, 'task-meta'));
        const actions = createNode('div', '', 'row');
        actions.style.marginTop = '10px';
        actions.appendChild(createButton('编辑', () => fillProfileForm(profile.id), 'secondary'));
        actions.appendChild(createButton('设为生成模型', () => {{
          generate.value = profile.id;
          updateGenerateProfileMeta();
        }}, 'secondary'));
        card.appendChild(actions);
        list.appendChild(card);
      }});
      updateGenerateProfileMeta();
    }}

    async function startGeneration() {{
      const premise = document.getElementById('premise').value.trim();
      if (!premise) {{
        setStatus('请先填写故事前提。');
        return;
      }}

      const payload = {{
        premise,
        genre: document.getElementById('genre').value,
        num_chapters: Number(document.getElementById('num_chapters').value || '3'),
        model_profile_id: document.getElementById('generate_profile_id').value || null,
        operation_mode: document.getElementById('operation_mode').value,
        freeze_failed_candidates: document.getElementById('freeze_failed_candidates').checked,
      }};

      setStatus('正在提交任务...');
      const res = await fetch('/api/generate', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!res.ok) {{
        setStatus(data.detail || '提交失败');
        return;
      }}

      currentTaskId = data.task_id;
      setStatus(`任务已创建：${{data.task_id}}\\n${{data.message}}`);
      pollTask();
      loadTasks();
    }}

    async function loadSettings() {{
      const res = await fetch('/api/settings/llm');
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('settings_status').textContent = data.detail || '读取配置失败';
        return;
      }}
      settingsState = data;
      document.getElementById('profile_base_url').value = data.base_url;
      document.getElementById('profile_model').value = data.model;
      document.getElementById('saved_operation_mode').value = data.operation_mode;
      document.getElementById('saved_freeze_failed_candidates').checked = Boolean(data.freeze_failed_candidates);
      document.getElementById('operation_mode').value = data.operation_mode;
      document.getElementById('freeze_failed_candidates').checked = Boolean(data.freeze_failed_candidates);
      document.getElementById('saved_badge').textContent = `已保存 API Key：${{data.has_api_key ? '是' : '否'}}`;
      document.getElementById('settings_status').textContent = data.message || '已读取默认配置';
      renderProfiles();
      fillProfileForm(data.default_profile_id || '');
      updateModeHints();
    }}

    async function saveRuntimePreferences() {{
      const payload = {{
        operation_mode: document.getElementById('saved_operation_mode').value,
        freeze_failed_candidates: document.getElementById('saved_freeze_failed_candidates').checked,
      }};
      const res = await fetch('/api/settings/llm/preferences', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('settings_status').textContent = data.detail || '保存失败';
        return;
      }}
      settingsState = data;
      document.getElementById('operation_mode').value = data.operation_mode;
      document.getElementById('freeze_failed_candidates').checked = Boolean(data.freeze_failed_candidates);
      document.getElementById('saved_badge').textContent = `已保存 API Key：${{data.has_api_key ? '是' : '否'}}`;
      document.getElementById('settings_status').textContent = data.message || '已保存运行偏好';
      updateModeHints();
    }}

    async function saveProfile() {{
      const payload = {{
        profile_id: currentProfileId || null,
        name: document.getElementById('profile_name').value.trim(),
        api_key: document.getElementById('profile_api_key').value.trim(),
        base_url: document.getElementById('profile_base_url').value.trim(),
        model: document.getElementById('profile_model').value.trim(),
        set_as_default: document.getElementById('profile_set_default').checked,
      }};
      if (!payload.name) {{
        document.getElementById('settings_status').textContent = '请先填写配置名称。';
        return;
      }}
      const res = await fetch('/api/settings/llm/profiles', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('settings_status').textContent = data.detail || '保存模型配置失败';
        return;
      }}
      settingsState = data;
      document.getElementById('settings_status').textContent = data.message || '模型配置已保存';
      document.getElementById('profile_api_key').value = '';
      renderProfiles();
      fillProfileForm(data.default_profile_id || payload.profile_id || '');
    }}

    async function setDefaultProfile() {{
      const profileId = document.getElementById('profile_picker').value || currentProfileId;
      if (!profileId) {{
        document.getElementById('settings_status').textContent = '请先选择一条模型配置。';
        return;
      }}
      const res = await fetch('/api/settings/llm/default-profile', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ profile_id: profileId }}),
      }});
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('settings_status').textContent = data.detail || '切换默认模型失败';
        return;
      }}
      settingsState = data;
      document.getElementById('settings_status').textContent = data.message || '默认模型已切换';
      renderProfiles();
      fillProfileForm(profileId);
    }}

    async function deleteProfile() {{
      const profileId = document.getElementById('profile_picker').value || currentProfileId;
      if (!profileId) {{
        document.getElementById('settings_status').textContent = '请先选择要删除的模型配置。';
        return;
      }}
      if (!window.confirm('确定删除这条模型配置吗？')) return;
      const res = await fetch(`/api/settings/llm/profiles/${{profileId}}`, {{
        method: 'DELETE',
      }});
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('settings_status').textContent = data.detail || '删除模型配置失败';
        return;
      }}
      settingsState = data;
      document.getElementById('settings_status').textContent = data.message || '模型配置已删除';
      renderProfiles();
      fillProfileForm(data.default_profile_id || '');
    }}

    async function pollTask() {{
      if (!currentTaskId) return;
      const res = await fetch(`/api/tasks/${{currentTaskId}}`);
      const data = await res.json();
      const lines = [
        `任务 ID：${{data.task_id}}`,
        `状态：${{data.status}}`,
      ];
      if (data.project_id) lines.push(`项目 ID：${{data.project_id}}`);
      if (Array.isArray(data.paused_chapters) && data.paused_chapters.length) lines.push(`暂停章节：${{data.paused_chapters.join(', ')}}`);
      if (Array.isArray(data.frozen_artifacts) && data.frozen_artifacts.length) lines.push(`冻结 artifact：${{data.frozen_artifacts.join(', ')}}`);
      if (data.status === 'needs_review') lines.push('提示：当前任务是按运行模式主动暂停，不是崩溃。');
      if (data.message) lines.push(`说明：${{data.message}}`);
      if (data.error) lines.push(`错误：${{data.error}}`);
      setStatus(lines.join('\\n'));

      if (data.status === 'running' || data.status === 'starting') {{
        window.setTimeout(pollTask, 1500);
        return;
      }}
      loadProjects();
      loadTasks();
    }}

    async function showReview(projectId, chapterNumber) {{
      const res = await fetch(`/api/projects/${{projectId}}/chapters/${{chapterNumber}}/review`);
      const data = await res.json();
      if (!res.ok) {{
        setStatus(data.detail || '读取 review 失败');
        return;
      }}
      const lines = [
        `项目：${{projectId}}`,
        `章节：第${{chapterNumber}}章《${{data.title}}》`,
        `状态：${{data.status}}`,
        `verdict：${{data.verdict}}`,
      ];
      if (Array.isArray(data.issues) && data.issues.length) {{
        lines.push('问题列表：');
        data.issues.forEach((issue, index) => {{
          lines.push(`${{index + 1}}. [${{issue.severity}}] ${{issue.description}}`);
        }});
      }} else {{
        lines.push('问题列表：无');
      }}
      window.alert(lines.join('\\n'));
    }}

    async function approveReview(projectId, chapterNumber, continueGeneration) {{
      setStatus(`正在处理第${{chapterNumber}}章 review...`);
      const res = await fetch(`/api/projects/${{projectId}}/chapters/${{chapterNumber}}/review/approve`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ continue_generation: Boolean(continueGeneration) }}),
      }});
      const data = await res.json();
      if (!res.ok) {{
        setStatus(data.detail || '处理 review 失败');
        return;
      }}
      if (data.task_id) {{
        currentTaskId = data.task_id;
        setStatus(`已接受第${{chapterNumber}}章并继续执行。\\n任务 ID：${{data.task_id}}\\n${{data.message}}`);
        pollTask();
      }} else {{
        setStatus(data.message || `已接受第${{chapterNumber}}章。`);
      }}
      loadProjects();
    }}

    async function showProvisional(projectId) {{
      setStatus(`正在读取项目 ${{projectId}} 的 provisional 预演...`);
      const res = await fetch(`/api/projects/${{projectId}}/provisional/latest`);
      const data = await res.json();
      if (!res.ok) {{
        setStatus(data.detail || '读取 provisional 预演失败');
        return;
      }}
      const lines = [
        `项目：${{projectId}}`,
        `Band：${{data.band_id}}`,
        `aggregate_verdict：${{data.aggregate_verdict}}`,
        `预演字数：${{data.preview_char_count}}`,
        `问题数：${{data.issue_count}} | 失败数：${{data.failure_count}}`,
      ];
      if (Array.isArray(data.chapters) && data.chapters.length) {{
        lines.push('章节预演：');
        data.chapters.forEach((chapter) => {{
          const chapterMeta = [
            `第${{chapter.chapter_number}}章《${{chapter.title}}》`,
            `verdict=${{chapter.verdict}}`,
            `字数=${{chapter.char_count}}`,
          ];
          if (chapter.projected_time_label) chapterMeta.push(`投影时间=${{chapter.projected_time_label}}`);
          lines.push(`- ${{chapterMeta.join(' | ')}}`);
          if (Array.isArray(chapter.events) && chapter.events.length) {{
            lines.push(`  事件：${{chapter.events.slice(0, 2).map((item) => item.summary || item.event_name || '事件').join(' / ')}}`);
          }}
          if (Array.isArray(chapter.issues) && chapter.issues.length) {{
            lines.push(`  问题：${{chapter.issues.slice(0, 2).map((item) => item.description || item.rule_name || 'issue').join(' / ')}}`);
          }}
        }});
      }}
      window.alert(lines.join('\\n'));
      setStatus(`已读取 ${{projectId}} 的 provisional 预演。`);
    }}

    async function loadProjects() {{
      const list = document.getElementById('project_list');
      const res = await fetch('/api/projects');
      const data = await res.json();
      clearNode(list);
      if (!Array.isArray(data) || data.length === 0) {{
        const emptyCard = createNode('div', '', 'project-card');
        emptyCard.appendChild(createNode('p', '还没有项目。先在上面提交一个生成任务。'));
        list.appendChild(emptyCard);
        return;
      }}
      for (const item of data) {{
        const chapters = Array.isArray(item.chapters) ? item.chapters : [];
        const card = document.createElement('article');
        card.className = 'project-card';
        const head = createNode('div', '', 'row');
        head.style.justifyContent = 'space-between';
        head.style.alignItems = 'flex-start';
        const titleWrap = document.createElement('div');
        titleWrap.appendChild(createNode('h3', item.title || '未命名项目'));
        head.appendChild(titleWrap);
        head.appendChild(createButton('删除项目', () => deleteProject(item.id, item.title || '未命名项目'), 'secondary danger'));
        card.appendChild(head);
        card.appendChild(createNode('p', item.premise || ''));
        card.appendChild(createNode('p', `题材：${{item.genre}}`, 'muted'));
        card.appendChild(createNode('p', `创建时间：${{item.created_at || ''}}`, 'muted'));
        if (item.latest_stage || item.pacing_verdict || item.current_time_label) {{
          const phaseMeta = [];
          if (item.latest_stage) phaseMeta.push(`阶段：${{item.latest_stage}}`);
          if (item.pacing_verdict) phaseMeta.push(`节奏：${{item.pacing_verdict}}`);
          if (item.current_time_label) phaseMeta.push(`时间：${{item.current_time_label}}`);
          card.appendChild(createNode('p', phaseMeta.join(' | '), 'muted'));
        }}
        if (item.active_arc_target_size) {{
          card.appendChild(createNode(
            'p',
            `Arc Envelope：${{item.active_arc_policy_tier}} | target ${{item.active_arc_target_size}}章 | range ${{item.active_arc_soft_min}}~${{item.active_arc_soft_max}} | band ${{item.active_arc_detailed_band_size}} | frozen ${{item.active_arc_frozen_zone_size}}`,
            'muted'
          ));
        }}
        if (item.active_arc_recommendation || item.active_arc_analysis_confidence) {{
          const recommendation = item.active_arc_recommendation || 'keep';
          const evidence = Array.isArray(item.active_arc_evidence) ? item.active_arc_evidence.slice(0, 2).join(' / ') : '';
          card.appendChild(createNode(
            'p',
            `Envelope Analysis：${{recommendation}} | confidence ${{(item.active_arc_analysis_confidence || 0).toFixed(2)}}${{evidence ? ` | ${{evidence}}` : ''}}`,
            'muted'
          ));
        }}
        if (item.provisional_band_id) {{
          card.appendChild(createNode(
            'p',
            `Provisional：${{item.provisional_band_id}} | verdict ${{item.provisional_aggregate_verdict || 'pass'}} | 预演字数 ${{item.provisional_preview_char_count}} | issues ${{item.provisional_issue_count}} | failures ${{item.provisional_failure_count}}`,
            'muted'
          ));
        }}
        if (item.pacing_summary) {{
          card.appendChild(createNode('p', `分析：${{item.pacing_summary}}`, 'muted'));
        }}
        if (item.world_pressure_level || item.world_pressure_summary) {{
          card.appendChild(createNode('p', `世界压力：${{item.world_pressure_level || 'steady'}} | ${{item.world_pressure_summary || ''}}`, 'muted'));
        }}
        if (item.last_replan_status && item.last_replan_reason) {{
          card.appendChild(createNode('p', `最近 replan(${{item.last_replan_status}})：${{item.last_replan_reason}}`, 'muted'));
        }}
        const chapterLabel = createNode('div', '章节状态', 'muted');
        chapterLabel.style.margin = '12px 0 4px';
        card.appendChild(chapterLabel);
        const chapterSummary = document.createElement('div');
        if (chapters.length) {{
          chapters.forEach((chapter) => {{
            const base = `第${{chapter.chapter_number}}章：${{chapter.status}}`;
            if (chapter.status === 'needs_review') {{
              const block = document.createElement('div');
              block.style.marginTop = '8px';
              block.style.paddingTop = '8px';
              block.style.borderTop = '1px dashed #d9c8ad';
              block.appendChild(createNode('div', base));
              const row = createNode('div', '', 'row');
              row.style.marginTop = '8px';
              row.appendChild(createButton('查看 review', () => showReview(item.id, chapter.chapter_number), 'secondary'));
              row.appendChild(createButton('接受', () => approveReview(item.id, chapter.chapter_number, false), 'secondary'));
              row.appendChild(createButton('接受并继续', () => approveReview(item.id, chapter.chapter_number, true), 'secondary'));
              block.appendChild(row);
              chapterSummary.appendChild(block);
              return;
            }}
            const line = createNode('div', base);
            line.style.marginTop = '6px';
            chapterSummary.appendChild(line);
          }});
        }} else {{
          chapterSummary.appendChild(createNode('div', '还没有章节。', 'muted'));
        }}
        card.appendChild(chapterSummary);

        const publishTools = createNode('div', '', 'project-tools');
        publishTools.appendChild(createNode('div', '发布到平台', 'muted'));
        const toolGrid = createNode('div', '', 'grid');
        toolGrid.style.marginTop = '8px';
        const chapterWrap = document.createElement('div');
        const chapterLabelSelect = createNode('label', '章节');
        chapterLabelSelect.setAttribute('for', `publish_chapter_${{item.id}}`);
        chapterWrap.appendChild(chapterLabelSelect);
        const chapterSelect = document.createElement('select');
        chapterSelect.id = `publish_chapter_${{item.id}}`;
        chapters.filter((chapter) => chapter.status !== 'planned').forEach((chapter) => {{
          const option = document.createElement('option');
          option.value = String(chapter.chapter_number);
          option.textContent = `第${{chapter.chapter_number}}章 · ${{chapter.status}}`;
          chapterSelect.appendChild(option);
        }});
        chapterWrap.appendChild(chapterSelect);
        toolGrid.appendChild(chapterWrap);

        const platformWrap = document.createElement('div');
        const platformLabel = createNode('label', '平台');
        platformLabel.setAttribute('for', `publish_platform_${{item.id}}`);
        platformWrap.appendChild(platformLabel);
        const platformSelect = document.createElement('select');
        platformSelect.id = `publish_platform_${{item.id}}`;
        (platformsState.length ? platformsState : [
          {{ platform_id: 'fanqie', display_name: '番茄小说' }},
          {{ platform_id: 'qidian', display_name: '起点小说' }},
        ]).forEach((platform) => {{
          const option = document.createElement('option');
          option.value = platform.platform_id;
          option.textContent = platform.display_name;
          platformSelect.appendChild(option);
        }});
        platformWrap.appendChild(platformSelect);
        toolGrid.appendChild(platformWrap);
        publishTools.appendChild(toolGrid);

        const bookLabel = createNode('label', '平台作品名');
        bookLabel.setAttribute('for', `publish_book_${{item.id}}`);
        publishTools.appendChild(bookLabel);
        const bookInput = document.createElement('input');
        bookInput.id = `publish_book_${{item.id}}`;
        bookInput.value = item.title || '';
        publishTools.appendChild(bookInput);

        const publishOptions = createNode('div', '', 'row');
        publishOptions.style.marginTop = '8px';
        const createCheckboxLabel = document.createElement('label');
        createCheckboxLabel.style.display = 'flex';
        createCheckboxLabel.style.gap = '10px';
        createCheckboxLabel.style.alignItems = 'flex-start';
        const createCheckbox = document.createElement('input');
        createCheckbox.type = 'checkbox';
        createCheckbox.style.width = 'auto';
        createCheckbox.style.marginTop = '4px';
        createCheckbox.id = `publish_create_${{item.id}}`;
        createCheckboxLabel.appendChild(createCheckbox);
        createCheckboxLabel.appendChild(createNode('span', '找不到作品时自动创建新书'));
        publishOptions.appendChild(createCheckboxLabel);
        publishTools.appendChild(publishOptions);

        const publishActions = createNode('div', '', 'row');
        publishActions.style.marginTop = '10px';
        publishActions.appendChild(createButton('直接发布', () => createPublishJob(item.id, true), 'primary'));
        publishActions.appendChild(createButton('保存草稿', () => createPublishJob(item.id, false), 'secondary'));
        publishTools.appendChild(publishActions);

        const publishStatus = createNode('div', chapters.length ? '选择章节后即可直接入队上传任务。' : '没有可发布章节。', 'status');
        publishStatus.id = `publish_status_${{item.id}}`;
        publishTools.appendChild(publishStatus);
        card.appendChild(publishTools);

        const projectLink = document.createElement('a');
        projectLink.className = 'button secondary';
        projectLink.href = `/api/projects/${{item.id}}`;
        projectLink.target = '_blank';
        projectLink.textContent = '查看项目 JSON';
        const projectActions = createNode('div', '', 'row');
        projectActions.style.marginTop = '12px';
        if (item.provisional_band_id) {{
          projectActions.appendChild(createButton('查看预演', () => showProvisional(item.id), 'secondary'));
        }}
        projectActions.appendChild(projectLink);
        card.appendChild(projectActions);
        list.appendChild(card);
      }}
    }}

    async function deleteProject(projectId, title) {{
      if (!window.confirm(`确定删除项目《${{title}}》吗？`)) return;
      const res = await fetch(`/api/projects/${{projectId}}`, {{
        method: 'DELETE',
      }});
      const data = await res.json();
      if (!res.ok) {{
        setStatus(data.detail || '删除项目失败');
        return;
      }}
      setStatus(data.message || '项目已删除');
      loadProjects();
      loadTasks();
      loadUploadJobs();
    }}

    async function loadPlatforms() {{
      const list = document.getElementById('platform_list');
      const res = await fetch('/api/publishers/platforms');
      const data = await res.json();
      clearNode(list);
      platformsState = Array.isArray(data) ? data : [];
      if (!platformsState.length) {{
        list.appendChild(createNode('div', '尚未读取到平台状态。', 'platform-card'));
        return;
      }}
      platformsState.forEach((item) => {{
        const card = createNode('article', '', 'platform-card');
        card.appendChild(createNode('strong', item.display_name));
        const statusText = `${{item.connected ? '已登录' : '未登录'}} | ${{item.extension_online ? '扩展在线' : '扩展离线'}}`;
        card.appendChild(createNode('div', statusText, `status ${{item.connected ? 'ok' : 'warn'}}`));
        card.appendChild(createNode('div', `执行端：${{item.extension_client_id || '未绑定'}}`, 'task-meta'));
        card.appendChild(createNode('div', `最近心跳：${{item.last_heartbeat_at || '无'}}`, 'task-meta'));
        if (item.last_error) {{
          card.appendChild(createNode('div', `最近错误：${{item.last_error}}`, 'task-meta'));
        }}
        list.appendChild(card);
      }});
    }}

    async function loadTasks() {{
      const list = document.getElementById('generation_tasks');
      const res = await fetch('/api/tasks?limit=20');
      const data = await res.json();
      clearNode(list);
      if (!Array.isArray(data) || !data.length) {{
        list.appendChild(createNode('div', '还没有生成任务。', 'task-card empty-state'));
        return;
      }}
      let hasRunning = false;
      data.forEach((item) => {{
        if (item.status === 'running' || item.status === 'starting') {{
          hasRunning = true;
        }}
        const card = createNode('article', '', 'task-card');
        card.appendChild(createNode('strong', `${{item.task_id}} · ${{item.status}}`));
        if (item.project_id) {{
          card.appendChild(createNode('div', `项目：${{item.project_id}}`, 'task-meta'));
        }}
        if (item.message) {{
          card.appendChild(createNode('div', item.message, 'task-meta'));
        }}
        if (item.error) {{
          card.appendChild(createNode('div', `错误：${{item.error}}`, 'task-meta'));
        }}
        card.appendChild(createNode('div', `创建：${{item.created_at || ''}} | 更新：${{item.updated_at || ''}}`, 'task-meta'));
        list.appendChild(card);
      }});
      if (tasksPollTimer) clearTimeout(tasksPollTimer);
      if (hasRunning) {{
        tasksPollTimer = window.setTimeout(loadTasks, 2000);
      }}
    }}

    async function loadUploadJobs() {{
      const list = document.getElementById('publish_tasks');
      const res = await fetch('/api/publishers/upload-jobs?limit=20');
      const data = await res.json();
      clearNode(list);
      if (!Array.isArray(data) || !data.length) {{
        list.appendChild(createNode('div', '还没有发布任务。', 'task-card empty-state'));
        return;
      }}
      let hasRunning = false;
      data.forEach((item) => {{
        if (item.status === 'pending' || item.status === 'running') {{
          hasRunning = true;
        }}
        const card = createNode('article', '', 'task-card');
        card.appendChild(createNode('strong', `${{item.display_name}} · ${{item.status}}`));
        card.appendChild(createNode('div', `《${{item.book_name}}》 · ${{item.chapter_title}}`, 'task-meta'));
        card.appendChild(createNode('div', `执行端：${{item.extension_client_id || '待领取'}}`, 'task-meta'));
        card.appendChild(createNode('div', `创建：${{item.created_at || ''}}`, 'task-meta'));
        if (item.message) {{
          card.appendChild(createNode('div', item.message, 'task-meta'));
        }}
        if (item.error) {{
          card.appendChild(createNode('div', `错误：${{item.error}}`, 'task-meta'));
        }}
        if (item.current_url) {{
          const link = document.createElement('a');
          link.href = item.current_url;
          link.target = '_blank';
          link.rel = 'noreferrer';
          link.textContent = item.current_url;
          card.appendChild(link);
        }}
        list.appendChild(card);
      }});
      if (uploadsPollTimer) clearTimeout(uploadsPollTimer);
      if (hasRunning) {{
        uploadsPollTimer = window.setTimeout(loadUploadJobs, 2500);
      }}
    }}

    async function createPublishJob(projectId, publish) {{
      const chapterSelect = document.getElementById(`publish_chapter_${{projectId}}`);
      const platformSelect = document.getElementById(`publish_platform_${{projectId}}`);
      const bookInput = document.getElementById(`publish_book_${{projectId}}`);
      const createCheckbox = document.getElementById(`publish_create_${{projectId}}`);
      const status = document.getElementById(`publish_status_${{projectId}}`);
      if (!chapterSelect || !chapterSelect.value) {{
        status.textContent = '请先选择章节。';
        status.className = 'status warn';
        return;
      }}
      const payload = {{
        platform: platformSelect.value,
        chapter_number: Number(chapterSelect.value),
        book_name: bookInput.value.trim(),
        publish: Boolean(publish),
        create_if_missing: Boolean(createCheckbox.checked),
      }};
      if (!payload.book_name) {{
        status.textContent = '请先填写平台作品名。';
        status.className = 'status warn';
        return;
      }}
      status.textContent = '正在创建发布任务...';
      status.className = 'status';
      const res = await fetch(`/api/projects/${{projectId}}/publishers/upload-jobs`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!res.ok) {{
        status.textContent = data.detail || '创建发布任务失败';
        status.className = 'status error';
        return;
      }}
      status.textContent = `发布任务已创建：${{data.job_id}}\\n${{data.message || ''}}`;
      status.className = 'status ok';
      loadUploadJobs();
    }}

    document.getElementById('profile_picker').addEventListener('change', (event) => fillProfileForm(event.target.value));
    document.getElementById('generate_profile_id').addEventListener('change', updateGenerateProfileMeta);
    document.getElementById('saved_operation_mode').addEventListener('change', updateModeHints);
    document.getElementById('operation_mode').addEventListener('change', updateModeHints);
    updateModeHints();
    setTaskView('all');
    loadSettings();
    loadPlatforms();
    loadTasks();
    loadUploadJobs();
    loadProjects();
  </script>
</body>
</html>
        """


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
