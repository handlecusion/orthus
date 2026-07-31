"use client";

import React from "react";
import dynamic from "next/dynamic";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  closestCenter,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import {
  api,
  type DashboardProject,
  type SupportNote,
  type SupportNoteInput,
  type SupportProgram,
  type SupportProgramInput,
  type TeamMember,
} from "@/lib/api";
import {
  DatePicker,
  TagPill,
  TagSelect,
  TAG_COLORS,
  labelStyle,
  memberColorMap,
  memberPillColor,
  type TagColor,
} from "@/components/dashboard/kit";
import {
  AutoGrowTitle,
  PeekDivider,
  PropRow,
  PropTextInput,
  PropUrlInput,
  SidePeek,
} from "@/components/dashboard/peek";
import { useGrabPan } from "@/lib/useGrabPan";
import { toExternalHref } from "@/lib/external-url";
import { hasTextSelectionWithin } from "@/lib/cardCopy";
import { ChannelHeader } from "@/components/ui";

// BlockNote는 window를 만지므로 클라이언트 전용으로 로드한다.
const RichBodyEditor = dynamic(
  () => import("@/components/dashboard/RichBody").then((m) => m.RichBodyEditor),
  { ssr: false },
);

// 보드 카드에서는 텍스트도 카드 드래그 시작점이다. 텍스트 선택/복사는 상세 패널에서 한다.
function supportShouldIgnoreCardDrag(target: EventTarget | null) {
  if (!(target instanceof Element)) return false;
  return Boolean(target.closest("button, textarea, select, a, input, [data-no-card-drag]"));
}

class SupportCardPointerSensor extends PointerSensor {
  static activators = [
    {
      eventName: "onPointerDown" as const,
      handler: ({ nativeEvent }: React.PointerEvent) => {
        if (!nativeEvent.isPrimary || nativeEvent.button !== 0) return false;
        return !supportShouldIgnoreCardDrag(nativeEvent.target);
      },
    },
  ];
}

/* ------------------------------- 상태 정의 ------------------------------ */

const STATUSES = [
  "시작 전",
  "서류제출완",
  "발표준비",
  "합격",
  "유감",
  "협약취소",
] as const;

// 시작 전만 마감일 순으로 보여주고, 나머지 컬럼은 sort_order(상태변경순 + 드래그
// 정렬)로 보여준다.
const DEADLINE_SORTED_STATUS = "시작 전";

// D-day 배지를 숨기는 종료 상태(마감일 의미가 사라진 컬럼).
const DONE_STATUSES = new Set(["합격", "유감", "협약취소"]);

const STATUS_COLOR: Record<string, TagColor> = {
  "시작 전": "gray",
  서류제출완: "blue",
  발표준비: "blue",
  합격: "green",
  유감: "pink",
  협약취소: "brown",
};

function statusColor(s: string): TagColor {
  return STATUS_COLOR[s] ?? "gray";
}

const PROJECT_TAG_COLORS: TagColor[] = [
  "orange",
  "teal",
  "purple",
  "brown",
  "yellow",
  "blue",
  "pink",
  "red",
];

function projectColor(name: string): TagColor {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return PROJECT_TAG_COLORS[h % PROJECT_TAG_COLORS.length];
}

function dday(deadline: string | null): { label: string; urgent: boolean } | null {
  if (!deadline) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const d = new Date(`${deadline}T00:00:00`);
  if (Number.isNaN(d.getTime())) return null;
  const diff = Math.round((d.getTime() - today.getTime()) / 86400000);
  if (diff === 0) return { label: "D-DAY", urgent: true };
  if (diff > 0) return { label: `D-${diff}`, urgent: diff <= 7 };
  return { label: `D+${-diff}`, urgent: false };
}

function fmtDate(v: string | null): string {
  if (!v) return "";
  const d = new Date(`${v}T00:00:00`);
  if (Number.isNaN(d.getTime())) return v;
  return `${d.getFullYear()}년 ${d.getMonth() + 1}월 ${d.getDate()}일`;
}

function toInput(p: SupportProgram): SupportProgramInput {
  return {
    name: p.name,
    status: p.status,
    project_id: p.project_id,
    company: p.company,
    deadline: p.deadline,
    presentation_deadline: p.presentation_deadline,
    presentation_date: p.presentation_date,
    presentation_time: p.presentation_time,
    owner_member_id: p.owner_member_id,
    task_number: p.task_number,
    url: p.url,
    follow_up: p.follow_up,
    body: p.body,
    sort_order: p.sort_order,
  };
}

/** 발표 단계 이후(발표준비/합격/유감)이거나 값이 이미 있으면 발표 필드 노출. */
function showsPresentation(p: SupportProgram): boolean {
  return (
    p.status === "발표준비" ||
    DONE_STATUSES.has(p.status) ||
    p.presentation_deadline != null ||
    p.presentation_date != null
  );
}

/* -------------------------------- 페이지 -------------------------------- */

