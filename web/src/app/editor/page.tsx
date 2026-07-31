"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { useSearchParams } from "next/navigation";
import {
  api,
  type Block,
  type DocumentDetail,
  type DocumentSummary,
} from "@/lib/api";
import {
  Chip,
  Skeleton,
  Toolbar,
  ToolbarButton,
  cx,
  inputClass,
} from "@/components/ui";
import type { EditorHandle } from "@/components/BlockNoteEditor";

// BlockNote touches `window`; load it client-only.
const BlockNoteEditor = dynamic(
  () =>
    import("@/components/BlockNoteEditor").then((m) => m.BlockNoteEditor),
  {
    ssr: false,
    loading: () => (
      <div className="space-y-2 pt-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-5/6" />
        <Skeleton className="h-3 w-3/4" />
      </div>
    ),
  },
);

type SaveState = "idle" | "saving" | "saved" | "publishing" | "published" | "error";

const NEW_DOC = "";

export default function EditorPage() {
  const searchParams = useSearchParams();
  const initialDocId = searchParams.get("doc");
  const [docs, setDocs] = useState<DocumentSummary[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  const [activeId, setActiveId] = useState<string>(NEW_DOC);
  const [activeSource, setActiveSource] = useState("editor");
  const [title, setTitle] = useState("");
  const [loadedBlocks, setLoadedBlocks] = useState<Block[] | null>([]);
  const [loadedMarkdown, setLoadedMarkdown] = useState<string>("");
  const [loadingDoc, setLoadingDoc] = useState(false);
  const [editorKey, setEditorKey] = useState(0);

  const [saveState, setSaveState] = useState<SaveState>("idle");
  // 저장하지 않은 편집이 있는지. 문서를 로드할 때마다 초기화하고(각 로드 지점에서
  // setDirty(false)), 사용자가 편집하면 true가 된다. 문서 전환·페이지 이탈 시 유실 경고에 쓴다.
  const [dirty, setDirty] = useState(false);
  const editorRef = useRef<EditorHandle | null>(null);

  // 저장 안 한 편집이 있는데 탭 닫기/새로고침/앱 이탈 시 브라우저 기본 경고를 띄운다.
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  // 저장 안 한 편집이 있으면 문서 전환/새 문서 전에 확인받는다(true=계속 진행).
  const confirmDiscardIfDirty = useCallback(() => {
    if (!dirty) return true;
    return window.confirm(
      "저장하지 않은 변경이 있습니다. 저장하지 않고 이동할까요?",
    );
  }, [dirty]);

  const refreshList = useCallback(async () => {
    try {
      const list = await api.listDocuments();
      setDocs(list);
      setListError(null);
    } catch (e) {
      setListError(e instanceof Error ? e.message : "목록을 불러오지 못했습니다.");
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function loadInitial() {
      try {
        const list = await api.listDocuments();
        if (cancelled) return;
        setDocs(list);
        setListError(null);
      } catch (e) {
        if (cancelled) return;
        setListError(e instanceof Error ? e.message : "목록을 불러오지 못했습니다.");
      }
    }

    void loadInitial();

    return () => {
      cancelled = true;
    };
  }, []);

  const openDoc = useCallback(async (docId: string) => {
    if (!confirmDiscardIfDirty()) return;
    setSaveState("idle");
    setLoadingDoc(true);
    setActiveId(docId);
    try {
      const doc: DocumentDetail = await api.getDocument(docId);
      setTitle(doc.title);
      setLoadedBlocks(doc.block_json);
      setLoadedMarkdown(doc.markdown ?? "");
      setActiveSource(doc.source);
    } catch {
      setLoadedBlocks([]);
      setLoadedMarkdown("");
    } finally {
      setDirty(false);
      setEditorKey((k) => k + 1);
      setLoadingDoc(false);
    }
  }, [confirmDiscardIfDirty]);

  useEffect(() => {
    if (!initialDocId) return;
    const docId = initialDocId;
    let cancelled = false;

    async function loadInitialDoc() {
      try {
        const doc: DocumentDetail = await api.getDocument(docId);
        if (cancelled) return;
        setSaveState("idle");
        setActiveId(docId);
        setTitle(doc.title);
        setLoadedBlocks(doc.block_json);
        setLoadedMarkdown(doc.markdown ?? "");
        setActiveSource(doc.source);
        setDirty(false);
        setEditorKey((k) => k + 1);
      } catch {
        if (cancelled) return;
        setLoadedBlocks([]);
        setLoadedMarkdown("");
      }
    }

    void loadInitialDoc();

    return () => {
      cancelled = true;
    };
  }, [initialDocId]);

  const newDoc = useCallback(() => {
    if (!confirmDiscardIfDirty()) return;
    setSaveState("idle");
    setActiveId(NEW_DOC);
    setActiveSource("editor");
    setTitle("");
    setLoadedBlocks([]);
    setLoadedMarkdown("");
    setDirty(false);
    setEditorKey((k) => k + 1);
  }, [confirmDiscardIfDirty]);

  const save = useCallback(async () => {
    if (!editorRef.current) return;
    setSaveState("saving");
    try {
      const block_json = editorRef.current.getBlocks();
      const markdown = await editorRef.current.getMarkdown();
      const body = {
        title: title.trim() || "제목 없음",
        block_json,
        markdown,
      };

      if (activeId === NEW_DOC) {
        const { doc_id } = await api.createDocument(body);
        // Seed the editor baseline with what we just saved BEFORE flipping the
        // id. The docId "" → doc_id change re-creates the BlockNote instance
        // (useCreateBlockNote dep) and it re-seeds from initialBlocks; without
        // this the stale-empty newDoc baseline wins and the body we just typed
        // vanishes on screen (and a follow-up save then overwrites the server
        // copy with the empty editor).
        setLoadedBlocks(block_json);
        setLoadedMarkdown(markdown);
        setActiveId(doc_id);
        setActiveSource("editor");
      } else {
        await api.updateDocument(activeId, body);
      }
      setDirty(false);
      setSaveState("saved");
      await refreshList();
      window.setTimeout(
        () => setSaveState((s) => (s === "saved" ? "idle" : s)),
        1800,
      );
    } catch {
      setSaveState("error");
    }
  }, [activeId, title, refreshList]);

  const publish = useCallback(async () => {
    if (!editorRef.current || activeId === NEW_DOC) return;
    setSaveState("publishing");
    try {
      const block_json = editorRef.current.getBlocks();
      const markdown = await editorRef.current.getMarkdown();
      const body = {
        title: title.trim() || "제목 없음",
        block_json,
        markdown,
      };

      await api.publishDocument(activeId, body);
      setDirty(false);
      setActiveSource("editor");
      setSaveState("published");
      await refreshList();
      window.setTimeout(
        () => setSaveState((s) => (s === "published" ? "idle" : s)),
        2200,
      );
    } catch {
      setSaveState("error");
    }
  }, [activeId, title, refreshList]);

  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row">
      {/* ───────── document list (panel surface) ───────── */}
      <aside
        className="flex max-h-[190px] w-full shrink-0 flex-col md:max-h-none md:w-[256px]"
        style={{
          background: "var(--color-panel)",
          borderRight: "1px solid var(--color-divider)",
          borderBottom: "1px solid var(--color-divider)",
        }}
      >
        <div
          className="flex items-center justify-between px-3 py-2.5"
          style={{ borderBottom: "1px solid var(--color-divider)" }}
        >
          <span
            className="text-[var(--text-body-sm)] font-extrabold uppercase tracking-[0.02em]"
            style={{ color: "var(--color-text-muted)" }}
          >
            문서
          </span>
          <ToolbarButton onClick={newDoc} icon={<PlusIcon />}>
            새 문서
          </ToolbarButton>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          {listError ? (
            <p
              className="px-2 py-2 text-[var(--text-body-sm)]"
              style={{ color: "var(--color-fail-fg)" }}
            >
              {listError}
            </p>
          ) : docs === null ? (
            <div className="space-y-1.5 px-1">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-9 w-full" />
              ))}
            </div>
          ) : docs.length === 0 ? (
            <p
              className="px-2 py-6 text-center text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              아직 문서가 없습니다.
              <br />새 문서를 만들어 보세요.
            </p>
          ) : (
            <ul className="flex flex-col gap-[2px]">
              {docs.map((d) => {
                const active = d.doc_id === activeId;
                return (
                  <li key={d.doc_id}>
                    <button
                      onClick={() => openDoc(d.doc_id)}
                      aria-current={active ? "true" : undefined}
                      className="w-full rounded-[6px] px-2 py-1.5 text-left transition-colors"
                      style={{
                        background: active
                          ? "var(--color-sidebar-active)"
                          : "transparent",
                      }}
                      onMouseEnter={(e) => {
                        if (!active)
                          e.currentTarget.style.background =
                            "color-mix(in srgb, var(--color-sidebar-active) 50%, transparent)";
                      }}
                      onMouseLeave={(e) => {
                        if (!active)
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
                        className="mt-0.5 flex items-center gap-1.5 text-[var(--text-meta)]"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        <Chip tone="neutral">{d.source}</Chip>
                        {formatDate(d.updated_at)}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </aside>

      {/* ───────── editor pane ───────── */}
      <div
        className="flex min-w-0 flex-1 flex-col"
        style={{ background: "var(--color-app-canvas)" }}
      >
        <header
          className="flex flex-wrap items-center gap-2 px-3 py-2.5 md:flex-nowrap md:gap-3 md:px-5"
          style={{
            background: "var(--color-app-canvas)",
            borderBottom: "1px solid var(--color-divider)",
          }}
        >
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="제목 없음"
            aria-label="문서 제목"
            className={cx(
              inputClass,
              "min-w-0 flex-1 border-transparent bg-transparent px-0 py-0 text-[var(--text-day-heading)] font-extrabold",
            )}
            style={{
              color: "var(--color-text-strong)",
              border: "1px solid transparent",
            }}
          />
          <Toolbar className="max-w-full shrink-0 overflow-x-auto">
            <SaveBadge state={saveState} source={activeSource} />
            {activeSource === "agent_draft" && activeId !== NEW_DOC ? (
              <ToolbarButton
                onClick={publish}
                disabled={saveState === "saving" || saveState === "publishing" || loadingDoc}
                icon={<PublishIcon />}
              >
                {saveState === "publishing" ? "게시 중…" : "게시"}
              </ToolbarButton>
            ) : null}
            <ToolbarButton
              tone="primary"
              onClick={save}
              disabled={saveState === "saving" || saveState === "publishing" || loadingDoc}
              icon={<SaveIcon />}
            >
              {saveState === "saving" ? "저장 중…" : "저장"}
            </ToolbarButton>
          </Toolbar>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[820px] px-4 py-5 md:px-6 md:py-6">
            {loadingDoc ? (
              <div className="space-y-2 pt-2">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-3 w-5/6" />
                <Skeleton className="h-3 w-2/3" />
              </div>
            ) : (
              <BlockNoteEditor
                key={editorKey}
                docId={activeId}
                initialBlocks={loadedBlocks}
                initialMarkdown={loadedMarkdown}
                handleRef={editorRef}
                onChange={() => setDirty(true)}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function SaveBadge({ state, source }: { state: SaveState; source: string }) {
  if (state === "saved") {
    return source === "agent_draft" ? (
      <Chip tone="pass">드래프트 저장됨</Chip>
    ) : (
      <Chip tone="pass">저장됨 · 재색인 완료</Chip>
    );
  }
  if (state === "published") return <Chip tone="pass">게시됨 · 재색인 완료</Chip>;
  if (state === "error") return <Chip tone="fail">저장 실패</Chip>;
  return null;
}

function formatDate(iso: string) {
  try {
    return new Date(iso).toLocaleDateString("ko-KR", {
      month: "short",
      day: "numeric",
    });
  } catch {
    return "";
  }
}

/* -------------------------------- icons -------------------------------- */

function PlusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

function SaveIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
      <path d="M17 21v-8H7v8M7 3v5h8" />
    </svg>
  );
}

function PublishIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 16V4" />
      <path d="m7 9 5-5 5 5" />
      <path d="M5 20h14" />
    </svg>
  );
}
