"use client";

/**
 * Owner-scoped durable outbox for personal notes.
 *
 * The quick-note window is a second webview and can disappear while a request is
 * in flight.  Server writes therefore are never the only copy: a merged create
 * or update record is stored in IndexedDB first and removed only after the
 * matching server response is acknowledged.  The tiny owner pointer lives in
 * localStorage so a cached offline shell can select the correct namespace before
 * /auth/me is reachable.
 */

export interface OfflineNoteOwner {
  userId: string;
  nodeId: string;
}

export interface OfflineNotePatch {
  title?: string;
  icon?: string | null;
  body?: string;
}

export interface OfflineNoteCreate extends OfflineNotePatch {
  page_id: string;
  title: string;
  icon: string | null;
  body: string;
  kind: "memo";
}

export type OfflineNoteOutboxRecord =
  | {
      key: string;
      scope: string;
      pageId: string;
      op: "create";
      create: OfflineNoteCreate;
      baseBody: string | null;
      updatedAt: number;
      blockedReason: string | null;
    }
  | {
      key: string;
      scope: string;
      pageId: string;
      op: "update";
      patch: OfflineNotePatch;
      baseBody: string | null;
      updatedAt: number;
      blockedReason: string | null;
    }
  | {
      key: string;
      scope: string;
      pageId: string;
      op: "delete";
      /** A create request was already in flight when deletion won.  A 404 is
       * not final until that request is known to have settled. */
      pendingCreate: boolean;
      /** Cross-webview safety lease. A different renderer may still be
       * awaiting the POST even though this renderer cannot observe it. */
      pendingCreateUntil: number | null;
      updatedAt: number;
      blockedReason: string | null;
    };

const OWNER_KEY = "orthus.offline.notes.owner.v2";
const OWNER_EVENT = "orthus:offline-note-owner-change";
const OWNER_CHANNEL = "orthus-offline-note-owner-v2";
const DB_NAME = "orthus-offline-notes";
// v2 adds the note_meta_outbox store (share/project/acknowledge mutations).
const DB_VERSION = 2;
const OUTBOX_STORE = "note_outbox";
const META_STORE = "note_meta_outbox";
const CREATE_MARKER_PREFIX = "orthus:v2:note-create:";
const DELETE_MARKER_PREFIX = "orthus:v2:note-delete:";
/** Cross-webview handoff of "open this note in the full app". Owner-scoped so a
 * different account can never resolve to the previous account's note. */
const OPEN_TARGET_BASE = "note-open-target";
const OPEN_TARGET_TTL_MS = 60_000;

const LEGACY_LIST_KEY = "orthus:quick-note-list";
const LEGACY_PAGE_CACHE_PREFIX = "orthus:page-cache:";
const LEGACY_DRAFT_PREFIX = "orthus:page-draft:";

function storageAvailable(): boolean {
  return typeof window !== "undefined";
}

function normalizeOwner(value: unknown): OfflineNoteOwner | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as { userId?: unknown; nodeId?: unknown };
  if (typeof raw.userId !== "string" || !raw.userId) return null;
  if (typeof raw.nodeId !== "string" || !raw.nodeId) return null;
  return { userId: raw.userId, nodeId: raw.nodeId };
}

export function getOfflineNoteOwner(): OfflineNoteOwner | null {
  if (!storageAvailable()) return null;
  try {
    return normalizeOwner(JSON.parse(window.localStorage.getItem(OWNER_KEY) ?? "null"));
  } catch {
    return null;
  }
}

export function offlineNoteScope(owner: OfflineNoteOwner | null = getOfflineNoteOwner()): string | null {
  if (!owner) return null;
  return `${encodeURIComponent(location.origin)}:${encodeURIComponent(owner.nodeId)}:${encodeURIComponent(owner.userId)}`;
}

/**
 * Called only after /auth/me succeeds.  A different account switches the active
 * namespace; it never reads the previous account's list/body/outbox.
 */
export function setOfflineNoteOwner(owner: OfflineNoteOwner): void {
  if (!storageAvailable() || !normalizeOwner(owner)) return;
  try {
    const previous = getOfflineNoteOwner();
    if (previous?.userId === owner.userId && previous.nodeId === owner.nodeId) return;
    window.localStorage.setItem(OWNER_KEY, JSON.stringify(owner));
    publishOwnerChange(owner);
  } catch {
    // Storage-disabled environments simply run online-only.
  }
}

