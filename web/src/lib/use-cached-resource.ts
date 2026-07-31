// 클라이언트 stale-while-revalidate 리소스 캐시 (즉시 내비게이션 1단계).
//
// 모든 페이지가 "use client" + mount fetch라 라우트를 오갈 때마다 스켈레톤 뒤에서
// 같은 데이터를 다시 받아온다. 이 훅은 마지막 응답을 모듈 레벨 Map에 담아 재방문
// 시 캐시를 **동기로** 먼저 그리고(스켈레톤 없음), 백그라운드 revalidate로 조용히
// 갱신한다. SoR은 언제나 서버 응답이다 — 캐시는 페인트 순서만 바꾸고, revalidate
// 결과가 도착하면 같은 key를 구독한 모든 컴포넌트에 반영된다.
//
// 무효화 규칙: 로그아웃 시 clearCachedResources()로 전체 폐기(AppShell logout 경로,
// offline-board-cache와 동일한 소유자 경계). TTL(기본 30초) 안에서는 revalidate를
// 생략해 연타 내비게이션 왕복을 막는다. 실패도 timestamp와 함께 기록해 같은 TTL
// 창 동안 자동 재검증을 건너뛴다(에러 backoff — 지속 실패 endpoint가 네트워크
// 속도로 무한 재시도되는 것을 차단; 명시적 refresh()/refreshCachedResource()는
// 우회). key=null이면 비활성(fetch/캐시 없음). SSR에서는 구독 없이 초기(빈)
// 상태만 반환한다.

import { useCallback, useEffect, useSyncExternalStore } from "react";

interface CacheEntry {
  data: unknown;
  ts: number;
}

interface ErrorEntry {
  err: unknown;
  ts: number;
}

const cache = new Map<string, CacheEntry>();
const inflight = new Map<string, Promise<void>>();
const errors = new Map<string, ErrorEntry>();
const subscribers = new Map<string, Set<() => void>>();
// useSyncExternalStore 스냅샷 — cache/errors/inflight의 어떤 변화든 key 버전을 올려
// 재렌더를 유도한다(스냅샷이 entry 자체면 error-only 변화가 bail-out돼 유실된다).
const versions = new Map<string, number>();
// key별 최신 fetcher — refresh()가 안정된 identity로 최신 fetcher를 집게 한다
// (fetcher는 보통 인라인 화살표 함수라 매 렌더 identity가 바뀐다). effect에서 갱신.
const fetchers = new Map<string, () => Promise<unknown>>();

const DEFAULT_TTL_MS = 30_000;

function bumpAndNotify(key: string): void {
  versions.set(key, (versions.get(key) ?? 0) + 1);
  const listeners = subscribers.get(key);
  if (!listeners) return;
  for (const listener of listeners) listener();
}

/** 로그아웃 등 소유자 전환 시 전체 캐시를 비운다(다음 사용자에게 이전 데이터 금지). */
export function clearCachedResources(): void {
  cache.clear();
  errors.clear();
  for (const key of [...subscribers.keys()]) bumpAndNotify(key);
}

// 로컬 mutation이 잦은 화면(예: 에이전트 채팅)용 명령형 접근 — 훅 대신 로컬 state를
// SoR로 유지하면서, 캐시는 mount 시 즉시 페인트 seed + 최신 상태 write-back으로만 쓴다.
export function readCachedResource<T>(key: string): T | null {
  const entry = cache.get(key);
  return entry ? (entry.data as T) : null;
}

export function writeCachedResource<T>(key: string, data: T): void {
  cache.set(key, { data, ts: Date.now() });
  errors.delete(key);
  bumpAndNotify(key);
}

/** 리소스 삭제(예: 대화 세션 삭제) 후 캐시 seed를 비운다 — 다음 방문이 stale
 *  데이터를 페인트하지 않도록. 구독자에게도 알린다. */
export function removeCachedResource(key: string): void {
  cache.delete(key);
  errors.delete(key);
  bumpAndNotify(key);
}

/** mutation 직후 파생 뷰(예: AppShell 배지) 즉시 갱신용 명령형 재검증. TTL/에러
 *  backoff를 우회한다는 점에서 훅의 refresh()와 같은 시맨틱이다. 구독자가 없어
 *  등록된 fetcher가 없으면 캐시/에러만 무효화해 다음 mount가 새로 받게 한다. */
