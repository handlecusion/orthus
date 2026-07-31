"use client";

// 계획·회고 화면의 KPI 회고 카드 — OKR 운영 루프의 리추얼 표면.
// weekly: 진행 중 KR 신뢰도 체크인(1~10 1탭 + 스파크라인 + 미기록 넛지).
// monthly: 지난달 타깃/분기말 KR 공식 채점(근거·제안 점수 표시, 사람이 확정).
// 서버(GET /dashboard/kpis/retro-card)가 캘린더 수학과 제안 공식을 소유하고,
// 이 컴포넌트는 fetch 실패 시 아무것도 렌더하지 않아 배포 순서에 내성이 있다.

import { useCallback, useEffect, useState } from "react";
import { api, type KpiRetroCard as RetroCard, type KpiRetroItem } from "@/lib/api";
import { Card, Chip, SectionHeader } from "@/components/ui";
import {
  ConfidenceSparkline,
  ProgressBar,
  ScorePicker,
  labelStyle,
} from "@/components/dashboard/kit";

const SECTION_LABEL: Record<string, string> = {
  month_target: "지난달 타깃 채점",
  quarter_kr: "분기 KR 채점",
  annual_kr: "연간 KR 채점",
};

function fmtMonth(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(`${iso}T00:00:00`);
  return `${d.getFullYear()}년 ${d.getMonth() + 1}월`;
}

function evidenceLine(it: KpiRetroItem): string {
  const parts: string[] = [];
  // progress 0.0도 유효한 근거 — != null로만 판별.
  if (it.kpi.progress != null) {
    parts.push(`측정 ${Math.round(it.kpi.progress * 100)}%`);
  }
  if (it.linked_total > 0) {
    const score =
      it.linked_score_avg != null
        ? ` · 점수평균 ${it.linked_score_avg}/10 (${it.linked_scored}건)`
        : "";
    parts.push(`실행 ${it.linked_done}/${it.linked_total}${score}`);
  }
  if (it.child_grade_avg != null) {
    parts.push(`하위 채점 평균 ${it.child_grade_avg}/10`);
  }
  return parts.length ? parts.join(" · ") : "근거 없음";
}

