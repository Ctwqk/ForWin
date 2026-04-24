import {
  AlertTriangle,
  BookOpen,
  Check,
  Database,
  Download,
  FileText,
  GitBranch,
  RefreshCw,
  Search,
  ShieldAlert,
  Upload,
  X
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";

type ProjectSummary = {
  id: string;
  title: string;
  genre: string;
  creation_status?: string;
};

type WorldModelSnapshotInfo = {
  id: string;
  project_id: string;
  as_of_chapter: number;
  version: number;
  status: string;
  source_digest: string;
  snapshot: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

type WorldModelPageInfo = {
  id: string;
  project_id: string;
  page_key: string;
  page_type: string;
  title: string;
  vault_path: string;
  markdown: string;
  frontmatter: Record<string, unknown>;
  content_hash: string;
  revision: number;
  status: string;
  as_of_chapter: number;
  updated_at: string;
};

type WorldModelConflictInfo = {
  id: string;
  conflict_type: string;
  severity: string;
  subject_key: string;
  description: string;
  status: string;
  created_at: string;
};

type WorldEditProposalInfo = {
  id: string;
  source: string;
  target_page_key: string;
  target_field: string;
  proposed_patch: Record<string, unknown>;
  reason: string;
  status: string;
  created_by: string;
  created_at: string;
  reviewed_at: string;
};

type ExportResponse = {
  ok: boolean;
  vault_root: string;
  exported_count: number;
  message: string;
};

type ImportResponse = {
  ok: boolean;
  vault_root: string;
  proposal_count: number;
  changed_paths: string[];
  message: string;
};

type TabKey = "pages" | "conflicts" | "proposals";

async function apiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    },
    ...init
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

function shortDigest(value: string): string {
  return value ? value.slice(0, 12) : "none";
}

function statusLabel(value: string): string {
  if (value === "pending") return "待审核";
  if (value === "accepted") return "已接受";
  if (value === "rejected") return "已拒绝";
  if (value === "superseded") return "已取代";
  return value || "unknown";
}

function snapshotTitle(snapshot: WorldModelSnapshotInfo | null): string {
  if (!snapshot) return "暂无快照";
  return `第 ${snapshot.as_of_chapter} 章后 · v${snapshot.version}`;
}

export default function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectId, setProjectId] = useState(() => new URLSearchParams(window.location.search).get("project_id") ?? "");
  const [snapshots, setSnapshots] = useState<WorldModelSnapshotInfo[]>([]);
  const [latest, setLatest] = useState<WorldModelSnapshotInfo | null>(null);
  const [pages, setPages] = useState<WorldModelPageInfo[]>([]);
  const [conflicts, setConflicts] = useState<WorldModelConflictInfo[]>([]);
  const [proposals, setProposals] = useState<WorldEditProposalInfo[]>([]);
  const [selectedPageKey, setSelectedPageKey] = useState("");
  const [query, setQuery] = useState("");
  const [pageType, setPageType] = useState("all");
  const [tab, setTab] = useState<TabKey>("pages");
  const [vaultRoot, setVaultRoot] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    void loadProjects();
  }, []);

  useEffect(() => {
    if (!projectId && projects.length > 0) {
      setProjectId(projects[0].id);
    }
  }, [projectId, projects]);

  useEffect(() => {
    if (!projectId) return;
    const url = new URL(window.location.href);
    url.searchParams.set("project_id", projectId);
    window.history.replaceState(null, "", url.toString());
    void refreshWorldModel(projectId);
  }, [projectId]);

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === projectId) ?? null,
    [projectId, projects]
  );

  const pageTypes = useMemo(() => {
    const values = Array.from(new Set(pages.map((page) => page.page_type))).sort();
    return ["all", ...values];
  }, [pages]);

  const filteredPages = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return pages.filter((page) => {
      const matchesType = pageType === "all" || page.page_type === pageType;
      const haystack = `${page.title} ${page.page_key} ${page.page_type}`.toLowerCase();
      return matchesType && (!needle || haystack.includes(needle));
    });
  }, [pages, pageType, query]);

  const selectedPage = useMemo(() => {
    return pages.find((page) => page.page_key === selectedPageKey) ?? filteredPages[0] ?? null;
  }, [filteredPages, pages, selectedPageKey]);

  const pendingProposals = proposals.filter((proposal) => proposal.status === "pending");
  const openConflicts = conflicts.filter((conflict) => conflict.status === "open");

  async function loadProjects() {
    setError("");
    try {
      const data = await apiJson<ProjectSummary[]>("/api/projects");
      setProjects(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "项目列表加载失败");
    }
  }

  async function refreshWorldModel(nextProjectId = projectId) {
    if (!nextProjectId) return;
    setBusy(true);
    setError("");
    try {
      const pageRows = await apiJson<WorldModelPageInfo[]>(`/api/projects/${nextProjectId}/world-model/pages`);
      const [snapshotRows, conflictRows, proposalRows] = await Promise.all([
        apiJson<WorldModelSnapshotInfo[]>(`/api/projects/${nextProjectId}/world-model/snapshots`),
        apiJson<WorldModelConflictInfo[]>(`/api/projects/${nextProjectId}/world-model/conflicts`),
        apiJson<WorldEditProposalInfo[]>(`/api/projects/${nextProjectId}/world-model/proposals`)
      ]);
      setSnapshots(snapshotRows);
      setLatest(snapshotRows[0] ?? null);
      setPages(pageRows);
      setConflicts(conflictRows);
      setProposals(proposalRows);
      if (!selectedPageKey && pageRows.length > 0) {
        setSelectedPageKey(pageRows[0].page_key);
      }
      setMessage("WorldModel 已刷新。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "WorldModel 加载失败");
    } finally {
      setBusy(false);
    }
  }

  async function exportVault(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiJson<ExportResponse>(`/api/projects/${projectId}/world-model/export-obsidian`, {
        method: "POST",
        body: JSON.stringify({ vault_root: vaultRoot })
      });
      setVaultRoot(result.vault_root);
      setMessage(result.message || `已导出 ${result.exported_count} 个页面。`);
      await refreshWorldModel(projectId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Obsidian 导出失败");
    } finally {
      setBusy(false);
    }
  }

  async function importVault() {
    if (!projectId) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiJson<ImportResponse>(`/api/projects/${projectId}/world-model/import-obsidian`, {
        method: "POST",
        body: JSON.stringify({ vault_root: vaultRoot })
      });
      setVaultRoot(result.vault_root);
      setMessage(result.message || `已生成 ${result.proposal_count} 个 proposal。`);
      await refreshWorldModel(projectId);
      setTab("proposals");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Obsidian 导入失败");
    } finally {
      setBusy(false);
    }
  }

  async function reviewProposal(proposalId: string, status: "accepted" | "rejected") {
    if (!projectId) return;
    setBusy(true);
    setError("");
    try {
      await apiJson<WorldEditProposalInfo>(
        `/api/projects/${projectId}/world-model/proposals/${proposalId}/review`,
        {
          method: "POST",
          body: JSON.stringify({ status, reason: status === "accepted" ? "World Studio accepted." : "World Studio rejected." })
        }
      );
      setMessage(status === "accepted" ? "Proposal 已接受。" : "Proposal 已拒绝。");
      await refreshWorldModel(projectId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Proposal 审核失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <nav className="studio-nav" aria-label="ForWin primary navigation">
            <a href="/">创作台</a>
            <a href="/world-studio" aria-current="page">
              World Studio
            </a>
          </nav>
          <p className="eyebrow">ForWin V3</p>
          <h1>World Studio</h1>
        </div>
        <div className="topbar-actions">
          <label className="project-picker">
            <span>Project</span>
            <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
              <option value="">选择项目</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.title || project.id}
                </option>
              ))}
            </select>
          </label>
          <button className="icon-button" type="button" onClick={() => refreshWorldModel()} disabled={busy || !projectId}>
            <RefreshCw size={16} />
            刷新
          </button>
        </div>
      </header>

      <section className="status-grid">
        <Metric icon={<Database size={18} />} label="Snapshot" value={snapshotTitle(latest)} detail={shortDigest(latest?.source_digest ?? "")} />
        <Metric icon={<FileText size={18} />} label="Pages" value={String(pages.length)} detail={`${pageTypes.length - 1} types`} />
        <Metric icon={<ShieldAlert size={18} />} label="Conflicts" value={String(openConflicts.length)} detail="open" tone={openConflicts.length ? "warn" : "ok"} />
        <Metric icon={<GitBranch size={18} />} label="Proposals" value={String(pendingProposals.length)} detail="pending" tone={pendingProposals.length ? "warn" : "ok"} />
      </section>

      {selectedProject ? (
        <section className="project-strip">
          <div>
            <strong>{selectedProject.title}</strong>
            <span>{selectedProject.genre}</span>
          </div>
          <span>{selectedProject.id}</span>
        </section>
      ) : null}

      <section className="toolbar">
        <form className="vault-form" onSubmit={exportVault}>
          <label htmlFor="vault_root">Vault path</label>
          <input
            id="vault_root"
            value={vaultRoot}
            onChange={(event) => setVaultRoot(event.target.value)}
            placeholder="默认 data/world_vaults/{project_id}"
            spellCheck={false}
          />
          <button type="submit" disabled={busy || !projectId}>
            <Download size={16} />
            导出
          </button>
          <button type="button" onClick={importVault} disabled={busy || !projectId}>
            <Upload size={16} />
            导入 proposal
          </button>
        </form>
      </section>

      {message ? <div className="notice success">{message}</div> : null}
      {error ? <div className="notice error">{error}</div> : null}

      <div className="workspace">
        <aside className="sidebar">
          <div className="tabs" role="tablist" aria-label="World Studio sections">
            <button className={tab === "pages" ? "active" : ""} type="button" onClick={() => setTab("pages")}>
              <BookOpen size={16} />
              页面
            </button>
            <button className={tab === "conflicts" ? "active" : ""} type="button" onClick={() => setTab("conflicts")}>
              <AlertTriangle size={16} />
              矛盾
            </button>
            <button className={tab === "proposals" ? "active" : ""} type="button" onClick={() => setTab("proposals")}>
              <GitBranch size={16} />
              Proposal
            </button>
          </div>

          {tab === "pages" ? (
            <>
              <div className="search-row">
                <Search size={16} />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索页面" />
              </div>
              <select value={pageType} onChange={(event) => setPageType(event.target.value)}>
                {pageTypes.map((type) => (
                  <option key={type} value={type}>
                    {type === "all" ? "全部类型" : type}
                  </option>
                ))}
              </select>
              <div className="list-scroll">
                {filteredPages.map((page) => (
                  <button
                    className={selectedPage?.page_key === page.page_key ? "page-item active" : "page-item"}
                    key={page.id}
                    type="button"
                    onClick={() => setSelectedPageKey(page.page_key)}
                  >
                    <span>{page.title}</span>
                    <small>{page.page_type} · ch {page.as_of_chapter}</small>
                  </button>
                ))}
              </div>
            </>
          ) : null}

          {tab === "conflicts" ? <ConflictList conflicts={conflicts} /> : null}
          {tab === "proposals" ? <ProposalList proposals={proposals} onReview={reviewProposal} busy={busy} /> : null}
        </aside>

        <section className="main-panel">
          {tab === "pages" ? <PageDetail page={selectedPage} snapshots={snapshots} /> : null}
          {tab === "conflicts" ? <ConflictDetail conflicts={conflicts} /> : null}
          {tab === "proposals" ? <ProposalDetail proposals={proposals} /> : null}
        </section>
      </div>
    </main>
  );
}