export function refreshCachedResource(key: string): Promise<void> {
  const fetcher = fetchers.get(key);
  if (fetcher) return revalidate(key, fetcher);
  cache.delete(key);
  errors.delete(key);
  bumpAndNotify(key);
  return Promise.resolve();
}

function revalidate(key: string, fetcher: () => Promise<unknown>): Promise<void> {
  const pending = inflight.get(key);
  if (pending) return pending;
  const promise = fetcher()
    .then((data) => {
      cache.set(key, { data, ts: Date.now() });
      errors.delete(key);
    })
    .catch((err) => {
      errors.set(key, { err, ts: Date.now() });
    })
    .finally(() => {
      inflight.delete(key);
      bumpAndNotify(key);
    });
  inflight.set(key, promise);
  // 시작도 알린다 — 캐시를 그린 컴포넌트의 stale(백그라운드 갱신 중) 표시용.
  bumpAndNotify(key);
  return promise;
}

export interface CachedResource<T> {
  data: T | null;
  error: unknown;
  /** 캐시도 에러도 없는 최초 로딩(스켈레톤 케이스). */
  loading: boolean;
  /** 캐시 데이터를 그린 상태에서 백그라운드 revalidate 진행 중. */
  stale: boolean;
  /** TTL 무시 강제 재검증. */
  refresh: () => Promise<void>;
}

export function useCachedResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  opts?: { ttlMs?: number },
): CachedResource<T> {
  const ttlMs = opts?.ttlMs ?? DEFAULT_TTL_MS;

  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      if (key === null) return () => {};
      let listeners = subscribers.get(key);
      if (!listeners) {
        listeners = new Set();
        subscribers.set(key, listeners);
      }
      listeners.add(onStoreChange);
      return () => {
        listeners.delete(onStoreChange);
        if (listeners.size === 0) {
          subscribers.delete(key);
          fetchers.delete(key);
        }
      };
    },
    [key],
  );
  const getSnapshot = useCallback(
    () => (key === null ? 0 : (versions.get(key) ?? 0)),
    [key],
  );
  useSyncExternalStore(subscribe, getSnapshot, () => 0);

  // 최신 fetcher를 module map에 유지 — fetcher는 보통 인라인 화살표 함수라 매 렌더
  // identity가 바뀐다. deps 없는 effect로 매 커밋 갱신해 두면 아래 데이터 effect와
  // refresh()가 안정된 경로(fetchers.get)로 항상 최신 closure를 집는다.
  useEffect(() => {
    if (key === null) return;
    fetchers.set(key, fetcher);
  });

  // 자동 재검증은 key/ttl 변화(mount 포함)에만 발화한다. fetcher를 deps에 두면
  // 실패 → bumpAndNotify → 재렌더 → 새 identity → 새 fetch의 무한 네트워크 루프가
  // 된다(예: member 403 /agent-work 배지, personal 노드 federated page graph 404).
  useEffect(() => {
    if (key === null) return;
    const current = cache.get(key);
    // TTL 이내의 신선한 캐시는 재검증하지 않는다(연타 내비게이션 왕복 차단).
    if (current && Date.now() - current.ts <= ttlMs) return;
    // 최근 실패한 key는 같은 창 동안 자동 재시도하지 않는다(에러 backoff —
    // 재마운트/재구독 storm 차단). 명시적 refresh()는 revalidate 직행이라 우회한다.
    const failed = errors.get(key);
    if (failed && Date.now() - failed.ts <= ttlMs) return;
    const latest = fetchers.get(key);
    if (latest) void revalidate(key, latest);
  }, [key, ttlMs]);

  const refresh = useCallback(() => {
    if (key === null) return Promise.resolve();
    const latest = fetchers.get(key);
    return latest ? revalidate(key, latest) : Promise.resolve();
  }, [key]);

  const entry = key === null ? null : (cache.get(key) ?? null);
  const error = key !== null && !entry ? (errors.get(key)?.err ?? null) : null;
  return {
    data: entry ? (entry.data as T) : null,
    error,
    loading: key !== null && !entry && error == null,
    stale: entry != null && key !== null && inflight.has(key),
    refresh,
  };
}
