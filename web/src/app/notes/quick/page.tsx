"use client";
/* eslint-disable react-hooks/set-state-in-effect -- owner-scoped cache is intentionally applied in a layout effect before first paint. */

/* Apple Notes-style quick window: the local list/chrome paints first, the heavy
 * BlockNote editor loads behind it, and every personal write is durable before
 * it reaches the network. */

import dynamic from "next/dynamic";
import {
  ExternalLink,
  FileText,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Share2,
  SquarePen,
} from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type DashboardPageSummary } from "@/lib/api";
import { NoteMetaPanel } from "@/components/notes/NoteMetaPanel";
import { ensureNoteMetaSyncWiring, flushNoteMetaOutbox } from "@/lib/note-meta-sync";
import {
  createOfflineNote,
  dismissTerminalPageSaveErrors,
  ensureOfflineNoteCreate,
  flushPageSaves,
  getConfirmedBody,
  getPageSaveState,
  purgePageSave,
  readCachedPageBody,
  readPageDraft,
  recoverOfflineNoteSaves,
  subscribePageSaveState,
  type PageSaveState,
} from "@/lib/pageSave";
import {
  getOfflineNoteOwner,
  isOfflineNoteCreate,
  isOfflineNoteDelete,
  migrateLegacyOwnedNoteCaches,
  readOfflineNoteOutbox,
  scopedNoteStorageKey,
  setNotesOpenTarget,
} from "@/lib/offline-notes";

const QuickNoteEditor = dynamic(
  () => import("@/components/dashboard/pageEditor").then((module) => module.PageStack),
  { ssr: false, loading: () => <EditorSkeleton /> },
);

type QuickNoteRow = DashboardPageSummary;
type ListCache = { rows: QuickNoteRow[]; currentId: string | null };

function listCacheKey(): string | null {
  return scopedNoteStorageKey("quick-note-list");
}

function validRow(value: unknown): value is QuickNoteRow {
  if (!value || typeof value !== "object") return false;
  const row = value as Partial<QuickNoteRow>;
  return typeof row.page_id === "string" && typeof row.title === "string";
}

function readListCache(): ListCache | null {
  if (typeof window === "undefined") return null;
  const key = listCacheKey();
  if (!key) return null;
  const owner = getOfflineNoteOwner();
  if (!owner) return null;
  try {
    const parsed = JSON.parse(localStorage.getItem(key) ?? "null") as Partial<ListCache> | null;
    if (!parsed || !Array.isArray(parsed.rows)) return null;
    return {
      rows: parsed.rows.filter(
        (row): row is QuickNoteRow =>
          validRow(row) && row.owner_id === owner.userId && !isOfflineNoteDelete(row.page_id),
      ),
      currentId: typeof parsed.currentId === "string" ? parsed.currentId : null,
    };
  } catch {
    return null;
  }
}

function writeListCache(rows: QuickNoteRow[], currentId: string | null): void {
  const key = listCacheKey();
  const owner = getOfflineNoteOwner();
  if (!key || !owner) return;
  try {
    // Empty is meaningful: persist it so a deleted last note never revives on
    // the next launch.  No 100-row truncation — the sidebar is the full list.
    const ownedRows = rows.filter((row) => row.owner_id === owner.userId);
    const ownedCurrent = ownedRows.some((row) => row.page_id === currentId) ? currentId : null;
    localStorage.setItem(
      key,
      JSON.stringify({ rows: ownedRows, currentId: ownedCurrent } satisfies ListCache),
    );
  } catch {
    // A storage quota failure degrades to online list loading.
  }
}

function toMillis(value: string | null): number {
  if (!value) return 0;
  const n = new Date(value).getTime();
  return Number.isFinite(n) ? n : 0;
}

function sortRows(rows: QuickNoteRow[]): QuickNoteRow[] {
  return [...rows].sort((a, b) => toMillis(b.updated_at) - toMillis(a.updated_at));
}

function mergeServerRows(server: QuickNoteRow[], local: QuickNoteRow[]): QuickNoteRow[] {
  const byId = new Map(
    server.filter((row) => !isOfflineNoteDelete(row.page_id)).map((row) => [row.page_id, row]),
  );
  for (const row of local) {
    if (
      !isOfflineNoteDelete(row.page_id) &&
      isOfflineNoteCreate(row.page_id) &&
      !byId.has(row.page_id)
    ) {
      byId.set(row.page_id, row);
    }
  }
  return sortRows([...byId.values()]);
}

