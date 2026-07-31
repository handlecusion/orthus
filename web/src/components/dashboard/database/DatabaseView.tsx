"use client";

/**
 * 노션식 데이터베이스 뷰 — 표(table) + 보드(kanban). 인라인/전체 페이지 공용.
 *
 * "칸반 보드 = 표 = 데이터베이스"라는 노션 모델대로, 하나의 행 컬렉션을 표/보드로
 * 전환해 본다. board view는 select/status 속성으로 group_by 하고(노션 status는 컬럼
 * 그룹 순서대로 정렬), 카드를 컬럼 사이로 드래그하면 그 속성 값이 바뀐다. 행은 노션처럼
 * 페이지(아이콘+제목+속성+본문)로 열린다. 속성·옵션(=칸반 컬럼)·행을 모두 인라인으로
 * 추가/편집하고, 모든 변경은 낙관적 업데이트 후 API에 영속화한다.
 *
 * databaseId 하나만 받아 자체적으로 번들(+팀 멤버)을 가져오므로, BlockNote 커스텀
 * 블록(인라인) 또는 전체 페이지 라우트에서 그대로 쓴다.
 */

import {
  type DragEvent,
  type MouseEvent as ReactMouseEvent,
  useCallback,
  useContext,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Copy,
  EyeOff,
  ExternalLink,
  Trash2,
} from "lucide-react";
import { SubpageNavContext } from "@/components/dashboard/subpageNav";
import {
  api,
  API_BASE,
  ApiError,
  type DbPropertyDef,
  type DbPropType,
  type DbViewFilterDef,
  type DbViewDef,
  type DbViewSortDef,
  type ProjectDatabase,
  type ProjectDatabaseBundle,
  type ProjectDatabaseRow,
  type TeamMember,
} from "@/lib/api";
import { listTeamMembersCached } from "@/lib/dashboard-cache";
import {
  Modal,
  TAG_COLORS,
  TagPill,
  anchoredStyle,
  memberColorMap,
  useAnchoredPopover,
  type TagColor,
} from "@/components/dashboard/kit";
import { cx } from "@/components/ui";
import {
  FileStrip,
  MediaCover,
  MediaGallery,
  normalizePeople,
  PropValue,
  RowAvatar,
  normalizeFiles,
  type MemberOption,
  type RowMeta,
} from "./cells";
import { type OptionSelectHandlers } from "./OptionSelect";
import {
  ADDABLE_TYPES,
  PROP_TYPE_ICON,
  PROP_TYPE_LABEL,
  findOption,
  groupRowsByOption,
  isOptionType,
  isReadonlyType,
  isSelectLike,
  OPTION_PALETTE,
  orderedOptions,
  rowTitle,
  shortId,
  titleProperty,
} from "./helpers";

// 행 본문 에디터. RichBody는 스키마(subpage/database 블록)를 통해 DatabaseView를
// 다시 import하므로 정적 순환을 피하려 동적 로드한다(ssr 불필요).
const RichBodyEditor = dynamic(
  () => import("@/components/dashboard/RichBody").then((m) => m.RichBodyEditor),
  { ssr: false },
);

const EMOJI_CHOICES = ["📄", "📝", "✅", "🎯", "🐛", "🚀", "📌", "🔧", "💡", "📊", "🎥", "🗂️"];

/* ---- shared context passed down to table/board/peek ---- */
type DbCtx = {
  db: ProjectDatabase;
  view: DbViewDef;
  rows: ProjectDatabaseRow[];
  visibleProps: DbPropertyDef[];
  members: MemberOption[];
  titleProp: DbPropertyDef;
  handlersFor: (propId: string) => OptionSelectHandlers;
  setRowProp: (rowId: string, propId: string, value: unknown) => void;
  setRowField: (
    rowId: string,
    patch: { icon?: string | null; cover?: string | null; body?: string | null },
  ) => void;
  addRow: (initial?: Record<string, unknown>) => Promise<string | null>;
  duplicateRow: (rowId: string, visibleRowIds?: string[]) => Promise<string | null>;
  deleteRow: (rowId: string) => void;
  moveRow: (rowId: string, groupPropId: string, optionId: string | null, sort: number) => void;
  moveRowOrder: (rowId: string, visibleRowIds: string[], direction: -1 | 1) => void;
  patchView: (patch: Partial<DbViewDef>) => void;
  addProperty: (type: DbPropType) => void;
  updateProperty: (propId: string, patch: Partial<DbPropertyDef>) => void;
  duplicateProperty: (propId: string) => void;
  moveProperty: (propId: string, direction: -1 | 1) => void;
  deleteProperty: (propId: string) => void;
  openRow: (rowId: string) => void;
};

type BoardColumn = { key: string; label: string; color: TagColor | null };
type BoardDropPlacement = "before" | "after";

function rowMeta(row: ProjectDatabaseRow): RowMeta {
  return { createdAt: row.created_at, updatedAt: row.updated_at };
}

function viewFilters(view: DbViewDef): DbViewFilterDef[] {
  return Array.isArray(view.filters) ? view.filters : [];
}

function viewSorts(view: DbViewDef): DbViewSortDef[] {
  return Array.isArray(view.sorts) ? view.sorts : [];
}

function normalizedNeedle(value: unknown): string {
  return String(value ?? "").trim().toLowerCase();
}

function propValueText(prop: DbPropertyDef, row: ProjectDatabaseRow, members: MemberOption[]): string {
  if (prop.type === "created_time") return row.created_at ?? "";
  if (prop.type === "last_edited_time") return row.updated_at ?? "";
  const value = row.props?.[prop.id];
  if (value == null || value === "") return "";
  if (prop.type === "select" || prop.type === "status") {
    return typeof value === "string" ? (findOption(prop, value)?.name ?? value) : "";
  }
  if (prop.type === "multi_select") {
    return Array.isArray(value)
      ? value.map((id) => findOption(prop, String(id))?.name ?? String(id)).join(" ")
      : "";
  }
  if (prop.type === "person") {
    return normalizePeople(value)
      .map((id) => members.find((m) => m.id === id)?.name ?? id)
      .join(" ");
  }
  if (prop.type === "checkbox") return value === true ? "checked true 완료" : "unchecked false";
  if (prop.type === "files") return normalizeFiles(value).map((file) => file.name).join(" ");
  return String(value);
}

