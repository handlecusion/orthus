"use client";

import {
  DndContext,
  DragOverlay,
  PointerSensor,
  pointerWithin,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type CollisionDetection,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import {
  Archive,
  ArrowUpDown,
  BarChart3,
  CalendarClock,
  CalendarDays,
  CalendarPlus,
  CalendarRange,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock,
  Copy,
  Eye,
  EyeOff,
  Flag,
  Hash,
  Home,
  Inbox,
  Maximize2,
  Minimize2,
  MoreHorizontal,
  PanelRightClose,
  Paperclip,
  Plus,
  Rows3,
  Target,
  Trash2,
  X,
} from "lucide-react";
import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { createPortal, flushSync } from "react-dom";
import {
  api,
  API_BASE,
  ApiError,
  demoAuthHeaders,
  type DayAllocation,
  type ObjectiveSubtask,
  type PersonalBoardBacklogBucket,
  type PersonalBoardBootstrap,
  type PersonalBoardDay,
  type PersonalBoardFixedEvent,
  type PersonalBoardNote,
  type PersonalBoardProject,
  type PersonalBoardSubtask,
  type PersonalBoardTask,
  type PersonalTaskComment,
  type PersonalTaskCreateInput,
  type PersonalTaskPatchInput,
  type PersonalWeeklyObjective,
  type WeeklyReviewResponse,
} from "@/lib/api";
import { Banner, Skeleton } from "@/components/ui";
import { hasTextSelectionWithin } from "@/lib/cardCopy";
import { Linkify } from "@/lib/linkify";
import {
  readBoardSnapshot,
  setOfflineBoardOwner,
  writeBoardSnapshot,
} from "@/lib/offline-board-cache";
import {
  SchedulePanel,
  type ScheduleEventBlock,
  SCHEDULE_END_HOUR,
  SCHEDULE_DEFAULT_DURATION_MIN,
  minToClock,
  offsetToScheduleMin,
  parseClockToMin,
} from "./SchedulePanel";
import {
  enqueueBoardMutation,
  type FlushHandlers,
  flushOutbox,
  isNetworkError,
  pendingMutationCount,
} from "@/lib/offline-board-outbox";
import "./personal-ojosama.css";

type Priority = "urgent" | "priority" | "normal" | "low";
type FilterMode = "all" | "open" | "done" | "high_priority";
type SortMode = "time" | "manual" | "priority";
type ViewMode = "home" | "weekly_plan" | "weekly_review";
type RightPanel = "backlog" | "schedule" | "hidden";
type SidebarItem = ViewMode;
type MoveDestination = { type: "day"; date: string } | { type: "backlog"; bucketId: string };
type TaskMenuState = { task: PersonalBoardTask; x: number; y: number };
type LayoutRect = Pick<DOMRect, "left" | "top" | "width" | "height">;

const priorityLabels: Record<Priority, string> = {
  urgent: "긴급",
  priority: "중요",
  normal: "None",
  low: "낮음",
};

const filterLabels: Record<FilterMode, string> = {
  all: "All tasks",
  open: "Open tasks",
  done: "Done tasks",
  high_priority: "High priority",
};

const sortLabels: Record<SortMode, string> = {
  time: "시간순",
  manual: "Manual order",
  priority: "Priority first",
};

const viewLabels: Record<ViewMode, string> = {
  home: "Home",
  weekly_plan: "Weekly plan",
  weekly_review: "Weekly review",
};

function parseViewMode(value: string | null): ViewMode | null {
  if (value === "home" || value === "weekly_plan" || value === "weekly_review") return value;
  // Back-compat: legacy "schedule" maps to Home, "backlog" stays a Home + open-panel signal.
  if (value === "schedule" || value === "backlog") return "home";
  return null;
}

const priorityRank: Record<Priority, number> = {
  urgent: 0,
  priority: 1,
  normal: 2,
  low: 3,
};

const layoutAnimations = new WeakMap<HTMLElement, Animation>();
const channelPalette = ["#5d9bd8", "#efc47b", "#62c4ba", "#c87ee9", "#9fb7c5", "#f07e5f"];
const DEFAULT_TIMEZONE = "Asia/Seoul";

const pad = (value: number) => String(value).padStart(2, "0");

function toDateKey(date: Date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function addDays(dateKey: string, days: number) {
  const date = new Date(`${dateKey}T12:00:00`);
  date.setDate(date.getDate() + days);
  return toDateKey(date);
}

function dateFromKey(dateKey: string) {
  return new Date(`${dateKey}T12:00:00`);
}

function weekdayLabel(dateKey: string) {
  return new Intl.DateTimeFormat("en-US", { weekday: "long" }).format(dateFromKey(dateKey));
}

function monthDayLabel(dateKey: string) {
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(dateFromKey(dateKey));
}

// weekday 0 = Sunday (matches backend day_allocations / weekly review semantics).
const WEEKDAY_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;

function sundayOf(dateKey: string) {
  const jsDay = dateFromKey(dateKey).getDay(); // 0 = Sunday = week start
  return addDays(dateKey, -jsDay);
}

function weekdayIndex(dateKey: string) {
  return dateFromKey(dateKey).getDay(); // Sun=0
}

function weekRangeLabel(weekStart: string) {
  const end = addDays(weekStart, 6);
  return `${monthDayLabel(weekStart)} \u2013 ${monthDayLabel(end)}`;
}

function allocationForWeekday(allocations: DayAllocation[], weekday: number) {
  return allocations.find((entry) => entry.weekday === weekday) ?? null;
}

function objectiveDefaultWeekday(weekStart: string, realToday: string) {
  return realToday >= weekStart && realToday <= addDays(weekStart, 6)
    ? weekdayIndex(realToday)
    : 0;
}

/** 서버 auto-complete 미러: 서브태스크가 있으면 완료 = 전부 완료, 없으면 수동값 유지. */
function deriveObjectiveCompleted(subtasks: ObjectiveSubtask[], fallback: boolean): boolean {
  return subtasks.length > 0 ? subtasks.every((s) => s.completed) : fallback;
}

function setAllocationAssigned(
  allocations: DayAllocation[],
  weekday: number,
  assigned: boolean,
): DayAllocation[] {
  const existing = allocationForWeekday(allocations, weekday);
  const next = allocations.filter((entry) => entry.weekday !== weekday);
  if (assigned) {
    next.push({ ...(existing ?? { weekday, minutes: null }), weekday, minutes: null });
  }
  return next.sort((a, b) => a.weekday - b.weekday);
}

function timeOfDayLabel(value: string | null) {
  if (!value) return null;
  const [hourValueRaw, minuteRaw] = value.slice(0, 5).split(":").map(Number);
  if (!Number.isFinite(hourValueRaw) || !Number.isFinite(minuteRaw)) return null;
  const hour = hourValueRaw % 12 || 12;
  const period = hourValueRaw >= 12 ? "pm" : "am";
  return `${hour}:${pad(minuteRaw)} ${period}`;
}

function dateTimePartsInTimezone(value: string | Date, timezone: string) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  if (!values.year || !values.month || !values.day || !values.hour || !values.minute) return null;
  return values as Record<"year" | "month" | "day" | "hour" | "minute", string>;
}

function dateKeyInTimezone(value: Date, timezone: string) {
  const parts = dateTimePartsInTimezone(value, timezone);
  return parts ? `${parts.year}-${parts.month}-${parts.day}` : toDateKey(value);
}

// Stable client-generated id for offline-first creation. Using a real UUID (not
// a temp marker) means an offline-created task keeps the same id after sync, and
// the server create is idempotent on it.
function makeClientId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

function formatSyncedAt(epochMs: number): string {
  const date = new Date(epochMs);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function timeInputValue(value: string, timezone: string) {
  const parts = dateTimePartsInTimezone(value, timezone);
  if (parts) return `${parts.hour}:${parts.minute}`;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "00:00";
  return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}

const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

type DueTone = "overdue" | "today" | "soon" | "normal" | "done";

// Wall-clock (dateKey + optional HH:MM[:SS]) in `timezone` -> epoch ms. Date-only
// (no time) anchors at end of day so a same-day deadline isn't already "passed" at
// 00:00. Two-pass offset back-out: take the offset at a first guess, then re-read it
// AT the candidate instant so DST-transition windows converge to the right hour
// (a single pass is off by ~1h on the two transition days for DST zones).
function zonedWallClockToEpoch(dateKey: string, time: string | null, timezone: string): number {
  const [y, mo, d] = dateKey.split("-").map(Number);
  const [hh, mm] = (time ?? "23:59").slice(0, 5).split(":").map(Number);
  if (![y, mo, d].every(Number.isFinite)) return Number.NaN;
  const asUTC = Date.UTC(y, mo - 1, d, hh || 0, mm || 0);
  // The zone's UTC offset (ms) at a given instant: read the wall clock it shows there.
  const offsetAt = (instant: number): number | null => {
    const parts = dateTimePartsInTimezone(new Date(instant), timezone);
    if (!parts) return null;
    const asWall = Date.UTC(
      Number(parts.year),
      Number(parts.month) - 1,
      Number(parts.day),
      Number(parts.hour),
      Number(parts.minute),
    );
    return asWall - instant;
  };
  const off1 = offsetAt(asUTC);
  if (off1 == null) return asUTC;
  const guess = asUTC - off1;
  const off2 = offsetAt(guess) ?? off1;
  return asUTC - off2;
}

// Calendar-day difference toKey - fromKey (positive = future). Noon anchor avoids
// DST off-by-one around midnight.
function dayDiffKeys(fromKey: string, toKey: string): number {
  const from = new Date(`${fromKey}T12:00:00`).getTime();
  const to = new Date(`${toKey}T12:00:00`).getTime();
  if (Number.isNaN(from) || Number.isNaN(to)) return 0;
  return Math.round((to - from) / DAY_MS);
}

// "몇시간 남음 / 몇일 남음" deadline chip. Returns null when there's no due date.
// Timed dues count down in minutes/hours/days; date-only dues use calendar-day
// granularity against the real today. Completed tasks show the date without alarm.
function dueChipInfo(
  dueDate: string | null,
  dueTime: string | null,
  timezone: string,
  realToday: string,
  nowEpoch: number,
  completed: boolean,
): { text: string; tone: DueTone; title: string } | null {
  if (!dueDate) return null;
  const clock = dueTime ? ` ${timeOfDayLabel(dueTime) ?? ""}`.trimEnd() : "";
  const title = `${monthDayLabel(dueDate)}${clock} 마감`;
  if (completed) return { text: monthDayLabel(dueDate), tone: "done", title };
  if (dueTime) {
    const diff = zonedWallClockToEpoch(dueDate, dueTime, timezone) - nowEpoch;
    if (Number.isNaN(diff)) return { text: monthDayLabel(dueDate), tone: "normal", title };
    if (diff <= 0) {
      const past = -diff;
      const text =
        past < HOUR_MS
          ? `${Math.max(1, Math.floor(past / 60_000))}분 지남`
          : past < DAY_MS
            ? `${Math.floor(past / HOUR_MS)}시간 지남`
            : `${Math.floor(past / DAY_MS)}일 지남`;
      return { text, tone: "overdue", title };
    }
    if (diff < HOUR_MS) return { text: `${Math.max(1, Math.floor(diff / 60_000))}분 남음`, tone: "soon", title };
    if (diff < DAY_MS) {
      const hours = Math.floor(diff / HOUR_MS);
      return { text: `${hours}시간 남음`, tone: hours < 6 ? "soon" : "normal", title };
    }
    const days = Math.floor(diff / DAY_MS);
    return { text: `${days}일 남음`, tone: days <= 2 ? "soon" : "normal", title };
  }
  const days = dayDiffKeys(realToday, dueDate);
  if (days < 0) return { text: `${-days}일 지남`, tone: "overdue", title };
  if (days === 0) return { text: "오늘 마감", tone: "today", title };
  if (days === 1) return { text: "내일 마감", tone: "soon", title };
  return { text: `${days}일 남음`, tone: days <= 2 ? "soon" : "normal", title };
}

function shouldIgnoreCardDrag(target: EventTarget | null) {
  // Element(not just HTMLElement)로 검사: 버튼 안의 <svg>/<path> 같은 SVGElement도
  // closest로 버튼을 찾아 드래그/열기에서 제외되게 한다.
  if (!(target instanceof Element)) return false;
  if (target.closest("[data-card-drag-handle]")) return false;
  // `label` is intentionally NOT here: a subtask row is a <label>, so the row
  // (title + padding) must stay a card-drag hitbox. The inner checkbox <input>
  // still blocks drag, so it stays a pure toggle.
  // 카드 전체(제목·서브태스크 텍스트 [data-card-text] 포함)를 드래그 핸들로 둔다 — 카드를
  // 옮기려고 텍스트를 잡고 끌어도 드래그가 시작된다. 텍스트는 CSS `user-select:none`이라
  // 끌 때 하이라이트(복사 선택)가 생기지 않고, 5px 드래그 활성화 거리가 클릭(상세 열기)과
  // 이동을 가른다. 버튼/입력/체크박스(input)는 그대로 제외해 토글/편집을 보존한다.
  return Boolean(target.closest("button, textarea, select, a, input, [data-no-card-drag]"));
}

function shouldIgnoreCardOpen(target: EventTarget | null) {
  // SVGElement(버튼 안 아이콘)도 포함하도록 Element로 검사한다.
  if (!(target instanceof Element)) return false;
  return Boolean(target.closest("button, textarea, select, a, input, label, [data-no-card-open]"));
}

class TaskCardPointerSensor extends PointerSensor {
  static activators = [
    {
      eventName: "onPointerDown" as const,
      handler: ({ nativeEvent }: ReactPointerEvent) => {
        if (!nativeEvent.isPrimary || nativeEvent.button !== 0) return false;
        return !shouldIgnoreCardDrag(nativeEvent.target);
      },
    },
  ];
}

// Day columns are full-height droppables that contain the task-card droppables.
// Prefer the inner task card when the pointer is over one, so dropping a card
// onto another reorders relative to it instead of resolving to the whole column
// (which would be a same-day no-op). Fall back to the column for empty areas.
//
// Crucially we drop the *dragged* item's own droppable first: the card moves
// with the pointer (CSS transform, no DragOverlay), so its own `task:<id>`
// droppable stays under the cursor and would otherwise win pointerWithin —
// resolving `over` to itself, which handleDragEnd treats as a no-op (card snaps
// back). The active id is the bare task id (task draggable) or the full
// `objective:…` id (objective draggable); exclude both forms.
const boardCollisionDetection: CollisionDetection = (args) => {
  const activeId = String(args.active.id);
  const selfIds = new Set([activeId, `task:${activeId}`]);
  const hits = pointerWithin(args).filter((h) => !selfIds.has(String(h.id)));
  const inner = hits.filter((h) => {
    const id = String(h.id);
    return id.startsWith("task:") || id.startsWith("objective:");
  });
  return inner.length ? inner : hits;
};

function asPriority(value: string): Priority {
  return value === "urgent" || value === "priority" || value === "low" ? value : "normal";
}

function taskMatchesFilter(task: PersonalBoardTask, mode: FilterMode) {
  if (mode === "open") return !task.completed;
  if (mode === "done") return task.completed;
  if (mode === "high_priority") return task.priority === "urgent" || task.priority === "priority";
  return true;
}

// 시간순 정렬 티어 키.
//  tier 0: 시작시간(scheduled_time)이 있는 티켓 → 시작시간 빠른 순(같으면 종료 빠른 순)
//  tier 1: 시작시간은 없지만 마감일(due_date)이 있는 티켓 → 마감 임박 순
//  tier 2: 둘 다 없는 티켓 → 맨 아래
// 키는 고정폭 문자열이라 사전식 비교가 시간 순서와 일치한다(타임존 불필요). 동률은 order_index.
function timeSortKey(task: PersonalBoardTask): [number, string] {
  if (task.scheduled_time) {
    return [0, `${task.scheduled_time}|${task.scheduled_end_time ?? "~"}`];
  }
  if (task.due_date) {
    return [1, `${task.due_date}T${task.due_time ?? "23:59:59"}`];
  }
  return [2, ""];
}

function sortTasks(tasks: PersonalBoardTask[], mode: SortMode) {
  return [...tasks].sort((a, b) => {
    // 처리 완료(done)한 티켓은 정렬 모드와 무관하게 항상 아래로 가라앉힌다.
    // 미완료 티켓끼리의 상대 순서는 기존 정렬(수동/시간/우선순위)을 그대로 유지하므로
    // "처리 안 된 것은 위, 처리 중(미완료)은 제자리, 완료는 아래"가 된다.
    // order_index는 건드리지 않아 다시 열면 원래 자리로 복귀한다.
    if (a.completed !== b.completed) return a.completed ? 1 : -1;
    if (mode === "priority") {
      const delta = priorityRank[asPriority(a.priority)] - priorityRank[asPriority(b.priority)];
      if (delta !== 0) return delta;
    } else if (mode === "time") {
      const [tierA, keyA] = timeSortKey(a);
      const [tierB, keyB] = timeSortKey(b);
      if (tierA !== tierB) return tierA - tierB;
      if (keyA !== keyB) return keyA < keyB ? -1 : 1;
    }
    return a.order_index - b.order_index;
  });
}

function prepareTasks(tasks: PersonalBoardTask[], filterMode: FilterMode, sortMode: SortMode) {
  return sortTasks(tasks.filter((task) => taskMatchesFilter(task, filterMode)), sortMode);
}

function columnProgress(tasks: PersonalBoardTask[]) {
  if (tasks.length === 0) return 0;
  const completed = tasks.filter((task) => task.completed).length;
  return Math.max(12, Math.round((completed / tasks.length) * 100));
}

function allTasks(payload: PersonalBoardBootstrap | null) {
  if (!payload) return [];
  return [
    ...payload.days.flatMap((day) => day.tasks),
    ...payload.backlog_buckets.flatMap((bucket) => bucket.tasks),
  ];
}

function sortContainerTasks(tasks: PersonalBoardTask[]) {
  return [...tasks].sort((a, b) => a.order_index - b.order_index);
}


// 낙관적으로 막 추가한 태스크는 서버가 UUID를 발급하기 전까지 `temp-...` id를 갖는다.
// 그 사이에 토글/이동/편집을 하면 temp id가 `/personal-board/tasks/{uuid}`로 전송돼
// 백엔드가 uuid_parsing 422를 낸다. API에 task_id를 보내는 모든 핸들러에서 차단한다.
function isTempTaskId(id: string) {
  return id.startsWith("temp-");
}

function collectLayoutRects(container: HTMLElement | null) {
  const rects = new Map<string, LayoutRect>();
  // 카드 위치를 스크롤 컨테이너(일별 보드)의 '컨텐츠 좌표'로 측정한다.
  // getBoundingClientRect는 뷰포트 상대라 보드가 수평/수직으로 스크롤하면 값이
  // 바뀌는데, 그 스크롤 변화가 FLIP deltaX/deltaY로 잡혀 같은 컬럼의 카드가
  // 좌우로 흔들리는 버그가 났다(예: 왼쪽 무한 로딩 scrollLeft 보정, 백그라운드
  // 갱신). container 오프셋(+scrollLeft/Top, -container 뷰포트 left/top)을 더해
  // 실제 레이아웃 재배치만 애니메이션되게 한다.
  const base = container?.getBoundingClientRect();
  const offsetX = container ? container.scrollLeft - (base?.left ?? 0) : 0;
  const offsetY = container ? container.scrollTop - (base?.top ?? 0) : 0;
  document.querySelectorAll<HTMLElement>("[data-layout-id]").forEach((element) => {
    const layoutId = element.dataset.layoutId;
    if (!layoutId) return;
    const rect = element.getBoundingClientRect();
    rects.set(layoutId, {
      left: rect.left + offsetX,
      top: rect.top + offsetY,
      width: rect.width,
      height: rect.height,
    });
  });
  return rects;
}

// `currentRects`는 진행 중인 transform이 없는 '정지 위치'여야 한다(호출 전에
// cancelLayoutAnimations로 멈춘 뒤 측정). 애니메이션 도중 측정한 좌표를 기준으로
// 쓰면 매 렌더(예: Add task 입력 타이핑)마다 카드가 다시 움직이는 피드백 루프가 생긴다.
function animateLayoutShift(
  previousRects: Map<string, LayoutRect>,
  currentRects: Map<string, LayoutRect>,
) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  document.querySelectorAll<HTMLElement>("[data-layout-id]").forEach((element) => {
    if (element.classList.contains("dragging")) return;
    const layoutId = element.dataset.layoutId;
    const previous = layoutId ? previousRects.get(layoutId) : null;
    const next = layoutId ? currentRects.get(layoutId) : null;
    if (!previous || !next) return;
    const deltaX = previous.left - next.left;
    const deltaY = previous.top - next.top;
    if (Math.abs(deltaX) <= 0.5 && Math.abs(deltaY) <= 0.5) return;
    const animation = element.animate(
      [{ transform: `translate3d(${deltaX}px, ${deltaY}px, 0)` }, { transform: "translate3d(0, 0, 0)" }],
      {
        duration: element.classList.contains("task-drop-preview") ? 185 : 165,
        easing: "cubic-bezier(0.2, 0, 0, 1)",
      },
    );
    layoutAnimations.set(element, animation);
    animation.onfinish = () => {
      if (layoutAnimations.get(element) === animation) layoutAnimations.delete(element);
    };
    animation.oncancel = () => {
      if (layoutAnimations.get(element) === animation) layoutAnimations.delete(element);
    };
  });
}

function cancelLayoutAnimations() {
  document.querySelectorAll<HTMLElement>("[data-layout-id]").forEach((element) => {
    layoutAnimations.get(element)?.cancel();
    layoutAnimations.delete(element);
  });
}

// ---- Today dropdown: jump to a date or pin a "from .. to" range ----
function DateRangeMenu({
  realToday,
  selectedDate,
  rangeFilter,
  rangePickStart,
  onGoToday,
  onClearRange,
  onPickDate,
}: {
  realToday: string;
  selectedDate: string;
  rangeFilter: { start: string; end: string } | null;
  rangePickStart: string | null;
  onGoToday: () => void;
  onClearRange: () => void;
  onPickDate: (dateKey: string) => void;
}) {
  const [monthAnchor, setMonthAnchor] = useState(() => `${(rangeFilter?.start ?? selectedDate).slice(0, 7)}-01`);
  const monthLabel = new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric" }).format(
    dateFromKey(monthAnchor),
  );
  // 6-week grid starting on the Sunday on/before the 1st of the month.
  const gridStart = sundayOf(monthAnchor);
  const cells = Array.from({ length: 42 }, (_, i) => addDays(gridStart, i));
  const inMonth = (d: string) => d.slice(0, 7) === monthAnchor.slice(0, 7);
  const inRange = (d: string) => !!rangeFilter && d >= rangeFilter.start && d <= rangeFilter.end;

  return (
    <div className="date-range-menu" role="dialog" aria-label="날짜 선택">
      <div className="drm-quick">
        <button className="drm-quick-btn" onClick={onGoToday} type="button">
          오늘로 가기
        </button>
        {rangeFilter ? (
          <button className="drm-quick-btn" onClick={onClearRange} type="button">
            전체 보기
          </button>
        ) : null}
      </div>
      <div className="drm-hint">
        {rangePickStart && !rangeFilter
          ? "종료일을 선택하세요 (부터 → 까지)"
          : "날짜 클릭=이동 · 두 번 클릭=기간 보기"}
      </div>
      <div className="drm-month-nav">
        <button
          aria-label="이전 달"
          className="drm-icon"
          onClick={() => setMonthAnchor((m) => `${addDays(m, -1).slice(0, 7)}-01`)}
          type="button"
        >
          ‹
        </button>
        <span>{monthLabel}</span>
        <button
          aria-label="다음 달"
          className="drm-icon"
          onClick={() => setMonthAnchor((m) => `${addDays(`${m.slice(0, 7)}-28`, 7).slice(0, 7)}-01`)}
          type="button"
        >
          ›
        </button>
      </div>
      <div className="drm-grid drm-weekdays">
        {["일", "월", "화", "수", "목", "금", "토"].map((w) => (
          <span className="drm-weekday" key={w}>
            {w}
          </span>
        ))}
      </div>
      <div className="drm-grid">
        {cells.map((d) => {
          const classes = ["drm-day"];
          if (!inMonth(d)) classes.push("muted");
          if (d === realToday) classes.push("today");
          if (d === rangePickStart) classes.push("pick-start");
          if (inRange(d)) classes.push("in-range");
          return (
            <button className={classes.join(" ")} key={d} onClick={() => onPickDate(d)} type="button">
              {Number(d.slice(8, 10))}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function PersonalWorkspaceBoard() {
  const [selectedDate, setSelectedDate] = useState(dateKeyInTimezone(new Date(), DEFAULT_TIMEZONE));
  const [payload, setPayload] = useState<PersonalBoardBootstrap | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState("Loading workspace data");
  // When set, the board is showing the offline read cache (last server sync at
  // this epoch ms). Cleared on a successful network load. (docs/offline-personal-board.md 1단계)
  const [offlineSyncedAt, setOfflineSyncedAt] = useState<number | null>(null);
  // Count of board edits made offline that are queued to sync on reconnect (2단계).
  const [pendingSyncCount, setPendingSyncCount] = useState(0);
  // Last successful *server* sync time, so an edit made after going offline
  // mid-session can show the correct "마지막 동기화" instead of "now".
  const lastSyncedAtRef = useRef<number>(Date.now());
  const [taskDrafts, setTaskDrafts] = useState<Record<string, string>>({});
  const [bucketDrafts, setBucketDrafts] = useState<Record<string, string>>({});
  // 티켓 댓글/노트 — 서버 영속(0092). taskComments는 팝업 오픈 시 로드하는 캐시,
  // taskNoteDrafts는 입력 중 텍스트의 오버레이(디바운스 저장 후 task.note가 SoR).
  const [taskComments, setTaskComments] = useState<Record<string, PersonalTaskComment[]>>({});
  const [taskNoteDrafts, setTaskNoteDrafts] = useState<Record<string, string>>({});
  // 진행 중 노트 저장(디바운스). 팝업 닫힘/페이지 숨김에서 flush한다.
  const pendingNoteSaveRef = useRef<{ task: PersonalBoardTask; value: string } | null>(null);
  const noteSaveTimerRef = useRef<number | null>(null);
  const flushNoteSaveRef = useRef<() => void>(() => {});
  const [activeBucketId, setActiveBucketId] = useState<string | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [activeOverId, setActiveOverId] = useState<string | null>(null);
  const [activeObjectiveDrag, setActiveObjectiveDrag] = useState<{ objectiveId: string; date: string } | null>(null);
  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const [filterMode, setFilterMode] = useState<FilterMode>("all");
  const [sortMode, setSortMode] = useState<SortMode>("time");
  const [viewMode, setViewMode] = useState<ViewMode>("home");
  // ---- Date-jump / range picker (Today dropdown) ----
  // 상단 Today를 누르면 달력 드롭다운이 열려 특정 날짜로 이동하거나 "언제부터
  // 언제까지" 기간을 골라 그 구간만 보게 한다. rangeFilter가 켜지면 day board는
  // 그 구간으로 고정되고 무한 스크롤(append/prepend)을 멈춘다.
  const [dateMenuOpen, setDateMenuOpen] = useState(false);
  const [rangePickStart, setRangePickStart] = useState<string | null>(null);
  const [rangeFilter, setRangeFilter] = useState<{ start: string; end: string } | null>(null);
  const dateMenuRef = useRef<HTMLDivElement | null>(null);
  const rangeFilterRef = useRef<{ start: string; end: string } | null>(null);
  // 스케줄 포커스(단일 컬럼) 동안은 좌측 보드를 패닝/휠로 옮기지 않으므로 과거 일자
  // prefetch도 막는다. 비-React 핸들러에서 읽으려고 ref로 둔다.
  const scheduleFocusRef = useRef(false);
  const pendingScrollDateRef = useRef<string | null>(null);
  const [activeSidebarItem, setActiveSidebarItem] = useState<SidebarItem>("home");
  const [rightPanel, setRightPanel] = useState<RightPanel>("hidden");
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null);
  const [editingObjective, setEditingObjective] = useState<{
    objectiveId: string;
    weekday: number;
    scope: "day" | "week";
  } | null>(null);
  const [instantPlacementTaskIds, setInstantPlacementTaskIds] = useState<ReadonlySet<string>>(() => new Set());
  const [mobileColumnId, setMobileColumnId] = useState(`day:${dateKeyInTimezone(new Date(), DEFAULT_TIMEZONE)}`);
  const [hiddenSubtasks, setHiddenSubtasks] = useState<ReadonlySet<string>>(() => new Set());
  const [taskMenu, setTaskMenu] = useState<TaskMenuState | null>(null);
  const [weeklyPlanWeekStart, setWeeklyPlanWeekStart] = useState<string | null>(null);
  const [objectivesByWeek, setObjectivesByWeek] = useState<Record<string, PersonalWeeklyObjective[]>>({});
  const loadedObjectiveWeeksRef = useRef<Set<string>>(new Set());
  const setWeekObjectives = useCallback((week: string, objectives: PersonalWeeklyObjective[]) => {
    setObjectivesByWeek((cur) => ({ ...cur, [week]: [...objectives].sort((a, b) => a.order_index - b.order_index) }));
  }, []);
  const [weeklyPlanLoading, setWeeklyPlanLoading] = useState(false);
  const [weeklyReviewWeekStart, setWeeklyReviewWeekStart] = useState<string | null>(null);
  const [weeklyReview, setWeeklyReview] = useState<WeeklyReviewResponse | null>(null);
  const [weeklyReviewLoading, setWeeklyReviewLoading] = useState(false);
  const isLoadingDaysRef = useRef(false);
  // True while a task/card is being dragged. Used to suppress mid-drag infinite
  // date loading, whose instant scrollLeft jump otherwise lurches the board.
  const isDraggingRef = useRef(false);
  // Last observed horizontal scroll, so past days only load on a real leftward
  // scroll (not on mount when scrollLeft is already 0).
  const lastScrollLeftRef = useRef(0);
  // payload.days snapshot for non-React pan/auto-scroll handlers. days를 effect
  // deps로 직접 쓰면 prepend 병합 순간 effect가 재등록되며 진행 중이던 팬 상태
  // (클로저 안 panning)가 리셋돼 "잡은 채 멈춤"이 됐다 — ref로 읽어 deps에서 뺀다.
  const daysRef = useRef<PersonalBoardDay[]>([]);
  const dayBoardRef = useRef<HTMLElement | null>(null);
  // 스케줄 패널 그리드 DOM. handleDragEnd가 포인터 Y → 시간으로 변환할 때 사용한다.
  const scheduleGridRef = useRef<HTMLDivElement | null>(null);
  const filterControlRef = useRef<HTMLDivElement | null>(null);
  const layoutRectsRef = useRef<Map<string, LayoutRect>>(new Map());
  const skipNextLayoutAnimationRef = useRef(false);
  // 드래그 동안 FLIP 측정을 건너뛰면 layoutRectsRef가 낡는다. 드롭 후 첫 측정에서
  // 낡은 기준으로 애니메이션하지 않도록 stale 표시만 남긴다.
  const flipRectsStaleRef = useRef(false);
  // bootstrap은 3일 창만 준다(_load_bootstrap). payload를 통째로 교체할 때마다
  // 아래 fill effect가 다시 7일 창을 채우도록 applyLoadedBoard에서 리셋한다.
  // (1회용 ref로 두면 캐시 페인트→네트워크 교체 레이스에서 fill 결과가 날아가
  // 3컬럼 + 오른쪽 공백으로 고착됐다 — 컬럼이 3개면 가로 스크롤이 없어서
  // 스크롤 기반 append도 영영 안 불린다.)
  const initialWindowFilledRef = useRef(false);
  const sensors = useSensors(useSensor(TaskCardPointerSensor, { activationConstraint: { distance: 5 } }));
  const workspaceTimezone = payload?.timezone ?? DEFAULT_TIMEZONE;
  // Real today, computed once per render from the workspace timezone (not the selected day).
  const realToday = dateKeyInTimezone(new Date(), workspaceTimezone);
  // Tick once a minute so due-date "N분/시간/일 남음" chips stay fresh without an
  // interaction. One root timer re-renders the board; cards read this nowEpoch.
  const [nowEpoch, setNowEpoch] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowEpoch(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  // /notifications 딥링크(`/board?view=backlog&task=<id>`)의 task id. payload가
  // 로드된 뒤 해당 태스크가 실제로 존재하면 상세 모달을 한 번 연다(아래 effect).
  const pendingDeepLinkTaskIdRef = useRef<string | null>(null);

  const applyRequestedViewFromLocation = useCallback((dateKey: string) => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const rawView = params.get("view");
    const rawTask = params.get("task");
    if (rawTask) pendingDeepLinkTaskIdRef.current = rawTask;
    const requestedView = parseViewMode(rawView);
    if (!requestedView && !rawTask) return;
    if (requestedView) {
      setActiveSidebarItem(requestedView);
      setViewMode(requestedView);
      // Legacy "backlog" deep link opens the slide-in panel over Home.
      if (rawView === "backlog") setRightPanel("backlog");
      // "schedule" deep link(모바일 하단 스케줄 버튼)은 Home 위에 당일 스케줄 패널을 연다.
      if (rawView === "schedule") setRightPanel("schedule");
      setMobileColumnId(`day:${dateKey}`);
    }

    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.delete("view");
    nextUrl.searchParams.delete("task");
    window.history.replaceState(null, "", `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);
  }, []);

  const applyLoadedBoard = useCallback((nextRaw: PersonalBoardBootstrap, fromCache = false) => {
    // 담당 회사 프로젝트 채널(kind="company")도 내 보드에 노출한다. "Assign to channel"
    // 피커에서 회사 프로젝트 카테고리로 뜨고(개인 채널과 분리), 그 채널에 넣은 업무는
    // scope='company'로 회사에 트래킹된다.
    const next: PersonalBoardBootstrap = nextRaw;
    // 보드 전체 교체는 카드가 낡은 위치→새 위치로 미끄러지는 FLIP 대상이 아니다.
    // (캐시→서버 교체 시 전 카드 측정+애니메이션 폭풍 = 진입 시 멈칫의 한 축)
    skipNextLayoutAnimationRef.current = true;
    // 교체된 payload는 다시 3일 창일 수 있다 — fill effect가 재검사하게 리셋.
    initialWindowFilledRef.current = false;
    setPayload(next);
    setSelectedDate(next.selected_date);
    setMobileColumnId(`day:${next.selected_date}`);
    setFilterMode((next.preferences.filter_mode as FilterMode) || "all");
    setSortMode((next.preferences.sort_mode as SortMode) || "time");
    {
      // 저장된 우측 패널 값을 그대로 복원한다(backlog/schedule 모두 reload 후 유지). 모르는 값은 hidden.
      const rp = next.preferences.right_panel;
      setRightPanel(rp === "backlog" || rp === "schedule" ? rp : "hidden");
    }
    if (!fromCache) {
      setStatusMessage(`Synced ${next.node_id}`);
      setOfflineSyncedAt(null);
      lastSyncedAtRef.current = Date.now();
      // Persist the fresh server snapshot for offline reads (best-effort).
      void writeBoardSnapshot(next);
    }
  }, []);

  const refresh = useCallback(async (dateKey = selectedDate) => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.getPersonalBoard(dateKey);
      applyLoadedBoard(next);
    } catch (e) {
      // Offline / transient: keep the currently shown board and flag offline
      // instead of a hard error, if we have a cached snapshot.
      const cached = await readBoardSnapshot().catch(() => null);
      if (cached) {
        setOfflineSyncedAt(cached.syncedAt);
      } else {
        setError(e instanceof Error ? e.message : "개인 보드를 불러오지 못했습니다.");
      }
    } finally {
      setLoading(false);
    }
  }, [applyLoadedBoard, selectedDate]);

  // ---- 자정 넘김 재부트스트랩 (전날 미완료 이월 트리거) ----
  // 서버의 전날 미완료 자동 이월(rollover_overdue_tasks)은 /personal-board/bootstrap
  // 에서만 실행된다. 앱(데스크톱 웹뷰/브라우저 탭)을 자정 넘어 계속 켜두면 보드가
  // 재부트스트랩할 계기가 없어 이월이 트리거되지 않고 화면도 어제 컬럼에 머문다
  // (라이브 진단 2026-07-12: 세션은 살아있는데 bootstrap 0회 → 7/11 open 태스크 잔류).
  // 날짜가 바뀌면 오늘 날짜로 재부트스트랩한다. 감지는 두 경로:
  //  1) nowEpoch 60초 틱 렌더마다 재계산되는 realToday의 변화 (계속 떠 있는 창)
  //  2) focus/visibilitychange (웹뷰 절전으로 타이머가 잠들었다 깨어나는 복귀)
  const rolloverDayRef = useRef(realToday);
  useEffect(() => {
    if (realToday === rolloverDayRef.current) return;
    rolloverDayRef.current = realToday;
    // 명시적으로 오늘 날짜를 넘겨야 서버 rollover 게이트(day0 >= real_today)를 통과한다.
    void refresh(realToday);
  }, [realToday, refresh]);
  useEffect(() => {
    const check = () => {
      if (document.visibilityState !== "visible") return;
      const today = dateKeyInTimezone(new Date(), workspaceTimezone);
      if (today === rolloverDayRef.current) return;
      rolloverDayRef.current = today;
      void refresh(today);
    };
    window.addEventListener("focus", check);
    document.addEventListener("visibilitychange", check);
    return () => {
      window.removeEventListener("focus", check);
      document.removeEventListener("visibilitychange", check);
    };
  }, [refresh, workspaceTimezone]);

  useEffect(() => {
    let cancelled = false;
    async function loadInitial() {
      setError(null);
      // Resolve the owner (online) so the cache namespace is correct; mirror to
      // localStorage for offline reads. Best-effort, non-blocking.
      void api
        .getAuthMe()
        .then((me) => setOfflineBoardOwner(me.user_id))
        .catch(() => undefined);

      // 1. Paint the offline cache immediately if present (stale-while-revalidate).
      let hadCache = false;
      try {
        const cached = await readBoardSnapshot();
        if (!cancelled && cached) {
          hadCache = true;
          applyLoadedBoard(cached.snapshot, true);
          applyRequestedViewFromLocation(cached.snapshot.selected_date);
          setOfflineSyncedAt(cached.syncedAt);
          setLoading(false);
        }
      } catch {
        /* cache unavailable — fall through to network */
      }

      // 2. Revalidate from the network.
      try {
        const next = await api.getPersonalBoard();
        if (cancelled) return;
        applyLoadedBoard(next);
        applyRequestedViewFromLocation(next.selected_date);
      } catch (e) {
        if (cancelled) return;
        // Keep the cached view if we have one; otherwise surface the error.
        if (!hadCache) {
          setError(e instanceof Error ? e.message : "개인 보드를 불러오지 못했습니다.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void loadInitial();
    return () => {
      cancelled = true;
    };
  }, [applyLoadedBoard, applyRequestedViewFromLocation]);

  // ---- Offline write + sync (2단계, docs/offline-personal-board.md) ----
  // Persist optimistic offline edits to the read cache so a reload keeps them,
  // preserving the last server sync time (not "now").
  useEffect(() => {
    if (payload && offlineSyncedAt != null) {
      void writeBoardSnapshot(payload, offlineSyncedAt);
    }
  }, [payload, offlineSyncedAt]);

  const markQueued = useCallback(() => {
    setOfflineSyncedAt((current) => current ?? lastSyncedAtRef.current);
    setPendingSyncCount((c) => c + 1);
  }, []);

  // Offline-aware task mutation wrappers: attempt the network call; if it fails
  // because we are offline, queue the mutation for replay and return the
  // optimistic result so the UI keeps the edit instead of rolling back.
  const createTaskRemote = useCallback(
    async (input: PersonalTaskCreateInput, optimistic: PersonalBoardTask): Promise<PersonalBoardTask> => {
      try {
        return await api.createPersonalTask(input);
      } catch (e) {
        if (isNetworkError(e) && input.task_id) {
          await enqueueBoardMutation({ kind: "create_task", clientId: input.task_id, input });
          markQueued();
          return optimistic;
        }
        throw e;
      }
    },
    [markQueued],
  );

  const updateTaskRemote = useCallback(
    async (
      taskId: string,
      patch: PersonalTaskPatchInput,
      optimistic: PersonalBoardTask,
    ): Promise<PersonalBoardTask> => {
      try {
        return await api.updatePersonalTask(taskId, patch);
      } catch (e) {
        if (isNetworkError(e)) {
          await enqueueBoardMutation({ kind: "update_task", taskId, patch });
          markQueued();
          return optimistic;
        }
        throw e;
      }
    },
    [markQueued],
  );

  const deleteTaskRemote = useCallback(
    async (taskId: string): Promise<void> => {
      try {
        await api.deletePersonalTask(taskId);
      } catch (e) {
        if (isNetworkError(e)) {
          await enqueueBoardMutation({ kind: "delete_task", taskId });
          markQueued();
          return;
        }
        throw e;
      }
    },
    [markQueued],
  );

  const flushHandlers = useMemo<FlushHandlers>(
    () => ({
      create_task: async (m) => {
        await api.createPersonalTask({ ...m.input, task_id: m.clientId });
      },
      update_task: async (m) => {
        await api.updatePersonalTask(m.taskId, m.patch);
      },
      delete_task: async (m) => {
        await api.deletePersonalTask(m.taskId);
      },
    }),
    [],
  );

  const syncOutbox = useCallback(async () => {
    if (typeof navigator !== "undefined" && navigator.onLine === false) return;
    const result = await flushOutbox(flushHandlers);
    setPendingSyncCount(result.remaining);
    if (!result.stoppedOffline && result.flushed > 0) {
      setOfflineSyncedAt(null);
      void refresh(selectedDate);
    }
  }, [flushHandlers, refresh, selectedDate]);

  useEffect(() => {
    void (async () => {
      setPendingSyncCount(await pendingMutationCount());
      await syncOutbox();
    })();
    window.addEventListener("online", syncOutbox);
    return () => window.removeEventListener("online", syncOutbox);
  }, [syncOutbox]);

  // ---- Infinite horizontal date window (Home day board only) ----
  // Merge fetched days into payload.days, deduped by date and kept date-sorted.
  // edge="append" adds to the end; edge="prepend" adds to the front.
  const mergeDays = useCallback((incoming: PersonalBoardDay[], edge: "append" | "prepend") => {
    setPayload((current) => {
      if (!current) return current;
      const seen = new Set(current.days.map((day) => day.date));
      const fresh = incoming.filter((day) => !seen.has(day.date));
      if (fresh.length === 0) return current;
      const merged = edge === "prepend" ? [...fresh, ...current.days] : [...current.days, ...fresh];
      merged.sort((a, b) => a.date.localeCompare(b.date));
      return { ...current, days: merged };
    });
  }, []);

  const loadDaysWindow = useCallback(
    async (start: string, count: number, edge: "append" | "prepend"): Promise<boolean> => {
      // 반환값: 실제로 로드가 완료됐으면 true. 동시 로드로 스킵됐거나 fetch가
      // 실패했으면 false — initial fill effect가 다음 payload 변경 때 재시도한다.
      if (isLoadingDaysRef.current) return false;
      isLoadingDaysRef.current = true;
      try {
        const result = await api.getPersonalDays(start, count);
        const board = dayBoardRef.current;
        if (edge === "prepend" && board) {
          // Preserve scroll position when inserting to the left so the view does not jump.
          const prevWidth = board.scrollWidth;
          const prevLeft = board.scrollLeft;
          // 왼쪽에 날짜를 끼우면 기존 카드의 컨텐츠 위치가 통째로 오른쪽으로 밀린다.
          // 아래 scrollLeft 보정으로 화면상 위치는 그대로지만, flushSync 도중 실행되는
          // FLIP은 보정 전 위치를 잡아 카드를 좌우로 흔든다. 이 머지 렌더는 FLIP을 건너뛴다.
          skipNextLayoutAnimationRef.current = true;
          flushSync(() => mergeDays(result.days, edge));
          board.scrollLeft = prevLeft + (board.scrollWidth - prevWidth);
        } else {
          mergeDays(result.days, edge);
        }
        return true;
      } catch {
        // Best-effort lazy load; a failed window fetch is non-fatal.
        return false;
      } finally {
        isLoadingDaysRef.current = false;
      }
    },
    [mergeDays],
  );

  // Scroll a given day column to the board's left edge (keeping its left inset),
  // so "go to today / go to date" lands that day as the left-most column.
  const scrollDateIntoView = useCallback((dateKey: string) => {
    const board = dayBoardRef.current;
    if (!board) return false;
    const col = board.querySelector(`[data-drop-id="day:${dateKey}"]`) as HTMLElement | null;
    if (!col) return false;
    const pad = parseFloat(getComputedStyle(board).paddingLeft) || 0;
    const delta = col.getBoundingClientRect().left - board.getBoundingClientRect().left - pad;
    board.scrollBy({ left: delta, behavior: "smooth" });
    return true;
  }, []);

  // Navigate the day board to a single date (clears any active range). Scrolls
  // immediately if the column is already loaded; otherwise reloads a window
  // around that date and scrolls once it renders (via pendingScrollDateRef).
  const goToDate = useCallback(
    (dateKey: string) => {
      setRangeFilter(null);
      setRangePickStart(null);
      setSelectedDate(dateKey);
      // 모바일은 단일 컬럼만 보여주므로(active-mobile) 날짜 이동 시 보이는 날짜도 같이 옮긴다.
      // 데스크톱에선 mobileColumnId가 표시에 영향 없으므로 무해하다.
      setMobileColumnId(`day:${dateKey}`);
      if (viewMode !== "home") setViewMode("home");
      requestAnimationFrame(() => {
        if (!scrollDateIntoView(dateKey)) {
          pendingScrollDateRef.current = dateKey;
          void refresh(dateKey);
        }
      });
    },
    [viewMode, scrollDateIntoView, refresh],
  );

  // Apply a "from .. to" range: load every day in the span, clamp the board to
  // it (infinite scroll pauses), and land the start day left-most.
  const applyRange = useCallback(
    (start: string, end: string) => {
      const span = Math.max(1, Math.round((dateFromKey(end).getTime() - dateFromKey(start).getTime()) / 86_400_000) + 1);
      setRangeFilter({ start, end });
      setRangePickStart(null);
      setSelectedDate(start);
      if (viewMode !== "home") setViewMode("home");
      pendingScrollDateRef.current = start;
      void loadDaysWindow(start, span, "append");
    },
    [viewMode, loadDaysWindow],
  );

  const clearRange = useCallback(() => {
    setRangeFilter(null);
    setRangePickStart(null);
  }, []);

  // Fill a 7-day window from the bootstrap's selected date (server bootstrap
  // only carries 3 days) so the board opens with that day as the left-most
  // column. Past days load on demand when the user scrolls left. Re-armed on
  // every bootstrap replace (applyLoadedBoard resets the ref); a pinned range
  // manages its own span, so skip while one is active.
  useEffect(() => {
    if (!payload || initialWindowFilledRef.current || rangeFilter) return;
    initialWindowFilledRef.current = true;
    // realToday가 아니라 bootstrap의 selected_date 기준: 과거 날짜로 점프한 뒤
    // refresh로 교체된 3일 창을 오늘 기준으로 채우면 중간이 빈(비연속) 컬럼이 된다.
    const desiredStart = payload.selected_date;
    const desiredEnd = addDays(desiredStart, 6);
    const present = new Set(payload.days.map((day) => day.date));
    let needsFill = false;
    for (let cursor = desiredStart; cursor <= desiredEnd; cursor = addDays(cursor, 1)) {
      if (!present.has(cursor)) {
        needsFill = true;
        break;
      }
    }
    if (needsFill) {
      void loadDaysWindow(desiredStart, 7, "append").then((loaded) => {
        // 동시 로드에 밀려 스킵됐거나 실패했으면 다음 payload 변경 때 재시도한다.
        // (그 동시 로드의 병합 자체가 payload를 바꿔 이 effect를 다시 깨운다.)
        if (!loaded) initialWindowFilledRef.current = false;
      });
    }
  }, [payload, rangeFilter, loadDaysWindow]);

  // 보드 DOM은 payload가 처음 도착해야 마운트된다. 아래 wheel/pan effect가
  // payload 자체를 deps로 두면 prepend 병합마다 재등록돼 진행 중인 팬이 끊기고,
  // deps에서 아예 빼면 마운트 시점(ref null)에만 실행돼 리스너가 안 붙는다 —
  // "마운트 됐는지"만 dep으로 쓴다.
  const boardMounted = payload != null;

  // Vertical mouse-wheel scrolls each column vertically (native), never the day
  // board horizontally. Horizontal date navigation is left to native sideways
  // scroll (trackpad swipe / shift+wheel) and grab-to-pan below. Only a sideways
  // wheel at the very left edge pulls older days in so the past keeps scrolling.
  useEffect(() => {
    const board = dayBoardRef.current;
    if (!board || viewMode !== "home") return;
    const onWheel = (event: WheelEvent) => {
      // Vertical wheel: let it scroll the hovered column natively, no hijack.
      if (Math.abs(event.deltaX) <= Math.abs(event.deltaY)) return;
      // Sideways wheel at the left edge pulls in older days.
      const days = daysRef.current;
      if (event.deltaX < 0 && board.scrollLeft <= 0 && days.length && !rangeFilterRef.current && !scheduleFocusRef.current) {
        void loadDaysWindow(addDays(days[0].date, -7), 7, "prepend");
      }
    };
    board.addEventListener("wheel", onWheel, { passive: true });
    return () => board.removeEventListener("wheel", onWheel);
  }, [viewMode, boardMounted, loadDaysWindow]);

  // ---- Grab-to-pan (click empty space and drag the board horizontally) ----
  // Distinct from task drag-and-drop: it only starts on empty board/column
  // background, never on cards or interactive controls, so it scrolls the date
  // window without touching dnd-kit. Sunsama-style "grab the canvas" navigation.
  useEffect(() => {
    const board = dayBoardRef.current;
    if (!board || viewMode !== "home") return;
    let panning = false;
    let lastX = 0;

    const isPannableTarget = (target: EventTarget | null) => {
      if (!(target instanceof HTMLElement)) return false;
      return !target.closest(
        ".task-card, .objective-card, form, button, a, input, textarea, select, label, [role='dialog'], [data-card-drag-handle], [data-no-card-drag]",
      );
    };

    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0 || event.pointerType === "touch") return;
      if (!isPannableTarget(event.target)) return;
      panning = true;
      lastX = event.clientX;
      board.classList.add("panning");
      event.preventDefault();
    };

    const onPointerMove = (event: PointerEvent) => {
      if (!panning) return;
      // 증분 이동: 절대(start 기준) 방식은 과거 날짜 prepend 시 스크롤 보정
      // (loadDaysWindow의 scrollLeft 재조정)을 다음 move가 낡은 기준으로 되돌려
      // 새 데이터가 와도 팬이 벽에 붙은 것처럼 멈췄다 — 잡은 채로 계속 끌리게
      // 마지막 포인터 위치와의 차이만 적용한다.
      const dx = event.clientX - lastX;
      lastX = event.clientX;
      board.scrollLeft -= dx;
      // Dragging right toward the left edge pulls older days in. Prefetch before
      // the hard edge (600px, append 쪽과 대칭) so the pan rarely hits the wall.
      const days = daysRef.current;
      if (dx > 0 && board.scrollLeft < 600 && days.length && !isLoadingDaysRef.current && !rangeFilterRef.current && !scheduleFocusRef.current) {
        void loadDaysWindow(addDays(days[0].date, -7), 7, "prepend");
      }
    };

    const endPan = () => {
      if (!panning) return;
      panning = false;
      board.classList.remove("panning");
    };

    board.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", endPan);
    window.addEventListener("pointercancel", endPan);
    return () => {
      board.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", endPan);
      window.removeEventListener("pointercancel", endPan);
      board.classList.remove("panning");
    };
    // payload를 deps에 넣으면 prepend 병합마다 effect가 재등록돼 진행 중인 팬이
    // 끊긴다(클로저 panning 리셋). days는 daysRef로 읽는다.
  }, [viewMode, boardMounted, loadDaysWindow]);

  // While dragging a ticket/objective, keep the horizontal board moving when
  // the pointer sits near the left/right edge. dnd-kit's built-in auto-scroll is
  // not reliable here because each day has its own vertical scroll container
  // nested inside the horizontal board.
  useEffect(() => {
    if (viewMode !== "home") return;
    let pointerX: number | null = null;
    let pointerY: number | null = null;
    let frame: number | null = null;

    const stop = () => {
      pointerX = null;
      pointerY = null;
      if (frame != null) {
        window.cancelAnimationFrame(frame);
        frame = null;
      }
    };

    const tick = () => {
      frame = null;
      const board = dayBoardRef.current;
      if (!isDraggingRef.current || !board || pointerX == null || pointerY == null) {
        stop();
        return;
      }

      const rect = board.getBoundingClientRect();
      const ySlack = 36;
      const xSlack = 72;
      const nearBoardVertically = pointerY >= rect.top - ySlack && pointerY <= rect.bottom + ySlack;
      const nearBoardHorizontally = pointerX >= rect.left - xSlack && pointerX <= rect.right + xSlack;
      if (!nearBoardVertically || !nearBoardHorizontally || rect.width <= 0) return;

      const edge = Math.min(112, Math.max(56, rect.width * 0.16));
      let direction = 0;
      let pressure = 0;
      if (pointerX < rect.left + edge) {
        direction = -1;
        pressure = (rect.left + edge - pointerX) / edge;
      } else if (pointerX > rect.right - edge) {
        direction = 1;
        pressure = (pointerX - (rect.right - edge)) / edge;
      }
      if (direction === 0) return;

      const step = direction * Math.ceil(Math.min(1, pressure) ** 2 * 26);
      board.scrollLeft += step;

      const days = daysRef.current;
      if (days.length > 0 && !rangeFilterRef.current && !scheduleFocusRef.current) {
        // 왼쪽도 오른쪽(600px)처럼 벽에 닿기 전에 프리페치 — scrollLeft 0까지
        // 기다렸다 로드하면 드래그가 로드 동안 벽에 멈춰 있어야 했다.
        if (step < 0 && board.scrollLeft < 600 && !isLoadingDaysRef.current) {
          void loadDaysWindow(addDays(days[0].date, -7), 7, "prepend");
        } else if (
          step > 0 &&
          board.scrollWidth - board.scrollLeft - board.clientWidth < 600 &&
          !isLoadingDaysRef.current
        ) {
          void loadDaysWindow(addDays(days[days.length - 1].date, 1), 7, "append");
        }
      }

      frame = window.requestAnimationFrame(tick);
    };

    const onPointerMove = (event: PointerEvent) => {
      if (!isDraggingRef.current) return;
      pointerX = event.clientX;
      pointerY = event.clientY;
      if (frame == null) frame = window.requestAnimationFrame(tick);
    };

    window.addEventListener("pointermove", onPointerMove, true);
    window.addEventListener("pointerup", stop, true);
    window.addEventListener("pointercancel", stop, true);
    return () => {
      stop();
      window.removeEventListener("pointermove", onPointerMove, true);
      window.removeEventListener("pointerup", stop, true);
      window.removeEventListener("pointercancel", stop, true);
    };
    // days를 deps로 두면 드래그 중 prepend/append 병합 순간 effect가 재등록돼
    // 자동 스크롤 프레임/포인터 상태가 리셋된다(순간 멈춤). daysRef로 읽는다.
  }, [viewMode, loadDaysWindow]);

  const handleDayBoardScroll = useCallback(() => {
    const board = dayBoardRef.current;
    if (!board || !payload || viewMode !== "home" || isLoadingDaysRef.current) return;
    // A pinned "from..to" range never grows the window past its bounds.
    if (rangeFilterRef.current) return;
    // Don't insert/prepend day columns while dragging — the instant scroll jump
    // makes the auto-scroll feel like it teleports. Resume after the drop.
    if (isDraggingRef.current) return;
    const days = payload.days;
    if (days.length === 0) return;
    const scrollLeft = board.scrollLeft;
    const scrollingLeft = scrollLeft < lastScrollLeftRef.current - 1;
    lastScrollLeftRef.current = scrollLeft;
    const distanceToRight = board.scrollWidth - scrollLeft - board.clientWidth;
    if (distanceToRight < 600) {
      void loadDaysWindow(addDays(days[days.length - 1].date, 1), 7, "append");
      return;
    }
    // Only load past days on an actual leftward scroll, never on mount (when
    // scrollLeft is already 0 and today should stay the left-most column).
    if (scrollLeft < 600 && scrollingLeft) {
      void loadDaysWindow(addDays(days[0].date, -7), 7, "prepend");
    }
  }, [payload, viewMode, loadDaysWindow]);

  // 스케줄 패널이 열려 있으면 좌측 보드를 선택일(당일) 한 컬럼으로 고정한다. 스케줄은
  // 당일 관리 전용이라, 티켓을 스케줄에 넣을 때 좌측 날짜 컬럼이 옮겨가지 않게 한다.
  const scheduleFocus = rightPanel === "schedule" && viewMode === "home";

  // Keep the range/schedule guard refs in sync for the (non-React) scroll/pan handlers.
  useEffect(() => {
    rangeFilterRef.current = rangeFilter;
    scheduleFocusRef.current = scheduleFocus;
  }, [rangeFilter, scheduleFocus]);
  useEffect(() => {
    daysRef.current = payload?.days ?? [];
  }, [payload?.days]);

  // After a date-jump that needed a reload (or a range apply), scroll the target
  // day to the left edge once its column has rendered.
  useEffect(() => {
    const target = pendingScrollDateRef.current;
    if (!target || viewMode !== "home") return;
    if (!(payload?.days ?? []).some((day) => day.date === target)) return;
    requestAnimationFrame(() => {
      if (scrollDateIntoView(target)) pendingScrollDateRef.current = null;
    });
  }, [payload, viewMode, scrollDateIntoView]);

  // Close the Today date menu on outside click (abandoning any half-made range).
  useEffect(() => {
    if (!dateMenuOpen) return;
    // pointerdown: 마우스·터치·펜 모두 커버(터치 기기에서 바깥 탭으로 안 닫히던 문제).
    const onDown = (event: PointerEvent) => {
      if (dateMenuRef.current && !dateMenuRef.current.contains(event.target as Node)) {
        setDateMenuOpen(false);
        setRangePickStart(null);
      }
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [dateMenuOpen]);

  // 카드 위치에 영향을 주는 값들만 의존성으로 둔다. Add task 입력의 `taskDrafts`
  // 같은 무관한 state는 매 키 입력마다 리렌더를 일으키지만 카드 위치는 그대로이므로,
  // 의존성에서 빼야 이 effect가 다시 돌지 않는다. 매 렌더마다 돌면 진행 중인 layout
  // 애니메이션을 cancel(=최종 위치로 순간 점프)시켜 타이핑 중 카드가 흔들린다.
  useLayoutEffect(() => {
    // 드래그 중에는 over 대상이 바뀔 때마다(activeOverId dep) 이 effect가 돌아
    // 전 카드 querySelectorAll ×3 + 카드별 getBoundingClientRect 강제 레이아웃이
    // paint 전에 매번 실행됐다 — 카드 수에 비례해 메인 스레드가 막혀 "드래그가
    // 잠깐잠깐 멈추는" 원인. 드래그 동안은 FLIP을 통째로 건너뛰고(드롭 프리뷰
    // 자리 이동은 스냅 배치), 기준 좌표만 stale로 표시해 드롭 후 첫 렌더에서
    // 애니메이션 없이 재측정한다.
    if (activeTaskId || activeObjectiveDrag) {
      flipRectsStaleRef.current = true;
      return;
    }
    // 진행 중인 layout 애니메이션을 먼저 멈춰 transform이 걷힌 '정지 위치'를 측정한다.
    // 그래야 다음 렌더의 기준 좌표가 정확해진다.
    cancelLayoutAnimations();
    const restingRects = collectLayoutRects(dayBoardRef.current);
    if (skipNextLayoutAnimationRef.current || flipRectsStaleRef.current) {
      skipNextLayoutAnimationRef.current = false;
      flipRectsStaleRef.current = false;
      layoutRectsRef.current = restingRects;
      return;
    }
    animateLayoutShift(layoutRectsRef.current, restingRects);
    layoutRectsRef.current = restingRects;
  }, [
    payload,
    objectivesByWeek,
    viewMode,
    rightPanel,
    sortMode,
    filterMode,
    rangeFilter,
    hiddenSubtasks,
    instantPlacementTaskIds,
    mobileColumnId,
    activeOverId,
    activeTaskId,
    activeObjectiveDrag,
  ]);

  useEffect(() => {
    if (!filterMenuOpen) return;
    function closeOnOutsidePointer(event: globalThis.PointerEvent) {
      if (!(event.target instanceof Node)) return;
      if (!filterControlRef.current?.contains(event.target)) setFilterMenuOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setFilterMenuOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [filterMenuOpen]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.dispatchEvent(
      new CustomEvent("orthus:personal-board-state", {
        detail: {
	          activeSidebarItem,
	          folders: payload?.folders ?? [],
	          rightPanel,
	          viewMode,
          workspaceName: payload?.workspace_name ?? "개인 워크스페이스",
        },
      }),
    );
  }, [
	    activeSidebarItem,
	    payload?.folders,
	    payload?.workspace_name,
    rightPanel,
    viewMode,
  ]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    function handleBoardCommand(event: Event) {
      const detail = (event as CustomEvent<{ command?: string }>).detail;
      const command = detail?.command;
      // Sidebar "Backlog" toggles the slide-in panel without leaving the day board.
      if (command === "backlog") {
        setActiveSidebarItem("home");
        setViewMode("home");
        setRightPanel((current) => {
          const next = current === "backlog" ? "hidden" : "backlog";
          void api.updatePersonalPreferences({ right_panel: next }).catch((error) => {
            setError(error instanceof Error ? error.message : "패널 상태 저장 실패");
          });
          return next;
        });
        setMobileColumnId(`day:${selectedDate}`);
        return;
      }
      // 모바일 하단 "스케줄" 버튼: Home 위에 당일 스케줄 패널을 연다(네비 목적이라 토글이
      // 아니라 항상 open). 뷰 상태는 backlog 커맨드와 같은 방식으로 저장한다.
      if (command === "schedule") {
        setActiveSidebarItem("home");
        setViewMode("home");
        setRightPanel((current) => {
          if (current !== "schedule") {
            void api.updatePersonalPreferences({ right_panel: "schedule" }).catch((error) => {
              setError(error instanceof Error ? error.message : "패널 상태 저장 실패");
            });
          }
          return "schedule";
        });
        setMobileColumnId(`day:${selectedDate}`);
        return;
      }
      const mapped: ViewMode | null =
        command === "home"
          ? "home"
          : command === "weekly_plan"
            ? "weekly_plan"
            : command === "weekly_review"
              ? "weekly_review"
              : null;
      if (mapped) {
        setActiveSidebarItem(mapped);
        setViewMode(mapped);
        setMobileColumnId(`day:${selectedDate}`);
        return;
      }
	      if (command === "refresh") {
	        void refresh();
	      }
    }
    window.addEventListener("orthus:personal-board-command", handleBoardCommand);
    return () => {
      window.removeEventListener("orthus:personal-board-command", handleBoardCommand);
    };
	  }, [refresh, selectedDate]);

  // 실시간 반영: 에이전트(orthus ticket CLI/MCP)나 다른 기기가 내 티켓을 바꾸면
  // 서버가 내용 없는 변경 핑(personal_board_changed, owner 필터는 서버)을 SSE로
  // 보낸다. 받으면 스켈레톤 없이 조용히 재조회해 열린 보드가 그 자리에서 갱신된다.
  const liveReloadRef = useRef<() => void>(() => {});
  useEffect(() => {
    liveReloadRef.current = () => {
      void api
        .getPersonalBoard(selectedDate)
        .then((next) => applyLoadedBoard(next))
        .catch(() => {
          /* 오프라인/일시 오류 — 다음 핑에서 다시 시도 */
        });
    };
  }, [selectedDate, applyLoadedBoard]);
  useEffect(() => {
    let es: EventSource | null = null;
    let backoff: ReturnType<typeof setTimeout> | null = null;
    let debounce: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    const connect = () => {
      if (closed) return;
      try {
        // 세션 쿠키 인증(plans 페이지와 동일 패턴) — cross-origin session mode용
        // withCredentials, same-origin은 자동.
        es = new EventSource(`${API_BASE}/dashboard/stream`, { withCredentials: true });
      } catch {
        return;
      }
      es.onmessage = (e) => {
        try {
          const ev = JSON.parse(e.data) as { type?: string };
          if (ev?.type !== "personal_board_changed") return;
          if (debounce) clearTimeout(debounce);
          // 연속 변경(에이전트가 티켓 여러 개 생성)을 1회 재조회로 합친다.
          debounce = setTimeout(() => liveReloadRef.current(), 400);
        } catch {
          /* keepalive 등 비-JSON 무시 */
        }
      };
      es.onerror = () => {
        es?.close();
        if (!closed) backoff = setTimeout(connect, 5000);
      };
    };
    connect();
    return () => {
      closed = true;
      es?.close();
      if (backoff) clearTimeout(backoff);
      if (debounce) clearTimeout(debounce);
    };
  }, []);

  const activeTask = activeTaskId ? allTasks(payload).find((task) => task.task_id === activeTaskId) ?? null : null;
  const activeObjectivePreview = activeObjectiveDrag
    ? (() => {
        const o = Object.values(objectivesByWeek)
          .flat()
          .find((item) => item.objective_id === activeObjectiveDrag.objectiveId);
        if (!o) return null;
        return {
          title: o.title,
          projectName: o.project?.name ?? null,
        };
      })()
    : null;
  const editingTask = editingTaskId ? allTasks(payload).find((task) => task.task_id === editingTaskId) ?? null : null;

  // 티켓 팝업을 열면 그 태스크의 댓글을 서버에서 로드한다. temp id(오프라인 생성
  // 직후)는 서버에 아직 없어 건너뛴다. 실패는 조용히 두고(빈 목록) 등록 시 에러로 안내.
  useEffect(() => {
    if (!editingTaskId || isTempTaskId(editingTaskId)) return;
    let cancelled = false;
    void api
      .listPersonalTaskComments(editingTaskId)
      .then((rows) => {
        if (!cancelled) {
          setTaskComments((current) => ({ ...current, [editingTaskId]: rows }));
        }
      })
      .catch(() => {
        /* 오프라인/일시 오류 — 기존 캐시 유지 */
      });
    return () => {
      cancelled = true;
    };
  }, [editingTaskId]);

  // 노트 디바운스 저장 중 탭 숨김/이탈(모바일 앱 전환, 창 닫기)이 오면 즉시 flush.
  useEffect(() => {
    const onHide = () => flushNoteSaveRef.current();
    // 데스크톱(WKWebView) 종료에선 pagehide가 안 올 수 있어 beforeunload도 함께 듣고,
    // 창 포커스 이탈(앱 전환)에도 미리 저장해 유실 창을 좁힌다.
    const onWindowBlur = () => flushNoteSaveRef.current();
    window.addEventListener("pagehide", onHide);
    window.addEventListener("beforeunload", onHide);
    window.addEventListener("blur", onWindowBlur);
    document.addEventListener("visibilitychange", onHide);
    return () => {
      window.removeEventListener("pagehide", onHide);
      window.removeEventListener("beforeunload", onHide);
      window.removeEventListener("blur", onWindowBlur);
      document.removeEventListener("visibilitychange", onHide);
    };
  }, []);

  const editingObj = editingObjective
    ? Object.values(objectivesByWeek)
        .flat()
        .find((o) => o.objective_id === editingObjective.objectiveId) ?? null
    : null;

	  const visibleDays = useMemo(() => {
	    const all = payload?.days ?? [];
	    if (!rangeFilter) return all;
	    return all.filter((day) => day.date >= rangeFilter.start && day.date <= rangeFilter.end);
	  }, [payload?.days, rangeFilter]);

	  const boardDays = useMemo(() => {
	    if (!scheduleFocus) return visibleDays;
	    const focused = visibleDays.filter((day) => day.date === selectedDate);
	    // selectedDate는 보통 항상 로드돼 있지만, 범위 로드 실패 등 드문 경우 빈 좌측 보드가
	    // 되지 않게 첫 컬럼으로 폴백한다.
	    return focused.length ? focused : visibleDays.slice(0, 1);
	  }, [scheduleFocus, visibleDays, selectedDate]);

	  const mobileTabs = useMemo(
	    () => [
	      ...boardDays.map((day) => ({ id: `day:${day.date}`, label: day.date === realToday ? "Today" : weekdayLabel(day.date) })),
	      ...(rightPanel === "backlog" ? [{ id: "backlog", label: "Backlog" }] : []),
	    ],
	    [realToday, rightPanel, boardDays],
	  );
  const activeMobileColumnId = mobileTabs.some((tab) => tab.id === mobileColumnId)
    ? mobileColumnId
    : mobileTabs[0]?.id;

  const backlogTasksByBucket = useMemo(() => {
    const map = new Map<string, PersonalBoardTask[]>();
    for (const bucket of payload?.backlog_buckets ?? []) {
      map.set(bucket.bucket_id, prepareTasks(bucket.tasks, filterMode, sortMode));
    }
    return map;
  }, [filterMode, payload?.backlog_buckets, sortMode]);

  // 스케줄 패널(선택일) 데이터: 그 날 태스크 + 고정 이벤트를 분 단위 블록으로, 그리고 오늘이면
  // 현재 시각(분)을 함께 만든다. 고정 이벤트는 타임존 변환이 필요해 여기서 분으로 바꿔 넘긴다.
  const scheduleDay = payload?.days.find((day) => day.date === selectedDate) ?? null;
  const scheduleEvents = useMemo<ScheduleEventBlock[]>(() => {
    return (scheduleDay?.fixed_events ?? []).map((ev) => {
      const startMin = parseClockToMin(timeInputValue(ev.starts_at, workspaceTimezone)) ?? 0;
      const parsedEnd = parseClockToMin(timeInputValue(ev.ends_at, workspaceTimezone));
      const endMin = parsedEnd != null && parsedEnd > startMin ? parsedEnd : startMin + 60;
      return { event_id: ev.event_id, title: ev.title, startMin, endMin, color: ev.project?.color ?? null };
    });
  }, [scheduleDay?.fixed_events, workspaceTimezone]);
  const scheduleNowMinutes = useMemo<number | null>(() => {
    if (selectedDate !== realToday) return null;
    const parts = dateTimePartsInTimezone(new Date(nowEpoch), workspaceTimezone);
    return parts ? Number(parts.hour) * 60 + Number(parts.minute) : null;
  }, [selectedDate, realToday, nowEpoch, workspaceTimezone]);

  function flash(message: string) {
    setStatusMessage(message);
    window.setTimeout(() => setStatusMessage("Personal wiki queued"), 1400);
  }

  // 아직 서버에 저장 중인(temp id) 태스크에 대한 액션은 차단한다. temp id가 API로
  // 가면 백엔드가 uuid_parsing 422를 낸다. 저장은 수백 ms면 끝나므로 잠깐만 막힌다.
  function guardTempTask(taskId: string): boolean {
    if (isTempTaskId(taskId)) {
      flash("저장 중입니다. 잠시 후 다시 시도하세요.");
      return true;
    }
    return false;
  }

  function setTaskInState(taskId: string, patch: Partial<PersonalBoardTask>) {
    updateTaskInState(taskId, (task) => ({ ...task, ...patch }));
  }

  function updateTaskInState(taskId: string, updater: (task: PersonalBoardTask) => PersonalBoardTask) {
    setPayload((current) => {
      if (!current) return current;
      const patchOne = (task: PersonalBoardTask) => (task.task_id === taskId ? updater(task) : task);
      return {
        ...current,
        days: current.days.map((day) => ({ ...day, tasks: day.tasks.map(patchOne) })),
        backlog_buckets: current.backlog_buckets.map((bucket) => ({
          ...bucket,
          tasks: bucket.tasks.map(patchOne),
        })),
      };
    });
  }

  function removeTaskInState(taskId: string) {
    setPayload((current) =>
      current
        ? {
            ...current,
            days: current.days.map((day) => ({
              ...day,
              tasks: day.tasks.filter((task) => task.task_id !== taskId),
            })),
            backlog_buckets: current.backlog_buckets.map((bucket) => ({
              ...bucket,
              tasks: bucket.tasks.filter((task) => task.task_id !== taskId),
            })),
          }
        : current,
    );
  }

  function addOptimisticTask(task: PersonalBoardTask) {
    setPayload((current) => {
      if (!current) return current;
      return {
        ...current,
        days: current.days.map((day) =>
          day.date === task.scheduled_date
            ? { ...day, tasks: sortContainerTasks([...day.tasks, task]) }
            : day,
        ),
        backlog_buckets: current.backlog_buckets.map((bucket) =>
          bucket.bucket_id === task.backlog_bucket_id
            ? { ...bucket, tasks: sortContainerTasks([...bucket.tasks, task]) }
            : bucket,
        ),
      };
    });
  }

  function upsertTaskInState(task: PersonalBoardTask) {
    setPayload((current) => {
      if (!current) return current;
      const days = current.days.map((day) => ({
        ...day,
        tasks: day.tasks.filter((item) => item.task_id !== task.task_id),
      }));
      const buckets = current.backlog_buckets.map((bucket) => ({
        ...bucket,
        tasks: bucket.tasks.filter((item) => item.task_id !== task.task_id),
      }));
      if (task.status === "archived") {
        return { ...current, days, backlog_buckets: buckets };
      }
      const nextDays = days.map((day) =>
        task.scheduled_date === day.date ? { ...day, tasks: sortContainerTasks([...day.tasks, task]) } : day,
      );
      const nextBuckets = buckets.map((bucket) =>
        task.backlog_bucket_id === bucket.bucket_id
          ? { ...bucket, tasks: sortContainerTasks([...bucket.tasks, task]) }
          : bucket,
      );
      return { ...current, days: nextDays, backlog_buckets: nextBuckets };
    });
  }

  function upsertProjectInState(project: PersonalBoardProject) {
    setPayload((current) => {
      if (!current) return current;
      return {
        ...current,
        projects: [...current.projects.filter((item) => item.project_id !== project.project_id), project].sort((a, b) => a.name.localeCompare(b.name)),
        days: current.days.map((day) => ({
          ...day,
          tasks: day.tasks.map((task) => (task.project?.project_id === project.project_id ? { ...task, project } : task)),
          fixed_events: day.fixed_events.map((event) => (event.project?.project_id === project.project_id ? { ...event, project } : event)),
        })),
        backlog_buckets: current.backlog_buckets.map((bucket) => ({
          ...bucket,
          tasks: bucket.tasks.map((task) => (task.project?.project_id === project.project_id ? { ...task, project } : task)),
        })),
      };
    });
  }

  function removeProjectFromState(projectId: string) {
    setPayload((current) => {
      if (!current) return current;
      const clearTask = <T extends { project?: PersonalBoardProject | null }>(item: T): T =>
        item.project?.project_id === projectId ? { ...item, project: null } : item;
      return {
        ...current,
        projects: current.projects.filter((item) => item.project_id !== projectId),
        days: current.days.map((day) => ({
          ...day,
          tasks: day.tasks.map(clearTask),
          fixed_events: day.fixed_events.map(clearTask),
        })),
        backlog_buckets: current.backlog_buckets.map((bucket) => ({
          ...bucket,
          tasks: bucket.tasks.map(clearTask),
        })),
      };
    });
  }

  function patchBacklogBucketInState(bucketId: string, patch: Partial<Omit<PersonalBoardBacklogBucket, "tasks">>) {
    setPayload((current) =>
      current
        ? {
            ...current,
            backlog_buckets: current.backlog_buckets.map((bucket) => (bucket.bucket_id === bucketId ? { ...bucket, ...patch } : bucket)),
          }
        : current,
    );
  }

  function moveTaskInState(
    task: PersonalBoardTask,
    destination: MoveDestination,
    orderIndex?: number,
  ) {
    setPayload((current) => {
      if (!current) return current;
      const nextTask: PersonalBoardTask = {
        ...task,
        scheduled_date: destination.type === "day" ? destination.date : null,
        scheduled_time: destination.type === "day" ? task.scheduled_time : null,
        backlog_bucket_id: destination.type === "backlog" ? destination.bucketId : null,
      };
      const insertAt = (items: PersonalBoardTask[]) => {
        const withoutMoving = items.filter((item) => item.task_id !== task.task_id);
        const index = orderIndex == null ? withoutMoving.length : Math.min(orderIndex, withoutMoving.length);
        const nextItems = [...withoutMoving.slice(0, index), nextTask, ...withoutMoving.slice(index)];
        return nextItems.map((item, nextOrder) => ({ ...item, order_index: nextOrder }));
      };
      const days = current.days.map((day) => {
        const baseTasks = day.tasks.filter((item) => item.task_id !== task.task_id);
        return {
          ...day,
          tasks: day.date === nextTask.scheduled_date ? insertAt(day.tasks) : baseTasks.map((item, index) => ({ ...item, order_index: index })),
        };
      });
      const backlogBuckets = current.backlog_buckets.map((bucket) => {
        const baseTasks = bucket.tasks.filter((item) => item.task_id !== task.task_id);
        return {
          ...bucket,
          tasks:
            bucket.bucket_id === nextTask.backlog_bucket_id
              ? insertAt(bucket.tasks)
              : baseTasks.map((item, index) => ({ ...item, order_index: index })),
        };
      });
      return { ...current, days, backlog_buckets: backlogBuckets };
    });
  }

  function markInstantPlacement(taskId: string) {
    setInstantPlacementTaskIds((current) => {
      if (current.has(taskId)) return current;
      const next = new Set(current);
      next.add(taskId);
      return next;
    });
  }

  function tasksForDestination(destination: MoveDestination) {
    return allTasks(payload)
      .filter((task) =>
        destination.type === "day"
          ? task.scheduled_date === destination.date
          : task.backlog_bucket_id === destination.bucketId,
      )
      .sort((a, b) => a.order_index - b.order_index);
  }

  function targetIndexForTask(targetTask: PersonalBoardTask, movingTaskId: string) {
    const destination = targetTask.scheduled_date
      ? ({ type: "day", date: targetTask.scheduled_date } as const)
      : ({ type: "backlog", bucketId: targetTask.backlog_bucket_id ?? "" } as const);
    const fullTasks = tasksForDestination(destination);
    const movingIndex = fullTasks.findIndex((task) => task.task_id === movingTaskId);
    const targetIndexFull = fullTasks.findIndex((task) => task.task_id === targetTask.task_id);
    const destinationTasks = fullTasks.filter((task) => task.task_id !== movingTaskId);
    const index = destinationTasks.findIndex((task) => task.task_id === targetTask.task_id);
    if (index < 0) return undefined;
    // Dragging a task DOWN onto a lower task drops it below that task; dragging
    // up (or in from another column) drops it above. Without this, dropping on
    // a task always inserts before it, so a task can never move past the one
    // directly below it.
    const movingDown = movingIndex > -1 && movingIndex < targetIndexFull;
    return movingDown ? index + 1 : index;
  }

  async function submitDayTask(event: FormEvent, dateKey: string) {
    event.preventDefault();
    const title = taskDrafts[dateKey]?.trim();
    if (!title) return;
    const orderIndex = payload?.days.find((day) => day.date === dateKey)?.tasks.length ?? 0;
    const clientId = makeClientId();
    const optimisticTask: PersonalBoardTask = {
      task_id: clientId,
      title,
      status: "open",
      completed: false,
      priority: "normal",
      scheduled_date: dateKey,
      scheduled_time: null,
      scheduled_end_time: null,
      no_return: false,
      due_date: null,
      due_time: null,
      backlog_bucket_id: null,
      order_index: orderIndex,
      source_kind: "manual",
      source_label: "saving",
      project: null,
      subtasks: [],
    };
    addOptimisticTask(optimisticTask);
    setTaskDrafts((current) => ({ ...current, [dateKey]: "" }));
    try {
      const task = await createTaskRemote(
        { title, scheduled_date: dateKey, priority: "normal", task_id: clientId },
        optimisticTask,
      );
      removeTaskInState(optimisticTask.task_id);
      upsertTaskInState(task);
      flash("Task added");
    } catch (e) {
      removeTaskInState(optimisticTask.task_id);
      setError(e instanceof Error ? e.message : "할 일 저장 실패");
      void refresh(selectedDate);
    }
  }

  async function submitBacklogTask(event: FormEvent, bucketId: string) {
    event.preventDefault();
    const title = bucketDrafts[bucketId]?.trim();
    if (!title) return;
    const orderIndex = payload?.backlog_buckets.find((bucket) => bucket.bucket_id === bucketId)?.tasks.length ?? 0;
    const clientId = makeClientId();
    const optimisticTask: PersonalBoardTask = {
      task_id: clientId,
      title,
      status: "open",
      completed: false,
      priority: "normal",
      scheduled_date: null,
      scheduled_time: null,
      scheduled_end_time: null,
      no_return: false,
      due_date: null,
      due_time: null,
      backlog_bucket_id: bucketId,
      order_index: orderIndex,
      source_kind: "manual",
      source_label: "saving",
      project: null,
      subtasks: [],
    };
    addOptimisticTask(optimisticTask);
    setBucketDrafts((current) => ({ ...current, [bucketId]: "" }));
    setActiveBucketId(null);
    try {
      const task = await createTaskRemote(
        { title, backlog_bucket_id: bucketId, priority: "normal", task_id: clientId },
        optimisticTask,
      );
      removeTaskInState(optimisticTask.task_id);
      upsertTaskInState(task);
      flash("Backlog task added");
    } catch (e) {
      removeTaskInState(optimisticTask.task_id);
      setError(e instanceof Error ? e.message : "백로그 저장 실패");
      void refresh(selectedDate);
    }
  }

  async function patchTask(task: PersonalBoardTask, patch: PersonalTaskPatchInput, failMessage = "할 일 수정 실패") {
    if (guardTempTask(task.task_id)) return;
    const optimistic: Partial<PersonalBoardTask> = {};
    if (patch.title !== undefined) optimistic.title = patch.title;
    if (patch.status !== undefined) {
      optimistic.status = patch.status;
      optimistic.completed = patch.status === "done" ? true : patch.status === "open" ? false : task.completed;
    }
    if (patch.priority !== undefined) optimistic.priority = patch.priority;
    if (patch.project_id !== undefined) {
      optimistic.project = patch.project_id ? payload?.projects.find((project) => project.project_id === patch.project_id) ?? null : null;
    }
    if (patch.scheduled_date !== undefined) optimistic.scheduled_date = patch.scheduled_date;
    if (patch.scheduled_time !== undefined) optimistic.scheduled_time = patch.scheduled_time;
    if (patch.scheduled_end_time !== undefined) optimistic.scheduled_end_time = patch.scheduled_end_time;
    if (patch.due_date !== undefined) optimistic.due_date = patch.due_date;
    if (patch.due_time !== undefined) optimistic.due_time = patch.due_time;
    if (patch.backlog_bucket_id !== undefined) optimistic.backlog_bucket_id = patch.backlog_bucket_id;
    if (patch.order_index != null) optimistic.order_index = patch.order_index;
    if (patch.note !== undefined) optimistic.note = patch.note;
    setTaskInState(task.task_id, optimistic);
    try {
      const updated = await updateTaskRemote(task.task_id, patch, { ...task, ...optimistic });
      upsertTaskInState(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : failMessage);
      void refresh(selectedDate);
    }
  }

  // 티켓 노트: 입력은 draft 오버레이에 두고 디바운스로 서버 저장한다(오프라인이면
  // updateTaskRemote가 큐잉). 팝업 닫힘/페이지 숨김 시 즉시 flush해 유실을 막는다.
  function changeTaskNote(task: PersonalBoardTask, value: string) {
    setTaskNoteDrafts((current) => ({ ...current, [task.task_id]: value }));
    pendingNoteSaveRef.current = { task, value };
    if (noteSaveTimerRef.current) window.clearTimeout(noteSaveTimerRef.current);
    noteSaveTimerRef.current = window.setTimeout(() => void flushNoteSave(), 800);
  }

  async function flushNoteSave(): Promise<boolean> {
    if (noteSaveTimerRef.current) {
      window.clearTimeout(noteSaveTimerRef.current);
      noteSaveTimerRef.current = null;
    }
    // pending은 "저장 성공을 확인한 뒤에만" 지운다. 미리 지우면 내비게이션에
    // fetch가 취소될 때 keepalive(숨김/언로드) 경로가 보낼 것이 없어져 유실된다.
    const pending = pendingNoteSaveRef.current;
    if (!pending) return true;
    const { task, value } = pending;
    if ((task.note ?? "") === value) {
      if (pendingNoteSaveRef.current === pending) pendingNoteSaveRef.current = null;
      return true;
    }
    const optimistic = { ...task, note: value };
    setTaskInState(task.task_id, optimistic);
    try {
      const updated = await updateTaskRemote(task.task_id, { note: value }, optimistic);
      upsertTaskInState(updated);
      if (pendingNoteSaveRef.current === pending) pendingNoteSaveRef.current = null;
      return true;
    } catch (e) {
      // 실패하면 pending을 남겨 다음 flush(디바운스/blur/닫기/숨김)가 재시도한다.
      // patchTask처럼 refresh()로 서버 옛값을 다시 당겨 입력을 덮지 않는다 —
      // draft 오버레이가 화면 입력을 지킨다.
      setError(e instanceof Error ? e.message : "노트 저장 실패");
      return false;
    }
  }

  // 탭 숨김/언로드 시의 flush: 일반 fetch는 페이지 이탈과 함께 취소돼 마지막
  // 입력이 유실될 수 있어 keepalive fetch로 직접 보낸다(64KB 한도 — note 캡 20K로 충분).
  function flushNoteSaveOnHide() {
    if (noteSaveTimerRef.current) {
      window.clearTimeout(noteSaveTimerRef.current);
      noteSaveTimerRef.current = null;
    }
    const pending = pendingNoteSaveRef.current;
    if (!pending) return;
    const { task, value } = pending;
    if ((task.note ?? "") === value || isTempTaskId(task.task_id)) return;
    // 창 전환(blur)처럼 페이지가 살아남는 경우를 위해 로컬 상태도 맞춰 둔다.
    setTaskInState(task.task_id, { ...task, note: value });
    void fetch(`${API_BASE}/personal-board/tasks/${task.task_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...demoAuthHeaders() },
      credentials: "include",
      keepalive: true,
      body: JSON.stringify({ note: value }),
    })
      .then((res) => {
        // 성공 확인 후에만 pending 해제 — 실패/취소면 남겨서 재시도되게 한다.
        if (res.ok && pendingNoteSaveRef.current === pending) {
          pendingNoteSaveRef.current = null;
        }
      })
      .catch(() => {
        /* 언로드 중 best-effort */
      });
  }

  // 최신 클로저의 flush를 ref로 노출 — 탭 숨김/언로드 리스너(1회 등록)가 stale
  // 클로저를 잡지 않게 한다. blur/숨김/언로드 전부 keepalive 변형을 쓴다
  // (일반 fetch는 내비게이션에 취소됨). 상태 정합은 flushNoteSaveOnHide가 맞춘다.
  flushNoteSaveRef.current = flushNoteSaveOnHide;

  async function addTaskComment(task: PersonalBoardTask, body: string) {
    if (guardTempTask(task.task_id)) return;
    try {
      const created = await api.addPersonalTaskComment(task.task_id, body);
      setTaskComments((current) => ({
        ...current,
        [task.task_id]: [...(current[task.task_id] ?? []), created],
      }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "댓글 저장 실패");
    }
  }

  async function toggleTask(task: PersonalBoardTask) {
    if (guardTempTask(task.task_id)) return;
    const status = task.status === "done" ? "open" : "done";
    // 완료/재오픈 시 카드가 아래로 가라앉거나 제자리로 올라오는 것을 layout 애니메이션으로
    // 보여준다(스킵하지 않는다). 스킵하면 순간 점프해 어디로 갔는지 알기 어렵다.
    await patchTask(task, { status }, status === "done" ? "완료 처리 실패" : "다시 열기 실패");
    flash(status === "done" ? "Task completed" : "Task reopened");
    // 본 태스크를 완료하면 미완료 서브태스크도 자동으로 완료 처리한다.
    if (status === "done") {
      const pending = task.subtasks.filter((s) => !s.completed);
      if (pending.length > 0) {
        updateTaskInState(task.task_id, (current) => ({
          ...current,
          subtasks: current.subtasks.map((s) => (s.completed ? s : { ...s, completed: true })),
        }));
        try {
          await Promise.all(
            pending.map((s) => api.updatePersonalSubtask(s.subtask_id, { completed: true })),
          );
        } catch {
          void refresh(selectedDate);
        }
      }
    }
  }

  async function archiveTask(task: PersonalBoardTask) {
    if (guardTempTask(task.task_id)) return;
    setTaskInState(task.task_id, { status: "archived" });
    if (editingTaskId === task.task_id) setEditingTaskId(null);
    try {
      const updated = await updateTaskRemote(
        task.task_id,
        { status: "archived" },
        { ...task, status: "archived" },
      );
      upsertTaskInState(updated);
      flash("Task archived");
    } catch (e) {
      setError(e instanceof Error ? e.message : "할 일 보관 실패");
      void refresh(selectedDate);
    }
  }

  // ---- Context-menu / more-button actions (Phase 1A) ----
  function someTopOfHour() {
    const next = new Date();
    next.setMinutes(0, 0, 0);
    next.setHours(next.getHours() + 1);
    return `${pad(next.getHours())}:${pad(next.getMinutes())}`;
  }

  async function addTaskToCalendar(task: PersonalBoardTask) {
    setTaskMenu(null);
    if (guardTempTask(task.task_id)) return;
    if (task.scheduled_date) {
      if (task.scheduled_time) return; // already has a time
      await patchTask(task, { scheduled_time: someTopOfHour() }, "시간 지정 실패");
      flash("Added to calendar");
      return;
    }
    // Backlog task: move it onto the selected/today date with a default time.
    const targetDate = visibleDays.some((day) => day.date === selectedDate) ? selectedDate : realToday;
    setTaskInState(task.task_id, {
      scheduled_date: targetDate,
      scheduled_time: someTopOfHour(),
      backlog_bucket_id: null,
    });
    try {
      const calendarPatch = {
        scheduled_date: targetDate,
        scheduled_time: someTopOfHour(),
        backlog_bucket_id: null,
      };
      const updated = await updateTaskRemote(task.task_id, calendarPatch, {
        ...task,
        ...calendarPatch,
      });
      upsertTaskInState(updated);
      flash("Added to calendar");
    } catch (e) {
      setError(e instanceof Error ? e.message : "캘린더 추가 실패");
      void refresh(selectedDate);
    }
  }

  async function moveTaskToBacklog(task: PersonalBoardTask) {
    setTaskMenu(null);
    if (guardTempTask(task.task_id)) return;
    const buckets = payload?.backlog_buckets ?? [];
    const bucket = buckets.find((item) => item.key === "someday") ?? buckets[0];
    if (!bucket) return;
    // Drag-to-backlog와 동일한 경로(moveTaskTo)로 통일한다. 과거 구현은 optimistic
    // 갱신에 in-place 패치(setTaskInState)를 써서 카드가 day 컬럼에 그대로 남아
    // 네트워크 응답 전까지 "안 옮겨진 것처럼" 보였고, 캘린더 재동기화 등과 겹치면
    // 두 번 눌러야 가는 것처럼 느껴졌다. moveTaskTo는 moveTaskInState로 즉시 재배치한다.
    await moveTaskTo(task, { type: "backlog", bucketId: bucket.bucket_id });
  }

  function toggleHiddenSubtasks(taskId: string) {
    setTaskMenu(null);
    setHiddenSubtasks((current) => {
      const next = new Set(current);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  }

  async function duplicateTask(task: PersonalBoardTask) {
    setTaskMenu(null);
    if (guardTempTask(task.task_id)) return;
    try {
      const created = await api.duplicatePersonalTask(task.task_id);
      upsertTaskInState(created);
      flash("Task duplicated");
    } catch (e) {
      setError(e instanceof Error ? e.message : "복제 실패");
      void refresh(selectedDate);
    }
  }

  async function deleteTask(task: PersonalBoardTask) {
    setTaskMenu(null);
    if (guardTempTask(task.task_id)) return;
    if (editingTaskId === task.task_id) setEditingTaskId(null);
    removeTaskInState(task.task_id);
    try {
      await deleteTaskRemote(task.task_id);
      flash("Task deleted");
    } catch (e) {
      setError(e instanceof Error ? e.message : "삭제 실패");
      void refresh(selectedDate);
    }
  }

  // 저장 중(temp id) 태스크의 상세 모달은 열지 않는다. 열면 모달 편집이 temp id로
  // 저장 API를 호출해 422가 난다.
  function openTaskDetail(taskId: string) {
    if (guardTempTask(taskId)) return;
    setEditingTaskId(taskId);
  }

  // /notifications 딥링크(?task=<id>): payload에 해당 태스크가 실제 존재하면 한 번만
  // 상세 모달을 연다. 캐시 페인트에는 없다가 네트워크 payload에 나타날 수 있어
  // 못 찾은 동안은 pending을 유지하고(끝내 없으면 — archived/unknown — 조용히 무시),
  // 찾은 순간 pending을 비워 이후 payload 갱신에 다시 열리지 않게 한다.
  useEffect(() => {
    const taskId = pendingDeepLinkTaskIdRef.current;
    if (!taskId || !payload) return;
    if (!allTasks(payload).some((task) => task.task_id === taskId)) return;
    pendingDeepLinkTaskIdRef.current = null;
    openTaskDetail(taskId);
    // openTaskDetail은 렌더마다 재생성되는 로컬 함수지만 동작은 안정적이다 —
    // payload 변화에만 반응해야 하므로 deps에서 뺀다(파일 내 기존 관례와 동일).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload]);

  function openTaskMenu(task: PersonalBoardTask, x: number, y: number) {
    if (guardTempTask(task.task_id)) return;
    setTaskMenu({ task, x, y });
  }

  async function changeTaskTitle(task: PersonalBoardTask, title: string) {
    const clean = title.trim();
    if (!clean || clean === task.title) return;
    await patchTask(task, { title: clean }, "제목 수정 실패");
  }

  async function changeTaskPriority(task: PersonalBoardTask, priority: Priority) {
    await patchTask(task, { priority }, "우선순위 수정 실패");
  }

  async function changeTaskProject(
    task: PersonalBoardTask,
    projectId: string | null,
    projectOverride?: PersonalBoardProject | null,
  ) {
    if (guardTempTask(task.task_id)) return;
    const project = projectId ? projectOverride ?? payload?.projects.find((item) => item.project_id === projectId) ?? null : null;
    setTaskInState(task.task_id, { project });
    try {
      const updated = await updateTaskRemote(
        task.task_id,
        { project_id: projectId },
        { ...task, project },
      );
      upsertTaskInState(updated);
      flash(projectId ? "Channel assigned" : "Channel cleared");
    } catch (e) {
      setError(e instanceof Error ? e.message : "채널 수정 실패");
      void refresh(selectedDate);
    }
  }

  // 시작+종료 시간을 함께 설정한다(스케줄 패널 드래그/리사이즈, 상세 팝업 입력에서 사용).
  // 시작이 없으면 종료도 없고, 종료가 시작보다 빠르거나 같으면 종료를 비운다(서버에도 가드 존재).
  async function changeTaskSchedule(
    task: PersonalBoardTask,
    scheduledTime: string | null,
    scheduledEndTime: string | null,
  ) {
    const start = scheduledTime || null;
    let end = start ? scheduledEndTime || null : null;
    // 입력은 "HH:MM"(5자), 서버 값은 "HH:MM:SS"(8자)라 폭이 다르면 사전식 비교가 어긋난다
    // (예: "10:00:00" <= "10:00"이 false). 분 단위로 정규화해 비교한다.
    if (start && end && end.slice(0, 5) <= start.slice(0, 5)) end = null;
    await patchTask(task, { scheduled_time: start, scheduled_end_time: end }, "시간 수정 실패");
  }

  // 스케줄 패널: 분 단위 시작/종료(이동·리사이즈·드롭 공통)를 저장한다. 패널은 선택일(selectedDate)을
  // 보여주므로, 다른 날/백로그의 할 일을 끌어오면 선택일로 옮기면서 시간을 배정한다.
  async function setTaskScheduleMinutes(task: PersonalBoardTask, startMin: number, endMin: number) {
    const start = minToClock(startMin);
    const end = endMin > startMin ? minToClock(endMin) : null;
    const patch: PersonalTaskPatchInput = { scheduled_time: start, scheduled_end_time: end };
    if (task.scheduled_date !== selectedDate) {
      patch.scheduled_date = selectedDate;
      patch.backlog_bucket_id = null;
    }
    await patchTask(task, patch, "시간 배정 실패");
  }

  // 보드 카드를 타임라인에 드롭했을 때: 시작 분 + (기존 길이 유지, 없으면 기본 60분) 종료를 배정.
  function assignTaskFromDrop(task: PersonalBoardTask, startMin: number) {
    const prevStart = parseClockToMin(task.scheduled_time);
    const prevEnd = parseClockToMin(task.scheduled_end_time);
    const duration =
      prevStart != null && prevEnd != null && prevEnd > prevStart
        ? prevEnd - prevStart
        : SCHEDULE_DEFAULT_DURATION_MIN;
    const gridEnd = SCHEDULE_END_HOUR * 60;
    // 그리드 맨 아래에 드롭해도 길이만큼 위로 당겨 시작을 잡는다(23:59 zero-length·그리드 넘침 방지).
    const start = Math.min(startMin, gridEnd - duration);
    void setTaskScheduleMinutes(task, start, start + duration);
  }

  async function changeTaskDue(task: PersonalBoardTask, dueDate: string | null, dueTime: string | null) {
    // Clearing the date clears the time; a time alone is invalid (server-guarded too).
    const nextTime = dueDate ? dueTime : null;
    await patchTask(task, { due_date: dueDate, due_time: nextTime }, "마감일 수정 실패");
  }

  async function createProjectChannel(name: string) {
    const clean = name.trim();
    if (!clean) return null;
    try {
      const project = await api.createPersonalProject({
        name: clean,
        color: channelPalette[clean.length % channelPalette.length],
      });
      upsertProjectInState(project);
      flash(`#${project.name} channel added`);
      return project;
    } catch (e) {
      setError(e instanceof Error ? e.message : "채널 생성 실패");
      return null;
    }
  }

  async function updateProjectChannel(
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) {
    const body = {
      ...patch,
      ...(patch.name !== undefined ? { name: patch.name.trim() } : {}),
    };
    if (body.name !== undefined && !body.name) return null;
    try {
      const project = await api.updatePersonalProject(projectId, body);
      upsertProjectInState(project);
      flash(`#${project.name} channel updated`);
      return project;
    } catch (e) {
      setError(e instanceof Error ? e.message : "채널 수정 실패");
      return null;
    }
  }

  async function deleteProjectChannel(projectId: string) {
    const target = (payload?.projects ?? []).find((item) => item.project_id === projectId);
    try {
      await api.deletePersonalProject(projectId);
      removeProjectFromState(projectId);
      flash(target ? `#${target.name} channel deleted` : "channel deleted");
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "채널 삭제 실패");
      return false;
    }
  }

  async function toggleBacklogBucket(bucket: PersonalBoardBacklogBucket) {
    const collapsed = !bucket.collapsed;
    patchBacklogBucketInState(bucket.bucket_id, { collapsed });
    try {
      const updated = await api.updatePersonalBacklogBucket(bucket.bucket_id, { collapsed });
      patchBacklogBucketInState(bucket.bucket_id, {
        badge: updated.badge,
        bucket_id: updated.bucket_id,
        collapsed: updated.collapsed,
        color: updated.color,
        key: updated.key,
        label: updated.label,
        order_index: updated.order_index,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "백로그 접기 실패");
      void refresh(selectedDate);
    }
  }

  async function moveTaskTo(
    task: PersonalBoardTask,
    destination: MoveDestination,
    orderIndex?: number,
    options: { optimistic?: boolean } = {},
  ) {
    if (guardTempTask(task.task_id)) return;
    if (destination.type === "day" && task.scheduled_date === destination.date && orderIndex == null) return;
    if (destination.type === "backlog" && task.backlog_bucket_id === destination.bucketId && orderIndex == null) return;

    if (options.optimistic !== false) {
      moveTaskInState(task, destination, orderIndex);
    }
    try {
      const nextScheduledDate = destination.type === "day" ? destination.date : null;
      const nextScheduledTime = destination.type === "day" ? task.scheduled_time : null;
      const nextBacklogBucketId = destination.type === "backlog" ? destination.bucketId : null;
      const body: PersonalTaskPatchInput = {
        scheduled_date: nextScheduledDate,
        scheduled_time: nextScheduledTime,
        backlog_bucket_id: nextBacklogBucketId,
      };
      if (orderIndex != null) body.order_index = orderIndex;
      const optimisticMoved: PersonalBoardTask = {
        ...task,
        scheduled_date: nextScheduledDate,
        scheduled_time: nextScheduledTime,
        backlog_bucket_id: nextBacklogBucketId,
        ...(orderIndex != null ? { order_index: orderIndex } : {}),
      };
      const updated = await updateTaskRemote(task.task_id, { ...body }, optimisticMoved);
      upsertTaskInState(updated);
      flash(destination.type === "day" ? `Moved to ${monthDayLabel(destination.date)}` : "Moved to backlog");
    } catch (e) {
      setError(e instanceof Error ? e.message : "이동 실패");
      void refresh(selectedDate);
    }
  }

  async function toggleSubtask(task: PersonalBoardTask, subtaskId: string, completed: boolean) {
    skipNextLayoutAnimationRef.current = true;
    updateTaskInState(task.task_id, (current) => ({
      ...current,
      subtasks: current.subtasks.map((subtask) =>
        subtask.subtask_id === subtaskId ? { ...subtask, completed } : subtask,
      ),
    }));
    try {
      const updated = await api.updatePersonalSubtask(subtaskId, { completed });
      updateTaskInState(task.task_id, (current) => ({
        ...current,
        subtasks: current.subtasks.map((subtask) => (subtask.subtask_id === subtaskId ? updated : subtask)),
      }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "하위 할 일 수정 실패");
      void refresh(selectedDate);
    }
  }

  async function addSubtask(task: PersonalBoardTask, title: string) {
    const clean = title.trim();
    if (!clean) return;
    if (guardTempTask(task.task_id)) return;
    const tempId = `temp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const optimistic: PersonalBoardSubtask = {
      subtask_id: tempId,
      task_id: task.task_id,
      title: clean,
      completed: false,
      order_index: task.subtasks.length,
    };
    updateTaskInState(task.task_id, (current) => ({
      ...current,
      subtasks: [...current.subtasks, optimistic],
    }));
    try {
      const subtask = await api.createPersonalSubtask(task.task_id, { title: clean });
      updateTaskInState(task.task_id, (current) => ({
        ...current,
        subtasks: current.subtasks.map((s) => (s.subtask_id === tempId ? subtask : s)),
      }));
    } catch (e) {
      updateTaskInState(task.task_id, (current) => ({
        ...current,
        subtasks: current.subtasks.filter((s) => s.subtask_id !== tempId),
      }));
      setError(e instanceof Error ? e.message : "하위 할 일 저장 실패");
    }
  }

  async function renameSubtask(task: PersonalBoardTask, subtaskId: string, title: string) {
    const clean = title.trim();
    // 내용을 모두 지우고 확정하면 그 서브태스크를 삭제한다(예전엔 빈 값이면 무시돼
    // 원래 제목으로 되돌아가는 회귀가 있었다).
    if (!clean) {
      void deleteSubtask(task, subtaskId);
      return;
    }
    const previous = task.subtasks.find((s) => s.subtask_id === subtaskId);
    if (previous && previous.title === clean) return;
    skipNextLayoutAnimationRef.current = true;
    updateTaskInState(task.task_id, (current) => ({
      ...current,
      subtasks: current.subtasks.map((subtask) =>
        subtask.subtask_id === subtaskId ? { ...subtask, title: clean } : subtask,
      ),
    }));
    try {
      const updated = await api.updatePersonalSubtask(subtaskId, { title: clean });
      updateTaskInState(task.task_id, (current) => ({
        ...current,
        subtasks: current.subtasks.map((subtask) => (subtask.subtask_id === subtaskId ? updated : subtask)),
      }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "하위 할 일 수정 실패");
      void refresh(selectedDate);
    }
  }

  async function deleteSubtask(task: PersonalBoardTask, subtaskId: string) {
    skipNextLayoutAnimationRef.current = true;
    const snapshot = task.subtasks;
    updateTaskInState(task.task_id, (current) => ({
      ...current,
      subtasks: current.subtasks.filter((subtask) => subtask.subtask_id !== subtaskId),
    }));
    // 아직 서버에 저장 전인(temp id) 서브태스크는 DELETE를 보내면 404("subtask not
    // found")가 나므로 로컬에서만 제거한다.
    if (isTempTaskId(subtaskId)) return;
    try {
      await api.deletePersonalSubtask(subtaskId);
    } catch (e) {
      // 삭제는 멱등이다 — 이미 없는(404) 서브태스크는 목표 상태(삭제됨)에 도달한
      // 것이므로 성공으로 처리하고 롤백/에러를 띄우지 않는다.
      if (e instanceof ApiError && e.status === 404) return;
      setError(e instanceof Error ? e.message : "하위 할 일 삭제 실패");
      updateTaskInState(task.task_id, (current) => ({ ...current, subtasks: snapshot }));
    }
  }

  function handleDragStart(event: DragStartEvent) {
    isDraggingRef.current = true;
    const rawId = String(event.active.id);
    if (rawId.startsWith("objective:")) {
      // `objective:{objectiveId}:{date}` — objectiveId is a colon-free UUID and
      // date is colon-free YYYY-MM-DD, so a plain split is unambiguous.
      const [, objectiveId, date] = rawId.split(":");
      setActiveObjectiveDrag({ objectiveId, date });
      setActiveOverId(null);
      return;
    }
    setActiveTaskId(rawId);
    setActiveOverId(null);
  }

  function handleDragOver(event: DragOverEvent) {
    setActiveOverId(event.over?.id ? String(event.over.id) : null);
  }

  async function handleDragEnd(event: DragEndEvent) {
    const activeId = String(event.active.id);
    if (activeId.startsWith("objective:")) {
      isDraggingRef.current = false;
      const [, objectiveId, sourceDate] = activeId.split(":");
      const overId = event.over?.id ? String(event.over.id) : null;
      const clearObjectiveDrag = () => {
        skipNextLayoutAnimationRef.current = true;
        setActiveObjectiveDrag(null);
        setActiveOverId(null);
      };

      // Resolve which day the drop landed on; objectives reorder within their own day only.
      let targetDay: string | null = null;
      if (overId?.startsWith("day:")) {
        targetDay = overId.replace("day:", "");
      } else if (overId?.startsWith("objective:")) {
        targetDay = overId.split(":")[2] ?? null;
      } else if (overId?.startsWith("task:")) {
        const overTask = allTasks(payload).find((item) => item.task_id === overId.replace("task:", ""));
        targetDay = overTask?.scheduled_date ?? null;
      }
      if (targetDay !== sourceDate) {
        const objectiveForMove = Object.values(objectivesByWeek)
          .flat()
          .find((o) => o.objective_id === objectiveId);
        // Same week, different day: move this day's instance to the target day,
        // merging its time + subtasks/comments into whatever the target already
        // has. Cross-week or unknown drops just revert.
        if (targetDay && objectiveForMove && sundayOf(targetDay) === objectiveForMove.week_start) {
          const fromWeekday = weekdayIndex(sourceDate);
          const toWeekday = weekdayIndex(targetDay);
          clearObjectiveDrag();
          try {
            const updated = await api.moveObjectiveDay(objectiveId, {
              from_weekday: fromWeekday,
              to_weekday: toWeekday,
            });
            upsertObjectiveInState(updated);
          } catch (e) {
            setError(e instanceof Error ? e.message : "목표 이동 실패");
            if (weeklyPlanWeekStart) void loadWeeklyPlan(weeklyPlanWeekStart);
          }
          return;
        }
        clearObjectiveDrag();
        return;
      }

      const objective = Object.values(objectivesByWeek)
        .flat()
        .find((o) => o.objective_id === objectiveId);
      if (!objective) {
        clearObjectiveDrag();
        return;
      }
      const sourceWeekday = weekdayIndex(sourceDate);
      const draggedCurrentPos = allocationForWeekday(objective.day_allocations, sourceWeekday)?.order ?? -0.5;

      // Build sourceDate's ordered item positions EXCLUDING the dragged objective.
      const dayTasks = payload?.days.find((d) => d.date === sourceDate)?.tasks ?? [];
      const otherObjectives = (objectivesByWeek[sundayOf(sourceDate)] ?? [])
        .filter((o) => o.objective_id !== objectiveId)
        .map((o) => {
          const a = allocationForWeekday(o.day_allocations, sourceWeekday);
          return a ? { pos: a.order ?? -0.5 } : null;
        })
        .filter((x): x is { pos: number } => x !== null);
      const list = [
        ...dayTasks.map((t) => ({ pos: t.order_index })),
        ...otherObjectives,
      ].sort((a, b) => a.pos - b.pos);

      // Determine insertion index relative to the over target.
      let index = list.length;
      if (overId && !overId.startsWith("day:")) {
        let targetPos: number | null = null;
        if (overId.startsWith("task:")) {
          const overTask = allTasks(payload).find((item) => item.task_id === overId.replace("task:", ""));
          targetPos = overTask?.order_index ?? null;
        } else if (overId.startsWith("objective:")) {
          const overObjectiveId = overId.split(":")[1];
          const overObjective = (objectivesByWeek[sundayOf(sourceDate)] ?? []).find(
            (o) => o.objective_id === overObjectiveId,
          );
          targetPos = overObjective
            ? allocationForWeekday(overObjective.day_allocations, sourceWeekday)?.order ?? -0.5
            : null;
        }
        if (targetPos != null) {
          const ti = list.findIndex((item) => item.pos === targetPos);
          if (ti < 0) {
            index = list.length;
          } else {
            index = draggedCurrentPos < targetPos ? ti + 1 : ti;
          }
        }
      }

      // Fractional midpoint between the neighbours at the insertion point.
      let newOrder: number;
      if (list.length === 0) {
        newOrder = 0;
      } else {
        const prevPos = list[index - 1]?.pos ?? (list[0]?.pos ?? 0) - 1;
        const nextPos = list[index]?.pos ?? (list[list.length - 1]?.pos ?? 0) + 1;
        newOrder = (prevPos + nextPos) / 2;
      }

      const next = objective.day_allocations.map((a) =>
        a.weekday === sourceWeekday ? { ...a, order: newOrder } : a,
      );
      void patchObjective(objective, { day_allocations: next }, { day_allocations: next });
      clearObjectiveDrag();
      return;
    }

    isDraggingRef.current = false;
    const taskId = activeId;
    const overId = event.over?.id ? String(event.over.id) : null;
    if (!overId || !payload) {
      skipNextLayoutAnimationRef.current = true;
      setActiveTaskId(null);
      setActiveOverId(null);
      return;
    }
    const task = allTasks(payload).find((item) => item.task_id === taskId);
    if (!task || overId === `task:${taskId}` || isTempTaskId(taskId)) {
      // 저장 중(temp id)인 카드는 드래그 이동을 무시한다. 로컬에서 옮겨봤자 저장 응답이
      // 오면 원래 생성일로 되돌아가고, moveTaskTo는 temp id를 거부한다.
      skipNextLayoutAnimationRef.current = true;
      if (isTempTaskId(taskId)) flash("저장 중입니다. 잠시 후 다시 시도하세요.");
      setActiveTaskId(null);
      setActiveOverId(null);
      return;
    }

    // 스케줄 패널에 드롭: 포인터 Y를 그리드 기준 시간으로 변환해 선택일에 시간 배정.
    if (overId === "schedule") {
      const grid = scheduleGridRef.current;
      if (grid) {
        const rect = grid.getBoundingClientRect();
        const activator = event.activatorEvent as PointerEvent | MouseEvent | null;
        const pointerY = (activator && "clientY" in activator ? activator.clientY : rect.top) + event.delta.y;
        assignTaskFromDrop(task, offsetToScheduleMin(pointerY - rect.top));
      }
      skipNextLayoutAnimationRef.current = true;
      setActiveTaskId(null);
      setActiveOverId(null);
      return;
    }

    let destination: MoveDestination | null = null;
    let orderIndex: number | undefined;
    if (overId.startsWith("day:")) {
      destination = { type: "day", date: overId.replace("day:", "") };
    } else if (overId.startsWith("backlog:")) {
      destination = { type: "backlog", bucketId: overId.replace("backlog:", "") };
    } else if (overId.startsWith("task:")) {
      const targetTask = allTasks(payload).find((item) => item.task_id === overId.replace("task:", ""));
      if (!targetTask) {
        skipNextLayoutAnimationRef.current = true;
        setActiveTaskId(null);
        setActiveOverId(null);
        return;
      }
      if (targetTask.scheduled_date) destination = { type: "day", date: targetTask.scheduled_date };
      if (targetTask.backlog_bucket_id) destination = { type: "backlog", bucketId: targetTask.backlog_bucket_id };
      orderIndex = targetIndexForTask(targetTask, taskId);
    }
    if (!destination) {
      skipNextLayoutAnimationRef.current = true;
      setActiveTaskId(null);
      setActiveOverId(null);
      return;
    }
    // 시간순/우선순위 정렬에서는 컬럼이 시간/우선순위로 재정렬되므로 카드 위 수동 reorder를
    // 그대로 두면 시각 순서와 order_index 좌표계가 어긋난다. 그래서 같은 컬럼 안에서 카드를
    // 다른 카드 위로 끌어 놓으면(명시적 재정렬) 수동 정렬로 전환해 그 순서를 반영하고, 빈 영역
    // 드롭은 no-op으로 둔다. 다른 날/백로그로 옮길 때는 order_index 계산 없이 append한다.
    if (sortMode !== "manual") {
      const sameContainer =
        (destination.type === "day" && task.scheduled_date === destination.date) ||
        (destination.type === "backlog" && task.backlog_bucket_id === destination.bucketId);
      if (sameContainer) {
        // 시간순/우선순위 정렬 중 같은 컬럼 안에서 카드를 다른 카드 위로 끌어 놓는 것은
        // "이 순서로 두고 싶다"는 명시적 수동 재정렬이다. 예전엔 완전 no-op이라 "드래그해도
        // 순서가 안 바뀐다"고 느꼈다. 이제 수동 정렬로 전환하고(서버 update_task가 order_index를
        // 재배열) 아래 공통 이동 경로에서 그 위치로 옮긴다. 타깃 카드가 없는 빈 영역 드롭
        // (orderIndex == null)은 의미 있는 순서가 없으므로 종전대로 무시한다.
        if (orderIndex == null) {
          skipNextLayoutAnimationRef.current = true;
          setActiveTaskId(null);
          setActiveOverId(null);
          return;
        }
        setSortMode("manual");
        setFilterMenuOpen(false);
        flash("드래그 순서에 맞춰 수동 정렬로 전환했어요");
        void api.updatePersonalPreferences({ sort_mode: "manual" }).catch(() => {});
      } else {
        orderIndex = undefined;
      }
    }
    // A same-day drop onto the column's empty area (no target task) should send
    // the task to the bottom, so only treat a same-bucket backlog drop as a
    // no-op. moveTaskInState appends when orderIndex is null.
    const noOpMove =
      orderIndex == null &&
      destination.type === "backlog" &&
      task.backlog_bucket_id === destination.bucketId;
    if (noOpMove) {
      skipNextLayoutAnimationRef.current = true;
      setActiveTaskId(null);
      setActiveOverId(null);
      return;
    }
    skipNextLayoutAnimationRef.current = true;
    flushSync(() => {
      markInstantPlacement(task.task_id);
      moveTaskInState(task, destination, orderIndex);
      setActiveTaskId(null);
      setActiveOverId(null);
    });
    await moveTaskTo(task, destination, orderIndex, { optimistic: false });
  }

  function changeViewMode(mode: ViewMode) {
    setActiveSidebarItem(mode);
    setViewMode(mode);
    setMobileColumnId(`day:${selectedDate}`);
  }

  // 우측 패널(Backlog/Schedule/숨김)을 적용한다. 두 패널은 day board 위에 슬라이드인하며
  // 뷰를 대체하지 않는다. 모바일 단일 컬럼 선택도 함께 맞춘다.
  async function applyRightPanel(next: RightPanel) {
    setViewMode("home");
    setActiveSidebarItem("home");
    setRightPanel(next);
    setMobileColumnId(next === "backlog" ? "backlog" : `day:${selectedDate}`);
    flash(next === "backlog" ? "Backlog opened" : next === "schedule" ? "Schedule opened" : "Panel collapsed");
    try {
      await api.updatePersonalPreferences({ right_panel: next });
    } catch (e) {
      setError(e instanceof Error ? e.message : "패널 상태 저장 실패");
    }
  }

  async function toggleBacklogPanel() {
    await applyRightPanel(rightPanel === "backlog" ? "hidden" : "backlog");
  }

  async function toggleSchedulePanel() {
    await applyRightPanel(rightPanel === "schedule" ? "hidden" : "schedule");
  }

  async function changeFilterMode(mode: FilterMode) {
    setFilterMode(mode);
    setFilterMenuOpen(false);
    flash(`${filterLabels[mode]} filter applied`);
    try {
      await api.updatePersonalPreferences({ filter_mode: mode });
    } catch (e) {
      setError(e instanceof Error ? e.message : "필터 저장 실패");
    }
  }

  async function changeSortMode(mode: SortMode) {
    setSortMode(mode);
    setFilterMenuOpen(false);
    flash(`${sortLabels[mode]} selected`);
    try {
      await api.updatePersonalPreferences({ sort_mode: mode });
    } catch (e) {
      setError(e instanceof Error ? e.message : "정렬 저장 실패");
    }
  }

  async function changeRightPanel(panel: RightPanel) {
    await applyRightPanel(panel);
  }

  // ---- Weekly plan (Phase 2E) ----
  const loadWeeklyPlan = useCallback(async (weekStart: string) => {
    setWeeklyPlanLoading(true);
    try {
      const result = await api.getPersonalWeeklyPlan(weekStart);
      setWeeklyPlanWeekStart(result.week_start);
      setWeekObjectives(result.week_start, result.objectives);
      loadedObjectiveWeeksRef.current.add(result.week_start);
    } catch (e) {
      setError(e instanceof Error ? e.message : "주간 계획을 불러오지 못했습니다.");
    } finally {
      setWeeklyPlanLoading(false);
    }
  }, [setWeekObjectives]);

  useEffect(() => {
    if (viewMode !== "weekly_plan") return;
    // Data-fetch effect: loadWeeklyPlan sets the loading flag + week start from the
    // response. The leading setLoading is intentional for this view-change fetch.
    void loadWeeklyPlan(weeklyPlanWeekStart ?? sundayOf(realToday));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode, weeklyPlanWeekStart, loadWeeklyPlan]);

  // Home view: load each visible week's objectives once so day columns can show
  // weekly-plan allocations alongside tasks. Shares state with the weekly plan view.
  useEffect(() => {
    if (viewMode !== "home" || !payload) return;
    const weeks = new Set((payload.days ?? []).map((d) => sundayOf(d.date)));
    weeks.forEach((week) => {
      if (loadedObjectiveWeeksRef.current.has(week)) return;
      loadedObjectiveWeeksRef.current.add(week);
      api
        .getPersonalWeeklyPlan(week)
        .then((res) => setWeekObjectives(res.week_start, res.objectives))
        .catch(() => {
          loadedObjectiveWeeksRef.current.delete(week);
        });
    });
  }, [viewMode, payload, setWeekObjectives]);

  function upsertObjectiveInState(objective: PersonalWeeklyObjective) {
    setObjectivesByWeek((cur) => {
      const w = objective.week_start;
      const list = cur[w] ?? [];
      const exists = list.some((i) => i.objective_id === objective.objective_id);
      const next = exists
        ? list.map((i) => (i.objective_id === objective.objective_id ? objective : i))
        : [...list, objective];
      return { ...cur, [w]: next.sort((a, b) => a.order_index - b.order_index) };
    });
  }

  async function createObjective(title: string) {
    const clean = title.trim();
    if (!clean || !weeklyPlanWeekStart) return;
    try {
      const created = await api.createPersonalObjective({ week_start: weeklyPlanWeekStart, title: clean });
      upsertObjectiveInState(created);
      flash("Objective added");
    } catch (e) {
      setError(e instanceof Error ? e.message : "목표 추가 실패");
    }
  }

  async function patchObjective(
    objective: PersonalWeeklyObjective,
    patch: { title?: string; completed?: boolean; day_allocations?: DayAllocation[]; project_id?: string | null; note?: string | null },
    optimistic?: Partial<PersonalWeeklyObjective>,
  ) {
    if (optimistic) upsertObjectiveInState({ ...objective, ...optimistic });
    try {
      const updated = await api.updatePersonalObjective(objective.objective_id, patch);
      upsertObjectiveInState(updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : "목표 수정 실패");
      if (weeklyPlanWeekStart) void loadWeeklyPlan(weeklyPlanWeekStart);
    }
  }

  async function addObjectiveSubtask(objective: PersonalWeeklyObjective, weekday: number, title: string) {
    const clean = title.trim();
    if (!clean) return;
    try {
      const created = await api.createObjectiveSubtask(objective.objective_id, { title: clean, weekday });
      const subtasks = [...objective.subtasks, created];
      // 서버 auto-complete를 미러: 서브태스크가 있으면 완료 = 전부 완료.
      upsertObjectiveInState({
        ...objective,
        subtasks,
        completed: deriveObjectiveCompleted(subtasks, objective.completed),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "하위 항목 추가 실패");
    }
  }

  async function toggleObjectiveSubtask(objective: PersonalWeeklyObjective, subtaskId: string, completed: boolean) {
    const optimistic = objective.subtasks.map((s) => (s.subtask_id === subtaskId ? { ...s, completed } : s));
    upsertObjectiveInState({
      ...objective,
      subtasks: optimistic,
      completed: deriveObjectiveCompleted(optimistic, objective.completed),
    });
    try {
      const updated = await api.updateObjectiveSubtask(subtaskId, { completed });
      const next = objective.subtasks.map((s) => (s.subtask_id === subtaskId ? updated : s));
      upsertObjectiveInState({
        ...objective,
        subtasks: next,
        completed: deriveObjectiveCompleted(next, objective.completed),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "하위 항목 수정 실패");
    }
  }

  // Toggle an objective subtask straight from its day card (resolves the full
  // objective by id, then reuses the existing optimistic toggle).
  function toggleObjectiveCardSubtask(objectiveId: string, subtaskId: string, completed: boolean) {
    const objective = Object.values(objectivesByWeek)
      .flat()
      .find((o) => o.objective_id === objectiveId);
    if (!objective) return;
    void toggleObjectiveSubtask(objective, subtaskId, completed);
  }

  async function addObjectiveComment(objective: PersonalWeeklyObjective, weekday: number, body: string) {
    const clean = body.trim();
    if (!clean) return;
    try {
      const created = await api.createObjectiveComment(objective.objective_id, { body: clean, weekday });
      upsertObjectiveInState({ ...objective, comments: [...objective.comments, created] });
    } catch (e) {
      setError(e instanceof Error ? e.message : "댓글 추가 실패");
    }
  }

  // Per-day note lives in day_allocations[weekday].note (preserve minutes/order).
  function setObjectiveDayNote(objective: PersonalWeeklyObjective, weekday: number, note: string) {
    const hasEntry = objective.day_allocations.some((a) => a.weekday === weekday);
    const next = hasEntry
      ? objective.day_allocations.map((a) => (a.weekday === weekday ? { ...a, note } : a))
      : [...objective.day_allocations, { weekday, minutes: null, note }];
    void patchObjective(objective, { day_allocations: next }, { day_allocations: next });
  }

  async function deleteObjective(objective: PersonalWeeklyObjective) {
    setObjectivesByWeek((cur) => ({
      ...cur,
      [objective.week_start]: (cur[objective.week_start] ?? []).filter((i) => i.objective_id !== objective.objective_id),
    }));
    try {
      await api.deletePersonalObjective(objective.objective_id);
      flash("Objective removed");
    } catch (e) {
      setError(e instanceof Error ? e.message : "목표 삭제 실패");
      if (weeklyPlanWeekStart) void loadWeeklyPlan(weeklyPlanWeekStart);
    }
  }

  // ---- Weekly review (Phase 2F) ----
  const loadWeeklyReview = useCallback(async (weekStart: string) => {
    setWeeklyReviewLoading(true);
    try {
      const result = await api.getPersonalWeeklyReview(weekStart);
      setWeeklyReviewWeekStart(result.week_start);
      setWeeklyReview(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "주간 리뷰를 불러오지 못했습니다.");
    } finally {
      setWeeklyReviewLoading(false);
    }
  }, []);

  useEffect(() => {
    if (viewMode !== "weekly_review") return;
    // Data-fetch effect: loadWeeklyReview sets the loading flag + week start from the
    // response. The leading setLoading is intentional for this view-change fetch.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadWeeklyReview(weeklyReviewWeekStart ?? sundayOf(realToday));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewMode, weeklyReviewWeekStart, loadWeeklyReview]);

  // Close the task context menu on click-away / Escape / scroll.
  useEffect(() => {
    if (!taskMenu) return;
    function close() {
      setTaskMenu(null);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") close();
    }
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
    };
  }, [taskMenu]);

  if (loading && !payload) {
    return <PersonalBoardSkeleton />;
  }

  return (
    <DndContext
      collisionDetection={boardCollisionDetection}
      // Keep dnd-kit's vertical assistance for nested day columns. Horizontal
      // edge scrolling is handled by the day-board effect above because this
      // board nests vertical scroll containers inside the horizontal scroller.
      autoScroll={{ acceleration: 4, threshold: { x: 0.15, y: 0.2 } }}
      onDragCancel={() => {
        isDraggingRef.current = false;
        skipNextLayoutAnimationRef.current = true;
        setActiveTaskId(null);
        setActiveObjectiveDrag(null);
        setActiveOverId(null);
      }}
      onDragEnd={(event) => void handleDragEnd(event)}
      onDragOver={handleDragOver}
      onDragStart={handleDragStart}
      sensors={sensors}
    >
      <section
        className={`personal-ojosama-root ${rightPanel !== "hidden" && viewMode === "home" ? "panel-visible" : "panel-hidden"}`}
        data-objective-dragging={activeObjectiveDrag ? activeObjectiveDrag.objectiveId : undefined}
      >
        <main className="board-area">
	          <header className="board-toolbar">
	            <div className="toolbar-left">
	              {viewMode === "home" ? (
	              <div className="date-nav" ref={dateMenuRef}>
                <button
	                  aria-label="Previous day"
		                  className="toolbar-button icon-only"
		                  onClick={() => {
		                    goToDate(addDays(selectedDate, -1));
	                  }}
                  type="button"
                >
                  ‹
                </button>
                <button
			                  className={`toolbar-button date-label ${dateMenuOpen ? "active" : ""}`}
			                  onClick={() => {
			                    setRangePickStart(null);
			                    setDateMenuOpen((open) => !open);
		                  }}
                  type="button"
                >
                  <CalendarDays size={15} />
                  {rangeFilter
                    ? `${monthDayLabel(rangeFilter.start)} – ${monthDayLabel(rangeFilter.end)}`
                    : selectedDate === realToday
                      ? "Today"
                      : `${weekdayLabel(selectedDate).slice(0, 3)} ${monthDayLabel(selectedDate)}`}
                </button>
                {dateMenuOpen ? (
                  <DateRangeMenu
                    realToday={realToday}
                    selectedDate={selectedDate}
                    rangeFilter={rangeFilter}
                    rangePickStart={rangePickStart}
                    onGoToday={() => { goToDate(realToday); setDateMenuOpen(false); }}
                    onClearRange={() => { clearRange(); setDateMenuOpen(false); }}
                    onPickDate={(d) => {
                      if (!rangePickStart || rangeFilter) {
                        setRangeFilter(null);
                        goToDate(d);
                        setRangePickStart(d);
                      } else {
                        const start = d < rangePickStart ? d : rangePickStart;
                        const end = d < rangePickStart ? rangePickStart : d;
                        applyRange(start, end);
                        setDateMenuOpen(false);
                      }
                    }}
                  />
                ) : null}
                <button
	                  aria-label="Next day"
		                  className="toolbar-button icon-only"
		                  onClick={() => {
		                    goToDate(addDays(selectedDate, 1));
	                  }}
                  type="button"
                >
                  ›
	                </button>
	              </div>
	              ) : null}
	              {/* 모바일 전용: 데스크톱은 toolbar-right에 Schedule 버튼이 있지만
	                  폰/태블릿(≤980px)에서는 그 줄이 숨겨지므로, Today 옆에 스케줄
	                  토글을 둔다(하단 네비의 스케줄 항목을 대체). */}
	              {viewMode === "home" ? (
	              <button
	                className={`toolbar-button schedule-mobile-toggle ${rightPanel === "schedule" ? "active" : ""}`}
	                onClick={() => void toggleSchedulePanel()}
	                type="button"
	              >
	                <CalendarRange size={15} />
	                스케줄
	              </button>
	              ) : null}
	              {viewMode === "home" ? (
	              <div className="filter-control" ref={filterControlRef}>
                <button className={`toolbar-button ${filterMenuOpen ? "active" : ""}`} onClick={() => setFilterMenuOpen((open) => !open)} type="button">
                  <Rows3 size={15} />
                  Filter
                </button>
                {filterMenuOpen ? (
                  <div className="filter-menu" role="menu">
                    <div className="filter-menu-group">
                      <span>Filter</span>
                      {(Object.keys(filterLabels) as FilterMode[]).map((mode) => (
                        <button className={filterMode === mode ? "active" : ""} key={mode} onClick={() => void changeFilterMode(mode)} type="button">
                          {filterLabels[mode]}
                        </button>
                      ))}
                    </div>
                    <div className="filter-menu-group">
                      <span>Sort</span>
                      {(Object.keys(sortLabels) as SortMode[]).map((mode) => (
                        <button className={sortMode === mode ? "active" : ""} key={mode} onClick={() => void changeSortMode(mode)} type="button">
                          {sortLabels[mode]}
                        </button>
                      ))}
                    </div>
                  </div>
	                ) : null}
	              </div>
	              ) : null}
	              <span
                className="mock-status"
                aria-live="polite"
                title={offlineSyncedAt ? "오프라인 — 마지막으로 동기화된 보드를 표시 중입니다" : undefined}
                style={offlineSyncedAt ? { color: "var(--color-attention, #ef7f62)" } : undefined}
              >
                {offlineSyncedAt
                  ? `오프라인 · 마지막 동기화 ${formatSyncedAt(offlineSyncedAt)}${pendingSyncCount ? ` · ${pendingSyncCount}건 대기` : ""}`
                  : pendingSyncCount
                    ? `동기화 중 · ${pendingSyncCount}건`
                    : statusMessage}
              </span>
            </div>
	            <div className="toolbar-right">
	              <button className={`toolbar-button ${viewMode === "home" ? "active" : ""}`} onClick={() => goToDate(realToday)} type="button">
	                <Home size={15} />
	                {viewLabels.home}
	              </button>
	              <button className={`toolbar-button ${viewMode === "weekly_plan" ? "active" : ""}`} onClick={() => changeViewMode("weekly_plan")} type="button">
	                <CalendarDays size={15} />
	                {viewLabels.weekly_plan}
	              </button>
	              <button className={`toolbar-button ${viewMode === "weekly_review" ? "active" : ""}`} onClick={() => changeViewMode("weekly_review")} type="button">
	                <BarChart3 size={15} />
	                {viewLabels.weekly_review}
	              </button>
	              <button className={`toolbar-button ${rightPanel === "schedule" ? "active" : ""}`} onClick={() => void toggleSchedulePanel()} type="button">
	                <CalendarRange size={15} />
	                Schedule
	              </button>
	              <button className={`toolbar-button ${rightPanel === "backlog" ? "active" : ""}`} onClick={() => void toggleBacklogPanel()} type="button">
	                <Archive size={15} />
	                Backlog
	              </button>
	            </div>
	          </header>

          {error ? (
            <div className="personal-board-error">
              <Banner tone="fail" title="요청 실패">
                {error}
              </Banner>
            </div>
          ) : null}

          {viewMode === "home" ? (
          <nav className="mobile-column-tabs" aria-label="Board columns">
            {mobileTabs.map((tab) => (
              <button
                aria-pressed={activeMobileColumnId === tab.id}
                className={activeMobileColumnId === tab.id ? "active" : ""}
                key={tab.id}
                onClick={() => setMobileColumnId(tab.id)}
                type="button"
              >
                {tab.label}
              </button>
            ))}
          </nav>
          ) : null}

	          <section className={`home-shell ${rightPanel !== "hidden" && viewMode === "home" ? "panel-visible" : "panel-hidden"} ${rightPanel === "schedule" && viewMode === "home" ? "schedule-visible" : ""}`}>
	            {viewMode === "home" ? (
	            <section
	              className={`day-board workflow-board ${scheduleFocus ? "single-column" : ""}`}
	              aria-label="Personal task board"
	              onScroll={handleDayBoardScroll}
	              ref={dayBoardRef}
	            >
	              {boardDays.map((day) => {
	                const tasks = prepareTasks(day.tasks, filterMode, sortMode);
	                const dayObjectives = (objectivesByWeek[sundayOf(day.date)] ?? [])
	                  .map((o) => {
	                    const weekday = weekdayIndex(day.date);
	                    const a = allocationForWeekday(o.day_allocations, weekday);
	                    return a
	                      ? {
	                          id: o.objective_id,
	                          objectiveId: o.objective_id,
	                          weekStart: o.week_start,
	                          weekday,
	                          order: a.order ?? null,
	                          title: o.title,
	                          completed: o.completed,
	                          color: o.project?.color ?? null,
	                          projectName: o.project?.name ?? null,
	                          subtasks: o.subtasks.filter((s) => s.weekday === weekday),
	                        }
	                      : null;
	                  })
	                  .filter((x): x is DayColumnObjective => x !== null);
	                return (
                  <DayColumn
                    key={day.date}
                    objectives={dayObjectives}
                    activeMobile={activeMobileColumnId === `day:${day.date}`}
                    activeOverId={activeOverId}
                    activeTask={activeTask}
                    dateKey={day.date}
                    realToday={realToday}
                    nowEpoch={nowEpoch}
                    fixedEvents={day.fixed_events}
                    notes={day.notes}
                    progress={columnProgress(tasks)}
	                    subtitle={monthDayLabel(day.date)}
	                    taskDraft={taskDrafts[day.date] ?? ""}
	                    tasks={tasks}
	                    timezone={workspaceTimezone}
		                    title={weekdayLabel(day.date)}
	                    instantPlacementTaskIds={instantPlacementTaskIds}
	                    isPast={day.date < realToday}
	                    isToday={day.date === realToday}
	                    hiddenSubtasks={hiddenSubtasks}
		                    sortMode={sortMode}
                    projects={payload?.projects ?? []}
                    onDraftChange={(value) => setTaskDrafts((current) => ({ ...current, [day.date]: value }))}
                    onOpenObjective={(objectiveId, weekday) =>
                      setEditingObjective({ objectiveId, weekday, scope: "day" })
                    }
                    onToggleObjectiveSubtask={toggleObjectiveCardSubtask}
                    onOpenTask={openTaskDetail}
                    onOpenTaskMenu={openTaskMenu}
                    onSubmit={(event) => void submitDayTask(event, day.date)}
                    onSortToggle={() => {
                      const order: SortMode[] = ["time", "manual", "priority"];
                      void changeSortMode(order[(order.indexOf(sortMode) + 1) % order.length]);
                    }}
                    onTaskArchive={archiveTask}
                    onTaskProject={changeTaskProject}
                    onTaskProjectCreate={createProjectChannel}
                    onTaskProjectUpdate={updateProjectChannel}
                    onTaskProjectDelete={deleteProjectChannel}
                    onTaskToggle={toggleTask}
                    onSubtaskToggle={toggleSubtask}
	                  />
	                );
	              })}
	            </section>
	            ) : null}

	            {viewMode === "weekly_plan" ? (
	              <WeeklyPlanView
	                weekStart={weeklyPlanWeekStart}
	                objectives={weeklyPlanWeekStart ? (objectivesByWeek[weeklyPlanWeekStart] ?? []) : []}
	                loading={weeklyPlanLoading}
	                projects={payload?.projects ?? []}
	                realToday={realToday}
	                onWeekStartChange={setWeeklyPlanWeekStart}
		                onCreate={createObjective}
		                onPatch={patchObjective}
		                onDelete={deleteObjective}
		                onOpenObjective={(objective) =>
		                  setEditingObjective({
		                    objectiveId: objective.objective_id,
		                    weekday: objectiveDefaultWeekday(objective.week_start, realToday),
		                    scope: "week",
		                  })
		                }
		                onCreateProject={createProjectChannel}
		                onUpdateProject={updateProjectChannel}
		                onDeleteProject={deleteProjectChannel}
		              />
	            ) : null}

	            {viewMode === "weekly_review" ? (
	              <WeeklyReviewView
	                weekStart={weeklyReviewWeekStart}
	                review={weeklyReview}
	                loading={weeklyReviewLoading}
	                projects={payload?.projects ?? []}
	                onWeekStartChange={setWeeklyReviewWeekStart}
	              />
	            ) : null}

	            {viewMode === "home" && rightPanel === "backlog" ? (
	              <BacklogPanel
	                activeMobile={activeMobileColumnId === "backlog"}
                activeBucketId={activeBucketId}
                activeOverId={activeOverId}
                activeTask={activeTask}
                bucketDrafts={bucketDrafts}
                buckets={payload?.backlog_buckets ?? []}
                timezone={workspaceTimezone}
                realToday={realToday}
                nowEpoch={nowEpoch}
	                filterLabel={filterLabels[filterMode]}
	                instantPlacementTaskIds={instantPlacementTaskIds}
	                hiddenSubtasks={hiddenSubtasks}
	                projects={payload?.projects ?? []}
                tasksByBucket={backlogTasksByBucket}
	                onCollapsePanel={() => void changeRightPanel("hidden")}
                onDraftChange={(bucketId, value) => setBucketDrafts((current) => ({ ...current, [bucketId]: value }))}
                onOpenTask={openTaskDetail}
                onOpenTaskMenu={openTaskMenu}
                onSetActiveBucket={setActiveBucketId}
                onSubmit={(event, bucketId) => void submitBacklogTask(event, bucketId)}
                onBucketToggle={toggleBacklogBucket}
                onTaskArchive={archiveTask}
                onTaskProject={changeTaskProject}
                onTaskProjectCreate={createProjectChannel}
                onTaskProjectUpdate={updateProjectChannel}
                onTaskProjectDelete={deleteProjectChannel}
                onTaskToggle={toggleTask}
                onSubtaskToggle={toggleSubtask}
              />
            ) : null}

	            {viewMode === "home" && rightPanel === "schedule" ? (
	              <SchedulePanel
	                dateKey={selectedDate}
	                title={selectedDate === realToday ? `오늘 · ${monthDayLabel(selectedDate)}` : `${weekdayLabel(selectedDate)} ${monthDayLabel(selectedDate)}`}
	                tasks={scheduleDay?.tasks ?? []}
	                events={scheduleEvents}
	                nowMinutes={scheduleNowMinutes}
	                gridRef={(node) => {
	                  scheduleGridRef.current = node;
	                }}
	                activeDragTaskId={activeTaskId}
	                onOpenTask={openTaskDetail}
	                onCommit={(task, startMin, endMin) => void setTaskScheduleMinutes(task, startMin, endMin)}
	                onCollapse={() => void changeRightPanel("hidden")}
	              />
	            ) : null}
          </section>
        </main>
      </section>
      {editingTask && payload
        ? createPortal(
            <TaskDetailPopup
              key={editingTask.task_id}
              buckets={payload.backlog_buckets}
              comments={taskComments[editingTask.task_id] ?? []}
              days={payload.days}
              noteText={taskNoteDrafts[editingTask.task_id] ?? editingTask.note ?? ""}
              task={editingTask}
              projects={payload.projects}
              timezone={workspaceTimezone}
              realToday={realToday}
              nowEpoch={nowEpoch}
              onArchive={archiveTask}
              onCommentAdd={(body) => void addTaskComment(editingTask, body)}
              onCreateProject={createProjectChannel}
              onClose={() => {
                // 입력 중이던 노트를 저장하고, 저장이 "성공했을 때만" draft 오버레이를
                // 걷어 서버 값이 SoR로 남게 한다. 실패하면 draft가 입력을 지키고
                // pending 재시도 대상으로 남는다(닫기 직후 재열람+새 입력도 보호).
                const closingId = editingTask.task_id;
                const draftAtClose = taskNoteDrafts[closingId];
                void flushNoteSave().then((saved) => {
                  if (!saved) return;
                  setTaskNoteDrafts((current) => {
                    if (!(closingId in current) || current[closingId] !== draftAtClose) {
                      return current;
                    }
                    const next = { ...current };
                    delete next[closingId];
                    return next;
                  });
                });
                setEditingTaskId(null);
              }}
              onMoveTask={moveTaskTo}
              onNoteTextChange={(value) => changeTaskNote(editingTask, value)}
              onNoteBlur={() => void flushNoteSave()}
              onProject={changeTaskProject}
              onProjectUpdate={updateProjectChannel}
              onProjectDelete={deleteProjectChannel}
              onPriority={changeTaskPriority}
              onSubtaskAdd={addSubtask}
              onSubtaskToggle={toggleSubtask}
              onSubtaskRename={renameSubtask}
              onSubtaskDelete={deleteSubtask}
              onSchedule={changeTaskSchedule}
              onDue={changeTaskDue}
              onTitle={changeTaskTitle}
              onToggle={toggleTask}
            />,
            document.body,
          )
        : null}
      {editingObj && editingObjective ? (
        <ObjectiveDetailModal
	          key={editingObj.objective_id}
	          objective={editingObj}
	          weekday={editingObjective.weekday}
	          scope={editingObjective.scope}
	          projects={payload?.projects ?? []}
	          realToday={realToday}
	          onPatch={patchObjective}
          onAddSubtask={addObjectiveSubtask}
          onToggleSubtask={toggleObjectiveSubtask}
          onAddComment={addObjectiveComment}
	          onSetDayNote={setObjectiveDayNote}
	          onClose={() => {
	            setEditingObjective(null);
	          }}
          onCreateProject={createProjectChannel}
          onUpdateProject={updateProjectChannel}
          onDeleteProject={deleteProjectChannel}
        />
      ) : null}
      {taskMenu
        ? createPortal(
            <TaskContextMenu
              task={taskMenu.task}
              x={taskMenu.x}
              y={taskMenu.y}
              hidden={hiddenSubtasks.has(taskMenu.task.task_id)}
              onAddToCalendar={() => void addTaskToCalendar(taskMenu.task)}
              onMoveToBacklog={() => void moveTaskToBacklog(taskMenu.task)}
              onToggleSubtasks={() => toggleHiddenSubtasks(taskMenu.task.task_id)}
              onDuplicate={() => void duplicateTask(taskMenu.task)}
              onDelete={() => void deleteTask(taskMenu.task)}
            />,
            document.body,
          )
        : null}
      <DragOverlay dropAnimation={null}>
        {activeTask ? (
          <TaskDragPreview task={activeTask} />
        ) : activeObjectivePreview ? (
          <ObjectiveDragPreview {...activeObjectivePreview} />
        ) : null}
      </DragOverlay>
    </DndContext>
  );
}

function PersonalBoardSkeleton() {
  return (
    <section className="personal-ojosama-root">
      <main className="board-area">
        <header className="board-toolbar">
          <Skeleton className="h-8 w-44" />
          <Skeleton className="h-8 w-28" />
        </header>
        <section className="home-shell">
          <section className="day-board workflow-board">
            {[0, 1, 2].map((item) => (
              <div className="day-column" key={item}>
                <Skeleton className="h-12 w-36" />
                <Skeleton className="mt-5 h-10 w-full" />
                <Skeleton className="mt-3 h-24 w-full" />
              </div>
            ))}
          </section>
        </section>
      </main>
    </section>
  );
}

type DayColumnObjective = {
  id: string;
  objectiveId: string;
  weekStart: string;
  weekday: number;
  order: number | null;
  title: string;
  completed: boolean;
  color: string | null;
  projectName: string | null;
  subtasks: ObjectiveSubtask[];
};

function DayColumn({
  activeMobile,
  activeOverId,
  activeTask,
  dateKey,
  fixedEvents,
  notes,
  objectives,
  progress,
  projects,
  instantPlacementTaskIds,
  isPast,
  isToday,
  hiddenSubtasks,
  sortMode,
  subtitle,
  taskDraft,
  tasks,
  timezone,
  realToday,
  nowEpoch,
  title,
  onDraftChange,
  onOpenObjective,
  onToggleObjectiveSubtask,
  onOpenTask,
  onOpenTaskMenu,
  onSubmit,
  onSortToggle,
  onTaskArchive,
  onTaskProject,
  onTaskProjectCreate,
  onTaskProjectUpdate,
  onTaskProjectDelete,
  onTaskToggle,
  onSubtaskToggle,
}: {
  activeMobile: boolean;
  activeOverId: string | null;
  activeTask: PersonalBoardTask | null;
  dateKey: string;
  fixedEvents: PersonalBoardFixedEvent[];
  notes: PersonalBoardNote[];
  objectives: DayColumnObjective[];
  progress: number;
  projects: PersonalBoardProject[];
  instantPlacementTaskIds: ReadonlySet<string>;
  isPast: boolean;
  isToday: boolean;
  hiddenSubtasks: ReadonlySet<string>;
  sortMode: SortMode;
  subtitle: string;
  taskDraft: string;
  tasks: PersonalBoardTask[];
  timezone: string;
  realToday: string;
  nowEpoch: number;
  title: string;
  onDraftChange: (value: string) => void;
  onOpenObjective: (objectiveId: string, weekday: number) => void;
  onToggleObjectiveSubtask: (objectiveId: string, subtaskId: string, completed: boolean) => void;
  onOpenTask: (taskId: string) => void;
  onOpenTaskMenu: (task: PersonalBoardTask, x: number, y: number) => void;
  onSubmit: (event: FormEvent) => void;
  onSortToggle: () => void;
  onTaskArchive: (task: PersonalBoardTask) => void;
  onTaskProject: (
    task: PersonalBoardTask,
    projectId: string | null,
    project?: PersonalBoardProject | null,
  ) => void | Promise<void>;
  onTaskProjectCreate: (name: string) => Promise<PersonalBoardProject | null>;
  onTaskProjectUpdate: (
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) => Promise<PersonalBoardProject | null>;
  onTaskProjectDelete: (projectId: string) => Promise<boolean>;
  onTaskToggle: (task: PersonalBoardTask) => void;
  onSubtaskToggle: (task: PersonalBoardTask, subtaskId: string, completed: boolean) => void;
}) {
  const { isOver, setNodeRef } = useDroppable({ id: `day:${dateKey}` });
  const addTaskInputRef = useRef<HTMLInputElement | null>(null);
  const showEndDropPreview = Boolean(activeTask && activeOverId === `day:${dateKey}` && activeTask.task_id !== tasks.at(-1)?.task_id);

  // Tasks and objectives share one ordered column. Tasks position by their
  // integer order_index; objectives float between them via a fractional per-day
  // order (unset objectives sort to the top at -0.5). Objectives win ties.
  type DayItem =
    | { kind: "task"; pos: number; key: string; task: PersonalBoardTask }
    | { kind: "objective"; pos: number; key: string; obj: DayColumnObjective };
  // 수동(manual) 정렬에서는 task order_index와 objective의 fractional order를 같은 공간에서
  // 섞어 정렬한다(드래그 재정렬 보존). 시간순/우선순위 정렬에서는 prepareTasks가 이미 정렬한
  // tasks 순서를 그대로 쓰고, 시간 개념이 없는 주간 목표는 컬럼 상단에 모아 보여준다.
  const items: DayItem[] =
    sortMode === "manual"
      ? [
          ...tasks.map((t): DayItem => ({ kind: "task", pos: t.order_index, key: t.task_id, task: t })),
          ...objectives.map((o): DayItem => ({ kind: "objective", pos: o.order ?? -0.5, key: o.id, obj: o })),
        ].sort((a, b) => {
          // 완료된 티켓은 order_index를 유지한 채 컬럼 맨 아래로 가라앉힌다(수동 정렬에서도).
          const aDone = a.kind === "task" && a.task.completed;
          const bDone = b.kind === "task" && b.task.completed;
          if (aDone !== bDone) return aDone ? 1 : -1;
          if (a.pos !== b.pos) return a.pos - b.pos;
          if (a.kind === b.kind) return 0;
          return a.kind === "objective" ? -1 : 1;
        })
      : [
          ...objectives
            .slice()
            .sort((a, b) => (a.order ?? -0.5) - (b.order ?? -0.5))
            .map((o): DayItem => ({ kind: "objective", pos: 0, key: o.id, obj: o })),
          ...tasks.map((t, i): DayItem => ({ kind: "task", pos: i, key: t.task_id, task: t })),
        ];

  return (
    <section
      className={`day-column ${activeMobile ? "active-mobile" : ""} ${isOver ? "drop-over" : ""} ${isPast ? "is-past" : ""} ${isToday ? "is-today" : ""}`}
      data-drop-id={`day:${dateKey}`}
      ref={setNodeRef}
    >
      <header className="day-header">
        <h2>{title}</h2>
        <p>{subtitle}</p>
        <div className="progress-track" aria-label={`${progress}% planned load`} style={{ "--progress": `${progress}%` } as CSSProperties}>
          <span />
        </div>
      </header>

      <form
        className="add-task-row"
        onSubmit={onSubmit}
        onClick={(event) => {
          // \ud589 \uc5b4\ub514\ub97c \ub20c\ub7ec\ub3c4(+ \uc544\uc774\ucf58 \ud3ec\ud568) \uc785\ub825\uc5d0 \ud3ec\ucee4\uc2a4. \ub2e8, \uc815\ub82c/\ucd94\uac00 \ubc84\ud2bc \ud074\ub9ad\uc740 \uadf8\ub300\ub85c.
          if ((event.target as HTMLElement).closest("button")) return;
          addTaskInputRef.current?.focus();
        }}
      >
        <button
          type="submit"
          className="add-task-submit"
          aria-label="\ud560 \uc77c \ucd94\uac00"
          title="\ud560 \uc77c \ucd94\uac00"
          onClick={() => {
            // \uc785\ub825\uc774 \ube44\uc5b4 \uc788\uc73c\uba74 \uc81c\ucd9c \ub300\uc2e0 \uc785\ub825\uce78\uc5d0 \ud3ec\ucee4\uc2a4\ud574 \ubc14\ub85c \uc4f8 \uc218 \uc788\uac8c.
            if (!taskDraft.trim()) addTaskInputRef.current?.focus();
          }}
        >
          <Plus size={16} />
        </button>
        <input ref={addTaskInputRef} aria-label="Add task" type="text" placeholder="Add task" value={taskDraft} onChange={(event) => onDraftChange(event.target.value)} />
        {tasks.length > 0 ? (
          <button aria-label={`정렬: ${sortLabels[sortMode]} (눌러서 전환)`} className={`add-task-sort-button ${sortMode !== "manual" ? "active" : ""}`} onClick={onSortToggle} title={`정렬: ${sortLabels[sortMode]} (눌러서 전환)`} type="button">
            <ArrowUpDown size={15} />
          </button>
        ) : null}
      </form>

      <div className="day-column-scroll">
      {fixedEvents.length > 0 ? (
        <div className="day-event-stack">
          {fixedEvents.map((event) => (
            <div className="day-event-row" key={event.event_id} title={event.title}>
              <Clock size={12} aria-hidden="true" />
              <span className="day-event-time">{timeOfDayLabel(timeInputValue(event.starts_at, timezone))}</span>
              <span className="day-event-title">{event.title}</span>
            </div>
          ))}
        </div>
      ) : null}

      <div className="card-stack">
        {items.map((item) =>
          item.kind === "task" ? (
            <Fragment key={item.key}>
              {activeTask && activeOverId === `task:${item.task.task_id}` && activeTask.task_id !== item.task.task_id ? (
                <TaskDragPreview dropTarget task={activeTask} />
              ) : null}
              <TaskCard
                instantPlacement={instantPlacementTaskIds.has(item.task.task_id)}
                subtasksHidden={hiddenSubtasks.has(item.task.task_id)}
                projects={projects}
                task={item.task}
                timezone={timezone}
                realToday={realToday}
                nowEpoch={nowEpoch}
                onArchive={onTaskArchive}
                onOpen={onOpenTask}
                onOpenMenu={onOpenTaskMenu}
                onProject={(projectId, project) => void onTaskProject(item.task, projectId, project)}
                onProjectCreate={onTaskProjectCreate}
                onProjectUpdate={onTaskProjectUpdate}
                onProjectDelete={onTaskProjectDelete}
                onToggle={onTaskToggle}
                onSubtaskToggle={onSubtaskToggle}
              />
            </Fragment>
          ) : (
            <ObjectiveCard
              key={item.key}
              obj={item.obj}
              dateKey={dateKey}
              activeOverId={activeOverId}
              onOpenObjective={onOpenObjective}
              onToggleSubtask={onToggleObjectiveSubtask}
            />
          ),
        )}
        {activeTask && showEndDropPreview ? <TaskDragPreview dropTarget task={activeTask} /> : null}
      </div>

      {notes.length > 0 ? (
        <div className="day-note-stack">
          {notes.map((note) => (
            <div className="day-note-row" key={note.note_id} title={note.body}>
              <span className="day-note-kind">{note.kind}</span>
              <span className="day-note-title">{note.title}</span>
            </div>
          ))}
        </div>
      ) : null}
      </div>
    </section>
  );
}

function ObjectiveCard({
  obj,
  dateKey,
  activeOverId,
  onOpenObjective,
  onToggleSubtask,
}: {
  obj: DayColumnObjective;
  dateKey: string;
  activeOverId: string | null;
  onOpenObjective: (objectiveId: string, weekday: number) => void;
  onToggleSubtask: (objectiveId: string, subtaskId: string, completed: boolean) => void;
}) {
  const sortedSubtasks = [...obj.subtasks].sort((a, b) => a.order_index - b.order_index);
  // A 5px drag-activation distance separates this click-to-open from a drag.
  const dragId = `objective:${obj.objectiveId}:${dateKey}`;
  const { attributes, isDragging, listeners, setNodeRef: setDraggableRef } = useDraggable({ id: dragId });
  const { setNodeRef: setDroppableRef } = useDroppable({ id: dragId });
  const setNodeRef = (node: HTMLElement | null) => {
    setDraggableRef(node);
    setDroppableRef(node);
  };
  // The dragged objective is shown via the DragOverlay preview, so the original
  // card stays in place (dimmed by `.dragging`) instead of floating around.
  const style: CSSProperties = {};

  return (
    <article
      className={`objective-card ${obj.completed ? "completed" : ""} ${isDragging ? "dragging" : ""} ${activeOverId === dragId ? "drop-over" : ""}`}
      key={obj.id}
      title={obj.title}
      onClick={(event) => {
        if (hasTextSelectionWithin(event.currentTarget)) return;
        onOpenObjective(obj.objectiveId, obj.weekday);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpenObjective(obj.objectiveId, obj.weekday);
        }
      }}
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      role="button"
      tabIndex={0}
    >
      <div className="objective-card-top">
      </div>
      <strong className="objective-card-title" data-card-text="">{obj.title}</strong>
      {sortedSubtasks.length > 0 ? (
        <div className="objective-card-subtasks">
          {sortedSubtasks.map((subtask) => (
            <div className="objective-card-subtask" key={subtask.subtask_id}>
              <button
                className={`objective-card-subtask-check ${subtask.completed ? "checked" : ""}`}
                type="button"
                aria-label={subtask.completed ? "Mark subtask open" : "Mark subtask done"}
                data-no-card-open
                data-no-card-drag
                onClick={(event) => {
                  event.stopPropagation();
                  event.preventDefault();
                  onToggleSubtask(obj.objectiveId, subtask.subtask_id, !subtask.completed);
                }}
              >
                <Check size={11} />
              </button>
              <span className={`objective-card-subtask-title ${subtask.completed ? "completed" : ""}`} data-card-text="" title={subtask.title}>
                {subtask.title}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      <div className="objective-card-foot">
        <Target size={15} className="objective-card-icon" aria-hidden />
        <span className="objective-card-channel"># {obj.projectName ?? "Unassigned"}</span>
      </div>
    </article>
  );
}

function TaskCard({
  instantPlacement,
  subtasksHidden,
  projects,
  task,
  timezone,
  realToday,
  nowEpoch,
  onArchive,
  onOpen,
  onOpenMenu,
  onProject,
  onProjectCreate,
  onProjectUpdate,
  onProjectDelete,
  onToggle,
  onSubtaskToggle,
}: {
  instantPlacement: boolean;
  subtasksHidden: boolean;
  projects: PersonalBoardProject[];
  task: PersonalBoardTask;
  timezone: string;
  realToday: string;
  nowEpoch: number;
  onArchive: (task: PersonalBoardTask) => void;
  onOpen: (taskId: string) => void;
  onOpenMenu: (task: PersonalBoardTask, x: number, y: number) => void;
  onProject: (projectId: string | null, project?: PersonalBoardProject | null) => void;
  onProjectCreate: (name: string) => Promise<PersonalBoardProject | null>;
  onProjectUpdate: (
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) => Promise<PersonalBoardProject | null>;
  onProjectDelete: (projectId: string) => Promise<boolean>;
  onToggle: (task: PersonalBoardTask) => void;
  onSubtaskToggle: (task: PersonalBoardTask, subtaskId: string, completed: boolean) => void;
}) {
  const { attributes, isDragging, listeners, setNodeRef: setDraggableRef, transform } = useDraggable({ id: task.task_id });
  const { isOver, setNodeRef: setDroppableRef } = useDroppable({ id: `task:${task.task_id}` });
  const setNodeRef = (node: HTMLElement | null) => {
    setDraggableRef(node);
    setDroppableRef(node);
  };
  const style: CSSProperties = {
    "--task-channel-color": task.project?.color ?? projects[0]?.color ?? "#34343a",
    ...(transform && (transform.x !== 0 || transform.y !== 0)
      ? { transform: `translate3d(${transform.x}px, ${transform.y}px, 0)` }
      : {}),
  } as CSSProperties;
  const visibleSubtasks = subtasksHidden ? [] : task.subtasks;
  const hiddenCount = subtasksHidden ? task.subtasks.length : 0;
  const dueChip = dueChipInfo(task.due_date, task.due_time, timezone, realToday, nowEpoch, task.completed);

  return (
    <article
      className={`task-card ${instantPlacement ? "instant-placement" : ""} ${task.completed ? "completed" : ""} ${task.subtasks.length >= 3 ? "subtask-rich" : ""} ${visibleSubtasks.length >= 3 ? "multi-subtasks" : ""} ${visibleSubtasks.length === 1 ? "single-subtask" : ""} ${visibleSubtasks.length === 0 ? "empty-summary" : ""} ${isDragging ? "dragging" : ""} ${isOver ? "drop-target" : ""}`}
      data-layout-id={`task:${task.task_id}`}
      data-task-id={task.task_id}
      onClick={(event) => {
        if (shouldIgnoreCardOpen(event.target)) return;
        // 카드 텍스트를 드래그-선택하고 뗀 click 으로는 상세를 열지 않는다(복사 허용).
        if (hasTextSelectionWithin(event.currentTarget)) return;
        onOpen(task.task_id);
      }}
      onContextMenu={(event) => {
        event.preventDefault();
        onOpenMenu(task, event.clientX, event.clientY);
      }}
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
    >
      <div className="task-card-top">
        <button className="drag-handle" data-card-drag-handle aria-label="Drag task" type="button">
          ⋮⋮
        </button>
        <span data-card-text="">{task.source_kind === "manual" ? "Task" : task.source_label ?? task.source_kind}</span>
        <button
          className="task-more-button"
          data-no-card-open
          data-no-card-drag
          onClick={(event) => {
            event.stopPropagation();
            const rect = event.currentTarget.getBoundingClientRect();
            onOpenMenu(task, rect.right, rect.bottom);
          }}
          aria-label="Task actions"
          type="button"
        >
          <MoreHorizontal size={14} />
        </button>
        <button className="task-archive-button" onClick={() => onArchive(task)} aria-label="Archive task" type="button">
          <Archive size={13} />
        </button>
      </div>
      {task.scheduled_time ? (
        <div className="task-time-label" data-card-text="">
          {timeOfDayLabel(task.scheduled_time)}
          {task.scheduled_end_time ? ` – ${timeOfDayLabel(task.scheduled_end_time)}` : ""}
        </div>
      ) : null}
      <div className="task-card-main">
        <strong className="task-title" data-card-text="" title={task.title}>
          {task.title}
        </strong>
      </div>
      {dueChip ? (
        <div className={`task-due-chip tone-${dueChip.tone}`} role="img" aria-label={`${dueChip.text}, ${dueChip.title}`} title={dueChip.title}>
          <CalendarClock size={12} aria-hidden="true" />
          <span data-card-text="">{dueChip.text}</span>
        </div>
      ) : null}
      {visibleSubtasks.length > 0 ? (
        <div className="subtask-list">
          {visibleSubtasks.map((subtask) => (
            // 행을 label로 감싸지 않는다: 체크 토글은 체크박스 주변(.subtask-check)만,
            // 텍스트 클릭은 카드 onClick으로 버블돼 티켓 상세를 연다(.task-title과 동일 패턴).
            <div className="subtask-row" key={subtask.subtask_id}>
              <label className="subtask-check" data-no-card-drag>
                <input
                  aria-label={subtask.completed ? "하위작업 완료 취소" : "하위작업 완료"}
                  checked={subtask.completed}
                  type="checkbox"
                  onChange={(event) => onSubtaskToggle(task, subtask.subtask_id, event.target.checked)}
                />
              </label>
              <span className="subtask-text" data-card-text="" title={subtask.title}>
                {subtask.title}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      {hiddenCount > 0 ? (
        <div className="subtask-count-chip">{hiddenCount} subtask{hiddenCount === 1 ? "" : "s"}</div>
      ) : null}
      <div className="task-card-meta">
        <div className="task-card-footer-icons">
          <button
            aria-label={task.completed ? "완료 취소" : "완료"}
            className={`task-card-footer-check ${task.completed ? "checked" : ""}`}
            data-no-card-open
            onClick={(event) => {
              event.stopPropagation();
              onToggle(task);
            }}
            type="button"
          >
            <Check size={12} />
          </button>
          <span aria-hidden="true" className="task-card-hover-action">
            <CalendarDays size={14} />
          </span>
          <span aria-hidden="true" className="task-card-hover-action">
            <Clock size={14} />
          </span>
          <span aria-hidden="true" className="task-card-hover-action">
            <Flag size={14} />
          </span>
        </div>
        <ChannelPicker
          project={task.project}
          projects={projects}
          sourceLabel={task.source_label ?? task.source_kind}
          variant="card"
          onCreateProject={onProjectCreate}
          onSelect={onProject}
          onUpdateProject={onProjectUpdate}
          onDeleteProject={onProjectDelete}
        />
      </div>
    </article>
  );
}

function ObjectiveDragPreview({
  title,
  projectName,
}: {
  title: string;
  projectName: string | null;
}) {
  return (
    <article className="objective-card objective-drag-preview">
      <div className="objective-card-top" />
      <strong className="objective-card-title">{title}</strong>
      <div className="objective-card-foot">
        <Target size={15} className="objective-card-icon" aria-hidden />
        <span className="objective-card-channel"># {projectName ?? "Unassigned"}</span>
      </div>
    </article>
  );
}

function TaskDragPreview({ dropTarget = false, task }: { dropTarget?: boolean; task: PersonalBoardTask }) {
  return (
    <article
      className={`task-drag-preview ${dropTarget ? "task-drop-preview" : ""} ${task.subtasks.length >= 3 ? "subtask-rich" : ""} ${task.subtasks.length >= 3 ? "multi-subtasks" : ""} ${task.subtasks.length === 0 ? "empty-summary" : ""}`}
      data-layout-id={dropTarget ? `drop-preview:${task.task_id}` : undefined}
      style={{ "--task-channel-color": task.project?.color ?? "#34343a" } as CSSProperties}
    >
      <div className="task-card-top">
        <span className="drag-preview-handle" aria-hidden="true">
          ⋮⋮
        </span>
        <span>{task.source_kind === "manual" ? "Task" : task.source_label ?? task.source_kind}</span>
        <Archive size={13} aria-hidden="true" />
      </div>
      {task.scheduled_time ? (
        <div className="task-time-label">
          {timeOfDayLabel(task.scheduled_time)}
          {task.scheduled_end_time ? ` – ${timeOfDayLabel(task.scheduled_end_time)}` : ""}
        </div>
      ) : null}
      <div className="task-card-main">
        <strong className="task-title" title={task.title}>
          {task.title}
        </strong>
      </div>
      {task.subtasks.length > 0 ? (
        <div className="subtask-list">
          {task.subtasks.map((subtask) => (
            <div className="drag-preview-subtask" key={subtask.subtask_id}>
              <span className={subtask.completed ? "checked" : ""} aria-hidden="true">
                <Check size={12} />
              </span>
              <span title={subtask.title}>{subtask.title}</span>
            </div>
          ))}
        </div>
      ) : null}
      <div className="task-card-meta">
        <div className="task-card-footer-icons">
          <span className={`task-card-footer-check ${task.completed ? "checked" : ""}`} aria-hidden="true">
            <Check size={12} />
          </span>
          <span className="task-card-hover-action visible" aria-hidden="true">
            <CalendarDays size={14} />
          </span>
          <span className="task-card-hover-action visible" aria-hidden="true">
            <Clock size={14} />
          </span>
          <span className="task-card-hover-action visible" aria-hidden="true">
            <Flag size={14} />
          </span>
        </div>
        <span className="drag-preview-channel" aria-hidden="true">
          <span>#</span>
          {task.project ? task.project.name : task.source_label ?? task.source_kind}
        </span>
      </div>
    </article>
  );
}

function ChannelPicker({
  project,
  projects,
  sourceLabel,
  variant = "detail",
  allowCompany = true,
  onCreateProject,
  onSelect,
  onUpdateProject,
  onDeleteProject,
}: {
  project: PersonalBoardProject | null;
  projects: PersonalBoardProject[];
  sourceLabel: string | null;
  variant?: "card" | "detail";
  // 회사 프로젝트 채널을 고를 수 있는지. 회사 공개 scope가 없는 항목(주간 목표 등)에는
  // false를 줘 회사 채널을 숨기고 잘못된 '회사 공개' 확인이 뜨지 않게 한다.
  allowCompany?: boolean;
  onCreateProject: (name: string) => Promise<PersonalBoardProject | null>;
  onSelect: (projectId: string | null, project?: PersonalBoardProject | null) => void | Promise<void>;
  onUpdateProject?: (
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) => Promise<PersonalBoardProject | null>;
  onDeleteProject?: (projectId: string) => Promise<boolean>;
}) {
  const pickerRef = useRef<HTMLDivElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const [managerOpen, setManagerOpen] = useState(false);
  const [placement, setPlacement] = useState<"above" | "below">("below");
  const [popoverPosition, setPopoverPosition] = useState<{ left: number; top: number; arrowLeft: number; width: number } | null>(null);
  const [query, setQuery] = useState("");
  const [channelDraft, setChannelDraft] = useState("");
  const [portalRoot, setPortalRoot] = useState<HTMLElement | null>(null);
  const [projectDrafts, setProjectDrafts] = useState<Record<string, { name: string; color: string }>>({});
  const normalizedQuery = query.trim().toLowerCase();
  const filteredProjects = projects.filter((item) => item.name.toLowerCase().includes(normalizedQuery));
  // 개인 채널(나만 봄) vs 회사 프로젝트 채널(담당 회사 프로젝트, 회사 공개)로 나눈다.
  // allowCompany=false면 회사 채널을 아예 노출하지 않는다(회사 공개 scope가 없는 표면).
  const personalProjects = filteredProjects.filter((item) => item.kind !== "company");
  const companyProjects = allowCompany
    ? filteredProjects.filter((item) => item.kind === "company")
    : [];
  const hasCompanyChannels = allowCompany && projects.some((item) => item.kind === "company");
  const showUnassigned = !normalizedQuery || "unassigned".includes(normalizedQuery);
  // Only surface the search box once the channel list is long enough to need it;
  // an empty search box on a short list just leaves an awkward blank gap.
  const showSearch = projects.length > 7;
  const color = project?.color ?? "#9a9ba0";

  const updatePlacement = useCallback(() => {
    const trigger = pickerRef.current?.querySelector(".channel-trigger");
    const rect = trigger?.getBoundingClientRect();
    if (!rect) return;

    const viewportPadding = 12;
    const rawPopoverWidth = variant === "detail" ? (managerOpen ? 340 : 300) : managerOpen ? 392 : 286;
    const popoverWidth = Math.min(rawPopoverWidth, Math.max(240, window.innerWidth - viewportPadding * 2));
    const baseHeight = variant === "detail" ? (managerOpen ? 390 : 348) : managerOpen ? 410 : 374;
    const maxHeight = Math.min(baseHeight, window.innerHeight - viewportPadding * 2);
    const cardRect = variant === "card" ? pickerRef.current?.closest(".task-card")?.getBoundingClientRect() : null;
    const preferredLeft = variant === "card" ? (cardRect ? cardRect.left + 16 : rect.left - 150) : rect.left;
    const maxLeft = Math.max(viewportPadding, window.innerWidth - popoverWidth - viewportPadding);
    const left = Math.min(Math.max(preferredLeft, viewportPadding), maxLeft);
    const spaceBelow = window.innerHeight - rect.bottom - 14 - viewportPadding;
    const spaceAbove = rect.top - 14 - viewportPadding;
    const nextPlacement = variant === "card" && spaceBelow < maxHeight && spaceAbove > spaceBelow ? "above" : "below";
    const unclampedTop = nextPlacement === "above" ? rect.top - maxHeight - 14 : rect.bottom + 14;
    const top = Math.min(Math.max(unclampedTop, viewportPadding), window.innerHeight - maxHeight - viewportPadding);
    const arrowLeft = Math.min(Math.max(rect.left + rect.width / 2 - left - 7, 24), popoverWidth - 24);

    setPopoverPosition({ left, top, arrowLeft, width: popoverWidth });
    setPlacement(variant === "card" ? nextPlacement : "below");
  }, [managerOpen, variant]);

  useLayoutEffect(() => {
    if (!open) return;
    function closeOnOutsidePointer(event: globalThis.PointerEvent) {
      if (!(event.target instanceof Node)) return;
      if (!pickerRef.current?.contains(event.target) && !popoverRef.current?.contains(event.target)) setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
    }
    updatePlacement();
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", updatePlacement);
    window.addEventListener("scroll", updatePlacement, true);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", updatePlacement);
      window.removeEventListener("scroll", updatePlacement, true);
    };
  }, [open, updatePlacement]);

  async function submitChannel(event: FormEvent) {
    event.preventDefault();
    const name = channelDraft.trim();
    if (!name) return;
    const created = await onCreateProject(name);
    if (!created) return;
    await onSelect(created.project_id, created);
    setChannelDraft("");
    setOpen(false);
    setManagerOpen(false);
  }

  // 채널 선택은 바로 적용한다. 회사 프로젝트 채널을 고르면 그 업무가 회사 공개가 되지만,
  // 채널 자체가 회사/개인을 명시하고 옵션에도 회사 공개 표시가 있어 매번 확인창을 띄우지
  // 않는다(할당 즉시 반영).
  function chooseChannel(item: PersonalBoardProject) {
    void onSelect(item.project_id, item);
    setOpen(false);
  }

  function changeProjectDraft(projectId: string, patch: Partial<{ name: string; color: string }>) {
    const item = projects.find((projectItem) => projectItem.project_id === projectId);
    setProjectDrafts((current) => ({
      ...current,
      [projectId]: {
        name: current[projectId]?.name ?? item?.name ?? "",
        color: current[projectId]?.color ?? item?.color ?? channelPalette[0],
        ...patch,
      },
    }));
  }

  async function submitProjectDraft(event: FormEvent, item: PersonalBoardProject) {
    event.preventDefault();
    const draft = projectDrafts[item.project_id] ?? { name: item.name, color: item.color ?? channelPalette[0] };
    const name = draft.name.trim();
    if (!name || !onUpdateProject) return;
    const updated = await onUpdateProject(item.project_id, { name, color: draft.color });
    if (!updated) return;
    setProjectDrafts((current) => ({
      ...current,
      [updated.project_id]: { name: updated.name, color: updated.color ?? channelPalette[0] },
    }));
  }

  const popoverStyle = popoverPosition
    ? ({
        "--channel-arrow-left": `${popoverPosition.arrowLeft}px`,
        left: popoverPosition.left,
        top: popoverPosition.top,
        width: popoverPosition.width,
      } as CSSProperties)
    : undefined;

  return (
    <div className={`channel-picker ${variant} ${placement} interactive`} data-no-card-drag data-no-card-open ref={pickerRef}>
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label="Assign channel"
        className="channel-trigger"
        onClick={(event) => {
          event.stopPropagation();
          setPortalRoot((event.currentTarget.closest(".task-popup-layer, .personal-ojosama-root") as HTMLElement | null) ?? document.body);
          setOpen((value) => !value);
          setManagerOpen(false);
          setQuery("");
          window.requestAnimationFrame(updatePlacement);
        }}
        style={{ "--channel-color": color } as CSSProperties}
        title={project ? `#${project.name}` : sourceLabel ?? "Unassigned"}
        type="button"
      >
        <span>#</span>
        {project?.name ?? sourceLabel ?? "Unassigned"}
      </button>
      {open && portalRoot
        ? createPortal(
            <div
              className={`channel-popover ${variant} ${placement} ${managerOpen ? "manager-open" : ""}`}
              onClick={(event) => event.stopPropagation()}
              ref={popoverRef}
              role="menu"
              style={popoverStyle}
            >
          {managerOpen ? (
            <div className="channel-manager">
              <div className="channel-manager-head">
                <button onClick={() => setManagerOpen(false)} type="button">
                  <ChevronLeft size={14} />
                  Assign channel
                </button>
                <strong>Channel manager</strong>
              </div>
              <div className="channel-manager-list">
                {projects.filter((item) => item.kind !== "company").map((item) => {
                  const draft = projectDrafts[item.project_id] ?? { name: item.name, color: item.color ?? channelPalette[0] };
                  return (
                    <form className="channel-manager-row channel-manager-edit-row" key={item.project_id} onSubmit={(event) => submitProjectDraft(event, item)}>
                      <span style={{ color: draft.color }}>#</span>
                      <input aria-label={`${item.name} channel name`} value={draft.name} onChange={(event) => changeProjectDraft(item.project_id, { name: event.target.value })} />
                      <div className="channel-color-swatches" aria-label={`${item.name} channel color`}>
                        {channelPalette.map((itemColor) => (
                          <button
                            aria-label={`Set ${item.name} color ${itemColor}`}
                            className={draft.color === itemColor ? "active" : ""}
                            key={itemColor}
                            onClick={() => changeProjectDraft(item.project_id, { color: itemColor })}
                            style={{ background: itemColor }}
                            type="button"
                          />
                        ))}
                      </div>
                      <button className="channel-save-button" aria-label={`Save ${item.name} channel`} type="submit">
                        <Check size={14} />
                      </button>
                      {onDeleteProject ? (
                        <button
                          aria-label={`Delete ${item.name} channel`}
                          className="channel-delete-button"
                          onClick={() => {
                            if (window.confirm(`#${item.name} 채널을 삭제할까요? 이 채널에 속한 작업은 미지정으로 바뀝니다.`)) {
                              void onDeleteProject(item.project_id);
                            }
                          }}
                          type="button"
                        >
                          <Trash2 size={14} />
                        </button>
                      ) : null}
                    </form>
                  );
                })}
              </div>
              <form className="channel-create-form" onSubmit={submitChannel}>
                <Plus size={15} />
                <input autoFocus placeholder="New channel" value={channelDraft} onChange={(event) => setChannelDraft(event.target.value)} />
                <button aria-label="Create channel" type="submit">
                  <Check size={14} />
                </button>
              </form>
              {hasCompanyChannels ? (
                <div className="channel-manager-note">
                  회사 프로젝트 채널은 담당에 따라 자동으로 관리돼요(여기서 수정·삭제 불가).
                </div>
              ) : null}
            </div>
          ) : (
            <>
              <div className="channel-popover-head">
                <span>Assign to channel:</span>
              </div>
              {showSearch ? (
                <input autoFocus aria-label="Search channels" className="channel-search" placeholder="Search..." value={query} onChange={(event) => setQuery(event.target.value)} />
              ) : null}
              <div className="channel-options">
                {showUnassigned ? (
                  <button
                    aria-checked={!project}
                    className={`channel-option ${project ? "" : "selected"}`}
                    onClick={() => {
                      void onSelect(null, null);
                      setOpen(false);
                    }}
                    role="menuitemradio"
                    type="button"
                  >
                    <span style={{ color: "#9a9ba0" }}>#</span>
                    <strong>Unassigned</strong>
                    {!project ? <Check size={18} /> : null}
                  </button>
                ) : null}
                {hasCompanyChannels && personalProjects.length ? (
                  <div className="channel-group-label">개인 채널</div>
                ) : null}
                {personalProjects.map((item) => (
                  <button
                    aria-checked={project?.project_id === item.project_id}
                    className={`channel-option ${project?.project_id === item.project_id ? "selected" : ""}`}
                    key={item.project_id}
                    onClick={() => chooseChannel(item)}
                    role="menuitemradio"
                    type="button"
                  >
                    <span style={{ color: item.color ?? "#9a9ba0" }}>#</span>
                    <strong>{item.name}</strong>
                    {project?.project_id === item.project_id ? <Check size={18} /> : null}
                  </button>
                ))}
                {companyProjects.length ? (
                  <div className="channel-group-label">회사 프로젝트 · 회사 공개</div>
                ) : null}
                {companyProjects.map((item) => (
                  <button
                    aria-checked={project?.project_id === item.project_id}
                    className={`channel-option company ${project?.project_id === item.project_id ? "selected" : ""}`}
                    key={item.project_id}
                    onClick={() => chooseChannel(item)}
                    role="menuitemradio"
                    title="회사 프로젝트 채널 · 회사 구성원 전체에게 공개"
                    type="button"
                  >
                    <span style={{ color: item.color ?? "#9a9ba0" }}>#</span>
                    <strong>{item.name}</strong>
                    <span className="channel-company-tag">회사</span>
                    {project?.project_id === item.project_id ? <Check size={18} /> : null}
                  </button>
                ))}
              </div>
              {onUpdateProject ? (
                <button className="channel-manage-button" onClick={() => setManagerOpen(true)} type="button">
                  Manage channels
                </button>
              ) : null}
            </>
          )}
            </div>,
            portalRoot,
          )
        : null}
    </div>
  );
}

function BacklogPanel({
  activeMobile,
  activeBucketId,
  activeOverId,
  activeTask,
  bucketDrafts,
  buckets,
  filterLabel,
  instantPlacementTaskIds,
  hiddenSubtasks,
  projects,
  tasksByBucket,
  timezone,
  realToday,
  nowEpoch,
  onCollapsePanel,
  onDraftChange,
  onBucketToggle,
  onOpenTask,
  onOpenTaskMenu,
  onSetActiveBucket,
  onSubmit,
  onTaskArchive,
  onTaskProject,
  onTaskProjectCreate,
  onTaskProjectUpdate,
  onTaskProjectDelete,
  onTaskToggle,
  onSubtaskToggle,
}: {
  activeMobile: boolean;
  activeBucketId: string | null;
  activeOverId: string | null;
  activeTask: PersonalBoardTask | null;
  bucketDrafts: Record<string, string>;
  buckets: PersonalBoardBacklogBucket[];
  filterLabel: string;
  instantPlacementTaskIds: ReadonlySet<string>;
  hiddenSubtasks: ReadonlySet<string>;
  projects: PersonalBoardProject[];
  tasksByBucket: Map<string, PersonalBoardTask[]>;
  timezone: string;
  realToday: string;
  nowEpoch: number;
  onCollapsePanel: () => void;
  onDraftChange: (bucketId: string, value: string) => void;
  onBucketToggle: (bucket: PersonalBoardBacklogBucket) => void;
  onOpenTask: (taskId: string) => void;
  onOpenTaskMenu: (task: PersonalBoardTask, x: number, y: number) => void;
  onSetActiveBucket: (bucketId: string | null) => void;
  onSubmit: (event: FormEvent, bucketId: string) => void;
  onTaskArchive: (task: PersonalBoardTask) => void;
  onTaskProject: (
    task: PersonalBoardTask,
    projectId: string | null,
    project?: PersonalBoardProject | null,
  ) => void | Promise<void>;
  onTaskProjectCreate: (name: string) => Promise<PersonalBoardProject | null>;
  onTaskProjectUpdate: (
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) => Promise<PersonalBoardProject | null>;
  onTaskProjectDelete: (projectId: string) => Promise<boolean>;
  onTaskToggle: (task: PersonalBoardTask) => void;
  onSubtaskToggle: (task: PersonalBoardTask, subtaskId: string, completed: boolean) => void;
}) {
  const [backlogView, setBacklogView] = useState<"buckets" | "channels" | "insights">("buckets");
  const allBacklogTasks = useMemo(() => [...tasksByBucket.values()].flat(), [tasksByBucket]);
  const channelGroups = useMemo(() => {
    const groups = new Map<string, { id: string; name: string; color: string; tasks: PersonalBoardTask[] }>();
    for (const task of allBacklogTasks) {
      const id = task.project?.project_id ?? "unassigned";
      const group = groups.get(id) ?? {
        id,
        name: task.project?.name ?? "Unassigned",
        color: task.project?.color ?? "#9a9ba0",
        tasks: [],
      };
      group.tasks.push(task);
      groups.set(id, group);
    }
    return [...groups.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [allBacklogTasks]);
  const completedCount = allBacklogTasks.filter((task) => task.completed).length;
  const populatedBucketCount = [...tasksByBucket.values()].filter((tasks) => tasks.length > 0).length;

  function renderTask(task: PersonalBoardTask) {
    return (
      <Fragment key={task.task_id}>
        {activeTask && activeOverId === `task:${task.task_id}` && activeTask.task_id !== task.task_id ? (
          <TaskDragPreview dropTarget task={activeTask} />
        ) : null}
        <TaskCard
          instantPlacement={instantPlacementTaskIds.has(task.task_id)}
          subtasksHidden={hiddenSubtasks.has(task.task_id)}
          projects={projects}
          task={task}
          timezone={timezone}
          realToday={realToday}
          nowEpoch={nowEpoch}
          onArchive={onTaskArchive}
          onOpen={onOpenTask}
          onOpenMenu={onOpenTaskMenu}
          onProject={(projectId, project) => void onTaskProject(task, projectId, project)}
          onProjectCreate={onTaskProjectCreate}
          onProjectUpdate={onTaskProjectUpdate}
          onProjectDelete={onTaskProjectDelete}
          onToggle={onTaskToggle}
          onSubtaskToggle={onSubtaskToggle}
        />
      </Fragment>
    );
  }

  return (
    <aside className={`backlog-panel ${activeMobile ? "active-mobile" : ""}`}>
      <div className="backlog-filter">{filterLabel === filterLabels.all ? "All backlog tasks" : filterLabel}</div>
      <header className="backlog-header">
        <div>
          <h2>Backlog</h2>
          <div className="backlog-view-buttons">
            <button aria-label="Group backlog by channel" aria-pressed={backlogView === "channels"} className={backlogView === "channels" ? "active" : ""} onClick={() => setBacklogView(backlogView === "channels" ? "buckets" : "channels")} type="button">
              <Hash size={15} />
            </button>
            <button aria-label="Show backlog analytics" aria-pressed={backlogView === "insights"} className={backlogView === "insights" ? "active" : ""} onClick={() => setBacklogView(backlogView === "insights" ? "buckets" : "insights")} type="button">
              <BarChart3 size={14} />
            </button>
          </div>
        </div>
        <button onClick={onCollapsePanel} aria-label="Collapse backlog" type="button">
          <PanelRightClose size={16} />
        </button>
      </header>

      {backlogView === "buckets" ? (
        <div className="backlog-buckets">
          {buckets.map((bucket) => (
              <BacklogBucketSection
                active={activeBucketId === bucket.bucket_id}
                activeOverId={activeOverId}
                activeTask={activeTask}
                bucket={bucket}
                draft={bucketDrafts[bucket.bucket_id] ?? ""}
                instantPlacementTaskIds={instantPlacementTaskIds}
                hiddenSubtasks={hiddenSubtasks}
                key={bucket.bucket_id}
                projects={projects}
              tasks={tasksByBucket.get(bucket.bucket_id) ?? []}
              timezone={timezone}
              realToday={realToday}
              nowEpoch={nowEpoch}
              onDraftChange={(value) => onDraftChange(bucket.bucket_id, value)}
              onBucketToggle={onBucketToggle}
              onOpenTask={onOpenTask}
              onOpenTaskMenu={onOpenTaskMenu}
              onSetActive={(active) => onSetActiveBucket(active ? bucket.bucket_id : null)}
              onSubmit={(event) => onSubmit(event, bucket.bucket_id)}
              onTaskArchive={onTaskArchive}
              onTaskProject={onTaskProject}
              onTaskProjectCreate={onTaskProjectCreate}
              onTaskProjectUpdate={onTaskProjectUpdate}
              onTaskProjectDelete={onTaskProjectDelete}
              onTaskToggle={onTaskToggle}
              onSubtaskToggle={onSubtaskToggle}
            />
          ))}
        </div>
      ) : null}
      {backlogView === "channels" ? (
        <div className="backlog-channel-view">
          {channelGroups.length > 0 ? (
            channelGroups.map((group) => (
              <section className="backlog-channel-section" key={group.id}>
                <div className="backlog-channel-title">
                  <span style={{ color: group.color }}>#</span>
                  <strong>{group.name}</strong>
                  <small>{group.tasks.length}</small>
                </div>
                <div className="bucket-task-list">{group.tasks.map(renderTask)}</div>
              </section>
            ))
          ) : (
            <div className="backlog-empty">No backlog tasks yet</div>
          )}
        </div>
      ) : null}
      {backlogView === "insights" ? (
        <div className="backlog-insights">
          <div className="backlog-stat-card">
            <span>Open</span>
            <strong>{allBacklogTasks.length - completedCount}</strong>
          </div>
          <div className="backlog-stat-card">
            <span>Done</span>
            <strong>{completedCount}</strong>
          </div>
          <div className="backlog-stat-card">
            <span>Buckets</span>
            <strong>{populatedBucketCount}/{buckets.length}</strong>
          </div>
          <div className="backlog-stat-card">
            <span>Channels</span>
            <strong>{channelGroups.length}</strong>
          </div>
        </div>
      ) : null}
    </aside>
  );
}

function BacklogBucketSection({
  active,
  activeOverId,
  activeTask,
  bucket,
  draft,
  instantPlacementTaskIds,
  hiddenSubtasks,
  projects,
  tasks,
  timezone,
  realToday,
  nowEpoch,
  onDraftChange,
  onBucketToggle,
  onOpenTask,
  onOpenTaskMenu,
  onSetActive,
  onSubmit,
  onTaskArchive,
  onTaskProject,
  onTaskProjectCreate,
  onTaskProjectUpdate,
  onTaskProjectDelete,
  onTaskToggle,
  onSubtaskToggle,
}: {
  active: boolean;
  activeOverId: string | null;
  activeTask: PersonalBoardTask | null;
  bucket: PersonalBoardBacklogBucket;
  draft: string;
  instantPlacementTaskIds: ReadonlySet<string>;
  hiddenSubtasks: ReadonlySet<string>;
  projects: PersonalBoardProject[];
  tasks: PersonalBoardTask[];
  timezone: string;
  realToday: string;
  nowEpoch: number;
  onDraftChange: (value: string) => void;
  onBucketToggle: (bucket: PersonalBoardBacklogBucket) => void;
  onOpenTask: (taskId: string) => void;
  onOpenTaskMenu: (task: PersonalBoardTask, x: number, y: number) => void;
  onSetActive: (active: boolean) => void;
  onSubmit: (event: FormEvent) => void;
  onTaskArchive: (task: PersonalBoardTask) => void;
  onTaskProject: (
    task: PersonalBoardTask,
    projectId: string | null,
    project?: PersonalBoardProject | null,
  ) => void | Promise<void>;
  onTaskProjectCreate: (name: string) => Promise<PersonalBoardProject | null>;
  onTaskProjectUpdate: (
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) => Promise<PersonalBoardProject | null>;
  onTaskProjectDelete: (projectId: string) => Promise<boolean>;
  onTaskToggle: (task: PersonalBoardTask) => void;
  onSubtaskToggle: (task: PersonalBoardTask, subtaskId: string, completed: boolean) => void;
}) {
  const { isOver, setNodeRef } = useDroppable({ id: `backlog:${bucket.bucket_id}` });
  const showEndDropPreview = Boolean(activeTask && activeOverId === `backlog:${bucket.bucket_id}` && activeTask.task_id !== tasks.at(-1)?.task_id);

  return (
    <section className={`backlog-bucket ${bucket.collapsed ? "collapsed" : ""} ${isOver ? "drop-over" : ""}`} data-drop-id={`backlog:${bucket.bucket_id}`} ref={setNodeRef}>
      <div className="backlog-bucket-title">
        <button className="bucket-collapse-button bucket-badge-button" style={{ "--bucket-color": bucket.color } as CSSProperties} aria-label={`${bucket.label} bucket`} onClick={() => onBucketToggle(bucket)} type="button">
          {bucket.badge}
        </button>
        <strong>{bucket.label}</strong>
        <button onClick={() => onSetActive(!active)} aria-label={`Add to ${bucket.label}`} type="button">
          <Plus size={17} />
        </button>
      </div>
      {active && !bucket.collapsed ? (
        <form className="bucket-add-row" onSubmit={onSubmit}>
          <input value={draft} autoFocus placeholder="Add backlog task" onChange={(event) => onDraftChange(event.target.value)} />
          <button type="submit">
            <Plus size={15} />
          </button>
        </form>
      ) : null}
      {!bucket.collapsed ? (
        <div className="bucket-task-list">
          {tasks.map((task) => (
            <Fragment key={task.task_id}>
              {activeTask && activeOverId === `task:${task.task_id}` && activeTask.task_id !== task.task_id ? (
                <TaskDragPreview dropTarget task={activeTask} />
              ) : null}
              <TaskCard
                instantPlacement={instantPlacementTaskIds.has(task.task_id)}
                subtasksHidden={hiddenSubtasks.has(task.task_id)}
                projects={projects}
                task={task}
                timezone={timezone}
                realToday={realToday}
                nowEpoch={nowEpoch}
                onArchive={onTaskArchive}
                onOpen={onOpenTask}
                onOpenMenu={onOpenTaskMenu}
                onProject={(projectId, project) => void onTaskProject(task, projectId, project)}
                onProjectCreate={onTaskProjectCreate}
                onProjectUpdate={onTaskProjectUpdate}
                onProjectDelete={onTaskProjectDelete}
                onToggle={onTaskToggle}
                onSubtaskToggle={onSubtaskToggle}
              />
            </Fragment>
          ))}
          {activeTask && showEndDropPreview ? <TaskDragPreview dropTarget task={activeTask} /> : null}
        </div>
      ) : null}
    </section>
  );
}

function TaskDetailSubtaskRow({
  subtask,
  onToggle,
  onRename,
  onDelete,
  onEnterNext,
}: {
  subtask: PersonalBoardSubtask;
  onToggle: (completed: boolean) => void;
  onRename: (title: string) => void;
  onDelete: () => void;
  onEnterNext: () => void;
}) {
  // 클릭-편집 토글 없이 항상 일반 텍스트 입력처럼 바로 수정 가능하게 한다.
  const [draft, setDraft] = useState(subtask.title);
  const [synced, setSynced] = useState(subtask.title);
  // 외부에서 title이 바뀌면(다른 경로 수정) 로컬 draft를 다시 맞춘다.
  if (subtask.title !== synced) {
    setSynced(subtask.title);
    setDraft(subtask.title);
  }

  // draft/원본/마지막 전송값을 ref로도 들고 있어, 모달이 바깥 클릭으로 먼저 unmount돼
  // input의 onBlur가 안 떠도 언마운트 flush로 저장한다. lastSentRef가 onBlur와
  // 언마운트 flush의 중복 저장을 막는다. (ref 갱신은 lint 때문에 effect에서)
  const draftRef = useRef(draft);
  const titleRef = useRef(subtask.title);
  const lastSentRef = useRef(subtask.title);
  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);
  useEffect(() => {
    titleRef.current = subtask.title;
    lastSentRef.current = subtask.title;
  }, [subtask.title]);

  function commit() {
    const clean = draftRef.current.trim();
    // 이미 보낸 값이거나 원본과 같으면 아무 것도 안 한다(중복 저장 방지).
    if (clean === lastSentRef.current || clean === titleRef.current) {
      if (draftRef.current !== clean) setDraft(clean);
      lastSentRef.current = clean || titleRef.current;
      return;
    }
    // 내용을 모두 지우고 확정하면 그 서브태스크를 삭제한다.
    if (!clean) {
      lastSentRef.current = "";
      onDelete();
      return;
    }
    lastSentRef.current = clean;
    onRename(clean);
  }

  // 모달 닫힘(unmount) 시에도 마지막 편집을 저장한다(최신 commit을 ref로 호출).
  const commitRef = useRef(commit);
  useEffect(() => {
    commitRef.current = commit;
  });
  useEffect(() => () => commitRef.current?.(), []);

  return (
    <div className="task-detail-subtask-row">
      <button
        type="button"
        className={`task-detail-subtask-check ${subtask.completed ? "checked" : ""}`}
        aria-label={subtask.completed ? "Mark subtask open" : "Mark subtask done"}
        onClick={() => onToggle(!subtask.completed)}
      >
        <Check size={13} />
      </button>
      <input
        className={`task-detail-subtask-edit ${subtask.completed ? "completed" : ""}`}
        aria-label="Subtask"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
            // 이어서 추가: 새 서브태스크 입력칸으로 이동.
            onEnterNext();
          } else if (event.key === "Backspace" && draft === "") {
            // 빈 상태에서 Backspace면 그 서브태스크 삭제.
            event.preventDefault();
            onDelete();
          }
        }}
      />
    </div>
  );
}

const DUE_WEEKDAYS_KO = ["일", "월", "화", "수", "목", "금", "토"] as const;
const DUE_MINUTE_STEPS = [0, 10, 20, 30, 40, 50] as const;

// Calendar popover for picking a due date. Clicking a day selects + closes.
function DueDatePopover({
  value,
  today,
  onSelect,
}: {
  value: string | null;
  today: string;
  onSelect: (dateKey: string) => void;
}) {
  const anchor = value ?? today;
  const [viewYear, setViewYear] = useState(() => Number(anchor.slice(0, 4)));
  const [viewMonth, setViewMonth] = useState(() => Number(anchor.slice(5, 7)) - 1);
  const firstWeekday = new Date(viewYear, viewMonth, 1).getDay();
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
  const cells: (number | null)[] = [
    ...Array.from({ length: firstWeekday }, () => null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];
  function shiftMonth(delta: number) {
    const d = new Date(viewYear, viewMonth + delta, 1);
    setViewYear(d.getFullYear());
    setViewMonth(d.getMonth());
  }
  return (
    <div className="task-due-popover task-due-cal" role="dialog" aria-label="마감일 선택">
      <div className="task-due-cal-head">
        <button type="button" onClick={() => shiftMonth(-1)} aria-label="이전 달">
          <ChevronLeft size={16} />
        </button>
        <strong>{`${viewYear}년 ${viewMonth + 1}월`}</strong>
        <button type="button" onClick={() => shiftMonth(1)} aria-label="다음 달">
          <ChevronRight size={16} />
        </button>
      </div>
      <div className="task-due-cal-grid">
        {DUE_WEEKDAYS_KO.map((w) => (
          <span key={w} className="task-due-cal-w">
            {w}
          </span>
        ))}
        {cells.map((day, index) => {
          if (day == null) return <span key={`blank-${index}`} className="task-due-cal-blank" />;
          const key = `${viewYear}-${pad(viewMonth + 1)}-${pad(day)}`;
          return (
            <button
              key={key}
              type="button"
              className={`task-due-cal-day ${key === value ? "selected" : ""} ${key === today ? "today" : ""}`}
              aria-label={key}
              aria-pressed={key === value}
              onClick={() => onSelect(key)}
            >
              {day}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Two-column time picker: hours on the left, 10-minute steps on the right. Each
// click commits the combined HH:MM; the popover stays open so both parts can be set.
function DueTimePopover({ value, onSelect }: { value: string | null; onSelect: (hhmm: string) => void }) {
  const hour = value ? Number(value.slice(0, 2)) : 9;
  const minute = value ? Number(value.slice(3, 5)) : 0;
  const selectedStep = DUE_MINUTE_STEPS.includes(minute as (typeof DUE_MINUTE_STEPS)[number])
    ? minute
    : (Math.round(minute / 10) * 10) % 60;
  const hourRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    hourRef.current?.scrollIntoView({ block: "center" });
  }, []);
  return (
    <div className="task-due-popover task-due-time-pop" role="dialog" aria-label="마감 시간 선택">
      <div className="task-due-time-col" role="listbox" aria-label="시">
        {Array.from({ length: 24 }, (_, h) => (
          <button
            key={h}
            ref={h === hour ? hourRef : undefined}
            type="button"
            className={`task-due-time-cell ${h === hour ? "selected" : ""}`}
            aria-pressed={h === hour}
            onClick={() => onSelect(`${pad(h)}:${pad(value ? minute : 0)}`)}
          >
            {pad(h)}시
          </button>
        ))}
      </div>
      <div className="task-due-time-col" role="listbox" aria-label="분">
        {DUE_MINUTE_STEPS.map((m) => (
          <button
            key={m}
            type="button"
            className={`task-due-time-cell ${m === selectedStep ? "selected" : ""}`}
            aria-pressed={m === selectedStep}
            onClick={() => onSelect(`${pad(hour)}:${pad(m)}`)}
          >
            {pad(m)}분
          </button>
        ))}
      </div>
    </div>
  );
}

// 노트(티켓·주간목표)는 비편집 상태에선 링크를 클릭할 수 있는 읽기뷰로 보여주고,
// 클릭/포커스하면 textarea 로 전환해 편집한다. textarea 는 그 자체로 링크를 누를 수
// 없어 URL 을 적어도 눌러 이동할 수 없던 문제를 이렇게 해결한다. 저장 배선(onChange
// 디바운스 저장 · onCommit blur flush)은 기존 그대로 위임하므로 저장 동작은 불변이다.
function NoteField({
  value,
  placeholder,
  ariaLabel,
  editClassName,
  readClassName,
  rows,
  onChange,
  onCommit,
}: {
  value: string;
  placeholder: string;
  ariaLabel: string;
  editClassName?: string;
  readClassName?: string;
  rows?: number;
  onChange: (value: string) => void;
  onCommit: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!editing) return;
    const el = textareaRef.current;
    if (!el) return;
    el.focus();
    // 클릭/키보드로 편집 진입 시 커서를 끝에 둔다(텍스트 전체 선택 방지).
    const end = el.value.length;
    el.setSelectionRange(end, end);
  }, [editing]);

  if (editing) {
    return (
      <textarea
        ref={textareaRef}
        aria-label={ariaLabel}
        className={editClassName}
        placeholder={placeholder}
        rows={rows}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onBlur={() => {
          setEditing(false);
          onCommit();
        }}
      />
    );
  }

  const isEmpty = value.trim().length === 0;
  const enterEdit = (event: { target: EventTarget | null }) => {
    // 링크를 눌렀을 땐 편집으로 들어가지 않고 링크가 열리게 둔다.
    if (event.target instanceof Element && event.target.closest("a")) return false;
    setEditing(true);
    return true;
  };
  return (
    <div
      className={`note-readview${readClassName ? ` ${readClassName}` : ""}${isEmpty ? " empty" : ""}`}
      tabIndex={0}
      title="클릭해 편집"
      aria-label={isEmpty ? ariaLabel : undefined}
      onClick={enterEdit}
      onKeyDown={(event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        // 포커스가 링크에 있으면 그 링크를 열게 두고, 아니면 편집 진입(스페이스 스크롤 방지).
        if (event.target instanceof Element && event.target.closest("a")) return;
        event.preventDefault();
        setEditing(true);
      }}
    >
      {isEmpty ? (
        <span className="note-readview-placeholder">{placeholder}</span>
      ) : (
        <Linkify text={value} />
      )}
    </div>
  );
}

function TaskDetailPopup({
  buckets,
  comments,
  days,
  noteText,
  projects,
  task,
  timezone,
  realToday,
  nowEpoch,
  onArchive,
  onCommentAdd,
  onCreateProject,
  onClose,
  onMoveTask,
  onNoteTextChange,
  onNoteBlur,
  onPriority,
  onProject,
  onProjectUpdate,
  onProjectDelete,
  onSubtaskAdd,
  onSubtaskToggle,
  onSubtaskRename,
  onSubtaskDelete,
  onSchedule,
  onDue,
  onTitle,
  onToggle,
}: {
  buckets: PersonalBoardBacklogBucket[];
  comments: PersonalTaskComment[];
  days: PersonalBoardDay[];
  noteText: string;
  projects: PersonalBoardProject[];
  task: PersonalBoardTask;
  timezone: string;
  realToday: string;
  nowEpoch: number;
  onArchive: (task: PersonalBoardTask) => void;
  onCommentAdd: (comment: string) => void;
  onCreateProject: (name: string) => Promise<PersonalBoardProject | null>;
  onClose: () => void;
  onMoveTask: (task: PersonalBoardTask, destination: MoveDestination) => void;
  onNoteTextChange: (value: string) => void;
  onNoteBlur: () => void;
  onPriority: (task: PersonalBoardTask, priority: Priority) => void;
  onProject: (
    task: PersonalBoardTask,
    projectId: string | null,
    project?: PersonalBoardProject | null,
  ) => void | Promise<void>;
  onProjectUpdate: (
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) => Promise<PersonalBoardProject | null>;
  onProjectDelete: (projectId: string) => Promise<boolean>;
  onSubtaskAdd: (task: PersonalBoardTask, title: string) => void;
  onSubtaskToggle: (task: PersonalBoardTask, subtaskId: string, completed: boolean) => void;
  onSubtaskRename: (task: PersonalBoardTask, subtaskId: string, title: string) => void;
  onSubtaskDelete: (task: PersonalBoardTask, subtaskId: string) => void;
  onSchedule: (task: PersonalBoardTask, start: string | null, end: string | null) => void;
  onDue: (task: PersonalBoardTask, dueDate: string | null, dueTime: string | null) => void;
  onTitle: (task: PersonalBoardTask, title: string) => void;
  onToggle: (task: PersonalBoardTask) => void;
}) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [actionMenuOpen, setActionMenuOpen] = useState(false);
  const [priorityMenuOpen, setPriorityMenuOpen] = useState(false);
  const [dueDatePickerOpen, setDueDatePickerOpen] = useState(false);
  const [dueTimePickerOpen, setDueTimePickerOpen] = useState(false);
  const [titleDraft, setTitleDraft] = useState(task.title);
  // 제목도 서브태스크처럼 ref에 동기 보관한다. 엔터/blur 없이 모달을 닫아도(바깥 클릭으로
  // input이 먼저 unmount되면 onBlur가 안 뜬다) 언마운트 flush로 저장하기 위함이다.
  const titleDraftRef = useRef(task.title);
  const lastSentTitleRef = useRef(task.title);
  const [subtaskDraft, setSubtaskDraft] = useState("");
  const [commentDraft, setCommentDraft] = useState("");
  const actionMenuRef = useRef<HTMLDivElement | null>(null);
  const priorityMenuRef = useRef<HTMLDivElement | null>(null);
  const dueDateCtrlRef = useRef<HTMLDivElement | null>(null);
  const dueTimeCtrlRef = useRef<HTMLDivElement | null>(null);
  const subtaskInputRef = useRef<HTMLInputElement | null>(null);
  // 입력 중인 서브태스크는 ref에도 동기 보관한다. 모달이 바깥 클릭으로 먼저 unmount되면
  // input의 onBlur가 안 도므로(focus된 요소가 사라짐) 입력이 유실되는데, 언마운트 시에도
  // 이 ref에서 flush해 저장한다. commit이 ref를 동기로 비우므로 중복 추가는 안 된다.
  const subtaskDraftRef = useRef("");
  const startLabel = task.scheduled_date ? monthDayLabel(task.scheduled_date) : "Backlog";
  const placementValue = task.scheduled_date ? `day:${task.scheduled_date}` : `backlog:${task.backlog_bucket_id ?? ""}`;
  const dueChip = dueChipInfo(task.due_date, task.due_time, timezone, realToday, nowEpoch, task.completed);
  const userName = "Local User";

  // Close the due date/time popovers on outside click or Escape. Escape is captured
  // so it closes the popover before the modal's own Escape handler can close the modal.
  useEffect(() => {
    if (!dueDatePickerOpen && !dueTimePickerOpen) return;
    function onPointerDown(event: globalThis.PointerEvent) {
      if (!(event.target instanceof Node)) return;
      if (dueDatePickerOpen && !dueDateCtrlRef.current?.contains(event.target)) setDueDatePickerOpen(false);
      if (dueTimePickerOpen && !dueTimeCtrlRef.current?.contains(event.target)) setDueTimePickerOpen(false);
    }
    function onKeyDownCapture(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setDueDatePickerOpen(false);
      setDueTimePickerOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDownCapture, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDownCapture, true);
    };
  }, [dueDatePickerOpen, dueTimePickerOpen]);

  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (priorityMenuOpen) {
        event.preventDefault();
        setPriorityMenuOpen(false);
        return;
      }
      if (actionMenuOpen) {
        event.preventDefault();
        setActionMenuOpen(false);
        return;
      }
      event.preventDefault();
      onClose();
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [actionMenuOpen, onClose, priorityMenuOpen]);

  useEffect(() => {
    if (!actionMenuOpen && !priorityMenuOpen) return;
    function closeOnOutsidePointer(event: globalThis.PointerEvent) {
      if (!(event.target instanceof Node)) return;
      if (!actionMenuRef.current?.contains(event.target)) setActionMenuOpen(false);
      if (!priorityMenuRef.current?.contains(event.target)) setPriorityMenuOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [actionMenuOpen, priorityMenuOpen]);

  // 모달 닫힘(unmount) 시에도 입력 중이던 서브태스크를 저장한다. 항상 최신 commit을
  // 가리키는 ref를 통해 호출해 stale closure를 피한다(ref 갱신은 effect에서).
  const commitSubtaskRef = useRef(commitSubtaskDraft);
  useEffect(() => {
    commitSubtaskRef.current = commitSubtaskDraft;
  });
  useEffect(() => () => commitSubtaskRef.current?.(), []);

  // 제목도 같은 방식으로 언마운트 시 flush한다(노션처럼 저장 버튼/엔터 없이 자동 저장).
  const commitTitleRef = useRef(commitTitleDraft);
  useEffect(() => {
    commitTitleRef.current = commitTitleDraft;
  });
  useEffect(() => () => commitTitleRef.current?.(), []);

  function focusSubtaskInput() {
    requestAnimationFrame(() => {
      subtaskInputRef.current?.scrollIntoView({ block: "nearest" });
      subtaskInputRef.current?.focus();
    });
  }

  function submitSubtask(event: FormEvent) {
    event.preventDefault();
    commitSubtaskDraft();
    requestAnimationFrame(() => subtaskInputRef.current?.focus());
  }

  // 포커스가 빠져나가도(다른 곳 클릭) 입력한 내용이 자동 추가되게 한다. ref에서 읽고
  // 동기로 비워, onBlur와 언마운트 flush가 둘 다 불려도 중복 추가되지 않는다.
  function commitSubtaskDraft() {
    const clean = subtaskDraftRef.current.trim();
    if (!clean) return;
    subtaskDraftRef.current = "";
    setSubtaskDraft("");
    onSubtaskAdd(task, clean);
  }

  // 제목을 엔터 없이도 저장한다(노션처럼). 포커스가 빠지거나 모달이 닫혀도 ref에서 읽어
  // flush하고, lastSentTitleRef로 onBlur와 언마운트 flush의 중복 저장을 막는다. 빈 제목은
  // 무시해 기존 제목을 지우지 않는다.
  function commitTitleDraft() {
    const clean = titleDraftRef.current.trim();
    if (!clean || clean === lastSentTitleRef.current) return;
    lastSentTitleRef.current = clean;
    onTitle(task, clean);
  }

  function submitComment(event: FormEvent) {
    event.preventDefault();
    const clean = commentDraft.trim();
    if (!clean) return;
    onCommentAdd(clean);
    setCommentDraft("");
  }

  return (
    <div
      className={`task-popup-layer ${isExpanded ? "expanded" : ""}`}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section aria-label="Task details" aria-modal="true" className={`task-detail-popup ${isExpanded ? "expanded" : ""}`} data-personal-task-detail={task.task_id} role="dialog">
        <header className="task-detail-topbar">
          <div className="task-detail-channel">
            <span>Channel</span>
            <ChannelPicker
              project={task.project}
              projects={projects}
              sourceLabel={task.source_label ?? task.source_kind}
              onCreateProject={onCreateProject}
              onSelect={(projectId, project) => void onProject(task, projectId, project)}
              onUpdateProject={onProjectUpdate}
              onDeleteProject={onProjectDelete}
            />
          </div>
          <div className="task-detail-top-actions">
            <div className="task-detail-priority" ref={priorityMenuRef}>
              <button aria-expanded={priorityMenuOpen} aria-haspopup="menu" aria-label="Priority" className="task-detail-icon-select task-detail-priority-button" onClick={() => setPriorityMenuOpen((open) => !open)} type="button">
                <Flag size={17} />
                <span>{priorityLabels[asPriority(task.priority)]}</span>
              </button>
              {priorityMenuOpen ? (
                <div className="task-detail-priority-menu" role="menu">
                  {(Object.keys(priorityLabels) as Priority[]).map((value) => (
                    <button
                      aria-checked={task.priority === value}
                      className={task.priority === value ? "selected" : ""}
                      key={value}
                      onClick={() => {
                        onPriority(task, value);
                        setPriorityMenuOpen(false);
                      }}
                      role="menuitemradio"
                      type="button"
                    >
                      <span>{priorityLabels[value]}</span>
                      {task.priority === value ? <Check size={14} /> : null}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="task-detail-start">
              <span>Start</span>
              <strong>{startLabel}</strong>
            </div>
            <label className="task-detail-icon-select">
              <CalendarDays size={17} />
              <select
                aria-label="Task placement"
                value={placementValue}
                onChange={(event) => {
                  const value = event.target.value;
                  if (value.startsWith("day:")) {
                    onMoveTask(task, { type: "day", date: value.replace("day:", "") });
                    return;
                  }
                  onMoveTask(task, { type: "backlog", bucketId: value.replace("backlog:", "") });
                }}
              >
                {days.map((day) => (
                  <option key={day.date} value={`day:${day.date}`}>
                    {monthDayLabel(day.date)}
                  </option>
                ))}
                {buckets.map((bucket) => (
                  <option key={bucket.bucket_id} value={`backlog:${bucket.bucket_id}`}>
                    {bucket.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="task-detail-icon-select task-detail-time-range">
              <Clock size={17} />
              <input
                aria-label="시작 시간"
                disabled={!task.scheduled_date}
                type="time"
                value={task.scheduled_time?.slice(0, 5) ?? ""}
                onChange={(event) =>
                  onSchedule(task, event.target.value || null, event.target.value ? task.scheduled_end_time : null)
                }
              />
              <span aria-hidden className="task-detail-time-sep">–</span>
              <input
                aria-label="종료 시간"
                disabled={!task.scheduled_time}
                type="time"
                value={task.scheduled_end_time?.slice(0, 5) ?? ""}
                onChange={(event) => onSchedule(task, task.scheduled_time, event.target.value || null)}
              />
            </label>
            <button className="task-detail-ghost-button" onClick={focusSubtaskInput} type="button">
              <Plus size={17} />
              Subtasks
            </button>
            <div className="task-detail-more" ref={actionMenuRef}>
              <button className="task-detail-icon-button" aria-expanded={actionMenuOpen} aria-label="More task actions" onClick={() => setActionMenuOpen((open) => !open)} type="button">
                <MoreHorizontal size={18} />
              </button>
              {actionMenuOpen ? (
                <div className="task-detail-more-menu" role="menu">
                  <button
                    onClick={() => {
                      setActionMenuOpen(false);
                      onArchive(task);
                    }}
                    role="menuitem"
                    type="button"
                  >
                    <Archive size={15} />
                    Archive
                  </button>
                </div>
              ) : null}
            </div>
            <button className="task-detail-icon-button task-detail-expand-button" aria-label={isExpanded ? "Restore task details" : "Expand task details"} aria-pressed={isExpanded} onClick={() => setIsExpanded((expanded) => !expanded)} type="button">
              {isExpanded ? <Minimize2 size={17} /> : <Maximize2 size={17} />}
            </button>
            <button className="task-detail-icon-button" aria-label="Close task details" onClick={onClose} type="button">
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="task-detail-scroll">
          <section className="task-detail-body">
            <div className="task-detail-title-row">
              <button className={`task-detail-check ${task.completed ? "checked" : ""}`} onClick={() => onToggle(task)} type="button" aria-label={task.completed ? "완료 취소" : "완료"}>
                <Check size={14} />
              </button>
              <input
                aria-label="Task title"
                className="task-detail-title"
                value={titleDraft}
                onBlur={commitTitleDraft}
                onChange={(event) => {
                  setTitleDraft(event.target.value);
                  titleDraftRef.current = event.target.value;
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    event.currentTarget.blur();
                  }
                }}
              />
            </div>

            <div className="task-detail-due-row">
              <div className="task-detail-due-control" ref={dueDateCtrlRef}>
                <button
                  type="button"
                  className={`task-detail-due-field ${dueDatePickerOpen ? "open" : ""} ${task.due_date ? "" : "placeholder"}`}
                  aria-haspopup="dialog"
                  aria-expanded={dueDatePickerOpen}
                  aria-label="마감일"
                  onClick={() => {
                    setDueTimePickerOpen(false);
                    setDueDatePickerOpen((open) => !open);
                  }}
                >
                  <CalendarClock size={15} aria-hidden="true" />
                  <span>{task.due_date ? monthDayLabel(task.due_date) : "마감일"}</span>
                </button>
                {dueDatePickerOpen ? (
                  <DueDatePopover
                    value={task.due_date}
                    today={realToday}
                    onSelect={(dateKey) => {
                      onDue(task, dateKey, task.due_time);
                      setDueDatePickerOpen(false);
                    }}
                  />
                ) : null}
              </div>
              <div className="task-detail-due-control" ref={dueTimeCtrlRef}>
                <button
                  type="button"
                  className={`task-detail-due-time ${dueTimePickerOpen ? "open" : ""} ${task.due_time ? "" : "placeholder"}`}
                  aria-haspopup="dialog"
                  aria-expanded={dueTimePickerOpen}
                  aria-label="마감 시간"
                  onClick={() => {
                    setDueDatePickerOpen(false);
                    // 마감 시간은 마감일이 있어야 성립한다. 예전엔 마감일이 없으면 이 버튼이
                    // disabled라 눌러도 아무 반응이 없어 "시간 선택이 안 된다"고 느꼈다. 이제
                    // 마감일이 없으면 이 할 일의 날짜(없으면 오늘)로 마감일을 먼저 잡고 시간
                    // 선택기를 바로 연다.
                    if (!task.due_date) {
                      void onDue(task, task.scheduled_date ?? realToday, task.due_time);
                    }
                    setDueTimePickerOpen((open) => !open);
                  }}
                >
                  <Clock size={15} aria-hidden="true" />
                  <span>{task.due_time ? task.due_time.slice(0, 5) : "시간"}</span>
                </button>
                {dueTimePickerOpen ? (
                  <DueTimePopover
                    value={task.due_time}
                    onSelect={(hhmm) => onDue(task, task.due_date, hhmm)}
                  />
                ) : null}
              </div>
              {dueChip ? <span className={`task-due-chip tone-${dueChip.tone}`} role="img" aria-label={`${dueChip.text}, ${dueChip.title}`} title={dueChip.title}>{dueChip.text}</span> : null}
              {task.due_date ? (
                <button className="task-detail-due-clear" onClick={() => onDue(task, null, null)} type="button" aria-label="마감일 제거">
                  <X size={13} />
                </button>
              ) : null}
            </div>

            <section className="task-detail-subtasks">
              {task.subtasks.length > 0 ? (
                <div className="task-detail-subtask-list">
                  {task.subtasks.map((subtask) => (
                    <TaskDetailSubtaskRow
                      key={subtask.subtask_id}
                      subtask={subtask}
                      onToggle={(completed) => onSubtaskToggle(task, subtask.subtask_id, completed)}
                      onRename={(title) => onSubtaskRename(task, subtask.subtask_id, title)}
                      onDelete={() => onSubtaskDelete(task, subtask.subtask_id)}
                      onEnterNext={focusSubtaskInput}
                    />
                  ))}
                </div>
              ) : null}
              <form className="task-detail-subtask-form" onSubmit={submitSubtask}>
                <span className="task-detail-subtask-add-icon" aria-hidden="true">
                  <Plus size={12} />
                </span>
                <input aria-label="Add subtask" placeholder="Add subtask" ref={subtaskInputRef} value={subtaskDraft} onChange={(event) => { setSubtaskDraft(event.target.value); subtaskDraftRef.current = event.target.value; }} onBlur={commitSubtaskDraft} />
              </form>
            </section>

            <section className="task-detail-notes task-detail-notes-editable">
              <NoteField
                ariaLabel="Task notes"
                placeholder="Notes..."
                value={noteText}
                onChange={onNoteTextChange}
                onCommit={onNoteBlur}
              />
            </section>
          </section>
        </div>
        <footer className="task-detail-footer">
          <form className="task-detail-comment-row" onSubmit={submitComment}>
            <div className="task-detail-avatar" aria-hidden="true">
              {userName.slice(0, 1)}
            </div>
            <input placeholder="Comment..." value={commentDraft} onChange={(event) => setCommentDraft(event.target.value)} />
            <button aria-label="Attach file" type="button">
              <Paperclip size={19} />
            </button>
          </form>
          {comments.length > 0 ? (
            <div className="task-detail-comments">
              {comments.map((comment) => {
                const author = comment.author_name || userName;
                return (
                  <div className="task-detail-comment" key={comment.comment_id}>
                    <div className="task-detail-avatar" aria-hidden="true">
                      {author.slice(0, 1)}
                    </div>
                    <div>
                      <strong>{author}</strong>
                      <p><Linkify text={comment.body} /></p>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
          <div className="task-detail-activity">
            <div className="activity-row">
              <span className="activity-dot" />
              <span>
                <strong>{userName}</strong> created this · now
              </span>
            </div>
          </div>
        </footer>
      </section>
    </div>
  );
}

function TaskContextMenu({
  task,
  x,
  y,
  hidden,
  onAddToCalendar,
  onMoveToBacklog,
  onToggleSubtasks,
  onDuplicate,
  onDelete,
}: {
  task: PersonalBoardTask;
  x: number;
  y: number;
  hidden: boolean;
  onAddToCalendar: () => void;
  onMoveToBacklog: () => void;
  onToggleSubtasks: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [position, setPosition] = useState({ left: x, top: y });

  useLayoutEffect(() => {
    const node = menuRef.current;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    const margin = 8;
    const left = Math.min(x, window.innerWidth - rect.width - margin);
    const top = Math.min(y, window.innerHeight - rect.height - margin);
    setPosition({ left: Math.max(margin, left), top: Math.max(margin, top) });
  }, [x, y]);

  const hasSubtasks = task.subtasks.length > 0;

  return (
    <div
      className="task-context-menu"
      ref={menuRef}
      role="menu"
      style={{ left: position.left, top: position.top }}
      onPointerDown={(event) => event.stopPropagation()}
      onContextMenu={(event) => event.preventDefault()}
    >
      <button role="menuitem" type="button" onClick={onAddToCalendar}>
        <CalendarPlus size={15} />
        <span>Add to calendar</span>
      </button>
      <button role="menuitem" type="button" onClick={onMoveToBacklog}>
        <Inbox size={15} />
        <span>Move to backlog</span>
      </button>
      <button role="menuitem" type="button" onClick={onToggleSubtasks} disabled={!hasSubtasks}>
        {hidden ? <Eye size={15} /> : <EyeOff size={15} />}
        <span>{hidden ? "Show subtasks" : "Hide subtasks"}</span>
      </button>
      <button role="menuitem" type="button" onClick={onDuplicate}>
        <Copy size={15} />
        <span>Duplicate</span>
      </button>
      <div className="task-context-menu-divider" />
      <button role="menuitem" type="button" className="danger" onClick={onDelete}>
        <Trash2 size={15} />
        <span>Delete</span>
      </button>
    </div>
  );
}

function WeeklyPlanView({
  weekStart,
  objectives,
  loading,
  projects,
  realToday,
  onWeekStartChange,
  onCreate,
  onPatch,
  onDelete,
  onOpenObjective,
  onCreateProject,
  onUpdateProject,
  onDeleteProject,
}: {
  weekStart: string | null;
  objectives: PersonalWeeklyObjective[];
  loading: boolean;
  projects: PersonalBoardProject[];
  realToday: string;
  onWeekStartChange: (weekStart: string) => void;
  onCreate: (title: string) => void | Promise<void>;
  onPatch: (
    objective: PersonalWeeklyObjective,
    patch: { title?: string; completed?: boolean; day_allocations?: DayAllocation[]; project_id?: string | null },
    optimistic?: Partial<PersonalWeeklyObjective>,
  ) => void | Promise<void>;
  onDelete: (objective: PersonalWeeklyObjective) => void | Promise<void>;
  onOpenObjective: (objective: PersonalWeeklyObjective) => void;
  onCreateProject: (name: string) => Promise<PersonalBoardProject | null>;
  onUpdateProject: (
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) => Promise<PersonalBoardProject | null>;
  onDeleteProject: (projectId: string) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState("");
  const start = weekStart ?? sundayOf(realToday);
  const thisWeekStart = sundayOf(realToday);
  const nextWeekStart = sundayOf(addDays(realToday, 7));
  const isThisWeek = start === thisWeekStart;
  const dayKeys = Array.from({ length: 7 }, (_, index) => addDays(start, index));

  function submitDraft(event: FormEvent) {
    event.preventDefault();
    const title = draft.trim();
    if (!title) return;
    void onCreate(title);
    setDraft("");
  }

  return (
    <section className="weekly-view weekly-plan" aria-label="Weekly plan">
      <header className="weekly-header">
        <div className="weekly-header-main">
          <button className="weekly-nav-button" aria-label="Previous week" type="button" onClick={() => onWeekStartChange(addDays(start, -7))}>
            <ChevronLeft size={16} />
          </button>
          <div className="weekly-header-titles">
            <h2>{weekRangeLabel(start)}</h2>
            <span>Routine planner</span>
          </div>
          <button className="weekly-nav-button" aria-label="Next week" type="button" onClick={() => onWeekStartChange(addDays(start, 7))}>
            <ChevronRight size={16} />
          </button>
        </div>
        <div className="weekly-week-toggle">
          <button className={isThisWeek ? "active" : ""} type="button" onClick={() => onWeekStartChange(thisWeekStart)}>
            This week
          </button>
          <button className={start === nextWeekStart ? "active" : ""} type="button" onClick={() => onWeekStartChange(nextWeekStart)}>
            Next week
          </button>
        </div>
      </header>

      <div className="weekly-grid-head">
        <div className="weekly-grid-head-spacer">Objective</div>
        <div className="weekly-day-headers">
          {dayKeys.map((dayKey, index) => (
            <div className={`weekly-day-header ${dayKey === realToday ? "is-today" : ""}`} key={dayKey}>
              <span>{WEEKDAY_SHORT[index]}</span>
              <strong>{dateFromKey(dayKey).getDate()}</strong>
            </div>
          ))}
        </div>
      </div>

      {loading && objectives.length === 0 ? (
        <div className="weekly-empty">Loading objectives…</div>
      ) : objectives.length === 0 ? (
        <div className="weekly-empty">No objectives for this week yet. Add one below.</div>
      ) : (
        <div className="weekly-objective-list">
          {objectives.map((objective) => (
            <ObjectiveRow
              key={objective.objective_id}
              objective={objective}
              dayKeys={dayKeys}
              realToday={realToday}
              projects={projects}
              onPatch={onPatch}
              onDelete={onDelete}
              onOpen={() => onOpenObjective(objective)}
              onCreateProject={onCreateProject}
              onUpdateProject={onUpdateProject}
              onDeleteProject={onDeleteProject}
            />
          ))}
        </div>
      )}

      <form className="weekly-add-objective" onSubmit={submitDraft}>
        <Plus size={16} />
        <input
          aria-label="Add objective"
          placeholder="Add weekly objective"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button type="submit" aria-label="Add objective">
          <Plus size={15} />
        </button>
      </form>
    </section>
  );
}

function ObjectiveRow({
  objective,
  dayKeys,
  realToday,
  projects,
  onPatch,
  onDelete,
  onOpen,
  onCreateProject,
  onUpdateProject,
  onDeleteProject,
}: {
  objective: PersonalWeeklyObjective;
  dayKeys: string[];
  realToday: string;
  projects: PersonalBoardProject[];
  onPatch: (
    objective: PersonalWeeklyObjective,
    patch: { title?: string; completed?: boolean; day_allocations?: DayAllocation[]; project_id?: string | null },
    optimistic?: Partial<PersonalWeeklyObjective>,
  ) => void | Promise<void>;
  onDelete: (objective: PersonalWeeklyObjective) => void | Promise<void>;
  onOpen: () => void;
  onCreateProject: (name: string) => Promise<PersonalBoardProject | null>;
  onUpdateProject: (
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) => Promise<PersonalBoardProject | null>;
  onDeleteProject: (projectId: string) => Promise<boolean>;
}) {
  const [titleDraft, setTitleDraft] = useState(objective.title);
  const [syncedTitle, setSyncedTitle] = useState(objective.title);
  // Re-sync the local draft when the persisted title changes (e.g. after save).
  if (objective.title !== syncedTitle) {
    setSyncedTitle(objective.title);
    setTitleDraft(objective.title);
  }

  function commitTitle() {
    const title = titleDraft.trim();
    if (!title || title === objective.title) {
      setTitleDraft(objective.title);
      return;
    }
    void onPatch(objective, { title }, { title });
  }

  function toggleDayAssignment(weekday: number) {
    const next = setAllocationAssigned(
      objective.day_allocations,
      weekday,
      allocationForWeekday(objective.day_allocations, weekday) == null,
    );
    void onPatch(objective, { day_allocations: next }, { day_allocations: next });
  }

  // 회사 주간계획 담당에서 자동 생성된 '회사 부여' 목표. 제목·프로젝트는 회사 소유라
  // 개인이 못 바꾸고 삭제도 못 한다(요일 분배·완료만). 회사쪽 담당 해제 시 사라진다.
  const isCompany = objective.source_kind === "company_plan";

  return (
    <article className={`objective-row ${objective.completed ? "completed" : ""}`}>
      <div className="objective-main">
        <button
          className={`objective-check ${objective.completed ? "checked" : ""}`}
          aria-label={objective.completed ? "Mark objective open" : "Mark objective done"}
          type="button"
          onClick={() => void onPatch(objective, { completed: !objective.completed }, { completed: !objective.completed })}
        >
          <Check size={13} />
        </button>
        <div className="objective-title-block">
          {isCompany ? (
            <span className="objective-company-badge" title="회사 주간계획에서 부여된 목표예요">
              회사 부여
            </span>
          ) : null}
          <input
            className="objective-title-input"
            aria-label="Objective title"
            value={titleDraft}
            readOnly={isCompany}
            onBlur={isCompany ? undefined : commitTitle}
            onChange={isCompany ? undefined : (event) => setTitleDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                event.currentTarget.blur();
              }
            }}
          />
          {isCompany ? (
            objective.project ? (
              <span className="objective-company-project">#{objective.project.name}</span>
            ) : null
          ) : (
            <ChannelPicker
              project={objective.project}
              projects={projects}
              sourceLabel={null}
              variant="card"
              allowCompany={false}
              onCreateProject={onCreateProject}
              onSelect={(projectId) => void onPatch(objective, { project_id: projectId }, { project_id: projectId, project: projects.find((item) => item.project_id === projectId) ?? null })}
              onUpdateProject={onUpdateProject}
              onDeleteProject={onDeleteProject}
            />
          )}
        </div>
        <button
          className="objective-open"
          aria-label="Open objective details"
          type="button"
          onClick={onOpen}
        >
          <Maximize2 size={14} />
        </button>
        {isCompany ? null : (
          <button
            className="objective-delete"
            aria-label="Delete objective"
            type="button"
            onClick={() => void onDelete(objective)}
          >
            <Trash2 size={14} />
          </button>
        )}
      </div>
      <div className="objective-day-cells">
        {dayKeys.map((dayKey, weekday) => {
          const allocation = allocationForWeekday(objective.day_allocations, weekday);
          const assigned = allocation != null;
          return (
            <div className={`objective-day-cell ${dayKey === realToday ? "is-today" : ""} ${assigned ? "filled" : ""}`} key={dayKey}>
              <button
                className="objective-day-button"
                type="button"
                onClick={() => toggleDayAssignment(weekday)}
                aria-label={assigned ? `Remove ${WEEKDAY_SHORT[weekday]} assignment` : `Assign to ${WEEKDAY_SHORT[weekday]}`}
                aria-pressed={assigned}
              >
                <Check size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </article>
  );
}

function relativeTimeFrom(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 45) return "now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

function ObjectiveDetailModal({
  objective,
  weekday,
  scope,
  projects,
  realToday,
  onPatch,
  onAddSubtask,
  onToggleSubtask,
  onAddComment,
  onSetDayNote,
  onClose,
  onCreateProject,
  onUpdateProject,
  onDeleteProject,
}: {
  objective: PersonalWeeklyObjective;
  weekday: number;
  scope: "day" | "week";
  projects: PersonalBoardProject[];
  realToday: string;
  onPatch: (
    objective: PersonalWeeklyObjective,
    patch: { title?: string; completed?: boolean; day_allocations?: DayAllocation[]; project_id?: string | null; note?: string | null },
    optimistic?: Partial<PersonalWeeklyObjective>,
  ) => void | Promise<void>;
  onAddSubtask: (objective: PersonalWeeklyObjective, weekday: number, title: string) => void | Promise<void>;
  onToggleSubtask: (objective: PersonalWeeklyObjective, subtaskId: string, completed: boolean) => void | Promise<void>;
  onAddComment: (objective: PersonalWeeklyObjective, weekday: number, body: string) => void | Promise<void>;
  onSetDayNote: (objective: PersonalWeeklyObjective, weekday: number, note: string) => void;
  onClose: () => void;
  onCreateProject: (name: string) => Promise<PersonalBoardProject | null>;
  onUpdateProject: (
    projectId: string,
    patch: Partial<Pick<PersonalBoardProject, "name" | "color">>,
  ) => Promise<PersonalBoardProject | null>;
  onDeleteProject: (projectId: string) => Promise<boolean>;
}) {
  const [titleDraft, setTitleDraft] = useState(objective.title);
  const [syncedTitle, setSyncedTitle] = useState(objective.title);
  // Re-sync the local draft when the persisted title changes (e.g. after save).
  if (objective.title !== syncedTitle) {
    setSyncedTitle(objective.title);
    setTitleDraft(objective.title);
  }

  const isWeekScope = scope === "week";
  const dayNote = allocationForWeekday(objective.day_allocations, weekday)?.note ?? "";
  const noteValue = isWeekScope ? objective.note ?? "" : dayNote;
  const [subtaskDraft, setSubtaskDraft] = useState("");
  const [commentDraft, setCommentDraft] = useState("");
  const [noteDraft, setNoteDraft] = useState(noteValue);
  const [syncedNote, setSyncedNote] = useState(noteValue);
  // 입력 중인 서브태스크를 ref에 동기 보관해, 모달이 먼저 닫혀 onBlur가 못 돌아도
  // 언마운트 flush로 저장한다. commit이 ref를 동기로 비워 중복 추가를 막는다.
  const subtaskDraftRef = useRef("");
  // Re-sync the local note draft when the persisted note changes (same pattern as the title input).
  if (noteValue !== syncedNote) {
    setSyncedNote(noteValue);
    setNoteDraft(noteValue);
  }

  function submitSubtask(event: FormEvent) {
    event.preventDefault();
    commitSubtaskDraft();
  }

  // 포커스가 빠져나가도(다른 곳 클릭/모달 닫힘) 입력한 내용이 자동 추가되게 한다.
  function commitSubtaskDraft() {
    const clean = subtaskDraftRef.current.trim();
    if (!clean) return;
    subtaskDraftRef.current = "";
    setSubtaskDraft("");
    void onAddSubtask(objective, weekday, clean);
  }

  const commitSubtaskRef = useRef(commitSubtaskDraft);
  useEffect(() => {
    commitSubtaskRef.current = commitSubtaskDraft;
  });
  useEffect(() => () => commitSubtaskRef.current?.(), []);

  // 노트/제목도 같은 방식으로 언마운트(Esc/배경 클릭/닫기 버튼/내비게이션) 시
  // flush한다 — NoteField/제목 입력은 onBlur에서만 저장하는데, blur 없이 모달이
  // 닫히면(Esc 등) 입력이 그대로 유실된다. commitNote/commitTitle은 draft가
  // 저장된 값과 같으면 no-op이라 안전하다.
  const commitNoteRef = useRef(commitNote);
  useEffect(() => {
    commitNoteRef.current = commitNote;
  });
  const commitTitleRef = useRef(commitTitle);
  useEffect(() => {
    commitTitleRef.current = commitTitle;
  });
  useEffect(
    () => () => {
      commitNoteRef.current?.();
      commitTitleRef.current?.();
    },
    [],
  );

  function submitComment(event: FormEvent) {
    event.preventDefault();
    const clean = commentDraft.trim();
    if (!clean) return;
    void onAddComment(objective, weekday, clean);
    setCommentDraft("");
  }

  function commitNote() {
    if (noteDraft === noteValue) return;
    if (isWeekScope) {
      void onPatch(objective, { note: noteDraft || null }, { note: noteDraft || null });
    } else {
      onSetDayNote(objective, weekday, noteDraft);
    }
  }

  const sortedSubtasks = objective.subtasks
    .filter((s) => (isWeekScope ? true : s.weekday === weekday))
    .sort((a, b) => (isWeekScope && a.weekday !== b.weekday ? a.weekday - b.weekday : a.order_index - b.order_index));
  const dayComments = objective.comments
    .filter((c) => (isWeekScope ? true : c.weekday === weekday))
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const focusedDayKey = addDays(objective.week_start, weekday);
  const focusedAllocation = allocationForWeekday(objective.day_allocations, weekday);
  const focusedAssigned = focusedAllocation != null;

  function toggleFocusedAssignment() {
    const next = setAllocationAssigned(objective.day_allocations, weekday, !focusedAssigned);
    void onPatch(objective, { day_allocations: next }, { day_allocations: next });
  }

  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  function commitTitle() {
    const title = titleDraft.trim();
    if (!title || title === objective.title) {
      setTitleDraft(objective.title);
      return;
    }
    void onPatch(objective, { title }, { title });
  }

  const thisWeek = sundayOf(realToday);
  const nextWeek = addDays(thisWeek, 7);
  const startLabel =
    objective.week_start === thisWeek ? "This Week" : objective.week_start === nextWeek ? "Next Week" : weekRangeLabel(objective.week_start);

  return createPortal(
    <div
      className="personal-ojosama-root"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section aria-label="Objective details" aria-modal="true" className="objective-detail-modal" role="dialog">
        <header className="objective-detail-topbar">
          <div className="objective-detail-channel">
            <span>Channel</span>
            <ChannelPicker
              project={objective.project}
              projects={projects}
              sourceLabel={null}
              variant="detail"
              allowCompany={false}
              onCreateProject={onCreateProject}
              onSelect={(projectId) =>
                void onPatch(
                  objective,
                  { project_id: projectId },
                  { project_id: projectId, project: projects.find((item) => item.project_id === projectId) ?? null },
                )
              }
              onUpdateProject={onUpdateProject}
              onDeleteProject={onDeleteProject}
            />
          </div>
          <div className="objective-detail-top-actions">
            <div className="objective-detail-start">
              <span>Start</span>
              <strong>{startLabel}</strong>
            </div>
            <button className="objective-detail-icon-button" aria-label="Close objective details" onClick={onClose} type="button">
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="objective-detail-body">
          <span className="objective-detail-kind">
            <Target size={14} aria-hidden />
            Weekly objective
          </span>
          <div className="objective-detail-title-row">
            <button
              className={`objective-detail-check ${objective.completed ? "checked" : ""}`}
              aria-label={objective.completed ? "Mark objective open" : "Mark objective done"}
              type="button"
              onClick={() => void onPatch(objective, { completed: !objective.completed }, { completed: !objective.completed })}
            >
              <Check size={14} />
            </button>
            <input
              aria-label="Objective title"
              className="objective-detail-title"
              value={titleDraft}
              onBlur={commitTitle}
              onChange={(event) => setTitleDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  event.currentTarget.blur();
                }
              }}
            />
          </div>

          <div className={`objective-detail-focused-day ${focusedDayKey === realToday ? "is-today" : ""} ${focusedAssigned ? "filled" : ""}`}>
            <span className="objective-detail-focused-day-head">
              {isWeekScope ? "Week" : `${WEEKDAY_SHORT[weekday]} ${dateFromKey(focusedDayKey).getDate()}`}
            </span>
            <div className="objective-detail-focused-day-cell">
              <button
                className="objective-detail-day-button"
                type="button"
                onClick={toggleFocusedAssignment}
                aria-label={focusedAssigned ? "Remove assignment" : "Assign objective"}
                aria-pressed={focusedAssigned}
              >
                <Check size={14} />
                <span>{focusedAssigned ? "Assigned" : "Not assigned"}</span>
              </button>
            </div>
          </div>

          <section className="objective-detail-subtasks">
            <span className="objective-detail-section-label">Subtasks</span>
            {sortedSubtasks.length > 0 ? (
              <div className="objective-detail-subtask-list">
                {sortedSubtasks.map((subtask) => (
                  <div className="objective-detail-subtask-row" key={subtask.subtask_id}>
                    <button
                      className={`objective-detail-subtask-check ${subtask.completed ? "checked" : ""}`}
                      type="button"
                      aria-label={subtask.completed ? "Mark subtask open" : "Mark subtask done"}
                      onClick={() => void onToggleSubtask(objective, subtask.subtask_id, !subtask.completed)}
                    >
                      <Check size={13} />
                    </button>
                    <span className={`objective-detail-subtask-title ${subtask.completed ? "completed" : ""}`}>
                      {isWeekScope ? (
                        <span className="objective-detail-subtask-day">{WEEKDAY_SHORT[subtask.weekday]}</span>
                      ) : null}
                      {subtask.title}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
            <form className="objective-detail-subtask-form" onSubmit={submitSubtask}>
              <Plus size={16} />
              <input
                aria-label="Add subtask"
                placeholder="Add subtask"
                value={subtaskDraft}
                onChange={(event) => { setSubtaskDraft(event.target.value); subtaskDraftRef.current = event.target.value; }}
                onBlur={commitSubtaskDraft}
              />
            </form>
          </section>

          <section className="objective-detail-note">
            <NoteField
              ariaLabel="Objective note"
              editClassName="objective-detail-note-input"
              readClassName="objective-detail-note-input"
              placeholder="Notes…"
              rows={3}
              value={noteDraft}
              onChange={(value) => setNoteDraft(value)}
              onCommit={commitNote}
            />
          </section>

          <section className="objective-detail-comments">
            <span className="objective-detail-section-label">Comments</span>
            <div className="objective-detail-comment-thread">
              {dayComments.map((comment) => {
                const name = comment.author_name?.trim() || "User";
                return (
                  <div className="objective-detail-comment" key={comment.comment_id}>
                    <div className="objective-detail-avatar" aria-hidden="true">
                      {name.slice(0, 1).toUpperCase()}
                    </div>
                    <div className="objective-detail-comment-main">
                      <div className="objective-detail-comment-head">
                        <strong>{name}</strong>
                        <span className="objective-detail-comment-time">{relativeTimeFrom(comment.created_at)}</span>
                      </div>
                      <p><Linkify text={comment.body} /></p>
                    </div>
                  </div>
                );
              })}
              <div className="objective-detail-activity-row">
                <span className="objective-detail-activity-dot" aria-hidden="true" />
                <span>created this · {relativeTimeFrom(objective.created_at)}</span>
              </div>
            </div>
            <form className="objective-detail-comment-form" onSubmit={submitComment}>
              <textarea
                aria-label="Add comment"
                placeholder="Comment…"
                rows={1}
                value={commentDraft}
                onChange={(event) => setCommentDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    submitComment(event);
                  }
                }}
              />
            </form>
          </section>
        </div>
      </section>
    </div>,
    document.body,
  );
}

function WeeklyReviewView({
  weekStart,
  review,
  loading,
  projects,
  onWeekStartChange,
}: {
  weekStart: string | null;
  review: WeeklyReviewResponse | null;
  loading: boolean;
  projects: PersonalBoardProject[];
  onWeekStartChange: (weekStart: string) => void;
}) {
  const start = weekStart ?? (review?.week_start ?? "");
  const dailyMax = Math.max(1, ...(review?.daily ?? []).map((day) => day.done_count));

  function projectColor(projectId: string | null) {
    if (!projectId) return "#9a9ba0";
    return projects.find((item) => item.project_id === projectId)?.color ?? "#9a9ba0";
  }

  return (
    <section className="weekly-view weekly-review" aria-label="Weekly review">
      <header className="weekly-header">
        <div className="weekly-header-main">
          <button className="weekly-nav-button" aria-label="Previous week" type="button" onClick={() => onWeekStartChange(addDays(start, -7))}>
            <ChevronLeft size={16} />
          </button>
          <div className="weekly-header-titles">
            <h2>{start ? weekRangeLabel(start) : "Weekly review"}</h2>
            <span>Retrospective</span>
          </div>
          <button className="weekly-nav-button" aria-label="Next week" type="button" onClick={() => onWeekStartChange(addDays(start, 7))}>
            <ChevronRight size={16} />
          </button>
        </div>
      </header>

      {loading && !review ? (
        <div className="weekly-empty">Loading review…</div>
      ) : (
        <div className="review-body">
          <aside className="review-rail">
            <div className="review-card">
              <span className="review-card-label">What got done</span>
              <strong className="review-headline">You completed {review?.total_done ?? 0} tasks this week</strong>
            </div>
            <div className="review-card">
              <span className="review-card-label">Daily completion</span>
              <div className="review-bar-chart">
                {(review?.daily ?? []).map((day, index) => (
                  <div className="review-bar-col" key={day.date}>
                    <div className="review-bar-track">
                      <div
                        className="review-bar-fill"
                        style={{ height: `${Math.round((day.done_count / dailyMax) * 100)}%` }}
                        aria-hidden="true"
                      />
                    </div>
                    <span className="review-bar-count">{day.done_count}</span>
                    <span className="review-bar-label">{WEEKDAY_SHORT[index]}</span>
                  </div>
                ))}
              </div>
            </div>
            <div className="review-card">
              <span className="review-card-label">By project</span>
              <div className="review-project-list">
                {(review?.by_project ?? []).length === 0 ? (
                  <span className="review-empty-line">No completed tasks</span>
                ) : (
                  (review?.by_project ?? []).map((entry) => (
                    <div className="review-project-row" key={entry.project_id ?? "none"}>
                      <span className="review-dot" style={{ background: entry.color ?? projectColor(entry.project_id) }} />
                      <span className="review-project-name">{entry.project_name ?? "No channel"}</span>
                      <strong className="review-project-count">{entry.done_count}</strong>
                    </div>
                  ))
                )}
              </div>
            </div>
          </aside>

          <div className="review-day-board">
            {(review?.daily ?? []).map((day, index) => (
              <section className="review-day-column" key={day.date}>
                <header className="review-day-header">
                  <h3>{WEEKDAY_SHORT[index]}</h3>
                  <span>{monthDayLabel(day.date)}</span>
                </header>
                <div className="review-day-tasks">
                  {day.done_tasks.length === 0 ? (
                    <div className="review-day-empty">—</div>
                  ) : (
                    day.done_tasks.map((task) => (
                      <article className="review-task-card" key={task.task_id}>
                        <span className="review-task-check" aria-hidden="true">
                          <Check size={11} />
                        </span>
                        <span className="review-task-title">{task.title}</span>
                        {task.project ? (
                          <span className="review-task-channel" style={{ color: task.project.color ?? "#9a9ba0" }}>
                            #{task.project.name}
                          </span>
                        ) : null}
                      </article>
                    ))
                  )}
                </div>
              </section>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
