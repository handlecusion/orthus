"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  api,
  type CalendarEvent,
  type FinanceSummary,
  type TeamMember,
} from "@/lib/api";
import {
  ChannelHeader,
  Card,
  Banner,
  Skeleton,
  StatTile,
  ListRow,
  Avatar,
  SectionHeader,
} from "@/components/ui";
import { money, labelStyle, memberColorMap } from "@/components/dashboard/kit";
import { CultureCard } from "@/components/dashboard/CultureCard";

function ymd(d: Date): string {
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

// "HH:MM:SS"/"HH:MM" → "HH:MM" (달력과 동일 규약).
function hhmm(t: string): string {
  return t.slice(0, 5);
}

// 일정 시간 라벨: 종일이면 "종일", 시간이 있으면 "HH:MM"(종료 있으면 "HH:MM–HH:MM").
function eventTimeLabel(e: CalendarEvent): string | null {
  if (e.all_day) return "종일";
  if (!e.start_time) return null;
  const start = hhmm(e.start_time);
  return e.end_time ? `${start}–${hhmm(e.end_time)}` : start;
}

// 정렬 키: 종일/시간 없는 일정을 맨 위로, 나머지는 시작 시간 순.
function eventSortKey(e: CalendarEvent): string {
  return e.all_day || !e.start_time ? "" : e.start_time;
}

export default function OverviewPage() {
  const [summary, setSummary] = useState<FinanceSummary | null>(null);
  const [todayEvents, setTodayEvents] = useState<CalendarEvent[]>([]);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const today = ymd(new Date());
    Promise.all([
      api.getFinanceSummary(),
      api.listCalendar(today, today),
      api.listTeamMembers(),
    ])
      .then(([s, ev, mem]) => {
        if (!alive) return;
        setSummary(s);
        setTodayEvents(ev);
        setMembers(mem);
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

  const memberColors = memberColorMap(members);
  const memberNames = (ids: string[]) => {
    const names = ids
      .map((id) => members.find((m) => m.member_id === id)?.name)
      .filter(Boolean);
    return names.length ? names.join(", ") : "회사 전체";
  };

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <ChannelHeader
        glyph="#"
        title="개요"
        subtitle="회사 운영 현황을 한눈에."
        people={members.map((m) => ({
          name: m.name,
          color: memberColors.get(m.member_id) || undefined,
        }))}
      />

      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">{error}</Banner>
        </div>
      ) : null}

      {loading ? (
        <Card className="p-4">
          <Skeleton className="h-5 w-40" />
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {/* 자금 요약 — 고정 스탯 */}
          {summary ? (
            <div>
              <SectionHeader label="📌 자금 요약" />
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <StatTile label="잔액" value={money(summary.balance, summary.currency)} href="/dashboard/finance" />
                <StatTile label="번레이트(월)" value={money(summary.monthly_burn, summary.currency)} href="/dashboard/finance" />
                <StatTile label="런웨이" value={summary.runway_months != null ? `${summary.runway_months}개월` : "—"} href="/dashboard/finance" />
                <StatTile label="총 매출" value={money(summary.total_revenue, summary.currency)} href="/dashboard/finance" />
              </div>
            </div>
          ) : null}

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* 오늘 팀 일정 */}
            <Card className="p-3 sm:p-4">
              <SectionHeader
                label="오늘 팀 일정"
                count={todayEvents.length}
                action={
                  <Link href="/dashboard/calendar" className="text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-accent)" }}>
                    달력 →
                  </Link>
                }
              />
              {todayEvents.length === 0 ? (
                <p className="text-[var(--text-body-sm)]" style={labelStyle()}>오늘 일정이 없습니다.</p>
              ) : (
                <div className="flex flex-col gap-0.5">
                  {[...todayEvents]
                    .sort((a, b) => eventSortKey(a).localeCompare(eventSortKey(b)))
                    .map((e) => {
                      const firstId = e.member_ids[0];
                      const firstName = firstId
                        ? members.find((m) => m.member_id === firstId)?.name
                        : undefined;
                      const timeLabel = eventTimeLabel(e);
                      const metaText = e.location
                        ? `📍 ${e.location}  ·  ${memberNames(e.member_ids)}`
                        : memberNames(e.member_ids);
                      return (
                        <ListRow
                          key={e.event_id}
                          leading={
                            firstName ? (
                              <Avatar name={firstName} color={memberColors.get(firstId) || undefined} size="sm" />
                            ) : undefined
                          }
                          title={<span style={{ color: "var(--color-text-strong)" }}>{e.title}</span>}
                          meta={metaText}
                          trailing={
                            timeLabel ? (
                              <span
                                className="tabular-nums text-[var(--text-meta)] font-semibold"
                                style={{ color: "var(--color-text-muted)" }}
                              >
                                {timeLabel}
                              </span>
                            ) : undefined
                          }
                        />
                      );
                    })}
                </div>
              )}
            </Card>
          </div>

          {/* 팀 로스터 */}
          <Card className="p-3 sm:p-4">
            <SectionHeader
              label="팀"
              count={members.length}
              action={
                <Link href="/dashboard/team" className="text-[var(--text-meta)] font-semibold" style={{ color: "var(--color-accent)" }}>
                  관리 →
                </Link>
              }
            />
            {members.length === 0 ? (
              <p className="text-[var(--text-body-sm)]" style={labelStyle()}>등록된 팀원이 없습니다.</p>
            ) : (
              <div className="flex flex-wrap gap-x-5 gap-y-2.5">
                {members.map((m) => (
                  <span key={m.member_id} className="inline-flex items-center gap-2">
                    <Avatar name={m.name} color={memberColors.get(m.member_id) || undefined} size="sm" />
                    <span className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-strong)" }}>
                      {m.name}
                      {m.title ? (
                        <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
                          {" · "}
                          {m.title}
                        </span>
                      ) : null}
                    </span>
                  </span>
                ))}
              </div>
            )}
          </Card>

          {/* 회사 컬처 · 사무실 정보 */}
          <CultureCard />
        </div>
      )}
    </div>
  );
}
