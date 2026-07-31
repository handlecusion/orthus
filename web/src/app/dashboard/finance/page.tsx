"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  type ApiKeyEntry,
  type FinanceAccount,
  type FinanceSummary,
  type LedgerEntry,
  type Subscription,
  type TeamMember,
} from "@/lib/api";
import { ChannelHeader, Card, Banner, Skeleton, Avatar, SectionHeader } from "@/components/ui";
import {
  CrudSection,
  type FieldSpec,
  money,
  labelStyle,
  memberColorMap,
} from "@/components/dashboard/kit";

function num(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}
function orNull(v: unknown): string | null {
  const s = String(v ?? "").trim();
  return s === "" ? null : s;
}

export default function FinancePage() {
  const [summary, setSummary] = useState<FinanceSummary | null>(null);
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [keys, setKeys] = useState<ApiKeyEntry[]>([]);
  const [accounts, setAccounts] = useState<FinanceAccount[]>([]);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    const [s, su, k, a, l, m] = await Promise.all([
      api.getFinanceSummary(),
      api.listSubscriptions(),
      api.listApiKeys(),
      api.listFinanceAccounts(),
      api.listLedger(),
      api.listTeamMembers(),
    ]);
    setSummary(s);
    setSubs(su);
    setKeys(k);
    setAccounts(a);
    setLedger(l);
    setMembers(m);
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.getFinanceSummary(),
      api.listSubscriptions(),
      api.listApiKeys(),
      api.listFinanceAccounts(),
      api.listLedger(),
      api.listTeamMembers(),
    ])
      .then(([s, su, k, a, l, m]) => {
        if (!alive) return;
        setSummary(s);
        setSubs(su);
        setKeys(k);
        setAccounts(a);
        setLedger(l);
        setMembers(m);
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
  const ownerRender = (id: string | null) => {
    const name = members.find((m) => m.member_id === id)?.name;
    if (!name) return "—";
    return (
      <span className="inline-flex items-center gap-1.5">
        <Avatar name={name} color={memberColors.get(name) || undefined} size="sm" />
        <span style={{ color: "var(--color-text-body)" }}>{name}</span>
      </span>
    );
  };

  const subFields: FieldSpec<Subscription>[] = [
    { key: "name", label: "이름", required: true },
    { key: "vendor", label: "공급사" },
    { key: "plan", label: "플랜", hideInTable: true },
    {
      key: "billing_cycle",
      label: "주기",
      type: "select",
      options: [
        { value: "monthly", label: "월간" },
        { value: "yearly", label: "연간" },
        { value: "one_time", label: "일회성" },
      ],
    },
    { key: "amount", label: "금액", type: "number", render: (r) => money(r.amount, r.currency) },
    {
      key: "status",
      label: "상태",
      type: "select",
      options: [
        { value: "active", label: "활성" },
        { value: "trial", label: "체험" },
        { value: "paused", label: "일시중지" },
        { value: "canceled", label: "해지" },
      ],
    },
    { key: "next_billing_date", label: "다음 결제일", type: "date" },
    { key: "category", label: "카테고리", hideInTable: true },
    {
      key: "owner_member_id",
      label: "담당자",
      type: "select",
      options: ownerOptions,
      render: (r) => ownerRender(r.owner_member_id),
    },
    { key: "masked_account", label: "결제수단(마스킹)", hideInTable: true, placeholder: "•••• 1234" },
    { key: "notes", label: "메모", type: "textarea", hideInTable: true },
  ];

  const keyFields: FieldSpec<ApiKeyEntry>[] = [
    { key: "service_name", label: "서비스", required: true },
    { key: "label", label: "라벨" },
    { key: "key_last4", label: "키 끝 4자리", placeholder: "마지막 4자리만", render: (r) => (r.key_last4 ? `•••• ${r.key_last4}` : "—") },
    {
      key: "environment",
      label: "환경",
      type: "select",
      options: [
        { value: "prod", label: "prod" },
        { value: "dev", label: "dev" },
        { value: "staging", label: "staging" },
      ],
    },
    { key: "monthly_cost", label: "월 비용", type: "number", render: (r) => money(r.monthly_cost, r.currency) },
    {
      key: "status",
      label: "상태",
      type: "select",
      options: [
        { value: "active", label: "활성" },
        { value: "revoked", label: "폐기" },
        { value: "expired", label: "만료" },
      ],
    },
    { key: "rotated_at", label: "교체일", type: "date", hideInTable: true },
    {
      key: "owner_member_id",
      label: "담당자",
      type: "select",
      options: ownerOptions,
      render: (r) => ownerRender(r.owner_member_id),
    },
    { key: "notes", label: "메모", type: "textarea", hideInTable: true },
  ];

  const accountFields: FieldSpec<FinanceAccount>[] = [
    { key: "account_name", label: "계정명", required: true },
    {
      key: "kind",
      label: "종류",
      type: "select",
      options: [
        { value: "login", label: "로그인" },
        { value: "saas", label: "SaaS" },
        { value: "bank", label: "은행" },
        { value: "card", label: "카드" },
        { value: "other", label: "기타" },
      ],
    },
    { key: "masked_identifier", label: "식별자(마스킹)", placeholder: "예: user@***, •••• 1234" },
    {
      key: "owner_member_id",
      label: "담당자",
      type: "select",
      options: ownerOptions,
      render: (r) => ownerRender(r.owner_member_id),
    },
    { key: "notes", label: "메모", type: "textarea", hideInTable: true },
  ];

  const ledgerFields: FieldSpec<LedgerEntry>[] = [
    { key: "entry_date", label: "날짜", type: "date", required: true },
    {
      key: "entry_type",
      label: "구분",
      type: "select",
      options: [
        { value: "revenue", label: "매출" },
        { value: "expense", label: "지출" },
      ],
      render: (r) => (r.entry_type === "revenue" ? "매출" : "지출"),
    },
    { key: "amount", label: "금액", type: "number", render: (r) => money(r.amount, r.currency) },
    { key: "category", label: "카테고리" },
    { key: "description", label: "내용" },
  ];

  const upcoming = subs
    .filter((s) => s.next_billing_date && (s.status === "active" || s.status === "trial"))
    .sort((a, b) => (a.next_billing_date! < b.next_billing_date! ? -1 : 1))
    .slice(0, 6);

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <ChannelHeader glyph="#" title="자금관리" subtitle="구독·API 키·계정·매출/지출을 관리합니다. 시크릿은 마스킹 메타데이터만 저장됩니다." />

      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">{error}</Banner>
        </div>
      ) : null}

      {/* 요약 KPI */}
      {loading ? (
        <Card className="mb-4 p-4">
          <Skeleton className="h-5 w-40" />
        </Card>
      ) : summary ? (
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          <Kpi label="잔액" value={money(summary.balance, summary.currency)} strong />
          <Kpi
            label="번레이트 (월 고정지출)"
            value={money(summary.monthly_burn, summary.currency)}
            tone="attention"
          />
          <Kpi
            label="런웨이"
            value={summary.runway_months != null ? `${summary.runway_months}개월` : "—"}
          />
          <Kpi label="총 매출" value={money(summary.total_revenue, summary.currency)} />
          <Kpi label="총 지출" value={money(summary.total_expense, summary.currency)} />
          <Kpi label="월 구독비" value={money(summary.monthly_subscription_cost, summary.currency)} />
          <Kpi label="월 API 비용" value={money(summary.monthly_api_cost, summary.currency)} />
          <Kpi label="활성 구독" value={`${summary.active_subscription_count}건`} />
        </div>
      ) : null}

      {/* 예정 결제 (구독 next_billing_date) */}
      {upcoming.length > 0 ? (
        <Card className="mb-4 p-3 sm:p-4">
          <SectionHeader label="예정된 결제" count={upcoming.length} />
          <ul className="flex flex-col gap-1.5">
            {upcoming.map((s) => (
              <li key={s.sub_id} className="flex items-center justify-between gap-2 text-[var(--text-body-sm)]">
                <span style={{ color: "var(--color-text-body)" }}>
                  <span className="font-mono" style={labelStyle()}>{s.next_billing_date}</span>{"  "}
                  {s.name}
                </span>
                <span style={{ color: "var(--color-text-strong)" }}>{money(s.amount, s.currency)}</span>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      <div className="flex flex-col gap-4">
        <CrudSection<Subscription>
          title="구독"
          fields={subFields}
          rows={subs}
          idKey="sub_id"
          canWrite
          makeEmpty={() => ({ billing_cycle: "monthly", status: "active", currency: "KRW", amount: 0 })}
          toInput={(d) => ({
            name: d.name, vendor: orNull(d.vendor), plan: orNull(d.plan),
            billing_cycle: d.billing_cycle || "monthly", amount: num(d.amount),
            currency: d.currency || "KRW", next_billing_date: orNull(d.next_billing_date),
            status: d.status || "active", category: orNull(d.category),
            owner_member_id: orNull(d.owner_member_id), masked_account: orNull(d.masked_account),
            notes: orNull(d.notes),
          })}
          onCreate={async (i) => { await api.createSubscription(i as never); await reload(); }}
          onUpdate={async (id, i) => { await api.updateSubscription(id, i as never); await reload(); }}
          onDelete={async (id) => { await api.deleteSubscription(id); await reload(); }}
        />

        <CrudSection<ApiKeyEntry>
          title="API 키"
          subtitle="실제 키 값은 저장하지 않습니다 — 끝 4자리/비용/담당자 등 메타데이터만 관리합니다."
          fields={keyFields}
          rows={keys}
          idKey="key_id"
          canWrite
          makeEmpty={() => ({ environment: "prod", status: "active", currency: "KRW", monthly_cost: 0 })}
          toInput={(d) => ({
            service_name: d.service_name, label: orNull(d.label), key_last4: orNull(d.key_last4),
            environment: d.environment || "prod", monthly_cost: num(d.monthly_cost),
            currency: d.currency || "KRW", status: d.status || "active",
            rotated_at: orNull(d.rotated_at), owner_member_id: orNull(d.owner_member_id),
            notes: orNull(d.notes),
          })}
          onCreate={async (i) => { await api.createApiKey(i as never); await reload(); }}
          onUpdate={async (id, i) => { await api.updateApiKey(id, i as never); await reload(); }}
          onDelete={async (id) => { await api.deleteApiKey(id); await reload(); }}
        />

        <CrudSection<FinanceAccount>
          title="계정"
          fields={accountFields}
          rows={accounts}
          idKey="account_id"
          canWrite
          makeEmpty={() => ({ kind: "login" })}
          toInput={(d) => ({
            account_name: d.account_name, kind: d.kind || "login",
            masked_identifier: orNull(d.masked_identifier),
            owner_member_id: orNull(d.owner_member_id), notes: orNull(d.notes),
          })}
          onCreate={async (i) => { await api.createFinanceAccount(i as never); await reload(); }}
          onUpdate={async (id, i) => { await api.updateFinanceAccount(id, i as never); await reload(); }}
          onDelete={async (id) => { await api.deleteFinanceAccount(id); await reload(); }}
        />

        <CrudSection<LedgerEntry>
          title="매출 / 지출"
          subtitle="여기 입력한 매출·지출로 잔액과 매출현황이 계산됩니다."
          fields={ledgerFields}
          rows={ledger}
          idKey="ledger_id"
          canWrite
          makeEmpty={() => ({ entry_type: "revenue", currency: "KRW", amount: 0 })}
          toInput={(d) => ({
            entry_date: d.entry_date, entry_type: d.entry_type || "revenue",
            amount: num(d.amount), currency: d.currency || "KRW",
            category: orNull(d.category), description: orNull(d.description),
            project_id: null,
          })}
          onCreate={async (i) => { await api.createLedger(i as never); await reload(); }}
          onUpdate={async (id, i) => { await api.updateLedger(id, i as never); await reload(); }}
          onDelete={async (id) => { await api.deleteLedger(id); await reload(); }}
        />
      </div>
    </div>
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
