/**
 * 테마 토큰 SoR — 사용자 정의 테마가 override할 수 있는 토큰의 단일 출처.
 *
 * - `THEME_TOKENS`: allowlist. globals.css `:root`의 구조 `--color-*` +
 *   `--channel-*` + 파생 semantic 중 안전한 것(accent)만 담는다. 그림자·폰트·
 *   spacing은 테마 대상이 아니다. 여기 없는 property는 적용 경로에서 걸러진다.
 * - `TOKEN_ALIASES`: 구조 이름 ↔ Tailwind `@theme` 단축 이름이 갈라지는 12쌍.
 *   utility(`bg-canvas`, `text-strong`, `bg-channel-blue` 등)는 단축 이름을
 *   읽으므로 override 적용 코드는 항상 두 이름을 같이 써야 한다.
 * - `BASE_TOKEN_VALUES`: 빌트인 라이트/다크 기본값(globals.css와 동일 hex).
 *   에디터의 현재값 표시와 "기본값으로" reset에 쓴다. globals.css 팔레트를
 *   바꾸면 여기도 같이 갱신한다.
 * - `PRESET_THEMES`: 코드 상수 프리셋. 각각 소수 토큰만 override하고 텍스트
 *   대비(AA)를 지키는 Ojosama 호환 팔레트다.
 *
 * 알려진 제외: `/board` 개인 보드(personal-ojosama.css)는 class-scope 로컬
 * 팔레트 + 하드코딩 hex라 사용자 정의 테마가 닿지 않는다 — 기본 라이트/다크로
 * 유지. 메일 HTML(signature/html-body/인용)은 수·발신 메일용 의도적 하드코딩
 * 색이라 테마를 따라가면 안 된다.
 *
 * 이 모듈은 server component(layout.tsx의 FOUC 스크립트 생성)에서도 import
 * 되므로 "use client"/hook 없이 순수 상수만 둔다.
 */

export type ThemeBase = "light" | "dark";

export type TokenGroupId = "surface" | "text" | "border" | "status" | "channel";

export interface TokenGroup {
  id: TokenGroupId;
  label: string;
}

export const TOKEN_GROUPS: readonly TokenGroup[] = [
  { id: "surface", label: "배경/표면" },
  { id: "text", label: "텍스트" },
  { id: "border", label: "구분선" },
  { id: "status", label: "강조/상태" },
  { id: "channel", label: "채널" },
] as const;

export interface ThemeTokenDef {
  /** 구조 CSS 변수 이름 (globals.css `:root` 기준). */
  token: string;
  label: string;
  group: TokenGroupId;
  /** true면 에디터 기본 노출, false면 "고급" disclosure 아래. */
  core: boolean;
}

