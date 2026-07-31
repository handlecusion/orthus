/**
 * KG 그래프 rel 색 스와치 — 범례(explorer/ring)와 노드 인스펙터가 공유하는 단일 출처.
 * rel → 의미 색 규칙(`relColor`)이 한 곳에서만 그려지도록, 세 곳에 흩어졌던 동일 마크업을
 * 이 컴포넌트로 모은다. 순수 장식(aria-hidden)이며 훅 없음.
 */
import { relColor } from "@/components/wiki/graph-shared";

export function RelSwatch({ rel, className }: { rel: string; className?: string }) {
  return (
    <span
      aria-hidden
      className={`inline-block h-2 w-2 shrink-0 rounded-full${className ? ` ${className}` : ""}`}
      style={{ background: relColor(rel) }}
    />
  );
}
