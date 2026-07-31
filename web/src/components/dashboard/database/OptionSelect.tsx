"use client";

/**
 * 노션식 선택 속성 에디터(select/status/multi_select 공용).
 * - 선택 + 옵션 검색, 새 옵션 즉시 생성("만들기")
 * - 옵션 관리: 이름 변경 · 색 변경 · 삭제 (칸반 컬럼 = 옵션 관리)
 */

import { useState } from "react";
import type { DbPropertyDef } from "@/lib/api";
import {
  TAG_COLORS,
  TagPill,
  anchoredStyle,
  useAnchoredPopover,
  type TagColor,
} from "@/components/dashboard/kit";
import { OPTION_PALETTE } from "./helpers";

export type OptionSelectHandlers = {
  addOption: (name: string, color?: TagColor) => string;
  renameOption: (optionId: string, name: string) => void;
  recolorOption: (optionId: string, color: TagColor) => void;
  moveOption: (optionId: string, targetOptionId: string, placement: "before" | "after") => void;
  deleteOption: (optionId: string) => void;
};

export function OptionSelect({
  prop,
  value,
  multi,
  onChange,
  handlers,
  placeholder = "비어 있음",
  fill = false,
}: {
  prop: DbPropertyDef;
  /** 단일=optionId|null, 다중=optionId[] */
  value: string | string[] | null;
  multi: boolean;
  onChange: (next: string | string[] | null) => void;
  handlers: OptionSelectHandlers;
  placeholder?: string;
  /** 트리거 버튼이 셀 너비를 꽉 채울지 */
  fill?: boolean;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<string | null>(null);

  const selectedIds = multi
    ? Array.isArray(value)
      ? value
      : []
    : value
      ? [value as string]
      : [];
  const selectedOpts = selectedIds
    .map((id) => prop.options.find((o) => o.id === id))
    .filter((o): o is NonNullable<typeof o> => !!o);

  const trimmed = q.trim();
  const filtered = trimmed
    ? prop.options.filter((o) => o.name.toLowerCase().includes(trimmed.toLowerCase()))
    : prop.options;
  const showCreate =
    trimmed.length > 0 && !prop.options.some((o) => o.name === trimmed);

  function pick(optionId: string) {
    if (multi) {
      const set = new Set(selectedIds);
      if (set.has(optionId)) set.delete(optionId);
      else set.add(optionId);
      onChange([...set]);
    } else {
      onChange(optionId);
      close();
    }
    setQ("");
  }

  function create() {
    const used = prop.options.map((o) => o.color);
    const palette = OPTION_PALETTE.find((c) => !used.includes(c)) ?? "gray";
    const id = handlers.addOption(trimmed, palette);
    pick(id);
  }

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setEditing(null);
          toggle();
        }}
        className="flex min-h-[28px] items-center gap-1 rounded-[6px] px-1.5 py-0.5 text-left hover:bg-[var(--color-app-canvas)]"
        style={{ width: fill ? "100%" : undefined, minWidth: 0 }}
      >
        {selectedOpts.length > 0 ? (
          <span className="flex flex-wrap items-center gap-1">
            {selectedOpts.map((o) => (
              <TagPill key={o.id} label={o.name} color={o.color as TagColor} />
            ))}
          </span>
        ) : (
          <span
            className="truncate text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-faint)" }}
          >
            {placeholder}
          </span>
        )}
      </button>

      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 overflow-y-auto rounded-[10px] p-1.5"
          style={{
            ...anchoredStyle(rect, 248, 320),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {editing ? (
            <OptionEditor
              option={prop.options.find((o) => o.id === editing)!}
              handlers={handlers}
              onBack={() => setEditing(null)}
              onDeleted={() => {
                setEditing(null);
                if (!multi && value === editing) onChange(null);
              }}
            />
          ) : (
            <>
              <input
                autoFocus
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && showCreate) {
                    e.preventDefault();
                    create();
                  }
                }}
                placeholder="검색 또는 생성"
                className="mb-1 w-full rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] outline-none"
                style={{
                  background: "var(--color-app-canvas)",
                  color: "var(--color-text-strong)",
                }}
              />
              <div className="flex flex-col">
                {filtered.map((o) => {
                  const on = selectedIds.includes(o.id);
                  return (
                    <div
                      key={o.id}
                      className="group flex items-center gap-1 rounded-[6px] px-1 py-0.5 hover:bg-[var(--color-app-canvas)]"
                    >
                      <button
                        type="button"
                        onClick={() => pick(o.id)}
                        className="flex min-h-[28px] flex-1 items-center gap-1.5"
                      >
                        <span
                          className="w-3 text-center text-[var(--text-meta)]"
                          style={{ color: "var(--color-text-faint)" }}
                        >
                          {on ? "✓" : ""}
                        </span>
                        <TagPill label={o.name} color={o.color as TagColor} />
                      </button>
                      <button
                        type="button"
                        onClick={() => setEditing(o.id)}
                        className="flex h-6 w-6 items-center justify-center rounded-[4px] text-[var(--text-meta)] opacity-0 group-hover:opacity-100 hover:bg-[var(--color-divider-soft)]"
                        style={{ color: "var(--color-text-muted)" }}
                        title="옵션 편집"
                      >
                        ⋯
                      </button>
                    </div>
                  );
                })}
                {showCreate ? (
                  <button
                    type="button"
                    onClick={create}
                    className="flex min-h-[28px] items-center gap-1.5 rounded-[6px] px-1.5 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    만들기
                    <TagPill label={trimmed} color="gray" />
                  </button>
                ) : null}
                {filtered.length === 0 && !showCreate ? (
                  <p
                    className="px-1.5 py-1 text-[var(--text-meta)]"
                    style={{ color: "var(--color-text-faint)" }}
                  >
                    옵션이 없습니다.
                  </p>
                ) : null}
              </div>
            </>
          )}
        </div>
      ) : null}
    </>
  );
}

