"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type DashboardProject,
  type Kpi,
  type KpiCadence,
  type KpiCheckin,
  type KpiConfidence,
  type KpiInput,
  type KpiLevel,
  type KpiLinked,
  type KpiSummary,
  type KpiTreeNode,
  type KpiWeeklyItem,
  type TeamMember,
} from "@/lib/api";
import { ChannelHeader, Avatar, Card, Banner, ToolbarButton, Skeleton } from "@/components/ui";
import {
  ConfidenceSparkline,
  Field,
  Modal,
  ProgressBar,
  Select,
  TextArea,
  TextInput,
  labelStyle,
  money,
  scoreTone,
} from "@/components/dashboard/kit";

const LEVEL_LABEL: Record<KpiLevel, string> = {
  north_star: "North Star",
  objective: "연간 목표",
  key_result: "핵심결과 (KR)",
  target: "월간 타깃",
};
const LEVEL_CADENCE: Record<KpiLevel, KpiCadence> = {
  north_star: "annual",
  objective: "annual",
  key_result: "quarterly",
  target: "monthly",
};
const METRIC_TYPES = [
  { value: "number", label: "숫자" },
  { value: "percent", label: "퍼센트(%)" },
  { value: "currency", label: "금액(₩)" },
  { value: "count", label: "카운트" },
  { value: "boolean", label: "완료여부" },
];
const STATUSES = [
  { value: "on_track", label: "순항" },
  { value: "at_risk", label: "주의" },
  { value: "off_track", label: "위험" },
  { value: "done", label: "완료" },
  { value: "archived", label: "보관" },
];
const STATUS_LABEL: Record<string, string> = Object.fromEntries(
  STATUSES.map((s) => [s.value, s.label]),
);

function fmtValue(k: { metric_type: string; unit: string | null }, v: number | null): string {
  if (v == null) return "—";
  if (k.metric_type === "currency") return money(v);
  if (k.metric_type === "percent") return `${v}%`;
  if (k.metric_type === "boolean") return v >= 1 ? "예" : "아니오";
  const n = v.toLocaleString("ko-KR");
  return k.unit ? `${n}${k.unit}` : n;
}

