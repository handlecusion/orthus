"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  api,
  type AuthConfig,
  type BoardCard,
  type BoardResponse,
} from "@/lib/api";
import { PersonalWorkspaceBoard as OjosamaPersonalWorkspaceBoard } from "./PersonalWorkspaceBoard";
import { suppressHtml5DragOnTextPointerDown } from "@/lib/cardCopy";
import { useGrabPan } from "@/lib/useGrabPan";
import { useCachedResource } from "@/lib/use-cached-resource";
import { useStoredPreference } from "@/lib/use-stored-preference";
import {
  PageHeader,
  Skeleton,
  cx,
} from "@/components/ui";
import {
  TagPill,
  TAG_COLORS,
  type TagColor,
} from "@/components/dashboard/kit";

const COLUMNS = ["Todo", "In Progress", "Review", "Done"] as const;
type Column = (typeof COLUMNS)[number];

// 지원사업 보드와 같은 상태 색 매핑(컬럼 헤더 pill 색).
const COLUMN_COLOR: Record<Column, TagColor> = {
  Todo: "gray",
  "In Progress": "blue",
  Review: "yellow",
  Done: "green",
};

/* ----------------------------- helpers ----------------------------- */

function groupByStatus(cards: BoardCard[]): Record<Column, BoardCard[]> {
  const out: Record<Column, BoardCard[]> = {
    Todo: [],
    "In Progress": [],
    Review: [],
    Done: [],
  };
  for (const c of cards) {
    const col = (COLUMNS as readonly string[]).includes(c.status)
      ? (c.status as Column)
      : "Todo";
    out[col].push(c);
  }
  return out;
}

function formatDue(due: string | null): string | null {
  if (!due) return null;
  try {
    return new Date(due).toLocaleDateString("ko-KR", {
      month: "short",
      day: "numeric",
    });
  } catch {
    return null;
  }
}

function getSyncState(card: BoardCard): {
  label: string;
  tone: "ok" | "warn" | "fail";
} | null {
  if (card.writeback_status === "failed") {
    return { label: "실패", tone: "fail" };
  }
  if (card.writeback_status === "synced") {
    return { label: "동기화", tone: "ok" };
  }
  if (
    card.is_local_override ||
    card.writeback_status === "disabled" ||
    card.writeback_status === "local_only"
  ) {
    return { label: "로컬", tone: "warn" };
  }
  return null;
}

/* ----------------------------- task card ----------------------------- */

function priorityColor(p: string): TagColor {
  const key = p.toLowerCase();
  if (/(urgent|high|급|높|p0|p1)/.test(key)) return "red";
  if (/(medium|보통|중|p2)/.test(key)) return "orange";
  if (/(low|낮|p3)/.test(key)) return "teal";
  return "purple";
}