export default function SupportBoardPage() {
  const [items, setItems] = React.useState<SupportProgram[]>([]);
  const [projects, setProjects] = React.useState<DashboardProject[]>([]);
  const [members, setMembers] = React.useState<TeamMember[]>([]);
  const [notes, setNotes] = React.useState<SupportNote[]>([]);
  const [loaded, setLoaded] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [openId, setOpenId] = React.useState<string | null>(null);
  const [dragId, setDragId] = React.useState<string | null>(null);
  const [projectFilter, setProjectFilter] = React.useState<string>("");
  // 빈 공간을 잡고 끌어서 좌우 스크롤 (내 보드와 동일)
  const scrollRef = React.useRef<HTMLDivElement | null>(null);
  useGrabPan(scrollRef);

  const sensors = useSensors(
    useSensor(SupportCardPointerSensor, { activationConstraint: { distance: 6 } }),
  );

  const reload = React.useCallback(
    () =>
      Promise.all([
        api.listSupportPrograms(),
        api.listDashboardProjects(),
        api.listTeamMembers(),
        api.listSupportNotes(),
      ])
        .then(([list, projs, mbrs, nts]) => {
          setItems(list);
          setProjects(projs.filter((p) => p.active));
          setMembers(mbrs);
          setNotes(nts);
          setError(null);
        })
        .catch((e: unknown) => {
          setError(e instanceof Error ? e.message : "불러오기 실패");
        })
        .finally(() => setLoaded(true)),
    [],
  );

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const memberColors = React.useMemo(() => memberColorMap(members), [members]);

  const visible = projectFilter
    ? items.filter((i) => i.project_id === projectFilter)
    : items;

  const byStatus = React.useMemo(() => {
    const map = new Map<string, SupportProgram[]>();
    for (const s of STATUSES) map.set(s, []);
    for (const it of visible) {
      const bucket = map.get(it.status) ?? map.get("시작 전")!;
      bucket.push(it);
    }
    for (const [status, bucket] of map.entries()) {
      if (status === DEADLINE_SORTED_STATUS) {
        // 시작 전: 마감일 오름차순(없으면 뒤로)
        bucket.sort((a, b) => {
          const da = a.deadline ?? "9999-12-31";
          const db = b.deadline ?? "9999-12-31";
          return da < db ? -1 : da > db ? 1 : 0;
        });
      } else {
        // 나머지: sort_order(상태변경순 + 드래그 정렬) 오름차순
        bucket.sort((a, b) => a.sort_order - b.sort_order || (a.created_at ?? "").localeCompare(b.created_at ?? ""));
      }
    }
    return map;
  }, [visible]);

  const open = openId ? items.find((i) => i.program_id === openId) ?? null : null;
  const dragging = dragId
    ? items.find((i) => i.program_id === dragId) ?? null
    : null;

  const itemsRef = React.useRef(items);
  React.useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  /** Optimistic local patch + server PATCH; server response wins (calendar link). */
  const patchItem = React.useCallback(
    (programId: string, patch: Partial<SupportProgramInput>) => {
      const cur = itemsRef.current.find((i) => i.program_id === programId);
      if (!cur) return;
      const next: SupportProgram = {
        ...cur,
        ...patch,
        project_name:
          "project_id" in patch
            ? (projects.find((p) => p.project_id === patch.project_id)?.name ??
              null)
            : cur.project_name,
        owner_name:
          "owner_member_id" in patch
            ? (members.find((m) => m.member_id === patch.owner_member_id)?.name ??
              null)
            : cur.owner_name,
      };
      setItems((prev) => prev.map((i) => (i.program_id === programId ? next : i)));
      void api
        .updateSupportProgram(programId, toInput(next))
        .then((saved) => {
          setItems((prev) =>
            prev.map((i) => (i.program_id === programId ? saved : i)),
          );
        })
        .catch(() => void reload());
    },
    [projects, members, reload],
  );

  async function createIn(status: string) {
    const bucket = byStatus.get(status) ?? [];
    const maxSort = bucket.reduce((m, i) => Math.max(m, i.sort_order), 0);
    try {
      const created = await api.createSupportProgram({
        name: "제목 없음",
        status,
        project_id: projectFilter || null,
        company: null,
        deadline: null,
        presentation_deadline: null,
        presentation_date: null,
        presentation_time: null,
        owner_member_id: null,
        task_number: null,
        url: null,
        follow_up: null,
        body: null,
        sort_order: maxSort + 1,
      });
      setItems((prev) => [...prev, created]);
      setOpenId(created.program_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "생성 실패");
    }
  }

  async function removeItem(programId: string) {
    if (!window.confirm("이 지원사업을 삭제할까요? 연결된 발표 일정도 함께 삭제됩니다."))
      return;
    setOpenId(null);
    setItems((prev) => prev.filter((i) => i.program_id !== programId));
    try {
      await api.deleteSupportProgram(programId);
    } catch {
      void reload();
    }
  }

  function handleDragStart(e: DragStartEvent) {
    setDragId(String(e.active.id));
  }

  function handleDragEnd(e: DragEndEvent) {
    setDragId(null);
    const overId = e.over?.id ? String(e.over.id) : null;
    if (!overId) return;
    const id = String(e.active.id);
    const item = items.find((i) => i.program_id === id);
    if (!item) return;

    // over가 컬럼이면 그 컬럼 끝에, 카드면 그 카드 위치에 끼워 넣는다.
    let target: string;
    let overItem: SupportProgram | null = null;
    if (overId.startsWith("col:")) {
      target = overId.slice(4);
    } else {
      overItem = items.find((i) => i.program_id === overId) ?? null;
      if (!overItem) return;
      target = overItem.status;
    }
    if (!STATUSES.includes(target as (typeof STATUSES)[number])) return;

    // 시작 전 컬럼은 마감일 정렬이라 수동 재정렬을 적용하지 않는다(컬럼 이동만 허용).
    if (target === DEADLINE_SORTED_STATUS) {
      if (item.status === target) return; // 같은 컬럼 내 재정렬 무시
      patchItem(id, { status: target });
      return;
    }

    // 대상 컬럼의 현재 순서에서 드래그 카드를 빼고, over 위치에 다시 끼운다.
    const current = (byStatus.get(target) ?? []).map((i) => i.program_id);
    const without = current.filter((pid) => pid !== id);
    let insertAt = without.length;
    if (overItem && overItem.program_id !== id) {
      const idx = without.indexOf(overItem.program_id);
      if (idx >= 0) insertAt = idx;
    }
    without.splice(insertAt, 0, id);

    // 순서/컬럼이 모두 그대로면 아무 것도 안 한다.
    const sameOrder =
      item.status === target &&
      current.length === without.length &&
      current.every((pid, i) => pid === without[i]);
    if (sameOrder) return;

    // 낙관적 갱신: 대상 컬럼의 sort_order/status를 새 순서로 다시 매긴다.
    const orderMap = new Map(without.map((pid, i) => [pid, i]));
    setItems((prev) =>
      prev.map((i) =>
        orderMap.has(i.program_id)
          ? { ...i, status: target, sort_order: orderMap.get(i.program_id)! }
          : i,
      ),
    );
    void api
      .reorderSupportPrograms(target, without)
      .then((saved) => setItems(saved))
      .catch(() => void reload());
  }

  return (
    <div className="mx-auto w-full max-w-[1400px] px-3 pb-10 pt-3 sm:px-5">
      {/* Slack 스타일 채널 헤더 — 기존 title/count/필터 select를 그대로 옮김 */}
      <ChannelHeader
        glyph="#"
        title="지원사업"
        subtitle={`${visible.length}건`}
        right={
          <select
            value={projectFilter}
            onChange={(e) => setProjectFilter(e.target.value)}
            className="rounded-[6px] px-2 text-[var(--text-body-sm)]"
            style={{
              minHeight: 36,
              border: "1px solid var(--color-divider)",
              background: "var(--color-card)",
              color: projectFilter
                ? "var(--color-text-strong)"
                : "var(--color-text-muted)",
            }}
            aria-label="프로젝트 필터"
          >
            <option value="">전체 프로젝트</option>
            {projects.map((p) => (
              <option key={p.project_id} value={p.project_id}>
                {p.name}
              </option>
            ))}
          </select>
        }
      />

      {error ? (
        <div
          className="mb-2 flex items-center justify-between gap-2 rounded-[8px] px-3 py-2 text-[var(--text-body-sm)]"
          style={{ background: "#fdebe8", color: "#88321f" }}
        >
          <span className="min-w-0 truncate">{error}</span>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={() => void reload()}
              className="rounded-[6px] px-2 py-1 font-semibold hover:bg-[rgba(0,0,0,0.05)]"
            >
              다시 시도
            </button>
            <button
              type="button"
              onClick={() => setError(null)}
              aria-label="에러 닫기"
              className="flex h-7 w-7 items-center justify-center rounded-[6px] hover:bg-[rgba(0,0,0,0.05)]"
            >
              ✕
            </button>
          </div>
        </div>
      ) : null}

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        <div ref={scrollRef} className="flex items-start gap-3 overflow-x-auto pb-2">
          {STATUSES.map((status) => (
            <BoardColumn
              key={status}
              status={status}
              items={byStatus.get(status) ?? []}
              loaded={loaded}
              memberColors={memberColors}
              onOpen={setOpenId}
              onCreate={() => void createIn(status)}
            />
          ))}
        </div>
        <DragOverlay dropAnimation={null}>
          {dragging ? (
            <CardBody item={dragging} memberColors={memberColors} overlay />
          ) : null}
        </DragOverlay>
      </DndContext>

      {/* 보드 아래 인라인 DB — 노션 페이지 구성과 동일 */}
      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <NotesSection
          kind="tip"
          title="📝 지원사업 꿀팁"
          notes={notes.filter((n) => n.kind === "tip")}
          setNotes={setNotes}
          onError={setError}
          reload={() => void reload()}
        />
        <NotesSection
          kind="site"
          title="🔍 지원사업 검색 사이트"
          notes={notes.filter((n) => n.kind === "site")}
          setNotes={setNotes}
          onError={setError}
          reload={() => void reload()}
        />
      </div>

      {open ? (
        <ProgramPeek
          item={open}
          projects={projects}
          members={members}
          onClose={() => setOpenId(null)}
          onPatch={(patch) => patchItem(open.program_id, patch)}
          onDelete={() => void removeItem(open.program_id)}
        />
      ) : null}
    </div>
  );
}