function publishOwnerChange(owner: OfflineNoteOwner | null): void {
  if (!storageAvailable()) return;
  window.dispatchEvent(new CustomEvent(OWNER_EVENT, { detail: owner }));
  if (typeof BroadcastChannel !== "undefined") {
    const channel = new BroadcastChannel(OWNER_CHANNEL);
    channel.postMessage(owner);
    channel.close();
  }
}

/** Lock every renderer to "no active owner" without deleting the prior
 * owner's durable rows. A later verified login can reactivate its namespace. */
export function deactivateOfflineNoteOwner(): void {
  if (!storageAvailable()) return;
  try {
    if (!window.localStorage.getItem(OWNER_KEY)) return;
    window.localStorage.removeItem(OWNER_KEY);
  } finally {
    publishOwnerChange(null);
  }
}

export function subscribeOfflineNoteOwner(
  callback: (owner: OfflineNoteOwner | null) => void,
): () => void {
  if (!storageAvailable()) return () => {};
  const onCustom = (event: Event) => {
    callback((event as CustomEvent<OfflineNoteOwner | null>).detail ?? null);
  };
  const onStorage = (event: StorageEvent) => {
    if (event.key === OWNER_KEY) callback(getOfflineNoteOwner());
  };
  window.addEventListener(OWNER_EVENT, onCustom);
  window.addEventListener("storage", onStorage);
  const channel =
    typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(OWNER_CHANNEL) : null;
  if (channel) channel.onmessage = (event) => callback(normalizeOwner(event.data));
  return () => {
    window.removeEventListener(OWNER_EVENT, onCustom);
    window.removeEventListener("storage", onStorage);
    channel?.close();
  };
}

export function scopedNoteStorageKey(base: string, pageId?: string): string | null {
  const scope = offlineNoteScope();
  if (!scope) return null;
  return `orthus:v2:${base}:${scope}${pageId ? `:${pageId}` : ""}`;
}

/**
 * Hand the full-app "/notes" a target note to open. The quick window and the
 * main window are separate webviews that share the same origin's storage, so an
 * owner-scoped localStorage pointer bridges them without any native/Rust change.
 */
export function setNotesOpenTarget(pageId: string): void {
  const key = scopedNoteStorageKey(OPEN_TARGET_BASE);
  if (!key || !storageAvailable()) return;
  try {
    window.localStorage.setItem(key, JSON.stringify({ pageId, at: Date.now() }));
  } catch {
    // Best-effort UX handoff; the `?note=` URL param is the primary path on web.
  }
}

/** Single-consume read: returns a fresh (<=60s) target page id and clears it so
 * the same handoff never reopens a note twice. */
export function takeNotesOpenTarget(): string | null {
  const key = scopedNoteStorageKey(OPEN_TARGET_BASE);
  if (!key || !storageAvailable()) return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    window.localStorage.removeItem(key);
    const parsed = JSON.parse(raw) as { pageId?: unknown; at?: unknown };
    if (typeof parsed.pageId !== "string" || typeof parsed.at !== "number") return null;
    if (Date.now() - parsed.at > OPEN_TARGET_TTL_MS) return null;
    return parsed.pageId;
  } catch {
    return null;
  }
}

function resolveScope(scope?: string | null): string | null {
  return scope === undefined ? offlineNoteScope() : scope;
}

function markerKey(pageId: string, scopeOverride?: string | null): string | null {
  const scope = resolveScope(scopeOverride);
  return scope ? `${CREATE_MARKER_PREFIX}${scope}:${pageId}` : null;
}

function deleteMarkerKey(pageId: string, scopeOverride?: string | null): string | null {
  const scope = resolveScope(scopeOverride);
  return scope ? `${DELETE_MARKER_PREFIX}${scope}:${pageId}` : null;
}

