"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  API_BASE,
  type CalendarEvent,
  type CalendarEventInput,
  type CalendarEventType,
  type CalendarRepeatFreq,
  type DashboardProject,
  type TeamMember,
} from "@/lib/api";
import { ChannelHeader, Avatar, Card, Banner, ToolbarButton } from "@/components/ui";
import {
  DatePicker,
  Field,
  Modal,
  Select,
  TagSelect,
  TextInput,
  TextArea,
  anchoredStyle,
  labelStyle,
  useAnchoredPopover,
  MEMBER_FALLBACK_PALETTE as PALETTE,
  type TagColor,
} from "@/components/dashboard/kit";
import { MonthCalendar } from "@/components/dashboard/MonthCalendar";
const TYPE_OPTIONS: { value: CalendarEventType; label: string; color: TagColor }[] = [
  { value: "meeting", label: "외부 미팅", color: "orange" },
  { value: "vacation", label: "휴가", color: "pink" },
  { value: "dev", label: "개발", color: "blue" },
  { value: "update", label: "업데이트", color: "green" },
];
const TYPE_LABELS = TYPE_OPTIONS.map((o) => o.label);
const typeLabel = (v: CalendarEventType): string =>
  TYPE_OPTIONS.find((o) => o.value === v)?.label ?? v;
const labelToType = (label: string): CalendarEventType =>
  TYPE_OPTIONS.find((o) => o.label === label)?.value ?? "meeting";
const typeColor = (label: string): TagColor =>
  TYPE_OPTIONS.find((o) => o.label === label)?.color ?? "gray";

// 시간 드롭다운: 왼쪽 시(00~23) · 오른쪽 분(10분 단위)
const HOUR_OPTIONS = Array.from({ length: 24 }, (_, i) => `${i}`.padStart(2, "0"));
const MINUTE_OPTIONS = ["00", "10", "20", "30", "40", "50"];

// 반복(루틴) 옵션. 값은 서버 repeat_freq와 1:1.
const REPEAT_OPTIONS: { value: "" | CalendarRepeatFreq; label: string }[] = [
  { value: "", label: "반복 안 함" },
  { value: "daily", label: "매일" },
  { value: "weekly", label: "매주" },
  { value: "biweekly", label: "격주" },
  { value: "monthly", label: "매월" },
];
// 요일 인덱스는 서버(Python date.weekday()) 규약: 0=월 … 6=일.
const REPEAT_WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"];
// "2026-07-02" → 파이썬 weekday(0=월…6=일). JS getDay()는 0=일이라 변환한다.
function pyWeekday(iso: string): number {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return 0;
  return (d.getDay() + 6) % 7;
}
function addDaysIso(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00`);
  d.setDate(d.getDate() + days);
  return ymd(d);
}
function diffDaysIso(a: string, b: string): number {
  const ta = new Date(`${a}T00:00:00`).getTime();
  const tb = new Date(`${b}T00:00:00`).getTime();
  if (Number.isNaN(ta) || Number.isNaN(tb)) return 0;
  return Math.round((tb - ta) / 86400000);
}

function ymd(d: Date): string {
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

// 추가 날짜 칩 표시: "2026-07-08" → "7/8 (수)".
// 신규 일정용 클라이언트 멱등 event_id. 백엔드 client_event_id는 UUID 타입이라
// 반드시 유효한 UUID v4여야 한다(비-UUID면 422). crypto.randomUUID는 secure
// context 전용이라 http://<LAN-IP>:3820(폰 QA) 같은 origin에선 없다 → getRandomValues
// 로 직접 v4를 만들고, 그마저 없으면 UUID 모양 문자열로 최후 폴백한다.
function newClientEventId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  if (typeof crypto !== "undefined" && crypto.getRandomValues) {
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40; // version 4
    b[8] = (b[8] & 0x3f) | 0x80; // variant
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0"));
    return `${h[0]}${h[1]}${h[2]}${h[3]}-${h[4]}${h[5]}-${h[6]}${h[7]}-${h[8]}${h[9]}-${h[10]}${h[11]}${h[12]}${h[13]}${h[14]}${h[15]}`;
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

const _WD_KO = ["일", "월", "화", "수", "목", "금", "토"];
function fmtExtraDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.getMonth() + 1}/${d.getDate()} (${_WD_KO[d.getDay()]})`;
}

// MonthCalendar는 일요일 정렬 + 42셀(6주) 고정 그리드라, 표시되는 마지막 셀이
// 다음 달 중순(말일 +최대 11일)까지, 첫 셀이 전달 말(1일 -최대 6일)까지 갈 수
// 있다. 이벤트 fetch 범위를 그 그리드 경계(startOfGrid ~ +41일)와 1:1로 맞춰야
// 인접월 trailing/leading 셀의 일정이 빠지지 않는다. (기존 말일+7 패딩은 그리드
// 끝을 못 따라가 7/8~7/11 같은 trailing 셀 일정이 누락됐다.)
function gridRange(year: number, month: number): { from: string; to: string } {
  const start = new Date(year, month, 1);
  start.setDate(1 - start.getDay()); // 그 주 일요일로
  const end = new Date(start);
  end.setDate(start.getDate() + 41); // 42셀 마지막
  return { from: ymd(start), to: ymd(end) };
}

