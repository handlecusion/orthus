"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type AuthConfig } from "@/lib/api";
import { cx } from "@/components/ui";

const TABS: { href: string; label: string }[] = [
  { href: "/dashboard", label: "개요" },
  { href: "/dashboard/kpi", label: "KPI" },
  { href: "/dashboard/plans", label: "계획·회고" },
  { href: "/dashboard/meetings", label: "회의록" },
  { href: "/dashboard/team", label: "팀" },
  { href: "/dashboard/partners", label: "파트너사" },
  { href: "/dashboard/support", label: "지원사업" },
  { href: "/dashboard/infra", label: "인프라" },
  { href: "/dashboard/finance", label: "자금관리" },
  { href: "/dashboard/graph", label: "지식그래프" },
];

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [loaded, setLoaded] = useState(false);
  // 데스크톱은 사이드바 "대시보드" 채널 그룹이 내비게이션을 담당하므로 가로 탭 스트립을
  // 숨기고, 모바일(<760px)에서만 탭 스트립을 노출한다(모바일 파리티 유지).
  const [compact, setCompact] = useState(false);
  useEffect(() => {
    const sync = () => setCompact(window.innerWidth < 760);
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  useEffect(() => {
    let alive = true;
    api
      .getAuthConfig()
      .then((c) => {
        if (alive) setAuthConfig(c);
      })
      .catch(() => {
        if (alive) setAuthConfig(null);
      })
      .finally(() => {
        if (alive) setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  const isPersonal = authConfig?.node_kind === "personal";

  // "프로젝트"는 사이드바 1급 섹션으로 빼냈으므로(노션식 전용 페이지), 그 경로와
  // 전체 페이지 데이터베이스(/dashboard/db)에서는 대시보드 탭바를 숨겨 독립된
  // 워크스페이스처럼 보이게 한다.
  const isProjects =
    pathname.startsWith("/dashboard/projects") || pathname.startsWith("/dashboard/db");

  if (loaded && isPersonal) {
    return (
      <div className="flex h-full min-h-0 items-center justify-center p-6">
        <div
          className="rounded-[var(--radius-card)] px-5 py-4 text-center text-[var(--text-body-sm)]"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-divider)",
            color: "var(--color-text-muted)",
          }}
        >
          컴패니 대시보드는 회사 노드에서만 사용할 수 있습니다.
        </div>
      </div>
    );
  }

  if (isProjects) {
    return (
      <div className="h-full min-h-0 overflow-y-auto">{children}</div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {compact ? (
      <nav
        className="flex gap-1 overflow-x-auto px-3 pt-3 sm:px-5"
        style={{
          background: "var(--color-app-canvas)",
          borderBottom: "1px solid var(--color-divider)",
        }}
        aria-label="대시보드 탭"
      >
        {TABS.map((tab) => {
          const active =
            tab.href === "/dashboard"
              ? pathname === "/dashboard"
              : pathname.startsWith(tab.href);
          return (
            <Link
              key={tab.href}
              href={tab.href}
              className={cx(
                "relative whitespace-nowrap rounded-t-[6px] px-3 py-2 text-[var(--text-body-sm)] transition-colors",
              )}
              style={{
                color: active
                  ? "var(--color-text-strong)"
                  : "var(--color-text-muted)",
                fontWeight: active ? 700 : 500,
                borderBottom: active
                  ? "2px solid var(--color-text-strong)"
                  : "2px solid transparent",
                minHeight: 44,
              }}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
      ) : null}
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </div>
  );
}
