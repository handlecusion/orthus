"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  type AgentWorkItem,
  type AgentWorkState,
  type AuthConfig,
  type AuthMe,
  type CalendarEvent,
  type PersonalBoardFolder,
  type PersonalBoardNotificationList,
  type TeamMember,
} from "@/lib/api";
import { clearApiSessionCaches } from "@/lib/api";
import { clearOfflineBoardCache } from "@/lib/offline-board-cache";
import { clearOutbox } from "@/lib/offline-board-outbox";
import {
  deactivateOfflineNoteOwner,
  getOfflineNoteOwner,
  offlineNoteScope,
  setOfflineNoteOwner,
  subscribeOfflineNoteOwner,
} from "@/lib/offline-notes";
import { resetPageSaveMemory } from "@/lib/pageSave";
import {
  clearCachedResources,
  refreshCachedResource,
  useCachedResource,
} from "@/lib/use-cached-resource";
import { useDesktopNotifySignal } from "@/lib/use-desktop-notify-signal";
import { BrandMark } from "./BrandMark";
import { OfflineServiceWorker } from "./OfflineServiceWorker";
import { cx } from "./ui";

// 인증 확인 fetch를 hydration·effect를 기다리지 않고 모듈 평가 즉시 시작한다.
// getAuthConfig/getAuthMe는 캐시+in-flight 공유라 AuthGate의 마운트 fetch가
// 이 결과를 그대로 재사용한다 — 콜드 로드에서 인증 왕복 한 층이 앞당겨진다.
if (typeof window !== "undefined") {
  void api.getAuthConfig().catch(() => undefined);
  void api
    .getAuthMe()
    .then((me) => setOfflineNoteOwner({ userId: me.user_id, nodeId: me.node_id }))
    .catch(() => undefined);
}

/**
 * AppShell — Ojosama operations-board layout.
 *
 *   ┌──────────────────┬──────────────────────────────────────┬───┐
 *   │ 236px sidebar    │ content (canvas, scrolls inside)     │56 │
 *   │ #e6e7e8          │ #f5f5f6                              │rail│
 *   └──────────────────┴──────────────────────────────────────┴───┘
 *
 * Sidebar groups its nav under uppercase section labels (DESIGN.md
 * "Section labels are uppercase, 13px, 800 weight"). Items are 38px
 * tall, 6px radius, active item background is sidebar-active.
 *
 * Integration rail is intentionally command-oriented. On /board it dispatches
 * personal-board commands; elsewhere it acts as compact navigation.
 */

interface NavItem {
  href: string;
  label: string;
  icon: React.ComponentType;
  // 이미 /board 위에 있을 때는 페이지 이동 대신 보드 커맨드 이벤트로 처리한다
  // (모바일 하단 "스케줄" — 보드가 리마운트되지 않아 ?view= 딥링크가 안 먹는 경우).
  boardCommand?: BoardCommand;
}

const NAV_SECTIONS: ReadonlyArray<{
  label: string;
  items: ReadonlyArray<NavItem>;
}> = [
  {
    label: "회사",
    items: [
      { href: "/wiki", label: "위키", icon: HomeIcon },
      { href: "/dashboard", label: "대시보드", icon: GaugeIcon },
      { href: "/dashboard/calendar", label: "팀 일정", icon: CalendarIcon },
      { href: "/mail", label: "메일", icon: MailIcon },
      { href: "/dashboard/projects", label: "프로젝트", icon: FolderTreeIcon },
      { href: "/connectors", label: "커넥터", icon: PlugIcon },
    ],
  },
  {
    // 회사+개인 양쪽을 함께 보는 통합 surface. Agent는 로그인 유저의 비서 inbox이고
    // 내부 queue/API 이름만 Agent Work로 남긴다. 체크리스트는 에이전트 화면의 탭으로
    // 흡수됐다((work) route group 공유 layout).
    label: "공통",
    items: [
      { href: "/ask", label: "질문", icon: TerminalIcon },
      { href: "/agent-work", label: "에이전트", icon: TaskIcon },
    ],
  },
  {
    // 접근 관리 / 테마 / 데스크탑 앱은 단일 "설정" 진입점 아래 탭으로 묶었다
    // (settings/layout.tsx).
    label: "설정",
    items: [{ href: "/settings", label: "설정", icon: GearIcon }],
  },
];