export function isOfflineNoteCreate(pageId: string): boolean {
  const key = markerKey(pageId);
  if (!key || !storageAvailable()) return false;
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

export function isOfflineNoteDelete(pageId: string): boolean {
  const key = deleteMarkerKey(pageId);
  if (!key || !storageAvailable()) return false;
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

/** The localStorage marker is a tiny write-ahead delete intent. It lets a
 * restart rebuild an IDB tombstone even if the renderer stopped between the
 * two durable writes. */
export function readOfflineNoteDeleteMarkers(
  scopeOverride?: string | null,
): string[] {
  const scope = resolveScope(scopeOverride);
  if (!scope || !storageAvailable()) return [];
  const prefix = `${DELETE_MARKER_PREFIX}${scope}:`;
  const pageIds: string[] = [];
  try {
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (key?.startsWith(prefix) && window.localStorage.getItem(key) === "1") {
        const pageId = key.slice(prefix.length);
        if (pageId) pageIds.push(pageId);
      }
    }
  } catch {
    return [];
  }
  return pageIds;
}

function setCreateMarker(
  pageId: string,
  active: boolean,
  scopeOverride?: string | null,
): void {
  const key = markerKey(pageId, scopeOverride);
  if (!key || !storageAvailable()) return;
  try {
    if (active) window.localStorage.setItem(key, "1");
    else window.localStorage.removeItem(key);
  } catch {
    // IndexedDB remains the primary durable record.
  }
}

function setDeleteMarker(
  pageId: string,
  active: boolean,
  scopeOverride?: string | null,
): void {
  const key = deleteMarkerKey(pageId, scopeOverride);
  if (!key || !storageAvailable()) return;
  try {
    if (active) window.localStorage.setItem(key, "1");
    else window.localStorage.removeItem(key);
  } catch {
    // IndexedDB remains the source of truth; this marker only hides stale list rows.
  }
}

function recordKey(scope: string, pageId: string): string {
  return `${scope}:${pageId}`;
}

function hasIDB(): boolean {
  return storageAvailable() && typeof window.indexedDB !== "undefined";
}

function openDB(): Promise<IDBDatabase> {
  if (!hasIDB()) return Promise.reject(new Error("IndexedDB를 사용할 수 없습니다"));
  return new Promise((resolve, reject) => {
    let request: IDBOpenDBRequest;
    try {
      request = window.indexedDB.open(DB_NAME, DB_VERSION);
    } catch (error) {
      reject(error);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(OUTBOX_STORE)) {
        const store = db.createObjectStore(OUTBOX_STORE, { keyPath: "key" });
        store.createIndex("scope", "scope", { unique: false });
      }
      if (!db.objectStoreNames.contains(META_STORE)) {
        const store = db.createObjectStore(META_STORE, { keyPath: "key" });
        store.createIndex("scope", "scope", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB 열기 실패"));
  });
}

async function runTransaction<T>(
  mode: IDBTransactionMode,
  initial: T,
  work: (store: IDBObjectStore, setResult: (value: T) => void) => void,
  storeName: string = OUTBOX_STORE,
): Promise<T> {
  const db = await openDB();
  try {
    return await new Promise<T>((resolve, reject) => {
      let result = initial;
      let tx: IDBTransaction;
      try {
        tx = db.transaction(storeName, mode);
      } catch (error) {
        reject(error);
        return;
      }
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => reject(tx.error ?? new Error("메모 로컬 저장 실패"));
      tx.onabort = () => reject(tx.error ?? new Error("메모 로컬 저장 중단"));
      try {
        work(tx.objectStore(storeName), (value) => {
          result = value;
        });
      } catch (error) {
        try {
          tx.abort();
        } catch {
          // The original error is more useful.
        }
        reject(error);
      }
    });
  } finally {
    db.close();
  }
}

export async function registerOfflineNoteCreate(
  create: OfflineNoteCreate,
  scopeOverride?: string | null,
  mode: "create" | "recover" = "create",
): Promise<boolean> {
  const scope = resolveScope(scopeOverride);
  if (!scope) throw new Error("로컬 메모 소유자를 확인할 수 없습니다");
  const key = recordKey(scope, create.page_id);
  await runTransaction<boolean>(
    "readwrite",
    false,
    (store, setResult) => {
      const request = store.get(key);
      request.onsuccess = () => {
        const current = request.result as OfflineNoteOutboxRecord | undefined;
        if (current) {
          setResult(current.op === "create");
          return;
        }
        // A recovery snapshot may have become stale while another webview
        // acknowledged it. Never resurrect that snapshot after the row vanished.
        if (mode === "recover") return;
        store.put({
          key,
          scope,
          pageId: create.page_id,
          op: "create",
          create,
          baseBody: "",
          updatedAt: Date.now(),
          blockedReason: null,
        } satisfies OfflineNoteOutboxRecord);
        setResult(true);
      };
    },
  );
  // Re-read after the write. This closes the marker race with a concurrent
  // acknowledge/delete transaction in another webview.
  return reconcileOfflineNoteCreate(create.page_id, scope);
}

export async function reconcileOfflineNoteCreate(
  pageId: string,
  scopeOverride?: string | null,
): Promise<boolean> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return false;
  const key = recordKey(scope, pageId);
  return runTransaction<boolean>("readonly", false, (store, setResult) => {
    const request = store.get(key);
    request.onsuccess = () => {
      const current = request.result as OfflineNoteOutboxRecord | undefined;
      const active = current?.op === "create";
      // Set while the read transaction still orders us against all writers.
      setCreateMarker(pageId, active, scope);
      setResult(active);
    };
  });
}

export async function reconcileOfflineNoteDelete(
  pageId: string,
  scopeOverride?: string | null,
): Promise<boolean> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return false;
  const key = recordKey(scope, pageId);
  return runTransaction<boolean>("readonly", false, (store, setResult) => {
    const request = store.get(key);
    request.onsuccess = () => {
      const current = request.result as OfflineNoteOutboxRecord | undefined;
      const active = current?.op === "delete";
      setDeleteMarker(pageId, active, scope);
      setResult(active);
    };
  });
}

/** Merge a final-value patch. PATCH is idempotent, so only the newest field value
 * per page needs to survive a crash; ordering within one page is unnecessary. */
export async function persistOfflineNotePatch(
  pageId: string,
  patch: OfflineNotePatch,
  baseBody: string | null,
  scopeOverride?: string | null,
): Promise<void> {
  const scope = resolveScope(scopeOverride);
  if (!scope) throw new Error("로컬 메모 소유자를 확인할 수 없습니다");
  const key = recordKey(scope, pageId);
  await runTransaction<void>(
    "readwrite",
    undefined,
    (store) => {
      const request = store.get(key);
      request.onsuccess = () => {
        const current = request.result as OfflineNoteOutboxRecord | undefined;
        if (current?.op === "create") {
          store.put({
            ...current,
            create: { ...current.create, ...patch },
            updatedAt: Date.now(),
            blockedReason: null,
          } satisfies OfflineNoteOutboxRecord);
          return;
        }
        // A late editor callback must never replace a durable deletion.
        if (current?.op === "delete") return;
        store.put({
          key,
          scope,
          pageId,
          op: "update",
          patch: { ...(current?.op === "update" ? current.patch : {}), ...patch },
          baseBody: current?.baseBody ?? baseBody,
          updatedAt: Date.now(),
          blockedReason: null,
        } satisfies OfflineNoteOutboxRecord);
      };
    },
  );
}

export async function readOfflineNoteOutbox(
  scopeOverride?: string | null,
): Promise<OfflineNoteOutboxRecord[]> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return [];
  try {
    return await runTransaction<OfflineNoteOutboxRecord[]>(
      "readonly",
      [],
      (store, setResult) => {
        const index = store.index("scope");
        const request = index.getAll(IDBKeyRange.only(scope));
        request.onsuccess = () => {
          const rows = (request.result as OfflineNoteOutboxRecord[]) ?? [];
          setResult(rows.sort((a, b) => a.updatedAt - b.updatedAt));
        };
      },
    );
  } catch {
    return [];
  }
}

