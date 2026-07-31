"use client";

/**
 * K5.0 chip-list view of the read-only KG page graph — rel-grouped chips.
 * Sibling of graph-ring-panel.tsx; both consume graph-shared helpers so K4b
 * /ask and K7 panels can reuse either view. Used as the 'list' toggle option
 * and as the graph-render error fallback.
 */
import { useState } from "react";
import Link from "next/link";
import { Chip } from "@/components/ui";
import {
  type GraphChip,
  type GraphGroup,
  conflictStatusLabel,
  conflictStatusResolved,
  CONFLICT_PLACEHOLDER_LABEL,
  formatDetectedAt,
  GRAPH_COPY,
  graphChipText,
  isClickableWikiNode,
  isOwnPersonalNode,
  isPlaceholderConflictCounterpart,
  nodeFillColor,
  nodeKindLabel,
  PERSONAL_NODE_LABEL,
  relLabelText,
  relTone,
  wikiNodeHref,
} from "@/components/wiki/graph-shared";

// FE7 — 모순 해소 진입점. WikiTask 리뷰(`/wiki/tasks`)는 어느 표면(페이지 패널·/ask 블록)에서도
// 항상 가용하므로 dead-end가 없다(K8 계획 FE7 "전체 태스크 보기" 안전 타겟).
const RESOLVE_TASKS_HREF = "/wiki/tasks";

// 칩 뷰는 built에서 groups/truncated만 필요(currentNodeId 불필요) — K4b/K7 재사용 시 seam 명시.
export type GraphChipGroupsProps = {
  built: { groups: GraphGroup[]; truncated: boolean };
  busy: boolean;
};

export function GraphChipGroups({ built, busy }: GraphChipGroupsProps) {
  return (
    <div
      className="space-y-3"
      style={busy ? { opacity: 0.6, transition: "opacity 120ms" } : undefined}
    >
      {built.groups.map((group) => (
        <div className="min-w-0" key={group.rel}>
          <div className="mb-1 flex flex-wrap items-center gap-1.5">
            <Chip tone={relTone(group.rel)}>
              {relLabelText(group.rel)}
            </Chip>
            {group.groupTotal > group.chips.length ? (
              <span
                className="text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                {group.chips.length}/{group.groupTotal}+
              </span>
            ) : null}
          </div>
          {group.rel === "CONFLICTS_WITH" ? (
            // 모순 그룹은 세로 스택 — 각 행이 칩 + (FE6) 이유/탐지시각 펼치기 + (FE7) 해소 링크.
            <div className="space-y-1.5">
              {group.chips.map((chip) => (
                <ConflictChipRow chip={chip} key={`${group.rel} ${chip.node.id}`} />
              ))}
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {group.chips.map((chip) => (
                <GraphChipView chip={chip} key={`${group.rel} ${chip.node.id}`} />
              ))}
            </div>
          )}
        </div>
      ))}
      {built.truncated ? (
        <p
          className="text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {GRAPH_COPY.truncated}
        </p>
      ) : null}
    </div>
  );
}