function hasPropValue(prop: DbPropertyDef, row: ProjectDatabaseRow): boolean {
  if (prop.type === "created_time") return !!row.created_at;
  if (prop.type === "last_edited_time") return !!row.updated_at;
  const value = row.props?.[prop.id];
  if (value == null || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (prop.type === "files") return normalizeFiles(value).length > 0;
  return true;
}

function rawPropValue(prop: DbPropertyDef, row: ProjectDatabaseRow): unknown {
  if (prop.type === "created_time") return row.created_at ?? null;
  if (prop.type === "last_edited_time") return row.updated_at ?? null;
  return row.props?.[prop.id] ?? null;
}

function filterMatches(
  filter: DbViewFilterDef,
  props: DbPropertyDef[],
  row: ProjectDatabaseRow,
  members: MemberOption[],
): boolean {
  const prop = props.find((p) => p.id === filter.prop_id);
  if (!prop) return true;
  const raw = rawPropValue(prop, row);
  const text = propValueText(prop, row, members).toLowerCase();
  switch (filter.op) {
    case "is_empty":
      return !hasPropValue(prop, row);
    case "is_not_empty":
      return hasPropValue(prop, row);
    case "checked":
      return raw === true;
    case "unchecked":
      return raw !== true;
    case "contains":
      return text.includes(normalizedNeedle(filter.value));
    case "is": {
      if (Array.isArray(raw)) return raw.map(String).includes(String(filter.value ?? ""));
      return String(raw ?? "") === String(filter.value ?? "");
    }
    case "is_not": {
      if (Array.isArray(raw)) return !raw.map(String).includes(String(filter.value ?? ""));
      return String(raw ?? "") !== String(filter.value ?? "");
    }
    default:
      return true;
  }
}

function sortKey(prop: DbPropertyDef, row: ProjectDatabaseRow, members: MemberOption[]): string | number {
  const raw = rawPropValue(prop, row);
  if (prop.type === "number") {
    const n = typeof raw === "number" ? raw : Number(raw);
    return Number.isFinite(n) ? n : Number.POSITIVE_INFINITY;
  }
  if (prop.type === "date" || prop.type === "created_time" || prop.type === "last_edited_time") {
    const ts = raw ? new Date(String(raw)).getTime() : Number.POSITIVE_INFINITY;
    return Number.isFinite(ts) ? ts : Number.POSITIVE_INFINITY;
  }
  if (prop.type === "checkbox") return raw === true ? 1 : 0;
  return propValueText(prop, row, members).toLowerCase();
}

function applyViewRows(
  rows: ProjectDatabaseRow[],
  props: DbPropertyDef[],
  view: DbViewDef,
  search: string,
  members: MemberOption[],
): ProjectDatabaseRow[] {
  const q = normalizedNeedle(search);
  let out = rows.filter((row) => {
    if (q) {
      const haystack = props.map((p) => propValueText(p, row, members)).join(" ").toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return viewFilters(view).every((filter) => filterMatches(filter, props, row, members));
  });

  const sorts = viewSorts(view).filter((sort) => props.some((p) => p.id === sort.prop_id));
  if (sorts.length) {
    out = [...out].sort((a, b) => {
      for (const sort of sorts) {
        const prop = props.find((p) => p.id === sort.prop_id);
        if (!prop) continue;
        const av = sortKey(prop, a, members);
        const bv = sortKey(prop, b, members);
        let cmp = 0;
        if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
        else cmp = String(av).localeCompare(String(bv), "ko");
        if (cmp !== 0) return sort.direction === "desc" ? -cmp : cmp;
      }
      return (a.sort_order ?? 0) - (b.sort_order ?? 0);
    });
  } else {
    out = [...out].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
  }
  return out;
}

function sortBetween(prev: ProjectDatabaseRow | null, next: ProjectDatabaseRow | null): number {
  if (prev && next) return ((prev.sort_order ?? 0) + (next.sort_order ?? 0)) / 2;
  if (prev) return (prev.sort_order ?? 0) + 1;
  if (next) return (next.sort_order ?? 0) - 1;
  return 1;
}

const COLUMN_WIDTH_MIN = 96;
const COLUMN_WIDTH_MAX = 720;

function defaultColumnWidth(prop: DbPropertyDef): number {
  if (prop.type === "title") return 260;
  if (prop.type === "files") return 260;
  if (prop.type === "person" || isOptionType(prop.type)) return 180;
  return 150;
}

function clampColumnWidth(width: number): number {
  return Math.max(COLUMN_WIDTH_MIN, Math.min(COLUMN_WIDTH_MAX, Math.round(width)));
}

function cloneRowProps(props: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(props ?? {})) as Record<string, unknown>;
}

function cloneJsonValue(value: unknown): unknown {
  if (value === undefined || value === null) return value;
  return JSON.parse(JSON.stringify(value)) as unknown;
}

function copiedPropertyName(properties: DbPropertyDef[], sourceName: string): string {
  const names = new Set(properties.map((property) => property.name));
  const base = `${sourceName} 복사본`;
  if (!names.has(base)) return base;
  let index = 2;
  while (names.has(`${base} ${index}`)) index += 1;
  return `${base} ${index}`;
}

function copiedPropertyValue(
  prop: DbPropertyDef,
  value: unknown,
  optionIdMap: Map<string, string>,
): unknown {
  if (value === undefined) return undefined;
  if (prop.type === "select" || prop.type === "status") {
    return typeof value === "string" ? (optionIdMap.get(value) ?? value) : value;
  }
  if (prop.type === "multi_select") {
    if (!Array.isArray(value)) return [];
    return value
      .filter((item): item is string => typeof item === "string")
      .map((id) => optionIdMap.get(id) ?? id);
  }
  return cloneJsonValue(value);
}

function copiedViewName(views: DbViewDef[], sourceName: string): string {
  const names = new Set(views.map((view) => view.name));
  const base = `${sourceName} 복사본`;
  if (!names.has(base)) return base;
  let index = 2;
  while (names.has(`${base} ${index}`)) index += 1;
  return `${base} ${index}`;
}

export function DatabaseView({
  databaseId,
  onDeleteDatabase,
  fullPage = false,
  readOnly = false,
  initialRowId = null,
  initialBundle = null,
}: {
  databaseId: string;
  onDeleteDatabase?: () => void;
  /** 전체 페이지 모드(테두리 없음, 넓은 레이아웃). */
  fullPage?: boolean;
  /** 공유 view 권한: 내용은 읽되 모든 native/custom mutation interaction을 잠근다. */
  readOnly?: boolean;
  /** 마운트 시 자동으로 열 행(전체 페이지 ?row= 딥링크). */
  initialRowId?: string | null;
  /** 부모 라우트가 이미 받은 번들 — 있으면 같은 번들을 다시 다운로드하지 않는다. */
  initialBundle?: ProjectDatabaseBundle | null;
}) {
  const router = useRouter();
  // 부모 페이지의 네비게이션 스택이 있으면 "페이지로 열기"를 그 안에서 연다(노션식).
  const { onOpenDatabase } = useContext(SubpageNavContext);
  const seeded = initialBundle?.database.database_id === databaseId ? initialBundle : null;
  const [db, setDb] = useState<ProjectDatabase | null>(seeded?.database ?? null);
  const [rows, setRows] = useState<ProjectDatabaseRow[]>(seeded?.rows ?? []);
  const [members, setMembers] = useState<MemberOption[]>([]);
  const [activeViewId, setActiveViewId] = useState<string | null>(
    seeded?.database.views[0]?.id ?? null,
  );
  const [peekRowId, setPeekRowId] = useState<string | null>(initialRowId);
  // body 지연 로드 중인 행 id — 행 id로 키를 잡아 다른 행으로 전환 시 상태가 새지 않는다.
  // 딥링크(initialRowId)도 body 확언 전에는 빈 에디터가 마운트되지 않게 loading으로 시작.
  const [peekBodyLoadingId, setPeekBodyLoadingId] = useState<string | null>(initialRowId);
  // body 지연 로드 실패 행 id — 서버 확언 없이는 절대 빈 편집 에디터를 열지 않는다.
  const [peekBodyErrorId, setPeekBodyErrorId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);
  // 셀/행 저장 실패 표시 — load 시점 error와 별도다. 기존 error는 `error && !db`
  // 게이트 때문에 db 로드 후에는 절대 렌더되지 않아, 저장 실패가 화면에 안 보이고
  // 새로고침하면 유실됐다(사용자는 저장됐다고 착각). db 로드 후에도 항상 노출한다.
  const [saveError, setSaveError] = useState<string | null>(null);

  const dbRef = useRef<ProjectDatabase | null>(seeded?.database ?? null);
  const rowsRef = useRef<ProjectDatabaseRow[]>(seeded?.rows ?? []);
  // body가 권위적으로 확인된 행(단건 조회/생성 응답/본문 편집) — 번들 목록 행은
  // body가 생략돼(null) 있어, 이 집합에 없으면 열 때 지연 로드가 필요하다.
  const bodyKnownRef = useRef<Set<string>>(new Set());

  const setDbBoth = useCallback((next: ProjectDatabase) => {
    dbRef.current = next;
    setDb(next);
  }, []);
  const setRowsBoth = useCallback((next: ProjectDatabaseRow[]) => {
    rowsRef.current = next;
    setRows(next);
  }, []);

  useEffect(() => {
    let alive = true;
    // initialBundle이 이 databaseId의 번들이면 재다운로드 없이 그대로 쓴다.
    const seededBundle =
      initialBundle?.database.database_id === databaseId ? initialBundle : null;
    Promise.all([
      // 목록 렌더에는 body가 필요 없어 opt-in 생략으로 받는다(열 때 지연 로드).
      seededBundle
        ? Promise.resolve(seededBundle)
        : api.getProjectDatabase(databaseId, { body: "none" }),
      listTeamMembersCached(),
    ])
      .then(([bundle, mb]) => {
        if (!alive) return;
        setDbBoth(bundle.database);
        // body=none 번들로 rows를 통째로 교체할 때, 이미 지연 로드된 body는 보존한다.
        // 안 하면 (strict-mode 이중 fetch/재조회의) 늦은 응답이 로드된 body를 null로
        // 되돌리는데 bodyKnownRef는 남아 있어 실 본문 위에 빈 에디터가 마운트된다.
        const prevBodies = new Map(
          rowsRef.current.filter((r) => r.body != null).map((r) => [r.row_id, r.body]),
        );
        setRowsBoth(
          bundle.rows.map((r) =>
            r.body == null && prevBodies.has(r.row_id)
              ? { ...r, body: prevBodies.get(r.row_id) ?? null }
              : r,
          ),
        );
        const colors = memberColorMap(mb);
        setMembers(
          mb.map((m: TeamMember) => ({
            id: m.member_id,
            name: m.name,
            color: colors.get(m.member_id) ?? "var(--channel-slate)",
          })),
        );
        setActiveViewId(bundle.database.views[0]?.id ?? null);
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : "불러오기 실패"));
    return () => {
      alive = false;
    };
  }, [databaseId, initialBundle, setDbBoth, setRowsBoth]);

  // 실시간 반영: 에이전트(orthus ticket)나 다른 화면이 이 보드의 행/스키마를 바꾸면
  // 서버가 내용 없는 변경 핑(project_board_changed)을 SSE로 보낸다. 받으면 스켈레톤
  // 없이 번들만 조용히 재조회한다(지연 로드된 body 보존 — 초기 로드와 동일 병합).
  const liveReloadRef = useRef<() => void>(() => {});
  useEffect(() => {
    liveReloadRef.current = () => {
      void api
        .getProjectDatabase(databaseId, { body: "none" })
        .then((bundle) => {
          const prevBodies = new Map(
            rowsRef.current.filter((r) => r.body != null).map((r) => [r.row_id, r.body]),
          );
          setDbBoth(bundle.database);
          setRowsBoth(
            bundle.rows.map((r) =>
              r.body == null && prevBodies.has(r.row_id)
                ? { ...r, body: prevBodies.get(r.row_id) ?? null }
                : r,
            ),
          );
        })
        .catch(() => {
          /* 일시 오류 — 다음 핑에서 재시도 */
        });
    };
  }, [databaseId, setDbBoth, setRowsBoth]);
  useEffect(() => {
    let es: EventSource | null = null;
    let backoff: ReturnType<typeof setTimeout> | null = null;
    let debounce: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    const connect = () => {
      if (closed) return;
      try {
        es = new EventSource(`${API_BASE}/dashboard/stream`, { withCredentials: true });
      } catch {
        return;
      }
      es.onmessage = (e) => {
        try {
          const ev = JSON.parse(e.data) as { type?: string; database_id?: string };
          if (ev?.type !== "project_board_changed" || ev.database_id !== databaseId) return;
          if (debounce) clearTimeout(debounce);
          // 연속 변경(에이전트 다건 생성)은 1회 재조회로 병합.
          debounce = setTimeout(() => liveReloadRef.current(), 400);
        } catch {
          /* keepalive 등 비-JSON 무시 */
        }
      };
      es.onerror = () => {
        es?.close();
        if (!closed) backoff = setTimeout(connect, 5000);
      };
    };
    connect();
    return () => {
      closed = true;
      es?.close();
      if (backoff) clearTimeout(backoff);
      if (debounce) clearTimeout(debounce);
    };
  }, [databaseId]);

  /* ---- database (properties/views/title) mutations ---- */
  const commitDb = useCallback(
    (next: ProjectDatabase) => {
      setDbBoth(next);
      void api
        .updateProjectDatabase(next.database_id, {
          title: next.title,
          icon: next.icon,
          properties: next.properties,
          views: next.views,
        })
        .catch(() => setError("저장 실패"));
    },
    [setDbBoth],
  );

  // 마지막으로 실패한 행 저장 — saveError 배너의 "재시도"가 그대로 다시 보낸다.
  const lastFailedRowSaveRef = useRef<{ rowId: string; patch: Record<string, unknown> } | null>(
    null,
  );

  const persistRow = useCallback(
    (rowId: string, patch: Record<string, unknown>) => {
      void api
        .updateDatabaseRow(databaseId, rowId, patch)
        .then(() => {
          lastFailedRowSaveRef.current = null;
          setSaveError(null);
        })
        .catch(() => {
          lastFailedRowSaveRef.current = { rowId, patch };
          setSaveError("저장 실패 — 새로고침 전에 다시 시도하세요");
        });
    },
    [databaseId],
  );

  const retryLastRowSave = useCallback(() => {
    const last = lastFailedRowSaveRef.current;
    if (last) persistRow(last.rowId, last.patch);
  }, [persistRow]);

  /** 번들 목록이 생략한 body를 단건 행 endpoint로 지연 로드해 rows에 합친다.
   *  서버가 body를 확언(성공 응답)한 경우에만 bodyKnownRef에 기록한다 — 5xx 같은
   *  일시 실패의 null을 권위값으로 오인하면 실제 본문을 빈 편집으로 덮어쓴다.
   *  단건 endpoint 404(구 API 배포 skew)만 body 포함 기본 번들 재조회로 fallback. */
  const ensureRowBody = useCallback(
    async (rowId: string): Promise<ProjectDatabaseRow | null> => {
      const row = rowsRef.current.find((r) => r.row_id === rowId) ?? null;
      if (!row || row.body != null || bodyKnownRef.current.has(rowId)) return row;
      const applyBody = (body: string | null) => {
        bodyKnownRef.current.add(rowId);
        const next = rowsRef.current.map((r) => (r.row_id === rowId ? { ...r, body } : r));
        setRowsBoth(next);
        return next.find((r) => r.row_id === rowId) ?? null;
      };
      try {
        const full = await api.getProjectDatabaseRow(databaseId, rowId);
        return applyBody(full.body ?? null);
      } catch (e) {
        // 404 외 실패(일시 5xx/네트워크)는 확언이 아니다 — 표시 없이 그대로 반환.
        if (!(e instanceof ApiError) || e.status !== 404) return row;
        try {
          // 구 API는 body=none 파라미터가 없으므로 기본(body 포함) 번들로 복원한다.
          const bundle = await api.getProjectDatabase(databaseId);
          const found = bundle.rows.find((r) => r.row_id === rowId);
          if (!found) return row; // 번들에도 없음(삭제 등) — 확언 아님.
          return applyBody(found.body ?? null);
        } catch {
          return row; // fallback도 실패 — known 표시 없이 다음 시도에 맡긴다.
        }
      }
    },
    [databaseId, setRowsBoth],
  );

  const setRowProp = useCallback(
    (rowId: string, propId: string, value: unknown) => {
      const next = rowsRef.current.map((r) =>
        r.row_id === rowId ? { ...r, props: { ...r.props, [propId]: value } } : r,
      );
      setRowsBoth(next);
      const row = next.find((r) => r.row_id === rowId);
      if (row) persistRow(rowId, { props: row.props });
    },
    [persistRow, setRowsBoth],
  );

  const setRowField = useCallback(
    (
      rowId: string,
      patch: { icon?: string | null; cover?: string | null; body?: string | null },
    ) => {
      if ("body" in patch) bodyKnownRef.current.add(rowId); // 편집한 본문이 권위값.
      const next = rowsRef.current.map((r) => (r.row_id === rowId ? { ...r, ...patch } : r));
      setRowsBoth(next);
      persistRow(rowId, patch);
    },
    [persistRow, setRowsBoth],
  );

  const addRow = useCallback(
    async (initial: Record<string, unknown> = {}) => {
      try {
        const row = await api.createDatabaseRow(databaseId, { props: initial });
        bodyKnownRef.current.add(row.row_id); // 생성 응답이 권위 body.
        setRowsBoth([...rowsRef.current, row]);
        return row.row_id;
      } catch {
        setError("행 추가 실패");
        return null;
      }
    },
    [databaseId, setRowsBoth],
  );

  const duplicateRow = useCallback(
    async (rowId: string, visibleRowIds?: string[]) => {
      let source = rowsRef.current.find((row) => row.row_id === rowId);
      if (!source) return null;
      // 목록 조회(body=none) 행은 body가 생략돼 있어 복제 전에 지연 로드한다.
      if (source.body == null && !bodyKnownRef.current.has(rowId)) {
        source = (await ensureRowBody(rowId)) ?? source;
        if (source.body == null && !bodyKnownRef.current.has(rowId)) {
          // body 미확언 상태로 복제하면 본문이 조용히 유실된다 — 복제 중단.
          setError("행 복제 실패");
          return null;
        }
      }
      const orderedScope =
        visibleRowIds?.length
          ? visibleRowIds
              .map((id) => rowsRef.current.find((row) => row.row_id === id))
              .filter((row): row is ProjectDatabaseRow => !!row)
          : [...rowsRef.current].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
      const sourceIndex = orderedScope.findIndex((row) => row.row_id === rowId);
      const nextRow = sourceIndex >= 0 ? (orderedScope[sourceIndex + 1] ?? null) : null;
      try {
        const row = await api.createDatabaseRow(databaseId, {
          props: cloneRowProps(source.props ?? {}),
          sort_order: sortBetween(source, nextRow),
          icon: source.icon ?? null,
          cover: source.cover ?? null,
          body: source.body ?? null,
        });
        bodyKnownRef.current.add(row.row_id);
        setRowsBoth([...rowsRef.current, row]);
        return row.row_id;
      } catch {
        setError("행 복제 실패");
        return null;
      }
    },
    [databaseId, ensureRowBody, setRowsBoth],
  );

  const deleteRow = useCallback(
    (rowId: string) => {
      setRowsBoth(rowsRef.current.filter((r) => r.row_id !== rowId));
      void api.deleteDatabaseRow(databaseId, rowId).catch(() => setError("삭제 실패"));
    },
    [databaseId, setRowsBoth],
  );

  // 열기 시점(이벤트)에 body 로드 필요 여부를 동기로 정해 빈 에디터가
  // 한 프레임 먼저 마운트되는 것을 막는다(실제 fetch는 peek effect가 수행).
  // 이전 실패 상태는 초기화해 다시 열면 새로 시도한다.
  const openRow = useCallback((rowId: string) => {
    const row = rowsRef.current.find((r) => r.row_id === rowId);
    const needsBody = !!row && row.body == null && !bodyKnownRef.current.has(rowId);
    setPeekBodyErrorId(null);
    setPeekBodyLoadingId(needsBody ? rowId : null);
    setPeekRowId(rowId);
  }, []);

  const moveRow = useCallback(
    (rowId: string, groupPropId: string, optionId: string | null, sort: number) => {
      const next = rowsRef.current.map((r) =>
        r.row_id === rowId
          ? { ...r, props: { ...r.props, [groupPropId]: optionId }, sort_order: sort }
          : r,
      );
      setRowsBoth(next);
      const row = next.find((r) => r.row_id === rowId);
      if (row) persistRow(rowId, { props: row.props, sort_order: sort });
    },
    [persistRow, setRowsBoth],
  );

  const moveRowOrder = useCallback(
    (rowId: string, visibleRowIds: string[], direction: -1 | 1) => {
      const visibleIndex = visibleRowIds.indexOf(rowId);
      const targetId = visibleRowIds[visibleIndex + direction];
      if (visibleIndex < 0 || !targetId) return;

      // 전 행 renumber(행 수만큼 PATCH) 대신, 전역 정렬에서 target의 이동 방향쪽
      // 이웃과 target 사이 fractional sort_order로 이동 행 1개만 PATCH한다.
      const ordered = [...rowsRef.current]
        .filter((row) => row.row_id !== rowId)
        .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
      const targetIndex = ordered.findIndex((row) => row.row_id === targetId);
      if (targetIndex < 0) return;
      const prevRow = direction < 0 ? (ordered[targetIndex - 1] ?? null) : ordered[targetIndex];
      const nextRow = direction < 0 ? ordered[targetIndex] : (ordered[targetIndex + 1] ?? null);
      const sort = sortBetween(prevRow, nextRow);
      setRowsBoth(
        rowsRef.current.map((row) => (row.row_id === rowId ? { ...row, sort_order: sort } : row)),
      );
      persistRow(rowId, { sort_order: sort });
    },
    [persistRow, setRowsBoth],
  );

  const addProperty = useCallback(
    (type: DbPropType) => {
      const cur = dbRef.current;
      if (!cur) return;
      const base: DbPropertyDef = {
        id: shortId(),
        name: PROP_TYPE_LABEL[type],
        type,
        options: [],
      };
      commitDb({ ...cur, properties: [...cur.properties, base] });
    },
    [commitDb],
  );

  const updateProperty = useCallback(
    (propId: string, patch: Partial<DbPropertyDef>) => {
      const cur = dbRef.current;
      if (!cur) return;
      commitDb({
        ...cur,
        properties: cur.properties.map((p) => (p.id === propId ? { ...p, ...patch } : p)),
      });
    },
    [commitDb],
  );

  const duplicateProperty = useCallback(
    (propId: string) => {
      const cur = dbRef.current;
      if (!cur) return;
      const prop = cur.properties.find((p) => p.id === propId);
      if (!prop || prop.type === "title") return;
      const propIndex = cur.properties.findIndex((p) => p.id === propId);
      const optionIdMap = new Map<string, string>();
      const copiedProp: DbPropertyDef = {
        ...prop,
        id: shortId(),
        name: copiedPropertyName(cur.properties, prop.name),
        options: prop.options.map((option) => {
          const id = shortId();
          optionIdMap.set(option.id, id);
          return { ...option, id };
        }),
      };
      const properties = [...cur.properties];
      properties.splice(propIndex + 1, 0, copiedProp);
      const nextDb = { ...cur, properties };
      const changedRows: ProjectDatabaseRow[] = [];
      const nextRows = rowsRef.current.map((row) => {
        if (isReadonlyType(prop.type) || !Object.prototype.hasOwnProperty.call(row.props, prop.id)) {
          return row;
        }
        const props = {
          ...row.props,
          [copiedProp.id]: copiedPropertyValue(prop, row.props[prop.id], optionIdMap),
        };
        const nextRow = { ...row, props };
        changedRows.push(nextRow);
        return nextRow;
      });
      setDbBoth(nextDb);
      setRowsBoth(nextRows);
      void api
        .updateProjectDatabase(nextDb.database_id, {
          title: nextDb.title,
          icon: nextDb.icon,
          properties: nextDb.properties,
          views: nextDb.views,
        })
        .then(() =>
          Promise.all(
            changedRows.map((row) =>
              api.updateDatabaseRow(databaseId, row.row_id, { props: row.props }),
            ),
          ),
        )
        .catch(() => setError("속성 복제 저장 실패"));
    },
    [databaseId, setDbBoth, setRowsBoth],
  );

  const moveProperty = useCallback(
    (propId: string, direction: -1 | 1) => {
      const cur = dbRef.current;
      if (!cur) return;
      const idx = cur.properties.findIndex((p) => p.id === propId);
      if (idx < 0 || cur.properties[idx]?.type === "title") return;
      const nextIdx = idx + direction;
      if (nextIdx < 1 || nextIdx >= cur.properties.length) return;
      const properties = [...cur.properties];
      const [prop] = properties.splice(idx, 1);
      properties.splice(nextIdx, 0, prop);
      commitDb({ ...cur, properties });
    },
    [commitDb],
  );

  const deleteProperty = useCallback(
    (propId: string) => {
      const cur = dbRef.current;
      if (!cur) return;
      const prop = cur.properties.find((p) => p.id === propId);
      if (!prop || prop.type === "title") return;
      const properties = cur.properties.filter((p) => p.id !== propId);
      const views = cur.views.map((v) => ({
        ...v,
        group_by: v.group_by === propId ? null : v.group_by,
        cover_property: v.cover_property === propId ? null : v.cover_property,
        hidden: (v.hidden ?? []).filter((id) => id !== propId),
        filters: viewFilters(v).filter((filter) => filter.prop_id !== propId),
        sorts: viewSorts(v).filter((sort) => sort.prop_id !== propId),
      }));
      commitDb({ ...cur, properties, views });
    },
    [commitDb],
  );

  /* option (select column) mutations bound per-property.
     주의: 반환 객체는 호출마다 새로 만들어지지만, 동작은 propId와 컴포넌트-수명
     안정 콜백(commitDb/persistRow/setRowsBoth)에만 의존한다 — PropValue memo는
     이 계약을 근거로 handlers 참조 비교를 생략한다(react-hooks/refs 룰 때문에
     렌더 시점 propId별 객체 캐시는 불가). */
  const handlersFor = useCallback(
    (propId: string): OptionSelectHandlers => ({
      addOption: (name, color) => {
        const cur = dbRef.current!;
        const id = shortId();
        const prop = cur.properties.find((p) => p.id === propId);
        // status 컬럼은 새 옵션을 'to_do' 그룹에 넣는다(노션 기본).
        const opt: { id: string; name: string; color: string; group?: string } = {
          id,
          name,
          color: color ?? "gray",
        };
        if (prop?.type === "status") opt.group = "to_do";
        const properties = cur.properties.map((p) =>
          p.id === propId ? { ...p, options: [...p.options, opt] } : p,
        );
        commitDb({ ...cur, properties });
        return id;
      },
      renameOption: (oid, name) => {
        const cur = dbRef.current!;
        commitDb({
          ...cur,
          properties: cur.properties.map((p) =>
            p.id === propId
              ? { ...p, options: p.options.map((o) => (o.id === oid ? { ...o, name } : o)) }
              : p,
          ),
        });
      },
      recolorOption: (oid, color) => {
        const cur = dbRef.current!;
        commitDb({
          ...cur,
          properties: cur.properties.map((p) =>
            p.id === propId
              ? { ...p, options: p.options.map((o) => (o.id === oid ? { ...o, color } : o)) }
              : p,
          ),
        });
      },
      moveOption: (oid, targetOid, placement) => {
        if (oid === targetOid) return;
        const cur = dbRef.current!;
        commitDb({
          ...cur,
          properties: cur.properties.map((p) => {
            if (p.id !== propId) return p;
            const moving = p.options.find((o) => o.id === oid);
            if (!moving || !p.options.some((o) => o.id === targetOid)) return p;
            const options = p.options.filter((o) => o.id !== oid);
            const targetIndex = options.findIndex((o) => o.id === targetOid);
            options.splice(targetIndex + (placement === "after" ? 1 : 0), 0, moving);
            return { ...p, options };
          }),
        });
      },
      deleteOption: (oid) => {
        const cur = dbRef.current!;
        commitDb({
          ...cur,
          properties: cur.properties.map((p) =>
            p.id === propId ? { ...p, options: p.options.filter((o) => o.id !== oid) } : p,
          ),
        });
        const next = rowsRef.current.map((r) => {
          const v = r.props[propId];
          if (v === oid) {
            const np = { ...r.props, [propId]: null };
            persistRow(r.row_id, { props: np });
            return { ...r, props: np };
          }
          if (Array.isArray(v) && v.includes(oid)) {
            const np = { ...r.props, [propId]: v.filter((x) => x !== oid) };
            persistRow(r.row_id, { props: np });
            return { ...r, props: np };
          }
          return r;
        });
        setRowsBoth(next);
      },
    }),
    [commitDb, persistRow, setRowsBoth],
  );

  const setView = useCallback(
    (viewId: string, patch: Partial<DbViewDef>) => {
      const cur = dbRef.current;
      if (!cur) return;
      commitDb({ ...cur, views: cur.views.map((v) => (v.id === viewId ? { ...v, ...patch } : v)) });
    },
    [commitDb],
  );

  const addView = useCallback(
    (type: "table" | "board") => {
      const cur = dbRef.current;
      if (!cur) return;
      const groupBy =
        type === "board" ? (cur.properties.find((p) => isSelectLike(p.type))?.id ?? null) : null;
      const view: DbViewDef = {
        id: shortId(),
        name: type === "board" ? "보드" : "표",
        type,
        group_by: groupBy,
        cover_property: null,
        hidden: [],
        filters: [],
        sorts: [],
        column_widths: {},
      };
      commitDb({ ...cur, views: [...cur.views, view] });
      setActiveViewId(view.id);
    },
    [commitDb],
  );

  const renameView = useCallback(
    (viewId: string, name: string) => {
      const cur = dbRef.current;
      if (!cur) return;
      const clean = name.trim();
      if (!clean) return;
      commitDb({
        ...cur,
        views: cur.views.map((v) => (v.id === viewId ? { ...v, name: clean } : v)),
      });
    },
    [commitDb],
  );

  const switchViewType = useCallback(
    (viewId: string, type: "table" | "board") => {
      const cur = dbRef.current;
      if (!cur) return;
      const firstGroupProp = cur.properties.find((prop) => isSelectLike(prop.type))?.id ?? null;
      const filePropIds = new Set(cur.properties.filter((prop) => prop.type === "files").map((prop) => prop.id));
      commitDb({
        ...cur,
        views: cur.views.map((view) => {
          if (view.id !== viewId || view.type === type) return view;
          if (type === "board") {
            const groupBy = cur.properties.some(
              (prop) => prop.id === view.group_by && isSelectLike(prop.type),
            )
              ? view.group_by
              : firstGroupProp;
            return {
              ...view,
              type,
              group_by: groupBy,
              cover_property:
                view.cover_property && filePropIds.has(view.cover_property)
                  ? view.cover_property
                  : null,
            };
          }
          return { ...view, type, group_by: null, cover_property: null };
        }),
      });
    },
    [commitDb],
  );

  const deleteView = useCallback(
    (viewId: string) => {
      const cur = dbRef.current;
      if (!cur || cur.views.length <= 1) return;
      const idx = cur.views.findIndex((v) => v.id === viewId);
      const views = cur.views.filter((v) => v.id !== viewId);
      if (!views.length) return;
      commitDb({ ...cur, views });
      if (activeViewId === viewId) {
        setActiveViewId(views[Math.max(0, idx - 1)]?.id ?? views[0].id);
      }
    },
    [activeViewId, commitDb],
  );

  const duplicateView = useCallback(
    (viewId: string) => {
      const cur = dbRef.current;
      if (!cur) return;
      const source = cur.views.find((v) => v.id === viewId);
      if (!source) return;
      const copied: DbViewDef = {
        ...source,
        id: shortId(),
        name: copiedViewName(cur.views, source.name),
        hidden: [...(source.hidden ?? [])],
        filters: viewFilters(source).map((filter) => ({ ...filter, id: shortId() })),
        sorts: viewSorts(source).map((sort) => ({ ...sort, id: shortId() })),
        column_widths: { ...(source.column_widths ?? {}) },
      };
      commitDb({ ...cur, views: [...cur.views, copied] });
      setActiveViewId(copied.id);
    },
    [commitDb],
  );

  const view = db ? (db.views.find((v) => v.id === activeViewId) ?? db.views[0]) : null;
  // 검색 입력은 즉시 반영하되 전 행 필터/정렬은 deferred 값으로만 다시 계산한다
  // (키 입력마다 applyViewRows 전체 재실행 방지).
  const deferredSearch = useDeferredValue(search);
  const viewRows = useMemo(
    () => (db && view ? applyViewRows(rows, db.properties, view, deferredSearch, members) : []),
    [db, view, rows, deferredSearch, members],
  );

  // 열린 행(peek)의 body 지연 로드 — 목록 조회(body=none)는 body가 생략돼 있다.
  // 성공은 rows 갱신(→ effect 재실행의 해제 분기)으로 해제되고, 실패는 에러
  // 상태로 남긴다(빈 편집 에디터 금지 — 재시도 버튼이 에러를 풀면 다시 시도).
  useEffect(() => {
    if (!peekRowId) return;
    const targetId = peekRowId;
    const row = rows.find((r) => r.row_id === targetId);
    if (!row) return;
    if (row.body != null || bodyKnownRef.current.has(targetId)) {
      setPeekBodyLoadingId((cur) => (cur === targetId ? null : cur));
      setPeekBodyErrorId((cur) => (cur === targetId ? null : cur));
      return;
    }
    if (peekBodyErrorId === targetId) return; // 에러 유지 — 자동 재시도 루프 방지.
    // loading 표시는 열기 이벤트 시점에 이미 동기로 걸려 있다(openRow/onRetryBody/
    // 딥링크 초기 state) — effect는 fetch 수행과 결과 반영만 담당한다.
    void ensureRowBody(targetId).then(() => {
      if (bodyKnownRef.current.has(targetId)) return; // 성공 — rows 갱신이 해제.
      // 로드 실패 — 서버 확언이 없으니 에디터 대신 에러 + 재시도를 보여준다.
      setPeekBodyLoadingId((cur) => (cur === targetId ? null : cur));
      setPeekBodyErrorId(targetId);
    });
  }, [peekRowId, rows, peekBodyErrorId, ensureRowBody]);

  if (error && !db) {
    return (
      <div
        className="rounded-[8px] px-3 py-2 text-[var(--text-body-sm)]"
        style={{ background: "var(--color-fail-bg)", color: "var(--color-fail-fg)" }}
      >
        데이터베이스 오류: {error}
      </div>
    );
  }
  if (!db || !view) {
    return (
      <div
        className="rounded-[8px] px-3 py-6 text-center text-[var(--text-body-sm)]"
        style={{ border: "1px solid var(--color-divider)", color: "var(--color-text-faint)" }}
      >
        데이터베이스 불러오는 중…
      </div>
    );
  }

  const titleProp = titleProperty(db);
  const hidden = new Set(view.hidden ?? []);
  const visibleProps = db.properties.filter((p) => p.type === "title" || !hidden.has(p.id));
  const ctx: DbCtx = {
    db,
    view,
    rows: viewRows,
    visibleProps,
    members,
    titleProp,
    handlersFor,
    setRowProp,
    setRowField,
    addRow,
    duplicateRow,
    deleteRow,
    moveRow,
    moveRowOrder,
    patchView: (patch) => setView(view.id, patch),
    addProperty,
    updateProperty,
    duplicateProperty,
    moveProperty,
    deleteProperty,
    openRow,
  };
  const peekRow = peekRowId ? (rows.find((r) => r.row_id === peekRowId) ?? null) : null;

  return (
    <fieldset
      contentEditable={false}
      disabled={readOnly}
      aria-readonly={readOnly || undefined}
      aria-label={readOnly ? "읽기 전용 데이터베이스" : undefined}
      onMouseDown={(e) => e.stopPropagation()}
      onClickCapture={(event) => {
        if (!readOnly) return;
        event.preventDefault();
        event.stopPropagation();
      }}
      onKeyDownCapture={(event) => {
        if (!readOnly || (event.key !== "Enter" && event.key !== " ")) return;
        event.preventDefault();
        event.stopPropagation();
      }}
      onDragStartCapture={(event) => {
        if (!readOnly) return;
        event.preventDefault();
        event.stopPropagation();
      }}
      onDropCapture={(event) => {
        if (!readOnly) return;
        event.preventDefault();
        event.stopPropagation();
      }}
      className={cx(
        "min-w-0 border-0 p-0 select-text",
        fullPage ? "m-0" : "mx-0 my-2 rounded-[10px]",
      )}
      style={
        fullPage
          ? undefined
          : { border: "1px solid var(--color-divider)", background: "var(--color-card)" }
      }
    >
      {/* header: title + view tabs */}
      <div
        className="flex flex-wrap items-center gap-2 px-3 py-2"
        style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
      >
        {db.icon ? <span className="text-[18px] leading-none">{db.icon}</span> : null}
        <DbTitle
          value={db.title}
          big={fullPage}
          onCommit={(t) => commitDb({ ...dbRef.current!, title: t || "데이터베이스" })}
        />
        <span className="flex-1" />
        <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
          {viewRows.length === rows.length ? `${rows.length}개` : `${viewRows.length}/${rows.length}개`}
        </span>
        {!fullPage && db.project_id ? (
          <button
            type="button"
            onClick={() =>
              onOpenDatabase
                ? onOpenDatabase(db.database_id)
                : router.push(`/dashboard/db/${db.database_id}`)
            }
            className="flex h-7 items-center rounded-[6px] px-2 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-text-muted)" }}
            title="전체 페이지로 열기"
          >
            ⤢ 페이지로 열기
          </button>
        ) : null}
        <DbMenu onDelete={onDeleteDatabase} />
      </div>

      <div
        className="flex flex-wrap items-center gap-1 px-3 pt-1.5"
        style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
      >
        {db.views.map((v) => (
          <ViewTab
            key={v.id}
            view={v}
            active={v.id === view.id}
            canDelete={db.views.length > 1}
            onSelect={() => setActiveViewId(v.id)}
            onRename={(name) => renameView(v.id, name)}
            onChangeType={(type) => switchViewType(v.id, type)}
            onDuplicate={() => duplicateView(v.id)}
            onDelete={() => deleteView(v.id)}
          />
        ))}
        <AddViewButton onAdd={addView} />
        <span className="flex-1" />
        {view.type === "board" ? (
          <>
            <GroupByPicker db={db} view={view} onPick={(pid) => setView(view.id, { group_by: pid })} />
            <BoardCardSettings
              db={db}
              view={view}
              onPatch={(patch) => setView(view.id, patch)}
            />
          </>
        ) : null}
      </div>

      <ViewToolbar
        db={db}
        view={view}
        members={members}
        search={search}
        onSearch={setSearch}
        onPatch={(patch) => setView(view.id, patch)}
      />

      {saveError ? (
        <div
          className="mx-3 mt-2 flex items-center justify-between gap-2 rounded-[8px] px-3 py-2 text-[var(--text-body-sm)]"
          style={{ background: "var(--color-fail-bg)", color: "var(--color-fail-fg)" }}
        >
          <span>{saveError}</span>
          <span className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={retryLastRowSave}
              className="rounded-[6px] px-2 py-1 hover:opacity-80"
              style={{ border: "1px solid var(--color-fail-fg)" }}
            >
              재시도
            </button>
            <button
              type="button"
              onClick={() => setSaveError(null)}
              className="rounded-[6px] px-2 py-1 hover:opacity-80"
            >
              닫기
            </button>
          </span>
        </div>
      ) : null}

      <div className={fullPage ? "py-3" : "p-3"}>
        {view.type === "board" ? (
          <BoardView ctx={ctx} view={view} />
        ) : (
          <TableView ctx={ctx} />
        )}
      </div>

      {peekRow ? (
        <RowPeek
          ctx={ctx}
          row={peekRow}
          bodyLoading={peekBodyLoadingId === peekRow.row_id}
          bodyError={peekBodyErrorId === peekRow.row_id}
          onRetryBody={() => {
            // loading을 함께 걸어 에러 해제~재시도 사이 프레임에도 빈 에디터가
            // 마운트되지 않게 한다(실제 fetch는 peek effect가 다시 수행).
            setPeekBodyLoadingId(peekRow.row_id);
            setPeekBodyErrorId(null);
          }}
          onClose={() => setPeekRowId(null)}
        />
      ) : null}
    </fieldset>
  );
}