/* --------------------------------- 컬럼 --------------------------------- */

function BoardColumn({
  status,
  items,
  loaded,
  memberColors,
  onOpen,
  onCreate,
}: {
  status: string;
  items: SupportProgram[];
  loaded: boolean;
  memberColors: Map<string, string>;
  onOpen: (id: string) => void;
  onCreate: () => void;
}) {
  const { isOver, setNodeRef } = useDroppable({ id: `col:${status}` });
  const c = TAG_COLORS[statusColor(status)];
  return (
    <div
      ref={setNodeRef}
      className="flex w-[272px] shrink-0 flex-col rounded-[10px] transition-colors"
      style={{
        background: isOver ? "var(--color-sidebar)" : "var(--color-panel)",
        border: "1px solid var(--color-divider-soft)",
        maxHeight: "68vh",
      }}
    >
      <div className="flex items-center justify-between gap-1 px-2.5 pb-1 pt-2.5">
        <div className="flex min-w-0 items-center gap-1.5">
          <span
            className="inline-flex items-center rounded-[4px] px-2 py-[2px] text-[var(--text-meta)] font-semibold whitespace-nowrap"
            style={{ background: c.bg, color: c.fg }}
          >
            {status}
          </span>
          <span
            className="text-[var(--text-meta)]"
            style={{ color: "var(--color-text-faint)" }}
          >
            {items.length}
          </span>
        </div>
        <button
          type="button"
          onClick={onCreate}
          aria-label={`${status}에 추가`}
          className="flex h-7 w-7 items-center justify-center rounded-[6px] text-[15px] hover:bg-[var(--color-card)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          +
        </button>
      </div>
      <div className="flex min-h-[40px] flex-col gap-1.5 overflow-y-auto px-2 pb-1.5">
        {items.map((item) => (
          <DraggableCard
            key={item.program_id}
            item={item}
            memberColors={memberColors}
            onOpen={onOpen}
          />
        ))}
        {loaded && items.length === 0 ? (
          <div
            className="px-1 py-1.5 text-[var(--text-meta)]"
            style={{ color: "var(--color-text-faint)" }}
          >
            비어 있음
          </div>
        ) : null}
      </div>
      <button
        type="button"
        onClick={onCreate}
        className="mx-2 mb-2 flex items-center gap-1 rounded-[6px] px-1.5 py-1.5 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-card)]"
        style={{ color: "var(--color-text-muted)", minHeight: 36 }}
      >
        <span className="text-[15px] leading-none">+</span> 새로 만들기
      </button>
    </div>
  );
}

