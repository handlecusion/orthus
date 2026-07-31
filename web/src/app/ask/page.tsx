"use client";

import Link from "next/link";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import {
  api,
  type AskScope,
  type AssistantResultPart,
  type AssistantResult,
  type AuthConfig,
  type DataGap,
  type KgGraphAnswer,
  type MailComposeDraft,
  type QueryValidation,
  type RoutedAnswer,
  type WikiAnswer,
  type WikiPageGraphResponse,
} from "@/lib/api";
import { GraphChipGroups } from "@/components/wiki/graph-chip-list";
import {
  buildGraphGroups,
  ENTITY_GLOSS,
  entityStarHeading,
  GRAPH_COPY,
  GRAPH_TOTAL_CAP,
  graphChipText,
  isClickableWikiNode,
  isEntityNode,
  wikiNodeHref,
} from "@/components/wiki/graph-shared";
import { ErrorBoundary } from "@/components/error-boundary";
import { useCompactAgentWorkShell } from "@/lib/use-compact-shell";
import { useStoredPreference } from "@/lib/use-stored-preference";

// 그래프 ring 뷰는 viewport 측정 기반이라 SSR을 끈다(하이드레이션 mismatch 방지). 위키 패널과 동일.
const GraphRingPanel = dynamic(() => import("@/components/wiki/graph-ring-panel"), {
  ssr: false,
});
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
} from "@/components/ui";

// K8.3 (D6) — router/graph.py의 _CONFLICT_VIEW_UNAVAILABLE과 짝. 어느 쪽이 바뀌면
// 배너가 조용히 사라지므로 양쪽에 같은 상수를 명시한다.
const CONFLICT_VIEW_UNAVAILABLE = "conflict_view_unavailable";

type ExamplePrompt = { q: string; hint: "structured" | "wiki" };

const COMPANY_EXAMPLES: ExamplePrompt[] = [
  { q: "협업업무표 상태별 건수", hint: "structured" },
  { q: "회사 미팅 요약", hint: "wiki" },
];

const PERSONAL_EXAMPLES: ExamplePrompt[] = [
  { q: "내 최근 AI 세션 요약", hint: "wiki" },
  { q: "개인 문서에서 이번 주 할 일", hint: "wiki" },
  { q: "협업업무표 상태별 건수", hint: "structured" },
];

const WIKI_SLUG_RE = /^[\p{L}\p{N}_/-]{1,200}$/u;

