"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type DashboardProject,
  type ProjectAssignment,
  type TeamMember,
} from "@/lib/api";
import { ChannelHeader, Avatar, Banner, ToolbarButton } from "@/components/ui";
import {
  Field,
  Modal,
  Section,
  Select,
  TextArea,
  TextInput,
  labelStyle,
} from "@/components/dashboard/kit";
import { AssignmentChips } from "@/components/dashboard/assignments";

function orNull(v: unknown): string | null {
  const s = String(v ?? "").trim();
  return s === "" ? null : s;
}

// 여러 메일을 실제 메일 쓰듯 띄어쓰기/쉼표/세미콜론/줄바꿈 아무거로나 구분해도
// 개별 주소로 분리한다.
function parseEmails(raw: string | null | undefined): string[] {
  if (!raw) return [];
  return raw
    .split(/[\s,;]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function EmailChips({ value }: { value: string | null | undefined }) {
  const emails = useMemo(() => parseEmails(value), [value]);
  const [copied, setCopied] = useState<string | null>(null);

  const copy = useCallback(async (email: string) => {
    try {
      await navigator.clipboard.writeText(email);
      setCopied(email);
      window.setTimeout(
        () => setCopied((c) => (c === email ? null : c)),
        1200,
      );
    } catch {
      /* clipboard 접근 실패 시 무시 */
    }
  }, []);

  if (emails.length === 0) {
    return <span style={{ color: "var(--color-text-muted)" }}>—</span>;
  }

  return (
    <div className="flex flex-wrap gap-1">
      {emails.map((email) => (
        <button
          key={email}
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            void copy(email);
          }}
          title="클릭하면 복사됩니다"
          className="inline-flex max-w-full items-center gap-1 rounded-[4px] px-2 py-[2px] text-[var(--text-meta)] transition-colors hover:brightness-[0.97]"
          style={{
            background: "var(--color-app-canvas)",
            border: "1px solid var(--color-divider)",
            color: "var(--color-text-body)",
          }}
        >
          <span className="truncate">{email}</span>
          <span
            className="shrink-0 text-[var(--text-meta)]"
            style={{
              color:
                copied === email
                  ? "var(--color-progress)"
                  : "var(--color-text-muted)",
            }}
          >
            {copied === email ? "복사됨" : "복사"}
          </span>
        </button>
      ))}
    </div>
  );
}

/** 로그인 계정 연결 상태 배지. linked=계정 연결됨(보드·배정 이어짐),
 *  invited=로그인 가능(첫 로그인 시 자동 연결), 그 외=미연결. */
function LinkBadge({ member }: { member: TeamMember }) {
  const tone = member.linked
    ? {
        label: "연결됨",
        fg: "var(--color-progress)",
        bg: "color-mix(in srgb, var(--color-progress) 12%, transparent)",
        title: "로그인 계정과 연결됨 — 내 보드·배정이 이어집니다",
      }
    : member.invited
      ? {
          label: "초대됨",
          fg: "var(--color-text-body)",
          bg: "var(--color-app-canvas)",
          title: "로그인 가능 — 첫 로그인 시 자동으로 연결됩니다",
        }
      : {
          label: "미연결",
          fg: "var(--color-text-muted)",
          bg: "transparent",
          title: "이메일이 없거나 초대되지 않음 — 로그인/연결되지 않습니다",
        };
  return (
    <span
      title={tone.title}
      className="inline-flex items-center gap-1 rounded-full px-2 py-[2px] text-[var(--text-meta)] whitespace-nowrap"
      style={{ background: tone.bg, color: tone.fg, border: "1px solid var(--color-divider)" }}
    >
      <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: tone.fg }} />
      {tone.label}
    </span>
  );
}

type Draft = {
  name: string;
  email: string;
  phone: string;
  join_date: string;
  birthday: string;
  address: string;
  emergency_contact: string;
  bank_account: string;
  color: string;
  bio: string;
  active: boolean;
};

const EMPTY: Draft = {
  name: "",
  email: "",
  phone: "",
  join_date: "",
  birthday: "",
  address: "",
  emergency_contact: "",
  bank_account: "",
  color: "#5d9bd8",
  bio: "",
  active: true,
};