function num(v: string): number | null {
  const s = v.trim();
  if (s === "") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

// 기간 뱃지: 연간/분기/월간 라벨. 연간 KR도 자연스럽게 표시된다.
function periodBadge(k: { cadence: KpiCadence; quarter: number | null; month: number | null }): string {
  if (k.cadence === "quarterly") return `Q${k.quarter ?? "?"}`;
  if (k.cadence === "monthly") return `${k.month ?? "?"}월`;
  return "연간";
}

// KPI의 기간이 이미 끝났는지(미채점 넛지용).
function periodEnded(k: Kpi): boolean {
  const today = new Date();
  let end: Date;
  if (k.cadence === "monthly" && k.month != null) {
    end = new Date(k.fiscal_year, k.month, 0); // 그 달 말일
  } else if (k.cadence === "quarterly" && k.quarter != null) {
    end = new Date(k.fiscal_year, k.quarter * 3, 0);
  } else {
    end = new Date(k.fiscal_year, 12, 0);
  }
  return end < today;
}

// 채점 배지 — grade=0도 유효한 채점(빨간 0/10)이라 != null로만 분기한다.
function GradeChip({ k }: { k: Kpi }) {
  if (k.grade != null) {
    const tone = scoreTone(k.grade);
    return (
      <span
        className="shrink-0 rounded-[4px] px-1.5 py-[1px] text-[var(--text-badge)] font-bold"
        style={{ background: tone.bg, color: tone.fg }}
        title={k.grade_note ? `채점 근거: ${k.grade_note}` : "공식 채점"}
      >
        채점 {k.grade}/10
      </span>
    );
  }
  if ((k.level === "target" || k.level === "key_result") && periodEnded(k)) {
    return (
      <span
        className="shrink-0 rounded-[4px] px-1.5 py-[1px] text-[var(--text-badge)] font-semibold"
        style={{ background: "var(--color-warn-bg)", color: "var(--color-warn-fg)" }}
        title="기간이 끝났지만 아직 채점하지 않았습니다 — 계획·회고의 월간 회고에서 채점하세요."
      >
        미채점
      </span>
    );
  }
  return null;
}

type Draft = {
  kpi_id: string | null;
  parent_id: string;
  level: KpiLevel;
  cadence: KpiCadence;
  fiscal_year: number;
  quarter: number;
  month: number;
  title: string;
  description: string;
  metric_type: string;
  unit: string;
  baseline: string;
  target: string;
  current_value: string;
  direction: "up" | "down";
  project_id: string;
  owner_member_id: string;
  status: string;
};

function emptyDraft(year: number, level: KpiLevel = "objective"): Draft {
  return {
    kpi_id: null,
    parent_id: "",
    level,
    cadence: LEVEL_CADENCE[level],
    fiscal_year: year,
    quarter: 1,
    month: 1,
    title: "",
    description: "",
    metric_type: "number",
    unit: "",
    baseline: "",
    target: "",
    current_value: "",
    direction: "up",
    project_id: "",
    owner_member_id: "",
    status: "on_track",
  };
}

function flatten(nodes: KpiTreeNode[]): KpiTreeNode[] {
  const out: KpiTreeNode[] = [];
  const walk = (n: KpiTreeNode) => {
    out.push(n);
    n.children.forEach(walk);
  };
  nodes.forEach(walk);
  return out;
}

export default function KpiPage() {
  const thisYear = new Date().getFullYear();
  const [year, setYear] = useState(2026 <= thisYear ? thisYear : 2026);
  const [tree, setTree] = useState<KpiTreeNode[]>([]);
  const [summary, setSummary] = useState<KpiSummary | null>(null);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [projectFilter, setProjectFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(emptyDraft(2026));
  const [busy, setBusy] = useState(false);
  const [modalErr, setModalErr] = useState<string | null>(null);

  const [checkinFor, setCheckinFor] = useState<Kpi | null>(null);

  // KR 주간 신뢰도(연도 배치) — kpi_id별 시리즈로 그룹해 스파크라인에 쓴다.
  const [confidence, setConfidence] = useState<KpiConfidence[]>([]);

  const reload = useCallback(
    async (y: number, pf: string) => {
      const [t, s, cf] = await Promise.all([
        api.getKpiTree(y, pf || undefined),
        api.getKpiSummary(y, pf || undefined),
        api.listKpiConfidence(y).catch(() => [] as KpiConfidence[]),
      ]);
      setError(null);
      setTree(t);
      setSummary(s);
      setConfidence(cf);
    },
    [],
  );

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.getKpiTree(year, projectFilter || undefined),
      api.getKpiSummary(year, projectFilter || undefined),
      api.listTeamMembers(),
      api.listDashboardProjects(),
      api.listKpiConfidence(year).catch(() => [] as KpiConfidence[]),
    ])
      .then(([t, s, mb, pr, cf]) => {
        if (!alive) return;
        setError(null);
        setTree(t);
        setSummary(s);
        setMembers(mb);
        setProjects(pr);
        setConfidence(cf);
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : "불러오기 실패"))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [year, projectFilter]);

  const confidenceByKpi = useMemo(() => {
    const m = new Map<string, KpiConfidence[]>();
    for (const c of confidence) {
      const list = m.get(c.kpi_id);
      if (list) list.push(c);
      else m.set(c.kpi_id, [c]);
    }
    return m;
  }, [confidence]);

  const flat = useMemo(() => flatten(tree), [tree]);
  const projectName = (id: string | null) =>
    id ? projects.find((p) => p.project_id === id)?.name ?? null : null;
  const memberName = (id: string | null) =>
    id ? members.find((m) => m.member_id === id)?.name ?? null : null;

  function openCreate(level: KpiLevel, parentId?: string) {
    const d = emptyDraft(year, level);
    if (parentId) d.parent_id = parentId;
    if (level === "key_result") d.quarter = 2;
    if (level === "target") d.month = new Date().getMonth() + 1;
    setDraft(d);
    setModalErr(null);
    setOpen(true);
  }
  function openEdit(k: Kpi) {
    setDraft({
      kpi_id: k.kpi_id,
      parent_id: k.parent_id ?? "",
      level: k.level,
      cadence: k.cadence,
      fiscal_year: k.fiscal_year,
      quarter: k.quarter ?? 2,
      month: k.month ?? new Date().getMonth() + 1,
      title: k.title,
      description: k.description ?? "",
      metric_type: k.metric_type,
      unit: k.unit ?? "",
      baseline: k.baseline == null ? "" : String(k.baseline),
      target: k.target == null ? "" : String(k.target),
      current_value: k.current_value == null ? "" : String(k.current_value),
      direction: k.direction,
      project_id: k.project_id ?? "",
      owner_member_id: k.owner_member_id ?? "",
      status: k.status,
    });
    setModalErr(null);
    setOpen(true);
  }

  async function save() {
    setBusy(true);
    setModalErr(null);
    const cadence = draft.cadence;
    const body: KpiInput = {
      parent_id: draft.parent_id || null,
      level: draft.level,
      cadence,
      fiscal_year: draft.fiscal_year,
      quarter: cadence === "quarterly" ? draft.quarter : null,
      month: cadence === "monthly" ? draft.month : null,
      title: draft.title,
      description: draft.description.trim() || null,
      metric_type: draft.metric_type as KpiInput["metric_type"],
      unit: draft.unit.trim() || null,
      baseline: num(draft.baseline),
      target: num(draft.target),
      current_value: num(draft.current_value),
      direction: draft.direction,
      project_id: draft.project_id || null,
      owner_member_id: draft.owner_member_id || null,
      status: draft.status as KpiInput["status"],
      sort_order: 0,
    };
    try {
      if (draft.kpi_id) await api.updateKpi(draft.kpi_id, body);
      else await api.createKpi(body);
      await reload(year, projectFilter);
      setOpen(false);
    } catch (e) {
      setModalErr(e instanceof Error ? e.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  }

  async function remove(k: Kpi) {
    const hasChildren = flat.some((x) => x.parent_id === k.kpi_id);
    const msg = hasChildren
      ? "하위 KPI도 함께 삭제됩니다. 삭제하시겠습니까?"
      : "이 KPI를 삭제하시겠습니까?";
    if (!window.confirm(msg)) return;
    try {
      await api.deleteKpi(k.kpi_id);
      await reload(year, projectFilter);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "삭제 실패");
    }
  }

  // 부모 후보: KR은 objective 아래, 월간 타깃은 objective(직접) 또는 KR 아래,
  // objective는 north_star(선택) 아래.
  const parentOptions = useMemo(() => {
    const want: KpiLevel[] =
      draft.level === "key_result"
        ? ["objective"]
        : draft.level === "target"
          ? ["objective", "key_result"]
          : draft.level === "objective"
            ? ["north_star"]
            : [];
    if (want.length === 0) return [];
    return flat.filter((k) => want.includes(k.level));
  }, [draft.level, flat]);

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <ChannelHeader
        glyph="#"
        title="KPI"
        subtitle="OKR · North Star — 월간·분기·년간 목표를 한 곳에서"
      />

      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">{error}</Banner>
        </div>
      ) : null}

      {/* 툴바: 연도 / 프로젝트 필터 / 추가 */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1">
          <ToolbarButton onClick={() => setYear((y) => y - 1)} aria-label="이전 연도">
            ◀
          </ToolbarButton>
          <span
            className="min-w-[64px] text-center text-[var(--text-body)] font-bold tabular-nums"
            style={{ color: "var(--color-text-strong)" }}
          >
            {year}
          </span>
          <ToolbarButton onClick={() => setYear((y) => y + 1)} aria-label="다음 연도">
            ▶
          </ToolbarButton>
        </div>
        <div style={{ minWidth: 150 }}>
          <Select
            aria-label="프로젝트 필터"
            value={projectFilter}
            options={[
              { value: "", label: "전체 프로젝트" },
              ...projects.map((p) => ({ value: p.project_id, label: p.name })),
            ]}
            onChange={(e) => setProjectFilter(e.target.value)}
          />
        </div>
        <div className="ml-auto flex gap-1.5">
          <ToolbarButton onClick={() => openCreate("objective")}>+ 연간 목표</ToolbarButton>
          <ToolbarButton tone="accent" onClick={() => openCreate("north_star")}>
            + North Star
          </ToolbarButton>
        </div>
      </div>

      {loading ? (
        <Card className="p-4">
          <Skeleton className="h-6 w-48" />
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {/* North Star */}
          {summary?.north_star ? (
            <NorthStarCard
              kpi={summary.north_star}
              projectName={projectName(summary.north_star.project_id)}
              onEdit={() => openEdit(summary.north_star!)}
              onCheckin={() => setCheckinFor(summary.north_star!)}
            />
          ) : (
            <Card className="p-4">
              <p className="text-[var(--text-body-sm)]" style={labelStyle()}>
                North Star Metric을 설정하면 회사의 단일 진북이 여기 표시됩니다. 우측 상단
                <strong style={{ color: "var(--color-text-strong)" }}> + North Star</strong>로 추가하세요.
              </p>
            </Card>
          )}

          {/* 요약 카드 */}
          {summary ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-6">
              <Kpi label="연간 목표" value={`${summary.objectives}`} />
              <Kpi label="핵심결과(KR)" value={`${summary.key_results}`} />
              <Kpi label="순항" value={`${summary.on_track}`} tone="#1f7a4d" />
              <Kpi label="주의·위험" value={`${summary.at_risk + summary.off_track}`} tone="#8a5a13" />
              <Kpi
                label="타깃 채점"
                value={summary.targets_total > 0 ? `${summary.targets_graded}/${summary.targets_total}` : "—"}
              />
              <Kpi
                label="평균 진행률"
                value={summary.avg_progress == null ? "—" : `${Math.round(summary.avg_progress * 100)}%`}
                strong
              />
            </div>
          ) : null}

          {/* OKR 트리 */}
          <Card className="p-3 sm:p-4">
            {tree.filter((n) => n.level === "objective").length === 0 ? (
              <p className="py-6 text-center text-[var(--text-body-sm)]" style={labelStyle()}>
                연간 목표(Objective)가 없습니다. <strong style={{ color: "var(--color-text-strong)" }}>+ 연간 목표</strong>로 시작하고,
                각 목표에 KR과 월간 타깃을 달아 트리를 완성하세요.
              </p>
            ) : (
              <div className="flex flex-col">
                {tree
                  .filter((n) => n.level === "objective")
                  .map((obj) => (
                    <ObjectiveBlock
                      key={obj.kpi_id}
                      obj={obj}
                      projectName={projectName}
                      memberName={memberName}
                      confidenceByKpi={confidenceByKpi}
                      onAddKr={(pid) => openCreate("key_result", pid)}
                      onAddTarget={(pid) => openCreate("target", pid)}
                      onEdit={openEdit}
                      onCheckin={(k) => setCheckinFor(k)}
                      onDelete={remove}
                    />
                  ))}
              </div>
            )}
          </Card>
        </div>
      )}

      {/* set/edit 모달 */}
      <KpiModal
        open={open}
        draft={draft}
        setDraft={setDraft}
        busy={busy}
        err={modalErr}
        members={members}
        projects={projects}
        parentOptions={parentOptions}
        onClose={() => setOpen(false)}
        onSave={save}
      />

      {/* 체크인 모달 (kpi별 remount로 폼 초기화) */}
      {checkinFor ? (
        <CheckinModal
          key={checkinFor.kpi_id}
          kpi={checkinFor}
          members={members}
          onClose={() => setCheckinFor(null)}
          onSaved={async () => {
            setCheckinFor(null);
            await reload(year, projectFilter);
          }}
        />
      ) : null}
    </div>
  );
}

