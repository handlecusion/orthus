// Offline mutation outbox + sync engine for the personal board (2단계).
//
// When a board write fails because the device is offline, the mutation is
// applied optimistically in the UI (existing behavior) and recorded here. On
// reconnect the queue is replayed against the server in FIFO order. Mutations
// carry client-generated ids and the server creates are idempotent
// (docs/offline-personal-board.md), so replaying is safe even if a mutation was
// partially applied before the connection dropped.
//
// Owner-scoped: records are namespaced by the authenticated user_id (shared with
// offline-board-cache). Cleared on logout.

import { ApiError, type PersonalTaskCreateInput, type PersonalTaskPatchInput } from "@/lib/api";
import { getOfflineBoardOwner } from "@/lib/offline-board-cache";

const DB_NAME = "orthus-offline-outbox";
const DB_VERSION = 1;
const STORE = "board_outbox";

export type BoardMutation =
  | { kind: "create_task"; clientId: string; input: PersonalTaskCreateInput }
  | { kind: "update_task"; taskId: string; patch: PersonalTaskPatchInput }
  | { kind: "delete_task"; taskId: string };

interface OutboxRecord {
  id: string;
  ownerId: string;
  seq: number;
  mutation: BoardMutation;
  enqueuedAt: number;
}

/** A network-layer failure (offline / upstream unreachable) vs an app error. */
export function isNetworkError(error: unknown): boolean {
  if (typeof navigator !== "undefined" && navigator.onLine === false) return true;
  // fetch() rejects with a TypeError on network failure (the api client rethrows it).
  return error instanceof TypeError;
}

// A transient server-side failure (5xx, or a non-gated 503 that survived the
// api client's own retry budget) — the mutation never really landed as a
// permanent rejection, so it's network-like and should retry rather than be
// dropped. create/update are idempotent (server generates ids from clientId),
// so retrying is safe.
export function isTransientServerError(error: unknown): boolean {
  return error instanceof ApiError && error.status >= 500;
}

function hasIDB(): boolean {
  return typeof window !== "undefined" && typeof window.indexedDB !== "undefined";
}

function newId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

function openDB(): Promise<IDBDatabase | null> {
  if (!hasIDB()) return Promise.resolve(null);
  return new Promise((resolve) => {
    let req: IDBOpenDBRequest;
    try {
      req = window.indexedDB.open(DB_NAME, DB_VERSION);
    } catch {
      resolve(null);
      return;
    }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const store = db.createObjectStore(STORE, { keyPath: "id" });
        store.createIndex("seq", "seq", { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => resolve(null);
  });
}

/** A monotonic-ish sequence so replay preserves enqueue order. */
function nextSeq(): number {
  return Date.now() * 1000 + Math.floor(Math.random() * 1000);
}

/** Queue a board mutation for later replay (call after the optimistic UI update). */
export async function enqueueBoardMutation(mutation: BoardMutation): Promise<void> {
  const ownerId = getOfflineBoardOwner();
  if (!ownerId) return;
  const db = await openDB();
  if (!db) return;
  try {
    await new Promise<void>((resolve) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
      tx.onabort = () => resolve();
      tx.objectStore(STORE).put({
        id: newId(),
        ownerId,
        seq: nextSeq(),
        mutation,
        enqueuedAt: Date.now(),
      } satisfies OutboxRecord);
    });
  } finally {
    db.close();
  }
}

async function readPending(ownerId: string): Promise<OutboxRecord[]> {
  const db = await openDB();
  if (!db) return [];
  try {
    const all = await new Promise<OutboxRecord[]>((resolve) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).getAll();
      req.onsuccess = () => resolve((req.result as OutboxRecord[]) ?? []);
      req.onerror = () => resolve([]);
    });
    return all.filter((r) => r.ownerId === ownerId).sort((a, b) => a.seq - b.seq);
  } finally {
    db.close();
  }
}

async function removeRecord(id: string): Promise<void> {
  const db = await openDB();
  if (!db) return;
  try {
    await new Promise<void>((resolve) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
      tx.onabort = () => resolve();
      tx.objectStore(STORE).delete(id);
    });
  } finally {
    db.close();
  }
}

/** Number of mutations waiting to sync for the current owner. */
export async function pendingMutationCount(): Promise<number> {
  const ownerId = getOfflineBoardOwner();
  if (!ownerId) return 0;
  return (await readPending(ownerId)).length;
}

export interface FlushHandlers {
  create_task: (m: Extract<BoardMutation, { kind: "create_task" }>) => Promise<void>;
  update_task: (m: Extract<BoardMutation, { kind: "update_task" }>) => Promise<void>;
  delete_task: (m: Extract<BoardMutation, { kind: "delete_task" }>) => Promise<void>;
}

export interface FlushResult {
  flushed: number;
  remaining: number;
  stoppedOffline: boolean;
}

let flushing = false;

/**
 * Replay queued mutations in order. Stops at the first network error or
 * transient server error (5xx / non-gated 503) and stays queued for the next
 * attempt. App-level errors (4xx) are treated as permanently-failed and
 * dropped so a poison mutation never blocks the queue; delete-on-missing
 * (404) is success.
 */
export async function flushOutbox(handlers: FlushHandlers): Promise<FlushResult> {
  const ownerId = getOfflineBoardOwner();
  if (!ownerId || flushing) return { flushed: 0, remaining: 0, stoppedOffline: false };
  flushing = true;
  let flushed = 0;
  try {
    const pending = await readPending(ownerId);
    for (const record of pending) {
      try {
        const m = record.mutation;
        if (m.kind === "create_task") await handlers.create_task(m);
        else if (m.kind === "update_task") await handlers.update_task(m);
        else await handlers.delete_task(m);
        await removeRecord(record.id);
        flushed += 1;
      } catch (error) {
        if (isNetworkError(error) || isTransientServerError(error)) {
          const remaining = (await readPending(ownerId)).length;
          return { flushed, remaining, stoppedOffline: true };
        }
        // App-level failure (validation/conflict) — drop so it can't wedge the
        // queue. The next bootstrap reconciles authoritative server state.
        await removeRecord(record.id);
        flushed += 1;
      }
    }
    return { flushed, remaining: 0, stoppedOffline: false };
  } finally {
    flushing = false;
  }
}

/** Drop the whole outbox (call on logout). */
export async function clearOutbox(): Promise<void> {
  const db = await openDB();
  if (!db) return;
  try {
    await new Promise<void>((resolve) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
      tx.onabort = () => resolve();
      tx.objectStore(STORE).clear();
    });
  } finally {
    db.close();
  }
}
