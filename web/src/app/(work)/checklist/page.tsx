"use client";

import { Check, ListChecks, MessageSquare, Pencil, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import {
  api,
  type AgentWorkItem,
  type AgentWorkReviewAction,
  type AgentWorkReviewDecision,
  type AgentWorkState,
} from "@/lib/api";
import {
  Banner,
  Card,
  Chip,
  ModeBadge,
  PageHeader,
  Skeleton,
  Toolbar,
  ToolbarButton,
} from "@/components/ui";
import { refreshAgentWorkBadge } from "@/components/AppShell";
import { useCompactAgentWorkShell } from "@/lib/use-compact-shell";

const ACTIVE_STATES = new Set<AgentWorkState>([
  "pending",
  "classified",
  "auto_execute",
  "draft_for_review",
  "request_more_data",
  "rejected",
]);

function encodePathSlug(slug: string): string {
  return slug.split("/").map(encodeURIComponent).join("/");
}

export default function ChecklistPage() {
  const searchParams = useSearchParams();
  const wikiSlugFilter = searchParams.get("wiki_slug")?.trim() || null;
  const workIdFilter = searchParams.get("work_id")?.trim() || null;
  const scopeParam = searchParams.get("scope")?.trim();
  const scopeFilter: "personal" | "company" | null =
    scopeParam === "personal" || scopeParam === "company" ? scopeParam : null;
  const compactShell = useCompactAgentWorkShell();

  const [items, setItems] = useState<AgentWorkItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [deciding, setDeciding] = useState<AgentWorkReviewAction | null>(null);
  const [decisionNote, setDecisionNote] = useState("");
  const [decisionNoEdit, setDecisionNoEdit] = useState(false);
  const [lastDecision, setLastDecision] = useState<AgentWorkReviewDecision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function loadItems() {
    setLoading(true);
    setError(null);
    try {
      const rows = await api.listAgentWork({
        ...(scopeFilter ? { scope: scopeFilter } : {}),
        ...(wikiSlugFilter ? { wiki_slug: wikiSlugFilter } : {}),
      });
      const visible = rows.filter(isChecklistItem);
      setItems(visible);
      setSelectedId((current) =>
        workIdFilter && visible.some((item) => item.work_id === workIdFilter)
          ? workIdFilter
          : current && visible.some((item) => item.work_id === current)
            ? current
            : visible[0]?.work_id ?? null,
      );
      setLastDecision(null);
      setDecisionNoEdit(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "체크리스트를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;

    async function run() {
      setLoading(true);
      setError(null);
      try {
        const rows = await api.listAgentWork({
          ...(scopeFilter ? { scope: scopeFilter } : {}),
          ...(wikiSlugFilter ? { wiki_slug: wikiSlugFilter } : {}),
        });
        if (cancelled) return;
        const visible = rows.filter(isChecklistItem);
        setItems(visible);
        setSelectedId((current) =>
          workIdFilter && visible.some((item) => item.work_id === workIdFilter)
            ? workIdFilter
            : current && visible.some((item) => item.work_id === current)
              ? current
              : visible[0]?.work_id ?? null,
        );
        setDecisionNoEdit(false);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "체크리스트를 불러오지 못했습니다.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void run();
    return () => {
      cancelled = true;
    };
  }, [wikiSlugFilter, workIdFilter, scopeFilter]);

  const selected = useMemo(
    () => items.find((item) => item.work_id === selectedId) ?? items[0] ?? null,
    [items, selectedId],
  );

  async function decideItem(action: AgentWorkReviewAction) {
    if (!selected) return;
    setDeciding(action);
    setError(null);
    setNotice(null);
    setLastDecision(null);
    try {
      const selectedEmailGate = getEmailAutoSendGate(selected.payload);
      const canRecordNoEdit = selected.state === "draft_for_review" && selectedEmailGate;
      const result = await api.decideAgentWork(selected.work_id, {
        decision: action,
        note: decisionNote.trim() ? decisionNote.trim() : null,
        no_edit: action === "approve" && canRecordNoEdit ? decisionNoEdit : null,
      });
      setItems((current) =>
        current
          .map((item) => (item.work_id === result.item.work_id ? result.item : item))
          .filter(isChecklistItem),
      );
      setSelectedId(result.item.work_id);
      setDecisionNote("");
      setDecisionNoEdit(false);
      setLastDecision(result.decision);
      setNotice(`${result.decision.decision}: ${result.decision.from_state} -> ${result.decision.to_state}`);
      // 항목 상태가 바뀌었으니 AppShell 배지도 즉시 재검증한다(60초 TTL 대기 없음).
      refreshAgentWorkBadge();
    } catch (e) {
      const message = e instanceof Error ? e.message : "decision 실패";
      await loadItems();
      setError(message);
    } finally {
      setDeciding(null);
    }
  }

  async function saveDraft(body: string) {
    if (!selected) return;
    setError(null);
    setNotice(null);
    const updated = await api.editAgentWorkDraft(selected.work_id, body);
    setItems((current) =>
      current
        .map((item) => (item.work_id === updated.work_id ? updated : item))
        .filter(isChecklistItem),
    );
    setNotice("초안 저장됨");
  }

  const counts = useMemo(() => summarize(items), [items]);
  const pageTitle = scopeFilter === "company" ? "체크리스트 · 회사" : "체크리스트";
  const pageSubtitle = `${items.length}개 항목 · 검토 ${counts.draft} · 정보 ${counts.data} · 자동 ${counts.auto}${wikiSlugFilter ? ` · wiki ${wikiSlugFilter}` : ""}`;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title={pageTitle}
        subtitle={pageSubtitle}
        right={
          <Toolbar>
            <ToolbarButton icon={<RefreshCw size={14} strokeWidth={1.8} />} onClick={() => void loadItems()}>
              새로고침
            </ToolbarButton>
          </Toolbar>
        }
      />

      <div
        className={
          compactShell
            ? "grid min-h-0 flex-1 auto-rows-max grid-cols-1 content-start gap-4 overflow-y-auto p-4 pb-[calc(var(--mobile-safe-bottom)+var(--mobile-nav-height)+16px)]"
            : "grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto p-5 xl:grid-cols-[minmax(0,440px)_minmax(0,1fr)] xl:overflow-hidden"
        }
      >
        <section className={compactShell ? "h-fit min-h-0 space-y-3" : "min-h-0 space-y-3 xl:overflow-y-auto"}>
          <Card className="p-3">
            <div className="mb-3 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <ListChecks size={16} strokeWidth={1.8} />
                <ModeBadge>CHECKLIST</ModeBadge>
              </div>
              <Chip tone="neutral">{items.length}</Chip>
            </div>
            <div className="grid grid-cols-3 gap-2">
              <MiniStat label="검토" value={counts.draft} />
              <MiniStat label="정보" value={counts.data} />
              <MiniStat label="자동" value={counts.auto} />
            </div>
          </Card>

          {error ? (
            <Banner tone="fail" title="실패">
              {error}
            </Banner>
          ) : null}
          {notice ? (
            <Banner tone="pass" title="완료">
              {notice}
            </Banner>
          ) : null}
          {wikiSlugFilter ? (
            <Card className="p-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-body)" }}>
              <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                <span className="break-words font-[family-name:var(--font-mono)]">{wikiSlugFilter}</span>
                <Link className="font-semibold" href="/checklist" style={{ color: "var(--color-text-strong)" }}>
                  해제
                </Link>
              </div>
            </Card>
          ) : null}

          {loading ? (
            <div className="space-y-2">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-[92px] w-full" />
              ))}
            </div>
          ) : items.length === 0 ? (
            <Card className="p-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
              지금 남은 체크리스트 없음
            </Card>
          ) : (
            <div className="space-y-2">
              {items.map((item) => (
                <WorkListItem
                  item={item}
                  key={item.work_id}
                  selected={selected?.work_id === item.work_id}
                  onSelect={() => {
                    setDecisionNote("");
                    setDecisionNoEdit(false);
                    setLastDecision(null);
                    setSelectedId(item.work_id);
                  }}
                />
              ))}
            </div>
          )}
        </section>

        <section className={compactShell ? "h-fit min-h-0" : "min-h-0 xl:overflow-y-auto"}>
          {selected ? (
            <WorkDetail
              compactShell={compactShell}
              deciding={deciding}
              decisionNote={decisionNote}
              decisionNoEdit={decisionNoEdit}
              item={selected}
              lastDecision={lastDecision}
              onDecision={(action) => void decideItem(action)}
              onSaveDraft={saveDraft}
              setDecisionNote={setDecisionNote}
              setDecisionNoEdit={setDecisionNoEdit}
            />
          ) : (
            <Card className="flex min-h-[200px] items-center justify-center p-4 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
              항목을 선택하세요.
            </Card>
          )}
        </section>
      </div>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: number }) {
  return (
    <div
      className="rounded-[var(--radius-toolbar)] px-2 py-2"
      style={{
        background: "var(--color-sidebar-active)",
        border: "1px solid var(--color-divider)",
      }}
    >
      <div className="text-[var(--text-meta)] font-bold" style={{ color: "var(--color-text-muted)" }}>
        {label}
      </div>
      <div className="font-[family-name:var(--font-mono)] text-[18px] font-semibold" style={{ color: "var(--color-text-strong)" }}>
        {value}
      </div>
    </div>
  );
}