/* ------------------------------ header bits ------------------------------ */
function DbTitle({
  value,
  onCommit,
  big = false,
}: {
  value: string;
  onCommit: (v: string) => void;
  big?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  const [focused, setFocused] = useState(false);
  const [seen, setSeen] = useState(value);
  const skip = useRef(false); // Escape 취소: blur 커밋을 한 번 건너뛴다.
  if (value !== seen) {
    setSeen(value);
    if (!focused) setDraft(value);
  }
  return (
    <input
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onFocus={() => setFocused(true)}
      onBlur={() => {
        setFocused(false);
        if (skip.current) {
          skip.current = false;
          setDraft(value);
          return;
        }
        if (draft.trim() !== value) onCommit(draft.trim());
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        else if (e.key === "Escape") {
          skip.current = true;
          (e.target as HTMLInputElement).blur();
        }
      }}
      className={cx(
        "rounded-[6px] bg-transparent px-1 py-0.5 font-bold outline-none focus:bg-[var(--color-app-canvas)]",
        big ? "text-[22px]" : "text-[var(--text-body)]",
      )}
      style={{ color: "var(--color-text-strong)" }}
    />
  );
}

function DbMenu({ onDelete }: { onDelete?: () => void }) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  if (!onDelete) return null;
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        className="flex h-7 w-7 items-center justify-center rounded-[6px] text-[var(--text-body)] hover:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-text-muted)" }}
        title="데이터베이스 메뉴"
      >
        ⋯
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 rounded-[10px] p-1"
          style={{
            ...anchoredStyle(rect, 180, 80),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          <button
            type="button"
            onClick={() => {
              close();
              if (window.confirm("이 데이터베이스를 삭제할까요? 모든 행이 함께 삭제됩니다."))
                onDelete();
            }}
            className="flex min-h-[34px] w-full items-center rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-fail-fg)" }}
          >
            데이터베이스 삭제
          </button>
        </div>
      ) : null}
    </>
  );
}

