"use client";

/**
 * prefers-reduced-motion을 구독하는 공용 훅.
 *
 * 마운트 시 1회만 읽지 않고 matchMedia change 이벤트를 구독해 OS 설정을
 * 세션 중 바꿔도 반영한다(WCAG 2.3.3). SSR에서는 false(=모션 허용)로 시작하고
 * 첫 effect에서 실제 값으로 동기화한다 — 하이드레이션 mismatch 없음.
 *
 * 렌더 안의 컴포넌트 전용이다(훅 규칙). 렌더 밖 명령형 코드는
 * `window.matchMedia(...).matches`를 호출 시점에 직접 읽으면 된다.
 */
import { useEffect, useState } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(QUERY).matches,
  );

  useEffect(() => {
    const mql = window.matchMedia(QUERY);
    const sync = () => setReduced(mql.matches);
    sync(); // 마운트~effect 사이 변경분 반영
    mql.addEventListener("change", sync);
    return () => mql.removeEventListener("change", sync);
  }, []);

  return reduced;
}
