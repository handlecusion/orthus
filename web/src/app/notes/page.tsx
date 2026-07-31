"use client";

/* 개인 메모(Personal Notes) — Apple Notes형 탐색 + 프로젝트별 보관함.
 * docs/personal-notes.md. quick-note의 local-first 저장 계약은 그대로 두고,
 * 전체 화면은 권한 안전 bulk projection으로 많은 메모를 분류·검색한다. */

import {
  Check,
  ChevronDown,
  FileText,
  Folder,
  FolderOpen,
  Inbox,
  Search,
  SlidersHorizontal,
  SquarePen,
  Trash2,
  Users,
  X,
} from "lucide-react";
import {
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import {
  api,
  type DashboardPageSummary,
  type DashboardProject,
  type NoteLibraryItem,
  type NoteProjectLink,
  type NoteShare,
} from "@/lib/api";
import { Banner, Skeleton } from "@/components/ui";
import { firstLineTitle, PageStack } from "@/components/dashboard/pageEditor";
import { createOfflineNote, purgePageSave } from "@/lib/pageSave";
import {
  getOfflineNoteOwner,
  isOfflineNoteCreate,
  isOfflineNoteDelete,
  migrateLegacyOwnedNoteCaches,
  readOfflineNoteOutbox,
  scopedNoteStorageKey,
  takeNotesOpenTarget,
} from "@/lib/offline-notes";
import { NoteMetaPanel } from "@/components/notes/NoteMetaPanel";

// 메모를 열기 전에 무거운 에디터 청크(BlockNote)를 미리 받아 peek 첫 오픈 지연을 없앤다.
if (typeof window !== "undefined") {
  void import("@/components/dashboard/RichBody");
}

type Tab = "mine" | "shared";
type SortMode = "updated" | "title";
const UNASSIGNED_FILTER = "__unassigned__";
// AppShell의 좌우 레일을 제외하고도 세 pane이 숨 쉴 수 있는 폭에서만
// 편집기를 고정한다. 그보다 좁으면 같은 편집기 DOM을 compact peek로 쓴다.
const DESKTOP_EDITOR_QUERY = "(min-width: 1280px)";

function subscribeDesktopEditor(callback: () => void) {
  const query = window.matchMedia(DESKTOP_EDITOR_QUERY);
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

function desktopEditorSnapshot() {
  return window.matchMedia(DESKTOP_EDITOR_QUERY).matches;
}

function serverDesktopEditorSnapshot() {
  return false;
}

type OpenNote = {
  kind: Tab;
  page_id: string;
  title: string;
  icon: string | null;
  owner_id: string | null;
  permission: "view" | "edit" | null;
  acknowledged_at: string | null;
};

type ProjectFacet = {
  project_id: string;
  name: string;
  color: string | null;
  parent_project_id: string | null;
  sort_order: number;
  depth: number;
  count: number;
};

function localItem(row: DashboardPageSummary): NoteLibraryItem {
  return {
    page_id: row.page_id,
    title: row.title,
    icon: row.icon,
    preview: row.preview ?? null,
    updated_at: row.updated_at,
    owner_id: row.owner_id ?? null,
    owner_name: null,
    access: "owner",
    permission: null,
    acknowledged_at: null,
    projects: [],
    share_count: 0,
    acknowledged_count: 0,
  };
}

function cachedMineRows(): NoteLibraryItem[] {
  const key = scopedNoteStorageKey("quick-note-list");
  const owner = getOfflineNoteOwner();
  if (!key || !owner) return [];
  try {
    const parsed = JSON.parse(localStorage.getItem(key) ?? "null") as {
      rows?: DashboardPageSummary[];
    } | null;
    return Array.isArray(parsed?.rows)
      ? parsed.rows
          .filter(
            (row) => row.owner_id === owner.userId && !isOfflineNoteDelete(row.page_id),
          )
          .map(localItem)
      : [];
  } catch {
    return [];
  }
}

function mergeMineRows(
  server: NoteLibraryItem[],
  local: NoteLibraryItem[],
): NoteLibraryItem[] {
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
  return [...byId.values()].sort(
    (a, b) => new Date(b.updated_at ?? 0).getTime() - new Date(a.updated_at ?? 0).getTime(),
  );
}

function toOpenNote(row: NoteLibraryItem, kind: Tab): OpenNote {
  return {
    kind,
    page_id: row.page_id,
    title: row.title,
    icon: row.icon,
    owner_id: row.owner_id,
    permission: kind === "shared" ? row.permission : null,
    acknowledged_at: kind === "shared" ? row.acknowledged_at : null,
  };
}

function projectDot(color: string | null) {
  return (
    <span
      className="h-2 w-2 shrink-0 rounded-full"
      style={{ background: color || "var(--color-text-faint)" }}
      aria-hidden
    />
  );
}

function formatUpdated(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  }
  if (date.getFullYear() === today.getFullYear()) {
    return `${date.getMonth() + 1}월 ${date.getDate()}일`;
  }
  return `${date.getFullYear()}.${date.getMonth() + 1}.${date.getDate()}`;
}

export default function NotesPage() {
  const [tab, setTab] = useState<Tab>("mine");
  const [mine, setMine] = useState<NoteLibraryItem[]>([]);
  const [shared, setShared] = useState<NoteLibraryItem[]>([]);
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Apple Notes처럼 보관함을 왕복해도 각 목록의 마지막 선택을 보존한다.
  // PageStack은 현재 탭의 선택 하나만 마운트하므로 숨은 편집기 저장 충돌도 없다.
  const [selections, setSelections] = useState<Partial<Record<Tab, OpenNote>>>({});
  const open = selections[tab] ?? null;
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const [sort, setSort] = useState<SortMode>("updated");
  const [selectedProjects, setSelectedProjects] = useState<string[]>([]);
  const [mobileFilterOpen, setMobileFilterOpen] = useState(false);
  const mobileFilterTriggerRef = useRef<HTMLButtonElement>(null);
  const mobileFilterDialogRef = useRef<HTMLElement>(null);
  const desktopEditor = useSyncExternalStore(
    subscribeDesktopEditor,
    desktopEditorSnapshot,
    serverDesktopEditorSnapshot,
  );
  // A note id the quick window (or a shared link) asked us to open.
  const [openTarget, setOpenTarget] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URL(window.location.href).searchParams.get("note") || takeNotesOpenTarget();
  });

  const selectNote = useCallback((note: OpenNote) => {
    setSelections((current) => ({ ...current, [note.kind]: note }));
  }, []);

  const clearSelection = useCallback((kind: Tab, pageId?: string) => {
    setSelections((current) => {
      if (!current[kind] || (pageId && current[kind]?.page_id !== pageId)) return current;
      return { ...current, [kind]: undefined };
    });
  }, []);

  const reloadMine = useCallback(
    () =>
      api.listNoteLibrary("mine").then((rows) => {
        migrateLegacyOwnedNoteCaches(rows);
        setMine((previous) => mergeMineRows(rows, previous));
        const firstVisible = rows.find((row) => !isOfflineNoteDelete(row.page_id));
        if (firstVisible && desktopEditorSnapshot()) {
          setSelections((current) =>
            current.mine ? current : { ...current, mine: toOpenNote(firstVisible, "mine") },
          );
        }
      }),
    [],
  );
  const reloadShared = useCallback(
    () => api.listNoteLibrary("shared").then(setShared),
    [],
  );
  const reloadProjects = useCallback(
    () => api.listDashboardProjects().then(setProjects).catch(() => undefined),
    [],
  );

  useEffect(() => {
    let alive = true;
    const cached = cachedMineRows();
    const cachedTimer = window.setTimeout(() => {
      if (!alive || cached.length === 0) return;
      setMine(cached);
      setLoading(false);
    }, 0);
    void readOfflineNoteOutbox().then((records) => {
      if (!alive) return;
      const owner = getOfflineNoteOwner();
      if (!owner) return;
      const deletedIds = new Set(
        records.filter((record) => record.op === "delete").map((record) => record.pageId),
      );
      if (deletedIds.size > 0) {
        setMine((previous) => previous.filter((row) => !deletedIds.has(row.page_id)));
        setSelections((previous) =>
          previous.mine && deletedIds.has(previous.mine.page_id)
            ? { ...previous, mine: undefined }
            : previous,
        );
      }
      const localRows: NoteLibraryItem[] = records
        .filter((record) => record.op === "create")
        .map((record) => ({
          page_id: record.pageId,
          title: record.op === "create" ? record.create.title : "새 메모",
          icon: record.op === "create" ? record.create.icon : null,
          preview: null,
          updated_at: new Date(record.updatedAt).toISOString(),
          owner_id: owner.userId,
          owner_name: null,
          access: "owner" as const,
          permission: null,
          acknowledged_at: null,
          projects: [],
          share_count: 0,
          acknowledged_count: 0,
        }));
      if (localRows.length > 0) {
        setMine((previous) => mergeMineRows(previous, localRows));
        setLoading(false);
      }
    });
    Promise.all([reloadMine(), reloadShared(), reloadProjects()])
      .then(() => {
        if (alive) setError(null);
      })
      .catch((reason: unknown) => {
        if (alive) setError(reason instanceof Error ? reason.message : "불러오기 실패");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
      window.clearTimeout(cachedTimer);
    };
  }, [reloadMine, reloadProjects, reloadShared]);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.has("note")) {
      url.searchParams.delete("note");
      window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }
  }, []);

  useEffect(() => {
    const pickup = () => {
      const target = takeNotesOpenTarget();
      if (target) setOpenTarget(target);
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") pickup();
    };
    window.addEventListener("focus", pickup);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", pickup);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  useEffect(() => {
    if (!mobileFilterOpen) return;
    const dialog = mobileFilterDialogRef.current;
    if (!dialog) return;
    const trigger = mobileFilterTriggerRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => dialog.focus());
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMobileFilterOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )].filter((element) => element.offsetParent !== null);
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!dialog.contains(active) || active === dialog || (!event.shiftKey && active === last)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      trigger?.focus();
    };
  }, [mobileFilterOpen]);

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 760px)");
    const closeHiddenSheet = (event: MediaQueryListEvent) => {
      if (event.matches) setMobileFilterOpen(false);
    };
    desktop.addEventListener("change", closeHiddenSheet);
    return () => desktop.removeEventListener("change", closeHiddenSheet);
  }, []);

  useEffect(() => {
    if (!openTarget) return;
    let alive = true;
    const resolve = async () => {
      const alreadySelected = Object.values(selections).find(
        (selection) => selection?.page_id === openTarget,
      );
      if (alreadySelected) {
        setTab(alreadySelected.kind);
        setOpenTarget(null);
        return;
      }
      const inMine = mine.find((row) => row.page_id === openTarget);
      if (inMine) {
        setTab("mine");
        selectNote(toOpenNote(inMine, "mine"));
        setOpenTarget(null);
        return;
      }
      const inShared = shared.find((row) => row.page_id === openTarget);
      if (inShared) {
        setTab("shared");
        selectNote(toOpenNote(inShared, "shared"));
        setOpenTarget(null);
        return;
      }
      if (loading) return;
      try {
        // 목록 로드와 handoff가 엇갈린 새 공유 링크일 수 있다. 단건 page
        // 응답만으로 mine/shared를 추정하면 view 공유를 owner처럼 열 수 있으므로
        // 두 권한 projection에서 다시 확인한 경우에만 연다(fail-closed).
        const [freshMine, freshShared] = await Promise.all([
          api.listNoteLibrary("mine"),
          api.listNoteLibrary("shared"),
        ]);
        if (!alive) return;
        setMine((previous) => mergeMineRows(freshMine, previous));
        setShared(freshShared);
        const resolvedMine = freshMine.find(
          (row) => row.page_id === openTarget && !isOfflineNoteDelete(row.page_id),
        );
        const resolvedShared = freshShared.find((row) => row.page_id === openTarget);
        if (resolvedMine) {
          setTab("mine");
          selectNote(toOpenNote(resolvedMine, "mine"));
        } else if (resolvedShared) {
          setTab("shared");
          selectNote(toOpenNote(resolvedShared, "shared"));
        }
      } catch {
        // 삭제됐거나 접근할 수 없는 deep-link는 조용히 버린다.
      } finally {
        if (alive) setOpenTarget(null);
      }
    };
    void resolve();
    return () => {
      alive = false;
    };
  }, [loading, mine, openTarget, selectNote, selections, shared]);

  const handleProjectsChanged = useCallback((pageId: string, links: NoteProjectLink[]) => {
    const patch = (rows: NoteLibraryItem[]) =>
      rows.map((row) => (row.page_id === pageId ? { ...row, projects: links } : row));
    setMine(patch);
    setShared(patch);
  }, []);

  const handleSharesChanged = useCallback((pageId: string, shares: NoteShare[]) => {
    setMine((rows) =>
      rows.map((row) =>
        row.page_id === pageId
          ? {
              ...row,
              share_count: shares.length,
              acknowledged_count: shares.filter((share) => share.acknowledged_at != null).length,
            }
          : row,
      ),
    );
  }, []);

  const handleRootBodyChanged = useCallback((pageId: string, body: string) => {
    const updatedAt = new Date().toISOString();
    const preview = firstLineTitle(body);
    const patch = (rows: NoteLibraryItem[]) =>
      rows.map((row) =>
        row.page_id === pageId ? { ...row, preview, updated_at: updatedAt } : row,
      );
    setMine(patch);
    setShared(patch);
  }, []);

  const handleAcknowledged = useCallback((pageId: string) => {
    const acknowledgedAt = new Date().toISOString();
    setShared((rows) =>
      rows.map((row) =>
        row.page_id === pageId ? { ...row, acknowledged_at: acknowledgedAt } : row,
      ),
    );
    setSelections((current) =>
      current.shared?.page_id === pageId
        ? { ...current, shared: { ...current.shared, acknowledged_at: acknowledgedAt } }
        : current,
    );
  }, []);

  const handleRootTitleChanged = useCallback(
    (
      pageId: string,
      kind: Tab,
      permission: "view" | "edit" | null,
      title: string,
    ) => {
      if (kind === "shared" && permission !== "edit") return;
      const updatedAt = new Date().toISOString();
      const patch = (rows: NoteLibraryItem[]) =>
        rows.map((row) => (row.page_id === pageId ? { ...row, title, updated_at: updatedAt } : row));
      setMine(patch);
      setShared(patch);
      setSelections((current) => {
        const selected = current[kind];
        return selected?.page_id === pageId
          ? { ...current, [kind]: { ...selected, title } }
          : current;
      });
    },
    [],
  );

  async function createMemo() {
    try {
      const owner = getOfflineNoteOwner();
      if (!owner) throw new Error("로그인 정보를 확인할 수 없습니다");
      const page = await createOfflineNote({ title: "새 메모", icon: null, body: "" });
      const row: NoteLibraryItem = {
        page_id: page.page_id,
        title: page.title,
        icon: page.icon,
        preview: null,
        updated_at: new Date().toISOString(),
        owner_id: owner.userId,
        owner_name: null,
        access: "owner",
        permission: null,
        acknowledged_at: null,
        projects: [],
        share_count: 0,
        acknowledged_count: 0,
      };
      setMine((previous) => [row, ...previous]);
      setQuery("");
      setSelectedProjects([]);
      setTab("mine");
      selectNote(toOpenNote(row, "mine"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "생성 실패");
    }
  }

  async function deleteMemo(id: string) {
    if (!window.confirm("이 메모를 삭제할까요? 안의 내용도 함께 사라집니다.")) return;
    try {
      await purgePageSave(id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "로컬 메모 삭제 준비 실패");
      return;
    }
    clearSelection("mine", id);
    setMine((previous) => previous.filter((row) => row.page_id !== id));
  }

  const sourceRows = tab === "mine" ? mine : shared;
  const projectCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of sourceRows) {
      for (const project of row.projects) {
        counts.set(project.project_id, (counts.get(project.project_id) ?? 0) + 1);
      }
    }
    return counts;
  }, [sourceRows]);
  const unassignedCount = useMemo(
    () => sourceRows.filter((row) => row.projects.length === 0).length,
    [sourceRows],
  );
  const facets = useMemo<ProjectFacet[]>(() => {
    type CatalogItem = {
      project_id: string;
      name: string;
      color: string | null;
      parent_project_id: string | null;
      sort_order: number;
      active: boolean;
    };
    const catalog = new Map<string, CatalogItem>();
    for (const project of projects) {
      catalog.set(project.project_id, {
        project_id: project.project_id,
        name: project.name,
        color: project.color,
        parent_project_id: project.parent_project_id,
        sort_order: project.sort_order,
        active: project.active,
      });
    }
    for (const row of sourceRows) {
      for (const project of row.projects) {
        if (!catalog.has(project.project_id)) {
          catalog.set(project.project_id, {
            project_id: project.project_id,
            name: project.name,
            color: project.color,
            parent_project_id: null,
            sort_order: Number.MAX_SAFE_INTEGER,
            active: false,
          });
        }
      }
    }
    const included = [...catalog.values()].filter(
      (project) => project.active || (projectCounts.get(project.project_id) ?? 0) > 0,
    );
    const includedIds = new Set(included.map((project) => project.project_id));
    const compare = (a: CatalogItem, b: CatalogItem) =>
      a.sort_order - b.sort_order || a.name.localeCompare(b.name, "ko");
    const children = new Map<string, CatalogItem[]>();
    const roots: CatalogItem[] = [];
    for (const project of included) {
      if (project.parent_project_id && includedIds.has(project.parent_project_id)) {
        const bucket = children.get(project.parent_project_id) ?? [];
        bucket.push(project);
        children.set(project.parent_project_id, bucket);
      } else {
        roots.push(project);
      }
    }
    roots.sort(compare);
    for (const bucket of children.values()) bucket.sort(compare);
    const flattened: ProjectFacet[] = [];
    const visited = new Set<string>();
    const visit = (project: CatalogItem, depth: number) => {
      if (visited.has(project.project_id)) return;
      visited.add(project.project_id);
      flattened.push({
        ...project,
        depth,
        count: projectCounts.get(project.project_id) ?? 0,
      });
      for (const child of children.get(project.project_id) ?? []) visit(child, depth + 1);
    };
    for (const root of roots) visit(root, 0);
    // Corrupt/cyclic parent data should not make an otherwise usable project
    // disappear from the filter; surface the remaining node as a root.
    for (const project of included.sort(compare)) visit(project, 0);
    return flattened;
  }, [projectCounts, projects, sourceRows]);

  const selectedSet = useMemo(() => new Set(selectedProjects), [selectedProjects]);
  const filteredRows = useMemo(() => {
    const normalizedQuery = deferredQuery.trim().toLocaleLowerCase("ko");
    const filtered = sourceRows.filter((row) => {
      const projectMatch =
        selectedSet.size === 0 ||
        (selectedSet.has(UNASSIGNED_FILTER) && row.projects.length === 0) ||
        row.projects.some((project) => selectedSet.has(project.project_id));
      if (!projectMatch) return false;
      if (!normalizedQuery) return true;
      return [
        row.title,
        row.preview ?? "",
        row.owner_name ?? "",
        ...row.projects.map((project) => project.name),
      ]
        .join(" ")
        .toLocaleLowerCase("ko")
        .includes(normalizedQuery);
    });
    return [...filtered].sort((a, b) => {
      if (sort === "title") return (a.title || "제목 없음").localeCompare(b.title || "제목 없음", "ko");
      return new Date(b.updated_at ?? 0).getTime() - new Date(a.updated_at ?? 0).getTime();
    });
  }, [deferredQuery, selectedSet, sort, sourceRows]);

  const toggleProjectFilter = useCallback((projectId: string) => {
    setSelectedProjects((current) =>
      current.includes(projectId)
        ? current.filter((id) => id !== projectId)
        : [...current, projectId],
    );
  }, []);

  const selectedLabels = useMemo(() => {
    const facetById = new Map(facets.map((facet) => [facet.project_id, facet]));
    return selectedProjects.map((id) => ({
      id,
      name:
        id === UNASSIGNED_FILTER
          ? "분류 안 됨"
          : facetById.get(id)?.name || "프로젝트",
    }));
  }, [facets, selectedProjects]);

  const selectedOutsideFilter = Boolean(
    open && !filteredRows.some((row) => row.page_id === open.page_id),
  );
  const compactEditorOpen = Boolean(open && !desktopEditor);
  const closeOpenNote = useCallback(() => clearSelection(tab), [clearSelection, tab]);
  const switchTab = useCallback(
    (next: Tab) => {
      if (next === tab) return;
      setTab(next);
      if (!desktopEditor) {
        // Compact 화면의 보관함 전환은 목록에서 시작한다. 데스크톱에서
        // 기억한 선택이 곧바로 modal editor를 덮어 띄우지 않게 한다.
        setSelections((current) => ({ ...current, [next]: undefined }));
        return;
      }
      const first = (next === "mine" ? mine : shared)[0];
      if (first) {
        setSelections((current) =>
          current[next] ? current : { ...current, [next]: toOpenNote(first, next) },
        );
      }
    },
    [desktopEditor, mine, shared, tab],
  );

  return (
    <div
      className="h-full min-h-0 w-full p-0 min-[760px]:p-3"
      style={
        {
          "--notes-accent": "color-mix(in srgb, var(--color-accent) 50%, #c88d18)",
          "--notes-accent-soft":
            "color-mix(in srgb, var(--notes-accent) 14%, var(--color-card))",
          "--notes-sidebar": "color-mix(in srgb, var(--color-panel) 94%, #eee4cf)",
          "--notes-list": "color-mix(in srgb, var(--color-card) 97%, #fff5dc)",
          "--notes-paper": "color-mix(in srgb, var(--color-card) 98%, #fffaf0)",
        } as React.CSSProperties
      }
    >
      <div className="h-full min-h-0" inert={mobileFilterOpen}>
        <div
          className="grid h-full min-h-0 min-w-0 grid-cols-1 overflow-hidden min-[760px]:grid-cols-[220px_minmax(0,1fr)] min-[760px]:rounded-[14px] min-[1280px]:grid-cols-[220px_350px_minmax(0,1fr)]"
          style={{
            background: "var(--notes-list)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-card)",
          }}
        >
          <aside
            className="hidden min-h-0 flex-col border-r min-[760px]:flex"
            style={{ background: "var(--notes-sidebar)", borderColor: "var(--color-divider)" }}
            aria-label="메모 보관함과 프로젝트"
            aria-hidden={compactEditorOpen || undefined}
            inert={compactEditorOpen}
          >
            <div className="px-3 pb-2 pt-4">
              <div className="flex items-center gap-2.5 px-1">
                <span
                  className="grid h-8 w-8 shrink-0 place-items-center rounded-[8px]"
                  style={{ background: "var(--notes-accent-soft)", color: "var(--notes-accent)" }}
                  aria-hidden
                >
                  <FileText size={17} strokeWidth={2.2} />
                </span>
                <div className="min-w-0 flex-1">
                  <h1 className="truncate text-[17px] font-bold tracking-[-0.02em]" style={{ color: "var(--color-text-strong)" }}>
                    메모
                  </h1>
                  <p className="text-[10px] font-medium" style={{ color: "var(--color-text-faint)" }}>
                    이 Mac의 메모 보관함
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => void createMemo()}
                className="mt-3 inline-flex h-9 w-full items-center justify-center gap-2 rounded-[8px] text-[var(--text-body-sm)] font-bold transition-colors hover:brightness-[0.98]"
                style={{ background: "var(--notes-accent-soft)", color: "var(--notes-accent)" }}
              >
                <SquarePen size={15} />
                새 메모
              </button>
            </div>

            <nav className="px-2 pb-2" aria-label="메모 보관함">
              <p className="px-2 pb-1 pt-2 text-[10px] font-extrabold uppercase tracking-[0.08em]" style={{ color: "var(--color-text-faint)" }}>
                보관함
              </p>
              <SidebarAccountButton
                active={tab === "mine"}
                count={mine.length}
                icon={<Inbox size={15} />}
                label="내 메모"
                onClick={() => switchTab("mine")}
              />
              <SidebarAccountButton
                active={tab === "shared"}
                count={shared.length}
                icon={<Users size={15} />}
                label="공유받은 메모"
                onClick={() => switchTab("shared")}
              />
            </nav>

            <div className="mx-3 border-t" style={{ borderColor: "var(--color-divider-soft)" }} />
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-0 pb-3">
              <ProjectFilterPanel
                total={sourceRows.length}
                unassignedCount={unassignedCount}
                facets={facets}
                selected={selectedSet}
                onToggle={toggleProjectFilter}
                onClear={() => setSelectedProjects([])}
              />
            </div>
          </aside>

          <main
            className="flex min-h-0 min-w-0 flex-col min-[1280px]:border-r"
            style={{ borderColor: "var(--color-divider)" }}
            aria-hidden={compactEditorOpen || undefined}
            inert={compactEditorOpen}
          >
            <header className="shrink-0 border-b px-3 pb-3 pt-3" style={{ borderColor: "var(--color-divider-soft)" }}>
              <div className="mb-2 flex min-h-10 items-center gap-2">
                <div className="min-w-0 flex-1">
                  <h2 className="truncate text-[18px] font-bold tracking-[-0.02em]" style={{ color: "var(--color-text-strong)" }}>
                    {tab === "mine" ? "내 메모" : "공유받은 메모"}
                  </h2>
                  <p className="text-[var(--text-meta)] tabular-nums" style={{ color: "var(--color-text-faint)" }}>
                    {sourceRows.length === filteredRows.length
                      ? `${sourceRows.length}개의 메모`
                      : `${sourceRows.length}개 중 ${filteredRows.length}개`}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void createMemo()}
                  className="grid h-11 w-11 shrink-0 place-items-center rounded-[8px] min-[760px]:hidden"
                  style={{ background: "var(--notes-accent-soft)", color: "var(--notes-accent)" }}
                  aria-label="새 메모"
                >
                  <SquarePen size={18} />
                </button>
                <label className="inline-flex h-11 shrink-0 items-center gap-1 text-[var(--text-meta)] font-semibold min-[760px]:h-9" style={{ color: "var(--color-text-muted)" }}>
                  <span className="sr-only">정렬</span>
                  <select
                    value={sort}
                    onChange={(event) => setSort(event.target.value as SortMode)}
                    className="h-11 rounded-[7px] bg-transparent pl-2 pr-1 text-[var(--text-body-sm)] font-semibold outline-none min-[760px]:h-9"
                    style={{ border: "1px solid var(--color-divider)", color: "var(--color-text-body)" }}
                    aria-label="메모 정렬"
                  >
                    <option value="updated">최근 수정순</option>
                    <option value="title">이름순</option>
                  </select>
                </label>
              </div>

              <div
                className="mb-2 grid grid-cols-2 gap-1 rounded-[9px] p-1 min-[760px]:hidden"
                style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider-soft)" }}
                aria-label="메모 보관함"
              >
                <SidebarAccountButton
                  active={tab === "mine"}
                  count={mine.length}
                  icon={<Inbox size={14} />}
                  label="내 메모"
                  onClick={() => switchTab("mine")}
                />
                <SidebarAccountButton
                  active={tab === "shared"}
                  count={shared.length}
                  icon={<Users size={14} />}
                  label="공유받음"
                  onClick={() => switchTab("shared")}
                />
              </div>

              <div className="flex items-center gap-2">
                <label
                  className="flex h-11 min-w-0 flex-1 items-center gap-2 rounded-[8px] px-3 min-[760px]:h-9"
                  style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider-soft)" }}
                >
                  <Search size={14} className="shrink-0" style={{ color: "var(--color-text-muted)" }} />
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="메모 검색"
                    aria-label="메모 검색"
                    className="min-w-0 flex-1 bg-transparent text-[var(--text-body-sm)] outline-none placeholder:text-[var(--color-text-faint)]"
                    style={{ color: "var(--color-text-strong)" }}
                  />
                  {query ? (
                    <button
                      type="button"
                      onClick={() => setQuery("")}
                      className="grid h-8 w-8 shrink-0 place-items-center rounded-[6px] hover:bg-[var(--color-toolbar-hover)]"
                      aria-label="검색 지우기"
                    >
                      <X size={13} />
                    </button>
                  ) : null}
                </label>
                <button
                  ref={mobileFilterTriggerRef}
                  type="button"
                  onClick={() => setMobileFilterOpen(true)}
                  aria-expanded={mobileFilterOpen}
                  aria-controls="notes-project-filter-dialog"
                  aria-label="프로젝트 필터"
                  className="inline-flex h-11 shrink-0 items-center gap-1.5 rounded-[8px] px-3 text-[var(--text-body-sm)] font-semibold min-[760px]:hidden"
                  style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider)" }}
                >
                  <SlidersHorizontal size={15} />
                  {selectedProjects.length > 0 ? (
                    <span className="tabular-nums" style={{ color: "var(--notes-accent)" }}>
                      {selectedProjects.length}
                    </span>
                  ) : null}
                </button>
              </div>

              <div className="mt-2 flex items-center gap-1.5 overflow-x-auto" aria-label="선택된 프로젝트 필터">
                {selectedLabels.length > 0 ? (
                  <>
                    {selectedLabels.map((label) => (
                      <button
                        key={label.id}
                        type="button"
                        onClick={() => toggleProjectFilter(label.id)}
                        className="inline-flex h-11 max-w-[180px] shrink-0 items-center gap-1.5 rounded-full px-2.5 text-[var(--text-meta)] font-bold min-[760px]:h-8"
                        style={{ background: "var(--notes-accent-soft)", color: "var(--notes-accent)" }}
                      >
                        <span className="truncate">{label.name}</span>
                        <X size={11} />
                      </button>
                    ))}
                    <button
                      type="button"
                      onClick={() => setSelectedProjects([])}
                      className="h-11 shrink-0 rounded-full px-2 text-[var(--text-meta)] font-semibold hover:bg-[var(--color-toolbar-hover)] min-[760px]:h-8"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      지우기
                    </button>
                  </>
                ) : (
                  <span className="inline-flex h-8 items-center gap-1.5 text-[var(--text-meta)] font-medium" style={{ color: "var(--color-text-faint)" }}>
                    <Folder size={12} /> 모든 프로젝트
                  </span>
                )}
              </div>
            </header>

            {error ? (
              <div className="shrink-0 p-3">
                <Banner tone="fail" title="메모를 불러오지 못했습니다">
                  {error}
                </Banner>
              </div>
            ) : null}

            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain" style={{ background: "var(--notes-list)" }}>
              {loading ? (
                <div className="space-y-2 p-3">
                  {[0, 1, 2, 3, 4, 5].map((row) => (
                    <Skeleton key={row} className="h-[92px] w-full rounded-[9px]" />
                  ))}
                </div>
              ) : filteredRows.length === 0 ? (
                <div className="px-6 py-16 text-center">
                  <FolderOpen size={27} className="mx-auto mb-3" style={{ color: "var(--color-text-faint)" }} />
                  <p className="text-[var(--text-body-sm)] font-bold" style={{ color: "var(--color-text-strong)" }}>
                    {sourceRows.length === 0
                      ? tab === "mine"
                        ? "아직 메모가 없습니다"
                        : "공유받은 메모가 없습니다"
                      : "조건에 맞는 메모가 없습니다"}
                  </p>
                  <p className="mx-auto mt-1 max-w-[250px] text-[var(--text-meta)] leading-5" style={{ color: "var(--color-text-muted)" }}>
                    {sourceRows.length === 0
                      ? tab === "mine"
                        ? "새 메모를 만들면 이 Mac에 먼저 안전하게 저장됩니다."
                        : "팀원이 공유한 메모가 여기에 모입니다."
                      : "검색어나 프로젝트 선택을 바꿔보세요."}
                  </p>
                  {sourceRows.length > 0 && (query || selectedProjects.length > 0) ? (
                    <button
                      type="button"
                      onClick={() => {
                        setQuery("");
                        setSelectedProjects([]);
                      }}
                      className="mt-4 h-11 rounded-[8px] px-3 text-[var(--text-body-sm)] font-bold"
                      style={{ background: "var(--notes-accent-soft)", color: "var(--notes-accent)" }}
                    >
                      필터 초기화
                    </button>
                  ) : null}
                </div>
              ) : (
                <NativeNoteList
                  rows={filteredRows}
                  tab={tab}
                  sort={sort}
                  selectedPageId={open?.page_id ?? null}
                  onOpen={(row) => selectNote(toOpenNote(row, tab))}
                />
              )}
            </div>

            {!loading && filteredRows.length > 0 ? (
              <div className="shrink-0 border-t px-3 py-1.5 text-right text-[10px] tabular-nums" style={{ borderColor: "var(--color-divider-soft)", color: "var(--color-text-faint)" }}>
                {filteredRows.length}개 표시
              </div>
            ) : null}
          </main>

          {open ? (
            <ResponsiveNoteEditor
              key={open.page_id}
              note={open}
              docked={desktopEditor}
              outsideFilter={selectedOutsideFilter}
              onClose={closeOpenNote}
              onDelete={deleteMemo}
              onAcknowledged={handleAcknowledged}
              onProjectsChanged={handleProjectsChanged}
              onSharesChanged={handleSharesChanged}
              onRootBodyChanged={handleRootBodyChanged}
              onRootTitleChanged={handleRootTitleChanged}
            />
          ) : desktopEditor ? (
            <EmptyNoteEditor />
          ) : null}
        </div>
      </div>

      {mobileFilterOpen ? (
        <div className="fixed inset-0 z-[70] min-[760px]:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-black/30"
            onClick={() => setMobileFilterOpen(false)}
            aria-label="프로젝트 필터 닫기"
          />
          <section
            ref={mobileFilterDialogRef}
            id="notes-project-filter-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="프로젝트 필터"
            tabIndex={-1}
            className="absolute inset-x-0 bottom-0 max-h-[76dvh] overflow-hidden rounded-t-[16px] shadow-[var(--shadow-modal)]"
            style={{ background: "var(--color-modal)", borderTop: "1px solid var(--color-divider)" }}
          >
            <div className="mx-auto mt-2 h-1 w-10 rounded-full bg-[var(--color-divider)]" aria-hidden />
            <div className="max-h-[calc(76dvh-62px)] overflow-y-auto overscroll-contain">
              <ProjectFilterPanel
                total={sourceRows.length}
                unassignedCount={unassignedCount}
                facets={facets}
                selected={selectedSet}
                onToggle={toggleProjectFilter}
                onClear={() => setSelectedProjects([])}
              />
            </div>
            <div className="border-t p-3" style={{ borderColor: "var(--color-divider-soft)" }}>
              <button
                type="button"
                onClick={() => setMobileFilterOpen(false)}
                className="h-11 w-full rounded-[8px] text-[var(--text-body-sm)] font-bold"
                style={{ background: "var(--notes-accent)", color: "var(--color-accent-fg)" }}
              >
                {filteredRows.length}개 메모 보기
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}

function SidebarAccountButton({
  active,
  count,
  icon,
  label,
  onClick,
}: {
  active: boolean;
  count: number;
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex h-11 w-full items-center gap-2 rounded-[7px] px-2.5 text-left text-[var(--text-body-sm)] font-semibold transition-colors min-[760px]:h-9"
      style={{
        background: active ? "var(--notes-accent-soft)" : "transparent",
        color: active ? "var(--notes-accent)" : "var(--color-text-body)",
      }}
      aria-pressed={active}
    >
      <span className="grid h-5 w-5 shrink-0 place-items-center" aria-hidden>
        {icon}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <span className="text-[10px] tabular-nums" style={{ color: "var(--color-text-faint)" }}>
        {count}
      </span>
    </button>
  );
}

function ProjectFilterPanel({
  total,
  unassignedCount,
  facets,
  selected,
  onToggle,
  onClear,
}: {
  total: number;
  unassignedCount: number;
  facets: ProjectFacet[];
  selected: ReadonlySet<string>;
  onToggle: (id: string) => void;
  onClear: () => void;
}) {
  return (
    <div className="p-2">
      <div className="flex h-11 items-center justify-between px-2">
        <span className="text-[10px] font-extrabold uppercase tracking-[0.08em]" style={{ color: "var(--color-text-faint)" }}>
          프로젝트
        </span>
        {selected.size > 0 ? (
          <button
            type="button"
            onClick={onClear}
            className="h-11 rounded-[5px] px-2 text-[var(--text-meta)] font-semibold hover:bg-[var(--color-toolbar-hover)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            초기화
          </button>
        ) : null}
      </div>

      <button
        type="button"
        onClick={onClear}
        className="flex h-11 w-full items-center gap-2 rounded-[7px] px-2.5 text-left text-[var(--text-body-sm)] font-semibold"
        style={{
          background: selected.size === 0 ? "var(--notes-accent-soft)" : "transparent",
          color: selected.size === 0 ? "var(--notes-accent)" : "var(--color-text-body)",
        }}
        aria-pressed={selected.size === 0}
      >
        <span className="grid h-5 w-5 shrink-0 place-items-center">
          {selected.size === 0 ? <Check size={15} strokeWidth={2.4} /> : <FolderOpen size={15} />}
        </span>
        <span className="min-w-0 flex-1 truncate">모든 메모</span>
        <span className="text-[var(--text-meta)] tabular-nums" style={{ color: "var(--color-text-muted)" }}>
          {total}
        </span>
      </button>

      <label
        className="flex h-11 cursor-pointer items-center gap-2 rounded-[7px] px-2.5 text-[var(--text-body-sm)] hover:bg-[var(--color-toolbar-hover)]"
        style={{
          background: selected.has(UNASSIGNED_FILTER) ? "var(--notes-accent-soft)" : "transparent",
          color: selected.has(UNASSIGNED_FILTER) ? "var(--notes-accent)" : "var(--color-text-body)",
        }}
      >
        <input
          type="checkbox"
          checked={selected.has(UNASSIGNED_FILTER)}
          onChange={() => onToggle(UNASSIGNED_FILTER)}
          className="h-4 w-4 shrink-0"
          style={{ accentColor: "var(--notes-accent)" }}
        />
        <span className="min-w-0 flex-1 truncate font-semibold">분류 안 됨</span>
        <span className="text-[var(--text-meta)] tabular-nums" style={{ color: "var(--color-text-muted)" }}>
          {unassignedCount}
        </span>
      </label>

      <div className="my-1 border-t" style={{ borderColor: "var(--color-divider-soft)" }} />
      {facets.length === 0 ? (
        <p className="px-2.5 py-5 text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
          활성 프로젝트가 없습니다.
        </p>
      ) : (
        facets.map((facet) => {
          const active = selected.has(facet.project_id);
          return (
            <label
              key={facet.project_id}
              className="flex h-11 cursor-pointer items-center gap-2 rounded-[7px] px-2.5 text-[var(--text-body-sm)] hover:bg-[var(--color-toolbar-hover)]"
              style={{
                paddingLeft: 10 + Math.min(facet.depth, 3) * 16,
                background: active ? "var(--notes-accent-soft)" : "transparent",
                color: active ? "var(--notes-accent)" : "var(--color-text-body)",
              }}
              title={facet.name}
            >
              <input
                type="checkbox"
                checked={active}
                onChange={() => onToggle(facet.project_id)}
                className="h-4 w-4 shrink-0"
                style={{ accentColor: "var(--notes-accent)" }}
              />
              {facet.depth ? projectDot(facet.color) : <Folder size={14} className="shrink-0" />}
              <span className="min-w-0 flex-1 truncate font-medium">{facet.name}</span>
              <span className="text-[var(--text-meta)] tabular-nums" style={{ color: "var(--color-text-muted)" }}>
                {facet.count}
              </span>
            </label>
          );
        })
      )}
    </div>
  );
}

function noteDateGroup(value: string | null): string {
  if (!value) return "이전";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "이전";
  const startToday = new Date();
  startToday.setHours(0, 0, 0, 0);
  const startDate = new Date(date);
  startDate.setHours(0, 0, 0, 0);
  const days = Math.floor((startToday.getTime() - startDate.getTime()) / 86_400_000);
  if (days <= 0) return "오늘";
  if (days < 7) return "이전 7일";
  return "이전";
}

function NativeNoteList({
  rows,
  tab,
  sort,
  selectedPageId,
  onOpen,
}: {
  rows: NoteLibraryItem[];
  tab: Tab;
  sort: SortMode;
  selectedPageId: string | null;
  onOpen: (row: NoteLibraryItem) => void;
}) {
  const groups = useMemo(() => {
    if (sort === "title") return [{ label: "이름순", rows }];
    const ordered = ["오늘", "이전 7일", "이전"];
    const buckets = new Map<string, NoteLibraryItem[]>();
    for (const row of rows) {
      const label = noteDateGroup(row.updated_at);
      const bucket = buckets.get(label) ?? [];
      bucket.push(row);
      buckets.set(label, bucket);
    }
    return ordered.flatMap((label) => {
      const bucket = buckets.get(label);
      return bucket?.length ? [{ label, rows: bucket }] : [];
    });
  }, [rows, sort]);

  return (
    <div className="px-2 pb-3" aria-label={tab === "mine" ? "내 메모 목록" : "공유받은 메모 목록"}>
      {groups.map((group) => (
        <section key={group.label} aria-label={group.label}>
          <h3
            className="sticky top-0 z-[1] px-2 pb-1 pt-3 text-[10px] font-extrabold uppercase tracking-[0.08em]"
            style={{ background: "var(--notes-list)", color: "var(--color-text-faint)" }}
          >
            {group.label}
          </h3>
          <div className="space-y-1">
            {group.rows.map((row) => {
              const selected = row.page_id === selectedPageId;
              const sharing =
                tab === "mine"
                  ? row.owner_id == null
                    ? "회사 공용"
                    : row.share_count > 0
                      ? `${row.share_count}명과 공유`
                      : "나만 보기"
                  : `${row.owner_name || "팀원"} · ${row.permission === "edit" ? "편집 가능" : "보기 전용"}`;
              return (
                <button
                  key={row.page_id}
                  type="button"
                  onClick={() => onOpen(row)}
                  className="min-h-[96px] w-full rounded-[9px] px-3 py-2.5 text-left transition-colors hover:bg-[var(--color-toolbar-hover)] focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--notes-accent)]"
                  style={{
                    background: selected ? "var(--notes-accent-soft)" : "transparent",
                    contentVisibility: "auto",
                    containIntrinsicSize: "96px",
                  }}
                  aria-current={selected ? "page" : undefined}
                >
                  <span className="flex min-w-0 items-baseline gap-2">
                    <span className="min-w-0 flex-1 truncate text-[var(--text-body-sm)] font-bold" style={{ color: "var(--color-text-strong)" }}>
                      {row.title || "제목 없음"}
                    </span>
                    <time
                      dateTime={row.updated_at ?? undefined}
                      className="shrink-0 text-[10px] tabular-nums"
                      style={{ color: selected ? "var(--notes-accent)" : "var(--color-text-faint)" }}
                    >
                      {formatUpdated(row.updated_at)}
                    </time>
                  </span>
                  <span className="mt-1 line-clamp-2 min-h-8 text-[var(--text-meta)] leading-4" style={{ color: "var(--color-text-muted)" }}>
                    {row.preview || "내용 없음"}
                  </span>
                  <span className="mt-1.5 flex min-w-0 items-center gap-1.5 overflow-hidden text-[10px]" style={{ color: "var(--color-text-faint)" }}>
                    {row.projects.length > 0 ? (
                      <span className="flex min-w-0 items-center gap-1.5 overflow-hidden">
                        {row.projects.slice(0, 2).map((project) => (
                          <span key={project.project_id} className="inline-flex min-w-0 items-center gap-1">
                            {projectDot(project.color)}
                            <span className="max-w-[84px] truncate">{project.name}</span>
                          </span>
                        ))}
                        {row.projects.length > 2 ? <span>+{row.projects.length - 2}</span> : null}
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1"><Folder size={10} /> 분류 안 됨</span>
                    )}
                    <span aria-hidden>·</span>
                    <span className="ml-auto inline-flex min-w-0 items-center gap-1">
                      <Users size={10} className="shrink-0" />
                      <span className="truncate">{sharing}</span>
                      {tab === "shared" && row.acknowledged_at ? (
                        <Check size={10} className="shrink-0" style={{ color: "var(--color-progress)" }} />
                      ) : null}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}

function ResponsiveNoteEditor({
  note,
  docked,
  outsideFilter,
  onClose,
  onDelete,
  onAcknowledged,
  onProjectsChanged,
  onSharesChanged,
  onRootBodyChanged,
  onRootTitleChanged,
}: {
  note: OpenNote;
  docked: boolean;
  outsideFilter: boolean;
  onClose: () => void;
  onDelete: (pageId: string) => void | Promise<void>;
  onAcknowledged: (pageId: string) => void;
  onProjectsChanged: (pageId: string, links: NoteProjectLink[]) => void;
  onSharesChanged: (pageId: string, shares: NoteShare[]) => void;
  onRootBodyChanged: (pageId: string, body: string) => void;
  onRootTitleChanged: (
    pageId: string,
    kind: Tab,
    permission: "view" | "edit" | null,
    title: string,
  ) => void;
}) {
  const [metaOpen, setMetaOpen] = useState(
    note.kind === "shared" && note.acknowledged_at == null,
  );
  const dialogRef = useRef<HTMLElement>(null);
  const readOnly = note.kind === "shared" && note.permission !== "edit";

  useEffect(() => {
    if (docked) return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusableElements = () =>
      [...dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [contenteditable="true"], [tabindex]:not([tabindex="-1"])',
      )].filter(
        (element) => element.offsetParent !== null && !element.matches(":disabled"),
      );
    const frame = window.requestAnimationFrame(() => {
      focusableElements()[0]?.focus();
    });
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = focusableElements();
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!dialog.contains(active) || (!event.shiftKey && active === last)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (!desktopEditorSnapshot()) {
        window.requestAnimationFrame(() => previousFocus?.focus());
      }
    };
  }, [docked, onClose]);

  return (
    <>
      {!docked ? (
        <button
          type="button"
          className="fixed inset-0 z-[59] bg-black/25"
          onClick={onClose}
          aria-label="메모 편집기 닫기"
        />
      ) : null}
      <section
        ref={dialogRef}
        className={
          docked
            ? "relative flex min-h-0 min-w-0 flex-col"
            : "fixed inset-y-0 right-0 z-[60] flex h-full w-full max-w-[720px] flex-col shadow-[var(--shadow-modal)]"
        }
        style={{ background: "var(--notes-paper)", borderLeft: "1px solid var(--color-divider)" }}
        role={docked ? undefined : "dialog"}
        aria-modal={docked ? undefined : true}
        aria-label={note.title || "메모"}
        tabIndex={docked ? undefined : -1}
      >
        <header
          className="flex min-h-[52px] shrink-0 items-center gap-2 border-b px-3"
          style={{ borderColor: "var(--color-divider-soft)", paddingTop: docked ? undefined : "env(safe-area-inset-top)" }}
        >
          {!docked ? (
            <button
              type="button"
              onClick={onClose}
              className="grid h-11 w-11 shrink-0 place-items-center rounded-[7px] hover:bg-[var(--color-toolbar-hover)] min-[760px]:h-9 min-[760px]:w-9"
              style={{ color: "var(--color-text-muted)" }}
              aria-label="닫기"
            >
              <X size={16} />
            </button>
          ) : null}
          <div className="min-w-0 flex-1">
            <p className="truncate text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
              {note.kind === "mine" ? "내 메모" : "공유받은 메모"}
            </p>
            {outsideFilter ? (
              <p className="truncate text-[10px] font-semibold" style={{ color: "var(--notes-accent)" }}>
                현재 프로젝트 필터 밖의 메모
              </p>
            ) : null}
          </div>
          <button
            type="button"
            onClick={() => setMetaOpen((current) => !current)}
            className="inline-flex h-11 shrink-0 items-center gap-1.5 rounded-[7px] px-2.5 text-[var(--text-meta)] font-bold hover:bg-[var(--color-toolbar-hover)] min-[760px]:h-9"
            style={{ color: metaOpen ? "var(--notes-accent)" : "var(--color-text-muted)" }}
            aria-expanded={metaOpen}
          >
            <Folder size={14} />
            <span className="hidden min-[420px]:inline">정리 · 공유</span>
            <ChevronDown size={12} style={{ transform: metaOpen ? "rotate(180deg)" : undefined }} />
          </button>
          {note.kind === "mine" ? (
            <button
              type="button"
              onClick={() => void onDelete(note.page_id)}
              className="grid h-11 w-11 shrink-0 place-items-center rounded-[7px] hover:bg-[var(--color-toolbar-hover)] min-[760px]:h-9 min-[760px]:w-9"
              style={{ color: "var(--color-attention)" }}
              aria-label="메모 삭제"
            >
              <Trash2 size={15} />
            </button>
          ) : null}
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          {readOnly ? (
            <div
              className="border-b px-4 py-2 text-center text-[var(--text-meta)] font-semibold"
              style={{
                background: "color-mix(in srgb, var(--color-warn-bg) 70%, var(--notes-paper))",
                borderColor: "var(--color-divider-soft)",
                color: "var(--color-warn-fg)",
              }}
            >
              읽기 전용 공유 · 내용을 확인한 뒤 정리 · 공유에서 얼라인 확인을 눌러주세요.
            </div>
          ) : null}

          <div className="mx-auto w-full max-w-[780px] px-5 pb-20 pt-4 min-[760px]:px-8 min-[760px]:pt-6">
            {metaOpen ? (
              <div className="mb-5 overflow-hidden rounded-[10px] border" style={{ borderColor: "var(--color-divider-soft)", background: "var(--color-panel)" }}>
                <NoteMetaPanel
                  key={note.page_id}
                  pageId={note.page_id}
                  canWrite={note.kind === "mine" || note.permission === "edit"}
                  recipient={
                    note.kind === "shared" ? { acknowledged_at: note.acknowledged_at } : null
                  }
                  embedded
                  onAcknowledged={() => onAcknowledged(note.page_id)}
                  onProjectsChanged={onProjectsChanged}
                  onSharesChanged={onSharesChanged}
                />
              </div>
            ) : null}

            <PageStack
              key={`editor:${note.page_id}`}
              rootPageId={note.page_id}
              rootSeed={{ title: note.title, icon: note.icon }}
              hideRootIcon
              minHeight={docked ? 520 : 400}
              draftPersistence={
                note.kind === "mine" && note.owner_id === getOfflineNoteOwner()?.userId
              }
              autoTitleFromBody={note.kind === "mine"}
              readOnly={readOnly}
              onRootBody={onRootBodyChanged}
              onRootTitle={(title) =>
                onRootTitleChanged(note.page_id, note.kind, note.permission, title)
              }
            />
          </div>
        </div>
      </section>
    </>
  );
}

function EmptyNoteEditor() {
  return (
    <section
      className="grid min-h-0 min-w-0 place-items-center px-8"
      style={{ background: "var(--notes-paper)" }}
      aria-label="선택된 메모 없음"
    >
      <div className="max-w-[260px] text-center">
        <span
          className="mx-auto grid h-16 w-16 place-items-center rounded-[18px]"
          style={{ background: "var(--notes-accent-soft)", color: "var(--notes-accent)" }}
          aria-hidden
        >
          <FileText size={28} strokeWidth={1.7} />
        </span>
        <p className="mt-4 text-[var(--text-body-sm)] font-bold" style={{ color: "var(--color-text-strong)" }}>
          메모를 선택하세요
        </p>
        <p className="mt-1 text-[var(--text-meta)] leading-5" style={{ color: "var(--color-text-muted)" }}>
          왼쪽 목록에서 메모를 고르면 이 종이 위에서 바로 읽고 편집할 수 있습니다.
        </p>
      </div>
    </section>
  );
}
