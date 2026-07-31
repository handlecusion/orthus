"use client";

import { useState } from "react";
import Link from "next/link";
import {
  type AgentDelegateTarget,
  type AgentDelegateTargetList,
  type AgentDelegateTargetNode,
  type AgentFolderList,
  type AgentWorkItem,
} from "@/lib/api";
import { Card, Chip, cx, inputClass } from "@/components/ui";

/* ---------------------------- delegation UI ---------------------------- */
// Friendly delegation controls — the UI alternative to the `위임:` text grammar.
// When 위임 is on, the primary chat action delegates the typed instruction to a
// local agent (self or a teammate's enrolled collector node) through the
// deterministic policy gate.

export type AgentRunner = "codex" | "claude" | "hermes";
export type AgentMode = "code" | "knowledge";
export const AGENT_RUNNERS: AgentRunner[] = ["codex", "claude", "hermes"];
export const DELEGATE_PLACEHOLDER =
  "위임할 작업 — 로컬에서 실행됩니다 (@ 폴더를 고르면 그 폴더에서)";

/** 위임 on/off pill. On = the primary action delegates the typed instruction to a
 *  local agent through the deterministic policy gate instead of asking /ask. */
export function DelegateToggle({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  return (
    <button
      aria-pressed={on}
      className="inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-meta)] font-semibold transition-colors"
      onClick={onToggle}
      title="질문 대신 로컬 에이전트에게 작업을 위임합니다."
      type="button"
      style={{
        background: on ? "var(--color-sidebar-active)" : "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        color: on ? "var(--color-text-strong)" : "var(--color-text-body)",
      }}
    >
      <RobotIcon />
      위임
    </button>
  );
}

/** Adapt a picked node's folders into the AgentFolderList shape the FolderPicker
 *  consumes, so one folder picker serves both self and teammate nodes. */
function nodeFolderList(
  node: AgentDelegateTargetNode | null,
): AgentFolderList {
  if (!node) {
    return { supported: true, default: null, recent: [], reported_at: null };
  }
  return {
    supported: true,
    default: node.default_folder,
    recent: node.recent_folders,
    reported_at: node.last_polled_at,
  };
}

export function DelegateControls({
  targets,
  targetsError,
  selectedUserId,
  onSelectUser,
  selectedDeviceId,
  onSelectDevice,
  runner,
  onRunner,
  cwd,
  onCwd,
  // Fallback path (targets unsupported / fetch failed): raw email + self folders.
  assignee,
  onAssignee,
  fallbackFolders,
  fallbackIsSelf,
}: {
  targets: AgentDelegateTargetList | null;
  targetsError: boolean;
  selectedUserId: string | null;
  onSelectUser: (userId: string) => void;
  selectedDeviceId: string | null;
  onSelectDevice: (deviceId: string | null) => void;
  runner: AgentRunner;
  onRunner: (v: AgentRunner) => void;
  cwd: string;
  onCwd: (v: string) => void;
  assignee: string;
  onAssignee: (v: string) => void;
  fallbackFolders: AgentFolderList | null;
  fallbackIsSelf: boolean;
}) {
  // Degrade to the raw-email path when the picker source can't be used.
  const useFallback = targetsError || targets?.supported === false;

  return (
    <div
      className="mt-2 flex flex-col gap-2 rounded-[var(--radius-toolbar)] p-2.5"
      style={{ background: "var(--color-panel)", border: "1px solid var(--color-divider-soft)" }}
    >
      {useFallback ? (
        <FallbackAssignee
          assignee={assignee}
          onAssignee={onAssignee}
          runner={runner}
          onRunner={onRunner}
          cwd={cwd}
          onCwd={onCwd}
          folders={fallbackFolders}
          isSelf={fallbackIsSelf}
        />
      ) : (
        <PickerControls
          targets={targets}
          selectedUserId={selectedUserId}
          onSelectUser={onSelectUser}
          selectedDeviceId={selectedDeviceId}
          onSelectDevice={onSelectDevice}
          runner={runner}
          onRunner={onRunner}
          cwd={cwd}
          onCwd={onCwd}
        />
      )}
    </div>
  );
}

/** Primary path: user dropdown + node dropdown + runner + folder picker. */
function PickerControls({
  targets,
  selectedUserId,
  onSelectUser,
  selectedDeviceId,
  onSelectDevice,
  runner,
  onRunner,
  cwd,
  onCwd,
}: {
  targets: AgentDelegateTargetList | null;
  selectedUserId: string | null;
  onSelectUser: (userId: string) => void;
  selectedDeviceId: string | null;
  onSelectDevice: (deviceId: string | null) => void;
  runner: AgentRunner;
  onRunner: (v: AgentRunner) => void;
  cwd: string;
  onCwd: (v: string) => void;
}) {
  const list = targets?.targets ?? [];
  const selectedTarget =
    list.find((t) => t.user_id === selectedUserId) ?? list[0] ?? null;
  const nodes = selectedTarget?.nodes ?? [];
  const selectedNode =
    nodes.find((n) => n.device_id === selectedDeviceId) ??
    (selectedDeviceId === null ? nodes.find((n) => n.device_id === null) : undefined) ??
    null;
  const loading = targets === null;

  return (
    <>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        {/* assignee (담당) */}
        <UserPicker
          targets={list}
          selected={selectedTarget}
          loading={loading}
          onSelect={onSelectUser}
        />
        {/* node (실행 노드) */}
        {nodes.length > 0 ? (
          <NodePicker
            nodes={nodes}
            selected={selectedNode}
            onSelect={onSelectDevice}
          />
        ) : selectedTarget && !loading ? (
          <p className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            등록된 실행 노드가 없습니다.
          </p>
        ) : null}
        {/* runner */}
        <Segmented
          label="실행기"
          value={runner}
          options={AGENT_RUNNERS.map((r) => ({ value: r, label: r }))}
          onChange={(v) => onRunner(v as AgentRunner)}
        />
      </div>

      {nodes.length > 0 ? (
        <FolderPicker
          cwd={cwd}
          onCwd={onCwd}
          folders={nodeFolderList(selectedNode)}
          highlight={false}
        />
      ) : null}
    </>
  );
}

/** Fallback path used only when the delegate-targets source is unavailable —
 *  preserves the prior raw-email + self-folder behavior so delegation degrades
 *  gracefully instead of hard-breaking. */
function FallbackAssignee({
  assignee,
  onAssignee,
  runner,
  onRunner,
  cwd,
  onCwd,
  folders,
  isSelf,
}: {
  assignee: string;
  onAssignee: (v: string) => void;
  runner: AgentRunner;
  onRunner: (v: AgentRunner) => void;
  cwd: string;
  onCwd: (v: string) => void;
  folders: AgentFolderList | null;
  isSelf: boolean;
}) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <label className="flex min-w-0 items-center gap-1.5">
          <span className="text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
            담당
          </span>
          <input
            value={assignee}
            onChange={(e) => onAssignee(e.target.value)}
            placeholder="나 (비우면 self)"
            className={cx(inputClass, "h-9 w-[180px] max-w-full px-2 text-[var(--text-body-sm)]")}
            aria-label="담당자 이메일 (비우면 본인)"
          />
        </label>
        <Segmented
          label="실행기"
          value={runner}
          options={AGENT_RUNNERS.map((r) => ({ value: r, label: r }))}
          onChange={(v) => onRunner(v as AgentRunner)}
        />
      </div>

      {isSelf ? (
        <FolderPicker cwd={cwd} onCwd={onCwd} folders={folders} highlight={false} />
      ) : (
        <p className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          팀원에게 위임하면 실행 폴더는 팀원 데몬이 정합니다(@ 폴더 선택은 본인 위임에서만).
        </p>
      )}
    </>
  );
}

