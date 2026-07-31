"use client";

/**
 * 노션식 페이지 편집 유닛.
 * - PageLevelView: 페이지 한 장(제목 + 자유 본문) 화면.
 * - PageStack: 한 루트 페이지에서 시작하는 하위 페이지 네비게이션 스택
 *   (브레드크럼 + 하위 페이지 열기/뒤로 + 이름 변경 시 부모 링크 동기화).
 *   메모 워크스페이스가 이 스택을 그대로 쓴다. 프로젝트 상세는 루트가
 *   프로젝트라 자체 스택을 쓰고 PageLevelView만 공유한다.
 *
 * 저장은 fire-and-forget이 아니라 lib/pageSave 큐를 쓴다: 페이지별 직렬화,
 * 네트워크/5xx 재시도, 4xx 종결 처리, (옵션) localStorage 드래프트 복구.
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { api, type DashboardPage } from "@/lib/api";
import {
  clearPageDraft,
  enqueuePageSave,
  isPageDirty,
  markWarmBaseVerified,
  notePageLoaded,
  notePageWarmSeeded,
  onBodyPatchDropped,
  readCachedPageBody,
  readPageDraft,
  resolveDraftOnLoad,
  wirePageSaveRecovery,
} from "@/lib/pageSave";
import { AutoGrowTitle, PeekDivider } from "@/components/dashboard/peek";
import { setSubpageTitleInBody } from "@/components/dashboard/subpageBlock";
import { offlineNoteScope } from "@/lib/offline-notes";

const RichBodyEditor = dynamic(
  () => import("@/components/dashboard/RichBody").then((m) => m.RichBodyEditor),
  { ssr: false },
);

export function PageLevelView({
  page,
  onRename,
  onSaveBody,
  onOpenPage,
  onOpenDatabase,
  onCreatePage,
  onCreateDatabase,
  hideTitle = false,
  hideIcon = false,
  minHeight = 360,
  autoFocus = false,
  draftMirror = false,
  readOnly = false,
}: {
  page: DashboardPage;
  onRename: (title: string) => void;
  onSaveBody: (body: string) => void;
  onOpenPage: (pageId: string) => void;
  /** database 링크 클릭 시 같은 스택에서 연다(노션식). */
  onOpenDatabase?: (databaseId: string) => void;
  onCreatePage: () => Promise<{ page_id: string; title: string }>;
  /** "/데이터베이스" "/보드" 실행 시 새 데이터베이스를 만들고 database_id를 돌려준다. */
  onCreateDatabase?: (opts: {
    view: "table" | "board";
    template: "basic" | "tasks";
  }) => Promise<{ database_id: string }>;
  /** 루트 컬처 본문 등 제목 없이 본문만 보여주고 싶을 때 */
  hideTitle?: boolean;
  /** Quick-note chrome already identifies the document; omit the decorative icon. */
  hideIcon?: boolean;
  minHeight?: number;
  /** 빠른 메모: 열리자마자 본문에 커서. */
  autoFocus?: boolean;
  /** 개인 메모: 편집 즉시 localStorage 드래프트 미러(공유 표면 금지). */
  draftMirror?: boolean;
  /** 공유 보기 권한처럼 제목과 본문을 탐색만 하는 표면. */
  readOnly?: boolean;
}) {
  return (
    <>
      {hideTitle ? null : (
        <>
          <div className="flex items-start gap-3">
            {hideIcon ? null : (
              <span className="mt-2 text-[22px] leading-none">{page.icon || "📄"}</span>
            )}
            <div className="min-w-0 flex-1">
              <AutoGrowTitle
                value={page.title}
                onCommit={(v) => onRename(v || "새 페이지")}
                readOnly={readOnly}
              />
            </div>
          </div>
          <PeekDivider />
        </>
      )}

      <RichBodyEditor
        key={page.page_id}
        id={page.page_id}
        value={page.body ?? ""}
        minHeight={minHeight}
        onCommit={onSaveBody}
        onOpenPage={onOpenPage}
        onOpenDatabase={onOpenDatabase}
        onCreatePage={onCreatePage}
        onCreateDatabase={onCreateDatabase}
        autoFocus={autoFocus}
        draftMirror={draftMirror}
        readOnly={readOnly}
      />
    </>
  );
}

/** 기본 제목(사용자가 아직 안 정한 상태) — 이 상태면 본문 첫 줄로 자동 제목. */
const DEFAULT_TITLES = new Set(["", "새 메모", "새 페이지", "제목 없음"]);