function AddViewButton({ onAdd }: { onAdd: (type: "table" | "board") => void }) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        className="rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-text-faint)" }}
        title="보기 추가"
      >
        +
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 rounded-[10px] p-1"
          style={{
            ...anchoredStyle(rect, 140, 90),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          {(["table", "board"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => {
                onAdd(t);
                close();
              }}
              className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-body)" }}
            >
              <span>{t === "board" ? "▦" : "▤"}</span>
              {t === "board" ? "보드" : "표"}
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

function ViewTab({
  view,
  active,
  canDelete,
  onSelect,
  onRename,
  onChangeType,
  onDuplicate,
  onDelete,
}: {
  view: DbViewDef;
  active: boolean;
  canDelete: boolean;
  onSelect: () => void;
  onRename: (name: string) => void;
  onChangeType: (type: "table" | "board") => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const [name, setName] = useState(view.name);
  const [seenName, setSeenName] = useState(view.name);
  if (view.name !== seenName) {
    setSeenName(view.name);
    setName(view.name);
  }
  const commitName = () => {
    const clean = name.trim();
    if (clean && clean !== view.name) onRename(clean);
  };
  return (
    <>
      <div
        className="group flex items-center rounded-t-[6px] text-[var(--text-body-sm)]"
        style={{
          color: active ? "var(--color-text-strong)" : "var(--color-text-muted)",
          fontWeight: active ? 600 : 500,
          borderBottom: active ? "2px solid var(--color-text-strong)" : "2px solid transparent",
        }}
      >
        <button
          type="button"
          onClick={onSelect}
          className="flex min-h-[34px] min-w-0 items-center gap-1 px-2 py-1.5 hover:bg-[var(--color-app-canvas)]"
        >
          <span className="shrink-0">{view.type === "board" ? "▦" : "▤"}</span>
          <span className="max-w-[140px] truncate">{view.name}</span>
        </button>
        <button
          ref={anchorRef}
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            toggle();
          }}
          className="mr-1 flex h-7 w-6 items-center justify-center rounded-[5px] opacity-70 hover:bg-[var(--color-app-canvas)] sm:opacity-0 sm:group-hover:opacity-100"
          style={{ color: "var(--color-text-faint)" }}
          title="보기 메뉴"
        >
          ⋯
        </button>
      </div>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 grid gap-1 rounded-[10px] p-2"
          style={{
            ...anchoredStyle(rect, 220, canDelete ? 210 : 168),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          <input
            value={name}
            autoFocus
            onChange={(event) => setName(event.target.value)}
            onBlur={commitName}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                commitName();
                close();
              } else if (event.key === "Escape") {
                setName(view.name);
                close();
              }
            }}
            className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)] outline-none"
            style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
          />
          <p className="px-1 text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
            타입: {view.type === "board" ? "보드" : "표"}
          </p>
          <button
            type="button"
            onClick={() => {
              close();
              onChangeType(view.type === "board" ? "table" : "board");
            }}
            className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-text-body)" }}
            data-db-view-switch=""
          >
            <span className="w-[13px] text-center">{view.type === "board" ? "▤" : "▦"}</span>
            {view.type === "board" ? "표로 전환" : "보드로 전환"}
          </button>
          <button
            type="button"
            onClick={() => {
              close();
              onDuplicate();
            }}
            className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-text-body)" }}
          >
            <Copy size={13} strokeWidth={2} aria-hidden="true" />
            보기 복제
          </button>
          {canDelete ? (
            <button
              type="button"
              onClick={() => {
                close();
                if (window.confirm("이 보기를 삭제할까요? 데이터 행은 삭제되지 않습니다.")) {
                  onDelete();
                }
              }}
              className="flex min-h-[32px] w-full items-center rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-fail-fg)" }}
            >
              보기 삭제
            </button>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

function GroupByPicker({
  db,
  view,
  onPick,
}: {
  db: ProjectDatabase;
  view: DbViewDef;
  onPick: (propId: string) => void;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const selectProps = db.properties.filter((p) => isSelectLike(p.type));
  const current = db.properties.find((p) => p.id === view.group_by);
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        className="rounded-[6px] px-2 py-1.5 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        그룹화: {current?.name ?? "—"} ▾
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 rounded-[10px] p-1"
          style={{
            ...anchoredStyle(rect, 180, 200),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          {selectProps.length === 0 ? (
            <p
              className="px-2 py-1 text-[var(--text-meta)]"
              style={{ color: "var(--color-text-faint)" }}
            >
              선택/상태 속성이 없습니다.
            </p>
          ) : (
            selectProps.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => {
                  onPick(p.id);
                  close();
                }}
                className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-body)" }}
              >
                <span>{p.id === view.group_by ? "✓" : ""}</span>
                {p.name}
              </button>
            ))
          )}
        </div>
      ) : null}
    </>
  );
}

