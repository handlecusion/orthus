"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  ChevronDown,
  Copy,
  Info,
  RefreshCw,
  RotateCw,
  Trash2,
  X,
} from "lucide-react";
import {
  API_BASE,
  api,
  type AuthConfig,
  type CollectorCommand,
  type CollectorCompileResponse,
  type CollectorEvidenceResponse,
  type CollectorEvidenceSource,
  type CollectorPersonalDataDeleteResponse,
  type ConnectorAccount,
  type ConnectorDocument,
  type ConnectorManifest,
  type ConnectorRun,
  type ConnectorScope,
  type ConnectorSyncResult,
  type ConnectorsResponse,
} from "@/lib/api";
import {
  Banner,
  Card,
  Chip,
  PageHeader,
  Skeleton,
  Toolbar,
  ToolbarButton,
  cx,
  inputClass,
  inputStyle,
} from "@/components/ui";

type Busy = { slug: string; action: "sync" } | null;
type ConnectorsVariant = "company" | "personal";

const COLLECTOR_SUPPORTED_SOURCES = new Set([
  "local_files",
  "codex_sessions",
  "claude_sessions",
  "chat_exports",
  "email_exports",
  "gws_gmail",
  "gws_drive",
  "github",
]);

// P6.7.5: mail connector slugs get a multi-account UX (one row per mailbox).
// Every other connector keeps the single-account form path unchanged.
const MAIL_SLUGS = new Set(["mail_nova", "mail_acme"]);

function isMailSlug(slug: string): boolean {
  return MAIL_SLUGS.has(slug);
}

