"use client";

/* Notion-style side peek shell + page-like editing primitives shared by
 * dashboard sections (지원사업 보드, 프로젝트 상세 등). */

import React from "react";
import { useCommitFlush } from "@/lib/use-commit-flush";

/** Right-side sliding panel. Children render inside a scrollable page body. */
export function SidePeek({
  label,
  toolbar,
  onClose,
  children,
  width = 560,
}: {
  label: string;
  toolbar?: React.ReactNode;
  onClose: () => void;
  children: React.ReactNode;
  width?: number;
}) {
  const [entered, setEntered] = React.useState(false);

  React.useEffect(() => {
    const t = requestAnimationFrame(() => setEntered(true));
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(t);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div
        className="flex-1 transition-opacity"
        style={{ background: "rgba(0,0,0,0.18)", opacity: entered ? 1 : 0 }}
        onClick={onClose}
      />
      <div
        className="flex h-full w-full flex-col transition-transform duration-200 ease-out"
        style={{
          maxWidth: width,
          background: "var(--color-modal)",
          boxShadow: "-8px 0 40px rgba(0,0,0,0.16)",
          // transform creates a containing block for position:fixed children
          // (TagSelect/DatePicker popovers), so it must be cleared once entered.
          transform: entered ? undefined : "translateX(24px)",
          opacity: entered ? 1 : 0,
        }}
        role="dialog"
        aria-label={label}
      >
        <div
          className="flex items-center justify-between px-3 py-2"
          style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
        >
          <button
            type="button"
            onClick={onClose}
            aria-label="닫기"
            className="flex h-9 w-9 items-center justify-center rounded-[6px] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            ✕
          </button>
          <div className="flex items-center gap-1">{toolbar}</div>
        </div>
        <div className="flex-1 overflow-y-auto px-6 pb-16 pt-6 sm:px-10">
          {children}
        </div>
      </div>
    </div>
  );
}

/** Notion-style property row: muted label column + value. */
export function PropRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-[34px] items-center gap-2 py-[3px]">
      <div
        className="w-[96px] shrink-0 text-[var(--text-body-sm)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        {label}
      </div>
      <div className="flex min-w-0 flex-1 items-center">{children}</div>
    </div>
  );
}

/** Notion-ish borderless inline text input that commits on blur/Enter. */
export function PropTextInput({
  value,
  placeholder,
  onCommit,
}: {
  value: string;
  placeholder?: string;
  onCommit: (v: string) => void;
}) {
  const [draft, setDraft] = React.useState(value);
  const [prev, setPrev] = React.useState(value);
  if (prev !== value) {
    setPrev(value);
    setDraft(value);
  }
  // Esc·백드롭·라우트 이동으로 픽이 언마운트되면 focused input의 onBlur가
  // 발화하지 않아 방금 쓴 값이 유실됐다. 모든 종료 경로에서 커밋한다
  // (draft===value면 no-op이라 중복 안전).
  useCommitFlush(() => {
    if (draft.trim() !== value) onCommit(draft.trim());
  });
  return (
    <input
      value={draft}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        if (draft.trim() !== value) onCommit(draft.trim());
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
      className="w-full rounded-[4px] px-1 py-1 text-[var(--text-body-sm)] outline-none hover:bg-[var(--color-app-canvas)] focus:bg-[var(--color-app-canvas)]"
      style={{ color: "var(--color-text-strong)", background: "transparent" }}
    />
  );
}

/**
 * Notion-style URL property cell. With a value, the URL text is a real link
 * (click → new tab) and clicking anywhere else in the cell switches to an
 * inline edit input. Empty values render the input directly.
 */
export function PropUrlInput({
  value,
  placeholder = "비어 있음",
  onCommit,
}: {
  value: string;
  placeholder?: string;
  onCommit: (v: string) => void;
}) {
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(value);
  const [prev, setPrev] = React.useState(value);
  if (prev !== value) {
    setPrev(value);
    setDraft(value);
  }
  // 편집 중 픽이 언마운트/창 숨김되면 onBlur 미발화로 유실 → 종료 경로에서도 커밋.
  useCommitFlush(() => {
    if (draft.trim() !== value) onCommit(draft.trim());
  });

  const href = /^https?:\/\//i.test(value) ? value : value ? `https://${value}` : "";

  if (!value || editing) {
    return (
      <input
        autoFocus={editing}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          setEditing(false);
          if (draft.trim() !== value) onCommit(draft.trim());
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          if (e.key === "Escape") {
            setDraft(value);
            setEditing(false);
          }
        }}
        className="w-full rounded-[4px] px-1 py-1 text-[var(--text-body-sm)] outline-none hover:bg-[var(--color-app-canvas)] focus:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-text-strong)", background: "transparent" }}
      />
    );
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => setEditing(true)}
      onKeyDown={(e) => {
        if (e.key === "Enter") setEditing(true);
      }}
      className="group/url flex w-full min-w-0 cursor-text items-center gap-1.5 rounded-[4px] px-1 py-1 hover:bg-[var(--color-app-canvas)]"
      title="클릭해서 수정"
    >
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        onClick={(e) => e.stopPropagation()}
        className="min-w-0 truncate text-[var(--text-body-sm)] underline decoration-[rgba(0,0,0,0.18)] underline-offset-2 hover:decoration-current"
        style={{ color: "var(--color-channel-blue, #5d9bd8)" }}
        title={`${value} 열기`}
      >
        {value}
      </a>
      <span
        className="shrink-0 text-[var(--text-meta)] opacity-0 transition-opacity group-hover/url:opacity-100"
        style={{ color: "var(--color-text-faint)" }}
        aria-hidden
      >
        ✎ 수정
      </span>
    </div>
  );
}