function BoardCardSettings({
  db,
  view,
  onPatch,
}: {
  db: ProjectDatabase;
  view: DbViewDef;
  onPatch: (patch: Partial<DbViewDef>) => void;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const fileProps = db.properties.filter((p) => p.type === "files");
  const current = fileProps.find((p) => p.id === view.cover_property) ?? null;
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        className="rounded-[6px] px-2 py-1.5 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        카드: {current?.name ?? "커버 없음"} ▾
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 rounded-[10px] p-1"
          style={{
            ...anchoredStyle(rect, 210, 220),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          <button
            type="button"
            onClick={() => {
              onPatch({ cover_property: null });
              close();
            }}
            className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-text-body)" }}
          >
            <span className="w-4 text-center" style={{ color: "var(--color-text-faint)" }}>
              {current ? "" : "✓"}
            </span>
            커버 없음
          </button>
          {fileProps.map((prop) => (
            <button
              key={prop.id}
              type="button"
              onClick={() => {
                onPatch({ cover_property: prop.id });
                close();
              }}
              className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-body)" }}
            >
              <span className="w-4 text-center" style={{ color: "var(--color-text-faint)" }}>
                {current?.id === prop.id ? "✓" : ""}
              </span>
              <span>{PROP_TYPE_ICON[prop.type]}</span>
              <span className="min-w-0 flex-1 truncate">{prop.name}</span>
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

const FILTER_OP_LABEL: Record<DbViewFilterDef["op"], string> = {
  contains: "포함",
  is: "같음",
  is_not: "아님",
  is_empty: "비어 있음",
  is_not_empty: "비어 있지 않음",
  checked: "체크됨",
  unchecked: "체크 안 됨",
};

function filterOpsFor(prop: DbPropertyDef): DbViewFilterDef["op"][] {
  if (prop.type === "checkbox") return ["checked", "unchecked", "is_empty", "is_not_empty"];
  if (isOptionType(prop.type) || prop.type === "person")
    return ["is", "is_not", "is_empty", "is_not_empty"];
  if (prop.type === "files") return ["contains", "is_empty", "is_not_empty"];
  return ["contains", "is", "is_not", "is_empty", "is_not_empty"];
}

function defaultFilterValue(prop: DbPropertyDef, members: MemberOption[]): string {
  if (isOptionType(prop.type)) return prop.options[0]?.id ?? "";
  if (prop.type === "person") return members[0]?.id ?? "";
  return "";
}

function filterChipLabel(db: ProjectDatabase, members: MemberOption[], filter: DbViewFilterDef): string {
  const prop = db.properties.find((p) => p.id === filter.prop_id);
  if (!prop) return "필터";
  let value = String(filter.value ?? "");
  if (isOptionType(prop.type)) value = findOption(prop, value)?.name ?? value;
  if (prop.type === "person") value = members.find((m) => m.id === value)?.name ?? value;
  const noValue = ["is_empty", "is_not_empty", "checked", "unchecked"].includes(filter.op);
  return `${prop.name} ${FILTER_OP_LABEL[filter.op]}${noValue ? "" : ` ${value}`}`;
}

function sortChipLabel(db: ProjectDatabase, sort: DbViewSortDef): string {
  const prop = db.properties.find((p) => p.id === sort.prop_id);
  return `${prop?.name ?? "속성"} ${sort.direction === "asc" ? "오름차순" : "내림차순"}`;
}

function ViewToolbar({
  db,
  view,
  members,
  search,
  onSearch,
  onPatch,
}: {
  db: ProjectDatabase;
  view: DbViewDef;
  members: MemberOption[];
  search: string;
  onSearch: (value: string) => void;
  onPatch: (patch: Partial<DbViewDef>) => void;
}) {
  const filters = viewFilters(view);
  const sorts = viewSorts(view);
  return (
    <div
      className="flex flex-wrap items-center gap-1.5 px-3 py-2"
      style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
    >
      <input
        value={search}
        onChange={(event) => onSearch(event.target.value)}
        placeholder="검색"
        className="h-8 min-w-[150px] rounded-[6px] px-2 text-[var(--text-body-sm)] outline-none"
        style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
      />
      <FilterButton db={db} view={view} members={members} onPatch={onPatch} />
      <SortButton db={db} view={view} onPatch={onPatch} />
      <PropertiesButton db={db} view={view} onPatch={onPatch} />
      {filters.map((filter) => (
        <button
          key={filter.id}
          type="button"
          onClick={() => onPatch({ filters: filters.filter((f) => f.id !== filter.id) })}
          className="h-7 rounded-[6px] px-2 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
          style={{ border: "1px solid var(--color-divider-soft)", color: "var(--color-text-muted)" }}
          title="필터 제거"
        >
          {filterChipLabel(db, members, filter)} ×
        </button>
      ))}
      {sorts.map((sort) => (
        <button
          key={sort.id}
          type="button"
          onClick={() => onPatch({ sorts: sorts.filter((s) => s.id !== sort.id) })}
          className="h-7 rounded-[6px] px-2 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
          style={{ border: "1px solid var(--color-divider-soft)", color: "var(--color-text-muted)" }}
          title="정렬 제거"
        >
          {sortChipLabel(db, sort)} ×
        </button>
      ))}
    </div>
  );
}

function FilterButton({
  db,
  view,
  members,
  onPatch,
}: {
  db: ProjectDatabase;
  view: DbViewDef;
  members: MemberOption[];
  onPatch: (patch: Partial<DbViewDef>) => void;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const firstProp = db.properties[0];
  const [propId, setPropId] = useState(firstProp?.id ?? "");
  const prop = db.properties.find((p) => p.id === propId) ?? firstProp;
  const [op, setOp] = useState<DbViewFilterDef["op"]>(prop ? filterOpsFor(prop)[0] : "contains");
  const [value, setValue] = useState(prop ? defaultFilterValue(prop, members) : "");
  if (!prop) return null;
  const ops = filterOpsFor(prop);
  const needsValue = !["is_empty", "is_not_empty", "checked", "unchecked"].includes(op);
  const add = () => {
    onPatch({
      filters: [
        ...viewFilters(view),
        { id: shortId(), prop_id: prop.id, op, value: needsValue ? value : null },
      ],
    });
    close();
  };
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        필터
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 grid gap-2 rounded-[10px] p-2"
          style={{
            ...anchoredStyle(rect, 260, 230),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          <select
            value={prop.id}
            onChange={(event) => {
              const next = db.properties.find((p) => p.id === event.target.value) ?? firstProp;
              setPropId(next.id);
              const nextOp = filterOpsFor(next)[0];
              setOp(nextOp);
              setValue(defaultFilterValue(next, members));
            }}
            className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)]"
            style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
          >
            {db.properties.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <select
            value={op}
            onChange={(event) => setOp(event.target.value as DbViewFilterDef["op"])}
            className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)]"
            style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
          >
            {ops.map((item) => (
              <option key={item} value={item}>
                {FILTER_OP_LABEL[item]}
              </option>
            ))}
          </select>
          {needsValue ? (
            <FilterValueInput prop={prop} members={members} value={value} onChange={setValue} />
          ) : null}
          <button
            type="button"
            onClick={add}
            className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)] font-semibold hover:bg-[var(--color-app-canvas)]"
            style={{ border: "1px solid var(--color-divider)", color: "var(--color-text-muted)" }}
          >
            필터 추가
          </button>
        </div>
      ) : null}
    </>
  );
}

function FilterValueInput({
  prop,
  members,
  value,
  onChange,
}: {
  prop: DbPropertyDef;
  members: MemberOption[];
  value: string;
  onChange: (value: string) => void;
}) {
  if (isOptionType(prop.type)) {
    return (
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)]"
        style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
      >
        {prop.options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.name}
          </option>
        ))}
      </select>
    );
  }
  if (prop.type === "person") {
    return (
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)]"
        style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
      >
        {members.map((member) => (
          <option key={member.id} value={member.id}>
            {member.name}
          </option>
        ))}
      </select>
    );
  }
  return (
    <input
      value={value}
      type={prop.type === "date" ? "date" : prop.type === "number" ? "number" : "text"}
      onChange={(event) => onChange(event.target.value)}
      className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)] outline-none"
      style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
    />
  );
}

function SortButton({
  db,
  view,
  onPatch,
}: {
  db: ProjectDatabase;
  view: DbViewDef;
  onPatch: (patch: Partial<DbViewDef>) => void;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const [propId, setPropId] = useState(db.properties[0]?.id ?? "");
  const [direction, setDirection] = useState<"asc" | "desc">("asc");
  const add = () => {
    if (!propId) return;
    onPatch({ sorts: [...viewSorts(view), { id: shortId(), prop_id: propId, direction }] });
    close();
  };
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        정렬
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 grid gap-2 rounded-[10px] p-2"
          style={{
            ...anchoredStyle(rect, 230, 160),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          <select
            value={propId}
            onChange={(event) => setPropId(event.target.value)}
            className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)]"
            style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
          >
            {db.properties.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <select
            value={direction}
            onChange={(event) => setDirection(event.target.value as "asc" | "desc")}
            className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)]"
            style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
          >
            <option value="asc">오름차순</option>
            <option value="desc">내림차순</option>
          </select>
          <button
            type="button"
            onClick={add}
            className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)] font-semibold hover:bg-[var(--color-app-canvas)]"
            style={{ border: "1px solid var(--color-divider)", color: "var(--color-text-muted)" }}
          >
            정렬 추가
          </button>
        </div>
      ) : null}
    </>
  );
}