/** BlockNote 블록 JSON에서 첫 텍스트 줄을 뽑아 제목 후보로 만든다. */
export function firstLineTitle(body: string): string | null {
  const clip = (s: string) => {
    const t = s.trim().replace(/\s+/g, " ");
    return t ? t.slice(0, 50) : null;
  };
  if (!body.trim()) return null;
  try {
    const blocks = JSON.parse(body) as unknown[];
    if (!Array.isArray(blocks)) return null;
    const textOf = (block: unknown): string => {
      const b = block as { content?: unknown };
      let out = "";
      if (Array.isArray(b.content)) {
        for (const item of b.content) {
          const it = item as { text?: string; content?: unknown[] };
          if (typeof it.text === "string") out += it.text;
          else if (Array.isArray(it.content)) {
            for (const inner of it.content) {
              const iv = inner as { text?: string };
              if (typeof iv.text === "string") out += iv.text;
            }
          }
        }
      }
      return out;
    };
    for (const block of blocks) {
      const t = clip(textOf(block));
      if (t) return t;
      const children = (block as { children?: unknown[] }).children;
      if (Array.isArray(children)) {
        for (const child of children) {
          const ct = clip(textOf(child));
          if (ct) return ct;
        }
      }
    }
    return null;
  } catch {
    // 평문 폴백(레거시 본문)
    for (const line of body.split("\n")) {
      const t = clip(line);
      if (t) return t;
    }
    return null;
  }
}

type PageRequestGuard = {
  lifecycleGeneration: number;
  ownerScope: string | null;
  requestedPageId: string | null;
  originPageId: string | null;
  openIntent: number | null;
};