export const THEME_TOKENS: readonly ThemeTokenDef[] = [
  // 배경/표면
  { token: "--color-app-canvas", label: "캔버스", group: "surface", core: true },
  { token: "--color-card", label: "카드", group: "surface", core: true },
  { token: "--color-panel", label: "패널", group: "surface", core: true },
  { token: "--color-sidebar", label: "사이드바", group: "surface", core: false },
  { token: "--color-sidebar-active", label: "사이드바 활성", group: "surface", core: false },
  { token: "--color-modal", label: "모달", group: "surface", core: false },
  { token: "--color-popover-dark", label: "팝오버", group: "surface", core: false },
  // 텍스트
  { token: "--color-text-strong", label: "텍스트 강조", group: "text", core: true },
  { token: "--color-text-body", label: "텍스트 본문", group: "text", core: true },
  { token: "--color-text-muted", label: "텍스트 보조", group: "text", core: true },
  { token: "--color-text-faint", label: "텍스트 희미", group: "text", core: false },
  // 구분선
  { token: "--color-divider", label: "구분선", group: "border", core: true },
  { token: "--color-divider-soft", label: "구분선(옅음)", group: "border", core: false },
  { token: "--color-toolbar-border", label: "툴바 테두리", group: "border", core: false },
  { token: "--color-toolbar-hover", label: "툴바 호버", group: "border", core: false },
  // 강조/상태
  { token: "--color-accent", label: "강조(액센트)", group: "status", core: true },
  { token: "--color-progress", label: "진행(초록)", group: "status", core: true },
  { token: "--color-attention", label: "주의(주황)", group: "status", core: true },
  { token: "--color-warn-fg", label: "경고 텍스트", group: "status", core: true },
  { token: "--color-fail-fg", label: "실패 텍스트", group: "status", core: true },
  { token: "--color-pass-fg", label: "성공 텍스트", group: "status", core: false },
  { token: "--color-pass-bg", label: "성공 배경", group: "status", core: false },
  { token: "--color-warn-bg", label: "경고 배경", group: "status", core: false },
  { token: "--color-fail-bg", label: "실패 배경", group: "status", core: false },
  // 채널 (배지/도트 전용 포인트 팔레트)
  { token: "--channel-blue", label: "채널 블루", group: "channel", core: false },
  { token: "--channel-amber", label: "채널 앰버", group: "channel", core: false },
  { token: "--channel-mint", label: "채널 민트", group: "channel", core: false },
  { token: "--channel-violet", label: "채널 바이올렛", group: "channel", core: false },
  { token: "--channel-pink", label: "채널 핑크", group: "channel", core: false },
  { token: "--channel-slate", label: "채널 슬레이트", group: "channel", core: false },
] as const;

export const THEMEABLE_TOKEN_SET: ReadonlySet<string> = new Set(
  THEME_TOKENS.map((t) => t.token),
);

/**
 * 구조 이름 → Tailwind `@theme` 단축 twin. globals.css에서 이름이 갈라지는
 * 쌍 전부(텍스트/표면 6 + 채널 6). override를 쓸 때 두 이름 모두에 setProperty
 * 해야 utility 클래스와 inline var() 소비자가 같이 바뀐다.
 */
export const TOKEN_ALIASES: Readonly<Record<string, string>> = {
  "--color-app-canvas": "--color-canvas",
  "--color-text-strong": "--color-strong",
  "--color-text-body": "--color-body",
  "--color-text-muted": "--color-muted",
  "--color-text-faint": "--color-faint",
  "--color-popover-dark": "--color-popover",
  "--channel-blue": "--color-channel-blue",
  "--channel-amber": "--color-channel-amber",
  "--channel-mint": "--color-channel-mint",
  "--channel-violet": "--color-channel-violet",
  "--channel-pink": "--color-channel-pink",
  "--channel-slate": "--color-channel-slate",
} as const;

/**
 * globals.css var() 파생 그래프 — 토큰 → 그 값을 공급하는 source 토큰.
 * BASE_TOKEN_VALUES는 resolved hex 스냅샷이라, source가 override된 문서에서는
 * 파생 토큰의 실제 resolve 값이 스냅샷과 달라진다. 저장 전 prune과 에디터
 * 유효값 표시는 이 그래프를 따라가야 프리뷰==저장이 유지된다.
 * globals.css의 Semantic derivations 선언과 항상 동기화한다.
 */
export const TOKEN_DERIVATIONS: Readonly<Record<string, string>> = {
  "--color-accent": "--channel-blue",
  "--color-text-link": "--color-accent",
  "--color-info-fg": "--color-accent",
} as const;

/**
 * 파생 source가 (전이적으로) override돼 있는가 — "기본값과 같은 override"
 * prune 판정용. source가 갈린 상태면 겉보기 기본값 pin도 실제 resolve 값이
 * 달라지므로 제거하면 안 된다.
 */
export function isDerivationSourceOverridden(
  token: string,
  overrides: Readonly<Record<string, string>>,
): boolean {
  let src = TOKEN_DERIVATIONS[token];
  while (src) {
    if (src in overrides) return true;
    src = TOKEN_DERIVATIONS[src];
  }
  return false;
}

