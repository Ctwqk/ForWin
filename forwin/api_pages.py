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
    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ForWin 创作台</title>
  <style>
    :root {{
      --paper:#f7f1e7;
      --panel:#fffaf3;
      --ink:#1d2522;
      --muted:#6e6250;
      --accent:#b24a28;
      --accent-2:#284d46;
      --line:#e7d8c4;
      --shadow:0 24px 70px rgba(68,46,24,.10);
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0;
      color:var(--ink);
      font-family:"Noto Serif SC","Source Han Serif SC",serif;
      background:
        radial-gradient(circle at top left, rgba(255,248,224,.95), transparent 33%),
        radial-gradient(circle at 85% 15%, rgba(219,118,63,.13), transparent 22%),
        linear-gradient(140deg, #f2eadb, #eadfcb 58%, #efe7da);
    }}
    .wrap {{ max-width:1180px; margin:0 auto; padding:36px 20px 72px; }}
    .hero {{
      display:grid; grid-template-columns:1.2fr .95fr; gap:22px; align-items:stretch;
    }}
    .panel {{
      background:rgba(255,250,243,.9);
      border:1px solid var(--line);
      border-radius:24px;
      box-shadow:var(--shadow);
      backdrop-filter:blur(8px);
    }}
    .lead {{
      padding:34px 32px;
      min-height:320px;
      display:flex;
      flex-direction:column;
      justify-content:space-between;
      background:
        linear-gradient(180deg, rgba(255,255,255,.28), rgba(255,255,255,0)),
        radial-gradient(circle at bottom right, rgba(178,74,40,.11), transparent 30%);
    }}
    .eyebrow {{
      display:inline-flex;
      align-items:center;
      gap:8px;
      border:1px solid rgba(178,74,40,.2);
      border-radius:999px;
      padding:7px 12px;
      color:var(--accent);
      font-size:13px;
      letter-spacing:.06em;
    }}
    h1 {{ margin:16px 0 10px; font-size:44px; line-height:1.06; }}
    .lead p {{ margin:0; font-size:16px; line-height:1.8; color:var(--muted); }}
    .hero-actions {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:24px; }}
    a.button, button {{
      appearance:none;
      border:0;
      border-radius:999px;
      padding:12px 18px;
      cursor:pointer;
      font:inherit;
      text-decoration:none;
      transition:transform .18s ease, box-shadow .18s ease, opacity .18s ease;
    }}
    a.button:hover, button:hover {{ transform:translateY(-1px); }}
    .primary {{
      color:#fff;
      background:linear-gradient(135deg, var(--accent), #cc6a3d);
      box-shadow:0 16px 30px rgba(178,74,40,.24);
    }}
    .secondary {{
      color:var(--accent-2);
      background:rgba(40,77,70,.08);
      border:1px solid rgba(40,77,70,.16);
    }}
    .form {{
      padding:28px 24px;
      display:grid;
      gap:14px;
      align-content:start;
    }}
    .form h2 {{ margin:0 0 4px; font-size:24px; }}
    .hint {{ color:var(--muted); font-size:14px; line-height:1.6; }}
    .grid {{
      display:grid;
      grid-template-columns:repeat(2, minmax(0, 1fr));
      gap:14px;
    }}
    label {{ display:block; font-size:14px; margin-bottom:6px; color:var(--muted); }}
    input, textarea, select {{
      width:100%;
      border:1px solid var(--line);
      border-radius:16px;
      padding:12px 14px;
      background:#fffdfa;
      color:var(--ink);
      font:inherit;
    }}
    textarea {{ min-height:180px; resize:vertical; }}
    .stats {{
      display:grid;
      grid-template-columns:repeat(3, minmax(0,1fr));
      gap:14px;
      margin-top:22px;
    }}
    .stat {{
      padding:16px 18px;
      border-radius:18px;
      background:rgba(255,255,255,.45);
      border:1px solid rgba(231,216,196,.8);
    }}
    .stat b {{ display:block; font-size:26px; margin-bottom:6px; }}
    .row {{
      display:flex;
      gap:12px;
      flex-wrap:wrap;
      align-items:center;
    }}
    .status {{
      margin-top:8px;
      padding:14px 16px;
      border-radius:16px;
      background:rgba(255,255,255,.54);
      border:1px solid var(--line);
      color:var(--muted);
      min-height:56px;
      line-height:1.6;
      white-space:pre-wrap;
    }}
    .projects {{
      margin-top:26px;
      padding:24px;
    }}
    .projects-head {{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
      margin-bottom:12px;
    }}
    .project-list {{
      display:grid;
      grid-template-columns:repeat(auto-fit, minmax(240px, 1fr));
      gap:14px;
    }}
    .project-card {{
      padding:18px;
      border-radius:18px;
      border:1px solid var(--line);
      background:rgba(255,255,255,.58);
    }}
    .project-card h3 {{ margin:0 0 8px; font-size:19px; }}
    .project-card p {{ margin:0 0 8px; color:var(--muted); line-height:1.6; }}
    .muted {{ color:var(--muted); }}
    @media (max-width: 900px) {{
      .hero {{ grid-template-columns:1fr; }}
      .grid, .stats {{ grid-template-columns:1fr; }}
      h1 {{ font-size:36px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <article class="panel lead">
        <div>
          <span class="eyebrow">FORWIN · Longform Web Novel Studio</span>
          <h1>从设定到章节，把长篇连载真正跑起来</h1>
          <p>这里可以直接发起生成任务，也可以跳到平台发布页做番茄 / 起点登录与上传。默认已经指向 MiniMax 的 OpenAI 兼容接口，你只需要在网页上填自己的 API Key 就能开始。</p>
          <div class="stats">
            <div class="stat">
              <b>MiniMax</b>
              <span class="muted">默认 Base URL 已预填</span>
            </div>
            <div class="stat">
              <b>Single</b>
              <span class="muted">阶段 0.5 默认先走单章草稿流程，优先保证生成闭环</span>
            </div>
            <div class="stat">
              <b>Phase 2</b>
              <span class="muted">支持项目生成、查询与发布联调</span>
            </div>
          </div>
        </div>
        <div class="hero-actions">
          <a class="button primary" href="#generator">开始生成</a>
          <a class="button secondary" href="/publishers">去平台登录与上传</a>
        </div>
      </article>
      <section class="panel form" id="generator">
        <div>
          <h2>生成新项目</h2>
          <div class="hint">你可以先把 API Key 保存成默认配置，也可以只在这一次生成里临时覆盖。Base URL 与模型名已经按 MiniMax M2.7 预填好了。</div>
        </div>
        <div class="panel" style="padding:18px 18px 12px; border-radius:18px; box-shadow:none; background:rgba(255,255,255,.45);">
          <div class="row" style="justify-content:space-between; align-items:flex-start;">
            <div>
              <h3 style="margin:0 0 6px; font-size:18px;">默认模型设置</h3>
              <div class="hint">保存后，后续生成会自动使用这份配置。这样你设置完以后，我也可以直接继续联调测试。</div>
            </div>
            <div id="saved_badge" class="hint">已保存 API Key：{"是" if has_api_key else "否"}</div>
          </div>
          <div class="grid" style="margin-top:12px;">
            <div>
              <label for="saved_api_key">已保存 API Key</label>
              <input id="saved_api_key" type="password" placeholder="sk-..." autocomplete="off">
            </div>
            <div>
              <label for="saved_model">已保存 Model</label>
              <input id="saved_model" value="{model}" spellcheck="false">
            </div>
          </div>
          <div class="grid" style="margin-top:6px;">
            <div>
              <label for="saved_operation_mode">运行模式</label>
              <select id="saved_operation_mode">
                <option value="blackbox" {"selected" if operation_mode == "blackbox" else ""}>黑箱模式</option>
                <option value="checkpoint" {"selected" if operation_mode == "checkpoint" else ""}>检查点模式</option>
                <option value="copilot" {"selected" if operation_mode == "copilot" else ""}>共驾模式</option>
              </select>
              <div id="saved_mode_hint" class="hint" style="margin-top:6px;"></div>
            </div>
            <div style="display:flex;align-items:end;">
              <label style="display:flex; gap:10px; align-items:flex-start; margin:0;">
                <input id="saved_freeze_failed_candidates" type="checkbox" style="width:auto; margin-top:4px;" {"checked" if freeze_failed_candidates else ""}>
                <span>状态写入失败时冻结 candidate artifact</span>
              </label>
            </div>
          </div>
          <div style="margin-top:6px;">
            <label for="saved_base_url">已保存 Base URL</label>
            <input id="saved_base_url" value="{base_url}" spellcheck="false">
          </div>
          <div class="row" style="margin-top:10px;">
            <button class="secondary" onclick="saveSettings()">保存默认配置</button>
            <button class="secondary" onclick="loadSettings()">重新读取配置</button>
          </div>
          <div id="settings_status" class="status" style="margin-top:12px;">默认配置尚未修改。</div>
        </div>
        <div>
          <label for="api_key">MiniMax API Key</label>
          <input id="api_key" type="password" placeholder="sk-..." autocomplete="off">
        </div>
        <div class="grid">
          <div>
            <label for="base_url">Base URL</label>
            <input id="base_url" value="{base_url}" spellcheck="false">
          </div>
          <div>
            <label for="model">Model</label>
            <input id="model" value="{model}" spellcheck="false">
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
            <label for="num_chapters">章节数</label>
            <input id="num_chapters" type="number" min="1" max="20" value="{default_chapters}">
          </div>
        </div>
        <div class="grid">
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

    <section class="panel projects">
      <div class="projects-head">
        <div>
          <h2 style="margin:0 0 6px; font-size:24px;">已有项目</h2>
          <div class="hint">任务完成后会出现在这里，方便你继续查看章节或去发布页操作。</div>
        </div>
        <a class="button secondary" href="/publishers">平台发布页</a>
      </div>
      <div id="project_list" class="project-list"></div>
    </section>
  </div>
  <script>
    let currentTaskId = null;

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
        api_key: document.getElementById('api_key').value.trim() || null,
        base_url: document.getElementById('base_url').value.trim(),
        model: document.getElementById('model').value.trim(),
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
    }}

    async function loadSettings() {{
      const res = await fetch('/api/settings/llm');
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('settings_status').textContent = data.detail || '读取配置失败';
        return;
      }}
      document.getElementById('saved_api_key').value = '';
      document.getElementById('saved_base_url').value = data.base_url;
      document.getElementById('saved_model').value = data.model;
      document.getElementById('saved_operation_mode').value = data.operation_mode;
      document.getElementById('saved_freeze_failed_candidates').checked = Boolean(data.freeze_failed_candidates);
      document.getElementById('base_url').value = data.base_url;
      document.getElementById('model').value = data.model;
      document.getElementById('operation_mode').value = data.operation_mode;
      document.getElementById('freeze_failed_candidates').checked = Boolean(data.freeze_failed_candidates);
      document.getElementById('saved_badge').textContent = `已保存 API Key：${{data.has_api_key ? '是' : '否'}}`;
      document.getElementById('settings_status').textContent = data.message || '已读取默认配置';
      updateModeHints();
    }}

    async function saveSettings() {{
      const payload = {{
        api_key: document.getElementById('saved_api_key').value.trim(),
        base_url: document.getElementById('saved_base_url').value.trim(),
        model: document.getElementById('saved_model').value.trim(),
        operation_mode: document.getElementById('saved_operation_mode').value,
        freeze_failed_candidates: document.getElementById('saved_freeze_failed_candidates').checked,
      }};
      const res = await fetch('/api/settings/llm', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById('settings_status').textContent = data.detail || '保存失败';
        return;
      }}
      document.getElementById('saved_api_key').value = '';
      document.getElementById('base_url').value = data.base_url;
      document.getElementById('model').value = data.model;
      document.getElementById('operation_mode').value = data.operation_mode;
      document.getElementById('freeze_failed_candidates').checked = Boolean(data.freeze_failed_candidates);
      document.getElementById('saved_badge').textContent = `已保存 API Key：${{data.has_api_key ? '是' : '否'}}`;
      document.getElementById('settings_status').textContent = data.message || '已保存默认配置';
      updateModeHints();
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
        card.appendChild(createNode('h3', item.title || '未命名项目'));
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

    document.getElementById('saved_operation_mode').addEventListener('change', updateModeHints);
    document.getElementById('operation_mode').addEventListener('change', updateModeHints);
    updateModeHints();
    loadSettings();
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
    :root {{ --bg:#f4efe6; --card:#fffaf1; --ink:#1f2a24; --accent:#b64b2a; --line:#e7d7bf; --muted:#755b3d; --ok:#226b37; --warn:#a25920; }}
    body {{ margin:0; font-family:"Noto Serif SC","Source Han Serif SC",serif; background:
      radial-gradient(circle at top left,#fff6df 0,transparent 35%),
      linear-gradient(135deg,#f4efe6,#efe2cf); color:var(--ink); }}
    .wrap {{ max-width:1100px; margin:0 auto; padding:40px 20px 80px; }}
    h1 {{ margin:0 0 8px; font-size:40px; }}
    p {{ line-height:1.6; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:18px; margin-top:28px; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:20px; padding:22px; box-shadow:0 18px 50px rgba(73,52,28,.08); }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }}
    button {{ border:0; background:var(--accent); color:#fff; border-radius:999px; padding:10px 16px; cursor:pointer; }}
    button.secondary {{ background:#e8d9c1; color:#5b4630; }}
    .muted {{ color:var(--muted); }}
    input, textarea, select {{ width:100%; margin-top:8px; margin-bottom:12px; border:1px solid var(--line); border-radius:12px; padding:10px 12px; background:#fff; font:inherit; box-sizing:border-box; }}
    textarea {{ min-height:180px; resize:vertical; }}
    .status {{ margin-top:10px; font-size:14px; color:var(--muted); white-space:pre-wrap; }}
    .ok {{ color:var(--ok); }}
    .warn {{ color:var(--warn); }}
    .summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; margin-top:18px; }}
    .task-list {{ display:grid; gap:12px; margin-top:16px; }}
    .task-item {{ border:1px solid var(--line); border-radius:16px; padding:16px; background:#fffdf8; }}
    .task-item strong {{ display:block; margin-bottom:6px; }}
    .task-item a {{ color:var(--accent); }}
    code {{ font-family:"SFMono-Regular","Consolas",monospace; font-size:13px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>平台发布管理</h1>
    <p>ForWin 现在把登录、上传和后续评论采集都交给浏览器扩展执行。后端只负责保存任务、接收扩展心跳和持久化结果，不再在服务器里偷偷跑浏览器。</p>
    <div class="summary-grid">
      <div class="card">
        <h2 style="margin-top:0;">扩展状态</h2>
        <div id="extension_summary" class="status">正在检测浏览器扩展...</div>
        <div class="actions">
          <button class="secondary" onclick="openExtensionOptions()">打开扩展设置</button>
          <a href="/api/publishers/extension-package" style="display:inline-flex;align-items:center;text-decoration:none;border:0;background:#e8d9c1;color:#5b4630;border-radius:999px;padding:10px 16px;">下载扩展包</a>
        </div>
      </div>
      <div class="card">
        <h2 style="margin-top:0;">安装提示</h2>
        <p class="muted">如果你是用 macOS 浏览器访问这台 Linux 后端，请先把扩展包下载到你的 Mac，再解压后用开发者模式加载。不要直接把 Linux 服务器路径当成本机路径使用。</p>
        <p><code>{extension_install_path}</code></p>
        <p class="muted">扩展里要填当前 ForWin 后端 URL 和共享 API Key。页面只通过扩展桥接登录，不再回退到旧的服务端扫码逻辑。首次安装时，请先在浏览器扩展管理页手动打开扩展选项；页面里的“打开扩展设置”按钮要等扩展已经完成首次配置后才会生效。</p>
      </div>
    </div>
    <div id="platforms" class="grid"></div>
    <div class="card" style="margin-top:22px;">
      <h2>上传任务</h2>
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
    <div class="card" style="margin-top:22px;">
      <h2>最近上传任务</h2>
      <div id="upload_jobs_status" class="status">正在加载任务列表...</div>
      <div id="upload_jobs_list" class="task-list"></div>
    </div>
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