export default function AskPage() {
  const searchParams = useSearchParams();
  const consumedQueryRef = useRef<string | null>(null);
  const contextWikiSlug = cleanWikiSlug(searchParams.get("context_wiki_slug"));
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<RoutedAnswer | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // P8.5: when central exposes own-personal merge, default to 회사+개인 ('all').
  // Default falls back to 회사 (no scope sent → server clamp).
  const [askScope, setAskScope] = useState<AskScope>("all");

  useEffect(() => {
    let alive = true;
    api
      .getAuthConfig()
      .then((config) => {
        if (alive) setAuthConfig(config);
      })
      .catch(() => {
        if (alive) setAuthConfig(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  const nodeKind = authConfig?.node_kind ?? null;
  const isPersonal = nodeKind === "personal";
  const ownerScopeEnabled = authConfig?.owner_scope_enabled === true;
  // P8.5: company node + flag → 사용자가 회사만 / 회사+개인 토글
  const showCompanyScopeSelector = nodeKind === "company" && ownerScopeEnabled;
  const examples = isPersonal ? PERSONAL_EXAMPLES : COMPANY_EXAMPLES;
  const scopeLabel =
    nodeKind === "company"
      ? "회사"
      : isPersonal
        ? "회사 + 개인"
        : "범위";
  const scopeTitle =
    nodeKind === "company"
      ? "central company node는 회사 scope만 사용합니다."
      : "personal node는 회사 + 내 개인 scope를 함께 볼 수 있습니다.";
  const pageSubtitle = isPersonal
    ? "로컬 개인 소스와 회사 위키를 같은 작업 표면에서 묻습니다."
    : "질문하면 알아서 — 데이터는 검증된 SQL로, 지식은 위키 근거로 답합니다.";
  const placeholder = isPersonal
    ? "예) 내 최근 AI 세션 요약 / 개인 문서에서 이번 주 할 일"
    : "예) 협업업무표 상태별 건수 / 회사 미팅 요약";

  const run = useCallback(async (q?: string) => {
    const query = (q ?? question).trim();
    if (!query) return;
    setQuestion(query);
    setLoading(true);
    setError(null);
    try {
      const res = await api.ask(
        query,
        showCompanyScopeSelector ? askScope : undefined,
        contextWikiSlug,
      );
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "질문 처리에 실패했습니다.");
      setResult(null);
    } finally {
      setLoading(false);
    }
  }, [askScope, contextWikiSlug, question, showCompanyScopeSelector]);

  useEffect(() => {
    const query = (searchParams.get("q") ?? "").trim();
    const queryKey = `${query}\n${contextWikiSlug ?? ""}`;
    if (!query || consumedQueryRef.current === queryKey) return;
    consumedQueryRef.current = queryKey;
    void run(query);
  }, [contextWikiSlug, run, searchParams]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="어시스턴트"
        subtitle={pageSubtitle}
        right={
          <Toolbar>
            {showCompanyScopeSelector ? (
              <AskScopeSelector value={askScope} onChange={setAskScope} />
            ) : (
              <ToolbarButton icon={<FilterIcon />} aria-label="스코프" title={scopeTitle}>
                {scopeLabel}
              </ToolbarButton>
            )}
            {authConfig ? (
              <ToolbarButton icon={<NodeIcon />} aria-label="노드">
                {authConfig.node_id}
              </ToolbarButton>
            ) : null}
            <span className="hidden sm:inline-flex">
              <ToolbarButton icon={<FolderIcon />} aria-label="프로젝트">
                모든 프로젝트
              </ToolbarButton>
            </span>
          </Toolbar>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[920px] px-3 py-3 sm:px-5 sm:py-5">
          {/* Ask box — quiet card surface, single textarea, examples row */}
          <Card className="p-3">
            <label htmlFor="ask-q" className="sr-only">
              질문
            </label>
            <textarea
              id="ask-q"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void run();
              }}
              rows={2}
              placeholder={placeholder}
              className={cx(
                inputClass,
                "resize-none border-transparent bg-transparent px-0 py-1.5",
              )}
              style={{ border: "1px solid transparent" }}
            />
            <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap gap-1.5">
                {examples.map((ex) => (
                  <ToolbarButton
                    key={ex.q}
                    onClick={() => void run(ex.q)}
                    className="h-7 px-2 text-[var(--text-meta)]"
                  >
                    <span>{ex.q}</span>
                    <span
                      className="font-[family-name:var(--font-mono)] uppercase tracking-[0.06em]"
                      style={{
                        color: "var(--color-text-muted)",
                        fontSize: "var(--text-micro)",
                      }}
                    >
                      {ex.hint === "structured" ? "데이터" : "위키"}
                    </span>
                  </ToolbarButton>
                ))}
              </div>
              <ToolbarButton
                className="h-11 justify-center px-3"
                tone="primary"
                onClick={() => void run()}
                disabled={loading || !question.trim()}
              >
                {loading ? "묻는 중…" : "질문 (⌘↵)"}
              </ToolbarButton>
            </div>
          </Card>

          {contextWikiSlug ? (
            <div className="mt-2 flex min-w-0 flex-wrap items-center gap-1.5 px-1">
              <Chip tone="neutral">
                <span>위키 컨텍스트</span>
              </Chip>
              <Link
                className="inline-flex min-h-8 max-w-full items-center rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)]"
                href={wikiPageHref(contextWikiSlug)}
                style={{
                  background: "var(--color-card)",
                  border: "1px solid var(--color-toolbar-border)",
                  color: "var(--color-text-strong)",
                }}
              >
                <span className="truncate font-[family-name:var(--font-mono)]">
                  {contextWikiSlug}
                </span>
              </Link>
            </div>
          ) : null}

          {error ? (
            <div className="mt-4">
              <Banner tone="fail" title="요청 실패">
                {error}
              </Banner>
            </div>
          ) : null}

          {result?.warnings.length && result.mode === "structured" ? (
            <div className="mt-4 space-y-2">
              {result.warnings.map((warning) => (
                <Banner key={warning} tone="warn" title="부분 결과">
                  {warning}
                </Banner>
              ))}
            </div>
          ) : null}

          {/* K8.3 (D6) — 모순 질의가 그래프 미가용으로 wiki로 demote되면 정직한 신호를 띄운다
              (침묵 demote가 "모순 없음"으로 오독되지 않게). */}
          {result?.warnings.includes(CONFLICT_VIEW_UNAVAILABLE) ? (
            <div className="mt-4">
              <Banner tone="warn" title="모순 확인 불가">
                지식 그래프를 일시적으로 사용할 수 없어 모순 여부를 확인하지 못했습니다. 아래
                답변은 일반 위키 근거 기반입니다.
              </Banner>
            </div>
          ) : null}

          {loading ? (
            <Card className="mt-4 p-3">
              <Skeleton className="h-3 w-28" />
              <Skeleton className="mt-2.5 h-14 w-full" />
            </Card>
          ) : result ? (
            <div className="mt-4 space-y-3">
              {/* Mode + echoed question. Mode uses ModeBadge (10px / 800). */}
              <div className="flex items-center justify-between gap-2 px-1">
                <ModeBadge>
                  {result.mode === "structured" ? "STRUCTURED" : result.mode.toUpperCase()}
                </ModeBadge>
                <span
                  className="truncate text-[var(--text-meta)]"
                  style={{ color: "var(--color-text-muted)" }}
                  title={result.question}
                >
                  {result.question}
                </span>
              </div>

              {result.mode === "structured" && result.structured ? (
                <StructuredView
                  result={result.structured}
                  parts={result.structured_parts}
                />
              ) : (result.mode === "wiki" || result.mode === "graph") && result.wiki ? (
                // K4b: a graph answer reuses the wiki body renderer (spec §8.1).
                // K8.5 (FE5): graph metadata (result.graph) is now surfaced as a read-only
                // block below the body so a conflict query actually SHOWS the conflicts
                // (previously dropped). Reuses the K5 GraphChipGroups renderer.
                <>
                  <WikiView answer={result.wiki} />
                  {result.mode === "graph" && result.graph ? (
                    <AskGraphBlock graph={result.graph} />
                  ) : null}
                </>
              ) : (
                <Card className="p-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
                  응답 본문이 비어 있습니다 (mode: {result.mode}).
                </Card>
              )}

              {/* Inline agentic /ask (bedrock) compose output: editable draft + 보내기. */}
              {result.mail_draft ? (
                <MailDraftCard
                  key={`${result.question}-${result.mail_draft.to}-${result.mail_draft.subject}`}
                  draft={result.mail_draft}
                />
              ) : null}
            </div>
          ) : (
            <EmptyState />
          )}
        </div>
      </div>
    </div>
  );
}


/* ----------------------------- scope selector ---------------------------- */
/**
 * P8.5 company-node owner-scope selector. Two options: "회사" (no scope, server
 * defaults to company) and "회사 + 개인" (sends scope=all to merge the owner's
 * personal data). 44px tap targets so the phone parity contract holds.
 */
function AskScopeSelector({
  onChange,
  value,
}: {
  onChange: (next: AskScope) => void;
  value: AskScope;
}) {
  return (
    <div
      aria-label="질문 스코프"
      className="inline-flex h-11 min-h-11 shrink-0 items-stretch overflow-hidden rounded-[var(--radius-toolbar)]"
      role="group"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        boxShadow: "var(--shadow-toolbar)",
      }}
    >
      <AskScopeOption
        active={value === "company"}
        label="회사"
        onClick={() => onChange("company")}
        title="회사 위키와 데이터만 검색합니다."
      />
      <span
        aria-hidden
        className="my-1 w-px"
        style={{ background: "var(--color-toolbar-border)" }}
      />
      <AskScopeOption
        active={value === "all"}
        label="회사 + 개인"
        onClick={() => onChange("all")}
        title="회사 위키/데이터와 내 개인 지식을 함께 검색합니다."
      />
    </div>
  );
}

function AskScopeOption({
  active,
  label,
  onClick,
  title,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      aria-pressed={active}
      className="inline-flex min-h-11 shrink-0 items-center whitespace-nowrap px-3 text-[var(--text-body-sm)] transition-colors"
      onClick={onClick}
      style={{
        background: active ? "var(--color-sidebar-active)" : "transparent",
        color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
        fontWeight: active ? 600 : 500,
      }}
      title={title}
      type="button"
    >
      {label}
    </button>
  );
}

/* --------------------------- structured view --------------------------- */

// K8.5 (FE5) — read-only graph block under a mode=graph answer. Adapts KgGraphAnswer to the
// WikiPageGraphResponse shape buildGraphGroups consumes, then reuses the K5 chip renderer.
// Conflict intent with zero CONFLICTS_WITH edges shows a plain reassuring line instead of an
// empty canvas (FE5: "미해소 모순이 없습니다") so a clean result never reads as broken.
function AskGraphBlock({ graph }: { graph: KgGraphAnswer }) {
  // K9.3b — entity intent는 hop-기반 칩이 아니라 **엔티티 중심 star**다(중심=비클릭 개체, spoke=
  // 언급 page). buildGraphGroups는 page anchor를 가정하므로(엔티티는 slug 없음) 전용 star 렌더로
  // 분기한다. 텍스트 답(grounding)은 단독 완결이고, star는 그 위 접힘 시각 레이어다(U5).
  if (graph.intent === "entity") return <AskEntityStar graph={graph} />;
  const isConflict = graph.intent === "conflict";
  const hasConflict = graph.edges.some((e) => e.rel === "CONFLICTS_WITH");
  // anchor slug를 buildGraphGroups의 currentId로 넘겨 hop 거리 + 충돌 역할 배지("이 페이지" vs
  // "상대")를 계산하고 시작 노드가 칩으로 새지 않게 한다. **`path_slugs[0]`(=시작 노드, raw)**를
  // 쓴다 — `params_redacted.slug`는 PII 마스킹돼(코드리뷰) raw `graph.nodes[].slug`와 매칭이
  // 깨질 수 있다(마스킹된 slug → currentId=null → 전부 "상대"+hop=Infinity). spine.path_slugs는
  // RETURN 노드 순서라 [0]이 anchor다(page_conflicts=페이지, conflicts_of=claim, path_between=slug_a).
  // FE5 — zero-conflict 안내가 "확인한 페이지(<slug>)"로 페이지를 명시하도록 early-return 위에 둔다.
  const anchorSlug = graph.path_slugs[0] ?? "";
  if (isConflict && !hasConflict) {
    return (
      <Card
        className="p-3 text-[var(--text-body-sm)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        {anchorSlug
          ? `확인한 페이지(${anchorSlug})에 미해소 모순이 없습니다.`
          : "확인한 페이지에 미해소 모순이 없습니다."}
      </Card>
    );
  }
  const data: WikiPageGraphResponse = {
    slug: anchorSlug,
    supported: true,
    reason: null,
    truncated: graph.truncated,
    nodes: graph.nodes,
    edges: graph.edges,
    entity_teaser: null, // K4b /ask graph는 entity 티저 없음(K9.3b 별도)
    personal_entity_search_deferred: false,
  };
  const built = buildGraphGroups(data);
  // FE5 framing — conflict 답변은 page_conflicts 그래프(page-BACKLINK-claim-CONFLICTS_WITH-
  // counterpart)를 담는데, /ask 모순 답변은 "모순"만 보여야 하므로 conflict intent에서는
  // CONFLICTS_WITH 그룹만 남긴다(BACKLINK claim 그룹은 currentId=anchorSlug로 이미 제외되지만,
  // 명시적으로 conflict-only로 좁힌다). relation/provenance intent는 경로 전체가 의미이므로 그대로 둔다.
  const groups = isConflict
    ? built.groups.filter((g) => g.rel === "CONFLICTS_WITH")
    : built.groups;
  if (groups.length === 0) return null;
  return (
    <Card className="p-3">
      <GraphChipGroups built={{ groups, truncated: built.truncated }} busy={false} />
    </Card>
  );
}

