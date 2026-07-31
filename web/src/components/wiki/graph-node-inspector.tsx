"use client";

/**
 * 노드 인스펙터 — Neo4j Browser식 클릭 사이드바의 **content-only** 본문.
 *
 * host-agnostic: 배치(데스크톱 side-dock 컬럼 vs 폰 하단 시트)는 GraphExplorerPanel이
 * 고르고, 각 래퍼가 자체 ErrorBoundary(+resetKey={activeId})로 이 본문을 감싼다. 여기는
 * 좌표/포인터/시트 기하를 모르고, 클라이언트 보유 데이터만 렌더한다(신규 API 없음).
 *
 * "클릭"의 두 의미(Slice 2.0): 선택/정보 보기는 모든 노드, 페이지 열기는 materialized
 * WikiPage 전용(pageHref). 히어로는 메타 나열이 아니라 "이 노드가 무엇이고 무엇에 연결됐는지"를
 * 먼저 보여준다(관계 요약을 주 콘텐츠로 승격).
 */
import { memo, type ReactNode } from "react";
import type { KgGraphNode } from "@/lib/api";
import {
  type NodeConflictMeta,
  conflictStatusLabel,
  conflictStatusResolved,
  entityKindLabel,
  formatDetectedAt,
  graphChipText,
  isEntityNode,
  isOwnPersonalNode,
  nodeFillColor,
  nodeKindLabel,
  PERSONAL_NODE_LABEL,
  relLabelText,
} from "@/components/wiki/graph-shared";
import { RelSwatch } from "@/components/wiki/graph-swatch";

// 인스펙터 보조 액션 버튼(이웃 펼치기·고정 해제·에러 fallback 닫기) 공용 스타일 — 카드+테두리+강조색.
export const SECONDARY_BUTTON_STYLE = {
  background: "var(--color-card)",
  border: "1px solid var(--color-toolbar-border)",
  color: "var(--color-text-strong)",
} as const;

// 중앙 정렬 상태 래퍼(빈 상태·에러 fallback 공용) — 같은 여백/정렬을 한 곳에서 관리.
export function InspectorCenteredState({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 px-4 py-8 text-center">
      {children}
    </div>
  );
}

export type InspectorRelationGroup = { rel: string; total: number; shown: KgGraphNode[] };

// 인스펙터 로컬 카피(뷰 무관 entityReadout과 분리 — ring엔 이 패널이 없다).
const NO_PAGE_HINT = "이 노드는 열 수 있는 페이지가 없어 정보만 표시합니다.";