function TaskCard({
  card,
  onDragStart,
}: {
  card: BoardCard;
  onDragStart: (e: React.DragEvent, card: BoardCard) => void;
}) {
  const due = formatDue(card.due_at);
  const syncState = getSyncState(card);

  return (
    <div
      draggable
      onMouseDown={suppressHtml5DragOnTextPointerDown}
      onDragStart={(e) => onDragStart(e, card)}
      className="relative cursor-grab rounded-[8px] px-2.5 py-2 active:cursor-grabbing"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-divider-soft)",
        boxShadow: "0 1px 4px rgba(0,0,0,0.08)",
      }}
    >
      {/* Local-override attention dot — top-right corner */}
      {card.is_local_override ? (
        <span
          aria-label="로컬 변경"
          className="absolute right-2 top-2 h-2 w-2 rounded-full"
          style={{ background: "var(--color-attention)" }}
        />
      ) : null}

      {/* Title */}
      <div
        className="pr-3 text-[var(--text-body-sm)] font-medium leading-snug"
        style={{ color: "var(--color-text-strong)" }}
        data-card-text=""
      >
        {card.title}
      </div>

      {/* Tags: priority */}
      {card.priority ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1">
          <TagPill label={card.priority} color={priorityColor(card.priority)} />
        </div>
      ) : null}

      {/* Due date */}
      {due ? (
        <div
          className="mt-1.5 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
          data-card-text=""
        >
          마감 {due}
        </div>
      ) : null}

      {/* Assignee */}
      {card.assignee ? (
        <div
          className="mt-1.5 flex items-center gap-1.5 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-faint)" }}
        >
          담당
          <span className="inline-flex items-center gap-1">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ background: "var(--channel-slate, var(--color-text-faint))" }}
            />
            <span style={{ color: "var(--color-text-muted)" }}>{card.assignee}</span>
          </span>
        </div>
      ) : null}

      {syncState ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <SyncChip tone={syncState.tone}>{syncState.label}</SyncChip>
          {card.writeback_status === "failed" && card.writeback_error ? (
            <span
              className="text-[var(--text-meta)]"
              style={{ color: "var(--color-fail-fg)" }}
            >
              {card.writeback_error}
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function SyncChip({
  tone,
  children,
}: {
  tone: "ok" | "warn" | "fail";
  children: React.ReactNode;
}) {
  const color =
    tone === "ok"
      ? "var(--color-pass-fg)"
      : tone === "fail"
        ? "var(--color-fail-fg)"
        : "var(--color-attention)";

  return (
    <span
      className="inline-flex items-center gap-1 rounded-[4px] px-1.5 py-[1px] text-[var(--text-meta)] font-semibold"
      style={{
        color,
        border: `1px solid color-mix(in srgb, ${color} 30%, transparent)`,
        background: `color-mix(in srgb, ${color} 10%, transparent)`,
      }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: color }}
      />
      {children}
    </span>
  );
}

/* ----------------------------- column ----------------------------- */

function BoardColumn({
  title,
  cards,
  activeMobile,
  onDragStart,
  onDrop,
  onDragOver,
  onDragLeave,
  isDragOver,
}: {
  title: Column;
  cards: BoardCard[];
  activeMobile: boolean;
  onDragStart: (e: React.DragEvent, card: BoardCard) => void;
  onDrop: (e: React.DragEvent, col: Column) => void;
  onDragOver: (e: React.DragEvent, col: Column) => void;
  onDragLeave: () => void;
  isDragOver: boolean;
}) {
  const c = TAG_COLORS[COLUMN_COLOR[title]];
  return (
    <div
      className={cx(
        "company-board-column flex shrink-0 flex-col rounded-[10px] transition-colors",
        activeMobile && "is-active-mobile",
      )}
      style={{
        width: "var(--day-column-width)",
        maxHeight: "68vh",
        background: isDragOver ? "var(--color-sidebar)" : "var(--color-panel)",
        border: "1px solid var(--color-divider-soft)",
      }}
      onDragOver={(e) => onDragOver(e, title)}
      onDragLeave={onDragLeave}
      onDrop={(e) => onDrop(e, title)}
    >
      {/* Column header — status pill + count (지원사업 스타일) */}
      <div className="flex items-center gap-1.5 px-2.5 pb-1 pt-2.5">
        <span
          className="inline-flex items-center rounded-[4px] px-2 py-[2px] text-[var(--text-meta)] font-semibold whitespace-nowrap"
          style={{ background: c.bg, color: c.fg }}
        >
          {title}
        </span>
        <span
          className="text-[var(--text-meta)]"
          style={{ color: "var(--color-text-faint)" }}
        >
          {cards.length}
        </span>
      </div>

      {/* Cards */}
      <div className="flex min-h-[40px] flex-col gap-1.5 overflow-y-auto px-2 pb-2">
        {cards.length === 0 ? (
          <div
            className="px-1 py-1.5 text-[var(--text-meta)]"
            style={{ color: "var(--color-text-faint)" }}
          >
            비어 있음
          </div>
        ) : (
          cards.map((card) => (
            <TaskCard key={String(card.row_id)} card={card} onDragStart={onDragStart} />
          ))
        )}
      </div>
    </div>
  );
}

/* ----------------------------- skeleton ----------------------------- */

function BoardSkeleton() {
  return (
    <div className="flex gap-4 p-5">
      {COLUMNS.map((col) => (
        <div
          key={col}
          className="flex shrink-0 flex-col gap-2"
          style={{ width: "var(--day-column-width)" }}
        >
          <Skeleton className="h-4 w-24" />
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-[62px] w-full" />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ----------------------------- page ----------------------------- */

type CentralBoardView = "company" | "personal";
const CENTRAL_BOARD_STORAGE_KEY = "orthus.board.view";
const CENTRAL_BOARD_VIEWS = ["company", "personal"] as const;

export default function BoardPage() {
  // auth config 게이트 — 최초 로드 이후에는 SWR 캐시(+api.ts 모듈 캐시)가 동기로
  // 풀려 스켈레톤 없이 바로 보드를 그린다. 진짜 첫 로드만 스켈레톤.
  const configRes = useCachedResource<AuthConfig>(
    "auth:config",
    () => api.getAuthConfig(),
    { ttlMs: 300_000 },
  );
  const config = configRes.data;
  const loadingConfig = configRes.loading;
  // P8.5: on company node + ORTHUS_OWNER_SCOPE_ENABLED we offer a 회사/내 보드 toggle.
  // A `?view=` param (e.g. the "내 보드" nav entry → /board?view=personal) wins over
  // the persisted choice so the link lands on the intended board.
  const [centralView, applyCentralView] = useStoredPreference<CentralBoardView>(
    CENTRAL_BOARD_STORAGE_KEY,
    CENTRAL_BOARD_VIEWS,
    "company",
    { paramKey: "view" },
  );

  const ownerScopeEnabled = config?.owner_scope_enabled === true;
  const showCentralToggle = config?.node_kind === "company" && ownerScopeEnabled;

  if (loadingConfig) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <PageHeader title="보드" subtitle="workspace loading" />
        <BoardSkeleton />
      </div>
    );
  }

  if (config?.node_kind === "personal") {
    return <OjosamaPersonalWorkspaceBoard />;
  }

  // 회사 보드 임시 숨김(2026-06-27): company 노드에서도 회사 보드(Nova 로드맵)와
  // "회사 보드 / 내 보드" 토글을 노출하지 않고 내 보드만 보여준다. /board = 내 보드.
  // 되돌리려면 HIDE_COMPANY_BOARD 를 false 로 바꾸면 토글이 그대로 복구된다.
  const HIDE_COMPANY_BOARD = true as boolean;

  if (showCentralToggle) {
    if (HIDE_COMPANY_BOARD) {
      return <OjosamaPersonalWorkspaceBoard />;
    }
    return (
      <CentralBoardWithToggle
        onViewChange={applyCentralView}
        view={centralView}
      />
    );
  }

  return <CompanyNovaBoard />;
}

/* ----------------------------- central toggle ---------------------------- */
/**
 * P8.5: company-node operator chooses between the Nova roadmap board and their
 * own personal workspace board. Toggle persists in localStorage. The selector
 * sits above the board content and is 44px tall to match the mobile parity
 * contract; personal board internals already render at any viewport.
 */
function CentralBoardWithToggle({
  onViewChange,
  view,
}: {
  onViewChange: (next: CentralBoardView) => void;
  view: CentralBoardView;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        className="shrink-0 px-3 py-2 sm:px-5"
        style={{
          background: "var(--color-app-canvas)",
          borderBottom: "1px solid var(--color-divider)",
        }}
      >
        <CentralBoardToggle onChange={onViewChange} value={view} />
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        {view === "personal" ? (
          <OjosamaPersonalWorkspaceBoard />
        ) : (
          <CompanyNovaBoard />
        )}
      </div>
    </div>
  );
}

function CentralBoardToggle({
  onChange,
  value,
}: {
  onChange: (next: CentralBoardView) => void;
  value: CentralBoardView;
}) {
  return (
    <div
      aria-label="보드 선택"
      className="inline-flex h-11 min-h-11 items-stretch overflow-hidden rounded-[var(--radius-toolbar)]"
      role="group"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        boxShadow: "var(--shadow-toolbar)",
      }}
    >
      <CentralBoardToggleOption
        active={value === "company"}
        label="회사 보드"
        onClick={() => onChange("company")}
        title="Nova 개발 로드맵 보드를 봅니다."
      />
      <span
        aria-hidden
        className="my-1 w-px"
        style={{ background: "var(--color-toolbar-border)" }}
      />
      <CentralBoardToggleOption
        active={value === "personal"}
        label="내 보드"
        onClick={() => onChange("personal")}
        title="내 개인 워크스페이스 보드를 봅니다."
      />
    </div>
  );
}

function CentralBoardToggleOption({
  active,
  label,
  onClick,
  title,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      aria-pressed={active}
      className="inline-flex min-h-11 items-center px-3 text-[var(--text-body-sm)] transition-colors"
      onClick={onClick}
      style={{
        background: active ? "var(--color-sidebar-active)" : "transparent",
        color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
        fontWeight: active ? 600 : 500,
      }}
      title={title}
      type="button"
    >
      {label}
    </button>
  );
}

function CompanyNovaBoard() {
  const [data, setData] = useState<BoardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dragOverCol, setDragOverCol] = useState<Column | null>(null);
  const [mobileColumn, setMobileColumn] = useState<Column>("Todo");
  const draggingCard = useRef<BoardCard | null>(null);
  // 빈 공간을 잡고 끌어서 좌우 스크롤 (내 보드와 동일)
  const scrollRef = useRef<HTMLDivElement | null>(null);
  useGrabPan(scrollRef);

  useEffect(() => {
    let cancelled = false;

    async function loadInitial() {
      try {
        const board = await api.listBoard();
        if (cancelled) return;
        setData(board);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "보드를 불러오지 못했습니다.");
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void loadInitial();

    return () => {
      cancelled = true;
    };
  }, []);

  const handleDragStart = useCallback(
    (e: React.DragEvent, card: BoardCard) => {
      draggingCard.current = card;
      e.dataTransfer.effectAllowed = "move";
    },
    [],
  );

  const handleDragOver = useCallback(
    (e: React.DragEvent, col: Column) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      setDragOverCol(col);
    },
    [],
  );

  const handleDragLeave = useCallback(() => {
    setDragOverCol(null);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent, col: Column) => {
      e.preventDefault();
      setDragOverCol(null);
      const card = draggingCard.current;
      draggingCard.current = null;
      if (!card || card.status === col) return;

      // Optimistic update
      setData((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          cards: prev.cards.map((c) =>
            c.row_id === card.row_id
              ? {
                  ...c,
                  status: col,
                  is_local_override: true,
                  writeback_status: "local_only",
                  writeback_error: null,
                }
              : c,
          ),
        };
      });

      // Persist
      api
        .setBoardStatus(card.row_id, col)
        .then((updated) => {
          setData((prev) => {
            if (!prev) return prev;
            return {
              ...prev,
              cards: prev.cards.map((c) =>
                c.row_id === card.row_id ? updated : c,
              ),
            };
          });
        })
        .catch(() => {
          // Revert on transport/server error; Notion write-back failures return a card.
          setData((prev) => {
            if (!prev) return prev;
            return {
              ...prev,
              cards: prev.cards.map((c) =>
                c.row_id === card.row_id ? card : c,
              ),
            };
          });
        });
    },
    [],
  );

  const cards = data?.cards ?? [];
  const overrideCount = cards.filter((c) => c.is_local_override).length;
  const failedCount = cards.filter((c) => c.writeback_status === "failed").length;
  const grouped = groupByStatus(cards);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="보드 — Nova 개발 로드맵"
        subtitle={
          loading
            ? "불러오는 중…"
            : `${cards.length}개 티켓${overrideCount > 0 ? ` · 로컬 변경 ${overrideCount}개` : ""}${
                failedCount > 0 ? ` · 동기화 실패 ${failedCount}개` : ""
              }`
        }
      />

      {loading ? (
        <BoardSkeleton />
      ) : error ? (
        <div className="p-5">
          <div
            className="rounded-[5px] px-3 py-2.5 text-[var(--text-body-sm)]"
            style={{
              background: "var(--color-fail-bg)",
              color: "var(--color-fail-fg)",
              border: "1px solid color-mix(in srgb, var(--color-fail-fg) 22%, white)",
            }}
          >
            <div className="font-semibold">불러오기 실패</div>
            <div className="mt-0.5 opacity-90">{error}</div>
          </div>
        </div>
      ) : (
        <>
        <nav className="company-board-mobile-tabs" aria-label="보드 상태">
          {COLUMNS.map((col) => (
            <button
              aria-pressed={mobileColumn === col}
              className={mobileColumn === col ? "active" : ""}
              key={col}
              onClick={() => setMobileColumn(col)}
              type="button"
            >
              <span>{col}</span>
              <span>{grouped[col].length}</span>
            </button>
          ))}
        </nav>
        <div
          ref={scrollRef}
          className={cx(
            "company-board-scroll flex min-h-0 flex-1 items-start gap-3 overflow-x-auto overflow-y-auto p-5",
          )}
        >
          {COLUMNS.map((col) => (
            <BoardColumn
              key={col}
              title={col}
              cards={grouped[col]}
              activeMobile={mobileColumn === col}
              onDragStart={handleDragStart}
              onDrop={handleDrop}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              isDragOver={dragOverCol === col}
            />
          ))}
        </div>
        </>
      )}
    </div>
  );
}