/**
 * 빌트인 라이트/다크 기본값 — globals.css `:root` / `[data-theme="dark"]`와
 * 동일한 hex. `--color-accent`는 파생(var(--channel-blue)) 대신 resolved hex를
 * 둔다(<input type="color">가 hex만 받는다).
 */
export const BASE_TOKEN_VALUES: Readonly<Record<ThemeBase, Readonly<Record<string, string>>>> = {
  light: {
    "--color-app-canvas": "#f5f5f6",
    "--color-card": "#ffffff",
    "--color-panel": "#fbfbfc",
    "--color-sidebar": "#e6e7e8",
    "--color-sidebar-active": "#d4d5d7",
    "--color-modal": "#ffffff",
    "--color-popover-dark": "#1f2023",
    "--color-text-strong": "#303133",
    "--color-text-body": "#4b4c4f",
    "--color-text-muted": "#737477",
    "--color-text-faint": "#a0a1a4",
    "--color-divider": "#dedfe1",
    "--color-divider-soft": "#ececee",
    "--color-toolbar-border": "#d4d5d8",
    "--color-toolbar-hover": "#f2f2f3",
    "--color-accent": "#5d9bd8",
    "--color-progress": "#57bf78",
    "--color-attention": "#ef7f62",
    "--color-warn-fg": "#9a5b00",
    "--color-fail-fg": "#b42318",
    "--color-pass-fg": "#1f7a4d",
    "--color-pass-bg": "#e8f6ee",
    "--color-warn-bg": "#fdf3e7",
    "--color-fail-bg": "#fdeceb",
    "--channel-blue": "#5d9bd8",
    "--channel-amber": "#efc47b",
    "--channel-mint": "#62c4ba",
    "--channel-violet": "#9b68d6",
    "--channel-pink": "#d36be9",
    "--channel-slate": "#9fb4bf",
  },
  dark: {
    "--color-app-canvas": "#1a1b1e",
    "--color-card": "#26272b",
    "--color-panel": "#1e1f23",
    "--color-sidebar": "#16171a",
    "--color-sidebar-active": "#2a2b2f",
    "--color-modal": "#2a2b30",
    "--color-popover-dark": "#0f1012",
    "--color-text-strong": "#f0f0f1",
    "--color-text-body": "#d0d1d4",
    "--color-text-muted": "#9a9b9f",
    "--color-text-faint": "#6a6b6f",
    "--color-divider": "#35363b",
    "--color-divider-soft": "#2c2d31",
    "--color-toolbar-border": "#3a3b40",
    "--color-toolbar-hover": "#2e2f34",
    "--color-accent": "#5d9bd8",
    "--color-progress": "#57bf78",
    "--color-attention": "#ef7f62",
    "--color-warn-fg": "#fbbf24",
    "--color-fail-fg": "#f87171",
    "--color-pass-fg": "#4ade80",
    "--color-pass-bg": "#0f2e1c",
    "--color-warn-bg": "#2d1e08",
    "--color-fail-bg": "#2d1212",
    "--channel-blue": "#5d9bd8",
    "--channel-amber": "#efc47b",
    "--channel-mint": "#62c4ba",
    "--channel-violet": "#9b68d6",
    "--channel-pink": "#d36be9",
    "--channel-slate": "#9fb4bf",
  },
} as const;

/** 프리셋/사용자 정의 테마 공통 형태. */
export interface ThemeDefinition {
  id: string;
  name: string;
  base: ThemeBase;
  /** allowlist 토큰 → hex color. base 기본값에서 바꿀 것만 담는다. */
  overrides: Record<string, string>;
}

/**
 * 빌트인 프리셋 — 각 base 팔레트 위에 표면/구분선/강조만 소수 override.
 * 텍스트 토큰은 라이트 프리셋에서 톤만 맞추고(모두 카드 대비 AA 이상),
 * 다크 프리셋은 다크 기본 텍스트(AA 검증 완료)를 그대로 쓴다.
 */
