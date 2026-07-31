"use client";

import { useEffect, useReducer, useRef, useState } from "react";
import { api, type ProjectAssignment, type ProjectRole } from "@/lib/api";
import {
  TAG_COLORS,
  TagPill,
  anchoredStyle,
  useAnchoredPopover,
  type TagColor,
} from "@/components/dashboard/kit";

// 역할 이름 기본 색(노드에 옵션이 시드되기 전/이름만 있을 때의 색 fallback).
const ROLE_COLOR: Record<string, TagColor> = {
  PM: "blue",
  기획: "yellow",
  개발: "green",
  AX: "purple",
  디자인: "pink",
  마케팅: "orange",
  운영: "teal",
  대표: "red",
  리드: "brown",
  기타: "gray",
};
export const roleColor = (v: string): TagColor => ROLE_COLOR[v] ?? "gray";

const ROLE_PALETTE: TagColor[] = [
  "gray",
  "brown",
  "orange",
  "yellow",
  "green",
  "blue",
  "purple",
  "pink",
  "red",
  "teal",
];

// 노드 역할 옵션을 한 번만 불러와 모든 RoleSelect/AssignmentChips가 공유한다. 추가/이름변경/
// 색/삭제가 일어나면 캐시를 갱신하고 구독자 전체를 리렌더한다(역할은 노드 단위라 충분).
let rolesCache: ProjectRole[] | null = null;
let rolesInflight: Promise<ProjectRole[]> | null = null;
const rolesSubs = new Set<() => void>();
function emitRoles() {
  for (const fn of rolesSubs) fn();
}
async function loadRoles(force = false): Promise<ProjectRole[]> {
  if (rolesCache && !force) return rolesCache;
  if (!rolesInflight || force) {
    rolesInflight = api
      .listProjectRoles()
      .then((r) => {
        rolesCache = r;
        rolesInflight = null;
        emitRoles();
        return r;
      })
      .catch((e) => {
        rolesInflight = null;
        throw e;
      });
  }
  return rolesInflight;
}

function useProjectRoles() {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    rolesSubs.add(force);
    void loadRoles();
    return () => {
      rolesSubs.delete(force);
    };
  }, []);
  return {
    roles: rolesCache ?? [],
    colorOf: (name: string): TagColor =>
      (rolesCache?.find((r) => r.name === name)?.color as TagColor) ?? roleColor(name),
    create: async (name: string, color: TagColor) => {
      const created = await api.createProjectRole({ name, color });
      await loadRoles(true);
      return created.name;
    },
    rename: async (roleId: string, name: string) => {
      await api.updateProjectRole(roleId, { name });
      await loadRoles(true);
    },
    recolor: async (roleId: string, color: TagColor) => {
      await api.updateProjectRole(roleId, { color });
      await loadRoles(true);
    },
    remove: async (roleId: string) => {
      await api.deleteProjectRole(roleId);
      await loadRoles(true);
    },
  };
}

/**
 * 역할 선택 + 옵션 관리(노션 선택 속성). 옵션을 고르거나, 새로 추가하거나, 각 옵션을
 * 이름변경·색변경·삭제할 수 있다. 옵션을 바꾸면 onOptionsChanged로 상위가 배정을
 * 다시 불러와 cascade(이름변경)/clear(삭제)를 반영하게 한다.
 */