// React.memo — 팬/줌은 setView로 부모를 리렌더하지만 인스펙터 props(node/relations/…)는 그대로라
// 스킵된다. 효과가 있으려면 부모가 onExpand/onOpenPage/onUnpin를 useCallback으로 안정화해야 한다.
export const GraphNodeInspector = memo(function GraphNodeInspector({
  node,
  conflict,
  relations,
  connectedCount,
  canExpand,
  pinned,
  pageHref,
  mode,
  onSelectNode,
  onExpand,
  onOpenPage,
  onUnpin,
  onClose,
}: {
  node: KgGraphNode;
  conflict: NodeConflictMeta | null;
  relations: InspectorRelationGroup[];
  connectedCount: number;
  canExpand: boolean;
  pinned: boolean;
  pageHref: string | null;
  mode: "dock" | "sheet";
  onSelectNode: (id: string) => void;
  onExpand: () => void;
  onOpenPage: () => void;
  onUnpin: () => void;
  onClose: () => void;
}) {
  const title = graphChipText(node);
  const isEntity = isEntityNode(node);
  const isPersonal = isOwnPersonalNode(node);
  const relCount = relations.length;

  const summary =
    connectedCount > 0
      ? `${connectedCount}개 항목과 ${relCount}종 관계로 연결됨`
      : "아직 연결된 관계가 없습니다.";

  const entityKind = isEntity ? entityKindLabel(node.entity_kind) : null;
  const mentionCount = isEntity ? node.mention_count : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 헤더: 종류 배지 + 닫기 */}
      <div className="flex items-start justify-between gap-2 px-3 pt-3">
        {/* 종류 배지 — 배경을 종류 tint로(칩·범례·그래프 fill과 4-표면 일관, PR2). 텍스트/포맷은
            칩·aria와 공유(nodeKindLabel). 긴 라벨은 min-w-0 + break로 좁은 폭에서도 헤더를 안 넘긴다. */}
        <span
          className="mt-0.5 inline-flex min-w-0 items-center rounded-[4px] px-1.5 py-[2px] text-[var(--text-meta)] font-semibold [overflow-wrap:anywhere]"
          style={{
            background: `color-mix(in srgb, ${nodeFillColor(node.label)} 22%, transparent)`,
            border: `1px solid color-mix(in srgb, ${nodeFillColor(node.label)} 45%, transparent)`,
            color: "var(--color-text-body)",
          }}
        >
          {nodeKindLabel(node)}
        </span>
        <button
          aria-label="인스펙터 닫기"
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-toolbar)] text-[var(--text-body)] font-bold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
          onClick={onClose}
          style={{ color: "var(--color-text-strong)" }}
          type="button"
        >
          ✕
        </button>
      </div>

      {/* 본문 — 스크롤 영역 */}
      <div
        className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 pb-2"
        style={mode === "sheet" ? { touchAction: "pan-y" } : undefined}
      >
        {/* 히어로: 제목 + 관계 요약 */}
        <div className="space-y-1">
          <h3
            className="break-words text-[var(--text-body)] font-bold leading-[1.35] [overflow-wrap:anywhere]"
            style={{ color: "var(--color-text-strong)" }}
          >
            {title}
          </h3>
          <p
            className="text-[var(--text-body-sm)] leading-[1.5]"
            style={{ color: "var(--color-text-body)" }}
          >
            {summary}
          </p>
        </div>

        {/* 속성(humanize — raw slug/불리언 금지) */}
        <dl className="space-y-1.5 text-[var(--text-meta)]">
          <PropRow
            label="상태"
            value={node.materialized ? "생성된 문서" : "참조만 됨 · 아직 만들어지지 않은 문서"}
          />
          <PropRow label="범위" value={isPersonal ? PERSONAL_NODE_LABEL : "회사"} />
          {entityKind ? <PropRow label="개체 종류" value={entityKind} /> : null}
          {mentionCount != null ? (
            <PropRow label="언급" value={`${mentionCount}개 페이지에 언급`} />
          ) : null}
          {conflict?.hasConflict ? (
            <PropRow
              label="충돌"
              value={conflictStatusLabel(conflict.status)}
              tone={conflictStatusResolved(conflict.status) ? "pass" : "warn"}
            />
          ) : null}
          {conflict?.conflictReason ? (
            <PropRow label="사유" value={conflict.conflictReason} />
          ) : null}
          {conflict?.detectedAt ? (
            <PropRow label="탐지" value={formatDetectedAt(conflict.detectedAt)} />
          ) : null}
        </dl>

        {pageHref == null ? (
          <p
            className="text-[var(--text-meta)] leading-[1.5]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {NO_PAGE_HINT}
          </p>
        ) : null}

        {/* 관계 목록(캡 적용됨) — 상대 이름 클릭 시 사이드바에서 순회 */}
        {relations.length > 0 ? (
          <div className="space-y-2">
            <p
              className="text-[var(--text-meta)] font-semibold uppercase tracking-[0.02em]"
              style={{ color: "var(--color-text-muted)" }}
            >
              연결된 관계
            </p>
            {relations.map((group) => (
              <div className="min-w-0 space-y-1" key={group.rel}>
                <div className="flex items-center gap-1.5">
                  <RelSwatch rel={group.rel} />
                  <span
                    className="text-[var(--text-meta)] font-semibold"
                    style={{ color: "var(--color-text-body)" }}
                  >
                    {relLabelText(group.rel)}
                  </span>
                  <span
                    className="text-[var(--text-meta)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {group.shown.length < group.total
                      ? `${group.shown.length}/${group.total}`
                      : group.total}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5 pl-3.5">
                  {group.shown.map((other) => (
                    <button
                      className="inline-flex min-h-11 max-w-full min-w-0 items-center rounded-[4px] px-1.5 py-[2px] text-left text-[var(--text-meta)] font-medium outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
                      key={other.id}
                      onClick={() => onSelectNode(other.id)}
                      style={{
                        background: "var(--color-card)",
                        border: "1px solid var(--color-divider)",
                        color: "var(--color-text-body)",
                      }}
                      type="button"
                    >
                      <span className="min-w-0 max-w-full break-all">{graphChipText(other)}</span>
                    </button>
                  ))}
                  {group.total > group.shown.length ? (
                    <span
                      className="inline-flex items-center text-[var(--text-meta)]"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      +{group.total - group.shown.length} 더
                    </span>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        ) : null}
      </div>

      {/* 액션 — 게이팅(죽은 버튼/링크 방지) */}
      {pageHref || canExpand || pinned ? (
        <div
          className="flex flex-wrap gap-2 border-t px-3 py-2"
          style={{ borderColor: "var(--color-toolbar-border)" }}
        >
          {pageHref ? (
            // 실제 앵커(<a href>)라 cmd/ctrl/휠클릭 새 탭·우클릭 새 탭 열기·hover 미리보기를
            // 브라우저가 처리한다. 수식자 없는 좌클릭만 SPA 내비게이션으로 가로챈다.
            <a
              className="inline-flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
              href={pageHref}
              onClick={(ev) => {
                // 수식자 클릭(새 탭/창)은 브라우저 기본 동작 보존. onClick은 좌클릭에서만 발화하므로
                // 중클릭/우클릭은 여기 오지 않고 <a href> 기본 동작으로 새 탭이 열린다.
                if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
                ev.preventDefault();
                onOpenPage();
              }}
              style={{
                background: "var(--color-sidebar-active)",
                color: "var(--color-text-strong)",
              }}
            >
              페이지 열기
            </a>
          ) : null}
          {canExpand ? (
            <button
              className="inline-flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
              onClick={onExpand}
              style={SECONDARY_BUTTON_STYLE}
              type="button"
            >
              이웃 펼치기
            </button>
          ) : null}
          {pinned ? (
            <button
              className="inline-flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
              onClick={onUnpin}
              style={SECONDARY_BUTTON_STYLE}
              type="button"
            >
              고정 해제
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
});

function PropRow({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "warn" | "pass";
}) {
  const valueColor =
    tone === "warn"
      ? "var(--color-warn-fg)"
      : tone === "pass"
        ? "var(--color-pass-fg)"
        : "var(--color-text-body)";
  return (
    <div className="flex gap-2">
      <dt className="shrink-0" style={{ color: "var(--color-text-muted)", minWidth: "3.5rem" }}>
        {label}
      </dt>
      <dd className="min-w-0 break-words [overflow-wrap:anywhere]" style={{ color: valueColor }}>
        {value}
      </dd>
    </div>
  );
}

// 데스크톱 도킹 컬럼의 빈 상태 — 사공간이 아니라 발견성(온보딩) 어포던스(C-level).
export function EmptyInspectorHint() {
  return (
    <InspectorCenteredState>
      <span aria-hidden className="text-2xl" style={{ color: "var(--color-text-muted)" }}>
        ◎
      </span>
      <p
        className="text-[var(--text-body-sm)] leading-[1.5]"
        style={{ color: "var(--color-text-muted)" }}
      >
        노드를 클릭하면 정보가 표시됩니다.
      </p>
    </InspectorCenteredState>
  );
}
