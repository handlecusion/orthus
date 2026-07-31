"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cx } from "@/components/ui";

// 에이전트 통합 화면. "에이전트(작업)"와 "체크리스트"를 두 사이드바 항목으로 두는
// 대신 단일 "에이전트" 진입점 아래 탭으로 묶는다. 두 route(/agent-work, /checklist)는
// 그대로 유지되고 각 페이지 컴포넌트는 건드리지 않는다.
const TABS: { href: string; label: string; match: (p: string) => boolean }[] = [
  { href: "/agent-work", label: "에이전트", match: (p) => p === "/agent-work" || p.startsWith("/agent-work/") },
  { href: "/checklist", label: "체크리스트", match: (p) => p.startsWith("/checklist") },
];

export default function WorkLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();

  return (
    <div className="flex h-full min-h-0 flex-col">
      <nav
        className="flex gap-1 overflow-x-auto px-3 pt-3 sm:px-5"
        style={{
          background: "var(--color-app-canvas)",
          borderBottom: "1px solid var(--color-divider)",
        }}
        aria-label="에이전트 탭"
      >
        {TABS.map((tab) => {
          const active = tab.match(pathname);
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
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </div>
  );
}
