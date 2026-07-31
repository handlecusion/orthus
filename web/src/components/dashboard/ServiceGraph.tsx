"use client";

/**
 * Service ↔ Project bipartite graph (N:M).
 *
 * Left  = 제공처(provider) groups (e.g. "APick", 잔여 ₩40,000) holding lightweight
 *         service rows (e.g. "실명인증 · 30원/건"). Each service carries a line color.
 * Right = dashboard projects.
 * Lines = straight connectors, colored per service.
 *
 * Click a service to enter link mode (click projects to toggle). Hover any node
 * (service or project) to highlight what it connects to. 제공처 owns the balance;
 * 단가/잔여 can be ₩ or $.
 */

import * as React from "react";
import {
  api,
  type DashboardProject,
  type InfraProvider,
  type InfraResource,
  type InfraResourceLink,
  type TeamMember,
} from "@/lib/api";
import { Card, Chip, ToolbarButton } from "@/components/ui";
import { Field, Modal, TagSelect, TextInput, labelStyle } from "@/components/dashboard/kit";
import { toExternalHref } from "@/lib/external-url";

const DEFAULT_LINE = "#9aa4b2";
const SWATCHES = [
  "#5d9bd8", "#62c4ba", "#7bbf6a", "#efc47b",
  "#e9897e", "#d77fb0", "#9b68d6", "#7a8aa0",
];

function money(n: number | null | undefined, currency: string | null): string {
  if (n == null) return "—";
  const s = Math.round(n).toLocaleString("ko-KR");
  return currency === "USD" ? `$${s}` : `₩${s}`;
}

const PRICE_UNITS = [
  { value: "per_call", label: "건당" },
  { value: "per_1m", label: "100만당" },
  { value: "monthly", label: "월 고정" },
  { value: "hourly", label: "시간당" },
  { value: "other", label: "기타" },
];
const UNIT_SUFFIX: Record<string, string> = {
  per_call: "/건",
  per_1m: "/1M",
  hourly: "/시간",
};
function priceLabel(s: InfraResource): string {
  if (s.unit_price == null) return "단가 미정";
  const m = money(s.unit_price, s.currency);
  if (s.price_unit === "monthly") return `월 ${m}`;
  return `${m}${UNIT_SUFFIX[s.price_unit ?? ""] ?? ""}`;
}

const STATUSES = [
  { value: "active", label: "사용중" },
  { value: "idle", label: "유휴" },
  { value: "reserved", label: "예약" },
  { value: "down", label: "장애" },
  { value: "expired", label: "만료" },
];
const CURRENCIES = [
  { value: "KRW", label: "₩" },
  { value: "USD", label: "$" },
];

function Segmented({
  value,
  options,
  onChange,
}: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <div
      className="inline-flex w-full gap-0.5 rounded-[9px] p-0.5"
      style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider)" }}
    >
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            onClick={() => onChange(o.value)}
            className="flex-1 rounded-[7px] px-2 py-1.5 text-[var(--text-body-sm)] font-medium transition-colors"
            style={{
              background: on ? "var(--color-card)" : "transparent",
              color: on ? "var(--color-text-strong)" : "var(--color-text-muted)",
              boxShadow: on ? "var(--shadow-card, 0 1px 2px rgba(0,0,0,0.06))" : "none",
            }}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

type Hover = { kind: "s" | "p"; id: string } | null;
type Edge = { key: string; rid: string; pid: string; color: string; x1: number; y1: number; x2: number; y2: number };