function PropertiesButton({
  db,
  view,
  onPatch,
}: {
  db: ProjectDatabase;
  view: DbViewDef;
  onPatch: (patch: Partial<DbViewDef>) => void;
}) {
  const { anchorRef, popRef, rect, open, toggle } = useAnchoredPopover();
  const hidden = new Set(view.hidden ?? []);
  const toggleProp = (prop: DbPropertyDef) => {
    if (prop.type === "title") return;
    const next = new Set(hidden);
    if (next.has(prop.id)) next.delete(prop.id);
    else next.add(prop.id);
    onPatch({ hidden: [...next] });
  };
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        className="h-8 rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        속성
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 grid max-h-[300px] gap-1 overflow-y-auto rounded-[10px] p-1.5"
          style={{
            ...anchoredStyle(rect, 230, 300),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          {db.properties.map((prop) => (
            <button
              key={prop.id}
              type="button"
              onClick={() => toggleProp(prop)}
              className="flex min-h-[32px] items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: prop.type === "title" ? "var(--color-text-faint)" : "var(--color-text-body)" }}
            >
              <span className="w-4 text-center" style={{ color: "var(--color-text-faint)" }}>
                {hidden.has(prop.id) ? "" : "✓"}
              </span>
              <span>{PROP_TYPE_ICON[prop.type]}</span>
              <span className="min-w-0 flex-1 truncate">{prop.name}</span>
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

function AddPropertyButton({ ctx }: { ctx: DbCtx }) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        className="flex h-8 items-center px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-text-faint)" }}
        title="속성 추가"
      >
        +
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 overflow-y-auto rounded-[10px] p-1"
          style={{
            ...anchoredStyle(rect, 180, 340),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          {ADDABLE_TYPES.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => {
                ctx.addProperty(t);
                close();
              }}
              className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-body)" }}
            >
              <span className="w-4 text-center" style={{ color: "var(--color-text-faint)" }}>
                {PROP_TYPE_ICON[t]}
              </span>
              {PROP_TYPE_LABEL[t]}
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

function PropHeaderMenu({ ctx, prop }: { ctx: DbCtx; prop: DbPropertyDef }) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const [name, setName] = useState(prop.name);
  const [seenName, setSeenName] = useState(prop.name);
  const skip = useRef(false); // Escape 취소: blur 커밋을 한 번 건너뛴다.
  const propIndex = ctx.db.properties.findIndex((p) => p.id === prop.id);
  const canMoveLeft = prop.type !== "title" && propIndex > 1;
  const canMoveRight = prop.type !== "title" && propIndex >= 1 && propIndex < ctx.db.properties.length - 1;
  const currentHidden = new Set(ctx.view.hidden ?? []);
  const activeSort = viewSorts(ctx.view).find((sort) => sort.prop_id === prop.id) ?? null;
  const quickSort = (direction: "asc" | "desc") => {
    const rest = viewSorts(ctx.view).filter((sort) => sort.prop_id !== prop.id);
    ctx.patchView({
      sorts: [{ id: activeSort?.id ?? shortId(), prop_id: prop.id, direction }, ...rest],
    });
    close();
  };
  const hideInView = () => {
    if (prop.type === "title") return;
    const nextHidden = new Set(currentHidden);
    nextHidden.add(prop.id);
    ctx.patchView({ hidden: [...nextHidden] });
    close();
  };
  if (prop.name !== seenName) {
    setSeenName(prop.name);
    setName(prop.name);
  }
  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={toggle}
        className="flex max-w-full min-w-0 items-center gap-1 text-left"
        style={{ color: "var(--color-text-muted)" }}
        title={prop.name}
      >
        <span
          className="shrink-0 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-faint)" }}
        >
          {PROP_TYPE_ICON[prop.type]}
        </span>
        <span className="truncate text-[var(--text-meta)] font-semibold">{prop.name}</span>
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 rounded-[10px] p-2"
          style={{
            ...anchoredStyle(rect, 228, prop.type === "title" ? 236 : 384),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          <input
            value={name}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onBlur={() => {
              if (skip.current) {
                skip.current = false;
                setName(prop.name);
                return;
              }
              if (name.trim() && name !== prop.name)
                ctx.updateProperty(prop.id, { name: name.trim() });
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                if (name.trim()) ctx.updateProperty(prop.id, { name: name.trim() });
                close();
              } else if (e.key === "Escape") {
                skip.current = true;
                setName(prop.name);
                close();
              }
            }}
            className="mb-1 w-full rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] outline-none"
            style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
          />
          <p
            className="px-1 py-0.5 text-[var(--text-meta)]"
            style={{ color: "var(--color-text-faint)" }}
          >
            타입: {PROP_TYPE_LABEL[prop.type]}
          </p>
          <div
            className="my-1 grid gap-1"
            style={{ borderTop: "1px solid var(--color-divider-soft)", paddingTop: 6 }}
          >
            <button
              type="button"
              onClick={() => quickSort("asc")}
              className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
              data-db-prop-sort="asc"
            >
              <ArrowUp size={13} strokeWidth={2} aria-hidden="true" />
              오름차순 정렬
              <span className="flex-1" />
              {activeSort?.direction === "asc" ? "✓" : ""}
            </button>
            <button
              type="button"
              onClick={() => quickSort("desc")}
              className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
              data-db-prop-sort="desc"
            >
              <ArrowDown size={13} strokeWidth={2} aria-hidden="true" />
              내림차순 정렬
              <span className="flex-1" />
              {activeSort?.direction === "desc" ? "✓" : ""}
            </button>
          </div>
          {prop.type !== "title" ? (
            <>
              <button
                type="button"
                onClick={hideInView}
                className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-muted)" }}
                data-db-prop-hide=""
              >
                <EyeOff size={13} strokeWidth={2} aria-hidden="true" />
                이 보기에서 숨기기
              </button>
              <button
                type="button"
                onClick={() => {
                  close();
                  ctx.duplicateProperty(prop.id);
                }}
                className="flex min-h-[32px] w-full items-center gap-2 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-muted)" }}
                data-db-prop-duplicate=""
              >
                <Copy size={13} strokeWidth={2} aria-hidden="true" />
                속성 복제
              </button>
              <div
                className="my-1 grid grid-cols-2 gap-1"
                style={{ borderTop: "1px solid var(--color-divider-soft)", paddingTop: 6 }}
              >
                <button
                  type="button"
                  disabled={!canMoveLeft}
                  onClick={() => {
                    ctx.moveProperty(prop.id, -1);
                    close();
                  }}
                  className="flex min-h-[32px] items-center justify-center gap-1.5 rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)] disabled:cursor-not-allowed disabled:opacity-40"
                  style={{ color: "var(--color-text-muted)" }}
                  title="왼쪽으로 이동"
                  data-db-prop-move="left"
                >
                  <ArrowLeft size={13} strokeWidth={2} aria-hidden="true" />
                  왼쪽
                </button>
                <button
                  type="button"
                  disabled={!canMoveRight}
                  onClick={() => {
                    ctx.moveProperty(prop.id, 1);
                    close();
                  }}
                  className="flex min-h-[32px] items-center justify-center gap-1.5 rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)] disabled:cursor-not-allowed disabled:opacity-40"
                  style={{ color: "var(--color-text-muted)" }}
                  title="오른쪽으로 이동"
                  data-db-prop-move="right"
                >
                  오른쪽
                  <ArrowRight size={13} strokeWidth={2} aria-hidden="true" />
                </button>
              </div>
              <button
                type="button"
                onClick={() => {
                  close();
                  ctx.deleteProperty(prop.id);
                }}
                className="mt-1 flex min-h-[32px] w-full items-center rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-fail-fg)" }}
              >
                속성 삭제
              </button>
            </>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

/* ------------------------------ table view ------------------------------ */
function TableView({ ctx }: { ctx: DbCtx }) {
  const props = ctx.visibleProps;
  const visibleRowIds = ctx.rows.map((row) => row.row_id);
  const manualOrderLocked = viewSorts(ctx.view).length > 0;
  const [draftWidths, setDraftWidths] = useState<Record<string, number>>({});
  const widthFor = (prop: DbPropertyDef) =>
    draftWidths[prop.id] ??
    ctx.view.column_widths?.[prop.id] ??
    defaultColumnWidth(prop);
  const resizeColumn = (event: ReactMouseEvent<HTMLButtonElement>, prop: DbPropertyDef) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const startWidth = widthFor(prop);
    const onMove = (moveEvent: MouseEvent) => {
      setDraftWidths((cur) => ({
        ...cur,
        [prop.id]: clampColumnWidth(startWidth + moveEvent.clientX - startX),
      }));
    };
    const onUp = (upEvent: MouseEvent) => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      const width = clampColumnWidth(startWidth + upEvent.clientX - startX);
      setDraftWidths((cur) => {
        const next = { ...cur };
        delete next[prop.id];
        return next;
      });
      ctx.patchView({ column_widths: { ...(ctx.view.column_widths ?? {}), [prop.id]: width } });
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };
  const resetColumnWidth = (prop: DbPropertyDef) => {
    const next = { ...(ctx.view.column_widths ?? {}) };
    delete next[prop.id];
    ctx.patchView({ column_widths: next });
  };
  return (
    <div className="overflow-x-auto">
      {/* width:max-content + min-width:100% → 컬럼이 min-width를 지키고 넘치면 가로 스크롤
          (w-full이면 컬럼이 짓눌려 헤더가 겹친다). */}
      <table
        className="border-collapse text-[var(--text-body-sm)]"
        style={{ width: "max-content", minWidth: "100%" }}
      >
        <thead>
          <tr style={{ borderBottom: "1px solid var(--color-divider)" }}>
            {props.map((p) => {
              const width = widthFor(p);
              return (
                <th
                  key={p.id}
                  className="group/header relative overflow-hidden px-2 py-1.5 text-left font-medium whitespace-nowrap"
                  style={{ width, minWidth: width, maxWidth: width }}
                >
                  <div className="pr-2">
                    <PropHeaderMenu ctx={ctx} prop={p} />
                  </div>
                  <button
                    type="button"
                    onMouseDown={(event) => resizeColumn(event, p)}
                    onDoubleClick={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      resetColumnWidth(p);
                    }}
                    className="absolute top-0 right-0 h-full w-2 cursor-col-resize opacity-0 group-hover/header:opacity-100 focus:opacity-100"
                    style={{ borderRight: "2px solid var(--color-divider)" }}
                    title="컬럼 너비 조절"
                    aria-label={`${p.name} 컬럼 너비 조절`}
                    data-db-column-resize={p.id}
                  />
                </th>
              );
            })}
            <th className="px-1 py-1.5" style={{ width: 40 }}>
              <AddPropertyButton ctx={ctx} />
            </th>
          </tr>
        </thead>
        <tbody>
          {ctx.rows.map((row) => (
            <tr
              key={row.row_id}
              className="group hover:bg-[var(--color-app-canvas)]"
              style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
            >
              {props.map((p, i) => {
                const width = widthFor(p);
                return (
                  <td
                    key={p.id}
                    className="px-1 py-0.5 align-middle"
                    style={{ width, minWidth: width, maxWidth: width }}
                  >
                    <div className="flex items-center gap-1">
                      <div className="min-w-0 flex-1">
                        <PropValue
                          prop={p}
                          value={row.props?.[p.id]}
                          onChange={(v) => ctx.setRowProp(row.row_id, p.id, v)}
                          handlers={ctx.handlersFor(p.id)}
                          members={ctx.members}
                          meta={rowMeta(row)}
                          databaseId={ctx.db.database_id}
                          rowId={row.row_id}
                          fill
                        />
                      </div>
                      {i === 0 ? (
                        <div className="flex shrink-0 items-center gap-0.5 opacity-0 group-hover:opacity-100">
                        <button
                          type="button"
                          onClick={() => ctx.moveRowOrder(row.row_id, visibleRowIds, -1)}
                          disabled={manualOrderLocked || visibleRowIds[0] === row.row_id}
                          className="flex h-6 w-6 items-center justify-center rounded-[4px] hover:bg-[var(--color-divider-soft)] disabled:cursor-not-allowed disabled:opacity-35"
                          style={{ color: "var(--color-text-muted)" }}
                          title={manualOrderLocked ? "정렬 적용 중에는 수동 순서 변경 불가" : "위로 이동"}
                          aria-label="행 위로 이동"
                          data-db-row-move="up"
                        >
                          <ArrowUp size={13} strokeWidth={2} aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          onClick={() => ctx.moveRowOrder(row.row_id, visibleRowIds, 1)}
                          disabled={
                            manualOrderLocked || visibleRowIds[visibleRowIds.length - 1] === row.row_id
                          }
                          className="flex h-6 w-6 items-center justify-center rounded-[4px] hover:bg-[var(--color-divider-soft)] disabled:cursor-not-allowed disabled:opacity-35"
                          style={{ color: "var(--color-text-muted)" }}
                          title={manualOrderLocked ? "정렬 적용 중에는 수동 순서 변경 불가" : "아래로 이동"}
                          aria-label="행 아래로 이동"
                          data-db-row-move="down"
                        >
                          <ArrowDown size={13} strokeWidth={2} aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            void ctx.duplicateRow(row.row_id, visibleRowIds);
                          }}
                          className="flex h-6 w-6 items-center justify-center rounded-[4px] hover:bg-[var(--color-divider-soft)]"
                          style={{ color: "var(--color-text-muted)" }}
                          title="행 복제"
                          aria-label="행 복제"
                          data-db-row-duplicate=""
                        >
                          <Copy size={13} strokeWidth={2} aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          onClick={() => ctx.openRow(row.row_id)}
                          className="flex h-6 w-6 items-center justify-center rounded-[4px] hover:bg-[var(--color-divider-soft)]"
                          style={{ color: "var(--color-text-muted)" }}
                          title="열기"
                          aria-label="행 열기"
                        >
                          <ExternalLink size={13} strokeWidth={2} aria-hidden="true" />
                        </button>
                        </div>
                      ) : null}
                    </div>
                  </td>
                );
              })}
              <td className="px-1 py-0.5 text-right">
                <button
                  type="button"
                  onClick={() => ctx.deleteRow(row.row_id)}
                  className="rounded-[4px] px-1 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                  style={{ color: "var(--color-fail-fg)" }}
                  title="행 삭제"
                >
                  <Trash2 size={13} strokeWidth={2} aria-hidden="true" />
                </button>
              </td>
            </tr>
          ))}
          <tr>
            <td colSpan={props.length + 1} className="px-1 py-1">
              <button
                type="button"
                onClick={() => ctx.addRow()}
                className="flex min-h-[32px] w-full items-center gap-1 rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-faint)" }}
              >
                + 새 행
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------ board view ------------------------------ */
function BoardView({ ctx, view }: { ctx: DbCtx; view: DbViewDef }) {
  const groupProp = ctx.db.properties.find((p) => p.id === view.group_by) ?? null;
  const dragRow = useRef<string | null>(null);
  const [overCol, setOverCol] = useState<string | null>(null);
  const [overCard, setOverCard] = useState<{
    rowId: string;
    placement: BoardDropPlacement;
  } | null>(null);

  if (!groupProp) {
    return (
      <p
        className="px-1 py-4 text-[var(--text-body-sm)]"
        style={{ color: "var(--color-text-faint)" }}
      >
        보드로 보려면 그룹화할 선택/상태 속성이 필요합니다. 우측 상단 “그룹화”에서 지정하세요.
      </p>
    );
  }

  const grouped = groupRowsByOption(ctx.rows, groupProp);
  const manualOrderLocked = viewSorts(view).length > 0;
  // status는 컬럼 그룹 순서(to_do→in_progress→complete)대로, select는 옵션 순서대로.
  const columns: BoardColumn[] = [
    ...orderedOptions(groupProp).map((o) => ({
      key: o.id,
      label: o.name,
      color: o.color as TagColor,
    })),
    { key: "__none__", label: "미지정", color: null },
  ];

  function drop(colKey: string) {
    const rowId = dragRow.current;
    dragRow.current = null;
    setOverCol(null);
    setOverCard(null);
    if (!rowId || !groupProp) return;
    const optionId = colKey === "__none__" ? null : colKey;
    const colRows = grouped.get(colKey) ?? [];
    const sort = (colRows[colRows.length - 1]?.sort_order ?? 0) + 1;
    ctx.moveRow(rowId, groupProp.id, optionId, sort);
  }

  function dragOverCard(
    event: DragEvent<HTMLDivElement>,
    colKey: string,
    targetRow: ProjectDatabaseRow,
  ) {
    const rowId = dragRow.current;
    if (!rowId || rowId === targetRow.row_id) return;
    event.preventDefault();
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const placement: BoardDropPlacement =
      event.clientY < rect.top + rect.height / 2 ? "before" : "after";
    setOverCol(colKey);
    setOverCard({ rowId: targetRow.row_id, placement });
  }

  function dropOnCard(
    event: DragEvent<HTMLDivElement>,
    colKey: string,
    targetRow: ProjectDatabaseRow,
    placement: BoardDropPlacement,
  ) {
    event.preventDefault();
    event.stopPropagation();
    const rowId = dragRow.current;
    dragRow.current = null;
    setOverCol(null);
    setOverCard(null);
    if (!rowId || !groupProp || rowId === targetRow.row_id) return;

    const optionId = colKey === "__none__" ? null : colKey;
    const colRows = (grouped.get(colKey) ?? []).filter((row) => row.row_id !== rowId);
    const targetIndex = colRows.findIndex((row) => row.row_id === targetRow.row_id);
    if (targetIndex < 0) return;
    const prev = placement === "before" ? colRows[targetIndex - 1] ?? null : targetRow;
    const next = placement === "before" ? targetRow : colRows[targetIndex + 1] ?? null;
    ctx.moveRow(rowId, groupProp.id, optionId, sortBetween(prev, next));
  }

  return (
    <div className="flex items-start gap-3 overflow-x-auto pb-1">
      {columns.map((col) => {
        const colRows = grouped.get(col.key) ?? [];
        const colRowIds = colRows.map((row) => row.row_id);
        return (
          <div
            key={col.key}
            className={cx("flex w-[260px] shrink-0 flex-col rounded-[10px]")}
            style={{
              background: overCol === col.key ? "var(--color-sidebar)" : "var(--color-panel)",
              border: "1px solid var(--color-divider-soft)",
              maxHeight: "62vh",
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setOverCol(col.key);
            }}
            onDragLeave={() => setOverCol((c) => (c === col.key ? null : c))}
            onDrop={(e) => {
              e.preventDefault();
              drop(col.key);
            }}
          >
            <BoardColumnHeader ctx={ctx} groupProp={groupProp} column={col} count={colRows.length} />
            <div className="flex min-h-[24px] flex-col gap-1.5 overflow-y-auto px-2 pb-2">
              {colRows.map((row) => (
                <BoardCard
                  key={row.row_id}
                  ctx={ctx}
                  row={row}
                  groupPropId={groupProp.id}
                  columnRowIds={colRowIds}
                  manualOrderLocked={manualOrderLocked}
                  dropPlacement={
                    overCard?.rowId === row.row_id ? overCard.placement : null
                  }
                  onDragOverCard={(event) => dragOverCard(event, col.key, row)}
                  onDragLeaveCard={() => setOverCard(null)}
                  onDropOnCard={(event, placement) => dropOnCard(event, col.key, row, placement)}
                  onDragStart={() => (dragRow.current = row.row_id)}
                />
              ))}
            </div>
            <button
              type="button"
              onClick={() => ctx.addRow(col.key === "__none__" ? {} : { [groupProp.id]: col.key })}
              className="m-2 mt-0 flex min-h-[30px] items-center rounded-[6px] px-2 text-left text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-faint)" }}
            >
              + 새 카드
            </button>
          </div>
        );
      })}
      <AddColumn ctx={ctx} groupProp={groupProp} />
    </div>
  );
}

