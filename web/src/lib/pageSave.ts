"use client";

/**
 * pageSave — dashboard_pages(메모/페이지) 저장 파이프라인.
 *
 * 기존 fire-and-forget(`void api.updateDashboardPage`)의 침묵 유실을 없앤다:
 * - 페이지별 직렬 큐: 같은 페이지의 title/body PATCH가 순서 보장으로 나간다.
 * - 재시도: 네트워크/5xx는 백오프로 자동 재시도, 소진 후에도 online/포커스/
 *   flush 이벤트에서 다시 시도한다. 4xx(권한/삭제/검증)는 재시도하지 않는다.
 * - 상태 구독: "saved | saving | local | error"를 UI가 구독한다. `local`은 이 Mac의
 *   owner-scoped durable outbox에는 저장됐고 서버 연결만 기다리는 상태다.
 * - 드래프트 미러: 서버 확인 전 본문을 localStorage에 보관해 창 강제 종료·
 *   오프라인·세션 만료에도 유실이 없게 한다. 복구 판정은 시계가 아니라
 *   "서버 본문이 마지막 확인 본문에서 안 움직였는가"로 한다(멀티 기기 안전).
 */

import { api, ApiError } from "@/lib/api";
import {
  acknowledgeOfflineNoteDelete,
  acknowledgeOfflineNoteCreate,
  acknowledgeOfflineNoteUpdate,
  blockOfflineNote,
  isOfflineNoteCreate,
  offlineNoteScope,
  persistOfflineNotePatch,
  readOfflineNoteDeleteMarkers,
  readOfflineNoteOutbox,
  reconcileOfflineNoteCreate,
  reconcileOfflineNoteDelete,
  registerOfflineNoteCreate,
  renewOfflineNoteDeleteCreateLease,
  scopedNoteStorageKey,
  settleOfflineNoteDeleteCreate,
  tombstoneOfflineNoteDelete,
  type OfflineNoteCreate,
  type OfflineNotePatch,
} from "@/lib/offline-notes";

export type PageSaveState = "saved" | "saving" | "local" | "error";

type PagePatch = { title?: string; icon?: string | null; body?: string };

type Entry = {
  pending: PagePatch | null;
  inflight: boolean;
  attempts: number;
  timer: ReturnType<typeof setTimeout> | null;
  lastError: string | null;
  /** pending.body가 처음 잡힐 때의 서버 확인 본문 — 지연 재전송 충돌 판정 기준. */
  baseBody: string | null | undefined;
  /** 한 번이라도 전송 실패한 pending인지(재전송 전 서버 재확인 대상). */
  everFailed: boolean;
  /** 웜스타트 캐시가 베이스인데 아직 서버로 검증 안 됨 — 첫 본문 전송 전
   *  서버 대조 필수(오래된 캐시 베이스가 다른 기기의 최신 본문을 덮는 것 방지). */
  baseUnverified: boolean;
  /** IndexedDB outbox write that must settle before the corresponding request. */
  durableWrite: Promise<void>;
  /** Network/auth is unavailable, but the owner-scoped durable copy is safe. */
  waitingForNetwork: boolean;
};

const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();
/** 페이지별 "서버가 갖고 있다고 확인된" 본문. GET 시드 + 저장 성공 시 갱신. */
const confirmedBody = new Map<string, string>();
/** 디바운스 등으로 아직 커밋 전인 "편집 중" 페이지 — 저장 상태기계에 노출한다. */
const editing = new Set<string>();
/** 확인 본문을 localStorage에도 영속하는 페이지(개인 메모 표면만 등록).
 *  다음 콜드 로드가 서버 왕복 없이 즉시 그리는 웜스타트 캐시가 된다. */
const persistentPages = new Set<string>();
/** Pages invalidated by an owner-scope switch. Late unmount commits from the old
 * renderer are ignored until a newly verified page load registers the ID again. */
const stalePersistentPages = new Set<string>();
/** Durable records recovered in this renderer or created locally. */
const durablePendingPages = new Set<string>();
const localCreateInflight = new Set<string>();
const localDeleteInflight = new Set<string>();
const cancelledPages = new Set<string>();

/** 확인 본문 갱신 단일 지점 — 영속 대상이면 캐시에도 쓴다. */
function setConfirmed(pageId: string, body: string) {
  confirmedBody.set(pageId, body);
  if (persistentPages.has(pageId)) writeCachedPageBody(pageId, body);
}

const BACKOFF_MS = [1500, 4000, 10000, 20000];
const MAX_AUTO_ATTEMPTS = 6;
const CREATE_DELETE_LEASE_MS = 120_000;
const CREATE_DELETE_HEARTBEAT_MS = 30_000;
const CREATE_REQUEST_TIMEOUT_MS = 60_000;

function entry(pageId: string): Entry {
  let e = entries.get(pageId);
  if (!e) {
    e = {
      pending: null,
      inflight: false,
      attempts: 0,
      timer: null,
      lastError: null,
      baseBody: undefined,
      everFailed: false,
      baseUnverified: false,
      durableWrite: Promise.resolve(),
      waitingForNetwork: false,
    };
    entries.set(pageId, e);
  }
  return e;
}

