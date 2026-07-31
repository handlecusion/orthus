"use client";

// Agent chat assistant-bubble markdown rendering + citation chips + copy button.
//
// XSS 경계: react-markdown은 rehype-raw 없이 raw HTML을 절대 HTML로 주입하지
// 않는다(이스케이프된 텍스트로 렌더). rehype-raw는 의도적으로 추가하지 않는다 —
// 추가하면 저장된 메시지 텍스트의 <script>/<img onerror> 류가 실제 DOM으로
// 들어간다. URL도 react-markdown 기본 defaultUrlTransform이 javascript: 등
// 위험 스킴을 제거한다.

import { Check, Copy } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { WikiLinkRef } from "@/lib/api";

// C7 칩 재수화의 SoR 정규식 (agent-work/page.tsx::rehydratedFragments가 사용).
// 저장된 메시지 텍스트의 프래그먼트 딥링크(`/checklist?work_id=<uuid>`, 구형
// `/agent-work#<uuid>`)를 매칭한다. **저장 텍스트 포맷은 절대 변경 금지** —
// 마크다운 렌더는 이 구간을 먼저 세그먼트로 떼어내 그대로 두고(아래
// splitFragmentDeeplinks), 잔여 텍스트만 마크다운으로 그린다.
export const FRAGMENT_DEEPLINK_RE =
  /\/(?:checklist\?work_id=|agent-work#)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/g;

interface MessageSegment {
  kind: "markdown" | "deeplink";
  text: string;
}

// 프래그먼트 딥링크 구간을 마크다운 파서에 넣지 않고 원문 그대로 분리한다.
// 마크다운이 딥링크 표기를 변형해도(자동 링크/이스케이프) 저장 텍스트는 그대로
// 이므로 재수화(rehydratedFragments)는 영향이 없지만, 화면 표기도 저장 포맷과
// 1:1로 유지해 혼선을 막는다.
export function splitFragmentDeeplinks(text: string): MessageSegment[] {
  const segments: MessageSegment[] = [];
  let last = 0;
  for (const match of text.matchAll(FRAGMENT_DEEPLINK_RE)) {
    const start = match.index ?? 0;
    if (start > last) segments.push({ kind: "markdown", text: text.slice(last, start) });
    segments.push({ kind: "deeplink", text: match[0] });
    last = start + match[0].length;
  }
  if (last < text.length) segments.push({ kind: "markdown", text: text.slice(last) });
  return segments.length ? segments : [{ kind: "markdown", text }];
}

// 채팅 버블 톤에 맞춘 prose 유사 스타일. 색은 버블에서 상속(경고/일반 톤 공용).
// 단일 개행 보존: remark는 soft break를 "\n" 텍스트로 남기므로 p/li의
// whitespace-pre-wrap이 기존 평문 멀티라인 메시지([실행 시작] ack 등) 표시를
// 그대로 유지한다(remark-breaks 불필요).
// react-markdown이 커스텀 컴포넌트에 넘기는 hast `node` prop을 DOM 스프레드에서
// 제거한다(React unknown-prop 경고 방지).
function domProps<T extends { node?: unknown }>(props: T): Omit<T, "node"> {
  const rest = { ...props } as T & { node?: unknown };
  delete rest.node;
  return rest;
}

const MD_COMPONENTS: Components = {
  p: (props) => (
    <p className="mb-2 whitespace-pre-wrap break-words last:mb-0" {...domProps(props)} />
  ),
  a: (props) => (
    <a
      className="break-all font-semibold underline underline-offset-2"
      target="_blank"
      rel="noopener noreferrer"
      {...domProps(props)}
    />
  ),
  ul: (props) => (
    <ul className="mb-2 list-disc space-y-0.5 pl-5 last:mb-0" {...domProps(props)} />
  ),
  ol: (props) => (
    <ol className="mb-2 list-decimal space-y-0.5 pl-5 last:mb-0" {...domProps(props)} />
  ),
  li: (props) => (
    <li className="whitespace-pre-wrap break-words" {...domProps(props)} />
  ),
  h1: (props) => (
    <h1 className="mb-1.5 mt-3 text-[1.05em] font-bold first:mt-0" {...domProps(props)} />
  ),
  h2: (props) => (
    <h2 className="mb-1.5 mt-3 text-[1.02em] font-bold first:mt-0" {...domProps(props)} />
  ),
  h3: (props) => (
    <h3 className="mb-1 mt-2.5 font-semibold first:mt-0" {...domProps(props)} />
  ),
  h4: (props) => (
    <h4 className="mb-1 mt-2 font-semibold first:mt-0" {...domProps(props)} />
  ),
  blockquote: (props) => (
    <blockquote
      className="mb-2 border-l-2 pl-3 opacity-85 last:mb-0"
      style={{ borderColor: "var(--color-divider)" }}
      {...domProps(props)}
    />
  ),
  hr: (props) => (
    <hr className="my-2 border-t" style={{ borderColor: "var(--color-divider)" }} {...domProps(props)} />
  ),
  // 블록 코드: 가로 스크롤은 pre 자체 컨테이너에서만(페이지 가로 스크롤 금지).
  // pre 안의 code는 인라인 code 배경/패딩을 상쇄한다.
  pre: (props) => (
    <pre
      className="mb-2 max-w-full overflow-x-auto rounded-[var(--radius-card)] border p-2.5 font-[family-name:var(--font-mono)] text-[var(--text-micro)] leading-[1.5] last:mb-0 [&>code]:border-0 [&>code]:bg-transparent [&>code]:p-0"
      style={{ borderColor: "var(--color-divider-soft)", background: "var(--color-card)" }}
      {...domProps(props)}
    />
  ),
  code: (props) => (
    <code
      className="rounded border px-1 py-0.5 font-[family-name:var(--font-mono)] text-[0.92em]"
      style={{ borderColor: "var(--color-divider-soft)", background: "var(--color-card)" }}
      {...domProps(props)}
    />
  ),
  // GFM 표: 표만 자체 컨테이너에서 가로 스크롤한다.
  table: (props) => (
    <div className="mb-2 max-w-full overflow-x-auto last:mb-0">
      <table className="w-max min-w-full border-collapse text-[var(--text-body-sm)]" {...domProps(props)} />
    </div>
  ),
  th: (props) => (
    <th
      className="border px-2 py-1 text-left font-semibold"
      style={{ borderColor: "var(--color-divider)" }}
      {...domProps(props)}
    />
  ),
  td: (props) => (
    <td
      className="whitespace-pre-wrap border px-2 py-1 align-top"
      style={{ borderColor: "var(--color-divider)" }}
      {...domProps(props)}
    />
  ),
};

/** 어시스턴트 메시지 본문 마크다운 렌더. 프래그먼트 딥링크 구간은 마크다운을
 *  거치지 않고 원문 그대로(클릭 가능한 링크로만 승격) 유지한다. */
export function MessageMarkdown({ text }: { text: string }) {
  const segments = useMemo(() => splitFragmentDeeplinks(text), [text]);
  return (
    <div className="min-w-0 break-words">
      {segments.map((segment, i) =>
        segment.kind === "deeplink" ? (
          <Link
            key={`${segment.text}-${i}`}
            href={segment.text}
            className="inline-block max-w-full truncate align-bottom font-[family-name:var(--font-mono)] text-[var(--text-micro)] underline underline-offset-2"
          >
            {segment.text}
          </Link>
        ) : (
          <ReactMarkdown key={i} components={MD_COMPONENTS} remarkPlugins={[remarkGfm]}>
            {segment.text}
          </ReactMarkdown>
        ),
      )}
    </div>
  );
}

function wikiPageHref(slug: string): string {
  return `/wiki/${slug.split("/").map(encodeURIComponent).join("/")}`;
}

/** 어시스턴트 버블 하단 인용 칩 — /ask·/wiki의 WikiCitationLinks 패턴 재사용.
 *  클릭 시 해당 위키 페이지로 이동. 데이터는 라이브 턴의 RoutedAnswer에서만
 *  온다(채팅 메시지는 평문 텍스트로 영속 — 히스토리 재로드 영속은 범위 밖). */
export function MessageCitationChips({ links }: { links: WikiLinkRef[] }) {
  if (!links.length) return null;
  return (
    <div
      className="mt-2.5 flex flex-wrap items-center gap-1.5 border-t pt-2.5"
      style={{ borderColor: "var(--color-divider)" }}
    >
      {links.map((link) => (
        <Link
          className="inline-flex min-h-8 max-w-full items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 py-1 text-[var(--text-body-sm)] no-underline"
          href={wikiPageHref(link.slug)}
          key={`${link.scope}-${link.slug}`}
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-divider)",
            color: "var(--color-text-strong)",
          }}
        >
          <span className="truncate">{link.title || link.slug}</span>
          <span className="shrink-0 font-[family-name:var(--font-mono)] text-[var(--text-meta)]">
            {link.scope === "company" ? "회사" : "개인"}
          </span>
        </Link>
      ))}
    </div>
  );
}

/** 어시스턴트 버블 복사 버튼 — 원문 텍스트(navigator.clipboard.writeText)를
 *  복사하고 1.5s "복사됨" 피드백. 데스크톱은 hover/focus 시 노출, 모바일
 *  (compact <760px)은 항상 노출 + 44px 탭 타깃. */
export function CopyMessageButton({ text, compact }: { text: string; compact: boolean }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard 미허용(비보안 컨텍스트 등) — 조용히 무시.
    }
  }
  return (
    <button
      aria-label={copied ? "복사됨" : "메시지 복사"}
      title="복사"
      type="button"
      onClick={copy}
      className={`inline-flex items-center gap-1 rounded-[var(--radius-toolbar)] px-1.5 text-[var(--text-meta)] transition-opacity ${
        compact
          ? "min-h-[44px] min-w-[44px] justify-center opacity-80"
          : "h-7 opacity-0 hover:opacity-100 focus-visible:opacity-100 group-hover:opacity-70"
      }`}
      style={{ color: "var(--color-text-muted)" }}
    >
      {copied ? (
        <>
          <Check size={14} strokeWidth={2} />
          <span>복사됨</span>
        </>
      ) : (
        <Copy size={14} strokeWidth={1.8} />
      )}
    </button>
  );
}
