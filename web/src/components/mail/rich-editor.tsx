"use client";

/**
 * 메일 작성용 경량 리치 텍스트 에디터.
 *
 * 의존성 없이 `contentEditable` + `document.execCommand`로 굵게/기울임/밑줄/
 * 글자크기/목록/링크 서식을 제공한다(Gmail식 작성 경험). 발송 시 부모가
 * `html`(서식 포함)과 plain text fallback을 함께 `POST /mail/send`로 보낸다 —
 * 백엔드 `MailSendRequest`는 이미 `html`을 받는다.
 *
 * execCommand는 deprecated지만 모든 현행 브라우저(Chrome/Safari/Firefox)에서
 * contentEditable 서식에 안정적으로 동작하고 외부 의존성이 0이라, 회사 내부
 * 메일 작성 박스에는 실용적 선택이다. 표준 리치 에디터(BlockNote)는 본문 작성용
 * 으로는 과하다.
 */

import { useEffect, useImperativeHandle, useRef, forwardRef, useCallback, useState } from "react";
import { Bold, Italic, Underline, List, ListOrdered, Link2, Eraser } from "lucide-react";

export type MailRichEditorHandle = {
  /** 현재 커서 위치에 HTML 조각을 삽입(메일 명함 삽입 등). */
  insertHtml: (html: string) => void;
  /** 에디터에 포커스를 준다. */
  focus: () => void;
};

type Props = {
  html: string;
  onChange: (html: string) => void;
  placeholder?: string;
  className?: string;
  /** 본문 영역 최소 높이(px). */
  minHeight?: number;
};

const FONT_SIZES: { label: string; value: string }[] = [
  { label: "작게", value: "2" },
  { label: "보통", value: "3" },
  { label: "크게", value: "5" },
  { label: "아주 크게", value: "6" },
];

type ActiveState = {
  bold: boolean;
  italic: boolean;
  underline: boolean;
  unorderedList: boolean;
  orderedList: boolean;
};

const EMPTY_ACTIVE_STATE: ActiveState = {
  bold: false,
  italic: false,
  underline: false,
  unorderedList: false,
  orderedList: false,
};

