/**
 * 평문 텍스트 안의 URL(예: 티켓 노트·댓글에 적어 넣은 링크)을 "클릭하면 바로 그
 * 사이트로 이동"하는 <a> 노드로 바꿔 준다.
 *
 * 사용자가 노트/댓글에 URL을 적어도 그동안은 그냥 텍스트라 눌러도 이동이 안 됐다.
 * 여기서 http(s):// 또는 www. 로 시작하는 토큰만 링크로 만든다. 스킴/`www.` 없는
 * 맨 도메인(Node.js, index.html 같은 것)은 오탐이 많아 일부러 제외한다.
 *
 * href 정규화·스킴 화이트리스트(javascript: 등 차단)는 toExternalHref가 담당한다.
 */
import type { ReactNode } from "react";

import { toExternalHref } from "./external-url";

// http(s):// 또는 www. 로 시작하고 공백/꺾쇠 전까지 이어지는 토큰.
const URL_PATTERN = /(?:https?:\/\/|www\.)[^\s<>]+/gi;

// URL 뒤에 붙은 문장부호를 링크에서 떼어낸다. 닫는 괄호는 URL 안에 여는 괄호가
// 없을 때(= 문장에서 URL을 괄호로 감싼 경우)만 떼어, 위키백과처럼 경로에 괄호가
// 들어간 URL은 보존한다.
function splitTrailingPunctuation(token: string): { url: string; trailing: string } {
  let url = token;
  let trailing = "";
  const bracketPairs: Record<string, string> = { ")": "(", "]": "[", "}": "{" };
  while (url.length > 0) {
    const last = url[url.length - 1];
    if (".,;:!?\"'»›".includes(last)) {
      trailing = last + trailing;
      url = url.slice(0, -1);
      continue;
    }
    const open = bracketPairs[last];
    if (open) {
      const opens = url.split(open).length - 1;
      const closes = url.split(last).length - 1;
      if (closes > opens) {
        trailing = last + trailing;
        url = url.slice(0, -1);
        continue;
      }
    }
    break;
  }
  return { url, trailing };
}

/**
 * 텍스트를 문자열 + <a> 노드의 배열로 쪼갠다. 그대로 JSX children으로 넣으면 된다.
 * `<a>` 는 새 탭으로 열고(rel=noopener), 클릭이 상위 카드/드래그 핸들러로 버블링돼
 * 상세가 다시 열리거나 드래그가 시작되지 않도록 stopPropagation 한다.
 */
export function linkifyToNodes(text: string | null | undefined): ReactNode[] {
  if (!text) return [];
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    const index = match.index ?? 0;
    const rawToken = match[0];
    const { url, trailing } = splitTrailingPunctuation(rawToken);
    if (index > cursor) nodes.push(text.slice(cursor, index));
    const href = url ? toExternalHref(url) : null;
    if (href) {
      nodes.push(
        <a
          key={`lk-${key++}`}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(event) => event.stopPropagation()}
        >
          {url}
        </a>,
      );
    } else if (url) {
      nodes.push(url);
    }
    if (trailing) nodes.push(trailing);
    cursor = index + rawToken.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

/** 텍스트를 linkify해 그대로 렌더한다. `<p>`/`<div>` 같은 컨테이너 안에 넣어 쓴다. */
export function Linkify({ text }: { text: string | null | undefined }) {
  return <>{linkifyToNodes(text)}</>;
}
