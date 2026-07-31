"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  api,
  type DashboardProject,
  type ProjectAssignment,
  type ProjectProgress,
  type TeamMember,
} from "@/lib/api";
import { ProgressBar, aggregateProgress } from "@/components/dashboard/progress";
import { DdayChip, HealthDot } from "@/components/dashboard/tracking";
import { SE_STAGES } from "@/components/dashboard/requirements";

const SE_LABEL = Object.fromEntries(SE_STAGES.map((s) => [s.key, s.label]));
import { PageHeader, Banner, ToolbarButton } from "@/components/ui";
import {
  Field,
  Modal,
  Section,
  TagSelect,
  TextArea,
  TextInput,
  labelStyle,
  memberColorMap,
  type TagColor,
} from "@/components/dashboard/kit";
import { AssignmentChips } from "@/components/dashboard/assignments";

const STATUSES = ["기획", "진행중", "보류", "완료", "종료"];
const STATUS_COLOR: Record<string, TagColor> = {
  기획: "gray",
  진행중: "green",
  보류: "yellow",
  완료: "blue",
  종료: "red",
};
const statusColor = (v: string): TagColor => STATUS_COLOR[v] ?? "gray";

function orNull(v: unknown): string | null {
  const s = String(v ?? "").trim();
  return s === "" ? null : s;
}

type Draft = {
  name: string;
  color: string;
  status: string;
  description: string;
};

const EMPTY: Draft = { name: "", color: "#5d9bd8", status: "", description: "" };