/* ---------------------------- status dot ---------------------------- */

type DotStatus = "online" | "stale" | "offline";

/** Online/offline status dot: online=green ●, stale=amber ●, offline=grey ○. */
function StatusDot({ status }: { status: DotStatus }) {
  const color =
    status === "online"
      ? "var(--color-pass-fg)"
      : status === "stale"
        ? "var(--color-warn-fg)"
        : "var(--color-text-faint)";
  const label = status === "online" ? "온라인" : status === "stale" ? "지연" : "오프라인";
  return (
    <span
      aria-label={label}
      className="inline-block h-2 w-2 shrink-0 rounded-full"
      style={{
        background: status === "offline" ? "transparent" : color,
        border: `1.5px solid ${color}`,
      }}
      title={label}
    />
  );
}

/* ---------------------------- user picker ---------------------------- */

function targetLabel(target: AgentDelegateTarget): string {
  const base = target.display_name || target.email || "이름 없음";
  return target.is_self ? `나 (본인)` : base;
}

function UserPicker({
  targets,
  selected,
  loading,
  onSelect,
}: {
  targets: AgentDelegateTarget[];
  selected: AgentDelegateTarget | null;
  loading: boolean;
  onSelect: (userId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <span className="text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
        담당
      </span>
      <div className="relative">
        <button
          aria-expanded={open}
          aria-haspopup="menu"
          className="inline-flex h-9 min-w-[180px] max-w-full items-center justify-between gap-2 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] transition-colors"
          disabled={loading || targets.length === 0}
          onClick={() => setOpen((v) => !v)}
          type="button"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-toolbar-border)",
            color: "var(--color-text-strong)",
          }}
        >
          <span className="flex min-w-0 items-center gap-1.5">
            {selected ? <StatusDot status={selected.online ? "online" : "offline"} /> : null}
            <span className="truncate font-medium">
              {loading ? "불러오는 중…" : selected ? targetLabel(selected) : "대상 없음"}
            </span>
          </span>
          <Caret />
        </button>
        {open ? (
          <DropdownShell onClose={() => setOpen(false)}>
            {targets.map((target) => {
              const active = selected?.user_id === target.user_id;
              const dot: DotStatus = target.online ? "online" : "offline";
              return (
                <button
                  key={target.user_id}
                  className="flex w-full items-center justify-between gap-2 rounded-[6px] px-2 py-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-sidebar-active)]"
                  onClick={() => {
                    onSelect(target.user_id);
                    setOpen(false);
                  }}
                  role="menuitem"
                  type="button"
                  style={{
                    background: active ? "var(--color-sidebar-active)" : "transparent",
                    color: "var(--color-text-strong)",
                  }}
                >
                  <span className="flex min-w-0 items-center gap-1.5">
                    <StatusDot status={dot} />
                    <span className="truncate font-medium">{targetLabel(target)}</span>
                  </span>
                  {target.nodes.length > 1 ? (
                    <span className="shrink-0 text-[var(--text-micro)]" style={{ color: "var(--color-text-muted)" }}>
                      노드 {target.nodes.length}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </DropdownShell>
        ) : null}
      </div>
    </div>
  );
}

/* ---------------------------- node picker ---------------------------- */

function nodeHint(node: AgentDelegateTargetNode): string {
  if (node.realtime) return "실시간";
  return relativePolled(node.last_polled_at);
}

function NodePicker({
  nodes,
  selected,
  onSelect,
}: {
  nodes: AgentDelegateTargetNode[];
  selected: AgentDelegateTargetNode | null;
  onSelect: (deviceId: string | null) => void;
}) {
  const [open, setOpen] = useState(false);

  // Exactly one node: read-only chip, no dropdown (still pins device_id upstream).
  if (nodes.length === 1) {
    const node = nodes[0];
    return (
      <div className="flex min-w-0 items-center gap-1.5">
        <span className="text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
          실행 노드
        </span>
        <span
          className="inline-flex h-9 max-w-full items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)]"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-toolbar-border)",
            color: "var(--color-text-strong)",
          }}
        >
          <StatusDot status={node.status} />
          <span className="truncate font-medium">{node.label}</span>
          <span className="shrink-0 text-[var(--text-micro)]" style={{ color: "var(--color-text-muted)" }}>
            {nodeHint(node)}
          </span>
        </span>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <span className="text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
        실행 노드
      </span>
      <div className="relative">
        <button
          aria-expanded={open}
          aria-haspopup="menu"
          className="inline-flex h-9 min-w-[160px] max-w-full items-center justify-between gap-2 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] transition-colors"
          onClick={() => setOpen((v) => !v)}
          type="button"
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-toolbar-border)",
            color: "var(--color-text-strong)",
          }}
        >
          <span className="flex min-w-0 items-center gap-1.5">
            {selected ? <StatusDot status={selected.status} /> : null}
            <span className="truncate font-medium">{selected ? selected.label : "노드 선택"}</span>
          </span>
          <Caret />
        </button>
        {open ? (
          <DropdownShell onClose={() => setOpen(false)}>
            {nodes.map((node) => {
              const active = selected?.device_id === node.device_id;
              return (
                <button
                  key={node.device_id ?? `__legacy__${node.label}`}
                  className="flex w-full items-center justify-between gap-2 rounded-[6px] px-2 py-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-sidebar-active)]"
                  onClick={() => {
                    onSelect(node.device_id);
                    setOpen(false);
                  }}
                  role="menuitem"
                  type="button"
                  style={{
                    background: active ? "var(--color-sidebar-active)" : "transparent",
                    color: "var(--color-text-strong)",
                  }}
                >
                  <span className="flex min-w-0 items-center gap-1.5">
                    <StatusDot status={node.status} />
                    <span className="truncate font-medium">{node.label}</span>
                  </span>
                  <span className="shrink-0 text-[var(--text-micro)]" style={{ color: "var(--color-text-muted)" }}>
                    {nodeHint(node)}
                  </span>
                </button>
              );
            })}
          </DropdownShell>
        ) : null}
      </div>
    </div>
  );
}

