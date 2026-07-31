import type {
  DbFileValue,
  DbPropertyDef,
  DbPropType,
  DbSelectOption,
  ProjectDatabase,
  ProjectDatabaseRow,
} from "@/lib/api";
import { type TagColor } from "@/components/dashboard/kit";

/** 새 선택 옵션/속성에 돌려쓸 색 팔레트(kit TAG_COLORS 키). */
export const OPTION_PALETTE: TagColor[] = [
  "gray",
  "blue",
  "green",
  "yellow",
  "orange",
  "red",
  "purple",
  "pink",
  "teal",
  "brown",
];

export function nextOptionColor(used: string[]): TagColor {
  const free = OPTION_PALETTE.find((c) => !used.includes(c));
  return free ?? OPTION_PALETTE[used.length % OPTION_PALETTE.length];
}

/** 속성/옵션/뷰 id (서버 _short_id와 같은 8자 규약). */
export function shortId(): string {
  const raw =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
  return raw.replace(/-/g, "").slice(0, 8);
}

export const PROP_TYPE_LABEL: Record<DbPropType, string> = {
  title: "제목",
  text: "텍스트",
  number: "숫자",
  select: "선택",
  status: "상태",
  multi_select: "다중 선택",
  date: "날짜",
  checkbox: "체크박스",
  person: "사람",
  url: "링크",
  files: "파일 & 미디어",
  created_time: "생성 시각",
  last_edited_time: "수정 시각",
};

export const PROP_TYPE_ICON: Record<DbPropType, string> = {
  title: "📝",
  text: "≡",
  number: "#",
  select: "▾",
  status: "◉",
  multi_select: "≣",
  date: "📅",
  checkbox: "☑",
  person: "👤",
  url: "🔗",
  files: "📎",
  created_time: "🕓",
  last_edited_time: "🖊",
};

/** 사용자가 추가할 수 있는 속성 타입(title은 자동 1개라 제외). */
export const ADDABLE_TYPES: DbPropType[] = [
  "text",
  "select",
  "status",
  "multi_select",
  "date",
  "number",
  "checkbox",
  "person",
  "url",
  "files",
  "created_time",
  "last_edited_time",
];

/** 값을 저장하지 않고 행 메타에서 파생되는 읽기 전용 속성. */
export const READONLY_TYPES: DbPropType[] = ["created_time", "last_edited_time"];
export function isReadonlyType(t: DbPropType): boolean {
  return t === "created_time" || t === "last_edited_time";
}

// 노션 status 컬럼 그룹 정렬 순서(보드 컬럼 배열).
const STATUS_GROUP_ORDER: Record<string, number> = {
  to_do: 0,
  in_progress: 1,
  complete: 2,
};

/** 보드 컬럼 순서: status는 그룹(to_do→in_progress→complete) 기준, 그 외는 옵션 순서. */
export function orderedOptions(prop: DbPropertyDef): DbSelectOption[] {
  if (prop.type !== "status") return prop.options;
  return [...prop.options].sort(
    (a, b) =>
      (STATUS_GROUP_ORDER[a.group ?? "to_do"] ?? 0) -
      (STATUS_GROUP_ORDER[b.group ?? "to_do"] ?? 0),
  );
}

export const SELECT_LIKE: DbPropType[] = ["select", "status"];
export function isSelectLike(t: DbPropType): boolean {
  return t === "select" || t === "status";
}
export function isOptionType(t: DbPropType): boolean {
  return t === "select" || t === "status" || t === "multi_select";
}

export function titleProperty(db: ProjectDatabase): DbPropertyDef {
  return db.properties.find((p) => p.type === "title") ?? db.properties[0];
}

export function rowTitle(row: ProjectDatabaseRow, titleProp: DbPropertyDef): string {
  const v = row.props?.[titleProp.id];
  return typeof v === "string" && v.trim() ? v : "";
}

/** files 속성 값(또는 임의 값)을 DbFileValue 배열로 안전 변환. */
export function asFileValues(value: unknown): DbFileValue[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (v): v is DbFileValue =>
      !!v && typeof v === "object" && typeof (v as DbFileValue).url === "string",
  );
}

function findImageBlock(blocks: unknown): string | null {
  if (!Array.isArray(blocks)) return null;
  for (const b of blocks) {
    const blk = b as { type?: string; props?: { url?: string }; children?: unknown };
    if (blk.type === "image" && blk.props?.url) return blk.props.url;
    const child = findImageBlock(blk.children);
    if (child) return child;
  }
  return null;
}

/** 카드/페이지 미리보기 이미지(노션 카드 커버처럼): 행 커버 → files 속성 첫 이미지 →
 *  본문 첫 이미지 블록. 없으면 null. */
export function rowPreviewImage(row: ProjectDatabaseRow, db: ProjectDatabase): string | null {
  if (row.cover) return row.cover;
  for (const p of db.properties) {
    if (p.type !== "files") continue;
    const img = asFileValues(row.props?.[p.id]).find((f) => f.type === "image");
    if (img) return img.url ?? null;
  }
  if (row.body) {
    try {
      return findImageBlock(JSON.parse(row.body));
    } catch {
      /* 평문 body — 이미지 없음 */
    }
  }
  return null;
}

export function findOption(
  prop: DbPropertyDef,
  optionId: string | null | undefined,
): DbSelectOption | null {
  if (!optionId) return null;
  return prop.options.find((o) => o.id === optionId) ?? null;
}

/** board 컬럼 그룹화: 그룹 속성 옵션 + "미지정" 버킷으로 행을 분류. */
export function groupRowsByOption(
  rows: ProjectDatabaseRow[],
  prop: DbPropertyDef,
): Map<string, ProjectDatabaseRow[]> {
  const map = new Map<string, ProjectDatabaseRow[]>();
  map.set("__none__", []);
  for (const o of prop.options) map.set(o.id, []);
  for (const r of rows) {
    const v = r.props?.[prop.id];
    const key = typeof v === "string" && map.has(v) ? v : "__none__";
    map.get(key)!.push(r);
  }
  for (const list of map.values())
    list.sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
  return map;
}
