"use client";

/**
 * 회사 프로젝트 상세 — 노션 페이지처럼 동작하는 전용 페이지.
 * 프로젝트 목록에서 프로젝트를 누르면 분할(side peek)이 아니라 이 페이지로 이동한다.
 * 자유 본문(BlockNote) 안에서 하위 페이지·인라인 데이터베이스(표/칸반 보드)를 넣어
 * 관리하고, 하단에 연결된 지원사업/회의록/파트너사/계획·회고/채널 업무를 보여준다.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  api,
  type DashboardPage,
  type DashboardPageSummary,
  type DashboardProject,
  type MeetingNote,
  type PartnerCompany,
  type PeriodSummary,
  type ProjectAssignment,
  type ProjectBoardTask,
  type ProjectDatabase,
  type ProjectProgress,
  type RequirementSummary,
  type SupportProgram,
  type TeamMember,
} from "@/lib/api";
import {
  enqueuePageSave,
  getPageSaveState,
  subscribePageSaveState,
  wirePageSaveRecovery,
} from "@/lib/pageSave";
import { ProgressBar, aggregateProgress } from "@/components/dashboard/progress";
import { RequirementsSection, SeStageStepper } from "@/components/dashboard/requirements";
import { setSubpageTitleInBody } from "@/components/dashboard/subpageBlock";
import { PageLevelView } from "@/components/dashboard/pageEditor";
import { DatabaseView } from "@/components/dashboard/database/DatabaseView";
import { Banner } from "@/components/ui";
import {
  TagPill,
  TagSelect,
  labelStyle,
  memberColorMap,
  type TagColor,
} from "@/components/dashboard/kit";
import { AssignmentChips } from "@/components/dashboard/assignments";
import {
  ActivityFeedSection,
  HealthSelect,
  TargetCountdown,
} from "@/components/dashboard/tracking";
import {
  AutoGrowTitle,
  PeekDivider,
  PeekSection,
  PropRow,
  PropTextInput,
} from "@/components/dashboard/peek";

const RichBodyEditor = dynamic(
  () => import("@/components/dashboard/RichBody").then((m) => m.RichBodyEditor),
  { ssr: false },
);

const STATUSES = ["기획", "진행중", "보류", "완료", "종료"];
const STATUS_COLOR: Record<string, TagColor> = {
  기획: "gray",
  진행중: "green",
  보류: "yellow",
  완료: "blue",
  종료: "red",
};
const statusColor = (v: string): TagColor => STATUS_COLOR[v] ?? "gray";

type Related = {
  programs: SupportProgram[];
  meetings: MeetingNote[];
  partners: PartnerCompany[];
  weeks: PeriodSummary[];
  boardTasks: ProjectBoardTask[];
};

export default function ProjectDetailPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;
  const router = useRouter();

  const [project, setProject] = useState<DashboardProject | null>(null);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [assignments, setAssignments] = useState<ProjectAssignment[]>([]);
  const [related, setRelated] = useState<Related | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 저장 실패 표시 — load 시점 error와 별도다. 기존 error는 `error && !project`
  // 게이트 때문에 project 로드 후에는 절대 렌더되지 않아, patchProject/페이지 저장
  // 실패가 화면에 안 보이고 새로고침하면 유실됐다. project 로드 후에도 항상 노출한다.
  const [saveError, setSaveError] = useState<string | null>(null);
  // 하위 프로젝트 계층 + 진행률(전 프로젝트 목록/집계에서 파생).
  const [allProjects, setAllProjects] = useState<DashboardProject[]>([]);
  const [progress, setProgress] = useState<Record<string, ProjectProgress>>({});
  const [reqSummary, setReqSummary] = useState<Record<string, RequirementSummary>>({});
  // 프로젝트 소속 데이터베이스(칸반 보드 포함). 보드가 없으면 자동으로 만들어 보장한다.
  const [databases, setDatabases] = useState<ProjectDatabase[]>([]);
  // 이 프로젝트에 링크된 메모(관련 메모, docs/personal-notes.md).
  const [projectNotes, setProjectNotes] = useState<DashboardPageSummary[]>([]);

  // 노션식 중첩 네비게이션 스택. 0번은 프로젝트(루트). 하위 페이지 + 데이터베이스를
  // 같은 스택에서 연다(인라인으로 박지 않고 클릭해 들어가는 데이터베이스 페이지).
  type Level =
    | { kind: "project" }
    | { kind: "page"; pageId: string }
    | { kind: "database"; databaseId: string };
  const [stack, setStack] = useState<Level[]>([{ kind: "project" }]);
  const [pages, setPages] = useState<Record<string, DashboardPage>>({});
  const [dbMeta, setDbMeta] = useState<Record<string, { title: string; icon: string | null }>>({});
  const [pageErr, setPageErr] = useState<string | null>(null);
  const current = stack[stack.length - 1];

  useEffect(() => {
    let alive = true;
    api
      .getDashboardProject(projectId)
      .then((p) => alive && setProject(p))
      .catch((e) => alive && setError(e instanceof Error ? e.message : "프로젝트를 불러오지 못했습니다."));
    Promise.all([api.listTeamMembers(), api.listAssignments({ projectId })])
      .then(([mb, asg]) => {
        if (!alive) return;
        setMembers(mb);
        setAssignments(asg);
      })
      .catch(() => {});
    Promise.all([
      api.listSupportPrograms(projectId),
      api.listMeetingNotes({ projectId }),
      api.listPartners(projectId),
      api.listWeeklyPeriods(projectId),
      api.listProjectBoardTasks(projectId),
    ])
      .then(([programs, meetings, partners, weeks, boardTasks]) => {
        if (alive) setRelated({ programs, meetings, partners, weeks, boardTasks });
      })
      .catch(() => {});
    api
      .listProjectNotes(projectId)
      .then((n) => alive && setProjectNotes(n))
      .catch(() => {});
    Promise.all([
      api.listDashboardProjects(),
      api.listProjectProgress(),
      api.listRequirementsSummary().catch(() => [] as RequirementSummary[]),
    ])
      .then(([all, prog, reqs]) => {
        if (!alive) return;
        setAllProjects(all);
        setProgress(Object.fromEntries(prog.map((p) => [p.project_id, p])));
        setReqSummary(Object.fromEntries(reqs.map((r) => [r.project_id, r])));
      })
      .catch(() => {});
    // 소속 데이터베이스 로드 + 칸반 보드 자동 보장(없을 때만 생성 — 멱등).
    api
      .listProjectDatabases(projectId)
      .then(async (dbs) => {
        if (!alive) return;
        if (!dbs.some((d) => d.views?.some((v) => v.type === "board"))) {
          try {
            await api.ensureProjectBoard(projectId);
            dbs = await api.listProjectDatabases(projectId);
          } catch {
            /* 보드 자동 생성 실패는 치명적이지 않다 */
          }
        }
        if (alive) setDatabases(dbs);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [projectId]);

  const subProjects = useMemo(
    () => allProjects.filter((p) => p.parent_project_id === projectId),
    [allProjects, projectId],
  );
  const parentProject = project?.parent_project_id
    ? (allProjects.find((p) => p.project_id === project.parent_project_id) ?? null)
    : null;
  const projectProgress = aggregateProgress(
    progress,
    projectId,
    subProjects.map((c) => c.project_id),
  );

  /** 하위 프로젝트 생성 → 노션처럼 바로 그 페이지로 이동. */
  async function addSubProject() {
    const name = window.prompt("하위 프로젝트 이름");
    if (!name?.trim()) return;
    try {
      const created = await api.createDashboardProject({
        name: name.trim(),
        color: project?.color ?? null,
        sort_order: 0,
        active: true,
        parent_project_id: projectId,
      });
      router.push(`/dashboard/projects/${created.project_id}`);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "하위 프로젝트 생성 실패");
    }
  }

  const memberOptions = useMemo(
    () => members.map((m) => ({ id: m.member_id, name: m.name })),
    [members],
  );
  const memberColors = useMemo(() => memberColorMap(members), [members]);

  const reloadAssignments = useCallback(async () => {
    setAssignments(await api.listAssignments({ projectId }));
  }, [projectId]);

  /** 프로젝트 필드 낙관적 패치 + 영속화. */
  const patchProject = useCallback(
    (patch: Partial<DashboardProject>) => {
      setProject((cur) => {
        if (!cur) return cur;
        const next = { ...cur, ...patch };
        void api
          .updateDashboardProject(projectId, {
            name: next.name,
            color: next.color,
            status: next.status,
            description: next.description,
            body: next.body,
            sort_order: next.sort_order,
            active: next.active,
            parent_project_id: next.parent_project_id,
            se_stage: next.se_stage,
            start_date: next.start_date,
            target_date: next.target_date,
            owner_member_id: next.owner_member_id,
            health: next.health,
          })
          .then(() => setSaveError(null))
          .catch(() => setSaveError("저장 실패 — 새로고침 전에 다시 시도하세요"));
        return next;
      });
    },
    [projectId],
  );

  // 하위 페이지 본문/제목 저장(savePageBody/renamePage)은 pageSave 큐를 통해 나간다 —
  // 직렬화 + 백오프 자동 재시도가 붙고, online/visibility 복귀 시에도 다시 시도한다.
  // 재시도가 소진돼 최종 실패하면 같은 saveError 배너로 노출한다.
  useEffect(() => {
    wirePageSaveRecovery();
    const syncSaveState = () => {
      const state = getPageSaveState();
      if (state.state === "error" && state.error) setSaveError(state.error);
    };
    syncSaveState();
    return subscribePageSaveState(syncSaveState);
  }, []);

  async function remove() {
    const msg =
      subProjects.length > 0
        ? `이 프로젝트를 삭제하시겠습니까? 하위 프로젝트 ${subProjects.length}개와 멤버 배정도 함께 삭제됩니다.`
        : "이 프로젝트를 삭제하시겠습니까? 멤버 배정도 함께 삭제됩니다.";
    if (!window.confirm(msg)) return;
    try {
      await api.deleteDashboardProject(projectId);
      router.push("/dashboard/projects");
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "삭제 실패");
    }
  }

  /* ---- 하위 페이지 / 데이터베이스 ---- */
  async function createPage() {
    const p = await api.createDashboardPage({ title: "새 페이지" });
    setPages((prev) => ({ ...prev, [p.page_id]: p }));
    return { page_id: p.page_id, title: p.title };
  }

  const createDatabase = useCallback(
    async ({
      view,
      template,
    }: {
      view: "table" | "board";
      template: "basic" | "tasks";
    }) => {
      const bundle = await api.createProjectDatabase({
        project_id: projectId,
        default_view: view,
        template,
        title: template === "tasks" ? "협업 업무표" : view === "board" ? "보드" : "데이터베이스",
      });
      api
        .listProjectDatabases(projectId)
        .then(setDatabases)
        .catch(() => {});
      return { database_id: bundle.database.database_id };
    },
    [projectId],
  );

  async function openPage(pageId: string) {
    if (!pages[pageId]) {
      try {
        const p = await api.getDashboardPage(pageId);
        setPages((prev) => ({ ...prev, [pageId]: p }));
      } catch {
        setPageErr("페이지를 불러오지 못했습니다.");
        return;
      }
    }
    setPageErr(null);
    setStack((prev) => [...prev, { kind: "page", pageId }]);
  }

  // 데이터베이스 링크 클릭 → 같은 스택에서 전체 페이지 데이터베이스로 들어간다(노션식).
  async function openDatabase(databaseId: string) {
    if (!dbMeta[databaseId]) {
      try {
        // 제목/아이콘만 필요 — 경량 meta endpoint 우선, 구 API(배포 skew) 404면
        // 기존 전체 번들 조회로 fallback.
        const m = await api
          .getProjectDatabaseMeta(databaseId)
          .catch(() =>
            api
              // body는 opt-in 생략(구 API는 파라미터를 무시하고 무해).
              .getProjectDatabase(databaseId, { body: "none" })
              .then((b) => ({ title: b.database.title, icon: b.database.icon })),
          );
        setDbMeta((prev) => ({
          ...prev,
          [databaseId]: { title: m.title, icon: m.icon },
        }));
      } catch {
        setPageErr("데이터베이스를 불러오지 못했습니다.");
        return;
      }
    }
    setPageErr(null);
    setStack((prev) => [...prev, { kind: "database", databaseId }]);
  }

  function savePageBody(pageId: string, body: string) {
    setPages((prev) => (prev[pageId] ? { ...prev, [pageId]: { ...prev[pageId], body } } : prev));
    // 빈 본문도 ""로 보낸다 — null은 서버가 "변경 없음"으로 취급해
    // 내용을 전부 지운 편집이 저장되지 않는다.
    // pageSave 큐 경유 — bare fire-and-forget(무 .catch, unhandled rejection) 대신
    // 직렬화 + 백오프 자동 재시도(소진 시 위 saveError 배너로 노출).
    enqueuePageSave(pageId, { body });
  }

  function renamePage(pageId: string, title: string) {
    const t = title.trim() || "새 페이지";
    setPages((prev) => (prev[pageId] ? { ...prev, [pageId]: { ...prev[pageId], title: t } } : prev));
    enqueuePageSave(pageId, { title: t });
    const idx = stack.findIndex((l) => l.kind === "page" && l.pageId === pageId);
    const parent = idx > 0 ? stack[idx - 1] : null;
    if (!parent || !project) return;
    if (parent.kind === "project") {
      const nb = setSubpageTitleInBody(project.body, pageId, t);
      if (nb !== project.body) patchProject({ body: nb });
    } else if (parent.kind === "page") {
      const pid = parent.pageId;
      const cur = pages[pid]?.body ?? null;
      const nb = setSubpageTitleInBody(cur, pageId, t);
      if (nb !== cur) {
        setPages((prev) => (prev[pid] ? { ...prev, [pid]: { ...prev[pid], body: nb } } : prev));
        enqueuePageSave(pid, { body: nb ?? "" });
      }
    }
  }

  if (error && !project) {
    return (
      <div className="mx-auto w-full max-w-[920px] px-3 py-4 sm:px-5">
        <BackLink />
        <div className="mt-3">
          <Banner tone="fail" title="오류">
            {error}
          </Banner>
        </div>
      </div>
    );
  }
  if (!project) {
    return (
      <div className="mx-auto w-full max-w-[920px] px-3 py-6 sm:px-5">
        <BackLink />
        <p className="mt-6 text-center text-[var(--text-body-sm)]" style={labelStyle()}>
          불러오는 중…
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-[920px] px-3 py-4 sm:px-5">
      <div className="mb-2 flex items-center justify-between">
        <span className="flex items-center gap-1">
          <BackLink />
          {parentProject ? (
            <Link
              href={`/dashboard/projects/${parentProject.project_id}`}
              className="flex h-9 items-center gap-1 rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              <span style={{ color: "var(--color-text-faint)" }}>/</span>
              {parentProject.name}
            </Link>
          ) : null}
        </span>
        <button
          type="button"
          onClick={remove}
          className="flex h-9 items-center rounded-[6px] px-2.5 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
          style={{ color: "var(--color-attention, #ef7f62)" }}
        >
          삭제
        </button>
      </div>

      {/* 브레드크럼(하위 페이지로 들어갔을 때) */}
      {stack.length > 1 ? (
        <div
          className="mb-2 flex flex-wrap items-center gap-1 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {stack.map((lvl, i) => {
            const label =
              lvl.kind === "project"
                ? project.name || "프로젝트"
                : lvl.kind === "database"
                  ? dbMeta[lvl.databaseId]?.title || "데이터베이스"
                  : pages[lvl.pageId]?.title || "페이지";
            const last = i === stack.length - 1;
            return (
              <span key={i} className="flex items-center gap-1">
                {i > 0 ? <span style={{ color: "var(--color-text-faint)" }}>/</span> : null}
                {last ? (
                  <span style={{ color: "var(--color-text-strong)", fontWeight: 600 }}>{label}</span>
                ) : (
                  <button
                    type="button"
                    onClick={() => setStack((prev) => prev.slice(0, i + 1))}
                    className="rounded-[4px] px-1 hover:bg-[var(--color-app-canvas)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {label}
                  </button>
                )}
              </span>
            );
          })}
        </div>
      ) : null}
      {pageErr ? (
        <p className="mb-2 text-[var(--text-body-sm)]" style={{ color: "var(--color-fail-fg)" }}>
          {pageErr}
        </p>
      ) : null}
      {saveError ? (
        <div
          className="mb-2 flex items-center justify-between gap-2 rounded-[8px] px-3 py-2 text-[var(--text-body-sm)]"
          style={{ background: "var(--color-fail-bg)", color: "var(--color-fail-fg)" }}
        >
          <span>{saveError}</span>
          <button
            type="button"
            onClick={() => setSaveError(null)}
            className="shrink-0 rounded-[6px] px-2 py-1 hover:opacity-80"
            style={{ border: "1px solid var(--color-fail-fg)" }}
          >
            닫기
          </button>
        </div>
      ) : null}

      {current.kind === "page" ? (
        <PageLevelView
          key={current.pageId}
          page={
            pages[current.pageId] ?? {
              page_id: current.pageId,
              title: "새 페이지",
              icon: null,
              body: null,
              kind: "page",
            }
          }
          onRename={(t) => renamePage(current.pageId, t)}
          onSaveBody={(b) => savePageBody(current.pageId, b)}
          onOpenPage={openPage}
          onOpenDatabase={openDatabase}
          onCreatePage={createPage}
          onCreateDatabase={createDatabase}
        />
      ) : current.kind === "database" ? (
        // 인라인으로 박지 않고, 클릭해 들어온 전체 페이지 데이터베이스(노션식).
        <DatabaseView key={current.databaseId} databaseId={current.databaseId} fullPage />
      ) : (
        <>
          <div className="flex items-start gap-3">
            <span
              className="mt-3 inline-block h-4 w-4 shrink-0 rounded-full"
              style={{ background: project.color || "var(--color-divider)" }}
            />
            <div className="min-w-0 flex-1">
              <AutoGrowTitle value={project.name} onCommit={(v) => v && patchProject({ name: v })} />
            </div>
          </div>

          <div className="mt-4 flex flex-col">
            <PropRow label="SE 단계">
              <SeStageStepper
                value={project.se_stage}
                onChange={(stage) => patchProject({ se_stage: stage })}
              />
            </PropRow>
            <PropRow label="진행률">
              <span className="flex h-7 items-center px-1">
                <ProgressBar value={projectProgress} />
              </span>
            </PropRow>
            <PropRow label="상태">
              <TagSelect
                value={project.status ?? ""}
                options={STATUSES}
                colorFor={statusColor}
                onChange={(v) => patchProject({ status: v || null })}
                variant="inline"
              />
            </PropRow>
            <PropRow label="기간">
              <span className="flex h-7 items-center gap-1 px-1 text-[var(--text-body-sm)]">
                <input
                  type="date"
                  value={project.start_date ?? ""}
                  onChange={(e) => patchProject({ start_date: e.target.value || null })}
                  aria-label="시작일"
                  className="rounded-[4px] bg-transparent px-1 outline-none hover:bg-[var(--color-app-canvas)]"
                  style={{ color: "var(--color-text-strong)", border: "1px solid transparent" }}
                />
                <span style={{ color: "var(--color-text-faint)" }}>→</span>
                <input
                  type="date"
                  value={project.target_date ?? ""}
                  onChange={(e) => patchProject({ target_date: e.target.value || null })}
                  aria-label="목표일"
                  className="rounded-[4px] bg-transparent px-1 outline-none hover:bg-[var(--color-app-canvas)]"
                  style={{ color: "var(--color-text-strong)", border: "1px solid transparent" }}
                />
                <TargetCountdown target={project.target_date} />
              </span>
            </PropRow>
            <PropRow label="오너 (DRI)">
              <select
                value={project.owner_member_id ?? ""}
                onChange={(e) => patchProject({ owner_member_id: e.target.value || null })}
                aria-label="프로젝트 오너"
                className="h-7 rounded-[4px] bg-transparent px-1 text-[var(--text-body-sm)] outline-none hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-strong)" }}
              >
                <option value="">미지정</option>
                {members.map((m) => (
                  <option key={m.member_id} value={m.member_id}>
                    {m.name}
                  </option>
                ))}
              </select>
            </PropRow>
            <PropRow label="건강">
              <HealthSelect
                value={project.health}
                onChange={(v) => patchProject({ health: v })}
              />
            </PropRow>
            <PropRow label="색상">
              <input
                type="color"
                value={project.color || "#5d9bd8"}
                onChange={(e) => patchProject({ color: e.target.value })}
                className="h-7 w-10 cursor-pointer rounded-[4px] border-0 bg-transparent p-0"
                aria-label="프로젝트 색상"
              />
            </PropRow>
            <PropRow label="멤버 · 역할">
              <AssignmentChips
                scope="project"
                ownerId={project.project_id}
                options={memberOptions}
                assignments={assignments}
                colors={memberColors}
                onChanged={reloadAssignments}
              />
            </PropRow>
            <PropRow label="요약">
              <PropTextInput
                value={project.description ?? ""}
                placeholder="목록에 보일 한 줄 요약"
                onCommit={(v) => patchProject({ description: v || null })}
              />
            </PropRow>
          </div>

          <PeekDivider />

          {/* SE 요구조건 대장 (SRR): 제약조건+목표, 상태로 검증 추적, 하위는 flow-down */}
          <RequirementsSection
            projectId={projectId}
            parentProjectId={project.parent_project_id}
            parentName={parentProject?.name}
          />

          <PeekDivider />

          {/* 프로젝트 티켓 — 팀원들이 이 프로젝트 채널(내 보드)에 올린 회사 공개 업무.
              scope='company' 행만 오며(개인 채널 업무 비노출), 에이전트/CLI가 만든
              티켓도 여기 모인다. 곁다리 peek이 아니라 1급 섹션(요구조건 바로 아래). */}
          <ProjectTicketsSection tasks={related?.boardTasks ?? null} />

          <PeekDivider />

          {/* 노션식 자유 본문: 볼드/헤딩/목록/이미지/토글/하위 페이지(/) + 데이터베이스(/)·보드(/) */}
          <RichBodyEditor
            key={project.project_id}
            id={project.project_id}
            value={project.body ?? ""}
            minHeight={360}
            onCommit={(v) => patchProject({ body: v || null })}
            onOpenPage={openPage}
            onOpenDatabase={openDatabase}
            onCreatePage={createPage}
            onCreateDatabase={createDatabase}
          />

          <PeekDivider />

          <ActivityFeedSection projectId={projectId} />

          <PeekDivider />

          {/* 보드 · 데이터베이스 — 모든 프로젝트가 자기 칸반 보드를 가진다(자동 보장). */}
          <PeekSection title="보드 · 데이터베이스" count={databases.length}>
            {databases.length === 0 ? (
              <PeekEmpty>보드를 준비하는 중…</PeekEmpty>
            ) : (
              databases.map((db) => {
                const viewType = db.views?.[0]?.type ?? "table";
                return (
                  <button
                    key={db.database_id}
                    type="button"
                    onClick={() => openDatabase(db.database_id)}
                    className="flex min-h-[36px] w-full items-center gap-2 rounded-[6px] px-1.5 py-1 text-left hover:bg-[var(--color-app-canvas)]"
                  >
                    <span className="shrink-0">{db.icon ?? (viewType === "board" ? "📋" : "🗂️")}</span>
                    <span
                      className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                      style={{ color: "var(--color-text-strong)", fontWeight: 500 }}
                    >
                      {db.title}
                    </span>
                    <TagPill
                      label={viewType === "board" ? "칸반 보드" : "표"}
                      color={viewType === "board" ? "blue" : "gray"}
                    />
                  </button>
                );
              })
            )}
          </PeekSection>

          {/* 관련 메모 — 이 프로젝트에 링크된 개인 메모(메모 쪽에서 프로젝트를 링크하면 여기 표시). */}
          <PeekSection
            title="관련 메모"
            count={projectNotes.length}
            action={
              <Link
                href="/notes"
                className="flex min-h-[32px] items-center rounded-[6px] px-2 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                메모 열기 ↗
              </Link>
            }
          >
            {projectNotes.length === 0 ? (
              <PeekEmpty>
                링크된 메모가 없습니다. 메모(좌측 개인 › 메모)에서 이 프로젝트를 링크하세요.
              </PeekEmpty>
            ) : (
              projectNotes.map((n) => (
                <Link
                  key={n.page_id}
                  href="/notes"
                  className="flex min-h-[36px] items-center gap-2 rounded-[6px] px-1.5 py-1 hover:bg-[var(--color-app-canvas)]"
                >
                  <span className="shrink-0">{n.icon || "📝"}</span>
                  <span
                    className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                    style={{ color: "var(--color-text-strong)", fontWeight: 500 }}
                  >
                    {n.title || "제목 없음"}
                  </span>
                  {n.updated_at ? (
                    <span
                      className="shrink-0 text-[var(--text-meta)]"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      {n.updated_at.slice(0, 10)}
                    </span>
                  ) : null}
                </Link>
              ))
            )}
          </PeekSection>

          {/* 하위 프로젝트 — 루트 프로젝트에서만 추가 가능(깊이 2단계). */}
          {!project.parent_project_id || subProjects.length > 0 ? (
            <PeekSection
              title="하위 프로젝트"
              count={subProjects.length}
              action={
                !project.parent_project_id ? (
                  <button
                    type="button"
                    onClick={addSubProject}
                    className="flex min-h-[32px] items-center rounded-[6px] px-2 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    ＋ 추가
                  </button>
                ) : undefined
              }
            >
              {subProjects.length === 0 ? (
                <PeekEmpty>하위 프로젝트가 없습니다. ＋ 추가로 만들어 보세요.</PeekEmpty>
              ) : (
                subProjects.map((sp) => (
                  <Link
                    key={sp.project_id}
                    href={`/dashboard/projects/${sp.project_id}`}
                    className="flex min-h-[36px] items-center gap-2 rounded-[6px] px-1.5 py-1 hover:bg-[var(--color-app-canvas)]"
                  >
                    <span
                      className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ background: sp.color || "var(--color-divider)" }}
                    />
                    <span
                      className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                      style={{ color: "var(--color-text-strong)", fontWeight: 500 }}
                    >
                      {sp.name}
                    </span>
                    {sp.status ? <TagPill label={sp.status} color={statusColor(sp.status)} /> : null}
                    {reqSummary[sp.project_id]?.total ? (
                      <span
                        className="shrink-0 text-[var(--text-meta)] tabular-nums"
                        title="요구조건 충족/전체"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        요구 {reqSummary[sp.project_id].satisfied}/{reqSummary[sp.project_id].total}
                      </span>
                    ) : null}
                    <ProgressBar value={aggregateProgress(progress, sp.project_id)} compact />
                  </Link>
                ))
              )}
            </PeekSection>
          ) : null}

          {related ? (
            <RelatedPanels related={related} />
          ) : (
            <p className="text-[var(--text-meta)]" style={labelStyle()}>
              연결 데이터 불러오는 중…
            </p>
          )}
        </>
      )}
    </div>
  );
}