/* --------------------------------- 카드 --------------------------------- */

function DraggableCard({
  item,
  memberColors,
  onOpen,
}: {
  item: SupportProgram;
  memberColors: Map<string, string>;
  onOpen: (id: string) => void;
}) {
  const { attributes, isDragging, listeners, setNodeRef } = useDraggable({
    id: item.program_id,
  });
  // 카드 자체를 droppable로 만들어 그 위치에 끼워 넣는 재정렬을 가능하게 한다.
  const { setNodeRef: setDropRef, isOver } = useDroppable({ id: item.program_id });
  return (
    <div
      ref={(el) => {
        setNodeRef(el);
        setDropRef(el);
      }}
      {...attributes}
      {...listeners}
      onClick={(e) => {
        // 과거 카드 텍스트 선택 제스처가 남아 있으면 상세 열기를 막는다.
        // 현재 보드 카드 텍스트는 select-none이라 복사는 상세 패널에서 한다.
        if (hasTextSelectionWithin(e.currentTarget)) return;
        onOpen(item.program_id);
      }}
      style={{
        opacity: isDragging ? 0.35 : 1,
        boxShadow: isOver ? "inset 0 2px 0 var(--color-text-strong)" : undefined,
      }}
    >
      <CardBody item={item} memberColors={memberColors} />
    </div>
  );
}