function Metric({
  icon,
  label,
  value,
  detail,
  tone = "neutral"
}: {
  icon: ReactNode;
  label: string;
  value: string;
  detail: string;
  tone?: "neutral" | "ok" | "warn";
}) {
  return (
    <div className={`metric ${tone}`}>
      <div className="metric-icon">{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        <small>{detail}</small>
      </div>
    </div>
  );
}

function PageDetail({ page, snapshots }: { page: WorldModelPageInfo | null; snapshots: WorldModelSnapshotInfo[] }) {
  if (!page) {
    return <EmptyState title="还没有 WorldModel 页面" text="锁定 Genesis 或导出 Obsidian 时会自动 bootstrap 第 0 章世界模型。" />;
  }
  return (
    <>
      <div className="panel-head">
        <div>
          <p className="eyebrow">{page.page_type}</p>
          <h2>{page.title}</h2>
        </div>
        <div className="pill-row">
          <span>{page.status}</span>
          <span>rev {page.revision}</span>
          <span>ch {page.as_of_chapter}</span>
        </div>
      </div>
      <div className="detail-grid">
        <section className="summary-panel">
          <h3>Metadata</h3>
          <dl>
            <dt>Page Key</dt>
            <dd>{page.page_key}</dd>
            <dt>Vault Path</dt>
            <dd>{page.vault_path}</dd>
            <dt>Content Hash</dt>
            <dd>{shortDigest(page.content_hash)}</dd>
          </dl>
        </section>
        <section className="summary-panel">
          <h3>Snapshot Timeline</h3>
          <ol className="timeline">
            {snapshots.slice(0, 8).map((snapshot) => (
              <li key={snapshot.id}>
                <strong>ch {snapshot.as_of_chapter}</strong>
                <span>v{snapshot.version}</span>
                <small>{shortDigest(snapshot.source_digest)}</small>
              </li>
            ))}
          </ol>
        </section>
      </div>
      <section className="markdown-panel">
        <h3>Markdown Projection</h3>
        <pre>{page.markdown}</pre>
      </section>
    </>
  );
}

function ConflictList({ conflicts }: { conflicts: WorldModelConflictInfo[] }) {
  if (conflicts.length === 0) return <EmptyState title="没有冲突" text="确定性 conflict detector 暂未发现 open issue。" compact />;
  return (
    <div className="list-scroll">
      {conflicts.map((conflict) => (
        <div className={`conflict-item ${conflict.severity}`} key={conflict.id}>
          <strong>{conflict.conflict_type}</strong>
          <span>{conflict.subject_key || "global"}</span>
          <small>{conflict.status}</small>
        </div>
      ))}
    </div>
  );
}

function ConflictDetail({ conflicts }: { conflicts: WorldModelConflictInfo[] }) {
  if (conflicts.length === 0) {
    return <EmptyState title="Conflict List" text="WorldModel 编译后会在这里展示死亡后行动、地点冲突、秘密提前揭示等确定性问题。" />;
  }
  return (
    <>
      <div className="panel-head">
        <div>
          <p className="eyebrow">Quality Model</p>
          <h2>Conflict List</h2>
        </div>
      </div>
      <div className="table-list">
        {conflicts.map((conflict) => (
          <article key={conflict.id}>
            <header>
              <strong>{conflict.conflict_type}</strong>
              <span className={`severity ${conflict.severity}`}>{conflict.severity}</span>
            </header>
            <p>{conflict.description || "无描述"}</p>
            <footer>
              <span>{conflict.subject_key || "global"}</span>
              <span>{conflict.created_at}</span>
            </footer>
          </article>
        ))}
      </div>
    </>
  );
}

function ProposalList({
  proposals,
  onReview,
  busy
}: {
  proposals: WorldEditProposalInfo[];
  onReview: (proposalId: string, status: "accepted" | "rejected") => void;
  busy: boolean;
}) {
  if (proposals.length === 0) return <EmptyState title="没有 proposal" text="Obsidian 导入不会直接改 canon，只会在这里生成待审记录。" compact />;
  return (
    <div className="list-scroll">
      {proposals.map((proposal) => (
        <div className="proposal-item" key={proposal.id}>
          <strong>{proposal.target_page_key}</strong>
          <span>{statusLabel(proposal.status)}</span>
          {proposal.status === "pending" ? (
            <div className="inline-actions">
              <button type="button" onClick={() => onReview(proposal.id, "accepted")} disabled={busy} title="接受 proposal">
                <Check size={14} />
              </button>
              <button type="button" onClick={() => onReview(proposal.id, "rejected")} disabled={busy} title="拒绝 proposal">
                <X size={14} />
              </button>
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function ProposalDetail({ proposals }: { proposals: WorldEditProposalInfo[] }) {
  if (proposals.length === 0) {
    return <EmptyState title="Proposal Review" text="从 Obsidian 导入的页面修改会停在 proposal 层，必须人工 accept 或 reject。" />;
  }
  return (
    <>
      <div className="panel-head">
        <div>
          <p className="eyebrow">Obsidian Import</p>
          <h2>Proposal Review</h2>
        </div>
      </div>
      <div className="table-list">
        {proposals.map((proposal) => (
          <article key={proposal.id}>
            <header>
              <strong>{proposal.target_page_key}</strong>
              <span>{statusLabel(proposal.status)}</span>
            </header>
            <p>{proposal.reason || "无说明"}</p>
            <pre>{JSON.stringify(proposal.proposed_patch, null, 2)}</pre>
          </article>
        ))}
      </div>
    </>
  );
}

function EmptyState({ title, text, compact = false }: { title: string; text: string; compact?: boolean }) {
  return (
    <div className={compact ? "empty compact" : "empty"}>
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  );
}