export default function ConnectorsPage() {
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const searchParams = useSearchParams();
  const tabParam = searchParams.get("tab");

  useEffect(() => {
    let alive = true;
    api
      .getAuthConfig()
      .then((c) => {
        if (alive) setAuthConfig(c);
      })
      .catch(() => {
        if (alive) setAuthConfig(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  const isPersonalNode = authConfig?.node_kind === "personal";
  // 회사 노드 + owner-scope일 때만 회사/개인 커넥터를 한 화면 탭으로 묶는다.
  // 그 외(개인 노드, owner-scope 미사용 회사 노드)는 단일 변형이라 탭바를 숨긴다.
  const showPersonalTab =
    authConfig?.node_kind === "company" && authConfig?.owner_scope_enabled === true;
  const activeVariant: ConnectorsVariant =
    isPersonalNode || (showPersonalTab && tabParam === "personal")
      ? "personal"
      : "company";

  if (!showPersonalTab) {
    return <ConnectorsWorkspace variant={isPersonalNode ? "personal" : "company"} />;
  }

  const tabs: { key: ConnectorsVariant; href: string; label: string }[] = [
    { key: "company", href: "/connectors", label: "회사" },
    { key: "personal", href: "/connectors?tab=personal", label: "개인" },
  ];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <nav
        className="flex gap-1 overflow-x-auto px-3 pt-3 sm:px-5"
        style={{
          background: "var(--color-app-canvas)",
          borderBottom: "1px solid var(--color-divider)",
        }}
        aria-label="커넥터 탭"
      >
        {tabs.map((tab) => {
          const active = activeVariant === tab.key;
          return (
            <Link
              key={tab.key}
              href={tab.href}
              className="relative whitespace-nowrap rounded-t-[6px] px-3 py-2 text-[var(--text-body-sm)] transition-colors"
              style={{
                color: active
                  ? "var(--color-text-strong)"
                  : "var(--color-text-muted)",
                fontWeight: active ? 700 : 500,
                borderBottom: active
                  ? "2px solid var(--color-text-strong)"
                  : "2px solid transparent",
                minHeight: 44,
              }}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {/* key로 변형 전환 시 깨끗하게 remount → 회사/개인 데이터 재적재 */}
        <ConnectorsWorkspace key={activeVariant} variant={activeVariant} />
      </div>
    </div>
  );
}

export function ConnectorsWorkspace({
  variant = "company",
}: {
  variant?: ConnectorsVariant;
}) {
  const [data, setData] = useState<ConnectorsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [lastRun, setLastRun] = useState<ConnectorSyncResult | null>(null);
  const [collectorCommands, setCollectorCommands] = useState<CollectorCommand[]>([]);
  const [collectorCommandError, setCollectorCommandError] = useState<string | null>(null);
  const [collectorEvidence, setCollectorEvidence] = useState<CollectorEvidenceResponse | null>(null);
  const [collectorEvidenceError, setCollectorEvidenceError] = useState<string | null>(null);
  const [collectorCompileRetry, setCollectorCompileRetry] =
    useState<CollectorCompileResponse | null>(null);
  const [collectorCompileRetryBusy, setCollectorCompileRetryBusy] = useState(false);
  const [collectorDeleteResult, setCollectorDeleteResult] =
    useState<CollectorPersonalDataDeleteResponse | null>(null);
  const [collectorDeleteBusy, setCollectorDeleteBusy] = useState<string | null>(null);
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  // ≥1280px shows the detail panel inline as a right column; below that the
  // panel would fall off the fold, so it opens as a slide-over on 설정 click.
  const [wide, setWide] = useState(false);
  const connectorScope: ConnectorScope | undefined =
    variant === "personal" ? "personal" : undefined;

  useEffect(() => {
    if (typeof window === "undefined") return;
    function syncWide() {
      setWide(window.innerWidth >= 1280);
    }
    syncWide();
    window.addEventListener("resize", syncWide);
    return () => window.removeEventListener("resize", syncWide);
  }, []);

  const loadCollectorCommandsFor = useCallback(
    async (connectorData: ConnectorsResponse) => {
      if (variant !== "personal" || connectorData.node_kind !== "company") {
        setCollectorCommands([]);
        setCollectorCommandError(null);
        return;
      }
      try {
        const res = await api.listCollectorCommands();
        setCollectorCommands(res.items);
        setCollectorCommandError(null);
      } catch (e) {
        setCollectorCommands([]);
        setCollectorCommandError(
          e instanceof Error ? e.message : "collector command queue를 불러오지 못했습니다.",
        );
      }
    },
    [variant],
  );

  const loadCollectorEvidenceFor = useCallback(
    async (connectorData: ConnectorsResponse) => {
      if (variant !== "personal" || connectorData.node_kind !== "company") {
        setCollectorEvidence(null);
        setCollectorEvidenceError(null);
        return;
      }
      try {
        const res = await api.getCollectorEvidence();
        setCollectorEvidence(res);
        setCollectorEvidenceError(null);
      } catch (e) {
        setCollectorEvidence(null);
        setCollectorEvidenceError(
          e instanceof Error ? e.message : "collector evidence를 불러오지 못했습니다.",
        );
      }
    },
    [variant],
  );

  const refresh = useCallback(async () => {
    try {
      const res = await api.listConnectors(connectorScope);
      setData(res);
      await loadCollectorCommandsFor(res);
      await loadCollectorEvidenceFor(res);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "커넥터 상태를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, [connectorScope, loadCollectorCommandsFor, loadCollectorEvidenceFor]);

  useEffect(() => {
    let cancelled = false;

    async function loadInitial() {
      try {
        const res = await api.listConnectors(connectorScope);
        if (cancelled) return;
        setData(res);
        await loadCollectorCommandsFor(res);
        await loadCollectorEvidenceFor(res);
        if (cancelled) return;
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "커넥터 상태를 불러오지 못했습니다.");
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void loadInitial();

    return () => {
      cancelled = true;
    };
  }, [connectorScope, loadCollectorCommandsFor, loadCollectorEvidenceFor]);

  // P6.7.5: a slug can now have several accounts (one per mailbox for mail slugs).
  // Non-mail slugs still resolve to 0/1 account, so the single-account path is
  // unchanged — `accountForSlug` returns the first (only) row for those.
  const accountsBySlug = useMemo(() => {
    const out = new Map<string, ConnectorAccount[]>();
    for (const account of data?.accounts ?? []) {
      const rows = out.get(account.connector_slug) ?? [];
      rows.push(account);
      out.set(account.connector_slug, rows);
    }
    return out;
  }, [data]);
  const accountForSlug = useCallback(
    (slug: string): ConnectorAccount | null => accountsBySlug.get(slug)?.[0] ?? null,
    [accountsBySlug],
  );
  const runsBySlug = useMemo(() => {
    const out = new Map<string, ConnectorRun[]>();
    for (const run of data?.runs ?? []) {
      const runs = out.get(run.connector_slug) ?? [];
      runs.push(run);
      out.set(run.connector_slug, runs);
    }
    return out;
  }, [data]);
  const docsBySlug = useMemo(() => {
    const out = new Map<string, ConnectorDocument[]>();
    for (const doc of data?.documents ?? []) {
      const docs = out.get(doc.connector_slug) ?? [];
      docs.push(doc);
      out.set(doc.connector_slug, docs);
    }
    return out;
  }, [data]);
  const runningSlugs = useMemo(() => {
    const out = new Set<string>();
    for (const run of data?.runs ?? []) {
      if (run.status === "running") out.add(run.connector_slug);
    }
    return out;
  }, [data]);
  const collectorCommandsBySlug = useMemo(() => {
    const out = new Map<string, CollectorCommand[]>();
    for (const command of collectorCommands) {
      if (command.kind !== "connector_sync") continue;
      const connector = collectorCommandConnector(command);
      if (!connector) continue;
      const commands = out.get(connector) ?? [];
      commands.push(command);
      out.set(connector, commands);
    }
    for (const commands of out.values()) {
      commands.sort(compareCollectorCommands);
    }
    return out;
  }, [collectorCommands]);
  const activeCollectorCommandSlugs = useMemo(() => {
    const out = new Set<string>();
    for (const [connectorSlug, commands] of collectorCommandsBySlug) {
      if (commands.some(isActiveCollectorCommand)) out.add(connectorSlug);
    }
    return out;
  }, [collectorCommandsBySlug]);

  useEffect(() => {
    if (!runningSlugs.size) return;
    const id = window.setInterval(() => {
      void refresh();
    }, 1500);
    return () => window.clearInterval(id);
  }, [refresh, runningSlugs]);

  useEffect(() => {
    if (!activeCollectorCommandSlugs.size) return;
    const id = window.setInterval(() => {
      void refresh();
    }, 3000);
    return () => window.clearInterval(id);
  }, [activeCollectorCommandSlugs, refresh]);

  const selectedManifest = useMemo(() => {
    if (!data?.manifests.length) return null;
    return (
      data.manifests.find((manifest) => manifest.slug === selectedSlug) ??
      data.manifests[0]
    );
  }, [data, selectedSlug]);
  const selectedAccount = selectedManifest ? accountForSlug(selectedManifest.slug) : null;
  const selectedAccounts = selectedManifest
    ? (accountsBySlug.get(selectedManifest.slug) ?? [])
    : [];
  const visibleLastRun = useMemo(() => {
    if (!lastRun) return null;
    const run = data?.runs.find((item) => item.run_id === lastRun.run_id);
    return run ? runToSyncResult(run, lastRun) : lastRun;
  }, [data, lastRun]);
  const remotePersonalWorkspace = variant === "personal" && data?.node_kind === "company";
  const orderedManifests = useMemo(() => {
    if (!data) return [];
    return [...data.manifests].sort((a, b) => {
      const aCount = accountsBySlug.get(a.slug)?.length ?? 0;
      const bCount = accountsBySlug.get(b.slug)?.length ?? 0;
      return (
        connectorSortRank(a, aCount, remotePersonalWorkspace) -
          connectorSortRank(b, bCount, remotePersonalWorkspace) ||
        connectorSuggestedOrder(a.slug) - connectorSuggestedOrder(b.slug) ||
        a.label.localeCompare(b.label, "ko")
      );
    });
  }, [accountsBySlug, data, remotePersonalWorkspace]);

  async function sync(slug: string) {
    if (remotePersonalWorkspace) {
      if (!COLLECTOR_SUPPORTED_SOURCES.has(slug)) {
        setError(`${slug}은 로컬 수집기 sync를 아직 지원하지 않습니다.`);
        setSelectedSlug(slug);
        return;
      }
      setBusy({ slug, action: "sync" });
      setError(null);
      setCollectorCommandError(null);
      try {
        const command = await api.createCollectorCommand({
          kind: "connector_sync",
          payload: { connector: slug },
        });
        setCollectorCommands((prev) => [
          command,
          ...prev.filter((item) => item.command_id !== command.command_id),
        ]);
        if (data) {
          await loadCollectorCommandsFor(data);
        }
        setSelectedSlug(slug);
        if (data) {
          await loadCollectorEvidenceFor(data);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "collector command enqueue 실패");
      } finally {
        setBusy(null);
      }
      return;
    }
    setBusy({ slug, action: "sync" });
    setError(null);
    try {
      const result = await api.syncConnector(slug, 8, true, connectorScope);
      setLastRun(result);
      await refresh();
      setSelectedSlug(slug);
    } catch (e) {
      setError(e instanceof Error ? e.message : "동기화 실패");
    } finally {
      setBusy(null);
    }
  }

  // P6.7.5: sync one mailbox row (mail slugs only). Goes through the same
  // owner-scoped sync route with an explicit account_id discriminator.
  async function syncMailbox(slug: string, accountId: string) {
    setBusy({ slug, action: "sync" });
    setError(null);
    try {
      const result = await api.syncConnector(slug, 8, true, connectorScope, accountId);
      setLastRun(result);
      await refresh();
      setSelectedSlug(slug);
    } catch (e) {
      setError(e instanceof Error ? e.message : "메일함 동기화 실패");
    } finally {
      setBusy(null);
    }
  }

  // P6.7.5: delete one mailbox row (owner-checked server-side; 404 if not yours).
  async function deleteMailbox(slug: string, accountId: string, label: string) {
    if (!window.confirm(`이 메일함을 삭제할까요?\n\n${label}`)) {
      return;
    }
    setBusy({ slug, action: "sync" });
    setError(null);
    try {
      await api.deleteConnectorAccount(slug, accountId, connectorScope);
      await refresh();
      setSelectedSlug(slug);
    } catch (e) {
      setError(e instanceof Error ? e.message : "메일함 삭제 실패");
    } finally {
      setBusy(null);
    }
  }

  // P6.7.5: add/update one mailbox row from the web UI (owner_addr keyed, so the
  // same domain can hold several). Nova authenticates server-side with the
  // shared APP_API_KEY and the explicit owner_addr stored here.
  async function addMailbox(slug: string, ownerAddr: string, ingestScope: string) {
    const addr = ownerAddr.trim().toLowerCase();
    if (!addr) return;
    setBusy({ slug, action: "sync" });
    setError(null);
    try {
      await api.configureConnector(
        slug,
        { settings: { owner_addr: addr, ingest_scope: ingestScope } },
        connectorScope,
      );
      await refresh();
      setSelectedSlug(slug);
    } catch (e) {
      setError(e instanceof Error ? e.message : "메일함 추가 실패");
    } finally {
      setBusy(null);
    }
  }

  async function retryCollectorCompile() {
    setCollectorCompileRetryBusy(true);
    setCollectorEvidenceError(null);
    try {
      const result = await api.retryCollectorCompile();
      setCollectorCompileRetry(result);
      if (data) {
        await loadCollectorEvidenceFor(data);
      }
    } catch (e) {
      setCollectorEvidenceError(e instanceof Error ? e.message : "personal wiki compile retry 실패");
    } finally {
      setCollectorCompileRetryBusy(false);
    }
  }

  async function deleteCollectorDocument(docId: string, title: string) {
    if (!window.confirm(`이 personal collector 문서를 삭제할까요?\n\n${title || docId}`)) {
      return;
    }
    setCollectorDeleteBusy(`doc:${docId}`);
    setCollectorEvidenceError(null);
    setCollectorDeleteResult(null);
    try {
      const result = await api.deleteCollectorDocument(docId);
      setCollectorDeleteResult(result);
      if (data) {
        await loadCollectorEvidenceFor(data);
      }
    } catch (e) {
      setCollectorEvidenceError(e instanceof Error ? e.message : "personal collector 문서 삭제 실패");
    } finally {
      setCollectorDeleteBusy(null);
    }
  }

  async function pruneCollectorSource(source: string, olderThanDays: number) {
    if (
      !window.confirm(
        `${source} personal collector 문서 중 ${olderThanDays}일보다 오래된 항목을 삭제할까요?`,
      )
    ) {
      return;
    }
    setCollectorDeleteBusy(`prune:${source}`);
    setCollectorEvidenceError(null);
    setCollectorDeleteResult(null);
    try {
      const result = await api.pruneCollectorPersonalData({
        older_than_days: olderThanDays,
        source,
      });
      setCollectorDeleteResult(result);
      if (data) {
        await loadCollectorEvidenceFor(data);
      }
    } catch (e) {
      setCollectorEvidenceError(e instanceof Error ? e.message : "personal collector prune 실패");
    } finally {
      setCollectorDeleteBusy(null);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title={variant === "personal" ? "개인 커넥터" : "커넥터"}
        subtitle={
          data
            ? variant === "personal"
              ? `개인 워크스페이스 · ${data.node_id} · ${data.accounts.length} accounts`
              : `${data.node_id} · ${data.node_kind} · ${data.accounts.length} accounts`
            : variant === "personal"
              ? "personal connector registry"
              : "connector registry"
        }
        right={
          <Toolbar>
            <ToolbarButton icon={<RefreshIcon />} onClick={() => void refresh()}>
              새로고침
            </ToolbarButton>
          </Toolbar>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[1280px] px-3 py-3 sm:px-5 sm:py-5">
          {error ? (
            <div className="mb-4">
              <Banner tone="fail" title="오류">
                {error}
              </Banner>
            </div>
          ) : null}

          {loading ? (
            <ConnectorSkeleton />
          ) : data ? (
            <>
              {variant === "personal" ? (
                <AgentSetupHub
                  manifests={orderedManifests}
                  centralUrl={resolveCollectorCentralUrl()}
                />
              ) : null}
              <PersonalDetailGate variant={variant}>
              {collectorCommandError ? (
                <div className="mb-4">
                  <Banner tone="warn" title="collector command queue">
                    {collectorCommandError}
                  </Banner>
                </div>
              ) : null}
              <ConnectorQuickStart
                manifests={orderedManifests}
                accountsBySlug={accountsBySlug}
                runsBySlug={runsBySlug}
                runningSlugs={runningSlugs}
                activeCollectorCommandSlugs={activeCollectorCommandSlugs}
                collectorCommandsBySlug={collectorCommandsBySlug}
                remotePersonalWorkspace={remotePersonalWorkspace}
                busy={busy}
                onSelect={setSelectedSlug}
                onSync={sync}
              />
              {remotePersonalWorkspace ? (
                <AdvancedDisclosure
                  className="mb-4"
                  title="Collector 세부 설정"
                  subtitle={collectorEvidence?.liveness.status ?? "status 확인 중"}
                >
                  <CollectorEvidencePanel
                    evidence={collectorEvidence}
                    error={collectorEvidenceError}
                    retryResult={collectorCompileRetry}
                    retryBusy={collectorCompileRetryBusy}
                    deleteResult={collectorDeleteResult}
                    deleteBusy={collectorDeleteBusy}
                    onRetryCompile={retryCollectorCompile}
                    onDeleteDocument={deleteCollectorDocument}
                    onPruneSource={pruneCollectorSource}
                  />
                </AdvancedDisclosure>
              ) : null}
              {(() => {
                const detailNode = (
                  <ConnectorDetail
                    key={selectedManifest?.slug ?? "empty"}
                    manifest={selectedManifest}
                    account={selectedAccount}
                    accounts={selectedAccounts}
                    onSyncMailbox={syncMailbox}
                    onDeleteMailbox={deleteMailbox}
                    onAddMailbox={addMailbox}
                    runs={selectedManifest ? (runsBySlug.get(selectedManifest.slug) ?? []) : []}
                    documents={selectedManifest ? (docsBySlug.get(selectedManifest.slug) ?? []) : []}
                    collectorCommands={
                      selectedManifest ? (collectorCommandsBySlug.get(selectedManifest.slug) ?? []) : []
                    }
                    remotePersonalWorkspace={remotePersonalWorkspace}
                    collectorSupported={
                      !remotePersonalWorkspace ||
                      (selectedManifest
                        ? COLLECTOR_SUPPORTED_SOURCES.has(selectedManifest.slug)
                        : true)
                    }
                    busy={busy}
                    lastRun={
                      selectedManifest && visibleLastRun?.connector_slug === selectedManifest.slug
                        ? visibleLastRun
                        : null
                    }
                  />
                );
                return (
                  <>
                    <div
                      className={cx(
                        "grid grid-cols-1 gap-3",
                        wide && "grid-cols-[minmax(0,1fr)_380px]",
                      )}
                    >
                      <div
                        className={cx(
                          "grid auto-rows-min grid-cols-1 content-start gap-3",
                          wide && "grid-cols-2",
                        )}
                      >
                        {orderedManifests.map((manifest) => {
                          const commands = collectorCommandsBySlug.get(manifest.slug) ?? [];
                          return (
                            <ConnectorCard
                              key={manifest.slug}
                              manifest={manifest}
                              account={accountForSlug(manifest.slug)}
                              accountCount={accountsBySlug.get(manifest.slug)?.length ?? 0}
                              running={runningSlugs.has(manifest.slug)}
                              collectorCommand={commands[0] ?? null}
                              collectorCommandActive={activeCollectorCommandSlugs.has(manifest.slug)}
                              busy={busy}
                              selected={selectedManifest?.slug === manifest.slug}
                              remotePersonalWorkspace={remotePersonalWorkspace}
                              collectorSupported={
                                !remotePersonalWorkspace ||
                                COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug)
                              }
                              onSelect={setSelectedSlug}
                              onSync={sync}
                            />
                          );
                        })}
                      </div>
                      {wide ? detailNode : null}
                    </div>
                    {!wide && selectedSlug ? (
                      <ConnectorSettingsOverlay onClose={() => setSelectedSlug(null)}>
                        {detailNode}
                      </ConnectorSettingsOverlay>
                    ) : null}
                  </>
                );
              })()}
              </PersonalDetailGate>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function runToSyncResult(run: ConnectorRun, fallback: ConnectorSyncResult): ConnectorSyncResult {
  return {
    account_id: run.account_id,
    connector_slug: run.connector_slug,
    run_id: run.run_id,
    status: run.status,
    report:
      run.status === "running"
        ? null
        : {
            created: run.created,
            updated: run.updated,
            skipped: run.skipped,
            errors: run.errors,
            total: run.fetched,
            doc_ids: fallback.report?.doc_ids ?? [],
          },
    error_message: run.error_message,
  };
}

function ConnectorQuickStart({
  manifests,
  accountsBySlug,
  runsBySlug,
  runningSlugs,
  activeCollectorCommandSlugs,
  collectorCommandsBySlug,
  remotePersonalWorkspace,
  busy,
  onSelect,
  onSync,
}: {
  manifests: ConnectorManifest[];
  accountsBySlug: Map<string, ConnectorAccount[]>;
  runsBySlug: Map<string, ConnectorRun[]>;
  runningSlugs: Set<string>;
  activeCollectorCommandSlugs: Set<string>;
  collectorCommandsBySlug: Map<string, CollectorCommand[]>;
  remotePersonalWorkspace: boolean;
  busy: Busy;
  onSelect: (slug: string) => void;
  onSync: (slug: string) => Promise<void>;
}) {
  const connectedCount = manifests.filter((manifest) =>
    connectorIsOnboarded(
      manifest,
      accountsBySlug.get(manifest.slug)?.length ?? 0,
      collectorCommandsBySlug.get(manifest.slug) ?? [],
      remotePersonalWorkspace,
    ),
  ).length;
  const readyCount = manifests.filter((manifest) =>
    connectorCanStartNow(manifest, remotePersonalWorkspace),
  ).length;
  const needsSetupCount = manifests.filter((manifest) =>
    connectorNeedsSetup(manifest, accountsBySlug.get(manifest.slug)?.length ?? 0),
  ).length;
  // Quick start surfaces every connector still waiting to be onboarded — not a
  // top-3 sample. Once a source is onboarded (account connected, or a Desktop
  // Collector sync has completed) it drops out and lives in the detail section
  // below. Legacy sources unsupported in this workspace are never actionable
  // here, so they are excluded too.
  const quickItems = manifests.filter((manifest) => {
    const accountCount = accountsBySlug.get(manifest.slug)?.length ?? 0;
    const commands = collectorCommandsBySlug.get(manifest.slug) ?? [];
    if (connectorIsOnboarded(manifest, accountCount, commands, remotePersonalWorkspace)) {
      return false;
    }
    if (remotePersonalWorkspace && !COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug)) {
      return false;
    }
    return true;
  });

  // Everything onboarded (or nothing actionable) -> hide the card entirely; the
  // detail section keeps full management of connected sources.
  if (!quickItems.length) return null;

  return (
    <Card className="mb-3 p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2
              className="text-[var(--text-body)] font-extrabold"
              style={{ color: "var(--color-text-strong)" }}
            >
              연결 시작
            </h2>
            <Chip tone={connectedCount ? "pass" : "muted"}>{connectedCount} 연결됨</Chip>
            <Chip tone={readyCount ? "pass" : "muted"}>{readyCount} 바로 가능</Chip>
            {needsSetupCount ? <Chip tone="warn">{needsSetupCount} 설정 필요</Chip> : null}
          </div>
          <p className="mt-1 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            먼저 쓸 소스만 고르고, 필수값이 있으면 그 값만 저장합니다.
          </p>
        </div>
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-2 2xl:grid-cols-3">
        {quickItems.map((manifest) => {
            const accounts = accountsBySlug.get(manifest.slug) ?? [];
            const runs = runsBySlug.get(manifest.slug) ?? [];
            const isRunning = runningSlugs.has(manifest.slug);
            const collectorActive = activeCollectorCommandSlugs.has(manifest.slug);
            const busyHere = busy?.slug === manifest.slug;
            const canSync =
              connectorCanStartNow(manifest, remotePersonalWorkspace) || accounts.length > 0;
            return (
              <div
                key={manifest.slug}
                className="flex min-w-0 flex-wrap items-center justify-between gap-2 rounded-[6px] border px-2 py-2"
                style={{
                  background: "var(--color-app-canvas)",
                  borderColor: "var(--color-divider-soft)",
                }}
              >
                <div className="min-w-0">
                  <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                    <span
                      className="truncate text-[var(--text-body-sm)] font-semibold"
                      style={{ color: "var(--color-text-strong)" }}
                      title={manifest.label}
                    >
                      {manifest.label}
                    </span>
                    <Chip tone={quickStateTone(manifest, accounts.length, remotePersonalWorkspace)}>
                      {quickStateText(manifest, accounts.length, remotePersonalWorkspace)}
                    </Chip>
                  </div>
                  <p
                    className="mt-1 truncate text-[var(--text-meta)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {quickHintText(manifest, accounts.length, runs[0] ?? null)}
                  </p>
                </div>
                <ToolbarButton
                  className="h-11 sm:h-8"
                  tone={canSync ? "primary" : "default"}
                  icon={canSync ? <SyncSmallIcon /> : <InfoSmallIcon />}
                  disabled={canSync && (isRunning || collectorActive || Boolean(busy))}
                  onClick={() => {
                    onSelect(manifest.slug);
                    if (canSync) void onSync(manifest.slug);
                  }}
                >
                  {canSync
                    ? isRunning || collectorActive || busyHere
                      ? "진행 중"
                      : remotePersonalWorkspace
                        ? "요청"
                        : "동기화"
                    : "설정"}
                </ToolbarButton>
              </div>
            );
          })}
      </div>
    </Card>
  );
}

function ConnectorCard({
  manifest,
  account,
  accountCount,
  running,
  collectorCommand,
  collectorCommandActive,
  busy,
  selected,
  remotePersonalWorkspace,
  collectorSupported,
  onSelect,
  onSync,
}: {
  manifest: ConnectorManifest;
  account: ConnectorAccount | null;
  accountCount: number;
  running: boolean;
  collectorCommand: CollectorCommand | null;
  collectorCommandActive: boolean;
  busy: Busy;
  selected: boolean;
  remotePersonalWorkspace: boolean;
  collectorSupported: boolean;
  onSelect: (slug: string) => void;
  onSync: (slug: string) => Promise<void>;
}) {
  const isSyncing = running || (busy?.slug === manifest.slug && busy.action === "sync");
  const canUse =
    (remotePersonalWorkspace &&
      collectorSupported &&
      (accountCount > 0 || manifest.default_configured || requiredFieldCount(manifest) === 0)) ||
    (manifest.can_ensure_default && manifest.default_configured);
  const managedState = collectorManagedState(
    manifest,
    account,
    remotePersonalWorkspace,
    collectorSupported,
  );
  const statusTone =
    managedState?.tone ?? (account ? "pass" : manifest.default_configured ? "muted" : "warn");
  const commandSummary = collectorCommand ? collectorCommandSummary(collectorCommand) : null;
  const setupFields = requiredFieldCount(manifest);
  const readiness = cardReadinessText(manifest, accountCount, remotePersonalWorkspace);
  const syncButtonText = remotePersonalWorkspace
    ? isSyncing
      ? "요청 중"
      : collectorCommandActive
        ? (collectorCommand?.status === "claimed" ? "실행 중" : "대기 중")
        : "동기화 요청"
    : isSyncing
      ? "동기화 중"
      : "동기화";

  return (
    <Card className="flex min-h-[150px] flex-col p-3" interactive>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2
              className="truncate text-[var(--text-body)] font-extrabold"
              style={{ color: "var(--color-text-strong)" }}
              title={manifest.label}
            >
              {manifest.label}
            </h2>
            <Chip tone={statusTone}>
              {managedState?.label ?? connectorStateText(manifest, account)}
            </Chip>
            {remotePersonalWorkspace && collectorCommand ? (
              <Chip tone={collectorCommandTone(collectorCommand.status)}>
                {collectorCommandLabel(collectorCommand.status)}
              </Chip>
            ) : null}
          </div>
          <p
            className="mt-1 max-h-[38px] overflow-hidden text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {manifest.description || manifest.slug}
          </p>
        </div>

        <div className="connector-card-actions flex shrink-0 flex-wrap justify-end gap-1.5">
          <ToolbarButton icon={<InfoSmallIcon />} onClick={() => onSelect(manifest.slug)}>
            설정
          </ToolbarButton>
          <ToolbarButton
            tone="primary"
            icon={<SyncSmallIcon />}
            title={
              remotePersonalWorkspace
                ? collectorSupported
                  ? "로컬 수집기 command queue에 등록합니다."
                  : "로컬 수집기가 아직 실행하지 않는 connector입니다."
                : undefined
            }
            disabled={!canUse || running || collectorCommandActive || Boolean(busy)}
            onClick={() => void onSync(manifest.slug)}
          >
            {syncButtonText}
          </ToolbarButton>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-1.5">
        <Chip tone={setupFields ? "warn" : "pass"}>
          {setupFields ? `필수값 ${setupFields}개` : "설정 없음"}
        </Chip>
        <Chip tone="neutral">{readiness}</Chip>
        {selected ? <Chip tone="neutral">선택됨</Chip> : null}
      </div>

      <div
        className="mt-auto flex flex-wrap justify-between gap-2 border-t pt-2 text-[var(--text-meta)]"
        style={{ borderColor: "var(--color-divider-soft)", color: "var(--color-text-muted)" }}
      >
        {isMailSlug(manifest.slug) ? (
          <span className="truncate">
            {accountCount ? `${accountCount}개 메일함` : "메일함 없음"}
          </span>
        ) : (
          <span className="truncate">{nextConnectorActionText(manifest, account, remotePersonalWorkspace)}</span>
        )}
        {remotePersonalWorkspace && commandSummary ? (
          <span
            className="truncate"
            style={{
              color:
                collectorCommand?.status === "failed"
                  ? "var(--color-fail-fg)"
                  : "var(--color-text-muted)",
            }}
            title={commandSummary}
          >
            {commandSummary}
          </span>
        ) : account?.last_error ? (
          <span className="truncate" style={{ color: "var(--color-fail-fg)" }}>
            {account.last_error}
          </span>
        ) : manifest.config_error ? (
          <span className="truncate" style={{ color: "var(--color-warn-fg)" }}>
            {manifest.config_error}
          </span>
        ) : (
          <span>ready</span>
        )}
      </div>
    </Card>
  );
}

// Below the inline-column breakpoint the detail panel opens here as a
// right-anchored slide-over (full-width on phones), so 설정 is reachable at
// any window size. Backdrop click / ✕ / Esc all close it.
function ConnectorSettingsOverlay({
  children,
  onClose,
}: {
  children: ReactNode;
  onClose: () => void;
}) {
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div aria-modal="true" role="dialog" className="fixed inset-0 z-50 flex justify-end">
      <button
        aria-label="설정 닫기"
        className="absolute inset-0 h-full w-full"
        onClick={onClose}
        style={{ background: "rgba(0, 0, 0, 0.22)" }}
        type="button"
      />
      <section
        className="relative flex h-full w-full max-w-[460px] flex-col"
        style={{
          background: "var(--color-app-canvas)",
          borderLeft: "1px solid var(--color-divider)",
          boxShadow: "-16px 0 42px rgba(0, 0, 0, 0.18)",
        }}
      >
        <div
          className="flex shrink-0 items-center justify-between gap-3 px-3 py-2.5"
          style={{ borderBottom: "1px solid var(--color-divider)" }}
        >
          <span
            className="text-[var(--text-body-sm)] font-extrabold"
            style={{ color: "var(--color-text-strong)" }}
          >
            커넥터 설정
          </span>
          <button
            aria-label="닫기"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[6px]"
            onClick={onClose}
            style={{
              background: "var(--color-card)",
              border: "1px solid var(--color-divider)",
              color: "var(--color-text-body)",
            }}
            type="button"
          >
            <X size={16} aria-hidden />
          </button>
        </div>
        <div
          className="min-h-0 flex-1 overflow-y-auto p-2"
          style={{ paddingBottom: "var(--mobile-safe-bottom)" }}
        >
          {children}
        </div>
      </section>
    </div>
  );
}

function ConnectorDetail({
  manifest,
  account,
  accounts,
  runs,
  documents,
  collectorCommands,
  remotePersonalWorkspace,
  collectorSupported,
  busy,
  lastRun,
  onSyncMailbox,
  onDeleteMailbox,
  onAddMailbox,
}: {
  manifest: ConnectorManifest | null;
  account: ConnectorAccount | null;
  accounts: ConnectorAccount[];
  runs: ConnectorRun[];
  documents: ConnectorDocument[];
  collectorCommands: CollectorCommand[];
  remotePersonalWorkspace: boolean;
  collectorSupported: boolean;
  busy: Busy;
  lastRun: ConnectorSyncResult | null;
  onSyncMailbox: (slug: string, accountId: string) => Promise<void>;
  onDeleteMailbox: (slug: string, accountId: string, label: string) => Promise<void>;
  onAddMailbox: (slug: string, ownerAddr: string, ingestScope: string) => Promise<void>;
}) {
  const [documentFilter, setDocumentFilter] = useState("");

  if (!manifest) {
    return (
      <Card className="p-3">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="mt-2 h-24 w-full" />
      </Card>
    );
  }

  const guide = connectorGuide(manifest, {
    remotePersonalWorkspace,
    collectorSupported,
  });
  const filteredDocuments = filterConnectorDocuments(documents, documentFilter);
  const requiredCount = requiredFieldCount(manifest);
  const latestRun = runs[0] ?? null;

  return (
    <Card className="p-3 xl:sticky xl:top-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2
            className="truncate text-[var(--text-body)] font-extrabold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {manifest.label}
          </h2>
          <p className="mt-1 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            {manifest.slug}
          </p>
        </div>
        {(() => {
          const managed = collectorManagedState(
            manifest,
            account,
            remotePersonalWorkspace,
            collectorSupported,
          );
          const tone =
            managed?.tone ?? (account ? "pass" : manifest.default_configured ? "neutral" : "warn");
          const label =
            managed?.label ??
            (account ? "연결됨" : manifest.default_configured ? "준비됨" : "설정 필요");
          return <Chip tone={tone}>{label}</Chip>;
        })()}
      </div>

      <ConnectorNextStep
        manifest={manifest}
        account={account}
        accountCount={accounts.length}
        latestRun={latestRun}
        remotePersonalWorkspace={remotePersonalWorkspace}
        collectorSupported={collectorSupported}
      />

      <div
        className="mt-3 grid grid-cols-1 gap-2 rounded-[6px] p-2 sm:grid-cols-2"
        style={{
          background: "var(--color-app-canvas)",
          border: "1px solid var(--color-divider-soft)",
        }}
      >
        <Meta label="status" value={account ? account.status : "not connected"} />
        <Meta label="last sync" value={formatDate(account?.last_sync_at ?? null)} />
        <Meta label="node" value={account?.node_id ?? "current"} />
        <Meta label="scope" value={account?.scope ?? dataScopeText(manifest)} />
      </div>

      <GuidePreview items={guide} />

      {isMailSlug(manifest.slug) ? (
        <MailAccountsSection
          manifest={manifest}
          accounts={accounts}
          busy={busy?.slug === manifest.slug ? busy.action : null}
          onSyncMailbox={onSyncMailbox}
          onDeleteMailbox={onDeleteMailbox}
          onAddMailbox={onAddMailbox}
        />
      ) : manifest.config_fields.length ? (
        <>
          <SectionTitle>{requiredCount ? "현재 설정" : "설정"}</SectionTitle>
          {account?.settings_redacted && Object.keys(account.settings_redacted).length ? (
            <div
              className="space-y-1 rounded-[6px] border p-2"
              style={{
                background: "var(--color-app-canvas)",
                borderColor: "var(--color-divider-soft)",
              }}
            >
              {Object.entries(account.settings_redacted).map(([k, v]) => (
                <div key={k} className="flex min-w-0 gap-2 text-[var(--text-body-sm)]">
                  <span
                    className="shrink-0 font-bold uppercase"
                    style={{ color: "var(--color-text-faint)" }}
                  >
                    {k}
                  </span>
                  <span
                    className="min-w-0 truncate font-[family-name:var(--font-mono)]"
                    style={{ color: "var(--color-text-body)" }}
                    title={String(v ?? "")}
                  >
                    {String(v ?? "")}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
          <p
            className="mt-2 text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            설정은 CLI에서:{" "}
            <code>orthus connector config {manifest.slug} --set &lt;key&gt;=&lt;value&gt;</code>
          </p>
        </>
      ) : null}

      {account?.last_error ? (
        <div className="mt-3">
          <Banner tone="fail" title="last error">
            {account.last_error}
          </Banner>
        </div>
      ) : manifest.config_error && manifest.config_fields.length ? (
        <div className="mt-3">
          <Banner tone="warn" title="필수값 필요">
            {manifest.config_error}
          </Banner>
        </div>
      ) : null}

      <AdvancedDisclosure
        className="mt-4"
        title="운영 기록"
        subtitle={`${runs.length} runs · ${documents.length} docs${
          remotePersonalWorkspace ? ` · ${collectorCommands.length} commands` : ""
        }`}
      >
        {remotePersonalWorkspace ? (
          <>
            <SectionTitle>collector queue</SectionTitle>
            <CollectorCommandHistory commands={collectorCommands} />
          </>
        ) : null}

        <SectionTitle>recent runs</SectionTitle>
        {lastRun ? (
          <Banner tone={statusBannerTone(lastRun.status)} title="current session">
            {lastRun.report
              ? `created ${lastRun.report.created} · updated ${lastRun.report.updated} · skipped ${lastRun.report.skipped} · errors ${lastRun.report.errors}`
              : (lastRun.error_message ?? "report 없음")}
          </Banner>
        ) : null}
        <RunHistory runs={runs} />

        <SectionTitle>recent docs</SectionTitle>
        <ConnectorDocuments
          documents={filteredDocuments}
          total={documents.length}
          filter={documentFilter}
          onFilterChange={setDocumentFilter}
        />
      </AdvancedDisclosure>
    </Card>
  );
}

function ConnectorNextStep({
  manifest,
  account,
  accountCount,
  latestRun,
  remotePersonalWorkspace,
  collectorSupported,
}: {
  manifest: ConnectorManifest;
  account: ConnectorAccount | null;
  accountCount: number;
  latestRun: ConnectorRun | null;
  remotePersonalWorkspace: boolean;
  collectorSupported: boolean;
}) {
  const tone = account
    ? "pass"
    : connectorCanStartNow(manifest, remotePersonalWorkspace) && collectorSupported
      ? "neutral"
      : "warn";
  return (
    <div
      className="mt-3 rounded-[6px] border p-2"
      style={{
        background: "var(--color-app-canvas)",
        borderColor: "var(--color-divider-soft)",
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <Chip tone={tone}>{quickStateText(manifest, accountCount, remotePersonalWorkspace)}</Chip>
          <span
            className="truncate text-[var(--text-body-sm)] font-semibold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {nextConnectorActionText(manifest, account, remotePersonalWorkspace)}
          </span>
        </div>
        <Chip tone="muted">{formatInterval(manifest.default_interval_seconds)}</Chip>
      </div>
      <p className="mt-2 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
        {quickHintText(manifest, accountCount, latestRun)}
      </p>
    </div>
  );
}

function GuidePreview({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <details className="group mt-3">
      <summary
        className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-2 rounded-[6px] border px-2 text-[var(--text-body-sm)] font-semibold sm:min-h-8"
        style={{
          background: "var(--color-app-canvas)",
          borderColor: "var(--color-divider-soft)",
          color: "var(--color-text-strong)",
        }}
      >
        <span className="min-w-0 truncate">{items[0]}</span>
        <ChevronDown
          className="h-4 w-4 shrink-0 transition-transform group-open:rotate-180"
          aria-hidden
        />
      </summary>
      <div className="mt-2 space-y-1.5 rounded-[6px] border p-2" style={{
        background: "var(--color-app-canvas)",
        borderColor: "var(--color-divider-soft)",
      }}>
        <GuideList items={items.slice(1)} />
      </div>
    </details>
  );
}

function AdvancedDisclosure({
  title,
  subtitle,
  className,
  children,
}: {
  title: string;
  subtitle?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <details
      className={cx("group rounded-[6px] border", className)}
      style={{
        background: "var(--color-app-canvas)",
        borderColor: "var(--color-divider-soft)",
      }}
    >
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 sm:min-h-9">
        <div className="min-w-0">
          <div
            className="text-[var(--text-body-sm)] font-extrabold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {title}
          </div>
          {subtitle ? (
            <div className="truncate text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
              {subtitle}
            </div>
          ) : null}
        </div>
        <ChevronDown
          className="h-4 w-4 shrink-0 transition-transform group-open:rotate-180"
          aria-hidden
        />
      </summary>
      <div className="border-t p-3" style={{ borderColor: "var(--color-divider-soft)" }}>
        {children}
      </div>
    </details>
  );
}

function resolveCollectorCentralUrl(): string {
  if (/^https?:\/\//.test(API_BASE)) return API_BASE;
  if (typeof window === "undefined") return API_BASE;
  const path = API_BASE.startsWith("/") ? API_BASE : `/${API_BASE}`;
  return `${window.location.origin}${path}`;
}

function CollectorEvidencePanel({
  evidence,
  error,
  retryResult,
  retryBusy,
  deleteResult,
  deleteBusy,
  onRetryCompile,
  onDeleteDocument,
  onPruneSource,
}: {
  evidence: CollectorEvidenceResponse | null;
  error: string | null;
  retryResult: CollectorCompileResponse | null;
  retryBusy: boolean;
  deleteResult: CollectorPersonalDataDeleteResponse | null;
  deleteBusy: string | null;
  onRetryCompile: () => Promise<void>;
  onDeleteDocument: (docId: string, title: string) => Promise<void>;
  onPruneSource: (source: string, olderThanDays: number) => Promise<void>;
}) {
  const sources = evidence?.sources ?? [];

  return (
    <Card className="mb-4 p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2
            className="text-[var(--text-body)] font-extrabold"
            style={{ color: "var(--color-text-strong)" }}
          >
            Collector evidence
          </h2>
          <p className="mt-1 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            pushed docs · command results · compile counts
          </p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Chip tone="neutral">{sources.length} sources</Chip>
          <Chip tone="muted">{evidence?.recent_commands.length ?? 0} commands</Chip>
          <ToolbarButton
            icon={<SyncSmallIcon />}
            disabled={retryBusy}
            onClick={() => void onRetryCompile()}
          >
            {retryBusy ? "compile 중" : "compile retry"}
          </ToolbarButton>
        </div>
      </div>

      {retryResult ? (
        <div className="mt-3">
          <Banner
            tone={retryResult.failed ? "warn" : "pass"}
            title="manual compile retry"
          >
            {collectorCompileResultText(retryResult)}
          </Banner>
        </div>
      ) : null}

      {evidence ? <CollectorLivenessPanel liveness={evidence.liveness} /> : null}

      {deleteResult ? (
        <div className="mt-3">
          <Banner tone="warn" title="personal collector data delete">
            {collectorDeleteResultText(deleteResult)}
          </Banner>
        </div>
      ) : null}

      {error ? (
        <div className="mt-3">
          <Banner tone="warn" title="collector evidence">
            {error}
          </Banner>
        </div>
      ) : !evidence ? (
        <p className="mt-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          collector evidence 확인 중
        </p>
      ) : sources.length ? (
        <div className="mt-3 grid grid-cols-1 gap-2 lg:grid-cols-2">
          {sources.map((source) => (
            <CollectorEvidenceSourceCard
              key={source.source}
              source={source}
              deleteBusy={deleteBusy}
              onDeleteDocument={onDeleteDocument}
              onPruneSource={onPruneSource}
            />
          ))}
        </div>
      ) : (
        <p className="mt-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          아직 collector-pushed document나 command result가 없습니다.
        </p>
      )}
    </Card>
  );
}

function CollectorLivenessPanel({
  liveness,
}: {
  liveness: CollectorEvidenceResponse["liveness"];
}) {
  return (
    <div
      className="mt-3 rounded-[6px] border p-2"
      style={{
        background: "var(--color-app-canvas)",
        borderColor: "var(--color-divider-soft)",
      }}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className="text-[var(--text-body-sm)] font-extrabold"
              style={{ color: "var(--color-text-strong)" }}
            >
              Collector liveness
            </span>
            <Chip tone={collectorLivenessTone(liveness.status)}>
              {collectorLivenessLabel(liveness.status)}
            </Chip>
          </div>
          <p className="mt-1 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            {liveness.reason}
          </p>
        </div>
        <Chip tone={liveness.pending_command_count ? "warn" : "muted"}>
          pending {liveness.pending_command_count}
        </Chip>
      </div>
      <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-4">
        <Meta label="last poll" value={formatDate(liveness.last_polled_at)} />
        <Meta label="scheduler" value={collectorSchedulerText(liveness)} />
        <Meta label="active tokens" value={liveness.active_token_count.toLocaleString("ko-KR")} />
        <Meta label="claimed" value={liveness.claimed_command_count.toLocaleString("ko-KR")} />
      </div>
      {liveness.last_status_error ? (
        <p className="mt-2 truncate text-[var(--text-meta)]" style={{ color: "var(--color-warn-fg)" }}>
          {liveness.last_status_error}
        </p>
      ) : null}
    </div>
  );
}

function CollectorEvidenceSourceCard({
  source,
  deleteBusy,
  onDeleteDocument,
  onPruneSource,
}: {
  source: CollectorEvidenceSource;
  deleteBusy: string | null;
  onDeleteDocument: (docId: string, title: string) => Promise<void>;
  onPruneSource: (source: string, olderThanDays: number) => Promise<void>;
}) {
  const command = source.latest_command;
  const [olderThanDays, setOlderThanDays] = useState("90");
  const pruneBusy = deleteBusy === `prune:${source.source}`;
  const cleanupBusy = Boolean(deleteBusy);

  function submitPrune(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = Number.parseInt(olderThanDays, 10);
    if (!Number.isFinite(parsed) || parsed < 1) return;
    void onPruneSource(source.source, parsed);
  }

  return (
    <div
      className="rounded-[6px] border p-2"
      style={{
        background: "var(--color-app-canvas)",
        borderColor: "var(--color-divider-soft)",
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <span
            className="truncate text-[var(--text-body-sm)] font-extrabold"
            style={{ color: "var(--color-text-strong)" }}
            title={source.source}
          >
            {source.source}
          </span>
          <Chip tone={source.document_count ? "pass" : "muted"}>
            {source.document_count.toLocaleString("ko-KR")} docs
          </Chip>
        </div>
        {command ? (
          <Chip tone={collectorCommandTone(command.status)}>
            {collectorCommandLabel(command.status)}
          </Chip>
        ) : (
          <Chip tone="muted">no command</Chip>
        )}
      </div>

      <p
        className="mt-2 truncate text-[var(--text-body-sm)]"
        style={{
          color: command?.status === "failed" ? "var(--color-fail-fg)" : "var(--color-text-body)",
        }}
        title={command ? collectorCommandSummary(command) : "command result 없음"}
      >
        {command ? collectorCommandSummary(command) : "command result 없음"}
      </p>
      <p
        className="mt-1 text-[var(--text-meta)] font-[family-name:var(--font-mono)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        {collectorCompileText(source)}
      </p>

      <form className="mt-2 flex flex-wrap items-center gap-1.5" onSubmit={submitPrune}>
        <input
          className={cx(inputClass, "h-8 w-20 px-2 text-[var(--text-body-sm)]")}
          min="1"
          step="1"
          type="number"
          value={olderThanDays}
          onChange={(event) => setOlderThanDays(event.target.value)}
          style={inputStyle}
          aria-label={`${source.source} prune older than days`}
        />
        <ToolbarButton
          disabled={cleanupBusy}
          type="submit"
          title="central owner-scope personal collector data만 삭제"
        >
          {pruneBusy ? "prune 중" : "old docs prune"}
        </ToolbarButton>
      </form>

      {source.recent_documents.length ? (
        <div className="mt-2 space-y-1">
          {source.recent_documents.slice(0, 3).map((doc) => (
            <div
              key={doc.doc_id}
              className="rounded-[6px] border px-2 py-1.5"
              style={{
                background: "var(--color-card)",
                borderColor: "var(--color-divider-soft)",
                color: "var(--color-text-body)",
              }}
            >
              <div className="flex items-center justify-between gap-2">
                <a
                  href={`/editor?doc=${encodeURIComponent(doc.doc_id)}`}
                  className="min-w-0 truncate text-[var(--text-body-sm)] font-semibold"
                  style={{ color: "var(--color-text-strong)" }}
                  title={doc.title}
                >
                  {doc.title || "제목 없음"}
                </a>
                <div className="flex shrink-0 items-center gap-1.5">
                  <span
                    className="text-[var(--text-meta)]"
                    style={{ color: "var(--color-text-faint)" }}
                  >
                    {formatDate(doc.source_last_edited_at ?? doc.updated_at)}
                  </span>
                  <ToolbarButton
                    disabled={cleanupBusy}
                    onClick={() => void onDeleteDocument(doc.doc_id, doc.title)}
                    title="central owner-scope personal collector data만 삭제"
                  >
                    {deleteBusy === `doc:${doc.doc_id}` ? "삭제 중" : "삭제"}
                  </ToolbarButton>
                </div>
              </div>
              <div className="mt-1 flex min-w-0 items-center gap-1.5 text-[var(--text-meta)]">
                <Chip tone={doc.source_account_id ? "neutral" : "muted"}>
                  {doc.source_account_id ? "account" : "no account"}
                </Chip>
                <span
                  className="min-w-0 truncate font-[family-name:var(--font-mono)]"
                  style={{ color: "var(--color-text-muted)" }}
                  title={doc.source_external_id ?? ""}
                >
                  {doc.source_external_id ?? doc.doc_id}
                </span>
              </div>
              <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1.5 text-[var(--text-meta)]">
                {doc.wiki_pages.length ? (
                  doc.wiki_pages.map((page) => (
                    <a
                      key={page.slug}
                      href={wikiPageHref(page)}
                      className="max-w-full truncate rounded-[999px] border px-2 py-0.5 font-semibold"
                      style={{
                        borderColor: "var(--color-pass-border)",
                        background: "var(--color-pass-bg)",
                        color: "var(--color-pass-fg)",
                      }}
                      title={page.slug}
                    >
                      wiki · {page.title || page.slug}
                    </a>
                  ))
                ) : doc.wiki_source_slug ? (
                  <Chip tone="muted">wiki source만 생성됨</Chip>
                ) : (
                  <Chip tone="warn">wiki page 없음</Chip>
                )}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          recent pushed docs 없음
        </p>
      )}
    </div>
  );
}

function collectorCompileText(source: CollectorEvidenceSource): string {
  const compile = source.latest_compile;
  if (!compile) return "compile evidence 없음";
  return [
    `indexed ${compile.indexed}`,
    `authored ${compile.authored}`,
    `skipped ${compile.skipped}`,
    `failed ${compile.failed}`,
  ].join(" · ");
}

function wikiPageHref(page: { slug: string; scope: "personal" }): string {
  const slug = page.slug.split("/").map(encodeURIComponent).join("/");
  return `/wiki/${slug}?scope=${encodeURIComponent(page.scope)}`;
}

function collectorCompileResultText(result: CollectorCompileResponse): string {
  return [
    `indexed ${result.indexed}`,
    `authored ${result.authored}`,
    `skipped ${result.skipped}`,
    `failed ${result.failed}`,
  ].join(" · ");
}

function collectorDeleteResultText(result: CollectorPersonalDataDeleteResponse): string {
  return [
    `documents ${result.documents_deleted}`,
    `connector items ${result.connector_items_deleted}`,
    `structured ${result.structured_rows_deleted}`,
    `chunks ${result.corpus_chunks_deleted}`,
    `embeddings ${result.corpus_embeddings_deleted}`,
    `wiki items ${result.wiki_items_deleted}`,
  ].join(" · ");
}

function ConnectorDocuments({
  documents,
  total,
  filter,
  onFilterChange,
}: {
  documents: ConnectorDocument[];
  total: number;
  filter: string;
  onFilterChange: (value: string) => void;
}) {
  if (!total) {
    return (
      <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
        아직 수집 문서 없음
      </p>
    );
  }

  return (
    <div className="mt-2 space-y-2">
      <input
        aria-label="connector document filter"
        className={cx(inputClass, "h-8 px-2 text-[var(--text-body-sm)]")}
        placeholder="title / external id"
        value={filter}
        onChange={(event) => onFilterChange(event.target.value)}
        style={inputStyle}
      />
      {documents.length ? (
        <div className="space-y-1.5">
          {documents.slice(0, 8).map((doc) => (
            <a
              key={doc.doc_id}
              href={`/editor?doc=${encodeURIComponent(doc.doc_id)}`}
              className="block rounded-[6px] border px-2 py-1.5 transition-colors"
              style={{
                background: "var(--color-app-canvas)",
                borderColor: "var(--color-divider-soft)",
                color: "var(--color-text-body)",
              }}
            >
              <div className="flex items-center justify-between gap-2">
                <span
                  className="min-w-0 truncate text-[var(--text-body-sm)] font-semibold"
                  style={{ color: "var(--color-text-strong)" }}
                  title={doc.title}
                >
                  {doc.title || "제목 없음"}
                </span>
                <span
                  className="shrink-0 text-[var(--text-meta)]"
                  style={{ color: "var(--color-text-faint)" }}
                >
                  {formatDate(doc.source_last_edited_at ?? doc.updated_at)}
                </span>
              </div>
              <div className="mt-1 flex items-center gap-1.5 text-[var(--text-meta)]">
                <Chip tone="neutral">{doc.source}</Chip>
                <span
                  className="min-w-0 truncate font-[family-name:var(--font-mono)]"
                  style={{ color: "var(--color-text-muted)" }}
                  title={doc.source_external_id ?? ""}
                >
                  {doc.source_external_id ?? doc.doc_id}
                </span>
              </div>
            </a>
          ))}
        </div>
      ) : (
        <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          필터 결과 없음
        </p>
      )}
    </div>
  );
}

function RunHistory({ runs }: { runs: ConnectorRun[] }) {
  if (!runs.length) {
    return (
      <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
        아직 실행 기록 없음
      </p>
    );
  }

  return (
    <div className="mt-2 space-y-1.5">
      {runs.slice(0, 6).map((run) => (
        <div
          key={run.run_id}
          className="rounded-[6px] border px-2 py-1.5"
          style={{
            background: "var(--color-app-canvas)",
            borderColor: "var(--color-divider-soft)",
          }}
        >
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <Chip tone={statusTone(run.status)}>{run.status}</Chip>
                <span
                  className="truncate text-[var(--text-meta)] font-bold uppercase"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {run.reason}
                </span>
              </div>
              <p
                className="mt-1 truncate text-[var(--text-body-sm)]"
                style={{ color: "var(--color-text-body)" }}
              >
                created {run.created} · updated {run.updated} · skipped {run.skipped} · errors{" "}
                {run.errors}
              </p>
            </div>
            <span
              className="shrink-0 text-right text-[var(--text-meta)]"
              style={{ color: "var(--color-text-faint)" }}
              title={run.finished_at ?? run.started_at ?? ""}
            >
              {formatDate(run.finished_at ?? run.started_at)}
            </span>
          </div>
          {run.error_message ? (
            <p
              className="mt-1 truncate text-[var(--text-meta)]"
              style={{ color: "var(--color-fail-fg)" }}
              title={run.error_message}
            >
              {run.error_message}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function CollectorCommandHistory({ commands }: { commands: CollectorCommand[] }) {
  if (!commands.length) {
    return (
      <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
        아직 command 없음
      </p>
    );
  }

  return (
    <div className="mt-2 space-y-1.5">
      {commands.slice(0, 6).map((command) => {
        const summary = collectorCommandSummary(command);
        return (
          <div
            key={command.command_id}
            className="rounded-[6px] border px-2 py-1.5"
            style={{
              background: "var(--color-app-canvas)",
              borderColor: "var(--color-divider-soft)",
            }}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="flex items-center gap-1.5">
                  <Chip tone={collectorCommandTone(command.status)}>
                    {collectorCommandLabel(command.status)}
                  </Chip>
                  <span
                    className="truncate text-[var(--text-meta)] font-bold uppercase"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {command.kind}
                  </span>
                </div>
                <p
                  className="mt-1 truncate text-[var(--text-body-sm)]"
                  style={{
                    color:
                      command.status === "failed"
                        ? "var(--color-fail-fg)"
                        : "var(--color-text-body)",
                  }}
                  title={summary}
                >
                  {summary}
                </p>
              </div>
              <span
                className="shrink-0 text-right text-[var(--text-meta)]"
                style={{ color: "var(--color-text-faint)" }}
                title={command.completed_at ?? command.claimed_at ?? command.created_at}
              >
                {formatDate(command.completed_at ?? command.claimed_at ?? command.created_at)}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function collectorCommandConnector(command: CollectorCommand): string | null {
  const connector = command.payload.connector;
  return typeof connector === "string" && connector.trim() ? connector.trim() : null;
}

function compareCollectorCommands(a: CollectorCommand, b: CollectorCommand): number {
  return Date.parse(b.created_at) - Date.parse(a.created_at);
}

function isActiveCollectorCommand(command: CollectorCommand): boolean {
  return command.status === "pending" || command.status === "claimed";
}

function collectorCommandLabel(status: CollectorCommand["status"]): string {
  if (status === "pending") return "queued";
  if (status === "claimed") return "pending";
  return status;
}

function collectorCommandTone(
  status: CollectorCommand["status"],
): "neutral" | "warn" | "fail" | "pass" | "muted" {
  if (status === "done") return "pass";
  if (status === "failed") return "fail";
  if (status === "pending" || status === "claimed") return "warn";
  return "neutral";
}

function collectorLivenessTone(
  status: CollectorEvidenceResponse["liveness"]["status"],
): "neutral" | "warn" | "fail" | "pass" | "muted" {
  if (status === "live") return "pass";
  if (status === "stale") return "warn";
  if (status === "offline") return "fail";
  return "muted";
}

function collectorLivenessLabel(status: CollectorEvidenceResponse["liveness"]["status"]): string {
  if (status === "live") return "live";
  if (status === "stale") return "stale";
  if (status === "offline") return "offline";
  return "token 필요";
}

function collectorSchedulerText(liveness: CollectorEvidenceResponse["liveness"]): string {
  if (liveness.scheduler_installed === null && liveness.scheduler_loaded === null) {
    return "unknown";
  }
  const installed = liveness.scheduler_installed ? "installed" : "not installed";
  const loaded = liveness.scheduler_loaded ? "loaded" : "not loaded";
  const interval = liveness.scheduler_interval_seconds
    ? ` · ${liveness.scheduler_interval_seconds}s`
    : "";
  return `${installed} · ${loaded}${interval}`;
}

function collectorCommandSummary(command: CollectorCommand): string {
  if (command.status === "pending") return "로컬 수집기에서 실행 대기";
  if (command.status === "claimed") return "로컬 수집기 실행 중";
  return collectorCommandResultText(command) ?? collectorCommandLabel(command.status);
}

function collectorCommandResultText(command: CollectorCommand): string | null {
  const result = command.result;
  if (!result) return null;
  const error = result.error;
  if (typeof error === "string" && error.trim()) return error.trim();
  const reason = result.reason;
  if (typeof reason === "string" && reason.trim()) return reason.trim();
  const parts = ["collected", "pushed", "created", "updated", "unchanged"]
    .map((key) => {
      const value = result[key];
      return typeof value === "number" ? `${key} ${value}` : null;
    })
    .filter((item): item is string => Boolean(item));
  return parts.length ? parts.join(" · ") : null;
}

function statusTone(status: string): "neutral" | "warn" | "fail" | "pass" | "muted" {
  if (status === "succeeded") return "pass";
  if (status === "failed") return "fail";
  if (status === "running") return "warn";
  return "neutral";
}

function statusBannerTone(status: string): "warn" | "fail" | "pass" {
  if (status === "succeeded") return "pass";
  if (status === "failed") return "fail";
  return "warn";
}

function filterConnectorDocuments(
  documents: ConnectorDocument[],
  filter: string,
): ConnectorDocument[] {
  const needle = filter.trim().toLowerCase();
  if (!needle) return documents;
  return documents.filter((doc) =>
    [doc.title, doc.source, doc.source_external_id ?? ""]
      .join("\n")
      .toLowerCase()
      .includes(needle),
  );
}

function GuideList({ items }: { items: string[] }) {
  return (
    <div className="space-y-1.5">
      {items.map((item) => (
        <p
          key={item}
          className="text-[var(--text-body-sm)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {item}
        </p>
      ))}
    </div>
  );
}

// connector text settings round-trip as csv->list[str], so a single mailbox
// field comes back as a one-element array. Coerce to a scalar for display.
function mailSettingScalar(value: unknown): string {
  if (Array.isArray(value)) return typeof value[0] === "string" ? value[0] : "";
  return typeof value === "string" ? value : "";
}

// P6.7.5 (§12/§12.11): mail slugs (`mail_nova`/`mail_acme`) register
// several mailboxes per domain. This section lists the user's registered
// mailboxes with per-mailbox Sync + Delete. Config is managed via CLI.
function MailAccountsSection({
  manifest,
  accounts,
  busy,
  onSyncMailbox,
  onDeleteMailbox,
  onAddMailbox,
}: {
  manifest: ConnectorManifest;
  accounts: ConnectorAccount[];
  busy: "sync" | null;
  onSyncMailbox: (slug: string, accountId: string) => Promise<void>;
  onDeleteMailbox: (slug: string, accountId: string, label: string) => Promise<void>;
  onAddMailbox: (slug: string, ownerAddr: string, ingestScope: string) => Promise<void>;
}) {
  const domain = manifest.slug === "mail_nova" ? "nova.example" : "acme.example";
  const [newAddr, setNewAddr] = useState("");
  const [newScope, setNewScope] = useState("owner");

  async function submit() {
    const addr = newAddr.trim();
    if (!addr || busy) return;
    await onAddMailbox(manifest.slug, addr, newScope);
    setNewAddr("");
    setNewScope("owner");
  }

  return (
    <>
      <SectionTitle>메일함</SectionTitle>
      {accounts.length ? (
        <div className="space-y-2">
          {accounts.map((account) => (
            <MailAccountRow
              key={account.account_id}
              slug={manifest.slug}
              account={account}
              busy={busy}
              onSyncMailbox={onSyncMailbox}
              onDeleteMailbox={onDeleteMailbox}
            />
          ))}
        </div>
      ) : (
        <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          등록된 메일함 없음
        </p>
      )}

      {/* P6.7.5: add another mailbox of this domain. The orthus server injects
          the provider app key; the browser only submits the mailbox owner. */}
      <div
        className="mt-2 rounded-[6px] border p-2"
        style={{ background: "var(--color-app-canvas)", borderColor: "var(--color-divider-soft)" }}
      >
        <div className="flex flex-wrap items-center gap-1.5">
          <input
            className={cx(inputClass, "h-11 min-w-0 flex-1 sm:h-9")}
            style={inputStyle}
            value={newAddr}
            onChange={(e) => setNewAddr(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
            placeholder={`you@${domain}`}
            inputMode="email"
            autoComplete="off"
            disabled={Boolean(busy)}
            aria-label="메일함 주소"
          />
          <select
            className={cx(inputClass, "h-11 sm:h-9")}
            style={inputStyle}
            value={newScope}
            onChange={(e) => setNewScope(e.target.value)}
            disabled={Boolean(busy)}
            aria-label="수집 범위"
          >
            <option value="owner">개인</option>
            <option value="company">회사 공유</option>
          </select>
          <ToolbarButton
            className="h-11 sm:h-9"
            tone="primary"
            disabled={Boolean(busy) || !newAddr.trim()}
            onClick={() => void submit()}
          >
            추가
          </ToolbarButton>
        </div>
        <p className="mt-1.5 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          같은 도메인(@{domain})에 여러 메일함을 등록할 수 있습니다. 서버가 보관한 공급자 키로
          인증하므로 브라우저에는 토큰을 입력하지 않습니다. (CLI:{" "}
          <code>orthus connector config {manifest.slug} --set owner_addr=&lt;email&gt;</code>)
        </p>
      </div>
    </>
  );
}

function MailAccountRow({
  slug,
  account,
  busy,
  onSyncMailbox,
  onDeleteMailbox,
}: {
  slug: string;
  account: ConnectorAccount;
  busy: "sync" | null;
  onSyncMailbox: (slug: string, accountId: string) => Promise<void>;
  onDeleteMailbox: (slug: string, accountId: string, label: string) => Promise<void>;
}) {
  const settings = account.settings_redacted ?? {};
  // connector settings store text fields as csv->list, so a mailbox owner_addr
  // round-trips as a one-element array; coerce to a scalar before display.
  const ownerAddr = mailSettingScalar(settings.owner_addr) || (account.account_label ?? "메일함");
  const ingestScope = mailSettingScalar(settings.ingest_scope) || "owner";
  const label = ownerAddr;

  return (
    <div
      className="rounded-[6px] border p-2"
      style={{
        background: "var(--color-app-canvas)",
        borderColor: "var(--color-divider-soft)",
      }}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span
              className="truncate text-[var(--text-body-sm)] font-semibold"
              style={{ color: "var(--color-text-strong)" }}
              title={ownerAddr}
            >
              {ownerAddr}
            </span>
            <Chip tone={ingestScope === "company" ? "warn" : "pass"}>
              {ingestScope === "company" ? "회사 공유" : "개인"}
            </Chip>
            <Chip tone={account.status === "active" ? "pass" : "muted"}>{account.status}</Chip>
          </div>
          <p
            className="mt-1 truncate text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            last: {formatDate(account.last_sync_at)}
          </p>
          {account.last_error ? (
            <p
              className="mt-1 truncate text-[var(--text-meta)]"
              style={{ color: "var(--color-fail-fg)" }}
              title={account.last_error}
            >
              {account.last_error}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <ToolbarButton
            className="h-11 sm:h-8"
            icon={<SyncSmallIcon />}
            disabled={Boolean(busy)}
            onClick={() => void onSyncMailbox(slug, account.account_id)}
          >
            동기화
          </ToolbarButton>
          <ToolbarButton
            className="h-11 sm:h-8"
            icon={<TrashSmallIcon />}
            disabled={Boolean(busy)}
            onClick={() => void onDeleteMailbox(slug, account.account_id, label)}
          >
            삭제
          </ToolbarButton>
        </div>
      </div>
    </div>
  );
}

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <div
      className="mb-1 mt-4 text-[var(--text-meta)] font-bold uppercase"
      style={{ color: "var(--color-text-muted)" }}
    >
      {children}
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div
        className="text-[var(--text-meta)] font-bold uppercase"
        style={{ color: "var(--color-text-faint)" }}
      >
        {label}
      </div>
      <div
        className="truncate text-[var(--text-body-sm)] font-medium"
        style={{ color: "var(--color-text-body)" }}
        title={value}
      >
        {value}
      </div>
    </div>
  );
}

function ConnectorSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
      {[0, 1, 2, 3].map((i) => (
        <Card key={i} className="p-3">
          <Skeleton className="h-4 w-36" />
          <Skeleton className="mt-2 h-8 w-full" />
          <Skeleton className="mt-3 h-16 w-full" />
        </Card>
      ))}
    </div>
  );
}

function requiredFieldCount(manifest: ConnectorManifest): number {
  return manifest.config_fields.filter((field) => field.required).length;
}

function connectorCanStartNow(
  manifest: ConnectorManifest,
  remotePersonalWorkspace: boolean,
): boolean {
  if (remotePersonalWorkspace) {
    return (
      COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug) &&
      (manifest.default_configured || requiredFieldCount(manifest) === 0)
    );
  }
  return manifest.can_ensure_default && manifest.default_configured;
}

function connectorNeedsSetup(manifest: ConnectorManifest, accountCount: number): boolean {
  return accountCount === 0 && !manifest.default_configured && requiredFieldCount(manifest) > 0;
}

// A connector is "onboarded" once it no longer needs the quick-start nudge:
// an account is connected, or (for local collector sources, which never create
// a central account) a connector_sync command has completed at least once.
function connectorIsOnboarded(
  manifest: ConnectorManifest,
  accountCount: number,
  commands: CollectorCommand[],
  remotePersonalWorkspace: boolean,
): boolean {
  if (accountCount > 0) return true;
  if (remotePersonalWorkspace && COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug)) {
    return commands.some((command) => command.status === "done");
  }
  return false;
}

function connectorSortRank(
  manifest: ConnectorManifest,
  accountCount: number,
  remotePersonalWorkspace: boolean,
): number {
  if (remotePersonalWorkspace && !COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug)) return 400;
  if (accountCount === 0 && connectorCanStartNow(manifest, remotePersonalWorkspace)) return 0;
  if (connectorNeedsSetup(manifest, accountCount)) return 100;
  if (accountCount > 0) return 200;
  return 300;
}

function connectorSuggestedOrder(slug: string): number {
  const order = [
    "local_files",
    "codex_sessions",
    "claude_sessions",
    "gws_gmail",
    "gws_drive",
    "github",
    "notion",
    "slack",
    "mail_nova",
    "mail_acme",
  ];
  const idx = order.indexOf(slug);
  return idx === -1 ? 99 : idx;
}

function quickStateText(
  manifest: ConnectorManifest,
  accountCount: number,
  remotePersonalWorkspace: boolean,
): string {
  if (accountCount > 0) return isMailSlug(manifest.slug) ? `${accountCount}개 연결` : "연결됨";
  if (remotePersonalWorkspace && !COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug)) return "미지원";
  if (connectorCanStartNow(manifest, remotePersonalWorkspace)) return "바로 가능";
  const required = requiredFieldCount(manifest);
  if (required) return `필수값 ${required}개`;
  return "확인 필요";
}

function quickStateTone(
  manifest: ConnectorManifest,
  accountCount: number,
  remotePersonalWorkspace: boolean,
): "neutral" | "warn" | "fail" | "pass" | "muted" {
  if (accountCount > 0) return "pass";
  if (remotePersonalWorkspace && !COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug)) return "muted";
  if (connectorCanStartNow(manifest, remotePersonalWorkspace)) return "pass";
  return "warn";
}

function quickHintText(
  manifest: ConnectorManifest,
  accountCount: number,
  latestRun: ConnectorRun | null,
): string {
  if (accountCount > 0) {
    if (latestRun?.error_message) return latestRun.error_message;
    if (latestRun) {
      return `최근 실행 ${formatDate(latestRun.finished_at ?? latestRun.started_at)} · created ${latestRun.created} · updated ${latestRun.updated}`;
    }
    return "연결됨 · 필요할 때 동기화";
  }
  const required = requiredFieldCount(manifest);
  if (required) return `${manifest.config_fields.filter((field) => field.required).map((field) => field.label).join(", ")} 저장 필요`;
  if (COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug)) return "요청하면 로컬 수집기가 처리";
  if (manifest.can_ensure_default && manifest.default_configured) return "동기화 누르면 자동 연결";
  return manifest.config_error || "연결 방식 확인 필요";
}

function cardReadinessText(
  manifest: ConnectorManifest,
  accountCount: number,
  remotePersonalWorkspace: boolean,
): string {
  if (accountCount > 0) return "동기화 가능";
  if (remotePersonalWorkspace && !COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug)) return "legacy";
  if (connectorCanStartNow(manifest, remotePersonalWorkspace)) return "클릭 후 수집";
  return "먼저 저장";
}

function nextConnectorActionText(
  manifest: ConnectorManifest,
  account: ConnectorAccount | null,
  remotePersonalWorkspace: boolean,
): string {
  if (account?.last_sync_at) return `last: ${formatDate(account.last_sync_at)}`;
  if (account) return "동기화 대기";
  if (remotePersonalWorkspace) {
    return COLLECTOR_SUPPORTED_SOURCES.has(manifest.slug)
      ? "로컬 수집기에 요청"
      : "legacy personal node 전용";
  }
  if (requiredFieldCount(manifest)) return "필수값 저장";
  if (manifest.can_ensure_default && manifest.default_configured) return "동기화로 연결";
  return "설정 확인";
}

function collectorManagedState(
  manifest: ConnectorManifest,
  account: ConnectorAccount | null,
  remotePersonalWorkspace: boolean,
  collectorSupported: boolean,
): { label: string; tone: "neutral" } | null {
  // Collector-managed connectors (gws_*, local_files, codex/claude_sessions,
  // github) run on the local collector with its own local config; the
  // collector executes autonomously regardless of any central account/config
  // (central settings are optional and merely pushed via /collector/config). So
  // "설정 필요" is misleading even when a central required field is unsaved —
  // status reflects that the collector manages it.
  if (account || !remotePersonalWorkspace || !collectorSupported) return null;
  return { label: "Collector", tone: "neutral" };
}

function connectorStateText(manifest: ConnectorManifest, account: ConnectorAccount | null): string {
  if (account) return "연결됨";
  if (!manifest.default_configured) return "설정 필요";
  if (!manifest.can_ensure_default) return "Collector";
  return "미연결";
}

function dataScopeText(manifest: ConnectorManifest): string {
  if (manifest.account_kinds.includes("personal") && manifest.account_kinds.includes("company")) {
    return "company, personal";
  }
  return manifest.account_kinds.join(", ");
}

function Pre({ children }: { children: string }) {
  return (
    <pre
      className="mt-1 overflow-x-auto rounded-[4px] px-2 py-1.5 text-[var(--text-body-xs)]"
      style={{
        background: "var(--color-surface-neutral)",
        color: "var(--color-text-body)",
        border: "1px solid var(--color-divider-soft)",
      }}
    >
      {children}
    </pre>
  );
}

function connectorGuide(
  manifest: ConnectorManifest,
  options: { remotePersonalWorkspace?: boolean; collectorSupported?: boolean } = {},
): string[] {
  const collectorRoot = "~/.orthus/collector";
  const remoteUnsupported =
    Boolean(options.remotePersonalWorkspace) && options.collectorSupported === false;
  const guides: Record<string, string[]> = {
    codex_sessions: [
      "기본 경로를 바로 읽음: ~/.codex/sessions, ~/.codex/history.jsonl.",
      `추가 파일은 ${collectorRoot}/imports/ai-sessions/codex 에 넣으면 됨.`,
      "별도 설정 없이 동기화 누르면 됨.",
    ],
    claude_sessions: [
      "기본 경로를 바로 읽음: ~/.claude/projects, ~/.claude/transcripts, ~/.claude/history.jsonl.",
      `추가 파일은 ${collectorRoot}/imports/ai-sessions/claude 에 넣으면 됨.`,
      "별도 설정 없이 동기화 누르면 됨.",
    ],
    chat_exports: [
      `ChatGPT/Claude export zip/json을 ${collectorRoot}/imports/chat-exports 에 넣음.`,
      "별도 설정 없이 동기화 누르면 됨.",
    ],
    email_exports: [
      `eml/mbox/json export를 ${collectorRoot}/imports/email-exports 에 넣음.`,
      "Gmail은 gws CLI connector를 쓰면 export 없이 직접 읽음.",
    ],
    local_files: [
      `수집할 md/txt/json/csv 파일을 ${collectorRoot}/imports/local-files 에 넣음.`,
      "별도 설정 없이 동기화 누르면 됨.",
    ],
    notion: remoteUnsupported
      ? [
          "personal Notion은 cutover 전 legacy personal-node connector로만 남음.",
          "로컬 수집기는 Notion을 실행하지 않음.",
          "회사 Notion은 회사 커넥터 화면에서 설정.",
        ]
      : [
          "Notion integration token만 저장하면 됨.",
          "저장된 token은 DB가 아니라 local secret store에 보관.",
        ],
    github: [
      "GitHub token과 owner/repo 목록만 저장하면 됨.",
      "저장된 token은 DB가 아니라 local secret store에 보관.",
    ],
    slack: [
      "Slack bot token과 channel id 목록만 저장하면 됨.",
      "회사 node에서만 사용.",
    ],
    mail_nova: [
      "메일함 주소(owner_addr)는 발송/수집 대상 @nova.example 주소입니다. owner/admin은 임의 회사 주소, 멤버는 본인 이메일만.",
      "수집 범위(ingest_scope): 개인(owner-only personal wiki) 또는 회사 공유(company wiki). 기본은 개인.",
      "API key는 nova 메일 서버 APP_API_KEY이며 local secret store에만 저장됩니다(DB엔 ref만).",
    ],
    mail_acme: [
      "메일함 주소(owner_addr)는 발송/수집 대상 @acme.example 주소입니다. owner/admin은 임의 회사 주소, 멤버는 본인 이메일만.",
      "수집 범위(ingest_scope): 개인(owner-only personal wiki) 또는 회사 공유(company wiki). 기본은 개인.",
      "API token은 acme 메일 서버 토큰이며 local secret store에만 저장됩니다(DB엔 ref만).",
    ],
    gws_gmail: [
      "node에 gws CLI를 설치하고 Google 계정 로그인을 마치면 됨.",
      "권장: brew install googleworkspace-cli",
      "로그인: gws auth login -s gmail",
    ],
    gws_drive: [
      "node에 gws CLI를 설치하고 Google 계정 로그인을 마치면 됨.",
      "권장: brew install googleworkspace-cli",
      "로그인: gws auth login -s drive",
    ],
  };
  return guides[manifest.slug] ?? ["연결 후 동기화 누르면 현재 node corpus/wiki로 들어감."];
}

function formatInterval(seconds: number | null): string {
  if (!seconds) return "manual";
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function formatDate(value: string | null): string {
  if (!value) return "never";
  try {
    return new Date(value).toLocaleString("ko-KR", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return value;
  }
}

function RefreshIcon() {
  return <RefreshCw size={16} strokeWidth={1.8} aria-hidden />;
}

function InfoSmallIcon() {
  return <Info size={16} strokeWidth={1.8} aria-hidden />;
}

function SyncSmallIcon() {
  return <RotateCw size={16} strokeWidth={1.8} aria-hidden />;
}

function CopySmallIcon() {
  return <Copy size={16} strokeWidth={1.8} aria-hidden />;
}

function TrashSmallIcon() {
  return <Trash2 size={16} strokeWidth={1.8} aria-hidden />;
}


/* ------------------------ Agent setup hub (P9.2 FE) -------------------- */

// Copy-button micro-feedback key: each button gets its own discriminator so
// only the clicked one swaps to "복사됨". Cleared on a short timer.
type SetupCopyKey =
  | "mcp_claude"
  | "mcp_codex"
  | "full_prompt"
  | "init_cmd"
  | "token"
  | `slug:${string}`;

type MCPVariant = "claude" | "codex";

interface AgentSetupHubProps {
  manifests: ConnectorManifest[];
  centralUrl: string;
}

/**
 * "에이전트로 설정하기" hub (personal variant only).
 *
 * The user pastes one of these blocks into a coding agent (Claude/Codex) and
 * the agent uses the `orthus` CLI (and optionally orthus MCP tools) to actually
 * write the connector config. Live tokens are never embedded — secrets come
 * from macOS Keychain inside the CLI / MCP runtime.
 */
function AgentSetupHub({ manifests, centralUrl }: AgentSetupHubProps) {
  const [mcpVariant, setMcpVariant] = useState<MCPVariant>("claude");
  const [copied, setCopied] = useState<SetupCopyKey | null>(null);
  const [issuing, setIssuing] = useState(false);
  const [issueError, setIssueError] = useState<string | null>(null);
  // 발급된 토큰을 잠깐 보관한다. 클립보드 자동복사가 실패하면(`copyFailed`) 값을 노출해
  // 사용자가 직접 복사하게 하고, 성공하면 값은 숨긴 채 확인만 보여준다.
  const [issuedToken, setIssuedToken] = useState<string | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);

  // Mail slugs declare both company + personal account_kinds. On the personal
  // hub we surface them as the user's mailbox setup path.
  const personalManifests = useMemo(
    () => manifests.filter((m) => m.account_kinds.includes("personal")),
    [manifests],
  );

  // Auto-reset feedback so the hub feels live but never stale.
  useEffect(() => {
    if (!copied) return;
    const id = window.setTimeout(() => setCopied(null), 1500);
    return () => window.clearTimeout(id);
  }, [copied]);

  async function copy(key: SetupCopyKey, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
    } catch {
      setCopied(null);
    }
  }

  // Issue a fresh owner-bound all-scope token and copy ONLY the token
  // value to the clipboard. The token never goes into the setup prompt (which
  // is pasted into an agent chat log) — the user registers it locally by running
  // `orthus init --mcp-token-stdin` and pasting it at the getpass prompt, so it
  // stays out of shell history and the agent log.
  async function issueAndCopyToken() {
    setIssuing(true);
    setIssueError(null);
    setCopyFailed(false);
    setIssuedToken(null);

    // 발급은 네트워크 호출이라, `await issue()` 뒤에 `writeText()`를 부르면 WKWebView
    // (Safari 엔진)에선 클릭의 user-activation 창이 이미 닫혀 클립보드 쓰기가 조용히
    // 거부된다 — 데스크톱 앱에서 "토큰 발급"만 복사가 안 되던 원인(E2E로 확인: 같은
    // 창에서 in-gesture인 "명령 복사"는 정상). 그래서 클릭 제스처 안에서 동기적으로
    // `clipboard.write`를 호출하고, 토큰 값은 promise로 넘겨 활성화를 유지한다.
    let tokenValue: string | null = null;
    const tokenPromise = api
      .issueCollectorToken({
        name: "orthus-cli",
        // One token that covers every local-agent path: MCP knowledge reads +
        // owner-bound board writes (ticket CRUD), collector ingest pushes (so the
        // same token works with `orthus init --collector-token-stdin`), daemon
        // command poll/claim, and agent_task dispatch. `ingest` already implies
        // `commands` server-side.
        scopes: ["ingest", "commands", "knowledge", "knowledge:write", "agent_task"],
      })
      .then((issued) => {
        tokenValue = issued.token;
        return issued.token;
      });

    // Promise-backed ClipboardItem: 활성화가 살아 있는 지금 write를 시작하고, 실제
    // 데이터(토큰)는 발급이 끝나면 채운다. write()/ClipboardItem 미지원 엔진에서는
    // 아래 writeText 폴백으로 내려간다.
    let copyPromise: Promise<void> | null = null;
    if (typeof ClipboardItem !== "undefined") {
      try {
        copyPromise = navigator.clipboard.write([
          new ClipboardItem({
            "text/plain": tokenPromise.then(
              (tok) => new Blob([tok], { type: "text/plain" }),
            ),
          }),
        ]);
        // 발급 실패로 promise가 rejected 되어도 unhandledrejection이 나지 않게.
        copyPromise.catch(() => {});
      } catch {
        copyPromise = null;
      }
    }

    try {
      // 발급 성공을 클립보드 복사 성공 여부와 무관하게 항상 눈에 보이게 남긴다. 복사가
      // 실패하면(copyFailed) 토큰을 노출해 직접 복사하게 한다.
      const token = tokenValue ?? (await tokenPromise);
      setIssuedToken(token);

      let copiedOk = false;
      if (copyPromise) {
        try {
          await copyPromise;
          copiedOk = true;
        } catch {
          copiedOk = false;
        }
      }
      if (!copiedOk) {
        // 폴백: 활성화가 남아 있는 엔진(Chromium 등)에서는 여기서 성공한다.
        try {
          await navigator.clipboard.writeText(token);
          copiedOk = true;
        } catch {
          copiedOk = false;
        }
      }
      setCopyFailed(!copiedOk);
      if (copiedOk) setCopied("token");
    } catch (e) {
      setIssueError(e instanceof Error ? e.message : "토큰 발급 실패");
    } finally {
      setIssuing(false);
    }
  }

  // 자동복사가 실패해 노출된 토큰을 사용자가 다시 복사 시도한다. 여전히 실패하면
  // input에서 직접 선택복사할 수 있게 노출 상태를 유지한다.
  async function copyIssuedTokenFallback() {
    if (!issuedToken) return;
    try {
      await navigator.clipboard.writeText(issuedToken);
      setCopied("token");
      setCopyFailed(false);
    } catch {
      /* keep the token visible for manual selection */
    }
  }

  const mcpText =
    mcpVariant === "claude" ? buildMcpClaudeJson(centralUrl) : buildMcpCodexToml(centralUrl);
  const fullPrompt = buildFullSetupPrompt(personalManifests, centralUrl);
  const initCmd = `orthus init --central-url ${centralUrl} --mcp-token-stdin --install-cli`;

  return (
    <Card className="mb-4 p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2
            className="text-[var(--text-body)] font-extrabold"
            style={{ color: "var(--color-text-strong)" }}
          >
            <span aria-hidden style={{ marginRight: 6 }}>⚡</span>
            에이전트로 설정하기
          </h2>
          <p
            className="mt-1 text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            프롬프트를 복사해 에이전트(Claude/Codex)에 붙여넣으면 커넥터가 설정됩니다.
            토큰/시크릿은 복사 블록에 포함되지 않으며 에이전트가 사용자에게 직접 묻습니다.
          </p>
        </div>
        <Chip tone="accent">{personalManifests.length} connectors</Chip>
      </div>

      {/* Primary action 1: MCP config (Claude JSON / Codex TOML). ---------- */}
      <div
        className="mt-3 rounded-[6px] border p-2"
        style={{
          background: "var(--color-app-canvas)",
          borderColor: "var(--color-divider-soft)",
        }}
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <strong
              className="text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-strong)" }}
            >
              MCP 설정 복사
            </strong>
            <p
              className="mt-0.5 text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              에이전트에 orthus MCP 서버를 등록합니다. 토큰은 macOS Keychain
              (service <code>orthus-mcp-token</code>)에서 읽습니다.
            </p>
          </div>
          <div
            className="inline-flex shrink-0 overflow-hidden rounded-[6px] border"
            style={{ borderColor: "var(--color-divider)" }}
            role="tablist"
            aria-label="MCP config 변형"
          >
            <MCPVariantTab
              active={mcpVariant === "claude"}
              onClick={() => setMcpVariant("claude")}
            >
              Claude JSON
            </MCPVariantTab>
            <MCPVariantTab
              active={mcpVariant === "codex"}
              onClick={() => setMcpVariant("codex")}
            >
              Codex TOML
            </MCPVariantTab>
          </div>
        </div>
        <pre
          className="mt-2 max-h-[180px] overflow-auto rounded-[4px] px-2 py-1.5 text-[var(--text-body-xs)] font-[family-name:var(--font-mono)]"
          style={{
            background: "var(--color-surface-neutral)",
            color: "var(--color-text-body)",
            border: "1px solid var(--color-divider-soft)",
          }}
        >
          {mcpText}
        </pre>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span
            className="text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            central URL: <code>{centralUrl}</code> · 토큰은 Keychain
          </span>
          <ToolbarButton
            className="min-h-[44px]"
            icon={<CopySmallIcon />}
            onClick={() =>
              void copy(mcpVariant === "claude" ? "mcp_claude" : "mcp_codex", mcpText)
            }
          >
            {copied === (mcpVariant === "claude" ? "mcp_claude" : "mcp_codex")
              ? "복사됨"
              : `${mcpVariant === "claude" ? "Claude" : "Codex"} 복사`}
          </ToolbarButton>
        </div>
      </div>

      {/* Step 1: register the CLI token in the shell (token never in prompt/log). */}
      <div
        className="mt-2 rounded-[6px] border p-2"
        style={{
          background: "var(--color-app-canvas)",
          borderColor: "var(--color-divider-soft)",
        }}
      >
        <strong
          className="text-[var(--text-body-sm)]"
          style={{ color: "var(--color-text-strong)" }}
        >
          1. CLI 토큰 등록{" "}
          <span style={{ color: "var(--color-text-muted)" }}>(셸 · 머신당 1회)</span>
        </strong>
        <p
          className="mt-0.5 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          ingest+commands+knowledge+agent_task 토큰을 셸에서 등록합니다. MCP 지식
          조회와 collector ingest를 한 토큰으로 씁니다. 토큰은 프롬프트·셸 히스토리·
          에이전트 로그에 남지 않습니다.
        </p>
        <Pre>{initCmd}</Pre>
        <div className="mt-2 flex flex-wrap gap-2">
          <ToolbarButton
            className="min-h-[44px]"
            tone="primary"
            icon={<CopySmallIcon />}
            onClick={() => void copy("init_cmd", initCmd)}
          >
            {copied === "init_cmd" ? "복사됨" : "명령 복사"}
          </ToolbarButton>
          <ToolbarButton
            className="min-h-[44px]"
            tone="primary"
            icon={<CopySmallIcon />}
            disabled={issuing}
            onClick={() => void issueAndCopyToken()}
          >
            {issuing ? "발급 중…" : copied === "token" ? "토큰 복사됨" : "토큰 발급"}
          </ToolbarButton>
        </div>
        {issueError ? (
          <p
            className="mt-1 text-[var(--text-meta)]"
            style={{ color: "var(--color-warn-fg)" }}
          >
            {issueError}
          </p>
        ) : null}
        {issuedToken && !issueError ? (
          copyFailed ? (
            <div
              className="mt-1 rounded-[6px] border p-2"
              style={{
                background: "var(--color-warn-bg)",
                borderColor: "var(--color-warn-border)",
                color: "var(--color-warn-fg)",
              }}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <strong className="text-[var(--text-body-sm)]">
                  토큰 발급됨 · 클립보드 자동복사 실패
                </strong>
                <ToolbarButton
                  icon={<CopySmallIcon />}
                  onClick={() => void copyIssuedTokenFallback()}
                >
                  {copied === "token" ? "복사됨" : "복사"}
                </ToolbarButton>
              </div>
              <input
                className={cx(
                  inputClass,
                  "mt-2 h-9 px-2 font-[family-name:var(--font-mono)]",
                )}
                readOnly
                value={issuedToken}
                aria-label="issued collector token"
                style={inputStyle}
                onFocus={(event) => event.currentTarget.select()}
              />
              <p className="mt-2 text-[var(--text-meta)]">
                아래 값을 복사해 CLI <code>MCP token:</code> 프롬프트에 붙여넣으세요. 이
                값은 다시 표시되지 않습니다.
              </p>
            </div>
          ) : (
            <p
              className="mt-1 text-[var(--text-meta)]"
              style={{ color: "var(--color-pass-fg)" }}
            >
              ✓ 토큰 발급됨 · 클립보드에 복사됨 — CLI <code>MCP token:</code> 프롬프트에
              ⌘V로 붙여넣으세요.
            </p>
          )
        ) : null}
        <p
          className="mt-2 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          ① <strong>명령 복사</strong> → 셸에 붙여넣고 실행. ② <strong>토큰 발급</strong>{" "}
          → <code>MCP token:</code> 프롬프트에 붙여넣기(⌘V) 후 Enter.{" "}
          <strong>입력은 화면에 보이지 않습니다 — 정상입니다.</strong> 등록 후에는{" "}
          <code>orthus</code> 명령이 Keychain 토큰을 자동으로 씁니다(재입력 불필요,
          폐기 금지).
        </p>
      </div>

      {/* Primary action 2: full natural-language setup prompt. ------------- */}
      <div
        className="mt-2 rounded-[6px] border p-2"
        style={{
          background: "var(--color-app-canvas)",
          borderColor: "var(--color-divider-soft)",
        }}
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <strong
              className="text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-strong)" }}
            >
              2. 커넥터 설정{" "}
              <span style={{ color: "var(--color-text-muted)" }}>(에이전트)</span>
            </strong>
            <p
              className="mt-0.5 text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              프롬프트를 복사해 코딩 에이전트에 붙여넣으면 개인 커넥터 설정이
              진행됩니다. 토큰은 포함되지 않습니다.
            </p>
          </div>
          <ToolbarButton
            className="min-h-[44px]"
            tone="primary"
            icon={<CopySmallIcon />}
            onClick={() => void copy("full_prompt", fullPrompt)}
          >
            {copied === "full_prompt" ? "복사됨" : "프롬프트 복사"}
          </ToolbarButton>
        </div>
      </div>

      {/* Per-connector rows. ---------------------------------------------- */}
      <div className="mt-3">
        <div
          className="mb-1.5 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          커넥터별 프롬프트
        </div>
        <ul className="flex flex-col gap-1">
          {personalManifests.map((manifest) => {
            const key: SetupCopyKey = `slug:${manifest.slug}`;
            const prompt = buildConnectorPrompt(manifest, centralUrl);
            return (
              <li
                key={manifest.slug}
                className="flex flex-wrap items-center justify-between gap-2 rounded-[6px] border px-2 py-1.5"
                style={{
                  background: "var(--color-app-canvas)",
                  borderColor: "var(--color-divider-soft)",
                }}
              >
                <div className="min-w-0">
                  <div
                    className="truncate text-[var(--text-body-sm)] font-semibold"
                    style={{ color: "var(--color-text-strong)" }}
                  >
                    {manifest.label}
                  </div>
                  <div
                    className="mt-0.5 truncate text-[var(--text-meta)] font-[family-name:var(--font-mono)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {manifest.slug}
                    {manifest.config_fields.length === 0
                      ? " · 설정 없음"
                      : ` · ${manifest.config_fields.length} fields`}
                  </div>
                </div>
                <ToolbarButton
                  className="min-h-[44px]"
                  icon={<CopySmallIcon />}
                  onClick={() => void copy(key, prompt)}
                  aria-label={`${manifest.label} 프롬프트 복사`}
                >
                  {copied === key ? "복사됨" : "프롬프트 복사"}
                </ToolbarButton>
              </li>
            );
          })}
        </ul>
      </div>
    </Card>
  );
}

function MCPVariantTab({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      role="tab"
      aria-selected={active}
      className="px-2.5 py-1 text-[var(--text-body-sm)] transition-colors"
      style={{
        background: active ? "var(--color-card)" : "transparent",
        color: active ? "var(--color-text-strong)" : "var(--color-text-muted)",
        fontWeight: active ? 600 : 500,
        minHeight: 32,
      }}
    >
      {children}
    </button>
  );
}

/** Personal-variant disclosure. Default collapsed. Company stays untouched. */
function PersonalDetailGate({
  variant,
  children,
}: {
  variant: ConnectorsVariant;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  if (variant !== "personal") {
    return <>{children}</>;
  }
  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded-[8px] px-3 text-left text-[var(--text-body-sm)] font-semibold"
        style={{
          background: "var(--color-app-canvas)",
          border: "1px solid var(--color-divider-soft)",
          color: "var(--color-text-strong)",
          minHeight: 44,
        }}
      >
        <span
          style={{
            transform: open ? "rotate(90deg)" : "none",
            transition: "transform 0.15s",
            display: "inline-block",
          }}
          aria-hidden
        >
          ▶
        </span>
        상세/상태 보기
        <span
          className="ml-auto text-[var(--text-body-xs)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {open ? "접기" : "펼치기"}
        </span>
      </button>
      {open ? <div className="mt-3">{children}</div> : null}
    </div>
  );
}

/* ----------------------- Agent prompt builders ------------------------- */

/**
 * MCP config (Claude shape). Matches what `orthus mcp config --client claude`
 * emits. Token is NOT embedded — MCP runtime reads it from Keychain
 * (service 'orthus-mcp-token').
 */
function buildMcpClaudeJson(centralUrl: string): string {
  const config = {
    mcpServers: {
      orthus: {
        command: "orthus",
        args: ["mcp", "serve"],
        env: { ORTHUS_MCP_CENTRAL_URL: centralUrl },
      },
    },
  };
  return JSON.stringify(config, null, 2);
}

/** MCP config (Codex TOML shape). Same shape semantics as the Claude variant. */
function buildMcpCodexToml(centralUrl: string): string {
  return [
    "[mcp_servers.orthus]",
    'command = "orthus"',
    'args = ["mcp", "serve"]',
    `env = { ORTHUS_MCP_CENTRAL_URL = "${centralUrl}" }`,
    "# token: macOS Keychain service 'orthus-mcp-token'",
  ].join("\n");
}

/** Natural-language prompt covering every personal connector at once. */
function buildFullSetupPrompt(
  manifests: ConnectorManifest[],
  centralUrl: string,
): string {
  const lines: string[] = [];
  lines.push("orthus 개인 커넥터를 설정해 주세요.");
  lines.push("");
  lines.push(
    "orthus는 회사 지식 + 개인 지식을 합쳐 자연어로 답하는 사내 비서/아카식입니다.",
  );
  lines.push(
    "당신은 로컬 셸의 `orthus` CLI(설치·인증되어 있어야 함)와, MCP가 등록돼 있다면 orthus MCP 도구를 사용할 수 있습니다.",
  );
  lines.push("");
  lines.push(`central API URL = ${centralUrl}`);
  lines.push(
    "(중요) URL 뒤의 `/api`는 Caddy strip-prefix 때문에 반드시 포함되어야 합니다.",
  );
  lines.push(`CLI는 \`orthus --central-url ${centralUrl} ...\` 형식으로 호출하세요.`);
  lines.push(
    "토큰 등록은 사용자가 위 '토큰 발급 + CLI 등록' 패널에서 직접 끝냅니다 — 이 프롬프트에서 토큰을 묻거나 다루지 마세요.",
  );
  lines.push("");
  lines.push("아래 커넥터들을 사용자가 지정하는 것만 설정해 주세요:");
  lines.push("");
  for (const m of manifests) {
    lines.push(`## ${m.label} (${m.slug})`);
    if (m.description) lines.push(m.description);
    if (m.config_fields.length === 0) {
      lines.push("- 별도 설정 필요 없음. 파일을 inbox에 넣은 뒤 동기화만 실행하면 됩니다.");
    } else {
      for (const field of m.config_fields) {
        // Optional secrets fall back to a shared company key (e.g. mail
        // api_key/api_token) — the user sets them manually only if needed, so
        // keep them out of the agent's task list.
        if (field.kind === "secret" && !field.required) {
          lines.push(
            `- (선택) ${field.key}: ${field.label} — 비워도 됩니다(기본값/회사 공용 키 사용). 필요할 때만 사용자가 직접 \`orthus connector config ${m.slug} --secret ${field.key}\`로 설정하니, 이 프롬프트에서는 다루지 마세요.`,
          );
          continue;
        }
        const secretTag = field.kind === "secret" ? " (secret, --secret로 입력)" : "";
        const requiredTag = field.required ? " [필수]" : "";
        lines.push(`- ${field.key}: ${field.label}${requiredTag}${secretTag}`);
      }
    }
    lines.push("");
  }
  lines.push("실행 절차:");
  lines.push(
    "1. 사용자가 지정한 커넥터마다 `orthus connector config <slug> --set key=value` (필요하면 `--secret <key>` 추가)로 설정합니다.",
  );
  lines.push(
    "2. [필수] 시크릿 값(API key/token/PAT 등)만 절대 추정하지 말고 사용자에게 한 줄씩 물어보세요. (선택) 시크릿은 사용자가 직접 설정하니 다루지 마세요. `--secret`는 getpass 입력으로 평문 노출을 방지합니다.",
  );
  lines.push(
    "3. 설정 후 `orthus connector show <slug>`로 결과를 검증하고 사용자에게 보고합니다.",
  );
  lines.push(
    "4. 설정 없는 커넥터(예: chat_exports/email_exports)는 `~/.orthus/collector/imports/...` 폴더에 파일을 넣으라고 사용자에게 안내하세요.",
  );
  return lines.join("\n");
}

/** Connector-specific prompt: deterministic `orthus connector config …` command. */
function buildConnectorPrompt(
  manifest: ConnectorManifest,
  centralUrl: string,
): string {
  const lines: string[] = [];
  lines.push(`orthus의 개인 커넥터 \`${manifest.slug}\` (${manifest.label})를 설정해 주세요.`);
  if (manifest.description) {
    lines.push("");
    lines.push(manifest.description);
  }
  lines.push("");
  lines.push(`central URL: ${centralUrl}`);
  lines.push("(URL 끝의 `/api`는 반드시 포함. Caddy strip-prefix.)");
  lines.push("");

  if (manifest.config_fields.length === 0) {
    lines.push("이 커넥터는 별도 설정이 필요 없습니다.");
    lines.push(
      "파일을 `~/.orthus/collector/imports/<source>/`에 넣은 뒤, 사용자가 화면의 동기화를 누르거나",
    );
    lines.push("`orthus collector sync --source <slug>`를 실행하면 됩니다.");
    lines.push("");
    lines.push(`검증: \`orthus --central-url ${centralUrl} connector show ${manifest.slug}\``);
    return lines.join("\n");
  }

  const setParts: string[] = [];
  const secretFlags: string[] = [];
  for (const field of manifest.config_fields) {
    if (field.kind === "secret") {
      secretFlags.push(`--secret ${field.key}`);
    } else {
      setParts.push(`--set ${field.key}=<${field.label}>`);
    }
  }

  const cmdParts = [`orthus --central-url ${centralUrl} connector config ${manifest.slug}`];
  cmdParts.push(...setParts);
  cmdParts.push(...secretFlags);
  lines.push("실행 명령:");
  lines.push(cmdParts.join(" \\\n  "));
  lines.push("");
  lines.push("필드 설명:");
  for (const field of manifest.config_fields) {
    const tags: string[] = [];
    if (field.kind === "secret") tags.push("secret");
    if (field.required) tags.push("필수");
    const tagStr = tags.length ? ` (${tags.join(", ")})` : "";
    lines.push(`- ${field.key}: ${field.label}${tagStr}`);
  }
  if (secretFlags.length) {
    lines.push("");
    lines.push(
      "시크릿 값(token/API key/PAT 등)은 추정하지 말고 사용자에게 물어보세요. `--secret`는 getpass로 입력받습니다.",
    );
  }
  lines.push("");
  lines.push(`검증: \`orthus --central-url ${centralUrl} connector show ${manifest.slug}\``);
  return lines.join("\n");
}