function WorkListItem({
  item,
  selected,
  onSelect,
}: {
  item: AgentWorkItem;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button className="block w-full text-left" onClick={onSelect} type="button">
      <Card
        interactive
        className="p-3"
        style={{
          outline: selected ? "2px solid var(--color-text-strong)" : "none",
          outlineOffset: "-2px",
        }}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <OutcomeChip item={item} />
              <StateChip state={item.state} />
              <Chip tone="neutral">{item.source_kind}</Chip>
            </div>
            <div className="mt-2 line-clamp-2 text-[var(--text-body-sm)] font-semibold leading-snug" style={{ color: "var(--color-text-strong)" }}>
              {item.title}
            </div>
          </div>
          <span className="shrink-0 font-[family-name:var(--font-mono)] text-[10px]" style={{ color: "var(--color-text-muted)" }}>
            {shortId(item.work_id)}
          </span>
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          <Chip tone="muted">{item.action_family}</Chip>
          <Chip tone="muted">{formatDate(item.updated_at)}</Chip>
        </div>
      </Card>
    </button>
  );
}

function WorkDetail({
  compactShell,
  deciding,
  decisionNote,
  decisionNoEdit,
  item,
  lastDecision,
  onDecision,
  onSaveDraft,
  setDecisionNote,
  setDecisionNoEdit,
}: {
  compactShell: boolean;
  deciding: AgentWorkReviewAction | null;
  decisionNote: string;
  decisionNoEdit: boolean;
  item: AgentWorkItem;
  lastDecision: AgentWorkReviewDecision | null;
  onDecision: (action: AgentWorkReviewAction) => void;
  onSaveDraft: (body: string) => Promise<void>;
  setDecisionNote: (value: string) => void;
  setDecisionNoEdit: (value: boolean) => void;
}) {
  const reviewable = item.state === "draft_for_review" || item.state === "request_more_data";
  const emailGate = getEmailAutoSendGate(item.payload);
  const emailDraft = getEmailDraft(item.payload);
  const canRecordNoEdit = item.state === "draft_for_review" && emailGate;
  return (
    <div className="space-y-3">
      <Card className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <ModeBadge>AGENT</ModeBadge>
              <OutcomeChip item={item} />
              <StateChip state={item.state} />
            </div>
            <h2 className="break-words text-[18px] font-extrabold leading-tight" style={{ color: "var(--color-text-strong)" }}>
              {item.title}
            </h2>
            <p className="mt-1 font-[family-name:var(--font-mono)] text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
              {item.work_id}
            </p>
          </div>
        </div>
      </Card>

      {emailDraft ? (
        <DraftCard
          draft={emailDraft}
          editable={reviewable && item.action_family === "email_send"}
          onSave={onSaveDraft}
        />
      ) : null}

      <Card className="p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <ModeBadge>검토</ModeBadge>
          {lastDecision?.work_id === item.work_id ? (
            <Chip tone="pass">
              {lastDecision.from_state}
              {" -> "}
              {lastDecision.to_state}
            </Chip>
          ) : (
            <Chip tone={reviewable ? "warn" : "muted"}>
              {reviewable ? "검토 필요" : "읽기 전용"}
            </Chip>
          )}
        </div>
        {reviewable ? (
          <div className="space-y-3">
            <textarea
              className="min-h-[72px] w-full resize-y rounded-[6px] p-2.5 text-[var(--text-body-sm)] outline-none focus:ring-2 sm:min-h-[84px]"
              maxLength={1200}
              onChange={(e) => setDecisionNote(e.target.value)}
              placeholder="검토 메모"
              style={{
                background: "var(--color-card)",
                border: "1px solid var(--color-toolbar-border)",
                color: "var(--color-text-body)",
              }}
              value={decisionNote}
            />
            {canRecordNoEdit ? (
              <label
                className={compactShell ? "flex min-h-11 items-center gap-2 text-[var(--text-body-sm)]" : "flex items-center gap-2 text-[var(--text-body-sm)]"}
                style={{ color: "var(--color-text-body)" }}
              >
                <input
                  checked={decisionNoEdit}
                  className="orthus-check"
                  onChange={(event) => setDecisionNoEdit(event.target.checked)}
                  type="checkbox"
                />
                수정 없이 승인
              </label>
            ) : null}
            <Toolbar
              className={compactShell ? "grid grid-cols-1 gap-2" : undefined}
              style={{
                alignItems: "stretch",
                flexWrap: "wrap",
                overflowX: "visible",
                width: "100%",
              }}
            >
              <ToolbarButton
                className={compactShell ? "h-11 justify-center" : undefined}
                disabled={deciding !== null}
                icon={<Check size={14} strokeWidth={1.8} />}
                onClick={() => onDecision("approve")}
                tone="primary"
              >
                승인
              </ToolbarButton>
              <ToolbarButton
                className={compactShell ? "h-11 justify-center" : undefined}
                disabled={deciding !== null}
                icon={<X size={14} strokeWidth={1.8} />}
                onClick={() => onDecision("dismiss")}
              >
                보류
              </ToolbarButton>
              {item.state === "draft_for_review" ? (
                <ToolbarButton
                  className={compactShell ? "h-11 justify-center" : undefined}
                  disabled={deciding !== null}
                  icon={<MessageSquare size={14} strokeWidth={1.8} />}
                  onClick={() => onDecision("request_more_data")}
                >
                  추가 정보 요청
                </ToolbarButton>
              ) : null}
            </Toolbar>
          </div>
        ) : (
          <p className="text-[var(--text-body-sm)] leading-[1.6]" style={{ color: "var(--color-text-muted)" }}>
            {formatStateLabel(item.state)}
          </p>
        )}
      </Card>

      <Card className="p-4">
        <div className="mb-2 flex items-center gap-2">
          <ModeBadge>POLICY</ModeBadge>
        </div>
        <p className="text-[var(--text-body-sm)] leading-[1.6]" style={{ color: "var(--color-text-body)" }}>
          {item.policy_reason ?? "policy reason 없음"}
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {item.reason_codes.length ? (
            item.reason_codes.map((code) => (
              <Chip key={code} tone="neutral">
                {code}
              </Chip>
            ))
          ) : (
            <Chip tone="muted">reason code 없음</Chip>
          )}
        </div>
      </Card>

      <Card className="p-4">
        <div className="mb-3 grid gap-2 sm:grid-cols-2">
          <KeyValue label="node" value={`${item.node_kind} · ${item.node_id}`} />
          <KeyValue label="source" value={`${item.source_kind} · ${item.source_ref_id}`} />
          <KeyValue label="correlation" value={item.correlation_id ?? "-"} mono />
          <KeyValue label="run" value={item.last_run_id ?? "-"} mono />
          <KeyValue label="created" value={formatDate(item.created_at)} />
          <KeyValue label="updated" value={formatDate(item.updated_at)} />
        </div>
        <RelatedWikiLinks slugs={item.wiki_slugs} />
      </Card>

      <details className="rounded-[8px]" style={{ background: "var(--color-card)", border: "1px solid var(--color-toolbar-border)" }}>
        <summary className="cursor-pointer select-none p-3 text-[var(--text-body-sm)] font-bold" style={{ color: "var(--color-text-muted)" }}>
          상세 보기 · 게이트 / 원본 데이터
        </summary>
        <div className="space-y-3 p-3 pt-0">
          {emailGate ? <EmailGateCard gate={emailGate} /> : null}
          <JsonCard title="EVIDENCE" value={item.evidence} />
          <JsonCard title="PAYLOAD" value={item.payload} />
        </div>
      </details>
    </div>
  );
}

