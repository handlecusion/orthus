"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  type DashboardProject,
  type GpuSummary,
  type InfraKind,
  type InfraProvider,
  type InfraResource,
  type InfraResourceLink,
  type MlPanel,
  type TeamMember,
} from "@/lib/api";
import { Card, Banner, Chip, Skeleton, ToolbarButton, ChannelHeader, Avatar } from "@/components/ui";
import {
  CrudSection,
  TagPill,
  labelStyle,
  memberColorMap,
  memberPillColor,
  type FieldSpec,
} from "@/components/dashboard/kit";
import { ServiceGraph } from "@/components/dashboard/ServiceGraph";
import { toExternalHref } from "@/lib/external-url";

function numOrNull(v: unknown): number | null {
  const s = String(v ?? "").trim();
  if (s === "") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}
function orNull(v: unknown): string | null {
  const s = String(v ?? "").trim();
  return s === "" ? null : s;
}

const STATUS_OPTIONS = [
  { value: "active", label: "사용중" },
  { value: "idle", label: "유휴" },
  { value: "reserved", label: "예약" },
  { value: "down", label: "장애" },
  { value: "expired", label: "만료" },
];

export default function InfraPage() {
  const [rows, setRows] = useState<InfraResource[]>([]);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [summary, setSummary] = useState<GpuSummary | null>(null);
  const [ml, setMl] = useState<MlPanel | null>(null);
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [links, setLinks] = useState<InfraResourceLink[]>([]);
  const [providers, setProviders] = useState<InfraProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);

  async function syncGpu() {
    setSyncing(true);
    setSyncMsg(null);
    setError(null);
    try {
      const r = await api.syncGpu();
      await reload();
      setSyncMsg(
        `동기화 완료 (${r.host}) — 총 ${r.total}장 · 사용 ${r.used} · 가용 ${r.available} · 평균 ${r.avg_util}%`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "GPU 동기화 실패");
    } finally {
      setSyncing(false);
    }
  }

  async function syncStorage() {
    setSyncing(true);
    setSyncMsg(null);
    setError(null);
    try {
      const r = await api.syncStorage();
      await reload();
      setSyncMsg(`스토리지 동기화 완료 — ${r.nodes}개 노드 · ${r.detail}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "스토리지 동기화 실패");
    } finally {
      setSyncing(false);
    }
  }

  const reload = useCallback(async () => {
    const [r, s, p, proj, lk, prov] = await Promise.all([
      api.listInfra(),
      api.getGpuSummary(),
      api.getMlPanel(),
      api.listDashboardProjects(),
      api.listInfraLinks(),
      api.listInfraProviders(),
    ]);
    setRows(r);
    setSummary(s);
    setMl(p);
    setProjects(proj);
    setLinks(lk);
    setProviders(prov);
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.listInfra(),
      api.getGpuSummary(),
      api.getMlPanel(),
      api.listTeamMembers(),
      api.listDashboardProjects(),
      api.listInfraLinks(),
      api.listInfraProviders(),
    ])
      .then(([r, s, p, m, proj, lk, prov]) => {
        if (!alive) return;
        setRows(r);
        setSummary(s);
        setMl(p);
        setMembers(m);
        setProjects(proj);
        setLinks(lk);
        setProviders(prov);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const ownerOptions = [
    { value: "", label: "— 담당자 없음 —" },
    ...members.map((m) => ({ value: m.member_id, label: m.name })),
  ];
  const memberColors = memberColorMap(members);
  const ownerName = (id: string | null) =>
    members.find((m) => m.member_id === id)?.name ?? "—";
  const ownerPill = (id: string | null) => {
    const member = members.find((m) => m.member_id === id);
    if (!member) return "—";
    // 담당자 셀: 팀일정과 동일한 멤버 색 아바타 + 이름 pill.
    return (
      <span className="inline-flex items-center gap-1.5">
        <Avatar name={member.name} color={memberColors.get(member.name)} size="sm" />
        <TagPill
          label={member.name}
          color={memberPillColor(memberColors.get(member.name))}
        />
      </span>
    );
  };
  const statusLabel = (s: string) =>
    STATUS_OPTIONS.find((o) => o.value === s)?.label ?? s;

  const byKind = (k: InfraKind) => rows.filter((r) => r.kind === k);

  // 공통 CRUD 핸들러 (kind 주입)
  const handlers = (kind: InfraKind) => ({
    onCreate: async (i: Record<string, unknown>) => {
      await api.createInfra({ ...i, kind } as never);
      await reload();
    },
    onUpdate: async (id: string, i: Record<string, unknown>) => {
      await api.updateInfra(id, { ...i, kind } as never);
      await reload();
    },
    onDelete: async (id: string) => {
      await api.deleteInfra(id);
      await reload();
    },
  });

  const gpuFields: FieldSpec<InfraResource>[] = [
    { key: "name", label: "이름", required: true, placeholder: "예: A100 노드 #1" },
    { key: "model", label: "모델", placeholder: "A100 / H200 / B200" },
    { key: "location", label: "위치", placeholder: "서버실 / 클라우드 / Tailscale 노드" },
    {
      key: "capacity",
      label: "장수",
      type: "number",
      render: (r) => `${r.used ?? 0}/${r.capacity ?? 0}${r.unit || "장"}`,
    },
    { key: "used", label: "사용중(장)", type: "number", hideInTable: true },
    { key: "unit", label: "단위", hideInTable: true, placeholder: "장" },
    { key: "status", label: "상태", type: "select", options: STATUS_OPTIONS, render: (r) => statusLabel(r.status) },
    { key: "usage_percent", label: "가동률(%)", type: "number", render: (r) => (r.usage_percent != null ? `${r.usage_percent}%` : "—") },
    { key: "period", label: "사용 기간", placeholder: "예: 2026-06 ~ 2026-12 / 7개월 확정" },
    { key: "owner_member_id", label: "담당자", type: "select", options: ownerOptions, render: (r) => ownerPill(r.owner_member_id) },
    { key: "link", label: "링크(Tailscale 등)", hideInTable: true },
    { key: "notes", label: "메모", type: "textarea", hideInTable: true },
  ];

  const storageFields: FieldSpec<InfraResource>[] = [
    { key: "name", label: "이름", required: true, placeholder: "예: NAS 메인" },
    { key: "model", label: "모델/유형", placeholder: "NAS / S3 / 로컬" },
    { key: "location", label: "위치" },
    {
      key: "capacity",
      label: "용량",
      type: "number",
      render: (r) => `${r.used ?? 0}/${r.capacity ?? 0}${r.unit || "TB"}`,
    },
    { key: "used", label: "사용중", type: "number", hideInTable: true },
    { key: "unit", label: "단위", hideInTable: true, placeholder: "TB" },
    { key: "status", label: "상태", type: "select", options: STATUS_OPTIONS, render: (r) => statusLabel(r.status) },
    { key: "period", label: "사용 기간", placeholder: "예: 2026-06 ~ 2026-12", hideInTable: true },
    { key: "owner_member_id", label: "담당자", type: "select", options: ownerOptions, render: (r) => ownerPill(r.owner_member_id) },
    { key: "notes", label: "메모", type: "textarea", hideInTable: true },
  ];

  const otherFields: FieldSpec<InfraResource>[] = [
    { key: "name", label: "이름", required: true },
    { key: "model", label: "유형/모델" },
    { key: "location", label: "위치" },
    { key: "status", label: "상태", type: "select", options: STATUS_OPTIONS, render: (r) => statusLabel(r.status) },
    { key: "period", label: "사용 기간", placeholder: "예: 2026-06 ~ 2026-12", hideInTable: true },
    { key: "owner_member_id", label: "담당자", type: "select", options: ownerOptions, render: (r) => ownerPill(r.owner_member_id) },
    { key: "notes", label: "메모", type: "textarea", hideInTable: true },
  ];

  // 모니터링 대시보드: link 에 Grafana 등 임베드 가능한 URL을 넣으면 카드로 렌더.
  const dashboardFields: FieldSpec<InfraResource>[] = [
    { key: "name", label: "이름", required: true, placeholder: "예: GPU KPI / 클러스터 개요" },
    { key: "link", label: "Grafana 링크", required: true, placeholder: "http://…/d/<uid>/…" },
    { key: "notes", label: "설명", type: "textarea", placeholder: "이 대시보드가 보여주는 것" },
  ];

  const toInput = (d: Record<string, unknown>) => ({
    name: d.name,
    vendor: orNull(d.vendor),
    model: orNull(d.model),
    location: orNull(d.location),
    status: d.status || "active",
    capacity: numOrNull(d.capacity),
    used: numOrNull(d.used),
    unit: orNull(d.unit),
    usage_percent: numOrNull(d.usage_percent),
    owner_member_id: orNull(d.owner_member_id),
    link: orNull(d.link),
    notes: orNull(d.notes),
    period: orNull(d.period),
    sort_order: 0,
  });

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <ChannelHeader
        glyph="#"
        title="인프라"
        subtitle="GPU·스토리지(NAS)·API 키 등 인프라 자원을 관리합니다."
        right={
          <div className="flex w-full items-center justify-end gap-2 sm:w-auto">
            <ToolbarButton onClick={syncStorage} disabled={syncing}>
              {syncing ? "동기화 중…" : "스토리지 동기화"}
            </ToolbarButton>
            <ToolbarButton tone="accent" onClick={syncGpu} disabled={syncing}>
              {syncing ? "동기화 중…" : "GPU 동기화"}
            </ToolbarButton>
          </div>
        }
      />

      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">{error}</Banner>
        </div>
      ) : null}
      {syncMsg ? (
        <div className="mb-3">
          <Banner tone="pass" title="GPU 동기화">{syncMsg}</Banner>
        </div>
      ) : null}

      {/* GPU 요약 KPI */}
      {loading ? (
        <Card className="mb-4 p-4">
          <Skeleton className="h-5 w-40" />
        </Card>
      ) : summary ? (
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Kpi label="총 GPU" value={`${summary.total}장`} />
          <Kpi label="사용중" value={`${summary.used}장`} tone="attention" />
          <Kpi label="가용(유휴)" value={`${summary.available}장`} strong />
          <Kpi label="장애" value={`${summary.down}장`} />
        </div>
      ) : null}

      {/* GPU 실시간 그래프 (Grafana). 대상 상태와 무관하게 페이지는 항상 렌더:
          임베드 가능 → iframe, 불가 → 링크 폴백, 미설정 → 미표시 */}
      {ml?.grafana_url ? <GrafanaEmbed ml={ml} /> : null}

      <div className="flex flex-col gap-4">
        <CrudSection<InfraResource>
          title="GPU"
          subtitle="A100·H200·B200 등. 장수/사용중/가동률/위치로 남는 자원을 파악합니다."
          fields={gpuFields}
          rows={byKind("gpu")}
          idKey="resource_id"
          canWrite
          makeEmpty={() => ({ status: "active", unit: "장" })}
          toInput={toInput}
          cardRender={(r) => (
            <GpuCard r={r} statusLabel={statusLabel} ownerName={ownerName} />
          )}
          {...handlers("gpu")}
        />
        <CrudSection<InfraResource>
          title="스토리지 / NAS"
          fields={storageFields}
          rows={byKind("storage")}
          idKey="resource_id"
          canWrite
          makeEmpty={() => ({ status: "active", unit: "TB" })}
          toInput={toInput}
          cardRender={(r) => (
            <StorageCard r={r} statusLabel={statusLabel} ownerName={ownerName} />
          )}
          {...handlers("storage")}
        />
        <ServiceGraph
          services={byKind("api_key")}
          providers={providers}
          projects={projects}
          links={links}
          members={members}
          ownerName={ownerName}
          canWrite
          onReload={reload}
        />
        <CrudSection<InfraResource>
          title="서버 / 기타"
          fields={otherFields}
          rows={[...byKind("server"), ...byKind("other")]}
          idKey="resource_id"
          canWrite
          makeEmpty={() => ({ status: "active" })}
          toInput={toInput}
          cardRender={(r) => (
            <ServerCard r={r} statusLabel={statusLabel} ownerName={ownerName} />
          )}
          {...handlers("other")}
        />

        {ml?.configured ? <MlflowSection ml={ml} /> : null}

        <CrudSection<InfraResource>
          title="모니터링 대시보드"
          subtitle="Grafana 등 임베드 가능한 대시보드 링크를 추가하면 여기에 실시간으로 표시됩니다."
          fields={dashboardFields}
          rows={byKind("dashboard")}
          idKey="resource_id"
          canWrite
          makeEmpty={() => ({ status: "active" })}
          toInput={toInput}
          emptyText="추가된 대시보드가 없습니다. ‘+ 추가’로 Grafana 링크를 넣어보세요."
          cardGridClassName="grid grid-cols-1 gap-4 lg:grid-cols-2"
          cardRender={(r) => <DashboardCard r={r} />}
          {...handlers("dashboard")}
        />
      </div>
    </div>
  );
}

// 어떤 Grafana 대시보드 URL이든 임베드용 kiosk URL로 정규화. Grafana가 아닌
// 링크는 그대로 임베드 시도. 파싱 실패 시 원본 반환(깨지지 않음).
function toEmbedSrc(url: string): string {
  try {
    const u = new URL(url);
    if (!u.searchParams.has("theme")) u.searchParams.set("theme", "light");
    if (!u.searchParams.has("refresh")) u.searchParams.set("refresh", "30s");
    let s = u.toString();
    // Grafana kiosk must be a BARE flag (`&kiosk`), not `kiosk=` — the empty-value
    // form does not hide the chrome. URLSearchParams can't emit a bare flag, so
    // append it manually when absent.
    if (!/[?&]kiosk(=|&|$)/.test(s)) s += (s.includes("?") ? "&" : "?") + "kiosk";
    return s;
  } catch {
    return url;
  }
}

function DashboardCard({ r }: { r: InfraResource }) {
  const link = r.link ?? "";
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div
            className="truncate text-[var(--text-body)] font-semibold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {r.name}
          </div>
          {r.notes ? (
            <div
              className="truncate text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {r.notes}
            </div>
          ) : null}
        </div>
        {link ? <DeepLink href={link}>열기 ↗</DeepLink> : null}
      </div>
      {link ? (
        <iframe
          src={toEmbedSrc(link)}
          title={r.name}
          className="w-full rounded-[8px]"
          style={{
            height: 320,
            border: "1px solid var(--color-divider)",
            display: "block",
            background: "var(--color-card)",
          }}
          loading="lazy"
        />
      ) : (
        <div className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          링크가 없습니다.
        </div>
      )}
    </div>
  );
}

function GpuCard({
  r,
  statusLabel,
  ownerName,
}: {
  r: InfraResource;
  statusLabel: (s: string) => string;
  ownerName: (id: string | null) => string;
}) {
  const cap = r.capacity ?? 0;
  const used = r.used ?? 0;
  const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
  const util = r.usage_percent;
  const tone =
    r.status === "down"
      ? "fail"
      : r.status === "reserved"
        ? "warn"
        : r.status === "active"
          ? "pass"
          : "neutral";
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span
          className="truncate text-[var(--text-body)] font-semibold"
          style={{ color: "var(--color-text-strong)" }}
        >
          {r.name}
        </span>
        <Chip tone={tone as never}>{statusLabel(r.status)}</Chip>
      </div>
      <div className="mb-3 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
        {r.model || "—"}
        {r.location ? ` · ${r.location}` : ""}
      </div>

      {/* 장수 사용 막대 */}
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          사용 {used}/{cap}
          {r.unit || "장"}
        </span>
        {util != null ? (
          <span
            className="text-[var(--text-meta)] font-medium"
            style={{ color: "var(--color-text-body)" }}
          >
            가동률 {util}%
          </span>
        ) : null}
      </div>
      <div
        className="h-2 w-full overflow-hidden rounded-full"
        style={{ background: "var(--color-divider)" }}
      >
        <div
          className="h-full rounded-full"
          style={{
            width: `${pct}%`,
            background:
              r.status === "reserved" ? "var(--color-warn-fg)" : "var(--color-accent, #5d9bd8)",
          }}
        />
      </div>

      <div
        className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[var(--text-meta)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        {r.period ? <span>기간 · {r.period}</span> : null}
        <CardOwner name={ownerName(r.owner_member_id)} />
      </div>
    </div>
  );
}

// 상태값 → Chip tone 매핑 (GpuCard와 동일 색 규칙을 카드 전반에서 공유).
function statusTone(s: string): "pass" | "fail" | "warn" | "neutral" {
  return s === "down"
    ? "fail"
    : s === "reserved"
      ? "warn"
      : s === "active"
        ? "pass"
        : "neutral";
}

// 카드 헤더: 이름 + 상태 Chip. hover 수정/삭제는 kit.tsx 오버레이 pill이 처리.
function CardHead({
  name,
  status,
  statusLabel,
}: {
  name: string;
  status: string;
  statusLabel: (s: string) => string;
}) {
  return (
    <div className="mb-1 flex items-center gap-2">
      <span
        className="truncate text-[var(--text-body)] font-semibold"
        style={{ color: "var(--color-text-strong)" }}
      >
        {name}
      </span>
      <Chip tone={statusTone(status)}>{statusLabel(status)}</Chip>
    </div>
  );
}

function CardMeta({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="mb-3 truncate text-[var(--text-meta)]"
      style={{ color: "var(--color-text-muted)" }}
    >
      {children}
    </div>
  );
}

function CardOwner({ name }: { name: string }) {
  return (
    <span
      className="inline-flex items-center gap-1.5 text-[var(--text-meta)]"
      style={{ color: "var(--color-text-muted)" }}
    >
      <span
        className="flex h-5 w-5 items-center justify-center rounded-full text-[var(--text-badge)] font-bold"
        style={{ background: "var(--color-app-canvas)", color: "var(--color-text-body)" }}
        aria-hidden
      >
        {name && name !== "—" ? name.trim().charAt(0).toUpperCase() : "·"}
      </span>
      {name}
    </span>
  );
}

// 링크 버튼이 카드 onClick(편집 모달)까지 트리거하지 않도록 stopPropagation 래핑.
function StopLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <span onClick={(e) => e.stopPropagation()}>
      <DeepLink href={href}>{children}</DeepLink>
    </span>
  );
}

function CapacityBar({
  used,
  total,
  unit,
  tone,
}: {
  used: number;
  total: number;
  unit: string;
  tone?: "warn";
}) {
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  return (
    <>
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          사용 {used}/{total}
          {unit}
        </span>
        <span
          className="text-[var(--text-meta)] font-medium tabular-nums"
          style={{ color: "var(--color-text-body)" }}
        >
          {pct}%
        </span>
      </div>
      <div
        className="h-2 w-full overflow-hidden rounded-full"
        style={{ background: "var(--color-divider)" }}
      >
        <div
          className="h-full rounded-full"
          style={{
            width: `${pct}%`,
            background:
              tone === "warn" ? "var(--color-warn-fg)" : "var(--color-accent, #5d9bd8)",
          }}
        />
      </div>
    </>
  );
}

function StorageCard({
  r,
  statusLabel,
  ownerName,
}: {
  r: InfraResource;
  statusLabel: (s: string) => string;
  ownerName: (id: string | null) => string;
}) {
  return (
    <div>
      <CardHead name={r.name} status={r.status} statusLabel={statusLabel} />
      <CardMeta>
        {r.model || "—"}
        {r.location ? ` · ${r.location}` : ""}
      </CardMeta>
      <CapacityBar used={r.used ?? 0} total={r.capacity ?? 0} unit={r.unit || "TB"} />
      <div className="mt-3">
        <CardOwner name={ownerName(r.owner_member_id)} />
      </div>
    </div>
  );
}

function ServerCard({
  r,
  statusLabel,
  ownerName,
}: {
  r: InfraResource;
  statusLabel: (s: string) => string;
  ownerName: (id: string | null) => string;
}) {
  return (
    <div>
      <CardHead name={r.name} status={r.status} statusLabel={statusLabel} />
      <CardMeta>
        {r.model || "—"}
        {r.location ? ` · ${r.location}` : ""}
      </CardMeta>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <CardOwner name={ownerName(r.owner_member_id)} />
        {r.link ? <StopLink href={r.link}>열기 ↗</StopLink> : null}
      </div>
    </div>
  );
}

function GrafanaEmbed({ ml }: { ml: MlPanel }) {
  const live = ml.grafana_ok;
  return (
    <Card className="mb-4 overflow-hidden p-0">
      <div
        className="flex items-center justify-between px-4 py-3"
        style={{ borderBottom: ml.grafana_embed_url ? "1px solid var(--color-divider)" : "none" }}
      >
        <div className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={
              live
                ? { background: "#22c55e", boxShadow: "0 0 0 3px rgba(34,197,94,0.18)" }
                : { background: "var(--color-text-muted)" }
            }
            aria-hidden
          />
          <h3
            className="text-[var(--text-h3)] font-semibold"
            style={{ color: "var(--color-text-strong)" }}
          >
            GPU 실시간 · Grafana
          </h3>
          <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            {ml.grafana_embed_url ? "5분 평균 · 30초 자동 갱신" : live ? "대시보드 없음" : "연결 불가"}
          </span>
        </div>
        {ml.grafana_url ? <DeepLink href={ml.grafana_url}>전체 화면 ↗</DeepLink> : null}
      </div>
      {ml.grafana_embed_url ? (
        <iframe
          src={ml.grafana_embed_url}
          title="Nova GPU Grafana"
          className="w-full"
          style={{ height: 560, border: "0", display: "block", background: "var(--color-card)" }}
          loading="lazy"
        />
      ) : (
        <div
          className="px-4 py-6 text-[var(--text-body-sm)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {live
            ? "Grafana에 임베드할 GPU 대시보드가 없습니다. ‘전체 화면’에서 직접 확인하세요."
            : "Grafana에 연결할 수 없습니다 (tailnet/서비스 상태 확인). ‘전체 화면’ 링크는 그대로 사용할 수 있습니다."}
        </div>
      )}
    </Card>
  );
}

function DeepLink({ href, children }: { href: string; children: React.ReactNode }) {
  const safeHref = toExternalHref(href);
  if (!safeHref) return null;
  return (
    <a
      href={safeHref}
      target="_blank"
      rel="noopener noreferrer"
      className="orthus-toolbar-button inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)] transition-colors"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        color: "var(--color-text-strong)",
        fontWeight: 500,
        boxShadow: "var(--shadow-toolbar)",
      }}
    >
      {children}
    </a>
  );
}

const ML_STATUS_TONE: Record<string, "pass" | "fail" | "warn" | "neutral"> = {
  FINISHED: "pass",
  FAILED: "fail",
  KILLED: "fail",
  RUNNING: "warn",
  SCHEDULED: "neutral",
};

function MlflowSection({ ml }: { ml: MlPanel }) {
  const fmtTime = (t: number | null) =>
    t ? new Date(t).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" }) : "—";
  return (
    <Card className="p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="text-[var(--text-h3)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
          ML 실험 (MLflow)
        </h3>
        {ml.mlflow_url ? <DeepLink href={ml.mlflow_url}>MLflow 열기 ↗</DeepLink> : null}
      </div>
      {ml.error ? (
        <Banner tone="warn" title="MLflow 연동 오류">{ml.error}</Banner>
      ) : ml.runs.length === 0 ? (
        <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          최근 실험 run이 없습니다.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[var(--text-body-sm)]">
            <thead>
              <tr style={{ color: "var(--color-text-muted)" }} className="text-left">
                <th className="pb-2 pr-3 font-medium">Run</th>
                <th className="pb-2 pr-3 font-medium">실험</th>
                <th className="pb-2 pr-3 font-medium">상태</th>
                <th className="pb-2 pr-3 font-medium">시작</th>
                <th className="pb-2 font-medium">메트릭</th>
              </tr>
            </thead>
            <tbody>
              {ml.runs.map((r) => (
                <tr key={r.run_id} style={{ borderTop: "1px solid var(--color-divider)" }}>
                  <td className="py-2 pr-3">
                    {toExternalHref(r.url) ? (
                      <a
                        href={toExternalHref(r.url)!}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{ color: "var(--color-accent)" }}
                      >
                        {r.run_name}
                      </a>
                    ) : (
                      r.run_name
                    )}
                  </td>
                  <td className="py-2 pr-3" style={{ color: "var(--color-text-body)" }}>
                    {r.experiment_name || "—"}
                  </td>
                  <td className="py-2 pr-3">
                    <Chip tone={ML_STATUS_TONE[r.status] ?? "neutral"}>{r.status || "—"}</Chip>
                  </td>
                  <td className="py-2 pr-3" style={{ color: "var(--color-text-muted)" }}>
                    {fmtTime(r.start_time)}
                  </td>
                  <td className="py-2">
                    <div className="flex flex-wrap gap-1">
                      {Object.entries(r.metrics)
                        .slice(0, 3)
                        .map(([k, v]) => (
                          <Chip key={k} tone="muted">
                            {k}={typeof v === "number" ? Number(v.toFixed(3)) : v}
                          </Chip>
                        ))}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function Kpi({
  label,
  value,
  strong,
  tone,
}: {
  label: string;
  value: string;
  strong?: boolean;
  tone?: "attention";
}) {
  const color = strong
    ? "var(--color-progress)"
    : tone === "attention"
      ? "var(--color-attention)"
      : "var(--color-text-strong)";
  return (
    <Card className="p-3">
      <p className="text-[var(--text-meta)] font-semibold" style={labelStyle()}>{label}</p>
      <p className="mt-1 truncate text-[var(--text-day-heading)] font-extrabold" style={{ color }}>
        {value}
      </p>
    </Card>
  );
}
