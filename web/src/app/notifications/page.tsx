"use client";

/* 개인 알림 — 위임/외부 생성으로 내 보드에 들어온 태스크의 알림 피드 (P10).
 * 서버 endpoint(`GET /personal-board/notifications`)는 병행 PR로 추가된다:
 * 미배포(404)면 빈 상태로 조용히 degrade한다. 행 클릭은 보드 딥링크
 * (`/board?view=backlog&task=<id>`)로 이동하고, 페이지 진입 시 lastSeen을
 * 갱신해 AppShell 배지를 리셋한다. */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type PersonalBoardNotificationItem,
} from "@/lib/api";
import {
  NOTIFICATIONS_LAST_SEEN_KEY,
  refreshNotificationsBadge,
} from "@/components/AppShell";
import { Banner, Skeleton } from "@/components/ui";

function relativeTime(iso: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "";
  const diffMin = Math.floor((Date.now() - ms) / 60_000);
  if (diffMin < 1) return "방금 전";
  if (diffMin < 60) return `${diffMin}분 전`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}시간 전`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 7) return `${diffDay}일 전`;
  return new Date(ms).toLocaleDateString("ko-KR");
}

function sourceChipLabel(item: PersonalBoardNotificationItem): string {
  if (item.source_kind === "delegation") {
    // source_label은 서버(N2)가 이미 "위임 — <위임자>" 형태로 만들어 준다 — 재접두 금지.
    return item.source_label ?? "위임";
  }
  return "보드";
}

export default function NotificationsPage() {
  const router = useRouter();
  const [items, setItems] = useState<PersonalBoardNotificationItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // 진입 시점의 lastSeen — 이 값보다 새 알림에만 unread 액센트를 그린다.
  // (로드 직후 lastSeen을 최신으로 올리므로, 갱신 전 값을 따로 잡아둔다.)
  const [lastSeenAtLoad, setLastSeenAtLoad] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const res = await api.boardNotifications(20);
        if (!alive) return;
        let prevSeen: string | null = null;
        try {
          prevSeen = window.localStorage.getItem(NOTIFICATIONS_LAST_SEEN_KEY);
        } catch {
          // storage blocked — unread accent simply won't render.
        }
        setLastSeenAtLoad(prevSeen);
        setItems(res.items);
        // 목록을 봤으니 최신 created_at으로 lastSeen을 올리고 배지를 리셋한다.
        let newest = "";
        let newestMs = 0;
        for (const item of res.items) {
          const ms = Date.parse(item.created_at);
          if (Number.isFinite(ms) && ms > newestMs) {
            newestMs = ms;
            newest = item.created_at;
          }
        }
        if (newest) {
          try {
            window.localStorage.setItem(NOTIFICATIONS_LAST_SEEN_KEY, newest);
          } catch {
            // storage blocked — badge will re-derive on next fetch anyway.
          }
        }
        refreshNotificationsBadge();
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 404) {
          // 서버 PR 미배포 — 빈 상태로 degrade (에러 배너 없음).
          setItems([]);
        } else {
          setError(e instanceof Error ? e.message : "알림을 불러오지 못했습니다.");
        }
      } finally {
        if (alive) setLoading(false);
      }
    }
    void load();
    return () => {
      alive = false;
    };
  }, []);

  const lastSeenMs = lastSeenAtLoad ? Date.parse(lastSeenAtLoad) : Number.NaN;

  return (
    <div className="h-full min-h-0 w-full overflow-y-auto p-0 min-[760px]:p-3">
      <div className="mx-auto w-full max-w-[720px] px-3 py-4 min-[760px]:px-0">
        <header className="mb-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <h1
              className="truncate text-[17px] font-bold tracking-[-0.02em]"
              style={{ color: "var(--color-text-strong)" }}
            >
              알림
            </h1>
            <p className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
              위임 등으로 내 보드에 새로 생긴 업무
            </p>
          </div>
        </header>

        {loading ? (
          <div className="grid gap-2" aria-label="알림 로딩 중">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : error ? (
          <Banner tone="fail" title="알림을 불러오지 못했습니다">
            {error}
          </Banner>
        ) : !items || items.length === 0 ? (
          <div
            className="rounded-[10px] px-4 py-10 text-center text-[var(--text-body-sm)]"
            style={{
              background: "var(--color-card)",
              border: "1px solid var(--color-divider)",
              color: "var(--color-text-muted)",
            }}
          >
            아직 알림이 없습니다
          </div>
        ) : (
          <ul
            className="overflow-hidden rounded-[10px]"
            style={{
              background: "var(--color-card)",
              border: "1px solid var(--color-divider)",
            }}
          >
            {items.map((item, index) => {
              const createdMs = Date.parse(item.created_at);
              const unread =
                Number.isFinite(lastSeenMs) &&
                Number.isFinite(createdMs) &&
                createdMs > lastSeenMs;
              return (
                <li
                  key={`${item.task_id}:${item.created_at}`}
                  style={
                    index > 0
                      ? { borderTop: "1px solid var(--color-divider)" }
                      : undefined
                  }
                >
                  <button
                    type="button"
                    onClick={() =>
                      router.push(`/board?view=backlog&task=${encodeURIComponent(item.task_id)}`)
                    }
                    className="flex min-h-11 w-full items-start gap-2.5 px-3 py-2.5 text-left transition-colors hover:bg-[color-mix(in_srgb,var(--color-sidebar-active)_66%,transparent)] min-[760px]:min-h-9"
                  >
                    <span
                      aria-label={unread ? "읽지 않은 알림" : undefined}
                      aria-hidden={unread ? undefined : true}
                      className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full"
                      style={{
                        background: unread ? "var(--color-attention)" : "transparent",
                      }}
                    />
                    <span className="min-w-0 flex-1">
                      <span
                        className="block truncate text-[var(--text-body-sm)] font-bold"
                        style={{ color: "var(--color-text-strong)" }}
                      >
                        {item.title}
                      </span>
                      <span className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[var(--text-meta)]">
                        <span
                          className="inline-flex shrink-0 items-center rounded-[999px] px-1.5 py-px font-semibold"
                          style={{
                            background:
                              "color-mix(in srgb, var(--color-accent) 14%, var(--color-card))",
                            color: "var(--color-text-body)",
                          }}
                        >
                          {sourceChipLabel(item)}
                        </span>
                        <span className="truncate" style={{ color: "var(--color-text-muted)" }}>
                          {relativeTime(item.created_at)}
                        </span>
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        <footer className="mt-3">
          <Link
            href="/board?view=personal"
            className="inline-flex h-11 items-center justify-center rounded-[8px] px-4 text-[var(--text-body-sm)] font-bold min-[760px]:h-9"
            style={{
              background: "var(--color-card)",
              border: "1px solid var(--color-divider)",
              color: "var(--color-text-body)",
            }}
          >
            모두 보기 — 내 보드
          </Link>
        </footer>
      </div>
    </div>
  );
}
