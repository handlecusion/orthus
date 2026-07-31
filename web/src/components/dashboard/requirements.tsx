"use client";

/**
 * 프로젝트 SE(시스템 엔지니어링) 관리 UI — docs/project-se-management.md.
 *
 * SE-Seminar의 절차를 프로젝트 페이지에 얹는다:
 * - SeStageStepper: SRR→SDR→PDR→CDR→V&V→운영 단계 표시/전환 (V-모델 좌변).
 * - RequirementsSection: 번호 붙은 요구조건 대장(제약조건/목표), 상태로 검증
 *   추적, 하위 프로젝트는 상위 요구조건 flow-down(복사+원본 링크).
 */

import { useCallback, useEffect, useState } from "react";
import {
  api,
  type ProjectRequirement,
  type ProjectRequirementInput,
} from "@/lib/api";
import { TagSelect, labelStyle, type TagColor } from "@/components/dashboard/kit";
import { PeekSection } from "@/components/dashboard/peek";
import { ProgressBar } from "@/components/dashboard/progress";

/* ---------------- SE 단계 스테퍼 ---------------- */

export const SE_STAGES: { key: string; label: string; hint: string }[] = [
  { key: "srr", label: "SRR", hint: "요구조건 정리" },
  { key: "sdr", label: "SDR", hint: "시스템 설계" },
  { key: "pdr", label: "PDR", hint: "예비 설계" },
  { key: "cdr", label: "CDR", hint: "상세 설계" },
  { key: "vnv", label: "V&V", hint: "검증" },
  { key: "ops", label: "운영", hint: "운영/유지" },
];

export function SeStageStepper({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (stage: string | null) => void;
}) {
  const activeIdx = SE_STAGES.findIndex((s) => s.key === value);
  return (
    <span className="flex flex-wrap items-center gap-1">
      {SE_STAGES.map((s, i) => {
        const active = s.key === value;
        const passed = activeIdx >= 0 && i < activeIdx;
        return (
          <button
            key={s.key}
            type="button"
            title={`${s.label} — ${s.hint}${active ? " (클릭하면 해제)" : ""}`}
            onClick={() => onChange(active ? null : s.key)}
            className="flex h-7 items-center gap-1 rounded-full px-2.5 text-[var(--text-meta)]"
            style={{
              background: active
                ? "var(--color-text-strong)"
                : passed
                  ? "var(--color-app-canvas)"
                  : "transparent",
              color: active
                ? "var(--color-app-surface, #fff)"
                : passed
                  ? "var(--color-text-body)"
                  : "var(--color-text-muted)",
              border: "1px solid var(--color-divider)",
              fontWeight: active ? 600 : 400,
            }}
          >
            {passed ? <span aria-hidden>✓</span> : null}
            {s.label}
          </button>
        );
      })}
      {activeIdx < 0 ? (
        <span className="px-1 text-[var(--text-meta)]" style={labelStyle()}>
          단계를 누르면 SE 관리가 시작됩니다
        </span>
      ) : null}
    </span>
  );
}

/* ---------------- 요구조건 대장 ---------------- */

const REQ_STATUS_LABEL: Record<ProjectRequirement["status"], string> = {
  open: "미충족",
  verifying: "검증 중",
  satisfied: "충족",
  failed: "실패",
};
const REQ_STATUS_FROM_LABEL = Object.fromEntries(
  Object.entries(REQ_STATUS_LABEL).map(([k, v]) => [v, k]),
) as Record<string, ProjectRequirement["status"]>;
const REQ_STATUS_COLOR: Record<string, TagColor> = {
  미충족: "gray",
  "검증 중": "blue",
  충족: "green",
  실패: "red",
};
const KIND_META: Record<
  ProjectRequirement["kind"],
  { title: string; prefix: string; subtitle: string }
> = {
  constraint: { title: "제약조건", prefix: "C", subtitle: "외부에서 주어지는 조건" },
  goal: { title: "프로젝트 목표", prefix: "R", subtitle: "집단이 세운 요구조건" },
};

function reqInput(r: ProjectRequirement): ProjectRequirementInput {
  return {
    kind: r.kind,
    text: r.text,
    verify_method: r.verify_method,
    notes: r.notes,
    status: r.status,
  };
}

