"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type DashboardProject,
  type PartnerCompany,
  type PartnerContact,
} from "@/lib/api";
import {
  ChannelHeader,
  Avatar,
  SectionHeader,
  Banner,
  Card,
  ToolbarButton,
  Chip,
} from "@/components/ui";
import {
  Field,
  Modal,
  Section,
  Select,
  TagSelect,
  TextArea,
  TextInput,
  labelStyle,
  type TagColor,
} from "@/components/dashboard/kit";
import { toExternalHref, displayUrl } from "@/lib/external-url";

const ORG_TYPES = [
  "보조출연사",
  "제작사",
  "AI 파운데이션",
  "업계 파트너",
  "인프라/클라우드",
  "API/플랫폼",
  "기타",
];
const ORG_COLOR: Record<string, TagColor> = {
  보조출연사: "blue",
  제작사: "green",
  "AI 파운데이션": "purple",
  "업계 파트너": "orange",
  "인프라/클라우드": "teal",
  "API/플랫폼": "brown",
  기타: "gray",
};

const STATUSES = [
  "시작전",
  "리스트업",
  "조사중",
  "컨택중",
  "미팅조율",
  "협상/제휴진행",
  "협업중",
  "제휴완료",
  "종료",
];
const STATUS_COLOR: Record<string, TagColor> = {
  시작전: "gray",
  리스트업: "gray",
  조사중: "yellow",
  컨택중: "blue",
  미팅조율: "purple",
  "협상/제휴진행": "pink",
  협업중: "green",
  제휴완료: "teal",
  종료: "red",
};

const orgColor = (v: string): TagColor => ORG_COLOR[v] ?? "gray";
const statusColor = (v: string): TagColor => STATUS_COLOR[v] ?? "gray";

function orNull(v: unknown): string | null {
  const s = String(v ?? "").trim();
  return s === "" ? null : s;
}

type Draft = {
  name: string;
  org_type: string;
  address: string;
  representative: string;
  project_ids: string[];
  field_tags: string;
  status: string;
  memo: string;
  next_action: string;
  last_contact: string;
  link: string;
};

const EMPTY_DRAFT: Draft = {
  name: "",
  org_type: "",
  address: "",
  representative: "",
  project_ids: [],
  field_tags: "",
  status: "",
  memo: "",
  next_action: "",
  last_contact: "",
  link: "",
};

function toDraft(p: PartnerCompany): Draft {
  return {
    name: p.name,
    org_type: p.org_type ?? "",
    address: p.address ?? "",
    representative: p.representative ?? "",
    project_ids: [...p.project_ids],
    field_tags: p.field_tags.join(", "),
    status: p.status ?? "",
    memo: p.memo ?? "",
    next_action: p.next_action ?? "",
    last_contact: p.last_contact ?? "",
    link: p.link ?? "",
  };
}

function draftToInput(d: Draft) {
  return {
    name: d.name,
    org_type: orNull(d.org_type),
    address: orNull(d.address),
    representative: orNull(d.representative),
    project_ids: d.project_ids,
    field_tags: d.field_tags
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    status: orNull(d.status),
    memo: orNull(d.memo),
    next_action: orNull(d.next_action),
    last_contact: orNull(d.last_contact),
    link: orNull(d.link),
    sort_order: 0,
    active: true,
  };
}

// Build the full update payload from a row, for single-field inline edits.
function partnerToInput(p: PartnerCompany) {
  return {
    name: p.name,
    org_type: p.org_type,
    address: p.address,
    representative: p.representative,
    project_ids: p.project_ids,
    field_tags: p.field_tags,
    status: p.status,
    memo: p.memo,
    next_action: p.next_action,
    last_contact: p.last_contact,
    link: p.link,
    sort_order: p.sort_order,
    active: p.active,
  };
}

