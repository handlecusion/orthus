"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  api,
  API_BASE,
  DEMO_USER_ID,
  type DashboardProject,
  type Kpi,
  type PeriodSummary,
  type PlanItem,
  type PresenceItem,
} from "@/lib/api";
import {
  ChannelHeader,
  Card,
  Banner,
  Chip,
  Skeleton,
  ToolbarButton,
  Avatar,
  ListRow,
  SectionHeader,
  cx,
} from "@/components/ui";
import { ScorePicker, Select, labelStyle } from "@/components/dashboard/kit";
import { ItemDetailModal } from "@/components/dashboard/ItemDetailModal";
import { KpiRetroCard } from "@/components/dashboard/KpiRetroCard";

type Mode = "weekly" | "monthly";
const BACKUP_REFRESH_MS = 120_000;

interface Entry {
  plan: PlanItem[];
  retro: PlanItem[];
  // 로드 시점 서버 타임스탬프. 저장 시 base_updated_at으로 되돌려 보내
  // '의도적 비우기'(로드함)와 '사고성 빈 저장'(로드 안 함)을 서버가 구분한다.
  updatedAt?: string | null;
}
interface AllEntry {
  plan: PlanItem[];
  retro: PlanItem[];
  prevPlan: PlanItem[];
  prevRetro: PlanItem[];
  updatedAt?: string | null;
  prevUpdatedAt?: string | null;
}
// Path A 라이브 협업: SSE로 중계되는 입력-중 plan/retro (저장 전, 영속 아님).
interface LiveEditEvent {
  type: "live_edit";
  kind: Mode;
  project_id: string;
  period_key: string;
  plan: PlanItem[];
  retro: PlanItem[];
  editor_id: string;
}

/* ----------------------------- date helpers ---------------------------- */
function ymd(d: Date): string {
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}
function sundayOf(d: Date): Date {
  const r = new Date(d);
  const offset = r.getDay(); // 0 = Sunday
  r.setDate(r.getDate() - offset);
  r.setHours(0, 0, 0, 0);
  return r;
}
function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}
function addMonths(d: Date, n: number): Date {
  const r = new Date(d);
  r.setMonth(r.getMonth() + n, 1);
  return r;
}
function weekLabel(sunday: Date): string {
  const sat = addDays(sunday, 6);
  return `${ymd(sunday)} ~ ${ymd(sat)} (일~토)`;
}
function monthLabel(d: Date): string {
  return `${d.getFullYear()}년 ${d.getMonth() + 1}월`;
}
function newId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `${Date.now()}-${Math.round(Math.random() * 1e6)}`;
  }
}

/* ---------------------- auto-save identity / snapshot ------------------- */
// 현재 보고 있는 (프로젝트·모드·기간) 식별자. 이게 바뀌면 다른 기간을 불러온 것.
function idOf(pid: string, mode: Mode, key: string): string {
  return `${pid || "all"}|${mode}|${key}`;
}
function serSingle(plan: PlanItem[], retro: PlanItem[], prev: Entry): string {
  return JSON.stringify({ plan, retro, prev });
}
function serAll(entries: Record<string, AllEntry>, projects: DashboardProject[]): string {
  return JSON.stringify(projects.map((p) => [p.project_id, entries[p.project_id] ?? null]));
}

// 프로젝트별 하위 칸 정의. AX는 "대본"/"아트인" 두 칸으로 자동 분리한다.
// 그 외 프로젝트는 분리하지 않는다(null → 단일 칸).
const PROJECT_GROUPS: Record<string, readonly string[]> = {
  AX: ["대본", "아트인"],
};
function groupsFor(name: string): readonly string[] | null {
  return PROJECT_GROUPS[name.trim()] ?? null;
}
// 항목을 칸에 배치. 미지정/모르는 group인 레거시 항목은 첫 칸으로.
function itemGroup(it: PlanItem, groups: readonly string[]): string {
  return it.group && groups.includes(it.group) ? it.group : groups[0];
}

function reconcileExpandedProjects(
  current: Set<string>,
  projects: DashboardProject[],
): Set<string> {
  const validIds = new Set(projects.map((p) => p.project_id));
  const next = new Set<string>();
  let changed = false;
  current.forEach((id) => {
    if (validIds.has(id)) next.add(id);
    else changed = true;
  });
  if (next.size === 0 && projects.length > 0) {
    next.add(projects[0].project_id);
    changed = true;
  }
  return changed ? next : current;
}