function CardBody({
  item,
  memberColors,
  overlay,
}: {
  item: SupportProgram;
  memberColors: Map<string, string>;
  overlay?: boolean;
}) {
  const done = DONE_STATUSES.has(item.status);
  const inPresentation = item.status === "발표준비";
  const d = dday(item.deadline);
  const pd = dday(item.presentation_date);
  return (
    <div
      className="cursor-grab select-none rounded-[8px] px-2.5 py-2 active:cursor-grabbing"
      style={{
        background: "var(--color-card)",
        boxShadow: overlay
          ? "var(--shadow-modal)"
          : "0 1px 4px rgba(0,0,0,0.08)",
        border: "1px solid var(--color-divider-soft)",
      }}
    >
      <div
        className="text-[var(--text-body-sm)] font-medium leading-snug"
        style={{ color: "var(--color-text-strong)" }}
        data-card-text=""
      >
        {item.name || "제목 없음"}
      </div>
      {item.project_name ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1">
          <TagPill label={item.project_name} color={projectColor(item.project_name)} />
        </div>
      ) : null}
      {item.deadline && !inPresentation ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span
            className="text-[var(--text-meta)]"
            style={{
              color:
                d?.urgent && !done
                  ? "var(--color-attention, #ef7f62)"
                  : "var(--color-text-muted)",
              fontWeight: d?.urgent && !done ? 700 : 500,
            }}
            data-card-text=""
          >
            마감 {item.deadline}
            {d && !done ? ` · ${d.label}` : ""}
          </span>
        </div>
      ) : null}
      {inPresentation && (item.presentation_deadline || item.presentation_date) ? (
        <div className="mt-1.5 flex flex-col gap-0.5">
          {item.presentation_deadline ? (
            <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
              준비 마감 {item.presentation_deadline}
            </span>
          ) : null}
          {item.presentation_date ? (
            <span
              className="text-[var(--text-meta)]"
              style={{
                color: pd?.urgent
                  ? "var(--color-attention, #ef7f62)"
                  : "var(--color-text-muted)",
                fontWeight: pd?.urgent ? 700 : 500,
              }}
            >
              발표 {item.presentation_date}
              {item.presentation_time ? ` ${item.presentation_time.slice(0, 5)}` : ""}
              {pd ? ` · ${pd.label}` : ""}
            </span>
          ) : null}
        </div>
      ) : null}
      {item.owner_name ? (
        <div
          className="mt-1.5 flex items-center gap-1.5 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-faint)" }}
        >
          담당
          <span className="inline-flex items-center gap-1">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{
                background:
                  memberColors.get(item.owner_member_id ?? "") ??
                  memberColors.get(item.owner_name) ??
                  "var(--channel-slate)",
              }}
            />
            <span style={{ color: "var(--color-text-muted)" }}>
              {item.owner_name}
            </span>
          </span>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------ 사이드 피크 ------------------------------ */