function RelatedWikiLinks({ slugs }: { slugs: string[] }) {
  return (
    <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--color-divider)" }}>
      <div className="mb-2 text-[var(--text-meta)] font-bold uppercase" style={{ color: "var(--color-text-muted)" }}>
        related wiki
      </div>
      {slugs.length ? (
        <div className="flex flex-wrap gap-1.5">
          {slugs.map((slug) => (
            <Link href={`/wiki/${encodePathSlug(slug)}`} key={slug}>
              <Chip tone="neutral">
                <span className="font-[family-name:var(--font-mono)]">{slug}</span>
              </Chip>
            </Link>
          ))}
        </div>
      ) : (
        <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          관련 wiki page provenance 없음
        </p>
      )}
    </div>
  );
}

type EmailAutoSendGate = {
  eligible: boolean;
  used_for_outcome: boolean;
  reason: string;
  threshold_met: boolean;
  recent_window_days: number;
  recent_total: number;
  recent_no_edit_approvals: number;
  recent_no_edit_approval_rate: number;
  checks: Record<string, boolean>;
  missing_checks: string[];
};

function EmailGateCard({ gate }: { gate: EmailAutoSendGate }) {
  const checkEntries = Object.entries(gate.checks);
  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <ModeBadge>EMAIL GATE</ModeBadge>
          <Chip tone={gate.eligible ? "pass" : "warn"}>
            {gate.eligible ? "eligible" : "blocked"}
          </Chip>
          <Chip tone={gate.used_for_outcome ? "pass" : "muted"}>
            outcome {gate.used_for_outcome ? "on" : "off"}
          </Chip>
        </div>
        <Chip tone={gate.threshold_met ? "pass" : "warn"}>
          threshold {gate.threshold_met ? "met" : "not met"}
        </Chip>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <KeyValue
          label="recent"
          value={`${gate.recent_no_edit_approvals}/${gate.recent_total} no-edit · ${gate.recent_window_days}d`}
        />
        <KeyValue label="rate" value={`${(gate.recent_no_edit_approval_rate * 100).toFixed(1)}%`} />
        <KeyValue label="reason" value={gate.reason} />
        <KeyValue label="missing" value={gate.missing_checks.length ? `${gate.missing_checks.length}` : "none"} />
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {gate.missing_checks.length ? (
          gate.missing_checks.map((check) => (
            <Chip key={check} tone="warn">
              {check}
            </Chip>
          ))
        ) : (
          <Chip tone="pass">all checks pass</Chip>
        )}
      </div>
      <div className="mt-3 grid gap-1.5 sm:grid-cols-2">
        {checkEntries.map(([name, ok]) => (
          <div
            className="flex min-w-0 items-center justify-between gap-2 rounded-[6px] px-2 py-1 text-[var(--text-meta)]"
            key={name}
            style={{
              background: "var(--color-sidebar-active)",
              color: "var(--color-text-body)",
            }}
          >
            <span className="min-w-0 truncate">{name}</span>
            <Chip tone={ok ? "pass" : "fail"}>{ok ? "pass" : "fail"}</Chip>
          </div>
        ))}
      </div>
    </Card>
  );
}

