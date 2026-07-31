"use client";

/**
 * 프로젝트 트래킹 UI (마이그레이션 0094 대응):
 * - 건강 신호(HealthSelect/HealthDot): on_track|at_risk|off_track 세그먼트.
 * - 목표일 D-day(DdayChip/TargetCountdown): 지남=빨강, 3일 내=주황.
 * - 활동 피드(ActivityFeedSection): 필드 변경·요구조건·배정·보드 행을
 *   "누가 · 무엇을 · 언제" 한 줄 문장으로 렌더.
 */

import { useEffect, useState } from "react";
import { api, type DashboardProject, type ProjectActivityItem } from "@/lib/api";

type Health = DashboardProject["health"];

export const HEALTH_LABEL: Record<string, string> = {
  on_track: "순항",
  at_risk: "주의",
  off_track: "위험",
};

const HEALTH_COLOR: Record<string, { fg: string; bg: string }> = {
  on_track: { fg: "#15803d", bg: "color-mix(in srgb, #22c55e 14%, transparent)" },
  at_risk: { fg: "#b45309", bg: "color-mix(in srgb, #f59e0b 16%, transparent)" },
  off_track: { fg: "#dc2626", bg: "color-mix(in srgb, #ef4444 13%, transparent)" },
};

/** 건강 신호 선택 — 라벨은 한국어, 저장값은 서버 enum. */
export function HealthSelect({
  value,
  onChange,
}: {
  value: Health;
  onChange: (v: Health) => void;
}) {
  return (
    <span className="flex h-7 items-center gap-1 px-1">
      {(Object.keys(HEALTH_LABEL) as Array<Exclude<Health, null>>).map((key) => {
        const active = value === key;
        const color = HEALTH_COLOR[key];
        return (
          <button
            key={key}
            type="button"
            onClick={() => onChange(active ? null : key)}
            className="rounded-full px-2 py-[2px] text-[var(--text-meta)] font-semibold transition-colors"
            style={{
              background: active ? color.bg : "transparent",
              color: active ? color.fg : "var(--color-text-faint)",
              border: `1px solid ${active ? "transparent" : "var(--color-divider-soft)"}`,
            }}
          >
            {HEALTH_LABEL[key]}
          </button>
        );
      })}
    </span>
  );
}

/** 목록용 건강 점+라벨. 미평가면 빈칸. */
export function HealthDot({ value }: { value: Health }) {
  if (!value) return <span style={{ color: "var(--color-text-faint)" }}>—</span>;
  const color = HEALTH_COLOR[value];
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-1.5 py-[1px] text-[var(--text-meta)] font-semibold"
      style={{ background: color.bg, color: color.fg }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color.fg }} />
      {HEALTH_LABEL[value]}
    </span>
  );
}