function OptionEditor({
  option,
  handlers,
  onBack,
  onDeleted,
}: {
  option: { id: string; name: string; color: string };
  handlers: OptionSelectHandlers;
  onBack: () => void;
  onDeleted: () => void;
}) {
  const [name, setName] = useState(option.name);
  return (
    <div className="flex flex-col gap-2 p-1">
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={onBack}
          className="flex h-7 w-7 items-center justify-center rounded-[6px] hover:bg-[var(--color-app-canvas)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          ‹
        </button>
        <input
          value={name}
          autoFocus
          onChange={(e) => setName(e.target.value)}
          onBlur={() => name.trim() && name !== option.name && handlers.renameOption(option.id, name.trim())}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              if (name.trim()) handlers.renameOption(option.id, name.trim());
              onBack();
            }
          }}
          className="flex-1 rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] outline-none"
          style={{ background: "var(--color-app-canvas)", color: "var(--color-text-strong)" }}
        />
      </div>
      <div className="px-1">
        <p className="mb-1 text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
          색상
        </p>
        <div className="grid grid-cols-5 gap-1">
          {OPTION_PALETTE.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => handlers.recolorOption(option.id, c)}
              className="flex h-7 items-center justify-center rounded-[5px]"
              style={{
                background: TAG_COLORS[c].bg,
                outline: option.color === c ? `2px solid ${TAG_COLORS[c].fg}` : "none",
              }}
              title={c}
            >
              {option.color === c ? (
                <span style={{ color: TAG_COLORS[c].fg }}>✓</span>
              ) : null}
            </button>
          ))}
        </div>
      </div>
      <button
        type="button"
        onClick={() => {
          handlers.deleteOption(option.id);
          onDeleted();
        }}
        className="flex min-h-[32px] items-center rounded-[6px] px-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
        style={{ color: "var(--color-fail-fg)" }}
      >
        삭제
      </button>
    </div>
  );
}