export function ServiceGraph({
  services,
  providers,
  projects,
  links,
  members,
  ownerName,
  canWrite,
  onReload,
}: {
  services: InfraResource[];
  providers: InfraProvider[];
  projects: DashboardProject[];
  links: InfraResourceLink[];
  members: TeamMember[];
  ownerName: (id: string | null) => string;
  canWrite: boolean;
  onReload: () => Promise<void>;
}) {
  const [selected, setSelected] = React.useState<string | null>(null);
  const [hover, setHover] = React.useState<Hover>(null);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);

  const providerById = new Map(providers.map((p) => [p.provider_id, p]));
  const serviceById = new Map(services.map((s) => [s.resource_id, s]));
  const servicesOf = (pid: string | null) =>
    services.filter((s) => (s.provider_id ?? null) === pid);
  const ungrouped = servicesOf(null);

  const linkByPair = React.useMemo(() => {
    const m = new Map<string, string>();
    for (const l of links) m.set(`${l.resource_id}|${l.project_id}`, l.link_id);
    return m;
  }, [links]);
  const isLinked = (rid: string, pid: string) => linkByPair.has(`${rid}|${pid}`);

  // ----- emphasis (hover OR selection) -----
  const linkLit = (rid: string, pid: string) => {
    if (selected && rid === selected) return true;
    if (!hover) return false;
    return hover.kind === "s" ? rid === hover.id : pid === hover.id;
  };
  const litS = new Set<string>();
  const litP = new Set<string>();
  for (const l of links) {
    if (linkLit(l.resource_id, l.project_id)) {
      litS.add(l.resource_id);
      litP.add(l.project_id);
    }
  }
  if (hover?.kind === "s") litS.add(hover.id);
  if (hover?.kind === "p") litP.add(hover.id);
  if (selected) litS.add(selected);
  const emphasis = hover != null || selected != null;
  const sDim = (id: string) => emphasis && !litS.has(id);
  const pDim = (id: string) => emphasis && !litP.has(id);

  // ----- SVG edges (straight) -----
  const wrapRef = React.useRef<HTMLDivElement>(null);
  const nodeRefs = React.useRef<Map<string, HTMLElement>>(new Map());
  const setNodeRef = (key: string) => (el: HTMLElement | null) => {
    if (el) nodeRefs.current.set(key, el);
    else nodeRefs.current.delete(key);
  };
  const [edges, setEdges] = React.useState<Edge[]>([]);
  const measure = React.useCallback(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const wb = wrap.getBoundingClientRect();
    const next: Edge[] = [];
    for (const l of links) {
      const s = nodeRefs.current.get(`s:${l.resource_id}`);
      const p = nodeRefs.current.get(`p:${l.project_id}`);
      if (!s || !p) continue;
      const sb = s.getBoundingClientRect();
      const pb = p.getBoundingClientRect();
      next.push({
        key: l.link_id,
        rid: l.resource_id,
        pid: l.project_id,
        color: serviceById.get(l.resource_id)?.color || DEFAULT_LINE,
        x1: sb.right - wb.left,
        y1: sb.top - wb.top + sb.height / 2,
        x2: pb.left - wb.left,
        y2: pb.top - wb.top + pb.height / 2,
      });
    }
    setEdges(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [links, services]);
  React.useLayoutEffect(() => {
    measure();
  }, [measure, services, providers, projects]);
  React.useEffect(() => {
    const ro = new ResizeObserver(() => measure());
    if (wrapRef.current) ro.observe(wrapRef.current);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [measure]);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      await onReload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "실패");
    } finally {
      setBusy(false);
    }
  }
  function toggleLink(rid: string, pid: string) {
    if (!canWrite) return;
    const existing = linkByPair.get(`${rid}|${pid}`);
    void run(() =>
      existing
        ? api.deleteInfraLink(existing)
        : api.addInfraLink({ resource_id: rid, project_id: pid }),
    );
  }

  const num = (v: string) => {
    const t = (v ?? "").trim();
    if (t === "") return null;
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
  };
  const str = (v: string | undefined) => {
    const t = (v ?? "").trim();
    return t === "" ? null : t;
  };

  // ----- service modal -----
  const [svcOpen, setSvcOpen] = React.useState(false);
  const [svcEditId, setSvcEditId] = React.useState<string | null>(null);
  const [svc, setSvc] = React.useState<Record<string, string>>({});
  const sF = (k: string, v: string) => setSvc((d) => ({ ...d, [k]: v }));
  function openSvcAdd(providerName = "") {
    setSvcEditId(null);
    setSvc({
      provider_name: providerName,
      status: "active",
      price_unit: "per_call",
      currency: "KRW",
      color: SWATCHES[services.length % SWATCHES.length],
      owner_member_id: "",
    });
    setErr(null);
    setSvcOpen(true);
  }
  function openSvcEdit(s: InfraResource) {
    setSvcEditId(s.resource_id);
    setSvc({
      name: s.name,
      provider_name: s.provider_id ? providerById.get(s.provider_id)?.name ?? "" : "",
      unit_price: s.unit_price != null ? String(s.unit_price) : "",
      price_unit: s.price_unit ?? "per_call",
      currency: s.currency ?? "KRW",
      color: s.color ?? DEFAULT_LINE,
      status: s.status,
      owner_member_id: s.owner_member_id ?? "",
      link: s.link ?? "",
    });
    setErr(null);
    setSvcOpen(true);
  }
  async function resolveProviderId(name: string): Promise<string | null> {
    const n = name.trim();
    if (!n) return null;
    const existing = providers.find((p) => p.name === n);
    if (existing) return existing.provider_id;
    const created = await api.createInfraProvider({
      name: n, balance: null, currency: "KRW", link: null, notes: null, sort_order: providers.length,
    });
    return created.provider_id;
  }
  function saveSvc() {
    const name = (svc.name ?? "").trim();
    if (!name) {
      setErr("이름을 입력하세요");
      return;
    }
    void run(async () => {
      const provider_id = await resolveProviderId(svc.provider_name ?? "");
      const body = {
        kind: "api_key" as const,
        name,
        vendor: null,
        model: null,
        location: null,
        status: (svc.status || "active") as InfraResource["status"],
        capacity: null,
        used: null,
        unit: null,
        usage_percent: null,
        owner_member_id: str(svc.owner_member_id),
        link: str(svc.link),
        notes: null,
        period: null,
        parent_id: null,
        unit_price: num(svc.unit_price ?? ""),
        balance: null,
        provider_id,
        price_unit: str(svc.price_unit) ?? "per_call",
        color: str(svc.color),
        currency: str(svc.currency) ?? "KRW",
        sort_order: 0,
      };
      if (svcEditId) await api.updateInfra(svcEditId, body);
      else await api.createInfra(body);
      setSvcOpen(false);
    });
  }
  function deleteSvc(id: string) {
    if (!window.confirm("이 서비스를 삭제하시겠습니까? 연결도 함께 삭제됩니다.")) return;
    void run(async () => {
      await api.deleteInfra(id);
      setSvcOpen(false);
    });
  }

  // ----- provider modal -----
  const [provOpen, setProvOpen] = React.useState(false);
  const [provEditId, setProvEditId] = React.useState<string | null>(null);
  const [prov, setProv] = React.useState<Record<string, string>>({});
  const pF = (k: string, v: string) => setProv((d) => ({ ...d, [k]: v }));
  function openProvAdd() {
    setProvEditId(null);
    setProv({ currency: "KRW" });
    setErr(null);
    setProvOpen(true);
  }
  function openProvEdit(p: InfraProvider) {
    setProvEditId(p.provider_id);
    setProv({
      name: p.name,
      balance: p.balance != null ? String(p.balance) : "",
      currency: p.currency ?? "KRW",
      link: p.link ?? "",
    });
    setErr(null);
    setProvOpen(true);
  }
  function saveProv() {
    const name = (prov.name ?? "").trim();
    if (!name) {
      setErr("제공처 이름을 입력하세요");
      return;
    }
    void run(async () => {
      const body = {
        name,
        balance: num(prov.balance ?? ""),
        currency: str(prov.currency) ?? "KRW",
        link: str(prov.link),
        notes: null,
        sort_order: providers.length,
      };
      if (provEditId) await api.updateInfraProvider(provEditId, body);
      else await api.createInfraProvider(body);
      setProvOpen(false);
    });
  }
  function deleteProv(id: string) {
    if (!window.confirm("제공처를 삭제하시겠습니까? 소속 서비스는 미분류로 남습니다.")) return;
    void run(async () => {
      await api.deleteInfraProvider(id);
      setProvOpen(false);
    });
  }

  // ----- inline project add -----
  const [addingProject, setAddingProject] = React.useState(false);
  const [projName, setProjName] = React.useState("");
  function addProject() {
    const name = projName.trim();
    if (!name) return;
    void run(async () => {
      await api.createDashboardProject({ name, color: null, sort_order: projects.length, active: true });
      setProjName("");
      setAddingProject(false);
    });
  }

  // ----- service row (flat, light) -----
  function ServiceRow({ s }: { s: InfraResource }) {
    const active = selected === s.resource_id;
    const color = s.color || DEFAULT_LINE;
    return (
      <div
        ref={setNodeRef(`s:${s.resource_id}`)}
        onMouseEnter={() => setHover({ kind: "s", id: s.resource_id })}
        onMouseLeave={() => setHover(null)}
        onClick={() => setSelected(active ? null : s.resource_id)}
        className="group/svc relative flex cursor-pointer items-center gap-2 rounded-[7px] px-2 py-1.5"
        style={{
          background: active ? "var(--color-card)" : "transparent",
          boxShadow: active ? `inset 0 0 0 1.5px ${color}` : "none",
          opacity: sDim(s.resource_id) ? 0.4 : 1,
        }}
      >
        <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: color }} aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[var(--text-body-sm)] font-medium" style={{ color: "var(--color-text-strong)" }}>
            {s.name}
          </span>
          <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            {priceLabel(s)}
            {s.owner_member_id ? ` · ${ownerName(s.owner_member_id)}` : ""}
          </span>
        </span>
        {toExternalHref(s.link) ? (
          <a
            href={toExternalHref(s.link)!}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="shrink-0 text-[var(--text-meta)] font-medium"
            style={{ color: "var(--color-text-muted)" }}
          >
            열기 ↗
          </a>
        ) : null}
        <Chip tone={s.status === "active" ? "pass" : s.status === "down" ? "fail" : "neutral"}>
          {STATUSES.find((x) => x.value === s.status)?.label ?? s.status}
        </Chip>
        {canWrite ? (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); openSvcEdit(s); }}
            className="absolute right-1 top-1 hidden rounded-[5px] px-1 text-[var(--text-meta)] group-hover/svc:block"
            style={{ background: "var(--color-card)", color: "var(--color-text-muted)", boxShadow: "var(--shadow-card)" }}
          >
            편집
          </button>
        ) : null}
      </div>
    );
  }

  const hasAny = services.length > 0 || providers.length > 0 || projects.length > 0;

  return (
    <Card className="p-3 sm:p-4">
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-[var(--text-body)] font-bold" style={{ color: "var(--color-text-strong)" }}>
            제공처 · 서비스 ↔ 프로젝트
          </h2>
          <p className="text-[var(--text-meta)]" style={labelStyle()}>
            서비스를 클릭한 뒤 프로젝트를 눌러 연결을 켜고 끕니다. 카드에 마우스를 올리면 연결이 강조됩니다.
          </p>
        </div>
        {canWrite ? (
          <div className="flex shrink-0 items-center gap-2">
            <ToolbarButton onClick={openProvAdd} disabled={busy}>+ 제공처</ToolbarButton>
            <ToolbarButton tone="primary" onClick={() => openSvcAdd()} disabled={busy}>+ 서비스</ToolbarButton>
          </div>
        ) : null}
      </div>

      {err ? <p className="mb-2 text-[var(--text-body-sm)]" style={{ color: "var(--color-fail-fg)" }}>{err}</p> : null}

      {!hasAny ? (
        <p className="py-6 text-center text-[var(--text-body-sm)]" style={labelStyle()}>
          제공처·서비스·프로젝트를 추가하면 연결 그래프가 표시됩니다.
        </p>
      ) : (
        <div ref={wrapRef} className="relative grid grid-cols-2 gap-x-10 sm:gap-x-16">
          <svg className="pointer-events-none absolute inset-0 h-full w-full" style={{ zIndex: 0 }}>
            {edges.map((e) => {
              const lit = linkLit(e.rid, e.pid);
              return (
                <line
                  key={e.key}
                  x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2}
                  stroke={e.color}
                  strokeWidth={lit ? 3 : 1.75}
                  strokeLinecap="round"
                  opacity={emphasis ? (lit ? 1 : 0.12) : 0.85}
                />
              );
            })}
          </svg>

          {/* left: providers + services */}
          <div className="relative flex flex-col gap-3" style={{ zIndex: 1 }}>
            {providers.map((p) => (
              <div
                key={p.provider_id}
                className="group/prov relative rounded-[10px] px-1.5 py-1.5"
                style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider)" }}
              >
                <div className="mb-1 flex items-center justify-between gap-2 px-1">
                  <span className="truncate text-[var(--text-body-sm)] font-bold" style={{ color: "var(--color-text-strong)" }}>
                    {p.name}
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    {toExternalHref(p.link) ? (
                      <a
                        href={toExternalHref(p.link)!}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-[var(--text-meta)] font-medium"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        열기 ↗
                      </a>
                    ) : null}
                    <span className="shrink-0 text-[var(--text-meta)] font-medium tabular-nums"
                      style={{ color: p.balance != null ? "var(--color-text-body)" : "var(--color-text-faint)" }}>
                      잔여 {money(p.balance, p.currency)}
                    </span>
                  </span>
                </div>
                <div className="flex flex-col gap-0.5">
                  {servicesOf(p.provider_id).map((s) => <ServiceRow key={s.resource_id} s={s} />)}
                  {canWrite ? (
                    <button type="button" onClick={() => openSvcAdd(p.name)}
                      className="rounded-[7px] px-2 py-1 text-left text-[var(--text-meta)] hover:bg-[var(--color-card)]"
                      style={{ color: "var(--color-text-muted)" }}>
                      + 서비스
                    </button>
                  ) : null}
                </div>
                {canWrite ? (
                  <button type="button" onClick={() => openProvEdit(p)}
                    className="absolute right-1 top-1 hidden rounded-[5px] px-1 text-[var(--text-meta)] group-hover/prov:block"
                    style={{ background: "var(--color-card)", color: "var(--color-text-muted)", boxShadow: "var(--shadow-card)" }}>
                    편집
                  </button>
                ) : null}
              </div>
            ))}
            {ungrouped.length > 0 ? (
              <div className="flex flex-col gap-0.5">
                {providers.length > 0 ? <span className="px-1 text-[var(--text-meta)]" style={labelStyle()}>미분류</span> : null}
                {ungrouped.map((s) => <ServiceRow key={s.resource_id} s={s} />)}
              </div>
            ) : null}
          </div>

          {/* right: projects */}
          <div className="relative flex flex-col gap-2" style={{ zIndex: 1 }}>
            {projects.map((p) => {
              const linked = selected ? isLinked(selected, p.project_id) : false;
              const clickable = canWrite && selected != null;
              return (
                <div
                  key={p.project_id}
                  ref={setNodeRef(`p:${p.project_id}`)}
                  role="button"
                  tabIndex={0}
                  onMouseEnter={() => setHover({ kind: "p", id: p.project_id })}
                  onMouseLeave={() => setHover(null)}
                  onClick={() => selected && toggleLink(selected, p.project_id)}
                  className="flex items-center justify-between gap-2 rounded-[8px] px-3 py-2 text-left"
                  style={{
                    background: linked ? "color-mix(in srgb, var(--color-accent, #5d9bd8) 12%, var(--color-card))" : "var(--color-card)",
                    border: `1px solid ${linked ? "var(--color-accent, #5d9bd8)" : "var(--color-divider)"}`,
                    cursor: clickable ? "pointer" : "default",
                    opacity: pDim(p.project_id) ? 0.4 : 1,
                  }}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: p.color || "var(--color-text-muted)" }} aria-hidden />
                    <span className="truncate text-[var(--text-body-sm)] font-medium" style={{ color: "var(--color-text-strong)" }}>
                      {p.name}
                    </span>
                  </span>
                  {clickable ? (
                    <span className="shrink-0 text-[var(--text-meta)] font-bold"
                      style={{ color: linked ? "var(--color-accent, #5d9bd8)" : "var(--color-text-faint)" }}>
                      {linked ? "✓" : "+"}
                    </span>
                  ) : null}
                </div>
              );
            })}
            {canWrite ? (
              addingProject ? (
                <div className="flex items-center gap-1.5">
                  <TextInput autoFocus className="min-w-0 flex-1" value={projName} placeholder="프로젝트 이름"
                    onChange={(e) => setProjName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addProject();
                      if (e.key === "Escape") { setProjName(""); setAddingProject(false); }
                    }} />
                  <ToolbarButton tone="primary" className="shrink-0 whitespace-nowrap" onClick={addProject} disabled={busy}>확인</ToolbarButton>
                  <ToolbarButton className="shrink-0 whitespace-nowrap" onClick={() => { setProjName(""); setAddingProject(false); }} disabled={busy}>취소</ToolbarButton>
                </div>
              ) : (
                <button type="button" onClick={() => setAddingProject(true)}
                  className="rounded-[8px] border border-dashed px-3 py-2 text-left text-[var(--text-meta)]"
                  style={{ borderColor: "var(--color-divider)", color: "var(--color-text-muted)" }}>
                  + 프로젝트
                </button>
              )
            ) : null}
          </div>
        </div>
      )}

      {selected && canWrite ? (
        <p className="mt-3 text-[var(--text-meta)]" style={labelStyle()}>
          선택됨 · 오른쪽 프로젝트를 눌러 연결/해제 · 다시 클릭하면 선택 해제
        </p>
      ) : null}

      {/* service modal */}
      <Modal
        open={svcOpen}
        title={svcEditId ? "서비스 수정" : "서비스 추가"}
        onClose={() => setSvcOpen(false)}
        footer={
          <>
            {svcEditId ? <ToolbarButton onClick={() => deleteSvc(svcEditId)} disabled={busy}>삭제</ToolbarButton> : null}
            <ToolbarButton onClick={() => setSvcOpen(false)} disabled={busy}>취소</ToolbarButton>
            <ToolbarButton tone="primary" onClick={saveSvc} disabled={busy}>{busy ? "저장 중…" : "저장"}</ToolbarButton>
          </>
        }
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="이름" full>
            <TextInput value={svc.name ?? ""} placeholder="예: 실명인증" onChange={(e) => sF("name", e.target.value)} />
          </Field>
          <Field label="제공처">
            <TagSelect value={svc.provider_name ?? ""} options={providers.map((p) => p.name)}
              onChange={(v) => sF("provider_name", v)} allowCustom placeholder="제공처 선택 또는 입력" />
          </Field>
          <Field label="통화">
            <Segmented value={svc.currency ?? "KRW"} options={CURRENCIES} onChange={(v) => sF("currency", v)} />
          </Field>
          <Field label="단가">
            <TextInput type="number" value={svc.unit_price ?? ""} placeholder="예: 30" onChange={(e) => sF("unit_price", e.target.value)} />
          </Field>
          <Field label="단가 기준">
            <Segmented value={svc.price_unit ?? "per_call"} options={PRICE_UNITS} onChange={(v) => sF("price_unit", v)} />
          </Field>
          <Field label="상태" full>
            <Segmented value={svc.status ?? "active"} options={STATUSES} onChange={(v) => sF("status", v)} />
          </Field>
          <Field label="담당자" full>
            <div className="flex flex-wrap gap-1.5">
              <MemberChip label="없음" color={null} on={!svc.owner_member_id} onClick={() => sF("owner_member_id", "")} />
              {members.map((m) => (
                <MemberChip key={m.member_id} label={m.name} color={m.color}
                  on={svc.owner_member_id === m.member_id}
                  onClick={() => sF("owner_member_id", m.member_id)} />
              ))}
            </div>
          </Field>
          <Field label="선 색" full>
            <div className="flex flex-wrap gap-2">
              {SWATCHES.map((c) => {
                const on = (svc.color ?? "") === c;
                return (
                  <button key={c} type="button" onClick={() => sF("color", c)}
                    className="h-7 w-7 rounded-full"
                    style={{ background: c, boxShadow: on ? "0 0 0 2px var(--color-card), 0 0 0 4px " + c : "none" }}
                    aria-label={c} />
                );
              })}
            </div>
          </Field>
          <Field label="링크/콘솔" full>
            <TextInput value={svc.link ?? ""} onChange={(e) => sF("link", e.target.value)} />
          </Field>
        </div>
      </Modal>

      {/* provider modal */}
      <Modal
        open={provOpen}
        title={provEditId ? "제공처 수정" : "제공처 추가"}
        onClose={() => setProvOpen(false)}
        footer={
          <>
            {provEditId ? <ToolbarButton onClick={() => deleteProv(provEditId)} disabled={busy}>삭제</ToolbarButton> : null}
            <ToolbarButton onClick={() => setProvOpen(false)} disabled={busy}>취소</ToolbarButton>
            <ToolbarButton tone="primary" onClick={saveProv} disabled={busy}>{busy ? "저장 중…" : "저장"}</ToolbarButton>
          </>
        }
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="제공처 이름" full>
            <TextInput value={prov.name ?? ""} placeholder="예: APick" onChange={(e) => pF("name", e.target.value)} />
          </Field>
          <Field label="통화">
            <Segmented value={prov.currency ?? "KRW"} options={CURRENCIES} onChange={(v) => pF("currency", v)} />
          </Field>
          <Field label="잔여량">
            <TextInput type="number" value={prov.balance ?? ""} placeholder="예: 40000" onChange={(e) => pF("balance", e.target.value)} />
          </Field>
          <Field label="링크/콘솔" full>
            <TextInput value={prov.link ?? ""} onChange={(e) => pF("link", e.target.value)} />
          </Field>
        </div>
      </Modal>
    </Card>
  );
}

function MemberChip({
  label,
  color,
  on,
  onClick,
}: {
  label: string;
  color: string | null;
  on: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[var(--text-body-sm)] font-medium transition-colors"
      style={{
        background: on ? "var(--color-app-canvas)" : "var(--color-card)",
        border: `1px solid ${on ? "var(--color-text-strong)" : "var(--color-divider)"}`,
        color: "var(--color-text-strong)",
      }}
    >
      <span className="h-2.5 w-2.5 rounded-full" style={{ background: color || "var(--color-text-faint)" }} aria-hidden />
      {label}
    </button>
  );
}
