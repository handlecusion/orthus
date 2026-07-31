"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type PromotionPackage,
  type PromotionStage,
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
  cx,
  inputClass,
  inputStyle,
} from "@/components/ui";

type StageFilter = "pending" | "approved" | "rejected" | "all";

const FILTERS: StageFilter[] = ["pending", "approved", "rejected", "all"];

export default function PromotePage() {
  const [filter, setFilter] = useState<StageFilter>("pending");
  const [stages, setStages] = useState<PromotionStage[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [packageText, setPackageText] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [staging, setStaging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function refreshStages() {
    setLoading(true);
    setError(null);
    try {
      const rows = await api.listPromotionStages(filter === "all" ? undefined : filter);
      setStages(rows);
      setSelectedId((current) => current ?? rows[0]?.stage_id ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "승격 목록을 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;

    async function loadInitial() {
      setLoading(true);
      setError(null);
      try {
        const rows = await api.listPromotionStages(filter === "all" ? undefined : filter);
        if (cancelled) return;
        setStages(rows);
        setSelectedId((current) => current ?? rows[0]?.stage_id ?? null);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "승격 목록을 불러오지 못했습니다.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void loadInitial();

    return () => {
      cancelled = true;
    };
  }, [filter]);

  const selected = useMemo(
    () => stages.find((stage) => stage.stage_id === selectedId) ?? stages[0] ?? null,
    [selectedId, stages],
  );

  async function stagePackage() {
    setStaging(true);
    setError(null);
    setNotice(null);
    try {
      const parsed = JSON.parse(packageText) as PromotionPackage;
      const staged = await api.stagePromotionPackage(parsed);
      setPackageText("");
      setNotice("staged");
      setFilter("pending");
      setStages((prev) => [staged, ...prev.filter((s) => s.stage_id !== staged.stage_id)]);
      setSelectedId(staged.stage_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "package staging failed");
    } finally {
      setStaging(false);
    }
  }

  async function decide(stage: PromotionStage, action: "approve" | "reject") {
    setBusyId(stage.stage_id);
    setError(null);
    setNotice(null);
    try {
      const updated =
        action === "approve"
          ? await api.approvePromotionStage(stage.stage_id)
          : await api.rejectPromotionStage(stage.stage_id);
      setNotice(action === "approve" ? "approved" : "rejected");
      setStages((prev) =>
        prev.map((row) => (row.stage_id === updated.stage_id ? updated : row)),
      );
      setSelectedId(updated.stage_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : `${action} failed`);
    } finally {
      setBusyId(null);
    }
  }

  const pendingCount = stages.filter((s) => s.status === "pending").length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="승격 검토"
        subtitle={`${stages.length}개 package${pendingCount > 0 ? ` · pending ${pendingCount}` : ""}`}
        right={
          <Toolbar>
            {FILTERS.map((item) => (
              <ToolbarButton
                key={item}
                tone={filter === item ? "primary" : "default"}
                onClick={() => setFilter(item)}
              >
                {item}
              </ToolbarButton>
            ))}
            <ToolbarButton onClick={() => void refreshStages()}>새로고침</ToolbarButton>
          </Toolbar>
        }
      />

      <div className="grid min-h-0 flex-1 grid-cols-[360px_minmax(0,1fr)] gap-4 overflow-hidden p-5">
        <section className="flex min-h-0 flex-col gap-3">
          <Card className="p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <ModeBadge>STAGE</ModeBadge>
              <span
                className="text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                package JSON
              </span>
            </div>
            <textarea
              value={packageText}
              onChange={(e) => setPackageText(e.target.value)}
              rows={7}
              spellCheck={false}
              placeholder='{"source_node_id":"personal-a", ...}'
              className={cx(inputClass, "resize-none font-[family-name:var(--font-mono)] text-[12px]")}
              style={inputStyle}
            />
            <div className="mt-2 flex justify-end">
              <ToolbarButton
                tone="primary"
                disabled={staging || !packageText.trim()}
                onClick={() => void stagePackage()}
              >
                {staging ? "staging…" : "stage"}
              </ToolbarButton>
            </div>
          </Card>

          {error ? <Banner tone="fail" title="실패">{error}</Banner> : null}
          {notice ? <Banner tone="pass" title="완료">{notice}</Banner> : null}

          <div className="min-h-0 flex-1 overflow-y-auto">
            {loading ? (
              <div className="space-y-2">
                {[0, 1, 2].map((i) => (
                  <Skeleton key={i} className="h-[76px] w-full" />
                ))}
              </div>
            ) : stages.length === 0 ? (
              <Card className="p-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
                staged package 없음
              </Card>
            ) : (
              <div className="space-y-2">
                {stages.map((stage) => (
                  <StageListItem
                    key={stage.stage_id}
                    stage={stage}
                    selected={selected?.stage_id === stage.stage_id}
                    onSelect={() => setSelectedId(stage.stage_id)}
                  />
                ))}
              </div>
            )}
          </div>
        </section>

        <section className="min-h-0 overflow-y-auto">
          {selected ? (
            <StageDetail
              stage={selected}
              busy={busyId === selected.stage_id}
              onApprove={() => void decide(selected, "approve")}
              onReject={() => void decide(selected, "reject")}
            />
          ) : (
            <Card className="flex h-full min-h-[260px] items-center justify-center p-6 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
              검토할 package 선택
            </Card>
          )}
        </section>
      </div>
    </div>
  );
}

function StageListItem({
  stage,
  selected,
  onSelect,
}: {
  stage: PromotionStage;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button type="button" onClick={onSelect} className="block w-full text-left">
      <Card
        interactive
        className="p-3"
        style={{
          outline: selected ? "2px solid var(--color-text-strong)" : "none",
          outlineOffset: "-2px",
        }}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
            {stage.sanitized_title}
          </span>
          <StatusChip status={stage.status} />
        </div>
        <div className="mt-1 flex items-center gap-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          <span className="truncate">{stage.source_node_id}</span>
          <span className="font-[family-name:var(--font-mono)]">{stage.target_project}</span>
        </div>
      </Card>
    </button>
  );
}

function StageDetail({
  stage,
  busy,
  onApprove,
  onReject,
}: {
  stage: PromotionStage;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  const canDecide = stage.status === "pending";
  return (
    <div className="space-y-3">
      <Card className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <ModeBadge>PROMOTE</ModeBadge>
              <StatusChip status={stage.status} />
              <Chip tone="neutral">{stage.target_project}</Chip>
            </div>
            <h2 className="break-words text-[18px] font-extrabold leading-tight" style={{ color: "var(--color-text-strong)" }}>
              {stage.sanitized_title}
            </h2>
            <p className="mt-1 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
              {stage.source_node_id} · {stage.source_doc_id}
            </p>
          </div>
          <Toolbar>
            <ToolbarButton disabled={!canDecide || busy} onClick={onReject}>
              reject
            </ToolbarButton>
            <ToolbarButton tone="primary" disabled={!canDecide || busy} onClick={onApprove}>
              approve
            </ToolbarButton>
          </Toolbar>
        </div>
      </Card>

      <ReviewSummary stage={stage} />
      <SanitizeDiff stage={stage} />
      <RunHistory stage={stage} />

      <Card className="p-4">
        <div className="mb-2 flex items-center justify-between gap-2">
          <ModeBadge>SANITIZED</ModeBadge>
          <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            {stage.sanitized_markdown.length.toLocaleString()} chars
          </span>
        </div>
        <pre
          className="max-h-[420px] overflow-auto whitespace-pre-wrap text-[var(--text-body-sm)] leading-[1.55]"
          style={{ color: "var(--color-text-body)" }}
        >
          {stage.sanitized_markdown}
        </pre>
      </Card>

      <Card className="p-4">
        <div className="mb-2 flex items-center gap-2">
          <ModeBadge>META</ModeBadge>
          {stage.promoted_doc_id ? <Chip tone="pass">promoted</Chip> : null}
        </div>
        <pre className="overflow-auto text-[12px] leading-[1.5]" style={{ color: "var(--color-text-muted)" }}>
          {JSON.stringify(
            {
              source_title: stage.source_title,
              source_meta: stage.source_meta,
              promoted_doc_id: stage.promoted_doc_id,
              audit_events: stage.audit_events,
              created_at: stage.created_at,
              decided_at: stage.decided_at,
            },
            null,
            2,
          )}
        </pre>
      </Card>
    </div>
  );
}

function SanitizeDiff({ stage }: { stage: PromotionStage }) {
  const exportStats = sanitizationGroup(stage.source_meta.export_sanitization);
  const centralStats = sanitizationGroup(stage.source_meta.central_sanitization);

  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <ModeBadge>SANITIZE DIFF</ModeBadge>
          <Chip tone={hasAnyChanged(exportStats) || hasAnyChanged(centralStats) ? "warn" : "pass"}>
            {hasAnyChanged(exportStats) || hasAnyChanged(centralStats) ? "changed" : "kept"}
          </Chip>
        </div>
        <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          raw content withheld
        </span>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <SanitizeGroup title="personal export" stats={exportStats} fields={["title", "markdown"]} />
        <SanitizeGroup
          title="central recheck"
          stats={centralStats}
          fields={["source_title", "sanitized_title", "markdown"]}
        />
      </div>
    </Card>
  );
}

function SanitizeGroup({
  title,
  stats,
  fields,
}: {
  title: string;
  stats: Record<string, Record<string, unknown>> | null;
  fields: string[];
}) {
  return (
    <div
      className="min-w-0 rounded-[var(--radius-toolbar)] px-2 py-1.5"
      style={{
        background: "var(--color-app-canvas)",
        border: "1px solid var(--color-divider)",
      }}
    >
      <div className="mb-2 text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-text-muted)" }}>
        {title}
      </div>
      {stats ? (
        <div className="space-y-2">
          {fields.map((field) => (
            <SanitizeField key={field} label={field} stats={stats[field] ?? null} />
          ))}
        </div>
      ) : (
        <div className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          no stats
        </div>
      )}
    </div>
  );
}

function SanitizeField({
  label,
  stats,
}: {
  label: string;
  stats: Record<string, unknown> | null;
}) {
  if (!stats) {
    return (
      <div className="flex items-center justify-between gap-2 text-[var(--text-body-sm)]">
        <span style={{ color: "var(--color-text-muted)" }}>{label}</span>
        <Chip tone="muted">missing</Chip>
      </div>
    );
  }

  const changed = Boolean(stats.changed);
  const inputChars = numberStat(stats.input_chars);
  const sanitizedChars = numberStat(stats.sanitized_chars);
  const removedChars = numberStat(stats.removed_chars);
  const secretMarkers = numberStat(stats.secret_markers);
  const piiChanged = Boolean(stats.pii_changed);

  return (
    <div className="min-w-0 border-t pt-2 first:border-t-0 first:pt-0" style={{ borderColor: "var(--color-divider)" }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium text-[var(--text-body-sm)]" style={{ color: "var(--color-text-strong)" }}>
          {label}
        </span>
        <Chip tone={changed ? "warn" : "pass"}>{changed ? "changed" : "kept"}</Chip>
      </div>
      <div className="mt-1 flex flex-wrap gap-1.5">
        <Chip tone="neutral">{inputChars.toLocaleString()} → {sanitizedChars.toLocaleString()} chars</Chip>
        {removedChars > 0 ? <Chip tone="warn">-{removedChars.toLocaleString()}</Chip> : null}
        {secretMarkers > 0 ? <Chip tone="warn">secret x{secretMarkers}</Chip> : null}
        {piiChanged ? <Chip tone="warn">pii masked</Chip> : null}
      </div>
    </div>
  );
}

function RunHistory({ stage }: { stage: PromotionStage }) {
  const events = stage.audit_events;
  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <ModeBadge>RUN HISTORY</ModeBadge>
          <Chip tone={events.length > 0 ? "neutral" : "muted"}>
            {events.length.toLocaleString()} events
          </Chip>
        </div>
        {stage.promoted_doc_id ? <Chip tone="pass">central import recorded</Chip> : null}
      </div>

      {events.length === 0 ? (
        <div className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          no audit events linked to this stage
        </div>
      ) : (
        <div className="space-y-2">
          {events.map((event) => (
            <div
              key={`${event.audit_id}:${event.node}:${event.phase}`}
              className="grid gap-2 rounded-[var(--radius-toolbar)] px-2 py-1.5 md:grid-cols-[180px_minmax(0,1fr)]"
              style={{
                background: "var(--color-app-canvas)",
                border: "1px solid var(--color-divider)",
              }}
            >
              <div className="flex min-w-0 items-center gap-2">
                <Chip tone={event.status === "error" ? "fail" : "pass"}>
                  {event.status}
                </Chip>
                <span
                  className="truncate font-[family-name:var(--font-mono)] text-[12px]"
                  style={{ color: "var(--color-text-strong)" }}
                >
                  {event.node.replace("promote.", "")}
                </span>
              </div>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[var(--text-meta)]">
                  <span style={{ color: "var(--color-text-muted)" }}>
                    {formatDateTime(event.occurred_at)}
                  </span>
                  <span
                    className="font-[family-name:var(--font-mono)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    run {formatId(event.node_run_id)}
                  </span>
                  <span
                    className="font-[family-name:var(--font-mono)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    audit {event.audit_id}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {historyMetaChips(event.meta).map((item) => (
                    <Chip key={item} tone="neutral">
                      {item}
                    </Chip>
                  ))}
                  {event.error_class ? <Chip tone="fail">{event.error_class}</Chip> : null}
                </div>
                {event.error_message ? (
                  <div className="mt-1 text-[var(--text-meta)]" style={{ color: "var(--color-fail-fg)" }}>
                    {event.error_message}
                  </div>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function ReviewSummary({ stage }: { stage: PromotionStage }) {
  const sourceName = metaString(stage.source_meta, "source") ?? "unknown";
  const sourceDbName = metaString(stage.source_meta, "source_db_name");
  const titleChanged = stage.source_title.trim() !== stage.sanitized_title.trim();
  const metaRows = Object.entries(stage.source_meta)
    .filter(([key]) => key !== "target_project")
    .slice(0, 8);

  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <ModeBadge>REVIEW</ModeBadge>
          <Chip tone="neutral">{stage.source_scope} → company</Chip>
          <Chip tone={titleChanged ? "warn" : "pass"}>
            {titleChanged ? "title sanitized" : "title kept"}
          </Chip>
        </div>
        <span
          className="font-[family-name:var(--font-mono)] text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {formatId(stage.stage_id)}
        </span>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-2">
          <SummaryRow label="source node" value={stage.source_node_id} />
          <SummaryRow label="source doc" value={formatId(stage.source_doc_id)} mono />
          <SummaryRow label="source" value={sourceDbName ? `${sourceName} · ${sourceDbName}` : sourceName} />
          <SummaryRow label="target project" value={stage.target_project} />
        </div>

        <div className="space-y-2">
          <SummaryRow label="created" value={formatDateTime(stage.created_at)} />
          <SummaryRow label="decided" value={formatDateTime(stage.decided_at)} />
          <SummaryRow label="chars" value={stage.sanitized_markdown.length.toLocaleString()} mono />
          <SummaryRow
            label="promoted doc"
            value={stage.promoted_doc_id ? formatId(stage.promoted_doc_id) : "none"}
            mono
          />
        </div>
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-2">
        <TitleDelta label="source title" value={stage.source_title} />
        <TitleDelta label="sanitized title" value={stage.sanitized_title} />
      </div>

      {metaRows.length > 0 ? (
        <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--color-divider)" }}>
          <div className="mb-2">
            <ModeBadge>META SUMMARY</ModeBadge>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {metaRows.map(([key, value]) => (
              <SummaryRow key={key} label={key} value={formatMetaValue(value)} />
            ))}
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function SummaryRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="grid grid-cols-[112px_minmax(0,1fr)] gap-2 text-[var(--text-body-sm)]">
      <span className="truncate" style={{ color: "var(--color-text-muted)" }}>
        {label}
      </span>
      <span
        className={cx("break-words", mono && "font-[family-name:var(--font-mono)] text-[12px]")}
        style={{ color: "var(--color-text-body)" }}
      >
        {value}
      </span>
    </div>
  );
}

function TitleDelta({ label, value }: { label: string; value: string }) {
  return (
    <div
      className="min-w-0 rounded-[var(--radius-toolbar)] px-2 py-1.5"
      style={{
        background: "var(--color-app-canvas)",
        border: "1px solid var(--color-divider)",
      }}
    >
      <div className="mb-1 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
        {label}
      </div>
      <div
        className="break-words text-[var(--text-body-sm)] font-medium"
        style={{ color: "var(--color-text-strong)" }}
      >
        {value || "Untitled promotion"}
      </div>
    </div>
  );
}

function StatusChip({ status }: { status: PromotionStage["status"] }) {
  const tone = status === "approved" ? "pass" : status === "rejected" ? "fail" : "warn";
  return <Chip tone={tone}>{status}</Chip>;
}

function metaString(meta: Record<string, unknown>, key: string): string | null {
  const value = meta[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function formatDateTime(value: string | null): string {
  if (!value) return "none";
  try {
    return new Date(value).toLocaleString("ko-KR", {
      dateStyle: "short",
      timeStyle: "short",
    });
  } catch {
    return value;
  }
}

function formatId(value: string): string {
  return value.length > 13 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function formatMetaValue(value: unknown): string {
  if (value == null) return "none";
  if (typeof value === "string") return value || "none";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function sanitizationGroup(value: unknown): Record<string, Record<string, unknown>> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const rows: Record<string, Record<string, unknown>> = {};
  for (const [key, item] of Object.entries(value)) {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      rows[key] = item as Record<string, unknown>;
    }
  }
  return Object.keys(rows).length > 0 ? rows : null;
}

function hasAnyChanged(group: Record<string, Record<string, unknown>> | null): boolean {
  return Object.values(group ?? {}).some((stats) => Boolean(stats.changed));
}

function numberStat(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function historyMetaChips(meta: Record<string, unknown>): string[] {
  const keys = ["stage_id", "promoted_doc_id", "source_node_id", "rejected_by"];
  return keys
    .filter((key) => meta[key] != null)
    .map((key) => `${key}: ${formatHistoryValue(meta[key])}`);
}

function formatHistoryValue(value: unknown): string {
  if (typeof value === "string") return formatId(value);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value == null) return "none";
  return formatMetaValue(value);
}