/* ---------------------------- shared dropdown ---------------------------- */

function Caret() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M6 9l6 6 6-6" />
    </svg>
  );
}

/** Shared dropdown surface + click-away scrim (matches FolderDropdown). */
function DropdownShell({
  children,
  onClose,
}: {
  children: React.ReactNode;
  onClose: () => void;
}) {
  return (
    <>
      <button aria-hidden className="fixed inset-0 z-10 cursor-default" onClick={onClose} tabIndex={-1} />
      <div
        className="absolute left-0 z-20 mt-1 max-h-[260px] w-[260px] max-w-[80vw] overflow-y-auto rounded-[var(--radius-toolbar)] p-1"
        role="menu"
        style={{
          background: "var(--color-card)",
          border: "1px solid var(--color-toolbar-border)",
          boxShadow: "var(--shadow-toolbar)",
        }}
      >
        {children}
      </div>
    </>
  );
}

/** Small segmented control matching the scope selector look. */
function Segmented({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
        {label}
      </span>
      <div
        className="inline-flex h-9 items-stretch overflow-hidden rounded-[var(--radius-toolbar)]"
        role="group"
        style={{ background: "var(--color-card)", border: "1px solid var(--color-toolbar-border)" }}
      >
        {options.map((opt, i) => (
          <span key={opt.value} className="flex items-stretch">
            {i > 0 ? (
              <span aria-hidden className="my-1 w-px" style={{ background: "var(--color-toolbar-border)" }} />
            ) : null}
            <button
              aria-pressed={value === opt.value}
              className="inline-flex items-center whitespace-nowrap px-2.5 text-[var(--text-body-sm)] transition-colors"
              onClick={() => onChange(opt.value)}
              type="button"
              style={{
                background: value === opt.value ? "var(--color-sidebar-active)" : "transparent",
                color: value === opt.value ? "var(--color-text-strong)" : "var(--color-text-body)",
                fontWeight: value === opt.value ? 600 : 500,
              }}
            >
              {opt.label}
            </button>
          </span>
        ))}
      </div>
    </div>
  );
}

