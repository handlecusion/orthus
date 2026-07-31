"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  api,
  type RecruitingCandidate,
  type RecruitingComment,
  type TeamMember,
} from "@/lib/api";
import { toExternalHref, displayUrl } from "@/lib/external-url";
import { PageHeader, Banner, ToolbarButton } from "@/components/ui";
import {
  Field,
  Modal,
  Section,
  Select,
  TextArea,
  TextInput,
  labelStyle,
} from "@/components/dashboard/kit";

function orNull(v: unknown): string | null {
  const s = String(v ?? "").trim();
  return s === "" ? null : s;
}

const SEND_STATUSES = ["컨택전", "컨택중", "프로젝트", "보류", "취소"] as const;
const STATUS_COLOR: Record<string, { bg: string; fg: string }> = {
  컨택전: { bg: "#ececee", fg: "#6b6c70" },
  컨택중: { bg: "#dce8fb", fg: "#2d5d96" },
  프로젝트: { bg: "#d7efdd", fg: "#1f7a4d" },
  보류: { bg: "#fbeccd", fg: "#8a5a13" },
  취소: { bg: "#f6dada", fg: "#a23b3b" },
};
function statusColor(s: string) {
  return STATUS_COLOR[s] ?? STATUS_COLOR["컨택전"];
}

type CandidateDraft = {
  name: string;
  role: string;
  education: string;
  phone: string;
  email: string;
  linkedin: string;
  send_status: string;
  note: string;
};

const EMPTY: CandidateDraft = {
  name: "",
  role: "",
  education: "",
  phone: "",
  email: "",
  linkedin: "",
  send_status: "컨택전",
  note: "",
};