/* --------------------------------- page --------------------------------- */
export default function PartnersPage() {
  const [partners, setPartners] = useState<PartnerCompany[]>([]);
  const [projects, setProjects] = useState<DashboardProject[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [fProject, setFProject] = useState("");
  const [fOrgType, setFOrgType] = useState("");
  const [fStatus, setFStatus] = useState("");
  const [query, setQuery] = useState("");

  const [open, setOpen] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [contacts, setContacts] = useState<PartnerContact[]>([]);
  const [busy, setBusy] = useState(false);
  const [modalErr, setModalErr] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setPartners(await api.listPartners());
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([api.listPartners(), api.listDashboardProjects()])
      .then(([ps, prj]) => {
        if (!alive) return;
        setPartners(ps);
        setProjects(prj);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "불러오기 실패");
      });
    return () => {
      alive = false;
    };
  }, []);

  const orgTypeOptions = useMemo(() => {
    const set = new Set(ORG_TYPES);
    partners.forEach((p) => p.org_type && set.add(p.org_type));
    return [...set];
  }, [partners]);

  const statusOptions = useMemo(() => {
    const set = new Set(STATUSES);
    partners.forEach((p) => p.status && set.add(p.status));
    return [...set];
  }, [partners]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return partners.filter((p) => {
      if (fProject && !p.project_ids.includes(fProject)) return false;
      if (fOrgType && p.org_type !== fOrgType) return false;
      if (fStatus && p.status !== fStatus) return false;
      if (q) {
        const hay = [p.name, p.org_type, p.representative, p.address]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [partners, fProject, fOrgType, fStatus, query]);

  // Inline edit straight from the table row (Notion-style). Optimistic.
  async function patchPartner(
    p: PartnerCompany,
    patch: Partial<PartnerCompany>,
  ) {
    setPartners((rows) =>
      rows.map((r) => (r.partner_id === p.partner_id ? { ...r, ...patch } : r)),
    );
    try {
      await api.updatePartner(p.partner_id, { ...partnerToInput(p), ...patch });
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "저장 실패");
      await reload();
    }
  }

  function openCreate() {
    setEditId(null);
    setDraft(EMPTY_DRAFT);
    setContacts([]);
    setModalErr(null);
    setOpen(true);
  }

  async function openEdit(p: PartnerCompany) {
    setEditId(p.partner_id);
    setDraft(toDraft(p));
    setContacts([]);
    setModalErr(null);
    setOpen(true);
    try {
      setContacts(await api.listPartnerContacts(p.partner_id));
    } catch {
      /* keep empty */
    }
  }

  async function savePartner() {
    setBusy(true);
    setModalErr(null);
    try {
      const input = draftToInput(draft);
      if (editId) {
        await api.updatePartner(editId, input);
      } else {
        const created = await api.createPartner(input);
        setEditId(created.partner_id);
      }
      await reload();
    } catch (e) {
      setModalErr(e instanceof Error ? e.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  }

  async function removePartner(id: string) {
    if (!window.confirm("이 파트너사를 삭제하시겠습니까? 소속 인물도 함께 삭제됩니다."))
      return;
    try {
      await api.deletePartner(id);
      await reload();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "삭제 실패");
    }
  }

  function setField<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((d) => ({ ...d, [key]: value }));
  }

  function toggleProject(id: string) {
    setDraft((d) => ({
      ...d,
      project_ids: d.project_ids.includes(id)
        ? d.project_ids.filter((x) => x !== id)
        : [...d.project_ids, id],
    }));
  }

  const reloadContacts = useCallback(async () => {
    if (editId) setContacts(await api.listPartnerContacts(editId));
    await reload();
  }, [editId, reload]);

  const tableHeaders = ["이름", "프로젝트", "기관형태", "상태", "장소", "인물"];

  return (
    <div className="mx-auto w-full max-w-[1100px] px-3 py-4 sm:px-5">
      <ChannelHeader
        glyph="#"
        title="파트너사"
        subtitle="프로젝트별 파트너사와 소속 인물을 한눈에 관리합니다."
      />
      {error ? (
        <div className="mb-3">
          <Banner tone="fail" title="오류">
            {error}
          </Banner>
        </div>
      ) : null}

      <Section
        title="파트너사"
        subtitle={`${filtered.length}곳`}
        action={
          <ToolbarButton tone="accent" onClick={openCreate}>
            + 추가
          </ToolbarButton>
        }
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Select
            aria-label="프로젝트 필터"
            value={fProject}
            options={[
              { value: "", label: "전체 프로젝트" },
              ...projects.map((p) => ({ value: p.project_id, label: p.name })),
            ]}
            onChange={(e) => setFProject(e.target.value)}
            style={{ minHeight: 44, width: "auto" }}
          />
          <Select
            aria-label="기관 형태 필터"
            value={fOrgType}
            options={[
              { value: "", label: "전체 형태" },
              ...orgTypeOptions.map((v) => ({ value: v, label: v })),
            ]}
            onChange={(e) => setFOrgType(e.target.value)}
            style={{ minHeight: 44, width: "auto" }}
          />
          <Select
            aria-label="상태 필터"
            value={fStatus}
            options={[
              { value: "", label: "전체 상태" },
              ...statusOptions.map((v) => ({ value: v, label: v })),
            ]}
            onChange={(e) => setFStatus(e.target.value)}
            style={{ minHeight: 44, width: "auto" }}
          />
          <TextInput
            aria-label="검색"
            placeholder="이름·대표·주소 검색"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ minHeight: 44, flex: "1 1 160px", minWidth: 140 }}
          />
        </div>

        {filtered.length === 0 ? (
          <p
            className="py-6 text-center text-[var(--text-body-sm)]"
            style={labelStyle()}
          >
            조건에 맞는 파트너사가 없습니다.
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
                  {tableHeaders.map((h) => (
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
                {filtered.map((p) => (
                  <tr
                    key={p.partner_id}
                    className="group hover:bg-[var(--color-app-canvas)]"
                    style={{ borderBottom: "1px solid var(--color-divider-soft)" }}
                  >
                    <td
                      className="cursor-pointer px-3 py-2.5 align-middle font-medium whitespace-nowrap"
                      style={{ color: "var(--color-text-strong)" }}
                      onClick={() => openEdit(p)}
                    >
                      {p.name}
                      {toExternalHref(p.link) ? (
                        <a
                          href={toExternalHref(p.link)!}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          className="ml-2 text-[var(--text-meta)] font-normal underline"
                          style={{ color: "var(--color-text-muted)" }}
                        >
                          {displayUrl(p.link)}
                        </a>
                      ) : null}
                    </td>
                    <td className="px-3 py-2.5 align-middle">
                      <ProjectPicker
                        projects={projects}
                        selected={p.project_ids}
                        onChange={(ids) =>
                          patchPartner(p, { project_ids: ids })
                        }
                      />
                    </td>
                    <td className="px-3 py-2.5 align-middle whitespace-nowrap">
                      <TagSelect
                        variant="inline"
                        value={p.org_type ?? ""}
                        options={ORG_TYPES}
                        colorFor={orgColor}
                        placeholder="—"
                        onChange={(v) =>
                          patchPartner(p, { org_type: v || null })
                        }
                      />
                    </td>
                    <td className="px-3 py-2.5 align-middle whitespace-nowrap">
                      <TagSelect
                        variant="inline"
                        value={p.status ?? ""}
                        options={STATUSES}
                        colorFor={statusColor}
                        placeholder="—"
                        onChange={(v) => patchPartner(p, { status: v || null })}
                      />
                    </td>
                    <td
                      className="px-3 py-2.5 align-middle whitespace-nowrap"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      <InlineText
                        value={p.address ?? ""}
                        placeholder="—"
                        onSave={(v) => patchPartner(p, { address: v || null })}
                      />
                    </td>
                    <td
                      className="cursor-pointer px-3 py-2.5 align-middle whitespace-nowrap"
                      style={{ color: "var(--color-text-body)" }}
                      onClick={() => openEdit(p)}
                    >
                      {p.contact_count}명
                    </td>
                    <td className="px-2 py-2.5 text-right whitespace-nowrap">
                      <button
                        onClick={() => openEdit(p)}
                        className="mr-2 text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                        style={{ color: "var(--color-text-muted)", minHeight: 32 }}
                      >
                        수정
                      </button>
                      <button
                        onClick={() => removePartner(p.partner_id)}
                        className="text-[var(--text-meta)] opacity-0 group-hover:opacity-100"
                        style={{ color: "var(--color-fail-fg)", minHeight: 32 }}
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
        title={editId ? "파트너사 수정" : "파트너사 추가"}
        onClose={() => setOpen(false)}
        footer={
          <>
            <ToolbarButton onClick={() => setOpen(false)} disabled={busy}>
              닫기
            </ToolbarButton>
            <ToolbarButton tone="accent" onClick={savePartner} disabled={busy}>
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
              onChange={(e) => setField("name", e.target.value)}
            />
          </Field>
          <Field label="기관 형태">
            <TagSelect
              value={draft.org_type}
              options={ORG_TYPES}
              colorFor={orgColor}
              onChange={(v) => setField("org_type", v)}
            />
          </Field>
          <Field label="상태">
            <TagSelect
              value={draft.status}
              options={STATUSES}
              colorFor={statusColor}
              onChange={(v) => setField("status", v)}
            />
          </Field>
          <Field label="대표">
            <TextInput
              value={draft.representative}
              onChange={(e) => setField("representative", e.target.value)}
            />
          </Field>
          <Field label="주소" full>
            <TextInput
              value={draft.address}
              onChange={(e) => setField("address", e.target.value)}
            />
          </Field>
          <Field label="프로젝트 (중복 선택)" full>
            <div className="flex flex-wrap gap-2 pt-1">
              {projects.map((p) => {
                const on = draft.project_ids.includes(p.project_id);
                return (
                  <button
                    key={p.project_id}
                    type="button"
                    onClick={() => toggleProject(p.project_id)}
                    className="rounded-full px-3 text-[var(--text-body-sm)]"
                    style={{
                      minHeight: 36,
                      border: "1px solid var(--color-divider)",
                      background: on
                        ? "var(--color-text-strong)"
                        : "var(--color-card)",
                      color: on ? "var(--color-card)" : "var(--color-text-body)",
                      fontWeight: on ? 600 : 400,
                    }}
                  >
                    {p.name}
                  </button>
                );
              })}
            </div>
          </Field>
          <Field label="분야 (쉼표로 구분)" full>
            <TextInput
              placeholder="예: Foundation Model, API/Platform"
              value={draft.field_tags}
              onChange={(e) => setField("field_tags", e.target.value)}
            />
          </Field>
          <Field label="최근 컨택">
            <TextInput
              type="date"
              value={draft.last_contact}
              onChange={(e) => setField("last_contact", e.target.value)}
            />
          </Field>
          <Field label="링크">
            <TextInput
              value={draft.link}
              onChange={(e) => setField("link", e.target.value)}
            />
          </Field>
          <Field label="다음 액션" full>
            <TextInput
              value={draft.next_action}
              onChange={(e) => setField("next_action", e.target.value)}
            />
          </Field>
          <Field label="메모" full>
            <TextArea
              value={draft.memo}
              onChange={(e) => setField("memo", e.target.value)}
            />
          </Field>
        </div>

        {editId ? (
          <ContactsEditor
            partnerId={editId}
            contacts={contacts}
            onChanged={reloadContacts}
          />
        ) : (
          <p className="mt-4 text-[var(--text-meta)]" style={labelStyle()}>
            먼저 저장하면 소속 인물을 추가할 수 있습니다.
          </p>
        )}
      </Modal>
    </div>
  );
}

/* ---------------------- inline project multi-select ---------------------- */
function ProjectPicker({
  projects,
  selected,
  onChange,
}: {
  projects: DashboardProject[];
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", onDoc);
    return () => document.removeEventListener("pointerdown", onDoc);
  }, [open]);

  const byId = (id: string) => projects.find((p) => p.project_id === id)?.name ?? id;
  function toggle(id: string) {
    onChange(
      selected.includes(id)
        ? selected.filter((x) => x !== id)
        : [...selected, id],
    );
  }

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
        className="flex min-h-[28px] items-center gap-1 rounded-[4px] px-1 -mx-1 hover:bg-[var(--color-card)]"
      >
        {selected.length === 0 ? (
          <span style={{ color: "var(--color-text-muted)" }}>—</span>
        ) : (
          <span className="flex flex-wrap gap-1">
            {selected.map((id) => (
              <Chip key={id}>{byId(id)}</Chip>
            ))}
          </span>
        )}
      </button>
      {open ? (
        <div
          className="absolute left-0 z-50 mt-1 w-[200px] overflow-hidden rounded-[10px] p-1"
          style={{
            background: "var(--color-modal)",
            border: "1px solid var(--color-divider)",
            boxShadow: "var(--shadow-modal)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {projects.map((p) => {
            const on = selected.includes(p.project_id);
            return (
              <button
                key={p.project_id}
                type="button"
                onClick={() => toggle(p.project_id)}
                className="flex w-full items-center gap-2 rounded-[6px] px-2 py-1.5 text-left text-[var(--text-body-sm)] hover:bg-[var(--color-app-canvas)]"
                style={{ minHeight: 32 }}
              >
                <span
                  className="flex h-4 w-4 items-center justify-center rounded-[3px] text-[10px]"
                  style={{
                    border: "1px solid var(--color-divider)",
                    background: on ? "var(--color-text-strong)" : "transparent",
                    color: "var(--color-card)",
                  }}
                >
                  {on ? "✓" : ""}
                </span>
                {p.name}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------- inline text cell edit ------------------------- */
function InlineText({
  value,
  placeholder = "—",
  onSave,
}: {
  value: string;
  placeholder?: string;
  onSave: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  function commit() {
    setEditing(false);
    if (draft.trim() !== value.trim()) onSave(draft.trim());
  }

  if (editing) {
    return (
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") {
            setDraft(value);
            setEditing(false);
          }
        }}
        onClick={(e) => e.stopPropagation()}
        className="w-full min-w-[120px] rounded-[4px] px-1.5 py-1 text-[var(--text-body-sm)] outline-none"
        style={{
          border: "1px solid var(--color-text-strong)",
          background: "var(--color-card)",
          color: "var(--color-text-strong)",
        }}
      />
    );
  }
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        setDraft(value);
        setEditing(true);
      }}
      className="block w-full rounded-[4px] px-1 -mx-1 py-0.5 text-left hover:bg-[var(--color-card)]"
      style={{ minHeight: 26, color: value ? "inherit" : "var(--color-text-muted)" }}
    >
      {value || placeholder}
    </button>
  );
}

/* ------------------------- nested contacts editor ------------------------- */

type ContactDraft = {
  name: string;
  role: string;
  phone: string;
  email: string;
  link: string;
  memo: string;
  is_primary: boolean;
};

const EMPTY_CONTACT: ContactDraft = {
  name: "",
  role: "",
  phone: "",
  email: "",
  link: "",
  memo: "",
  is_primary: false,
};

function ContactsEditor({
  partnerId,
  contacts,
  onChanged,
}: {
  partnerId: string;
  contacts: PartnerContact[];
  onChanged: () => Promise<void>;
}) {
  const [cid, setCid] = useState<string | null>(null);
  const [draft, setDraft] = useState<ContactDraft>(EMPTY_CONTACT);
  const [busy, setBusy] = useState(false);

  function startNew() {
    setCid(null);
    setDraft(EMPTY_CONTACT);
  }
  function startEdit(c: PartnerContact) {
    setCid(c.contact_id);
    setDraft({
      name: c.name,
      role: c.role ?? "",
      phone: c.phone ?? "",
      email: c.email ?? "",
      link: c.link ?? "",
      memo: c.memo ?? "",
      is_primary: c.is_primary,
    });
  }

  async function save() {
    if (!draft.name.trim()) return;
    setBusy(true);
    const input = {
      name: draft.name,
      role: orNull(draft.role),
      phone: orNull(draft.phone),
      email: orNull(draft.email),
      channels: [],
      link: orNull(draft.link),
      memo: orNull(draft.memo),
      is_primary: draft.is_primary,
      sort_order: 0,
    };
    try {
      if (cid) await api.updatePartnerContact(cid, input);
      else await api.createPartnerContact(partnerId, input);
      startNew();
      await onChanged();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "저장 실패");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    if (!window.confirm("이 인물을 삭제하시겠습니까?")) return;
    try {
      await api.deletePartnerContact(id);
      if (cid === id) startNew();
      await onChanged();
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "삭제 실패");
    }
  }

  return (
    <div
      className="mt-4 pt-4"
      style={{ borderTop: "1px solid var(--color-divider-soft)" }}
    >
      <SectionHeader label="소속 인물" count={contacts.length} />

      {contacts.length > 0 ? (
        <ul className="mb-3 flex flex-col gap-1">
          {contacts.map((c) => (
            <li
              key={c.contact_id}
              className="flex items-center justify-between gap-2 rounded-[6px] px-2 py-1.5"
              style={{ background: "var(--color-app-canvas)" }}
            >
              <span className="flex min-w-0 items-center gap-2">
                <Avatar name={c.name} size="sm" />
                <span className="min-w-0 text-[var(--text-body-sm)]">
                <span style={{ color: "var(--color-text-strong)", fontWeight: 600 }}>
                  {c.name}
                </span>
                {c.role ? (
                  <span style={{ color: "var(--color-text-muted)" }}> · {c.role}</span>
                ) : null}
                {c.is_primary ? (
                  <span style={{ color: "var(--color-text-muted)" }}> · 대표</span>
                ) : null}
                {c.phone ? (
                  <span style={{ color: "var(--color-text-muted)" }}> · {c.phone}</span>
                ) : null}
                {toExternalHref(c.link) ? (
                  <span style={{ color: "var(--color-text-muted)" }}>
                    {" · "}
                    <a
                      href={toExternalHref(c.link)!}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      {displayUrl(c.link)}
                    </a>
                  </span>
                ) : null}
                </span>
              </span>
              <span className="whitespace-nowrap">
                <button
                  onClick={() => startEdit(c)}
                  className="mr-2 text-[var(--text-meta)]"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  수정
                </button>
                <button
                  onClick={() => remove(c.contact_id)}
                  className="text-[var(--text-meta)]"
                  style={{ color: "var(--color-fail-fg)" }}
                >
                  삭제
                </button>
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      <Card className="p-3">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <Field label="이름">
            <TextInput
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </Field>
          <Field label="역할/직책">
            <TextInput
              placeholder="대표 / 반장 / 담당자 …"
              value={draft.role}
              onChange={(e) => setDraft({ ...draft, role: e.target.value })}
            />
          </Field>
          <Field label="연락처">
            <TextInput
              value={draft.phone}
              onChange={(e) => setDraft({ ...draft, phone: e.target.value })}
            />
          </Field>
          <Field label="이메일">
            <TextInput
              value={draft.email}
              onChange={(e) => setDraft({ ...draft, email: e.target.value })}
            />
          </Field>
          <Field label="링크" full>
            <TextInput
              value={draft.link}
              onChange={(e) => setDraft({ ...draft, link: e.target.value })}
            />
          </Field>
        </div>
        <label className="mt-2 flex items-center gap-2 text-[var(--text-body-sm)]">
          <input
            type="checkbox"
            className="orthus-check"
            checked={draft.is_primary}
            onChange={(e) => setDraft({ ...draft, is_primary: e.target.checked })}
          />
          대표 담당자로 표시
        </label>
        <div className="mt-2 flex items-center gap-2">
          <ToolbarButton tone="primary" onClick={save} disabled={busy}>
            {cid ? "인물 수정" : "인물 추가"}
          </ToolbarButton>
          {cid ? (
            <ToolbarButton onClick={startNew} disabled={busy}>
              취소
            </ToolbarButton>
          ) : null}
        </div>
      </Card>
    </div>
  );
}
