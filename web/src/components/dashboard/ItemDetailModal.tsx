"use client";

import { useCallback, useEffect, useRef } from "react";
import dynamic from "next/dynamic";
import type { Block } from "@/lib/api";
import type { EditorHandle } from "@/components/BlockNoteEditor";
import { ToolbarButton, Skeleton } from "@/components/ui";
import { useCommitFlush } from "@/lib/use-commit-flush";
import { blocksAreEmpty } from "@/lib/blocks";

// BlockNote touches `window`; load client-only (same pattern as /editor, 회의록).
const BlockNoteEditor = dynamic(
  () => import("@/components/BlockNoteEditor").then((m) => m.BlockNoteEditor),
  {
    ssr: false,
    loading: () => (
      <div className="space-y-2 pt-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-3 w-full" />
      </div>
    ),
  },
);

/** Parse stored detail (BlockNote blocks JSON) back to blocks; null if empty. */
function parseDetail(detail: string | null | undefined): Block[] | null {
  if (!detail) return null;
  try {
    const parsed = JSON.parse(detail);
    if (Array.isArray(parsed)) return parsed as Block[];
  } catch {
    // legacy plain text → single paragraph
    return [
      { type: "paragraph", content: [{ type: "text", text: detail, styles: {} }] },
    ] as unknown as Block[];
  }
  return null;
}

function uploadAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(new Error("이미지 읽기 실패"));
    reader.readAsDataURL(file);
  });
}

/**
 * Notion-style rich detail editor for a plan/retro item. The item's title stays
 * inline in the list; this modal edits the longer body (bold, headings, images…).
 */