export default function TeamPage() {
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [assignments, setAssignments] = useState<ProjectAssignment[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [open, setOpen] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [modalErr, setModalErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [mb, pr, asg] = await Promise.all([
      api.listTeamMembers(),
      api.listDashboardProjects(),
      api.listAssignments(),
    ]);
    setMembers(mb);
    setProjects(pr);
    setAssignments(asg);
  }, []);

  const reloadAssignments = useCallback(async () => {
    setAssignments(await api.listAssignments());
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([
      api.listTeamMembers(),
      api.listDashboardProjects(),
      api.listAssignments(),
    ])
      .then(([mb, pr, asg]) => {
        if (!alive) return;
        setMembers(mb);
        setProjects(pr);
        setAssignments(asg);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
      });
    return () => {
      alive = false;
    };
  }, []);

  const projectOptions = useMemo(
    () => projects.map((p) => ({ id: p.project_id, name: p.name })),
    [projects],
  );
  const byMember = useMemo(() => {
    const map: Record<string, ProjectAssignment[]> = {};
    assignments.forEach((a) => {
      (map[a.member_id] ??= []).push(a);
    });
    return map;
  }, [assignments]);

  function openCreate() {
    setEditId(null);
    setDraft(EMPTY);
    setModalErr(null);
    setOpen(true);
  }
  function openEdit(m: TeamMember) {
    setEditId(m.member_id);
    setDraft({
      name: m.name,
      email: m.email ?? "",
      phone: m.phone ?? "",
      join_date: m.join_date ?? "",
      birthday: m.birthday ?? "",
      address: m.address ?? "",
      emergency_contact: m.emergency_contact ?? "",
      bank_account: m.bank_account ?? "",
      color: m.color ?? "#5d9bd8",
      bio: m.bio ?? "",
      active: m.active,
    });
    setModalErr(null);
    setOpen(true);
  }

  async function save() {
    setBusy(true);
    setModalErr(null);
    const input = {
      name: draft.name,
      title: null,
      department: null,
      email: orNull(draft.email),
      phone: orNull(draft.phone),
      join_date: orNull(draft.join_date),
      birthday: orNull(draft.birthday),
      address: orNull(draft.address),
      emergency_contact: orNull(draft.emergency_contact),
      bank_account: orNull(draft.bank_account),
      color: orNull(draft.color),
      bio: orNull(draft.bio),
      sort_order: 0,
      active: draft.active,
    };
    try {
      if (editId) await api.updateTeamMember(editId, input as never);
      else await api.createTeamMember(input as never);
      await load();
      setOpen(false);
    } catch (e) {
      setModalErr(e instanceof Error ? e.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    if (!window.confirm("이 팀원을 삭제하시겠습니까?")) return;
    try {
      await api.deleteTeamMember(id);
      await load();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "삭제 실패");
    }
  }

  const headers = ["이름", "이메일", "연락처", "프로젝트 · 역할", "상태", "연결"];

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <ChannelHeader title="팀" subtitle="팀원 정보를 관리합니다." glyph="#" />
      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">
            {error}
          </Banner>
        </div>
      ) : null}

      {/* 인재영입은 별도 페이지로 이동 */}
      <Link
        href="/dashboard/team/recruiting"
        className="mb-4 flex items-center justify-between rounded-[10px] px-4 py-3 transition-colors hover:brightness-[0.98]"
        style={{
          background: "var(--color-card)",
          border: "1px solid var(--color-divider)",
        }}
      >
        <span className="flex items-center gap-2">
          <span aria-hidden>🎯</span>
          <span
            className="text-[var(--text-body)] font-bold"
            style={{ color: "var(--color-text-strong)" }}
          >
            인재영입
          </span>
          <span className="text-[var(--text-body-sm)]" style={labelStyle()}>
            앞으로 영입하거나 킵인터치할 후보 리스트
          </span>
        </span>
        <span
          className="text-[var(--text-body-sm)] font-semibold"
          style={{ color: "var(--color-progress)" }}
        >
          열기 →
        </span>
      </Link>

      <Section
        title="팀원"
        subtitle={`${members.length}명`}
        action={
          <ToolbarButton tone="accent" onClick={openCreate}>
            + 추가
          </ToolbarButton>
        }
      >
        {members.length === 0 ? (
          <p
            className="py-6 text-center text-[var(--text-body-sm)]"
            style={labelStyle()}
          >
            아직 팀원이 없습니다.
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
                {members.map((m) => (
                  <tr
                    key={m.member_id}
                    className="group hover:bg-[var(--color-app-canvas)]"
                    style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
                  >
                    <td
                      className="cursor-pointer px-3 py-2.5 align-middle whitespace-nowrap"
                      onClick={() => openEdit(m)}
                    >
                      <span className="flex items-center gap-2">
                        <Avatar name={m.name} color={m.color || undefined} size="sm" />
                        <span style={{ color: "var(--color-text-strong)", fontWeight: 600 }}>
                          {m.name}
                        </span>
                      </span>
                    </td>
                    <td
                      className="px-3 py-2.5 align-middle"
                      style={{ maxWidth: 280 }}
                    >
                      <EmailChips value={m.email} />
                    </td>
                    <td
                      className="cursor-pointer px-3 py-2.5 align-middle whitespace-nowrap"
                      style={{ color: "var(--color-text-body)" }}
                      onClick={() => openEdit(m)}
                    >
                      {m.phone ?? "—"}
                    </td>
                    <td className="px-3 py-2.5 align-middle">
                      <AssignmentChips
                        scope="member"
                        ownerId={m.member_id}
                        options={projectOptions}
                        assignments={byMember[m.member_id] ?? []}
                        onChanged={reloadAssignments}
                      />
                    </td>
                    <td
                      className="cursor-pointer px-3 py-2.5 align-middle whitespace-nowrap"
                      style={{ color: "var(--color-text-body)" }}
                      onClick={() => openEdit(m)}
                    >
                      {m.active ? "재직" : "비활성"}
                    </td>
                    <td className="px-3 py-2.5 align-middle whitespace-nowrap">
                      <LinkBadge member={m} />
                    </td>
                    <td className="px-2 py-2.5 text-right whitespace-nowrap">
                      <button
                        onClick={() => openEdit(m)}
                        className="mr-2 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        수정
                      </button>
                      <button
                        onClick={() => remove(m.member_id)}
                        className="text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                        style={{ color: "var(--color-fail-fg)" }}
                      >
                        삭제
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Modal
        open={open}
        title={editId ? "팀원 수정" : "팀원 추가"}
        onClose={() => setOpen(false)}
        footer={
          <>
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
              value={String(draft.active)}
              options={[
                { value: "true", label: "재직" },
                { value: "false", label: "비활성" },
              ]}
              onChange={(e) => setDraft({ ...draft, active: e.target.value === "true" })}
            />
          </Field>
          <Field label="이메일 (여러 개는 띄어쓰기 또는 쉼표로 구분)" full>
            <TextInput
              value={draft.email}
              onChange={(e) => setDraft({ ...draft, email: e.target.value })}
            />
          </Field>
          <Field label="연락처">
            <TextInput
              value={draft.phone}
              onChange={(e) => setDraft({ ...draft, phone: e.target.value })}
            />
          </Field>
          <Field label="색상(달력)">
            <TextInput
              value={draft.color}
              placeholder="#5d9bd8"
              onChange={(e) => setDraft({ ...draft, color: e.target.value })}
            />
          </Field>
          <Field label="입사일">
            <TextInput
              type="date"
              value={draft.join_date}
              onChange={(e) => setDraft({ ...draft, join_date: e.target.value })}
            />
          </Field>
          <Field label="생일">
            <TextInput
              type="date"
              value={draft.birthday}
              onChange={(e) => setDraft({ ...draft, birthday: e.target.value })}
            />
          </Field>
          <Field label="주소" full>
            <TextInput
              value={draft.address}
              onChange={(e) => setDraft({ ...draft, address: e.target.value })}
            />
          </Field>
          <Field label="비상 연락처">
            <TextInput
              value={draft.emergency_contact}
              onChange={(e) =>
                setDraft({ ...draft, emergency_contact: e.target.value })
              }
            />
          </Field>
          <Field label="계좌 (은행/계좌번호)">
            <TextInput
              value={draft.bank_account}
              placeholder="예: 카카오뱅크 3333-01-..."
              onChange={(e) => setDraft({ ...draft, bank_account: e.target.value })}
            />
          </Field>
          <Field label="소개" full>
            <TextArea
              value={draft.bio}
              onChange={(e) => setDraft({ ...draft, bio: e.target.value })}
            />
          </Field>
        </div>

        {editId ? (
          <div
            className="mt-4 pt-4"
            style={{ borderTop: "1px solid var(--color-divider-soft)" }}
          >
            <h3
              className="mb-2 text-[var(--text-body-sm)] font-bold"
              style={{ color: "var(--color-text-strong)" }}
            >
              프로젝트 · 역할
            </h3>
            <AssignmentChips
              scope="member"
              ownerId={editId}
              options={projectOptions}
              assignments={byMember[editId] ?? []}
              onChanged={reloadAssignments}
            />
          </div>
        ) : (
          <p className="mt-4 text-[var(--text-meta)]" style={labelStyle()}>
            먼저 저장하면 프로젝트를 배정할 수 있습니다.
          </p>
        )}
      </Modal>
    </div>
  );
}