/** `@` run-folder picker: a button that drops down the selected node's reported
 *  folders (default + recent), plus a manual path field. Selecting sets cwd. */
function FolderPicker({
  cwd,
  onCwd,
  folders,
  highlight,
}: {
  cwd: string;
  onCwd: (v: string) => void;
  folders: AgentFolderList | null;
  highlight: boolean;
}) {
  const [open, setOpen] = useState(false);
  const options = folderOptions(folders);
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <div className="relative">
          <button
            aria-expanded={open}
            className="inline-flex h-9 items-center gap-1.5 whitespace-nowrap rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] font-semibold transition-colors"
            onClick={() => setOpen((v) => !v)}
            type="button"
            disabled={folders?.supported === false}
            title={
              folders?.supported === false
                ? "agent_task가 꺼져 있어 폴더를 불러올 수 없습니다."
                : "데몬이 보고한 실행 폴더에서 선택"
            }
            style={{
              background: "var(--color-card)",
              border: `1px solid ${highlight ? "var(--color-warn-fg)" : "var(--color-toolbar-border)"}`,
              color: "var(--color-text-strong)",
            }}
          >
            <span className="font-[family-name:var(--font-mono)]">@</span>
            실행 폴더
          </button>
          {open ? (
            <FolderDropdown
              options={options}
              folders={folders}
              onPick={(v) => {
                onCwd(v);
                setOpen(false);
              }}
              onClose={() => setOpen(false)}
            />
          ) : null}
        </div>
        <input
          value={cwd}
          onChange={(e) => onCwd(e.target.value)}
          placeholder="/경로 또는 @ 로 선택 (~ 가능)"
          className={cx(inputClass, "h-9 w-[280px] max-w-full px-2 font-[family-name:var(--font-mono)] text-[var(--text-body-sm)]")}
          aria-label="실행 폴더 경로"
        />
        {cwd ? (
          <button
            className="inline-flex h-9 items-center px-2 text-[var(--text-meta)]"
            onClick={() => onCwd("")}
            type="button"
            style={{ color: "var(--color-text-muted)" }}
            aria-label="폴더 지우기"
          >
            지우기
          </button>
        ) : null}
      </div>
    </div>
  );
}

