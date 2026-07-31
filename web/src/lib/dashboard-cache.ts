/**
 * 대시보드 공용 조회의 모듈 레벨 TTL 캐시(의존성 없음).
 *
 * 한 화면에 DatabaseView 인스턴스가 여러 개 떠도(인라인 DB 블록 다수)
 * listTeamMembers 같은 공용 목록을 인스턴스마다 refetch하지 않게 한다.
 * in-flight promise를 공유해 동시 마운트 시에도 요청은 1회다.
 * 실패한 promise는 캐시에서 비워 다음 호출이 재시도한다.
 */
import { api, type TeamMember } from "@/lib/api";

const TTL_MS = 5 * 60 * 1000;

type Entry<T> = { at: number; promise: Promise<T> };

let teamMembersEntry: Entry<TeamMember[]> | null = null;

export function listTeamMembersCached(): Promise<TeamMember[]> {
  const now = Date.now();
  if (teamMembersEntry && now - teamMembersEntry.at < TTL_MS) return teamMembersEntry.promise;
  const entry: Entry<TeamMember[]> = {
    at: now,
    promise: api.listTeamMembers().catch((e: unknown) => {
      if (teamMembersEntry === entry) teamMembersEntry = null;
      throw e;
    }),
  };
  teamMembersEntry = entry;
  return entry.promise;
}

/** 팀원 생성/수정/삭제 mutation 후 호출 — TTL이 남아 있어도 다음 조회가
 *  최신 목록을 다시 받는다(사람 선택기 최대 5분 stale 방지). */
export function invalidateTeamMembersCache(): void {
  teamMembersEntry = null;
}