export default function RecruitingPage() {
  const [candidates, setCandidates] = useState<RecruitingCandidate[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [members, setMembers] = useState<TeamMember[]>([]);
  const [open, setOpen] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState<CandidateDraft>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [modalErr, setModalErr] = useState<string | null>(null);

  // 후보별 팀원 코멘트
  const [comments, setComments] = useState<RecruitingComment[]>([]);
  const [commentAuthor, setCommentAuthor] = useState("");
  const [commentBody, setCommentBody] = useState("");
  const [commentBusy, setCommentBusy] = useState(false);

  const reload = useCallback(async () => {
    setCandidates(await api.listRecruitingCandidates());
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([api.listRecruitingCandidates(), api.listTeamMembers()])
      .then(([cd, mb]) => {
        if (!alive) return;
        setCandidates(cd);
        setMembers(mb);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
      });
    return () => {
      alive = false;
    };
  }, []);

  function openCreate() {
    setEditId(null);
    setDraft(EMPTY);
    setComments([]);
    setCommentBody("");
    setModalErr(null);
    setOpen(true);
  }
  function openEdit(c: RecruitingCandidate) {
    setEditId(c.candidate_id);
    setDraft({
      name: c.name,
      role: c.role ?? "",
      education: c.education ?? "",
      phone: c.phone ?? "",
      email: c.email ?? "",
      linkedin: c.linkedin ?? "",
      send_status: c.send_status ?? "컨택전",
      note: c.note ?? "",
    });
    setComments([]);
    setCommentBody("");
    setModalErr(null);
    setOpen(true);
    void api
      .listRecruitingComments(c.candidate_id)
      .then(setComments)
      .catch(() => setComments([]));
  }

  async function addComment() {
    if (!editId) return;
    const author = commentAuthor.trim();
    const body = commentBody.trim();
    if (!author || !body) return;
    setCommentBusy(true);
    try {
      const created = await api.createRecruitingComment(editId, {
        author_member_id: members.find((m) => m.name === author)?.member_id ?? null,
        author_name: author,
        body,
      });
      setComments((cur) => [...cur, created]);
      setCommentBody("");
    } catch (e) {
      setModalErr(e instanceof Error ? e.message : "코멘트 저장 실패");
    } finally {
      setCommentBusy(false);
    }
  }

  async function removeComment(commentId: string) {
    if (!editId) return;
    try {
      await api.deleteRecruitingComment(editId, commentId);
      setComments((cur) => cur.filter((c) => c.comment_id !== commentId));
    } catch (e) {
      setModalErr(e instanceof Error ? e.message : "코멘트 삭제 실패");
    }
  }

  function toInput(d: CandidateDraft) {
    return {
      name: d.name,
      role: orNull(d.role),
      education: orNull(d.education),
      phone: orNull(d.phone),
      email: orNull(d.email),
      linkedin: orNull(d.linkedin),
      send_status: d.send_status || "컨택전",
      note: orNull(d.note),
      sort_order: 0,
    };
  }

  async function save() {
    setBusy(true);
    setModalErr(null);
    try {
      const input = toInput(draft);
      if (editId) await api.updateRecruitingCandidate(editId, input as never);
      else await api.createRecruitingCandidate(input as never);
      await reload();
      setOpen(false);
    } catch (e) {
      setModalErr(e instanceof Error ? e.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    if (!window.confirm("이 후보를 삭제하시겠습니까?")) return;
    try {
      await api.deleteRecruitingCandidate(id);
      await reload();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "삭제 실패");
    }
  }

  // 상태만 빠르게 바꾸는 인라인 변경(노션 select처럼).
  async function quickStatus(c: RecruitingCandidate, status: string) {
    setCandidates((cur) =>
      cur.map((x) => (x.candidate_id === c.candidate_id ? { ...x, send_status: status } : x)),
    );
    try {
      const { candidate_id: _omit, ...rest } = c;
      void _omit;
      await api.updateRecruitingCandidate(c.candidate_id, {
        ...rest,
        send_status: status,
      } as never);
    } catch (e) {
      setError(e instanceof Error ? e.message : "상태 변경 실패");
      await reload();
    }
  }

  const headers = [
    "이름",
    "분야 · 포지션",
    "학력(전공)",
    "전화번호",
    "이메일",
    "링크드인",
    "상태",
    "메모",
  ];

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <div className="mb-1">
        <Link
          href="/dashboard/team"
          className="text-[var(--text-meta)]"
          style={labelStyle()}
        >
          ← 팀
        </Link>
      </div>
      <PageHeader
        title="인재영입"
        subtitle="앞으로 영입하거나 킵인터치할 후보 리스트. 상태로 콜드메일 진행을 트래킹합니다."
      />
      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">
            {error}
          </Banner>
        </div>
      ) : null}

      <Section
        title="후보"
        subtitle={`${candidates.length}명`}
        action={
          <ToolbarButton tone="primary" onClick={openCreate}>
            + 후보 추가
          </ToolbarButton>
        }
      >
        {candidates.length === 0 ? (
          <p
            className="py-6 text-center text-[var(--text-body-sm)]"
            style={labelStyle()}
          >
            아직 후보가 없습니다. 상태·정리 메모와 함께 후보를 등록해 보세요.
          </p>
        ) : (
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
                  {headers.map((h) => (
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
                {candidates.map((c) => {
                  const sc = statusColor(c.send_status);
                  return (
                    <tr
                      key={c.candidate_id}
                      className="group hover:bg-[var(--color-app-canvas)]"
                      style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
                    >
                      <td
                        className="cursor-pointer px-3 py-2.5 align-middle whitespace-nowrap"
                        onClick={() => openEdit(c)}
                        style={{ color: "var(--color-text-strong)", fontWeight: 600 }}
                      >
                        {c.name}
                      </td>
                      <td
                        className="cursor-pointer px-3 py-2.5 align-middle"
                        style={{ color: "var(--color-text-body)", maxWidth: 200 }}
                        onClick={() => openEdit(c)}
                      >
                        <span className="block truncate">{c.role ?? "—"}</span>
                      </td>
                      <td
                        className="cursor-pointer px-3 py-2.5 align-middle"
                        style={{ color: "var(--color-text-body)", maxWidth: 200 }}
                        onClick={() => openEdit(c)}
                      >
                        <span className="block truncate">{c.education ?? "—"}</span>
                      </td>
                      <td
                        className="cursor-pointer px-3 py-2.5 align-middle whitespace-nowrap"
                        style={{ color: "var(--color-text-body)" }}
                        onClick={() => openEdit(c)}
                      >
                        {c.phone ?? "—"}
                      </td>
                      <td
                        className="cursor-pointer px-3 py-2.5 align-middle"
                        style={{ color: "var(--color-text-muted)", maxWidth: 200 }}
                        onClick={() => openEdit(c)}
                      >
                        <span className="block truncate">{c.email ?? "—"}</span>
                      </td>
                      <td className="px-3 py-2.5 align-middle" style={{ maxWidth: 170 }}>
                        {toExternalHref(c.linkedin) ? (
                          <a
                            href={toExternalHref(c.linkedin)!}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="block truncate underline"
                            style={{ color: "var(--color-progress)" }}
                          >
                            {displayUrl(c.linkedin)}
                          </a>
                        ) : (
                          <span style={labelStyle()}>—</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5 align-middle whitespace-nowrap">
                        <select
                          value={c.send_status}
                          onChange={(e) => void quickStatus(c, e.target.value)}
                          className="cursor-pointer rounded-[5px] border-0 px-2 py-1 text-[var(--text-meta)] font-semibold outline-none"
                          style={{ background: sc.bg, color: sc.fg }}
                        >
                          {SEND_STATUSES.map((s) => (
                            <option key={s} value={s}>
                              {s}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td
                        className="cursor-pointer px-3 py-2.5 align-middle"
                        style={{ color: "var(--color-text-muted)", maxWidth: 260 }}
                        onClick={() => openEdit(c)}
                      >
                        <span className="block truncate">{c.note ?? "—"}</span>
                      </td>
                      <td className="px-2 py-2.5 text-right whitespace-nowrap">
                        <button
                          onClick={() => openEdit(c)}
                          className="mr-2 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                          style={{ color: "var(--color-text-muted)" }}
                        >
                          열기
                        </button>
                        <button
                          onClick={() => remove(c.candidate_id)}
                          className="text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                          style={{ color: "var(--color-fail-fg)" }}
                        >
                          삭제
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Modal
        open={open}
        title={editId ? "후보 상세" : "후보 추가"}
        onClose={() => setOpen(false)}
        footer={
          <>
            {editId ? (
              <ToolbarButton
                onClick={() => {
                  setOpen(false);
                  void remove(editId);
                }}
                disabled={busy}
              >
                삭제
              </ToolbarButton>
            ) : null}
            <ToolbarButton onClick={() => setOpen(false)} disabled={busy}>
              닫기
            </ToolbarButton>
            <ToolbarButton tone="primary" onClick={save} disabled={busy}>
              {busy ? "저장 중…" : "저장"}
            </ToolbarButton>
          </>
        }
      >
        {modalErr ? (
          <p
            className="mb-3 text-[var(--text-body-sm)]"
            style={{ color: "var(--color-fail-fg)" }}
          >
            {modalErr}
          </p>
        ) : null}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="이름">
            <TextInput
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </Field>
          <Field label="상태">
            <Select
              value={draft.send_status}
              options={SEND_STATUSES.map((s) => ({ value: s, label: s }))}
              onChange={(e) => setDraft({ ...draft, send_status: e.target.value })}
            />
          </Field>
          <Field label="분야 · 포지션">
            <TextInput
              value={draft.role}
              placeholder="예: 비디오 생성 모델 연구"
              onChange={(e) => setDraft({ ...draft, role: e.target.value })}
            />
          </Field>
          <Field label="학력 (전공)">
            <TextInput
              value={draft.education}
              placeholder="예: KAIST 전산학 박사"
              onChange={(e) => setDraft({ ...draft, education: e.target.value })}
            />
          </Field>
          <Field label="전화번호">
            <TextInput
              value={draft.phone}
              placeholder="예: 010-1234-5678"
              onChange={(e) => setDraft({ ...draft, phone: e.target.value })}
            />
          </Field>
          <Field label="이메일">
            <TextInput
              value={draft.email}
              onChange={(e) => setDraft({ ...draft, email: e.target.value })}
            />
          </Field>
          <Field label="링크드인">
            <TextInput
              value={draft.linkedin}
              placeholder="https://www.linkedin.com/in/..."
              onChange={(e) => setDraft({ ...draft, linkedin: e.target.value })}
            />
          </Field>
          <Field label="메모 (정리 · 논문/작업물/프로필 링크)" full>
            <TextArea
              value={draft.note}
              placeholder="경력·강점·관심사·논문/작업물/프로필 링크·연락 맥락 등 그 사람에 대한 정리"
              onChange={(e) => setDraft({ ...draft, note: e.target.value })}
              style={{ minHeight: 140 }}
            />
          </Field>
        </div>

        {/* 팀 코멘트 — 각 팀원이 메모를 남겨 영입 여부를 함께 정리한다 */}
        {editId ? (
          <div
            className="mt-4 pt-4"
            style={{ borderTop: "1px solid var(--color-divider-soft)" }}
          >
            <h3
              className="mb-2 text-[var(--text-body-sm)] font-bold"
              style={{ color: "var(--color-text-strong)" }}
            >
              팀 코멘트
              <span className="ml-1 font-normal" style={labelStyle()}>
                {comments.length}
              </span>
            </h3>

            {comments.length > 0 ? (
              <ul className="mb-3 flex flex-col gap-2">
                {comments.map((c) => (
                  <li
                    key={c.comment_id}
                    className="group rounded-[8px] px-3 py-2"
                    style={{
                      background: "var(--color-app-canvas)",
                      border: "1px solid var(--color-divider-soft)",
                    }}
                  >
                    <div className="mb-0.5 flex items-center justify-between gap-2">
                      <span
                        className="text-[var(--text-meta)] font-semibold"
                        style={{ color: "var(--color-text-strong)" }}
                      >
                        {c.author_name}
                      </span>
                      <span className="flex items-center gap-2">
                        <span className="text-[var(--text-meta)]" style={labelStyle()}>
                          {c.created_at.slice(0, 10)}
                        </span>
                        <button
                          onClick={() => void removeComment(c.comment_id)}
                          className="text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                          style={{ color: "var(--color-fail-fg)" }}
                        >
                          삭제
                        </button>
                      </span>
                    </div>
                    <p
                      className="text-[var(--text-body-sm)] whitespace-pre-wrap"
                      style={{ color: "var(--color-text-body)" }}
                    >
                      {c.body}
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mb-3 text-[var(--text-meta)]" style={labelStyle()}>
                아직 코멘트가 없습니다. 팀원별 의견을 남겨 보세요.
              </p>
            )}

            <div className="flex flex-col gap-2 sm:flex-row sm:items-start">
              <div className="sm:w-40 sm:shrink-0">
                <Select
                  value={commentAuthor}
                  options={[
                    { value: "", label: "작성자 선택" },
                    ...members.map((m) => ({ value: m.name, label: m.name })),
                  ]}
                  onChange={(e) => setCommentAuthor(e.target.value)}
                />
              </div>
              <TextInput
                value={commentBody}
                placeholder="이 후보에 대한 의견 / 영입 판단 메모"
                onChange={(e) => setCommentBody(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void addComment();
                  }
                }}
              />
              <ToolbarButton
                tone="primary"
                onClick={() => void addComment()}
                disabled={commentBusy || !commentAuthor.trim() || !commentBody.trim()}
              >
                남기기
              </ToolbarButton>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
