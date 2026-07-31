"use client";

/**
 * 라이트/다크/시스템 + 프리셋/사용자 정의 테마 훅.
 *
 * SoR는 localStorage("orthus-theme"). 값은
 * `"light" | "dark" | "system" | <preset id> | "custom:<id>"`이며 미설정이면
 * `"system"`. `resolvedTheme`은 언제나 BASE 모드(light|dark)만 돌려준다 —
 * BlockNote 등 base 모드 소비자 계약 유지. 프리셋/사용자 정의 테마는
 * base 위에 allowlist 토큰 override를 documentElement inline style로 얹는다.
 *
 * 적용 순서(applyThemeResolution):
 * 1. `data-theme=base` 먼저 stamp — `[data-theme="dark"]` cascade가
 *    color-scheme/그림자/기본 팔레트를 잡는다.
 * 2. allowlist 전체 inline property 제거 — 이전 테마(또는 FOUC 스크립트)가
 *    남긴 override를 상태 없이 청소해 빌트인 light/dark로 돌아갈 때 깨끗하다.
 * 3. override를 구조 이름 + Tailwind alias twin 양쪽에 setProperty.
 * 4. 캔버스를 override한 테마면 meta[name=theme-color]도 동기화(원본 복원 포함).
 *
 * 구현:
 * - `theme`은 `useStoredPreference` validator 변형(localStorage backed).
 * - OS color-scheme 변화는 `useSyncExternalStore`로 subscribe.
 * - 사용자 정의 테마 목록 변화(저장/삭제/다른 탭)는 useCustomThemes가 흘려준다.
 *
 * FOUC 방지는 `layout.tsx`의 inline `<script>`가 first paint 전에 동일한 로직
 * (base stamp + override setProperty)을 실행해 처리한다.
 */
import { useEffect, useSyncExternalStore } from "react";
import {
  BASE_TOKEN_VALUES,
  getPresetTheme,
  THEME_TOKENS,
  THEMEABLE_TOKEN_SET,
  TOKEN_ALIASES,
  TOKEN_DERIVATIONS,
  type ThemeDefinition,
} from "./theme-tokens";
import { readCustomThemes, useCustomThemes } from "./use-custom-themes";
import { useStoredPreference } from "./use-stored-preference";

/** "light" | "dark" | "system" | preset id | `custom:<id>` (리터럴 자동완성 유지). */
export type ThemePreference = "light" | "dark" | "system" | (string & {});
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "orthus-theme";
export const CUSTOM_THEME_PREFIX = "custom:";

export function isThemePreference(raw: string): raw is ThemePreference {
  return (
    raw === "light" ||
    raw === "dark" ||
    raw === "system" ||
    getPresetTheme(raw) !== undefined ||
    raw.startsWith(CUSTOM_THEME_PREFIX)
  );
}

function subscribeToColorScheme(callback: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  if (typeof mq.addEventListener === "function") {
    mq.addEventListener("change", callback);
    return () => mq.removeEventListener("change", callback);
  }
  mq.addListener(callback);
  return () => mq.removeListener(callback);
}

function getSystemColorScheme(): ResolvedTheme {
  if (typeof window === "undefined") return "light";
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

function getServerColorScheme(): ResolvedTheme {
  return "light";
}

export interface ThemeResolution {
  base: ResolvedTheme;
  overrides: Record<string, string>;
  /** 활성 프리셋/사용자 정의 테마 객체(빌트인 light/dark/system이면 null). */
  activeTheme: ThemeDefinition | null;
}

export function resolveThemePreference(
  pref: ThemePreference,
  systemScheme: ResolvedTheme,
  customThemes: readonly ThemeDefinition[],
): ThemeResolution {
  if (pref === "light" || pref === "dark") {
    // (string & {}) 브랜치가 리터럴 비교로 좁혀지지 않아 명시적으로 고정한다.
    const base: ResolvedTheme = pref === "dark" ? "dark" : "light";
    return { base, overrides: {}, activeTheme: null };
  }
  const preset = getPresetTheme(pref);
  if (preset) {
    return { base: preset.base, overrides: preset.overrides, activeTheme: preset };
  }
  if (pref.startsWith(CUSTOM_THEME_PREFIX)) {
    const id = pref.slice(CUSTOM_THEME_PREFIX.length);
    const custom = customThemes.find((t) => t.id === id);
    if (custom) {
      return { base: custom.base, overrides: custom.overrides, activeTheme: custom };
    }
  }
  // "system" + 삭제된 custom id 등 알 수 없는 값 → OS 스킴 (FOUC 스크립트와 동일).
  return { base: systemScheme, overrides: {}, activeTheme: null };
}

/** theme-color meta 원본 content — override 해제 시 복원용. */
const metaThemeColorOriginals = new Map<HTMLMetaElement, string>();

function syncMetaThemeColor(resolution: ThemeResolution): void {
  const canvasOverride = resolution.overrides["--color-app-canvas"];
  const metas = document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]');
  metas.forEach((meta) => {
    if (!metaThemeColorOriginals.has(meta)) {
      metaThemeColorOriginals.set(meta, meta.content);
    }
    meta.content = canvasOverride ?? metaThemeColorOriginals.get(meta) ?? meta.content;
  });
}