function KeyValue({
  label,
  mono,
  value,
}: {
  label: string;
  mono?: boolean;
  value: string;
}) {
  return (
    <div>
      <div className="mb-1 text-[var(--text-meta)] font-bold uppercase" style={{ color: "var(--color-text-muted)" }}>
        {label}
      </div>
      <div
        className={`break-words text-[var(--text-body-sm)] ${mono ? "font-[family-name:var(--font-mono)]" : ""}`}
        style={{ color: "var(--color-text-body)" }}
      >
        {value}
      </div>
    </div>
  );
}

function getEmailAutoSendGate(payload: Record<string, unknown>): EmailAutoSendGate | null {
  const gate = payload.email_auto_send_gate;
  if (!isRecord(gate)) return null;
  const checks = isRecord(gate.checks)
    ? Object.fromEntries(Object.entries(gate.checks).map(([key, value]) => [key, value === true]))
    : {};
  return {
    eligible: gate.eligible === true,
    used_for_outcome: gate.used_for_outcome === true,
    reason: typeof gate.reason === "string" ? gate.reason : "-",
    threshold_met: gate.threshold_met === true,
    recent_window_days: numberValue(gate.recent_window_days),
    recent_total: numberValue(gate.recent_total),
    recent_no_edit_approvals: numberValue(gate.recent_no_edit_approvals),
    recent_no_edit_approval_rate: numberValue(gate.recent_no_edit_approval_rate),
    checks,
    missing_checks: stringArray(gate.missing_checks),
  };
}