function Kpi({ label, value, strong, tone }: { label: string; value: string; strong?: boolean; tone?: string }) {
  return (
    <Card className="p-3">
      <p className="text-[var(--text-meta)] font-semibold" style={labelStyle()}>{label}</p>
      <p
        className="mt-1 truncate text-[var(--text-day-heading)] font-extrabold tabular-nums"
        style={{ color: tone ?? (strong ? "var(--color-progress)" : "var(--color-text-strong)") }}
      >
        {value}
      </p>
    </Card>
  );
}

function NorthStarCard({
  kpi,
  projectName,
  onEdit,
  onCheckin,
}: {
  kpi: Kpi;
  projectName: string | null;
  onEdit: () => void;
  onCheckin: () => void;
}) {
  return (
    <Card className="p-4" style={{ borderColor: "var(--color-progress)" }}>
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-[var(--text-meta)] font-bold" style={{ color: "var(--color-progress)" }}>
          ★ NORTH STAR {projectName ? `· ${projectName}` : ""}
        </span>
        <span className="flex gap-2">
          <button onClick={onCheckin} className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>체크인</button>
          <button onClick={onEdit} className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>편집</button>
        </span>
      </div>
      <h2 className="text-[var(--text-day-heading)] font-extrabold" style={{ color: "var(--color-text-strong)" }}>
        {kpi.title}
      </h2>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="text-[var(--text-day-heading)] font-extrabold tabular-nums" style={{ color: "var(--color-text-strong)" }}>
          {fmtValue(kpi, kpi.current_value)}
        </span>
        <span className="text-[var(--text-body-sm)]" style={labelStyle()}>
          / 목표 {fmtValue(kpi, kpi.target)}
        </span>
      </div>
      <div className="mt-2">
        <ProgressBar value={kpi.progress} tone={kpi.status} height={10} />
      </div>
    </Card>
  );
}

