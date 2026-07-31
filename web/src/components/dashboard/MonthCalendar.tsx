"use client";

/**
 * MonthCalendar — Sunday-first month grid showing team events as colored
 * chips. Each event is colored by its own color or its member's color so the
 * whole team's schedule reads at a glance.
 */

import * as React from "react";
import type { CalendarEvent } from "@/lib/api";
import { cx } from "@/components/ui";

const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];
// 셀에 직접 보여줄 최대 일정 수. 이보다 많으면 "+N 더보기"로 접고, 눌러서
// 그 날의 전체 일정을 팝오버에서 본다.
const MAX_CHIPS = 3;

// "HH:MM:SS"/"HH:MM" → "HH:MM". 복귀 시간 표시에 쓴다.
function hhmm(t: string): string {
  return t.slice(0, 5);
}

// "복귀불가" 태그 — 팀 일정에서 이 일정 뒤 담당자가 복귀하지 않음을 한눈에 보이게 한다.
// 집 이모지(🏠)로 표시하고, 스크린리더용 "복귀불가"는 sr-only로 둔다.
// 제목이 잘려도 보이도록 제목 옆 flex-none 형제로 둔다(아젠다/팝오버 행처럼 여유 있는 곳).
function NoReturnTag() {
  return (
    <span
      className="flex-none rounded px-1 text-[var(--text-micro)] font-semibold"
      style={{ background: "var(--color-sidebar-active)", color: "var(--color-text-strong)" }}
      title="복귀불가"
    >
      <span aria-hidden>🏠</span>
      <span className="sr-only">복귀불가</span>
    </span>
  );
}

// "복귀 시간" 태그 — 담당자가 복귀하는 시각을 한눈에 보이게 한다(복귀불가와 상호 배타적).
function ReturnTimeTag({ time }: { time: string }) {
  return (
    <span
      className="flex-none rounded px-1 text-[var(--text-micro)] font-semibold"
      style={{ background: "var(--color-sidebar-active)", color: "var(--color-text-strong)" }}
      title={`복귀 ${hhmm(time)}`}
    >
      <span aria-hidden>↩</span>
      <span className="sr-only">복귀 </span>
      {hhmm(time)}
    </span>
  );
}