function BackLink() {
  return (
    <Link
      href="/dashboard/projects"
      className="flex h-9 items-center rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
      style={{ color: "var(--color-text-muted)" }}
    >
      ← 프로젝트
    </Link>
  );
}

const TICKET_PRIORITY_RANK: Record<string, number> = { urgent: 0, priority: 1, normal: 2, low: 3 };

function ticketPriorityPill(priority: string): { label: string; color: TagColor } | null {
  if (priority === "urgent") return { label: "긴급", color: "red" };
  if (priority === "priority") return { label: "우선", color: "orange" };
  if (priority === "low") return { label: "낮음", color: "teal" };
  return null; // normal은 pill 생략 — 목록 소음을 줄인다.
}

/** 프로젝트 티켓 — 전 팀원이 이 프로젝트 채널(내 보드)에 올린 회사 공개 업무.
    미완료(우선순위→날짜순) 먼저, 완료는 아래로. scope='company' 행만 온다. */
function ProjectTicketsSection({ tasks }: { tasks: ProjectBoardTask[] | null }) {
  const sorted = [...(tasks ?? [])].sort((a, b) => {
    const openA = a.status === "open" ? 0 : 1;
    const openB = b.status === "open" ? 0 : 1;
    if (openA !== openB) return openA - openB;
    const pa = TICKET_PRIORITY_RANK[a.priority] ?? 2;
    const pb = TICKET_PRIORITY_RANK[b.priority] ?? 2;
    if (pa !== pb) return pa - pb;
    return (a.scheduled_date ?? "9999-99-99").localeCompare(b.scheduled_date ?? "9999-99-99");
  });
  const openCount = sorted.filter((t) => t.status === "open").length;
  return (
    <PeekSection title="티켓" count={tasks === null ? 0 : openCount}>
      {tasks === null ? (
        <PeekEmpty>티켓을 불러오는 중…</PeekEmpty>
      ) : sorted.length === 0 ? (
        <PeekEmpty>
          아직 티켓이 없습니다. 내 보드에서 이 프로젝트 채널에 업무를 올리면 여기 모입니다.
        </PeekEmpty>
      ) : (
        sorted.slice(0, 30).map((t) => {
          const pill = ticketPriorityPill(t.priority);
          return (
            <div
              key={t.task_id}
              className="flex min-h-[36px] items-center gap-2 rounded-[6px] px-1.5 py-1"
            >
              <span className="shrink-0 text-[var(--text-meta)] tabular-nums" style={labelStyle()}>
                {t.owner_name ?? "—"}
              </span>
              {pill ? <TagPill label={pill.label} color={pill.color} /> : null}
              <span
                className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                style={{
                  color: "var(--color-text-body)",
                  textDecoration: t.completed ? "line-through" : undefined,
                  opacity: t.completed ? 0.55 : 1,
                }}
              >
                {t.title}
              </span>
              {t.due_date ? (
                <span className="shrink-0 text-[var(--text-meta)]" style={labelStyle()}>
                  마감 {t.due_date.slice(5)}
                </span>
              ) : null}
              <span className="shrink-0 text-[var(--text-meta)] tabular-nums" style={labelStyle()}>
                {t.scheduled_date ? t.scheduled_date.slice(5) : "백로그"}
              </span>
            </div>
          );
        })
      )}
    </PeekSection>
  );
}