export default function PlansPage() {
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [projectId, setProjectId] = useState<string>("");
  const [expandedAllProjectIds, setExpandedAllProjectIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [mode, setMode] = useState<Mode>("weekly");
  const [ref, setRef] = useState<Date>(() => new Date());
  // ---- 프레즌스(누가 어디 작성 중) ----
  // 5초마다 heartbeat를 보내 본인 위치를 알리고 활성 사용자 목록을 받는다.
  // 단일 사용자(데모) 환경에서는 본인만 보이므로 화면엔 '나 외 다른 사용자'만 표시.
  const [presences, setPresences] = useState<PresenceItem[]>([]);
  const projectIdRef = useRef("");
  useEffect(() => {
    projectIdRef.current = projectId;
  }, [projectId]);
  useEffect(() => {
    let alive = true;
    const beat = () => {
      api
        .presenceHeartbeat({
          area: "plans",
          project_id: projectIdRef.current || null,
          section: null,
        })
        .then((list) => {
          if (alive) setPresences(list);
        })
        .catch(() => {
          /* 프레즌스는 부가 기능 */
        });
    };
    beat();
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      beat();
    }, 5000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, []);

  // 담당자 부여용 팀원 목록
  const [members, setMembers] = useState<Member[]>([]);
  useEffect(() => {
    let alive = true;
    api
      .listTeamMembers()
      .then((ms) => {
        if (alive)
          setMembers(ms.map((m) => ({ member_id: m.member_id, name: m.name, color: m.color })));
      })
      .catch(() => {
        /* 담당자 셀렉터는 부가 — 실패해도 계획·회고는 동작 */
      });
    return () => {
      alive = false;
    };
  }, []);

  // 계획 항목에 연결할 KPI 목록(해당 연도). 변경 시 셀렉터 옵션도 갱신.
  const [kpis, setKpis] = useState<Kpi[]>([]);
  const fiscalYear = ref.getFullYear();
  useEffect(() => {
    let alive = true;
    api
      .listKpis(fiscalYear)
      .then((ks) => alive && setKpis(ks))
      .catch(() => alive && setKpis([]));
    return () => {
      alive = false;
    };
  }, [fiscalYear]);
  const kpiLevelRank: Record<string, number> = { objective: 0, key_result: 1, target: 2 };
  // 계획/회고에서 KPI를 고를 때는 그 프로젝트(섹터)의 KPI + 회사 공통 KPI(project_id 없음,
  // 예: 투자)만 보여준다 — 다른 프로젝트의 KPI는 빼서 "모든 KPI"가 아니라 섹터별로 좁힌다.
  const kpiOptionsFor = (forProjectId: string) =>
    kpis
      .filter((k) => k.level !== "north_star")
      .filter((k) => !forProjectId || !k.project_id || k.project_id === forProjectId)
      .slice()
      .sort(
        (a, b) =>
          (kpiLevelRank[a.level] ?? 3) - (kpiLevelRank[b.level] ?? 3) ||
          a.sort_order - b.sort_order,
      )
      .map((k) => {
        const period =
          k.cadence === "monthly"
            ? `${k.month}월`
            : k.cadence === "quarterly"
              ? `Q${k.quarter}`
              : "연간";
        const lv =
          k.level === "objective" ? "목표" : k.level === "key_result" ? "KR" : "월타깃";
        const scope = k.project_id ? "" : "·회사"; // 회사 공통 KPI 표시(예: 투자)
        return { value: k.kpi_id, label: `[${period}·${lv}${scope}] ${k.title}` };
      });

  // 이번 기간: plan(=다음 점검용 계획), retro(회고 메모)
  const [plan, setPlan] = useState<PlanItem[]>([]);
  const [retro, setRetro] = useState<PlanItem[]>([]);
  // 현재 기간 로드 타임스탬프(단일 프로젝트 보기). 저장 시 base_updated_at으로 전송.
  const [curUpdatedAt, setCurUpdatedAt] = useState<string | null>(null);
  // 지난 기간 entry — '계획 점검'은 지난 기간에 세운 계획을 본다.
  const [prevEntry, setPrevEntry] = useState<Entry>({ plan: [], retro: [] });
  // 전체(모든 프로젝트) 보기용: projectId === "" 이면 전체
  const [allEntries, setAllEntries] = useState<Record<string, AllEntry>>({});
  const isAll = projectId === "";
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [newProject, setNewProject] = useState("");
  const [periods, setPeriods] = useState<PeriodSummary[]>([]);
  const [periodOpen, setPeriodOpen] = useState(false);
  const periodBoxRef = useRef<HTMLDivElement>(null);
  // 자동 저장 기준선: 마지막으로 서버와 일치한 (식별자, 직렬화 데이터).
  const savedRef = useRef<{ id: string; data: string }>({ id: "", data: "" });
  // ---- Path A 라이브 협업 편집 ----
  // 세션별 편집자 id(내가 보낸 라이브 이벤트 에코를 무시), 마지막으로 원격에서
  // 적용한 {plan,retro} 직렬화(원격 적용분을 되쏘는 echo 루프 차단), 그리고 SSE
  // 콜백(마운트 1회)에서 최신 상태로 라이브 이벤트를 적용하기 위한 ref 콜백.
  const editorIdRef = useRef<string>(newId());
  // 방금 원격에서 적용한 plan/retro 스냅샷(직렬화). 수신자는 현재 내용이 이 스냅샷과
  // "정확히 같을 때만" 저장/되쏘기를 건너뛴다(그 내용은 송신자가 저장). 내가 한 글자라도
  // 더 치면(스냅샷과 diverge하면) 곧바로 저장·중계된다. (구현: 예전엔 700ms 시간창으로
  // 막았는데, 그 창 안의 내 입력을 통째로 삼켜 유실시켰다 — content 비교로 교체.)
  const remoteLiveRef = useRef<{ id: string; plan: string; retro: string } | null>(null);
  const applyLiveRef = useRef<(ev: LiveEditEvent) => void>(() => {});

  // 주차/월 드롭다운: 바깥 클릭 시 닫는다(오버레이 대신 document 클릭 감지).
  useEffect(() => {
    if (!periodOpen) return;
    const onDocDown = (ev: MouseEvent) => {
      if (periodBoxRef.current && !periodBoxRef.current.contains(ev.target as Node)) {
        setPeriodOpen(false);
      }
    };
    document.addEventListener("pointerdown", onDocDown);
    return () => document.removeEventListener("pointerdown", onDocDown);
  }, [periodOpen]);

  const loadPeriods = useCallback(() => {
    if (!projectId) {
      setPeriods([]);
      return;
    }
    const call =
      mode === "weekly"
        ? api.listWeeklyPeriods(projectId)
        : api.listMonthlyPeriods(projectId);
    call.then(setPeriods).catch(() => setPeriods([]));
  }, [projectId, mode]);

  // load projects
  useEffect(() => {
    let alive = true;
    api
      .listDashboardProjects()
      .then((ps) => {
        if (!alive) return;
        setProjects(ps);
        setExpandedAllProjectIds((current) => reconcileExpandedProjects(current, ps));
        if (ps.length === 0) setLoading(false);
        // 기본은 "전체"(projectId = "")로 둔다.
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "프로젝트 불러오기 실패");
      });
    return () => {
      alive = false;
    };
  }, []);

  // ---- 실시간 동기화 (SSE 즉시 반영) ----
  // 협업자가 저장하는 즉시 서버가 SSE로 변경을 푸시하고, 그 자리에서 재조회한다
  // (폴링 지연 없음). 내가 편집 중(미저장 변경)일 때는 덮어쓰지 않도록, 현재
  // 데이터가 마지막 저장 기준선과 같을 때(유휴)만 재조회한다. 편집 중에 들어온
  // 변경은 pendingRef에 표시해 두고, 저장으로 유휴가 되면 즉시 따라잡는다.
  // SSE가 끊긴 환경을 대비해 느린 백업 폴링도 둔다.
  const [reloadTick, setReloadTick] = useState(0);
  // KPI 회고 카드 전용 재조회 신호(SSE kind:"kpi") — plan/retro 상태와 완전 분리라
  // 편집 중 덮어쓰기/유휴 로직과 상호작용하지 않는다.
  const [kpiTick, setKpiTick] = useState(0);
  const idleRef = useRef(false);
  const pendingRef = useRef(false);
  // 마지막으로 화면에 적용한 (프로젝트|모드|기간) 컨텍스트. 컨텍스트 전환이 아닌
  // 백그라운드 새로고침(reloadTick/폴링)이 진행 중 편집을 덮어쓰지 않도록,
  // 새로고침은 사용자가 유휴(저장됨)일 때만 서버 값을 적용하는 데 쓴다.
  const ctxRef = useRef("");
  const modeRef = useRef<Mode>(mode);
  useEffect(() => {
    modeRef.current = mode;
  }, [mode]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const curKey = mode === "weekly" ? ymd(sundayOf(ref)) : ymd(addMonths(ref, 0));
    const id = idOf(isAll ? "" : projectId, mode, curKey);
    const loadedProjects = isAll
      ? projects.filter((p) => allEntries[p.project_id])
      : projects;
    const data = isAll
      ? serAll(allEntries, loadedProjects)
      : serSingle(plan, retro, prevEntry);
    const idle = savedRef.current.id === id && savedRef.current.data === data;
    idleRef.current = idle;
    // 편집 중에 밀린 변경이 있고, 방금 저장으로 유휴가 됐다면 즉시 따라잡는다.
    if (idle && pendingRef.current) {
      pendingRef.current = false;
      setReloadTick((t) => t + 1);
    }
  });
  // SSE: 저장 즉시 변경 핑을 받아 유휴면 바로 재조회, 편집 중이면 pending 표시.
  useEffect(() => {
    const onChange = (kind: string) => {
      if (kind !== modeRef.current) return; // weekly/monthly 현재 보기만
      if (idleRef.current) setReloadTick((t) => t + 1);
      else pendingRef.current = true;
    };
    let es: EventSource | null = null;
    let backoff: ReturnType<typeof setTimeout> | null = null;
    const connect = () => {
      try {
        // 세션 쿠키로 인증. same-origin(공개 호스트)은 자동, cross-origin
        // session mode를 위해 withCredentials를 켠다.
        es = new EventSource(`${API_BASE}/dashboard/stream`, { withCredentials: true });
      } catch {
        return;
      }
      es.onmessage = (e) => {
        try {
          const ev = JSON.parse(e.data);
          // 라이브 협업 편집: 재조회 없이 그 자리에서 적용(협업자 글자 즉시 반영).
          if (ev?.type === "live_edit") {
            applyLiveRef.current(ev as LiveEditEvent);
            return;
          }
          // KPI 신뢰도/채점 핑: 회고 카드만 재조회(plan/retro 상태 무접촉).
          if (ev?.kind === "kpi") {
            setKpiTick((t) => t + 1);
            return;
          }
          // 저장 핑: 유휴면 재조회로 권위 상태 reconcile.
          if (ev && typeof ev.kind === "string") onChange(ev.kind);
        } catch {
          /* keepalive 등 비-JSON 무시 */
        }
      };
      es.onerror = () => {
        es?.close();
        es = null;
        // 자동 재연결(브라우저 기본)이 막히는 경우를 대비한 수동 백오프.
        if (!backoff) backoff = setTimeout(() => {
          backoff = null;
          connect();
        }, 3000);
      };
    };
    connect();
    return () => {
      if (backoff) clearTimeout(backoff);
      es?.close();
    };
  }, []);
  // 백업 폴링(SSE 두절 대비, 느리게).
  useEffect(() => {
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      if (loading || projects.length === 0) return;
      if (idleRef.current) setReloadTick((t) => t + 1);
    }, BACKUP_REFRESH_MS);
    return () => clearInterval(iv);
  }, [loading, projects.length]);

  useEffect(() => {
    let alive = true;
    const curKey = mode === "weekly" ? ymd(sundayOf(ref)) : ymd(addMonths(ref, 0));
    const prevKey =
      mode === "weekly" ? ymd(addDays(sundayOf(ref), -7)) : ymd(addMonths(ref, -1));
    const getAt = (pid: string, key: string) =>
      mode === "weekly" ? api.getWeekly(pid, key) : api.getMonthly(pid, key);

    // 컨텍스트(프로젝트/모드/기간) 전환이면 서버 값을 그대로 적용한다.
    // 같은 컨텍스트의 백그라운드 새로고침은 fetch가 도는 동안 사용자가 입력했을
    // 수 있으므로, 적용 시점에 유휴가 아니면(진행 중 편집) 덮어쓰지 않는다.
    //
    // 전체(isAll) 보기는 projects 목록 자체도 컨텍스트에 포함한다. 마운트 직후
    // projects가 아직 비어 있을 때 이 effect가 먼저 한 번 돌면 Promise.all([])이
    // 즉시 resolve돼 ctxRef/savedRef를 '빈 projects' 기준으로 claim해 버린다.
    // 그 뒤 실제 projects가 도착해 effect가 재실행되면 (a) 같은 ctxId라 컨텍스트
    // 전환이 아니고 (b) savedRef 기준선이 빈 목록이라 idle도 아니어서, 아래 적용
    // 가드가 첫 로드 데이터를 버린다(빈 화면). projects fingerprint를 ctxId에 넣어
    // 빈→채워짐 전이를 컨텍스트 전환으로 인식하게 한다.
    const expandedProjects = isAll
      ? projects.filter((p) => expandedAllProjectIds.has(p.project_id))
      : [];
    const projectsKey = isAll ? expandedProjects.map((p) => p.project_id).join(",") : "";
    const ctxId = `${isAll ? `*:${projectsKey}` : projectId}|${mode}|${curKey}`;
    const isContextChange = ctxRef.current !== ctxId;
    ctxRef.current = ctxId;

    if (isAll) {
      if (expandedProjects.length === 0) {
        return () => {
          alive = false;
        };
      }
      // 프로젝트마다 [이번, 지난] 두 기간을 불러온다.
      Promise.all(
        expandedProjects.map((p) =>
          Promise.all([getAt(p.project_id, curKey), getAt(p.project_id, prevKey)]),
        ),
      )
        .then((pairs) => {
          if (!alive) return;
          // 같은 컨텍스트 새로고침은 진행 중 편집을 덮어쓰지 않는다.
          if (!isContextChange && !idleRef.current) return;
          setAllEntries((current) => {
            const map: Record<string, AllEntry> = { ...current };
            expandedProjects.forEach((p, i) => {
              const [cur, prev] = pairs[i];
              map[p.project_id] = {
                plan: cur?.plan_items ?? [],
                retro: cur?.retro_items ?? [],
                prevPlan: prev?.plan_items ?? [],
                prevRetro: prev?.retro_items ?? [],
                updatedAt: cur?.updated_at ?? null,
                prevUpdatedAt: prev?.updated_at ?? null,
              };
            });
            // 방금 불러온 상태를 자동 저장 기준선으로 잡는다(불러오자마자 저장 방지).
            savedRef.current = { id: idOf("", mode, curKey), data: serAll(map, expandedProjects) };
            return map;
          });
          setSavedAt(null);
          setError(null);
          remoteLiveRef.current = null;
        })
        .catch((e) => {
          if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
        })
        .finally(() => {
          if (alive) setLoading(false);
        });
    } else {
      Promise.all([getAt(projectId, curKey), getAt(projectId, prevKey)])
        .then(([cur, prev]) => {
          if (!alive) return;
          // 같은 컨텍스트 새로고침은 진행 중 편집을 덮어쓰지 않는다.
          if (!isContextChange && !idleRef.current) return;
          const curPlan = cur?.plan_items ?? [];
          const curRetro = cur?.retro_items ?? [];
          const prevE: Entry = {
            plan: prev?.plan_items ?? [],
            retro: prev?.retro_items ?? [],
            updatedAt: prev?.updated_at ?? null,
          };
          setPlan(curPlan);
          setRetro(curRetro);
          setCurUpdatedAt(cur?.updated_at ?? null);
          setPrevEntry(prevE);
          setSavedAt(null);
          setError(null);
          // 방금 불러온 상태를 자동 저장 기준선으로 잡는다(불러오자마자 저장 방지).
          savedRef.current = {
            id: idOf(projectId, mode, curKey),
            data: serSingle(curPlan, curRetro, prevE),
          };
          remoteLiveRef.current = null;
        })
        .catch((e) => {
          if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
        })
        .finally(() => {
          if (alive) setLoading(false);
        });
    }
    return () => {
      alive = false;
    };
  }, [projectId, mode, ref, projects, expandedAllProjectIds, isAll, reloadTick]);

  useEffect(() => {
    let alive = true;
    const call = projectId
      ? mode === "weekly"
        ? api.listWeeklyPeriods(projectId)
        : api.listMonthlyPeriods(projectId)
      : Promise.resolve([] as PeriodSummary[]);
    call
      .then((ps) => {
        if (alive) setPeriods(ps);
      })
      .catch(() => {
        if (alive) setPeriods([]);
      });
    return () => {
      alive = false;
    };
  }, [projectId, mode]);

  const curKey = mode === "weekly" ? ymd(sundayOf(ref)) : ymd(addMonths(ref, 0));
  const prevKey =
    mode === "weekly" ? ymd(addDays(sundayOf(ref), -7)) : ymd(addMonths(ref, -1));
  const isRemoteLiveOnly = (id: string, planItems: PlanItem[], retroItems: PlanItem[]) => {
    const remote = remoteLiveRef.current;
    return (
      remote?.id === id &&
      remote.plan === JSON.stringify(planItems) &&
      remote.retro === JSON.stringify(retroItems)
    );
  };

  // 라이브 협업 적용기를 매 렌더 후 최신 상태로 갱신(ref → SSE 콜백이 마운트 1회
  // 클로저에서도 최신 값을 본다). 가드: 내 에코·다른 모드/기간 무시. 내가 편집
  // 중(미저장)일 땐 내 입력을 덮지 않도록 건너뛴다(유휴일 때만 협업자 글자 즉시 반영).
  useEffect(() => {
    applyLiveRef.current = (ev: LiveEditEvent) => {
      if (ev.editor_id === editorIdRef.current) return;
      if (ev.kind !== modeRef.current) return;
      if (ev.period_key !== curKey) return;
      // v1: 단일-프로젝트 보기에서, 내가 편집 중이 아닐 때만 적용(내 입력 보호).
      if (isAll || ev.project_id !== projectId) return;
      if (!idleRef.current) return;
      // 방금 적용한 원격 내용의 스냅샷을 남긴다. 아래 저장/중계 effect는 현재 내용이
      // 이 스냅샷과 "그대로 같을 때만" 건너뛴다(그 내용은 송신자가 저장). 내가 한 글자라도
      // 더 치면 스냅샷과 달라져 곧바로 저장·중계된다(구 700ms 시간창의 입력-삼킴 유실 제거).
      remoteLiveRef.current = {
        id: idOf(ev.project_id, ev.kind, ev.period_key),
        plan: JSON.stringify(ev.plan),
        retro: JSON.stringify(ev.retro),
      };
      setPlan(ev.plan);
      setRetro(ev.retro);
    };
  });

  // 입력-중 plan/retro를 120ms 디바운스로 라이브 중계(저장 디바운스 500ms와 별개).
  // 단일-프로젝트 보기에서만 송신(payload 프로젝트가 명확). 방금 받은 원격 내용 그대로면
  // (내 편집 없음) 되쏘지 않는다(에코 루프 차단) — 내가 diverge하면 곧바로 송신.
  useEffect(() => {
    if (isAll || !projectId) return;
    if (isRemoteLiveOnly(idOf(projectId, mode, curKey), plan, retro)) return;
    const t = setTimeout(() => {
      void api
        .liveEditPlan({
          kind: mode,
          project_id: projectId,
          period_key: curKey,
          plan,
          retro,
          editor_id: editorIdRef.current,
        })
        .catch(() => undefined);
    }, 120);
    return () => clearTimeout(t);
  }, [plan, retro, projectId, mode, isAll, curKey]);

  const saveEntry = (pid: string, key: string, e: Entry) =>
    mode === "weekly"
      ? api.saveWeekly({
          project_id: pid,
          week_start: key,
          plan_items: e.plan,
          retro_items: e.retro,
          base_updated_at: e.updatedAt ?? null,
        })
      : api.saveMonthly({
          project_id: pid,
          month: key,
          plan_items: e.plan,
          retro_items: e.retro,
          base_updated_at: e.updatedAt ?? null,
        });

  // 서버가 빈 덮어쓰기를 막은 409: 하드 에러 대신 최신 상태로 새로고침하고 안내만 남긴다.
  // (커밋 3165861의 클라이언트 빈값 가드는 1차 방어로 유지된다.)
  const isConflict = (e: unknown) =>
    typeof e === "object" &&
    e !== null &&
    "status" in e &&
    (e as { status?: number }).status === 409;
  const refreshAfterConflict = () => {
    setRef((d) => new Date(d));
    setSavedAt(
      `${new Date().toLocaleTimeString("ko-KR")} · 빈 덮어쓰기를 건너뛰고 최신으로 새로고침`,
    );
  };

  // 자동 저장: 항목 추가/수정/완료체크/세부 변경 후 잠깐 멈추면 저장 버튼 없이 저장한다.
  useEffect(() => {
    if (loading || projects.length === 0) return;
    const loadedProjects = isAll
      ? projects.filter((p) => allEntries[p.project_id])
      : projects;
    if (isAll && loadedProjects.length === 0) return;
    const id = idOf(isAll ? "" : projectId, mode, curKey);
    const data = isAll
      ? serAll(allEntries, loadedProjects)
      : serSingle(plan, retro, prevEntry);
    // 원격 라이브 편집을 막 적용한 직후엔 그 내용을 저장하지 않는다(송신자가 저장).
    // 현재(정규화된) 상태를 기준선으로 채택해 재저장 루프를 막고, 이후 send-ping
    // refetch가 권위 상태로 reconcile한다.
    if (!isAll && isRemoteLiveOnly(id, plan, retro)) {
      savedRef.current = { id, data };
      return;
    }
    // 기간/프로젝트가 막 바뀐 경우는 기준선만 잡고 저장하지 않는다.
    if (savedRef.current.id !== id) {
      savedRef.current = { id, data };
      return;
    }
    if (savedRef.current.data === data) return; // 변경 없음
    const t = setTimeout(async () => {
      try {
        if (isAll) {
          for (const p of loadedProjects) {
            const e = allEntries[p.project_id];
            if (!e) continue;
            await saveEntry(p.project_id, curKey, {
              plan: e.plan,
              retro: e.retro,
              updatedAt: e.updatedAt ?? null,
            });
            // 지난 주는 client state가 빈 경우 덮어쓰지 않는다(로드 레이스로 인한 silent wipe 방지).
            if (e.prevPlan.length || e.prevRetro.length) {
              await saveEntry(p.project_id, prevKey, {
                plan: e.prevPlan,
                retro: e.prevRetro,
                updatedAt: e.prevUpdatedAt ?? null,
              });
            }
          }
        } else if (projectId) {
          await saveEntry(projectId, curKey, { plan, retro, updatedAt: curUpdatedAt });
          // 지난 주는 client state가 빈 경우 덮어쓰지 않는다(로드 레이스로 인한 silent wipe 방지).
          if (prevEntry.plan.length || prevEntry.retro.length) {
            await saveEntry(projectId, prevKey, prevEntry);
          }
        }
        savedRef.current = { id, data };
        remoteLiveRef.current = null;
        setSavedAt(new Date().toLocaleTimeString("ko-KR"));
        loadPeriods();
      } catch (err) {
        if (isConflict(err)) {
          refreshAfterConflict();
        } else {
          setError(err instanceof Error ? err.message : "저장 실패");
        }
      }
    }, 500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan, retro, prevEntry, allEntries, isAll, projectId, mode, curKey, prevKey, projects, loading]);

  // 페이지를 떠날 때(언마운트) 디바운스(500ms) 대기 중인 변경을 즉시 저장한다.
  // 세부사항 작성 후 저장이 트리거되기 전에 다른 탭으로 나가도 유실되지 않게 한다.
  // (in-flight fetch는 언마운트 후에도 계속되므로 fire-and-forget로 충분하다.)
  const flushRef = useRef<() => void>(() => {});
  useEffect(() => {
    flushRef.current = () => {
      if (loading || projects.length === 0) return;
      const loadedProjects = isAll
        ? projects.filter((p) => allEntries[p.project_id])
        : projects;
      if (isAll && loadedProjects.length === 0) return;
      const id = idOf(isAll ? "" : projectId, mode, curKey);
      const data = isAll ? serAll(allEntries, loadedProjects) : serSingle(plan, retro, prevEntry);
      if (savedRef.current.id !== id || savedRef.current.data === data) return;
      if (!isAll && isRemoteLiveOnly(id, plan, retro)) {
        savedRef.current = { id, data };
        return;
      }
      savedRef.current = { id, data };
      void (async () => {
        try {
          if (isAll) {
            for (const p of loadedProjects) {
              const e = allEntries[p.project_id];
              if (!e) continue;
              await saveEntry(p.project_id, curKey, {
                plan: e.plan,
                retro: e.retro,
                updatedAt: e.updatedAt ?? null,
              });
              if (e.prevPlan.length || e.prevRetro.length) {
                await saveEntry(p.project_id, prevKey, {
                  plan: e.prevPlan,
                  retro: e.prevRetro,
                  updatedAt: e.prevUpdatedAt ?? null,
                });
              }
            }
          } else if (projectId) {
            await saveEntry(projectId, curKey, { plan, retro, updatedAt: curUpdatedAt });
            if (prevEntry.plan.length || prevEntry.retro.length) {
              await saveEntry(projectId, prevKey, prevEntry);
            }
          }
        } catch {
          /* 언마운트 중 저장 — 에러는 무시 */
        }
      })();
    };
  });
  useEffect(() => () => flushRef.current(), []);

  // 프로젝트/모드/기간을 전환할 때도 대기 중인 변경을 즉시 저장한다.
  // 세부(상세)는 500ms 디바운스 auto-save에만 실려 저장되는데, 전환이 그 타이머를
  // 취소하고 새 기준선만 잡아 방금 쓴 세부가 통째로 유실됐다(언마운트 flush는 페이지를
  // 아예 떠날 때만 동작해 이 in-page 전환은 못 잡았다). effect cleanup은 flushRef가
  // 다음 렌더 클로저로 교체되기 전에 실행되므로, flushRef.current()는 전환 직전(이전)
  // 컨텍스트의 상태를 그 컨텍스트에 저장한다.
  const flushCtxKey = idOf(isAll ? "" : projectId, mode, curKey);
  useEffect(() => () => flushRef.current(), [flushCtxKey]);

  async function save() {
    if (!projectId) return;
    const id = idOf(projectId, mode, curKey);
    const data = serSingle(plan, retro, prevEntry);
    if (isRemoteLiveOnly(id, plan, retro)) {
      savedRef.current = { id, data };
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await saveEntry(projectId, curKey, { plan, retro, updatedAt: curUpdatedAt });
      // '계획 점검'에서 지난 기간 계획의 달성 점수를 바꿨을 수 있으니 지난 기간도 저장.
      // 단, client state가 빈 경우는 덮어쓰지 않는다(로드 레이스로 인한 silent wipe 방지).
      if (prevEntry.plan.length || prevEntry.retro.length) {
        await saveEntry(projectId, prevKey, prevEntry);
      }
      savedRef.current = {
        id,
        data,
      };
      remoteLiveRef.current = null;
      setSavedAt(new Date().toLocaleTimeString("ko-KR"));
      loadPeriods();
    } catch (e) {
      if (isConflict(e)) {
        refreshAfterConflict();
      } else {
        setError(e instanceof Error ? e.message : "저장 실패");
      }
    } finally {
      setBusy(false);
    }
  }

  async function saveAll() {
    const loadedProjects = projects.filter((p) => allEntries[p.project_id]);
    if (loadedProjects.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      for (const p of loadedProjects) {
        const e = allEntries[p.project_id];
        if (!e) continue;
        await saveEntry(p.project_id, curKey, {
          plan: e.plan,
          retro: e.retro,
          updatedAt: e.updatedAt ?? null,
        });
        // 지난 주는 client state가 빈 경우 덮어쓰지 않는다(로드 레이스로 인한 silent wipe 방지).
        if (e.prevPlan.length || e.prevRetro.length) {
          await saveEntry(p.project_id, prevKey, {
            plan: e.prevPlan,
            retro: e.prevRetro,
            updatedAt: e.prevUpdatedAt ?? null,
          });
        }
      }
      savedRef.current = { id: idOf("", mode, curKey), data: serAll(allEntries, loadedProjects) };
      remoteLiveRef.current = null;
      setSavedAt(new Date().toLocaleTimeString("ko-KR"));
    } catch (e) {
      if (isConflict(e)) {
        refreshAfterConflict();
      } else {
        setError(e instanceof Error ? e.message : "저장 실패");
      }
    } finally {
      setBusy(false);
    }
  }

  // Enter 저장: 한 프로젝트만 저장(전체 보기에서 항목마다 전부 저장하지 않도록)
  async function commitProject(pid: string) {
    const e = allEntries[pid];
    if (!e) return;
    setError(null);
    try {
      await saveEntry(pid, curKey, {
        plan: e.plan,
        retro: e.retro,
        updatedAt: e.updatedAt ?? null,
      });
      // 지난 주는 client state가 빈 경우 덮어쓰지 않는다(로드 레이스로 인한 silent wipe 방지).
      if (e.prevPlan.length || e.prevRetro.length) {
        await saveEntry(pid, prevKey, {
          plan: e.prevPlan,
          retro: e.prevRetro,
          updatedAt: e.prevUpdatedAt ?? null,
        });
      }
      setSavedAt(new Date().toLocaleTimeString("ko-KR"));
    } catch (err) {
      if (isConflict(err)) {
        refreshAfterConflict();
      } else {
        setError(err instanceof Error ? err.message : "저장 실패");
      }
    }
  }

  async function addProject() {
    const name = newProject.trim();
    if (!name) return;
    try {
      const p = await api.createDashboardProject({ name, color: null, sort_order: 0, active: true });
      setNewProject("");
      const ps = await api.listDashboardProjects();
      setProjects(ps);
      setExpandedAllProjectIds((current) => reconcileExpandedProjects(current, ps));
      setProjectId(p.project_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "프로젝트 추가 실패");
    }
  }

  async function deleteProject() {
    if (!projectId) return;
    const cur = projects.find((p) => p.project_id === projectId);
    if (
      !window.confirm(
        `'${cur?.name ?? "프로젝트"}'를 삭제하시겠습니까?\n이 프로젝트의 모든 주간/월간 계획·회고도 함께 삭제됩니다.`,
      )
    )
      return;
    try {
      await api.deleteDashboardProject(projectId);
      const ps = await api.listDashboardProjects();
      setProjects(ps);
      setExpandedAllProjectIds((current) => reconcileExpandedProjects(current, ps));
      setProjectId(ps.length ? ps[0].project_id : "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "프로젝트 삭제 실패");
    }
  }

  // item mutators
  const addPlan = (group?: string) =>
    setPlan((p) => [...p, { id: newId(), text: "", done: false, group: group ?? null }]);
  const addRetro = (group?: string) =>
    setRetro((r) => [...r, { id: newId(), text: "", done: false, group: group ?? null }]);
  const setPlanText = (id: string, text: string) =>
    setPlan((p) => p.map((it) => (it.id === id ? { ...it, text } : it)));
  const togglePlanDone = (id: string) =>
    setPlan((p) => p.map((it) => (it.id === id ? { ...it, done: !it.done } : it)));
  const removePlan = (id: string) => setPlan((p) => p.filter((it) => it.id !== id));
  const setRetroText = (id: string, text: string) =>
    setRetro((r) => r.map((it) => (it.id === id ? { ...it, text } : it)));
  const removeRetro = (id: string) => setRetro((r) => r.filter((it) => it.id !== id));
  const setPlanDetail = (id: string, detail: string | null) =>
    setPlan((p) => p.map((it) => (it.id === id ? { ...it, detail } : it)));
  const setRetroDetail = (id: string, detail: string | null) =>
    setRetro((r) => r.map((it) => (it.id === id ? { ...it, detail } : it)));
  // '계획 점검' = 지난 기간 계획의 달성 점수 기록 (지난 기간 row에 저장됨)
  const setPrevDetail = (id: string, detail: string | null) =>
    setPrevEntry((e) => ({
      ...e,
      plan: e.plan.map((it) => (it.id === id ? { ...it, detail } : it)),
    }));
  const setPlanAssignee = (id: string, a: string | null) =>
    setPlan((p) => p.map((it) => (it.id === id ? { ...it, assignee_member_id: a } : it)));
  const setRetroAssignee = (id: string, a: string | null) =>
    setRetro((r) => r.map((it) => (it.id === id ? { ...it, assignee_member_id: a } : it)));
  const setPrevAssignee = (id: string, a: string | null) =>
    setPrevEntry((e) => ({
      ...e,
      plan: e.plan.map((it) => (it.id === id ? { ...it, assignee_member_id: a } : it)),
    }));
  const setPlanKpi = (id: string, k: string | null) =>
    setPlan((p) => p.map((it) => (it.id === id ? { ...it, kpi_id: k } : it)));
  const setPrevKpi = (id: string, k: string | null) =>
    setPrevEntry((e) => ({
      ...e,
      plan: e.plan.map((it) => (it.id === id ? { ...it, kpi_id: k } : it)),
    }));
  // 계획 점검 달성 점수(0~10) — done 토글과 동일하게 지난 기간 row에 저장된다.
  // score=0 유효(지움은 null) — 기존 prev-기간 autosave 경로에 그대로 탑승.
  const setPrevScore = (id: string, score: number | null) =>
    setPrevEntry((e) => ({
      ...e,
      plan: e.plan.map((it) => (it.id === id ? { ...it, score } : it)),
    }));

  const singleHandlers: PanelHandlers = {
    onAddPlan: addPlan,
    onPlanText: setPlanText,
    onTogglePlanDone: togglePlanDone,
    onRemovePlan: removePlan,
    onPlanDetail: setPlanDetail,
    onPlanAssignee: setPlanAssignee,
    onPlanKpi: setPlanKpi,
    onAddRetro: addRetro,
    onRetroText: setRetroText,
    onRemoveRetro: removeRetro,
    onRetroDetail: setRetroDetail,
    onRetroAssignee: setRetroAssignee,
    onPrevDetail: setPrevDetail,
    onPrevAssignee: setPrevAssignee,
    onPrevKpi: setPrevKpi,
    onPrevScore: setPrevScore,
    onCommit: save,
  };

  // 전체 보기에서 프로젝트별 항목 변경
  function updateAll(pid: string, fn: (e: AllEntry) => AllEntry) {
    setAllEntries((s) => ({
      ...s,
      [pid]: fn(s[pid] ?? { plan: [], retro: [], prevPlan: [], prevRetro: [] }),
    }));
  }
  const allHandlers = (pid: string): PanelHandlers => ({
    onAddPlan: (group?: string) =>
      updateAll(pid, (e) => ({
        ...e,
        plan: [...e.plan, { id: newId(), text: "", done: false, group: group ?? null }],
      })),
    onPlanText: (id, text) =>
      updateAll(pid, (e) => ({ ...e, plan: e.plan.map((it) => (it.id === id ? { ...it, text } : it)) })),
    onTogglePlanDone: (id) =>
      updateAll(pid, (e) => ({
        ...e,
        plan: e.plan.map((it) => (it.id === id ? { ...it, done: !it.done } : it)),
      })),
    onRemovePlan: (id) =>
      updateAll(pid, (e) => ({ ...e, plan: e.plan.filter((it) => it.id !== id) })),
    onPlanDetail: (id, detail) =>
      updateAll(pid, (e) => ({
        ...e,
        plan: e.plan.map((it) => (it.id === id ? { ...it, detail } : it)),
      })),
    onAddRetro: (group?: string) =>
      updateAll(pid, (e) => ({
        ...e,
        retro: [...e.retro, { id: newId(), text: "", done: false, group: group ?? null }],
      })),
    onRetroText: (id, text) =>
      updateAll(pid, (e) => ({ ...e, retro: e.retro.map((it) => (it.id === id ? { ...it, text } : it)) })),
    onRemoveRetro: (id) =>
      updateAll(pid, (e) => ({ ...e, retro: e.retro.filter((it) => it.id !== id) })),
    onRetroDetail: (id, detail) =>
      updateAll(pid, (e) => ({
        ...e,
        retro: e.retro.map((it) => (it.id === id ? { ...it, detail } : it)),
      })),
    onPrevDetail: (id, detail) =>
      updateAll(pid, (e) => ({
        ...e,
        prevPlan: e.prevPlan.map((it) => (it.id === id ? { ...it, detail } : it)),
      })),
    onPlanAssignee: (id, a) =>
      updateAll(pid, (e) => ({
        ...e,
        plan: e.plan.map((it) => (it.id === id ? { ...it, assignee_member_id: a } : it)),
      })),
    onRetroAssignee: (id, a) =>
      updateAll(pid, (e) => ({
        ...e,
        retro: e.retro.map((it) => (it.id === id ? { ...it, assignee_member_id: a } : it)),
      })),
    onPrevAssignee: (id, a) =>
      updateAll(pid, (e) => ({
        ...e,
        prevPlan: e.prevPlan.map((it) => (it.id === id ? { ...it, assignee_member_id: a } : it)),
      })),
    onPlanKpi: (id, k) =>
      updateAll(pid, (e) => ({
        ...e,
        plan: e.plan.map((it) => (it.id === id ? { ...it, kpi_id: k } : it)),
      })),
    onPrevKpi: (id, k) =>
      updateAll(pid, (e) => ({
        ...e,
        prevPlan: e.prevPlan.map((it) => (it.id === id ? { ...it, kpi_id: k } : it)),
      })),
    onPrevScore: (id, score) =>
      updateAll(pid, (e) => ({
        ...e,
        prevPlan: e.prevPlan.map((it) => (it.id === id ? { ...it, score } : it)),
      })),
    onCommit: () => commitProject(pid),
  });

  const toggleAllProject = (pid: string) => {
    setExpandedAllProjectIds((current) => {
      const next = new Set(current);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });
  };

  const stepLabel =
    mode === "weekly" ? weekLabel(sundayOf(ref)) : monthLabel(addMonths(ref, 0));
  const goPrev = () => setRef((d) => (mode === "weekly" ? addDays(d, -7) : addMonths(d, -1)));
  const goNext = () => setRef((d) => (mode === "weekly" ? addDays(d, 7) : addMonths(d, 1)));
  const goNow = () => setRef(new Date());
  const currentPeriodKey =
    mode === "weekly" ? ymd(sundayOf(ref)) : ymd(addMonths(ref, 0));

  // 드롭다운에 빈 주차/월도 포함해 아무 기간이나 선택해 채울 수 있게 한다.
  const savedMap = new Map(periods.map((p) => [p.period, p]));
  const base = new Date();
  const pickerKeys: string[] = [];
  if (mode === "weekly") {
    for (let i = 3; i >= -8; i--) pickerKeys.push(ymd(addDays(sundayOf(base), i * 7)));
  } else {
    for (let i = 2; i >= -6; i--) pickerKeys.push(ymd(addMonths(base, i)));
  }
  periods.forEach((p) => {
    if (!pickerKeys.includes(p.period)) pickerKeys.push(p.period);
  });
  const pickerPeriods = pickerKeys
    .sort((a, b) => (a < b ? 1 : -1))
    .map((key) => ({ key, saved: savedMap.get(key) }));

  // 나 외에 지금 계획·회고를 보고 있는 사람들(노션식 프레즌스)
  const others = presences.filter((p) => p.user_id !== DEMO_USER_ID);
  const currentProject = projects.find((p) => p.project_id === projectId);
  const projectName = currentProject?.name;
  const projectColor = currentProject?.color;

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <ChannelHeader
        glyph={isAll || !projectColor ? "#" : { dot: projectColor }}
        title={isAll ? "전체" : projectName || "프로젝트"}
        subtitle={`${mode === "weekly" ? "주간" : "월간"} 계획·회고 · ${stepLabel}`}
        people={others.map((p) => {
          const proj =
            p.project_id && projects.find((x) => x.project_id === p.project_id)?.name;
          return {
            name: proj ? `${p.name} · ${proj}` : p.name,
            color: p.color || undefined,
            presence: "editing" as const,
          };
        })}
        right={
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1">
              {(["weekly", "monthly"] as Mode[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className="h-8 rounded-[6px] px-3 text-[var(--text-body-sm)] transition-colors"
                  style={{
                    background: mode === m ? "var(--color-accent-soft)" : "var(--color-card)",
                    border: `1px solid ${mode === m ? "var(--color-accent-border)" : "var(--color-toolbar-border)"}`,
                    fontWeight: mode === m ? 700 : 500,
                    color: mode === m ? "var(--color-accent)" : "var(--color-text-strong)",
                  }}
                >
                  {m === "weekly" ? "주간" : "월간"}
                </button>
              ))}
            </div>
            <span className="hidden text-[var(--text-meta)] sm:inline" style={labelStyle()}>
              {busy ? "저장 중…" : savedAt ? `자동 저장됨 ${savedAt}` : "자동 저장"}
            </span>
            <ToolbarButton
              tone="accent"
              onClick={isAll ? saveAll : save}
              disabled={busy || projects.length === 0}
            >
              {isAll ? "전체 저장" : "저장"}
            </ToolbarButton>
          </div>
        }
      />

      {/* 슬림 서브툴바 — 프로젝트 스위처(채널) + 기간 네비 */}
      <div className="mb-4 mt-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="whitespace-nowrap text-[var(--text-meta)] font-semibold" style={labelStyle()}>
            프로젝트
          </span>
          {projects.length ? (
            <>
              <Select
                value={projectId}
                options={[
                  { value: "", label: "전체" },
                  ...projects.map((p) => ({ value: p.project_id, label: p.name })),
                ]}
                onChange={(e) => setProjectId(e.target.value)}
                className="max-w-[220px]"
              />
              {!isAll ? (
                <ToolbarButton
                  onClick={deleteProject}
                  title="현재 프로젝트 삭제"
                  className="whitespace-nowrap"
                >
                  삭제
                </ToolbarButton>
              ) : null}
            </>
          ) : (
            <span className="text-[var(--text-body-sm)]" style={labelStyle()}>
              프로젝트가 없습니다 →
            </span>
          )}
          <input
            value={newProject}
            onChange={(e) => setNewProject(e.target.value)}
            placeholder="새 프로젝트"
            className="h-8 w-[120px] rounded-[5px] px-2 text-[var(--text-body-sm)]"
            style={{ border: "1px solid var(--color-divider)", background: "var(--color-card)" }}
            onKeyDown={(e) => e.key === "Enter" && addProject()}
          />
          <ToolbarButton onClick={addProject} className="whitespace-nowrap">+ 프로젝트</ToolbarButton>
        </div>

        <div className="flex items-center gap-1">
          <ToolbarButton onClick={goPrev}>‹ 이전</ToolbarButton>
          {/* 주차/월 드롭다운 — 클릭하면 전체 목록이 쫙 펼쳐져 선택 */}
          <div className="relative" ref={periodBoxRef}>
            <button
              onClick={() => setPeriodOpen((o) => !o)}
              className="flex h-8 items-center gap-1.5 rounded-[6px] px-2.5 text-[var(--text-body-sm)] font-semibold"
              style={{
                border: "1px solid var(--color-toolbar-border)",
                background: "var(--color-card)",
                color: "var(--color-text-strong)",
              }}
            >
              {stepLabel} <span style={labelStyle()}>▾</span>
            </button>
            {periodOpen ? (
                <div
                  className="absolute left-0 z-50 mt-1 max-h-[300px] w-[320px] overflow-y-auto rounded-[8px]"
                  style={{
                    background: "var(--color-card)",
                    border: "1px solid var(--color-divider)",
                    boxShadow: "var(--shadow-popover)",
                  }}
                >
                  <div
                    className="px-3 py-2 text-[var(--text-meta)] font-semibold"
                    style={{ color: "var(--color-text-muted)", borderBottom: "1px solid var(--color-divider)" }}
                  >
                    {mode === "weekly" ? "주차 선택" : "월 선택"} (작성된 {periods.length})
                  </div>
                  {pickerPeriods.map(({ key, saved }) => {
                    const active = key === currentPeriodKey;
                    const d = new Date(`${key}T00:00:00`);
                    return (
                      <button
                        key={key}
                        onClick={() => {
                          setRef(d);
                          setPeriodOpen(false);
                        }}
                        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-[var(--text-body-sm)]"
                        style={{
                          borderBottom: "1px solid var(--color-divider-soft)",
                          background: active ? "var(--color-sidebar-active)" : "transparent",
                          fontWeight: active ? 700 : 500,
                          color: saved ? "var(--color-text-strong)" : "var(--color-text-muted)",
                        }}
                      >
                        <span>{mode === "weekly" ? weekLabel(d) : monthLabel(d)}</span>
                        {saved ? (
                          <span className="flex shrink-0 gap-2 text-[var(--text-meta)]" style={labelStyle()}>
                            <span>계획 {saved.plan_count}·완료 {saved.plan_done}</span>
                            <span>회고 {saved.retro_count}</span>
                          </span>
                        ) : null}
                      </button>
                    );
                  })}
                </div>
            ) : null}
          </div>
          <ToolbarButton onClick={goNext}>다음 ›</ToolbarButton>
          <ToolbarButton onClick={goNow}>현재</ToolbarButton>
        </div>
      </div>

      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">{error}</Banner>
        </div>
      ) : null}

      {loading ? (
        <Card className="p-4">
          <Skeleton className="h-5 w-40" />
        </Card>
      ) : projects.length === 0 ? (
        <Card className="p-6 text-center text-[var(--text-body-sm)]">
          <span style={labelStyle()}>먼저 프로젝트를 추가하세요.</span>
        </Card>
      ) : isAll ? (
        // 전체: 펼친 프로젝트만 지연 로드한다. KPI 회고 카드는 회사 단위 리추얼이라
        // 프로젝트 패널마다 중복하지 않고 상단에 1개만 둔다(회사 공통 KPI 중복 방지).
        <div className="flex flex-col gap-4">
          <KpiRetroCard
            mode={mode}
            refKey={curKey}
            projectId={null}
            reloadSignal={kpiTick}
            framed
          />
          {projects.map((p) => {
            const expanded = expandedAllProjectIds.has(p.project_id);
            const e = allEntries[p.project_id];
            return (
              <Card key={p.project_id} className="p-3 sm:p-4">
                <button
                  type="button"
                  onClick={() => toggleAllProject(p.project_id)}
                  className={cx(
                    "flex min-h-11 w-full items-center justify-between gap-3 text-left",
                    expanded && "mb-3",
                  )}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <span
                      className="inline-block h-3 w-3 shrink-0 rounded-full"
                      style={{ background: p.color || "var(--channel-slate)" }}
                    />
                    <span
                      className="truncate text-[var(--text-body)] font-bold"
                      style={{ color: "var(--color-text-strong)" }}
                    >
                      {p.name}
                    </span>
                  </span>
                  <span
                    className="shrink-0 text-[var(--text-meta)] font-semibold"
                    style={labelStyle()}
                  >
                    {expanded ? "접기" : "펼치기"}
                  </span>
                </button>
                {expanded ? (
                  e ? (
                    <PlanRetroPanels
                      mode={mode}
                      plan={e.plan}
                      retro={e.retro}
                      planPrev={e.prevPlan}
                      handlers={allHandlers(p.project_id)}
                      groups={groupsFor(p.name)}
                      members={members}
                      kpiOptions={kpiOptionsFor(p.project_id)}
                    />
                  ) : (
                    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                      <div
                        className="rounded-[var(--radius-card)] p-3 sm:p-4"
                        style={{
                          border: "1px solid var(--color-divider)",
                          background: "var(--color-panel)",
                        }}
                      >
                        <Skeleton className="h-5 w-32" />
                      </div>
                      <div
                        className="rounded-[var(--radius-card)] p-3 sm:p-4"
                        style={{
                          border: "1px solid var(--color-divider)",
                          background: "var(--color-panel)",
                        }}
                      >
                        <Skeleton className="h-5 w-32" />
                      </div>
                    </div>
                  )
                ) : null}
              </Card>
            );
          })}
        </div>
      ) : (
        <PlanRetroPanels
          mode={mode}
          plan={plan}
          retro={retro}
          planPrev={prevEntry.plan}
          handlers={singleHandlers}
          groups={groupsFor(projects.find((p) => p.project_id === projectId)?.name ?? "")}
          members={members}
          kpiOptions={kpiOptionsFor(projectId)}
          retroCard={
            <KpiRetroCard
              mode={mode}
              refKey={curKey}
              projectId={projectId}
              reloadSignal={kpiTick}
            />
          }
        />
      )}
    </div>
  );
}