/** Remove only fields that still equal the acknowledged payload.  If the user
 * typed again while the request was in flight, the newer field remains queued. */
export async function acknowledgeOfflineNoteUpdate(
  pageId: string,
  sent: OfflineNotePatch,
  scopeOverride?: string | null,
): Promise<OfflineNotePatch | null> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return null;
  const key = recordKey(scope, pageId);
  return runTransaction<OfflineNotePatch | null>(
    "readwrite",
    null,
    (store, setResult) => {
      const request = store.get(key);
      request.onsuccess = () => {
        const current = request.result as OfflineNoteOutboxRecord | undefined;
        if (!current || current.op !== "update") {
          setResult(null);
          return;
        }
        const remaining = { ...current.patch };
        for (const field of ["title", "icon", "body"] as const) {
          if (field in sent && remaining[field] === sent[field]) delete remaining[field];
        }
        if (Object.keys(remaining).length === 0) store.delete(key);
        else store.put({ ...current, patch: remaining, updatedAt: Date.now() });
        setResult(Object.keys(remaining).length ? remaining : null);
      };
    },
  );
}

/** A successful POST makes the page server-backed.  Return any values typed
 * during the request so pageSave can follow with one normal PATCH. */
export async function acknowledgeOfflineNoteCreate(
  pageId: string,
  sent: OfflineNoteCreate,
  scopeOverride?: string | null,
): Promise<OfflineNotePatch | null> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return null;
  const key = recordKey(scope, pageId);
  const remaining = await runTransaction<OfflineNotePatch | null>(
    "readwrite",
    null,
    (store, setResult) => {
      const request = store.get(key);
      request.onsuccess = () => {
        const current = request.result as OfflineNoteOutboxRecord | undefined;
        if (!current) {
          setResult(null);
          return;
        }
        if (current.op === "update") {
          // Another webview already acknowledged the idempotent create and
          // converted newer typing into an update. Help flush it here too.
          setResult(current.patch);
          return;
        }
        if (current.op === "delete") {
          setResult(null);
          return;
        }
        const patch: OfflineNotePatch = {};
        for (const field of ["title", "icon", "body"] as const) {
          if (current.create[field] !== sent[field]) {
            (patch as Record<string, unknown>)[field] = current.create[field];
          }
        }
        if (Object.keys(patch).length === 0) store.delete(key);
        else {
          store.put({
            key,
            scope,
            pageId,
            op: "update",
            patch,
            baseBody: sent.body,
            updatedAt: Date.now(),
            blockedReason: null,
          } satisfies OfflineNoteOutboxRecord);
        }
        setResult(Object.keys(patch).length ? patch : null);
      };
    },
  );
  await reconcileOfflineNoteCreate(pageId, scope);
  return remaining;
}

