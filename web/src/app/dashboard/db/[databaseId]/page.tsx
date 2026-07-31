"use client";

/**
 * 전체 페이지 데이터베이스 — 노션의 inline=false 풀페이지 DB.
 * 인라인 블록의 "페이지로 열기" 또는 데이터베이스 링크 행에서 진입한다.
 * ?row=<rowId> 딥링크면 해당 행(페이지)을 자동으로 연다.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { api, type DashboardProject, type ProjectDatabaseBundle } from "@/lib/api";
import { DatabaseView } from "@/components/dashboard/database/DatabaseView";
import { TagPill, labelStyle } from "@/components/dashboard/kit";

export default function FullPageDatabase() {
  const params = useParams<{ databaseId: string }>();
  const databaseId = params.databaseId;
  const searchParams = useSearchParams();
  const rowId = searchParams.get("row");

  // 번들은 여기서 1회만 받아 DatabaseView에 그대로 넘긴다(이중 다운로드 제거).
  // databaseId를 함께 저장해 라우트 전환 시 stale 데이터를 렌더에서 걸러낸다
  // (effect 본문 동기 setState 없이 리셋 효과).
  const [loaded, setLoaded] = useState<{
    databaseId: string;
    bundle: ProjectDatabaseBundle;
    project: DashboardProject | null;
    parent: DashboardProject | null;
  } | null>(null);
  const [missingId, setMissingId] = useState<string | null>(null);

  // 번들 + 프로젝트 목록을 병렬로 받아 브레드크럼의 프로젝트/상위 프로젝트를
  // 목록에서 로컬 resolve한다(기존 번들→프로젝트→상위 3단 waterfall 제거).
  useEffect(() => {
    let alive = true;
    Promise.all([
      // 목록 렌더용 번들 — 행 body는 opt-in 생략(행 열 때 DatabaseView가 지연 로드).
      api.getProjectDatabase(databaseId, { body: "none" }),
      // 브레드크럼 실패는 기존처럼 조용히 무시(빈 브레드크럼).
      api.listDashboardProjects().catch(() => [] as DashboardProject[]),
    ])
      .then(([b, projects]) => {
        if (!alive) return;
        const project =
          projects.find((it) => it.project_id === b.database.project_id) ?? null;
        const parent = project?.parent_project_id
          ? (projects.find((it) => it.project_id === project.parent_project_id) ?? null)
          : null;
        setLoaded({ databaseId, bundle: b, project, parent });
      })
      .catch(() => alive && setMissingId(databaseId));
    return () => {
      alive = false;
    };
  }, [databaseId]);

  const current = loaded?.databaseId === databaseId ? loaded : null;
  const missing = missingId === databaseId;
  const project = current?.project ?? null;
  const parent = current?.parent ?? null;

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <div className="mb-3 flex flex-wrap items-center gap-1 text-[var(--text-meta)]" style={labelStyle()}>
        <Link
          href="/dashboard/projects"
          className="rounded-[4px] px-1 hover:bg-[var(--color-app-canvas)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          프로젝트
        </Link>
        {parent ? (
          <>
            <span style={{ color: "var(--color-text-faint)" }}>/</span>
            <Link
              href={`/dashboard/projects/${parent.project_id}`}
              className="rounded-[4px] px-1 hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {parent.name}
            </Link>
          </>
        ) : null}
        {project ? (
          <>
            <span style={{ color: "var(--color-text-faint)" }}>/</span>
            <Link
              href={`/dashboard/projects/${project.project_id}`}
              className="rounded-[4px] px-1 hover:bg-[var(--color-app-canvas)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {project.name}
            </Link>
            {parent ? <TagPill label="하위 프로젝트" color="purple" /> : null}
          </>
        ) : null}
      </div>

      {missing ? (
        <p className="py-10 text-center text-[var(--text-body-sm)]" style={labelStyle()}>
          데이터베이스를 찾을 수 없습니다.
        </p>
      ) : current ? (
        <DatabaseView
          databaseId={databaseId}
          fullPage
          initialRowId={rowId}
          initialBundle={current.bundle}
        />
      ) : (
        <div
          className="rounded-[8px] px-3 py-6 text-center text-[var(--text-body-sm)]"
          style={{ color: "var(--color-text-faint)" }}
        >
          데이터베이스 불러오는 중…
        </div>
      )}
    </div>
  );
}
