"use client";

/* 메모 SidePeek 상단 메타 패널: 프로젝트 링크 · 팀 공유 · 얼라인 확인.
 * docs/personal-notes.md. 회사 노드(owner-scope) 전용 표면이므로 팀원/프로젝트
 * 목록은 회사 대시보드 디렉터리를 재사용한다. */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type DashboardProject,
  type NoteProjectLink,
  type NoteShare,
  type TeamMember,
} from "@/lib/api";
import { isNetworkError, isTransientServerError } from "@/lib/offline-board-outbox";
import { scopedNoteStorageKey } from "@/lib/offline-notes";
import {
  ensureNoteMetaSyncWiring,
  pendingNoteMetaForPage,
  queueNoteMeta,
  subscribeNoteMetaPending,
} from "@/lib/note-meta-sync";
import { PeekSection } from "@/components/dashboard/peek";
import { useAnchoredPopover, anchoredStyle } from "@/components/dashboard/kit";

/** A failure that means "not applied, retry later" rather than "rejected". */
function shouldQueue(error: unknown): boolean {
  return isNetworkError(error) || isTransientServerError(error);
}

function readJsonCache<T>(key: string | null): T | null {
  if (!key || typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeJsonCache(key: string | null, value: unknown): void {
  if (!key || typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Best-effort offline mirror; the server remains the source of truth.
  }
}

function isPendingShareId(shareId: string): boolean {
  return shareId.startsWith("pending:");
}

function dot(color: string | null | undefined) {
  return (
    <span
      className="inline-block h-2 w-2 shrink-0 rounded-full"
      style={{ background: color || "var(--color-text-faint)" }}
      aria-hidden
    />
  );
}

export function NoteMetaPanel({
  pageId,
  canWrite,
  recipient,
  embedded = false,
  onAcknowledged,
  onProjectsChanged,
  onSharesChanged,
}: {
  pageId: string;
  canWrite: boolean;
  /** 메모장 편집기 disclosure 안에서는 바깥 카드 chrome을 부모가 담당한다. */
  embedded?: boolean;
  /** 공유받은 메모로 열었을 때(수신자 관점). null이면 소유자/공용 관점. */
  recipient?: { acknowledged_at: string | null } | null;
  onAcknowledged?: () => void;
  /** 전체 메모 목록의 project facet을 optimistic state와 즉시 맞춘다. */
  onProjectsChanged?: (pageId: string, links: NoteProjectLink[]) => void;
  /** 전체 메모 목록의 공유 인원/확인 수를 즉시 맞춘다. */
  onSharesChanged?: (pageId: string, shares: NoteShare[]) => void;
}) {
  // Seed from the owner-scoped cache so an offline panel shows the last-known
  // state and can still pick projects/members. The panel only mounts after a
  // client interaction (keyed by page_id), so a lazy initializer is SSR-safe.
  const [links, setLinks] = useState<NoteProjectLink[]>(
    () => readJsonCache<NoteProjectLink[]>(scopedNoteStorageKey("note-links", pageId)) ?? [],
  );
  const [shares, setShares] = useState<NoteShare[]>(
    () => readJsonCache<NoteShare[]>(scopedNoteStorageKey("note-shares", pageId)) ?? [],
  );
  const [projects, setProjects] = useState<DashboardProject[]>(
    () =>
      (readJsonCache<DashboardProject[]>(scopedNoteStorageKey("dir-projects")) ?? []).filter(
        (x) => x.active,
      ),
  );
  const [members, setMembers] = useState<TeamMember[]>(
    () => readJsonCache<TeamMember[]>(scopedNoteStorageKey("dir-members")) ?? [],
  );
  const [acked, setAcked] = useState<boolean>(recipient?.acknowledged_at != null);
  const [ackKey, setAckKey] = useState<string>(pageId);
  const [busy, setBusy] = useState(false);
  const [pendingSync, setPendingSync] = useState(0);
  const projectMutationInFlight = useRef(false);

  // pageId(또는 수신자 컨텍스트)가 바뀌면 acked를 prop에서 다시 파생한다.
  // (render 중 setState — "prop 변화에 상태 맞추기" 공인 패턴, effect setState 회피.)
  const ackToken = `${pageId}:${recipient?.acknowledged_at ?? ""}`;
  if (ackKey !== ackToken) {
    setAckKey(ackToken);
    setAcked(recipient?.acknowledged_at != null);
  }

  const {
    anchorRef: projAnchor,
    popRef: projPopRef,
    rect: projRect,
    open: projOpen,
    toggle: projToggle,
  } = useAnchoredPopover();
  const {
    anchorRef: shareAnchor,
    popRef: sharePopRef,
    rect: shareRect,
    open: shareOpen,
    toggle: shareToggle,
  } = useAnchoredPopover();

  // Per-page + directory caches so an offline panel shows the last-known state
  // and can still pick projects/members. Server response overwrites the cache.
  const reloadLinks = useCallback(() => {
    const cacheKey = scopedNoteStorageKey("note-links", pageId);
    return api
      .listNoteProjects(pageId)
      .then((rows) => {
        setLinks(rows);
        onProjectsChanged?.(pageId, rows);
        writeJsonCache(cacheKey, rows);
      })
      .catch(() => undefined);
  }, [onProjectsChanged, pageId]);
  const reloadShares = useCallback(() => {
    const cacheKey = scopedNoteStorageKey("note-shares", pageId);
    return api
      .listNoteShares(pageId)
      .then((rows) => {
        setShares(rows);
        onSharesChanged?.(pageId, rows);
        writeJsonCache(cacheKey, rows);
      })
      .catch(() => undefined);
  }, [onSharesChanged, pageId]);

  const refreshPending = useCallback(
    () =>
      pendingNoteMetaForPage(pageId)
        .then((rows) => setPendingSync(rows.length))
        .catch(() => undefined),
    [pageId],
  );

  // Reconcile the cached state against the server (setState lives in the .then
  // callbacks, so no synchronous setState-in-effect).
  useEffect(() => {
    void reloadLinks();
    void reloadShares();
    void refreshPending();
  }, [pageId, reloadLinks, reloadShares, refreshPending]);

  useEffect(() => {
    const projKey = scopedNoteStorageKey("dir-projects");
    const memKey = scopedNoteStorageKey("dir-members");
    api
      .listDashboardProjects()
      .then((p) => {
        const active = p.filter((x) => x.active);
        setProjects(active);
        writeJsonCache(projKey, active);
      })
      .catch(() => undefined);
    api
      .listTeamMembers()
      .then((m) => {
        setMembers(m);
        writeJsonCache(memKey, m);
      })
      .catch(() => undefined);
  }, [pageId]);

  // Reconnect: a settled flush changed server state → reconcile the lists.
  useEffect(() => {
    ensureNoteMetaSyncWiring();
    return subscribeNoteMetaPending(() => {
      void refreshPending();
      if (typeof navigator !== "undefined" && navigator.onLine) {
        void reloadLinks();
        void reloadShares();
      }
    });
  }, [refreshPending, reloadLinks, reloadShares]);

  const linkedIds = new Set(links.map((l) => l.project_id));
  const sharedUserIds = new Set(shares.map((s) => s.shared_with));

  async function toggleProject(project: DashboardProject) {
    if (projectMutationInFlight.current) return;
    projectMutationInFlight.current = true;
    const had = linkedIds.has(project.project_id);
    const nextIds = had
      ? links.filter((l) => l.project_id !== project.project_id).map((l) => l.project_id)
      : [...links.map((l) => l.project_id), project.project_id];
    const previous = links;
    const optimistic: NoteProjectLink[] = had
      ? links.filter((l) => l.project_id !== project.project_id)
      : [...links, { project_id: project.project_id, name: project.name, color: project.color }];
    setLinks(optimistic);
    onProjectsChanged?.(pageId, optimistic);
    setBusy(true);
    try {
      const updated = await api.setNoteProjects(pageId, nextIds);
      setLinks(updated);
      onProjectsChanged?.(pageId, updated);
      writeJsonCache(scopedNoteStorageKey("note-links", pageId), updated);
    } catch (error) {
      if (shouldQueue(error)) {
        await queueNoteMeta(pageId, { kind: "projects", projectIds: nextIds });
        writeJsonCache(scopedNoteStorageKey("note-links", pageId), optimistic);
      } else {
        setLinks(previous);
        onProjectsChanged?.(pageId, previous);
      }
    } finally {
      projectMutationInFlight.current = false;
      setBusy(false);
    }
  }

  async function shareWith(member: TeamMember) {
    if (!member.user_id) return;
    const optimistic: NoteShare = {
      share_id: `pending:${member.user_id}`,
      page_id: pageId,
      shared_with: member.user_id,
      shared_with_name: member.name,
      shared_with_color: member.color ?? null,
      permission: "view",
      created_by: "",
      acknowledged_at: null,
      created_at: new Date().toISOString(),
    };
    const previous = shares;
    const optimisticShares = [...shares, optimistic];
    setShares(optimisticShares);
    onSharesChanged?.(pageId, optimisticShares);
    setBusy(true);
    try {
      const updated = await api.addNoteShares(pageId, {
        shared_with: [member.user_id],
        permission: "view",
      });
      setShares(updated);
      onSharesChanged?.(pageId, updated);
      writeJsonCache(scopedNoteStorageKey("note-shares", pageId), updated);
    } catch (error) {
      if (shouldQueue(error)) {
        await queueNoteMeta(pageId, {
          kind: "share",
          userId: member.user_id,
          present: true,
          permission: "view",
          shareId: null,
        });
      } else {
        setShares(previous);
        onSharesChanged?.(pageId, previous);
      }
    } finally {
      setBusy(false);
    }
  }

  async function setSharePermission(share: NoteShare, permission: "view" | "edit") {
    const previous = shares;
    const optimisticShares = shares.map((s) =>
      s.share_id === share.share_id ? { ...s, permission } : s,
    );
    setShares(optimisticShares);
    onSharesChanged?.(pageId, optimisticShares);
    setBusy(true);
    try {
      const updated = await api.addNoteShares(pageId, {
        shared_with: [share.shared_with],
        permission,
      });
      setShares(updated);
      onSharesChanged?.(pageId, updated);
      writeJsonCache(scopedNoteStorageKey("note-shares", pageId), updated);
    } catch (error) {
      if (shouldQueue(error)) {
        await queueNoteMeta(pageId, {
          kind: "share",
          userId: share.shared_with,
          present: true,
          permission,
          shareId: isPendingShareId(share.share_id) ? null : share.share_id,
        });
      } else {
        setShares(previous);
        onSharesChanged?.(pageId, previous);
      }
    } finally {
      setBusy(false);
    }
  }

  async function removeShare(share: NoteShare) {
    const previous = shares;
    const optimisticShares = shares.filter((s) => s.share_id !== share.share_id);
    setShares(optimisticShares);
    onSharesChanged?.(pageId, optimisticShares);
    setBusy(true);
    try {
      if (!isPendingShareId(share.share_id)) {
        await api.deleteNoteShare(pageId, share.share_id);
      }
      await reloadShares();
    } catch (error) {
      if (shouldQueue(error)) {
        await queueNoteMeta(pageId, {
          kind: "share",
          userId: share.shared_with,
          present: false,
          permission: share.permission,
          shareId: isPendingShareId(share.share_id) ? null : share.share_id,
        });
        writeJsonCache(
          scopedNoteStorageKey("note-shares", pageId),
          previous.filter((s) => s.share_id !== share.share_id),
        );
      } else {
        setShares(previous);
        onSharesChanged?.(pageId, previous);
      }
    } finally {
      setBusy(false);
    }
  }

  async function acknowledge() {
    const previous = acked;
    setAcked(true);
    setBusy(true);
    try {
      await api.acknowledgeNote(pageId);
      onAcknowledged?.();
    } catch (error) {
      if (shouldQueue(error)) {
        await queueNoteMeta(pageId, { kind: "acknowledge" });
        onAcknowledged?.();
      } else {
        setAcked(previous);
      }
    } finally {
      setBusy(false);
    }
  }

  const shareableMembers = members.filter(
    (m) => m.user_id && m.active !== false && !sharedUserIds.has(m.user_id),
  );
  const ackedCount = shares.filter((s) => s.acknowledged_at != null).length;

  return (
    <div
      className={embedded ? "p-3" : "mb-4 rounded-[10px] p-3"}
      style={
        embedded
          ? { background: "transparent" }
          : {
              background: "var(--color-app-canvas)",
              border: "1px solid var(--color-divider-soft)",
            }
      }
    >
      {pendingSync > 0 ? (
        <div
          className="mb-3 flex items-center gap-1.5 rounded-[6px] px-2 py-1 text-[var(--text-meta)] font-semibold"
          style={{ background: "var(--color-warn-bg)", color: "var(--color-warn-fg)" }}
        >
          이 Mac에 저장됨 · 연결되면 자동 동기화 {pendingSync > 1 ? `(${pendingSync})` : ""}
        </div>
      ) : null}

      {/* 수신자 얼라인 확인 배너 */}
      {recipient ? (
        <div className="mb-3 flex items-center justify-between gap-2">
          <span className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            {acked ? "이 메모를 확인했습니다." : "이 공유 메모를 읽고 팀과 얼라인을 맞추세요."}
          </span>
          <button
            type="button"
            disabled={busy || acked}
            onClick={() => void acknowledge()}
            className="inline-flex h-8 shrink-0 items-center gap-1 rounded-[6px] px-2.5 text-[var(--text-body-sm)] font-semibold"
            style={{
              background: acked ? "var(--color-ok-bg, #e6f4ea)" : "var(--color-sidebar-active)",
              color: acked ? "var(--color-ok-fg, #216e39)" : "var(--color-text-strong)",
              border: "1px solid var(--color-divider)",
              opacity: acked ? 0.9 : 1,
            }}
          >
            {acked ? "✓ 확인함" : "얼라인 확인"}
          </button>
        </div>
      ) : null}

      {/* 프로젝트 링크 */}
      <PeekSection
        title="프로젝트"
        count={links.length}
        action={
          canWrite ? (
            <button
              ref={projAnchor}
              type="button"
              onClick={projToggle}
              className="rounded-[5px] px-2 py-1 text-[var(--text-meta)] font-semibold hover:bg-[var(--color-card)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              + 링크
            </button>
          ) : null
        }
      >
        {links.length === 0 ? (
          <p className="text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
            링크된 프로젝트 없음
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {links.map((l) => (
              <span
                key={l.project_id}
                className="inline-flex items-center gap-1.5 rounded-[6px] px-2 py-1 text-[var(--text-meta)] font-medium"
                style={{ background: "var(--color-card)", border: "1px solid var(--color-divider-soft)" }}
              >
                {dot(l.color)}
                <span style={{ color: "var(--color-text-body)" }}>{l.name}</span>
                {canWrite ? (
                  <button
                    type="button"
                    disabled={busy}
                    aria-label={`${l.name} 링크 해제`}
                    onClick={() =>
                      void toggleProject({ project_id: l.project_id } as DashboardProject)
                    }
                    className="ml-0.5 text-[var(--color-text-faint)] hover:text-[var(--color-text-body)]"
                  >
                    ✕
                  </button>
                ) : null}
              </span>
            ))}
          </div>
        )}
      </PeekSection>

      {projOpen && projRect ? (
        <div
          ref={projPopRef}
          className="z-[60] overflow-y-auto rounded-[8px] py-1 shadow-lg"
          style={{
            ...anchoredStyle(projRect, 240),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
          }}
        >
          {projects.length === 0 ? (
            <div className="px-3 py-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
              프로젝트 없음
            </div>
          ) : (
            projects.map((p) => (
              <button
                key={p.project_id}
                type="button"
                disabled={busy}
                onClick={() => void toggleProject(p)}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-body)" }}
              >
                <span className="w-3 shrink-0">{linkedIds.has(p.project_id) ? "✓" : ""}</span>
                {dot(p.color)}
                <span className="truncate">{p.name}</span>
              </button>
            ))
          )}
        </div>
      ) : null}

      {/* 공유 */}
      <PeekSection
        title="공유"
        count={shares.length}
        action={
          canWrite ? (
            <button
              ref={shareAnchor}
              type="button"
              onClick={shareToggle}
              className="rounded-[5px] px-2 py-1 text-[var(--text-meta)] font-semibold hover:bg-[var(--color-card)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              + 팀원
            </button>
          ) : null
        }
      >
        {shares.length === 0 ? (
          <p className="text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
            아직 공유하지 않음
          </p>
        ) : (
          <div className="flex flex-col gap-1">
            {canWrite && ackedCount > 0 ? (
              <p className="mb-0.5 text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
                {ackedCount}/{shares.length} 확인함
              </p>
            ) : null}
            {shares.map((s) => (
              <div key={s.share_id} className="flex items-center gap-2">
                {dot(s.shared_with_color)}
                <span className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]" style={{ color: "var(--color-text-body)" }}>
                  {s.shared_with_name}
                </span>
                {s.acknowledged_at ? (
                  <span
                    className="shrink-0 rounded-[4px] px-1.5 py-0.5 text-[10px] font-bold"
                    style={{ background: "var(--color-ok-bg, #e6f4ea)", color: "var(--color-ok-fg, #216e39)" }}
                    title={`확인: ${s.acknowledged_at.slice(0, 16).replace("T", " ")}`}
                  >
                    ✓ 확인
                  </span>
                ) : (
                  <span className="shrink-0 text-[10px]" style={{ color: "var(--color-text-faint)" }}>
                    대기
                  </span>
                )}
                {canWrite ? (
                  <>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void setSharePermission(s, s.permission === "edit" ? "view" : "edit")}
                      className="shrink-0 rounded-[4px] px-1.5 py-0.5 text-[10px] font-semibold"
                      style={{ background: "var(--color-card)", border: "1px solid var(--color-divider-soft)", color: "var(--color-text-muted)" }}
                      title="권한 전환"
                    >
                      {s.permission === "edit" ? "편집" : "보기"}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      aria-label={`${s.shared_with_name} 공유 해제`}
                      onClick={() => void removeShare(s)}
                      className="shrink-0 text-[var(--color-text-faint)] hover:text-[var(--color-text-body)]"
                    >
                      ✕
                    </button>
                  </>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </PeekSection>

      {shareOpen && shareRect ? (
        <div
          ref={sharePopRef}
          className="z-[60] overflow-y-auto rounded-[8px] py-1 shadow-lg"
          style={{
            ...anchoredStyle(shareRect, 240),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
          }}
        >
          {shareableMembers.length === 0 ? (
            <div className="px-3 py-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
              공유할 팀원 없음 (로그인 연결된 팀원만)
            </div>
          ) : (
            shareableMembers.map((m) => (
              <button
                key={m.member_id}
                type="button"
                disabled={busy}
                onClick={() => void shareWith(m)}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-body)" }}
              >
                {dot(m.color)}
                <span className="truncate">{m.name}</span>
              </button>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}