export async function blockOfflineNote(
  pageId: string,
  reason: string,
  scopeOverride?: string | null,
): Promise<void> {
  const scope = resolveScope(scopeOverride);
  if (!scope) throw new Error("로컬 메모 소유자를 확인할 수 없습니다");
  const key = recordKey(scope, pageId);
  await runTransaction<void>(
    "readwrite",
    undefined,
    (store) => {
      const request = store.get(key);
      request.onsuccess = () => {
        const current = request.result as OfflineNoteOutboxRecord | undefined;
        if (current) store.put({ ...current, blockedReason: reason, updatedAt: Date.now() });
      };
    },
  );
}

export async function cancelOfflineNote(
  pageId: string,
  scopeOverride?: string | null,
): Promise<void> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return;
  const key = recordKey(scope, pageId);
  await runTransaction<void>("readwrite", undefined, (store) => {
    store.delete(key);
  });
  await reconcileOfflineNoteCreate(pageId, scope);
  await reconcileOfflineNoteDelete(pageId, scope);
}

export async function tombstoneOfflineNoteDelete(
  pageId: string,
  pendingCreate: boolean,
  scopeOverride?: string | null,
): Promise<void> {
  const scope = resolveScope(scopeOverride);
  if (!scope) throw new Error("로컬 메모 소유자를 확인할 수 없습니다");
  const key = recordKey(scope, pageId);
  // Write-ahead: if the renderer stops before IDB commit/oncomplete, startup
  // reconstructs this owner-scoped intent instead of flashing a deleted row.
  setDeleteMarker(pageId, true, scope);
  await runTransaction<void>("readwrite", undefined, (store) => {
    const request = store.get(key);
    request.onsuccess = () => {
      const current = request.result as OfflineNoteOutboxRecord | undefined;
      const now = Date.now();
      const hasPendingCreate =
        pendingCreate ||
        current?.op === "create" ||
        (current?.op === "delete" && current.pendingCreate);
      store.put({
        key,
        scope,
        pageId,
        op: "delete",
        pendingCreate: hasPendingCreate,
        pendingCreateUntil: hasPendingCreate
          ? Math.max(
              now + 120_000,
              current?.op === "delete" ? (current.pendingCreateUntil ?? 0) : 0,
            )
          : null,
        updatedAt: now,
        blockedReason: null,
      } satisfies OfflineNoteOutboxRecord);
    };
  });
  await reconcileOfflineNoteCreate(pageId, scope);
  await reconcileOfflineNoteDelete(pageId, scope);
}