export const MailRichEditor = forwardRef<MailRichEditorHandle, Props>(function MailRichEditor(
  { html, onChange, placeholder = "내용을 입력하세요", className, minHeight = 220 },
  ref,
) {
  const editorRef = useRef<HTMLDivElement | null>(null);
  const [active, setActive] = useState<ActiveState>(EMPTY_ACTIVE_STATE);

  const refreshActiveState = useCallback(() => {
    const el = editorRef.current;
    const selection = document.getSelection();
    const anchor = selection?.anchorNode ?? null;
    const selectionInside = Boolean(el && anchor && el.contains(anchor));
    const focusedInside = Boolean(el && document.activeElement === el);
    if (!selectionInside && !focusedInside) {
      setActive(EMPTY_ACTIVE_STATE);
      return;
    }
    setActive({
      bold: queryCommandState("bold"),
      italic: queryCommandState("italic"),
      underline: queryCommandState("underline"),
      unorderedList: queryCommandState("insertUnorderedList"),
      orderedList: queryCommandState("insertOrderedList"),
    });
  }, []);

  const sync = useCallback(() => {
    const el = editorRef.current;
    if (el) onChange(el.innerHTML);
  }, [onChange]);

  // 외부에서 값이 바뀐 경우(발송 후 리셋, 명함 삽입 등)만 DOM에 반영한다.
  // 타이핑 중(에디터가 포커스 상태) 덮어쓰면 커서가 튀므로 그때는 건너뛴다.
  useEffect(() => {
    const el = editorRef.current;
    if (!el) return;
    if (document.activeElement === el) return;
    if (el.innerHTML !== html) el.innerHTML = html;
  }, [html]);

  const exec = useCallback(
    (command: string, value?: string) => {
      editorRef.current?.focus();
      document.execCommand(command, false, value);
      sync();
      window.requestAnimationFrame(refreshActiveState);
    },
    [refreshActiveState, sync],
  );

  const insertHtml = useCallback(
    (snippet: string) => {
      editorRef.current?.focus();
      document.execCommand("insertHTML", false, snippet);
      sync();
      window.requestAnimationFrame(refreshActiveState);
    },
    [refreshActiveState, sync],
  );

  useEffect(() => {
    document.addEventListener("selectionchange", refreshActiveState);
    return () => document.removeEventListener("selectionchange", refreshActiveState);
  }, [refreshActiveState]);

  useImperativeHandle(
    ref,
    () => ({
      insertHtml,
      focus: () => editorRef.current?.focus(),
    }),
    [insertHtml],
  );

  const addLink = useCallback(() => {
    const url = window.prompt("링크 주소(URL)를 입력하세요", "https://");
    if (!url) return;
    const trimmed = url.trim();
    if (!/^https?:\/\//i.test(trimmed)) {
      window.alert("http:// 또는 https:// 로 시작하는 주소만 추가할 수 있어요.");
      return;
    }
    exec("createLink", trimmed);
  }, [exec]);

  return (
    <div
      // flex-col + 본문 flex-1: 부모가 준 높이(flex-1 등)를 본문 영역이 채운다.
      // minHeight는 루트에 둔다 — overflow:hidden 요소는 flex 최소 크기가 0이라
      // 내부(contentEditable)에만 min-height를 주면 좁은 화면에서 눌려 사라진다.
      className={["flex flex-col", className].filter(Boolean).join(" ")}
      style={{
        border: "1px solid var(--color-divider)",
        borderRadius: "8px",
        overflow: "hidden",
        background: "var(--color-surface, #fff)",
        minHeight: `${minHeight}px`,
      }}
    >
      <div
        className="flex shrink-0 flex-wrap items-center gap-1 px-2 py-1"
        style={{ borderBottom: "1px solid var(--color-divider)" }}
      >
        <ToolBtn active={active.bold} label="굵게" onClick={() => exec("bold")}>
          <Bold size={15} />
        </ToolBtn>
        <ToolBtn active={active.italic} label="기울임" onClick={() => exec("italic")}>
          <Italic size={15} />
        </ToolBtn>
        <ToolBtn active={active.underline} label="밑줄" onClick={() => exec("underline")}>
          <Underline size={15} />
        </ToolBtn>
        <Divider />
        <select
          aria-label="글자 크기"
          className="min-h-[32px] rounded-[6px] px-1 text-[var(--text-meta)]"
          defaultValue="3"
          onChange={(e) => exec("fontSize", e.target.value)}
          style={{
            border: "1px solid var(--color-divider)",
            background: "transparent",
            color: "var(--color-text-strong)",
          }}
        >
          {FONT_SIZES.map((size) => (
            <option key={size.value} value={size.value}>
              {size.label}
            </option>
          ))}
        </select>
        <Divider />
        <ToolBtn active={active.unorderedList} label="글머리 기호" onClick={() => exec("insertUnorderedList")}>
          <List size={15} />
        </ToolBtn>
        <ToolBtn active={active.orderedList} label="번호 매기기" onClick={() => exec("insertOrderedList")}>
          <ListOrdered size={15} />
        </ToolBtn>
        <ToolBtn label="링크" onClick={addLink}>
          <Link2 size={15} />
        </ToolBtn>
        <Divider />
        <ToolBtn label="서식 지우기" onClick={() => exec("removeFormat")}>
          <Eraser size={15} />
        </ToolBtn>
      </div>
      <div
        ref={editorRef}
        contentEditable
        suppressContentEditableWarning
        role="textbox"
        aria-multiline="true"
        aria-label="메일 내용"
        data-placeholder={placeholder}
        className="mail-rich-body flex-1 overflow-y-auto px-3 py-2 text-[var(--text-body-sm)] outline-none"
        onInput={sync}
        onBlur={sync}
        onFocus={refreshActiveState}
        onKeyUp={refreshActiveState}
        onMouseUp={refreshActiveState}
        style={{
          color: "var(--color-text-strong)",
          lineHeight: 1.6,
        }}
      />
      <style jsx global>{`
        .mail-rich-body:empty::before {
          content: attr(data-placeholder);
          color: var(--color-text-muted);
          pointer-events: none;
        }
        .mail-rich-body a {
          color: var(--color-progress, #2563eb);
          text-decoration: underline;
        }
        .mail-rich-body ul {
          list-style: disc;
          padding-left: 1.4em;
        }
        .mail-rich-body ol {
          list-style: decimal;
          padding-left: 1.4em;
        }
      `}</style>
    </div>
  );
});

function ToolBtn({
  active = false,
  label,
  onClick,
  children,
}: {
  active?: boolean;
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      title={label}
      // mousedown preventDefault: 버튼 클릭으로 에디터 선택(커서)이 사라지지 않게.
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      className="flex min-h-[32px] min-w-[32px] items-center justify-center rounded-[6px] transition-colors hover:bg-[var(--color-hover,rgba(0,0,0,0.05))]"
      style={{
        background: active ? "var(--color-sidebar-active)" : "transparent",
        border: active ? "1px solid var(--color-divider)" : "1px solid transparent",
        color: active ? "var(--color-text-strong)" : "var(--color-text-muted)",
        fontWeight: active ? 700 : 500,
      }}
    >
      {children}
    </button>
  );
}

function Divider() {
  return <span className="mx-0.5 h-4 w-px shrink-0" style={{ background: "var(--color-divider)" }} />;
}

function queryCommandState(command: string): boolean {
  try {
    return document.queryCommandState(command);
  } catch {
    return false;
  }
}

/**
 * 발송 시 plain-text fallback(`MailSendRequest.text`)을 만든다.
 * 블록 요소/`<br>`은 줄바꿈으로 보존해 단일 줄로 뭉개지지 않게 한다.
 */
export function htmlToPlainText(html: string): string {
  if (!html) return "";
  if (typeof document === "undefined") {
    return html
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/(p|div|li|tr|h[1-6])>/gi, "\n")
      .replace(/<[^>]+>/g, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }
  const div = document.createElement("div");
  div.innerHTML = html;
  div.querySelectorAll("br").forEach((br) => br.replaceWith("\n"));
  div
    .querySelectorAll("p,div,li,tr,h1,h2,h3,h4,h5,h6")
    .forEach((el) => el.append("\n"));
  return (div.textContent ?? "").replace(/\n{3,}/g, "\n\n").trim();
}
