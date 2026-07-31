"use client";

/**
 * Replay engine for the note meta outbox (share / project link / acknowledge).
 *
 * The storage lives in `offline-notes.ts`; this module owns the network replay
 * and the reconnect wiring. Every target endpoint is idempotent (share upsert,
 * project full-replace, acknowledge set-timestamp), so a record can be replayed
 * safely after a partial apply. A record whose page is still an un-synced
 * offline create is skipped until the create reaches the server (its meta write
 * would otherwise 404).
 */

import { ApiError, api } from "@/lib/api";
import { isNetworkError, isTransientServerError } from "@/lib/offline-board-outbox";
import { subscribePageSaveState } from "@/lib/pageSave";
import {
  enqueueNoteMeta,
  isOfflineNoteCreate,
  offlineNoteScope,
  readNoteMetaForPage,
  readNoteMetaOutbox,
  removeNoteMeta,
  type NoteMetaMutation,
  type NoteMetaRecord,
} from "@/lib/offline-notes";

const listeners = new Set<() => void>();

/** Notified when the pending set changes (enqueue or a flush settled it). */
export function subscribeNoteMetaPending(callback: () => void): () => void {
  listeners.add(callback);
  return () => {
    listeners.delete(callback);
  };
}

function notifyPending(): void {
  for (const callback of [...listeners]) {
    try {
      callback();
    } catch {
      // A misbehaving subscriber must not wedge the flush loop.
    }
  }
}

export async function pendingNoteMetaForPage(pageId: string): Promise<NoteMetaRecord[]> {
  return readNoteMetaForPage(pageId);
}

/** Queue a meta mutation and immediately try to flush it (no-op offline). */
export async function queueNoteMeta(pageId: string, mutation: NoteMetaMutation): Promise<void> {
  await enqueueNoteMeta(pageId, mutation);
  notifyPending();
  void flushNoteMetaOutbox();
}

async function applyMeta(record: NoteMetaRecord): Promise<void> {
  const mutation = record.mutation;
  if (mutation.kind === "projects") {
    await api.setNoteProjects(record.pageId, mutation.projectIds);
    return;
  }
  if (mutation.kind === "acknowledge") {
    await api.acknowledgeNote(record.pageId);
    return;
  }
  // share
  if (mutation.present) {
    await api.addNoteShares(record.pageId, {
      shared_with: [mutation.userId],
      permission: mutation.permission,
    });
  } else if (mutation.shareId) {
    await api.deleteNoteShare(record.pageId, mutation.shareId);
  }
  // present=false && shareId=null → a share that was only ever queued offline and
  // then removed before syncing; nothing to undo on the server.
}

let flushing = false;

/**
 * Replay every pending meta mutation for the active owner, oldest first.
 * Idempotent and re-entrant-safe. Returns the number of records settled.
 */
export async function flushNoteMetaOutbox(): Promise<number> {
  if (flushing) return 0;
  if (typeof navigator !== "undefined" && navigator.onLine === false) return 0;
  if (!offlineNoteScope()) return 0;
  flushing = true;
  let settled = 0;
  try {
    const records = await readNoteMetaOutbox();
    for (const record of records) {
      // The page must exist server-side before its meta writes land; a still
      // pending offline create would 404. A later flush (after the create syncs)
      // picks it up.
      if (isOfflineNoteCreate(record.pageId)) continue;
      try {
        await applyMeta(record);
        await removeNoteMeta(record.key);
        settled += 1;
      } catch (error) {
        if (isNetworkError(error) || isTransientServerError(error)) {
          // Went offline again / server hiccup — keep queued and stop; the next
          // online/visibility event retries from the top.
          break;
        }
        if (error instanceof ApiError && error.status === 404) {
          // The note or share was removed elsewhere — the desired end state is
          // already reached; drop the record.
          await removeNoteMeta(record.key);
          settled += 1;
          continue;
        }
        // Any other app error (e.g. 403 lost write permission) — drop it so one
        // poison record can never wedge the queue.
        await removeNoteMeta(record.key);
        settled += 1;
      }
    }
  } finally {
    flushing = false;
    if (settled > 0) notifyPending();
  }
  return settled;
}

let wired = false;
let saveSettleTimer: ReturnType<typeof setTimeout> | null = null;

/** Register the reconnect/refocus flush listeners exactly once. */
export function ensureNoteMetaSyncWiring(): void {
  if (wired || typeof window === "undefined") return;
  wired = true;
  window.addEventListener("online", () => void flushNoteMetaOutbox());
  window.addEventListener("focus", () => void flushNoteMetaOutbox());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void flushNoteMetaOutbox();
  });
  // A meta mutation on a still-offline-created note is skipped by the flush
  // (its PATCH/POST would 404 until the create lands). The create settles
  // through pageSave, not an online/focus event, so retry on save-state
  // changes too — otherwise the queued share/project would wait for the next
  // focus. Debounced so a "saving"→"saved" burst triggers a single flush.
  subscribePageSaveState(() => {
    if (saveSettleTimer) return;
    saveSettleTimer = setTimeout(() => {
      saveSettleTimer = null;
      void flushNoteMetaOutbox();
    }, 300);
  });
}