export async function settleOfflineNoteDeleteCreate(
  pageId: string,
  scopeOverride?: string | null,
): Promise<void> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return;
  const key = recordKey(scope, pageId);
  await runTransaction<void>("readwrite", undefined, (store) => {
    const request = store.get(key);
    request.onsuccess = () => {
      const current = request.result as OfflineNoteOutboxRecord | undefined;
      if (current?.op === "delete" && current.pendingCreate) {
        store.put({
          ...current,
          pendingCreate: false,
          pendingCreateUntil: null,
          updatedAt: Date.now(),
        });
      }
    };
  });
}

/** A renderer with a live create request renews the delete tombstone lease.
 * If that renderer crashes, renewal stops and recovery may safely delete after
 * the last lease expires. */
export async function renewOfflineNoteDeleteCreateLease(
  pageId: string,
  pendingCreateUntil: number,
  scopeOverride?: string | null,
): Promise<void> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return;
  const key = recordKey(scope, pageId);
  await runTransaction<void>("readwrite", undefined, (store) => {
    const request = store.get(key);
    request.onsuccess = () => {
      const current = request.result as OfflineNoteOutboxRecord | undefined;
      if (current?.op === "delete" && current.pendingCreate) {
        store.put({
          ...current,
          pendingCreateUntil: Math.max(
            pendingCreateUntil,
            current.pendingCreateUntil ?? 0,
          ),
        });
      }
    };
  });
}

export async function acknowledgeOfflineNoteDelete(
  pageId: string,
  scopeOverride?: string | null,
): Promise<void> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return;
  const key = recordKey(scope, pageId);
  await runTransaction<void>("readwrite", undefined, (store) => {
    const request = store.get(key);
    request.onsuccess = () => {
      const current = request.result as OfflineNoteOutboxRecord | undefined;
      if (current?.op === "delete") store.delete(key);
    };
  });
  await reconcileOfflineNoteDelete(pageId, scope);
}

// ---- Note meta outbox (share / project link / acknowledge) ----------------
//
// Separate from the body/create/delete outbox above: a note has many meta
// mutations (share with several people, set projects, acknowledge) whereas the
// body outbox holds exactly one create|update|delete per page. All target
// endpoints are idempotent (upsert / full-replace / set-timestamp), so the
// durable record stores the *desired end state* per natural key and replay is
// safe. Latest-wins collapses an offline burst of toggles to one final call.

export type NoteMetaMutation =
  | { kind: "projects"; projectIds: string[] }
  | {
      kind: "share";
      userId: string;
      /** true = share with this user at `permission`; false = unshare. */
      present: boolean;
      permission: "view" | "edit";
      /** Known server share id (needed to replay an unshare); null if the share
       * was only ever queued offline and never reached the server. */
      shareId: string | null;
    }
  | { kind: "acknowledge" };

export interface NoteMetaRecord {
  key: string;
  scope: string;
  pageId: string;
  mutation: NoteMetaMutation;
  updatedAt: number;
}

function metaNaturalKey(scope: string, pageId: string, mutation: NoteMetaMutation): string {
  switch (mutation.kind) {
    case "projects":
      return `${scope}:${pageId}:projects`;
    case "share":
      return `${scope}:${pageId}:share:${mutation.userId}`;
    case "acknowledge":
      return `${scope}:${pageId}:ack`;
  }
}

/** Queue (or overwrite) a note meta mutation. Latest desired-state per natural
 * key wins, so repeated offline toggles never stack up. */
export async function enqueueNoteMeta(
  pageId: string,
  mutation: NoteMetaMutation,
  scopeOverride?: string | null,
): Promise<void> {
  const scope = resolveScope(scopeOverride);
  if (!scope) throw new Error("로컬 메모 소유자를 확인할 수 없습니다");
  const key = metaNaturalKey(scope, pageId, mutation);
  await runTransaction<void>(
    "readwrite",
    undefined,
    (store) => {
      store.put({ key, scope, pageId, mutation, updatedAt: Date.now() } satisfies NoteMetaRecord);
    },
    META_STORE,
  );
}

export async function readNoteMetaOutbox(
  scopeOverride?: string | null,
): Promise<NoteMetaRecord[]> {
  const scope = resolveScope(scopeOverride);
  if (!scope) return [];
  try {
    return await runTransaction<NoteMetaRecord[]>(
      "readonly",
      [],
      (store, setResult) => {
        const index = store.index("scope");
        const request = index.getAll(IDBKeyRange.only(scope));
        request.onsuccess = () => {
          const rows = (request.result as NoteMetaRecord[]) ?? [];
          setResult(rows.sort((a, b) => a.updatedAt - b.updatedAt));
        };
      },
      META_STORE,
    );
  } catch {
    return [];
  }
}

