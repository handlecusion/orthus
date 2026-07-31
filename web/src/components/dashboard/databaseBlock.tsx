"use client";

/**
 * database 커스텀 블록 — 노션식 "페이지 안의 데이터베이스".
 *
 * props.inline:
 *  - true  → 본문 안에 표/보드(칸반)를 인라인 렌더(DatabaseView).
 *  - false → 클릭하면 전체 페이지로 가는 링크 행(노션 inline=false 풀페이지 DB).
 *
 * 블록은 databaseId만 들고, 실제 데이터(속성/행)는 DatabaseView/링크가 직접 API에서
 * 가져온다. 메뉴의 "데이터베이스 삭제"는 블록을 본문에서 제거하고 서버 데이터까지 지운다.
 */

import { useContext, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { createReactBlockSpec } from "@blocknote/react";
import { api } from "@/lib/api";
import { DatabaseView } from "@/components/dashboard/database/DatabaseView";
import { SubpageNavContext } from "@/components/dashboard/subpageNav";

function deleteDatabaseFromBlock(editor: unknown, block: unknown, databaseId: string) {
  void api.deleteProjectDatabase(databaseId).catch(() => {});
  try {
    (editor as { removeBlocks: (blocks: unknown[]) => void }).removeBlocks([block]);
  } catch {
    /* 이미 제거됨 */
  }
}

/** inline=false: 전체 페이지 데이터베이스로 가는 링크 행. 노션식으로, 부모 페이지의
 *  네비게이션 스택(onOpenDatabase)이 있으면 그 안에서 열고, 없으면 전체 페이지 라우트로 간다. */
function DatabaseLink({ databaseId }: { databaseId: string }) {
  const router = useRouter();
  const { onOpenDatabase } = useContext(SubpageNavContext);
  const [meta, setMeta] = useState<{ title: string; icon: string | null; count: number } | null>(
    null,
  );
  useEffect(() => {
    let alive = true;
    // 경량 meta endpoint 우선 — 구 API(배포 skew)에서 404면 기존 전체 번들로 fallback.
    api
      .getProjectDatabaseMeta(databaseId)
      .then((m) => {
        if (alive) setMeta({ title: m.title, icon: m.icon, count: m.row_count });
      })
      .catch(() => {
        // 제목/아이콘/행 수만 필요 — body는 opt-in 생략(구 API는 파라미터 무시).
        api
          .getProjectDatabase(databaseId, { body: "none" })
          .then((b) => {
            if (alive)
              setMeta({ title: b.database.title, icon: b.database.icon, count: b.rows.length });
          })
          .catch(() => {});
      });
    return () => {
      alive = false;
    };
  }, [databaseId]);
  return (
    <div
      contentEditable={false}
      onMouseDown={(e) => e.preventDefault()}
      onClick={() =>
        onOpenDatabase ? onOpenDatabase(databaseId) : router.push(`/dashboard/db/${databaseId}`)
      }
      role="button"
      title="열기"
      className="my-1 flex w-full cursor-pointer items-center gap-2 rounded-[6px] px-1.5 py-1.5 hover:bg-[var(--color-app-canvas)]"
      style={{ border: "1px solid var(--color-divider-soft)" }}
    >
      <span className="text-[16px] leading-none">{meta?.icon || "🗂️"}</span>
      {/* DB 제목은 Cmd+C 로 복사할 수 있게 선택을 허용한다(행 preventDefault 회피). */}
      <span
        onMouseDown={(e) => e.stopPropagation()}
        className="min-w-0 flex-1 truncate text-[var(--text-body-sm)] font-semibold underline decoration-[rgba(0,0,0,0.18)] underline-offset-2 select-text"
        style={{ color: "var(--color-text-strong)" }}
      >
        {meta?.title || "데이터베이스"}
      </span>
      {meta ? (
        <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
          {meta.count}개
        </span>
      ) : null}
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

function InlineDatabase({
  databaseId,
  editor,
  block,
}: {
  databaseId: string;
  editor: unknown;
  block: unknown;
}) {
  const { readOnly } = useContext(SubpageNavContext);
  return (
    <div contentEditable={false}>
      {readOnly ? (
        <div
          className="mb-1 text-right text-[10px] font-semibold"
          style={{ color: "var(--color-text-faint)" }}
        >
          읽기 전용 데이터베이스
        </div>
      ) : null}
      <DatabaseView
        databaseId={databaseId}
        readOnly={readOnly}
        onDeleteDatabase={
          readOnly ? undefined : () => deleteDatabaseFromBlock(editor, block, databaseId)
        }
      />
    </div>
  );
}

export const databaseBlock = createReactBlockSpec(
  {
    type: "database",
    propSchema: {
      databaseId: { default: "" },
      inline: { default: true },
    },
    content: "none",
  },
  {
    render: ({ block, editor }) => {
      const databaseId = block.props.databaseId;
      const inline = block.props.inline !== false;
      if (!databaseId) {
        return (
          <div
            contentEditable={false}
            className="my-2 rounded-[8px] px-3 py-4 text-center text-[var(--text-body-sm)]"
            style={{
              border: "1px dashed var(--color-divider)",
              color: "var(--color-text-faint)",
            }}
          >
            빈 데이터베이스
          </div>
        );
      }
      if (!inline) return <DatabaseLink databaseId={databaseId} />;
      return (
        <InlineDatabase
          databaseId={databaseId}
          editor={editor}
          block={block}
        />
      );
    },
  },
);