function ddayInfo(dateStr: string): { days: number; label: string } | null {
  const parsed = new Date(`${dateStr.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((parsed.getTime() - today.getTime()) / 86400000);
  const dday = days === 0 ? "D-day" : days > 0 ? `D-${days}` : `D+${-days}`;
  return { days, label: `${parsed.getMonth() + 1}/${parsed.getDate()} · ${dday}` };
}

/** 목표일 D-day 칩 (목록/카드 공용). */
export function DdayChip({ date }: { date: string | null }) {
  if (!date) return <span style={{ color: "var(--color-text-faint)" }}>—</span>;
  const info = ddayInfo(date);
  if (!info) return <span style={{ color: "var(--color-text-faint)" }}>{date}</span>;
  const overdue = info.days < 0;
  const soon = info.days >= 0 && info.days <= 3;
  return (
    <span
      className="rounded-[4px] px-1.5 py-[1px] text-[var(--text-meta)] font-semibold"
      style={{
        background: overdue
          ? HEALTH_COLOR.off_track.bg
          : soon
            ? HEALTH_COLOR.at_risk.bg
            : "var(--color-app-canvas)",
        color: overdue
          ? HEALTH_COLOR.off_track.fg
          : soon
            ? HEALTH_COLOR.at_risk.fg
            : "var(--color-text-muted)",
      }}
    >
      {info.label}
    </span>
  );
}

/** 상세 헤더용: 목표일 대비 남은 일수 요약(입력창 옆 보조 표시). */
export function TargetCountdown({ target }: { target: string | null }) {
  if (!target) return null;
  const info = ddayInfo(target);
  if (!info) return null;
  return <DdayChip date={target} />;
}

/* ------------------------------ 활동 피드 ------------------------------ */

const FIELD_LABEL: Record<string, string> = {
  name: "이름",
  status: "상태",
  se_stage: "SE 단계",
  health: "건강",
  start_date: "시작일",
  target_date: "목표일",
  owner_member_id: "오너",
  parent_project_id: "상위 프로젝트",
  description: "요약",
};

/** uuid처럼 값 자체가 읽을거리가 아닌 필드는 before→after를 감춘다. */
const OPAQUE_FIELDS = new Set(["owner_member_id", "parent_project_id"]);

function healthText(v: string | null): string | null {
  return v ? (HEALTH_LABEL[v] ?? v) : v;
}

function describe(item: ProjectActivityItem): string {
  const q = (v: string | null) => (v ? `'${v}'` : "없음");
  if (item.entity_type === "project") {
    const label = FIELD_LABEL[item.field ?? ""] ?? item.field ?? "필드";
    if (item.field === "description") return "요약을 수정했습니다";
    if (item.field && OPAQUE_FIELDS.has(item.field)) return `${label}를 변경했습니다`;
    const before = item.field === "health" ? healthText(item.before) : item.before;
    const after = item.field === "health" ? healthText(item.after) : item.after;
    return `${label}을(를) ${q(before)} → ${q(after)}로 변경했습니다`;
  }
  if (item.entity_type === "requirement") {
    if (item.action === "create") return `요구조건을 추가했습니다: "${item.after ?? ""}"`;
    if (item.action === "delete") return `요구조건을 삭제했습니다: "${item.before ?? ""}"`;
    return `요구조건 상태를 ${q(item.before)} → ${q(item.after)}로 바꿨습니다`;
  }
  if (item.entity_type === "assignment") {
    return item.action === "assign"
      ? `멤버를 배정했습니다: ${item.after ?? ""}`
      : `멤버 배정을 해제했습니다: ${item.before ?? ""}`;
  }
  if (item.entity_type === "row") {
    const title = item.after ? `"${item.after}"` : "항목";
    return item.action === "create"
      ? `보드에 ${title}을(를) 추가했습니다`
      : `보드에서 ${title}을(를) 삭제했습니다`;
  }
  return `${item.entity_type} ${item.action}`;
}

function relativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diff = Math.max(0, Date.now() - t);
  const min = Math.floor(diff / 60000);
  if (min < 1) return "방금";
  if (min < 60) return `${min}분 전`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours}시간 전`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}일 전`;
  const d = new Date(t);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

/** 프로젝트 상세의 활동 피드 섹션 (최근 30건, 읽기 전용). */
export function ActivityFeedSection({ projectId }: { projectId: string }) {
  const [items, setItems] = useState<ProjectActivityItem[] | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .listProjectActivity(projectId, 30)
      .then((rows) => {
        if (alive) setItems(rows);
      })
      .catch(() => {
        if (alive) setItems([]);
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  return (
    <section>
      <h3
        className="mb-2 text-[var(--text-meta)] font-bold uppercase tracking-wide"
        style={{ color: "var(--color-text-muted)" }}
      >
        활동 {items?.length ? items.length : ""}
      </h3>
      {items == null ? (
        <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-faint)" }}>
          불러오는 중…
        </p>
      ) : items.length === 0 ? (
        <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-faint)" }}>
          아직 기록된 활동이 없습니다. 상태·기간·요구조건·보드 변경이 여기 쌓입니다.
        </p>
      ) : (
        <ul className="grid gap-1.5">
          {items.map((item) => (
            <li key={item.activity_id} className="flex min-w-0 items-baseline gap-2">
              <span
                className="shrink-0 text-[var(--text-meta)] font-semibold"
                style={{ color: "var(--color-text-strong)" }}
              >
                {item.actor_name || "시스템"}
              </span>
              <span
                className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                style={{ color: "var(--color-text-body)" }}
                title={describe(item)}
              >
                {describe(item)}
              </span>
              <span
                className="shrink-0 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-faint)" }}
              >
                {relativeTime(item.created_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