interface PanelHandlers {
  onAddPlan: (group?: string) => void;
  onPlanText: (id: string, text: string) => void;
  onTogglePlanDone: (id: string) => void;
  onRemovePlan: (id: string) => void;
  onPlanDetail: (id: string, detail: string | null) => void;
  onPlanAssignee: (id: string, assignee: string | null) => void;
  onAddRetro: (group?: string) => void;
  onRetroText: (id: string, text: string) => void;
  onRemoveRetro: (id: string) => void;
  onRetroDetail: (id: string, detail: string | null) => void;
  onRetroAssignee: (id: string, assignee: string | null) => void;
  onPrevDetail: (id: string, detail: string | null) => void;
  onPrevAssignee: (id: string, assignee: string | null) => void;
  onPlanKpi: (id: string, kpiId: string | null) => void;
  onPrevKpi: (id: string, kpiId: string | null) => void;
  /** 계획 점검 달성 점수(0~10, null=지움 — 0 유효). */
  onPrevScore: (id: string, score: number | null) => void;
  onCommit: () => void;
}

type Member = { member_id: string; name: string; color?: string | null };

function PlanRetroPanels({
  mode,
  plan,
  retro,
  planPrev,
  handlers,
  groups,
  members,
  kpiOptions,
  retroCard,
}: {
  mode: Mode;
  plan: PlanItem[];
  retro: PlanItem[];
  planPrev: PlanItem[];
  handlers: PanelHandlers;
  /** 하위 칸 목록(예: AX의 ["대본","아트인"]). null이면 단일 칸. */
  groups?: readonly string[] | null;
  /** 담당자 부여용 팀원 목록. */
  members?: Member[];
  /** 계획 항목을 연결할 KPI 옵션(value=kpi_id, label=표시). */
  kpiOptions?: { value: string; label: string }[];
  /** 회고 칼럼에 끼워 넣는 KPI 회고 카드(신뢰도/채점) — 단일 프로젝트 보기 전용. */
  retroCard?: ReactNode;
}) {
  const prevLabel = mode === "weekly" ? "지난 주" : "지난 달";
  // 항목 세부(노션식) 편집 모달
  const [detailCtx, setDetailCtx] = useState<{
    item: PlanItem;
    save: (detail: string | null) => void;
    onAssignee: (assignee: string | null) => void;
    onKpi?: (kpiId: string | null) => void;
  } | null>(null);

  // 칸이 있으면 group으로 필터, 없으면 전체 그대로.
  const byGroup = (items: PlanItem[], g: string) =>
    groups ? items.filter((it) => itemGroup(it, groups) === g) : items;

  // 계획 점검 달성 점수 요약 — score=0도 채점(!= null 필터, truthiness 금지).
  const prevScoredList = planPrev.filter((it) => it.score != null);
  const prevScored = {
    count: prevScoredList.length,
    avg:
      prevScoredList.length > 0
        ? Math.round(
            (prevScoredList.reduce((s, it) => s + (it.score as number), 0) /
              prevScoredList.length) *
              10,
          ) / 10
        : 0,
  };

  // ---- 한 묶음(전체 또는 한 칸)에 대한 렌더 헬퍼 ----
  // 점검은 점수 단일 입력(체크박스 없음 — 오너 결정 2026-07-12). 완료 표시는
  // 점수에서 파생: score>=7 = 달성(서버도 저장 시 done을 같은 규칙으로 파생).
  // 점수 미기록 항목은 주중 체크박스 값(done)을 그대로 따른다. 0점 유효.
  const renderPrevCheck = (items: PlanItem[]) =>
    items.length === 0 ? (
      <p className="text-[var(--text-meta)]" style={labelStyle()}>
        {prevLabel}에 세운 계획이 없습니다.
      </p>
    ) : (
      <div className="flex flex-col gap-0.5">
        {items.map((it) => {
          const m = members?.find((mm) => mm.member_id === it.assignee_member_id);
          const achieved = it.score != null ? it.score >= 7 : it.done;
          return (
            <ListRow
              key={it.id}
              muted={achieved}
              title={
                <span
                  className="whitespace-pre-wrap"
                  style={{
                    textDecoration: achieved ? "line-through" : "none",
                    color: achieved ? "var(--color-text-faint)" : "var(--color-text-body)",
                  }}
                >
                  {it.text || "(빈 항목)"}
                </span>
              }
              trailing={
                <span className="flex shrink-0 items-center gap-1.5">
                  <ScorePicker
                    value={it.score}
                    onChange={(s) => handlers.onPrevScore(it.id, s)}
                    title="달성 점수 0~10 — 7점 이상이면 완료로 처리됩니다"
                  />
                  {m ? (
                    <Avatar name={m.name} color={m.color || undefined} size="sm" title={m.name} />
                  ) : null}
                </span>
              }
              actions={
                <button
                  onClick={() =>
                    setDetailCtx({
                      item: it,
                      save: (d) => handlers.onPrevDetail(it.id, d),
                      onAssignee: (a) => handlers.onPrevAssignee(it.id, a),
                      onKpi: (k) => handlers.onPrevKpi(it.id, k),
                    })
                  }
                  className="text-[var(--text-meta)]"
                  style={{ color: it.detail ? "var(--color-accent)" : "var(--color-text-muted)" }}
                  title="세부"
                >
                  {it.detail ? "세부●" : "세부"}
                </button>
              }
            />
          );
        })}
      </div>
    );

  const renderRetroMemo = (items: PlanItem[]) => (
    <ItemRows
      items={items}
      showDone={false}
      onText={handlers.onRetroText}
      onRemove={handlers.onRemoveRetro}
      onOpenDetail={(it) =>
        setDetailCtx({
          item: it,
          save: (d) => handlers.onRetroDetail(it.id, d),
          onAssignee: (a) => handlers.onRetroAssignee(it.id, a),
        })
      }
      onAssignee={handlers.onRetroAssignee}
      onEnter={(it) => {
        handlers.onAddRetro(it.group ?? undefined);
        handlers.onCommit();
      }}
      placeholder="배운 점 / 잘된 점 / 개선할 점"
      emptyText="회고 메모가 없습니다."
      members={members}
    />
  );

  const renderPlanRows = (items: PlanItem[]) => (
    <ItemRows
      items={items}
      showDone
      onText={handlers.onPlanText}
      onToggle={handlers.onTogglePlanDone}
      onRemove={handlers.onRemovePlan}
      onOpenDetail={(it) =>
        setDetailCtx({
          item: it,
          save: (d) => handlers.onPlanDetail(it.id, d),
          onAssignee: (a) => handlers.onPlanAssignee(it.id, a),
          onKpi: (k) => handlers.onPlanKpi(it.id, k),
        })
      }
      onAssignee={handlers.onPlanAssignee}
      onEnter={(it) => {
        handlers.onAddPlan(it.group ?? undefined);
        handlers.onCommit();
      }}
      placeholder="계획 항목"
      emptyText="계획 항목이 없습니다. + 항목으로 추가하세요."
      members={members}
    />
  );

  const groupBox = {
    border: "1px solid var(--color-divider)",
    background: "var(--color-panel)",
  };

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      {detailCtx ? (
        <ItemDetailModal
          open
          title={detailCtx.item.text}
          detail={detailCtx.item.detail}
          members={members}
          assignee={detailCtx.item.assignee_member_id}
          onAssignee={(a) => {
            detailCtx.onAssignee(a);
            // 모달 안의 칩이 즉시 갱신되도록 detailCtx 항목도 업데이트
            setDetailCtx((c) =>
              c ? { ...c, item: { ...c.item, assignee_member_id: a } } : c,
            );
          }}
          kpiOptions={detailCtx.onKpi ? kpiOptions : undefined}
          kpiId={detailCtx.item.kpi_id}
          onKpi={
            detailCtx.onKpi
              ? (k) => {
                  detailCtx.onKpi?.(k);
                  setDetailCtx((c) => (c ? { ...c, item: { ...c.item, kpi_id: k } } : c));
                }
              : undefined
          }
          onClose={() => setDetailCtx(null)}
          onSave={(d) => {
            detailCtx.save(d);
            setDetailCtx(null);
          }}
          onLiveDetail={(d) => {
            // 모달을 닫지 않고 세부를 리스트 state로 계속 반영 → 명시적 닫기 없이
            // 페이지를 떠나도(사이드바 이동/뒤로가기/탭 숨김) 저장 루프가 보존한다.
            detailCtx.save(d);
            setDetailCtx((c) =>
              c ? { ...c, item: { ...c.item, detail: d } } : c,
            );
          }}
        />
      ) : null}
      {/* LEFT: 회고 */}
      <Card className="p-3 sm:p-4">
        <SectionHeader
          label={mode === "weekly" ? "주간 회고" : "월간 회고"}
          action={
            prevScored.count > 0 ? (
              <Chip tone="neutral">
                실행 점수 평균 {prevScored.avg}/10 · {prevScored.count}/{planPrev.length} 채점
              </Chip>
            ) : undefined
          }
        />
        <p className="mb-3 -mt-1 text-[var(--text-meta)]" style={labelStyle()}>
          {prevLabel}에 세운 계획을 점검하고 회고를 남기세요.
        </p>
        {/* 그룹 칸 프로젝트는 카드가 칸 위에 오고, 단일 칸은 점검과 메모 사이(아래 분기). */}
        {groups ? retroCard : null}

        {groups ? (
          groups.map((g) => (
            <div key={g} className="mb-3 rounded-[8px] p-2.5 last:mb-0" style={groupBox}>
              <p
                className="mb-2 text-[var(--text-body-sm)] font-bold"
                style={{ color: "var(--color-text-strong)" }}
              >
                {g}
              </p>
              <div className="mb-3">
                <p className="mb-1.5 text-[var(--text-meta)] font-semibold" style={labelStyle()}>
                  계획 점검 ({prevLabel} 계획)
                </p>
                {renderPrevCheck(byGroup(planPrev, g))}
              </div>
              <div>
                <div className="mb-1.5 flex items-center justify-between">
                  <p className="text-[var(--text-meta)] font-semibold" style={labelStyle()}>
                    회고 메모
                  </p>
                  <button
                    onClick={() => handlers.onAddRetro(g)}
                    className="text-[var(--text-meta)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    + 추가
                  </button>
                </div>
                {renderRetroMemo(byGroup(retro, g))}
              </div>
            </div>
          ))
        ) : (
          <>
            <div className="mb-4">
              <SectionHeader
                label={<>계획 점검 <span style={labelStyle()}>({prevLabel})</span></>}
                count={planPrev.length}
              />
              {renderPrevCheck(planPrev)}
            </div>
            {retroCard}
            <div>
              <SectionHeader
                label="회고 메모"
                count={retro.length}
                action={
                  <button
                    onClick={() => handlers.onAddRetro()}
                    className="text-[var(--text-meta)] font-semibold"
                    style={{ color: "var(--color-accent)" }}
                  >
                    + 추가
                  </button>
                }
              />
              {renderRetroMemo(retro)}
            </div>
          </>
        )}
      </Card>

      {/* RIGHT: 계획 */}
      <Card className="p-3 sm:p-4">
        <SectionHeader
          label={mode === "weekly" ? "주간 계획" : "월간 계획 · KPI"}
          count={plan.length}
          action={
            !groups ? (
              <button
                onClick={() => handlers.onAddPlan()}
                className="text-[var(--text-meta)] font-semibold"
                style={{ color: "var(--color-accent)" }}
              >
                + 항목
              </button>
            ) : undefined
          }
        />
        <p className="mb-3 -mt-1 text-[var(--text-meta)]" style={labelStyle()}>
          {mode === "weekly" ? "이번 주에 할 일을 적으세요." : "이번 달 목표·KPI를 적으세요."}
          {" "}· Enter=저장·다음 / Shift+Enter=줄바꿈
        </p>
        {groups ? (
          groups.map((g) => (
            <div key={g} className="mb-3 rounded-[8px] p-2.5 last:mb-0" style={groupBox}>
              <div className="mb-1.5 flex items-center justify-between">
                <p
                  className="text-[var(--text-body-sm)] font-bold"
                  style={{ color: "var(--color-text-strong)" }}
                >
                  {g}
                </p>
                <button
                  onClick={() => handlers.onAddPlan(g)}
                  className="text-[var(--text-meta)]"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  + 항목
                </button>
              </div>
              {renderPlanRows(byGroup(plan, g))}
            </div>
          ))
        ) : (
          renderPlanRows(plan)
        )}
      </Card>
    </div>
  );
}