export default function ProjectsPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [assignments, setAssignments] = useState<ProjectAssignment[]>([]);
  const [progress, setProgress] = useState<Record<string, ProjectProgress>>({});
  const [error, setError] = useState<string | null>(null);

  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  // 하위 프로젝트 추가 모달일 때 부모 project_id.
  const [parentId, setParentId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [modalErr, setModalErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [pr, mb, asg, prog] = await Promise.all([
      api.listDashboardProjects(),
      api.listTeamMembers(),
      api.listAssignments(),
      api.listProjectProgress().catch(() => [] as ProjectProgress[]),
    ]);
    setProjects(pr);
    setMembers(mb);
    setAssignments(asg);
    setProgress(Object.fromEntries(prog.map((p) => [p.project_id, p])));
  }, []);

  const reloadAssignments = useCallback(async () => {
    setAssignments(await api.listAssignments());
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.listDashboardProjects(),
      api.listTeamMembers(),
      api.listAssignments(),
      api.listProjectProgress().catch(() => [] as ProjectProgress[]),
    ])
      .then(([pr, mb, asg, prog]) => {
        if (!alive) return;
        setProjects(pr);
        setMembers(mb);
        setAssignments(asg);
        setProgress(Object.fromEntries(prog.map((p) => [p.project_id, p])));
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
      });
    return () => {
      alive = false;
    };
  }, []);

  const memberOptions = useMemo(
    () => members.map((m) => ({ id: m.member_id, name: m.name })),
    [members],
  );
  const memberColors = useMemo(() => memberColorMap(members), [members]);
  const byProject = useMemo(() => {
    const map: Record<string, ProjectAssignment[]> = {};
    assignments.forEach((a) => {
      (map[a.project_id] ??= []).push(a);
    });
    return map;
  }, [assignments]);

  function openCreate(parent: string | null = null) {
    setDraft(EMPTY);
    setParentId(parent);
    setModalErr(null);
    setOpen(true);
  }

  async function save() {
    setBusy(true);
    setModalErr(null);
    try {
      const created = await api.createDashboardProject({
        name: draft.name,
        color: orNull(draft.color),
        status: orNull(draft.status),
        description: orNull(draft.description),
        sort_order: 0,
        active: true,
        parent_project_id: parentId,
      });
      setOpen(false);
      // 생성 직후 노션처럼 바로 상세 페이지로 이동한다.
      router.push(`/dashboard/projects/${created.project_id}`);
    } catch (e) {
      setModalErr(e instanceof Error ? e.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string, childCount: number) {
    const msg =
      childCount > 0
        ? `이 프로젝트를 삭제하시겠습니까? 하위 프로젝트 ${childCount}개와 멤버 배정도 함께 삭제됩니다.`
        : "이 프로젝트를 삭제하시겠습니까? 멤버 배정도 함께 삭제됩니다.";
    if (!window.confirm(msg)) return;
    try {
      await api.deleteDashboardProject(id);
      await load();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "삭제 실패");
    }
  }

  function openProject(id: string) {
    router.push(`/dashboard/projects/${id}`);
  }

  /** 프로젝트 칸반 보드로 바로 진입 — 보드가 없으면 만들어서 연다(멱등). */
  async function openBoard(id: string) {
    try {
      const db = await api.ensureProjectBoard(id);
      router.push(`/dashboard/db/${db.database_id}`);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "보드 열기 실패");
    }
  }

  // 계층 정렬: 루트 → 그 하위들 순. 부모가 없는(고아) 하위는 루트처럼 취급.
  const childrenOf = useMemo(() => {
    const ids = new Set(projects.map((p) => p.project_id));
    const map: Record<string, DashboardProject[]> = {};
    projects.forEach((p) => {
      if (p.parent_project_id && ids.has(p.parent_project_id)) {
        (map[p.parent_project_id] ??= []).push(p);
      }
    });
    return map;
  }, [projects]);
  const orderedRows = useMemo(() => {
    const ids = new Set(projects.map((p) => p.project_id));
    const roots = projects.filter(
      (p) => !p.parent_project_id || !ids.has(p.parent_project_id),
    );
    return roots.flatMap((r) => [
      { p: r, depth: 0 },
      ...(childrenOf[r.project_id] ?? []).map((c) => ({ p: c, depth: 1 })),
    ]);
  }, [projects, childrenOf]);

  const headers = ["프로젝트", "진행률", "건강", "상태", "목표일", "오너", "설명", "멤버 · 역할"];

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <PageHeader
        title="프로젝트"
        subtitle="프로젝트를 누르면 노션처럼 전용 페이지가 열립니다. 페이지 안에서 칸반 보드·데이터베이스를 넣어 관리하세요."
      />
      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">
            {error}
          </Banner>
        </div>
      ) : null}

      <Section
        title="프로젝트"
        subtitle={`${projects.length}개`}
        action={
          <ToolbarButton tone="primary" onClick={() => openCreate()}>
            + 추가
          </ToolbarButton>
        }
      >
        {projects.length === 0 ? (
          <p
            className="py-6 text-center text-[var(--text-body-sm)]"
            style={labelStyle()}
          >
            아직 프로젝트가 없습니다.
          </p>
        ) : (
          <div
            className="overflow-x-auto rounded-[8px]"
            style={{ border: "1px solid var(--color-divider)" }}
          >
            <table className="w-full border-collapse text-[var(--text-body-sm)]">
              <thead>
                <tr
                  style={{
                    background: "var(--color-app-canvas)",
                    borderBottom: "1px solid var(--color-divider)",
                  }}
                >
                  {headers.map((h) => (
                    <th
                      key={h}
                      className="px-3 py-2 text-left font-medium whitespace-nowrap"
                      style={labelStyle()}
                    >
                      {h}
                    </th>
                  ))}
                  <th className="w-px px-2" />
                </tr>
              </thead>
              <tbody>
                {orderedRows.map(({ p, depth }) => {
                  const children = childrenOf[p.project_id] ?? [];
                  const prog = aggregateProgress(
                    progress,
                    p.project_id,
                    children.map((c) => c.project_id),
                  );
                  return (
                    <tr
                      key={p.project_id}
                      className="group hover:bg-[var(--color-app-canvas)]"
                      style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
                    >
                      <td
                        className="cursor-pointer px-3 py-2.5 align-middle whitespace-nowrap"
                        onClick={() => openProject(p.project_id)}
                      >
                        <span
                          className="flex items-center gap-2"
                          style={depth ? { paddingLeft: 22 } : undefined}
                        >
                          {depth ? (
                            <span style={{ color: "var(--color-text-faint)" }}>↳</span>
                          ) : null}
                          <span
                            className="inline-block h-2.5 w-2.5 rounded-full"
                            style={{ background: p.color || "var(--color-divider)" }}
                          />
                          <span
                            style={{
                              color: "var(--color-text-strong)",
                              fontWeight: depth ? 500 : 600,
                            }}
                          >
                            {p.name}
                          </span>
                          {p.se_stage && SE_LABEL[p.se_stage] ? (
                            <span
                              className="rounded-full px-1.5 py-0.5 text-[var(--text-meta)]"
                              title="SE 단계"
                              style={{
                                border: "1px solid var(--color-divider)",
                                color: "var(--color-text-muted)",
                              }}
                            >
                              {SE_LABEL[p.se_stage]}
                            </span>
                          ) : null}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 align-middle whitespace-nowrap">
                        <ProgressBar value={prog} compact />
                      </td>
                      <td className="px-3 py-2.5 align-middle whitespace-nowrap">
                        <HealthDot value={p.health} />
                      </td>
                      <td className="px-3 py-2.5 align-middle whitespace-nowrap">
                        <TagSelect
                          variant="inline"
                          value={p.status ?? ""}
                          options={STATUSES}
                          colorFor={statusColor}
                          placeholder="—"
                          onChange={async (v) => {
                            await api.updateDashboardProject(p.project_id, {
                              name: p.name,
                              color: p.color,
                              status: v || null,
                              description: p.description,
                              body: p.body,
                              sort_order: p.sort_order,
                              active: p.active,
                              parent_project_id: p.parent_project_id,
                              se_stage: p.se_stage,
                              start_date: p.start_date,
                              target_date: p.target_date,
                              owner_member_id: p.owner_member_id,
                              health: p.health,
                            });
                            await load();
                          }}
                        />
                      </td>
                      <td className="px-3 py-2.5 align-middle whitespace-nowrap">
                        <DdayChip date={p.target_date} />
                      </td>
                      <td
                        className="px-3 py-2.5 align-middle whitespace-nowrap text-[var(--text-body-sm)]"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        {members.find((m) => m.member_id === p.owner_member_id)?.name ?? "—"}
                      </td>
                      <td
                        className="cursor-pointer px-3 py-2.5 align-middle"
                        style={{ color: "var(--color-text-muted)", maxWidth: 260 }}
                        onClick={() => openProject(p.project_id)}
                      >
                        <span className="block truncate">{p.description ?? "—"}</span>
                      </td>
                      <td className="px-3 py-2.5 align-middle">
                        <AssignmentChips
                          scope="project"
                          ownerId={p.project_id}
                          options={memberOptions}
                          assignments={byProject[p.project_id] ?? []}
                          colors={memberColors}
                          onChanged={reloadAssignments}
                        />
                      </td>
                      <td className="px-2 py-2.5 text-right whitespace-nowrap">
                        {depth === 0 ? (
                          <button
                            onClick={() => openCreate(p.project_id)}
                            className="mr-2 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                            style={{ color: "var(--color-text-muted)" }}
                          >
                            ＋하위
                          </button>
                        ) : null}
                        <button
                          onClick={() => openBoard(p.project_id)}
                          className="mr-2 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                          style={{ color: "var(--color-text-muted)" }}
                        >
                          보드
                        </button>
                        <button
                          onClick={() => openProject(p.project_id)}
                          className="mr-2 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                          style={{ color: "var(--color-text-muted)" }}
                        >
                          열기
                        </button>
                        <button
                          onClick={() => remove(p.project_id, children.length)}
                          className="text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                          style={{ color: "var(--color-fail-fg)" }}
                        >
                          삭제
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Modal
        open={open}
        title={
          parentId
            ? `하위 프로젝트 추가 — ${projects.find((p) => p.project_id === parentId)?.name ?? ""}`
            : "프로젝트 추가"
        }
        onClose={() => setOpen(false)}
        footer={
          <>
            <ToolbarButton onClick={() => setOpen(false)} disabled={busy}>
              닫기
            </ToolbarButton>
            <ToolbarButton tone="primary" onClick={save} disabled={busy}>
              {busy ? "저장 중…" : "저장"}
            </ToolbarButton>
          </>
        }
      >
        {modalErr ? (
          <p
            className="mb-3 text-[var(--text-body-sm)]"
            style={{ color: "var(--color-fail-fg)" }}
          >
            {modalErr}
          </p>
        ) : null}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="이름">
            <TextInput
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </Field>
          <Field label="상태">
            <TagSelect
              value={draft.status}
              options={STATUSES}
              colorFor={statusColor}
              onChange={(v) => setDraft({ ...draft, status: v })}
            />
          </Field>
          <Field label="색상">
            <TextInput
              value={draft.color}
              placeholder="#5d9bd8"
              onChange={(e) => setDraft({ ...draft, color: e.target.value })}
            />
          </Field>
          <Field label="설명" full>
            <TextArea
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            />
          </Field>
        </div>
        <p className="mt-4 text-[var(--text-meta)]" style={labelStyle()}>
          저장하면 노션 페이지처럼 프로젝트 상세가 열립니다. 멤버 배정·본문·칸반 보드는 거기서 관리하세요.
        </p>
      </Modal>
    </div>
  );
}
