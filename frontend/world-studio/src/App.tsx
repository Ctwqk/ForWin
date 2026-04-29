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
  UserRound,
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

type PersonalitySkillInfo = {
  name: string;
  version: string;
  description: string;
  skill_type: string;
  path: string;
  incomplete?: boolean;
};

type CharacterPersonalityInfo = {
  character_id: string;
  character_name: string;
  personality_loadout: Record<string, unknown>;
};

type PersonalityCatalogResponse = {
  skills: PersonalitySkillInfo[];
};

type CharacterPersonalityResponse = {
  characters: CharacterPersonalityInfo[];
};

type PersonalityCoverageCharacter = {
  character_id: string;
  character_name: string;
  assignment_mode: string;
  assignment_status: string;
  manual_override: boolean;
  issues: string[];
};

type PersonalityCoverageResponse = {
  character_count: number;
  with_valid_loadout: number;
  missing_loadout: number;
  fallback_used: number;
  manual_override: number;
  needs_review: number;
  coverage_ratio: number;
  issue_counts: Record<string, number>;
  characters: PersonalityCoverageCharacter[];
};

type PersonalityAssignmentReportResponse = {
  character_id?: string;
  character_name?: string;
  personality_assignment: Record<string, unknown>;
  decision_events?: Record<string, unknown>[];
};

type PersonalityPreviewResponse = {
  personality_loadout: Record<string, unknown>;
  personality_assignment: Record<string, unknown>;
  validation?: Record<string, unknown>;
};

type ActiveContextPreviewResponse = {
  active_personality_context: Record<string, unknown>;
};

type PersonalityMetricsResponse = {
  character_creation_total: number;
  character_creation_auto_personality_assigned_total: number;
  character_creation_manual_override_total: number;
  character_creation_fallback_used_total: number;
  character_creation_low_confidence_total: number;
  character_integrity_missing_loadout_total: number;
  personality_assignment_confidence_avg: number;
  personality_ooc_issue_total_by_assignment_mode: Record<string, number>;
  most_used_dominant_skills: Array<{ skill: string; count: number }>;
};

type CharacterCreateDraft = {
  name: string;
  aliases: string;
  description: string;
  importance: number;
  publicIdentity: string;
  roleArchetype: string;
  narrativeRole: string;
  factionId: string;
  goal: string;
  personalityTags: string;
};

type PersonalityCoverageFilter =
  | "all"
  | "missing_loadout"
  | "fallback_used"
  | "valid_needs_review"
  | "manual_override"
  | "stress_mode_without_trigger"
  | "social_mask_without_active_when";

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

type TabKey = "pages" | "conflicts" | "proposals" | "personality";

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

function formatLoadout(value: Record<string, unknown>): string {
  return JSON.stringify(value && Object.keys(value).length > 0 ? value : defaultPersonalityLoadout(), null, 2);
}

function defaultPersonalityLoadout(): Record<string, unknown> {
  return {
    dominant: null,
    secondary: [],
    social_mask: [],
    stress_modes: [],
    relationship_patterns: [],
    overrides: {}
  };
}

