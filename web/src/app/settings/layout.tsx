"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cx } from "@/components/ui";

// 설정 통합 화면. "접근 관리 / 테마"를 사이드바 항목으로 두는 대신
// 단일 "설정" 진입점 아래 탭으로 묶는다(대시보드 탭바와 동일 패턴). 하위 route는
// 그대로 유지되므로 각 페이지 컴포넌트는 건드리지 않는다.
const TABS: { href: string; label: string }[] = [
  { href: "/settings/access", label: "접근 관리" },
  { href: "/settings/appearance", label: "테마" },
];

export default function SettingsLayout({
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
        aria-label="설정 탭"
      >
        {TABS.map((tab) => {
          const active = pathname.startsWith(tab.href);
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
