"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import {
  api,
  API_BASE,
  type Block,
  type DashboardProject,
  type MeetingAttachment,
  type MeetingNote,
  type MeetingNoteInput,
  type PartnerCompany,
  type TeamMember,
  type UploadProgress,
} from "@/lib/api";
import { ChannelHeader, Avatar, Card, Banner, ToolbarButton, Skeleton, cx } from "@/components/ui";
import {
  DatePicker,
  Field,
  Select,
  TextInput,
  labelStyle,
} from "@/components/dashboard/kit";
import { InlineMembers, InlineSelect, InlineText } from "@/components/dashboard/inline";
import type { EditorHandle } from "@/components/BlockNoteEditor";
import { blocksAreEmpty } from "@/lib/blocks";

const BlockNoteEditor = dynamic(
  () => import("@/components/BlockNoteEditor").then((m) => m.BlockNoteEditor),
  {
    ssr: false,
    loading: () => (
      <div className="space-y-2 pt-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-5/6" />
      </div>
    ),
  },
);

type Kind = "internal" | "external";

function todayIso(): string {
  const d = new Date();
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

function parseBody(body: string | null): Block[] | null {
  if (!body) return null;
  try {
    const parsed = JSON.parse(body);
    if (Array.isArray(parsed)) return parsed as Block[];
  } catch {
    // legacy/plain text below
  }
  return body.split("\n").map((line) => ({
    type: "paragraph",
    content: line ? [{ type: "text", text: line, styles: {} }] : [],
  })) as unknown as Block[];
}

function uploadAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(new Error("이미지 읽기 실패"));
    reader.readAsDataURL(file);
  });
}

/** 첨부 capability URL(`/dashboard/media/...`)을 로드 가능한 절대 URL로. */
function mediaHref(url: string): string {
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}

