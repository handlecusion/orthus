"use client";

import { useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import { useDroppable } from "@dnd-kit/core";
import { CalendarRange, X } from "lucide-react";
import type { PersonalBoardTask } from "@/lib/api";

/* ----------------------------- 타임라인 상수 ----------------------------- */
// 썬사마식 당일 타임라인. 6시~24시(자정)를 한 시간 HOUR_PX 높이의 그리드로 그린다.
// 드롭/이동/리사이즈는 SNAP_MIN(15분) 단위로 스냅한다. 이 상수들은 부모(handleDragEnd)의
// 포인터→시간 계산과 공유한다.
export const SCHEDULE_START_HOUR = 6;
export const SCHEDULE_END_HOUR = 24;
export const SCHEDULE_HOUR_PX = 52;
export const SCHEDULE_SNAP_MIN = 15;
export const SCHEDULE_MIN_BLOCK_MIN = 15;
export const SCHEDULE_DEFAULT_DURATION_MIN = 60;

const START_MIN = SCHEDULE_START_HOUR * 60;
const END_MIN = SCHEDULE_END_HOUR * 60;
const GRID_HEIGHT = (SCHEDULE_END_HOUR - SCHEDULE_START_HOUR) * SCHEDULE_HOUR_PX;

const pad = (n: number) => String(n).padStart(2, "0");

export function clampScheduleMin(min: number): number {
  return Math.max(START_MIN, Math.min(END_MIN, min));
}

export function snapScheduleMin(min: number): number {
  return Math.round(min / SCHEDULE_SNAP_MIN) * SCHEDULE_SNAP_MIN;
}

// "HH:MM[:SS]" → 분(자정 기준). null/형식오류는 null.
export function parseClockToMin(value: string | null | undefined): number | null {
  if (!value) return null;
  const [h, m] = value.split(":");
  const hh = Number(h);
  const mm = Number(m);
  if (!Number.isFinite(hh) || !Number.isFinite(mm)) return null;
  return hh * 60 + mm;
}

// 분 → "HH:MM:00"(서버 Time 포맷).
export function minToClock(min: number): string {
  const clamped = Math.max(0, Math.min(24 * 60 - 1, Math.round(min)));
  return `${pad(Math.floor(clamped / 60))}:${pad(clamped % 60)}:00`;
}

// 그리드 상단 기준 오프셋(px) → 스냅된 분(자정 기준).
export function offsetToScheduleMin(relPx: number): number {
  const raw = START_MIN + (relPx / SCHEDULE_HOUR_PX) * 60;
  return clampScheduleMin(snapScheduleMin(raw));
}

function minToOffset(min: number): number {
  return ((min - START_MIN) / 60) * SCHEDULE_HOUR_PX;
}

function minLabel(min: number): string {
  // 카드의 시간 표기(timeOfDayLabel)와 동일하게 오전/오후 대신 am/pm으로 맞춘다.
  const h24 = Math.floor(min / 60) % 24;
  const m = min % 60;
  const period = h24 >= 12 ? "pm" : "am";
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  return m === 0 ? `${h12} ${period}` : `${h12}:${pad(m)} ${period}`;
}

/* ----------------------------- 미리 변환된 고정 이벤트 블록 ----------------------------- */
// 고정 이벤트(starts_at/ends_at)는 타임존 변환이 필요하므로 부모에서 분 단위로 변환해 넘긴다.
export type ScheduleEventBlock = {
  event_id: string;
  title: string;
  startMin: number;
  endMin: number;
  color: string | null;
};

type DragState = {
  taskId: string;
  mode: "move" | "resize";
  startMin: number;
  endMin: number;
  moved: boolean;
};

/* ----------------------------- 패널 ----------------------------- */
export function SchedulePanel({
  dateKey,
  title,
  tasks,
  events,
  nowMinutes,
  gridRef,
  activeDragTaskId,
  onOpenTask,
  onCommit,
  onCollapse,
}: {
  dateKey: string;
  title: string;
  tasks: PersonalBoardTask[];
  events: ScheduleEventBlock[];
  nowMinutes: number | null;
  gridRef: (node: HTMLDivElement | null) => void;
  activeDragTaskId: string | null;
  onOpenTask: (taskId: string) => void;
  // 시작/종료 분을 확정 저장한다(이동/리사이즈 공통). end는 항상 start보다 크다.
  onCommit: (task: PersonalBoardTask, startMin: number, endMin: number) => void;
  onCollapse: () => void;
}) {
  const { isOver, setNodeRef } = useDroppable({ id: "schedule" });
  const [drag, setDrag] = useState<DragState | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const pointerStartRef = useRef<{ y: number; startMin: number; endMin: number } | null>(null);

  // 타임 블록이 있는 태스크만(자정 기준 분 파싱). 시작 없으면 제외.
  const timed = tasks
    .map((task) => {
      const startMin = parseClockToMin(task.scheduled_time);
      if (startMin == null) return null;
      const parsedEnd = parseClockToMin(task.scheduled_end_time);
      const endMin =
        parsedEnd != null && parsedEnd > startMin
          ? parsedEnd
          : startMin + SCHEDULE_DEFAULT_DURATION_MIN;
      return { task, startMin, endMin, hasEnd: parsedEnd != null && parsedEnd > startMin };
    })
    .filter((x): x is { task: PersonalBoardTask; startMin: number; endMin: number; hasEnd: boolean } => x !== null);

  const hours: number[] = [];
  for (let h = SCHEDULE_START_HOUR; h < SCHEDULE_END_HOUR; h += 1) hours.push(h);

  function beginPointerDrag(
    event: ReactPointerEvent<HTMLDivElement>,
    task: PersonalBoardTask,
    startMin: number,
    endMin: number,
  ) {
    // 좌클릭(주 버튼)만. 블록 본문=이동, 하단 핸들=리사이즈로 모드를 가린다.
    if (event.button !== 0) return;
    const mode: "move" | "resize" = (event.target as HTMLElement).closest(".schedule-resize-handle")
      ? "resize"
      : "move";
    event.preventDefault();
    // 포인터 캡처는 항상 블록(currentTarget)에. 하위 핸들에서 시작해도 이후 move/up이
    // 블록 핸들러로만 들어와 중복 호출이 없다.
    event.currentTarget.setPointerCapture?.(event.pointerId);
    pointerStartRef.current = { y: event.clientY, startMin, endMin };
    const next: DragState = { taskId: task.task_id, mode, startMin, endMin, moved: false };
    dragRef.current = next;
    setDrag(next);
  }

  function onPointerMove(event: ReactPointerEvent) {
    const cur = dragRef.current;
    const origin = pointerStartRef.current;
    if (!cur || !origin) return;
    const deltaMin = snapScheduleMin(((event.clientY - origin.y) / SCHEDULE_HOUR_PX) * 60);
    let nextStart = origin.startMin;
    let nextEnd = origin.endMin;
    if (cur.mode === "move") {
      const duration = origin.endMin - origin.startMin;
      nextStart = clampScheduleMin(origin.startMin + deltaMin);
      // 끝이 그리드를 벗어나면 시작을 당겨 길이를 유지한다.
      nextEnd = nextStart + duration;
      if (nextEnd > END_MIN) {
        nextEnd = END_MIN;
        nextStart = nextEnd - duration;
      }
    } else {
      nextEnd = clampScheduleMin(origin.endMin + deltaMin);
      if (nextEnd < origin.startMin + SCHEDULE_MIN_BLOCK_MIN) {
        nextEnd = origin.startMin + SCHEDULE_MIN_BLOCK_MIN;
      }
    }
    const moved = cur.moved || Math.abs(event.clientY - origin.y) > 3;
    const next: DragState = { ...cur, startMin: nextStart, endMin: nextEnd, moved };
    dragRef.current = next;
    setDrag(next);
  }

  function endPointerDrag(task: PersonalBoardTask) {
    const cur = dragRef.current;
    dragRef.current = null;
    pointerStartRef.current = null;
    setDrag(null);
    if (!cur) return;
    if (!cur.moved) {
      // 거의 안 움직였으면 클릭으로 보고 상세를 연다.
      onOpenTask(task.task_id);
      return;
    }
    onCommit(task, cur.startMin, cur.endMin);
  }

  return (
    <aside className="schedule-panel" aria-label="당일 스케줄">
      <header className="schedule-panel-header">
        <div className="schedule-panel-title">
          <CalendarRange size={15} aria-hidden />
          <span>스케줄</span>
          <span className="schedule-panel-date">{title}</span>
        </div>
        <button
          className="schedule-panel-collapse"
          onClick={onCollapse}
          type="button"
          aria-label="스케줄 패널 닫기"
        >
          <X size={15} />
        </button>
      </header>

      <div className="schedule-scroll">
        <div
          className={`schedule-grid ${isOver ? "drop-over" : ""}`}
          data-date={dateKey}
          ref={(node) => {
            setNodeRef(node);
            gridRef(node);
          }}
          style={{ height: GRID_HEIGHT } as CSSProperties}
        >
          {hours.map((h) => (
            <div
              className="schedule-hour-row"
              key={h}
              style={{ top: minToOffset(h * 60), height: SCHEDULE_HOUR_PX } as CSSProperties}
            >
              <span className="schedule-hour-label">{minLabel(h * 60)}</span>
              <span className="schedule-hour-line" />
            </div>
          ))}

          {nowMinutes != null && nowMinutes >= START_MIN && nowMinutes <= END_MIN ? (
            <div className="schedule-now-line" style={{ top: minToOffset(nowMinutes) } as CSSProperties}>
              <span className="schedule-now-dot" />
            </div>
          ) : null}

          {/* 고정 이벤트(읽기 전용) */}
          {events.map((ev) => {
            // 그리드 가시 범위(06:00–24:00)와 겹치지 않는 이벤트는 그리지 않는다
            // (예: 새벽 05:00 이벤트가 18px 유령 블록으로 06:00 줄에 붙는 것 방지).
            if (ev.endMin <= START_MIN || ev.startMin >= END_MIN) return null;
            const top = minToOffset(Math.max(START_MIN, ev.startMin));
            const height = Math.max(
              18,
              minToOffset(Math.min(END_MIN, ev.endMin)) - top,
            );
            return (
              <div
                className="schedule-event-block"
                key={ev.event_id}
                title={ev.title}
                style={
                  {
                    top,
                    height,
                    "--schedule-block-color": ev.color ?? "var(--color-text-faint, #9aa0a6)",
                  } as CSSProperties
                }
              >
                <span className="schedule-block-time">{minLabel(ev.startMin)}</span>
                <span className="schedule-block-title">{ev.title}</span>
              </div>
            );
          })}

          {/* 태스크 타임 블록(이동/리사이즈 가능) */}
          {timed.map(({ task, startMin, endMin }) => {
            const live = drag && drag.taskId === task.task_id ? drag : null;
            const s = live ? live.startMin : startMin;
            const e = live ? live.endMin : endMin;
            const top = minToOffset(s);
            const height = Math.max(SCHEDULE_MIN_BLOCK_MIN * (SCHEDULE_HOUR_PX / 60), minToOffset(e) - top);
            const dim = activeDragTaskId === task.task_id;
            return (
              <div
                className={`schedule-task-block ${task.completed ? "completed" : ""} ${live ? "dragging" : ""} ${dim ? "is-source" : ""}`}
                key={task.task_id}
                style={
                  {
                    top,
                    height,
                    "--schedule-block-color": task.project?.color ?? "var(--channel-slate, #5b6470)",
                  } as CSSProperties
                }
                onPointerDown={(event) => beginPointerDrag(event, task, startMin, endMin)}
                onPointerMove={onPointerMove}
                onPointerUp={() => endPointerDrag(task)}
                onPointerCancel={() => {
                  dragRef.current = null;
                  pointerStartRef.current = null;
                  setDrag(null);
                }}
                role="button"
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onOpenTask(task.task_id);
                  }
                }}
                title={`${minLabel(s)} – ${minLabel(e)} · ${task.title}`}
              >
                <span className="schedule-block-time">
                  {minLabel(s)} – {minLabel(e)}
                </span>
                <span className="schedule-block-title">{task.title}</span>
                <span className="schedule-resize-handle" aria-hidden />
              </div>
            );
          })}

          {timed.length === 0 && events.length === 0 ? (
            <div className="schedule-empty">
              할 일을 여기로 끌어오거나, 할 일을 열어 시작·종료 시간을 입력하면 일정에 표시됩니다.
            </div>
          ) : null}
        </div>
      </div>
    </aside>
  );
}