function pad(n: number): string {
  return `${n}`.padStart(2, "0");
}

function listTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return `${date.getMonth() + 1}월 ${date.getDate()}일`;
}

function bodyText(body: string): string {
  if (!body.trim()) return "내용 없음";
  try {
    const blocks = JSON.parse(body) as unknown[];
    if (!Array.isArray(blocks)) return body.trim().replace(/\s+/g, " ").slice(0, 70);
    let text = "";
    const visit = (value: unknown) => {
      if (!value || text.length >= 90) return;
      if (Array.isArray(value)) {
        for (const item of value) visit(item);
        return;
      }
      if (typeof value !== "object") return;
      const object = value as { text?: unknown; content?: unknown; children?: unknown };
      if (typeof object.text === "string") text += ` ${object.text}`;
      visit(object.content);
      visit(object.children);
    };
    visit(blocks);
    return text.trim().replace(/\s+/g, " ").slice(0, 70) || "내용 없음";
  } catch {
    return body.trim().replace(/\s+/g, " ").slice(0, 70) || "내용 없음";
  }
}

function notePreview(pageId: string, fallback?: string | null): string {
  const body = readPageDraft(pageId)?.body ?? readCachedPageBody(pageId);
  return body === null || body === undefined ? fallback || "내용 없음" : bodyText(body);
}

function groupLabel(value: string | null): "오늘" | "이전 7일" | "이전" {
  const time = toMillis(value);
  if (!time) return "이전";
  const date = new Date(time);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return "오늘";
  const sevenDaysAgo = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7).getTime();
  return time >= sevenDaysAgo ? "이전 7일" : "이전";
}

function EditorSkeleton() {
  return (
    <div className="px-8 py-7" aria-label="편집기 불러오는 중">
      <div className="h-8 w-2/5 animate-pulse rounded-md bg-[var(--color-divider-soft)]" />
      <div className="mt-7 h-px bg-[var(--color-divider-soft)]" />
      <div className="mt-7 space-y-3">
        <div className="h-3 w-4/5 animate-pulse rounded bg-[var(--color-divider-soft)]" />
        <div className="h-3 w-3/5 animate-pulse rounded bg-[var(--color-divider-soft)]" />
        <div className="h-3 w-2/3 animate-pulse rounded bg-[var(--color-divider-soft)]" />
      </div>
    </div>
  );
}