function BoardColumnHeader({
  ctx,
  groupProp,
  column,
  count,
}: {
  ctx: DbCtx;
  groupProp: DbPropertyDef;
  column: BoardColumn;
  count: number;
}) {
  const option = groupProp.options.find((o) => o.id === column.key) ?? null;
  const handlers = ctx.handlersFor(groupProp.id);
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const [name, setName] = useState(option?.name ?? column.label);
  const [seenName, setSeenName] = useState(option?.name ?? column.label);
  const skipBlurCommit = useRef(false);

  const currentName = option?.name ?? column.label;
  const currentColor = (option?.color ?? column.color ?? "gray") as TagColor;
  const ordered = orderedOptions(groupProp);
  const optionIndex = option ? ordered.findIndex((o) => o.id === option.id) : -1;
  const leftTarget = optionIndex > 0 ? ordered[optionIndex - 1] : null;
  const rightTarget = optionIndex >= 0 && optionIndex < ordered.length - 1 ? ordered[optionIndex + 1] : null;
  const canMoveLeft =
    !!option &&
    !!leftTarget &&
    (groupProp.type !== "status" || (leftTarget.group ?? "to_do") === (option.group ?? "to_do"));
  const canMoveRight =
    !!option &&
    !!rightTarget &&
    (groupProp.type !== "status" || (rightTarget.group ?? "to_do") === (option.group ?? "to_do"));
  if (currentName !== seenName) {
    setSeenName(currentName);
    setName(currentName);
  }

  function commitName() {
    const clean = name.trim();
    if (option && clean && clean !== option.name) {
      handlers.renameOption(option.id, clean);
    } else {
      setName(currentName);
    }
  }

  function cancelNameEdit() {
    skipBlurCommit.current = true;
    setName(currentName);
    close();
    window.setTimeout(() => {
      skipBlurCommit.current = false;
    }, 0);
  }

  return (
    <div className="group/column-header flex min-h-[38px] items-center gap-1.5 px-2.5 pb-1 pt-2.5">
      {column.color ? (
        <TagPill label={currentName} color={currentColor} />
      ) : (
        <span
          className="min-w-0 truncate text-[var(--text-meta)] font-medium"
          style={{ color: "var(--color-text-faint)" }}
        >
          {currentName}
        </span>
      )}
      <span
        className="text-[var(--text-meta)] tabular-nums"
        style={{ color: "var(--color-text-faint)" }}
      >
        {count}
      </span>
      <span className="flex-1" />
      {option ? (
        <>
          <button
            ref={anchorRef}
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setName(option.name);
              toggle();
            }}
            className="flex h-7 w-7 items-center justify-center rounded-[6px] text-[var(--text-meta)] opacity-100 hover:bg-[var(--color-app-canvas)] md:opacity-0 md:group-hover/column-header:opacity-100"
            style={{ color: "var(--color-text-muted)" }}
            title="컬럼 메뉴"
            data-board-column-menu={option.id}
          >
            ⋯
          </button>
          {open && rect ? (
            <div
              ref={popRef}
              className="z-50 rounded-[10px] p-2"
              style={{
                ...anchoredStyle(rect, 236, 304),
                background: "var(--color-modal)",
                border: "1px solid var(--color-divider)",
                boxShadow: "var(--shadow-modal)",
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <input
                value={name}
                autoFocus
                onChange={(e) => setName(e.target.value)}
                onBlur={() => {
                  if (skipBlurCommit.current) {
                    skipBlurCommit.current = false;
                    setName(currentName);
                    return;
                  }
                  commitName();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    commitName();
                    close();
                  } else if (e.key === "Escape") {
                    cancelNameEdit();
                  }
                }}
                className="mb-2 w-full rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] outline-none"
                style={{
                  background: "var(--color-app-canvas)",
                  color: "var(--color-text-strong)",
                }}
                aria-label="컬럼 이름"
                data-board-column-rename={option.id}
              />
              <div className="px-0.5">
                <p
                  className="mb-1 text-[var(--text-meta)]"
                  style={{ color: "var(--color-text-faint)" }}
                >
                  색상
                </p>
                <div className="grid grid-cols-5 gap-1">
                  {OPTION_PALETTE.map((color) => (
                    <button
                      key={color}
                      type="button"
                      onClick={() => handlers.recolorOption(option.id, color)}
                      className="flex h-7 items-center justify-center rounded-[5px]"
                      style={{
                        background: TAG_COLORS[color].bg,
                        outline:
                          currentColor === color ? `2px solid ${TAG_COLORS[color].fg}` : "none",
                      }}
                      title={`색상 ${color}`}
                      data-board-column-color={color}
                    >
                      {currentColor === color ? (
                        <span style={{ color: TAG_COLORS[color].fg }}>✓</span>
                      ) : null}
                    </button>
                  ))}
                </div>
              </div>
              <div
                className="mt-2 grid grid-cols-2 gap-1"
                style={{ borderTop: "1px solid var(--color-divider-soft)", paddingTop: 8 }}
              >
                <button
                  type="button"
                  disabled={!canMoveLeft || !leftTarget}
                  onClick={() => {
                    if (!option || !leftTarget) return;
                    handlers.moveOption(option.id, leftTarget.id, "before");
                    close();
                  }}
                  className="flex min-h-[32px] items-center justify-center gap-1.5 rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)] disabled:cursor-not-allowed disabled:opacity-40"
                  style={{ color: "var(--color-text-muted)" }}
                  title={
                    groupProp.type === "status" && leftTarget && !canMoveLeft
                      ? "다른 상태 그룹으로는 이동할 수 없습니다"
                      : "왼쪽으로 이동"
                  }
                  data-board-column-move="left"
                >
                  <ArrowLeft size={13} strokeWidth={2} aria-hidden="true" />
                  왼쪽
                </button>
                <button
                  type="button"
                  disabled={!canMoveRight || !rightTarget}
                  onClick={() => {
                    if (!option || !rightTarget) return;
                    handlers.moveOption(option.id, rightTarget.id, "after");
                    close();
                  }}
                  className="flex min-h-[32px] items-center justify-center gap-1.5 rounded-[6px] px-2 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)] disabled:cursor-not-allowed disabled:opacity-40"
                  style={{ color: "var(--color-text-muted)" }}
                  title={
                    groupProp.type === "status" && rightTarget && !canMoveRight
                      ? "다른 상태 그룹으로는 이동할 수 없습니다"
                      : "오른쪽으로 이동"
                  }
                  data-board-column-move="right"
                >
                  오른쪽
                  <ArrowRight size={13} strokeWidth={2} aria-hidden="true" />
                </button>
              </div>
              <button
                type="button"
                onClick={() => {
                  const ok = window.confirm(
                    "이 컬럼을 삭제할까요? 해당 카드들은 미지정으로 이동합니다.",
                  );
                  if (!ok) return;
                  handlers.deleteOption(option.id);
                  close();
                }}
                className="mt-2 flex min-h-[32px] w-full items-center rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-fail-fg)" }}
                data-board-column-delete={option.id}
              >
                컬럼 삭제
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function BoardCard({
  ctx,
  row,
  groupPropId,
  columnRowIds,
  manualOrderLocked,
  dropPlacement,
  onDragOverCard,
  onDragLeaveCard,
  onDropOnCard,
  onDragStart,
}: {
  ctx: DbCtx;
  row: ProjectDatabaseRow;
  groupPropId: string;
  columnRowIds: string[];
  manualOrderLocked: boolean;
  dropPlacement: BoardDropPlacement | null;
  onDragOverCard: (event: DragEvent<HTMLDivElement>) => void;
  onDragLeaveCard: () => void;
  onDropOnCard: (event: DragEvent<HTMLDivElement>, placement: BoardDropPlacement) => void;
  onDragStart: () => void;
}) {
  const title = rowTitle(row, ctx.titleProp);
  const isFirstInColumn = columnRowIds[0] === row.row_id;
  const isLastInColumn = columnRowIds[columnRowIds.length - 1] === row.row_id;
  const hidden = new Set(ctx.view.hidden ?? []);
  const coverProp = ctx.db.properties.find(
    (p) => p.id === ctx.view.cover_property && p.type === "files",
  );
  const coverFiles = coverProp ? normalizeFiles(row.props?.[coverProp.id]) : [];
  const hasCoverMedia = coverFiles.some((file) => file.type === "image" || file.type === "video");
  const fallbackCover = !hasCoverMedia && row.cover ? row.cover : null;
  // 카드 미리보기 속성: 제목·그룹 속성·빈 값 제외(노션 협업업무표 카드처럼).
  const preview = ctx.db.properties.filter(
    (p) =>
      p.id !== ctx.titleProp.id &&
      p.id !== groupPropId &&
      !(hasCoverMedia && p.id === coverProp?.id) &&
      !hidden.has(p.id) &&
      !isReadonlyType(p.type) &&
      row.props?.[p.id] != null &&
      row.props?.[p.id] !== "" &&
      !(Array.isArray(row.props?.[p.id]) && (row.props[p.id] as unknown[]).length === 0),
  );
  return (
    <div
      draggable
      onDragStart={(e) => {
        e.stopPropagation();
        e.dataTransfer.effectAllowed = "move";
        onDragStart();
      }}
      onDragOver={onDragOverCard}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          onDragLeaveCard();
        }
      }}
      onDrop={(event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        const placement =
          dropPlacement ?? (event.clientY < rect.top + rect.height / 2 ? "before" : "after");
        onDropOnCard(event, placement);
      }}
      onClick={() => ctx.openRow(row.row_id)}
      // 노션처럼 카드 어디를 잡아도 이동 드래그가 된다 — 텍스트 선택이 드래그를
      // 가로채지 않게 select-none(복사는 카드를 열어서). cardCopy 유틸 미사용.
      className="group/card relative cursor-pointer select-none rounded-[8px] px-2.5 py-2 active:cursor-grabbing"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-divider-soft)",
        boxShadow: "0 1px 3px rgba(0,0,0,0.06)",
      }}
    >
      {dropPlacement ? (
        <span
          aria-hidden="true"
          className={cx(
            "pointer-events-none absolute left-2 right-2 z-20 h-[2px] rounded-full",
            dropPlacement === "before" ? "top-0" : "bottom-0",
          )}
          style={{ background: "var(--color-text-strong)" }}
        />
      ) : null}
      <div className="absolute top-1.5 right-1.5 z-10 hidden items-center gap-0.5 rounded-[6px] p-0.5 group-hover/card:flex">
        <button
          type="button"
          onMouseDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            ctx.moveRowOrder(row.row_id, columnRowIds, -1);
          }}
          disabled={manualOrderLocked || isFirstInColumn}
          className="flex h-6 w-6 items-center justify-center rounded-[4px] hover:bg-[var(--color-divider-soft)] disabled:cursor-not-allowed disabled:opacity-35"
          style={{ background: "var(--color-modal)", color: "var(--color-text-muted)" }}
          title={manualOrderLocked ? "정렬 적용 중에는 수동 순서 변경 불가" : "위로 이동"}
          aria-label="카드 위로 이동"
          data-board-card-move="up"
        >
          <ArrowUp size={13} strokeWidth={2} aria-hidden="true" />
        </button>
        <button
          type="button"
          onMouseDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            ctx.moveRowOrder(row.row_id, columnRowIds, 1);
          }}
          disabled={manualOrderLocked || isLastInColumn}
          className="flex h-6 w-6 items-center justify-center rounded-[4px] hover:bg-[var(--color-divider-soft)] disabled:cursor-not-allowed disabled:opacity-35"
          style={{ background: "var(--color-modal)", color: "var(--color-text-muted)" }}
          title={manualOrderLocked ? "정렬 적용 중에는 수동 순서 변경 불가" : "아래로 이동"}
          aria-label="카드 아래로 이동"
          data-board-card-move="down"
        >
          <ArrowDown size={13} strokeWidth={2} aria-hidden="true" />
        </button>
        <button
          type="button"
          onMouseDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            void ctx.duplicateRow(row.row_id, columnRowIds);
          }}
          className="flex h-6 w-6 items-center justify-center rounded-[4px] hover:bg-[var(--color-divider-soft)]"
          style={{ background: "var(--color-modal)", color: "var(--color-text-muted)" }}
          title="카드 복제"
          aria-label="카드 복제"
          data-board-card-duplicate=""
        >
          <Copy size={13} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>
      {hasCoverMedia ? <MediaCover files={coverFiles} /> : null}
      {fallbackCover ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={fallbackCover}
          alt=""
          className="-mx-2.5 mb-2 h-[116px] w-[calc(100%+20px)] rounded-t-[8px] object-cover"
          draggable={false}
        />
      ) : null}
      <div
        className="flex items-center gap-1.5 text-[var(--text-body-sm)] font-medium leading-snug"
        style={{ color: "var(--color-text-strong)" }}
      >
        {row.icon ? <span>{row.icon}</span> : null}
        <span className="min-w-0 flex-1">{title || "제목 없음"}</span>
      </div>
      {preview.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1">
          {preview.map((p) => (
            <CardPreviewValue key={p.id} ctx={ctx} prop={p} value={row.props[p.id]} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function CardPreviewValue({ ctx, prop, value }: { ctx: DbCtx; prop: DbPropertyDef; value: unknown }) {
  if (isOptionType(prop.type)) {
    const ids = Array.isArray(value) ? (value as string[]) : [value as string];
    return (
      <>
        {ids
          .map((id) => prop.options.find((o) => o.id === id))
          .filter((o): o is NonNullable<typeof o> => !!o)
          .map((o) => (
            <TagPill key={o.id} label={o.name} color={o.color as TagColor} />
          ))}
      </>
    );
  }
  if (prop.type === "person") {
    const people = normalizePeople(value)
      .map((id) => ctx.members.find((x) => x.id === id))
      .filter((m): m is MemberOption => !!m);
    if (!people.length) return null;
    return (
      <>
        {people.map((m) => (
          <span key={m.id} className="inline-flex items-center gap-1">
            <RowAvatar name={m.name} color={m.color} />
            <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
              {m.name}
            </span>
          </span>
        ))}
      </>
    );
  }
  if (prop.type === "checkbox") {
    return (
      <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
        {value === true ? `☑ ${prop.name}` : `☐ ${prop.name}`}
      </span>
    );
  }
  if (prop.type === "files") {
    return <FileStrip files={normalizeFiles(value)} compact />;
  }
  if (prop.type === "date") {
    return <DueBadge value={String(value)} />;
  }
  return (
    <span
      className="rounded-[4px] px-1.5 py-[1px] text-[var(--text-meta)]"
      style={{ background: "var(--color-app-canvas)", color: "var(--color-text-muted)" }}
    >
      {prop.type === "url" ? "🔗 " : ""}
      {String(value)}
    </span>
  );
}

/** 카드 날짜 칩: D-day 색상(지남=빨강, 3일 내=주황). 파싱 불가 값은 원문 표시. */
function DueBadge({ value }: { value: string }) {
  const parsed = new Date(`${value.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) {
    return (
      <span
        className="rounded-[4px] px-1.5 py-[1px] text-[var(--text-meta)]"
        style={{ background: "var(--color-app-canvas)", color: "var(--color-text-muted)" }}
      >
        📅 {value}
      </span>
    );
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((parsed.getTime() - today.getTime()) / 86400000);
  const overdue = days < 0;
  const soon = days >= 0 && days <= 3;
  const dday = days === 0 ? "D-day" : days > 0 ? `D-${days}` : `D+${-days}`;
  const label = `${parsed.getMonth() + 1}/${parsed.getDate()}`;
  return (
    <span
      className="rounded-[4px] px-1.5 py-[1px] text-[var(--text-meta)] font-semibold"
      style={{
        background: overdue
          ? "color-mix(in srgb, var(--color-fail-fg, #dc2626) 12%, transparent)"
          : soon
            ? "color-mix(in srgb, var(--color-warn-fg, #d9a400) 14%, transparent)"
            : "var(--color-app-canvas)",
        color: overdue
          ? "var(--color-fail-fg, #dc2626)"
          : soon
            ? "var(--color-warn-fg, #b45309)"
            : "var(--color-text-muted)",
      }}
    >
      📅 {label} · {dday}
    </span>
  );
}

function AddColumn({ ctx, groupProp }: { ctx: DbCtx; groupProp: DbPropertyDef }) {
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const handlers = ctx.handlersFor(groupProp.id);
  if (!adding) {
    return (
      <button
        type="button"
        onClick={() => setAdding(true)}
        className="mt-1 flex h-9 w-[160px] shrink-0 items-center rounded-[10px] px-3 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
        style={{ border: "1px dashed var(--color-divider)", color: "var(--color-text-faint)" }}
      >
        + 컬럼 추가
      </button>
    );
  }
  const commit = () => {
    const t = name.trim();
    if (t) handlers.addOption(t);
    setName("");
    setAdding(false);
  };
  return (
    <div
      className="mt-1 w-[200px] shrink-0 rounded-[10px] p-2"
      style={{ border: "1px solid var(--color-divider)", background: "var(--color-panel)" }}
    >
      <input
        autoFocus
        value={name}
        onChange={(e) => setName(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") {
            setName("");
            setAdding(false);
          }
        }}
        placeholder="컬럼 이름"
        className="w-full rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] outline-none"
        style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
      />
    </div>
  );
}

/* 노션식 행 커버: 상단 배너 이미지 + 추가/변경/제거(업로드). */
function RowCover({ ctx, row }: { ctx: DbCtx; row: ProjectDatabaseRow }) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);

  async function upload(files: FileList | null) {
    const file = files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      const asset = await api.uploadDashboardMedia(file);
      ctx.setRowField(row.row_id, { cover: asset.url });
    } catch {
      /* 업로드 실패는 현재 row 편집 흐름을 막지 않는다. */
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  return (
    <div className="mb-3">
      {row.cover ? (
        <div className="group relative overflow-hidden rounded-[10px]">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={row.cover} alt="" className="h-[160px] w-full object-cover" />
          <div className="absolute top-2 right-2 hidden gap-1 group-hover:flex">
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              disabled={busy}
              className="rounded-[6px] px-2 py-1 text-[var(--text-meta)]"
              style={{
                background: "var(--color-modal)",
                border: "1px solid var(--color-divider)",
                color: "var(--color-text-muted)",
              }}
            >
              {busy ? "업로드 중…" : "커버 변경"}
            </button>
            <button
              type="button"
              onClick={() => ctx.setRowField(row.row_id, { cover: "" })}
              className="rounded-[6px] px-2 py-1 text-[var(--text-meta)]"
              style={{
                background: "var(--color-modal)",
                border: "1px solid var(--color-divider)",
                color: "var(--color-fail-fg)",
              }}
            >
              제거
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={busy}
          className="flex h-8 items-center gap-1 rounded-[6px] px-2 text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
          style={{ color: "var(--color-text-faint)" }}
        >
          🖼 {busy ? "업로드 중…" : "커버 추가"}
        </button>
      )}
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(e) => void upload(e.target.files)}
      />
    </div>
  );
}

/* ------------------------------ row page (peek) ------------------------------ */
function RowPeek({
  ctx,
  row,
  bodyLoading = false,
  bodyError = false,
  onRetryBody,
  onClose,
}: {
  ctx: DbCtx;
  row: ProjectDatabaseRow;
  /** 목록 조회가 생략한 body를 단건 조회로 불러오는 중 — 에디터 대신 placeholder. */
  bodyLoading?: boolean;
  /** body 지연 로드 실패 — 서버 확언 없이 빈 에디터를 열면 실제 본문을 덮어쓴다. */
  bodyError?: boolean;
  onRetryBody?: () => void;
  onClose: () => void;
}) {
  const title = rowTitle(row, ctx.titleProp);
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const mediaFiles = ctx.db.properties
    .filter((p) => p.type === "files")
    .flatMap((p) => normalizeFiles(row.props?.[p.id]));
  return (
    <Modal
      open
      title={`${row.icon ? row.icon + " " : ""}${title || "항목"}`}
      onClose={onClose}
      footer={
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={async () => {
              const newRowId = await ctx.duplicateRow(row.row_id);
              if (newRowId) ctx.openRow(newRowId);
            }}
            className="flex h-9 items-center gap-1.5 rounded-[6px] px-3 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            <Copy size={14} strokeWidth={2} aria-hidden="true" />
            복제
          </button>
          <button
            type="button"
            onClick={() => {
              ctx.deleteRow(row.row_id);
              onClose();
            }}
            className="flex h-9 items-center rounded-[6px] px-3 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-fail-fg)" }}
          >
            삭제
          </button>
        </div>
      }
    >
      {/* 노션 페이지 커버 */}
      <RowCover ctx={ctx} row={row} />

      {/* icon + 큰 제목 (노션 페이지 머리) */}
      <div className="mb-3 flex items-center gap-2">
        <button
          ref={anchorRef}
          type="button"
          onClick={toggle}
          className="flex h-9 w-9 items-center justify-center rounded-[8px] text-[20px] hover:bg-[var(--color-app-canvas)]"
          title="아이콘"
        >
          {row.icon || "📄"}
        </button>
        <div className="min-w-0 flex-1">
          <PropValue
            prop={ctx.titleProp}
            value={row.props?.[ctx.titleProp.id]}
            onChange={(v) => ctx.setRowProp(row.row_id, ctx.titleProp.id, v)}
            handlers={ctx.handlersFor(ctx.titleProp.id)}
            members={ctx.members}
            databaseId={ctx.db.database_id}
            rowId={row.row_id}
            fill
          />
        </div>
      </div>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-[60] grid grid-cols-6 gap-1 rounded-[10px] p-2"
          style={{
            ...anchoredStyle(rect, 240, 140),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
        >
          {EMOJI_CHOICES.map((e) => (
            <button
              key={e}
              type="button"
              onClick={() => {
                ctx.setRowField(row.row_id, { icon: e });
                close();
              }}
              className="flex h-8 items-center justify-center rounded-[6px] text-[18px] hover:bg-[var(--color-app-canvas)]"
            >
              {e}
            </button>
          ))}
        </div>
      ) : null}

      {/* 속성 목록 (제목 제외) */}
      <div className="flex flex-col gap-2">
        {ctx.db.properties
          .filter((p) => p.type !== "title")
          .map((p) => (
            <div key={p.id} className="flex items-start gap-2">
              <div
                className="flex w-[112px] shrink-0 items-center gap-1.5 pt-1.5 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                <span style={{ color: "var(--color-text-faint)" }}>{PROP_TYPE_ICON[p.type]}</span>
                <span className="truncate">{p.name}</span>
              </div>
              <div className="min-w-0 flex-1">
                <PropValue
                  prop={p}
                  value={row.props?.[p.id]}
                  onChange={(v) => ctx.setRowProp(row.row_id, p.id, v)}
                  handlers={ctx.handlersFor(p.id)}
                  members={ctx.members}
                  meta={rowMeta(row)}
                  databaseId={ctx.db.database_id}
                  rowId={row.row_id}
                  fill
                />
              </div>
            </div>
          ))}
      </div>

      <div className="my-3" style={{ borderTop: "1px solid var(--color-divider-soft)" }} />

      <MediaGallery files={mediaFiles} />

      {mediaFiles.length ? (
        <div className="my-3" style={{ borderTop: "1px solid var(--color-divider-soft)" }} />
      ) : null}

      {/* 노션 행 페이지 본문 — body 지연 로드가 확언된 후에만 에디터를 마운트한다
          (로드 전/실패 시 마운트하면 빈 본문 편집이 실제 본문을 덮어쓴다). */}
      {bodyLoading ? (
        <p
          className="px-1 py-6 text-[var(--text-body-sm)]"
          style={{ color: "var(--color-text-faint)" }}
        >
          본문 불러오는 중…
        </p>
      ) : bodyError ? (
        <div className="flex flex-wrap items-center gap-2 px-1 py-6">
          <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-fail-fg)" }}>
            본문을 불러오지 못했습니다.
          </p>
          <button
            type="button"
            onClick={onRetryBody}
            className="flex h-9 items-center rounded-[6px] px-3 text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
            style={{ border: "1px solid var(--color-divider)", color: "var(--color-text-muted)" }}
          >
            재시도
          </button>
        </div>
      ) : (
        <RichBodyEditor
          key={row.row_id}
          id={row.row_id}
          value={row.body ?? ""}
          minHeight={200}
          onCommit={(v) => ctx.setRowField(row.row_id, { body: v || null })}
        />
      )}
    </Modal>
  );
}
