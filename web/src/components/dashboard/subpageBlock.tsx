"use client";

/**
 * subpage 커스텀 블록 — 노션식 "페이지 안의 페이지".
 * 본문(BlockNote) 안에서 page_id를 참조하는 클릭 가능한 줄로 렌더된다.
 * 클릭하면 SubpageNavContext.onOpenPage(pageId)로 해당 페이지를 연다.
 * 본문 안에 또 subpage 블록을 넣어 무한 중첩이 가능하다.
 */

import { useContext } from "react";
import { createReactBlockSpec } from "@blocknote/react";
import { BlockNoteSchema, defaultBlockSpecs } from "@blocknote/core";
import { databaseBlock } from "@/components/dashboard/databaseBlock";
import { SubpageNavContext } from "@/components/dashboard/subpageNav";

// 기존 import 경로 호환을 위해 re-export(소비자는 subpageBlock에서 가져온다).
export { SubpageNavContext };

function SubpageRow({
  pageId,
  title,
  icon,
}: {
  pageId: string;
  title: string;
  icon: string;
}) {
  const { onOpenPage } = useContext(SubpageNavContext);
  return (
    <div
      contentEditable={false}
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => {
        if (pageId) onOpenPage?.(pageId);
      }}
      className="my-0.5 flex w-full cursor-pointer items-center gap-2 rounded-[6px] px-1.5 py-1 hover:bg-[var(--color-app-canvas)]"
      role="button"
      title="페이지 열기"
    >
      <span className="text-[16px] leading-none">{icon || "📄"}</span>
      {/* 제목 텍스트는 Cmd+C 로 복사할 수 있게 선택을 허용한다. 행의 onMouseDown
          preventDefault(에디터 캐럿 방지)가 제목에는 걸리지 않도록 stopPropagation 한다.
          클릭(네비게이션)은 onClick 으로 그대로 동작한다. */}
      <span
        onMouseDown={(e) => e.stopPropagation()}
        className="min-w-0 flex-1 truncate text-[var(--text-body-sm)] underline decoration-[rgba(0,0,0,0.18)] underline-offset-2 select-text"
        style={{ color: "var(--color-text-strong)", fontWeight: 600 }}
      >
        {title || "새 페이지"}
      </span>
      <span
        className="shrink-0 text-[var(--text-meta)]"
        style={{ color: "var(--color-text-faint)" }}
        aria-hidden
      >
        ↗
      </span>
    </div>
  );
}

export const subpageBlock = createReactBlockSpec(
  {
    type: "subpage",
    propSchema: {
      pageId: { default: "" },
      title: { default: "새 페이지" },
      icon: { default: "📄" },
    },
    content: "none",
  },
  {
    render: ({ block }) => (
      <SubpageRow
        pageId={block.props.pageId}
        title={block.props.title}
        icon={block.props.icon}
      />
    ),
  },
);

/** 기본 블록 + subpage 를 모두 포함하는 에디터 스키마.
 * createReactBlockSpec은 (options?) => BlockSpec 형태의 creator를 돌려주므로
 * 스키마에 넣을 때 한 번 호출해 BlockSpec으로 만든다. */
export const schemaWithSubpage = BlockNoteSchema.create({
  blockSpecs: {
    ...defaultBlockSpecs,
    subpage: subpageBlock(),
    database: databaseBlock(),
  },
});

export type RBPartialBlock = (typeof schemaWithSubpage)["PartialBlock"];

/**
 * body(BlockNote JSON 문자열)에서 특정 page_id를 참조하는 subpage 블록의 title을
 * 새 값으로 바꿔 다시 직렬화한다(중첩 children 포함). 본문이 JSON이 아니거나
 * 해당 블록이 없으면 원본을 그대로 돌려준다. 하위 페이지 이름을 바꿨을 때 부모
 * 본문의 링크 라벨을 동기화하는 데 쓴다.
 */
export function setSubpageTitleInBody(
  body: string | null,
  pageId: string,
  title: string,
): string | null {
  if (!body) return body;
  let doc: unknown;
  try {
    doc = JSON.parse(body);
  } catch {
    return body;
  }
  if (!Array.isArray(doc)) return body;
  let changed = false;
  const walk = (blocks: unknown[]) => {
    for (const b of blocks) {
      const blk = b as {
        type?: string;
        props?: { pageId?: string; title?: string };
        children?: unknown[];
      };
      if (blk.type === "subpage" && blk.props?.pageId === pageId) {
        if (blk.props.title !== title) {
          blk.props.title = title;
          changed = true;
        }
      }
      if (Array.isArray(blk.children) && blk.children.length > 0) walk(blk.children);
    }
  };
  walk(doc);
  return changed ? JSON.stringify(doc) : body;
}