function FolderDropdown({
  options,
  folders,
  onPick,
  onClose,
}: {
  options: Array<{ path: string; tag: string }>;
  folders: AgentFolderList | null;
  onPick: (v: string) => void;
  onClose: () => void;
}) {
  return (
    <>
      <button aria-hidden className="fixed inset-0 z-10 cursor-default" onClick={onClose} tabIndex={-1} />
      <div
        className="absolute left-0 z-20 mt-1 max-h-[260px] w-[300px] max-w-[80vw] overflow-y-auto rounded-[var(--radius-toolbar)] p-1"
        role="menu"
        style={{
          background: "var(--color-card)",
          border: "1px solid var(--color-toolbar-border)",
          boxShadow: "var(--shadow-toolbar)",
        }}
      >
        {options.length === 0 ? (
          <p className="px-2 py-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            {folders?.supported === false
              ? "agent_task가 비활성화되어 있습니다."
              : "데몬이 보고한 폴더가 없습니다. 경로를 직접 입력하세요."}
          </p>
        ) : (
          options.map((opt) => (
            <button
              key={`${opt.tag}-${opt.path}`}
              className="flex w-full items-center justify-between gap-2 rounded-[6px] px-2 py-2 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-sidebar-active)]"
              onClick={() => onPick(opt.path)}
              role="menuitem"
              type="button"
              style={{ color: "var(--color-text-strong)" }}
            >
              <span className="truncate font-[family-name:var(--font-mono)]">{opt.path}</span>
              <span className="shrink-0 text-[var(--text-micro)]" style={{ color: "var(--color-text-muted)" }}>
                {opt.tag}
              </span>
            </button>
          ))
        )}
      </div>
    </>
  );
}