export const PRESET_THEMES: readonly ThemeDefinition[] = [
  {
    id: "preset-ocean",
    name: "오션",
    base: "light",
    overrides: {
      "--color-app-canvas": "#eaf1f6",
      "--color-sidebar": "#dbe7ef",
      "--color-sidebar-active": "#c3d7e5",
      "--color-panel": "#f4f8fb",
      "--color-divider": "#d3e0e9",
      "--color-divider-soft": "#e4edf3",
      "--color-toolbar-border": "#c9d8e3",
      "--color-accent": "#2f7fc1",
    },
  },
  {
    id: "preset-sepia",
    name: "세피아",
    base: "light",
    overrides: {
      "--color-app-canvas": "#f3ede1",
      "--color-sidebar": "#e9e0cd",
      "--color-sidebar-active": "#dbcfb4",
      "--color-panel": "#faf5ea",
      "--color-card": "#fffdf6",
      "--color-modal": "#fffdf6",
      "--color-divider": "#e0d7c2",
      "--color-divider-soft": "#ece5d3",
      "--color-toolbar-border": "#d6ccb5",
      "--color-text-strong": "#362f27",
      "--color-text-body": "#514a3e",
      "--color-text-muted": "#7c7365",
      "--color-accent": "#9c6b2f",
    },
  },
  {
    id: "preset-forest",
    name: "포레스트",
    base: "light",
    overrides: {
      "--color-app-canvas": "#ecf3ec",
      "--color-sidebar": "#dde9dd",
      "--color-sidebar-active": "#c8dcc8",
      "--color-panel": "#f5faf5",
      "--color-divider": "#d4e2d4",
      "--color-divider-soft": "#e5efe5",
      "--color-toolbar-border": "#c9dbc9",
      "--color-accent": "#2e7d4f",
      "--color-progress": "#43a368",
    },
  },
  {
    id: "preset-midnight",
    name: "미드나이트",
    base: "dark",
    overrides: {
      "--color-app-canvas": "#12151d",
      "--color-sidebar": "#0d1017",
      "--color-sidebar-active": "#222938",
      "--color-panel": "#171b24",
      "--color-card": "#1d222d",
      "--color-modal": "#212734",
      "--color-divider": "#2d3444",
      "--color-divider-soft": "#252b38",
      "--color-toolbar-border": "#333b4d",
      "--color-accent": "#7fb3e8",
    },
  },
  {
    id: "preset-mocha",
    name: "모카",
    base: "dark",
    overrides: {
      "--color-app-canvas": "#1b1713",
      "--color-sidebar": "#15110e",
      "--color-sidebar-active": "#2d2620",
      "--color-panel": "#201b16",
      "--color-card": "#28221c",
      "--color-modal": "#2c2620",
      "--color-divider": "#3a322a",
      "--color-divider-soft": "#2f2822",
      "--color-toolbar-border": "#443b31",
      "--color-text-strong": "#f3ede5",
      "--color-text-body": "#d9d1c6",
      "--color-text-muted": "#a69c8e",
      "--color-accent": "#d9a05b",
    },
  },
] as const;

export function getPresetTheme(id: string): ThemeDefinition | undefined {
  return PRESET_THEMES.find((p) => p.id === id);
}

/** hex 색만 허용 — 에디터 산출 형식이며 FOUC 경로에 임의 CSS가 들어가지 않게 한다. */
const HEX_COLOR_RE = /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;

export function isThemeColorValue(value: unknown): value is string {
  return typeof value === "string" && HEX_COLOR_RE.test(value.trim());
}

/** 저장/적용 전 overrides 정리: allowlist 밖 토큰·hex가 아닌 값 제거. */
export function sanitizeOverrides(raw: unknown): Record<string, string> {
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return {};
  const out: Record<string, string> = {};
  for (const [token, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!THEMEABLE_TOKEN_SET.has(token)) continue;
    if (!isThemeColorValue(value)) continue;
    out[token] = value.trim().toLowerCase();
  }
  return out;
}