function notify() {
  for (const cb of listeners) cb();
}

/** 전체 집계 저장 상태. 서버가 끊겨도 durable outbox가 있으면 error가 아니라
 *  `local`이다 — UI는 "이 Mac에 저장됨 · 동기화 대기"로 구분해 보여준다. */
export function getPageSaveState(): { state: PageSaveState; error: string | null } {
  let saving = editing.size > 0;
  let local = durablePendingPages.size > 0 || localCreateInflight.size > 0;
  for (const e of entries.values()) {
    if (e.lastError) return { state: "error", error: e.lastError };
    if (e.inflight) saving = true;
    if (e.pending) {
      if (e.waitingForNetwork) local = true;
      else saving = true;
    }
  }
  return { state: saving ? "saving" : local ? "local" : "saved", error: null };
}

/** 커밋 전 편집 중 표시(디바운스 창). 커밋 시점에 clear된다. */
export function markPageEditing(pageId: string) {
  if (!editing.has(pageId)) {
    editing.add(pageId);
    notify();
  }
}

export function clearPageEditing(pageId: string) {
  if (editing.delete(pageId)) notify();
}

export function subscribePageSaveState(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/** 페이지 로드시 서버 본문을 알려준다(드래프트 복구 판정의 기준점).
 *  persistCache=true면 이 페이지의 확인 본문을 웜스타트 캐시로도 영속한다. */
export function notePageLoaded(pageId: string, body: string | null, persistCache = false) {
  if (persistCache) {
    stalePersistentPages.delete(pageId);
    persistentPages.add(pageId);
  }
  setConfirmed(pageId, body ?? "");
}

/** 웜스타트 시드: 캐시 본문을 임시 확인 본문 + "미검증 베이스"로 잡는다.
 *  검증(markWarmBaseVerified) 전의 본문 전송은 pump가 서버 대조 후에만 보낸다. */
export function notePageWarmSeeded(pageId: string, cachedBody: string) {
  stalePersistentPages.delete(pageId);
  persistentPages.add(pageId);
  confirmedBody.set(pageId, cachedBody);
  const e = entry(pageId);
  if (e.baseBody === undefined) e.baseBody = cachedBody;
  e.baseUnverified = true;
}

/** 웜 베이스가 실서버와 정렬됐음을 표시(재검증 GET이 판정한 뒤 호출). */
export function markWarmBaseVerified(pageId: string) {
  const e = entries.get(pageId);
  if (!e) return;
  e.baseUnverified = false;
  if (!e.pending && !e.inflight) e.baseBody = undefined;
}

/** 충돌로 본문 전송이 폐기됐을 때(서버 승리) 소비자에게 알린다 —
 *  에디터를 서버 본문으로 리시드해 화면-서버 괴리를 남기지 않기 위해. */
type BodyDropListener = (pageId: string, serverBody: string | null) => void;
const dropListeners = new Set<BodyDropListener>();
export function onBodyPatchDropped(cb: BodyDropListener): () => void {
  dropListeners.add(cb);
  return () => {
    dropListeners.delete(cb);
  };
}

/** 이 창이 마지막으로 확인한 서버 본문(외부 편집 감지용). 미로드면 null. */
export function getConfirmedBody(pageId: string): string | null {
  return confirmedBody.has(pageId) ? (confirmedBody.get(pageId) as string) : null;
}

/** 커밋 전 입력·전송 대기·전송 중 등 "로컬이 서버보다 앞서 있는" 상태인지. */
export function isPageDirty(pageId: string): boolean {
  if (editing.has(pageId)) return true;
  const e = entries.get(pageId);
  return !!e && (e.inflight || !!e.pending);
}

function errStatus(err: unknown): number | null {
  return err instanceof ApiError ? err.status : null;
}

async function markDurableBlock(
  pageId: string,
  e: Entry,
  message: string,
  scope: string | null,
) {
  try {
    await blockOfflineNote(pageId, message, scope);
  } catch (reason) {
    e.lastError =
      reason instanceof Error ? reason.message : "이 Mac에 메모 상태를 기록하지 못했습니다";
  }
}

function clientPageId(): string | null {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  if (!globalThis.crypto?.getRandomValues) return null;
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

/** Create a memo locally first.  The client UUID is also the eventual server
 * primary key, so the editor/list never remount merely because connectivity
 * returns. */
export async function createOfflineNote(
  input: Omit<OfflineNoteCreate, "page_id" | "kind"> & { page_id?: string },
): Promise<OfflineNoteCreate> {
  const pageId = input.page_id ?? clientPageId();
  if (!pageId) throw new Error("이 환경에서는 로컬 메모 ID를 만들 수 없습니다");
  const create: OfflineNoteCreate = {
    page_id: pageId,
    title: input.title,
    icon: input.icon,
    body: input.body,
    kind: "memo",
  };
  await ensureOfflineNoteCreate(create);
  return create;
}

/** Reconstruct an outbox row from the owner-scoped list/body cache if an IDB
 * transaction was unavailable during the original click. Idempotent. */
export async function ensureOfflineNoteCreate(
  create: OfflineNoteCreate,
  mode: "create" | "recover" = "create",
): Promise<boolean> {
  const pageId = create.page_id;
  const scope = offlineNoteScope();
  if (!scope) throw new Error("로컬 메모 소유자를 확인할 수 없습니다");
  const generation = recoveryGeneration;
  const registered = await registerOfflineNoteCreate(create, scope, mode);
  if (!registered || !isOfflineRequestContextCurrent(generation, scope)) return false;
  persistentPages.add(pageId);
  const cached = readCachedPageBody(pageId);
  if (cached === null) writeCachedPageBody(pageId, create.body);
  notePageWarmSeeded(pageId, cached ?? create.body);
  durablePendingPages.add(pageId);
  notify();
  void syncOfflineNoteCreate(pageId, scope);
  return true;
}

function isOfflineRequestContextCurrent(generation: number, scope: string): boolean {
  return generation === recoveryGeneration && scope === offlineNoteScope();
}

function isPageRequestContextCurrent(
  generation: number,
  durableScope: string | null,
): boolean {
  return (
    generation === recoveryGeneration &&
    (durableScope === null || durableScope === offlineNoteScope())
  );
}

async function syncOfflineNoteDelete(
  pageId: string,
  scopeOverride?: string | null,
): Promise<void> {
  if (localDeleteInflight.has(pageId) || localCreateInflight.has(pageId)) return;
  const scope = scopeOverride ?? offlineNoteScope();
  if (!scope) return;
  const generation = recoveryGeneration;
  if (!isOfflineRequestContextCurrent(generation, scope)) return;
  const rows = await readOfflineNoteOutbox(scope);
  if (!isOfflineRequestContextCurrent(generation, scope)) return;
  const record = rows.find((row) => row.pageId === pageId && row.op === "delete");
  if (!record || record.op !== "delete") return;

  // Another webview may own the create fetch. Keep the tombstone durable and
  // wait for that renderer to settle it. The bounded lease is only a crash
  // fallback; normal create completion clears pendingCreate immediately.
  let pendingCreate = record.pendingCreate;
  const pendingUntil = record.pendingCreateUntil ?? record.updatedAt + 120_000;
  if (pendingCreate && Date.now() < pendingUntil) {
    durablePendingPages.add(pageId);
    const e = entry(pageId);
    e.waitingForNetwork = true;
    scheduleDeleteRetry(pageId, scope, pendingUntil - Date.now());
    notify();
    return;
  }
  if (pendingCreate) {
    await settleOfflineNoteDeleteCreate(pageId, scope);
    pendingCreate = false;
  }
  if (!isOfflineRequestContextCurrent(generation, scope)) return;

  localDeleteInflight.add(pageId);
  durablePendingPages.add(pageId);
  const e = entry(pageId);
  e.waitingForNetwork = false;
  e.lastError = null;
  notify();
  try {
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    await api.deleteDashboardPage(pageId);
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    await acknowledgeOfflineNoteDelete(pageId, scope);
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    localDeleteInflight.delete(pageId);
    cancelledPages.delete(pageId);
    durablePendingPages.delete(pageId);
    entries.delete(pageId);
    notify();
  } catch (reason) {
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    localDeleteInflight.delete(pageId);
    const status = errStatus(reason);
    // A missing ordinary page is already deleted.  For a cancelled in-flight
    // create, however, 404 can race ahead of the POST commit, so retain and retry.
    if (status === 404 && !pendingCreate) {
      await acknowledgeOfflineNoteDelete(pageId, scope);
      cancelledPages.delete(pageId);
      durablePendingPages.delete(pageId);
      entries.delete(pageId);
      notify();
      return;
    }
    if (status === 400 || status === 403 || status === 422) {
      e.lastError = status === 403 ? "메모 삭제 권한이 없습니다" : "메모 삭제가 거부되었습니다";
      await markDurableBlock(pageId, e, e.lastError, scope);
    } else {
      e.attempts += 1;
      e.waitingForNetwork = true;
      scheduleDeleteRetry(pageId, scope);
    }
    notify();
  }
}

function scheduleDeleteRetry(pageId: string, scope: string, delayOverride?: number) {
  const e = entry(pageId);
  if (e.timer) return;
  const delay =
    delayOverride === undefined
      ? BACKOFF_MS[Math.min(Math.max(e.attempts - 1, 0), BACKOFF_MS.length - 1)]
      : Math.max(250, delayOverride);
  e.timer = setTimeout(() => {
    e.timer = null;
    void syncOfflineNoteDelete(pageId, scope);
  }, delay);
}

async function syncOfflineNoteCreate(
  pageId: string,
  scopeOverride?: string | null,
): Promise<void> {
  if (localCreateInflight.has(pageId)) return;
  const scope = scopeOverride ?? offlineNoteScope();
  if (!scope) return;
  const generation = recoveryGeneration;
  if (!isOfflineRequestContextCurrent(generation, scope)) return;
  const rows = await readOfflineNoteOutbox(scope);
  if (!isOfflineRequestContextCurrent(generation, scope)) return;
  let record = rows.find((row) => row.pageId === pageId && row.op === "create");
  if (!record || record.op !== "create") {
    await reconcileOfflineNoteCreate(pageId, scope);
    const tombstone = rows.find((row) => row.pageId === pageId && row.op === "delete");
    if (tombstone) void syncOfflineNoteDelete(pageId, scope);
    return;
  }

  // A keystroke is mirrored synchronously before the editor's 800ms commit.
  // Merge it into the durable create payload before a restart/reconnect replay.
  const draft = readPageDraft(pageId);
  if (draft && draft.body !== record.create.body) {
    try {
      await persistOfflineNotePatch(pageId, { body: draft.body }, record.baseBody, scope);
      const latest = await readOfflineNoteOutbox(scope);
      if (!isOfflineRequestContextCurrent(generation, scope)) return;
      record = latest.find((row) => row.pageId === pageId && row.op === "create") as
        | Extract<(typeof latest)[number], { op: "create" }>
        | undefined;
      if (!record) return;
    } catch (reason) {
      if (!isOfflineRequestContextCurrent(generation, scope)) return;
      const e = entry(pageId);
      e.lastError =
        reason instanceof Error ? reason.message : "이 Mac에 메모를 저장하지 못했습니다";
      notify();
      return;
    }
  }

  localCreateInflight.add(pageId);
  durablePendingPages.add(pageId);
  const e = entry(pageId);
  e.waitingForNetwork = false;
  e.lastError = null;
  notify();
  const sent = record.create;
  const renewCreateLease = () => {
    void renewOfflineNoteDeleteCreateLease(
      pageId,
      Date.now() + CREATE_DELETE_LEASE_MS,
      scope,
    ).catch(() => undefined);
  };
  renewCreateLease();
  const createLeaseTimer = setInterval(renewCreateLease, CREATE_DELETE_HEARTBEAT_MS);
  const createController = new AbortController();
  const createTimeout = setTimeout(() => createController.abort(), CREATE_REQUEST_TIMEOUT_MS);
  try {
    if (!isOfflineRequestContextCurrent(generation, scope)) {
      clearInterval(createLeaseTimer);
      clearTimeout(createTimeout);
      return;
    }
    const created = await api.createDashboardPage(sent, { signal: createController.signal });
    clearInterval(createLeaseTimer);
    clearTimeout(createTimeout);
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    const latest = await readOfflineNoteOutbox(scope);
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    const deleted = latest.some((row) => row.pageId === pageId && row.op === "delete");
    if (cancelledPages.has(pageId) || deleted) {
      await settleOfflineNoteDeleteCreate(pageId, scope);
      localCreateInflight.delete(pageId);
      notify();
      void syncOfflineNoteDelete(pageId, scope);
      return;
    }
    const remaining = await acknowledgeOfflineNoteCreate(pageId, sent, scope);
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    localCreateInflight.delete(pageId);
    e.waitingForNetwork = false;
    e.attempts = 0;
    e.everFailed = false;
    e.baseUnverified = false;
    e.baseBody = undefined;
    setConfirmed(pageId, created.body ?? sent.body);
    maybeClearDraft(pageId, created.body ?? sent.body);
    if (remaining) {
      durablePendingPages.add(pageId);
      enqueuePageSave(pageId, remaining);
    } else {
      durablePendingPages.delete(pageId);
    }
    notify();
  } catch (err) {
    clearInterval(createLeaseTimer);
    clearTimeout(createTimeout);
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    localCreateInflight.delete(pageId);
    const status = errStatus(err);
    const latest = await readOfflineNoteOutbox(scope);
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    const deleted = latest.some((row) => row.pageId === pageId && row.op === "delete");
    if (cancelledPages.has(pageId) || deleted) {
      // Deterministic rejection proves no create can commit later. Network/5xx
      // remains uncertain, so its tombstone keeps pendingCreate=true and treats
      // 404 as non-final until a DELETE actually succeeds.
      if (status === 400 || status === 403 || status === 409 || status === 422) {
        await settleOfflineNoteDeleteCreate(pageId, scope);
      }
      void syncOfflineNoteDelete(pageId, scope);
      notify();
      return;
    }
    e.waitingForNetwork = true;
    if (status === 400 || status === 403 || status === 409 || status === 422) {
      const message =
        status === 403
          ? "메모 동기화 권한이 없습니다"
          : status === 409
            ? "메모 ID 충돌로 동기화가 보류됐습니다"
            : "메모 동기화가 거부되었습니다";
      e.lastError = message;
      await markDurableBlock(pageId, e, message, scope);
    } else {
      e.attempts += 1;
      scheduleRetry(pageId);
    }
    notify();
  }
}

let recoveryPromise: Promise<void> | null = null;
let recoveryGeneration = 0;

/** Restore durable create/update records after a webview restart. */
export function recoverOfflineNoteSaves(): Promise<void> {
  if (recoveryPromise) return recoveryPromise;
  const generation = recoveryGeneration;
  recoveryPromise = (async () => {
    const scope = offlineNoteScope();
    if (!scope) return;
    for (const pageId of readOfflineNoteDeleteMarkers(scope)) {
      if (!isOfflineRequestContextCurrent(generation, scope)) return;
      await tombstoneOfflineNoteDelete(pageId, false, scope);
    }
    const rows = await readOfflineNoteOutbox(scope);
    if (!isOfflineRequestContextCurrent(generation, scope)) return;
    for (const snapshot of rows) {
      if (generation !== recoveryGeneration) return;
      let record = snapshot;
      persistentPages.add(record.pageId);
      durablePendingPages.add(record.pageId);
      if (record.blockedReason) {
        entry(record.pageId).lastError = record.blockedReason;
        continue;
      }
      if (record.op === "delete") {
        await reconcileOfflineNoteDelete(record.pageId, scope);
        if (!isOfflineRequestContextCurrent(generation, scope)) return;
        void syncOfflineNoteDelete(record.pageId, scope);
        continue;
      }
      if (record.op === "create") {
        // CAS-only recovery: never reinsert a snapshot another webview already
        // acknowledged or replaced with an update/delete.
        try {
          const active = await registerOfflineNoteCreate(record.create, scope, "recover");
          if (!active) {
            // The snapshot raced with another webview's acknowledge/delete.
            // Re-read instead of leaving a phantom local-pending indicator.
            const latestRows = await readOfflineNoteOutbox(scope);
            if (generation !== recoveryGeneration) return;
            const latest = latestRows.find((candidate) => candidate.pageId === record.pageId);
            if (!latest) {
              durablePendingPages.delete(record.pageId);
              continue;
            }
            record = latest;
          }
        } catch (reason) {
          entry(record.pageId).lastError =
            reason instanceof Error ? reason.message : "이 Mac의 메모를 복구하지 못했습니다";
          continue;
        }
        if (record.blockedReason) {
          entry(record.pageId).lastError = record.blockedReason;
          continue;
        }
        if (record.op === "create") {
          void syncOfflineNoteCreate(record.pageId, scope);
          continue;
        }
        if (record.op === "delete") {
          void syncOfflineNoteDelete(record.pageId, scope);
          continue;
        }
      }
      const cached = readCachedPageBody(record.pageId);
      const base = cached ?? record.baseBody;
      if (typeof base === "string") {
        confirmedBody.set(record.pageId, base);
        const e = entry(record.pageId);
        e.baseBody = base;
        e.baseUnverified = true;
        e.everFailed = true;
      }
      enqueuePageSave(record.pageId, record.patch);
    }
    notify();
  })().finally(() => {
    recoveryPromise = null;
  });
  return recoveryPromise;
}

function scheduleRetry(pageId: string) {
  const e = entry(pageId);
  if (e.timer || (!persistentPages.has(pageId) && e.attempts > MAX_AUTO_ATTEMPTS)) return;
  const delay = BACKOFF_MS[Math.min(e.attempts - 1, BACKOFF_MS.length - 1)];
  e.timer = setTimeout(() => {
    e.timer = null;
    if (isOfflineNoteCreate(pageId)) void syncOfflineNoteCreate(pageId);
    else void pump(pageId);
  }, delay);
}

async function pump(pageId: string): Promise<void> {
  const e = entry(pageId);
  const generation = recoveryGeneration;
  const durableScope = persistentPages.has(pageId) ? offlineNoteScope() : null;
  if (e.inflight || !e.pending) return;
  if (isOfflineNoteCreate(pageId)) {
    void e.durableWrite
      .then(() => syncOfflineNoteCreate(pageId, durableScope))
      .catch((error: unknown) => {
        if (!isPageRequestContextCurrent(generation, durableScope)) return;
        e.lastError =
          error instanceof Error ? error.message : "이 Mac에 메모를 저장하지 못했습니다";
        notify();
      });
    return;
  }
  let patch = e.pending;
  e.pending = null;
  e.inflight = true;
  e.waitingForNetwork = false;
  notify();

  // A personal note write reaches the network only after its owner-scoped
  // IndexedDB outbox transaction has completed (local-first durability).
  try {
    await e.durableWrite;
  } catch (error) {
    if (!isPageRequestContextCurrent(generation, durableScope)) return;
    e.inflight = false;
    e.pending = { ...patch, ...(e.pending ?? {}) };
    e.lastError =
      error instanceof Error ? error.message : "이 Mac에 메모를 저장하지 못했습니다";
    notify();
    return;
  }
  if (!isPageRequestContextCurrent(generation, durableScope)) return;

  // 실패했던 본문의 재전송, 그리고 웜스타트 미검증 베이스의 첫 전송은 보내기
  // 전에 서버가 그 사이 움직였는지 확인한다. 다른 창/기기/팀원이 먼저
  // 저장했으면 서버 승리(드래프트 복구와 같은 규칙) — 오래된 베이스가 남의
  // 최신 본문을 통째로 덮지 않게 한다.
  if (
    (e.everFailed || e.baseUnverified) &&
    patch.body !== undefined &&
    typeof e.baseBody === "string"
  ) {
    try {
      if (!isPageRequestContextCurrent(generation, durableScope)) return;
      const remote = await api.getDashboardPage(pageId);
      if (!isPageRequestContextCurrent(generation, durableScope)) return;
      e.baseUnverified = false;
      if ((remote.body ?? "") !== e.baseBody) {
        if (persistentPages.has(pageId)) {
          // Never discard an offline personal edit.  Keep the local body and
          // durable record blocked for explicit resolution instead of applying
          // the old server-wins/drop behavior used by shared pages.
          e.pending = { ...patch, ...(e.pending ?? {}) };
          e.inflight = false;
          e.lastError = "다른 곳의 편집과 충돌했습니다. 이 Mac의 내용은 보관되어 있습니다";
          await markDurableBlock(pageId, e, e.lastError, durableScope);
          notify();
          return;
        }
        setConfirmed(pageId, remote.body ?? "");
        clearPageDraft(pageId);
        const dropped = { ...patch };
        delete dropped.body;
        patch = dropped;
        for (const cb of dropListeners) cb(pageId, remote.body);
        if (Object.keys(patch).length === 0) {
          e.inflight = false;
          e.attempts = 0;
          e.everFailed = false;
          e.baseBody = undefined;
          e.lastError = null;
          notify();
          if (e.pending) void pump(pageId);
          return;
        }
      }
    } catch {
      if (!isPageRequestContextCurrent(generation, durableScope)) return;
      // 확인 실패(네트워크 등)면 그대로 전송을 시도한다 — 어차피 같이 실패한다.
      // (웜 미검증 상태는 유지되어 다음 전송에서 다시 검증한다.)
      if (e.baseUnverified) {
        e.pending = { ...patch, ...(e.pending ?? {}) };
        e.inflight = false;
        e.attempts += 1;
        e.everFailed = true;
        e.waitingForNetwork = persistentPages.has(pageId);
        scheduleRetry(pageId);
        notify();
        return;
      }
    }
  }

  try {
    if (!isPageRequestContextCurrent(generation, durableScope)) return;
    await api.updateDashboardPage(pageId, patch);
    if (!isPageRequestContextCurrent(generation, durableScope)) return;
    const remaining = persistentPages.has(pageId)
      ? await acknowledgeOfflineNoteUpdate(pageId, patch, durableScope)
      : null;
    if (!isPageRequestContextCurrent(generation, durableScope)) return;
    e.inflight = false;
    e.attempts = 0;
    e.lastError = null;
    e.everFailed = false;
    e.waitingForNetwork = false;
    if (patch.body !== undefined) {
      e.baseBody = undefined;
      setConfirmed(pageId, patch.body);
      maybeClearDraft(pageId, patch.body);
    }
    if (remaining) {
      durablePendingPages.add(pageId);
      e.pending = { ...remaining, ...(e.pending ?? {}) };
    } else if (!e.pending) {
      durablePendingPages.delete(pageId);
    }
    notify();
    if (e.pending) void pump(pageId);
  } catch (err) {
    if (!isPageRequestContextCurrent(generation, durableScope)) return;
    e.inflight = false;
    // 실패한 patch를 되돌리되, 그 사이 들어온 새 값이 이긴다.
    e.pending = { ...patch, ...(e.pending ?? {}) };
    e.everFailed = true;
    const status = errStatus(err);
    if (status === 403 || status === 404 || status === 422 || status === 400) {
      // 영구 실패: 재시도 무의미(권한 없음/삭제됨/검증 실패). 조용히 버리지 않고 표시.
      if (!persistentPages.has(pageId)) e.pending = null;
      e.attempts = 0;
      e.lastError =
        status === 403
          ? "저장 권한이 없습니다"
          : status === 404
            ? "삭제되었거나 접근할 수 없는 메모입니다"
            : "저장이 거부되었습니다";
      if (persistentPages.has(pageId)) {
        await markDurableBlock(pageId, e, e.lastError, durableScope);
      }
      else if (status === 404) clearPageDraft(pageId);
    } else {
      // 네트워크/5xx/401: 백오프 재시도. 드래프트 미러가 있으므로 데이터는 안전.
      e.attempts += 1;
      e.waitingForNetwork = persistentPages.has(pageId);
      e.lastError =
        !persistentPages.has(pageId) && e.attempts > MAX_AUTO_ATTEMPTS
          ? "저장하지 못했습니다"
          : null;
      scheduleRetry(pageId);
    }
    notify();
  }
}

/** 저장 요청. 같은 페이지의 미전송 patch와 병합되고 순서대로 전송된다. */
export function enqueuePageSave(pageId: string, patch: PagePatch) {
  if (stalePersistentPages.has(pageId)) {
    clearPageEditing(pageId);
    return;
  }
  const e = entry(pageId);
  if (patch.body !== undefined && e.baseBody === undefined) {
    // 이 본문 편집이 시작될 때 서버가 갖고 있던 본문을 기억한다(충돌 판정 기준).
    e.baseBody = confirmedBody.has(pageId) ? (confirmedBody.get(pageId) as string) : null;
  }
  if (persistentPages.has(pageId)) {
    durablePendingPages.add(pageId);
    const base = typeof e.baseBody === "string" ? e.baseBody : getConfirmedBody(pageId);
    const scope = offlineNoteScope();
    e.durableWrite = e.durableWrite
      .catch(() => undefined)
      .then(() => persistOfflineNotePatch(pageId, patch as OfflineNotePatch, base, scope));
  }
  if (isOfflineNoteCreate(pageId)) {
    e.lastError = null;
    e.waitingForNetwork = false;
    notify();
    const generation = recoveryGeneration;
    const scope = offlineNoteScope();
    void e.durableWrite
      .then(() => syncOfflineNoteCreate(pageId, scope))
      .catch((reason: unknown) => {
        if (!isPageRequestContextCurrent(generation, scope)) return;
        e.lastError =
          reason instanceof Error ? reason.message : "이 Mac에 메모를 저장하지 못했습니다";
        notify();
      });
    return;
  }
  e.pending = { ...(e.pending ?? {}), ...patch };
  e.lastError = null;
  e.waitingForNetwork = false;
  if (e.timer) {
    clearTimeout(e.timer);
    e.timer = null;
    e.attempts = 0;
  }
  notify();
  void pump(pageId);
}

/** 대기 중인 저장을 즉시 전송 시도(창 숨김/재시도 버튼). */
export function flushPageSaves() {
  for (const [pageId, e] of entries) {
    if (e.timer) {
      clearTimeout(e.timer);
      e.timer = null;
    }
    e.attempts = 0;
    e.waitingForNetwork = false;
    if (isOfflineNoteCreate(pageId)) void syncOfflineNoteCreate(pageId);
    else if (e.pending && !e.inflight) void pump(pageId);
  }
  void recoverOfflineNoteSaves();
}

/** 재시도할 것이 남지 않은 종결 에러(4xx) 항목을 정리해 표시등을 되살린다.
 *  재시도 버튼에서 flushPageSaves와 함께 호출 — 죽은 항목이 표시등을 영구
 *  점유하고 버튼을 no-op으로 만드는 것을 막는다. */
export function dismissTerminalPageSaveErrors() {
  let changed = false;
  for (const [pageId, e] of entries) {
    if (e.lastError && !e.pending && !e.inflight && !durablePendingPages.has(pageId)) {
      if (e.timer) clearTimeout(e.timer);
      entries.delete(pageId);
      changed = true;
    }
  }
  if (changed) notify();
}

/** 페이지 삭제 시 durable create/update outbox까지 먼저 취소한다. */
export async function purgePageSave(pageId: string): Promise<void> {
  const scope = offlineNoteScope();
  if (!scope) throw new Error("로컬 메모 소유자를 확인할 수 없습니다");
  const pendingCreate = isOfflineNoteCreate(pageId) || localCreateInflight.has(pageId);
  cancelledPages.add(pageId);
  try {
    // Deletion replaces, rather than removes, the pending create/update. It is
    // cleared only after the server confirms DELETE (or a safe non-create 404).
    await tombstoneOfflineNoteDelete(pageId, pendingCreate, scope);
  } catch (reason) {
    cancelledPages.delete(pageId);
    throw reason;
  }
  const e = entries.get(pageId);
  if (e) {
    if (e.timer) clearTimeout(e.timer);
    entries.delete(pageId);
  }
  editing.delete(pageId);
  confirmedBody.delete(pageId);
  persistentPages.delete(pageId);
  stalePersistentPages.add(pageId);
  durablePendingPages.add(pageId);
  try {
    const key = pageCacheKey(pageId);
    if (key) localStorage.removeItem(key);
  } catch {
    // ignore
  }
  clearPageDraft(pageId);
  notify();
  void syncOfflineNoteDelete(pageId, scope);
}

/** Logout/account-switch boundary for module-level state shared by the current
 * renderer.  Durable storage is cleared separately by clearOfflineNoteStorage. */
export function resetPageSaveMemory() {
  recoveryGeneration += 1;
  recoveryPromise = null;
  for (const pageId of persistentPages) stalePersistentPages.add(pageId);
  for (const e of entries.values()) if (e.timer) clearTimeout(e.timer);
  entries.clear();
  editing.clear();
  confirmedBody.clear();
  persistentPages.clear();
  durablePendingPages.clear();
  localCreateInflight.clear();
  localDeleteInflight.clear();
  cancelledPages.clear();
  notify();
}

/* ---- 드래프트 미러 (localStorage) ---- */

const DRAFT_MAX_BYTES = 400_000;

/* ---- 웜스타트 본문 캐시 (localStorage) ----
 * 마지막으로 서버가 확인해 준 본문을 영속해, 다음 콜드 로드가 GET을 기다리지
 * 않고 즉시 그리게 한다. 진실은 여전히 서버 — 로드 후 GET으로 재검증한다. */

function pageCacheKey(pageId: string): string | null {
  return scopedNoteStorageKey("page-cache", pageId);
}

function pageDraftKey(pageId: string): string | null {
  return scopedNoteStorageKey("page-draft", pageId);
}

function writeCachedPageBody(pageId: string, body: string) {
  try {
    const key = pageCacheKey(pageId);
    if (!key) return;
    const raw = JSON.stringify({ body });
    if (raw.length > DRAFT_MAX_BYTES) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, raw);
  } catch {
    // localStorage 불가 시 웜스타트만 포기
  }
}

/** 마지막 확인 본문 캐시. 없으면 null(콜드 로드). */
export function readCachedPageBody(pageId: string): string | null {
  try {
    const key = pageCacheKey(pageId);
    if (!key) return null;
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { body?: unknown };
    return typeof parsed.body === "string" ? parsed.body : null;
  } catch {
    return null;
  }
}

type Draft = { body: string; base: string | null; ts: number };

function preserveDraftConflict(pageId: string, draft: Draft) {
  const message = "다른 곳의 편집과 충돌했습니다. 이 Mac의 내용은 보관되어 있습니다";
  persistentPages.add(pageId);
  durablePendingPages.add(pageId);
  const e = entry(pageId);
  e.baseBody = draft.base;
  e.baseUnverified = true;
  e.everFailed = true;
  e.lastError = message;
  const scope = offlineNoteScope();
  e.durableWrite = e.durableWrite
    .catch(() => undefined)
    .then(() => persistOfflineNotePatch(pageId, { body: draft.body }, draft.base, scope))
    .then(() => blockOfflineNote(pageId, message, scope));
  notify();
}

/** 매 편집마다 호출: 서버 확인 전 본문을 보관한다. base=마지막 확인 본문. */
export function writePageDraft(pageId: string, body: string) {
  try {
    if (stalePersistentPages.has(pageId)) return;
    const key = pageDraftKey(pageId);
    if (!key) return;
    const base = confirmedBody.has(pageId) ? (confirmedBody.get(pageId) as string) : null;
    if (base !== null && body === base) {
      // 서버와 동일해졌으면 미러 불필요.
      localStorage.removeItem(key);
      return;
    }
    const raw = JSON.stringify({ body, base, ts: Date.now() } satisfies Draft);
    if (raw.length > DRAFT_MAX_BYTES) return; // 대형 본문(인라인 이미지)은 미러 생략
    localStorage.setItem(key, raw);
  } catch {
    // localStorage 불가(쿼터/프라이빗 모드)여도 편집을 막지 않는다.
  }
}

export function readPageDraft(pageId: string): Draft | null {
  try {
    const key = pageDraftKey(pageId);
    if (!key) return null;
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Draft;
    if (typeof parsed.body !== "string") return null;
    return parsed;
  } catch {
    return null;
  }
}

export function clearPageDraft(pageId: string) {
  try {
    const key = pageDraftKey(pageId);
    if (key) localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

function maybeClearDraft(pageId: string, confirmedBodyValue: string) {
  const d = readPageDraft(pageId);
  if (d && d.body === confirmedBodyValue) clearPageDraft(pageId);
}

/**
 * 로드된 서버 본문과 남아 있는 드래프트를 비교해 복구할 본문을 결정한다.
 * - 드래프트가 없거나 서버 본문과 같으면: 서버 본문 사용(드래프트 정리).
 * - 서버 본문이 드래프트의 base에서 안 움직였으면: 이 창이 저장 못 한 편집이
 *   유일한 진행분 → 드래프트 복구(+재저장 필요).
 * - 서버 본문이 base와 다르면: 개인 메모는 로컬 본문을 durable blocked copy로 보존,
 *   공유 표면만 기존 server-wins 규칙으로 드래프트를 폐기한다.
 */
export function resolveDraftOnLoad(
  pageId: string,
  serverBody: string | null,
): { body: string | null; restored: boolean; conflicted: boolean } {
  const server = serverBody ?? "";
  const draft = readPageDraft(pageId);
  if (!draft || draft.body === server) {
    if (draft) clearPageDraft(pageId);
    return { body: serverBody, restored: false, conflicted: false };
  }
  if (draft.base === null || draft.base === server) {
    return { body: draft.body, restored: true, conflicted: false };
  }
  if (persistentPages.has(pageId)) {
    preserveDraftConflict(pageId, draft);
    return { body: draft.body, restored: false, conflicted: true };
  }
  clearPageDraft(pageId);
  return { body: serverBody, restored: false, conflicted: false };
}

/* ---- 전역 회수 이벤트: 온라인 복귀/창 표시 시 미전송분 재시도 ---- */

let wired = false;
export function wirePageSaveRecovery() {
  if (wired || typeof window === "undefined") return;
  wired = true;
  void recoverOfflineNoteSaves();
  window.addEventListener("online", () => flushPageSaves());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") flushPageSaves();
  });
}