// 대시보드(회사) 하위 표면 — 사이드바 "대시보드" 채널 그룹으로 접어 노출한다.
// 팀 일정(캘린더)은 "회사" 최상위 nav 항목 + 레일 퀵패널로 이동했으므로 여기서는 제외한다.
const DASHBOARD_CHANNELS: ReadonlyArray<{ href: string; label: string }> = [
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

// 개인(owner-scope) 전용 섹션. 회사 노드 + ORTHUS_OWNER_SCOPE_ENABLED일 때만 회사 섹션
// 바로 아래에 끼워 넣는다. 보드는 ojosama 개인 워크스페이스("내 보드")로 바로 진입한다.
const PERSONAL_NAV_SECTION: { label: string; items: ReadonlyArray<NavItem> } = {
  label: "개인",
  items: [
    { href: "/board?view=personal", label: "내 보드", icon: BoardIcon },
    { href: "/notes", label: "메모", icon: NoteIcon },
    { href: "/mail", label: "메일", icon: MailIcon },
    { href: "/notifications", label: "알림", icon: BellIcon },
    { href: "/connectors/personal", label: "개인 커넥터", icon: PlugIcon },
  ],
};

// 레일 슬롯이 열 수 있는 슬라이드 패널. 지금은 팀 일정만 있고, 데스크톱에서만
// 패널로 열리며 모바일에서는 slot.href로 대신 이동한다.
type RailPanel = "team_schedule";

type RailSlot = {
  key: string;
  label: string;
  icon: React.ComponentType;
  href?: string;
  boardCommand?: BoardCommand;
  // 지정 시 데스크톱 레일에서 이 슬롯은 페이지 이동 대신 슬라이드 패널을 토글한다.
  panel?: RailPanel;
  hasNotification?: boolean;
};

type BoardViewCommand = "home" | "weekly_plan" | "weekly_review" | "schedule" | "backlog";
type BoardCommand = BoardViewCommand | "refresh";
type BoardCommandDetail = {
  command: BoardCommand;
};

const BOARD_RAIL_SLOTS: ReadonlyArray<RailSlot> = [
  { key: "home", label: "Home", icon: HomeIcon, boardCommand: "home" },
  { key: "weekly_plan", label: "Weekly plan", icon: CalendarIcon, boardCommand: "weekly_plan" },
  { key: "weekly_review", label: "Weekly review", icon: GaugeIcon, boardCommand: "weekly_review" },
  { key: "backlog", label: "Backlog", icon: ArchiveIcon, boardCommand: "backlog" },
];

// 팀 일정 슬롯 — 회사/owner-scope 레일 공용. 데스크톱은 빠른 미리보기 패널을
// 토글하고, 모바일(빠른 액션)에서는 href로 팀 일정 전체 페이지로 이동한다.
const TEAM_SCHEDULE_SLOT: RailSlot = {
  key: "team_schedule",
  label: "팀 일정",
  icon: CalendarIcon,
  href: "/dashboard/calendar",
  panel: "team_schedule",
};

const COMPANY_RAIL_SLOTS: ReadonlyArray<RailSlot> = [
  { key: "mail", label: "Mail", icon: MailIcon, href: "/mail" },
  { key: "agent_work", label: "체크리스트", icon: ChecklistIcon, href: "/checklist", hasNotification: true },
  TEAM_SCHEDULE_SLOT,
  { key: "slack", label: "Slack (회사)", icon: PlugIcon, href: "/connectors", hasNotification: true },
  { key: "notion", label: "Notion (회사)", icon: PlugIcon, href: "/connectors" },
  { key: "wiki_tasks", label: "Wiki tasks", icon: TaskIcon, href: "/wiki/tasks" },
];

const OWNER_SCOPE_RAIL_SLOTS: ReadonlyArray<RailSlot> = [
  { key: "mail", label: "Mail", icon: MailIcon, href: "/mail" },
  { key: "agent_work", label: "체크리스트", icon: ChecklistIcon, href: "/checklist", hasNotification: true },
  TEAM_SCHEDULE_SLOT,
  { key: "personal_connectors", label: "개인 커넥터", icon: PlugIcon, href: "/connectors/personal" },
  { key: "wiki_tasks", label: "Wiki tasks", icon: TaskIcon, href: "/wiki/tasks" },
];

const PERSONAL_RAIL_SLOTS: ReadonlyArray<RailSlot> = [
  { key: "mail", label: "메일", icon: MailIcon, href: "/mail" },
  { key: "agent_work", label: "체크리스트", icon: ChecklistIcon, href: "/checklist?scope=personal", hasNotification: true },
  { key: "local_files", label: "로컬 파일", icon: FolderTreeIcon, href: "/connectors/personal" },
  { key: "gws", label: "Gmail / Drive", icon: PlugIcon, href: "/connectors/personal" },
  { key: "ai_sessions", label: "AI 세션", icon: TerminalIcon, href: "/connectors/personal" },
  { key: "help", label: "도움말", icon: TaskIcon, href: "/wiki/tasks" },
];

const MOBILE_PRIMARY_NAV: ReadonlyArray<NavItem> = [
  { href: "/dashboard", label: "대시보드", icon: GaugeIcon },
  { href: "/mail", label: "메일", icon: MailIcon },
  // "스케줄"은 보드 안에서 Today 옆 스케줄 버튼으로 여니, 하단 네비에서는
  // 보드와 중복인 별도 스케줄 항목을 두지 않는다.
  { href: "/ask", label: "질문", icon: TerminalIcon },
  { href: "/checklist", label: "체크리스트", icon: ChecklistIcon },
  { href: "/board", label: "보드", icon: BoardIcon },
];

const DEV_AUTO_LOGIN_EMAIL = process.env.NEXT_PUBLIC_DEV_AUTO_LOGIN_EMAIL?.trim() ?? "";

const OPEN_AGENT_WORK_STATES = new Set<AgentWorkState>([
  "pending",
  "classified",
  "auto_execute",
  "draft_for_review",
  "request_more_data",
  "rejected",
]);

// 체크리스트 배지 SWR 캐시 key. 배지는 60초 TTL + focus 갱신으로 돌지만, agent-work
// decision(승인/기각/정보요청) 직후에는 refreshAgentWorkBadge()로 즉시 재검증한다.
const AGENT_WORK_BADGE_KEY = "agent-work:badge";

/** agent-work 항목 상태를 바꾼 mutation 직후 호출 — 배지가 TTL(최대 60초) 만료를
 *  기다리지 않고 즉시 갱신되게 한다(use-cached-resource 명령형 재검증). */
export function refreshAgentWorkBadge(): void {
  void refreshCachedResource(AGENT_WORK_BADGE_KEY);
}

// 개인 "알림" 탭 배지 SWR 캐시 key. lastSeen(localStorage) 기준 unread만 센다.
const NOTIFICATIONS_BADGE_KEY = "notifications:badge";
// /notifications 페이지와 배지가 공유하는 마지막 확인 시각(ISO string).
export const NOTIFICATIONS_LAST_SEEN_KEY = "orthus.notifications.lastSeen";

/** /notifications 페이지에서 lastSeen을 갱신한 직후 호출 — 배지가 TTL 만료를
 *  기다리지 않고 즉시 재검증/재계산되게 한다(refreshAgentWorkBadge와 동형). */
export function refreshNotificationsBadge(): void {
  void refreshCachedResource(NOTIFICATIONS_BADGE_KEY);
}

// Mirror checklist/page.tsx isChecklistItem: the badge must count exactly what
// the 체크리스트 list shows. wiki_task rows and agent_task (chat delegation) rows
// are excluded from the checklist, so they must not inflate the badge either.
function isChecklistBadgeItem(item: AgentWorkItem): boolean {
  return (
    item.source_kind !== "wiki_task" &&
    item.action_family !== "agent_task" &&
    OPEN_AGENT_WORK_STATES.has(item.state)
  );
}

function NavCaretIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

// 사이드바 "대시보드" 채널 그룹 — 상단 가로 탭 스트립을 접어 슬랙식 채널 목록으로 노출한다.
// 라우팅은 그대로(각 채널은 기존 /dashboard/* 라우트 Link). 활성 채널은 인디고 액센트 레일.
function DashboardNavGroup({
  open,
  collapsed,
  onToggle,
  channelActive,
}: {
  open: boolean;
  collapsed: boolean;
  onToggle: () => void;
  channelActive: (href: string) => boolean;
}) {
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className={cx(
          "group flex h-[38px] w-full items-center gap-2 rounded-[6px] px-2.5 transition-colors",
          collapsed && "justify-center px-0",
        )}
        style={{ color: "var(--color-text-body)" }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background =
            "color-mix(in srgb, var(--color-sidebar-active) 55%, transparent)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = "transparent";
        }}
      >
        <span className="flex h-[18px] w-[18px] shrink-0 items-center justify-center" aria-hidden>
          <GaugeIcon />
        </span>
        <span className={cx("truncate text-[13px] font-semibold", collapsed && "sr-only")}>
          대시보드
        </span>
        {!collapsed ? (
          <span
            className="ml-auto flex h-4 w-4 items-center justify-center transition-transform"
            style={{
              color: "var(--color-text-faint)",
              transform: open ? "rotate(90deg)" : "rotate(0deg)",
            }}
            aria-hidden
          >
            <NavCaretIcon />
          </span>
        ) : null}
      </button>
      {open && !collapsed ? (
        <ul className="mt-[2px] flex flex-col gap-[1px]">
          {DASHBOARD_CHANNELS.map((ch) => {
            const active = channelActive(ch.href);
            return (
              <li key={ch.href}>
                <Link
                  href={ch.href}
                  aria-current={active ? "page" : undefined}
                  className="group relative flex h-[30px] items-center gap-1 rounded-[6px] pl-[26px] pr-2.5 transition-colors"
                  style={{
                    background: active ? "var(--color-accent-soft)" : "transparent",
                    color: active ? "var(--color-text-strong)" : "var(--color-text-muted)",
                    boxShadow: active ? "inset 3px 0 0 var(--color-accent)" : undefined,
                  }}
                  onMouseEnter={(e) => {
                    if (!active)
                      e.currentTarget.style.background =
                        "color-mix(in srgb, var(--color-sidebar-active) 45%, transparent)";
                  }}
                  onMouseLeave={(e) => {
                    if (!active) e.currentTarget.style.background = "transparent";
                  }}
                >
                  <span
                    className="text-[13px]"
                    style={{ color: active ? "var(--color-accent)" : "var(--color-text-faint)" }}
                    aria-hidden
                  >
                    #
                  </span>
                  <span className={cx("truncate text-[13px]", active && "font-semibold")}>
                    {ch.label}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      ) : null}
    </li>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [noteOwnerScope, setNoteOwnerScope] = useState(
    () => offlineNoteScope() ?? "locked",
  );
  const noteOwnerScopeRef = useRef(noteOwnerScope);

  useEffect(() => {
    const applyOwner = (owner = getOfflineNoteOwner()) => {
      const nextScope = offlineNoteScope(owner) ?? "locked";
      if (nextScope === noteOwnerScopeRef.current) return;
      noteOwnerScopeRef.current = nextScope;
      clearApiSessionCaches();
      clearCachedResources();
      resetPageSaveMemory();
      setNoteOwnerScope(nextScope);
    };
    const unsubscribe = subscribeOfflineNoteOwner(applyOwner);
    const initial = window.setTimeout(() => applyOwner(), 0);
    return () => {
      window.clearTimeout(initial);
      unsubscribe();
    };
  }, []);

  const scopedChildren = <Fragment key={noteOwnerScope}>{children}</Fragment>;

  if (pathname.startsWith("/login")) {
    return (
      <>
        <OfflineServiceWorker />
        {scopedChildren}
      </>
    );
  }

  // 빠른 메모(글로벌 단축키 소형 창)는 인증만 거치고 사이드바/레일 없이
  // 페이지 자체 UI만 렌더한다. 460px 창 안에 앱 셸이 통째로 들어가지 않게.
  const chromeless = pathname.startsWith("/notes/quick");

  return (
    <>
      <OfflineServiceWorker />
      <AuthGate key={noteOwnerScope}>
        {chromeless ? <>{scopedChildren}</> : <ShellFrame>{scopedChildren}</ShellFrame>}
      </AuthGate>
    </>
  );
}

function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [ready, setReady] = useState(false);

  // The desktop quick-note shell may paint its last authenticated, owner-scoped
  // local namespace immediately.  The auth request below still runs and a
  // definitive 401/403 replaces it with login; logout clears the namespace.
  useLayoutEffect(() => {
    if (
      pathname.startsWith("/notes/quick") &&
      navigator.onLine === false &&
      getOfflineNoteOwner()
    ) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- local-first quick shell before paint
      setReady(true);
    }
  }, [pathname]);

  // 인증 확인은 최초 mount 1회만 — 라우트 전환마다 게이트를 다시 돌리지 않는다
  // (세션 만료는 이후 API 401이 request() redirect로 처리). config/me는 병렬로 띄워
  // 직렬 왕복 지연을 없앤다.
  useEffect(() => {
    let alive = true;
    const mePromise = api.getAuthMe().then((me) => {
      setOfflineNoteOwner({ userId: me.user_id, nodeId: me.node_id });
      return me;
    });
    // config가 session 모드가 아니면 mePromise를 기다리지 않으므로, 그 경로의
    // unhandled rejection을 미리 흡수한다(await 경로의 throw에는 영향 없음).
    mePromise.catch(() => undefined);
    api
      .getAuthConfig()
      .then(async (config) => {
        if (config.auth_mode !== "session") return;
        try {
          await mePromise;
        } catch (err) {
          if (!shouldAutoDevLogin(config, err)) throw err;
          try {
            await api.devLogin(DEV_AUTO_LOGIN_EMAIL);
            const me = await api.getAuthMe();
            setOfflineNoteOwner({ userId: me.user_id, nodeId: me.node_id });
          } catch {
            throw err;
          }
        }
      })
      .then(() => {
        if (alive) setReady(true);
      })
      .catch((err) => {
        if (!alive) return;
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          deactivateOfflineNoteOwner();
          const next = `${window.location.pathname}${window.location.search}`;
          const error = err.status === 403 ? "&error=not_invited" : "";
          window.location.replace(`/login?next=${encodeURIComponent(next)}${error}`);
          return;
        }
        setReady(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  // 라우트 전환마다 가벼운 세션 재확인 — getAuthMe는 30초 캐시라 실제 왕복은 최대
  // 30초당 1회다. 세션 만료(401)는 이후 API 호출의 request() redirect도 잡지만,
  // mid-session allowlist 회수(403)는 request()에서 처리할 수 없다: 백엔드는 403을
  // 정당한 in-app 권한 거부에도 쓰므로("node operator required" 등, orthus/auth.py)
  // blanket 403 redirect는 member의 정상 화면을 로그아웃시킨다. /auth/me의 403만
  // "not invited/회수"를 뜻하므로 여기서만 기존 시맨틱(/login?error=not_invited)을
  // 복원한다. 렌더는 막지 않는다(백그라운드 검사).
  useEffect(() => {
    // 최초 mount 검사는 위 one-shot 게이트 담당 — ready 전에 겹쳐 돌리면 dev
    // auto-login(401 → devLogin) 경합으로 잘못된 /login redirect가 난다.
    if (!ready) return;
    let alive = true;
    api
      .getAuthConfig()
      .then((config) => (config.auth_mode === "session" ? api.getAuthMe() : null))
      .then((me) => {
        if (me) setOfflineNoteOwner({ userId: me.user_id, nodeId: me.node_id });
      })
      .catch((err: unknown) => {
        if (!alive) return;
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          deactivateOfflineNoteOwner();
          const next = `${window.location.pathname}${window.location.search}`;
          const error = err.status === 403 ? "&error=not_invited" : "";
          window.location.replace(`/login?next=${encodeURIComponent(next)}${error}`);
        }
      });
    return () => {
      alive = false;
    };
  }, [pathname, ready]);

  if (!ready) {
    // 빈 화면 대신 가벼운 브랜드 스켈레톤 — 게이트가 도는 짧은 시간에도 앱이
    // 살아 있음을 보여준다(토큰만 사용, 콘텐츠 스켈레톤은 각 페이지 담당).
    return (
      <div
        className="flex h-screen w-screen items-center justify-center"
        style={{ background: "var(--color-app-canvas)", height: "100dvh" }}
      >
        <div className="flex animate-pulse flex-col items-center gap-3">
          <BrandMark size="sm" />
          <span
            aria-hidden
            className="h-1 w-24 rounded-full"
            style={{ background: "var(--color-divider)" }}
          />
        </div>
      </div>
    );
  }

  return <>{children}</>;
}

function shouldAutoDevLogin(config: AuthConfig, err: unknown): boolean {
  return (
    Boolean(DEV_AUTO_LOGIN_EMAIL) &&
    config.auth_mode === "session" &&
    config.dev_login_enabled === true &&
    err instanceof ApiError &&
    err.status === 401
  );
}

function ShellFrame({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const router = useRouter();
  const isBoard = pathname === "/board";
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [assistantDraft, setAssistantDraft] = useState("");
  const [compactShell, setCompactShell] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [navigationCollapsed, setNavigationCollapsed] = useState(false);
  // "대시보드" 채널 그룹 펼침 상태 — localStorage에 지속(기본 펼침).
  const [dashboardOpen, setDashboardOpen] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    return window.localStorage.getItem("orthus-dashboard-nav") !== "collapsed";
  });
  // 데스크톱 레일에서 열려 있는 슬라이드 패널(현재 팀 일정만).
  const [openRailPanel, setOpenRailPanel] = useState<RailPanel | null>(null);
  const [boardRailState, setBoardRailState] = useState<{
    activeSidebarItem: string;
    folders: PersonalBoardFolder[];
    viewMode: string;
    workspaceName: string;
    rightPanel: string;
  }>({
    activeSidebarItem: "home",
    folders: [],
    viewMode: "home",
    workspaceName: "개인 워크스페이스",
    rightPanel: "hidden",
  });
  const nodeKind = authConfig?.node_kind ?? null;
  const isPersonalNode = nodeKind === "personal";
  const isPersonalBoard = isBoard && isPersonalNode;
  const hideBoardChrome = isPersonalBoard && compactShell;
  const showDesktopChrome = !compactShell && !hideBoardChrome;
  const showMobileChrome = compactShell;
  const showPersonalSection =
    nodeKind === "company" && authConfig?.owner_scope_enabled === true;
  const railSlots = isPersonalBoard
    ? BOARD_RAIL_SLOTS
    : isPersonalNode
      ? PERSONAL_RAIL_SLOTS
      : showPersonalSection
        ? OWNER_SCOPE_RAIL_SLOTS
        : COMPANY_RAIL_SLOTS;
  const brandTitle = isPersonalNode ? boardRailState.workspaceName : "대리.ai";
  const brandTone = isPersonalNode ? "LOCAL" : "CENTRAL";
  const brandSubtitle = authConfig
    ? `${isPersonalNode ? "개인 노드" : "회사 노드"} · ${authConfig.node_id}`
    : "아카식 + 비서";
  // 컴패니 대시보드는 회사 노드에서만 노출.
  // 개인 노드 보드는 위 Ojosama 섹션(Home)이 진입점이라 좌측 nav "보드" 항목은 중복 → 제거.
  // 회사 노드 + owner-scope면 회사 섹션 바로 아래에 개인 섹션을 끼운다 → 회사/개인/공통/설정.
  const personalNodeBaseSections = NAV_SECTIONS.map((section) => ({
        ...section,
        label: section.label === "회사" ? "개인 지식" : section.label,
        items: section.items.filter(
          (item) =>
            item.href !== "/dashboard" &&
            item.href !== "/connectors" &&
            // 대시보드/팀 일정은 회사 노드 전용(개인 노드에선 "회사 노드에서만" 안내).
            item.href !== "/dashboard/calendar" &&
            // 메일은 개인 섹션(내 보드 아래)으로 옮겨 중복을 막는다.
            item.href !== "/mail" &&
            !item.href.startsWith("/board"),
        ),
      })).filter((section) => section.items.length > 0);
  // owner-scope 회사 노드에서도 개인 섹션에 메일이 들어가므로 회사 섹션의 메일은 뺀다.
  const ownerScopeFirstSection = {
    ...NAV_SECTIONS[0],
    items: NAV_SECTIONS[0].items.filter((item) => item.href !== "/mail"),
  };
  // owner-scope에선 커넥터가 회사 섹션의 "커넥터"(회사/개인 탭)로 통합되므로
  // 개인 섹션의 "개인 커넥터" 중복 항목은 뺀다.
  const ownerScopePersonalSection = {
    ...PERSONAL_NAV_SECTION,
    items: PERSONAL_NAV_SECTION.items.filter(
      (item) => item.href !== "/connectors/personal",
    ),
  };
  const navSections = isPersonalNode
    ? [PERSONAL_NAV_SECTION, ...personalNodeBaseSections]
    : showPersonalSection
      ? [ownerScopeFirstSection, ownerScopePersonalSection, ...NAV_SECTIONS.slice(1)]
      : NAV_SECTIONS;

  useEffect(() => {
    let alive = true;
    api
      .getAuthConfig()
      .then((config) => {
        if (alive) setAuthConfig(config);
      })
      .catch(() => {
        if (alive) setAuthConfig(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  // 체크리스트 배지 — 라우트 전환마다 전체 목록을 다시 받지 않는다. inbox-summary
  // count는 wiki_task/agent_task 행을 포함해 배지 시맨틱(isChecklistBadgeItem)과
  // 다르므로 listAgentWork를 유지하되 60초 TTL SWR 캐시 + window focus 갱신으로 돌린다.
  const badgeRes = useCachedResource<AgentWorkItem[]>(
    AGENT_WORK_BADGE_KEY,
    () => api.listAgentWork(),
    { ttlMs: 60_000 },
  );
  const agentWorkCount = badgeRes.data
    ? badgeRes.data.filter(isChecklistBadgeItem).length
    : null;
  // 개인 "알림" 배지 — 위임 등으로 내 보드에 생성된 태스크 알림의 unread 수.
  // 서버 endpoint 미배포(404)면 error로 남고 count는 0 → 배지 미표시(silent).
  const notificationsRes = useCachedResource<PersonalBoardNotificationList>(
    NOTIFICATIONS_BADGE_KEY,
    () => api.boardNotifications(20),
    { ttlMs: 60_000 },
  );
  // 롤아웃 첫 로드 prime: lastSeen이 없으면 전부 읽음 처리하고 최신 created_at만
  // 저장한다 — 기능 배포 순간 거대한 backlog 배지가 뜨지 않게 (mail notify와 동형).
  useEffect(() => {
    const items = notificationsRes.data?.items;
    if (!items || items.length === 0) return;
    try {
      if (window.localStorage.getItem(NOTIFICATIONS_LAST_SEEN_KEY)) return;
      let newest = "";
      let newestMs = 0;
      for (const item of items) {
        const ms = Date.parse(item.created_at);
        if (Number.isFinite(ms) && ms > newestMs) {
          newestMs = ms;
          newest = item.created_at;
        }
      }
      if (newest) window.localStorage.setItem(NOTIFICATIONS_LAST_SEEN_KEY, newest);
    } catch {
      // storage blocked — this session simply shows no unread badge.
    }
  }, [notificationsRes.data]);
  // unread = lastSeen보다 새 알림 수. data 객체는 revalidate마다 새 identity라
  // (/notifications의 lastSeen 갱신 → refreshNotificationsBadge 이후) 재계산된다.
  // 첫(hydration) 렌더는 캐시가 비어 data=null → 0이라 SSR 마크업과도 일치한다.
  const notificationsUnread = useMemo(() => {
    const items = notificationsRes.data?.items;
    if (!items || typeof window === "undefined") return 0;
    let lastSeen: string | null = null;
    try {
      lastSeen = window.localStorage.getItem(NOTIFICATIONS_LAST_SEEN_KEY);
    } catch {
      return 0;
    }
    if (!lastSeen) return 0; // unprimed — prime effect가 전부 읽음 처리한다.
    const lastSeenMs = Date.parse(lastSeen);
    if (!Number.isFinite(lastSeenMs)) return 0;
    return items.filter((item) => {
      const ms = Date.parse(item.created_at);
      return Number.isFinite(ms) && ms > lastSeenMs;
    }).length;
  }, [notificationsRes.data]);

  const refreshBadge = badgeRes.refresh;
  const refreshNotifyBadge = notificationsRes.refresh;
  useEffect(() => {
    function handleFocus() {
      void refreshBadge();
      void refreshNotifyBadge();
    }
    window.addEventListener("focus", handleFocus);
    return () => {
      window.removeEventListener("focus", handleFocus);
    };
  }, [refreshBadge, refreshNotifyBadge]);

  // Desktop-only 네이티브 OS 알림 (use-desktop-notify-signal.ts에서 폴링/발화).
  // mail은 기존 `OrthusNotify` 게이트/키/억제 경로 그대로(무동작 변경 리팩터),
  // board/review는 orthus://notify/board·review 라우팅을 아는 신형 shell 빌드만
  // 광고하는 `OrthusNotify/2` 게이트라 구 앱은 신호 자체를 받지 않는다.
  useDesktopNotifySignal({
    channel: "mail",
    storageKey: "orthus.mail.notify.lastSeen",
    uaToken: "OrthusNotify",
    fetchState: api.mailNotifyState,
    suppressPaths: ["/mail"],
  });
  useDesktopNotifySignal({
    channel: "board",
    storageKey: "orthus.board.notify.lastSeen",
    uaToken: "OrthusNotify/2",
    fetchState: api.boardNotifyState,
    suppressPaths: ["/board", "/notifications"],
  });
  useDesktopNotifySignal({
    channel: "review",
    storageKey: "orthus.review.notify.lastSeen",
    uaToken: "OrthusNotify/2",
    fetchState: api.agentWorkNotifyState,
    suppressPaths: ["/agent-work", "/notifications"],
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    function syncCompactShell() {
      const compact = window.innerWidth < 760;
      setCompactShell(compact);
      // 데스크톱 전용 슬라이드 패널은 모바일 폭에서 렌더되지 않으므로 같이 닫는다.
      if (compact) setOpenRailPanel(null);
    }
    syncCompactShell();
    window.addEventListener("resize", syncCompactShell);
    return () => {
      window.removeEventListener("resize", syncCompactShell);
    };
  }, []);

  useEffect(() => {
    if (!isBoard || typeof window === "undefined") return;
    function handleBoardState(event: Event) {
      const detail = (
        event as CustomEvent<{
          activeSidebarItem?: string;
          folders?: PersonalBoardFolder[];
          viewMode?: string;
          workspaceName?: string;
          rightPanel?: string;
        }>
      ).detail;
      setBoardRailState((current) => ({
        activeSidebarItem: detail?.activeSidebarItem ?? current.activeSidebarItem,
        folders: detail?.folders ?? current.folders,
        viewMode: detail?.viewMode ?? current.viewMode,
        workspaceName: detail?.workspaceName ?? current.workspaceName,
        rightPanel: detail?.rightPanel ?? current.rightPanel,
      }));
    }
    window.addEventListener("orthus:personal-board-state", handleBoardState);
    return () => {
      window.removeEventListener("orthus:personal-board-state", handleBoardState);
    };
  }, [isBoard]);

  function activateRailSlot(slot: RailSlot) {
    if (slot.boardCommand && isPersonalNode) {
      activateBoardView(slot.boardCommand);
      return;
    }
    // 데스크톱 레일에서는 패널 슬롯을 토글한다. 모바일(빠른 액션)에서는 패널
    // 대신 slot.href로 전체 페이지로 이동한다.
    if (slot.panel && !compactShell) {
      setOpenRailPanel((current) => (current === slot.panel ? null : slot.panel ?? null));
      return;
    }
    if (slot.href) {
      // 클라이언트 라우팅 — 전체 문서 리로드(window.location.assign) 금지.
      router.push(slot.href);
    }
  }

  function railSlotActive(slot: RailSlot) {
    if (isPersonalBoard && slot.boardCommand) {
      if (slot.boardCommand === "backlog") return boardRailState.rightPanel === "backlog";
      return boardRailState.viewMode === slot.boardCommand;
    }
    if (slot.panel && !compactShell) return openRailPanel === slot.panel;
    return slot.href ? hrefActive(slot.href) : false;
  }

  function toggleDashboardNav() {
    setDashboardOpen((open) => {
      const next = !open;
      try {
        window.localStorage.setItem("orthus-dashboard-nav", next ? "open" : "collapsed");
      } catch {
        /* localStorage 불가(사파리 프라이빗 등)면 세션 한정 토글로 폴백 */
      }
      return next;
    });
  }

  // 대시보드 채널 개별 활성 판정 — "개요"(/dashboard)는 정확 일치, 나머지는 하위 경로 포함.
  function dashboardChannelActive(href: string): boolean {
    if (href === "/dashboard") return pathname === "/dashboard";
    return pathname === href || pathname.startsWith(`${href}/`);
  }

  function navItemActive(item: NavItem) {
    const { base, query } = splitHref(item.href);
    if (base === "/wiki") {
      return (
        (pathname === "/wiki" ||
          (pathname.startsWith("/wiki/") && !pathname.startsWith("/wiki/tasks"))) &&
        queryActive(query)
      );
    }
    if (base === "/connectors") {
      return pathname === "/connectors" && queryActive(query);
    }
    // 에이전트와 체크리스트는 (work) route group 공유 탭이므로 사이드바 "에이전트"는
    // 두 route 모두에서 활성화한다.
    if (base === "/agent-work") {
      return (
        (pathname === "/agent-work" ||
          pathname.startsWith("/agent-work/") ||
          pathname.startsWith("/checklist")) &&
        queryActive(query)
      );
    }
    // "프로젝트"를 사이드바 1급 항목으로 빼냈으므로(`/dashboard/projects`,
    // 전체 페이지 DB `/dashboard/db`), 그 하위에서는 "대시보드"가 아니라
    // "프로젝트"만 활성화되게 한다. "팀 일정"(`/dashboard/calendar`)도 1급 항목이라
    // 그 하위에서는 "대시보드"가 아니라 "팀 일정"만 활성화한다.
    const isProjectSection =
      pathname.startsWith("/dashboard/projects") || pathname.startsWith("/dashboard/db");
    const isCalendarSection = pathname.startsWith("/dashboard/calendar");
    if (base === "/dashboard/projects") {
      return isProjectSection && queryActive(query);
    }
    if (base === "/dashboard/calendar") {
      return isCalendarSection && queryActive(query);
    }
    if (base === "/dashboard") {
      return (
        (pathname === "/dashboard" ||
          (pathname.startsWith("/dashboard/") &&
            !isProjectSection &&
            !isCalendarSection)) &&
        queryActive(query)
      );
    }
    return hrefActive(item.href);
  }

  function splitHref(href: string): { base: string; query: string } {
    const [base, query = ""] = href.split("?");
    return { base, query };
  }

  function queryActive(query: string): boolean {
    if (!query) return true;
    const target = new URLSearchParams(query);
    for (const [key, value] of target.entries()) {
      if (searchParams.get(key) !== value) return false;
    }
    return true;
  }

  function hrefActive(href: string): boolean {
    const { base, query } = splitHref(href);
    const baseActive = pathname === base || pathname.startsWith(`${base}/`);
    if (!baseActive) return false;
    if (query) return queryActive(query);
    if (base === "/board" && searchParams.has("view")) return false;
    return true;
  }

  function dispatchBoardCommand(command: BoardCommand) {
    if (typeof window === "undefined") return;
    window.dispatchEvent(
      new CustomEvent<BoardCommandDetail>("orthus:personal-board-command", {
        detail: { command },
      }),
    );
  }

  function activateBoardView(command: BoardCommand) {
    if (typeof window === "undefined") return;
    // 이미 /board 위에서는 커맨드 이벤트로 처리(리마운트 없음 — ?view= 딥링크 무시됨).
    // 다른 라우트에서는 client push — 보드가 새로 mount되며 ?view=를 읽는다
    // (cold load 딥링크는 useStoredPreference paramKey가 처리, 시맨틱 동일).
    if (isPersonalBoard) {
      dispatchBoardCommand(command);
      return;
    }
    if (command !== "refresh") {
      router.push(`/board?view=${command}`);
    }
  }

  function submitAssistant(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = assistantDraft.trim();
    if (!query) return;
    setAssistantDraft("");
    router.push(`/ask?q=${encodeURIComponent(query)}`);
  }

  return (
    <div
      className="grid h-screen w-screen overflow-hidden"
      style={{
        gridTemplateColumns: compactShell
          ? "minmax(0, 1fr)"
          : `${navigationCollapsed ? "64px" : "var(--sidebar-width)"} minmax(0, 1fr) var(--integration-rail-width)`,
        gridTemplateRows: compactShell ? "minmax(0, 1fr) auto" : "minmax(0, 1fr)",
        height: "100dvh",
        background: "var(--color-app-canvas)",
      }}
    >
      {/* ───────── Sidebar ───────── */}
      {showDesktopChrome ? (
      <aside
        aria-label="주요 내비게이션"
        className="flex h-full min-h-0 flex-col"
        style={{ background: "var(--color-sidebar)" }}
      >
        {/* Brand block — uses sidebar-active so the logo sits in the
            same surface tone as the active nav item below. */}
        <div className="px-3 pt-3 pb-2">
          <div
            className={cx(
              "flex items-center rounded-[6px]",
              navigationCollapsed ? "flex-col gap-1.5 px-1.5 py-2" : "gap-2 px-2.5 py-2",
            )}
            style={{ background: "var(--color-sidebar-active)" }}
          >
            <BrandMark size="sm" />
            <div className={cx("min-w-0 flex-1 leading-tight", navigationCollapsed && "sr-only")}>
              <div
                className="flex min-w-0 items-center gap-1.5 text-[13px] font-bold"
                style={{ color: "var(--color-text-strong)" }}
              >
                <span className="truncate">{brandTitle}</span>
                <span
                  className="shrink-0 rounded-[3px] px-1 py-[1px] font-[family-name:var(--font-mono)] text-[8px] font-bold"
                  style={{
                    background: "var(--color-card)",
                    color: "var(--color-text-muted)",
                    border: "1px solid var(--color-divider)",
                    lineHeight: 1,
                  }}
                >
                  {brandTone}
                </span>
              </div>
              <div
                className="truncate text-[10px] font-medium"
                style={{ color: "var(--color-text-muted)" }}
              >
                {brandSubtitle}
              </div>
            </div>
            {isPersonalNode ? (
              <button
                aria-label={navigationCollapsed ? "Expand navigation" : "Collapse navigation"}
                aria-pressed={navigationCollapsed}
                className={cx(
                  "flex shrink-0 items-center justify-center rounded-[5px] transition-transform",
                  navigationCollapsed ? "h-6 w-6" : "h-7 w-7",
                )}
                onClick={() => setNavigationCollapsed((collapsed) => !collapsed)}
                style={{
                  color: "var(--color-text-muted)",
                  transform: navigationCollapsed ? "rotate(180deg)" : undefined,
                }}
                type="button"
              >
                <ChevronsLeftIcon />
              </button>
            ) : null}
          </div>
        </div>

        {/* Nav — sectioned with uppercase 13px/800 labels */}
        <nav className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-2 py-2">
	          {isPersonalNode ? (
	            <div>
	              <div className={cx("px-2 pb-1 pt-1 text-[13px] font-extrabold uppercase tracking-[0.02em]", navigationCollapsed && "sr-only")} style={{ color: "var(--color-text-muted)" }}>
	                Ojosama
	              </div>
	              <div className="flex flex-col gap-[2px]">
	                <BoardSideButton active={isPersonalBoard && boardRailState.activeSidebarItem === "home"} collapsed={navigationCollapsed} icon={HomeIcon} label="Home" onClick={() => activateBoardView("home")} />
	                <BoardSideButton active={isPersonalBoard && boardRailState.activeSidebarItem === "weekly_plan"} collapsed={navigationCollapsed} icon={CalendarIcon} label="Weekly plan" onClick={() => activateBoardView("weekly_plan")} />
	                <BoardSideButton active={isPersonalBoard && boardRailState.activeSidebarItem === "weekly_review"} collapsed={navigationCollapsed} icon={GaugeIcon} label="Weekly review" onClick={() => activateBoardView("weekly_review")} />
	                <BoardSideButton active={isPersonalBoard && boardRailState.rightPanel === "backlog"} collapsed={navigationCollapsed} icon={ArchiveIcon} label="Backlog" onClick={() => activateBoardView("backlog")} />
	              </div>
	            </div>
	          ) : null}
          {navSections.map((section) => (
            <div key={section.label}>
              <div
                className={cx("px-2 pb-1 pt-1 text-[13px] font-extrabold uppercase tracking-[0.02em]", navigationCollapsed && "sr-only")}
                style={{ color: "var(--color-text-muted)" }}
              >
                {section.label}
              </div>
              <ul className="flex flex-col gap-[2px]">
                {section.items.map((item) => {
                  // "대시보드"는 flat 링크 대신 접이식 채널 그룹으로 렌더(데스크톱 전용).
                  if (item.href === "/dashboard") {
                    return (
                      <DashboardNavGroup
                        key="dashboard-group"
                        open={dashboardOpen}
                        collapsed={navigationCollapsed}
                        onToggle={toggleDashboardNav}
                        channelActive={dashboardChannelActive}
                      />
                    );
                  }
                  const active = navItemActive(item);
                  const Icon = item.icon;
                  return (
                    <li key={item.href}>
                      <Link
                        href={item.href}
                        aria-current={active ? "page" : undefined}
                        className={cx(
                          "group relative flex h-[38px] items-center gap-2 rounded-[6px] px-2.5 transition-colors",
                          navigationCollapsed && "justify-center px-0",
                        )}
                        style={{
                          background: active
                            ? "var(--color-sidebar-active)"
                            : "transparent",
                          color: active
                            ? "var(--color-text-strong)"
                            : "var(--color-text-body)",
                        }}
                        onMouseEnter={(e) => {
                          if (!active)
                            e.currentTarget.style.background =
                              "color-mix(in srgb, var(--color-sidebar-active) 55%, transparent)";
                        }}
                        onMouseLeave={(e) => {
                          if (!active)
                            e.currentTarget.style.background = "transparent";
                        }}
                      >
                        <span
                          className="flex h-[18px] w-[18px] shrink-0 items-center justify-center"
                          aria-hidden
                        >
                          <Icon />
                        </span>
                        <span className={cx("truncate text-[13px] font-medium", navigationCollapsed && "sr-only")}>
                          {item.label}
                        </span>
                        {isAgentQueueHref(item.href) ? (
                          <AgentWorkBadge
                            className={navigationCollapsed ? "absolute right-0 top-0" : "ml-auto"}
                            compact={navigationCollapsed}
                            count={agentWorkCount}
                          />
                        ) : null}
                        {isNotificationsHref(item.href) && notificationsUnread > 0 ? (
                          <AgentWorkBadge
                            className={navigationCollapsed ? "absolute right-0 top-0" : "ml-auto"}
                            compact={navigationCollapsed}
                            count={notificationsUnread}
                          />
                        ) : null}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>

        <AuthFooter collapsed={navigationCollapsed} />
      </aside>
      ) : null}

      {/* ───────── Content canvas ───────── */}
      <main
        className="flex h-full min-w-0 flex-col overflow-hidden"
        style={{
          background: "var(--color-app-canvas)",
          gridColumn: compactShell ? "1" : undefined,
          gridRow: compactShell ? "1" : undefined,
        }}
      >
        {/* 모바일에서는 상단 고정 커맨드 바(위키 홈·메일·에이전트 요청)를 없앤다 —
            하단 네비가 같은 목적지를 다 커버한다(owner 피드백 2026-07-05). */}
        {!compactShell ? (
          <WorkspaceCommandBar
            agentWorkCount={agentWorkCount}
            assistantDraft={assistantDraft}
            onAssistantDraftChange={setAssistantDraft}
            onAssistantSubmit={submitAssistant}
            pathname={pathname}
          />
        ) : null}
        <div className="min-h-0 flex-1 overflow-hidden">{children}</div>
      </main>

      {/* ───────── Mobile navigation ───────── */}
      {showMobileChrome ? (
        <MobileChrome
          isPersonalBoard={isPersonalBoard}
          mobileMenuOpen={mobileMenuOpen}
          navItemActive={navItemActive}
          navSections={navSections}
          onActivateRailSlot={activateRailSlot}
          onCloseMenu={() => setMobileMenuOpen(false)}
          onToggleMenu={() => setMobileMenuOpen((open) => !open)}
          agentWorkCount={agentWorkCount}
          notificationsUnread={notificationsUnread}
          railSlotActive={railSlotActive}
          railSlots={railSlots}
        />
      ) : null}

      {/* ───────── Integration rail ───────── */}
      {showDesktopChrome ? (
      <aside
        aria-label="통합 레일"
        className="flex h-full flex-col items-center gap-1 py-3"
        style={{
          background: "var(--color-sidebar)",
          borderLeft: "1px solid var(--color-divider)",
        }}
      >
	        {railSlots.map((slot) => {
	              const Icon = slot.icon;
	              const active = railSlotActive(slot);
	              return (
                <RailButton
                  active={active}
                  hasNotification={slot.hasNotification}
                  notificationCount={slot.key === "agent_work" ? agentWorkCount : undefined}
                  key={slot.key}
                  label={slot.label}
                  onClick={() => activateRailSlot(slot)}
                >
                  <Icon />
                </RailButton>
              );
	            })}
	        {!isPersonalNode ? (
	          <>
	            <div
	              aria-hidden
	              className="my-1 h-px w-5"
	              style={{ background: "var(--color-divider)" }}
	            />
	            <button
	              type="button"
	              title="Add integration"
	              aria-label="Add integration"
	              onClick={() => router.push("/connectors")}
	              className="flex h-9 w-9 items-center justify-center rounded-[6px] transition-colors"
	              style={{ color: "var(--color-text-faint)" }}
	              onMouseEnter={(e) => {
	                e.currentTarget.style.background = "var(--color-sidebar-active)";
	                e.currentTarget.style.color = "var(--color-text-body)";
	              }}
	              onMouseLeave={(e) => {
	                e.currentTarget.style.background = "transparent";
	                e.currentTarget.style.color = "var(--color-text-faint)";
	              }}
	            >
	              <PlusIcon />
	            </button>
	          </>
	        ) : null}
      </aside>
      ) : null}

      {/* ───────── Team schedule quick panel (rail) ───────── */}
      {showDesktopChrome && openRailPanel === "team_schedule" ? (
        <TeamSchedulePanel onClose={() => setOpenRailPanel(null)} />
      ) : null}
    </div>
  );
}

function MobileChrome({
  agentWorkCount,
  isPersonalBoard,
  mobileMenuOpen,
  navItemActive,
  navSections,
  notificationsUnread,
  onActivateRailSlot,
  onCloseMenu,
  onToggleMenu,
  railSlotActive,
  railSlots,
}: {
  agentWorkCount: number | null;
  isPersonalBoard: boolean;
  mobileMenuOpen: boolean;
  navItemActive: (item: NavItem) => boolean;
  navSections: ReadonlyArray<{ label: string; items: ReadonlyArray<NavItem> }>;
  notificationsUnread: number;
  onActivateRailSlot: (slot: RailSlot) => void;
  onCloseMenu: () => void;
  onToggleMenu: () => void;
  railSlotActive: (slot: RailSlot) => boolean;
  railSlots: ReadonlyArray<RailSlot>;
}) {
  const boardSlots = railSlots.filter((slot) => slot.boardCommand);
  return (
    <>
      <nav
        aria-label="모바일 내비게이션"
        className="grid min-h-[var(--mobile-nav-height)] items-stretch gap-1 px-2 pt-1.5"
        style={{
          background: "var(--color-sidebar)",
          borderTop: "1px solid var(--color-divider)",
          paddingBottom: "max(6px, var(--mobile-safe-bottom))",
          gridColumn: "1",
          gridTemplateColumns: `repeat(${isPersonalBoard ? 5 : MOBILE_PRIMARY_NAV.length + 1}, minmax(0, 1fr))`,
          gridRow: "2",
        }}
      >
        {isPersonalBoard
          ? (
              <>
                {boardSlots.slice(0, 3).map((slot) => {
                  const Icon = slot.icon;
                  return (
                    <MobileRailButton
                      active={railSlotActive(slot)}
                      key={slot.key}
                      label={slot.label}
                      onClick={() => onActivateRailSlot(slot)}
                    >
                      <Icon />
                    </MobileRailButton>
                  );
                })}
                <Link
                  className="flex min-w-0 flex-col items-center justify-center gap-0.5 rounded-[6px] px-1 text-[10px] font-bold"
                  href="/ask"
                  style={{
                    background: "transparent",
                    color: "var(--color-text-muted)",
                  }}
                >
                  <span className="flex h-[20px] w-[20px] items-center justify-center" aria-hidden>
                    <TerminalIcon />
                  </span>
                  <span className="max-w-full truncate">질문</span>
                </Link>
              </>
            )
          : MOBILE_PRIMARY_NAV.map((item) => {
              const Icon = item.icon;
              const active = navItemActive(item);
              const boardCommand = item.boardCommand;
              return (
                <Link
                  aria-current={active ? "page" : undefined}
                  className="relative flex min-w-0 flex-col items-center justify-center gap-0.5 rounded-[6px] px-1 text-[10px] font-bold"
                  href={item.href}
                  key={item.href}
                  onClick={
                    boardCommand
                      ? (event) => {
                          // 보드 위에서 누르면 리마운트가 없어 ?view= 딥링크가 무시되므로
                          // 커맨드 이벤트로 직접 스케줄 패널을 연다.
                          if (window.location.pathname === "/board") {
                            event.preventDefault();
                            window.dispatchEvent(
                              new CustomEvent<BoardCommandDetail>("orthus:personal-board-command", {
                                detail: { command: boardCommand },
                              }),
                            );
                          }
                        }
                      : undefined
                  }
                  style={{
                    background: active ? "var(--color-sidebar-active)" : "transparent",
                    color: active ? "var(--color-text-strong)" : "var(--color-text-muted)",
                  }}
                >
                  <span className="flex h-[20px] w-[20px] items-center justify-center" aria-hidden>
                    <Icon />
                  </span>
                  <span className="max-w-full truncate">{item.label}</span>
                  {isAgentQueueHref(item.href) ? (
                    <AgentWorkBadge className="absolute right-1 top-1" compact count={agentWorkCount} />
                  ) : null}
                </Link>
              );
            })}
        <button
          aria-expanded={mobileMenuOpen}
          aria-label="더보기 메뉴"
          className="relative flex min-w-0 flex-col items-center justify-center gap-0.5 rounded-[6px] px-1 text-[10px] font-bold"
          onClick={onToggleMenu}
          style={{
            background: mobileMenuOpen ? "var(--color-sidebar-active)" : "transparent",
            color: mobileMenuOpen ? "var(--color-text-strong)" : "var(--color-text-muted)",
          }}
          type="button"
        >
          <span className="flex h-[20px] w-[20px] items-center justify-center" aria-hidden>
            <MoreIcon />
          </span>
          <span>더보기</span>
          {isPersonalBoard ? (
            <AgentWorkBadge className="absolute right-1 top-1" compact count={agentWorkCount} />
          ) : null}
        </button>
      </nav>

      {mobileMenuOpen ? (
        <div
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-end"
          role="dialog"
        >
          <button
            aria-label="모바일 메뉴 닫기"
            className="absolute inset-0 h-full w-full"
            onClick={onCloseMenu}
            style={{ background: "rgba(0, 0, 0, 0.22)" }}
            type="button"
          />
          <section
            className="relative grid max-h-[82dvh] w-full grid-rows-[auto_minmax(0,1fr)_auto] rounded-t-[10px]"
            style={{
              background: "var(--color-sidebar)",
              borderTop: "1px solid var(--color-divider)",
              boxShadow: "0 -16px 42px rgba(0, 0, 0, 0.18)",
              paddingBottom: "var(--mobile-safe-bottom)",
            }}
          >
            <div
              className="flex items-center justify-between gap-3 px-4 py-3"
              style={{ borderBottom: "1px solid var(--color-divider)" }}
            >
              <div className="flex min-w-0 items-center gap-2">
                <BrandMark size="sm" />
                <div className="min-w-0">
                <div
                  className="truncate text-[var(--text-body-sm)] font-extrabold"
                  style={{ color: "var(--color-text-strong)" }}
                >
                  대리.ai
                </div>
                <div
                  className="truncate text-[var(--text-meta)]"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  모바일 메뉴
                </div>
                </div>
              </div>
              <button
                aria-label="닫기"
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[6px]"
                onClick={onCloseMenu}
                style={{
                  background: "var(--color-card)",
                  border: "1px solid var(--color-divider)",
                  color: "var(--color-text-body)",
                }}
                type="button"
              >
                <CloseIcon />
              </button>
            </div>

            <nav className="min-h-0 overflow-y-auto px-3 py-3">
              {navSections.map((section) => (
                <div className="mb-4" key={section.label}>
                  <div
                    className="px-1 pb-1 text-[var(--text-meta)] font-extrabold uppercase"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {section.label}
                  </div>
                  <div className="grid gap-1">
                    {section.items.map((item) => {
                      const Icon = item.icon;
                      const active = navItemActive(item);
                      return (
                        <Link
                          aria-current={active ? "page" : undefined}
                          className="flex min-h-11 items-center gap-2 rounded-[6px] px-2.5 text-[var(--text-body-sm)] font-semibold"
                          href={item.href}
                          key={item.href}
                          onClick={onCloseMenu}
                          style={{
                            background: active ? "var(--color-sidebar-active)" : "var(--color-card)",
                            border: "1px solid var(--color-divider)",
                            color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
                          }}
                        >
                          <span className="flex h-[18px] w-[18px] shrink-0 items-center justify-center" aria-hidden>
                            <Icon />
                          </span>
                          <span className="truncate">{item.label}</span>
                          {isAgentQueueHref(item.href) ? (
                            <AgentWorkBadge className="ml-auto" count={agentWorkCount} />
                          ) : null}
                          {isNotificationsHref(item.href) && notificationsUnread > 0 ? (
                            <AgentWorkBadge className="ml-auto" count={notificationsUnread} />
                          ) : null}
                        </Link>
                      );
                    })}
                  </div>
                </div>
              ))}
              {railSlots.length ? (
                <div>
                  <div
                    className="px-1 pb-1 text-[var(--text-meta)] font-extrabold uppercase"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    빠른 액션
                  </div>
                  <div className="grid gap-1">
                    {railSlots.map((slot) => {
                      const Icon = slot.icon;
                      const active = railSlotActive(slot);
                      return (
                        <button
                          aria-pressed={active || undefined}
                          className="flex min-h-11 items-center gap-2 rounded-[6px] px-2.5 text-[var(--text-body-sm)] font-semibold"
                          key={slot.key}
                          onClick={() => {
                            onActivateRailSlot(slot);
                            onCloseMenu();
                          }}
                          style={{
                            background: active ? "var(--color-sidebar-active)" : "var(--color-card)",
                            border: "1px solid var(--color-divider)",
                            color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
                          }}
                          type="button"
                        >
                          <span className="flex h-[18px] w-[18px] shrink-0 items-center justify-center" aria-hidden>
                            <Icon />
                          </span>
                          <span className="truncate">{slot.label}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              ) : null}
            </nav>

            <AuthFooter collapsed={false} />
          </section>
        </div>
      ) : null}
    </>
  );
}

function WorkspaceCommandBar({
  agentWorkCount,
  assistantDraft,
  onAssistantDraftChange,
  onAssistantSubmit,
  pathname,
}: {
  agentWorkCount: number | null;
  assistantDraft: string;
  onAssistantDraftChange: (value: string) => void;
  onAssistantSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
  pathname: string;
}) {
  const wikiActive = pathname === "/wiki" || pathname.startsWith("/wiki/");
  const mailActive = pathname === "/mail" || pathname.startsWith("/mail/");
  const agentWorkActive = pathname === "/agent-work" || pathname.startsWith("/agent-work/");

  return (
    <div
      className="shrink-0 px-3 py-2 sm:px-5"
      style={{
        background: "var(--color-app-canvas)",
        borderBottom: "1px solid var(--color-divider)",
      }}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Link
          aria-current={wikiActive ? "page" : undefined}
          className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] font-semibold"
          href="/wiki"
          style={{
            background: wikiActive ? "var(--color-sidebar-active)" : "var(--color-card)",
            border: "1px solid var(--color-toolbar-border)",
            boxShadow: "var(--shadow-toolbar)",
            color: "var(--color-text-strong)",
          }}
        >
          <span className="flex h-4 w-4 items-center justify-center" aria-hidden>
            <SearchIcon />
          </span>
          <span>위키 홈</span>
        </Link>

        <Link
          aria-current={mailActive ? "page" : undefined}
          className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] font-semibold"
          href="/mail"
          style={{
            background: mailActive ? "var(--color-sidebar-active)" : "var(--color-card)",
            border: "1px solid var(--color-toolbar-border)",
            boxShadow: "var(--shadow-toolbar)",
            color: "var(--color-text-strong)",
          }}
        >
          <span className="flex h-4 w-4 items-center justify-center" aria-hidden>
            <MailIcon />
          </span>
          <span>메일</span>
        </Link>

        <form className="flex min-w-[190px] flex-1 items-center gap-1.5" onSubmit={onAssistantSubmit}>
          <label className="sr-only" htmlFor="global-assistant-q">
            에이전트
          </label>
          <div
            className="flex h-8 min-w-0 flex-1 items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2"
            style={{
              background: "var(--color-card)",
              border: "1px solid var(--color-toolbar-border)",
              boxShadow: "var(--shadow-toolbar)",
            }}
          >
            <span
              className="flex h-4 w-4 shrink-0 items-center justify-center"
              style={{ color: "var(--color-text-muted)" }}
              aria-hidden
            >
              <TerminalIcon />
            </span>
            <input
              id="global-assistant-q"
              className="min-w-0 flex-1 bg-transparent text-[var(--text-body-sm)] outline-none"
              onChange={(event) => onAssistantDraftChange(event.target.value)}
              placeholder="에이전트에게 요청"
              style={{ color: "var(--color-text-body)" }}
              value={assistantDraft}
            />
          </div>
          <button
            className="inline-flex h-8 shrink-0 items-center rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] font-semibold"
            disabled={!assistantDraft.trim()}
            style={{
              background: "var(--color-card)",
              border: "1px solid var(--color-toolbar-border)",
              boxShadow: "var(--shadow-toolbar)",
              color: "var(--color-text-strong)",
              opacity: assistantDraft.trim() ? 1 : 0.55,
            }}
            type="submit"
          >
            실행
          </button>
        </form>

        <Link
          aria-current={agentWorkActive ? "page" : undefined}
          className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] font-semibold"
          href="/agent-work"
          style={{
            background: agentWorkActive ? "var(--color-sidebar-active)" : "var(--color-card)",
            border: "1px solid var(--color-toolbar-border)",
            boxShadow: "var(--shadow-toolbar)",
            color: "var(--color-text-strong)",
          }}
        >
          <span className="flex h-4 w-4 items-center justify-center" aria-hidden>
            <TaskIcon />
          </span>
          <span>에이전트</span>
          <AgentWorkBadge count={agentWorkCount} />
        </Link>
      </div>
    </div>
  );
}

function MobileRailButton({
  active,
  children,
  label,
  onClick,
}: {
  active: boolean;
  children: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      aria-pressed={active || undefined}
      className="flex min-w-0 flex-col items-center justify-center gap-0.5 rounded-[6px] px-1 text-[10px] font-bold"
      onClick={onClick}
      style={{
        background: active ? "var(--color-sidebar-active)" : "transparent",
        color: active ? "var(--color-text-strong)" : "var(--color-text-muted)",
      }}
      type="button"
    >
      <span className="flex h-[20px] w-[20px] items-center justify-center" aria-hidden>
        {children}
      </span>
      <span className="max-w-full truncate">{label}</span>
    </button>
  );
}

function BoardSideButton({
  active = false,
  collapsed,
  icon: Icon,
  label,
  onClick,
}: {
  active?: boolean;
  collapsed: boolean;
  icon: React.ComponentType;
  label: string;
  onClick?: () => void;
}) {
  return (
    <button
      aria-label={label}
      aria-pressed={active || undefined}
      className={cx(
        "group flex h-[38px] items-center gap-2 rounded-[6px] px-2.5 transition-colors",
        collapsed && "justify-center px-0",
      )}
      onClick={onClick}
      style={{
        background: active ? "var(--color-sidebar-active)" : "transparent",
        color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
      }}
      onMouseEnter={(e) => {
        if (!active)
          e.currentTarget.style.background =
            "color-mix(in srgb, var(--color-sidebar-active) 55%, transparent)";
      }}
      onMouseLeave={(e) => {
        if (!active) e.currentTarget.style.background = "transparent";
      }}
      type="button"
    >
      <span className="flex h-[18px] w-[18px] shrink-0 items-center justify-center" aria-hidden>
        <Icon />
      </span>
      <span className={cx("truncate text-[13px] font-medium", collapsed && "sr-only")}>
        {label}
      </span>
    </button>
  );
}

/* ---------------------- Team schedule quick panel ---------------------- */

// 레일 "팀 일정" 버튼이 여는 슬라이드 패널. /dashboard/calendar 전체 페이지로
// 가지 않고도 앞으로 2주 팀 일정을 한눈에 본다. 열릴 때만 마운트돼 그때 fetch한다.
const TEAM_SCHEDULE_WINDOW_DAYS = 14;
const _WEEKDAY_KO = ["일", "월", "화", "수", "목", "금", "토"];

function ymdLocal(d: Date): string {
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

function dayHeading(iso: string, todayIso: string, tomorrowIso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  const md = Number.isNaN(d.getTime())
    ? iso
    : `${d.getMonth() + 1}/${d.getDate()} (${_WEEKDAY_KO[d.getDay()]})`;
  if (iso === todayIso) return `오늘 · ${md}`;
  if (iso === tomorrowIso) return `내일 · ${md}`;
  return md;
}

function eventTimeLabel(e: CalendarEvent): string {
  if (e.all_day || !e.start_time) return "종일";
  const s = e.start_time.slice(0, 5);
  const en = e.end_time ? e.end_time.slice(0, 5) : "";
  return en ? `${s}–${en}` : s;
}

function TeamSchedulePanel({ onClose }: { onClose: () => void }) {
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let alive = true;
    const today = new Date();
    const from = ymdLocal(today);
    const end = new Date(today);
    end.setDate(end.getDate() + TEAM_SCHEDULE_WINDOW_DAYS - 1);
    const to = ymdLocal(end);
    Promise.all([api.listCalendar(from, to), api.listTeamMembers()])
      .then(([ev, mem]) => {
        if (!alive) return;
        setEvents(ev);
        setMembers(mem);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const todayIso = ymdLocal(new Date());
  const tomorrowIso = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    return ymdLocal(d);
  }, []);

  const memberColor = (id: string) =>
    members.find((m) => m.member_id === id)?.color ?? null;
  const membersLabel = (ids: string[]): string => {
    if (!ids.length) return "회사 전체";
    if (members.length > 0 && members.every((m) => ids.includes(m.member_id))) {
      return "회사 전체";
    }
    const names = ids
      .map((id) => members.find((m) => m.member_id === id)?.name)
      .filter((n): n is string => Boolean(n));
    return names.length ? names.join(", ") : "회사 전체";
  };

  // 날짜별 그룹(오름차순). 하루 안에서는 종일 먼저, 그다음 시작 시간 순.
  const grouped = useMemo(() => {
    const byDay = new Map<string, CalendarEvent[]>();
    for (const e of events) {
      const arr = byDay.get(e.event_date) ?? [];
      arr.push(e);
      byDay.set(e.event_date, arr);
    }
    return [...byDay.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(
        ([iso, evs]) =>
          [
            iso,
            [...evs].sort((a, b) => {
              const at = a.all_day || !a.start_time ? "" : a.start_time;
              const bt = b.all_day || !b.start_time ? "" : b.start_time;
              return at.localeCompare(bt);
            }),
          ] as [string, CalendarEvent[]],
      );
  }, [events]);

  return (
    <>
      {/* 바깥 클릭 닫기(투명 오버레이). 레일 버튼 위도 덮으므로 다시 눌러도 닫힌다. */}
      <button
        aria-label="팀 일정 패널 닫기"
        type="button"
        onClick={onClose}
        className="fixed inset-0 z-40"
        style={{ background: "transparent" }}
      />
      <aside
        aria-label="팀 일정"
        className="fixed z-50 flex flex-col"
        style={{
          top: 0,
          bottom: 0,
          right: "var(--integration-rail-width)",
          width: "min(340px, calc(100vw - var(--integration-rail-width)))",
          background: "var(--color-card)",
          borderLeft: "1px solid var(--color-divider)",
          boxShadow: "-16px 0 42px rgba(0, 0, 0, 0.14)",
        }}
      >
        <div
          className="flex shrink-0 items-center justify-between gap-2 px-3.5 py-3"
          style={{ borderBottom: "1px solid var(--color-divider)" }}
        >
          <div className="flex min-w-0 items-center gap-2">
            <span
              className="flex h-[18px] w-[18px] items-center justify-center"
              style={{ color: "var(--color-text-muted)" }}
              aria-hidden
            >
              <CalendarIcon />
            </span>
            <h2
              className="truncate text-[var(--text-body)] font-bold"
              style={{ color: "var(--color-text-strong)" }}
            >
              팀 일정
            </h2>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <Link
              href="/dashboard/calendar"
              onClick={onClose}
              className="rounded-[6px] px-2 py-1 text-[var(--text-meta)] font-semibold"
              style={{
                border: "1px solid var(--color-divider)",
                background: "var(--color-app-canvas)",
                color: "var(--color-text-body)",
              }}
            >
              달력 전체
            </Link>
            <button
              type="button"
              aria-label="닫기"
              onClick={onClose}
              className="flex h-7 w-7 items-center justify-center rounded-[6px]"
              style={{ color: "var(--color-text-muted)" }}
            >
              <CloseIcon />
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-3.5 py-3">
          {loading ? (
            <p
              className="text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              불러오는 중…
            </p>
          ) : error ? (
            <p
              className="text-[var(--text-body-sm)]"
              style={{ color: "var(--color-fail-fg)" }}
            >
              {error}
            </p>
          ) : grouped.length === 0 ? (
            <p
              className="text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              앞으로 {TEAM_SCHEDULE_WINDOW_DAYS}일 동안 등록된 팀 일정이 없습니다.
            </p>
          ) : (
            <div className="flex flex-col gap-4">
              {grouped.map(([iso, evs]) => (
                <div key={iso}>
                  <div
                    className="mb-1.5 text-[var(--text-meta)] font-extrabold uppercase tracking-[0.02em]"
                    style={{
                      color:
                        iso === todayIso
                          ? "var(--color-text-strong)"
                          : "var(--color-text-muted)",
                    }}
                  >
                    {dayHeading(iso, todayIso, tomorrowIso)}
                  </div>
                  <ul className="flex flex-col gap-1.5">
                    {evs.map((e) => (
                      <li
                        key={`${e.event_id}-${iso}`}
                        className="rounded-[8px] px-2.5 py-2"
                        style={{
                          background: "var(--color-app-canvas)",
                          border: "1px solid var(--color-divider)",
                        }}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <span
                            className="text-[var(--text-body-sm)] font-semibold"
                            style={{ color: "var(--color-text-strong)" }}
                          >
                            {e.title}
                          </span>
                          <span
                            className="shrink-0 font-[family-name:var(--font-mono)] text-[var(--text-meta)]"
                            style={{ color: "var(--color-text-muted)" }}
                          >
                            {eventTimeLabel(e)}
                          </span>
                        </div>
                        <div className="mt-1 flex items-center gap-1.5">
                          {e.member_ids.slice(0, 5).map((id) => {
                            const c = memberColor(id);
                            return c ? (
                              <span
                                key={id}
                                className="inline-block h-2 w-2 shrink-0 rounded-full"
                                style={{ background: c }}
                                aria-hidden
                              />
                            ) : null;
                          })}
                          <span
                            className="truncate text-[var(--text-meta)]"
                            style={{ color: "var(--color-text-muted)" }}
                          >
                            {membersLabel(e.member_ids)}
                          </span>
                          {e.no_return ? (
                            <span
                              className="ml-0.5 shrink-0 rounded-[4px] px-1 py-[1px] text-[9px] font-bold"
                              style={{
                                border: "1px solid var(--color-fail-fg)",
                                color: "var(--color-fail-fg)",
                              }}
                            >
                              복귀불가
                            </span>
                          ) : null}
                        </div>
                        {e.location ? (
                          <div
                            className="mt-0.5 truncate text-[var(--text-meta)]"
                            style={{ color: "var(--color-text-faint)" }}
                          >
                            📍 {e.location}
                          </div>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </div>
      </aside>
    </>
  );
}

function RailButton({
  active,
  children,
  hasNotification,
  label,
  notificationCount,
  onClick,
}: {
  active: boolean;
  children: React.ReactNode;
  hasNotification?: boolean;
  label: string;
  notificationCount?: number | null;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={active || undefined}
      onClick={onClick}
      className="relative flex h-9 w-9 items-center justify-center rounded-[6px] transition-colors"
      style={{
        background: active ? "var(--color-sidebar-active)" : "transparent",
        color: active ? "var(--color-text-strong)" : "var(--color-text-faint)",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = "var(--color-sidebar-active)";
        e.currentTarget.style.color = "var(--color-text-body)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = active
          ? "var(--color-sidebar-active)"
          : "transparent";
        e.currentTarget.style.color = active
          ? "var(--color-text-strong)"
          : "var(--color-text-faint)";
      }}
    >
      {children}
      {notificationCount != null ? (
        <AgentWorkBadge className="absolute right-0 top-0 translate-x-1/4 -translate-y-1/4" compact count={notificationCount} />
      ) : hasNotification ? (
        <span
          aria-hidden
          className="absolute right-[7px] top-[7px] h-1.5 w-1.5 rounded-full"
          style={{ background: "var(--color-attention)" }}
        />
      ) : null}
    </button>
  );
}

function AgentWorkBadge({
  className,
  compact = false,
  count,
}: {
  className?: string;
  compact?: boolean;
  count: number | null;
}) {
  const label = count == null ? "…" : String(count);
  const ariaLabel = count == null ? "에이전트 카운트 로딩 중" : `미처리 에이전트 항목 ${count}개`;

  return (
    <span
      aria-label={ariaLabel}
      className={cx(
        "inline-flex shrink-0 items-center justify-center rounded-[999px] font-[family-name:var(--font-mono)] font-extrabold",
        compact ? "min-h-4 min-w-4 px-1 text-[9px]" : "min-h-5 min-w-5 px-1.5 text-[10px]",
        className,
      )}
      style={{
        background: "var(--color-attention)",
        color: "var(--color-text-strong)",
        lineHeight: 1,
      }}
    >
      {label}
    </span>
  );
}

function isAgentQueueHref(href: string) {
  return splitHrefValue(href).base === "/checklist";
}

function isNotificationsHref(href: string) {
  return splitHrefValue(href).base === "/notifications";
}

function splitHrefValue(href: string): { base: string; query: string } {
  const [base, query = ""] = href.split("?");
  return { base, query };
}

function AuthFooter({ collapsed }: { collapsed: boolean }) {
  const [me, setMe] = useState<AuthMe | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .getAuthMe()
      .then((data) => {
        if (alive) setMe(data);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  async function logout() {
    await api.logout().catch(() => undefined);
    // Purge the owner-scoped offline board cache + queued offline mutations so
    // the next user on this browser never sees the previous owner's data.
    // In-memory auth/SWR 캐시도 같은 소유자 경계로 함께 비운다.
    clearApiSessionCaches();
    clearCachedResources();
    resetPageSaveMemory();
    deactivateOfflineNoteOwner();
    await clearOfflineBoardCache().catch(() => undefined);
    await clearOutbox().catch(() => undefined);
    window.location.replace("/login?error=logged_out");
  }

  return (
    <div className="px-3 py-3">
      <div
        className="rounded-[6px] px-2.5 py-2"
        style={{ background: "var(--color-sidebar-active)" }}
      >
        {me ? (
          <div className="flex items-center gap-2">
            <div className={cx("min-w-0 flex-1", collapsed && "sr-only")}>
              <div
                className="truncate text-[11px] font-semibold"
                style={{ color: "var(--color-text-strong)" }}
              >
                {me.email ?? me.display_name}
              </div>
              <div
                className="mt-0.5 truncate font-[family-name:var(--font-mono)] text-[10px] tracking-[0.04em]"
                style={{ color: "var(--color-text-muted)" }}
              >
                {me.node_id} · {me.role ?? me.auth_mode}
              </div>
            </div>
            {me.auth_mode === "session" ? (
              <button
                type="button"
                title="로그아웃"
                aria-label="로그아웃"
                onClick={() => void logout()}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[5px]"
                style={{ color: "var(--color-text-muted)" }}
              >
                <LogoutIcon />
              </button>
            ) : null}
          </div>
        ) : (
          <Link
            href="/login"
            className="block truncate text-[11px] font-semibold"
            style={{ color: failed ? "var(--color-fail-fg)" : "var(--color-text-strong)" }}
          >
            로그인
          </Link>
        )}
      </div>
    </div>
  );
}

/* -------------------------------- icons -------------------------------- */
/* Line icons, 1.6 stroke, ~18px target — matches DESIGN.md spec. */

function ChevronsLeftIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m11 17-5-5 5-5" />
      <path d="m18 17-5-5 5-5" />
    </svg>
  );
}

function HomeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m3 11 9-8 9 8" />
      <path d="M5 10v10h14V10" />
      <path d="M10 20v-6h4v6" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.2-3.2" />
    </svg>
  );
}

function MailIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="m4 7 8 6 8-6" />
    </svg>
  );
}

function TerminalIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m5 8 4 4-4 4" />
      <path d="M13 16h6" />
    </svg>
  );
}

function FolderTreeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M3 4.5A1.5 1.5 0 0 1 4.5 3h3l2 2.5h5a1.5 1.5 0 0 1 1.5 1.5v2" />
      <rect x="13" y="11" width="8" height="4.5" rx="1" />
      <rect x="13" y="18" width="8" height="3" rx="1" />
      <path d="M9 9v8a1.5 1.5 0 0 0 1.5 1.5H13" />
      <path d="M13 13.25h-2.5" />
    </svg>
  );
}

function BoardIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="3" width="5" height="18" rx="1" />
      <rect x="10" y="3" width="5" height="12" rx="1" />
      <rect x="17" y="3" width="5" height="15" rx="1" />
    </svg>
  );
}

function BellIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M6 9a6 6 0 0 1 12 0c0 5 2 6.5 2 6.5H4S6 14 6 9Z" />
      <path d="M10.3 19.5a2 2 0 0 0 3.4 0" />
    </svg>
  );
}

function NoteIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M5 3h9l5 5v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z" />
      <path d="M14 3v5h5" />
      <path d="M8 13h7M8 17h5" />
    </svg>
  );
}

function ArchiveIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="4" y="4" width="16" height="5" rx="1" />
      <path d="M6 9v9a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V9" />
      <path d="M10 13h4" />
    </svg>
  );
}

function GaugeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="3" width="8" height="8" rx="1.5" />
      <rect x="13" y="3" width="8" height="5" rx="1.5" />
      <rect x="13" y="10" width="8" height="11" rx="1.5" />
      <rect x="3" y="13" width="8" height="8" rx="1.5" />
    </svg>
  );
}

function CalendarIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="4" width="18" height="17" rx="2" />
      <path d="M16 2v4M8 2v4M3 10h18" />
    </svg>
  );
}

function PlugIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 22v-5" />
      <path d="M9 8V2M15 8V2" />
      <path d="M18 8H6v3a6 6 0 0 0 12 0Z" />
    </svg>
  );
}

function TaskIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="4" y="3" width="16" height="18" rx="2" />
      <path d="m8 9 1.5 1.5L12 8" />
      <path d="M14 10h3" />
      <path d="m8 15 1.5 1.5L12 14" />
      <path d="M14 16h3" />
    </svg>
  );
}

function ChecklistIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m3 17 2 2 4-4" />
      <path d="m3 7 2 2 4-4" />
      <path d="M13 6h8" />
      <path d="M13 12h8" />
      <path d="M13 18h8" />
    </svg>
  );
}

function GearIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="3.2" />
      <path d="M19.4 12a7.4 7.4 0 0 0-.1-1.2l2-1.5-2-3.4-2.3.9a7.3 7.3 0 0 0-2-1.2L14.6 2h-4l-.4 2.6a7.3 7.3 0 0 0-2 1.2l-2.3-.9-2 3.4 2 1.5a7.4 7.4 0 0 0 0 2.4l-2 1.5 2 3.4 2.3-.9a7.3 7.3 0 0 0 2 1.2l.4 2.6h4l.4-2.6a7.3 7.3 0 0 0 2-1.2l2.3.9 2-3.4-2-1.5a7.4 7.4 0 0 0 .1-1.2Z" />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M10 5H6.8A1.8 1.8 0 0 0 5 6.8v10.4A1.8 1.8 0 0 0 6.8 19H10" />
      <path d="M15 8l4 4-4 4" />
      <path d="M9 12h10" />
    </svg>
  );
}

function MoreIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="5" cy="12" r="1.4" />
      <circle cx="12" cy="12" r="1.4" />
      <circle cx="19" cy="12" r="1.4" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M18 6 6 18" />
      <path d="m6 6 12 12" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </svg>
  );
}
