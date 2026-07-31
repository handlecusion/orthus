"use client";

/**
 * 프로젝트 진행률 표시 — 소속 데이터베이스 status 속성(complete 그룹) 집계 기반.
 * 목록/상세/하위 프로젝트 카드가 같은 바를 공유한다. 루트 프로젝트는 자신 +
 * 하위 프로젝트 집계를 합산해 보여준다(aggregateProgress).
 */

import type { ProjectProgress } from "@/lib/api";

/** 프로젝트 자신 + 하위 프로젝트들의 진행률 합산. 집계 대상 행이 없으면 null. */
export function aggregateProgress(
  progressMap: Record<string, ProjectProgress>,
  projectId: string,
  childIds: string[] = [],
): { total: number; complete: number; inProgress: number; percent: number } | null {
  let total = 0;
  let complete = 0;
  let inProgress = 0;
  for (const id of [projectId, ...childIds]) {
    const p = progressMap[id];
    if (!p) continue;
    total += p.total;
    complete += p.complete;
    inProgress += p.in_progress;
  }
  if (total === 0) return null;
  return { total, complete, inProgress, percent: Math.round((complete / total) * 100) };
}

export function ProgressBar({
  value,
  compact = false,
}: {
  value: { total: number; complete: number; inProgress: number; percent: number } | null;
  compact?: boolean;
}) {
  if (!value) {
    return (
      <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
        —
      </span>
    );
  }
  const donePct = Math.min(100, (value.complete / value.total) * 100);
  const doingPct = Math.min(100 - donePct, (value.inProgress / value.total) * 100);
  return (
    <span className="inline-flex items-center gap-2" title={`완료 ${value.complete} · 진행 중 ${value.inProgress} · 전체 ${value.total}`}>
      <span
        className="relative inline-block overflow-hidden rounded-full"
        style={{
          width: compact ? 64 : 96,
          height: 6,
          background: "var(--color-divider-soft, var(--color-divider))",
        }}
      >
        <span
          className="absolute inset-y-0 left-0"
          style={{ width: `${donePct}%`, background: "var(--color-success-fg, #4f9d69)" }}
        />
        <span
          className="absolute inset-y-0"
          style={{
            left: `${donePct}%`,
            width: `${doingPct}%`,
            background: "var(--color-info-fg, #5d9bd8)",
            opacity: 0.55,
          }}
        />
      </span>
      <span
        className="text-[var(--text-meta)] tabular-nums"
        style={{ color: "var(--color-text-muted)" }}
      >
        {value.percent}%
      </span>
    </span>
  );
}