// 시간 선택: 왼쪽 시 · 오른쪽 분(10분 단위) 2열 드롭다운. value는 "HH:MM".
function TimeField({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const hh = value ? value.slice(0, 2) : "";
  const mm = value ? value.slice(3, 5) : "";
  const cellStyle = (active: boolean): React.CSSProperties => ({
    background: active ? "var(--color-sidebar-active)" : "transparent",
    color: "var(--color-text-strong)",
    fontWeight: active ? 700 : 500,
  });
  return (
    <div className="relative">
      <button
        ref={anchorRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
        className="flex h-9 w-full items-center justify-between rounded-[5px] px-2.5 text-left text-[var(--text-body)]"
        style={{
          border: "1px solid var(--color-divider)",
          background: "var(--color-card)",
          color: value ? "var(--color-text-strong)" : "var(--color-text-faint)",
        }}
      >
        <span>{value || "선택"}</span>
        <span style={{ color: "var(--color-text-muted)" }}>▾</span>
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 flex flex-col overflow-hidden rounded-[8px]"
          style={{
            ...anchoredStyle(rect, 150, 264),
            background: "var(--color-card)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-popover)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            type="button"
            onClick={() => {
              onChange("");
              close();
            }}
            className="w-full px-3 py-1.5 text-left text-[var(--text-meta)]"
            style={{
              color: "var(--color-text-muted)",
              borderBottom: "1px solid var(--color-divider)",
            }}
          >
            지정 안 함
          </button>
          <div className="flex min-h-0 flex-1">
            <div
              className="flex-1 overflow-y-auto"
              style={{ borderRight: "1px solid var(--color-divider)" }}
            >
              {HOUR_OPTIONS.map((h) => (
                <button
                  key={h}
                  type="button"
                  onClick={() => onChange(`${h}:${mm || "00"}`)}
                  className="w-full px-3 py-1.5 text-center text-[var(--text-body-sm)]"
                  style={cellStyle(h === hh)}
                >
                  {h}
                </button>
              ))}
            </div>
            <div className="flex-1 overflow-y-auto">
              {MINUTE_OPTIONS.map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => onChange(`${hh || "00"}:${m}`)}
                  className="w-full px-3 py-1.5 text-center text-[var(--text-body-sm)]"
                  style={cellStyle(m === mm)}
                >
                  {m}
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

// 편집 중인 일정의 식별자(event_id)는 Draft에 넣지 않는다 — 필드 편집의
// 오브젝트-스프레드(stale closure)로 event_id가 지워져 중복 생성되던 문제를 막기
// 위해 별도 editingId 상태/ref로 관리한다.
type Draft = {
  title: string;
  member_ids: string[];
  event_type: CalendarEventType;
  project_id: string;
  location: string;
  event_date: string;
  end_date: string;
  start_time: string;
  end_time: string;
  no_return: boolean;
  // "복귀 시간"("HH:MM" 또는 ""). no_return과 상호 배타적.
  return_time: string;
  repeat_freq: "" | CalendarRepeatFreq;
  repeat_weekdays: number[];
  repeat_until: string;
  // 반복과 별개로, 같은 일정을 붙일 임의 추가 날짜(ISO) 목록.
  extra_dates: string[];
  description: string;
};

// Draft → 서버 저장 payload. 자동 저장·keepalive가 공유한다.
function buildEventBody(draft: Draft): CalendarEventInput {
  return {
    member_ids: draft.member_ids,
    title: draft.title,
    description: draft.description || null,
    all_day: !draft.start_time,
    event_date: draft.event_date,
    end_date: draft.end_date || null,
    start_time: draft.start_time || null,
    end_time: draft.end_time || null,
    no_return: draft.no_return,
    // 복귀 불가면 복귀 시간은 비운다(상호 배타적, 서버도 정리).
    return_time: draft.no_return ? null : draft.return_time || null,
    repeat_freq: draft.repeat_freq || null,
    repeat_weekdays:
      draft.repeat_freq === "weekly" || draft.repeat_freq === "biweekly"
        ? draft.repeat_weekdays
        : [],
    repeat_until: draft.repeat_freq ? draft.repeat_until || null : null,
    // 추가 날짜는 반복과 독립 — 시작일과 겹치는 값은 서버가 정리한다.
    extra_dates: draft.extra_dates.filter((d) => d !== draft.event_date),
    starts_at: null,
    ends_at: null,
    event_type: draft.event_type,
    color: null,
    project_id: draft.project_id || null,
    location: draft.location.trim() || null,
  };
}

// 제목 없이 닫기/숨김이 발생해도 지켜야 할 "실제 내용"이 있는지 판정한다.
// 메모·담당자·장소·시간·추가 날짜 중 하나라도 있으면 true — 이 경우 닫기/숨김
// 경로는 폐기 대신 임시 제목으로 구제 저장한다(아래 closeEditor/flushOnHide).
function hasContent(d: Draft): boolean {
  return Boolean(
    d.description.trim() ||
      d.member_ids.length > 0 ||
      d.location.trim() ||
      d.start_time ||
      d.end_time ||
      d.end_date ||
      d.extra_dates.length > 0,
  );
}
// /editor 페이지의 "title.trim() || '제목 없음'" 관례와 동일한 구제용 임시 제목.
const FALLBACK_TITLE = "제목 없음";

export default function CalendarPage() {
  const [ref, setRef] = useState<Date>(() => {
    const d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), 1);
  });
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  // 장소 입력 자동완성용 — 과거에 입력된 distinct 장소 목록(노드 전체 기간).
  const [locationOptions, setLocationOptions] = useState<string[]>([]);
  const [filterMember, setFilterMember] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  // 편집 중 일정의 식별자(신규는 null, 첫 저장 성공 시 채워짐). render용 상태 +
  // 핸들러/이펙트용 ref를 함께 갱신한다(Draft와 분리 — 위 Draft 주석 참조).
  const [editingId, setEditingId] = useState<string | null>(null);
  const editingIdRef = useRef<string | null>(null);
  // 신규 일정용 클라이언트 생성 멱등 id(편집 세션 동안 고정). 자동 저장의 create가
  // keepalive/재시도/bfcache로 여러 번 나가도 서버가 이 id로 upsert해 중복을 막는다.
  const clientCreateIdRef = useRef<string | null>(null);
  // 기존 일정을 열었을 때 서버에 저장돼 있던 원래 제목(편집 중 제목을 지워도
  // 보존). 닫기/숨김 구제 저장에서 제목을 빈 값으로 덮어쓰지 않기 위해 쓴다.
  const originalTitleRef = useRef<string>("");
  // "다른 날짜에도 추가" 입력용 임시 날짜(추가 버튼을 누르면 draft.extra_dates로).
  const [extraDateDraft, setExtraDateDraft] = useState<string>("");
  // 자동 저장 상태 표시(저장 버튼 대체).
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");

  // 자동 저장 배관: 디바운스 타이머 + 저장 직렬화 체인 + 최신 draft 미러.
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveChain = useRef<Promise<void>>(Promise.resolve());
  const latestDraftRef = useRef<Draft | null>(null);
  // 열림 직후 첫 draft 세팅은 저장을 예약하지 않는다.
  const justOpenedRef = useRef(false);
  // 이번 편집 세션에서 실제 변경이 있었는지(닫기 때 무편집이면 저장/반영 생략).
  const dirtyRef = useRef(false);

  const year = ref.getFullYear();
  const month = ref.getMonth();

  const colorMap = useMemo(() => {
    const map = new Map<string, string>();
    members.forEach((m, i) => {
      map.set(m.member_id, m.color || PALETTE[i % PALETTE.length]);
    });
    return map;
  }, [members]);

  // 담당자 없음 또는 팀 전체(전원) = 빨강. 일부 복수 담당자는 각 멤버 색을 한 줄에 나눠 표시.
  const COMPANY_COLOR = "#e5484d";
  const colorsFor = useCallback(
    (e: CalendarEvent): string[] => {
      const allSelected =
        members.length > 0 && members.every((m) => e.member_ids.includes(m.member_id));
      if (allSelected) return [e.color || COMPANY_COLOR];
      const cols = e.member_ids
        .map((id) => colorMap.get(id))
        .filter((c): c is string => Boolean(c));
      if (cols.length) return cols;
      return [e.color || COMPANY_COLOR];
    },
    [colorMap, members],
  );

  const reload = useCallback(async () => {
    const { from, to } = gridRange(year, month);
    const [ev, mem, proj, locs] = await Promise.all([
      api.listCalendar(from, to),
      api.listTeamMembers(),
      api.listDashboardProjects(),
      api.listCalendarLocations(),
    ]);
    setEvents(ev);
    setMembers(mem);
    setProjects(proj);
    setLocationOptions(locs);
  }, [year, month]);

  useEffect(() => {
    let alive = true;
    const { from, to } = gridRange(year, month);
    Promise.all([
      api.listCalendar(from, to),
      api.listTeamMembers(),
      api.listDashboardProjects(),
      api.listCalendarLocations(),
    ])
      .then(([ev, mem, proj, locs]) => {
        if (!alive) return;
        setEvents(ev);
        setMembers(mem);
        setProjects(proj);
        setLocationOptions(locs);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
      });
    return () => {
      alive = false;
    };
  }, [year, month]);

  const shown = filterMember
    ? events.filter((e) => e.member_ids.includes(filterMember))
    : events;

  function openCreate(dateIso: string, options?: { noReturn?: boolean }) {
    justOpenedRef.current = true;
    dirtyRef.current = false;
    editingIdRef.current = null;
    clientCreateIdRef.current = newClientEventId();
    originalTitleRef.current = "";
    setEditingId(null);
    setSaveState("idle");
    setDraft({
      title: options?.noReturn ? "복귀불가 일정" : "",
      member_ids: [],
      event_type: "meeting",
      project_id: "",
      location: "",
      event_date: dateIso,
      end_date: "",
      start_time: "",
      end_time: "",
      no_return: options?.noReturn ?? false,
      return_time: "",
      repeat_freq: "",
      repeat_weekdays: [],
      repeat_until: "",
      extra_dates: [],
      description: "",
    });
    setExtraDateDraft("");
    setOpen(true);
  }
  function openCreateNoReturn(dateIso: string) {
    openCreate(dateIso, { noReturn: true });
  }
  function openEdit(e: CalendarEvent) {
    // 반복·추가날짜로 펼쳐진 회차는 클릭한 회차가 아니라 시리즈(마스터)를 편집한다.
    // 펼쳐진 회차면 series_start가 마스터 시작일이라, 회차 기준으로 이동된
    // event_date/end_date를 마스터 기준으로 되돌린다. (repeat_freq만 보면 추가날짜
    // 전용 회차를 놓치므로 series_start 유무로 판정한다.)
    const isOccurrence = Boolean(e.series_start);
    const seriesStart = isOccurrence ? (e.series_start ?? e.event_date) : e.event_date;
    const shift = isOccurrence ? diffDaysIso(seriesStart, e.event_date) : 0;
    justOpenedRef.current = true;
    dirtyRef.current = false;
    editingIdRef.current = e.event_id;
    clientCreateIdRef.current = null;
    originalTitleRef.current = e.title;
    setEditingId(e.event_id);
    setSaveState("idle");
    setDraft({
      title: e.title,
      member_ids: e.member_ids ?? [],
      event_type: e.event_type,
      project_id: e.project_id ?? "",
      location: e.location ?? "",
      event_date: seriesStart,
      end_date: e.end_date ? addDaysIso(e.end_date, -shift) : "",
      start_time: e.start_time ? e.start_time.slice(0, 5) : "",
      end_time: e.end_time ? e.end_time.slice(0, 5) : "",
      no_return: e.no_return ?? false,
      return_time: e.return_time ? e.return_time.slice(0, 5) : "",
      repeat_freq: e.repeat_freq ?? "",
      repeat_weekdays: e.repeat_weekdays ?? [],
      repeat_until: e.repeat_until ?? "",
      // 추가 날짜는 마스터 소유라 모든 회차가 같은 목록을 들고 있다.
      extra_dates: e.extra_dates ?? [],
      description: e.description ?? "",
    });
    setExtraDateDraft("");
    setOpen(true);
  }

  function toggleMember(id: string) {
    setDraft((dft) =>
      dft
        ? {
            ...dft,
            member_ids: dft.member_ids.includes(id)
              ? dft.member_ids.filter((m) => m !== id)
              : [...dft.member_ids, id],
          }
        : dft,
    );
  }

  // 추가 날짜 넣기 — 시작일과 같거나 이미 있는 날짜는 무시.
  function addExtraDate(iso: string) {
    const v = (iso || "").trim();
    if (!v) return;
    setDraft((dft) => {
      if (!dft) return dft;
      if (v === dft.event_date || dft.extra_dates.includes(v)) return dft;
      return { ...dft, extra_dates: [...dft.extra_dates, v].sort() };
    });
    setExtraDateDraft("");
  }
  function removeExtraDate(iso: string) {
    setDraft((dft) =>
      dft ? { ...dft, extra_dates: dft.extra_dates.filter((d) => d !== iso) } : dft,
    );
  }
  // 대표(시작일) 날짜만 빼기 — 남은 추가 날짜 중 가장 이른 날짜를 새 대표(event_date)로
  // 승격하고 나머지 날짜는 그대로 둔다. 추가 날짜가 없으면(유일한 날짜) no-op이고
  // (일정 전체 삭제는 삭제 버튼), 반복 일정은 시작일이 반복 기준점이라 여기서 빼지 않는다.
  function removeMainDate() {
    setDraft((dft) => {
      if (!dft) return dft;
      if (dft.repeat_freq) return dft;
      if (!dft.extra_dates.length) return dft;
      const [promoted, ...rest] = [...dft.extra_dates].sort();
      return { ...dft, event_date: promoted, extra_dates: rest };
    });
  }

  function toggleRepeatWeekday(idx: number) {
    setDraft((dft) =>
      dft
        ? {
            ...dft,
            repeat_weekdays: dft.repeat_weekdays.includes(idx)
              ? dft.repeat_weekdays.filter((d) => d !== idx)
              : [...dft.repeat_weekdays, idx].sort((a, b) => a - b),
          }
        : dft,
    );
  }

  function toggleAllMembers() {
    setDraft((dft) => {
      if (!dft) return dft;
      const allIds = members.map((m) => m.member_id);
      const allOn = allIds.length > 0 && allIds.every((id) => dft.member_ids.includes(id));
      return { ...dft, member_ids: allOn ? [] : allIds };
    });
  }

  // 한 번에 하나씩만 실행되는 실제 저장. 새 일정은 첫 유효 저장에서 생성하고
  // 이후 저장은 update로 이어진다(제목이 비면 저장하지 않는다 — 빈 행 방지).
  const runSave = useCallback(async (reflect: boolean): Promise<void> => {
    const d = latestDraftRef.current;
    if (!d || !d.title.trim()) {
      setSaveState((s) => (s === "saving" ? "idle" : s));
      return;
    }
    // 새 저장 시도는 이전 실패 배너를 지운다(성공 후에도 배너가 남지 않도록).
    setError(null);
    setSaveState("saving");
    const body = buildEventBody(d);
    try {
      const id = editingIdRef.current;
      if (id) {
        await api.updateCalendarEvent(id, body, { reflect });
      } else {
        // 멱등 client_event_id로 create — 재시도/keepalive가 겹쳐도 서버가 upsert.
        const saved = await api.createCalendarEvent(body, {
          reflect,
          clientEventId: clientCreateIdRef.current ?? undefined,
        });
        // 식별자를 ref/상태에 기록한다(draft를 건드리지 않아 필드 편집의 stale
        // 스프레드로 지워지지 않고, 저장 재예약도 유발하지 않는다). 체인이 직렬
        // 실행하므로 다음 저장은 이 id로 update된다.
        editingIdRef.current = saved.event_id;
        setEditingId(saved.event_id);
      }
      setSaveState("saved");
    } catch (e) {
      setSaveState("error");
      setError(e instanceof Error ? e.message : "자동 저장 실패");
      throw e;
    }
  }, []);

  // 저장을 직렬화(동시 create 방지, 순서 보장). reflect=false면 위키 반영 스킵.
  const enqueueSave = useCallback(
    (reflect: boolean): Promise<void> => {
      const next = saveChain.current.then(() => runSave(reflect));
      saveChain.current = next.catch(() => {});
      return next;
    },
    [runSave],
  );

  const scheduleSave = useCallback(() => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      void enqueueSave(false);
    }, 800);
  }, [enqueueSave]);

  // 최신 draft를 ref에 미러(닫기/keepalive/타이머·저장 체인이 최신값을 읽도록).
  // 이 이펙트가 아래 스케줄 이펙트보다 먼저 선언돼 draft 변경 시 먼저 실행된다.
  useEffect(() => {
    latestDraftRef.current = draft;
  }, [draft]);

  // 편집 시 자동 저장 예약(열림 직후 첫 draft 세팅은 제외). event_id는 draft에
  // 없으므로 create 후 재예약 걱정이 없다.
  useEffect(() => {
    if (!open || !draft) return;
    if (justOpenedRef.current) {
      justOpenedRef.current = false;
      return;
    }
    dirtyRef.current = true;
    scheduleSave();
  }, [draft, open, scheduleSave]);

  // 닫기: 디바운스 취소 → 최종 저장 + 위키 반영(reflect=true) 1회 → 목록 새로고침.
  const closeEditor = useCallback(async () => {
    if (saveTimer.current) {
      clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    let d = latestDraftRef.current;
    // 제목은 비었지만 메모·담당자·장소·시간 등 실제 내용이 남아 있으면 폐기하지
    // 않는다 — 신규 일정은 임시 제목으로, 기존 일정은 서버의 원래 제목을 되살려
    // 저장한다(제목만 지웠다고 서버 제목까지 빈 값으로 덮어쓰지 않기 위함).
    if (d && dirtyRef.current && !d.title.trim() && hasContent(d)) {
      d = {
        ...d,
        title: clientCreateIdRef.current
          ? FALLBACK_TITLE
          : originalTitleRef.current || FALLBACK_TITLE,
      };
      // draft 상태(setDraft)는 건드리지 않는다 — open 상태에서 draft만 바꾸면
      // 자동 저장 예약 이펙트가 다시 돌아 불필요한 타이머가 하나 더 생긴다.
      // 저장은 이 ref만으로 충분하다(runSave가 latestDraftRef를 읽는다).
      latestDraftRef.current = d;
    }
    // 실제 편집이 있었을 때만 최종 저장 + 위키 반영(무편집 브라우징은 스킵).
    if (d && d.title.trim() && dirtyRef.current) {
      try {
        await enqueueSave(true);
      } catch {
        // 저장 실패 시 모달을 닫거나 draft를 버리지 않는다 — 입력이 유실되지
        // 않도록 편집창을 열어 둔 채 saveState="error"("저장 안 됨 — 다시 시도")를
        // 유지하고, 다시 닫기를 누르면 재시도한다.
        return;
      }
    } else if (clientCreateIdRef.current && (!d || !d.title.trim())) {
      // 이번 세션에서 새로 만든(clientCreateIdRef 존재) 일정을 제목·내용 모두
      // 없이 닫으면 유령 행을 남기지 않고 버린다(내용이 있었다면 위에서 이미
      // 구제 저장됐다). openEdit로 연 기존 일정은 clientCreateIdRef가 null이라
      // 여기 걸리지 않아 절대 자동 삭제되지 않는다. in-flight create가 있으면
      // 먼저 끝내고(체인) 그 id로 삭제한다.
      await saveChain.current.catch(() => {});
      const createdId = editingIdRef.current;
      if (createdId) {
        try {
          await api.deleteCalendarEvent(createdId);
        } catch {
          // best-effort — 실패해도 빈 제목은 서버가 거부하므로 실제 손상은 없다.
        }
      }
    }
    dirtyRef.current = false;
    editingIdRef.current = null;
    clientCreateIdRef.current = null;
    originalTitleRef.current = "";
    setOpen(false);
    setDraft(null);
    setEditingId(null);
    setSaveState("idle");
    await reload();
  }, [enqueueSave, reload]);

  // 브라우저 종료/이동 중 미저장 방지(디바운스 창을 못 넘긴 마지막 변경).
  // pagehide 하나만 바인딩한다(beforeunload와 동시 바인딩 시 dispatch가 두 번).
  // 신규 일정도 flush하지만 create는 멱등 client_event_id로 나가므로, in-flight
  // create나 bfcache 재실행과 겹쳐도 서버가 upsert해 중복 행이 생기지 않는다.
  useEffect(() => {
    function flushOnHide() {
      let d = latestDraftRef.current;
      if (!open || !d || !dirtyRef.current) return;
      if (!d.title.trim()) {
        // 제목이 비어 있으면: 다른 내용도 없는 진짜 빈 초안은 그대로 흘려보내고
        // (스팸 방지), 내용이 있으면 닫기와 동일하게 임시/원래 제목으로 구제한다.
        if (!hasContent(d)) return;
        d = {
          ...d,
          title: clientCreateIdRef.current
            ? FALLBACK_TITLE
            : originalTitleRef.current || FALLBACK_TITLE,
        };
      }
      const id = editingIdRef.current;
      const body = buildEventBody(d);
      let url: string;
      if (id) {
        url = `${API_BASE}/dashboard/calendar/events/${id}?reflect=false`;
      } else {
        const cid = clientCreateIdRef.current;
        const extra = cid ? `&client_event_id=${encodeURIComponent(cid)}` : "";
        url = `${API_BASE}/dashboard/calendar/events?reflect=false${extra}`;
      }
      try {
        void fetch(url, {
          method: id ? "PATCH" : "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          keepalive: true,
          body: JSON.stringify(body),
        });
      } catch {
        // best-effort
      }
    }
    window.addEventListener("pagehide", flushOnHide);
    return () => {
      window.removeEventListener("pagehide", flushOnHide);
    };
  }, [open]);

  async function remove() {
    const d = latestDraftRef.current;
    // 아직 생성 안 된 새 일정(제목만 있고 저장 전)은 그냥 닫는다. 삭제 버튼은
    // editingId가 있을 때만 보이지만 방어적으로 처리한다.
    if (!editingIdRef.current) {
      if (saveTimer.current) {
        clearTimeout(saveTimer.current);
        saveTimer.current = null;
      }
      dirtyRef.current = false;
      clientCreateIdRef.current = null;
      setOpen(false);
      setDraft(null);
      setEditingId(null);
      setSaveState("idle");
      return;
    }
    const msg = d?.repeat_freq
      ? "반복 일정입니다. 모든 반복 회차가 함께 삭제됩니다. 삭제하시겠습니까?"
      : d?.extra_dates.length
        ? `이 일정과 추가된 날짜 ${d.extra_dates.length}개가 모두 삭제됩니다. 삭제하시겠습니까?`
        : "삭제하시겠습니까?";
    if (!window.confirm(msg)) return;
    if (saveTimer.current) {
      clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    setBusy(true);
    try {
      // 진행 중인 자동 저장이 끝난 뒤(그 시점의 최종 id로) 삭제(경합 방지).
      await saveChain.current.catch(() => {});
      const id = editingIdRef.current;
      if (id) await api.deleteCalendarEvent(id);
      dirtyRef.current = false;
      editingIdRef.current = null;
      clientCreateIdRef.current = null;
      setOpen(false);
      setDraft(null);
      setEditingId(null);
      setSaveState("idle");
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "삭제 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <ChannelHeader
        glyph="#"
        title="팀 일정"
        subtitle="팀원별 일정을 한 달력에서 한눈에 봅니다."
        right={
          <div className="flex w-full items-center justify-end gap-1.5 sm:w-auto">
            <ToolbarButton onClick={() => openCreateNoReturn(ymd(new Date()))}>
              + 복귀불가 일정
            </ToolbarButton>
            <ToolbarButton tone="accent" onClick={() => openCreate(ymd(new Date()))}>
              + 일정
            </ToolbarButton>
          </div>
        }
      />

      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">{error}</Banner>
        </div>
      ) : null}

      <Card className="mb-3 flex flex-wrap items-center justify-between gap-3 p-3">
        <div className="flex items-center gap-1">
          <ToolbarButton onClick={() => setRef(new Date(year, month - 1, 1))}>‹</ToolbarButton>
          <span
            className="px-2 text-[var(--text-body)] font-bold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {year}년 {month + 1}월
          </span>
          <ToolbarButton onClick={() => setRef(new Date(year, month + 1, 1))}>›</ToolbarButton>
          <ToolbarButton
            onClick={() => {
              const d = new Date();
              setRef(new Date(d.getFullYear(), d.getMonth(), 1));
            }}
          >
            오늘
          </ToolbarButton>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-[var(--text-meta)] font-semibold" style={labelStyle()}>
            팀원
          </span>
          {[{ member_id: "", name: "전체", color: COMPANY_COLOR }, ...members].map((m, i) => {
            const on = filterMember === m.member_id;
            const dot = m.member_id
              ? m.color || PALETTE[(i - 1) % PALETTE.length]
              : COMPANY_COLOR;
            return (
              <button
                key={m.member_id || "__all__"}
                type="button"
                onClick={() => setFilterMember(m.member_id)}
                className="flex items-center gap-1.5 rounded-[6px] px-2 py-1 text-[var(--text-body-sm)]"
                style={{
                  border: `1px solid ${on ? "var(--color-text-strong)" : "var(--color-divider)"}`,
                  background: on ? "var(--color-sidebar-active)" : "var(--color-card)",
                  color: "var(--color-text-strong)",
                  fontWeight: on ? 700 : 500,
                }}
              >
                <span
                  className="inline-block h-2.5 w-2.5 rounded-full"
                  style={{ background: dot }}
                />
                {m.name}
              </button>
            );
          })}
        </div>
      </Card>

      {/* 팀원 색상 범례 */}
      {members.length ? (
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <span className="flex items-center gap-1.5 text-[var(--text-meta)]" style={labelStyle()}>
            <span
              className="inline-block h-3 w-3 rounded-full"
              style={{ background: COMPANY_COLOR }}
            />
            전체
          </span>
          {members.map((m, i) => (
            <span key={m.member_id} className="flex items-center gap-1.5 text-[var(--text-meta)]" style={labelStyle()}>
              <Avatar name={m.name} color={m.color || PALETTE[i % PALETTE.length]} size="xs" />
              {m.name}
            </span>
          ))}
        </div>
      ) : null}

      <MonthCalendar
        year={year}
        month={month}
        events={shown}
        colorsFor={colorsFor}
        onSelectDate={openCreate}
        onSelectEvent={openEdit}
      />

      <Modal
        open={open}
        title={
          editingId ? (draft?.repeat_freq ? "반복 일정 수정" : "일정 수정") : "일정 추가"
        }
        onClose={closeEditor}
        footer={
          <>
            {editingId ? (
              <ToolbarButton onClick={remove} disabled={busy} className="mr-auto">
                삭제
              </ToolbarButton>
            ) : null}
            {/* 저장 버튼 대신 자동 저장 상태를 표시한다(추가/수정하면 바로 저장). */}
            <span
              className="mr-1 self-center text-[var(--text-meta)]"
              style={{ color: saveState === "error" ? "#e5484d" : "var(--color-text-muted)" }}
            >
              {saveState === "saving"
                ? "저장 중…"
                : saveState === "saved"
                  ? "자동 저장됨"
                  : saveState === "error"
                    ? "저장 안 됨 — 다시 시도"
                    : draft && !draft.title.trim()
                      ? "제목을 입력하면 자동 저장됩니다"
                      : ""}
            </span>
            <ToolbarButton tone="primary" onClick={closeEditor} disabled={busy}>
              닫기
            </ToolbarButton>
          </>
        }
      >
        {draft ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="제목" full>
              <TextInput
                value={draft.title}
                onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                placeholder="일정 제목"
              />
            </Field>
            <Field label="팀원 (복수 선택)" full>
              <div className="flex flex-wrap gap-1.5">
                {members.length === 0 ? (
                  <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
                    등록된 팀원이 없습니다.
                  </span>
                ) : (
                  <>
                  {(() => {
                    const allOn =
                      members.length > 0 &&
                      members.every((m) => draft.member_ids.includes(m.member_id));
                    return (
                      <button
                        type="button"
                        onClick={toggleAllMembers}
                        className="flex items-center gap-1.5 rounded-[6px] px-2 py-1 text-[var(--text-body-sm)]"
                        style={{
                          border: `1px solid ${allOn ? "var(--color-text-strong)" : "var(--color-divider)"}`,
                          background: allOn ? "var(--color-sidebar-active)" : "var(--color-card)",
                          color: "var(--color-text-strong)",
                          fontWeight: allOn ? 700 : 500,
                        }}
                      >
                        팀 전체
                      </button>
                    );
                  })()}
                  {members.map((m) => {
                    const on = draft.member_ids.includes(m.member_id);
                    return (
                      <button
                        key={m.member_id}
                        type="button"
                        onClick={() => toggleMember(m.member_id)}
                        className="flex items-center gap-1.5 rounded-[6px] px-2 py-1 text-[var(--text-body-sm)]"
                        style={{
                          border: `1px solid ${on ? "var(--color-text-strong)" : "var(--color-divider)"}`,
                          background: on ? "var(--color-sidebar-active)" : "var(--color-card)",
                          color: "var(--color-text-strong)",
                          fontWeight: on ? 700 : 500,
                        }}
                      >
                        <span
                          className="inline-block h-2.5 w-2.5 rounded-full"
                          style={{ background: m.color || "var(--channel-slate)" }}
                        />
                        {m.name}
                      </button>
                    );
                  })}
                  </>
                )}
              </div>
            </Field>
            <Field label="종류">
              <TagSelect
                value={typeLabel(draft.event_type)}
                options={TYPE_LABELS}
                colorFor={typeColor}
                allowCustom={false}
                clearable={false}
                onChange={(label) =>
                  setDraft({ ...draft, event_type: labelToType(label) })
                }
              />
            </Field>
            <Field label="프로젝트">
              <Select
                className="h-9"
                value={draft.project_id}
                options={[
                  { value: "", label: "— 없음 —" },
                  ...projects.map((p) => ({ value: p.project_id, label: p.name })),
                ]}
                onChange={(e) => setDraft({ ...draft, project_id: e.target.value })}
              />
            </Field>
            <Field label="시작일">
              <DatePicker
                value={draft.event_date}
                onChange={(v) => setDraft({ ...draft, event_date: v })}
              />
            </Field>
            <Field label="시작 시간">
              <TimeField
                value={draft.start_time}
                onChange={(v) => setDraft({ ...draft, start_time: v })}
              />
            </Field>
            <Field label="종료 시간(선택)">
              <TimeField
                value={draft.end_time}
                onChange={(v) => setDraft({ ...draft, end_time: v })}
              />
            </Field>
            <Field label="복귀(선택)">
              <div className="flex items-center gap-1.5">
                <div className="min-w-0 flex-1">
                  {/* 복귀 시간: 지정하면 복귀 불가는 자동 해제(상호 배타적). */}
                  <TimeField
                    value={draft.no_return ? "" : draft.return_time}
                    onChange={(v) =>
                      setDraft({
                        ...draft,
                        return_time: v,
                        no_return: v ? false : draft.no_return,
                      })
                    }
                  />
                </div>
                <button
                  type="button"
                  aria-pressed={draft.no_return}
                  onClick={() =>
                    setDraft({
                      ...draft,
                      no_return: !draft.no_return,
                      // 복귀 불가로 켜면 복귀 시간을 비운다.
                      return_time: !draft.no_return ? "" : draft.return_time,
                    })
                  }
                  title={
                    draft.no_return
                      ? "복귀 불가로 표시됩니다(🏠). 다시 누르면 해제됩니다"
                      : "누르면 이 일정 뒤 담당자가 복귀하지 않음(복귀 불가)을 팀에 표시합니다"
                  }
                  className="flex-none rounded-[6px] px-2.5 text-[var(--text-body-sm)]"
                  style={{
                    minHeight: 40,
                    border: `1px solid ${draft.no_return ? "var(--color-text-strong)" : "var(--color-divider)"}`,
                    background: draft.no_return ? "var(--color-sidebar-active)" : "var(--color-card)",
                    color: "var(--color-text-strong)",
                    fontWeight: draft.no_return ? 700 : 500,
                    whiteSpace: "nowrap",
                  }}
                >
                  {draft.no_return ? "🏠 복귀 불가" : "🏠 복귀 불가로"}
                </button>
              </div>
            </Field>
            <Field label="종료일(선택)">
              <DatePicker
                value={draft.end_date}
                onChange={(v) => setDraft({ ...draft, end_date: v })}
                placeholder="없음"
              />
            </Field>
            <Field label="반복">
              <Select
                className="h-9"
                value={draft.repeat_freq}
                options={REPEAT_OPTIONS}
                onChange={(e) => {
                  const freq = e.target.value as "" | CalendarRepeatFreq;
                  setDraft((dft) =>
                    dft
                      ? {
                          ...dft,
                          repeat_freq: freq,
                          // 매주/격주로 바꿀 때 요일이 비어 있으면 시작일 요일을 기본 선택.
                          repeat_weekdays:
                            (freq === "weekly" || freq === "biweekly") &&
                            dft.repeat_weekdays.length === 0
                              ? [pyWeekday(dft.event_date)]
                              : dft.repeat_weekdays,
                        }
                      : dft,
                  );
                }}
              />
            </Field>
            {draft.repeat_freq ? (
              <Field label="반복 종료일(선택)">
                <DatePicker
                  value={draft.repeat_until}
                  onChange={(v) => setDraft({ ...draft, repeat_until: v })}
                  placeholder="무기한"
                />
              </Field>
            ) : null}
            {draft.repeat_freq === "weekly" || draft.repeat_freq === "biweekly" ? (
              <Field label="반복 요일" full>
                <div className="flex flex-wrap gap-1.5">
                  {REPEAT_WEEKDAYS_KO.map((label, idx) => {
                    const on = draft.repeat_weekdays.includes(idx);
                    return (
                      <button
                        key={label}
                        type="button"
                        onClick={() => toggleRepeatWeekday(idx)}
                        className="flex h-9 w-9 items-center justify-center rounded-[6px] text-[var(--text-body-sm)]"
                        style={{
                          border: `1px solid ${on ? "var(--color-text-strong)" : "var(--color-divider)"}`,
                          background: on ? "var(--color-sidebar-active)" : "var(--color-card)",
                          color: "var(--color-text-strong)",
                          fontWeight: on ? 700 : 500,
                        }}
                      >
                        {label}
                      </button>
                    );
                  })}
                </div>
              </Field>
            ) : null}
            {editingId && draft.repeat_freq ? (
              <div
                className="rounded-[6px] px-2.5 py-1.5 text-[var(--text-meta)] sm:col-span-2"
                style={{
                  background: "var(--color-panel)",
                  color: "var(--color-text-muted)",
                  border: "1px solid var(--color-divider-soft)",
                }}
              >
                반복 일정입니다 — 수정·삭제는 모든 반복 회차에 함께 적용됩니다.
              </div>
            ) : null}
            {/* 다른 날짜에도 추가: 반복(규칙)과 별개로 같은 일정을 임의 날짜에 붙인다.
                수정·삭제는 이 일정 전체(추가된 날짜 포함)에 함께 적용된다. */}
            <Field label="다른 날짜에도 추가" full>
              <div className="flex flex-wrap items-center gap-1.5">
                <div className="min-w-[150px]">
                  <DatePicker
                    value={extraDateDraft}
                    onChange={(v) => {
                      // DatePicker에서 날짜를 고르면 바로 칩으로 추가한다.
                      setExtraDateDraft(v);
                      addExtraDate(v);
                    }}
                    placeholder="날짜 선택"
                  />
                </div>
                {extraDateDraft ? (
                  <ToolbarButton onClick={() => addExtraDate(extraDateDraft)}>추가</ToolbarButton>
                ) : null}
              </div>
              {draft.extra_dates.length ? (
                <>
                  {/* 대표(시작일) + 추가 날짜를 한 목록으로 보여 준다. 각 날짜의 ✕로
                      그 날짜만 빼고 나머지는 유지한다. 대표는 ✕ 시 다음(가장 이른)
                      날짜로 승격된다(반복 일정은 시작일=반복 기준점이라 비활성). */}
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {Array.from(new Set([draft.event_date, ...draft.extra_dates]))
                      .filter(Boolean)
                      .sort()
                      .map((iso) => {
                        const isMain = iso === draft.event_date;
                        const mainLocked = isMain && Boolean(draft.repeat_freq);
                        return (
                          <span
                            key={iso}
                            className="flex items-center gap-1 rounded-[6px] px-2 py-1 text-[var(--text-body-sm)]"
                            style={{
                              border: "1px solid var(--color-divider)",
                              background: isMain ? "var(--color-sidebar-active)" : "var(--color-card)",
                              color: "var(--color-text-strong)",
                              fontWeight: isMain ? 700 : 500,
                            }}
                          >
                            {isMain ? (
                              <span
                                className="rounded-[4px] px-1 text-[var(--text-meta)]"
                                style={{
                                  background: "var(--color-panel)",
                                  color: "var(--color-text-muted)",
                                  fontWeight: 600,
                                }}
                              >
                                대표
                              </span>
                            ) : null}
                            {fmtExtraDate(iso)}
                            <button
                              type="button"
                              disabled={mainLocked}
                              onClick={() => (isMain ? removeMainDate() : removeExtraDate(iso))}
                              aria-label={`${iso} 제거`}
                              title={
                                isMain
                                  ? mainLocked
                                    ? "반복 시작일이라 여기서 뺄 수 없어요 — 반복을 끄거나 일정 전체를 삭제하세요"
                                    : "대표 날짜만 빼고 나머지 날짜는 그대로 둡니다"
                                  : `${iso} 제거`
                              }
                              className="flex h-4 w-4 items-center justify-center rounded-full text-[var(--text-meta)]"
                              style={{
                                color: mainLocked ? "var(--color-divider)" : "var(--color-text-muted)",
                                cursor: mainLocked ? "not-allowed" : "pointer",
                              }}
                            >
                              ✕
                            </button>
                          </span>
                        );
                      })}
                  </div>
                  <p className="mt-1 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
                    각 날짜의 ✕로 그 날짜만 뺄 수 있어요(대표 포함) — 나머지 날짜는 그대로 유지됩니다.
                  </p>
                </>
              ) : (
                <p className="mt-1 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
                  이 일정을 그대로 붙일 날짜를 골라 추가하세요. 수정·삭제는 추가된 날짜에도 함께 적용됩니다.
                </p>
              )}
            </Field>
            <Field label="장소(선택)" full>
              <TextInput
                value={draft.location}
                placeholder="예: 중앙센터 404호 / 줌 링크"
                list="calendar-location-options"
                onChange={(e) => setDraft({ ...draft, location: e.target.value })}
              />
              <datalist id="calendar-location-options">
                {locationOptions.map((loc) => (
                  <option key={loc} value={loc} />
                ))}
              </datalist>
            </Field>
            <Field label="메모" full>
              <TextArea
                value={draft.description}
                onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              />
            </Field>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