type EmailDraftView = { recipient: string; subject: string; body: string };

function getEmailDraft(payload: Record<string, unknown>): EmailDraftView | null {
  const draft = payload.email_draft;
  if (!isRecord(draft)) return null;
  const body = typeof draft.body_template === "string" ? draft.body_template : "";
  if (!body.trim()) return null;
  return {
    recipient: typeof draft.recipient_hint === "string" ? draft.recipient_hint : "",
    subject: typeof draft.subject_hint === "string" ? draft.subject_hint : "",
    body,
  };
}

function DraftCard({
  draft,
  editable,
  onSave,
}: {
  draft: EmailDraftView;
  editable: boolean;
  onSave: (body: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(draft.body);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const surface = {
    background: "var(--color-card)",
    border: "1px solid var(--color-toolbar-border)",
    color: "var(--color-text-body)",
  };
  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <ModeBadge>초안</ModeBadge>
          <Chip tone="warn">보내기 전 검토</Chip>
        </div>
        {editable && !editing ? (
          <ToolbarButton
            icon={<Pencil size={14} strokeWidth={1.8} />}
            onClick={() => {
              setText(draft.body);
              setErr(null);
              setEditing(true);
            }}
          >
            수정
          </ToolbarButton>
        ) : null}
      </div>
      {draft.recipient || draft.subject ? (
        <div className="mb-3 grid gap-2 sm:grid-cols-2">
          {draft.recipient ? <KeyValue label="받는 사람" value={draft.recipient} /> : null}
          {draft.subject ? <KeyValue label="제목" value={draft.subject} /> : null}
        </div>
      ) : null}
      {editing ? (
        <div className="space-y-2">
          <textarea
            className="min-h-[220px] w-full resize-y rounded-[6px] p-3 text-[var(--text-body-sm)] leading-[1.7] outline-none focus:ring-2"
            onChange={(e) => setText(e.target.value)}
            style={surface}
            value={text}
          />
          {err ? (
            <p className="text-[var(--text-body-sm)]" style={{ color: "#c0392b" }}>
              {err}
            </p>
          ) : null}
          <Toolbar style={{ flexWrap: "wrap" }}>
            <ToolbarButton
              disabled={saving || !text.trim()}
              icon={<Check size={14} strokeWidth={1.8} />}
              onClick={async () => {
                setSaving(true);
                setErr(null);
                try {
                  await onSave(text);
                  setEditing(false);
                } catch (e) {
                  setErr(e instanceof Error ? e.message : "저장 실패");
                } finally {
                  setSaving(false);
                }
              }}
              tone="primary"
            >
              저장
            </ToolbarButton>
            <ToolbarButton
              disabled={saving}
              icon={<X size={14} strokeWidth={1.8} />}
              onClick={() => {
                setEditing(false);
                setErr(null);
              }}
            >
              취소
            </ToolbarButton>
          </Toolbar>
        </div>
      ) : (
        <div
          className="whitespace-pre-wrap break-words rounded-[6px] p-3 text-[var(--text-body-sm)] leading-[1.7]"
          style={surface}
        >
          {draft.body}
        </div>
      )}
    </Card>
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function numberValue(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function stringArray(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function JsonCard({ title, value }: { title: string; value: unknown }) {
  return (
    <Card className="p-4">
      <div className="mb-2 flex items-center gap-2">
        <ModeBadge>{title}</ModeBadge>
      </div>
      <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap break-words rounded-[6px] p-3 font-[family-name:var(--font-mono)] text-[11px]" style={{ background: "var(--color-sidebar-active)", color: "var(--color-text-body)" }}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </Card>
  );
}

function OutcomeChip({ item }: { item: AgentWorkItem }) {
  const outcome = item.policy_outcome ?? "pending";
  if (outcome === "auto_execute") return <Chip tone="pass">auto</Chip>;
  if (outcome === "draft_for_review") return <Chip tone="warn">draft</Chip>;
  if (outcome === "request_more_data") return <Chip tone="warn">data</Chip>;
  if (outcome === "reject") return <Chip tone="fail">reject</Chip>;
  return <Chip tone="muted">{outcome}</Chip>;
}

function StateChip({ state }: { state: AgentWorkState }) {
  if (state === "resolved") return <Chip tone="pass">resolved</Chip>;
  if (state === "dismissed") return <Chip tone="fail">dismissed</Chip>;
  if (state === "request_more_data") return <Chip tone="warn">request data</Chip>;
  if (state === "draft_for_review") return <Chip tone="warn">review</Chip>;
  if (state === "auto_execute") return <Chip tone="pass">auto</Chip>;
  if (state === "rejected") return <Chip tone="fail">rejected</Chip>;
  return <Chip tone="muted">{state}</Chip>;
}

function formatStateLabel(state: AgentWorkState) {
  const labels: Partial<Record<AgentWorkState, string>> = {
    auto_execute: "자동 처리 대기",
    classified: "분류됨",
    dismissed: "보류됨",
    draft_for_review: "검토 필요",
    pending: "대기 중",
    rejected: "거절됨",
    request_more_data: "추가 정보 필요",
    resolved: "완료됨",
  };
  return labels[state] ?? state;
}

function summarize(items: AgentWorkItem[]) {
  return items.reduce(
    (acc, item) => {
      if (item.policy_outcome === "draft_for_review") acc.draft += 1;
      if (item.policy_outcome === "request_more_data") acc.data += 1;
      if (item.policy_outcome === "auto_execute") acc.auto += 1;
      return acc;
    },
    { auto: 0, data: 0, draft: 0 },
  );
}

function isChecklistItem(item: AgentWorkItem) {
  // agent_task = chat delegations (지식/코드 위임). They run and return their result
  // inline in the agent chat (history), so they are NOT review-queue items — the
  // 체크리스트 is for things needing operator feedback or email drafts (email_send).
  // Keep them out of the checklist regardless of run state (auto_execute / failed).
  return (
    item.source_kind !== "wiki_task" &&
    item.action_family !== "agent_task" &&
    ACTIVE_STATES.has(item.state)
  );
}

function shortId(id: string) {
  return id.slice(0, 8);
}

function formatDate(value: string) {
  return new Date(value).toLocaleString("ko-KR");
}