/** Notion page title: borderless, auto-grow, commits on blur. */
export function AutoGrowTitle({
  value,
  onCommit,
  readOnly = false,
}: {
  value: string;
  onCommit: (v: string) => void;
  readOnly?: boolean;
}) {
  const [draft, setDraft] = React.useState(value);
  const [prev, setPrev] = React.useState(value);
  const ref = React.useRef<HTMLTextAreaElement>(null);
  if (prev !== value) {
    setPrev(value);
    setDraft(value);
  }
  React.useEffect(() => {
    const el = ref.current;
    if (el) {
      el.style.height = "0px";
      el.style.height = `${el.scrollHeight}px`;
    }
  }, [draft]);
  // 제목은 blur에서만 커밋됐는데, 데스크탑 빠른 메모 창을 단축키로 숨기면
  // blur가 오지 않아 입력한 제목이 유실됐다. 창 숨김/unmount에서도 커밋한다
  // (draft===value면 no-op이라 중복 안전).
  const commitRef = React.useRef<() => void>(() => {});
  React.useEffect(() => {
    commitRef.current = () => {
      if (!readOnly && draft.trim() !== value) onCommit(draft.trim());
    };
  });
  React.useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === "hidden") commitRef.current();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      commitRef.current();
    };
  }, []);
  return (
    <textarea
      ref={ref}
      value={draft}
      rows={1}
      placeholder="제목 없음"
      readOnly={readOnly}
      aria-readonly={readOnly}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        if (!readOnly && draft.trim() !== value) onCommit(draft.trim());
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          (e.target as HTMLTextAreaElement).blur();
        }
      }}
      className="w-full resize-none overflow-hidden bg-transparent outline-none"
      style={{
        color: "var(--color-text-strong)",
        fontSize: 28,
        lineHeight: 1.25,
        fontWeight: 700,
        letterSpacing: "-0.01em",
      }}
    />
  );
}

/** Notion page body: borderless textarea with comfortable reading rhythm. */
export function BodyEditor({
  value,
  placeholder,
  minHeight = 220,
  onCommit,
}: {
  value: string;
  placeholder?: string;
  minHeight?: number;
  onCommit: (v: string) => void;
}) {
  const [draft, setDraft] = React.useState(value);
  const [prev, setPrev] = React.useState(value);
  const ref = React.useRef<HTMLTextAreaElement>(null);
  if (prev !== value) {
    setPrev(value);
    setDraft(value);
  }
  React.useEffect(() => {
    const el = ref.current;
    if (el) {
      el.style.height = "0px";
      el.style.height = `${Math.max(el.scrollHeight, minHeight)}px`;
    }
  }, [draft, minHeight]);
  // 본문도 onBlur에서만 커밋됐다 — 언마운트/창 숨김 등 종료 경로에서도 커밋.
  useCommitFlush(() => {
    if (draft !== value) onCommit(draft);
  });
  return (
    <textarea
      ref={ref}
      value={draft}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        if (draft !== value) onCommit(draft);
      }}
      className="w-full resize-none bg-transparent outline-none"
      style={{
        color: "var(--color-text-body)",
        fontSize: 15,
        lineHeight: 1.7,
        minHeight,
      }}
    />
  );
}

/** Divider between the property table and the page body. */
export function PeekDivider() {
  return (
    <div
      className="my-4"
      style={{ borderTop: "1px solid var(--color-divider-soft)" }}
    />
  );
}

/** Section heading inside a peek page (related-data lists). */
export function PeekSection({
  title,
  count,
  action,
  children,
}: {
  title: string;
  count?: number;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-5">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div className="flex items-baseline gap-1.5">
          <h3
            className="text-[var(--text-body-sm)] font-bold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {title}
          </h3>
          {typeof count === "number" ? (
            <span
              className="text-[var(--text-meta)]"
              style={{ color: "var(--color-text-faint)" }}
            >
              {count}
            </span>
          ) : null}
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}