export function ItemDetailModal({
  open,
  title,
  detail,
  readOnly = false,
  onClose,
  onSave,
  onLiveDetail,
  members,
  assignee,
  onAssignee,
  kpiOptions,
  kpiId,
  onKpi,
}: {
  open: boolean;
  title: string;
  detail: string | null | undefined;
  readOnly?: boolean;
  onClose: () => void;
  onSave: (detail: string | null) => void;
  /**
   * 편집 중 세부를 부모 리스트 state로 계속 올리는(닫지 않는) 콜백. 있으면 모달을
   * 명시적으로 닫지 않고 페이지를 떠나도(사이드바 이동/뒤로가기/탭 숨김) 부모의 저장
   * 루프가 세부를 보존한다. `onSave`와 달리 모달을 닫지 않는다.
   */
  onLiveDetail?: (detail: string | null) => void;
  /** 담당자 부여용 팀원 목록. 있으면 모달 상단에 담당자 셀렉터를 보인다. */
  members?: { member_id: string; name: string; color?: string | null }[];
  assignee?: string | null;
  /** 담당자 변경(즉시 적용). 부여되면 그 사람 개인 보드 주간 플랜에 반영. */
  onAssignee?: (memberId: string | null) => void;
  /** 연결 가능한 KPI 목록(value=kpi_id, label=표시). 있으면 KPI 연결 셀렉터를 보인다. */
  kpiOptions?: { value: string; label: string }[];
  kpiId?: string | null;
  onKpi?: (kpiId: string | null) => void;
}) {
  const editorRef = useRef<EditorHandle | null>(null);
  const liftTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 현재 에디터 내용을 저장 가능한 세부 문자열로(또는 비었으면 null) 만든다.
  const computeDetail = useCallback((): string | null => {
    const ed = editorRef.current;
    if (!ed) return null;
    const blocks = ed.getBlocks();
    return blocksAreEmpty(blocks) ? null : JSON.stringify(blocks);
  }, []);

  // 편집 내용을 부모 리스트 state로 올린다(닫지 않음). 종료 경로/디바운스에서 호출.
  const lift = useCallback(() => {
    if (readOnly || !onLiveDetail || !editorRef.current) return;
    onLiveDetail(computeDetail());
  }, [readOnly, onLiveDetail, computeDetail]);

  // 언마운트·탭 숨김·pagehide 등 모든 종료 경로에서 마지막 편집을 부모로 올린다.
  useCommitFlush(lift);

  useEffect(
    () => () => {
      if (liftTimer.current) clearTimeout(liftTimer.current);
    },
    [],
  );

  // 편집할 때마다 600ms 디바운스로 부모 state에 반영 → 명시적 닫기 없이 떠나도 보존.
  const handleEditorChange = useCallback(() => {
    if (readOnly || !onLiveDetail) return;
    if (liftTimer.current) clearTimeout(liftTimer.current);
    liftTimer.current = setTimeout(lift, 600);
  }, [readOnly, onLiveDetail, lift]);

  if (!open) return null;

  function save() {
    onSave(computeDetail());
  }

  // 바깥 클릭/닫기로 모달을 떠나도 작성한 세부 내용을 자동 저장한다(읽기전용 제외).
  // save()가 onSave를 호출하고, 부모가 onSave에서 모달을 닫는다.
  function closeWithSave() {
    if (readOnly) {
      onClose();
      return;
    }
    save();
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center sm:items-center"
      style={{ background: "rgba(0,0,0,0.32)" }}
      onClick={closeWithSave}
    >
      <div
        className="flex w-full max-w-[680px] flex-col rounded-t-[var(--radius-modal)] sm:rounded-[var(--radius-modal)]"
        style={{ background: "var(--color-modal)", boxShadow: "var(--shadow-modal)", maxHeight: "90dvh" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          className="flex items-center justify-between gap-2 px-4 py-3"
          style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
        >
          <h2
            className="min-w-0 flex-1 truncate text-[var(--text-body)] font-bold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {title || "(제목 없음)"} <span style={{ color: "var(--color-text-muted)" }}>· 세부</span>
          </h2>
        </div>
        {members && onAssignee ? (
          <div
            className="flex items-center gap-2 px-4 py-2.5"
            style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
          >
            <span
              className="shrink-0 text-[var(--text-body-sm)] font-semibold"
              style={{ color: "var(--color-text-muted)" }}
            >
              담당자
            </span>
            {assignee ? (
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{
                  background:
                    members.find((m) => m.member_id === assignee)?.color ||
                    "var(--color-divider)",
                }}
              />
            ) : null}
            <select
              value={assignee ?? ""}
              onChange={(e) => onAssignee(e.target.value || null)}
              disabled={readOnly}
              className="rounded-[6px] px-2 py-1 text-[var(--text-body-sm)] outline-none"
              style={{
                background: "var(--color-card)",
                border: "1px solid var(--color-divider)",
                color: assignee ? "var(--color-text-strong)" : "var(--color-text-faint)",
              }}
            >
              <option value="">— 미지정 —</option>
              {members.map((m) => (
                <option key={m.member_id} value={m.member_id}>
                  {m.name}
                </option>
              ))}
            </select>
            <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
              부여하면 그 사람 개인 보드 주간 플랜에 들어갑니다
            </span>
          </div>
        ) : null}
        {kpiOptions && onKpi ? (
          <div
            className="flex items-center gap-2 px-4 py-2.5"
            style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
          >
            <span
              className="shrink-0 text-[var(--text-body-sm)] font-semibold"
              style={{ color: "var(--color-text-muted)" }}
            >
              KPI 연결
            </span>
            <select
              value={kpiId ?? ""}
              onChange={(e) => onKpi(e.target.value || null)}
              disabled={readOnly}
              className="min-w-0 flex-1 rounded-[6px] px-2 py-1 text-[var(--text-body-sm)] outline-none"
              style={{
                background: "var(--color-card)",
                border: "1px solid var(--color-divider)",
                color: kpiId ? "var(--color-text-strong)" : "var(--color-text-faint)",
              }}
            >
              <option value="">— 연결 안 함 —</option>
              {kpiOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <span className="shrink-0 text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
              이 주간 실행이 KPI에 묶입니다
            </span>
          </div>
        ) : null}
        <div className="min-h-[260px] flex-1 overflow-y-auto px-4 py-3">
          <BlockNoteEditor
            docId={`detail-${title}`}
            initialBlocks={parseDetail(detail)}
            handleRef={editorRef}
            uploadFile={uploadAsDataUrl}
            onChange={handleEditorChange}
          />
        </div>
        <div
          className="flex items-center justify-end gap-2 px-4 py-3"
          style={{ borderTop: "1px solid var(--color-divider-soft)" }}
        >
          <ToolbarButton onClick={closeWithSave}>닫기</ToolbarButton>
          {readOnly ? null : (
            <ToolbarButton tone="primary" onClick={save}>
              저장
            </ToolbarButton>
          )}
        </div>
      </div>
    </div>
  );
}
