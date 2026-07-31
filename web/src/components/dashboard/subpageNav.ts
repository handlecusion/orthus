"use client";

import { createContext } from "react";

/**
 * 본문(BlockNote) 안 하위 페이지/데이터베이스 블록이 부모의 네비게이션 스택을 부르는 채널.
 *
 * subpageBlock ↔ databaseBlock 순환 import를 끊으려고 컨텍스트만 별도 모듈로 둔다
 * (둘 다 여기서 import). onOpenDatabase가 없으면 databaseBlock은 전체 페이지 라우트로
 * fallback 한다.
 */
export const SubpageNavContext = createContext<{
  onOpenPage?: (pageId: string) => void;
  onOpenDatabase?: (databaseId: string) => void;
  /** 공유 view 권한에서는 inline DB의 모든 mutation control도 잠근다. */
  readOnly?: boolean;
}>({});
