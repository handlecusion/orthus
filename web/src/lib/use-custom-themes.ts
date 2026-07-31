"use client";

/**
 * 사용자 정의 테마 CRUD — localStorage("orthus-custom-themes") backed.
 *
 * 값은 `ThemeDefinition[]` JSON. 읽기에서 방어적으로 검증한다: 배열이 아니면
 * 버리고, id/name/base가 깨진 entry는 drop, overrides는 allowlist+hex로
 * filter(sanitizeOverrides). 디바이스 선호라 로그아웃과 무관하게 남는다
 * (orthus-theme와 동일 정책).
 *
 * 같은 탭 쓰기는 module-level listener로, 다른 탭 쓰기는 window "storage"
 * 이벤트로 반영한다. snapshot은 raw 문자열 기준 캐시로 reference-stable하게
 * 유지한다(useSyncExternalStore 요구).
 */

import { useCallback, useSyncExternalStore } from "react";
import { sanitizeOverrides, type ThemeBase, type ThemeDefinition } from "./theme-tokens";

export const CUSTOM_THEMES_KEY = "orthus-custom-themes";

const EMPTY: readonly ThemeDefinition[] = [];

let cacheRaw: string | null | undefined;
let cache: readonly ThemeDefinition[] = EMPTY;
/** setItem이 막힌 환경(사파리 private 등)의 세션 내 fallback — 성공 write 시 해제. */
let memoryThemes: readonly ThemeDefinition[] | null = null;
const listeners = new Set<() => void>();

function parseThemes(raw: string | null): readonly ThemeDefinition[] {
  if (!raw) return EMPTY;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return EMPTY;
    const out: ThemeDefinition[] = [];
    const seen = new Set<string>();
    for (const entry of parsed) {
      if (entry == null || typeof entry !== "object") continue;
      const { id, name, base } = entry as Record<string, unknown>;
      if (typeof id !== "string" || !id || seen.has(id)) continue;
      if (base !== "light" && base !== "dark") continue;
      seen.add(id);
      out.push({
        id,
        name: typeof name === "string" && name.trim() ? name.trim() : "이름 없는 테마",
        base: base as ThemeBase,
        overrides: sanitizeOverrides((entry as Record<string, unknown>).overrides),
      });
    }
    return out;
  } catch {
    return EMPTY;
  }
}

/** 현재 저장된 사용자 정의 테마(검증 통과분). hook 밖(테마 resolve 등)에서도 사용. */
export function readCustomThemes(): readonly ThemeDefinition[] {
  if (typeof window === "undefined") return EMPTY;
  // setItem 실패로 in-memory만 남은 상태 — localStorage의 stale raw로 덮지 않는다.
  if (memoryThemes) return memoryThemes;
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(CUSTOM_THEMES_KEY);
  } catch {
    return EMPTY;
  }
  if (raw !== cacheRaw) {
    cacheRaw = raw;
    cache = parseThemes(raw);
  }
  return cache;
}

function notify(): void {
  for (const listener of listeners) listener();
}

function writeThemes(themes: readonly ThemeDefinition[]): void {
  try {
    window.localStorage.setItem(CUSTOM_THEMES_KEY, JSON.stringify(themes));
  } catch {
    /* 차단 스토리지 — 세션 내 in-memory로만 유지(read가 이 값을 우선한다) */
    memoryThemes = themes;
    notify();
    return;
  }
  memoryThemes = null;
  cacheRaw = undefined; // 다음 read에서 재파싱
  notify();
}

function subscribe(callback: () => void): () => void {
  listeners.add(callback);
  const onStorage = (event: StorageEvent) => {
    // key === null은 storage.clear() — 함께 갱신.
    if (event.key === null || event.key === CUSTOM_THEMES_KEY) callback();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(callback);
    window.removeEventListener("storage", onStorage);
  };
}

function getServerSnapshot(): readonly ThemeDefinition[] {
  return EMPTY;
}

export function useCustomThemes(): {
  themes: readonly ThemeDefinition[];
  saveTheme: (theme: ThemeDefinition) => void;
  deleteTheme: (id: string) => void;
} {
  const themes = useSyncExternalStore(subscribe, readCustomThemes, getServerSnapshot);

  // upsert: 같은 id가 있으면 교체(이름변경/편집), 없으면 추가(생성/복제).
  const saveTheme = useCallback((theme: ThemeDefinition) => {
    const clean: ThemeDefinition = {
      id: theme.id,
      name: theme.name.trim() || "이름 없는 테마",
      base: theme.base,
      overrides: sanitizeOverrides(theme.overrides),
    };
    const current = readCustomThemes();
    const next = current.some((t) => t.id === clean.id)
      ? current.map((t) => (t.id === clean.id ? clean : t))
      : [...current, clean];
    writeThemes(next);
  }, []);

  const deleteTheme = useCallback((id: string) => {
    writeThemes(readCustomThemes().filter((t) => t.id !== id));
  }, []);

  return { themes, saveTheme, deleteTheme };
}