function ProgramPeek({
  item,
  projects,
  members,
  onClose,
  onPatch,
  onDelete,
}: {
  item: SupportProgram;
  projects: DashboardProject[];
  members: TeamMember[];
  onClose: () => void;
  onPatch: (patch: Partial<SupportProgramInput>) => void;
  onDelete: () => void;
}) {
  const projectName = item.project_id
    ? (projects.find((p) => p.project_id === item.project_id)?.name ?? "")
    : "";
  const ownerName = item.owner_member_id
    ? (members.find((m) => m.member_id === item.owner_member_id)?.name ??
      item.owner_name ??
      "")
    : "";
  const memberColors = React.useMemo(() => memberColorMap(members), [members]);

  return (
    <SidePeek
      label={item.name || "지원사업 상세"}
      onClose={onClose}
      toolbar={
        <>
          {toExternalHref(item.url) ? (
            <a
              href={toExternalHref(item.url)!}
              target="_blank"
              rel="noopener noreferrer"
              className="flex h-9 items-center rounded-[6px] px-2.5 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              공고 열기 ↗
            </a>
          ) : null}
          <button
            type="button"
            onClick={onDelete}
            className="flex h-9 items-center rounded-[6px] px-2.5 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-attention, #ef7f62)" }}
          >
            삭제
          </button>
        </>
      }
    >
      <AutoGrowTitle
        value={item.name === "제목 없음" ? "" : item.name}
        onCommit={(v) => onPatch({ name: v || "제목 없음" })}
      />

      <div className="mt-4 flex flex-col">
        <PropRow label="상태">
          <TagSelect
            value={item.status}
            options={[...STATUSES]}
            onChange={(v) => v && onPatch({ status: v })}
            colorFor={statusColor}
            allowCustom={false}
            clearable={false}
            variant="inline"
          />
        </PropRow>
        <PropRow label="프로젝트">
          <TagSelect
            value={projectName}
            options={projects.map((p) => p.name)}
            onChange={(v) => {
              const proj = projects.find((p) => p.name === v);
              onPatch({ project_id: proj ? proj.project_id : null });
            }}
            colorFor={projectColor}
            allowCustom={false}
            placeholder="비어 있음"
            variant="inline"
          />
        </PropRow>
        <PropRow label="담당자">
          <TagSelect
            value={ownerName}
            options={members.map((m) => m.name)}
            onChange={(v) => {
              const m = members.find((x) => x.name === v);
              onPatch({ owner_member_id: m ? m.member_id : null });
            }}
            colorFor={(name) => memberPillColor(memberColors.get(name))}
            allowCustom={false}
            placeholder="비어 있음"
            variant="inline"
          />
        </PropRow>
        <PropRow label="마감일">
          <div className="max-w-[220px]">
            <DatePicker
              value={item.deadline ?? ""}
              onChange={(v) => onPatch({ deadline: v || null })}
            />
          </div>
          <DdayBadge date={item.deadline} muted={item.status !== "시작 전"} />
        </PropRow>
        {showsPresentation(item) ? (
          <>
            <PropRow label="발표준비 마감일">
              <div className="max-w-[220px]">
                <DatePicker
                  value={item.presentation_deadline ?? ""}
                  onChange={(v) => onPatch({ presentation_deadline: v || null })}
                />
              </div>
            </PropRow>
            <PropRow label="발표일">
              <div className="max-w-[220px]">
                <DatePicker
                  value={item.presentation_date ?? ""}
                  onChange={(v) =>
                    onPatch(
                      v
                        ? { presentation_date: v }
                        : { presentation_date: null, presentation_time: null },
                    )
                  }
                />
              </div>
              {item.presentation_date ? (
                <PresentationTimeSelect
                  value={item.presentation_time}
                  onChange={(t) => onPatch({ presentation_time: t })}
                />
              ) : null}
              <DdayBadge
                date={item.presentation_date}
                muted={DONE_STATUSES.has(item.status)}
              />
              {item.calendar_event_id ? (
                <span
                  className="ml-2 whitespace-nowrap text-[var(--text-meta)]"
                  style={{ color: "var(--color-progress, #57bf78)" }}
                >
                  ✓ 팀 일정 등록됨
                </span>
              ) : null}
            </PropRow>
          </>
        ) : null}
        <PropRow label="과제번호">
          <PropTextInput
            value={item.task_number ?? ""}
            placeholder="비어 있음"
            onCommit={(v) => onPatch({ task_number: v || null })}
          />
        </PropRow>
        <PropRow label="URL">
          <PropUrlInput
            value={item.url ?? ""}
            onCommit={(v) => onPatch({ url: v || null })}
          />
        </PropRow>
        <PropRow label="후속조치">
          <PropTextInput
            value={item.follow_up ?? ""}
            placeholder="비어 있음"
            onCommit={(v) => onPatch({ follow_up: v || null })}
          />
        </PropRow>
        <PropRow label="등록일">
          <span
            className="px-1 text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {fmtDate(item.created_at ? item.created_at.slice(0, 10) : null) || "—"}
          </span>
        </PropRow>
      </div>

      <PeekDivider />

      <RichBodyEditor
        id={item.program_id}
        value={item.body ?? ""}
        minHeight={220}
        onCommit={(v) => onPatch({ body: v || null })}
      />
    </SidePeek>
  );
}

/* 발표 시간 선택 — 팀일정 입력과 같은 시(00~23)/분(10분 단위) 드롭다운. */
const PT_HOURS = Array.from({ length: 24 }, (_, i) => `${i}`.padStart(2, "0"));
const PT_MINUTES = ["00", "10", "20", "30", "40", "50"];

function PresentationTimeSelect({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (t: string | null) => void;
}) {
  const hhmm = value ? value.slice(0, 5) : "";
  const [h, m] = hhmm ? hhmm.split(":") : ["", ""];
  const selectStyle: React.CSSProperties = {
    background: "var(--color-card)",
    border: "1px solid var(--color-divider)",
    color: h ? "var(--color-text-strong)" : "var(--color-text-faint)",
  };
  return (
    <div className="ml-2 flex items-center gap-1">
      <select
        value={h}
        onChange={(e) => {
          const nh = e.target.value;
          onChange(nh ? `${nh}:${m || "00"}` : null);
        }}
        className="h-8 rounded-[6px] px-1.5 text-[var(--text-body-sm)] outline-none"
        style={selectStyle}
        aria-label="발표 시"
      >
        <option value="">시간 —</option>
        {PT_HOURS.map((o) => (
          <option key={o} value={o}>
            {o}시
          </option>
        ))}
      </select>
      {h ? (
        <select
          value={PT_MINUTES.includes(m) ? m : "00"}
          onChange={(e) => onChange(`${h}:${e.target.value}`)}
          className="h-8 rounded-[6px] px-1.5 text-[var(--text-body-sm)] outline-none"
          style={{ ...selectStyle, color: "var(--color-text-strong)" }}
          aria-label="발표 분"
        >
          {PT_MINUTES.map((o) => (
            <option key={o} value={o}>
              {o}분
            </option>
          ))}
        </select>
      ) : null}
    </div>
  );
}