function folderOptions(folders: AgentFolderList | null): Array<{ path: string; tag: string }> {
  if (!folders) return [];
  const out: Array<{ path: string; tag: string }> = [];
  const seen = new Set<string>();
  if (folders.default) {
    out.push({ path: folders.default, tag: "기본" });
    seen.add(folders.default);
  }
  for (const p of folders.recent) {
    if (seen.has(p)) continue;
    seen.add(p);
    out.push({ path: p, tag: "최근" });
  }
  return out;
}

/** Relative "마지막 폴링" hint for an offline/stale node. */
function relativePolled(iso: string | null): string {
  if (!iso) return "기록 없음";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "기록 없음";
  const diffMin = Math.round((Date.now() - then) / 60000);
  if (diffMin < 1) return "방금";
  if (diffMin < 60) return `${diffMin}분 전`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}시간 전`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay}일 전`;
}

/** Ack card for a created delegation work item — mirrors the chat intake ack. */
export function DelegateResultCard({ item, onDismiss }: { item: AgentWorkItem; onDismiss: () => void }) {
  const tone = DELEGATE_STATE_TONE[item.state] ?? "neutral";
  return (
    <Card className="p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-1.5">
            <Chip tone={tone}>{DELEGATE_STATE_LABEL[item.state] ?? item.state}</Chip>
            <span className="truncate text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
              {item.title}
            </span>
          </div>
          <p className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            {DELEGATE_STATE_MESSAGE[item.state] ?? "에이전트 작업으로 접수되었습니다."}
            {item.policy_reason ? ` · ${item.policy_reason}` : ""}
          </p>
          <Link
            href={`/checklist?work_id=${item.work_id}`}
            className="text-[var(--text-meta)] font-semibold"
            style={{ color: "var(--color-text-strong)" }}
          >
            에이전트 작업에서 보기 →
          </Link>
        </div>
        <button
          className="inline-flex h-8 items-center px-2 text-[var(--text-meta)]"
          onClick={onDismiss}
          type="button"
          style={{ color: "var(--color-text-muted)" }}
          aria-label="닫기"
        >
          닫기
        </button>
      </div>
    </Card>
  );
}

export const DELEGATE_STATE_LABEL: Record<string, string> = {
  auto_execute: "실행 시작",
  draft_for_review: "검토 대기",
  request_more_data: "정보 필요",
  reject: "거부됨",
};
export const DELEGATE_STATE_TONE: Record<string, "pass" | "warn" | "fail" | "neutral"> = {
  auto_execute: "pass",
  draft_for_review: "neutral",
  request_more_data: "warn",
  reject: "fail",
};
export const DELEGATE_STATE_MESSAGE: Record<string, string> = {
  auto_execute: "로컬 에이전트로 전송했습니다. 완료되면 에이전트 작업에 결과가 채워집니다.",
  draft_for_review: "정책 게이트가 검토를 요구했습니다. 에이전트 작업에서 승인하세요.",
  request_more_data: "추가 정보가 필요합니다. 에이전트 작업에서 확인하세요.",
  reject: "정책 게이트가 이 작업을 거부했습니다.",
};

function RobotIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="4" y="8" width="16" height="11" rx="2" />
      <path d="M12 8V4M9 3h6" />
      <circle cx="9" cy="13" r="1" />
      <circle cx="15" cy="13" r="1" />
    </svg>
  );
}