function ObjectiveBlock({
  obj,
  projectName,
  memberName,
  confidenceByKpi,
  onAddKr,
  onAddTarget,
  onEdit,
  onCheckin,
  onDelete,
}: {
  obj: KpiTreeNode;
  projectName: (id: string | null) => string | null;
  memberName: (id: string | null) => string | null;
  confidenceByKpi: Map<string, KpiConfidence[]>;
  onAddKr: (parentId: string) => void;
  onAddTarget: (parentId: string) => void;
  onEdit: (k: Kpi) => void;
  onCheckin: (k: Kpi) => void;
  onDelete: (k: Kpi) => void;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="border-b py-3 last:border-b-0" style={{ borderColor: "var(--color-divider-soft)" }}>
      <div className="flex items-start gap-2">
        <button
          onClick={() => setOpen((v) => !v)}
          className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-[6px] hover:bg-[var(--color-app-canvas)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {open ? "▾" : "▸"}
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-[var(--text-meta)] font-bold" style={{ color: "var(--color-text-muted)" }}>연간</span>
            <span className="truncate text-[var(--text-body)] font-bold" style={{ color: "var(--color-text-strong)" }}>
              {obj.title}
            </span>
            {projectName(obj.project_id) ? (
              <span className="rounded-[5px] px-1.5 py-[1px] text-[var(--text-badge)]" style={{ background: "var(--color-app-canvas)", color: "var(--color-text-muted)" }}>
                {projectName(obj.project_id)}
              </span>
            ) : null}
          </div>
          <div className="mt-1.5 max-w-[420px]">
            <ProgressBar value={obj.progress} tone={obj.status} />
          </div>
        </div>
        <div className="flex shrink-0 gap-2 pt-0.5">
          <button onClick={() => onAddKr(obj.kpi_id)} className="text-[var(--text-meta)]" style={{ color: "var(--color-progress)" }}>+ KR</button>
          <button onClick={() => onAddTarget(obj.kpi_id)} className="text-[var(--text-meta)]" style={{ color: "var(--color-progress)" }}>+ 월간</button>
          <button onClick={() => onEdit(obj)} className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>편집</button>
          <button onClick={() => onDelete(obj)} className="text-[var(--text-meta)]" style={{ color: "var(--color-fail-fg)" }}>삭제</button>
        </div>
      </div>

      {open ? (
        <div className="mt-2 flex flex-col gap-1.5 pl-8">
          {obj.children.length === 0 ? (
            <p className="text-[var(--text-meta)]" style={labelStyle()}>KR 또는 월간 타깃을 추가하세요.</p>
          ) : (
            obj.children.map((c) =>
              c.level === "target" ? (
                <TargetRow key={c.kpi_id} t={c} onEdit={onEdit} onCheckin={onCheckin} onDelete={onDelete} />
              ) : (
                <KrRow
                  key={c.kpi_id}
                  kr={c}
                  memberName={memberName}
                  confidence={confidenceByKpi.get(c.kpi_id) ?? []}
                  onAddTarget={onAddTarget}
                  onEdit={onEdit}
                  onCheckin={onCheckin}
                  onDelete={onDelete}
                />
              ),
            )
          )}
        </div>
      ) : null}
    </div>
  );
}

function KrRow({
  kr,
  memberName,
  confidence,
  onAddTarget,
  onEdit,
  onCheckin,
  onDelete,
}: {
  kr: KpiTreeNode;
  memberName: (id: string | null) => string | null;
  confidence: KpiConfidence[];
  onAddTarget: (parentId: string) => void;
  onEdit: (k: Kpi) => void;
  onCheckin: (k: Kpi) => void;
  onDelete: (k: Kpi) => void;
}) {
  const latest = confidence.length > 0 ? confidence[confidence.length - 1] : null;
  return (
    <div className="rounded-[8px] p-2" style={{ background: "var(--color-app-canvas)" }}>
      <div className="flex items-center gap-2">
        <span className="rounded-[4px] px-1.5 py-[1px] text-[var(--text-badge)] font-semibold" style={{ background: "#dce8fb", color: "#2d5d96" }}>
          {periodBadge(kr)}
        </span>
        <span className="min-w-0 flex-1 truncate text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
          {kr.title}
        </span>
        <GradeChip k={kr} />
        <span className="shrink-0 text-[var(--text-meta)] tabular-nums" style={labelStyle()}>
          {fmtValue(kr, kr.current_value)} / {fmtValue(kr, kr.target)}
        </span>
      </div>
      <div className="mt-1.5 flex items-center gap-2">
        <div className="max-w-[300px] flex-1">
          <ProgressBar value={kr.progress} tone={kr.status} />
        </div>
        <span className="rounded-[4px] px-1.5 py-[1px] text-[var(--text-badge)]" style={{ background: "var(--color-card)", color: "var(--color-text-muted)" }}>
          {STATUS_LABEL[kr.status] ?? kr.status}
        </span>
        {latest ? (
          <span
            className="flex shrink-0 items-center gap-1.5"
            title={`주간 신뢰도 — 최근 ${latest.confidence}/10 (${latest.week_start} 주)`}
          >
            <ConfidenceSparkline points={confidence} width={72} height={20} />
            <span
              className="rounded-[4px] px-1.5 py-[1px] text-[var(--text-badge)] font-bold"
              style={{ background: scoreTone(latest.confidence).bg, color: scoreTone(latest.confidence).fg }}
            >
              자신감 {latest.confidence}
            </span>
          </span>
        ) : null}
        {memberName(kr.owner_member_id) ? (
          <span className="flex shrink-0 items-center gap-1 text-[var(--text-meta)]" style={labelStyle()}>
            <Avatar name={memberName(kr.owner_member_id)!} size="sm" />
            {memberName(kr.owner_member_id)}
          </span>
        ) : null}
        <span className="ml-auto flex shrink-0 gap-2">
          <button onClick={() => onAddTarget(kr.kpi_id)} className="text-[var(--text-meta)]" style={{ color: "var(--color-progress)" }}>+ 월타깃</button>
          <button onClick={() => onCheckin(kr)} className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>체크인</button>
          <button onClick={() => onEdit(kr)} className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>편집</button>
          <button onClick={() => onDelete(kr)} className="text-[var(--text-meta)]" style={{ color: "var(--color-fail-fg)" }}>삭제</button>
        </span>
      </div>
      {kr.children.length > 0 ? (
        <div className="mt-2 flex flex-col gap-1 pl-3">
          {kr.children.map((t) => (
            <TargetRow key={t.kpi_id} t={t} onEdit={onEdit} onCheckin={onCheckin} onDelete={onDelete} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function TargetRow({
  t,
  onEdit,
  onCheckin,
  onDelete,
}: {
  t: KpiTreeNode;
  onEdit: (k: Kpi) => void;
  onCheckin: (k: Kpi) => void;
  onDelete: (k: Kpi) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="rounded-[4px] px-1 py-[1px] text-[var(--text-badge)] font-semibold" style={{ background: "var(--color-card)", color: "var(--color-text-muted)" }}>
        {periodBadge(t)}
      </span>
      <span className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]" style={{ color: "var(--color-text-body)" }}>
        {t.title}
      </span>
      <GradeChip k={t} />
      <span className="shrink-0 text-[var(--text-meta)] tabular-nums" style={labelStyle()}>
        {fmtValue(t, t.current_value)} / {fmtValue(t, t.target)}
      </span>
      <div className="w-[120px] shrink-0">
        <ProgressBar value={t.progress} tone={t.status} height={6} />
      </div>
      <span className="flex shrink-0 gap-1.5">
        <button onClick={() => onCheckin(t)} className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>체크인</button>
        <button onClick={() => onEdit(t)} className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>편집</button>
        <button onClick={() => onDelete(t)} className="text-[var(--text-meta)]" style={{ color: "var(--color-fail-fg)" }}>×</button>
      </span>
    </div>
  );
}

function KpiModal({
  open,
  draft,
  setDraft,
  busy,
  err,
  members,
  projects,
  parentOptions,
  onClose,
  onSave,
}: {
  open: boolean;
  draft: Draft;
  setDraft: (d: Draft) => void;
  busy: boolean;
  err: string | null;
  members: TeamMember[];
  projects: DashboardProject[];
  parentOptions: KpiTreeNode[];
  onClose: () => void;
  onSave: () => void;
}) {
  const cadence = draft.cadence;
  return (
    <Modal
      open={open}
      title={draft.kpi_id ? "KPI 편집" : `${LEVEL_LABEL[draft.level]} 추가`}
      onClose={onClose}
      footer={
        <>
          <ToolbarButton onClick={onClose} disabled={busy}>닫기</ToolbarButton>
          <ToolbarButton tone="accent" onClick={onSave} disabled={busy}>
            {busy ? "저장 중…" : "저장"}
          </ToolbarButton>
        </>
      }
    >
      {err ? <p className="mb-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-fail-fg)" }}>{err}</p> : null}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="레벨">
          <Select
            value={draft.level}
            options={[
              { value: "north_star", label: "North Star" },
              { value: "objective", label: "연간 목표 (Objective)" },
              { value: "key_result", label: "핵심결과 (KR)" },
              { value: "target", label: "월간 타깃" },
            ]}
            onChange={(e) => {
              const level = e.target.value as KpiLevel;
              setDraft({ ...draft, level, cadence: LEVEL_CADENCE[level], parent_id: "" });
            }}
          />
        </Field>
        <Field label="주기">
          <Select
            value={draft.cadence}
            options={[
              { value: "annual", label: "연간" },
              { value: "quarterly", label: "분기" },
              { value: "monthly", label: "월간" },
            ]}
            onChange={(e) => setDraft({ ...draft, cadence: e.target.value as KpiCadence })}
          />
        </Field>
        <Field label="연도">
          <TextInput
            type="number"
            value={String(draft.fiscal_year)}
            onChange={(e) => setDraft({ ...draft, fiscal_year: Number(e.target.value) || draft.fiscal_year })}
          />
        </Field>
        {cadence === "quarterly" ? (
          <Field label="분기">
            <Select
              value={String(draft.quarter)}
              options={[1, 2, 3, 4].map((q) => ({ value: String(q), label: `Q${q}` }))}
              onChange={(e) => setDraft({ ...draft, quarter: Number(e.target.value) })}
            />
          </Field>
        ) : null}
        {cadence === "monthly" ? (
          <Field label="월">
            <Select
              value={String(draft.month)}
              options={Array.from({ length: 12 }, (_, i) => ({ value: String(i + 1), label: `${i + 1}월` }))}
              onChange={(e) => setDraft({ ...draft, month: Number(e.target.value) })}
            />
          </Field>
        ) : null}
        {parentOptions.length > 0 ? (
          <Field label="상위 KPI" full>
            <Select
              value={draft.parent_id}
              options={[
                { value: "", label: "— 없음 —" },
                ...parentOptions.map((p) => ({ value: p.kpi_id, label: p.title })),
              ]}
              onChange={(e) => setDraft({ ...draft, parent_id: e.target.value })}
            />
          </Field>
        ) : null}
        <Field label="제목" full>
          <TextInput value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
        </Field>
        {draft.level !== "objective" ? (
          <>
            <Field label="지표 유형">
              <Select
                value={draft.metric_type}
                options={METRIC_TYPES}
                onChange={(e) => setDraft({ ...draft, metric_type: e.target.value })}
              />
            </Field>
            <Field label="단위">
              <TextInput value={draft.unit} placeholder="예: 컷, %, 명" onChange={(e) => setDraft({ ...draft, unit: e.target.value })} />
            </Field>
            <Field label="시작값(baseline)">
              <TextInput type="number" value={draft.baseline} onChange={(e) => setDraft({ ...draft, baseline: e.target.value })} />
            </Field>
            <Field label="목표값(target)">
              <TextInput type="number" value={draft.target} onChange={(e) => setDraft({ ...draft, target: e.target.value })} />
            </Field>
            <Field label="현재값">
              <TextInput type="number" value={draft.current_value} onChange={(e) => setDraft({ ...draft, current_value: e.target.value })} />
            </Field>
            <Field label="방향">
              <Select
                value={draft.direction}
                options={[
                  { value: "up", label: "클수록 좋음 ↑" },
                  { value: "down", label: "작을수록 좋음 ↓" },
                ]}
                onChange={(e) => setDraft({ ...draft, direction: e.target.value as "up" | "down" })}
              />
            </Field>
          </>
        ) : null}
        <Field label="프로젝트">
          <Select
            value={draft.project_id}
            options={[
              { value: "", label: "— 없음 —" },
              ...projects.map((p) => ({ value: p.project_id, label: p.name })),
            ]}
            onChange={(e) => setDraft({ ...draft, project_id: e.target.value })}
          />
        </Field>
        <Field label="담당자">
          <Select
            value={draft.owner_member_id}
            options={[
              { value: "", label: "— 없음 —" },
              ...members.map((m) => ({ value: m.member_id, label: m.name })),
            ]}
            onChange={(e) => setDraft({ ...draft, owner_member_id: e.target.value })}
          />
        </Field>
        <Field label="상태">
          <Select
            value={draft.status}
            options={STATUSES}
            onChange={(e) => setDraft({ ...draft, status: e.target.value })}
          />
        </Field>
        <Field label="설명" full>
          <TextArea value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
        </Field>
      </div>
    </Modal>
  );
}

function CheckinModal({
  kpi,
  members,
  onClose,
  onSaved,
}: {
  kpi: Kpi;
  members: TeamMember[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [value, setValue] = useState(kpi.current_value == null ? "" : String(kpi.current_value));
  const [status, setStatus] = useState<string>(kpi.status);
  const [author, setAuthor] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [log, setLog] = useState<KpiCheckin[]>([]);
  const [linked, setLinked] = useState<KpiLinked | null>(null);
  const [weeklyItems, setWeeklyItems] = useState<KpiWeeklyItem[]>([]);

  useEffect(() => {
    let alive = true;
    api.listKpiCheckins(kpi.kpi_id).then((l) => alive && setLog(l)).catch(() => alive && setLog([]));
    api.getKpiLinked(kpi.kpi_id).then((l) => alive && setLinked(l)).catch(() => alive && setLinked(null));
    api.getKpiWeeklyItems(kpi.kpi_id).then((w) => alive && setWeeklyItems(w)).catch(() => alive && setWeeklyItems([]));
    return () => {
      alive = false;
    };
  }, [kpi.kpi_id]);

  const memberName = (id: string | null) => (id ? members.find((m) => m.member_id === id)?.name ?? null : null);

  async function save() {
    setBusy(true);
    setErr(null);
    const n = value.trim() === "" ? null : Number(value);
    try {
      await api.createKpiCheckin(kpi.kpi_id, {
        value: n != null && Number.isFinite(n) ? n : null,
        status: status as KpiCheckin["status"],
        note: note.trim() || null,
        author_member_id: author || null,
      });
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "체크인 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title="KPI 체크인"
      onClose={onClose}
      footer={
        <>
          <ToolbarButton onClick={onClose} disabled={busy}>닫기</ToolbarButton>
          <ToolbarButton tone="accent" onClick={save} disabled={busy}>
            {busy ? "저장 중…" : "체크인"}
          </ToolbarButton>
        </>
      }
    >
      <p className="mb-3 text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
        {kpi.title}
      </p>
      {err ? <p className="mb-2 text-[var(--text-body-sm)]" style={{ color: "var(--color-fail-fg)" }}>{err}</p> : null}

      {/* 연관 데이터(프로젝트·기간 기준) */}
      {linked && linked.project_id ? (
        <div className="mb-3 grid grid-cols-2 gap-2">
          <MiniStat label="주간계획 (완료/계획)" value={`${linked.weekly_plan_done}/${linked.weekly_plan_count}`} />
          <MiniStat label="월간계획 (완료/계획)" value={`${linked.monthly_plan_done}/${linked.monthly_plan_count}`} />
        </div>
      ) : null}

      {/* 이 KPI에 연결된 주간 실행(계획·회고 주간 항목) */}
      {weeklyItems.length > 0 ? (
        <div className="mb-3">
          <h3 className="mb-1.5 text-[var(--text-body-sm)] font-bold" style={{ color: "var(--color-text-strong)" }}>
            연결된 주간 실행
            <span className="ml-1 font-normal" style={labelStyle()}>
              {weeklyItems.filter((w) => w.done).length}/{weeklyItems.length}
            </span>
          </h3>
          <ul className="flex flex-col gap-1">
            {weeklyItems.map((w) => (
              <li
                key={`${w.entry_id}-${w.item_id}`}
                className="flex items-center gap-2 rounded-[6px] px-2 py-1 text-[var(--text-body-sm)]"
                style={{ background: "var(--color-app-canvas)" }}
              >
                <span className="shrink-0 text-[var(--text-meta)] tabular-nums" style={labelStyle()}>
                  {w.week_start.slice(5)}주
                </span>
                <span
                  className="min-w-0 flex-1 truncate"
                  style={{
                    color: w.done ? "var(--color-text-faint)" : "var(--color-text-body)",
                    textDecoration: w.done ? "line-through" : "none",
                  }}
                >
                  {w.text || "(빈 항목)"}
                </span>
                <span className="shrink-0 text-[var(--text-meta)]" style={{ color: w.done ? "var(--color-progress)" : "var(--color-text-muted)" }}>
                  {w.done ? "완료" : "진행"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="현재값">
          <TextInput type="number" value={value} onChange={(e) => setValue(e.target.value)} />
        </Field>
        <Field label="상태">
          <Select value={status} options={STATUSES} onChange={(e) => setStatus(e.target.value)} />
        </Field>
        <Field label="작성자">
          <Select
            value={author}
            options={[{ value: "", label: "— 없음 —" }, ...members.map((m) => ({ value: m.member_id, label: m.name }))]}
            onChange={(e) => setAuthor(e.target.value)}
          />
        </Field>
        <Field label="메모" full>
          <TextArea value={note} onChange={(e) => setNote(e.target.value)} placeholder="이번 체크인 근거/맥락" />
        </Field>
      </div>

      {log.length > 0 ? (
        <div className="mt-4 pt-3" style={{ borderTop: "1px solid var(--color-divider-soft)" }}>
          <h3 className="mb-2 text-[var(--text-body-sm)] font-bold" style={{ color: "var(--color-text-strong)" }}>체크인 로그</h3>
          <ul className="flex flex-col gap-1.5">
            {log.map((c) => (
              <li key={c.checkin_id} className="flex items-center gap-2 text-[var(--text-body-sm)]">
                <span className="tabular-nums" style={labelStyle()}>{c.checkin_date.slice(5)}</span>
                <span className="font-semibold tabular-nums" style={{ color: "var(--color-text-strong)" }}>
                  {c.value == null ? "—" : fmtValue(kpi, c.value)}
                </span>
                {c.status ? <span style={labelStyle()}>· {STATUS_LABEL[c.status] ?? c.status}</span> : null}
                {memberName(c.author_member_id) ? <span style={labelStyle()}>· {memberName(c.author_member_id)}</span> : null}
                {c.note ? <span className="truncate" style={{ color: "var(--color-text-body)" }}>· {c.note}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Modal>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[8px] px-2.5 py-1.5" style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider-soft)" }}>
      <p className="text-[var(--text-badge)] font-semibold" style={labelStyle()}>{label}</p>
      <p className="text-[var(--text-body-sm)] font-bold tabular-nums" style={{ color: "var(--color-text-strong)" }}>{value}</p>
    </div>
  );
}