function RoleSelect({
  value,
  onChange,
  onOptionsChanged,
}: {
  value: string;
  onChange: (v: string) => void;
  onOptionsChanged?: () => void | Promise<void>;
}) {
  const { roles, colorOf, create, rename, recolor, remove } = useProjectRoles();
  const { anchorRef, popRef, rect, open, toggle, close } = useAnchoredPopover();
  const [q, setQ] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const busyRef = useRef(false);

  const trimmed = q.trim();
  const filtered = q
    ? roles.filter((r) => r.name.toLowerCase().includes(q.toLowerCase()))
    : roles;
  const showAdd = trimmed.length > 0 && !roles.some((r) => r.name === trimmed);

  async function guard<T>(fn: () => Promise<T>): Promise<T | undefined> {
    if (busyRef.current) return undefined;
    busyRef.current = true;
    try {
      return await fn();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "역할 처리 실패");
      return undefined;
    } finally {
      busyRef.current = false;
    }
  }

  function pick(v: string) {
    onChange(v);
    close();
    setQ("");
  }

  async function addNew() {
    const name = await guard(() => create(trimmed, ROLE_PALETTE[trimmed.length % ROLE_PALETTE.length]));
    if (name === undefined) return;
    await onOptionsChanged?.();
    pick(name);
  }

  async function saveRename(role: ProjectRole) {
    const name = editName.trim();
    if (!name || name === role.name) {
      setEditingId(null);
      return;
    }
    const ok = await guard(async () => {
      await rename(role.role_id, name);
      return true;
    });
    if (!ok) return;
    setEditingId(null);
    if (value === role.name) onChange(name); // 현재 배정이 그 역할이면 라벨도 갱신
    await onOptionsChanged?.();
  }

  async function changeColor(role: ProjectRole, color: TagColor) {
    const ok = await guard(async () => {
      await recolor(role.role_id, color);
      return true;
    });
    if (ok) await onOptionsChanged?.();
  }

  async function removeRole(role: ProjectRole) {
    if (!window.confirm(`'${role.name}' 역할을 삭제할까요?\n이 역할로 배정된 멤버는 역할이 비워집니다.`)) return;
    const ok = await guard(async () => {
      await remove(role.role_id);
      return true;
    });
    if (!ok) return;
    if (value === role.name) onChange("");
    setEditingId(null);
    await onOptionsChanged?.();
  }

  return (
    <div className="relative">
      <button
        ref={anchorRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setQ("");
          setEditingId(null);
          toggle();
        }}
        className="flex items-center rounded-[4px] px-1 -mx-1 text-left hover:bg-[var(--color-card)]"
        style={{ minHeight: 28 }}
      >
        {value ? (
          <TagPill label={value} color={colorOf(value)} />
        ) : (
          <span style={{ color: "var(--color-text-muted)" }}>역할</span>
        )}
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 flex flex-col overflow-hidden rounded-[10px]"
          style={{
            ...anchoredStyle(rect, 240, 320),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="p-1.5" style={{ borderBottom: "1px solid var(--color-divider-soft)" }}>
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="검색 또는 직접 입력"
              className="w-full rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] outline-none"
              style={{ background: "var(--color-app-canvas)" }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && showAdd) {
                  e.preventDefault();
                  void addNew();
                }
              }}
            />
          </div>
          <div className="flex-1 overflow-y-auto p-1">
            {value ? (
              <button
                type="button"
                onClick={() => pick("")}
                className="flex w-full items-center rounded-[6px] px-2 py-1.5 text-left text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
                style={{ color: "var(--color-text-muted)", minHeight: 32 }}
              >
                · 비우기
              </button>
            ) : null}
            {filtered.map((r) =>
              editingId === r.role_id ? (
                <div key={r.role_id} className="rounded-[6px] px-1.5 py-1.5" style={{ background: "var(--color-app-canvas)" }}>
                  <input
                    autoFocus
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        void saveRename(r);
                      } else if (e.key === "Escape") {
                        setEditingId(null);
                      }
                    }}
                    className="mb-1.5 w-full rounded-[5px] px-2 py-1 text-[var(--text-body-sm)] outline-none"
                    style={{ background: "var(--color-card)", border: "1px solid var(--color-divider)" }}
                  />
                  <div className="flex flex-wrap items-center gap-1">
                    {ROLE_PALETTE.map((c) => (
                      <button
                        key={c}
                        type="button"
                        aria-label={`색 ${c}`}
                        onClick={() => void changeColor(r, c)}
                        className="h-5 w-5 rounded-full"
                        style={{
                          background: TAG_COLORS[c].bg,
                          outline: r.color === c ? "2px solid var(--color-text-strong)" : "none",
                          outlineOffset: 1,
                        }}
                      />
                    ))}
                  </div>
                  <div className="mt-1.5 flex items-center justify-between">
                    <button
                      type="button"
                      onClick={() => void removeRole(r)}
                      className="rounded-[5px] px-2 py-1 text-[var(--text-meta)]"
                      style={{ color: "var(--color-text-danger, #c0392b)" }}
                    >
                      삭제
                    </button>
                    <div className="flex gap-1">
                      <button
                        type="button"
                        onClick={() => setEditingId(null)}
                        className="rounded-[5px] px-2 py-1 text-[var(--text-meta)]"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        취소
                      </button>
                      <button
                        type="button"
                        onClick={() => void saveRename(r)}
                        className="rounded-[5px] px-2 py-1 text-[var(--text-meta)]"
                        style={{ color: "var(--color-text-strong)", fontWeight: 600 }}
                      >
                        저장
                      </button>
                    </div>
                  </div>
                </div>
              ) : (
                <div key={r.role_id} className="group flex items-center gap-1 rounded-[6px] hover:bg-[var(--color-app-canvas)]">
                  <button
                    type="button"
                    onClick={() => pick(r.name)}
                    className="flex flex-1 items-center px-2 py-1.5 text-left"
                    style={{ minHeight: 32 }}
                  >
                    <TagPill label={r.name} color={colorOf(r.name)} />
                  </button>
                  <button
                    type="button"
                    aria-label={`${r.name} 역할 수정`}
                    onClick={() => {
                      setEditingId(r.role_id);
                      setEditName(r.name);
                    }}
                    className="mr-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-[5px] text-[var(--text-meta)] hover:bg-[var(--color-card)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    ✎
                  </button>
                </div>
              ),
            )}
            {showAdd ? (
              <button
                type="button"
                onClick={() => void addNew()}
                className="flex w-full items-center gap-1 rounded-[6px] px-2 py-1.5 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ minHeight: 32, color: "var(--color-text-muted)" }}
              >
                + &ldquo;{trimmed}&rdquo; 역할 추가
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Inline editor for member↔project↔role assignments. scope "member" fixes the
 * member and picks projects; scope "project" fixes the project and picks members.
 * Opens a fixed-position popover (escapes table overflow) to add/role/remove.
 */
export function AssignmentChips({
  scope,
  ownerId,
  options,
  assignments,
  colors,
  onChanged,
}: {
  scope: "member" | "project";
  ownerId: string;
  options: { id: string; name: string }[];
  assignments: ProjectAssignment[];
  /** 이름/ID → 멤버 색 (팀일정과 동일 매핑). 있으면 이름 앞에 색 점을 찍는다. */
  colors?: Map<string, string>;
  onChanged: () => void | Promise<void>;
}) {
  const { anchorRef, popRef, rect, open, toggle } = useAnchoredPopover();
  const { colorOf } = useProjectRoles();

  const otherId = (a: ProjectAssignment) =>
    scope === "member" ? a.project_id : a.member_id;
  const otherName = (a: ProjectAssignment) =>
    scope === "member" ? a.project_name : a.member_name;
  const usedIds = new Set(assignments.map(otherId));
  const addable = options.filter((o) => !usedIds.has(o.id));

  async function add(otherEntityId: string) {
    const body =
      scope === "member"
        ? { project_id: otherEntityId, member_id: ownerId, role: null }
        : { project_id: ownerId, member_id: otherEntityId, role: null };
    try {
      await api.createAssignment(body);
      await onChanged();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "추가 실패");
    }
  }
  async function setRole(a: ProjectAssignment, role: string) {
    try {
      await api.updateAssignmentRole(a.assignment_id, role || null);
      await onChanged();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "저장 실패");
    }
  }
  async function remove(a: ProjectAssignment) {
    try {
      await api.deleteAssignment(a.assignment_id);
      await onChanged();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "삭제 실패");
    }
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
        className="flex min-h-[28px] flex-wrap items-center gap-1 rounded-[4px] px-1 -mx-1 text-left hover:bg-[var(--color-card)]"
      >
        {assignments.length === 0 ? (
          <span
            className="inline-flex items-center gap-1 rounded-[6px] px-2 py-[3px] text-[var(--text-meta)] font-semibold"
            style={{
              border: "1px dashed var(--color-divider)",
              color: "var(--color-progress, #2563eb)",
            }}
          >
            + {scope === "project" ? "멤버 배정" : "프로젝트 배정"}
          </span>
        ) : (
          assignments.map((a) => {
            const dot = colors?.get(otherName(a) ?? "");
            return (
              <span
                key={a.assignment_id}
                className="inline-flex items-center gap-1 rounded-[4px] px-2 py-[2px] text-[var(--text-meta)]"
                style={{ background: "var(--color-app-canvas)" }}
              >
                {dot ? (
                  <span
                    className="inline-block h-2 w-2 shrink-0 rounded-full"
                    style={{ background: dot }}
                  />
                ) : null}
                <span style={{ color: "var(--color-text-strong)", fontWeight: 600 }}>
                  {otherName(a) ?? "?"}
                </span>
                {a.role ? <TagPill label={a.role} color={colorOf(a.role)} /> : null}
              </span>
            );
          })
        )}
      </button>
      {open && rect ? (
        <div
          ref={popRef}
          className="z-50 flex flex-col overflow-hidden rounded-[10px]"
          style={{
            ...anchoredStyle(rect, 280, 300),
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex-1 overflow-y-auto p-1.5">
            {assignments.length === 0 ? (
              <p
                className="px-2 py-1 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                아직 배정이 없습니다.
              </p>
            ) : (
              assignments.map((a) => (
                <div
                  key={a.assignment_id}
                  className="flex items-center gap-2 rounded-[6px] px-1.5 py-1"
                >
                  {colors?.get(otherName(a) ?? "") ? (
                    <span
                      className="inline-block h-2 w-2 shrink-0 rounded-full"
                      style={{ background: colors.get(otherName(a) ?? "") }}
                    />
                  ) : null}
                  <span
                    className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                    style={{ color: "var(--color-text-strong)" }}
                  >
                    {otherName(a) ?? "?"}
                  </span>
                  <RoleSelect
                    value={a.role ?? ""}
                    onChange={(v) => setRole(a, v)}
                    onOptionsChanged={onChanged}
                  />
                  <button
                    type="button"
                    onClick={() => remove(a)}
                    className="flex h-6 w-6 items-center justify-center rounded-[5px] text-[var(--text-meta)] hover:bg-[var(--color-app-canvas)]"
                    style={{ color: "var(--color-text-muted)" }}
                    aria-label="배정 삭제"
                  >
                    ✕
                  </button>
                </div>
              ))
            )}
          </div>
          <div
            className="p-1.5"
            style={{ borderTop: "1px solid var(--color-divider-soft)" }}
          >
            {addable.length === 0 ? (
              <p
                className="px-2 py-1 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                {scope === "member" ? "모든 프로젝트 배정됨" : "모든 팀원 배정됨"}
              </p>
            ) : (
              <select
                value=""
                onChange={(e) => {
                  if (e.target.value) add(e.target.value);
                }}
                className="w-full rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] outline-none"
                style={{
                  background: "var(--color-app-canvas)",
                  color: "var(--color-text-body)",
                }}
              >
                <option value="">
                  {scope === "member" ? "+ 프로젝트 추가…" : "+ 팀원 추가…"}
                </option>
                {addable.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