export function KpiRetroCard({
  mode,
  refKey,
  projectId,
  reloadSignal,
  framed = false,
}: {
  mode: "weekly" | "monthly";
  refKey: string;
  /** null = 전체(모든 프로젝트 + 회사 공통). */
  projectId: string | null;
  /** SSE kind:"kpi" 수신 등 외부 재조회 신호(증가 카운터). */
  reloadSignal: number;
  /** 전체 모드 상단 배치용 자체 Card 래핑(내용 없으면 빈 껍데기도 안 그린다). */
  framed?: boolean;
}) {
  const [card, setCard] = useState<RetroCard | null>(null);
  const [failed, setFailed] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [noteFor, setNoteFor] = useState<string | null>(null);
  const [noteDraft, setNoteDraft] = useState("");

  const reload = useCallback(
    () =>
      api
        .getKpiRetroCard(mode, refKey, projectId ?? undefined)
        .then((c) => {
          setCard(c);
          setFailed(false);
        })
        // 구 백엔드/일시 오류 — 카드 자체를 숨긴다(회고 흐름 무간섭).
        .catch(() => setFailed(true)),
    [mode, refKey, projectId],
  );

  useEffect(() => {
    void reload();
  }, [reload, reloadSignal]);

  if (failed || card == null) return null;

  const recordConfidence = async (it: KpiRetroItem, n: number) => {
    setBusyId(it.kpi.kpi_id);
    setError(null);
    try {
      await api.setKpiConfidence(it.kpi.kpi_id, {
        confidence: n,
        week_start: card.ref,
      });
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "신뢰도 기록 실패");
    } finally {
      setBusyId(null);
    }
  };

  const recordGrade = async (it: KpiRetroItem, n: number | null) => {
    setBusyId(it.kpi.kpi_id);
    setError(null);
    try {
      if (n == null) {
        // 말소는 operator 전용 — 403이면 안내만.
        await api.clearKpiGrade(it.kpi.kpi_id);
      } else {
        await api.gradeKpi(it.kpi.kpi_id, {
          grade: n,
          note: it.kpi.grade_note ?? undefined,
        });
      }
      await reload();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "채점 실패";
      setError(/403/.test(msg) ? "채점 말소는 운영자만 할 수 있습니다." : msg);
    } finally {
      setBusyId(null);
    }
  };

  const saveNote = async (it: KpiRetroItem) => {
    if (it.kpi.grade == null) return; // 채점 후에만 근거 메모(0점도 채점)
    setBusyId(it.kpi.kpi_id);
    setError(null);
    try {
      await api.gradeKpi(it.kpi.kpi_id, {
        grade: it.kpi.grade,
        note: noteDraft.trim() || null,
      });
      setNoteFor(null);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "메모 저장 실패");
    } finally {
      setBusyId(null);
    }
  };

  const emptyLine = (text: string) => (
    <p className="text-[var(--text-meta)]" style={labelStyle()}>
      {text}{" "}
      <a href="/dashboard/kpi" style={{ color: "var(--color-accent)" }}>
        KPI 페이지에서 추가
      </a>
    </p>
  );

  const gradeRow = (it: KpiRetroItem) => (
    <div
      key={it.kpi.kpi_id}
      className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1.5"
    >
      <span
        className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
        style={{ color: "var(--color-text-body)" }}
        title={it.kpi.title}
      >
        {it.kpi.title}
      </span>
      <span className="text-[11px]" style={{ color: "var(--color-text-muted)" }}>
        {evidenceLine(it)}
      </span>
      {it.kpi.grade == null && it.suggested_grade != null ? (
        <Chip tone="accent">제안 {it.suggested_grade}</Chip>
      ) : null}
      <ScorePicker
        value={it.kpi.grade}
        onChange={(n) => void recordGrade(it, n)}
        label="채점"
        disabled={busyId === it.kpi.kpi_id}
        title="0~10 공식 채점 (재채점 가능)"
      />
      {it.kpi.grade != null ? (
        <button
          type="button"
          onClick={() => {
            setNoteFor(noteFor === it.kpi.kpi_id ? null : it.kpi.kpi_id);
            setNoteDraft(it.kpi.grade_note ?? "");
          }}
          className="text-[11px]"
          style={{
            color: it.kpi.grade_note
              ? "var(--color-accent)"
              : "var(--color-text-muted)",
          }}
        >
          {it.kpi.grade_note ? "근거●" : "근거"}
        </button>
      ) : null}
      {noteFor === it.kpi.kpi_id ? (
        <div className="flex w-full items-center gap-1.5">
          <input
            value={noteDraft}
            onChange={(e) => setNoteDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void saveNote(it);
              if (e.key === "Escape") setNoteFor(null);
            }}
            placeholder="채점 근거 메모"
            className="min-h-[44px] flex-1 rounded-[8px] border px-2 text-[13px]"
            style={{
              background: "var(--color-card)",
              borderColor: "var(--color-divider)",
              color: "var(--color-text-body)",
            }}
          />
          <button
            type="button"
            onClick={() => void saveNote(it)}
            disabled={busyId === it.kpi.kpi_id}
            className="min-h-[44px] rounded-[8px] border px-3 text-[12px] font-semibold"
            style={{
              color: "var(--color-accent)",
              borderColor: "var(--color-divider)",
            }}
          >
            저장
          </button>
        </div>
      ) : null}
    </div>
  );

  const body = (
    <div className={framed ? "" : "mb-4"}>
      {error ? (
        <p className="mb-1.5 text-[11px]" style={{ color: "var(--color-fail-fg)" }}>
          {error}
        </p>
      ) : null}

      {mode === "weekly" ? (
        <>
          <SectionHeader
            label="KR 신뢰도 체크인"
            action={
              card.unrecorded_kr_count > 0 ? (
                <Chip tone="warn">이번 주 미기록 {card.unrecorded_kr_count}</Chip>
              ) : card.confidence_krs.length > 0 ? (
                <Chip tone="pass">이번 주 완료</Chip>
              ) : undefined
            }
          />
          {card.confidence_krs.length === 0
            ? emptyLine("진행 중 KR이 없습니다.")
            : card.confidence_krs.map((it) => {
                const thisWeek = it.confidence_series.find(
                  (p) => p.week_start === card.ref,
                );
                return (
                  <div
                    key={it.kpi.kpi_id}
                    className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1.5"
                  >
                    <span
                      className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
                      style={{ color: "var(--color-text-body)" }}
                      title={it.kpi.title}
                    >
                      {it.kpi.title}
                    </span>
                    <div className="w-24 shrink-0">
                      <ProgressBar value={it.kpi.final_progress} height={6} />
                    </div>
                    <ConfidenceSparkline points={it.confidence_series} />
                    <ScorePicker
                      value={thisWeek ? thisWeek.confidence : null}
                      onChange={(n) => {
                        if (n != null) void recordConfidence(it, n);
                      }}
                      min={1}
                      label="기록"
                      allowClear={false}
                      disabled={busyId === it.kpi.kpi_id}
                      title="이번 주 달성 자신감 1~10"
                    />
                  </div>
                );
              })}
        </>
      ) : (
        <>
          <SectionHeader
            label={`${SECTION_LABEL.month_target} (${fmtMonth(card.review_start)})`}
            action={
              card.ungraded_past_count > 0 ? (
                <Chip tone="warn">미채점 지난 KPI {card.ungraded_past_count}건</Chip>
              ) : undefined
            }
          />
          {card.grade_targets.length === 0
            ? emptyLine(`${fmtMonth(card.review_start)} 타깃이 없습니다.`)
            : card.grade_targets.map(gradeRow)}
          {card.grade_krs.length > 0 ? (
            <>
              <div className="mt-3" />
              <SectionHeader
                label={
                  card.grade_krs.some((it) => it.section === "annual_kr")
                    ? "분기·연간 KR 채점"
                    : SECTION_LABEL.quarter_kr
                }
              />
              {card.grade_krs.map(gradeRow)}
            </>
          ) : null}
        </>
      )}
    </div>
  );

  return framed ? <Card className="p-3 sm:p-4">{body}</Card> : body;
}