function RelatedPanels({ related }: { related: Related }) {
  return (
    <>
      <PeekSection
        title="지원사업"
        count={related.programs.length}
        action={<PeekLink href="/dashboard/support" />}
      >
        {related.programs.length === 0 ? (
          <PeekEmpty>연결된 지원사업이 없습니다.</PeekEmpty>
        ) : (
          related.programs.map((g) => (
            <Link
              key={g.program_id}
              href="/dashboard/support"
              className="flex min-h-[36px] items-center gap-2 rounded-[6px] px-1.5 py-1 hover:bg-[var(--color-app-canvas)]"
            >
              <TagPill
                label={g.status}
                color={
                  g.status === "합격"
                    ? "green"
                    : g.status === "유감"
                      ? "pink"
                      : g.status === "시작 전"
                        ? "gray"
                        : "blue"
                }
              />
              <span
                className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                style={{ color: "var(--color-text-strong)" }}
              >
                {g.name}
              </span>
              {g.deadline ? (
                <span className="text-[var(--text-meta)]" style={labelStyle()}>
                  {g.deadline}
                </span>
              ) : null}
            </Link>
          ))
        )}
      </PeekSection>

      <PeekSection
        title="회의록"
        count={related.meetings.length}
        action={<PeekLink href="/dashboard/meetings" />}
      >
        {related.meetings.length === 0 ? (
          <PeekEmpty>연결된 회의록이 없습니다.</PeekEmpty>
        ) : (
          related.meetings.slice(0, 5).map((m) => (
            <Link
              key={m.note_id}
              href="/dashboard/meetings"
              className="flex min-h-[36px] items-center gap-2 rounded-[6px] px-1.5 py-1 hover:bg-[var(--color-app-canvas)]"
            >
              <span className="shrink-0 text-[var(--text-meta)] tabular-nums" style={labelStyle()}>
                {m.meeting_date}
              </span>
              <span
                className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                style={{ color: "var(--color-text-strong)" }}
              >
                {m.title}
              </span>
            </Link>
          ))
        )}
      </PeekSection>

      <PeekSection
        title="파트너사"
        count={related.partners.length}
        action={<PeekLink href="/dashboard/partners" />}
      >
        {related.partners.length === 0 ? (
          <PeekEmpty>연결된 파트너사가 없습니다.</PeekEmpty>
        ) : (
          related.partners.slice(0, 5).map((pt) => (
            <Link
              key={pt.partner_id}
              href="/dashboard/partners"
              className="flex min-h-[36px] items-center gap-2 rounded-[6px] px-1.5 py-1 hover:bg-[var(--color-app-canvas)]"
            >
              <span
                className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                style={{ color: "var(--color-text-strong)" }}
              >
                {pt.name}
              </span>
              {pt.status ? <TagPill label={pt.status} color="teal" /> : null}
            </Link>
          ))
        )}
      </PeekSection>

      <PeekSection
        title="계획 · 회고"
        count={related.weeks.length}
        action={<PeekLink href="/dashboard/plans" />}
      >
        {related.weeks.length === 0 ? (
          <PeekEmpty>주간 계획 기록이 없습니다.</PeekEmpty>
        ) : (
          related.weeks.slice(0, 4).map((w) => (
            <Link
              key={w.period}
              href="/dashboard/plans"
              className="flex min-h-[36px] items-center gap-2 rounded-[6px] px-1.5 py-1 hover:bg-[var(--color-app-canvas)]"
            >
              <span className="shrink-0 text-[var(--text-meta)] tabular-nums" style={labelStyle()}>
                {w.period} 주
              </span>
              <span
                className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                style={{ color: "var(--color-text-body)" }}
              >
                계획 {w.plan_done}/{w.plan_count} · 회고 {w.retro_count}
              </span>
            </Link>
          ))
        )}
      </PeekSection>

      {/* 채널 업무는 상단 1급 "티켓" 섹션(ProjectTicketsSection)으로 승격 — 중복 제거. */}
    </>
  );
}

function PeekLink({ href }: { href: string }) {
  return (
    <Link
      href={href}
      className="flex min-h-[32px] items-center rounded-[6px] px-2 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
      style={{ color: "var(--color-text-muted)" }}
    >
      전체 보기 →
    </Link>
  );
}

function PeekEmpty({ children }: { children: React.ReactNode }) {
  return (
    <p className="px-1.5 py-1 text-[var(--text-meta)]" style={labelStyle()}>
      {children}
    </p>
  );
}