export default function QuickNotePage() {
  const [notes, setNotes] = useState<QuickNoteRow[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [seedTitle, setSeedTitle] = useState("새 메모");
  const [query, setQuery] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [metaOpen, setMetaOpen] = useState(false);
  const [online, setOnline] = useState(true);
  const [save, setSave] = useState<{ state: PageSaveState; error: string | null }>({
    state: "saved",
    error: null,
  });
  const [refreshTick, setRefreshTick] = useState(0);
  const [knownUpdatedAt, setKnownUpdatedAt] = useState<Record<string, string | null>>({});

  const currentIdRef = useRef<string | null>(null);
  const lastListRefreshAt = useRef(0);
  const autoCreated = useRef(false);
  const busyRef = useRef(false);
  const ensuredLocalCreates = useRef(new Set<string>());

  useEffect(() => {
    currentIdRef.current = currentId;
  }, [currentId]);

  useLayoutEffect(() => {
    const cached = readListCache();
    if (!cached) return;
    const rows = sortRows(cached.rows);
    const selected =
      cached.currentId && rows.some((row) => row.page_id === cached.currentId)
        ? cached.currentId
        : rows[0]?.page_id ?? null;
    currentIdRef.current = selected;
    setKnownUpdatedAt(Object.fromEntries(rows.map((row) => [row.page_id, row.updated_at])));
    // Cache paint happens before the first browser paint; network reconciliation
    // is deliberately deferred to the effects below.
    setNotes(rows);
    setCurrentId(selected);
    setSeedTitle(rows.find((row) => row.page_id === selected)?.title ?? "새 메모");
    setLoaded(true);
  }, []);

  useEffect(() => {
    ensureNoteMetaSyncWiring();
    const initial = window.setTimeout(() => {
      setOnline(navigator.onLine);
    }, 0);
    const onOnline = () => setOnline(true);
    const onOffline = () => setOnline(false);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      window.clearTimeout(initial);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, []);

  useEffect(() => subscribePageSaveState(() => setSave(getPageSaveState())), []);

  useEffect(() => {
    const activeOwner = getOfflineNoteOwner();
    if (!activeOwner) return;
    for (const row of notes) {
      if (
        row.owner_id !== activeOwner.userId ||
        !isOfflineNoteCreate(row.page_id) ||
        ensuredLocalCreates.current.has(row.page_id)
      ) {
        continue;
      }
      ensuredLocalCreates.current.add(row.page_id);
      const body = readPageDraft(row.page_id)?.body ?? readCachedPageBody(row.page_id) ?? "";
      void ensureOfflineNoteCreate({
        page_id: row.page_id,
        title: row.title,
        icon: row.icon,
        body,
        kind: "memo",
      }, "recover");
    }
  }, [notes]);

  useEffect(() => {
    if (!loaded) return;
    writeListCache(notes, currentId);
  }, [notes, currentId, loaded]);

  const selectNote = useCallback((row: QuickNoteRow) => {
    setCurrentId(row.page_id);
    setSeedTitle(row.title);
    setKnownUpdatedAt((previous) => ({ ...previous, [row.page_id]: row.updated_at }));
    setError(null);
  }, []);

  const newNote = useCallback(async () => {
    if (busyRef.current) return;
    const owner = getOfflineNoteOwner();
    if (!owner) {
      setError("로그인 정보를 확인한 뒤 다시 시도해 주세요.");
      return;
    }
    busyRef.current = true;
    setBusy(true);
    try {
      const created = await createOfflineNote({ title: "새 메모", icon: null, body: "" });
      const now = new Date().toISOString();
      const row: QuickNoteRow = {
        page_id: created.page_id,
        title: created.title,
        icon: created.icon,
        kind: "memo",
        updated_at: now,
        owner_id: owner.userId,
      };
      setNotes((previous) => sortRows([row, ...previous]));
      selectNote(row);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "로컬 메모를 만들지 못했습니다.");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, [selectNote]);

  const refreshOnShow = useCallback(
    async (force = false) => {
      const now = Date.now();
      if (!force && now - lastListRefreshAt.current < 4000) return;
      lastListRefreshAt.current = now;
      let rows: QuickNoteRow[];
      try {
        rows = await api.listDashboardPages("memo", "mine", true);
      } catch {
        setOnline(false);
        setLoaded(true);
        return;
      }
      setOnline(true);
      migrateLegacyOwnedNoteCaches(rows);
      const visibleRows = rows.filter((candidate) => !isOfflineNoteDelete(candidate.page_id));
      setNotes((previous) => mergeServerRows(rows, previous));
      setError(null);
      setLoaded(true);
      const current = currentIdRef.current;
      if (!current) {
        const first = visibleRows[0];
        if (first) selectNote(first);
        return;
      }
      const row = visibleRows.find((candidate) => candidate.page_id === current);
      if (!row) {
        if (isOfflineNoteCreate(current)) return;
        try {
          await purgePageSave(current);
        } catch {
          setError("이 Mac의 저장 큐를 정리하지 못했습니다.");
          return;
        }
        const first = visibleRows[0];
        if (first) selectNote(first);
        else setCurrentId(null);
        return;
      }
      const known = knownUpdatedAt[current];
      if (!row.updated_at || row.updated_at === known) return;
      if (getPageSaveState().state !== "saved") return;
      try {
        const remote = await api.getDashboardPage(current);
        if (currentIdRef.current !== current) return;
        setKnownUpdatedAt((previous) => ({ ...previous, [current]: row.updated_at }));
        if ((remote.body ?? "") !== (getConfirmedBody(current) ?? "")) {
          setSeedTitle(row.title);
          setRefreshTick((value) => value + 1);
        }
      } catch {
        // The cached editor remains usable; retry on the next focus/online event.
      }
    },
    [knownUpdatedAt, selectNote],
  );

  useEffect(() => {
    let alive = true;
    void readOfflineNoteOutbox().then((records) => {
      if (!alive) return;
      const activeOwner = getOfflineNoteOwner();
      if (!activeOwner) return;
      const deletedIds = new Set(
        records.filter((record) => record.op === "delete").map((record) => record.pageId),
      );
      if (deletedIds.size > 0) {
        setNotes((previous) => previous.filter((row) => !deletedIds.has(row.page_id)));
        const selected = currentIdRef.current;
        if (selected && deletedIds.has(selected)) {
          currentIdRef.current = null;
          setCurrentId(null);
        }
      }
      const localRows: QuickNoteRow[] = records
        .filter((record) => record.op === "create")
        .map((record) => ({
          page_id: record.pageId,
          title: record.op === "create" ? record.create.title : "새 메모",
          icon: record.op === "create" ? record.create.icon : null,
          kind: "memo",
          updated_at: new Date(record.updatedAt).toISOString(),
          owner_id: activeOwner.userId,
          preview: record.op === "create" ? bodyText(record.create.body) : null,
        }));
      if (localRows.length > 0) {
        setNotes((previous) => {
          const byId = new Map(previous.map((row) => [row.page_id, row]));
          for (const row of localRows) byId.set(row.page_id, row);
          return sortRows([...byId.values()]);
        });
        setLoaded(true);
      }
    });
    void recoverOfflineNoteSaves().then(() => {
      if (alive) setSave(getPageSaveState());
    });
    api
      .listDashboardPages("memo", "mine", true)
      .then((rows) => {
        if (!alive) return;
        setOnline(true);
        migrateLegacyOwnedNoteCaches(rows);
        const visibleRows = rows.filter((candidate) => !isOfflineNoteDelete(candidate.page_id));
        setNotes((previous) => mergeServerRows(rows, previous));
        setLoaded(true);
        const selected = currentIdRef.current;
        if (!selected && visibleRows.length > 0) selectNote(visibleRows[0]);
        else if (!selected && visibleRows.length === 0 && !autoCreated.current) {
          autoCreated.current = true;
          void newNote();
        }
      })
      .catch(() => {
        if (!alive) return;
        setOnline(false);
        setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [newNote, selectNote]);

  useEffect(() => {
    if (!loaded || currentId || notes.length === 0) return;
    selectNote(notes[0]);
  }, [currentId, loaded, notes, selectNote]);

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") void refreshOnShow();
    };
    const onFocus = () => void refreshOnShow();
    const onOnline = () => {
      flushPageSaves();
      void flushNoteMetaOutbox();
      void refreshOnShow(true);
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onFocus);
    window.addEventListener("online", onOnline);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("online", onOnline);
    };
  }, [refreshOnShow]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (
        (event.metaKey || event.ctrlKey) &&
        !event.shiftKey &&
        !event.altKey &&
        event.key.toLowerCase() === "n"
      ) {
        event.preventDefault();
        void newNote();
      } else if (
        (event.metaKey || event.ctrlKey) &&
        event.shiftKey &&
        event.key.toLowerCase() === "l"
      ) {
        event.preventDefault();
        setSidebarOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [newNote]);

  const current = currentId ? notes.find((row) => row.page_id === currentId) ?? null : null;
  const owner = getOfflineNoteOwner();
  const canPersistCurrent = Boolean(current && owner && current.owner_id === owner.userId);
  const hasOfflineBody = Boolean(
    current &&
      (readPageDraft(current.page_id) !== null || readCachedPageBody(current.page_id) !== null),
  );
  const normalizedQuery = query.trim().toLocaleLowerCase("ko");
  const filtered = normalizedQuery
    ? notes.filter((row) => {
        const preview = notePreview(row.page_id, row.preview);
        return `${row.title} ${preview}`.toLocaleLowerCase("ko").includes(normalizedQuery);
      })
    : notes;
  const groupOrder = ["오늘", "이전 7일", "이전"] as const;
  const groups = groupOrder
    .map((label) => ({ label, rows: filtered.filter((row) => groupLabel(row.updated_at) === label) }))
    .filter((group) => group.rows.length > 0);

  const fullNotesHref = currentId
    ? `/notes?note=${encodeURIComponent(currentId)}`
    : "/notes";

  return (
    <div
      className="flex w-screen overflow-hidden bg-[var(--color-card)] text-[var(--color-text-body)]"
      style={{ height: "100dvh" }}
    >
      {sidebarOpen ? (
        <aside
          className="flex w-[232px] min-w-[190px] shrink-0 flex-col border-r border-[var(--color-divider)] bg-[var(--color-sidebar)] max-[560px]:w-[44vw] max-[560px]:min-w-[160px]"
          aria-label="메모 목록"
        >
          <div className="flex h-12 shrink-0 items-center gap-2 px-3">
            <div className="min-w-0 flex-1">
              <p className="truncate text-[14px] font-bold text-[var(--color-text-strong)]">메모</p>
              <p className="text-[10px] tabular-nums text-[var(--color-text-muted)]">
                {notes.length.toLocaleString()}개
              </p>
            </div>
            <button
              type="button"
              onClick={() => void newNote()}
              disabled={busy}
              className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-[var(--color-accent)] transition-colors hover:bg-[var(--color-sidebar-active)] disabled:opacity-40"
              title="새 메모 (⌘N)"
              aria-label="새 메모"
            >
              <SquarePen size={17} strokeWidth={1.9} />
            </button>
          </div>

          <div className="px-2 pb-2">
            <label className="flex h-8 items-center gap-1.5 rounded-lg border border-[var(--color-divider)] bg-[var(--color-card)] px-2 shadow-[0_1px_1px_rgba(0,0,0,0.03)]">
              <Search size={13} className="shrink-0 text-[var(--color-text-muted)]" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="검색"
                className="min-w-0 flex-1 bg-transparent text-[12px] text-[var(--color-text-strong)] outline-none placeholder:text-[var(--color-text-faint)]"
              />
            </label>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-1.5 pb-3">
            {!loaded ? (
              <div className="space-y-2 px-1 pt-2">
                {[0, 1, 2, 3, 4].map((item) => (
                  <div key={item} className="h-[58px] animate-pulse rounded-lg bg-[var(--color-divider-soft)]" />
                ))}
              </div>
            ) : groups.length === 0 ? (
              <div className="px-3 py-10 text-center text-[12px] text-[var(--color-text-muted)]">
                {query ? "검색 결과가 없습니다" : "새 메모를 만들어 바로 적어보세요"}
              </div>
            ) : (
              groups.map((group) => (
                <section key={group.label} className="mb-3" aria-labelledby={`group-${group.label}`}>
                  <h2
                    id={`group-${group.label}`}
                    className="px-2 pb-1 pt-1 text-[11px] font-bold text-[var(--color-text-body)]"
                  >
                    {group.label}
                  </h2>
                  <div className="space-y-0.5">
                    {group.rows.map((row) => {
                      const selected = row.page_id === currentId;
                      return (
                        <button
                          key={row.page_id}
                          type="button"
                          onClick={() => selectNote(row)}
                          aria-current={selected ? "page" : undefined}
                          className="block min-h-[62px] w-full touch-manipulation rounded-lg px-2.5 py-2 text-left transition-colors"
                          style={{
                            background: selected ? "var(--color-accent-soft)" : "transparent",
                            color: "var(--color-text-strong)",
                            contentVisibility: "auto",
                            containIntrinsicSize: "62px",
                          }}
                        >
                          <span className="flex min-w-0 items-baseline gap-2">
                            <span className="min-w-0 flex-1 truncate text-[12.5px] font-bold">
                              {row.title || "제목 없음"}
                            </span>
                            <span className="shrink-0 text-[10px] tabular-nums text-[var(--color-text-muted)]">
                              {listTime(row.updated_at)}
                            </span>
                          </span>
                          <span className="mt-1 block truncate text-[11px] leading-4 text-[var(--color-text-muted)]">
                            {notePreview(row.page_id, row.preview)}
                          </span>
                          {isOfflineNoteCreate(row.page_id) ? (
                            <span className="mt-0.5 block text-[9px] font-semibold text-[var(--color-warn-fg)]">
                              이 Mac에 저장됨
                            </span>
                          ) : null}
                        </button>
                      );
                    })}
                  </div>
                </section>
              ))
            )}
          </div>
        </aside>
      ) : null}

      <main className="flex min-w-0 flex-1 flex-col bg-[var(--color-card)]">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-[var(--color-divider-soft)] px-2.5">
          <button
            type="button"
            onClick={() => setSidebarOpen((value) => !value)}
            className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-toolbar-hover)]"
            title={sidebarOpen ? "목록 숨기기 (⇧⌘L)" : "목록 보기 (⇧⌘L)"}
            aria-label={sidebarOpen ? "메모 목록 숨기기" : "메모 목록 보기"}
          >
            {sidebarOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
          </button>

          <div className="min-w-0 flex-1 text-center">
            <p className="truncate text-[12px] font-semibold text-[var(--color-text-strong)]">
              {current?.title || "빠른 메모"}
            </p>
            <p className="text-[9px] text-[var(--color-text-faint)]">
              {!online
                ? "오프라인"
                : save.state === "saving"
                  ? "저장 중…"
                  : save.state === "local"
                    ? "이 Mac에 저장됨 · 동기화 대기"
                    : save.state === "error"
                      ? "동기화 확인 필요"
                      : "동기화됨"}
            </p>
          </div>

          {canPersistCurrent ? (
            <button
              type="button"
              onClick={() => setMetaOpen((value) => !value)}
              aria-pressed={metaOpen}
              className="grid h-8 w-8 shrink-0 touch-manipulation place-items-center rounded-lg transition-colors hover:bg-[var(--color-toolbar-hover)]"
              style={{
                color: metaOpen ? "var(--color-accent)" : "var(--color-text-muted)",
                background: metaOpen ? "var(--color-accent-soft)" : "transparent",
              }}
              title="공유 · 프로젝트"
              aria-label="공유 · 프로젝트 설정"
            >
              <Share2 size={15} />
            </button>
          ) : null}

          <a
            href={fullNotesHref}
            onClick={() => {
              // Owner-scoped handoff so the full app opens exactly this note; the
              // main + quick webviews share storage, so no native change is needed.
              if (currentId) setNotesOpenTarget(currentId);
            }}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-8 shrink-0 touch-manipulation items-center gap-1 rounded-lg px-2 text-[11px] font-semibold text-[var(--color-text-muted)] transition-colors hover:bg-[var(--color-toolbar-hover)] hover:text-[var(--color-text-strong)]"
            title="전체 메모 열기"
          >
            전체
            <ExternalLink size={12} />
          </a>
        </header>

        {error || save.state === "error" ? (
          <div
            className="flex shrink-0 items-center gap-2 border-b border-[var(--color-divider-soft)] px-3 py-2 text-[11px]"
            style={{ background: "var(--color-warn-bg)", color: "var(--color-warn-fg)" }}
          >
            <span className="min-w-0 flex-1 truncate">{error ?? save.error ?? "동기화를 확인해 주세요"}</span>
            <button
              type="button"
              onClick={() => {
                flushPageSaves();
                dismissTerminalPageSaveErrors();
              }}
              className="shrink-0 rounded-md px-2 py-1 font-bold hover:bg-black/5"
            >
              다시 시도
            </button>
          </div>
        ) : null}

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-4 max-[560px]:px-3">
          {metaOpen && current && canPersistCurrent ? (
            <div className="mx-auto max-w-[680px]">
              <NoteMetaPanel key={current.page_id} pageId={current.page_id} canWrite />
            </div>
          ) : null}
          {current ? (
            canPersistCurrent && (online || hasOfflineBody) ? (
              <QuickNoteEditor
                key={`${current.page_id}:${refreshTick}`}
                rootPageId={current.page_id}
                rootSeed={{ title: seedTitle, icon: current.icon }}
                minHeight={420}
                autoFocus
                hideRootIcon
                draftPersistence
                autoTitleFromBody
                onRootTitle={(title) => {
                  const now = new Date().toISOString();
                  setNotes((previous) =>
                    sortRows(
                      previous.map((row) =>
                        row.page_id === current.page_id ? { ...row, title, updated_at: now } : row,
                      ),
                    ),
                  );
                }}
              />
            ) : online ? (
              <QuickNoteEditor
                key={`${current.page_id}:${refreshTick}`}
                rootPageId={current.page_id}
                rootSeed={{ title: seedTitle, icon: current.icon }}
                minHeight={420}
                autoFocus
                hideRootIcon
                autoTitleFromBody
                onRootTitle={(title) =>
                  setNotes((previous) =>
                    previous.map((row) => (row.page_id === current.page_id ? { ...row, title } : row)),
                  )
                }
              />
            ) : (
              <div className="mx-auto mt-16 max-w-sm text-center">
                <FileText size={30} className="mx-auto text-[var(--color-text-faint)]" />
                <p className="mt-3 text-[13px] font-semibold text-[var(--color-text-strong)]">
                  이 메모는 온라인에서 열 수 있습니다
                </p>
                <p className="mt-1 text-[11px] leading-5 text-[var(--color-text-muted)]">
                  본인 소유 메모도 온라인에서 한 번 연 뒤부터 이 Mac에서 오프라인으로
                  편집할 수 있습니다.
                </p>
              </div>
            )
          ) : loaded ? (
            <div className="mx-auto mt-16 max-w-sm text-center">
              <FileText size={30} className="mx-auto text-[var(--color-text-faint)]" />
              <p className="mt-3 text-[13px] font-semibold text-[var(--color-text-strong)]">
                메모를 선택하거나 새로 만드세요
              </p>
              <button
                type="button"
                onClick={() => void newNote()}
                className="mt-4 rounded-lg bg-[var(--color-accent)] px-3 py-2 text-[12px] font-semibold text-white"
              >
                새 메모
              </button>
            </div>
          ) : (
            <EditorSkeleton />
          )}
        </div>
      </main>
    </div>
  );
}
