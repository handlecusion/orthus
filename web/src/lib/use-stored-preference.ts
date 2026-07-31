"use client";

/**
 * localStorage에 기억하는 client-only enum 선호값 훅(예: 보기 방식 토글).
 *
 * post-mount setState 대신 lazy initializer로 첫 client 렌더에서 저장값을 읽어
 * 잘못된 값 flash를 막는다(react-hooks/set-state-in-effect 회피). SSR/차단 환경은
 * fallback. 저장 실패(사파리 private 등)는 무시하되 in-memory 토글은 동작한다.
 *
 * `values`는 고정 배열(기존 API) 또는 validator 함수(type guard)를 받는다.
 * validator 변형은 open-ended 값(예: 테마 preset id, `custom:<id>`)을 저장할 때
 * 쓴다 — 배열 API 호출부는 그대로 동작한다.
 *
 * `paramKey`를 주면 같은 이름의 URL 쿼리 파라미터가 저장값보다 우선한다
 * (예: 네비게이션 링크가 의도한 뷰로 착지하도록).
 */
import { useState } from "react";

export function useStoredPreference<T extends string>(
  key: string,
  values: readonly T[] | ((raw: string) => raw is T),
  fallback: T,
  options?: { paramKey?: string },
): [T, (next: T) => void] {
  const accepts = (raw: string): raw is T =>
    Array.isArray(values) ? (values as readonly string[]).includes(raw) : (values as (raw: string) => raw is T)(raw);

  const read = (): T => {
    if (typeof window === "undefined") return fallback;
    try {
      const paramKey = options?.paramKey;
      if (paramKey) {
        const param = new URLSearchParams(window.location.search).get(paramKey);
        if (param != null && accepts(param)) {
          return param;
        }
      }
      const raw = window.localStorage.getItem(key);
      if (raw != null && accepts(raw)) {
        return raw;
      }
    } catch {
      /* SSR/차단 스토리지 — fallback */
    }
    return fallback;
  };

  const [value, setValue] = useState<T>(read);

  const set = (next: T) => {
    setValue(next);
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(key, next);
    } catch {
      /* 차단 스토리지 — in-memory 토글은 유지 */
    }
  };

  return [value, set];
}