function DdayBadge({ date, muted }: { date: string | null; muted?: boolean }) {
  const d = dday(date);
  if (!d || muted) return null;
  return (
    <span
      className="ml-2 rounded-[4px] px-1.5 py-[2px] text-[var(--text-meta)] font-bold"
      style={
        d.urgent
          ? { background: "#fdebe8", color: "#88321f" }
          : { background: "var(--color-app-canvas)", color: "var(--color-text-muted)" }
      }
    >
      {d.label}
    </span>
  );
}

/* --------------------- 보드 아래 인라인 DB (꿀팁/사이트) -------------------- */

function NotesSection({
  kind,
  title,
  notes,
  setNotes,
  onError,
  reload,
}: {
  kind: "tip" | "site";
  title: string;
  notes: SupportNote[];
  setNotes: React.Dispatch<React.SetStateAction<SupportNote[]>>;
  onError: (msg: string | null) => void;
  reload: () => void;
}) {
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const [peekId, setPeekId] = React.useState<string | null>(null);
  const peekNote = peekId ? (notes.find((n) => n.note_id === peekId) ?? null) : null;

  function toNoteInput(n: SupportNote): SupportNoteInput {
    return {
      kind: n.kind,
      title: n.title,
      description: n.description,
      url: n.url,
      body: n.body,
      sort_order: n.sort_order,
    };
  }

  async function add() {
    try {
      const created = await api.createSupportNote({
        kind,
        title: kind === "site" ? "새 사이트" : "새 꿀팁",
        description: null,
        url: null,
        body: null,
        sort_order: notes.reduce((m, n) => Math.max(m, n.sort_order), 0) + 1,
      });
      setNotes((prev) => [...prev, created]);
      if (kind === "site") setPeekId(created.note_id);
      else setExpandedId(created.note_id);
    } catch (e) {
      onError(e instanceof Error ? e.message : "추가 실패");
    }
  }

  function patch(noteId: string, p: Partial<SupportNoteInput>) {
    const cur = notes.find((n) => n.note_id === noteId);
    if (!cur) return;
    const next = { ...cur, ...p };
    setNotes((prev) => prev.map((n) => (n.note_id === noteId ? next : n)));
    void api.updateSupportNote(noteId, toNoteInput(next)).catch(() => {
      onError("저장 실패");
      reload();
    });
  }

  async function remove(noteId: string) {
    if (!window.confirm("삭제할까요?")) return;
    setNotes((prev) => prev.filter((n) => n.note_id !== noteId));
    try {
      await api.deleteSupportNote(noteId);
    } catch {
      onError("삭제 실패");
    }
  }

  return (
    <section
      className="rounded-[10px] p-3"
      style={{
        background: "var(--color-panel)",
        border: "1px solid var(--color-divider-soft)",
      }}
    >
      <div className="mb-1.5 flex items-center justify-between">
        <h2
          className="text-[var(--text-body-sm)] font-bold"
          style={{ color: "var(--color-text-strong)" }}
        >
          {title}
          <span className="ml-1.5 font-normal" style={{ color: "var(--color-text-faint)" }}>
            {notes.length}
          </span>
        </h2>
        <button
          type="button"
          onClick={() => void add()}
          className="flex h-8 items-center rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-card)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          + 추가
        </button>
      </div>
      {notes.length === 0 ? (
        <p className="px-1 py-1 text-[var(--text-meta)]" style={labelStyle()}>
          비어 있음
        </p>
      ) : kind === "site" ? (
        /* 노션식 인라인 테이블: 이름 | URL | 설명 모두 제자리 수정 */
        <div
          className="overflow-hidden rounded-[8px]"
          style={{ border: "1px solid var(--color-divider-soft)", background: "var(--color-card)" }}
        >
          <div
            className="grid grid-cols-[minmax(140px,1.1fr)_minmax(180px,1.5fr)_1fr_32px] gap-1 px-1.5 py-1 text-[var(--text-meta)]"
            style={{
              color: "var(--color-text-faint)",
              borderBottom: "1px solid var(--color-divider-soft)",
              background: "var(--color-panel)",
            }}
          >
            <span className="px-1">이름</span>
            <span className="px-1">URL</span>
            <span className="px-1">설명</span>
            <span />
          </div>
          {notes.map((n) => (
            <div
              key={n.note_id}
              className="group grid grid-cols-[minmax(140px,1.1fr)_minmax(180px,1.5fr)_1fr_32px] items-center gap-1 px-1.5 py-[2px]"
              style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
            >
              <div className="relative flex min-w-0 items-center">
                <PropTextInput
                  value={n.title}
                  placeholder="이름"
                  onCommit={(v) => v && patch(n.note_id, { title: v })}
                />
                <button
                  type="button"
                  onClick={() => setPeekId(n.note_id)}
                  className="absolute right-0 shrink-0 rounded-[4px] px-1.5 py-[2px] text-[var(--text-meta)] opacity-0 shadow-sm group-hover:opacity-100"
                  style={{
                    background: "var(--color-card)",
                    border: "1px solid var(--color-divider)",
                    color: "var(--color-text-muted)",
                  }}
                >
                  열기
                </button>
              </div>
              <PropUrlInput
                value={n.url ?? ""}
                placeholder="URL"
                onCommit={(v) => patch(n.note_id, { url: v || null })}
              />
              <PropTextInput
                value={n.description ?? ""}
                placeholder="설명"
                onCommit={(v) => patch(n.note_id, { description: v || null })}
              />
              <button
                type="button"
                onClick={() => void remove(n.note_id)}
                aria-label="삭제"
                className="flex h-7 w-7 items-center justify-center rounded-[5px] text-[var(--text-meta)] opacity-0 group-hover:opacity-100 hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-attention, #ef7f62)" }}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="flex flex-col">
          {notes.map((n) => (
            <div
              key={n.note_id}
              className="group rounded-[6px] px-1.5 py-1 hover:bg-[var(--color-card)]"
            >
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() =>
                    setExpandedId(expandedId === n.note_id ? null : n.note_id)
                  }
                  aria-label="펼치기"
                  className="flex h-6 w-5 shrink-0 items-center justify-center text-[10px]"
                  style={{ color: "var(--color-text-faint)" }}
                >
                  {expandedId === n.note_id ? "▾" : "▸"}
                </button>
                <div className="min-w-0 flex-1">
                  <PropTextInput
                    value={n.title}
                    placeholder="제목"
                    onCommit={(v) => v && patch(n.note_id, { title: v })}
                  />
                </div>
                <button
                  type="button"
                  onClick={() => {
                    // 같은 노트의 인라인 에디터와 피크 에디터가 동시에 열려 있으면
                    // 서로 다른 값으로 덮어쓸 수 있어, 피크를 열 때 인라인은 접는다.
                    setExpandedId(null);
                    setPeekId(n.note_id);
                  }}
                  className="shrink-0 rounded-[4px] px-1.5 py-[2px] text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                  style={{
                    background: "var(--color-app-canvas)",
                    border: "1px solid var(--color-divider)",
                    color: "var(--color-text-muted)",
                  }}
                >
                  열기
                </button>
                <button
                  type="button"
                  onClick={() => void remove(n.note_id)}
                  className="shrink-0 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                  style={{ color: "var(--color-attention, #ef7f62)" }}
                >
                  삭제
                </button>
              </div>
              {expandedId === n.note_id ? (
                <div className="mb-1 ml-7 flex flex-col gap-1">
                  <RichBodyEditor
                    id={n.note_id}
                    value={n.body ?? ""}
                    minHeight={60}
                    onCommit={(v) => patch(n.note_id, { body: v || null })}
                  />
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
      {peekNote ? (
        <NotePeek
          note={peekNote}
          onClose={() => setPeekId(null)}
          onPatch={(p) => patch(peekNote.note_id, p)}
          onDelete={() => {
            setPeekId(null);
            void remove(peekNote.note_id);
          }}
        />
      ) : null}
    </section>
  );
}

/** 노션 row→page처럼 노트 하나를 SidePeek으로 열어 세부 정보를 보고 수정한다. */
function NotePeek({
  note,
  onClose,
  onPatch,
  onDelete,
}: {
  note: SupportNote;
  onClose: () => void;
  onPatch: (p: Partial<SupportNoteInput>) => void;
  onDelete: () => void;
}) {
  return (
    <SidePeek
      label={note.kind === "site" ? "지원사업 검색 사이트" : "지원사업 꿀팁"}
      onClose={onClose}
      toolbar={
        <>
          {toExternalHref(note.url) ? (
            <a
              href={toExternalHref(note.url)!}
              target="_blank"
              rel="noopener noreferrer"
              className="flex h-9 items-center rounded-[6px] px-2.5 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              사이트 열기 ↗
            </a>
          ) : null}
          <button
            type="button"
            onClick={onDelete}
            className="flex h-9 items-center rounded-[6px] px-2.5 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-attention, #ef7f62)" }}
          >
            삭제
          </button>
        </>
      }
    >
      <AutoGrowTitle
        value={note.title === "새 사이트" || note.title === "새 꿀팁" ? "" : note.title}
        onCommit={(v) =>
          onPatch({ title: v || (note.kind === "site" ? "새 사이트" : "새 꿀팁") })
        }
      />
      <div className="mt-4 flex flex-col">
        <PropRow label="URL">
          <PropUrlInput
            value={note.url ?? ""}
            onCommit={(v) => onPatch({ url: v || null })}
          />
        </PropRow>
        <PropRow label="설명">
          <PropTextInput
            value={note.description ?? ""}
            placeholder="비어 있음"
            onCommit={(v) => onPatch({ description: v || null })}
          />
        </PropRow>
      </div>
      <PeekDivider />
      <RichBodyEditor
        id={note.note_id}
        value={note.body ?? ""}
        minHeight={160}
        onCommit={(v) => onPatch({ body: v || null })}
      />
    </SidePeek>
  );
}
