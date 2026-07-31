"use client";

/**
 * Dashboard UI kit — small token-driven form/table/modal primitives plus a
 * schema-driven CrudSection used across finance/team sections. Keeps the
 * dashboard pages declarative (just describe fields) instead of repeating
 * list/modal/delete boilerplate per entity.
 */

import * as React from "react";
import { Card, ToolbarButton, cx, inputClass, inputStyle } from "@/components/ui";

/* ------------------------------- helpers ------------------------------- */

export function money(n: number | null | undefined, currency = "KRW"): string {
  const v = typeof n === "number" ? n : 0;
  if (currency === "KRW") return `₩${Math.round(v).toLocaleString("ko-KR")}`;
  return `${v.toLocaleString("ko-KR")} ${currency}`;
}

export function labelStyle(): React.CSSProperties {
  return { color: "var(--color-text-muted)" };
}

// KPI 진행률 바. value 0..1 (null이면 미측정 "—"). tone = 상태색.
const PROGRESS_TONE: Record<string, string> = {
  on_track: "var(--color-progress, #57bf78)",
  at_risk: "#d9a514",
  off_track: "var(--color-fail-fg, #c0493b)",
  done: "var(--color-progress, #57bf78)",
  archived: "var(--color-divider)",
};
export function ProgressBar({
  value,
  tone = "on_track",
  height = 8,
}: {
  value: number | null | undefined;
  tone?: string;
  height?: number;
}) {
  const pct = value == null ? null : Math.max(0, Math.min(100, Math.round(value * 100)));
  const color = PROGRESS_TONE[tone] ?? PROGRESS_TONE.on_track;
  return (
    <div className="flex items-center gap-2">
      <div
        className="relative flex-1 overflow-hidden rounded-full"
        style={{ height, background: "var(--color-app-canvas)", border: "1px solid var(--color-divider-soft)" }}
      >
        <div
          className="absolute inset-y-0 left-0 rounded-full transition-[width]"
          style={{ width: `${pct ?? 0}%`, background: color }}
        />
      </div>
      <span
        className="w-9 shrink-0 text-right text-[var(--text-meta)] font-semibold tabular-nums"
        style={{ color: "var(--color-text-muted)" }}
      >
        {pct == null ? "—" : `${pct}%`}
      </span>
    </div>
  );
}

/* --------------------------------- Field ------------------------------- */

export function Field({
  label,
  children,
  full = false,
}: {
  label: string;
  children: React.ReactNode;
  full?: boolean;
}) {
  return (
    <label className={cx("flex flex-col gap-1", full && "sm:col-span-2")}>
      <span
        className="text-[var(--text-meta)] font-semibold"
        style={labelStyle()}
      >
        {label}
      </span>
      {children}
    </label>
  );
}

export function TextInput(
  props: React.InputHTMLAttributes<HTMLInputElement>,
) {
  return <input {...props} className={cx(inputClass, props.className)} style={{ ...inputStyle, ...props.style }} />;
}

export function TextArea(
  props: React.TextareaHTMLAttributes<HTMLTextAreaElement>,
) {
  return (
    <textarea
      {...props}
      className={cx(inputClass, "min-h-[72px] resize-y", props.className)}
      style={{ ...inputStyle, ...props.style }}
    />
  );
}

