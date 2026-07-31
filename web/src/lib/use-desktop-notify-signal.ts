"use client";

// Desktop-only 네이티브 OS 알림 신호 훅 (mail 인라인 effect에서 추출 — PR3).
//
// 감지는 값싼 로컬 폴링(count/max)이고, 실제 알림은 Tauri shell이
// `orthus://notify/<channel>` deep link로 띄운다 — 원격 페이지에 Tauri IPC를
// 노출하지 않는 설계라 orthus://collector/open과 같은 web→shell 채널을 재사용한다.
// 일반 브라우저에서는 no-op.
//
// UA 게이트: 채널별 uaToken이 있는 shell 빌드만 신호를 받는다.
// - mail: `OrthusNotify` — orthus://notify/mail을 처리하는 빌드가 광고.
// - board/review: `OrthusNotify/2` — board/review 라우팅을 아는 신형 빌드만
//   광고하므로, 구 앱이 misroute할 신호(시스템 opener 폴백/포커스 스틸)를
//   애초에 받지 않는다.
//
// 구서버 호환: notify-state endpoint가 아직 없는 서버는 404(ApiError)를 던진다
// — poll의 catch가 조용히 삼키고 다음 tick에 재시도한다.

import { useEffect, useRef } from "react";

/** MailNotifyState / PersonalBoardNotifyState / AgentWorkNotifyState 공통 동형. */
export interface DesktopNotifyState {
  total: number;
  latest_at: string | null;
  latest_title: string | null;
}

export interface DesktopNotifySignalOptions {
  /** deep link 채널 — `orthus://notify/<channel>` URL에 들어간다. */
  channel: "mail" | "board" | "review";
  /** localStorage key — `{at, total}` JSON으로 마지막 관측 상태를 저장. */
  storageKey: string;
  /** UA 게이트 토큰 — navigator.userAgent에 없으면 훅 전체가 no-op. */
  uaToken: string;
  /** notify-state fetch — `{total, latest_at, latest_title}` 동형이면 된다. */
  fetchState: () => Promise<DesktopNotifyState>;
  /** 해당 화면을 보고 있는 동안(문서 visible + pathname prefix 일치) 알림 억제. */
  suppressPaths: string[];
}

export function useDesktopNotifySignal(options: DesktopNotifySignalOptions) {
  const { channel, storageKey, uaToken } = options;
  // fetchState/suppressPaths는 호출부에서 인라인 리터럴로 오기 쉬워 ref로 최신
  // 값만 집는다 — effect는 mount 1회 + 60s interval을 유지한다(기존 mail effect와
  // 동일한 수명).
  const fetchStateRef = useRef(options.fetchState);
  const suppressPathsRef = useRef(options.suppressPaths);
  useEffect(() => {
    fetchStateRef.current = options.fetchState;
    suppressPathsRef.current = options.suppressPaths;
  });

  useEffect(() => {
    // SSR/일반 브라우저/게이트 미충족 shell — 전부 no-op.
    if (typeof navigator === "undefined" || !navigator.userAgent.includes(uaToken)) {
      return;
    }
    let alive = true;
    let primed = false;
    let lastMs = 0;
    let lastTotal = 0;
    try {
      const raw = window.localStorage.getItem(storageKey);
      if (raw) {
        const parsed = JSON.parse(raw) as { at?: string; total?: number };
        lastMs = parsed.at ? Date.parse(parsed.at) : 0;
        lastTotal = parsed.total ?? 0;
        primed = true;
      }
    } catch {
      // malformed cache — treat this session's first poll as the prime.
    }

    function fireNotification(count: number, subject: string | null) {
      const params = new URLSearchParams({ count: String(count) });
      if (subject) params.set("subject", subject.slice(0, 120));
      try {
        // Blocked in on_navigation (no real navigation) — just the signal.
        window.location.assign(`orthus://notify/${channel}?${params.toString()}`);
      } catch {
        // desktop-only channel; ignore in any non-shell context.
      }
    }

    async function poll() {
      let state: DesktopNotifyState;
      try {
        state = await fetchStateRef.current();
      } catch {
        // not logged in / offline / 구서버 404 — retry next tick.
        return;
      }
      if (!alive) return;
      const atMs = state.latest_at ? Date.parse(state.latest_at) : 0;
      const total = state.total ?? 0;
      const advanced = atMs > 0 && atMs > lastMs;
      if (primed && advanced) {
        // Don't nag while the user is actively looking at the surface.
        const onSurfaceNow =
          document.visibilityState === "visible" &&
          suppressPathsRef.current.some((prefix) =>
            window.location.pathname.startsWith(prefix),
          );
        if (!onSurfaceNow) {
          fireNotification(Math.max(1, total - lastTotal), state.latest_title);
        }
      }
      if (advanced || !primed) {
        lastMs = atMs;
        lastTotal = total;
        primed = true;
        try {
          window.localStorage.setItem(
            storageKey,
            JSON.stringify({ at: state.latest_at, total }),
          );
        } catch {
          // storage blocked — in-memory state still de-dupes this session.
        }
      }
    }

    void poll();
    const timer = window.setInterval(() => void poll(), 60_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [channel, storageKey, uaToken]);
}