function formatBytes(n: number): string {
  if (!n || !Number.isFinite(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// 작성 중 유실 방지용 로컬 드래프트(본문). 저장 성공 시 지운다 → 열었을 때 남아
// 있으면 마지막 세션이 저장 못 하고 닫힌 것(크래시/창닫힘)이라 복구를 제안한다.
const DRAFT_PREFIX = "orthus:meeting-body-draft:";
function draftKey(noteId: string): string {
  return `${DRAFT_PREFIX}${noteId}`;
}
function readBodyDraft(noteId: string): Block[] | null {
  try {
    const raw = window.localStorage.getItem(draftKey(noteId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { blocks?: Block[] };
    return Array.isArray(parsed.blocks) ? parsed.blocks : null;
  } catch {
    return null;
  }
}
function writeBodyDraft(noteId: string, blocks: Block[]): void {
  try {
    window.localStorage.setItem(
      draftKey(noteId),
      JSON.stringify({ blocks, ts: Date.now() }),
    );
  } catch {
    // storage 불가 환경은 온라인 저장만으로 동작
  }
}
function clearBodyDraft(noteId: string): void {
  try {
    window.localStorage.removeItem(draftKey(noteId));
  } catch {
    // noop
  }
}

type SaveState = "idle" | "dirty" | "saving" | "saved" | "error";

type Meta = {
  note_id: string | null;
  title: string;
  project_id: string;
  partner_id: string;
  meeting_kind: Kind;
  meeting_date: string;
  attendee_ids: string[];
  source: string;
};

export default function MeetingsPage() {
  const [notes, setNotes] = useState<MeetingNote[]>([]);
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [partners, setPartners] = useState<PartnerCompany[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [tab, setTab] = useState<Kind>("internal");
  const [projectFilter, setProjectFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [newTitle, setNewTitle] = useState("");
  const [adding, setAdding] = useState(false);

  function toggleExpand(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Inline-edit a single field of a manual meeting straight from the table.
  async function patchNote(n: MeetingNote, partial: Partial<MeetingNoteInput>) {
    const next = { ...n, ...partial } as MeetingNote;
    setNotes((rows) => rows.map((r) => (r.note_id === n.note_id ? next : r)));
    const input: MeetingNoteInput = {
      title: next.title,
      project_id: next.project_id,
      partner_id: next.meeting_kind === "external" ? next.partner_id : null,
      meeting_kind: next.meeting_kind,
      meeting_date: next.meeting_date,
      attendee_ids: next.attendee_ids,
      body: next.body,
    };
    try {
      await api.updateMeetingNote(n.note_id, input);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "저장 실패");
      await reload();
    }
  }

  async function addInline() {
    const title = newTitle.trim();
    if (!title || adding) return;
    setAdding(true);
    try {
      await api.createMeetingNote({
        title,
        project_id: null,
        partner_id: null,
        meeting_kind: tab,
        meeting_date: todayIso(),
        attendee_ids: [],
        body: null,
      });
      setNewTitle("");
      await reload();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "추가 실패");
    } finally {
      setAdding(false);
    }
  }

  const [meta, setMeta] = useState<Meta | null>(null);
  const [bodyBlocks, setBodyBlocks] = useState<Block[] | null>(null);
  const [editorKey, setEditorKey] = useState(0);
  const editorRef = useRef<EditorHandle | null>(null);
  const metaRef = useRef<Meta | null>(null);
  useEffect(() => {
    metaRef.current = meta;
  }, [meta]);

  // 첨부(PDF 등 미팅자료)
  const [attachments, setAttachments] = useState<MeetingAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadPct, setUploadPct] = useState(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // 저장 유실 방지: 본문 자동저장 + 로컬 드래프트 복구
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [draftBlocks, setDraftBlocks] = useState<Block[] | null>(null);
  const dirtyRef = useRef(false);
  const autosaveTimer = useRef<number | null>(null);
  // 저장이 진행 중인 동안 들어온 편집을 안전하게 다루기 위한 가드:
  //  - savingRef: 동시 PUT 방지(순서 뒤바뀐 저장이 오래된 본문을 덮어쓰지 않게).
  //  - editSeqRef: 편집 세대 카운터. 저장 시작 시점 세대를 캡처해, 저장 완료 시
  //    세대가 그대로면 clean으로 보고 dirty/드래프트를 정리하고, 그새 편집이
  //    들어와 세대가 바뀌었으면 dirty·드래프트를 유지한 채 재저장을 예약한다.
  const savingRef = useRef(false);
  const editSeqRef = useRef(0);

  const reload = useCallback(async () => {
    setNotes(await api.listMeetingNotes());
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.listMeetingNotes(),
      api.listDashboardProjects(),
      api.listTeamMembers(),
      api.listPartners(),
    ])
      .then(([n, p, m, pt]) => {
        if (!alive) return;
        setNotes(n);
        setProjects(p);
        setMembers(m);
        setPartners(pt);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
      });
    return () => {
      alive = false;
    };
  }, []);

  const projectName = (id: string | null) =>
    projects.find((p) => p.project_id === id)?.name ?? "—";
  const partnerName = (id: string | null) =>
    partners.find((p) => p.partner_id === id)?.name ?? "—";
  const attendeeMembers = (ids: string[]) =>
    ids
      .map((id) => members.find((m) => m.member_id === id))
      .filter((m): m is TeamMember => Boolean(m));

  const filtered = useMemo(
    () =>
      notes.filter(
        (n) =>
          n.meeting_kind === tab &&
          (!projectFilter || n.project_id === projectFilter),
      ),
    [notes, tab, projectFilter],
  );

  // 현재 meta(+본문)를 서버 입력으로. autosave 타이머에서도 최신값을 읽도록 metaRef 사용.
  function currentInput(bodyStr: string | null): MeetingNoteInput {
    const m = metaRef.current!;
    return {
      title: m.title,
      project_id: m.project_id || null,
      partner_id: m.meeting_kind === "external" ? m.partner_id || null : null,
      meeting_kind: m.meeting_kind,
      meeting_date: m.meeting_date,
      attendee_ids: m.attendee_ids,
      body: bodyStr,
    };
  }

  function serializeBody(): string | null {
    // 에디터 미마운트면 null(=본문 변경 없음, 서버가 기존 본문 보존). 로드된 에디터가
    // 비어 있으면 ""(명시적 비우기) — onChange 이후에만 호출되므로 로드 레이스 아님.
    // 빈 여부는 lossy markdown 길이 대신 블록 구조로 판정한다(이미지/체크박스/표만
    // 있는 본문을 ""로 지우던 wipe 방지 — #677 blocksAreEmpty).
    if (!editorRef.current) return null;
    const blocks = editorRef.current.getBlocks();
    return blocksAreEmpty(blocks) ? "" : JSON.stringify(blocks);
  }

  function cancelAutosave() {
    if (autosaveTimer.current) {
      window.clearTimeout(autosaveTimer.current);
      autosaveTimer.current = null;
    }
  }

  function scheduleAutosave() {
    cancelAutosave();
    autosaveTimer.current = window.setTimeout(() => void runSave(false), 1500);
  }

  // 변경 감지 → dirty 표시 + 자동저장 예약. body/meta 편집 공통.
  function touch() {
    const m = metaRef.current;
    if (!m?.note_id || m.source !== "manual") return;
    editSeqRef.current += 1; // 새 편집 세대
    dirtyRef.current = true;
    setSaveState("dirty");
    scheduleAutosave();
  }

  // 진행 중인 저장이 끝날 때까지 대기(최대 ~4s). 명시 저장·닫기 전 flush에서 사용.
  async function waitForSaveIdle() {
    for (let i = 0; i < 100 && savingRef.current; i++) {
      await new Promise((r) => window.setTimeout(r, 40));
    }
  }

  function updateMeta(partial: Partial<Meta>) {
    setMeta((m) => (m ? { ...m, ...partial } : m));
    touch();
  }

  function onEditorChange() {
    const m = metaRef.current;
    if (!m?.note_id || m.source !== "manual") return;
    // 로컬 드래프트 즉시 기록(자동저장 실패/창 닫힘에도 작성분 유지)
    if (editorRef.current) writeBodyDraft(m.note_id, editorRef.current.getBlocks());
    touch();
  }

  // 자동저장(+명시 저장). force=true면 dirty 아니어도 저장한다.
  async function runSave(force: boolean): Promise<boolean> {
    const m = metaRef.current;
    if (!m?.note_id) return false;
    if (!force && !dirtyRef.current) return true;
    // 이미 저장이 진행 중이면 새 PUT를 겹치지 않는다(순서 뒤바뀐 덮어쓰기 방지).
    // dirty를 유지하고 재예약해, 진행 중 저장이 끝난 뒤 최신본을 다시 저장한다.
    if (savingRef.current) {
      dirtyRef.current = true;
      scheduleAutosave();
      return false;
    }
    savingRef.current = true;
    const gen = editSeqRef.current; // 저장 시작 시점 편집 세대
    cancelAutosave();
    setSaveState("saving");
    try {
      const bodyStr = serializeBody();
      const input = currentInput(bodyStr);
      await api.updateMeetingNote(m.note_id, input);
      setNotes((rows) => rows.map((r) => (r.note_id === m.note_id ? { ...r, ...input } : r)));
      if (editSeqRef.current === gen) {
        // 저장하는 동안 새 편집이 없었으면 clean 처리.
        dirtyRef.current = false;
        clearBodyDraft(m.note_id);
        setSaveState("saved");
      } else {
        // 저장 중 새 편집이 들어왔다 → dirty·드래프트 유지, finally에서 재저장 예약.
        setSaveState("dirty");
      }
      return true;
    } catch (e) {
      setSaveState("error"); // 드래프트는 남겨 복구 가능
      if (force) setError(e instanceof Error ? e.message : "저장 실패");
      return false;
    } finally {
      savingRef.current = false;
      if (dirtyRef.current) scheduleAutosave(); // 대기 중 편집분 flush
    }
  }

  function openEdit(n: MeetingNote) {
    cancelAutosave();
    dirtyRef.current = false;
    setSaveState("idle");
    setError(null);
    setMeta({
      note_id: n.note_id,
      title: n.title,
      project_id: n.project_id ?? "",
      partner_id: n.partner_id ?? "",
      meeting_kind: n.meeting_kind,
      meeting_date: n.meeting_date,
      attendee_ids: n.attendee_ids ?? [],
      source: n.source,
    });
    setAttachments(n.attachments ?? []);
    setUploadPct(0);
    // 마지막 세션이 저장 못 하고 닫힌 흔적(드래프트)이 남아 있으면 복구를 제안한다.
    setDraftBlocks(n.source === "manual" ? readBodyDraft(n.note_id) : null);
    setBodyBlocks(parseBody(n.body));
    setEditorKey((k) => k + 1);
  }

  async function closeEditor() {
    // 닫기 전 대기 중 변경분을 확실히 저장(진행 중 저장을 기다린 뒤 강제 flush).
    cancelAutosave();
    await waitForSaveIdle();
    if (dirtyRef.current && metaRef.current?.note_id) await runSave(true);
    cancelAutosave();
    setDraftBlocks(null);
    setMeta(null);
    setBodyBlocks(null);
    setSaveState("idle");
    dirtyRef.current = false;
  }

  function restoreDraft() {
    if (!draftBlocks) return;
    setBodyBlocks(draftBlocks);
    setEditorKey((k) => k + 1);
    // 복구된 본문은 에디터 remount라 onChange가 안 뜬다 → 직접 dirty + 저장 예약.
    editSeqRef.current += 1;
    dirtyRef.current = true;
    setSaveState("dirty");
    scheduleAutosave();
    setDraftBlocks(null);
  }
  function dismissDraft() {
    if (metaRef.current?.note_id) clearBodyDraft(metaRef.current.note_id);
    setDraftBlocks(null);
  }

  function toggleAttendee(id: string) {
    setMeta((m) =>
      m
        ? {
            ...m,
            attendee_ids: m.attendee_ids.includes(id)
              ? m.attendee_ids.filter((a) => a !== id)
              : [...m.attendee_ids, id],
          }
        : m,
    );
    touch();
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await waitForSaveIdle(); // 진행 중 자동저장과 겹치지 않게
      await runSave(true);
    } finally {
      setBusy(false);
    }
  }

  async function onPickAttachments(files: FileList | null) {
    const m = metaRef.current;
    if (!files || !m?.note_id || uploading) return;
    const list = Array.from(files);
    for (const file of list) {
      setUploading(true);
      setUploadPct(0);
      try {
        const att = await api.uploadMeetingAttachment(m.note_id, file, {
          onProgress: (p: UploadProgress) =>
            setUploadPct(p.total ? Math.round((p.loaded / p.total) * 100) : 0),
        });
        setAttachments((prev) => [...prev, att]);
        setNotes((rows) =>
          rows.map((r) =>
            r.note_id === m.note_id
              ? { ...r, attachments: [...(r.attachments ?? []), att] }
              : r,
          ),
        );
      } catch (e) {
        window.alert(e instanceof Error ? e.message : "첨부 업로드 실패");
      } finally {
        setUploading(false);
      }
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function removeAttachment(a: MeetingAttachment) {
    const m = metaRef.current;
    if (!m?.note_id) return;
    if (!window.confirm(`'${a.filename}' 첨부를 삭제할까요?`)) return;
    try {
      await api.deleteMeetingAttachment(m.note_id, a.attachment_id);
      setAttachments((prev) => prev.filter((x) => x.attachment_id !== a.attachment_id));
      setNotes((rows) =>
        rows.map((r) =>
          r.note_id === m.note_id
            ? { ...r, attachments: (r.attachments ?? []).filter((x) => x.attachment_id !== a.attachment_id) }
            : r,
        ),
      );
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "첨부 삭제 실패");
    }
  }

  async function remove() {
    if (!meta?.note_id) return;
    if (!window.confirm("삭제하시겠습니까?")) return;
    setBusy(true);
    try {
      cancelAutosave();
      dirtyRef.current = false;
      clearBodyDraft(meta.note_id);
      await api.deleteMeetingNote(meta.note_id);
      setMeta(null);
      setBodyBlocks(null);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "삭제 실패");
    } finally {
      setBusy(false);
    }
  }

  // 미저장 변경이 있으면 창/탭 닫기·새로고침 시 경고.
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  /* ----------------------------- edit view ----------------------------- */
  if (meta) {
    const isAuto = meta.source !== "manual";
    return (
      <div className="mx-auto w-full max-w-[860px] px-3 py-4 sm:px-5">
        <ChannelHeader
          glyph="#"
          title={meta.note_id ? "회의록" : "새 회의록"}
          subtitle="제목·프로젝트·회사·날짜·참석자를 채우고 본문은 자유롭게 작성하세요."
          right={
            <div className="flex items-center gap-2">
              {!isAuto ? <SaveIndicator state={saveState} /> : null}
              <ToolbarButton onClick={() => void closeEditor()} disabled={busy}>
                목록
              </ToolbarButton>
              {meta.note_id && !isAuto ? (
                <ToolbarButton onClick={remove} disabled={busy}>
                  삭제
                </ToolbarButton>
              ) : null}
              {!isAuto ? (
                <ToolbarButton
                  tone="accent"
                  onClick={save}
                  disabled={busy || saveState === "saving" || !meta.title.trim()}
                >
                  {busy || saveState === "saving" ? "저장 중…" : "저장"}
                </ToolbarButton>
              ) : null}
            </div>
          }
        />

        {error ? (
          <div className="mb-3">
            <Banner tone="fail" title="오류">
              {error}
            </Banner>
          </div>
        ) : null}
        {isAuto ? (
          <div className="mb-3">
            <Banner tone="warn" title="자동 생성">
              계획·회고에서 자동 생성된 내부 회의입니다. 내용은 계획·회고 탭에서 수정하세요.
            </Banner>
          </div>
        ) : null}
        {draftBlocks ? (
          <div className="mb-3">
            <Banner tone="warn" title="저장되지 않은 작성분 발견">
              <div className="flex flex-col gap-2">
                <span>
                  마지막에 저장하지 못하고 닫힌 본문이 이 기기에 남아 있습니다. 복구할까요?
                </span>
                <div className="flex gap-2">
                  <ToolbarButton tone="accent" onClick={restoreDraft}>
                    복구
                  </ToolbarButton>
                  <ToolbarButton onClick={dismissDraft}>버림</ToolbarButton>
                </div>
              </div>
            </Banner>
          </div>
        ) : null}

        <Card className="mb-3 p-3 sm:p-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="제목" full>
              <TextInput
                value={meta.title}
                onChange={(e) => updateMeta({ title: e.target.value })}
                placeholder="회의 제목"
                disabled={isAuto}
              />
            </Field>
            <Field label="종류">
              <div className="flex gap-1">
                {(["internal", "external"] as Kind[]).map((k) => (
                  <button
                    key={k}
                    type="button"
                    disabled={isAuto}
                    onClick={() => updateMeta({ meeting_kind: k })}
                    className="flex-1 rounded-[6px] px-2 text-[var(--text-body-sm)]"
                    style={{
                      minHeight: 38,
                      border: `1px solid ${meta.meeting_kind === k ? "var(--color-text-strong)" : "var(--color-divider)"}`,
                      background:
                        meta.meeting_kind === k
                          ? "var(--color-sidebar-active)"
                          : "var(--color-card)",
                      color: "var(--color-text-strong)",
                      fontWeight: meta.meeting_kind === k ? 700 : 500,
                    }}
                  >
                    {k === "internal" ? "내부 회의" : "외부 회의"}
                  </button>
                ))}
              </div>
            </Field>
            <Field label="프로젝트">
              <Select
                value={meta.project_id}
                disabled={isAuto}
                options={[
                  { value: "", label: "— 없음 —" },
                  ...projects.map((p) => ({ value: p.project_id, label: p.name })),
                ]}
                onChange={(e) => updateMeta({ project_id: e.target.value })}
              />
            </Field>
            {meta.meeting_kind === "external" ? (
              <Field label="회사 (파트너사)">
                <Select
                  value={meta.partner_id}
                  options={[
                    { value: "", label: "— 없음 —" },
                    ...partners.map((p) => ({ value: p.partner_id, label: p.name })),
                  ]}
                  onChange={(e) => updateMeta({ partner_id: e.target.value })}
                />
              </Field>
            ) : null}
            <Field label="날짜">
              <DatePicker
                value={meta.meeting_date}
                clearable={false}
                onChange={(v) => updateMeta({ meeting_date: v })}
              />
            </Field>
            <Field label="참석자 (복수 선택)" full>
              <div className="flex flex-wrap gap-1.5">
                {members.length === 0 ? (
                  <span style={labelStyle()}>등록된 팀원이 없습니다.</span>
                ) : (
                  members.map((m) => {
                    const on = meta.attendee_ids.includes(m.member_id);
                    return (
                      <button
                        key={m.member_id}
                        type="button"
                        disabled={isAuto}
                        onClick={() => toggleAttendee(m.member_id)}
                        className="flex items-center gap-1.5 rounded-[6px] px-2 py-1 text-[var(--text-body-sm)]"
                        style={{
                          border: `1px solid ${on ? "var(--color-text-strong)" : "var(--color-divider)"}`,
                          background: on ? "var(--color-sidebar-active)" : "var(--color-card)",
                          color: "var(--color-text-strong)",
                          fontWeight: on ? 700 : 500,
                        }}
                      >
                        <Avatar name={m.name} color={m.color || undefined} size="xs" />
                        {m.name}
                      </button>
                    );
                  })
                )}
              </div>
            </Field>
          </div>
        </Card>

        {meta.note_id && !isAuto ? (
          <Card className="mb-3 p-3 sm:p-4">
            <div className="mb-2 flex items-center justify-between gap-2">
              <p className="text-[var(--text-meta)] font-semibold" style={labelStyle()}>
                미팅 자료 (PDF·이미지·문서 첨부)
              </p>
              <div className="flex items-center gap-2">
                {uploading ? (
                  <span className="text-[var(--text-meta)]" style={labelStyle()}>
                    업로드 중… {uploadPct}%
                  </span>
                ) : null}
                <ToolbarButton
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading}
                >
                  파일 추가
                </ToolbarButton>
              </div>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => void onPickAttachments(e.target.files)}
            />
            {attachments.length === 0 ? (
              <p className="text-[var(--text-meta)]" style={labelStyle()}>
                아직 첨부한 자료가 없습니다. PDF·이미지 등 회의 자료를 올려 두면 함께 보관됩니다.
              </p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {attachments.map((a) => (
                  <li
                    key={a.attachment_id}
                    className="flex items-center gap-2 rounded-[6px] px-2 py-1.5"
                    style={{
                      border: "1px solid var(--color-divider)",
                      background: "var(--color-card)",
                    }}
                  >
                    <span aria-hidden>📎</span>
                    <a
                      href={mediaHref(a.url)}
                      target="_blank"
                      rel="noreferrer"
                      className="min-w-0 flex-1 truncate font-medium hover:underline"
                      style={{ color: "var(--color-text-strong)" }}
                      title={a.filename}
                    >
                      {a.filename}
                    </a>
                    {a.size_bytes ? (
                      <span
                        className="whitespace-nowrap text-[var(--text-meta)]"
                        style={labelStyle()}
                      >
                        {formatBytes(a.size_bytes)}
                      </span>
                    ) : null}
                    <button
                      type="button"
                      onClick={() => void removeAttachment(a)}
                      className="text-[var(--text-meta)]"
                      style={{ color: "var(--color-fail-fg)" }}
                    >
                      삭제
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        ) : null}

        <Card className="p-2 sm:p-3">
          <p className="px-1 pb-1 text-[var(--text-meta)] font-semibold" style={labelStyle()}>
            본문 — “/” 를 입력하면 제목·목록·이미지·표 등 블록을 추가할 수 있습니다.
          </p>
          <BlockNoteEditor
            key={editorKey}
            docId={meta.note_id ?? ""}
            initialBlocks={bodyBlocks}
            handleRef={editorRef}
            uploadFile={uploadAsDataUrl}
            onChange={isAuto ? undefined : onEditorChange}
          />
        </Card>
      </div>
    );
  }

  /* ----------------------------- list view ----------------------------- */
  const external = tab === "external";
  // 프로젝트 컬럼은 내부·외부 모두 노출, 회사 컬럼은 외부 전용 → 양쪽 6칸.
  const cols = 6;
  const memberEntities = members.map((m) => ({
    id: m.member_id,
    name: m.name,
    color: m.color,
  }));
  const projectEntities = projects.map((p) => ({
    id: p.project_id,
    name: p.name,
    color: p.color,
  }));
  const partnerEntities = partners.map((p) => ({ id: p.partner_id, name: p.name }));

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <ChannelHeader
        glyph="#"
        title="회의록"
        subtitle="내부/외부 회의를 노션 DB처럼 관리합니다. 셀을 바로 눌러 수정하고, 계획·회고는 주간/월간 내부 회의로 자동 정리됩니다."
      />

      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">
            {error}
          </Banner>
        </div>
      ) : null}

      {/* internal / external tabs */}
      <div className="mb-3 flex items-center gap-2">
        {(["internal", "external"] as Kind[]).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setTab(k)}
            className={cx("rounded-[8px] px-3 text-[var(--text-body-sm)]")}
            style={{
              minHeight: 38,
              border: `1px solid ${tab === k ? "var(--color-text-strong)" : "var(--color-divider)"}`,
              background: tab === k ? "var(--color-text-strong)" : "var(--color-card)",
              color: tab === k ? "var(--color-card)" : "var(--color-text-body)",
              fontWeight: tab === k ? 700 : 500,
            }}
          >
            {k === "internal" ? "내부 회의" : "외부 회의"}
          </button>
        ))}
        <div className="ml-auto">
          <Select
            aria-label="프로젝트 필터"
            value={projectFilter}
            options={[
              { value: "", label: "전체 프로젝트" },
              ...projects.map((p) => ({ value: p.project_id, label: p.name })),
            ]}
            onChange={(e) => setProjectFilter(e.target.value)}
            style={{ minHeight: 38, width: "auto" }}
          />
        </div>
      </div>

      <div
        className="overflow-x-auto rounded-[8px]"
        style={{ border: "1px solid var(--color-divider)" }}
      >
        <table className="w-full border-collapse text-[var(--text-body-sm)]">
          <thead>
            <tr
              style={{
                background: "var(--color-app-canvas)",
                borderBottom: "1px solid var(--color-divider)",
              }}
            >
              {!external ? <th className="w-8 px-1" /> : null}
              {[
                "제목",
                "프로젝트",
                ...(external ? ["회사"] : []),
                "날짜",
                "참석자",
              ].map((h) => (
                <th
                  key={h}
                  className="px-3 py-2 text-left font-medium whitespace-nowrap"
                  style={labelStyle()}
                >
                  {h}
                </th>
              ))}
              <th className="w-px px-2" />
            </tr>
          </thead>
          <tbody>
            {filtered.map((n) => {
              const auto = n.source !== "manual";
              const isOpen = expanded.has(n.note_id);
              return (
                <Fragment key={n.note_id}>
                  <tr
                    className="group hover:bg-[var(--color-app-canvas)]"
                    style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
                  >
                    {!external ? (
                      <td className="px-1 align-middle">
                        {auto ? (
                          <button
                            type="button"
                            onClick={() => toggleExpand(n.note_id)}
                            className="flex h-7 w-7 items-center justify-center rounded-[6px] hover:bg-[var(--color-card)]"
                            style={{ color: "var(--color-text-muted)" }}
                            aria-label="펼치기"
                          >
                            {isOpen ? "▾" : "▸"}
                          </button>
                        ) : null}
                      </td>
                    ) : null}
                    {/* title */}
                    <td className="px-3 py-2 align-middle">
                      <div className="flex items-center gap-1.5">
                        {auto ? (
                          <button
                            type="button"
                            onClick={() => toggleExpand(n.note_id)}
                            className="flex items-center gap-1.5 text-left font-medium"
                            style={{ color: "var(--color-text-strong)" }}
                          >
                            {n.title}
                            <span
                              className="rounded-[4px] px-1.5 py-[1px] text-[var(--text-badge)]"
                              style={{ background: "#fdecc8", color: "#604b28" }}
                            >
                              계획·회고
                            </span>
                          </button>
                        ) : (
                          <InlineText
                            value={n.title}
                            strong
                            placeholder="(제목 없음)"
                            onSave={(v) => patchNote(n, { title: v || n.title })}
                          />
                        )}
                        {(n.attachments?.length ?? 0) > 0 ? (
                          <span
                            className="whitespace-nowrap rounded-[4px] px-1.5 py-[1px] text-[var(--text-badge)]"
                            style={{
                              background: "var(--color-app-canvas)",
                              color: "var(--color-text-muted)",
                              border: "1px solid var(--color-divider)",
                            }}
                            title={`첨부 ${n.attachments.length}개`}
                          >
                            📎 {n.attachments.length}
                          </span>
                        ) : null}
                      </div>
                    </td>
                    {/* 프로젝트 — 내부·외부 공통 */}
                    <td className="px-3 py-2 align-middle whitespace-nowrap">
                      {auto ? (
                        projectName(n.project_id)
                      ) : (
                        <InlineSelect
                          value={n.project_id}
                          options={projectEntities}
                          onChange={(id) => patchNote(n, { project_id: id })}
                        />
                      )}
                    </td>
                    {/* 회사 — 외부 전용 */}
                    {external ? (
                      <td className="px-3 py-2 align-middle whitespace-nowrap">
                        {auto ? (
                          n.partner_id ? partnerName(n.partner_id) : "—"
                        ) : (
                          <InlineSelect
                            value={n.partner_id}
                            options={partnerEntities}
                            placeholder="회사 선택"
                            onChange={(id) => patchNote(n, { partner_id: id })}
                          />
                        )}
                      </td>
                    ) : null}
                    {/* date */}
                    <td className="px-3 py-2 align-middle whitespace-nowrap">
                      {auto ? (
                        <span style={{ color: "var(--color-text-body)" }}>
                          {n.meeting_date}
                        </span>
                      ) : (
                        <div style={{ width: 150 }}>
                          <DatePicker
                            value={n.meeting_date}
                            clearable={false}
                            onChange={(v) => v && patchNote(n, { meeting_date: v })}
                          />
                        </div>
                      )}
                    </td>
                    {/* attendees */}
                    <td className="px-3 py-2 align-middle">
                      {auto ? (
                        attendeeMembers(n.attendee_ids).length ? (
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                            {attendeeMembers(n.attendee_ids).map((m) => (
                              <span
                                key={m.member_id}
                                className="inline-flex items-center gap-1"
                                style={{ color: "var(--color-text-body)" }}
                              >
                                <Avatar name={m.name} color={m.color || undefined} size="sm" />
                                {m.name}
                              </span>
                            ))}
                          </div>
                        ) : (
                          <span style={{ color: "var(--color-text-body)" }}>—</span>
                        )
                      ) : (
                        <InlineMembers
                          members={memberEntities}
                          selected={n.attendee_ids}
                          onChange={(ids) => patchNote(n, { attendee_ids: ids })}
                        />
                      )}
                    </td>
                    {/* body / actions */}
                    <td className="px-2 py-2 text-right whitespace-nowrap">
                      {auto ? null : (
                        <>
                          <button
                            onClick={() => openEdit(n)}
                            className="mr-2 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                            style={{ color: "var(--color-text-muted)" }}
                          >
                            본문
                          </button>
                          <button
                            onClick={async () => {
                              if (!window.confirm("삭제하시겠습니까?")) return;
                              await api.deleteMeetingNote(n.note_id);
                              await reload();
                            }}
                            className="text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                            style={{ color: "var(--color-fail-fg)" }}
                          >
                            삭제
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                  {auto && isOpen ? (
                    <tr style={{ borderBottom: "1px solid var(--color-divider-soft)" }}>
                      <td />
                      <td colSpan={cols - 1} className="px-3 pb-3">
                        <MeetingBody body={n.body} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
            {/* inline add row (manual meetings only) */}
            <tr>
              {!external ? <td className="px-1" /> : null}
              <td colSpan={cols - (external ? 1 : 2)} className="px-3 py-2">
                <input
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") addInline();
                  }}
                  onBlur={addInline}
                  placeholder={
                    external ? "+ 외부 회의 추가 (제목 입력 후 Enter)" : "+ 내부 회의 추가 (제목 입력 후 Enter)"
                  }
                  className="w-full rounded-[6px] px-2 py-1.5 text-[var(--text-body-sm)] outline-none"
                  style={{ background: "transparent", color: "var(--color-text-body)" }}
                />
              </td>
              <td />
            </tr>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={cols} className="px-3 pb-6 text-center" style={labelStyle()}>
                  {tab === "internal"
                    ? "내부 회의가 없습니다. 계획·회고를 저장하면 자동으로 정리됩니다."
                    : "외부 회의가 없습니다. 위 입력칸에서 바로 추가하세요."}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ------------------------- autosave status indicator ------------------------- */
function SaveIndicator({ state }: { state: SaveState }) {
  const map: Record<SaveState, { text: string; color: string } | null> = {
    idle: null,
    dirty: { text: "변경됨 · 자동 저장 대기", color: "var(--color-text-muted)" },
    saving: { text: "저장 중…", color: "var(--color-text-muted)" },
    saved: { text: "저장됨", color: "var(--color-ok-fg, var(--color-text-muted))" },
    error: { text: "저장 실패 · 재시도 필요", color: "var(--color-fail-fg)" },
  };
  const cfg = map[state];
  if (!cfg) return null;
  return (
    <span
      className="whitespace-nowrap text-[var(--text-meta)]"
      style={{ color: cfg.color }}
    >
      {cfg.text}
    </span>
  );
}

/* ------------------- aggregated plan·retro body renderer ------------------- */
function MeetingBody({ body }: { body: string | null }) {
  if (!body) {
    return (
      <span className="text-[var(--text-meta)]" style={labelStyle()}>
        내용 없음
      </span>
    );
  }
  const lines = body.split("\n");
  return (
    <div className="flex flex-col gap-0.5 pl-1">
      {lines.map((raw, i) => {
        const line = raw.trimEnd();
        if (line.startsWith("## ")) {
          return (
            <div
              key={i}
              className="mt-2 text-[var(--text-body-sm)] font-bold"
              style={{ color: "var(--color-text-strong)" }}
            >
              {line.slice(3)}
            </div>
          );
        }
        if (line.startsWith("### ")) {
          return (
            <div
              key={i}
              className="mt-1 text-[var(--text-meta)] font-semibold"
              style={{ color: "var(--color-text-muted)" }}
            >
              {line.slice(4)}
            </div>
          );
        }
        const m = line.match(/^- (\[[ x]\] )?(.*)$/);
        if (m) {
          const checked = m[1] === "[x] ";
          return (
            <div
              key={i}
              className="flex items-start gap-1.5 pl-2 text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-body)" }}
            >
              {m[1] ? <span>{checked ? "☑" : "☐"}</span> : <span>·</span>}
              <span style={checked ? { color: "var(--color-text-muted)" } : undefined}>
                {m[2]}
              </span>
            </div>
          );
        }
        if (!line) return <div key={i} className="h-1" />;
        return (
          <div
            key={i}
            className="text-[var(--text-body-sm)]"
            style={{ color: "var(--color-text-body)" }}
          >
            {line}
          </div>
        );
      })}
    </div>
  );
}