/** Pending meta mutations for one page (drives the "동기화 대기" UI). */
export async function readNoteMetaForPage(
  pageId: string,
  scopeOverride?: string | null,
): Promise<NoteMetaRecord[]> {
  const records = await readNoteMetaOutbox(scopeOverride);
  return records.filter((record) => record.pageId === pageId);
}

export async function removeNoteMeta(key: string): Promise<void> {
  await runTransaction<void>(
    "readwrite",
    undefined,
    (store) => {
      store.delete(key);
    },
    META_STORE,
  ).catch(() => undefined);
}

async function clearNoteMetaOutboxScope(scope: string): Promise<void> {
  await runTransaction<void>(
    "readwrite",
    undefined,
    (store) => {
      const index = store.index("scope");
      const request = index.openKeyCursor(IDBKeyRange.only(scope));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        store.delete(cursor.primaryKey);
        cursor.continue();
      };
    },
    META_STORE,
  ).catch(() => undefined);
}

/** Migrate only rows whose owner was verified by the live server response.
 * Unknown/shared/legacy-public rows stay quarantined under unread v1 keys. */
export function migrateLegacyOwnedNoteCaches(
  rows: Array<{ page_id: string; owner_id?: string | null }>,
): void {
  const owner = getOfflineNoteOwner();
  if (!owner || !storageAvailable()) return;
  try {
    for (const row of rows) {
      if (row.owner_id !== owner.userId) continue;
      for (const [legacyPrefix, nextBase] of [
        [LEGACY_PAGE_CACHE_PREFIX, "page-cache"],
        [LEGACY_DRAFT_PREFIX, "page-draft"],
      ] as const) {
        const legacyKey = `${legacyPrefix}${row.page_id}`;
        const raw = window.localStorage.getItem(legacyKey);
        const nextKey = scopedNoteStorageKey(nextBase, row.page_id);
        if (!raw || !nextKey) continue;
        if (window.localStorage.getItem(nextKey) === null) {
          window.localStorage.setItem(nextKey, raw);
        }
        window.localStorage.removeItem(legacyKey);
      }
    }
  } catch {
    // Leave the legacy copy in place if quota/storage access fails.
  }
}

function purgeLegacyUnscopedNoteStorage(): void {
  if (!storageAvailable()) return;
  try {
    window.localStorage.removeItem(LEGACY_LIST_KEY);
    const doomed: string[] = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i);
      if (
        key &&
        !key.startsWith("orthus:v2:") &&
        (key.startsWith(LEGACY_PAGE_CACHE_PREFIX) || key.startsWith(LEGACY_DRAFT_PREFIX))
      ) {
        doomed.push(key);
      }
    }
    for (const key of doomed) window.localStorage.removeItem(key);
  } catch {
    // Best-effort migration: legacy keys are never read by v2 regardless.
  }
}

/** Logout boundary.  Remove the active owner's cached content and durable queue
 * before dropping the owner pointer so another account cannot address it. */
export async function clearOfflineNoteStorage(): Promise<void> {
  const scope = offlineNoteScope();
  if (storageAvailable()) {
    try {
      if (scope) {
        const prefix = "orthus:v2:";
        const doomed: string[] = [];
        for (let i = 0; i < window.localStorage.length; i += 1) {
          const key = window.localStorage.key(i);
          if (key && key.startsWith(prefix) && key.includes(scope)) doomed.push(key);
        }
        for (const key of doomed) window.localStorage.removeItem(key);
      }
      purgeLegacyUnscopedNoteStorage();
    } catch {
      // Continue with IndexedDB cleanup.
    }
  }
  try {
    if (scope) {
      await runTransaction<void>("readwrite", undefined, (store) => {
        const index = store.index("scope");
        const request = index.openKeyCursor(IDBKeyRange.only(scope));
        request.onsuccess = () => {
          const cursor = request.result;
          if (!cursor) return;
          store.delete(cursor.primaryKey);
          cursor.continue();
        };
      });
      await clearNoteMetaOutboxScope(scope);
    }
  } finally {
    deactivateOfflineNoteOwner();
  }
}