/** 메모 등 페이지를 루트로 한 하위 페이지 네비게이션 스택. */
export function PageStack({
  rootPageId,
  rootSeed,
  onRootTitle,
  onRootBody,
  hideRootTitle = false,
  hideRootIcon = false,
  minHeight = 360,
  autoFocus = false,
  draftPersistence = false,
  autoTitleFromBody = false,
  readOnly = false,
}: {
  rootPageId: string;
  /** 루트 로드 전 보여줄 제목/아이콘(목록에서 알고 있는 값). */
  rootSeed?: { title: string; icon: string | null };
  /** 루트 제목이 바뀌면 목록 갱신용으로 알린다. */
  onRootTitle?: (title: string) => void;
  /** 루트 본문 커밋을 목록 preview/수정시각에 반영한다. */
  onRootBody?: (pageId: string, body: string) => void;
  /** 루트 레벨에서 제목을 숨긴다(컬처 자유 본문처럼 섹션으로 끼울 때). */
  hideRootTitle?: boolean;
  /** Omit the root page's decorative icon (quick-note two-pane layout). */
  hideRootIcon?: boolean;
  minHeight?: number;
  /** 빠른 메모: 열리자마자 본문에 커서. */
  autoFocus?: boolean;
  /** 개인 메모 전용: 편집 드래프트 미러 + 로드 시 미전송 드래프트 복구.
   *  공유 표면(컬처 등)에서 켜면 남의 최신 편집을 덮을 수 있어 금지. */
  draftPersistence?: boolean;
  /** 제목이 기본값이면 본문 첫 줄로 자동 제목(애플 메모장식). */
  autoTitleFromBody?: boolean;
  /** 공유 보기 권한처럼 제목과 본문을 수정할 수 없는 표면. */
  readOnly?: boolean;
}) {
  const [stack, setStack] = useState<string[]>([rootPageId]);
  const [pages, setPages] = useState<Record<string, DashboardPage>>({});
  const [err, setErr] = useState<string | null>(null);
  /** 웜스타트 캐시 본문 → 서버 검증 본문 재시드용 에디터 리마운트 키. */
  const [seedTick, setSeedTick] = useState(0);
  /** pageId → 마지막으로 자동 설정한 제목(사용자가 손대면 자동 제목 중단). */
  const autoTitles = useRef<Record<string, string>>({});
  const currentId = stack[stack.length - 1];
  const lifecycleGenerationRef = useRef(0);
  const rootPageIdRef = useRef(rootPageId);
  const openIntentRef = useRef(0);

  useEffect(() => {
    wirePageSaveRecovery();
  }, []);

  // AccountShell owner key remount와 root 전환 모두 async 응답보다 우선한다.
  // Strict Mode의 setup→cleanup→setup에서도 이전 generation 응답은 폐기된다.
  useLayoutEffect(() => {
    rootPageIdRef.current = rootPageId;
    lifecycleGenerationRef.current += 1;
    return () => {
      lifecycleGenerationRef.current += 1;
    };
  }, [rootPageId]);

  const stackRef = useRef(stack);
  useLayoutEffect(() => {
    stackRef.current = stack;
  }, [stack]);

  function capturePageRequest(
    requestedPageId: string | null,
    originPageId: string | null = null,
    openIntent: number | null = null,
  ): PageRequestGuard {
    return {
      lifecycleGeneration: lifecycleGenerationRef.current,
      ownerScope: offlineNoteScope(),
      requestedPageId,
      originPageId,
      openIntent,
    };
  }

  function isPageRequestCurrent(guard: PageRequestGuard): boolean {
    if (lifecycleGenerationRef.current !== guard.lifecycleGeneration) return false;
    if (offlineNoteScope() !== guard.ownerScope) return false;
    if (guard.openIntent !== null && openIntentRef.current !== guard.openIntent) return false;
    if (guard.originPageId !== null) {
      const liveStack = stackRef.current;
      if (liveStack[liveStack.length - 1] !== guard.originPageId) return false;
    }
    return true;
  }

  // 웜 베이스 충돌로 본문 전송이 폐기(서버 승리)되면 에디터를 서버 본문으로
  // 리시드한다 — 화면이 저장 안 된 본문을 계속 들고 있지 않게.
  useEffect(() => {
    return onBodyPatchDropped((pageId, serverBody) => {
      setPages((prev) =>
        prev[pageId] ? { ...prev, [pageId]: { ...prev[pageId], body: serverBody } } : prev,
      );
      if (stackRef.current.includes(pageId)) {
        setSeedTick((t) => t + 1);
        setErr("다른 곳에서 먼저 저장된 최신 내용으로 갱신했습니다. 방금 입력은 저장되지 않았어요.");
      }
    });
  }, []);

  /** 페이지를 GET하고, 지난 세션이 저장을 확인 못 한 드래프트가 있으면 복구 +
   *  재저장한다. 루트와 하위 페이지 모두 같은 규칙을 탄다(하위만 빼면 크래시
   *  후 하위 페이지 편집이 유실된다). */
  async function loadPageResolvingDraft(
    pageId: string,
    guard: PageRequestGuard,
  ): Promise<DashboardPage | null> {
    const p = await api.getDashboardPage(pageId);
    if (
      !isPageRequestCurrent(guard) ||
      guard.requestedPageId !== pageId ||
      p.page_id !== pageId
    ) {
      return null;
    }
    notePageLoaded(pageId, p.body, draftPersistence);
    if (draftPersistence) {
      const resolved = resolveDraftOnLoad(pageId, p.body);
      if (resolved.restored) {
        enqueuePageSave(pageId, { body: resolved.body ?? "" });
        return { ...p, body: resolved.body };
      }
      if (resolved.conflicted) {
        setErr("다른 곳의 편집과 충돌했습니다. 이 Mac의 내용은 보관되어 있습니다.");
        return { ...p, body: resolved.body };
      }
    }
    return p;
  }

  // 루트 페이지 로드. 개인 메모(draftPersistence)는 웜스타트를 쓴다: 마지막 확인
  // 본문 캐시(+미전송 드래프트 표시)로 즉시 에디터를 띄우고, GET은 백그라운드
  // 재검증으로 돌린다 — 서버가 다르고 로컬이 깨끗할 때만 재시드한다.
  useEffect(() => {
    let alive = true;
    let warmBody: string | null = null;
    let warmSeeded = false;
    if (draftPersistence) {
      const cached = readCachedPageBody(rootPageId);
      if (cached !== null) {
        warmBody = cached;
        // 크래시 드래프트는 표시만 우선한다(저장/폐기 결정은 실서버 GET 후 —
        // 캐시 기준으로 재저장하면 다른 기기의 더 새 본문을 덮을 수 있다).
        const draft = readPageDraft(rootPageId);
        if (draft && draft.body !== cached && (draft.base === null || draft.base === cached)) {
          warmBody = draft.body;
        }
        warmSeeded = true;
        // 캐시를 "미검증 베이스"로 시드 — 검증 전 본문 전송은 pump가 서버
        // 대조 후에만 내보낸다(오래된 캐시가 타 기기 최신 본문을 덮는 것 방지).
        notePageWarmSeeded(rootPageId, cached);
        // 웜스타트 1회: GET을 기다리지 않고 캐시 본문으로 즉시 에디터를 띄운다.
        // eslint-disable-next-line react-hooks/set-state-in-effect
        setPages((prev) =>
          prev[rootPageId]
            ? prev
            : {
                ...prev,
                [rootPageId]: {
                  page_id: rootPageId,
                  title: rootSeed?.title ?? "메모",
                  icon: rootSeed?.icon ?? null,
                  body: warmBody,
                  kind: "memo",
                },
              },
        );
      }
    }
    const requestGuard = capturePageRequest(rootPageId);
    api
      .getDashboardPage(rootPageId)
      .then((p) => {
        if (
          !alive ||
          !isPageRequestCurrent(requestGuard) ||
          requestGuard.requestedPageId !== rootPageId ||
          rootPageIdRef.current !== rootPageId ||
          p.page_id !== rootPageId
        ) {
          return;
        }
        notePageLoaded(rootPageId, p.body, draftPersistence);
        let page = p;
        let restored = false;
        let conflicted = false;
        if (draftPersistence) {
          const resolved = resolveDraftOnLoad(rootPageId, p.body);
          if (resolved.restored) {
            if (!warmSeeded || !isPageDirty(rootPageId)) {
              restored = true;
              page = { ...p, body: resolved.body };
              // 드래프트는 실서버 본문 기준으로 검증됐다 — 베이스 정렬 후 재저장.
              markWarmBaseVerified(rootPageId);
              enqueuePageSave(rootPageId, { body: resolved.body ?? "" });
            } else {
              // 웜 화면 위에 이미 라이브 입력이 있다 — 입력이 우선, 옛 드래프트 폐기.
              clearPageDraft(rootPageId);
            }
          } else if (resolved.conflicted) {
            conflicted = true;
            page = { ...p, body: resolved.body };
            setErr("다른 곳의 편집과 충돌했습니다. 이 Mac의 내용은 보관되어 있습니다.");
          }
        }
        if (!warmSeeded) {
          setPages((prev) => ({ ...prev, [rootPageId]: page }));
          return;
        }
        if (conflicted) {
          setPages((prev) => ({ ...prev, [rootPageId]: page }));
          setSeedTick((t) => t + 1);
          return;
        }
        // 웜스타트 재검증 3분기:
        // (1) 서버 판정 본문 == 화면 본문 → 베이스 검증 완료, 제목만 최신화.
        // (2) 다르지만 로컬이 깨끗(또는 드래프트 복구) → 서버/복구 본문으로 재시드.
        // (3) 다르고 로컬이 더티 → 베이스 미검증 유지; 커밋 시 pump가 서버 대조로
        //     판정한다(충돌이면 서버 승리 + onBodyPatchDropped 리시드).
        const shown = warmBody ?? "";
        const target = page.body ?? "";
        if (target === shown) {
          markWarmBaseVerified(rootPageId);
          setPages((prev) => ({
            ...prev,
            [rootPageId]: { ...page, body: prev[rootPageId]?.body ?? page.body },
          }));
        } else if (restored || !isPageDirty(rootPageId)) {
          markWarmBaseVerified(rootPageId);
          setPages((prev) => ({ ...prev, [rootPageId]: page }));
          setSeedTick((t) => t + 1);
        } else {
          setPages((prev) => ({
            ...prev,
            [rootPageId]: { ...page, body: prev[rootPageId]?.body ?? page.body },
          }));
        }
      })
      .catch(() => {
        // 웜스타트로 이미 그렸다면 조용히 둔다(오프라인에서도 이어서 작성 가능).
        // 베이스는 미검증으로 남아 커밋 시 pump가 서버 대조 후에만 전송한다.
        if (
          alive &&
          !warmSeeded &&
          isPageRequestCurrent(requestGuard) &&
          rootPageIdRef.current === rootPageId
        ) {
          setErr("페이지를 불러오지 못했습니다.");
        }
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rootPageId]);

  async function createPage() {
    const liveStack = stackRef.current;
    const originPageId = liveStack[liveStack.length - 1] ?? null;
    const requestGuard = capturePageRequest(null, originPageId);
    const p = await api.createDashboardPage({ title: "새 페이지" });
    if (!isPageRequestCurrent(requestGuard)) {
      throw new Error("stale page create response");
    }
    notePageLoaded(p.page_id, p.body);
    setPages((prev) => ({ ...prev, [p.page_id]: p }));
    return { page_id: p.page_id, title: p.title };
  }

  async function openPage(pageId: string) {
    const liveStack = stackRef.current;
    const originPageId = liveStack[liveStack.length - 1] ?? null;
    const openIntent = ++openIntentRef.current;
    const requestGuard = capturePageRequest(pageId, originPageId, openIntent);
    if (!pages[pageId]) {
      try {
        const p = await loadPageResolvingDraft(pageId, requestGuard);
        if (!p || !isPageRequestCurrent(requestGuard)) return;
        setPages((prev) => ({ ...prev, [pageId]: p }));
      } catch {
        if (isPageRequestCurrent(requestGuard)) {
          setErr("페이지를 불러오지 못했습니다.");
        }
        return;
      }
    }
    if (!isPageRequestCurrent(requestGuard)) return;
    setErr(null);
    setStack((prev) => [...prev, pageId]);
  }

  function renamePage(pageId: string, title: string) {
    const t = title.trim() || "새 페이지";
    setPages((prev) =>
      prev[pageId] ? { ...prev, [pageId]: { ...prev[pageId], title: t } } : prev,
    );
    enqueuePageSave(pageId, { title: t });
    const idx = stack.indexOf(pageId);
    if (idx <= 0) {
      onRootTitle?.(t); // 루트(메모) 제목은 목록과 동기화
      return;
    }
    const parentId = stack[idx - 1];
    const cur = pages[parentId]?.body ?? null;
    const nb = setSubpageTitleInBody(cur, pageId, t);
    if (nb !== cur) {
      setPages((prev) =>
        prev[parentId] ? { ...prev, [parentId]: { ...prev[parentId], body: nb } } : prev,
      );
      enqueuePageSave(parentId, { body: nb ?? "" });
    }
  }

  function savePageBody(pageId: string, body: string) {
    setPages((prev) =>
      prev[pageId] ? { ...prev, [pageId]: { ...prev[pageId], body } } : prev,
    );
    // 주의: 빈 본문도 ""로 보낸다. null은 서버가 "변경 없음"으로 취급해
    // 내용을 다 지운 편집이 영영 저장되지 않는 버그가 있었다.
    enqueuePageSave(pageId, { body });
    if (pageId === rootPageId) onRootBody?.(rootPageId, body);

    if (autoTitleFromBody) {
      const curTitle = (pages[pageId]?.title ?? "").trim();
      const eligible =
        DEFAULT_TITLES.has(curTitle) || curTitle === autoTitles.current[pageId];
      if (eligible) {
        const derived = firstLineTitle(body);
        if (derived && derived !== curTitle) {
          autoTitles.current[pageId] = derived;
          renamePage(pageId, derived);
        }
      }
    }
  }

  const current: DashboardPage | undefined = pages[currentId];

  return (
    <>
      {stack.length > 1 ? (
        <div
          className="mb-2 flex flex-wrap items-center gap-1 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {stack.map((pid, i) => {
            const label =
              i === 0
                ? pages[pid]?.title || rootSeed?.title || "메모"
                : pages[pid]?.title || "페이지";
            const last = i === stack.length - 1;
            return (
              <span key={pid} className="flex items-center gap-1">
                {i > 0 ? <span style={{ color: "var(--color-text-faint)" }}>/</span> : null}
                {last ? (
                  <span style={{ color: "var(--color-text-strong)", fontWeight: 600 }}>
                    {label}
                  </span>
                ) : (
                  <button
                    type="button"
                    onClick={() => setStack((prev) => prev.slice(0, i + 1))}
                    className="rounded-[4px] px-1 hover:bg-[var(--color-app-canvas)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {label}
                  </button>
                )}
              </span>
            );
          })}
        </div>
      ) : null}
      {err ? (
        <p className="mb-2 text-[var(--text-body-sm)]" style={{ color: "var(--color-fail-fg)" }}>
          {err}
        </p>
      ) : null}

      {current ? (
        <PageLevelView
          key={`${currentId}:${seedTick}`}
          page={current}
          onRename={(t) => renamePage(currentId, t)}
          onSaveBody={(b) => savePageBody(currentId, b)}
          onOpenPage={openPage}
          onCreatePage={createPage}
          hideTitle={hideRootTitle && stack.length === 1}
          hideIcon={hideRootIcon && stack.length === 1}
          minHeight={minHeight}
          autoFocus={autoFocus}
          draftMirror={draftPersistence}
          readOnly={readOnly}
        />
      ) : err ? null : (
        // 본문 로드 전에는 에디터를 만들지 않는다. 빈 에디터가 실본문보다
        // 먼저 시드되면 이후 편집이 실본문을 덮어쓸 수 있다(로드-시딩 레이스).
        <div
          className="animate-pulse rounded-[8px]"
          style={{ minHeight: 120, background: "var(--color-app-canvas)" }}
        />
      )}
    </>
  );
}