function GraphChipView({ chip }: { chip: GraphChip }) {
  const { node } = chip;
  const text = graphChipText(node);
  // FE4 — placeholder conflict counterpart는 KG에 본문이 없으므로 slug를 주장 문장으로 dump하지
  // 않고 명시 라벨 + slug(식별자, muted)로 표기한다(클라이언트 #2/round2 #1).
  const placeholderCounterpart = isPlaceholderConflictCounterpart(chip);
  const href = wikiNodeHref(node);
  const clickable = isClickableWikiNode(node);
  // 택 B′ — 배지 배경을 종류 tint로(3-뷰 fill=종류 일관). 클릭 신호는 wrapper 배경
  // (Link=sidebar-active vs span=card+divider)이 담당하므로 채널이 겹치지 않는다. 텍스트는 공용
  // nodeKindLabel(인스펙터·explorer·ring과 표기 일치).
  const kindFill = nodeFillColor(node.label);
  const inner = (
    <>
      <span
        className="inline-flex shrink-0 items-center rounded-[4px] px-1.5 py-[1px] text-[var(--text-meta)] font-medium"
        style={{
          background: `color-mix(in srgb, ${kindFill} 22%, transparent)`,
          color: "var(--color-text-body)",
          border: `1px solid color-mix(in srgb, ${kindFill} 45%, transparent)`,
        }}
      >
        {nodeKindLabel(node)}
      </span>
      {/* K7.3 — ring 뷰 personal halo와 parity. caller 본인 personal 노드만 표식. */}
      {isOwnPersonalNode(node) ? <Chip tone="accent">{PERSONAL_NODE_LABEL}</Chip> : null}
      {/* 충돌 역할 배지(이 페이지 vs 상대) — 양쪽 주장을 보여주되 어느 편인지 구분. */}
      {chip.conflictSide ? (
        <Chip tone={chip.conflictSide === "own" ? "neutral" : "accent"}>
          {chip.conflictSide === "own" ? "이 페이지" : "상대"}
        </Chip>
      ) : null}
      {/* A7 — 3-뷰 일관: **충돌 칩(conflictSide 있음)** 에만 상태어를 단다. conflictSide는
          CONFLICTS_WITH rel에서만 세팅되므로 이것이 진짜 충돌 신호다. status property 부재여도
          "확인이 필요한 충돌"을 달고, status가 있으면 해소/미해소로 평문화(raw 미노출). 비-충돌 rel이
          우연히 status를 실어도(conflictSide 없음) 충돌로 오표기하지 않는다. */}
      {chip.conflictSide != null ? (
        <Chip tone={conflictStatusResolved(chip.status) ? "pass" : "warn"}>
          {conflictStatusLabel(chip.status)}
        </Chip>
      ) : null}
      {placeholderCounterpart ? (
        <>
          <span className="min-w-0 max-w-full break-words">{CONFLICT_PLACEHOLDER_LABEL}</span>
          <span
            className="min-w-0 max-w-full break-all font-mono text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {text}
          </span>
        </>
      ) : (
        <span className="min-w-0 max-w-full break-all">{text}</span>
      )}
    </>
  );
  if (clickable && href) {
    return (
      <Link
        className="inline-flex max-w-full min-w-0 items-center gap-1 rounded-[4px] px-1.5 py-[2px] text-[var(--text-meta)] font-medium transition"
        href={href}
        style={{
          background: "var(--color-sidebar-active)",
          color: "var(--color-text-body)",
        }}
      >
        {inner}
      </Link>
    );
  }
  return (
    <span
      className="inline-flex max-w-full min-w-0 cursor-default items-center gap-1 rounded-[4px] px-1.5 py-[2px] text-[var(--text-meta)] font-medium"
      style={{
        background: "var(--color-card)",
        color: "var(--color-text-body)",
        border: "1px solid var(--color-divider)",
      }}
    >
      {inner}
    </span>
  );
}

// K8 FE6+FE7 — 모순 행: 칩(반대편 주장) + 이유/탐지시각 펼치기(있을 때) + 해소 진입점.
function ConflictChipRow({ chip }: { chip: GraphChip }) {
  const [open, setOpen] = useState(false);
  const hasDetail = Boolean(chip.conflictReason || chip.detectedAt);
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <GraphChipView chip={chip} />
        {hasDetail ? (
          <button
            aria-expanded={open}
            className="inline-flex min-h-[44px] items-center rounded-[4px] px-2 text-[var(--text-meta)] font-medium"
            onClick={() => setOpen((v) => !v)}
            style={{ color: "var(--color-text-muted)", border: "1px solid var(--color-divider)" }}
            type="button"
          >
            {open ? "이유 접기" : "이유 보기"}
          </button>
        ) : null}
        {/* FE7 — dead-end 없는 해소 진입점(WikiTask 리뷰). 매칭 task가 없어도 전체 리뷰로 안내.
            P5 44px tap-target(패널 토글과 동일 높이). */}
        <Link
          className="inline-flex min-h-[44px] items-center rounded-[4px] px-2 text-[var(--text-meta)] font-medium"
          href={RESOLVE_TASKS_HREF}
          style={{ color: "var(--color-text-link)" }}
        >
          이 모순 해소하기 →
        </Link>
      </div>
      {open && hasDetail ? (
        <div
          className="mt-1 space-y-0.5 rounded-[4px] px-2 py-1.5 text-[var(--text-meta)]"
          style={{ background: "var(--color-card)", border: "1px solid var(--color-divider)" }}
        >
          {chip.conflictReason ? (
            <p className="break-words" style={{ color: "var(--color-text-body)" }}>
              {chip.conflictReason}
            </p>
          ) : null}
          {chip.detectedAt ? (
            <p style={{ color: "var(--color-text-muted)" }}>
              탐지: {formatDetectedAt(chip.detectedAt)}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
