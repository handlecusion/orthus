"use client";

/**
 * RichBodyEditor — peek 본문용 자동 저장 BlockNote 에디터.
 *
 * 회의록과 같은 저장 규약을 쓴다: body 컬럼에 BlockNote 블록 JSON 문자열을
 * 저장하고, JSON이 아닌 기존 평문은 줄 단위 문단으로 파싱해 보여준다.
 * 명시적 저장 버튼이 없는 peek 흐름에 맞춰 입력 후 디바운스/blur/unmount
 * 시점에 onCommit으로 자동 커밋한다. 이미지는 base64 data-URL로 인라인
 * 임베드되어 별도 업로드 백엔드가 필요 없다. window를 만지므로 소비자는
 * next/dynamic ssr:false로 로드해야 한다.
 *
 * 토글(접을 수 있는 목록/제목)은 BlockNote 기본 슬래시 메뉴로 제공된다.
 * onCreatePage/onOpenPage를 주면 "/하위 페이지" 슬래시로 노션식 중첩 페이지를
 * 만들고 클릭해 들어갈 수 있다.
 */

import { useEffect, useMemo, useRef } from "react";
import { BlockNoteView } from "@blocknote/mantine";
import {
  SuggestionMenuController,
  getDefaultReactSlashMenuItems,
  useCreateBlockNote,
  type DefaultReactSuggestionItem,
} from "@blocknote/react";
import { filterSuggestionItems } from "@blocknote/core";
import { ko } from "@blocknote/core/locales";
import { api } from "@/lib/api";
import { clearPageEditing, markPageEditing, writePageDraft } from "@/lib/pageSave";
import {
  SubpageNavContext,
  schemaWithSubpage,
  type RBPartialBlock,
} from "@/components/dashboard/subpageBlock";

import "@blocknote/core/fonts/inter.css";
import "@blocknote/mantine/style.css";

function parseBody(body: string): RBPartialBlock[] {
  if (!body.trim()) return [{ type: "paragraph" }] as RBPartialBlock[];
  try {
    const parsed = JSON.parse(body);
    if (Array.isArray(parsed) && parsed.length > 0) return parsed as RBPartialBlock[];
  } catch {
    // 평문 폴백
  }
  return body.split("\n").map((line) => ({
    type: "paragraph",
    content: line ? [{ type: "text", text: line, styles: {} }] : [],
  })) as RBPartialBlock[];
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(new Error("파일 읽기 실패"));
    reader.readAsDataURL(file);
  });
}

/** 이미지/동영상/파일을 실서버에 업로드하고 URL을 돌려준다(노션식). base64 인라인은
 *  동영상에 비현실적이라 실파일 저장으로 바꿨다. 업로드 실패 시 이미지에 한해 base64
 *  폴백으로 에디터를 막지 않는다. */
async function uploadMedia(file: File): Promise<string> {
  try {
    const asset = await api.uploadDashboardMedia(file);
    return asset.url;
  } catch {
    if (file.type.startsWith("image/")) return readAsDataUrl(file);
    throw new Error("업로드 실패");
  }
}

/** 빈 문단만 있는 문서인지 — 빈 문서는 body를 ""로 저장해 평문 규약을 유지한다. */
function isEmptyDoc(blocks: unknown[]): boolean {
  return blocks.every((b) => {
    const blk = b as { type?: string; content?: unknown[]; children?: unknown[] };
    return (
      blk.type === "paragraph" &&
      (!blk.content || blk.content.length === 0) &&
      (!blk.children || blk.children.length === 0)
    );
  });
}

