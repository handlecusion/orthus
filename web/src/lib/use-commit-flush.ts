"use client";

import { useEffect, useRef } from "react";

/**
 * 편집 커밋(저장)이 명시적 blur/닫기 외의 **모든 종료 경로**에서도 확실히 발화하도록
 * 하는 공유 훅. 다음 시점에 최신 `commit`을 호출한다:
 *   - 컴포넌트 언마운트 (Esc·백드롭·라우트 이동으로 모달/픽이 사라질 때)
 *   - 탭 숨김 (`visibilitychange` → hidden; 모바일 앱 전환·탭 전환)
 *   - `pagehide` / `beforeunload` (탭 닫기·새로고침)
 *
 * 왜 필요한가: plain input의 onBlur, BlockNote의 명시적 pull, 모달의 onClose-save는
 * 포커스된 채 컴포넌트가 언마운트되는 종료 경로(Esc, 백드롭 클릭, 사이드바 이동)에서
 * 발화하지 않아 방금 쓴 내용이 통째로 유실됐다. 코드베이스에는 이미 AutoGrowTitle
 * (`peek.tsx`)과 RichBody가 각자 이 3-event flush를 갖고 있었으나 표면마다 불균일하게
 * 적용돼 대부분의 입력이 새고 있었다. 이 훅이 그 패턴을 하나로 통일한다.
 *
 * `commit`은 스스로 idempotent해야 한다 — 변경이 없으면 no-op이어야 하며(호출부가
 * dirty 가드를 둔다), 그래야 언마운트마다 무해하게 호출할 수 있다. 동기 best-effort
 * 저장에 적합하다(로컬 state로 lift 또는 localStorage 미러). `beforeunload` 시점의
 * async 네트워크 저장까지 보장하려면 호출부가 keepalive/sendBeacon을 써야 한다.
 */
export function useCommitFlush(commit: () => void): void {
  const ref = useRef(commit);
  // 렌더마다 최신 클로저로 갱신(렌더 중 ref 변경을 피해 동시성 안전).
  useEffect(() => {
    ref.current = commit;
  });

  useEffect(() => {
    const flush = () => {
      try {
        ref.current();
      } catch {
        /* best-effort flush — 저장 실패가 종료 자체를 막지 않게 삼킨다 */
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") flush();
    };
    window.addEventListener("pagehide", flush);
    window.addEventListener("beforeunload", flush);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pagehide", flush);
      window.removeEventListener("beforeunload", flush);
      document.removeEventListener("visibilitychange", onVisibility);
      // 언마운트 자체가 가장 흔한 종료 경로다 — 마지막으로 한 번 flush.
      flush();
    };
  }, []);
}