function defaultCharacterCreateDraft(): CharacterCreateDraft {
  return {
    name: "",
    aliases: "",
    description: "",
    importance: 5,
    publicIdentity: "",
    roleArchetype: "",
    narrativeRole: "",
    factionId: "",
    goal: "",
    personalityTags: ""
  };
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
  const [personalitySkills, setPersonalitySkills] = useState<PersonalitySkillInfo[]>([]);
  const [characterPersonalities, setCharacterPersonalities] = useState<CharacterPersonalityInfo[]>([]);
  const [personalityCoverage, setPersonalityCoverage] = useState<PersonalityCoverageResponse | null>(null);
  const [personalityMetrics, setPersonalityMetrics] = useState<PersonalityMetricsResponse | null>(null);
  const [personalityCoverageFilter, setPersonalityCoverageFilter] = useState<PersonalityCoverageFilter>("all");
  const [selectedCharacterId, setSelectedCharacterId] = useState("");
  const [personalityDraft, setPersonalityDraft] = useState("");
  const [characterCreateDraft, setCharacterCreateDraft] = useState<CharacterCreateDraft>(defaultCharacterCreateDraft);
  const [personalityPreview, setPersonalityPreview] = useState<PersonalityPreviewResponse | null>(null);
  const [createLoadoutDraft, setCreateLoadoutDraft] = useState("");
  const [assignmentReport, setAssignmentReport] = useState<PersonalityAssignmentReportResponse | null>(null);
  const [activeContextPreview, setActiveContextPreview] = useState<ActiveContextPreviewResponse | null>(null);
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

  const filteredCharacterPersonalities = useMemo(() => {
    if (personalityCoverageFilter === "all" || !personalityCoverage) return characterPersonalities;
    const visibleIds = new Set(
      personalityCoverage.characters
        .filter((character) => character.issues.some((issue) => issueMatchesFilter(issue, personalityCoverageFilter)))
        .map((character) => character.character_id)
    );
    return characterPersonalities.filter((character) => visibleIds.has(character.character_id));
  }, [characterPersonalities, personalityCoverage, personalityCoverageFilter]);

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

  async function refreshWorldModel(nextProjectId = projectId, options: { updateMessage?: boolean } = {}) {
    if (!nextProjectId) return;
    const updateMessage = options.updateMessage ?? true;
    setBusy(true);
    setError("");
    try {
      const pageRows = await apiJson<WorldModelPageInfo[]>(`/api/projects/${nextProjectId}/world-model/pages`);
      const [snapshotRows, conflictRows, proposalRows, skillRows, personalityRows, coverageRows, metricsRows] = await Promise.all([
        apiJson<WorldModelSnapshotInfo[]>(`/api/projects/${nextProjectId}/world-model/snapshots`),
        apiJson<WorldModelConflictInfo[]>(`/api/projects/${nextProjectId}/world-model/conflicts`),
        apiJson<WorldEditProposalInfo[]>(`/api/projects/${nextProjectId}/world-model/proposals`),
        apiJson<PersonalityCatalogResponse>("/api/personality-skills"),
        apiJson<CharacterPersonalityResponse>(`/api/projects/${nextProjectId}/book-state/characters/personality`),
        apiJson<PersonalityCoverageResponse>(`/api/projects/${nextProjectId}/characters/personality/coverage`),
        apiJson<PersonalityMetricsResponse>(`/api/projects/${nextProjectId}/characters/personality/metrics`)
      ]);
      setSnapshots(snapshotRows);
      setLatest(snapshotRows[0] ?? null);
      setPages(pageRows);
      setConflicts(conflictRows);
      setProposals(proposalRows);
      setPersonalitySkills(skillRows.skills);
      setCharacterPersonalities(personalityRows.characters);
      setPersonalityCoverage(coverageRows);
      setPersonalityMetrics(metricsRows);
      const nextCharacter =
        personalityRows.characters.find((item) => item.character_id === selectedCharacterId) ??
        personalityRows.characters[0] ??
        null;
      setSelectedCharacterId(nextCharacter?.character_id ?? "");
      setPersonalityDraft(formatLoadout(nextCharacter?.personality_loadout ?? {}));
      setAssignmentReport(null);
      setActiveContextPreview(null);
      if (!selectedPageKey && pageRows.length > 0) {
        setSelectedPageKey(pageRows[0].page_key);
      }
      if (updateMessage) {
        setMessage("WorldModel 已刷新。");
      }
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
      await refreshWorldModel(projectId, { updateMessage: false });
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
      await refreshWorldModel(projectId, { updateMessage: false });
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
      await refreshWorldModel(projectId, { updateMessage: false });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Proposal 审核失败");
    } finally {
      setBusy(false);
    }
  }

  async function savePersonalityLoadout(loadout: Record<string, unknown>) {
    if (!projectId || !selectedCharacterId) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiJson<CharacterPersonalityInfo>(
        `/api/projects/${projectId}/book-state/characters/${selectedCharacterId}/personality-loadout`,
        {
          method: "PUT",
          body: JSON.stringify({ personality_loadout: loadout, reason: "World Studio personality editor." })
        }
      );
      setMessage("人物性格 loadout 已保存。");
      setCharacterPersonalities((current) =>
        current.map((item) => (item.character_id === result.character_id ? result : item))
      );
      setPersonalityDraft(formatLoadout(result.personality_loadout));
    } catch (err) {
      setError(err instanceof Error ? err.message : "人物性格保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function previewCharacterPersonality() {
    if (!projectId) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiJson<PersonalityPreviewResponse>(
        `/api/projects/${projectId}/characters/personality/preview`,
        {
          method: "POST",
          body: JSON.stringify(characterCreatePayload(characterCreateDraft))
        }
      );
      setPersonalityPreview(result);
      setCreateLoadoutDraft(formatLoadout(result.personality_loadout));
      setMessage("自动性格预览已更新。");
    } catch (err) {
      setError(err instanceof Error ? err.message : "自动性格预览失败");
    } finally {
      setBusy(false);
    }
  }

  async function createCharacterFromDraft() {
    if (!projectId || !characterCreateDraft.name.trim()) return;
    const parsed = parseLoadoutDraft(createLoadoutDraft);
    setBusy(true);
    setError("");
    try {
      await apiJson<Record<string, unknown>>(`/api/projects/${projectId}/characters`, {
        method: "POST",
        body: JSON.stringify({
          ...characterCreatePayload(characterCreateDraft),
          personality_loadout: parsed.ok ? parsed.value : null,
          personality_policy: parsed.ok && createLoadoutDraft.trim() ? "manual" : "auto",
          audit_reason: "World Studio character creation."
        })
      });
      setCharacterCreateDraft(defaultCharacterCreateDraft());
      setPersonalityPreview(null);
      setCreateLoadoutDraft("");
      setMessage("人物已创建。");
      await refreshWorldModel(projectId, { updateMessage: false });
    } catch (err) {
      setError(err instanceof Error ? err.message : "人物创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function loadAssignmentReport(characterId = selectedCharacterId) {
    if (!projectId || !characterId) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiJson<PersonalityAssignmentReportResponse>(
        `/api/projects/${projectId}/characters/${characterId}/personality/assignment-report`
      );
      setAssignmentReport(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "assignment report 加载失败");
    } finally {
      setBusy(false);
    }
  }

  async function reassignSelectedCharacter() {
    if (!projectId || !selectedCharacterId) return;
    setBusy(true);
    setError("");
    try {
      await apiJson<Record<string, unknown>>(
        `/api/projects/${projectId}/characters/${selectedCharacterId}/personality/reassign`,
        {
          method: "POST",
          body: JSON.stringify({ mode: "auto_rule", respect_manual_override: true, reason: "World Studio reassign." })
        }
      );
      setMessage("人物性格已重新分配。");
      await refreshWorldModel(projectId, { updateMessage: false });
    } catch (err) {
      setError(err instanceof Error ? err.message : "重新分配失败");
    } finally {
      setBusy(false);
    }
  }

  async function previewActiveContext() {
    if (!projectId || !selectedCharacterId) return;
    const parsed = parseLoadoutDraft(personalityDraft);
    if (!parsed.ok) return;
    const character = characterPersonalities.find((item) => item.character_id === selectedCharacterId);
    setBusy(true);
    setError("");
    try {
      const result = await apiJson<ActiveContextPreviewResponse>(
        `/api/projects/${projectId}/characters/personality/active-context/preview`,
        {
          method: "POST",
          body: JSON.stringify({
            character_id: selectedCharacterId,
            character_name: character?.character_name ?? "",
            personality_loadout: parsed.value,
            scene_flags: ["public_scene"]
          })
        }
      );
      setActiveContextPreview(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "active context preview 失败");
    } finally {
      setBusy(false);
    }
  }

  async function enrichRelationships() {
    if (!projectId) return;
    setBusy(true);
    setError("");
    try {
      await apiJson<Record<string, unknown>>(
        `/api/projects/${projectId}/characters/personality/relationships/enrich`,
        {
          method: "POST",
          body: JSON.stringify({ reason: "World Studio relationship enrichment." })
        }
      );
      setMessage("关系人格 enrichment 已执行。");
      await refreshWorldModel(projectId, { updateMessage: false });
    } catch (err) {
      setError(err instanceof Error ? err.message : "关系人格 enrichment 失败");
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
            <button className={tab === "personality" ? "active" : ""} type="button" onClick={() => setTab("personality")}>
              <UserRound size={16} />
              人物性格
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
          {tab === "personality" ? (
            <>
              <PersonalityCoveragePanel
                coverage={personalityCoverage}
                metrics={personalityMetrics}
                filter={personalityCoverageFilter}
                onFilterChange={setPersonalityCoverageFilter}
              />
              <PersonalityCharacterList
                characters={filteredCharacterPersonalities}
                coverage={personalityCoverage}
                selectedCharacterId={selectedCharacterId}
                onSelect={(character) => {
                  setSelectedCharacterId(character.character_id);
                  setPersonalityDraft(formatLoadout(character.personality_loadout));
                  setAssignmentReport(null);
                  setActiveContextPreview(null);
                  void loadAssignmentReport(character.character_id);
                }}
              />
            </>
          ) : null}
        </aside>

        <section className="main-panel">
          {tab === "pages" ? <PageDetail page={selectedPage} snapshots={snapshots} /> : null}
          {tab === "conflicts" ? <ConflictDetail conflicts={conflicts} /> : null}
          {tab === "proposals" ? <ProposalDetail proposals={proposals} /> : null}
          {tab === "personality" ? (
            <>
              <CharacterCreateForm
                draft={characterCreateDraft}
                setDraft={setCharacterCreateDraft}
                preview={personalityPreview}
                loadoutDraft={createLoadoutDraft}
                setLoadoutDraft={setCreateLoadoutDraft}
                onPreview={previewCharacterPersonality}
                onCreate={createCharacterFromDraft}
                busy={busy}
              />
              <PersonalityEditor
                character={filteredCharacterPersonalities.find((item) => item.character_id === selectedCharacterId) ?? null}
                skills={personalitySkills}
                draft={personalityDraft}
                setDraft={setPersonalityDraft}
                onSave={savePersonalityLoadout}
                onReport={() => loadAssignmentReport()}
                onReassign={reassignSelectedCharacter}
                onActivePreview={previewActiveContext}
                onRelationshipEnrich={enrichRelationships}
                busy={busy}
              />
              <AssignmentReportView report={assignmentReport} />
              <ActiveContextPreview preview={activeContextPreview} />
            </>
          ) : null}
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

const PERSONALITY_COVERAGE_FILTERS: PersonalityCoverageFilter[] = [
  "all",
  "missing_loadout",
  "fallback_used",
  "valid_needs_review",
  "manual_override",
  "stress_mode_without_trigger",
  "social_mask_without_active_when"
];

function PersonalityCoveragePanel({
  coverage,
  metrics,
  filter,
  onFilterChange
}: {
  coverage: PersonalityCoverageResponse | null;
  metrics: PersonalityMetricsResponse | null;
  filter: PersonalityCoverageFilter;
  onFilterChange: (filter: PersonalityCoverageFilter) => void;
}) {
  const ratio = coverage ? `${Math.round((coverage.coverage_ratio || 0) * 100)}%` : "0%";
  return (
    <section className="coverage-panel">
      <header>
        <span>Coverage</span>
        <strong>{ratio}</strong>
      </header>
      <div className="coverage-stats">
        <span>{coverage?.with_valid_loadout ?? 0} valid</span>
        <span>{coverage?.missing_loadout ?? 0} missing</span>
        <span>{coverage?.fallback_used ?? 0} fallback</span>
        <span>{coverage?.needs_review ?? 0} review</span>
      </div>
      <div className="coverage-stats metrics-summary">
        <span>{metrics?.character_creation_manual_override_total ?? 0} manual override</span>
        <span>{metrics?.character_creation_low_confidence_total ?? 0} low confidence</span>
        <span>{Object.values(metrics?.personality_ooc_issue_total_by_assignment_mode ?? {}).reduce((sum, value) => sum + value, 0)} OOC issue</span>
        <span>{metrics?.most_used_dominant_skills?.[0]?.skill ?? "no dominant skill"}</span>
      </div>
      <div className="coverage-filters">
        {PERSONALITY_COVERAGE_FILTERS.map((item) => (
          <button
            className={filter === item ? "active" : ""}
            key={item}
            type="button"
            onClick={() => onFilterChange(item)}
            title={item}
          >
            {coverageFilterLabel(item)}
          </button>
        ))}
      </div>
    </section>
  );
}

function PersonalityCharacterList({
  characters,
  coverage,
  selectedCharacterId,
  onSelect
}: {
  characters: CharacterPersonalityInfo[];
  coverage: PersonalityCoverageResponse | null;
  selectedCharacterId: string;
  onSelect: (character: CharacterPersonalityInfo) => void;
}) {
  if (characters.length === 0) {
    return <EmptyState title="没有角色" text="BookState character 节点出现后，可在这里设置人物性格 loadout。" compact />;
  }
  return (
    <div className="list-scroll">
      {characters.map((character) => (
        <button
          className={selectedCharacterId === character.character_id ? "page-item active" : "page-item"}
          key={character.character_id}
          type="button"
          onClick={() => onSelect(character)}
        >
          <span>{character.character_name || character.character_id}</span>
          <small>{dominantSkillName(character.personality_loadout) || coverageStatusLabel(coverage, character.character_id)}</small>
        </button>
      ))}
    </div>
  );
}

function CharacterCreateForm({
  draft,
  setDraft,
  preview,
  loadoutDraft,
  setLoadoutDraft,
  onPreview,
  onCreate,
  busy
}: {
  draft: CharacterCreateDraft;
  setDraft: (draft: CharacterCreateDraft) => void;
  preview: PersonalityPreviewResponse | null;
  loadoutDraft: string;
  setLoadoutDraft: (value: string) => void;
  onPreview: () => void;
  onCreate: () => void;
  busy: boolean;
}) {
  const update = (key: keyof CharacterCreateDraft, value: string | number) => setDraft({ ...draft, [key]: value });
  return (
    <section className="character-create-form">
      <div className="panel-head">
        <div>
          <p className="eyebrow">Character Creation</p>
          <h2>创建人物</h2>
        </div>
        <div className="inline-actions">
          <button type="button" onClick={onPreview} disabled={busy || !draft.name.trim()}>
            <RefreshCw size={16} />
            预览
          </button>
          <button type="button" onClick={onCreate} disabled={busy || !draft.name.trim()}>
            <Check size={16} />
            创建
          </button>
        </div>
      </div>
      <div className="form-grid">
        <label className="field-stack">
          <span>姓名</span>
          <input value={draft.name} onChange={(event) => update("name", event.target.value)} />
        </label>
        <label className="field-stack">
          <span>别名</span>
          <input value={draft.aliases} onChange={(event) => update("aliases", event.target.value)} />
        </label>
        <label className="field-stack">
          <span>重要度</span>
          <input type="number" min={1} max={10} value={draft.importance} onChange={(event) => update("importance", Number(event.target.value))} />
        </label>
        <label className="field-stack">
          <span>public identity</span>
          <input value={draft.publicIdentity} onChange={(event) => update("publicIdentity", event.target.value)} />
        </label>
        <label className="field-stack">
          <span>role archetype</span>
          <input value={draft.roleArchetype} onChange={(event) => update("roleArchetype", event.target.value)} />
        </label>
        <label className="field-stack">
          <span>narrative role</span>
          <input value={draft.narrativeRole} onChange={(event) => update("narrativeRole", event.target.value)} />
        </label>
        <label className="field-stack">
          <span>faction</span>
          <input value={draft.factionId} onChange={(event) => update("factionId", event.target.value)} />
        </label>
        <label className="field-stack">
          <span>personality tags</span>
          <input value={draft.personalityTags} onChange={(event) => update("personalityTags", event.target.value)} />
        </label>
      </div>
      <label className="field-stack">
        <span>描述</span>
        <textarea value={draft.description} onChange={(event) => update("description", event.target.value)} />
      </label>
      <label className="field-stack">
        <span>goal</span>
        <input value={draft.goal} onChange={(event) => update("goal", event.target.value)} />
      </label>
      <PersonalityPreviewPanel preview={preview} />
      <label className="field-stack">
        <span>可编辑 loadout JSON</span>
        <textarea className="json-editor small" value={loadoutDraft} onChange={(event) => setLoadoutDraft(event.target.value)} spellCheck={false} />
      </label>
    </section>
  );
}

function PersonalityPreviewPanel({ preview }: { preview: PersonalityPreviewResponse | null }) {
  if (!preview) return null;
  const assignment = preview.personality_assignment;
  return (
    <section className="personality-preview-panel">
      <h3>自动性格预览</h3>
      <div className="coverage-stats">
        <span>dominant: {dominantSkillName(preview.personality_loadout) || "none"}</span>
        <span>confidence: {String(assignment.confidence ?? "")}</span>
        <span>status: {String(assignment.status ?? "")}</span>
      </div>
      <pre>{JSON.stringify(assignment.reason_tags ?? [], null, 2)}</pre>
    </section>
  );
}

function PersonalityEditor({
  character,
  skills,
  draft,
  setDraft,
  onSave,
  onReport,
  onReassign,
  onActivePreview,
  onRelationshipEnrich,
  busy
}: {
  character: CharacterPersonalityInfo | null;
  skills: PersonalitySkillInfo[];
  draft: string;
  setDraft: (value: string) => void;
  onSave: (loadout: Record<string, unknown>) => void;
  onReport: () => void;
  onReassign: () => void;
  onActivePreview: () => void;
  onRelationshipEnrich: () => void;
  busy: boolean;
}) {
  if (!character) {
    return <EmptyState title="人物性格" text="选择一个 BookState character 后编辑 personality_loadout。" />;
  }
  const parsed = parseLoadoutDraft(draft);
  const dominant = parsed.ok ? dominantSkillName(parsed.value) : "";
  const traits = skills.filter((skill) => skill.skill_type === "trait");

  function updateDominant(skillName: string) {
    const base = parsed.ok ? parsed.value : defaultPersonalityLoadout();
    setDraft(
      formatLoadout({
        ...base,
        dominant: skillName ? { skill: skillName, weight: 0.75 } : null
      })
    );
  }

  return (
    <div className="personality-editor">
      <div className="panel-head">
        <div>
          <p className="eyebrow">Character Personality</p>
          <h2>{character.character_name || character.character_id}</h2>
        </div>
        <button
          type="button"
          disabled={busy || !parsed.ok}
          onClick={() => {
            if (parsed.ok) onSave(parsed.value);
          }}
        >
          <Check size={16} />
          保存
        </button>
        <button type="button" disabled={busy} onClick={onReport}>
          查看 report
        </button>
        <button type="button" disabled={busy} onClick={onReassign}>
          重新分配
        </button>
        <button type="button" disabled={busy || !parsed.ok} onClick={onActivePreview}>
          Active context
        </button>
        <button type="button" disabled={busy} onClick={onRelationshipEnrich}>
          关系 enrichment
        </button>
      </div>

      <div className="personality-grid">
        <section className="summary-panel">
          <h3>Dominant Trait</h3>
          <label className="field-stack">
            <span>主性格 skill</span>
            <select value={dominant} onChange={(event) => updateDominant(event.target.value)}>
              <option value="">未设置</option>
              {traits.map((skill) => (
                <option key={skill.name} value={skill.name}>
                  {skill.name}
                </option>
              ))}
            </select>
          </label>
        </section>
        <section className="summary-panel">
          <h3>Skill Catalog</h3>
          <div className="skill-catalog">
            {skills.slice(0, 48).map((skill) => (
              <div key={skill.name}>
                <strong>{skill.name}</strong>
                <span>{skill.skill_type || "unknown"}</span>
                <small>{skill.description || (skill.incomplete ? "待填写 metadata" : "")}</small>
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="markdown-panel">
        <h3>personality_loadout JSON</h3>
        <textarea
          className="json-editor"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          spellCheck={false}
        />
        {!parsed.ok ? <p className="inline-error">JSON 格式不正确，修正后才能保存。</p> : null}
      </section>
    </div>
  );
}

function AssignmentReportView({ report }: { report: PersonalityAssignmentReportResponse | null }) {
  if (!report) return null;
  return (
    <section className="assignment-report">
      <h3>Assignment Report</h3>
      <pre>{JSON.stringify(report.personality_assignment, null, 2)}</pre>
    </section>
  );
}

function ActiveContextPreview({ preview }: { preview: ActiveContextPreviewResponse | null }) {
  if (!preview) return null;
  return (
    <section className="active-context-preview">
      <h3>Active Context Preview</h3>
      <pre>{JSON.stringify(preview.active_personality_context, null, 2)}</pre>
    </section>
  );
}

function parseLoadoutDraft(raw: string): { ok: true; value: Record<string, unknown> } | { ok: false; value: Record<string, unknown> } {
  try {
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? { ok: true, value: parsed as Record<string, unknown> }
      : { ok: false, value: {} };
  } catch {
    return { ok: false, value: {} };
  }
}

function characterCreatePayload(draft: CharacterCreateDraft): Record<string, unknown> {
  const tags = draft.personalityTags
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    source: "world_studio_manual",
    name: draft.name.trim(),
    aliases: draft.aliases
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    description: draft.description,
    importance: draft.importance,
    profile: {
      public_identity: draft.publicIdentity,
      role_archetype: draft.roleArchetype,
      narrative_role: draft.narrativeRole,
      personality_tags: tags
    },
    state: {
      faction_id: draft.factionId,
      goal: draft.goal
    },
    personality_tags: tags
  };
}

function issueMatchesFilter(issue: string, filter: PersonalityCoverageFilter): boolean {
  if (filter === "all") return true;
  return issue === filter || issue.startsWith(`${filter}:`);
}

function coverageFilterLabel(filter: PersonalityCoverageFilter): string {
  if (filter === "all") return "全部";
  if (filter === "missing_loadout") return "缺失";
  if (filter === "fallback_used") return "Fallback";
  if (filter === "valid_needs_review") return "需复核";
  if (filter === "manual_override") return "人工锁定";
  if (filter === "stress_mode_without_trigger") return "Stress trigger";
  if (filter === "social_mask_without_active_when") return "Mask active";
  return filter;
}

function coverageStatusLabel(coverage: PersonalityCoverageResponse | null, characterId: string): string {
  const item = coverage?.characters.find((character) => character.character_id === characterId);
  if (!item) return "未设置主性格";
  if (item.issues.includes("missing_loadout")) return "missing_loadout";
  if (item.issues.includes("fallback_used")) return "fallback_used";
  if (item.issues.includes("valid_needs_review")) return "valid_needs_review";
  return item.assignment_status || "未设置主性格";
}

function dominantSkillName(loadout: Record<string, unknown>): string {
  const dominant = loadout.dominant;
  if (!dominant || typeof dominant !== "object" || Array.isArray(dominant)) return "";
  const skill = (dominant as Record<string, unknown>).skill;
  return typeof skill === "string" ? skill : "";
}

function EmptyState({ title, text, compact = false }: { title: string; text: string; compact?: boolean }) {
  return (
    <div className={compact ? "empty compact" : "empty"}>
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  );
}