export function Select({
  options,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement> & {
  options: { value: string; label: string }[];
}) {
  return (
    <select {...props} className={cx(inputClass, props.className)} style={{ ...inputStyle, ...props.style }}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

/* --------------------------- anchored popover --------------------------- */
// Popovers inside modals get clipped by the modal's overflow:auto. We render
// them position:fixed (viewport-anchored, not clipped) and flip upward when
// there isn't enough room below the trigger.

export function useAnchoredPopover() {
  const anchorRef = React.useRef<HTMLButtonElement | null>(null);
  const popRef = React.useRef<HTMLDivElement | null>(null);
  const [rect, setRect] = React.useState<DOMRect | null>(null);
  const open = rect != null;

  const toggle = React.useCallback(() => {
    setRect((r) => (r ? null : anchorRef.current?.getBoundingClientRect() ?? null));
  }, []);
  const close = React.useCallback(() => setRect(null), []);

  React.useEffect(() => {
    if (!open) return;
    // 바깥 클릭/탭으로 닫기: pointerdown은 마우스·터치·펜을 모두 커버한다.
    // (mousedown만 듣던 과거엔 터치 기기에서 바깥을 탭해도 안 닫히는 버그가 있었다 —
    // 모바일 탭은 mousedown을 항상 발생시키지 않는다.)
    const onDoc = (e: PointerEvent) => {
      const t = e.target as Node;
      if (anchorRef.current?.contains(t) || popRef.current?.contains(t)) return;
      setRect(null);
    };
    const onMove = () => {
      if (anchorRef.current) setRect(anchorRef.current.getBoundingClientRect());
    };
    document.addEventListener("pointerdown", onDoc);
    window.addEventListener("scroll", onMove, true);
    window.addEventListener("resize", onMove);
    return () => {
      document.removeEventListener("pointerdown", onDoc);
      window.removeEventListener("scroll", onMove, true);
      window.removeEventListener("resize", onMove);
    };
  }, [open]);

  return { anchorRef, popRef, rect, open, toggle, close };
}

export function anchoredStyle(
  rect: DOMRect,
  width: number,
  estHeight = 280,
): React.CSSProperties {
  const gap = 6;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const below = vh - rect.bottom;
  const flipUp = below < estHeight && rect.top > below;
  let left = rect.left;
  if (left + width > vw - 8) left = vw - 8 - width;
  if (left < 8) left = 8;
  return {
    position: "fixed",
    left,
    width,
    ...(flipUp
      ? { bottom: vh - rect.top + gap, maxHeight: rect.top - gap - 8 }
      : { top: rect.bottom + gap, maxHeight: below - gap - 8 }),
  };
}

/* ------------------------------ Tag pills ------------------------------ */
// Notion-database style pastel select pills, shared across dashboard sections.

export type TagColor =
  | "gray"
  | "brown"
  | "orange"
  | "yellow"
  | "green"
  | "blue"
  | "purple"
  | "pink"
  | "red"
  | "teal";

export const TAG_COLORS: Record<TagColor, { bg: string; fg: string }> = {
  gray: { bg: "#e9e9e7", fg: "#4a4a47" },
  brown: { bg: "#eee0da", fg: "#64473a" },
  orange: { bg: "#fadec9", fg: "#603b2c" },
  yellow: { bg: "#fdecc8", fg: "#604b28" },
  green: { bg: "#dbeddb", fg: "#2a503a" },
  blue: { bg: "#d3e5ef", fg: "#28526b" },
  purple: { bg: "#e8deee", fg: "#492f5e" },
  pink: { bg: "#f5e0e9", fg: "#5e2a44" },
  red: { bg: "#ffe2dd", fg: "#6e2a26" },
  teal: { bg: "#d3e7e3", fg: "#28514a" },
};

/** TagColor token or a raw { bg, fg } pair (e.g. member colors). */
export type PillColor = TagColor | { bg: string; fg: string };

/* ----------------------------- Member colors ---------------------------- */
// 팀일정(/dashboard/calendar)과 동일한 멤버 색 규칙: DB의 member.color 우선,
// 없으면 등록 순서 기반 channel 팔레트 폴백. 멤버 이름이 보이는 모든 화면이
// 이 매핑을 공유한다.

export const MEMBER_FALLBACK_PALETTE = [
  "var(--channel-blue)",
  "var(--channel-mint)",
  "var(--channel-violet)",
  "var(--channel-amber)",
  "var(--channel-pink)",
  "var(--channel-slate)",
];

/** member_id와 name 양쪽을 키로 갖는 resolved 멤버 색 맵. */
export function memberColorMap(
  members: { member_id: string; name: string; color?: string | null }[],
): Map<string, string> {
  const map = new Map<string, string>();
  members.forEach((m, i) => {
    const c =
      m.color || MEMBER_FALLBACK_PALETTE[i % MEMBER_FALLBACK_PALETTE.length];
    map.set(m.member_id, c);
    map.set(m.name, c);
  });
  return map;
}

/** 멤버 색을 TagPill용 파스텔 bg/fg 쌍으로 변환. */
export function memberPillColor(color?: string | null): { bg: string; fg: string } {
  const c = color || "var(--channel-slate)";
  return {
    bg: `color-mix(in srgb, ${c} 24%, white)`,
    fg: `color-mix(in srgb, ${c} 55%, black)`,
  };
}

export function TagPill({
  label,
  color = "gray",
}: {
  label: string;
  color?: PillColor;
}) {
  const c = typeof color === "string" ? TAG_COLORS[color] : color;
  return (
    <span
      className="inline-flex items-center rounded-[4px] px-2 py-[2px] text-[var(--text-meta)] font-medium whitespace-nowrap"
      style={{ background: c.bg, color: c.fg }}
    >
      {label}
    </span>
  );
}

/**
 * Notion-style single-select with a tag popover. Clicking always reveals the
 * full option list (unlike a native datalist, which filters by the current
 * value). Supports free-text custom values when allowCustom is set.
 * variant "field" renders a bordered box; "inline" renders just the pill for
 * in-table editing.
 */
export function TagSelect({
  value,
  options,
  onChange,
  colorFor,
  allowCustom = true,
  clearable = true,
  placeholder = "비어 있음",
  variant = "field",
}: {
  value: string;
  options: string[];
  onChange: (v: string) => void;
  colorFor?: (v: string) => PillColor;
  allowCustom?: boolean;
  clearable?: boolean;
  placeholder?: string;
  variant?: "field" | "inline";
}) {
  const [q, setQ] = React.useState("");
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const color = (v: string): PillColor => colorFor?.(v) ?? "gray";

  const merged = React.useMemo(() => {
    const set = [...options];
    if (value && !set.includes(value)) set.unshift(value);
    return set;
  }, [options, value]);
  const filtered = q
    ? merged.filter((o) => o.toLowerCase().includes(q.toLowerCase()))
    : merged;
  const trimmed = q.trim();
  const showAdd = allowCustom && trimmed.length > 0 && !merged.includes(trimmed);

  function pick(v: string) {
    onChange(v);
    close();
    setQ("");
  }

  const popWidth = variant === "field" && rect ? rect.width : 220;

  return (
    <div className="relative">
      <button
        ref={anchorRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setQ("");
          toggle();
        }}
        className={cx(
          "flex items-center text-left text-[var(--text-body-sm)]",
          variant === "field"
            ? "w-full rounded-[8px] px-2.5"
            : "rounded-[4px] px-1 -mx-1 hover:bg-[var(--color-card)]",
        )}
        style={
          variant === "field"
            ? {
                minHeight: 40,
                border: "1px solid var(--color-divider)",
                background: "var(--color-card)",
              }
            : { minHeight: 28 }
        }
      >
        {value ? (
          <TagPill label={value} color={color(value)} />
        ) : (
          <span style={{ color: "var(--color-text-muted)" }}>{placeholder}</span>
        )}
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 flex flex-col overflow-hidden rounded-[10px]"
          style={{
            ...anchoredStyle(rect, popWidth),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div
            className="p-1.5"
            style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
          >
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="검색 또는 직접 입력"
              className="w-full rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] outline-none"
              style={{ background: "var(--color-app-canvas)" }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && showAdd) {
                  e.preventDefault();
                  pick(trimmed);
                }
              }}
            />
          </div>
          <div className="flex-1 overflow-y-auto p-1">
            {value && clearable ? (
              <button
                type="button"
                onClick={() => pick("")}
                className="flex w-full items-center rounded-[6px] px-2 py-1.5 text-left text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-muted)", minHeight: 32 }}
              >
                · 비우기
              </button>
            ) : null}
            {filtered.map((o) => (
              <button
                key={o}
                type="button"
                onClick={() => pick(o)}
                className="flex w-full items-center rounded-[6px] px-2 py-1.5 text-left hover:bg-[var(--color-app-canvas)]"
                style={{ minHeight: 32 }}
              >
                <TagPill label={o} color={color(o)} />
              </button>
            ))}
            {showAdd ? (
              <button
                type="button"
                onClick={() => pick(trimmed)}
                className="flex w-full items-center gap-1 rounded-[6px] px-2 py-1.5 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ minHeight: 32, color: "var(--color-text-muted)" }}
              >
                + &ldquo;{trimmed}&rdquo; 추가
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------ DatePicker ----------------------------- */
// Notion-style month-grid date popover (replaces the flaky native date input).
const WEEKDAY_KR = ["일", "월", "화", "수", "목", "금", "토"];

function pad2(n: number): string {
  return `${n}`.padStart(2, "0");
}
function toIso(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}
function fmtDateLabel(iso: string, placeholder: string): string {
  if (!iso) return placeholder;
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return placeholder;
  return `${pad2(d.getMonth() + 1)}.${pad2(d.getDate())}(${WEEKDAY_KR[d.getDay()]})`;
}

export function DatePicker({
  value,
  onChange,
  placeholder = "날짜 선택",
  clearable = true,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  clearable?: boolean;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const base = value ? new Date(`${value}T00:00:00`) : new Date();
  const [view, setView] = React.useState(
    () => new Date(base.getFullYear(), base.getMonth(), 1),
  );

  function toggleOpen() {
    if (!open && value) {
      const d = new Date(`${value}T00:00:00`);
      if (!Number.isNaN(d.getTime()))
        setView(new Date(d.getFullYear(), d.getMonth(), 1));
    }
    toggle();
  }

  const y = view.getFullYear();
  const m = view.getMonth();
  const firstDow = new Date(y, m, 1).getDay();
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  const cells: (number | null)[] = [];
  for (let i = 0; i < firstDow; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);

  return (
    <div className="relative">
      <button
        ref={anchorRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          toggleOpen();
        }}
        className="flex w-full items-center rounded-[8px] px-2.5 text-left text-[var(--text-body-sm)]"
        style={{
          minHeight: 40,
          border: "1px solid var(--color-divider)",
          background: "var(--color-card)",
          color: value ? "var(--color-text-strong)" : "var(--color-text-muted)",
        }}
      >
        {fmtDateLabel(value, placeholder)}
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 overflow-y-auto rounded-[10px] p-2"
          style={{
            ...anchoredStyle(rect, 248, 320),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-1 flex items-center justify-between px-1">
            <button
              type="button"
              onClick={() => setView(new Date(y, m - 1, 1))}
              className="flex h-7 w-7 items-center justify-center rounded-[6px] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              ‹
            </button>
            <span
              className="text-[var(--text-body-sm)] font-semibold"
              style={{ color: "var(--color-text-strong)" }}
            >
              {y}년 {m + 1}월
            </span>
            <button
              type="button"
              onClick={() => setView(new Date(y, m + 1, 1))}
              className="flex h-7 w-7 items-center justify-center rounded-[6px] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              ›
            </button>
          </div>
          <div className="grid grid-cols-7 gap-0.5">
            {WEEKDAY_KR.map((w) => (
              <div
                key={w}
                className="py-1 text-center text-[var(--text-badge)] font-semibold"
                style={{ color: "var(--color-text-muted)" }}
              >
                {w}
              </div>
            ))}
            {cells.map((d, i) => {
              if (d === null) return <div key={`b${i}`} />;
              const iso = toIso(new Date(y, m, d));
              const selected = iso === value;
              return (
                <button
                  key={iso}
                  type="button"
                  onClick={() => {
                    onChange(iso);
                    close();
                  }}
                  className="flex h-8 items-center justify-center rounded-[6px] text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                  style={
                    selected
                      ? { background: "var(--color-text-strong)", color: "var(--color-card)" }
                      : { color: "var(--color-text-body)" }
                  }
                >
                  {d}
                </button>
              );
            })}
          </div>
          {clearable && value ? (
            <button
              type="button"
              onClick={() => {
                onChange("");
                close();
              }}
              className="mt-1 w-full rounded-[6px] py-1.5 text-center text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              지정 안 함
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/* --------------------------------- Modal ------------------------------- */

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center sm:items-center"
      style={{ background: "rgba(0,0,0,0.32)" }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-[560px] rounded-t-[var(--radius-modal)] sm:rounded-[var(--radius-modal)]"
        style={{
          background: "var(--color-modal)",
          boxShadow: "var(--shadow-modal)",
          maxHeight: "90dvh",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          className="flex items-center justify-between px-4 py-3"
          style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
        >
          <h2
            className="text-[var(--text-body)] font-bold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {title}
          </h2>
          <button
            onClick={onClose}
            aria-label="닫기"
            className="flex h-8 w-8 items-center justify-center rounded-[6px]"
            style={{ color: "var(--color-text-muted)" }}
          >
            ✕
          </button>
        </div>
        <div className="overflow-y-auto px-4 py-4" style={{ maxHeight: "calc(90dvh - 116px)" }}>
          {children}
        </div>
        {footer ? (
          <div
            className="flex items-center justify-end gap-2 px-4 py-3"
            style={{ borderTop: "1px solid var(--color-divider-soft)" }}
          >
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/* ------------------------------- Section ------------------------------- */

export function Section({
  title,
  subtitle,
  action,
  children,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Card className="p-3 sm:p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h2
            className="text-[var(--text-body)] font-bold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {title}
          </h2>
          {subtitle ? (
            <p className="text-[var(--text-meta)]" style={labelStyle()}>
              {subtitle}
            </p>
          ) : null}
        </div>
        {action}
      </div>
      {children}
    </Card>
  );
}

/* ----------------------------- CrudSection ----------------------------- */

export type FieldType = "text" | "number" | "date" | "select" | "textarea";

export interface FieldSpec<T> {
  key: Extract<keyof T, string>;
  label: string;
  type?: FieldType;
  options?: { value: string; label: string }[];
  required?: boolean;
  placeholder?: string;
  hideInTable?: boolean;
  full?: boolean;
  render?: (row: T) => React.ReactNode;
}

export function CrudSection<T extends object>({
  title,
  subtitle,
  fields,
  rows,
  idKey,
  makeEmpty,
  toInput,
  onCreate,
  onUpdate,
  onDelete,
  canWrite,
  emptyText = "아직 항목이 없습니다.",
  cardRender,
  cardGridClassName = "grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3",
}: {
  title: string;
  subtitle?: string;
  fields: FieldSpec<T>[];
  rows: T[];
  idKey: Extract<keyof T, string>;
  makeEmpty: () => Record<string, unknown>;
  toInput: (draft: Record<string, unknown>) => Record<string, unknown>;
  onCreate: (input: Record<string, unknown>) => Promise<void>;
  onUpdate: (id: string, input: Record<string, unknown>) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  canWrite: boolean;
  emptyText?: string;
  // When provided, rows render as a card grid (each card = cardRender(row))
  // instead of a table. Add/edit/delete still use the shared modal below.
  cardRender?: (row: T) => React.ReactNode;
  cardGridClassName?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const [editId, setEditId] = React.useState<string | null>(null);
  const [draft, setDraft] = React.useState<Record<string, unknown>>({});
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);

  const tableFields = fields.filter((f) => !f.hideInTable);
  const get = (row: T, key: string) => (row as Record<string, unknown>)[key];

  function openCreate() {
    setEditId(null);
    setDraft(makeEmpty());
    setErr(null);
    setOpen(true);
  }
  function openEdit(row: T) {
    setEditId(String(get(row, idKey)));
    const d: Record<string, unknown> = {};
    fields.forEach((f) => {
      d[f.key] = get(row, f.key) ?? "";
    });
    setDraft(d);
    setErr(null);
    setOpen(true);
  }

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const input = toInput(draft);
      if (editId) await onUpdate(editId, input);
      else await onCreate(input);
      setOpen(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    if (!window.confirm("삭제하시겠습니까?")) return;
    try {
      await onDelete(id);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "삭제 실패");
    }
  }

  function setField(key: string, value: unknown) {
    setDraft((d) => ({ ...d, [key]: value }));
  }

  return (
    <Section
      title={title}
      subtitle={subtitle}
      action={
        canWrite ? (
          <ToolbarButton tone="primary" onClick={openCreate}>
            + 추가
          </ToolbarButton>
        ) : null
      }
    >
      {rows.length === 0 ? (
        <p className="py-6 text-center text-[var(--text-body-sm)]" style={labelStyle()}>
          {emptyText}
        </p>
      ) : cardRender ? (
        <div className={cardGridClassName}>
          {rows.map((row) => (
            <div
              key={String(get(row, idKey))}
              className={cx(
                "group relative rounded-[12px] p-4 transition-shadow",
                canWrite && "cursor-pointer",
              )}
              style={{
                background: "var(--color-card)",
                border: "1px solid var(--color-divider)",
                boxShadow: "var(--shadow-card, 0 1px 2px rgba(0,0,0,0.04))",
              }}
              onClick={canWrite ? () => openEdit(row) : undefined}
            >
              {cardRender(row)}
              {canWrite ? (
                // Floating overlay pill — solid background so it sits cleanly ON
                // TOP of card content on hover; no reserved gutter (no pr-16).
                <div
                  className="absolute right-1.5 top-1.5 flex items-center gap-1 rounded-[8px] px-1 py-0.5 opacity-0 transition-opacity group-hover:opacity-100"
                  style={{
                    background: "var(--color-card)",
                    border: "1px solid var(--color-divider)",
                    boxShadow: "var(--shadow-card, 0 1px 2px rgba(0,0,0,0.04))",
                  }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    onClick={() => openEdit(row)}
                    className="rounded-[5px] px-1.5 py-0.5 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    수정
                  </button>
                  <button
                    onClick={() => remove(String(get(row, idKey)))}
                    className="rounded-[5px] px-1.5 py-0.5 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
                    style={{ color: "var(--color-fail-fg)" }}
                  >
                    삭제
                  </button>
                </div>
              ) : null}
            </div>
          ))}
        </div>
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
                {tableFields.map((f) => (
                  <th
                    key={f.key}
                    className="px-3 py-2 text-left font-medium whitespace-nowrap"
                    style={labelStyle()}
                  >
                    {f.label}
                  </th>
                ))}
                {canWrite ? <th className="w-px px-2" /> : null}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={String(get(row, idKey))}
                  className={cx(
                    "group hover:bg-[var(--color-app-canvas)]",
                    canWrite && "cursor-pointer",
                  )}
                  style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
                  onClick={canWrite ? () => openEdit(row) : undefined}
                >
                  {tableFields.map((f) => (
                    <td
                      key={f.key}
                      className="px-3 py-2.5 align-middle"
                      style={{ color: "var(--color-text-body)" }}
                    >
                      {f.render
                        ? f.render(row)
                        : formatCell(get(row, f.key), f)}
                    </td>
                  ))}
                  {canWrite ? (
                    <td
                      className="px-2 py-2.5 text-right whitespace-nowrap"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <button
                        onClick={() => openEdit(row)}
                        className="mr-2 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        수정
                      </button>
                      <button
                        onClick={() => remove(String(get(row, idKey)))}
                        className="text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                        style={{ color: "var(--color-fail-fg)" }}
                      >
                        삭제
                      </button>
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Modal
        open={open}
        title={editId ? `${title} 수정` : `${title} 추가`}
        onClose={() => setOpen(false)}
        footer={
          <>
            <ToolbarButton onClick={() => setOpen(false)} disabled={busy}>
              취소
            </ToolbarButton>
            <ToolbarButton tone="primary" onClick={save} disabled={busy}>
              {busy ? "저장 중…" : "저장"}
            </ToolbarButton>
          </>
        }
      >
        {err ? (
          <p className="mb-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-fail-fg)" }}>
            {err}
          </p>
        ) : null}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {fields.map((f) => (
            <Field key={f.key} label={f.label} full={f.full || f.type === "textarea"}>
              {f.type === "textarea" ? (
                <TextArea
                  value={String(draft[f.key] ?? "")}
                  placeholder={f.placeholder}
                  onChange={(e) => setField(f.key, e.target.value)}
                />
              ) : f.type === "select" ? (
                <Select
                  value={String(draft[f.key] ?? "")}
                  options={f.options ?? []}
                  onChange={(e) => setField(f.key, e.target.value)}
                />
              ) : (
                <TextInput
                  type={f.type === "number" ? "number" : f.type === "date" ? "date" : "text"}
                  value={String(draft[f.key] ?? "")}
                  placeholder={f.placeholder}
                  onChange={(e) => setField(f.key, e.target.value)}
                />
              )}
            </Field>
          ))}
        </div>
      </Modal>
    </Section>
  );
}

function formatCell<T>(value: unknown, f: FieldSpec<T>): React.ReactNode {
  if (value === null || value === undefined || value === "") return "—";
  if (f.type === "number") {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString("ko-KR") : String(value);
  }
  if (f.type === "select" && f.options) {
    return f.options.find((o) => o.value === String(value))?.label ?? String(value);
  }
  return String(value);
}

/* ------------------------- OKR 채점 프리미티브 ------------------------- */
// 0~10 달성 점수/1~10 신뢰도 공용. 톤 밴드는 OKR 관례(0.7=양호)를 따른다.
// 주의: 0은 유효한 점수 — 호출부는 항상 `value != null`로 판별한다.

export function scoreTone(n: number): { bg: string; fg: string } {
  if (n <= 3) return TAG_COLORS.red;
  if (n <= 6) return TAG_COLORS.yellow;
  return TAG_COLORS.green;
}

/** 칩 트리거 → 0~10(min=1이면 1~10) 버튼 그리드 팝오버. 탭 1회로 기록. */
export function ScorePicker({
  value,
  onChange,
  min = 0,
  label = "점수",
  allowClear = true,
  disabled = false,
  title,
}: {
  value: number | null | undefined;
  onChange: (v: number | null) => void;
  min?: 0 | 1;
  label?: string;
  allowClear?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const has = value != null; // 0 유효 — truthiness 금지
  const tone = has ? scoreTone(value as number) : null;
  const nums: number[] = [];
  for (let i = min; i <= 10; i += 1) nums.push(i);
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        disabled={disabled}
        title={title}
        className="shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-bold leading-[18px]"
        style={
          tone
            ? { background: tone.bg, color: tone.fg, borderColor: "transparent" }
            : {
                background: "transparent",
                color: "var(--color-text-muted)",
                borderColor: "var(--color-divider)",
              }
        }
      >
        {has ? `${value}/10` : label}
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="rounded-[10px] border p-2 shadow-lg"
          style={{
            ...anchoredStyle(rect, 316, 150),
            zIndex: 70,
            background: "var(--color-panel)",
            borderColor: "var(--color-divider)",
          }}
        >
          <div className="flex flex-wrap gap-1.5">
            {nums.map((n) => {
              const t = scoreTone(n);
              const selected = value === n;
              return (
                <button
                  key={n}
                  type="button"
                  onClick={() => {
                    onChange(n);
                    close();
                  }}
                  className="h-11 w-11 rounded-[8px] text-[13px] font-bold"
                  style={{
                    background: t.bg,
                    color: t.fg,
                    outline: selected ? "2px solid var(--color-accent)" : "none",
                    outlineOffset: 1,
                  }}
                >
                  {n}
                </button>
              );
            })}
            {allowClear && has ? (
              <button
                type="button"
                onClick={() => {
                  onChange(null);
                  close();
                }}
                className="h-11 rounded-[8px] border px-2.5 text-[12px]"
                style={{
                  color: "var(--color-text-muted)",
                  borderColor: "var(--color-divider)",
                }}
              >
                지움
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}

/** 주간 신뢰도 추이 스파크라인 — y 도메인 1~10 고정, 결측 주는 선을 끊는다(보간 금지). */
export function ConfidenceSparkline({
  points,
  width = 96,
  height = 24,
}: {
  points: { week_start: string; confidence: number }[];
  width?: number;
  height?: number;
}) {
  if (points.length === 0) {
    return (
      <span className="text-[11px]" style={{ color: "var(--color-text-faint)" }}>
        기록 없음
      </span>
    );
  }
  const DAY = 86400000;
  const times = points.map((p) => new Date(`${p.week_start}T00:00:00`).getTime());
  const min = times[0];
  const span = Math.max(times[times.length - 1] - min, 1);
  const x = (t: number) => 3 + (width - 6) * ((t - min) / span);
  const y = (c: number) => height - 3 - (height - 6) * ((c - 1) / 9);
  const segs: { x: number; y: number }[][] = [];
  let cur: { x: number; y: number }[] = [];
  points.forEach((p, i) => {
    if (i > 0 && times[i] - times[i - 1] > 8 * DAY) {
      segs.push(cur);
      cur = [];
    }
    cur.push({ x: x(times[i]), y: y(p.confidence) });
  });
  segs.push(cur);
  const last = points[points.length - 1];
  const color = scoreTone(last.confidence).fg;
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-label={`신뢰도 추이 (최근 ${last.confidence}/10)`}
      className="shrink-0"
    >
      {segs
        .filter((s) => s.length >= 2)
        .map((s, i) => (
          <polyline
            key={i}
            points={s.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ")}
            fill="none"
            stroke={color}
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))}
      {segs.flat().map((p, i, all) => (
        <circle
          key={i}
          cx={p.x}
          cy={p.y}
          r={i === all.length - 1 ? 2.5 : 1.5}
          fill={color}
        />
      ))}
    </svg>
  );
}