export function RichBodyEditor({
  id,
  value,
  onCommit,
  minHeight = 160,
  onOpenPage,
  onOpenDatabase,
  onCreatePage,
  onCreateDatabase,
  autoFocus = false,
  draftMirror = false,
  readOnly = false,
}: {
  /** 문서 정체성(program_id/note_id/page_id). 바뀌면 에디터를 재시드한다. */
  id: string;
  value: string;
  onCommit: (v: string) => void;
  minHeight?: number;
  /** 마운트/창 포커스 시 에디터에 커서를 준다(빠른 메모 전용 — 기본 off). */
  autoFocus?: boolean;
  /** 매 편집을 localStorage 드래프트로 미러(개인 메모 전용 — 기본 off).
   *  공유 표면(컬처/프로젝트/DB row)에서 켜면 남의 최신 편집을 덮을 수 있어 금지. */
  draftMirror?: boolean;
  /** 공유 보기 권한처럼 내용을 탐색만 하고 편집/커밋하지 않는 표면. */
  readOnly?: boolean;
  /** subpage 블록 클릭 시 해당 페이지를 연다(있으면 하위 페이지 기능 활성). */
  onOpenPage?: (pageId: string) => void;
  /** database 블록(inline=false 링크) 클릭 시 같은 페이지 스택에서 연다(노션식). */
  onOpenDatabase?: (databaseId: string) => void;
  /** "/하위 페이지" 실행 시 새 페이지를 만들고 page_id/title을 돌려준다. */
  onCreatePage?: () => Promise<{ page_id: string; title: string }>;
  /** "/데이터베이스" "/보드" 실행 시 새 데이터베이스를 만들고 database_id를 돌려준다. */
  onCreateDatabase?: (opts: {
    view: "table" | "board";
    template: "basic" | "tasks";
  }) => Promise<{ database_id: string }>;
}) {
  // peek이 열려 있는 동안에는 에디터가 source of truth다. 서버 응답으로
  // value가 바뀌어도 재시드하지 않고 id 변경 시에만 새로 시드한다.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const initialContent = useMemo(() => parseBody(value), [id]);
  const editor = useCreateBlockNote(
    { schema: schemaWithSubpage, initialContent, uploadFile: uploadMedia, dictionary: ko },
    [id],
  );

  const lastSaved = useRef(value);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const commitNow = () => {
    if (readOnly) return;
    // 커밋 시점에 "편집 중(디바운스)" 표시를 걷는다 — 이후 저장 상태는 큐가 안다.
    clearPageEditing(id);
    const doc = editor.document as unknown[];
    const next = isEmptyDoc(doc) ? "" : JSON.stringify(doc);
    if (next !== lastSaved.current) {
      lastSaved.current = next;
      onCommit(next);
    }
  };
  const commitRef = useRef(commitNow);
  useEffect(() => {
    commitRef.current = commitNow;
  });

  useEffect(() => {
    lastSaved.current = value;
    if (timer.current) clearTimeout(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // peek 닫힘/레벨 전환(unmount) 시 미저장분 커밋
  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
      commitRef.current();
    };
  }, [id]);

  // 창 숨김/탭 전환/닫힘 시 디바운스를 기다리지 않고 즉시 커밋한다.
  // 데스크탑 빠른 메모 창은 글로벌 단축키로 숨겨질 때 blur가 보장되지 않고,
  // WKWebView는 pagehide가 안 올 수 있어 세 이벤트를 모두 듣는다(커밋은
  // lastSaved 대조로 중복 방지되므로 다중 발화에 안전).
  useEffect(() => {
    const flush = () => {
      if (timer.current) clearTimeout(timer.current);
      commitRef.current();
    };
    const onVis = () => {
      if (document.visibilityState === "hidden") flush();
    };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("pagehide", flush);
    window.addEventListener("beforeunload", flush);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("pagehide", flush);
      window.removeEventListener("beforeunload", flush);
    };
  }, []);

  // 빠른 메모: 마운트 직후와 창이 다시 포커스될 때 바로 타이핑할 수 있게
  // 에디터 끝으로 커서를 준다. 다른 입력(제목/검색)에 포커스가 있으면 뺏지 않는다.
  useEffect(() => {
    if (!autoFocus) return;
    const focusEnd = () => {
      try {
        const doc = editor.document;
        const last = doc[doc.length - 1];
        if (last) editor.setTextCursorPosition(last, "end");
        editor.focus();
      } catch {
        // 에디터 초기화 전이면 다음 기회에.
      }
    };
    const t = setTimeout(focusEnd, 60);
    const onWindowFocus = () => {
      const ae = document.activeElement;
      if (!ae || ae === document.body) focusEnd();
    };
    window.addEventListener("focus", onWindowFocus);
    return () => {
      clearTimeout(t);
      window.removeEventListener("focus", onWindowFocus);
    };
  }, [autoFocus, editor]);

  // "/페이지" 슬래시 항목: 하위 페이지를 만들고 본문에 링크를 끼운 뒤 진입한다.
  // async 콜백이라 await 전에 커서 블록을 먼저 잡아두고(타이밍 안전) 그 자리에
  // subpage 블록을 넣는다. getItems가 메뉴를 열 때마다 호출되므로 최신 클로저 사용.
  const pageItem: DefaultReactSuggestionItem = {
    title: "페이지",
    subtext: "하위 페이지를 만듭니다",
    aliases: ["페이지", "하위 페이지", "하위페이지", "subpage", "page", "새 페이지"],
    // 그룹명은 (a) 기본 그룹명과도, (b) 이 항목 title("페이지")과도 달라야 한다.
    // 슬래시 메뉴가 그룹 헤더와 항목을 같은 리스트에서 라벨을 key로 렌더하기 때문에
    // 그룹명=title이면 키 충돌로 항목이 누락된다(클릭 무반응).
    group: "삽입",
    icon: <span>📄</span>,
    onItemClick: async () => {
      if (!onCreatePage) return;
      const ref = editor.getTextCursorPosition().block;
      let page: { page_id: string; title: string };
      try {
        page = await onCreatePage();
      } catch {
        return;
      }
      const block = {
        type: "subpage" as const,
        props: { pageId: page.page_id, title: page.title || "새 페이지" },
      };
      const hasText = Array.isArray(ref.content) && ref.content.length > 0;
      if (!hasText && ref.type === "paragraph") editor.updateBlock(ref, block);
      else editor.insertBlocks([block], ref, "after");
      onOpenPage?.(page.page_id);
    },
  };

  // "/데이터베이스" "/표" "/보드" 슬래시 항목: 새 인라인 데이터베이스를 만들고 본문에
  // database 블록을 끼운다. onCreateDatabase가 있을 때만 노출(프로젝트/페이지 상세).
  function makeDbItem(
    title: string,
    cfg: {
      view: "table" | "board";
      template: "basic" | "tasks";
      inline: boolean;
      subtext: string;
    },
    aliases: string[],
  ): DefaultReactSuggestionItem {
    return {
      title,
      subtext: cfg.subtext,
      aliases,
      group: "삽입",
      icon: <span>{cfg.view === "board" ? "▦" : "▤"}</span>,
      onItemClick: async () => {
        if (!onCreateDatabase) return;
        const ref = editor.getTextCursorPosition().block;
        let created: { database_id: string };
        try {
          created = await onCreateDatabase({ view: cfg.view, template: cfg.template });
        } catch {
          return;
        }
        const block = {
          type: "database" as const,
          props: { databaseId: created.database_id, inline: cfg.inline },
        };
        const hasText = Array.isArray(ref.content) && ref.content.length > 0;
        if (!hasText && ref.type === "paragraph") {
          const updated = editor.updateBlock(ref, block);
          editor.insertBlocks([{ type: "paragraph" }], updated, "after");
        } else {
          editor.insertBlocks([block, { type: "paragraph" }], ref, "after");
        }
      },
    };
  }

  // 기본 슬래시 항목 한국어 라벨 보정: 토글은 기본 라벨이 "접을 수 있는 …"이라
  // 용어를 "토글"로 바꾸고(검색도 "/토글"로 바로), 별칭도 더한다.
  const slashItems = (query: string) => {
    const defaults = getDefaultReactSlashMenuItems(editor).map((it) => {
      if (typeof it.title === "string" && it.title.includes("접을 수 있")) {
        const title = it.title
          .replace("접을 수 있는 목록", "토글")
          .replace("접을 수 있는 제목", "토글 제목");
        return {
          ...it,
          title,
          aliases: [
            ...(it.aliases ?? []),
            "토글",
            "토글 열기",
            "토글 열어",
            "토글 목록",
            "toggle",
            "접기",
          ],
        };
      }
      return it;
    });
    const dbItems = onCreateDatabase
      ? [
          makeDbItem(
            "데이터베이스",
            { view: "table", template: "basic", inline: true, subtext: "인라인 표" },
            ["데이터베이스", "표", "table", "database", "db", "데이터"],
          ),
          makeDbItem(
            "보드 (협업 업무표)",
            { view: "board", template: "tasks", inline: true, subtext: "노션식 칸반 보드" },
            ["보드", "칸반", "kanban", "board", "칸반보드", "협업업무표", "업무표"],
          ),
          makeDbItem(
            "데이터베이스 — 전체 페이지",
            { view: "table", template: "basic", inline: false, subtext: "페이지로 여는 표" },
            ["데이터베이스 페이지", "전체 페이지", "fullpage", "페이지 데이터베이스"],
          ),
          makeDbItem(
            "보드 — 전체 페이지",
            { view: "board", template: "tasks", inline: false, subtext: "페이지로 여는 칸반" },
            ["보드 페이지", "협업업무표 페이지", "전체 페이지 보드"],
          ),
        ]
      : [];
    return filterSuggestionItems(
      [...defaults, ...(onCreatePage ? [pageItem] : []), ...dbItems],
      query,
    );
  };

  return (
    <SubpageNavContext.Provider value={{ onOpenPage, onOpenDatabase, readOnly }}>
      <div
        style={{ minHeight }}
        onBlur={() => {
          if (timer.current) clearTimeout(timer.current);
          commitRef.current();
        }}
      >
        <BlockNoteView
          editor={editor}
          editable={!readOnly}
          theme="light"
          className="peek-rich-body"
          slashMenu={false}
          onChange={() => {
            if (readOnly) return;
            // 디바운스 창 동안 "편집 중"을 저장 상태기계에 알린다 — 이게 없으면
            // 미커밋 입력이 "저장됨"으로 보여 표시등이 거짓말하고, 빠른 메모의
            // 외부 변경 리마운트가 더티 에디터 위에서 발화할 수 있다.
            markPageEditing(id);
            if (draftMirror) {
              // 키 입력 단위 유실 방지: 서버 확인 전 본문을 즉시 미러.
              const doc = editor.document as unknown[];
              writePageDraft(id, isEmptyDoc(doc) ? "" : JSON.stringify(doc));
            }
            if (timer.current) clearTimeout(timer.current);
            timer.current = setTimeout(() => commitRef.current(), 800);
          }}
        >
          {readOnly ? null : (
            <SuggestionMenuController
              triggerCharacter="/"
              getItems={async (query) => slashItems(query)}
            />
          )}
        </BlockNoteView>
      </div>
    </SubpageNavContext.Provider>
  );
}