/** blur/Enter 시 커밋하는 셀 인풋 (요구조건 내용/검증 방법). */
function CellInput({
  value,
  placeholder,
  onCommit,
  grow,
}: {
  value: string;
  placeholder?: string;
  onCommit: (v: string) => void;
  grow?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  // 서버값이 바뀌면 draft를 렌더 중에 재시드한다 (effect-setState 금지 패턴 대응).
  const [seeded, setSeeded] = useState(value);
  if (seeded !== value) {
    setSeeded(value);
    setDraft(value);
  }
  return (
    <input
      value={draft}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => draft.trim() !== value && onCommit(draft)}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        if (e.key === "Escape") setDraft(value);
      }}
      className={`h-8 rounded-[4px] border-0 bg-transparent px-1.5 text-[var(--text-body-sm)] outline-none hover:bg-[var(--color-app-canvas)] focus:bg-[var(--color-app-canvas)] ${grow ? "min-w-0 flex-1" : "w-[130px] shrink-0"}`}
      style={{ color: "var(--color-text-body)" }}
    />
  );
}

export function RequirementsSection({
  projectId,
  parentProjectId,
  parentName,
}: {
  projectId: string;
  /** 하위 프로젝트일 때 상위 project_id — flow-down 버튼 노출 조건. */
  parentProjectId: string | null;
  parentName?: string | null;
}) {
  const [reqs, setReqs] = useState<ProjectRequirement[]>([]);
  const [err, setErr] = useState<string | null>(null);
  // flow-down 피커: 상위 요구조건 목록 + 체크 상태. null=닫힘.
  const [picker, setPicker] = useState<{
    parent: ProjectRequirement[];
    checked: Set<string>;
  } | null>(null);

  const reload = useCallback(async () => {
    try {
      setReqs(await api.listProjectRequirements(projectId));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "요구조건을 불러오지 못했습니다.");
    }
  }, [projectId]);

  useEffect(() => {
    let alive = true;
    api
      .listProjectRequirements(projectId)
      .then((rs) => {
        if (alive) setReqs(rs);
      })
      .catch((e) => {
        if (alive) setErr(e instanceof Error ? e.message : "요구조건을 불러오지 못했습니다.");
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  async function add(kind: ProjectRequirement["kind"]) {
    const text = window.prompt(`${KIND_META[kind].title} 내용`);
    if (!text?.trim()) return;
    try {
      await api.createProjectRequirement(projectId, { kind, text: text.trim() });
      await reload();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "추가 실패");
    }
  }

  function patch(r: ProjectRequirement, part: Partial<ProjectRequirementInput>) {
    const next = { ...reqInput(r), ...part };
    setReqs((cur) =>
      cur.map((x) => (x.requirement_id === r.requirement_id ? { ...x, ...next } : x)),
    );
    void api
      .updateProjectRequirement(projectId, r.requirement_id, next)
      .catch(() => {
        setErr("저장 실패");
        void reload();
      });
  }

  async function remove(r: ProjectRequirement) {
    if (!window.confirm("이 요구조건을 삭제하시겠습니까?")) return;
    await api.deleteProjectRequirement(projectId, r.requirement_id);
    await reload();
  }

  async function openPicker() {
    if (!parentProjectId) return;
    const parent = await api.listProjectRequirements(parentProjectId);
    const already = new Set(
      reqs.map((r) => r.parent_requirement_id).filter(Boolean) as string[],
    );
    setPicker({
      parent: parent.filter((p) => !already.has(p.requirement_id)),
      checked: new Set(),
    });
  }

  async function runFlowDown() {
    if (!picker || picker.checked.size === 0) return;
    await api.flowDownRequirements(projectId, [...picker.checked]);
    setPicker(null);
    await reload();
  }

  const satisfied = reqs.filter((r) => r.status === "satisfied").length;
  const verifying = reqs.filter((r) => r.status === "verifying").length;
  const fulfillment =
    reqs.length > 0
      ? {
          total: reqs.length,
          complete: satisfied,
          inProgress: verifying,
          percent: Math.round((satisfied / reqs.length) * 100),
        }
      : null;

  return (
    <PeekSection
      title="요구조건"
      count={reqs.length}
      action={
        <span className="flex items-center gap-1">
          <ProgressBar value={fulfillment} compact />
          {parentProjectId ? (
            <ReqButton onClick={openPicker}>↑ 상위에서 가져오기</ReqButton>
          ) : null}
          <ReqButton onClick={() => add("constraint")}>＋ 제약조건</ReqButton>
          <ReqButton onClick={() => add("goal")}>＋ 목표</ReqButton>
        </span>
      }
    >
      {err ? (
        <p className="px-1.5 text-[var(--text-meta)]" style={{ color: "var(--color-fail-fg)" }}>
          {err}
        </p>
      ) : null}

      {picker ? (
        <div
          className="mx-1.5 mb-2 rounded-[8px] p-2"
          style={{ border: "1px solid var(--color-divider)", background: "var(--color-app-canvas)" }}
        >
          <p className="mb-1 px-1 text-[var(--text-meta)]" style={labelStyle()}>
            {parentName ?? "상위 프로젝트"}의 요구조건에서 이 하위 프로젝트로 가져올 항목
            (원본 링크가 남습니다)
          </p>
          {picker.parent.length === 0 ? (
            <p className="px-1 py-1 text-[var(--text-meta)]" style={labelStyle()}>
              가져올 수 있는 요구조건이 없습니다.
            </p>
          ) : (
            picker.parent.map((p) => (
              <label
                key={p.requirement_id}
                className="flex min-h-[32px] cursor-pointer items-center gap-2 rounded-[4px] px-1 hover:bg-[var(--color-app-surface,#fff)]"
              >
                <input
                  type="checkbox"
                  checked={picker.checked.has(p.requirement_id)}
                  onChange={(e) => {
                    const next = new Set(picker.checked);
                    if (e.target.checked) next.add(p.requirement_id);
                    else next.delete(p.requirement_id);
                    setPicker({ ...picker, checked: next });
                  }}
                />
                <span className="text-[var(--text-meta)] tabular-nums" style={labelStyle()}>
                  {KIND_META[p.kind].prefix}
                  {p.num}
                </span>
                <span className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]">
                  {p.text}
                </span>
              </label>
            ))
          )}
          <div className="mt-1 flex justify-end gap-1">
            <ReqButton onClick={() => setPicker(null)}>닫기</ReqButton>
            <ReqButton onClick={runFlowDown} primary>
              {picker.checked.size}개 가져오기
            </ReqButton>
          </div>
        </div>
      ) : null}

      {reqs.length === 0 && !picker ? (
        <p className="px-1.5 py-1 text-[var(--text-meta)]" style={labelStyle()}>
          아직 요구조건이 없습니다. 외부 제약조건과 프로젝트 목표를 번호로 정리해 보세요
          (SRR).
        </p>
      ) : (
        (["constraint", "goal"] as const).map((kind) => {
          const group = reqs.filter((r) => r.kind === kind);
          if (group.length === 0) return null;
          return (
            <div key={kind} className="mb-1.5">
              <p
                className="px-1.5 pb-0.5 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)", fontWeight: 600 }}
              >
                {KIND_META[kind].title}
                <span className="ml-1 font-normal" style={labelStyle()}>
                  {KIND_META[kind].subtitle}
                </span>
              </p>
              {group.map((r) => (
                <div
                  key={r.requirement_id}
                  className="group flex min-h-[36px] items-center gap-1 rounded-[6px] px-1.5 hover:bg-[var(--color-app-canvas)]"
                >
                  <span
                    className="w-8 shrink-0 text-[var(--text-meta)] tabular-nums"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {KIND_META[kind].prefix}
                    {r.num}
                  </span>
                  <CellInput grow value={r.text} onCommit={(v) => v.trim() && patch(r, { text: v.trim() })} />
                  <CellInput
                    value={r.verify_method ?? ""}
                    placeholder="검증 방법"
                    onCommit={(v) => patch(r, { verify_method: v.trim() || null })}
                  />
                  <TagSelect
                    variant="inline"
                    value={REQ_STATUS_LABEL[r.status]}
                    options={Object.values(REQ_STATUS_LABEL)}
                    colorFor={(v) => REQ_STATUS_COLOR[v] ?? "gray"}
                    onChange={(v) => v && patch(r, { status: REQ_STATUS_FROM_LABEL[v] })}
                  />
                  {r.parent_requirement_id ? (
                    <span
                      title="상위 프로젝트 요구조건에서 flow-down됨"
                      className="shrink-0 text-[var(--text-meta)]"
                      style={{ color: "var(--color-text-faint)" }}
                    >
                      ↑상위
                    </span>
                  ) : null}
                  <button
                    type="button"
                    onClick={() => remove(r)}
                    className="shrink-0 px-1 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                    style={{ color: "var(--color-fail-fg)" }}
                  >
                    삭제
                  </button>
                </div>
              ))}
            </div>
          );
        })
      )}
    </PeekSection>
  );
}

function ReqButton({
  children,
  onClick,
  primary,
}: {
  children: React.ReactNode;
  onClick: () => void;
  primary?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-[32px] items-center rounded-[6px] px-2 text-[var(--text-meta)] whitespace-nowrap hover:bg-[var(--color-app-canvas)]"
      style={
        primary
          ? {
              background: "var(--color-text-strong)",
              color: "var(--color-app-surface, #fff)",
            }
          : { color: "var(--color-text-muted)" }
      }
    >
      {children}
    </button>
  );
}