/**
 * resolution을 문서에 적용. useTheme effect 외에 테마 에디터의 라이브
 * 프리뷰(임시 draft)도 이 함수를 직접 쓴다.
 */
export function applyThemeResolution(resolution: ThemeResolution): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.dataset.theme = resolution.base;
  // 상태 추적 대신 allowlist 전체를 청소 — FOUC 스크립트가 먼저 얹은 inline
  // override까지 포함해 이전 테마 잔여물이 남지 않는다(removeProperty no-op 저렴).
  for (const def of THEME_TOKENS) {
    root.style.removeProperty(def.token);
    const alias = TOKEN_ALIASES[def.token];
    if (alias) root.style.removeProperty(alias);
  }
  for (const [token, value] of Object.entries(resolution.overrides)) {
    if (!THEMEABLE_TOKEN_SET.has(token)) continue;
    root.style.setProperty(token, value);
    const alias = TOKEN_ALIASES[token];
    if (alias) root.style.setProperty(alias, value);
  }
  syncMetaThemeColor(resolution);
}

/**
 * 저장된 선호를 다시 적용(라이브 프리뷰 취소/에디터 unmount 복구용).
 * 멱등이라 아무 때나 불러도 안전하다.
 */
export function reapplyStoredTheme(): void {
  if (typeof window === "undefined") return;
  let pref: ThemePreference = "system";
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw != null && isThemePreference(raw)) pref = raw;
  } catch {
    /* 차단 스토리지 — system */
  }
  applyThemeResolution(resolveThemePreference(pref, getSystemColorScheme(), readCustomThemes()));
}

export function useTheme(): {
  theme: ThemePreference;
  setTheme: (next: ThemePreference) => void;
  resolvedTheme: ResolvedTheme;
  activeTheme: ThemeDefinition | null;
} {
  const [theme, setTheme] = useStoredPreference<ThemePreference>(
    STORAGE_KEY,
    isThemePreference,
    "system",
  );

  const systemScheme = useSyncExternalStore(
    subscribeToColorScheme,
    getSystemColorScheme,
    getServerColorScheme,
  );

  const { themes: customThemes } = useCustomThemes();

  const resolution = resolveThemePreference(theme, systemScheme, customThemes);

  useEffect(() => {
    applyThemeResolution(resolveThemePreference(theme, systemScheme, customThemes));
  }, [theme, systemScheme, customThemes]);

  return {
    theme,
    setTheme,
    resolvedTheme: resolution.base,
    activeTheme: resolution.activeTheme,
  };
}

/**
 * 에디터 현재값 표시용: base 기본값 위에 overrides를 겹친 유효 색.
 * 직접 override가 없는 파생 토큰(TOKEN_DERIVATIONS)은 source 체인을 따라
 * 내려간다 — source가 override된 문서에서 CSS var()가 실제로 resolve하는
 * 값과 동일하게 보여야 프리뷰==저장이 맞는다.
 */
export function effectiveTokenValue(
  base: ResolvedTheme,
  overrides: Record<string, string>,
  token: string,
): string {
  let current: string | undefined = token;
  while (current) {
    const override = overrides[current];
    if (override) return override;
    const next: string | undefined = TOKEN_DERIVATIONS[current];
    if (!next) return BASE_TOKEN_VALUES[base][current] ?? "#000000";
    current = next;
  }
  return "#000000";
}