// 노션식 인라인 담당자 토글. 항목을 쓰는 줄 옆에서 바로 담당자를 고른다(세부 모달 불필요).
// 담당자 목록(members)이 없으면 렌더하지 않는다(담당자 기능은 부가).
function AssigneePicker({
  members,
  value,
  onChange,
}: {
  members?: Member[];
  value: string | null | undefined;
  onChange: (memberId: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!members || members.length === 0) return null;
  const current = members.find((m) => m.member_id === value) || null;

  return (
    <div ref={boxRef} className="relative mt-1 shrink-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={current ? `담당: ${current.name}` : "담당자 지정"}
        aria-label={current ? `담당자 ${current.name}, 변경` : "담당자 지정"}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex h-[26px] items-center gap-1 rounded-[5px] px-1.5 text-[var(--text-meta)]"
        style={{
          border: "1px solid var(--color-divider)",
          background: "var(--color-card)",
          color: current ? "var(--color-text-strong)" : "var(--color-text-faint)",
        }}
      >
        {current ? (
          <Avatar name={current.name} color={current.color || undefined} size="xs" />
        ) : (
          <span
            className="inline-block h-4 w-4 shrink-0 rounded-full"
            style={{ border: "1px dashed var(--color-text-faint)" }}
          />
        )}
        {/* 폰(<760px)에선 이름을 숨겨 아이콘만 — 항목 텍스트영역 압박 방지(P5 모바일). */}
        <span className="hidden max-w-[60px] truncate min-[760px]:inline">
          {current ? current.name : "담당"}
        </span>
      </button>
      {open ? (
        <div
          role="menu"
          className="absolute right-0 z-30 mt-1 max-h-56 w-44 overflow-y-auto rounded-[6px] py-1 shadow-lg"
          style={{ border: "1px solid var(--color-divider)", background: "var(--color-panel)" }}
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              onChange(null);
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-faint)" }}
          >
            <span
              className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
              style={{ background: "var(--color-divider)" }}
            />
            담당 해제
          </button>
          {members.map((m) => (
            <button
              key={m.member_id}
              type="button"
              role="menuitem"
              onClick={() => {
                onChange(m.member_id);
                setOpen(false);
              }}
              className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[var(--text-body-sm)]"
              style={{
                color: "var(--color-text-strong)",
                fontWeight: m.member_id === value ? 600 : 400,
                background: m.member_id === value ? "var(--color-card)" : "transparent",
              }}
            >
              <Avatar name={m.name} color={m.color || undefined} size="xs" />
              <span className="flex-1 truncate">{m.name}</span>
              {m.member_id === value ? <span aria-hidden>✓</span> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ItemRows({
  items,
  showDone,
  onText,
  onToggle,
  onRemove,
  onOpenDetail,
  onAssignee,
  onEnter,
  placeholder,
  emptyText,
  members,
}: {
  items: PlanItem[];
  showDone: boolean;
  onText: (id: string, text: string) => void;
  onToggle?: (id: string) => void;
  onRemove: (id: string) => void;
  onOpenDetail?: (item: PlanItem) => void;
  /** 인라인 담당자 지정(노션식). 항목 줄 옆 토글에서 바로 적용. */
  onAssignee?: (id: string, memberId: string | null) => void;
  /** Enter(=저장+다음 항목). Shift+Enter는 줄바꿈. */
  onEnter?: (item: PlanItem) => void;
  placeholder: string;
  emptyText: string;
  members?: Member[];
}) {
  const ulRef = useRef<HTMLUListElement | null>(null);
  if (items.length === 0) {
    return (
      <p className="text-[var(--text-meta)]" style={labelStyle()}>
        {emptyText}
      </p>
    );
  }
  return (
    <ul ref={ulRef} className="flex flex-col gap-1.5">
      {items.map((it) => (
        <li
          key={it.id}
          className="group flex items-start gap-2 rounded-[6px] px-1 py-0.5 transition-colors hover:bg-[color-mix(in_srgb,var(--color-sidebar-active)_45%,transparent)]"
        >
          {showDone && onToggle ? (
            <input
              type="checkbox"
              checked={it.done}
              onChange={() => onToggle(it.id)}
              className="orthus-check mt-2 shrink-0"
            />
          ) : null}
          <textarea
            value={it.text}
            placeholder={placeholder}
            rows={1}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && onEnter) {
                // Enter = 저장 + 다음 항목 / Shift+Enter = 줄바꿈
                e.preventDefault();
                onEnter(it);
                window.setTimeout(() => {
                  const tas = ulRef.current?.querySelectorAll("textarea");
                  tas?.[tas.length - 1]?.focus();
                }, 40);
              }
            }}
            onChange={(e) => {
              onText(it.id, e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = `${e.target.scrollHeight}px`;
            }}
            ref={(el) => {
              if (el) {
                el.style.height = "auto";
                el.style.height = `${el.scrollHeight}px`;
              }
            }}
            className={cx(
              "min-h-[34px] flex-1 resize-none overflow-hidden rounded-[5px] px-2 py-1.5 text-[var(--text-body-sm)] leading-relaxed",
            )}
            style={{
              border: "1px solid var(--color-divider)",
              background: "var(--color-card)",
              color: "var(--color-text-strong)",
            }}
          />
          {onAssignee ? (
            <AssigneePicker
              members={members}
              value={it.assignee_member_id}
              onChange={(a) => onAssignee(it.id, a)}
            />
          ) : it.assignee_member_id ? (
            <span
              className="mt-2.5 inline-block h-2.5 w-2.5 shrink-0 rounded-full"
              title={members?.find((m) => m.member_id === it.assignee_member_id)?.name}
              style={{
                background:
                  members?.find((m) => m.member_id === it.assignee_member_id)?.color ||
                  "var(--color-divider)",
              }}
            />
          ) : null}
          <div className="orthus-row-actions mt-1.5 flex shrink-0 items-center gap-1.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
            {onOpenDetail ? (
              <button
                onClick={() => onOpenDetail(it)}
                className="text-[var(--text-meta)]"
                style={{ color: it.detail ? "var(--color-accent)" : "var(--color-text-muted)" }}
                title="세부 작성 (볼드·제목·이미지 등)"
              >
                {it.detail ? "세부●" : "세부"}
              </button>
            ) : null}
            <button
              onClick={() => onRemove(it.id)}
              aria-label="삭제"
              className="text-[var(--text-meta)]"
              style={{ color: "var(--color-text-faint)" }}
            >
              ✕
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}
