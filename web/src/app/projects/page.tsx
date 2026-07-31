"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type Project,
  type ProjectGroup,
  type ProjectsResult,
} from "@/lib/api";
import {
  Banner,
  Card,
  PageHeader,
  Skeleton,
  Toolbar,
  cx,
} from "@/components/ui";

/* Human-facing project labels — the enum value stays canonical on the wire. */
const PROJECT_LABEL: Record<string, string> = {
  atlas: "아틀라스",
  nova: "노바",
  orbit: "오르빗",
  company: "전사",
};

/* Re-tag store labels for the success surface. */
const STORE_LABEL: Record<string, string> = {
  notion_rows: "구조화 행",
  wiki: "위키",
  corpus_chunks: "코퍼스",
};

function projectLabel(p: string): string {
  return PROJECT_LABEL[p] ?? p;
}

interface RetagNotice {
  db_name: string;
  project: string;
  updated: Record<string, number>;
}

const ALL_FILTER = "__all__";

export default function ProjectsPage() {
  const [data, setData] = useState<ProjectsResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingDb, setSavingDb] = useState<string | null>(null);
  const [notice, setNotice] = useState<RetagNotice | null>(null);
  const [filter, setFilter] = useState<string>(ALL_FILTER);

  useEffect(() => {
    let cancelled = false;

    async function loadInitial() {
      try {
        const res = await api.listProjects();
        if (cancelled) return;
        setData(res);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "분류를 불러오지 못했습니다.");
        setData(null);
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void loadInitial();

    return () => {
      cancelled = true;
    };
  }, []);

  async function changeProject(db_name: string, project: Project) {
    setSavingDb(db_name);
    setError(null);
    try {
      const res = await api.setProjectOverride(db_name, project);
      setNotice({
        db_name: res.db_name,
        project: res.project,
        updated: res.updated,
      });
      const fresh = await api.listProjects();
      setData(fresh);
    } catch (e) {
      setError(e instanceof Error ? e.message : "분류 변경에 실패했습니다.");
    } finally {
      setSavingDb(null);
    }
  }

  async function clearProject(db_name: string) {
    setSavingDb(db_name);
    setError(null);
    try {
      const res = await api.clearProjectOverride(db_name);
      setNotice({
        db_name: res.db_name,
        project: res.project,
        updated: res.updated,
      });
      const fresh = await api.listProjects();
      setData(fresh);
    } catch (e) {
      setError(e instanceof Error ? e.message : "분류 초기화에 실패했습니다.");
    } finally {
      setSavingDb(null);
    }
  }

  const projects = useMemo(() => data?.projects ?? [], [data]);

  const grouped = useMemo(() => {
    if (!data) return [];
    const byProject = new Map<string, ProjectGroup[]>();
    for (const g of data.groups) {
      const list = byProject.get(g.project) ?? [];
      list.push(g);
      byProject.set(g.project, list);
    }
    const orderedKeys = [
      ...projects.filter((p) => byProject.has(p)),
      ...[...byProject.keys()].filter((p) => !projects.includes(p)),
    ];
    return orderedKeys.map((project) => ({
      project,
      rows: (byProject.get(project) ?? []).sort((a, b) => b.count - a.count),
      total: (byProject.get(project) ?? []).reduce(
        (sum, r) => sum + r.count,
        0,
      ),
    }));
  }, [data, projects]);

  const visibleGroups = useMemo(() => {
    if (filter === ALL_FILTER) return grouped;
    return grouped.filter((g) => g.project === filter);
  }, [grouped, filter]);

  const totalRows = data?.groups.reduce((s, g) => s + g.count, 0) ?? 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="프로젝트 정리"
        subtitle="Notion DB를 토큰 소속으로 자동 분류했습니다. 잘못된 항목은 직접 바꾸면 기존 데이터까지 다시 태깅됩니다."
        right={
          <Toolbar>
            <span
              className="text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {data?.groups.length ?? 0}개 DB ·{" "}
              <span className="font-[family-name:var(--font-mono)]">
                {totalRows.toLocaleString()}
              </span>
              행
            </span>
          </Toolbar>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[940px] px-3 py-3 sm:px-5 sm:py-5">
          {/* Filter row — small chip buttons matching toolbar style */}
          {grouped.length > 0 ? (
            <Toolbar className="mb-3">
              <FilterChip
                active={filter === ALL_FILTER}
                onClick={() => setFilter(ALL_FILTER)}
                count={grouped.reduce((s, g) => s + g.rows.length, 0)}
              >
                전체
              </FilterChip>
              {grouped.map((g) => (
                <FilterChip
                  key={g.project}
                  active={filter === g.project}
                  onClick={() => setFilter(g.project)}
                  count={g.rows.length}
                >
                  {projectLabel(g.project)}
                </FilterChip>
              ))}
            </Toolbar>
          ) : null}

          {error ? (
            <div className="mb-3">
              <Banner tone="fail" title="요청 실패">
                {error}
              </Banner>
            </div>
          ) : null}

          {notice ? <RetagNotice notice={notice} /> : null}

          {loading ? (
            <LoadingSkeleton />
          ) : grouped.length === 0 ? (
            <EmptyState />
          ) : (
            <div className="space-y-4">
              {visibleGroups.map((group) => (
                <section
                  key={group.project}
                  aria-label={`${projectLabel(group.project)} 프로젝트`}
                >
                  <div className="mb-1.5 flex items-baseline gap-2 px-1">
                    <h2
                      className="text-[var(--text-body)] font-bold"
                      style={{ color: "var(--color-text-strong)" }}
                    >
                      {projectLabel(group.project)}
                    </h2>
                    <span
                      className="font-[family-name:var(--font-mono)] text-[var(--text-meta)] uppercase tracking-[0.04em]"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      {group.project}
                    </span>
                    <span
                      className="text-[var(--text-meta)]"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      {group.rows.length}개 DB · 총{" "}
                      <span className="font-[family-name:var(--font-mono)]">
                        {group.total.toLocaleString()}
                      </span>
                      행
                    </span>
                  </div>

                  {/* Each row is its own compact card. List, not table — */}
                  {/* lighter visual weight per DESIGN.md "card row" rhythm. */}
                  <ul className="flex flex-col gap-1">
                    {group.rows.map((row) => (
                      <li key={row.db_name}>
                        <DbRow
                          dbName={row.db_name}
                          count={row.count}
                          project={row.project}
                          source={row.source}
                          projects={projects}
                          saving={savingDb === row.db_name}
                          onChange={(p) => void changeProject(row.db_name, p)}
                          onClear={() => void clearProject(row.db_name)}
                        />
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------- DB row ------------------------------- */

function DbRow({
  dbName,
  count,
  project,
  source,
  projects,
  saving,
  onChange,
  onClear,
}: {
  dbName: string;
  count: number;
  project: string;
  source: string;
  projects: string[];
  saving: boolean;
  onChange: (p: Project) => void;
  onClear: () => void;
}) {
  return (
    <Card interactive className="project-db-row flex min-h-[42px] items-center gap-3 px-3 py-1.5">
      <span
        className="flex-1 truncate text-[var(--text-body-sm)] font-semibold"
        style={{ color: "var(--color-text-strong)" }}
        title={dbName}
      >
        {dbName}
      </span>
      <span
        className="w-[64px] shrink-0 text-right font-[family-name:var(--font-mono)] text-[var(--text-meta)]"
        style={{ color: "var(--color-text-body)" }}
      >
        {count.toLocaleString()}
      </span>
      <span className="w-[64px] shrink-0">
        <SourceTag source={source} />
      </span>
      <span className="w-[180px] shrink-0">
        <ProjectSelect
          value={project}
          projects={projects}
          source={source}
          saving={saving}
          onChange={onChange}
          onClear={onClear}
        />
      </span>
    </Card>
  );
}

/* ----------------------------- filter chip ---------------------------- */

function FilterChip({
  active,
  onClick,
  count,
  children,
}: {
  active: boolean;
  onClick: () => void;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className="inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] transition-colors"
      style={{
        background: active
          ? "var(--color-sidebar-active)"
          : "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        color: active
          ? "var(--color-text-strong)"
          : "var(--color-text-body)",
        fontWeight: active ? 600 : 500,
        boxShadow: active ? "none" : "var(--shadow-toolbar)",
      }}
      onMouseEnter={(e) => {
        if (!active)
          e.currentTarget.style.background = "var(--color-toolbar-hover)";
      }}
      onMouseLeave={(e) => {
        if (!active) e.currentTarget.style.background = "var(--color-card)";
      }}
    >
      {children}
      <span
        className="font-[family-name:var(--font-mono)] text-[var(--text-meta)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        {count}
      </span>
    </button>
  );
}

/* ----------------------------- project select -------------------------- */

const CLEAR_SENTINEL = "__clear__";

function ProjectSelect({
  value,
  projects,
  source,
  saving,
  onChange,
  onClear,
}: {
  value: string;
  projects: string[];
  source: string;
  saving: boolean;
  onChange: (p: Project) => void;
  onClear: () => void;
}) {
  const options = projects.includes(value) ? projects : [value, ...projects];

  function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    if (e.target.value === CLEAR_SENTINEL) {
      onClear();
    } else {
      onChange(e.target.value as Project);
    }
  }

  return (
    <div className="relative inline-flex w-full items-center">
      <select
        value={value}
        disabled={saving}
        aria-label="프로젝트 선택"
        onChange={handleChange}
        className={cx(
          "h-8 w-full appearance-none rounded-[var(--radius-toolbar)] py-1.5 pl-2.5 pr-7 text-[var(--text-body-sm)] transition-colors",
          "disabled:cursor-wait disabled:opacity-60",
        )}
        style={{
          background: "var(--color-card)",
          border: "1px solid var(--color-toolbar-border)",
          color: "var(--color-text-strong)",
          boxShadow: "var(--shadow-toolbar)",
        }}
      >
        {options.map((p) => (
          <option key={p} value={p}>
            {projectLabel(p)} ({p})
          </option>
        ))}
        {source === "override" ? (
          <option value={CLEAR_SENTINEL}>↩ 자동으로 되돌리기</option>
        ) : null}
      </select>
      <span
        className="pointer-events-none absolute right-2 flex items-center"
        style={{ color: "var(--color-text-muted)" }}
      >
        {saving ? (
          <svg
            className="animate-spin"
            width="11"
            height="11"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            aria-hidden
          >
            <path d="M21 12a9 9 0 1 1-6.2-8.5" />
          </svg>
        ) : (
          <svg
            width="11"
            height="11"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            <path d="m6 9 6 6 6-6" />
          </svg>
        )}
      </span>
    </div>
  );
}

/* ------------------------------ source tag ---------------------------- */
/*
 * Per brief: "수정됨 / 자동 = subtle text-meta badge". Lowest-contrast
 * variant — no fill, no border. The presence of the small attention dot
 * carries the "overridden" signal without weight.
 */

function SourceTag({ source }: { source: string }) {
  const overridden = source === "override";
  return (
    <span
      className="inline-flex items-center gap-1 text-[var(--text-meta)] font-medium"
      style={{
        color: overridden
          ? "var(--color-text-strong)"
          : "var(--color-text-muted)",
      }}
    >
      {overridden ? (
        <span
          aria-hidden
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ background: "var(--color-attention)" }}
        />
      ) : null}
      {overridden ? "수정됨" : "자동"}
    </span>
  );
}

/* ------------------------------ retag notice -------------------------- */

function RetagNotice({ notice }: { notice: RetagNotice }) {
  const parts = Object.entries(notice.updated)
    .filter(([, n]) => n > 0)
    .map(([store, n]) => `${STORE_LABEL[store] ?? store} ${n.toLocaleString()}`);
  return (
    <div className="mb-3">
      <Banner
        tone="pass"
        title={`${notice.db_name} → ${projectLabel(notice.project)} 재태깅 완료`}
      >
        {parts.length > 0 ? (
          <span className="font-[family-name:var(--font-mono)] text-[var(--text-meta)]">
            {parts.join(" · ")}
          </span>
        ) : (
          <span>재태깅된 항목이 없습니다.</span>
        )}
      </Banner>
    </div>
  );
}

/* --------------------------- loading / empty -------------------------- */

function LoadingSkeleton() {
  return (
    <div className="space-y-4">
      {[0, 1].map((g) => (
        <section key={g}>
          <div className="mb-1.5 px-1">
            <Skeleton className="h-3 w-24" />
          </div>
          <ul className="flex flex-col gap-1">
            {[0, 1, 2].map((r) => (
              <li key={r}>
                <Card className="flex h-[42px] items-center gap-3 px-3">
                  <Skeleton className="h-3 flex-1" />
                  <Skeleton className="h-3 w-12" />
                  <Skeleton className="h-4 w-12" />
                  <Skeleton className="h-7 w-[180px]" />
                </Card>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="mt-10 flex flex-col items-center justify-center py-10 text-center">
      <div
        className="flex h-10 w-10 items-center justify-center rounded-[6px]"
        style={{
          background: "var(--color-card)",
          border: "1px solid var(--color-divider)",
          color: "var(--color-text-muted)",
        }}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M3 7.5 12 3l9 4.5-9 4.5z" />
          <path d="m3 12 9 4.5L21 12" />
          <path d="m3 16.5 9 4.5 9-4.5" />
        </svg>
      </div>
      <p
        className="mt-2.5 max-w-[360px] text-[var(--text-body-sm)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        시드된 데이터가 없습니다 —{" "}
        <span
          className="font-[family-name:var(--font-mono)]"
          style={{ color: "var(--color-text-strong)" }}
        >
          make import-notion
        </span>{" "}
        후 다시 확인하세요.
      </p>
    </div>
  );
}

