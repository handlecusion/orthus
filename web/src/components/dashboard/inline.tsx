"use client";

import { useState } from "react";
import {
  anchoredStyle,
  useAnchoredPopover,
} from "@/components/dashboard/kit";

/* --------------------------- inline text cell --------------------------- */
export function InlineText({
  value,
  placeholder = "—",
  onSave,
  strong = false,
}: {
  value: string;
  placeholder?: string;
  onSave: (v: string) => void;
  strong?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  function commit() {
    setEditing(false);
    if (draft.trim() !== value.trim()) onSave(draft.trim());
  }

  if (editing) {
    return (
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") {
            setDraft(value);
            setEditing(false);
          }
        }}
        onClick={(e) => e.stopPropagation()}
        className="w-full min-w-[120px] rounded-[4px] px-1.5 py-1 text-[var(--text-body-sm)] outline-none"
        style={{
          border: "1px solid var(--color-text-strong)",
          background: "var(--color-card)",
          color: "var(--color-text-strong)",
        }}
      />
    );
  }
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        setDraft(value);
        setEditing(true);
      }}
      className="block w-full rounded-[4px] px-1 -mx-1 py-0.5 text-left hover:bg-[var(--color-card)]"
      style={{
        minHeight: 26,
        color: value ? "var(--color-text-strong)" : "var(--color-text-muted)",
        fontWeight: strong && value ? 600 : 400,
      }}
    >
      {value || placeholder}
    </button>
  );
}

/* ----------------------- inline single-entity select ----------------------- */
export type Entity = { id: string; name: string; color?: string | null };

export function InlineSelect({
  value,
  options,
  onChange,
  placeholder = "—",
  emptyLabel = "— 없음 —",
}: {
  value: string | null;
  options: Entity[];
  onChange: (id: string | null) => void;
  placeholder?: string;
  emptyLabel?: string;
}) {
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const current = options.find((o) => o.id === value) ?? null;

  return (
    <div className="relative">
      <button
        ref={anchorRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
        className="flex min-h-[28px] items-center gap-1.5 rounded-[4px] px-1 -mx-1 text-left hover:bg-[var(--color-card)]"
      >
        {current ? (
          <>
            {current.color ? (
              <span
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ background: current.color }}
              />
            ) : null}
            <span style={{ color: "var(--color-text-body)" }}>{current.name}</span>
          </>
        ) : (
          <span style={{ color: "var(--color-text-muted)" }}>{placeholder}</span>
        )}
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 overflow-y-auto rounded-[10px] p-1"
          style={{
            ...anchoredStyle(rect, 200, 280),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            type="button"
            onClick={() => {
              onChange(null);
              close();
            }}
            className="flex w-full items-center rounded-[6px] px-2 py-1.5 text-left text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
            style={{ color: "var(--color-text-muted)", minHeight: 32 }}
          >
            {emptyLabel}
          </button>
          {options.map((o) => (
            <button
              key={o.id}
              type="button"
              onClick={() => {
                onChange(o.id);
                close();
              }}
              className="flex w-full items-center gap-2 rounded-[6px] px-2 py-1.5 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
              style={{ minHeight: 32 }}
            >
              {o.color ? (
                <span
                  className="inline-block h-2.5 w-2.5 rounded-full"
                  style={{ background: o.color }}
                />
              ) : null}
              {o.name}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/* ----------------------- inline multi-member select ----------------------- */
export function InlineMembers({
  members,
  selected,
  onChange,
}: {
  members: { id: string; name: string; color?: string | null }[];
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const { anchorRef, popRef, rect, open, toggle } = useAnchoredPopover();
  const names = selected
    .map((id) => members.find((m) => m.id === id)?.name)
    .filter(Boolean);

  function toggleId(id: string) {
    onChange(
      selected.includes(id)
        ? selected.filter((x) => x !== id)
        : [...selected, id],
    );
  }

  return (
    <div className="relative">
      <button
        ref={anchorRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
        className="block min-h-[28px] w-full rounded-[4px] px-1 -mx-1 py-0.5 text-left hover:bg-[var(--color-card)]"
        style={{
          color: names.length ? "var(--color-text-body)" : "var(--color-text-muted)",
        }}
      >
        {names.length ? names.join(", ") : "—"}
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 overflow-y-auto rounded-[10px] p-1"
          style={{
            ...anchoredStyle(rect, 200, 260),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {members.length === 0 ? (
            <p
              className="px-2 py-1 text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              팀원이 없습니다.
            </p>
          ) : (
            members.map((m) => {
              const on = selected.includes(m.id);
              return (
                <button
                  key={m.id}
                  type="button"
                  onClick={() => toggleId(m.id)}
                  className="flex w-full items-center gap-2 rounded-[6px] px-2 py-1.5 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                  style={{ minHeight: 32 }}
                >
                  <span
                    className="flex h-4 w-4 items-center justify-center rounded-[3px] text-[10px]"
                    style={{
                      border: "1px solid var(--color-divider)",
                      background: on ? "var(--color-text-strong)" : "transparent",
                      color: "var(--color-card)",
                    }}
                  >
                    {on ? "✓" : ""}
                  </span>
                  {m.color ? (
                    <span
                      className="inline-block h-2.5 w-2.5 rounded-full"
                      style={{ background: m.color }}
                    />
                  ) : null}
                  {m.name}
                </button>
              );
            })
          )}
        </div>
      ) : null}
    </div>
  );
}