function ymd(d: Date): string {
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}
// "2026-06-12" → "6월 12일 (금)". 팝오버 헤더용.
function fmtDayKo(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getMonth() + 1}월 ${d.getDate()}일 (${WEEKDAYS[d.getDay()]})`;
}
function startOfGrid(year: number, month: number): Date {
  const first = new Date(year, month, 1);
  const offset = first.getDay(); // Sunday = 0
  const start = new Date(first);
  start.setDate(first.getDate() - offset);
  start.setHours(0, 0, 0, 0);
  return start;
}

// 한 일정이 하루 셀에 그려질 때의 조각. 다중일이면 시작/끝 여부로 칩 모서리를
// 둥글리고 연결된 막대처럼 보이게 한다.
type EventSlice = {
  event: CalendarEvent;
  isSpan: boolean; // end_date가 시작일보다 뒤인 다중일 일정
  isStart: boolean;
  isEnd: boolean;
};

// 모바일 월 셀의 짧은 시간 라벨. 다중일 일정에 같은 시작 시간을 매일 반복해
// 보여주지 않고, 중간은 "계속", 마지막 날은 종료 시간이 있으면 "~HH:MM"으로
// 표시해 그 날 새로 시작하는 일정처럼 오해하지 않게 한다.
function mobileSliceTime(slice: EventSlice): string {
  if (slice.isSpan && !slice.isStart) {
    if (slice.isEnd && slice.event.end_time) return `~${hhmm(slice.event.end_time)}`;
    return "계속";
  }
  return slice.event.start_time ? hhmm(slice.event.start_time) : "종일";
}

// [event_date, end_date] 사이의 모든 날짜(ISO)를 돌려준다. end_date가 없거나
// 시작일보다 빠르면 시작일 하루만.
function spanDates(startIso: string, endIso: string | null): string[] {
  const out: string[] = [startIso];
  if (!endIso || endIso <= startIso) return out;
  const cur = new Date(`${startIso}T00:00:00`);
  const end = new Date(`${endIso}T00:00:00`);
  if (Number.isNaN(cur.getTime()) || Number.isNaN(end.getTime())) return out;
  out.length = 0;
  let guard = 0;
  while (cur <= end && guard < 366) {
    out.push(ymd(cur));
    cur.setDate(cur.getDate() + 1);
    guard += 1;
  }
  return out;
}

// 폰(컴팩트 셸, <760px)에서는 터치용 월 그리드와 선택한 날짜의 아젠다를 함께
// 보여준다. AppShell의 760px 임계값과 globals.css `@media (max-width: 759px)`에
// 맞춘다.
function useIsCompact(): boolean {
  const [compact, setCompact] = React.useState(false);
  React.useEffect(() => {
    const mq = window.matchMedia("(max-width: 759px)");
    const update = () => setCompact(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return compact;
}

// 다중일 일정에서 해당 날짜가 몇 일째인지(1-base). 단일일은 1.
function dayIndexInSpan(startIso: string, iso: string): number {
  const a = new Date(`${startIso}T00:00:00`).getTime();
  const b = new Date(`${iso}T00:00:00`).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return 1;
  return Math.floor((b - a) / 86400000) + 1;
}

function chipBackground(colors: string[]): string {
  // 단일=단색, 복수=각 멤버 색을 한 줄에 균등 분할
  if (colors.length <= 1) return colors[0] ?? "#9fb4bf";
  const n = colors.length;
  const stops = colors
    .map((c, i) => `${c} ${(i / n) * 100}% ${((i + 1) / n) * 100}%`)
    .join(", ");
  return `linear-gradient(90deg, ${stops})`;
}

export function MonthCalendar({
  year,
  month,
  events,
  colorsFor,
  onSelectDate,
  onSelectEvent,
}: {
  year: number;
  month: number; // 0-based
  events: CalendarEvent[];
  colorsFor: (e: CalendarEvent) => string[];
  onSelectDate: (dateIso: string) => void;
  onSelectEvent: (e: CalendarEvent) => void;
}) {
  // "+N 더보기"로 펼친 날. 그 날의 전체 일정을 팝오버에서 보여준다.
  const [openDay, setOpenDay] = React.useState<string | null>(null);
  // 모바일 월 달력에서 선택한 날짜. 월을 이동했을 때 이전 선택이 새 달력의
  // leading/trailing 셀에 우연히 남지 않도록 선택 당시 월도 같이 보관한다.
  const [mobileSelection, setMobileSelection] = React.useState<{
    monthKey: string;
    iso: string;
  } | null>(null);
  const todayIso = ymd(new Date());
  const start = startOfGrid(year, month);
  const cells: Date[] = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    cells.push(d);
  }

  // 다중일 일정은 시작일~종료일 모든 날짜 셀에 조각을 넣어 캘린더에서 기간이
  // 이어져 보이게 한다. 같은 날 안에서는 다중일 일정을 먼저(위에) 둔다.
  const byDate = new Map<string, EventSlice[]>();
  for (const e of events) {
    const days = spanDates(e.event_date, e.end_date);
    const isSpan = days.length > 1;
    days.forEach((iso, i) => {
      const list = byDate.get(iso) ?? [];
      list.push({
        event: e,
        isSpan,
        isStart: i === 0,
        isEnd: i === days.length - 1,
      });
      byDate.set(iso, list);
    });
  }
  // 같은 날 안에서는 시작 시간이 빠른 순으로 위에 온다(만든 순서 아님).
  // 시간이 없는 종일/다중일 일정은 빈 문자열이라 자연히 맨 위에 모인다.
  for (const list of byDate.values()) {
    list.sort((a, b) => {
      const ta = a.event.start_time ?? "";
      const tb = b.event.start_time ?? "";
      if (ta !== tb) return ta.localeCompare(tb);
      return a.event.title.localeCompare(b.event.title);
    });
  }

  const openDayEvents = openDay ? byDate.get(openDay) ?? [] : [];

  const compact = useIsCompact();

  // 폰: 데스크톱과 같은 월 맥락을 유지하되, 좁은 셀에는 팀원 색으로 칠한
  // `시간 + 제목` 미니카드를 최대 2개 표시한다. 날짜를 고르면 바로 아래
  // 아젠다에서 전체 제목과 종료 시간까지 확인/편집한다.
  if (compact) {
    const monthKey = `${year}-${month}`;
    const isCurrentMonth =
      new Date().getFullYear() === year && new Date().getMonth() === month;
    const selectedIso =
      mobileSelection?.monthKey === monthKey
        ? mobileSelection.iso
        : isCurrentMonth
          ? todayIso
          : ymd(new Date(year, month, 1));
    const selectedEvents = byDate.get(selectedIso) ?? [];

    return (
      <div className="flex flex-col gap-3">
        <div
          className="overflow-hidden rounded-[var(--radius-card)]"
          style={{
            border: "1px solid var(--color-divider)",
            background: "var(--color-card)",
          }}
        >
          <div className="grid grid-cols-7" aria-hidden>
            {WEEKDAYS.map((weekday, index) => (
              <div
                key={weekday}
                className="flex h-8 items-center justify-center text-[var(--text-meta)] font-semibold"
                style={{
                  color:
                    index === 0 || index === 6
                      ? "var(--color-text-muted)"
                      : "var(--color-text-body)",
                  borderBottom: "1px solid var(--color-divider)",
                  background: "var(--color-panel)",
                }}
              >
                {weekday}
              </div>
            ))}
          </div>
          <div className="grid grid-cols-7">
            {cells.map((date, index) => {
              const iso = ymd(date);
              const inMonth = date.getMonth() === month;
              const isToday = iso === todayIso;
              const isSelected = iso === selectedIso;
              const dayEvents = byDate.get(iso) ?? [];
              const isWeekend = index % 7 === 0 || index % 7 === 6;
              const visibleEvents = dayEvents.slice(0, 2);
              const hiddenEventCount = Math.max(0, dayEvents.length - visibleEvents.length);
              const eventSummary = visibleEvents
                .map((slice) => `${mobileSliceTime(slice)} ${slice.event.title}`)
                .join(", ");
              return (
                <button
                  key={iso}
                  type="button"
                  aria-label={`${fmtDayKo(iso)}, 일정 ${dayEvents.length}개${eventSummary ? `: ${eventSummary}` : ""}`}
                  aria-current={isToday ? "date" : undefined}
                  aria-pressed={isSelected}
                  onClick={() => setMobileSelection({ monthKey, iso })}
                  className="relative flex min-h-[84px] min-w-0 flex-col items-center px-[2px] pb-1 pt-1 transition-colors"
                  style={{
                    borderRight:
                      index % 7 !== 6 ? "1px solid var(--color-divider-soft)" : undefined,
                    borderBottom: "1px solid var(--color-divider-soft)",
                    background: isSelected
                      ? "var(--color-sidebar-active)"
                      : inMonth
                        ? "var(--color-card)"
                        : "var(--color-panel)",
                    boxShadow: isSelected
                      ? "inset 0 0 0 1px var(--color-text-strong)"
                      : undefined,
                    opacity: inMonth || isSelected ? 1 : 0.52,
                  }}
                >
                  <span className="flex h-6 w-full items-center justify-between gap-0.5">
                    <span
                      className="inline-flex h-6 min-w-6 items-center justify-center rounded-full px-1 text-[var(--text-meta)] tabular-nums"
                      style={{
                        background: isToday ? "var(--color-text-strong)" : "transparent",
                        color: isToday
                          ? "var(--color-card)"
                          : isWeekend
                            ? "var(--color-text-muted)"
                            : "var(--color-text-body)",
                        fontWeight: isToday || isSelected ? 700 : 500,
                      }}
                    >
                      {date.getDate()}
                    </span>
                    {hiddenEventCount > 0 ? (
                      <span
                        aria-hidden
                        className="truncate text-[8px] font-bold leading-none"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        +{hiddenEventCount}
                      </span>
                    ) : null}
                  </span>
                  <span className="mt-0.5 flex w-full min-w-0 flex-1 flex-col gap-0.5 overflow-hidden">
                    {visibleEvents.map((slice, eventIndex) => {
                      const event = slice.event;
                      const time = mobileSliceTime(slice);
                      return (
                        <span
                          aria-hidden
                          key={`${event.event_id}-${iso}-${eventIndex}-mobile-chip`}
                          className="flex h-[24px] w-full min-w-0 flex-none flex-col overflow-hidden rounded-[3px] text-left"
                          style={{
                            background: "var(--color-card)",
                            border: "1px solid var(--color-divider-soft)",
                          }}
                          title={`${time} · ${event.title}`}
                        >
                          <span
                            className="h-[3px] w-full flex-none"
                            style={{ background: chipBackground(colorsFor(event)) }}
                          />
                          <span
                            className="block truncate px-[2px] text-[9px] font-bold leading-[9px] tabular-nums"
                            style={{ color: "var(--color-text-muted)" }}
                          >
                            {time}
                          </span>
                          <span
                            className="block truncate px-[2px] text-[9px] font-semibold leading-[10px]"
                            style={{ color: "var(--color-text-strong)" }}
                          >
                            {event.title}
                          </span>
                        </span>
                      );
                    })}
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <section
          aria-label={`${fmtDayKo(selectedIso)} 일정`}
          className="overflow-hidden rounded-[var(--radius-card)]"
          style={{
            border: "1px solid var(--color-divider)",
            background: "var(--color-card)",
          }}
        >
          <div
            className="flex min-h-12 items-center justify-between gap-2 px-3"
            style={{
              borderBottom: selectedEvents.length
                ? "1px solid var(--color-divider-soft)"
                : undefined,
              background: "var(--color-panel)",
            }}
          >
            <div className="min-w-0">
              <h3
                className="truncate text-[var(--text-body-sm)] font-bold"
                style={{ color: "var(--color-text-strong)" }}
              >
                {fmtDayKo(selectedIso)}
                {selectedIso === todayIso ? (
                  <span
                    className="ml-1.5 rounded-[5px] px-1.5 py-[1px] text-[var(--text-micro)] font-bold align-middle"
                    style={{
                      background: "var(--color-text-strong)",
                      color: "var(--color-card)",
                    }}
                  >
                    오늘
                  </span>
                ) : null}
              </h3>
              <p
                className="mt-0.5 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                {selectedEvents.length > 0
                  ? `일정 ${selectedEvents.length}개`
                  : "등록된 일정 없음"}
              </p>
            </div>
            <button
              type="button"
              onClick={() => onSelectDate(selectedIso)}
              className="flex min-h-11 flex-none items-center rounded-[7px] px-3 text-[var(--text-body-sm)] font-semibold"
              style={{
                border: "1px solid var(--color-divider)",
                background: "var(--color-card)",
                color: "var(--color-text-strong)",
              }}
            >
              + 일정
            </button>
          </div>

          {selectedEvents.length ? (
            <div className="flex flex-col">
              {selectedEvents.map((slice, eventIndex) => {
                const event = slice.event;
                const startTime = event.start_time ? hhmm(event.start_time) : null;
                const endTime = event.end_time ? hhmm(event.end_time) : null;
                const spanDay = slice.isSpan
                  ? dayIndexInSpan(event.event_date, selectedIso)
                  : 0;
                return (
                  <button
                    key={`${event.event_id}-${event.event_date}-${selectedIso}-${eventIndex}-mobile`}
                    type="button"
                    onClick={() => onSelectEvent(event)}
                    className="flex min-h-[52px] items-center gap-2.5 px-3 text-left"
                    style={{
                      borderTop:
                        eventIndex > 0 ? "1px solid var(--color-divider-soft)" : undefined,
                    }}
                  >
                    <span
                      className="h-8 w-1 flex-none rounded-full"
                      style={{ background: chipBackground(colorsFor(event)) }}
                    />
                    <span
                      className="w-[70px] flex-none text-[var(--text-meta)] tabular-nums font-semibold"
                      style={{
                        color: startTime
                          ? "var(--color-text-muted)"
                          : "var(--color-text-faint)",
                      }}
                    >
                      {startTime ? (
                        <>
                          {startTime}
                          {endTime ? (
                            <span className="block font-medium">– {endTime}</span>
                          ) : null}
                        </>
                      ) : (
                        "종일"
                      )}
                    </span>
                    <span
                      className="min-w-0 flex-1 truncate text-[var(--text-body-sm)] font-medium"
                      style={{ color: "var(--color-text-strong)" }}
                    >
                      {event.repeat_freq ? (
                        <>
                          <span aria-hidden style={{ color: "var(--color-text-muted)" }}>
                            ↻{" "}
                          </span>
                          <span className="sr-only">반복 일정 </span>
                        </>
                      ) : null}
                      {event.title}
                      {spanDay ? (
                        <span style={{ color: "var(--color-text-faint)", fontWeight: 500 }}>
                          {" "}
                          ({spanDay}일째)
                        </span>
                      ) : null}
                    </span>
                    {event.no_return ? (
                      <NoReturnTag />
                    ) : event.return_time ? (
                      <ReturnTimeTag time={event.return_time} />
                    ) : null}
                  </button>
                );
              })}
            </div>
          ) : null}
        </section>
      </div>
    );
  }

  return (
    <>
    <div
      className="overflow-hidden rounded-[var(--radius-card)]"
      style={{ border: "1px solid var(--color-divider)", background: "var(--color-card)" }}
    >
      <div className="grid grid-cols-7">
        {WEEKDAYS.map((w, i) => (
          <div
            key={w}
            className="px-2 py-1.5 text-center text-[var(--text-meta)] font-semibold"
            style={{
              color: i === 0 || i === 6 ? "var(--color-text-muted)" : "var(--color-text-body)",
              borderBottom: "1px solid var(--color-divider)",
              background: "var(--color-panel)",
            }}
          >
            {w}
          </div>
        ))}
      </div>
      <div className="grid grid-cols-7">
        {cells.map((d, idx) => {
          const iso = ymd(d);
          const inMonth = d.getMonth() === month;
          const isToday = iso === todayIso;
          const dayEvents = byDate.get(iso) ?? [];
          const isWeekend = idx % 7 === 0 || idx % 7 === 6;
          return (
            <div
              key={iso}
              role="button"
              tabIndex={0}
              onClick={() => onSelectDate(iso)}
              onKeyDown={(ev) => {
                if (ev.key === "Enter" || ev.key === " ") {
                  ev.preventDefault();
                  onSelectDate(iso);
                }
              }}
              className={cx("relative min-h-[88px] p-1.5 text-left align-top transition-colors")}
              style={{
                borderRight: idx % 7 !== 6 ? "1px solid var(--color-divider-soft)" : undefined,
                borderBottom: "1px solid var(--color-divider-soft)",
                background: inMonth ? "var(--color-card)" : "var(--color-panel)",
                opacity: inMonth ? 1 : 0.6,
                cursor: "pointer",
              }}
            >
              {/* 날짜는 셀 좌상단 코너에 고정 — 일정 개수와 무관하게 위치 유지 */}
              <span
                className={cx(
                  "absolute left-1 top-1 z-[1] inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1 text-[var(--text-meta)]",
                )}
                style={{
                  background: isToday ? "var(--color-text-strong)" : "transparent",
                  color: isToday
                    ? "var(--color-card)"
                    : isWeekend
                      ? "var(--color-text-muted)"
                      : "var(--color-text-body)",
                  fontWeight: isToday ? 700 : 500,
                }}
              >
                {d.getDate()}
              </span>
              <div className="mt-6 flex flex-col gap-1">
                {dayEvents.slice(0, MAX_CHIPS).map((slice) => {
                  const e = slice.event;
                  // 다중일 막대는 시작/끝에서만 모서리를 둥글려 연결돼 보이게.
                  const r = (on: boolean) => (on ? 4 : slice.isSpan ? 0 : 4);
                  // 라벨은 시작일이나 주 시작(일요일)에서만 — 그 외 연결 칸은 빈 막대.
                  const isWeekStart = idx % 7 === 0;
                  const showLabel = !slice.isSpan || slice.isStart || isWeekStart;
                  const t = slice.isStart && e.start_time ? e.start_time.slice(0, 5) : null;
                  return (
                    <span
                      key={`${e.event_id}-${e.event_date}-${iso}`}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        onSelectEvent(e);
                      }}
                      className="truncate px-1.5 py-[1px] text-[var(--text-micro)] font-medium"
                      style={{
                        background: chipBackground(colorsFor(e)),
                        color: "#fff",
                        fontSize: "10px",
                        lineHeight: 1.4,
                        textShadow: "0 0 2px rgba(0,0,0,0.35)",
                        borderTopLeftRadius: r(slice.isStart),
                        borderBottomLeftRadius: r(slice.isStart),
                        borderTopRightRadius: r(slice.isEnd),
                        borderBottomRightRadius: r(slice.isEnd),
                        // 끝나는 날 이후로 이어지지 않게, 시작/주중에는 살짝 우측 여백을 줄임
                        marginRight: slice.isSpan && !slice.isEnd ? -6 : 0,
                      }}
                      title={`${t ? `${t} ${e.title}` : e.title}${e.repeat_freq ? " · 반복" : ""}${e.no_return ? " · 복귀불가" : e.return_time ? ` · 복귀 ${e.return_time.slice(0, 5)}` : ""}`}
                    >
                      {t ? <b style={{ fontWeight: 700 }}>{t} </b> : null}
                      {/* 반복 일정 표시: ↻는 장식이라 aria-hidden, 낭독용 텍스트는 sr-only. */}
                      {showLabel && e.repeat_freq ? (
                        <>
                          <span aria-hidden>↻&nbsp;</span>
                          <span className="sr-only">반복 일정 </span>
                        </>
                      ) : null}
                      {/* "복귀불가" 표시: 제목보다 앞에 둬서 칸이 잘려도 보이게.
                          ⊘는 장식이라 aria-hidden, 스크린리더용 텍스트는 sr-only로 따로 둔다. */}
                      {showLabel && e.no_return ? (
                        <>
                          <span aria-hidden>🏠&nbsp;</span>
                          <span className="sr-only">복귀불가 </span>
                        </>
                      ) : showLabel && e.return_time ? (
                        <>
                          <b aria-hidden style={{ fontWeight: 700 }}>
                            ↩{e.return_time.slice(0, 5)}&nbsp;
                          </b>
                          <span className="sr-only">복귀 {e.return_time.slice(0, 5)} </span>
                        </>
                      ) : null}
                      {showLabel ? e.title : " "}
                    </span>
                  );
                })}
                {dayEvents.length > MAX_CHIPS ? (
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(ev) => {
                      ev.stopPropagation();
                      setOpenDay(iso);
                    }}
                    onKeyDown={(ev) => {
                      if (ev.key === "Enter" || ev.key === " ") {
                        ev.preventDefault();
                        ev.stopPropagation();
                        setOpenDay(iso);
                      }
                    }}
                    className="cursor-pointer rounded px-1 text-[var(--text-micro)] font-medium hover:underline"
                    style={{ color: "var(--color-text-muted)", fontSize: "10px" }}
                  >
                    +{dayEvents.length - MAX_CHIPS} 더보기
                  </span>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </div>

    {/* "+N 더보기"를 누르면 그 날의 전체 일정을 보여주는 팝오버. 캘린더 컨테이너의
        overflow-hidden에 잘리지 않도록 화면 고정(fixed) 오버레이로 띄운다. */}
    {openDay ? (
      <div
        className="fixed inset-0 z-50 flex items-center justify-center p-4"
        style={{ background: "rgba(0,0,0,0.35)" }}
        onClick={() => setOpenDay(null)}
      >
        <div
          className="flex max-h-[70vh] w-full max-w-[360px] flex-col overflow-hidden rounded-[var(--radius-card)]"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-popover)",
          }}
          onClick={(ev) => ev.stopPropagation()}
        >
          <div
            className="flex items-center justify-between px-4 py-2.5"
            style={{ borderBottom: "1px solid var(--color-divider)" }}
          >
            <span
              className="text-[var(--text-body)] font-bold"
              style={{ color: "var(--color-text-strong)" }}
            >
              {fmtDayKo(openDay)}
            </span>
            <button
              type="button"
              onClick={() => setOpenDay(null)}
              className="flex h-7 w-7 items-center justify-center rounded-[6px] text-[var(--text-body)]"
              style={{ color: "var(--color-text-muted)" }}
              aria-label="닫기"
            >
              ✕
            </button>
          </div>
          <div className="flex flex-col gap-1 overflow-y-auto p-2">
            {openDayEvents.map((slice) => {
              const e = slice.event;
              const t = e.start_time ? e.start_time.slice(0, 5) : null;
              return (
                <button
                  key={`${e.event_id}-${e.event_date}-pop`}
                  type="button"
                  onClick={() => {
                    onSelectEvent(e);
                    setOpenDay(null);
                  }}
                  className="flex items-center gap-2 rounded-[6px] px-2 py-1.5 text-left"
                  style={{ color: "var(--color-text-strong)" }}
                >
                  <span
                    className="inline-block h-3 w-3 flex-none rounded-full"
                    style={{ background: chipBackground(colorsFor(e)) }}
                  />
                  {t ? (
                    <span
                      className="flex-none text-[var(--text-meta)] tabular-nums font-semibold"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      {t}
                    </span>
                  ) : (
                    <span
                      className="flex-none text-[var(--text-meta)]"
                      style={{ color: "var(--color-text-faint)" }}
                    >
                      종일
                    </span>
                  )}
                  <span className="truncate text-[var(--text-body-sm)] font-medium">
                    {e.repeat_freq ? (
                      <>
                        <span aria-hidden style={{ color: "var(--color-text-muted)" }}>
                          ↻{" "}
                        </span>
                        <span className="sr-only">반복 일정 </span>
                      </>
                    ) : null}
                    {e.title}
                  </span>
                  {e.no_return ? (
                    <NoReturnTag />
                  ) : e.return_time ? (
                    <ReturnTimeTag time={e.return_time} />
                  ) : null}
                </button>
              );
            })}
          </div>
          <div className="px-2 py-2" style={{ borderTop: "1px solid var(--color-divider)" }}>
            <button
              type="button"
              onClick={() => {
                const iso = openDay;
                setOpenDay(null);
                if (iso) onSelectDate(iso);
              }}
              className="w-full rounded-[6px] px-2 py-1.5 text-center text-[var(--text-body-sm)] font-medium"
              style={{
                border: "1px solid var(--color-divider)",
                background: "var(--color-card)",
                color: "var(--color-text-strong)",
              }}
            >
              + 이 날 일정 추가
            </button>
          </div>
        </div>
      </div>
    ) : null}
    </>
  );
}
