/**
 * KG 그래프 범례 — explorer 탐색기와 /ask ring이 공유하는 단일 출처(중복 제거).
 * 두 줄(종류/관계) + 상태 표식 한 줄. 종류=테두리 두른 채운 원, 관계=선(fill hue와 rel 선 hue가
 * 겹쳐도 form으로 구분, §Slice1 항목 6). 등장한 종류·관계·상태만 노출한다. 폰(compact)은
 * `범례 보기` disclosure로 접어 그래프/카드 공간을 확보한다(P5.2 선례, /ask 카드 부풂 방지).
 */
import { NODE_LABEL, nodeFillColor, REL_LABEL, relColor } from "@/components/wiki/graph-shared";

export type GraphLegendProps = {
  labels: string[];
  rels: string[];
  compact: boolean;
  // 상태 표식 — 실제 그래프에 존재할 때만 노출(등장한 것만 원칙).
  hasAnchor: boolean;
  hasPlaceholder: boolean;
  hasConflict: boolean; // 미해소/상태-미상 충돌(warn)
  hasResolvedConflict: boolean; // 해소된 충돌(pass)
  hasPersonal: boolean;
};

const rowCls = "flex flex-wrap gap-x-3 gap-y-1";
const itemCls = "inline-flex items-center gap-1 text-[var(--text-meta)]";
const itemStyle = { color: "var(--color-text-muted)" } as const;

function LegendBody(props: GraphLegendProps) {
  const { labels, rels, hasAnchor, hasPlaceholder, hasConflict, hasResolvedConflict, hasPersonal } =
    props;
  const showState =
    hasAnchor || hasPlaceholder || hasConflict || hasResolvedConflict || hasPersonal;
  return (
    <div className="flex min-w-0 flex-col gap-1" aria-hidden>
      {labels.length > 0 ? (
        <ul className={rowCls}>
          {labels.map((label) => (
            <li key={label} className={itemCls} style={itemStyle}>
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{
                  background: nodeFillColor(label),
                  border: "1px solid var(--color-toolbar-border)",
                }}
              />
              {NODE_LABEL[label] ?? label}
            </li>
          ))}
        </ul>
      ) : null}
      {rels.length > 0 ? (
        <ul className={rowCls}>
          {rels.map((rel) => (
            <li key={rel} className={itemCls} style={itemStyle}>
              <span
                className="inline-block h-[2px] w-3 shrink-0 rounded-full"
                style={{ background: relColor(rel) }}
              />
              {REL_LABEL[rel] ?? rel}
            </li>
          ))}
        </ul>
      ) : null}
      {showState ? (
        <ul className={rowCls}>
          {hasAnchor ? (
            <li className={itemCls} style={itemStyle}>
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ background: "var(--color-text-strong)" }}
              />
              현재 페이지
            </li>
          ) : null}
          {hasPlaceholder ? (
            <li className={itemCls} style={itemStyle}>
              {/* 미생성 = 채운 원 + 점선 테두리(실제 placeholder 노드가 종류색 채움 + 점선이라
                  중립 채움 + 점선으로 대표 표기 — 빈 원이 아니다). */}
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{
                  background: "var(--color-divider)",
                  border: "1.5px dashed var(--color-text-muted)",
                }}
              />
              미생성(점선)
            </li>
          ) : null}
          {hasConflict ? (
            <li className={itemCls} style={itemStyle}>
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ border: "1.5px solid var(--color-warn-fg)" }}
              />
              확인 필요 충돌
            </li>
          ) : null}
          {hasResolvedConflict ? (
            <li className={itemCls} style={itemStyle}>
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ border: "1.5px solid var(--color-pass-fg)" }}
              />
              해소된 충돌
            </li>
          ) : null}
          {hasPersonal ? (
            <li className={itemCls} style={itemStyle}>
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ border: "1.5px solid var(--color-accent)" }}
              />
              내 개인 메모
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}

export function GraphLegend(props: GraphLegendProps) {
  const hasAny =
    props.labels.length > 0 ||
    props.rels.length > 0 ||
    props.hasAnchor ||
    props.hasPlaceholder ||
    props.hasConflict ||
    props.hasResolvedConflict ||
    props.hasPersonal;
  if (!hasAny) return null;
  if (props.compact) {
    return (
      <details className="min-w-0">
        <summary
          className="inline-flex min-h-11 cursor-pointer list-none items-center gap-1 text-[var(--text-meta)] font-semibold [&::-webkit-details-marker]:hidden"
          style={{ color: "var(--color-text-muted)" }}
        >
          범례 보기
        </summary>
        <div className="mt-1">
          <LegendBody {...props} />
        </div>
      </details>
    );
  }
  return <LegendBody {...props} />;
}
