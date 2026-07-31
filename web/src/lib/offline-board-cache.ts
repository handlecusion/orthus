// Owner-scoped offline read cache for the personal board (1단계).
//
// Stores the last-loaded `PersonalBoardBootstrap` snapshot in IndexedDB so the
// board can paint instantly and survive a dropped connection / deploy blip
// (stale-while-revalidate). Read-only: writes still go to the server. Offline
// editing + sync is 2단계 (docs/offline-personal-board.md).
//
// Owner scoping: snapshots are namespaced by the authenticated user_id. The
// last-known user_id is mirrored to localStorage so the correct namespace is
// available even before /auth/me resolves (or while offline). Cleared on logout.

import type { PersonalBoardBootstrap } from "@/lib/api";

const DB_NAME = "orthus-offline";
const DB_VERSION = 1;
const STORE = "personal_board";
const OWNER_KEY = "orthus.offline.board.owner";

export interface CachedBoardSnapshot {
  ownerId: string;
  snapshot: PersonalBoardBootstrap;
  syncedAt: number;
}

function hasIDB(): boolean {
  return typeof window !== "undefined" && typeof window.indexedDB !== "undefined";
}

export function setOfflineBoardOwner(userId: string): void {
  if (typeof window === "undefined" || !userId) return;
  try {
    window.localStorage.setItem(OWNER_KEY, userId);
  } catch {
    /* storage disabled — degrade to no cache */
  }
}

export function getOfflineBoardOwner(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(OWNER_KEY);
  } catch {
    return null;
  }
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
        db.createObjectStore(STORE, { keyPath: "ownerId" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => resolve(null);
  });
}

/**
 * Persist the latest board snapshot for the current owner (best-effort).
 * `syncedAt` defaults to now (a fresh server load); pass the prior value to
 * persist optimistic offline edits without resetting the last-synced time.
 */
export async function writeBoardSnapshot(
  snapshot: PersonalBoardBootstrap,
  syncedAt: number = Date.now(),
): Promise<void> {
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
      tx.objectStore(STORE).put({ ownerId, snapshot, syncedAt });
    });
  } finally {
    db.close();
  }
}

/** Read the latest cached snapshot for the current owner, or null. */
export async function readBoardSnapshot(): Promise<CachedBoardSnapshot | null> {
  const ownerId = getOfflineBoardOwner();
  if (!ownerId) return null;
  const db = await openDB();
  if (!db) return null;
  try {
    return await new Promise<CachedBoardSnapshot | null>((resolve) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(ownerId);
      req.onsuccess = () => {
        const value = req.result as CachedBoardSnapshot | undefined;
        resolve(value && value.ownerId === ownerId ? value : null);
      };
      req.onerror = () => resolve(null);
    });
  } finally {
    db.close();
  }
}

/** Drop all cached board data and the owner pointer (call on logout). */
export async function clearOfflineBoardCache(): Promise<void> {
  if (typeof window !== "undefined") {
    try {
      window.localStorage.removeItem(OWNER_KEY);
    } catch {
      /* ignore */
    }
  }
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