// sticky 펼침 선호(한 번 닫으면 유지, U5 firm). useStoredPreference로 SSR-safe(기본 접힘).
const ENTITY_STAR_OPEN_KEY = "orthus:askEntityStarOpen";
const ENTITY_STAR_OPEN_VALUES = ["open", "closed"] as const;
// 모바일 hard-cap(U5/NH-5) — phone에서 spoke 과밀 ring을 강제 금지. desktop은 GRAPH_TOTAL_CAP.
const ENTITY_STAR_PHONE_CAP = 8;

// K9.3b — entity intent 답 아래 cross-page 연결 star(D-#5 옵션 B). entity_mentions가 이미 돌린
// 결과(엔티티 중심 star + MENTIONED_IN 다리)를 그대로 시각화한다 — 페이지들이 그 개체를 매개로
// 어떻게 엮였는지(2-hop 다리)가 보인다. 본문(grounding)은 단독 완결이고 이건 위 시각 레이어다
// (불변식 5 보존). 기본 접힘(firm) + sticky, hub면 정직한 개수 표기 + 캡(모바일 hard-cap),
// 엔티티 중심 비클릭(ring이 label!==WikiPage를 비링크 렌더), page spoke는 클릭→/wiki 이동.
function AskEntityStar({ graph }: { graph: KgGraphAnswer }) {
  const compact = useCompactAgentWorkShell();
  const [openPref, setOpenPref] = useStoredPreference<(typeof ENTITY_STAR_OPEN_VALUES)[number]>(
    ENTITY_STAR_OPEN_KEY,
    ENTITY_STAR_OPEN_VALUES,
    "closed",
  );
  const open = openPref === "open";
  const toggle = () => setOpenPref(open ? "closed" : "open");

  const entity = graph.nodes.find(isEntityNode);
  const pages = graph.nodes.filter(isClickableWikiNode);
  // 엔티티 중심/spoke가 없으면 미렌더 — 텍스트 답이 단독 완결(U5, calm 빈 상태). 서버는 보통
  // 이 경우 no_groundable_pages로 wiki demote하므로 방어적 가드다.
  if (!entity || pages.length === 0) return null;

  const cap = compact ? ENTITY_STAR_PHONE_CAP : GRAPH_TOTAL_CAP;
  const shownPages = pages.slice(0, cap);
  const keep = new Set<string>([entity.id, ...shownPages.map((p) => p.id)]);
  const nodes = [entity, ...shownPages];
  const edges = graph.edges.filter((e) => keep.has(e.src) && keep.has(e.dst));
  const truncated = graph.truncated || pages.length > shownPages.length;

  return (
    <Card className="p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p
          className="min-w-0 break-words text-[var(--text-body-sm)] font-semibold [overflow-wrap:anywhere]"
          style={{ color: "var(--color-text-strong)" }}
        >
          {entityStarHeading(entity, shownPages.length)}
        </p>
        <button
          aria-expanded={open}
          className="inline-flex min-h-11 shrink-0 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
          onClick={toggle}
          style={{
            background: "var(--color-card)",
            border: "1px solid var(--color-toolbar-border)",
            color: "var(--color-text-strong)",
          }}
          type="button"
        >
          {open ? GRAPH_COPY.entityCollapse : GRAPH_COPY.entityExpand}
        </button>
      </div>
      {open ? (
        <div className="mt-3">
          <p className="mb-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            {ENTITY_GLOSS}
          </p>
          {/* 그래프 렌더 실패는 fail-open: 클릭 가능한 page 목록으로 강등(텍스트 답은 이미 단독 완결). */}
          <ErrorBoundary
            fallback={<EntityPageList pages={shownPages} />}
            onError={(err) => console.error("entity star render failed; page list fallback", err)}
            resetKey={`${entity.id}:${open}`}
          >
            <GraphRingPanel
              compact={compact}
              currentNodeId={entity.id}
              edges={edges}
              nodes={nodes}
            />
          </ErrorBoundary>
          {truncated ? (
            <p className="mt-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
              {GRAPH_COPY.truncated}
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

// ring 렌더 실패 시 fail-open fallback — 언급 page를 클릭 가능한 링크 목록으로. spoke 노드와
// 동일한 href 인코딩(wikiNodeHref)을 써 ring과 같은 이동 대상이다.
function EntityPageList({ pages }: { pages: KgGraphAnswer["nodes"] }) {
  return (
    <ul className="space-y-1">
      {pages.map((p) => {
        const href = wikiNodeHref(p);
        return (
          <li key={p.id}>
            {href ? (
              <Link
                className="text-[var(--text-body-sm)] underline"
                href={href}
                style={{ color: "var(--color-text-strong)" }}
              >
                {graphChipText(p)}
              </Link>
            ) : (
              <span className="text-[var(--text-body-sm)]">{graphChipText(p)}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function StructuredView({
  result,
  parts = [],
}: {
  result: AssistantResult;
  parts?: AssistantResultPart[];
}) {
  const visibleParts = parts.length
    ? parts
    : [{ source_scope: "company" as const, source_node_id: null, result }];
  if (visibleParts.length > 1) {
    return (
      <div className="space-y-3">
        {visibleParts.map((part) => (
          <section
            key={`${part.source_scope}-${part.source_node_id ?? "node"}-${part.result.query_id}`}
            className="space-y-2"
          >
            <div className="flex items-center gap-2 px-1">
              <Chip tone={part.source_scope === "company" ? "pass" : "neutral"}>
                {scopeLabel(part.source_scope)}
              </Chip>
              {part.source_node_id ? (
                <span
                  className="font-[family-name:var(--font-mono)] text-[var(--text-meta)]"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {part.source_node_id}
                </span>
              ) : null}
            </div>
            <StructuredResultBody result={part.result} />
          </section>
        ))}
      </div>
    );
  }
  return <StructuredResultBody result={visibleParts[0].result} />;
}

function StructuredResultBody({ result }: { result: AssistantResult }) {
  const rejected = result.status === "rejected";
  const failed = result.status === "failed";
  const executed = result.status === "executed";

  return (
    <div className="space-y-3">
      {rejected ? (
        <Banner
          tone="warn"
          title={`거부됨 — ${result.validation.rejected_reason ?? "사유 미상"}`}
        >
          {result.message ??
            "안전 게이트를 통과하지 못해 실행이 차단되었습니다. 결과는 표시되지 않습니다."}
        </Banner>
      ) : null}

      {failed ? (
        <Banner tone="fail" title="실행 실패">
          {result.message ?? "질의 실행 중 오류가 발생했습니다."}
        </Banner>
      ) : null}

      {executed ? (
        <section aria-label="질의 결과">
          <div className="mb-1.5 flex items-baseline justify-between gap-2 px-1">
            <h2
              className="text-[var(--text-body)] font-bold"
              style={{ color: "var(--color-text-strong)" }}
            >
              결과
            </h2>
            <span
              className="text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {result.row_count ?? result.rows.length}행
              {result.latency_ms != null ? (
                <>
                  {" · "}
                  <span className="font-[family-name:var(--font-mono)]">
                    {result.latency_ms}ms
                  </span>
                </>
              ) : null}
            </span>
          </div>
          <ResultTable columns={result.columns} rows={result.rows} />
        </section>
      ) : null}

      {/* Compiled SQL — collapsible card */}
      <Card className="overflow-hidden p-0">
        <details className="group">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2">
            <span className="flex items-center gap-2">
              <span
                className="text-[var(--text-body-sm)] font-semibold"
                style={{ color: "var(--color-text-strong)" }}
              >
                생성된 SQL
              </span>
              <StatusChip status={result.status} />
            </span>
            <ChevronIcon />
          </summary>
          <div
            className="border-t p-3"
            style={{ borderColor: "var(--color-divider-soft)" }}
          >
            <pre
              className="overflow-x-auto rounded-[5px] p-2.5 font-[family-name:var(--font-mono)] text-[var(--text-body-sm)] leading-[1.5]"
              style={{
                background: "var(--color-panel)",
                border: "1px solid var(--color-divider-soft)",
                color: "var(--color-text-strong)",
              }}
            >
              <code>{result.compiled?.sql ?? "-- (SQL이 생성되지 않았습니다)"}</code>
            </pre>
            {result.compiled ? (
              <p
                className="mt-1.5 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                dialect{" "}
                <span className="font-[family-name:var(--font-mono)]">
                  {result.compiled.dialect}
                </span>
              </p>
            ) : null}
            {result.validation.injected_limit != null ? (
              <p
                className="mt-1 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                안전 LIMIT{" "}
                <span
                  className="font-[family-name:var(--font-mono)]"
                  style={{ color: "var(--color-text-strong)" }}
                >
                  {result.validation.injected_limit}
                </span>{" "}
                주입됨
              </p>
            ) : null}

            <div className="mt-2.5">
              <ValidationGates validation={result.validation} />
            </div>
          </div>
        </details>
      </Card>

      {result.grounding.length > 0 ? (
        <div
          className="flex flex-wrap items-center gap-1.5 px-1 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          <span
            className="font-semibold"
            style={{ color: "var(--color-text-body)" }}
          >
            근거
          </span>
          {result.grounding.map((g, i) => (
            <Chip key={`${g.kind}-${g.ref}-${i}`} tone="neutral">
              <span className="font-[family-name:var(--font-mono)]">
                {g.kind}:{g.ref.slice(0, 8)}
              </span>
            </Chip>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------ wiki view ------------------------------ */

function WikiView({ answer }: { answer: WikiAnswer }) {
  return (
    <div className="space-y-3">
      {answer.warnings.length ? (
        <div className="space-y-2">
          {answer.warnings.map((warning) => (
            <Banner key={warning} tone="warn" title="부분 결과">
              {warning}
            </Banner>
          ))}
        </div>
      ) : null}
      <Card className="p-4">
        <p
          className="whitespace-pre-wrap text-[var(--text-body)] leading-[1.55]"
          style={{ color: "var(--color-text-strong)" }}
        >
          {answer.answer}
        </p>
        <WikiCitationLinks answer={answer} />
      </Card>

      {answer.gap ? <GapBanner answer={answer} /> : null}

      <section aria-label="근거 출처">
        <div className="mb-1.5 flex items-baseline gap-2 px-1">
          <h2
            className="text-[var(--text-body)] font-bold"
            style={{ color: "var(--color-text-strong)" }}
          >
            근거
          </h2>
          <span
            className="text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {answer.sources.length}개 출처
          </span>
        </div>

        {answer.sources.length === 0 ? (
          <Card className="p-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            관련 근거를 찾지 못했습니다.
          </Card>
        ) : (
          <ol className="space-y-2">
            {answer.sources.map((s, i) => (
              <li key={`${s.page_slug}-${i}`}>
                <Card interactive className="p-3">
                  <div className="mb-1.5 flex items-center justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
                      <span
                        className="flex h-4 w-4 shrink-0 items-center justify-center rounded-[3px] font-[family-name:var(--font-mono)] text-[10px]"
                        style={{
                          background: "var(--color-panel)",
                          border: "1px solid var(--color-divider)",
                          color: "var(--color-text-body)",
                        }}
                      >
                        {i + 1}
                      </span>
                      <span className="truncate font-semibold" style={{ color: "var(--color-text-strong)" }}>
                        {s.title}
                      </span>
                      <span className="shrink-0 font-[family-name:var(--font-mono)] tracking-[0.04em]">
                        {s.kind}
                      </span>
                      <Chip tone={s.source_scope === "company" ? "pass" : "neutral"}>
                        {scopeLabel(s.source_scope)}
                      </Chip>
                    </span>
                    <ScoreBadge score={s.score} />
                  </div>
                  <p
                    className="whitespace-pre-wrap text-[var(--text-body-sm)] leading-[1.55]"
                    style={{ color: "var(--color-text-body)" }}
                  >
                    {s.excerpt}
                  </p>
                  {s.provenance.length > 0 ? (
                    <div className="mt-2 flex flex-wrap items-center gap-1">
                      {s.provenance.map((p, pi) => (
                        <Chip key={`${p}-${pi}`} tone="neutral">
                          <span className="font-[family-name:var(--font-mono)]">{p}</span>
                        </Chip>
                      ))}
                    </div>
                  ) : null}
                </Card>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}

function WikiCitationLinks({ answer }: { answer: WikiAnswer }) {
  if (!answer.wiki_links.length) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t pt-3" style={{ borderColor: "var(--color-divider)" }}>
      {answer.wiki_links.map((link) => (
        <Link
          className="inline-flex min-h-8 max-w-full items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)]"
          href={wikiPageHref(link.slug)}
          key={`${link.scope}-${link.slug}`}
          style={{
            background: "var(--color-panel)",
            border: "1px solid var(--color-divider)",
            color: "var(--color-text-strong)",
          }}
        >
          <span className="truncate">{link.title || link.slug}</span>
          <span className="shrink-0 font-[family-name:var(--font-mono)] text-[var(--text-meta)]">
            {link.scope === "company" ? "회사" : "개인"}
          </span>
        </Link>
      ))}
    </div>
  );
}

/* ----------------------------- mail draft ------------------------------ */

/**
 * Inline agentic /ask (bedrock engine) compose output. The assistant produced a
 * NON-sent draft; the operator edits the fields and clicks 보내기, which POSTs to
 * /mail/send — the click IS the owner approval (the assistant never sends). The
 * send endpoint re-validates from_addr ownership + rate limit, so a bad edit
 * fails closed there.
 */
function MailDraftCard({ draft }: { draft: MailComposeDraft }) {
  const [from, setFrom] = useState(draft.from_addr);
  const [to, setTo] = useState(draft.to);
  const [subject, setSubject] = useState(draft.subject);
  const [body, setBody] = useState(draft.body);
  const [status, setStatus] = useState<"idle" | "sending" | "sent">("idle");
  const [error, setError] = useState<string | null>(null);

  const sent = status === "sent";
  const canSend =
    status === "idle" && from.trim().includes("@") && to.trim().includes("@");

  async function send() {
    setStatus("sending");
    setError(null);
    try {
      const res = await api.sendMail({
        from_addr: from.trim(),
        to: to.trim(),
        subject,
        text: body,
      });
      if (res.status === "sent") {
        setStatus("sent");
      } else {
        setStatus("idle");
        setError(res.error ?? "발송에 실패했습니다.");
      }
    } catch (e) {
      setStatus("idle");
      setError(e instanceof Error ? e.message : "발송에 실패했습니다.");
    }
  }

  return (
    <Card className="space-y-2.5 p-4">
      <div className="flex items-center justify-between gap-2">
        <h2
          className="text-[var(--text-body)] font-bold"
          style={{ color: "var(--color-text-strong)" }}
        >
          메일 초안
        </h2>
        <span
          className="text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          보내기 = 즉시 발송 · 검토 후 직접 누르세요
        </span>
      </div>

      <MailDraftField label="보내는사람">
        <input
          className={cx(inputClass, "w-full")}
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          disabled={sent}
          placeholder="me@acme.example"
        />
      </MailDraftField>
      <MailDraftField label="받는사람">
        <input
          className={cx(inputClass, "w-full")}
          value={to}
          onChange={(e) => setTo(e.target.value)}
          disabled={sent}
          placeholder="someone@example.com"
        />
      </MailDraftField>
      <MailDraftField label="제목">
        <input
          className={cx(inputClass, "w-full")}
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          disabled={sent}
        />
      </MailDraftField>
      <MailDraftField label="본문">
        <textarea
          className={cx(inputClass, "w-full resize-y")}
          rows={8}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          disabled={sent}
        />
      </MailDraftField>

      {error ? (
        <Banner tone="fail" title="발송 실패">
          {error}
        </Banner>
      ) : null}
      {sent ? (
        <Banner tone="pass" title="보냈습니다">
          메일이 발송됐습니다.
        </Banner>
      ) : null}

      <div className="flex justify-end">
        <ToolbarButton
          className="h-11 justify-center px-4"
          tone="primary"
          onClick={() => void send()}
          disabled={!canSend}
        >
          {status === "sending" ? "보내는 중…" : sent ? "보냄" : "보내기"}
        </ToolbarButton>
      </div>
    </Card>
  );
}

function MailDraftField({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span
        className="mb-1 block text-[var(--text-meta)] font-semibold"
        style={{ color: "var(--color-text-body)" }}
      >
        {label}
      </span>
      {children}
    </label>
  );
}

function cleanWikiSlug(value: string | null): string | null {
  const slug = value?.trim() ?? "";
  return WIKI_SLUG_RE.test(slug) ? slug : null;
}

function wikiPageHref(slug: string): string {
  return `/wiki/${slug.split("/").map(encodeURIComponent).join("/")}`;
}

function ScoreBadge({ score }: { score: number }) {
  return (
    <span
      className="font-[family-name:var(--font-mono)] text-[var(--text-meta)] tracking-[0.04em]"
      style={{ color: "var(--color-text-muted)" }}
    >
      score {score.toFixed(3)}
    </span>
  );
}

function scopeLabel(scope: "company" | "personal") {
  return scope === "company" ? "회사" : "개인";
}

/* ------------------------------- data gap ------------------------------ */

const GAP_REASON_LABEL: Record<DataGap["reason"], string> = {
  no_data: "데이터 없음",
  weak_retrieval: "관련 자료 부족",
  insufficient_grounding: "정리된 설명 부족",
  missing_link: "자료 간 연결 없음",
};

/** Shown when the wiki answer was poorly grounded. The deterministic message is
 *  always present; pressing the button asks the server for an LLM suggestion of
 *  what to add where, and records the gap into the backlog as 'feedback'. */
function GapBanner({ answer }: { answer: WikiAnswer }) {
  const gap = answer.gap;
  const [suggestion, setSuggestion] = useState<DataGap | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!gap) return null;

  async function loadSuggestion() {
    setLoading(true);
    setError(null);
    try {
      setSuggestion(await api.gapFeedback(answer.question));
    } catch (e) {
      setError(e instanceof Error ? e.message : "보강 제안 생성에 실패했습니다.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Banner tone="warn" title={`데이터 부족 · ${GAP_REASON_LABEL[gap.reason]}`}>
      <p className="leading-[1.55]">{gap.message}</p>
      {suggestion ? (
        <GapSuggestion gap={suggestion} />
      ) : (
        <div className="mt-2 flex items-center gap-2">
          <ToolbarButton onClick={() => void loadSuggestion()} disabled={loading}>
            {loading ? "보강 제안 생성 중…" : "어떤 데이터를 넣을까요?"}
          </ToolbarButton>
          {error ? (
            <span className="text-[var(--text-meta)]" style={{ color: "var(--color-fail-fg)" }}>
              {error}
            </span>
          ) : null}
        </div>
      )}
    </Banner>
  );
}

function GapSuggestion({ gap }: { gap: DataGap }) {
  return (
    <div className="mt-2.5 space-y-2">
      <div className="flex flex-wrap items-center gap-1.5 text-[var(--text-meta)]">
        {gap.suggested_target ? (
          <span style={{ color: "var(--color-text-strong)" }}>
            <span className="font-semibold">추가할 곳</span> · {gap.suggested_target}
          </span>
        ) : null}
        {gap.suggested_connector ? (
          <Chip tone="neutral">
            <span className="font-[family-name:var(--font-mono)]">
              sync: {gap.suggested_connector}
            </span>
          </Chip>
        ) : null}
      </div>
      {gap.suggested_fields.length > 0 ? (
        <div className="space-y-2">
          {gap.suggested_fields.map((section, si) => (
            <div key={`${section.title}-${si}`}>
              <p
                className="text-[var(--text-body-sm)] font-semibold"
                style={{ color: "var(--color-text-strong)" }}
              >
                {section.title}
              </p>
              <ul
                className="ml-4 list-disc text-[var(--text-body-sm)] leading-[1.6]"
                style={{ color: "var(--color-text-body)" }}
              >
                {section.items.map((item, ii) => (
                  <li key={ii}>{item}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          추가할 정보 제안을 생성하지 못했습니다.
        </p>
      )}
    </div>
  );
}

/* ----------------------------- validation ------------------------------ */

const GATES: Array<{ key: keyof QueryValidation; label: string }> = [
  { key: "parsed", label: "파싱" },
  { key: "statement_kind", label: "구문 종류" },
  { key: "schema_ok", label: "스키마" },
  { key: "read_only_ok", label: "읽기 전용" },
  { key: "explain_ok", label: "EXPLAIN" },
  { key: "injected_limit", label: "LIMIT 주입" },
];

function ValidationGates({ validation }: { validation: QueryValidation }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {GATES.map(({ key, label }) => {
        const { pass, value } = interpretGate(key, validation[key]);
        return (
          <span
            key={key}
            className="inline-flex items-center gap-1 rounded-[4px] px-1.5 py-[2px] text-[var(--text-meta)] font-medium"
            style={{
              background: pass ? "var(--color-pass-bg)" : "var(--color-fail-bg)",
              color: pass ? "var(--color-pass-fg)" : "var(--color-fail-fg)",
            }}
          >
            <GateIcon pass={pass} />
            {label}
            {value != null ? (
              <span className="font-[family-name:var(--font-mono)] opacity-80">
                {value}
              </span>
            ) : null}
          </span>
        );
      })}
    </div>
  );
}

function interpretGate(
  key: keyof QueryValidation,
  raw: QueryValidation[keyof QueryValidation],
): { pass: boolean; value: string | null } {
  if (key === "statement_kind") {
    const kind = typeof raw === "string" ? raw : null;
    return { pass: kind === "select", value: kind };
  }
  if (key === "injected_limit") {
    const n = typeof raw === "number" ? raw : null;
    return { pass: n != null, value: n != null ? String(n) : "none" };
  }
  return { pass: raw === true, value: null };
}

function GateIcon({ pass }: { pass: boolean }) {
  return pass ? (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="m5 13 4 4L19 7" />
    </svg>
  ) : (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M6 6l12 12M18 6 6 18" />
    </svg>
  );
}

function StatusChip({ status }: { status: string }) {
  const map: Record<string, { tone: "pass" | "fail" | "warn"; label: string }> =
    {
      executed: { tone: "pass", label: "실행됨" },
      rejected: { tone: "warn", label: "거부됨" },
      failed: { tone: "fail", label: "실패" },
    };
  const cfg = map[status] ?? { tone: "warn" as const, label: status };
  return <Chip tone={cfg.tone}>{cfg.label}</Chip>;
}

function ChevronIcon() {
  return (
    <svg
      className="shrink-0 transition-transform group-open:rotate-180"
      width="12" height="12" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden
      style={{ color: "var(--color-text-muted)" }}
    >
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

/* ------------------------------- results ------------------------------- */

function ResultTable({
  columns,
  rows,
}: {
  columns: string[];
  rows: unknown[][];
}) {
  if (rows.length === 0) {
    return (
      <Card className="p-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
        조건에 맞는 행이 없습니다.
      </Card>
    );
  }
  return (
    <Card className="overflow-hidden p-0">
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[var(--text-body-sm)]">
          <thead>
            <tr
              style={{
                background: "var(--color-panel)",
                borderBottom: "1px solid var(--color-divider)",
              }}
            >
              {columns.map((c) => (
                <th
                  key={c}
                  scope="col"
                  className="whitespace-nowrap px-2.5 py-1.5 text-left font-[family-name:var(--font-mono)] text-[var(--text-meta)] font-semibold tracking-[0.04em]"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, ri) => (
              <tr
                key={ri}
                className="last:border-0"
                style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
              >
                {row.map((cell, ci) => (
                  <td
                    key={ci}
                    className="whitespace-nowrap px-2.5 py-1.5 font-[family-name:var(--font-mono)] text-[var(--text-body-sm)]"
                    style={{ color: "var(--color-text-strong)" }}
                  >
                    {formatCell(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function formatCell(cell: unknown): string {
  if (cell === null || cell === undefined) return "∅";
  if (typeof cell === "object") return JSON.stringify(cell);
  return String(cell);
}

function EmptyState() {
  return (
    <div className="mt-10 flex flex-col items-center justify-center py-10 text-center">
      <div
        className="flex h-10 w-10 items-center justify-center rounded-[6px]"
        style={{
          background: "var(--color-card)",
          border: "1px solid var(--color-divider)",
          color: "var(--color-text-muted)",
        }}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="m5 8 4 4-4 4" />
          <path d="M13 16h6" />
        </svg>
      </div>
      <p
        className="mt-2.5 max-w-[360px] text-[var(--text-body-sm)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        한 곳에서 묻습니다. 데이터 질문은 검증된 SQL로 표를, 지식 질문은 근거가 붙은 위키 답변을 돌려드립니다.
      </p>
    </div>
  );
}

/* -------------------------------- icons -------------------------------- */

function FilterIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M3 4h18l-7 9v7l-4-2v-5z" />
    </svg>
  );
}

function FolderIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2.5h9A1.5 1.5 0 0 1 21 9v8.5A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z" />
    </svg>
  );
}

function NodeIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <path d="M9 9h6v6H9z" />
      <path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" />
    </svg>
  );
}
