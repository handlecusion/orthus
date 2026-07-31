"use client";

import {
  type AgentDelegateTarget,
  type AgentDelegateTargetList,
} from "@/lib/api";

/* ---------------------------- @멘션 자동완성 ---------------------------- */
// 채팅 입력창에서 `@`로 시작하는 단어를 입력하면 팀원(위임 대상) 목록을 띄우는
// 경량 인라인 자동완성. 서버 채팅 위임 파서가 앞머리 `@<email>` 토큰을 기대하므로
// 선택 시 항상 canonical email 토큰(`@user@example.com `)으로 치환한다.
// 데이터 소스는 기존 GET /agent-work/delegate-targets(targets) — 신규 endpoint 없음.

/** 캐럿이 올라가 있는 `@단어` 조각. start=@ 위치, end=캐럿, query=@ 뒤 텍스트. */
export interface MentionFragment {
  start: number;
  end: number;
  query: string;
}

/** 캐럿 위치의 단어가 멘션 트리거인지 판정한다. `@`가 텍스트 시작 또는 공백 뒤에
 *  올 때만 멘션으로 본다(문장 중간의 이메일 표기 등은 그냥 텍스트). */
export function findMentionFragment(text: string, caret: number): MentionFragment | null {
  if (caret < 1 || caret > text.length) return null;
  // 캐럿 왼쪽으로 공백/줄바꿈 전까지가 현재 단어 — 루프 종료 조건이 곧
  // "@ 앞은 텍스트 시작 또는 공백" 보장이다.
  let start = caret;
  while (start > 0 && !/\s/.test(text[start - 1])) start -= 1;
  if (text[start] !== "@") return null;
  return { start, end: caret, query: text.slice(start + 1, caret) };
}

/** 입력 조각으로 팀원 목록을 필터링한다. display_name/email 대소문자 무시
 *  부분일치, 최대 max건. 삽입 토큰이 이메일이라 이메일 없는 대상은 제안하지 않는다. */
export function filterMentionTargets(
  targets: AgentDelegateTarget[],
  query: string,
  max = 6,
): AgentDelegateTarget[] {
  const q = query.trim().toLowerCase();
  const out: AgentDelegateTarget[] = [];
  for (const target of targets) {
    if (!target.email) continue;
    if (q) {
      const name = (target.display_name ?? "").toLowerCase();
      const email = target.email.toLowerCase();
      if (!name.includes(q) && !email.includes(q)) continue;
    }
    out.push(target);
    if (out.length >= max) break;
  }
  return out;
}

/** 드래프트가 알려진 팀원 이메일 `@<email>`로 **시작**하면 그 대상과 멘션 토큰을
 *  뗀 나머지 지시문을 돌려준다. 정확한 email 일치(대소문자 무시)만 인정 — 문장
 *  중간 멘션이나 미등록 주소는 null(일반 orchestrate 경로 유지). */
export function parseLeadingMention(
  text: string,
  targets: AgentDelegateTargetList | null,
): { target: AgentDelegateTarget; instruction: string } | null {
  const trimmed = text.trimStart();
  if (!trimmed.startsWith("@")) return null;
  const match = /^@(\S+)/.exec(trimmed);
  if (!match) return null;
  const token = match[1].toLowerCase();
  const target =
    (targets?.targets ?? []).find((t) => (t.email ?? "").toLowerCase() === token) ?? null;
  if (!target) return null;
  const instruction = trimmed.slice(match[0].length).trim();
  // 지시문이 비면 위임할 내용이 없으므로 멘션 위임으로 취급하지 않는다.
  if (!instruction) return null;
  return { target, instruction };
}

export const MENTION_LISTBOX_ID = "agent-mention-listbox";

export function mentionOptionId(index: number): string {
  return `agent-mention-opt-${index}`;
}

/** UserPicker의 온라인 점과 같은 시맨틱의 경량 상태 점(멘션 목록 전용). */
function MentionStatusDot({ online }: { online: boolean }) {
  const color = online ? "var(--color-pass-fg)" : "var(--color-text-faint)";
  const label = online ? "온라인" : "오프라인";
  return (
    <span
      aria-label={label}
      className="inline-block h-2 w-2 shrink-0 rounded-full"
      style={{
        background: online ? color : "transparent",
        border: `1.5px solid ${color}`,
      }}
      title={label}
    />
  );
}

/** 입력창 위에 뜨는 멘션 후보 패널. DropdownShell(버튼 anchored)과 달리 textarea
 *  래퍼에 절대배치되는 위쪽 오버레이라 별도 구현 — 스타일/roles는 동형이다.
 *  키보드 내비게이션은 textarea onKeyDown이 소유하고(입력 포커스 유지), 여기서는
 *  마우스/터치 선택만 처리한다. onMouseDown preventDefault로 textarea blur를 막는다. */
export function MentionAutocomplete({
  candidates,
  highlightIndex,
  onHighlight,
  onSelect,
}: {
  candidates: AgentDelegateTarget[];
  highlightIndex: number;
  onHighlight: (index: number) => void;
  onSelect: (target: AgentDelegateTarget) => void;
}) {
  if (candidates.length === 0) return null;
  return (
    <div
      aria-label="팀원 멘션"
      className="absolute bottom-full left-0 z-20 mb-1 max-h-[264px] w-full max-w-[340px] overflow-y-auto rounded-[var(--radius-toolbar)] p-1"
      id={MENTION_LISTBOX_ID}
      role="listbox"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        boxShadow: "var(--shadow-toolbar)",
      }}
    >
      {candidates.map((target, index) => {
        const active = index === highlightIndex;
        return (
          <button
            aria-selected={active}
            className="flex min-h-[44px] w-full items-center justify-between gap-2 rounded-[6px] px-2 py-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-sidebar-active)]"
            id={mentionOptionId(index)}
            key={target.user_id}
            onClick={() => onSelect(target)}
            onMouseDown={(event) => event.preventDefault()}
            onMouseEnter={() => onHighlight(index)}
            role="option"
            type="button"
            style={{
              background: active ? "var(--color-sidebar-active)" : "transparent",
              color: "var(--color-text-strong)",
            }}
          >
            <span className="flex min-w-0 items-center gap-1.5">
              <MentionStatusDot online={target.online} />
              <span className="truncate font-medium">
                {target.display_name || target.email}
                {target.is_self ? " (나)" : ""}
              </span>
            </span>
            <span
              className="max-w-[45%] shrink-0 truncate text-[var(--text-micro)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {target.email}
            </span>
          </button>
        );
      })}
    </div>
  );
}
