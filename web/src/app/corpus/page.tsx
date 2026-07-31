"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
import {
  api,
  type Block,
  type CorpusDocumentDetail,
  type CorpusListItem,
  type CorpusListResponse,
} from "@/lib/api";
import { Chip, Skeleton, Toolbar, ToolbarButton, cx, inputClass } from "@/components/ui";
import type { EditorHandle } from "@/components/BlockNoteEditor";

// BlockNote touches `window`; load it client-only.
const BlockNoteEditor = dynamic(
  () => import("@/components/BlockNoteEditor").then((m) => m.BlockNoteEditor),
  {
    ssr: false,
    loading: () => (
      <div className="space-y-2 pt-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-5/6" />
      </div>
    ),
  },
);

type SaveState = "idle" | "saving" | "saved" | "error";
type SortMode = "recent" | "title";

const PAGE_SIZE = 50;
const COMPACT_WIDTH = 760;
const ALL_SOURCE = "__all__";

export default function CorpusPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const docParam = searchParams.get("doc")?.trim() || null;
  const compactShell = useCompactShell();

  const [source, setSource] = useState<string>(ALL_SOURCE);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [sort, setSort] = useState<SortMode>("recent");

  const [list, setList] = useState<CorpusListResponse | null>(null);
  const [items, setItems] = useState<CorpusListItem[]>([]);
  const [offset, setOffset] = useState(0);
  const [listError, setListError] = useState<string | null>(null);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);

  const [doc, setDoc] = useState<CorpusDocumentDetail | null>(null);
  const [title, setTitle] = useState("");
  const [loadedBlocks, setLoadedBlocks] = useState<Block[] | null>([]);
  const [loadedMarkdown, setLoadedMarkdown] = useState("");
  const [loadingDoc, setLoadingDoc] = useState(false);
  const [editorKey, setEditorKey] = useState(0);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  // 저장하지 않은 편집(본문/제목) 여부. 문서 로드마다 초기화(로드 지점에서 setDirty(false)).
  const [dirty, setDirty] = useState(false);
  const editorRef = useRef<EditorHandle | null>(null);

  // 저장 안 한 편집이 있는데 탭 닫기/새로고침 시 브라우저 기본 경고를 띄운다.
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  // 저장 안 한 편집이 있으면 문서 전환/목록 이동 전에 확인받는다(true=계속 진행).
  const confirmDiscardIfDirty = useCallback(() => {
    if (!dirty) return true;
    return window.confirm(
      "저장하지 않은 변경이 있습니다. 저장하지 않고 이동할까요?",
    );
  }, [dirty]);

  // debounce the search input (~300ms) before it drives the query.
  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => window.clearTimeout(handle);
  }, [search]);

  const queryParams = useMemo(
    () => ({
      source: source === ALL_SOURCE ? undefined : source,
      q: debouncedSearch || undefined,
      sort,
    }),
    [source, debouncedSearch, sort],
  );

  // First page reloads whenever any filter changes.
  useEffect(() => {
    let cancelled = false;

    async function run() {
      setLoadingList(true);
      setListError(null);
      setOffset(0);
      try {
        const res = await api.listCorpus({ ...queryParams, limit: PAGE_SIZE, offset: 0 });
        if (cancelled) return;
        setList(res);
        setItems(res.items);
      } catch (e) {
        if (cancelled) return;
        setListError(e instanceof Error ? e.message : "목록을 불러오지 못했습니다.");
        setList(null);
        setItems([]);
      } finally {
        if (!cancelled) setLoadingList(false);
      }
    }

    void run();

    return () => {
      cancelled = true;
    };
  }, [queryParams]);

  const loadMore = useCallback(async () => {
    if (loadingMore) return;
    const nextOffset = offset + PAGE_SIZE;
    setLoadingMore(true);
    try {
      const res = await api.listCorpus({
        ...queryParams,
        limit: PAGE_SIZE,
        offset: nextOffset,
      });
      setItems((current) => [...current, ...res.items]);
      setList(res);
      setOffset(nextOffset);
    } catch (e) {
      setListError(e instanceof Error ? e.message : "더 불러오지 못했습니다.");
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, offset, queryParams]);

  // Load the selected doc (driven by the ?doc= URL param).
  useEffect(() => {
    let cancelled = false;

    async function run() {
      if (!docParam) {
        setDoc(null);
        return;
      }
      setLoadingDoc(true);
      setSaveState("idle");
      try {
        const detail = await api.getCorpusDocument(docParam);
        if (cancelled) return;
        setDoc(detail);
        setTitle(detail.title);
        setLoadedBlocks(detail.block_json);
        setLoadedMarkdown(detail.markdown ?? "");
        setDirty(false);
        setEditorKey((k) => k + 1);
      } catch (e) {
        if (cancelled) return;
        setDoc(null);
        setListError(e instanceof Error ? e.message : "문서를 불러오지 못했습니다.");
      } finally {
        if (!cancelled) setLoadingDoc(false);
      }
    }

    void run();

    return () => {
      cancelled = true;
    };
  }, [docParam]);

  const openDoc = useCallback(
    (docId: string) => {
      if (!confirmDiscardIfDirty()) return;
      router.push(`/corpus?doc=${encodeURIComponent(docId)}`);
    },
    [router, confirmDiscardIfDirty],
  );

  const backToList = useCallback(() => {
    if (!confirmDiscardIfDirty()) return;
    router.push("/corpus");
  }, [router, confirmDiscardIfDirty]);

  const save = useCallback(async () => {
    if (!editorRef.current || !docParam) return;
    setSaveState("saving");
    try {
      const block_json = editorRef.current.getBlocks();
      const markdown = await editorRef.current.getMarkdown();
      await api.updateCorpusDocument(docParam, {
        title: title.trim() || "제목 없음",
        block_json,
        markdown,
      });
      setDirty(false);
      setSaveState("saved");
      // reflect the new title/preview in the list without a full reload
      setItems((current) =>
        current.map((it) =>
          it.doc_id === docParam ? { ...it, title: title.trim() || "제목 없음" } : it,
        ),
      );
      window.setTimeout(() => setSaveState((s) => (s === "saved" ? "idle" : s)), 1800);
    } catch {
      setSaveState("error");
    }
  }, [docParam, title]);

  const sources = useMemo(() => {
    const counts = list?.counts_by_source ?? {};
    return Object.entries(counts).sort((a, b) => b[1] - a[1]);
  }, [list]);
  const totalForSource = useMemo(
    () => sources.reduce((sum, [, count]) => sum + count, 0),
    [sources],
  );

  const showList = !compactShell || !docParam;
  const showDetail = !compactShell || Boolean(docParam);
  const hasMore = items.length < (list?.total ?? 0);

  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row">
      {/* ───────── list / filter panel ───────── */}
      {showList ? (
        <aside
          className={cx(
            "flex min-h-0 w-full shrink-0 flex-col md:w-[320px]",
            compactShell ? "h-full" : "",
          )}
          style={{
            background: "var(--color-panel)",
            borderRight: "1px solid var(--color-divider)",
          }}
        >
          <div
            className="flex flex-col gap-2 px-3 py-2.5"
            style={{ borderBottom: "1px solid var(--color-divider)" }}
          >
            <div className="flex items-center justify-between gap-2">
              <span
                className="text-[var(--text-body-sm)] font-extrabold uppercase tracking-[0.02em]"
                style={{ color: "var(--color-text-muted)" }}
              >
                코퍼스
              </span>
              <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
                {list ? `${list.total.toLocaleString()}건` : ""}
              </span>
            </div>

            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="제목 · 본문 검색"
              aria-label="코퍼스 검색"
              className={cx(inputClass, "min-h-11")}
              style={{ border: "1px solid var(--color-divider)" }}
            />

            <div className="flex items-center gap-1.5">
              <SortToggle active={sort === "recent"} onClick={() => setSort("recent")}>
                최신
              </SortToggle>
              <SortToggle active={sort === "title"} onClick={() => setSort("title")}>
                제목
              </SortToggle>
            </div>

            {/* source filter chips */}
            <div className="flex flex-wrap gap-1.5">
              <SourceChip
                active={source === ALL_SOURCE}
                count={totalForSource}
                onClick={() => setSource(ALL_SOURCE)}
              >
                전체
              </SourceChip>
              {sources.map(([name, count]) => (
                <SourceChip
                  key={name}
                  active={source === name}
                  count={count}
                  onClick={() => setSource(name)}
                >
                  {name}
                </SourceChip>
              ))}
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
            {listError ? (
              <p
                className="px-2 py-2 text-[var(--text-body-sm)]"
                style={{ color: "var(--color-fail-fg)" }}
              >
                {listError}
              </p>
            ) : loadingList ? (
              <div className="space-y-1.5 px-1">
                {[0, 1, 2, 3, 4].map((i) => (
                  <Skeleton key={i} className="h-14 w-full" />
                ))}
              </div>
            ) : items.length === 0 ? (
              <p
                className="px-2 py-6 text-center text-[var(--text-body-sm)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                조건에 맞는 문서가 없습니다.
              </p>
            ) : (
              <>
                <ul className="flex flex-col gap-[2px]">
                  {items.map((d) => (
                    <li key={d.doc_id}>
                      <button
                        onClick={() => openDoc(d.doc_id)}
                        aria-current={d.doc_id === docParam ? "true" : undefined}
                        className="w-full rounded-[6px] px-2.5 py-2 text-left transition-colors"
                        style={{
                          background:
                            d.doc_id === docParam ? "var(--color-sidebar-active)" : "transparent",
                          minHeight: 44,
                        }}
                        onMouseEnter={(e) => {
                          if (d.doc_id !== docParam)
                            e.currentTarget.style.background =
                              "color-mix(in srgb, var(--color-sidebar-active) 50%, transparent)";
                        }}
                        onMouseLeave={(e) => {
                          if (d.doc_id !== docParam)
                            e.currentTarget.style.background = "transparent";
                        }}
                      >
                        <span
                          className="block truncate text-[var(--text-body-sm)] font-semibold"
                          style={{ color: "var(--color-text-strong)" }}
                        >
                          {d.title || "제목 없음"}
                        </span>
                        <span
                          className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[var(--text-meta)]"
                          style={{ color: "var(--color-text-muted)" }}
                        >
                          <Chip tone="neutral">{d.source}</Chip>
                          {d.owner_name ? <span className="truncate">{d.owner_name}</span> : null}
                          <span>{formatDate(d.updated_at)}</span>
                        </span>
                        {d.preview ? (
                          <span
                            className="mt-1 block truncate text-[var(--text-meta)]"
                            style={{ color: "var(--color-text-faint)" }}
                          >
                            {d.preview}
                          </span>
                        ) : null}
                      </button>
                    </li>
                  ))}
                </ul>
                {hasMore ? (
                  <div className="px-1 py-2">
                    <button
                      onClick={() => void loadMore()}
                      disabled={loadingMore}
                      className="w-full rounded-[6px] px-2.5 text-[var(--text-body-sm)] font-semibold disabled:opacity-50"
                      style={{
                        minHeight: 44,
                        background: "var(--color-card)",
                        border: "1px solid var(--color-toolbar-border)",
                        color: "var(--color-text-strong)",
                      }}
                    >
                      {loadingMore ? "불러오는 중…" : "더 보기"}
                    </button>
                  </div>
                ) : null}
              </>
            )}
          </div>
        </aside>
      ) : null}

      {/* ───────── detail / editor panel ───────── */}
      {showDetail ? (
        <div
          className="flex min-w-0 flex-1 flex-col"
          style={{ background: "var(--color-app-canvas)" }}
        >
          {docParam ? (
            <>
              <header
                className="flex flex-wrap items-center gap-2 px-3 py-2.5 md:flex-nowrap md:gap-3 md:px-5"
                style={{
                  background: "var(--color-app-canvas)",
                  borderBottom: "1px solid var(--color-divider)",
                }}
              >
                {compactShell ? (
                  <button
                    onClick={backToList}
                    aria-label="목록으로"
                    className="flex shrink-0 items-center justify-center rounded-[6px]"
                    style={{
                      minHeight: 44,
                      minWidth: 44,
                      background: "var(--color-card)",
                      border: "1px solid var(--color-toolbar-border)",
                      color: "var(--color-text-body)",
                    }}
                    type="button"
                  >
                    <BackIcon />
                  </button>
                ) : null}
                <input
                  value={title}
                  onChange={(e) => {
                    setTitle(e.target.value);
                    setDirty(true);
                  }}
                  placeholder="제목 없음"
                  aria-label="문서 제목"
                  className={cx(
                    inputClass,
                    "min-w-0 flex-1 border-transparent bg-transparent px-0 py-0 text-[var(--text-day-heading)] font-extrabold",
                  )}
                  style={{ color: "var(--color-text-strong)", border: "1px solid transparent" }}
                />
                <Toolbar className="max-w-full shrink-0 overflow-x-auto">
                  <SaveBadge state={saveState} />
                  {doc ? <Chip tone="muted">{doc.source}</Chip> : null}
                  <ToolbarButton
                    tone="primary"
                    onClick={() => void save()}
                    disabled={saveState === "saving" || loadingDoc}
                    icon={<SaveIcon />}
                  >
                    {saveState === "saving" ? "저장 중…" : "저장"}
                  </ToolbarButton>
                </Toolbar>
              </header>

              <div className="min-h-0 flex-1 overflow-y-auto">
                <div className="mx-auto w-full max-w-[860px] px-3 py-3 md:px-5 md:py-4">
                  {loadingDoc ? (
                    <div className="space-y-2 pt-2">
                      <Skeleton className="h-4 w-full" />
                      <Skeleton className="h-3 w-5/6" />
                      <Skeleton className="h-3 w-2/3" />
                    </div>
                  ) : (
                    <BlockNoteEditor
                      key={editorKey}
                      docId={docParam}
                      initialBlocks={loadedBlocks}
                      initialMarkdown={loadedMarkdown}
                      handleRef={editorRef}
                      onChange={() => setDirty(true)}
                      className="min-h-[40vh]"
                    />
                  )}
                </div>
              </div>
            </>
          ) : (
            <div
              className="flex h-full flex-col items-center justify-center px-6 text-center text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              왼쪽 목록에서 문서를 선택하세요.
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

function SaveBadge({ state }: { state: SaveState }) {
  if (state === "saved") return <Chip tone="pass">저장됨 · 재색인 완료</Chip>;
  if (state === "error") return <Chip tone="fail">저장 실패</Chip>;
  return null;
}

function SourceChip({
  active,
  count,
  children,
  onClick,
}: {
  active: boolean;
  count: number;
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className="inline-flex items-center gap-1 rounded-[4px] px-1.5 py-[3px] text-[var(--text-meta)] font-medium transition-colors"
      style={{
        background: active ? "var(--color-sidebar-active)" : "var(--color-card)",
        border: "1px solid var(--color-divider)",
        color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
      }}
      type="button"
    >
      <span className="max-w-[120px] truncate">{children}</span>
      <span style={{ color: "var(--color-text-muted)" }}>{count}</span>
    </button>
  );
}

function SortToggle({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className="inline-flex h-8 items-center rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] font-semibold transition-colors"
      style={{
        background: active ? "var(--color-sidebar-active)" : "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        color: "var(--color-text-strong)",
      }}
      type="button"
    >
      {children}
    </button>
  );
}

function useCompactShell() {
  const [compact, setCompact] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined") return;
    function sync() {
      setCompact(window.innerWidth < COMPACT_WIDTH);
    }
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);
  return compact;
}

function formatDate(iso: string) {
  try {
    return new Date(iso).toLocaleDateString("ko-KR", { month: "short", day: "numeric" });
  } catch {
    return "";
  }
}

/* -------------------------------- icons -------------------------------- */

function SaveIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
      <path d="M17 21v-8H7v8M7 3v5h8" />
    </svg>
  );
}

function BackIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m15 18-6-6 6-6" />
    </svg>
  );
}
